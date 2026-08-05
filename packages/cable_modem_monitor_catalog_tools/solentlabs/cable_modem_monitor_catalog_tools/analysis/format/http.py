"""Phase 5 - HTTP format detection.

Identifies data pages in the HAR and classifies their format:
table, table_transposed, javascript, json, or html_fields.

Per docs/ONBOARDING_SPEC.md Phase 5 (HTTP transport).
"""

from __future__ import annotations

import json as json_mod
import re
from typing import Any

from ...validation.har_utils import (
    content_type_of,
    decode_body,
    has_content,
    is_static_resource,
    path_from_url,
)
from .html_parsing import detect_label_pairs, detect_tables
from .table_analysis import is_channel_table, is_transposed
from .types import (
    DetectedJsFunction,
    DetectedJsJsonVariable,
    PageAnalysis,
)

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------

_DATA_CONTENT_TYPES: frozenset[str] = frozenset({"text/html", "application/json", "application/xml", "text/xml"})

# JS function name pattern for channel data (Init*TagValue)
_JS_FUNCTION_PATTERN = re.compile(
    r"function\s+(Init\w*TagValue)\s*\([^)]*\)\s*\{(.*?)\n\s*\}",
    re.DOTALL,
)

# JS tagValueList assignment pattern
_JS_TAG_VALUE_PATTERN = re.compile(
    r"(?:var\s+)?tagValueList\s*=\s*['\"]([^'\"]+)['\"]",
)

# JS block comment pattern (strip before tagValueList search)
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# Common JS delimiters
_JS_DELIMITERS: tuple[str, ...] = ("|", ",", ";", "^")

# JS variable assignment containing a JSON array: name = [{...}, ...]
_JS_JSON_VAR_PATTERN = re.compile(
    r"(\w+)\s*=\s*(\[.*?\])\s*;",
    re.DOTALL,
)


# -----------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------


