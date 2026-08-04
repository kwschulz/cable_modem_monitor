"""Phase 6 - DOCSIS service flow detection.

A service flow resource lists one entry per flow, each carrying the
flow's direction and the provisioned maxima for that direction. Core
reduces those entries with system_info ``child_aggregates``; this
module recognizes the shape in a JSON body and emits the aggregate
definitions that read it.

Per docs/ONBOARDING_SPEC.md Phase 6 and SYSTEM_INFO_SPEC.md
child_aggregates.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from .types import ChildAggregateDetail

_PATTERNS_PATH = Path(__file__).parent / "service_flow_patterns.json"

# Canonical DOCS-IF3-MIB direction words -> field suffix. This one stays
# in code: it defines registered output field names, not wire
# recognition, so it is a contract rather than an extension point.
_DIRECTION_SUFFIX: dict[str, str] = {"downstream": "down", "upstream": "up"}

# Order aggregates are emitted in. Kept here rather than taken from the
# pattern file's key order, which JSON does not guarantee.
_STEM_ORDER: tuple[str, ...] = ("provisioned_speed", "provisioned_burst")


@functools.lru_cache(maxsize=1)
def _load_patterns() -> dict[str, Any]:
    """Load and cache the wire-facing service flow vocabulary."""
    data: dict[str, Any] = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    return data


def _direction_keys() -> frozenset[str]:
    """Item keys that name a flow's direction."""
    return frozenset(_load_patterns()["direction_keys"])


def _metric_keys() -> dict[str, str]:
    """Provisioned-maximum key -> canonical field stem."""
    keys: dict[str, str] = _load_patterns()["metric_keys"]
    return keys


def _direction_aliases() -> dict[str, str]:
    """Wire spellings that are not the canonical direction words."""
    aliases: dict[str, str] = _load_patterns()["direction_aliases"]
    return aliases


def detect_service_flow_aggregates(data: dict[str, Any]) -> list[ChildAggregateDetail]:
    """Detect service flow child_aggregates in a JSON response body.

    Returns the aggregates for the first array whose items carry both a
    recognized direction and at least one provisioned-maximum key, or an
    empty list.
    """
    for array_path, items in _find_dict_arrays(data):
        item_path, unwrapped = _unwrap_items(items)
        aggregates = _build_aggregates(unwrapped, array_path, item_path)
        if aggregates:
            return aggregates
    return []


def _find_dict_arrays(
    data: dict[str, Any],
    prefix: str = "",
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Collect every dot-path -> list-of-dicts pair in the body."""
    found: list[tuple[str, list[dict[str, Any]]]] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
            found.append((path, value))
        elif isinstance(value, dict):
            found.extend(_find_dict_arrays(value, path))
    return found


def _unwrap_items(
    items: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Strip a uniform single-key wrapper object, returning its key as item_path.

    Some firmware nests each entry as ``{"serviceFlow": {...}}``; Core's
    ``item_path`` unwraps that. Only strip when every item shares one
    wrapper key, otherwise the array is already flat.
    """
    wrapper_keys = set()
    for item in items:
        if len(item) != 1:
            return "", items
        key, value = next(iter(item.items()))
        if not isinstance(value, dict):
            return "", items
        wrapper_keys.add(key)

    if len(wrapper_keys) != 1:
        return "", items

    wrapper = wrapper_keys.pop()
    return wrapper, [item[wrapper] for item in items]


def _build_aggregates(
    items: list[dict[str, Any]],
    array_path: str,
    item_path: str,
) -> list[ChildAggregateDetail]:
    """Emit one aggregate per (provisioned metric, observed direction)."""
    direction_key = _find_direction_key(items)
    if not direction_key:
        return []

    directions, direction_map = _resolve_directions(items, direction_key)
    if not directions:
        return []

    aggregates: list[ChildAggregateDetail] = []
    for metric_key, stem in _observed_metrics(items):
        for canonical in directions:
            aggregates.append(
                ChildAggregateDetail(
                    array_path=array_path,
                    item_path=item_path,
                    # Filter values compare as text; Core rejects a
                    # numeric rule at config load.
                    filter={direction_key: canonical},
                    map=dict(direction_map),
                    max=metric_key,
                    field=f"{stem}_{_DIRECTION_SUFFIX[canonical]}",
                    type=_metric_type(items, metric_key),
                )
            )
    return aggregates


def _find_direction_key(items: list[dict[str, Any]]) -> str:
    """Return the observed casing of the direction key, if present."""
    for item in items:
        for key in item:
            if key.strip().lower() in _direction_keys():
                return key
    return ""


def _resolve_directions(
    items: list[dict[str, Any]],
    direction_key: str,
) -> tuple[list[str], dict[str, str]]:
    """Resolve observed direction values to canonical words plus a normalizing map."""
    canonical_seen: list[str] = []
    mapping: dict[str, str] = {}
    aliases = _direction_aliases()

    for item in items:
        raw = item.get(direction_key)
        if not isinstance(raw, str | int):
            continue
        text = str(raw).strip()
        lowered = text.lower()

        if lowered in _DIRECTION_SUFFIX:
            canonical = lowered
        elif lowered in aliases:
            canonical = aliases[lowered]
        else:
            # An unrecognized spelling is skipped, never guessed at: a
            # wrong direction would silently attribute a flow to the
            # opposite direction's field.
            continue

        # Core compares filter rules byte for byte, so every spelling that
        # is not already the canonical word has to normalize in the map.
        # Casing counts: "Downstream" against a downstream rule matches
        # nothing and yields no value, without erroring.
        if text != canonical:
            mapping[text] = canonical

        if canonical not in canonical_seen:
            canonical_seen.append(canonical)

    # Stable emission order regardless of the order flows appear on the wire.
    ordered = [d for d in _DIRECTION_SUFFIX if d in canonical_seen]
    return ordered, mapping


def _observed_metrics(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return (observed key casing, canonical field stem) for each provisioned metric.

    One entry per stem: two accepted spellings of the same metric would
    otherwise both write the same field.
    """
    vocabulary = _metric_keys()

    # A stem the pattern file adds without updating _STEM_ORDER still
    # emits, sorted after the known ones, rather than being dropped.
    known = [s for s in _STEM_ORDER if s in set(vocabulary.values())]
    extra = sorted(set(vocabulary.values()) - set(_STEM_ORDER))

    metrics: list[tuple[str, str]] = []
    for stem in known + extra:
        spellings = sorted(k for k, s in vocabulary.items() if s == stem)
        for normalized in spellings:
            observed = _find_metric_key(items, normalized)
            if observed:
                metrics.append((observed, stem))
                break
    return metrics


def _find_metric_key(items: list[dict[str, Any]], normalized: str) -> str:
    """Return the observed casing of a provisioned-metric key, if present."""
    for item in items:
        for key in item:
            if key.strip().lower() == normalized:
                return key
    return ""


def _metric_type(items: list[dict[str, Any]], metric_key: str) -> str:
    """Type a provisioned metric from its observed values."""
    for item in items:
        value = item.get(metric_key)
        if isinstance(value, bool):
            continue
        if isinstance(value, float):
            return "float"
    return "integer"
