"""Cross-file consistency gate for every shipped catalog entry.

``validate_cross_file`` runs at intake, inside ``generate_config``. That
covers generated configs and nothing else — most catalog fixes land as a
hand edit to a committed parser.yaml, which never passes through intake.
This test closes that gap by running the same checks over the shipped
files, so a rule like the provisioned-speed direction guard applies to
the catalog as it ships rather than only to the moment it was born.

Auto-parametrized by conftest.py via ``config_pair``. Adding a modem =
adding files.
"""

from __future__ import annotations

from pathlib import Path

from solentlabs.cable_modem_monitor_core.config_loader import load_modem_config, load_parser_config
from solentlabs.cable_modem_monitor_core.validation.cross_file import validate_cross_file


def test_shipped_config_cross_file(config_pair: tuple[Path, Path]) -> None:
    """Every shipped modem.yaml + parser.yaml pair passes cross-file validation."""
    modem_path, parser_path = config_pair
    errors = validate_cross_file(load_modem_config(modem_path), load_parser_config(parser_path))

    assert not errors, "{}: {} cross-file error(s):\n{}".format(
        modem_path.parent.name,
        len(errors),
        "\n".join(f"  - {e}" for e in errors),
    )
