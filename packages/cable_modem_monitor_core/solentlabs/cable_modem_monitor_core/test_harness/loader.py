"""Modem directory loading for mock server construction.

Loads HAR entries and modem config from a modem directory, producing
everything ``HARMockServer`` needs. Shared by the standalone serve
entry point and any programmatic use that starts from a directory path.

The test runner (``runner.py``) operates at a different level — it
takes pre-resolved paths from ``ModemTestCase`` rather than discovering
them. Both paths converge at ``HARMockServer(entries, modem_config)``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config_loader import load_modem_config
from ..har import load_har_json
from ..models.modem_config.config import ModemConfig
from .discovery import resolve_modem_config

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerConfig:
    """Everything needed to construct a ``HARMockServer``.

    Attributes:
        har_entries: HAR ``log.entries`` list.
        modem_config: Validated ``ModemConfig`` instance.
        modem_name: Human-readable name (e.g., ``arris/sb8200``).
    """

    har_entries: list[dict[str, Any]]
    modem_config: ModemConfig
    modem_name: str


def load_server_from_modem_dir(
    modem_dir: Path,
    har_name: str | None = None,
) -> ServerConfig:
    """Load HAR entries and modem config from a modem directory.

    Finds ``test_data/*.har``, resolves ``modem*.yaml`` using the same
    name-matching logic as test discovery (``modem-{name}.har`` ->
    ``modem-{name}.yaml``, fallback to ``modem.yaml``).

    Args:
        modem_dir: Path to the modem directory containing ``modem.yaml``
            and ``test_data/``.
        har_name: Specific HAR file name within ``test_data/`` (e.g.,
            ``modem.har``). If ``None``, uses the first ``.har`` file
            found (sorted alphabetically).

    Returns:
        ``ServerConfig`` with loaded HAR entries and modem config.

    Raises:
        FileNotFoundError: If the modem directory, ``test_data/``
            subdirectory, HAR file, or modem config cannot be found.
        ValueError: If the HAR file is malformed or the modem config
            fails validation.
    """
    modem_dir = modem_dir.resolve()

    if not modem_dir.is_dir():
        raise FileNotFoundError(f"Modem directory not found: {modem_dir}")

    test_data_dir = modem_dir / "test_data"
    if not test_data_dir.is_dir():
        raise FileNotFoundError(f"No test_data/ directory in {modem_dir}")

    # Find HAR file
    har_path = _find_har(test_data_dir, har_name)

    # Resolve modem config using same logic as test discovery
    modem_config_path = resolve_modem_config(har_path.stem, modem_dir)
    if modem_config_path is None:
        raise FileNotFoundError(f"No modem config found for {har_path.name} in {modem_dir}")

    # Load HAR entries
    har_entries = _load_har_entries(har_path)

    # Load modem config
    try:
        modem_config = load_modem_config(modem_config_path)
    except Exception as e:
        raise ValueError(f"Invalid modem config {modem_config_path}: {e}") from e

    # Derive human-readable name
    modem_name = f"{modem_dir.parent.name}/{modem_dir.name}"

    return ServerConfig(
        har_entries=har_entries,
        modem_config=modem_config,
        modem_name=modem_name,
    )


def _find_har(test_data_dir: Path, har_name: str | None) -> Path:
    """Find a HAR file in the test_data directory.

    Args:
        test_data_dir: Path to the ``test_data/`` directory.
        har_name: Specific file name, or ``None`` for first found.

    Returns:
        Path to the HAR file.

    Raises:
        FileNotFoundError: If no matching HAR file exists.
    """
    if har_name is not None:
        har_path = test_data_dir / har_name
        if not har_path.is_file():
            raise FileNotFoundError(f"HAR file not found: {har_path}")
        return har_path

    har_files = sorted(test_data_dir.glob("*.har"))
    if not har_files:
        raise FileNotFoundError(f"No .har files in {test_data_dir}")

    return har_files[0]


def _load_har_entries(har_path: Path) -> list[dict[str, Any]]:
    """Load and extract entries from a HAR file.

    Args:
        har_path: Path to the ``.har`` file.

    Returns:
        List of HAR ``log.entries`` dicts.

    Raises:
        ValueError: If the file is not valid HAR JSON.
    """
    try:
        har_data = load_har_json(har_path)
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"Failed to read HAR file {har_path}: {e}") from e

    try:
        entries: list[dict[str, Any]] = har_data["log"]["entries"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"Invalid HAR structure in {har_path}: missing log.entries") from e

    return entries
