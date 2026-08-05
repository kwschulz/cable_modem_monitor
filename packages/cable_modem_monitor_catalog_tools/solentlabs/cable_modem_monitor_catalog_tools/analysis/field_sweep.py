"""Field-granularity sweep of a committed catalog entry against its own HAR.

Reads a capture for keys that resolve to a registry field, and reports
the ones the committed entry never extracts.

**How this differs from ``unread_resources``.** That module answers
"which endpoints did nothing read?" — endpoint granularity, no registry
knowledge, run at intake on a HAR that has no committed config yet. It
is blind to an endpoint that *is* read but read incompletely: the
SB8200's ``/cmconnectionstatus.html`` is a parser resource, so it is
never unread, and its "Boot State" row goes unmapped in silence. This
module answers "which registry fields did the capture carry that the
entry does not populate?" — field granularity, registry-aware, run over
the already-committed catalog.

**What counts as extracted** is the committed ``parser.yaml`` plus the
committed golden file. A field can reach the output without a
``field:`` line — ``channel_type: {fixed: qam}``, ``fixed_fields``, the
XML ``lock_status: {all_of: [...]}`` derivation, an ``aggregate:``
total, or a ``parser.py`` post-processor. Reading the golden alongside
the config covers the last of those, which no static read of the YAML
can see.

**Only fields with a canonical home are surfaced.** The vocabulary is
the shipped alias maps — ``field_registry.json`` for channel fields,
``mapping.system_info`` for system_info, ``mapping.service_flows`` for
the provisioned maxima. Serial numbers, MAC addresses, boot filenames,
event logs and MTA lines have no registry field; they are new-schema
questions gated by ARCHITECTURE_DECISIONS § Core Schema Model, and
several are PII-adjacent. This module cannot name them and does not try.

**Keys only, never values** — same rule as ``unread_resources``, for
the same reason: the report flows into an LLM context and often into a
GitHub issue, and values are where the identifiers live.

**Union across directions.** A channel key carries no reliable
direction, so a channel field counts as extracted when *either*
``downstream`` or ``upstream`` populates it. A field read downstream
but missed upstream does not surface. Widening that would need a
direction the capture does not carry.

See INTAKE_PIPELINE.md § Catalog Field Sweep.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as DefusedET

from ..validation.har_utils import content_type_of, decode_body, is_static_resource
from .format.http import analyze_page
from .format.table_analysis import is_channel_table
from .format.types import PageAnalysis
from .mapping.field_resolution import HEADER_FIELD_MAP, JSON_KEY_MAP
from .mapping.service_flows import detect_service_flow_aggregates
from .mapping.system_info import _ID_FIELD_MAP, _JSON_SYSINFO_MAP, _LABEL_FIELD_MAP

Section = Literal["channel", "system_info"]

# Response content types that can carry a parseable field name. Mirrors
# format.http's data-page filter.
_DATA_CONTENT_TYPES: tuple[str, ...] = ("text/html", "application/json", "application/xml", "text/xml")

# parser.yaml keys whose own name is the field they populate, rather
# than carrying a nested ``field:`` line. Enumerated from the parser
# config models (ChannelTypeConfig on every section, LockStatusAllOf on
# xml). A section-level block here means the field is derived, not read
# from a column.
_SELF_NAMING_BLOCKS: tuple[str, ...] = ("channel_type", "lock_status")

# Subtrees that select or constrain rows without populating anything.
# ``filter: {channel_type: qam}`` names a field it does not write.
_NON_POPULATING_KEYS: frozenset[str] = frozenset({"filter"})


@dataclass(frozen=True)
class FieldGap:
    """A registry field the capture carries and the committed entry does not extract."""

    section: Section
    field: str
    capture_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for report output."""
        return {
            "section": self.section,
            "field": self.field,
            "capture_keys": list(self.capture_keys),
        }


def sweep_entry(
    parser_config: dict[str, Any],
    golden: dict[str, Any] | None,
    entries: list[dict[str, Any]],
) -> list[FieldGap]:
    """Report registry fields present in the capture that the entry never populates."""
    channel_extracted, system_extracted = extracted_fields(parser_config, golden)
    channel_seen, system_seen = captured_fields(entries)

    gaps = [
        FieldGap("channel", field, tuple(sorted(keys)))
        for field, keys in sorted(channel_seen.items())
        if field not in channel_extracted
    ]
    gaps += [
        FieldGap("system_info", field, tuple(sorted(keys)))
        for field, keys in sorted(system_seen.items())
        if field not in system_extracted
    ]
    return gaps


# -----------------------------------------------------------------------
# What the committed entry extracts
# -----------------------------------------------------------------------


