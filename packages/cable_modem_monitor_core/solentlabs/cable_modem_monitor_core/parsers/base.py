"""BaseParser ABC — the extraction interface.

All format-specific parsers inherit from this. Each implementation is
parameterized by parser.yaml section config and extracts data from
pre-fetched resources without making network calls.

See PARSING_SPEC.md for the full extraction interface contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseParser(ABC):
    """Abstract base class for format-specific parsers.

    Channel parsers (HTMLTableParser, HTMLTableTransposedParser, etc.)
    return ``list[dict[str, Any]]`` — one dict per channel.

    System info parsers (HTMLFieldsParser for html_fields sources)
    return ``dict[str, Any]`` — flat key-value pairs whose values carry
    the type declared in parser.yaml.
    """

    @abstractmethod
    def parse(self, resources: dict[str, Any]) -> list[dict[str, Any]] | dict[str, Any]:
        """Extract data from pre-fetched resources.

        Args:
            resources: Resource dict keyed by URL path (BeautifulSoup for HTML,
                parsed dict for JSON). Built by the resource loader or HAR replay.

        Returns:
            Channel list or system_info dict, depending on parser type.
        """
