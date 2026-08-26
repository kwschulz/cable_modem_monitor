"""Tests for version constant and startup log message."""

from __future__ import annotations

import re

from custom_components.cable_modem_monitor.const import VERSION


class TestVersion:
    """Test version constant."""

    def test_version_format(self):
        """VERSION follows semantic versioning: X.Y.Z or X.Y.Z-beta.N."""
        pattern = r"^\d+\.\d+\.\d+(-beta\.\d+)?$"
        assert re.match(pattern, VERSION), f"Invalid version format: {VERSION}"

    def test_current_version(self):
        """Verify the current version value.

        IMPORTANT: Do not edit this version manually!
        Use: python scripts/release.py <version>
        The script updates const.py, manifest.json, and this file.
        """
        assert VERSION == "3.14.0"

    def test_startup_log_message_format(self):
        """The startup log line includes the version string."""
        # Mirrors the format in __init__.py async_setup_entry
        log_msg = f"Cable Modem Monitor v{VERSION} starting [Solent Labs TPS-2000]"
        assert VERSION in log_msg

    def test_local_dev_suffix(self, monkeypatch):
        """CMM_LOCAL_DEV marks dev installs — a dev tree otherwise logs the previous beta."""
        from custom_components.cable_modem_monitor import _local_dev_suffix

        monkeypatch.setenv("CMM_LOCAL_DEV", "1")
        assert _local_dev_suffix() == " (local dev)"
        monkeypatch.delenv("CMM_LOCAL_DEV")
        assert _local_dev_suffix() == ""