def extracted_fields(
    parser_config: dict[str, Any],
    golden: dict[str, Any] | None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return (channel, system_info) field names the committed entry populates."""
    channel = _declared(parser_config.get("downstream")) | _declared(parser_config.get("upstream"))
    system = _declared(parser_config.get("system_info"))
    # Aggregate and computed blocks name their outputs as mapping keys.
    for block in ("aggregate", "computed"):
        node = parser_config.get(block)
        if isinstance(node, dict):
            system |= set(node)

    if golden:
        for direction in ("downstream", "upstream"):
            for record in golden.get(direction) or []:
                if isinstance(record, dict):
                    channel |= set(record)
        info = golden.get("system_info")
        if isinstance(info, dict):
            system |= set(info)

    return frozenset(channel), frozenset(system)


def _declared(node: Any) -> set[str]:
    """Collect every field name a parser.yaml subtree populates."""
    found: set[str] = set()
    if isinstance(node, list):
        for item in node:
            found |= _declared(item)
        return found
    if not isinstance(node, dict):
        return found

    for key, value in node.items():
        if key in _NON_POPULATING_KEYS:
            continue
        if key == "field" and isinstance(value, str):
            found.add(value)
        elif key == "fixed_fields" and isinstance(value, dict):
            found |= set(value)
        elif key in _SELF_NAMING_BLOCKS and value:
            # The block's own name is the field. Its body names the
            # *source* (``channel_type: {field: modulation}``), which is
            # read, not written — so do not recurse into it.
            found.add(key)
        else:
            found |= _declared(value)

    return found


# -----------------------------------------------------------------------
# What the capture carries
# -----------------------------------------------------------------------


def captured_fields(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Map each registry field the capture carries to the wire keys that named it."""
    channel_keys: set[str] = set()
    system_keys: set[str] = set()
    aggregate_fields: set[str] = set()

    for entry in _data_entries(entries):
        _scan_entry(entry, channel_keys, system_keys, aggregate_fields)

    channel = _resolve(channel_keys, (JSON_KEY_MAP, HEADER_FIELD_MAP))
    system = _resolve(system_keys, (_JSON_SYSINFO_MAP, _LABEL_FIELD_MAP, _ID_FIELD_MAP))
    for field in aggregate_fields:
        # Service flow aggregates are recognized by shape, not by a
        # single key, so they carry the array's own name as evidence.
        system.setdefault(field, set()).add("serviceFlows[]")
    return channel, system


def _data_entries(entries: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield every 200 response that can carry a field name.

    Deliberately not deduplicated by path, unlike ``identify_data_pages``:
    HNAP and CBN answer every call from one endpoint, so one response
    per path would drop most of the capture.
    """
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        response = entry.get("response", {})
        if not url or is_static_resource(url):
            continue
        if response.get("status") != 200:
            continue
        if not any(known in content_type_of(response) for known in _DATA_CONTENT_TYPES):
            continue
        if decode_body(response):
            yield entry


def _scan_entry(
    entry: dict[str, Any],
    channel_keys: set[str],
    system_keys: set[str],
    aggregate_fields: set[str],
) -> None:
    """Collect candidate key names from one response."""
    page = analyze_page(entry)

    if page.json_data is not None:
        _walk_json(page.json_data, False, channel_keys, system_keys)
        aggregate_fields.update(a.field for a in detect_service_flow_aggregates(page.json_data))
        return

    # analyze_page classifies HTML only; XML is decoded here.
    if "xml" in page.content_type:
        _walk_xml(decode_body(entry.get("response", {})), channel_keys, system_keys)
        return

    _walk_page(page, channel_keys, system_keys)


def _walk_json(node: Any, in_array: bool, channel_keys: set[str], system_keys: set[str]) -> None:
    """Split JSON keys by shape: inside an array of objects is channel context."""
    if isinstance(node, list):
        for item in node:
            _walk_json(item, True, channel_keys, system_keys)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if isinstance(value, (dict, list)):
            _walk_json(value, in_array, channel_keys, system_keys)
        elif in_array:
            channel_keys.add(key)
        else:
            system_keys.add(key)


def _walk_xml(body: str, channel_keys: set[str], system_keys: set[str]) -> None:
    """Split XML tags and attributes by shape: repeated siblings are channel context."""
    try:
        root = DefusedET.fromstring(body)
    except ParseError:
        return

    def descend(element: Element, in_array: bool) -> None:
        children = list(element)
        repeated = {tag for tag in (c.tag for c in children) if sum(1 for c in children if c.tag == tag) > 1}
        for child in children:
            # Everything below a repeated element stays in channel
            # context — the per-channel field names are its children,
            # and each of those is unique within its own parent.
            child_in_array = in_array or child.tag in repeated
            target = channel_keys if child_in_array else system_keys
            if not list(child):
                target.add(child.tag)
            target.update(child.attrib)
            descend(child, child_in_array)

    descend(root, False)


def _walk_page(page: PageAnalysis, channel_keys: set[str], system_keys: set[str]) -> None:
    """Collect channel-table headers and labels, and page-level label-value pairs."""
    for variable in page.js_json_variables:
        for item in variable.data:
            _walk_json(item, True, channel_keys, system_keys)

    for table in page.tables:
        if not is_channel_table(table):
            continue
        # Transposed tables carry the field name in the first column.
        labels = set(table.headers) | {row[0] for row in table.rows if row}
        # One recognized label is not a channel table. ``is_channel_table``
        # accepts on a single match, and "Status" alone matches the
        # lock_status vocabulary — which is how a DOCSIS boot-progress
        # table ("Task" / "Status") passes it. A real channel table names
        # several fields; requiring two distinct ones costs nothing,
        # because a table offering only one field offers almost nothing.
        if len(_resolve(labels, (JSON_KEY_MAP, HEADER_FIELD_MAP))) < 2:
            continue
        channel_keys.update(labels)

    for pair in page.label_pairs:
        system_keys.add(pair.label)
        if pair.element_id:
            system_keys.add(pair.element_id)


def _resolve(
    keys: set[str],
    maps: tuple[dict[str, tuple[str, int]], ...],
) -> dict[str, set[str]]:
    """Group wire keys by the registry field their alias maps resolve to."""
    resolved: dict[str, set[str]] = {}
    for key in keys:
        normalized = key.strip().lower().rstrip(":")
        for alias_map in maps:
            if normalized in alias_map:
                resolved.setdefault(alias_map[normalized][0], set()).add(key)
    return resolved