def identify_data_pages(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter HAR entries to data pages.

    Returns entries with: status 200, non-static, has content,
    and data-bearing Content-Type.

    When multiple entries share the same path (e.g., a login URL with a
    query parameter followed by the same path without query), the entry
    with the most content wins.  This handles auth flows where an
    authentication acknowledgment (text/html, 31 bytes) appears before
    the actual data page at the same base path.
    """
    candidates: dict[str, dict[str, Any]] = {}

    for entry in entries:
        resp = entry.get("response", {})
        req = entry.get("request", {})
        url = req.get("url", "")
        status = resp.get("status", 0)

        if status != 200:
            continue
        if is_static_resource(url):
            continue
        if not has_content(resp):
            continue

        content_type = content_type_of(resp)
        if not any(ct in content_type for ct in _DATA_CONTENT_TYPES):
            continue

        path = path_from_url(url)
        existing = candidates.get(path)
        if existing is None:
            candidates[path] = entry
        else:
            # Prefer the entry with more content (real data page over auth ack)
            existing_size = len(existing.get("response", {}).get("content", {}).get("text", ""))
            new_size = len(resp.get("content", {}).get("text", ""))
            if new_size > existing_size:
                candidates[path] = entry

    # Return in original HAR order
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for entry in entries:
        url = entry.get("request", {}).get("url", "")
        path = path_from_url(url)
        if path in candidates and path not in seen and candidates[path] is entry:
            result.append(entry)
            seen.add(path)

    return result


def analyze_page(entry: dict[str, Any]) -> PageAnalysis:
    """Analyze a single data page entry for extractable content.

    Returns a PageAnalysis with all detected content types:
    tables, JS functions, label-value pairs, and/or JSON data.
    A page can contribute to multiple sections.
    """
    req = entry.get("request", {})
    resp = entry.get("response", {})
    url = req.get("url", "")
    resource = path_from_url(url)
    content_type = content_type_of(resp)
    body = decode_body(resp)

    page = PageAnalysis(resource=resource, content_type=content_type)

    if _looks_like_json(content_type, body):
        page.json_data = _parse_json_body(body)

    # Fall through to HTML parsing when JSON sniffing matched but
    # parsing failed (body started with { or [ but wasn't valid JSON).
    if page.json_data is None and "text/html" in content_type:
        page.tables = detect_tables(body)
        page.js_functions = _detect_js_functions(body)
        page.js_json_variables = _detect_js_json_variables(body)
        page.label_pairs = detect_label_pairs(body)

    return page


def classify_page_format(page: PageAnalysis) -> str:
    """Classify the primary data format of a page.

    Returns one of: json, javascript_json, javascript, table,
    table_transposed, html_fields, or unknown.
    """
    if page.json_data is not None:
        return "json"

    if page.js_json_variables:
        return "javascript_json"

    if page.js_functions:
        return "javascript"

    if page.tables:
        # Check if any table has channel data
        for table in page.tables:
            if is_channel_table(table):
                if is_transposed(table):
                    return "table_transposed"
                return "table"

    if page.label_pairs:
        return "html_fields"

    return "unknown"


# -----------------------------------------------------------------------
# JavaScript data detection
# -----------------------------------------------------------------------


def _detect_js_functions(body: str) -> list[DetectedJsFunction]:
    """Detect JS functions with delimited data strings."""
    functions: list[DetectedJsFunction] = []

    for match in _JS_FUNCTION_PATTERN.finditer(body):
        name = match.group(1)
        func_body = match.group(2)

        # Strip block comments — some modems have a commented-out
        # tagValueList example before the real assignment
        clean_body = _JS_BLOCK_COMMENT.sub("", func_body)

        # Look for tagValueList assignment
        tag_match = _JS_TAG_VALUE_PATTERN.search(clean_body)
        if not tag_match:
            continue

        raw_value = tag_match.group(1)

        # Detect delimiter
        delimiter = _detect_delimiter(raw_value)
        if not delimiter:
            continue

        values = raw_value.split(delimiter)
        if len(values) < 3:
            continue

        functions.append(
            DetectedJsFunction(
                name=name,
                body=func_body,
                delimiter=delimiter,
                values=values,
            )
        )

    return functions


def _detect_js_json_variables(body: str) -> list[DetectedJsJsonVariable]:
    """Detect JS variable assignments containing JSON arrays.

    Finds patterns like ``json_dsData = [{...}, ...]`` in
    ``<script>`` blocks.  Only includes variables whose value
    parses as a JSON array of dicts with 2+ keys (channel objects).
    """
    variables: list[DetectedJsJsonVariable] = []

    for match in _JS_JSON_VAR_PATTERN.finditer(body):
        name = match.group(1)
        raw_json = match.group(2)

        try:
            data = json_mod.loads(raw_json)
        except (json_mod.JSONDecodeError, ValueError):
            continue

        if not isinstance(data, list) or not data or not isinstance(data[0], dict) or len(data[0]) < 2:
            continue

        variables.append(DetectedJsJsonVariable(name=name, data=data))

    return variables


def _detect_delimiter(raw_value: str) -> str:
    """Detect the delimiter character in a delimited string."""
    for d in _JS_DELIMITERS:
        if d in raw_value:
            parts = raw_value.split(d)
            if len(parts) >= 3:
                return d
    return ""


# -----------------------------------------------------------------------
# JSON body parsing
# -----------------------------------------------------------------------


def _looks_like_json(content_type: str, body: str) -> bool:
    """Check if a response is JSON, even when Content-Type lies.

    Matches explicit ``application/json`` Content-Type, handles
    misspellings (e.g., ``applation/json``), and sniffs bodies
    that start with ``[`` or ``{`` when served as ``text/html``.
    """
    if "json" in content_type:
        return True
    stripped = body.lstrip()
    return bool(stripped and stripped[0] in ("{", "["))


def _parse_json_body(body: str) -> dict[str, Any] | None:
    """Parse a JSON response body, returning None on failure.

    Top-level arrays are wrapped as ``{"_raw": [...]}`` to match
    the runtime loader convention (see coda56 parser.yaml).
    """
    if not body:
        return None
    try:
        data = json_mod.loads(body)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return {"_raw": data}
        return None
    except (json_mod.JSONDecodeError, TypeError):
        return None
