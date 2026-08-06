"""HNAPFieldsParser — extract system_info from HNAP SOAP responses.

HNAP system_info sources declare a ``response_key`` and field mappings
that extract values from the corresponding action response JSON.

See PARSING_SPEC.md System Info HNAP section.
"""

from __future__ import annotations

import logging
from typing import Any

from ...models.parser_config.system_info import HNAPSystemInfoSource
from ..base import BaseParser
from ..diagnostics import record_failed_field
from ..type_conversion import convert_value

_logger = logging.getLogger(__name__)


class HNAPFieldsParser(BaseParser):
    """Extract system_info fields from an HNAP action response.

    Each instance handles one ``HNAPSystemInfoSource`` from parser.yaml.
    The coordinator creates one instance per source and merges results.

    Args:
        config: Validated ``HNAPSystemInfoSource`` config.
    """

    def __init__(self, config: HNAPSystemInfoSource) -> None:
        self._config = config
        # Conversion-rejected raw values from the most recent parse —
        # PARSING_SPEC § Field Outcomes.
        self.failed_fields: dict[str, str] = {}

    def parse(self, resources: dict[str, Any]) -> dict[str, Any]:
        """Extract system_info fields from the HNAP response.

        Args:
            resources: Resource dict with ``"hnap_response"`` key
                containing the ``GetMultipleHNAPsResponse`` dict.

        Returns:
            Flat dict of field name → typed value.
        """
        hnap_response = resources.get("hnap_response")
        if not isinstance(hnap_response, dict):
            _logger.warning("No hnap_response in resources")
            return {}

        action_response = hnap_response.get(self._config.response_key)
        if not isinstance(action_response, dict):
            _logger.warning(
                "Response key '%s' not found in hnap_response",
                self._config.response_key,
            )
            return {}

        result: dict[str, Any] = {}
        self.failed_fields = {}
        for field_mapping in self._config.fields:
            value = action_response.get(field_mapping.source, "")
            if value:
                converted = convert_value(
                    value,
                    field_mapping.type,
                    map_config=field_mapping.map,
                    input_format=field_mapping.format,
                    scale=field_mapping.scale,
                )
                if converted is not None:
                    result[field_mapping.field] = converted
                else:
                    record_failed_field(self.failed_fields, field_mapping.field, value)

        return result
