"""Catalog-driven regression for the channel mapping manager.

For every committed ``modem.expected.json`` in the catalog, drive the
data through ``build_channel_map`` in both NUMBER and ID modes.  This
closes the seam between Core's parser output and HA's mapping layer:
``intake_pipeline_regression`` validates HAR -> parser; this validates
parser -> ``build_channel_map``.

A new modem with a parser whose output shape differs from the rest of
the catalog (e.g., omits ``lock_status``) will surface here before it
ships, instead of crashing setup on a user's hardware.

See CHANNEL_IDENTIFICATION_SPEC.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.cable_modem_monitor.const import ChannelIdentity
from custom_components.cable_modem_monitor.mapping_manager import build_channel_map
from tests.fixture_helpers import load_fixture

_CATALOG_ROOT = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "cable_modem_monitor_catalog"
    / "solentlabs"
    / "cable_modem_monitor_catalog"
    / "modems"
)


def _expected_json_paths() -> list[Path]:
    return sorted(_CATALOG_ROOT.rglob("modem.expected.json"))


def _modem_id(path: Path) -> str:
    return f"{path.parts[-4]}/{path.parts[-3]}"


@pytest.mark.parametrize(
    "expected_path",
    _expected_json_paths(),
    ids=_modem_id,
)
@pytest.mark.parametrize(
    "mode",
    [ChannelIdentity.NUMBER, ChannelIdentity.ID],
    ids=["number", "id"],
)
def test_catalog_modem_data_survives_mapping(expected_path: Path, mode: ChannelIdentity) -> None:
    """Every catalog modem's parser output must survive build_channel_map.

    No exception in either mode.  Locked channels must produce slot
    entries; missing-``lock_status`` modems are treated as locked per
    Core's contract and must also produce slot entries.
    """
    data = load_fixture(expected_path)
    downstream = data.get("downstream", [])
    upstream = data.get("upstream", [])

    result = build_channel_map(downstream, upstream, mode)

    # Sanity floor: when the input has channels with the keys this
    # mode needs, the result must not be silently empty.  Catches the
    # "ID mode empties everything for modems without lock_status"
    # regression in addition to the NUMBER-mode crash.
    if mode == ChannelIdentity.NUMBER:
        ds_keyable = any(c.get("channel_number") is not None for c in downstream)
        us_keyable = any(c.get("channel_number") is not None for c in upstream)
    else:
        ds_keyable = any(c.get("channel_type") is not None and c.get("channel_id") is not None for c in downstream)
        us_keyable = any(c.get("channel_type") is not None and c.get("channel_id") is not None for c in upstream)

    if ds_keyable:
        assert result.downstream, (
            f"{_modem_id(expected_path)} {mode.value}: " f"downstream had keyable channels but result is empty"
        )
    if us_keyable:
        assert result.upstream, (
            f"{_modem_id(expected_path)} {mode.value}: " f"upstream had keyable channels but result is empty"
        )
