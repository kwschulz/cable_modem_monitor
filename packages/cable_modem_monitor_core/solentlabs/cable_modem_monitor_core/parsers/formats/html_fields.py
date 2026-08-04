"""HTMLFieldsParser — extract system_info from HTML via label, id, or CSS.

Produces a flat ``dict[str, Any]`` from named fields in HTML pages.
Used for system_info sources with ``format: html_fields``.

See PARSING_SPEC.md System Info / html_fields selector types.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from bs4 import BeautifulSoup, Tag

from ...models.parser_config.system_info import HTMLFieldMapping, HTMLFieldsSource
from ..base import BaseParser
from ..diagnostics import record_failed_field
from ..type_conversion import convert_value

_logger = logging.getLogger(__name__)


class HTMLFieldsParser(BaseParser):
    """Extract system_info fields from HTML via label text or element id.

    Each instance handles one ``HTMLFieldsSource`` (one resource with a
    list of field mappings). The caller merges results from multiple sources.

    Args:
        source: html_fields source config from parser.yaml system_info section.
    """

    def __init__(self, source: HTMLFieldsSource) -> None:
        self._resource = source.resource
        self._fields = source.fields
        # Conversion-rejected raw values from the most recent parse —
        # PARSING_SPEC § Field Outcomes.
        self.failed_fields: dict[str, str] = {}

    def parse(self, resources: dict[str, Any]) -> dict[str, Any]:
        """Extract named fields from the configured HTML resource.

        Args:
            resources: Resource dict (path -> BeautifulSoup).

        Returns:
            Flat dict of field name -> typed value.
        """
        soup = resources.get(self._resource)
        if soup is None:
            _logger.warning("Resource '%s' not found", self._resource)
            return {}

        result: dict[str, Any] = {}
        self.failed_fields = {}
        for field_cfg in self._fields:
            value = _extract_field(soup, field_cfg)
            if value is not None:
                converted = convert_value(
                    value,
                    field_cfg.type,
                    map_config=field_cfg.map,
                    input_format=field_cfg.format,
                    scale=field_cfg.scale,
                )
                if converted is not None:
                    result[field_cfg.field] = converted
                else:
                    record_failed_field(self.failed_fields, field_cfg.field, value)

        return result


def _extract_field(soup: BeautifulSoup | Tag, field_cfg: HTMLFieldMapping) -> str | None:
    """Extract a single field value using the configured locator.

    Tries locators in order: id, css, label. Returns the first match.
    Applies optional pattern regex to the extracted text.
    """
    raw: str | None = None

    if field_cfg.id:
        raw = _extract_by_id(soup, field_cfg.id, field_cfg.attribute)
    elif field_cfg.css:
        raw = _extract_by_css(soup, field_cfg.css, field_cfg.attribute)
    elif field_cfg.label:
        raw = _extract_by_label(soup, field_cfg.label)

    if raw is None:
        return None

    # Apply optional pattern regex
    if field_cfg.pattern:
        match = re.search(field_cfg.pattern, raw)
        if match and match.lastindex:
            raw = match.group(1)
        elif match:
            raw = match.group(0)
        else:
            _logger.debug(
                "Pattern '%s' did not match text '%s' for field '%s'",
                field_cfg.pattern,
                raw,
                field_cfg.field,
            )
            return None

    return raw.strip() if raw else None


def _extract_by_id(soup: BeautifulSoup | Tag, element_id: str, attribute: str = "") -> str | None:
    """Extract value from an element by its id attribute."""
    element = soup.find(id=element_id)
    if element is None or not isinstance(element, Tag):
        return None
    return _element_value(element, attribute)


def _extract_by_css(soup: BeautifulSoup | Tag, css_selector: str, attribute: str = "") -> str | None:
    """Extract value from an element by CSS selector."""
    element = soup.select_one(css_selector)
    if element is None or not isinstance(element, Tag):
        return None
    return _element_value(element, attribute)


def _element_value(element: Tag, attribute: str) -> str | None:
    """Extract text content or an attribute value from an element.

    BeautifulSoup returns multi-valued HTML attributes (e.g. ``class``)
    as lists. This function joins them with spaces to match the original
    HTML representation, so downstream patterns and consumers see
    ``"success"`` or ``"glyphicon glyphicon-ok"`` instead of a Python
    list repr.
    """
    if not attribute:
        return str(element.get_text())
    val = element.get(attribute)
    if val is None:
        return None
    if isinstance(val, list):
        return " ".join(str(v) for v in val)
    return str(val)


def _extract_by_label(soup: BeautifulSoup | Tag, label_text: str) -> str | None:
    """Extract value adjacent to a label, using structural cascade.

    Tries multiple HTML patterns to find the value element next to a
    label. This is anti-fragile — firmware updates that change HTML
    structure don't break configs because multiple patterns are tried.

    Two kinds of elements are skipped as wrappers:

    1. Elements with block-level children (``table``, ``div``, etc.) —
       their ``get_text()`` includes all nested content.
    2. Elements whose direct child (also a cascade-supported tag)
       contains the label text — the child is a more specific match
       and will be found later in the loop.

    Cascade order:
    1. ``<td>label</td><td>value</td>`` (sibling cells)
    2. ``<th>label</th>`` paired with ``<td>`` in same row
    3. ``<span>label</span>`` followed by sibling ``<span>``
    4. ``<dt>label</dt><dd>value</dd>`` (definition list)
    5. ``<div>label</div>`` followed by sibling ``<div>``
    """
    # Normalize label for matching
    label_lower = label_text.lower().strip()

    # Search all text-containing elements
    for element in soup.find_all(["td", "th", "span", "dt", "div", "label"]):
        if not isinstance(element, Tag):
            continue

        # Skip wrapper elements — their get_text() includes all nested
        # content, causing false matches on label text.
        if element.find(_BLOCK_LEVEL_TAGS):
            continue

        text = element.get_text(strip=True)
        if label_lower not in text.lower():
            continue

        # Prefer leaf matches: if a direct child is also a cascade-
        # supported tag and contains the label text, skip this element
        # and let the child match on a later iteration.
        if _has_child_with_label(element, label_lower):
            continue

        # Try structural patterns based on element type
        value = _try_label_cascade(element)
        if value is not None:
            return value

    return None


# Block-level tags that indicate a wrapper element when found as children.
_BLOCK_LEVEL_TAGS = ["table", "div", "section", "article", "ul", "ol", "dl"]

# Tags that participate in the label cascade — used to detect when a
# parent element should defer to a more specific child match.
_SEARCH_TAGS = frozenset({"td", "th", "span", "dt", "div", "label"})


def _has_child_with_label(element: Tag, label_lower: str) -> bool:
    """Check if a direct child (cascade-supported tag) contains the label text."""
    for child in element.children:
        if not isinstance(child, Tag):
            continue
        if child.name not in _SEARCH_TAGS:
            continue
        if label_lower in child.get_text(strip=True).lower():
            return True
    return False


_CascadeHandler = Callable[[Tag], str | None]


def _try_label_cascade(label_element: Tag) -> str | None:
    """Try structural patterns to find the value adjacent to a label element."""
    tag = label_element.name
    handler = _LABEL_CASCADE_HANDLERS.get(tag)
    if handler is not None:
        return handler(label_element)
    return None


def _cascade_td(el: Tag) -> str | None:
    """td → next sibling td."""
    sibling = el.find_next_sibling("td")
    if sibling and isinstance(sibling, Tag):
        return str(sibling.get_text())
    return None


def _cascade_th(el: Tag) -> str | None:
    """th → paired td in same row."""
    row = el.find_parent("tr")
    if row and isinstance(row, Tag):
        tds = row.find_all("td")
        if tds:
            return str(tds[0].get_text())
    return None


def _cascade_sibling(sibling_tag: str) -> _CascadeHandler:
    """Build a cascade handler for same-tag sibling lookup."""

    def handler(el: Tag) -> str | None:
        sibling = el.find_next_sibling(sibling_tag)
        if sibling and isinstance(sibling, Tag):
            return str(sibling.get_text())
        return None

    return handler


def _cascade_dt(el: Tag) -> str | None:
    """dt → next dd."""
    dd = el.find_next_sibling("dd")
    if dd and isinstance(dd, Tag):
        return str(dd.get_text())
    return None


def _cascade_label(el: Tag) -> str | None:
    """label → associated input (by for=) or next sibling."""
    for_id = el.get("for")
    if for_id:
        parent = el.find_parent()
        target = parent.find(id=for_id) if parent else None
        if target and isinstance(target, Tag):
            val = target.get("value")
            return str(val) if val is not None else str(target.get_text())
    sibling = el.find_next_sibling()
    if sibling and isinstance(sibling, Tag):
        return str(sibling.get_text())
    return None


_LABEL_CASCADE_HANDLERS: dict[str, _CascadeHandler] = {
    "td": _cascade_td,
    "th": _cascade_th,
    "span": _cascade_sibling("span"),
    "dt": _cascade_dt,
    "div": _cascade_sibling("div"),
    "label": _cascade_label,
}
