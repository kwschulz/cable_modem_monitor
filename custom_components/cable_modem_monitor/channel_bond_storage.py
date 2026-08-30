"""Per-entry persistence for the channel-bond onboarding notifier.

Backs the notifier with HA's ``Store`` helper instead of config-entry
data. Entry-data writes trigger the integration's update listener, which
reloads the integration, so per-entry state that changes at runtime
cannot live there. Store writes don't fire the listener.

Payload schema::

    {
        "baseline_downstream": int,
        "baseline_upstream": int,
    }

Absence of the Store key means either a fresh install (onboarding not
yet fired) or an upgraded entry that pre-dates this feature. The
coordinator distinguishes the two via ``CONF_CHANNEL_ONBOARDING_ELIGIBLE``
on ``entry.data`` — set once at config-flow create time, never mutated
afterwards (so it doesn't trip the update listener either).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from homeassistant.helpers.storage import Store

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_STORAGE_VERSION = 1
_STORAGE_KEY_TEMPLATE = "cable_modem_monitor.{entry_id}.channel_bond"


@dataclass(frozen=True)
class BondState:
    """Persisted baseline totals for one modem."""

    baseline_downstream: int
    baseline_upstream: int


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, int]]:
    return Store(
        hass,
        _STORAGE_VERSION,
        _STORAGE_KEY_TEMPLATE.format(entry_id=entry_id),
    )


async def async_load_bond_state(hass: HomeAssistant, entry_id: str) -> BondState | None:
    """Return the persisted baseline, or ``None`` if never saved."""
    payload = await _store(hass, entry_id).async_load()
    if payload is None:
        return None
    return BondState(
        baseline_downstream=payload["baseline_downstream"],
        baseline_upstream=payload["baseline_upstream"],
    )


async def async_save_bond_state(hass: HomeAssistant, entry_id: str, state: BondState) -> None:
    """Persist the new baseline."""
    await _store(hass, entry_id).async_save(asdict(state))


async def async_remove_bond_state(hass: HomeAssistant, entry_id: str) -> None:
    """Delete persisted state when the entry is removed."""
    await _store(hass, entry_id).async_remove()
