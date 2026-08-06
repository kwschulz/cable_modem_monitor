"""Post-analysis unread-resource reporting.

Lists the HAR's JSON endpoints that the generated config will not read,
each with its key skeleton and value types. It is the LLM's view of what
nothing in the pipeline looked at.

**Why this exists:** gap categories are endpoint-level and cover auth and
actions only. A data endpoint the generator never maps produces no gap
and no warning — intake writes a ``parser.yaml`` that omits it, every
gate stays green, and the page is invisible. Issue #185's HAR carried
``/rest/v1/cablemodem/serviceflows`` at 200 with four registered Tier-2
fields on it, and nothing asked what had never been looked at.

**Keys and types only, never values.** This artifact flows into an LLM
context and often into a GitHub issue. Keys are what make the judgment
possible: an LLM recognizes ``maxTrafficRate`` as a provisioned rate,
where ``856000000`` on its own tells it nothing. Values are where MAC
addresses, serial numbers and boot filenames live — the same #185 HAR
answers ``/rest/v1/system/gateway/provisioning`` with a ``macAddress``
and fills its event log with ``CM-MAC=`` strings.

**Not a gate.** Every HAR has unread endpoints, so a gate here would
fail every intake. This rides alongside ``warnings``: always present,
informational, never failing. Classifying what an endpoint contains and
deciding whether it is worth mapping is the reading LLM's job, not this
module's.

See ONBOARDING_SPEC.md "Post-Analysis: Unread Resources".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from ..validation.har_utils import content_type_of, is_static_resource, path_from_url
from .actions.types import ActionsDetail
from .auth.types import AuthDetail

# Auth fields that name an endpoint the generated config fetches. A
# resource reached during the login flow is read, not unread.
_AUTH_ENDPOINT_FIELDS: tuple[str, ...] = ("login_endpoint", "login_page", "action")

# Content types that never carry a parser resource, even when the body
# happens to parse as JSON.
_EXCLUDED_CONTENT_TYPES: tuple[str, ...] = ("css", "javascript", "font", "image")


@dataclass
class UnreadResource:
    """A JSON endpoint present in the HAR that the generated config does not read."""

    path: str
    status: int
    content_type: str
    shape: Any

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for MCP tool output."""
        return {
            "path": self.path,
            "status": self.status,
            "content_type": self.content_type,
            "shape": self.shape,
        }


def detect_unread_resources(
    entries: list[dict[str, Any]],
    sections: dict[str, Any] | None,
    auth: AuthDetail,
    actions: ActionsDetail,
    transport: str,
) -> list[UnreadResource]:
    """Report the HAR's 2xx JSON endpoints that no part of the config consumes."""
    mapped = _mapped_endpoints(sections, auth, actions)

    unread: list[UnreadResource] = []
    for path, (response, body) in sorted(_json_candidates(entries).items()):
        # HNAP puts every call, data and action alike, behind one endpoint.
        # Reporting it as unread would be wrong on every HNAP modem.
        if transport == "hnap" and "/HNAP1/" in path:
            continue
        if any(_endpoint_matches(path, endpoint) for endpoint in mapped):
            continue
        unread.append(
            UnreadResource(
                path=path,
                status=response.get("status", 0),
                content_type=content_type_of(response),
                shape=_shape_of(body),
            )
        )
    return unread


# -----------------------------------------------------------------------
# Candidate selection
# -----------------------------------------------------------------------


def _json_candidates(
    entries: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], Any]]:
    """Map each path answering 2xx with a JSON object or array to its richest response."""
    candidates: dict[str, tuple[dict[str, Any], Any]] = {}
    sizes: dict[str, int] = {}

    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        response = entry.get("response", {})
        if not url or is_static_resource(url):
            continue
        if not 200 <= response.get("status", 0) < 300:
            continue
        if any(excluded in content_type_of(response) for excluded in _EXCLUDED_CONTENT_TYPES):
            continue

        text = response.get("content", {}).get("text", "") or ""
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(body, (dict, list)):
            continue

        # Keyed by path, not path+query: cache-busting nonces (the
        # TG3442DE's per-request `_n`) would otherwise make every request
        # its own identity and match no configured resource.
        path = _normalize_endpoint(path_from_url(url))
        if path not in candidates or len(text) > sizes[path]:
            candidates[path] = (response, body)
            sizes[path] = len(text)

    return candidates


