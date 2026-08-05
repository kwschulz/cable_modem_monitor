"""Tests for shared HAR entry inspection utilities."""

from __future__ import annotations

import base64

from solentlabs.cable_modem_monitor_catalog_tools.validation.har_utils import decode_body


class TestDecodeBody:
    """Tests for decode_body base64 handling."""

    def test_plain_text_passthrough(self) -> None:
        """Non-encoded body is returned as-is."""
        resp = {"content": {"text": "<html>data</html>"}}
        assert decode_body(resp) == "<html>data</html>"

    def test_base64_decoded(self) -> None:
        """Base64-encoded body is decoded."""
        original = '{"nodes": [{"num": "1"}]}'
        encoded = base64.b64encode(original.encode()).decode()
        resp = {"content": {"text": encoded, "encoding": "base64"}}
        assert decode_body(resp) == original

    def test_missing_content(self) -> None:
        """Missing content object returns empty string."""
        assert decode_body({}) == ""

    def test_base64_invalid_returns_raw(self) -> None:
        """Invalid base64 is suppressed, raw body returned."""
        resp = {"content": {"text": "not-valid-base64!!!", "encoding": "base64"}}
        assert decode_body(resp) == "not-valid-base64!!!"

    def test_base64_empty_body_skips_decode(self) -> None:
        """Empty body with base64 encoding returns empty string."""
        resp = {"content": {"text": "", "encoding": "base64"}}
        assert decode_body(resp) == ""
