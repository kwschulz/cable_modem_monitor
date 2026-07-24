"""Every committed golden validates against the event bus payload schema.

Regression net for #92: the DM1000 OFDMA hook appended a channel without
``channel_number``. HAR replay passed (parser and golden agreed on the
gap), but once the event payload shipped, ``ModemDataPayload.model_validate``
crashed every poll. Goldens are parser output and the payload is fired
from that output unconditionally, so a schema violation here is a
runtime crash. Runs for every modem regardless of ``status``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from solentlabs.cable_modem_monitor_core.orchestration.event_payload import (
    ModemDataPayload,
)

_MODEMS_ROOT = Path(__file__).parent.parent / "solentlabs" / "cable_modem_monitor_catalog" / "modems"
_GOLDENS = sorted(_MODEMS_ROOT.rglob("test_data/*.expected.json"))


def _golden_id(path: Path) -> str:
    modem_dir = path.parent.parent
    return f"{modem_dir.parent.name}/{modem_dir.name}/{path.name}"


@pytest.mark.parametrize("golden_path", _GOLDENS, ids=_golden_id)
def test_golden_validates_as_event_payload(golden_path: Path) -> None:
    """Golden passes the same validation to_event_payload runs per poll."""
    ModemDataPayload.model_validate(json.loads(golden_path.read_text()))