# -----------------------------------------------------------------------
# What the config reads
# -----------------------------------------------------------------------


def _mapped_endpoints(
    sections: dict[str, Any] | None,
    auth: AuthDetail,
    actions: ActionsDetail,
) -> set[str]:
    """Collect every endpoint the generated config will fetch."""
    mapped = {_normalize_endpoint(resource) for resource in _collect_resources(sections)}

    for name in _AUTH_ENDPOINT_FIELDS:
        value = auth.fields.get(name)
        if isinstance(value, str) and value:
            mapped.add(_normalize_endpoint(value))

    for action in (actions.logout, actions.restart):
        if action is None:
            continue
        for value in (action.endpoint, action.pre_fetch_url):
            if value:
                mapped.add(_normalize_endpoint(value))

    return mapped


def _collect_resources(node: Any) -> Iterator[str]:
    """Yield every ``resource`` value anywhere in the analysis sections tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "resource" and isinstance(value, str) and value:
                yield value
            else:
                yield from _collect_resources(value)
    elif isinstance(node, list):
        for item in node:
            yield from _collect_resources(item)


def _normalize_endpoint(raw: str) -> str:
    """Reduce a URL, resource, or action endpoint to a leading-slash path."""
    path = path_from_url(raw)
    return path if path.startswith("/") else "/" + path


def _endpoint_matches(candidate: str, mapped: str) -> bool:
    """Match a captured path against a configured endpoint, tolerating placeholders."""
    if candidate == mapped:
        return True
    # Endpoints like /rest/v1/user/{auth:user_id}/token/{auth:token} are
    # resolved at runtime, so they never equal the captured path.
    if "{" not in mapped:
        return False
    captured_parts = candidate.strip("/").split("/")
    mapped_parts = mapped.strip("/").split("/")
    if len(captured_parts) != len(mapped_parts):
        return False
    return all(
        (part.startswith("{") and part.endswith("}")) or part == captured
        for captured, part in zip(captured_parts, mapped_parts, strict=True)
    )


# -----------------------------------------------------------------------
# Key skeleton
# -----------------------------------------------------------------------


def _shape_of(value: Any) -> Any:
    """Reduce a JSON body to its key skeleton with value types, discarding all values."""
    if isinstance(value, dict):
        return {key: _shape_of(item) for key, item in value.items()}
    if isinstance(value, list):
        merged: Any = None
        for item in value:
            item_shape = _shape_of(item)
            merged = item_shape if merged is None else _merge_shapes(merged, item_shape)
        # One merged element stands for the whole array: heterogeneous
        # arrays (QAM beside OFDM) must show every key either variant has.
        return [] if merged is None else [merged]
    return _type_name(value)


def _merge_shapes(left: Any, right: Any) -> Any:
    """Union two skeletons of the same array, keeping the more informative side."""
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = _merge_shapes(merged[key], value) if key in merged else value
        return merged
    if isinstance(left, list) and isinstance(right, list):
        if not left:
            return right
        if not right:
            return left
        return [_merge_shapes(left[0], right[0])]
    if left == right:
        return left
    # A structure carries more shape than a bare type name.
    if isinstance(left, (dict, list)):
        return left
    if isinstance(right, (dict, list)):
        return right
    return "|".join(sorted(set(str(left).split("|")) | set(str(right).split("|"))))


def _type_name(value: Any) -> str:
    """Name a JSON scalar's type; bool is checked before int, which it subclasses."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__
