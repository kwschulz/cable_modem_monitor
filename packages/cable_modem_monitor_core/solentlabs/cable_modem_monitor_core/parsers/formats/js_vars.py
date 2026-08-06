"""JSVarsParser — extract key-value pairs from JS variable assignments.

Handles simple ``var x = 'value'`` or ``x = 'value'`` assignments in
HTML ``<script>`` tags. Each field mapping names a JS variable and the
output field it maps to.

Unlike ``JSEmbeddedParser`` / ``JSSystemInfoParser`` (which parse
``tagValueList`` delimited strings), this targets standalone named
variables — common in Arris gateway firmware (e.g., ``js_FWVersion``,
``js_HWTypeVersion``).

See PARSING_SPEC.md System Info section (javascript_vars source).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ...models.parser_config.system_info import JSVarsSystemInfoSource
from ..base import BaseParser
from ..diagnostics import record_failed_field
from ..type_conversion import convert_value

_logger = logging.getLogger(__name__)

# Matches: var x = 'value'  or  x = 'value'  (single-quoted)
_JS_VAR_RE = re.compile(r"(?:var\s+)?(\w+)\s*=\s*'([^']*)'")


class JSVarsParser(BaseParser):
    """Extract key-value pairs from JS variable assignments.

    Args:
        config: Validated ``JSVarsSystemInfoSource`` from parser.yaml.
    """

    def __init__(self, config: JSVarsSystemInfoSource) -> None:
        self._config = config
        # Conversion-rejected raw values from the most recent parse —
        # PARSING_SPEC § Field Outcomes.
        self.failed_fields: dict[str, str] = {}

    def parse(self, resources: dict[str, Any]) -> dict[str, Any]:
        """Extract fields from JS variable assignments.

        Args:
            resources: Resource dict (path -> BeautifulSoup).

        Returns:
            Dict of field names to typed values.
        """
        soup = resources.get(self._config.resource)
        if soup is None:
            _logger.warning("Resource '%s' not found", self._config.resource)
            return {}

        # Build lookup: JS variable name -> field mapping
        var_to_mapping = {f.source: f for f in self._config.fields}

        result: dict[str, Any] = {}
        self.failed_fields = {}
        for script in soup.find_all("script"):
            text = script.string
            if not text:
                continue
            for match in _JS_VAR_RE.finditer(text):
                var_name = match.group(1)
                field_def = var_to_mapping.get(var_name)
                if field_def is not None:
                    raw_value = match.group(2)
                    converted = convert_value(
                        raw_value,
                        field_def.type,
                        map_config=field_def.map,
                        input_format=field_def.format,
                        scale=field_def.scale,
                    )
                    if converted is not None:
                        result[field_def.field] = converted
                    elif raw_value:
                        record_failed_field(self.failed_fields, field_def.field, raw_value)

        return result
