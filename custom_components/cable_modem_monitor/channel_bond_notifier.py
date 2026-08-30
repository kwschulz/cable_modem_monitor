"""Channel-bond onboarding detection — pure logic.

Decides whether a poll should fire the one-time onboarding notification,
based on whether any state has been stored for this entry yet and whether
the entry was set up after this feature landed (eligible for onboarding)
or upgraded from an older version (not eligible — silent init only).

HA-free so tests can run without mocks. Persistence is handled by
:mod:`channel_bond_storage`; the coordinator wires both together and
fires the HA ``persistent_notification.create`` service.

Tracks bonded channel totals only (downstream + upstream). Changes to
those totals no longer notify — see ``docs/HA_ADAPTER_SPEC.md``
§ Notifications and #197. The totals remain available every poll on the
DS and US Channel Count sensors, which automations can consume without
interrupting anyone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .channel_bond_storage import BondState

NotifierAction = Literal["none", "silent_init", "onboarding"]


@dataclass(frozen=True)
class ChannelTotals:
    downstream: int
    upstream: int


def evaluate(
    *,
    current: ChannelTotals,
    stored: BondState | None,
    onboarding_eligible: bool,
    recovery_active: bool,
) -> NotifierAction:
    """Decide what the coordinator should do with the current totals.

    Args:
        current: Totals from this poll's ``system_info``.
        stored: Persisted baseline, or ``None`` if no state has ever
            been saved for this entry.
        onboarding_eligible: ``True`` for entries created after this
            feature shipped (config flow sets the entry-data flag);
            ``False`` for entries upgraded from an older version.
        recovery_active: Orchestrator's recovery flag. Counts flux
            during a recovery window is expected and suppressed.

    ``stored`` is still read even though the totals are no longer
    compared: its presence is what distinguishes an already-onboarded
    entry from a fresh one, so the onboarding notification fires exactly
    once per config entry.
    """
    # A zero-channel reading means "no data yet" (booting / no_signal page),
    # never a real bond — an operational modem always reports channels. Don't
    # act on it and (since the call site only persists when action != "none")
    # don't let it become the stored baseline. This is the primary guard:
    # recovery_active is time-boxed and a real outage can outlive the window.
    if current.downstream == 0 and current.upstream == 0:
        return "none"

    if recovery_active:
        return "none"

    if stored is None:
        return "onboarding" if onboarding_eligible else "silent_init"

    # Totals differing from the stored baseline is deliberately not an
    # action. The comparison watched bonded totals rather than entities,
    # which is the wrong value in both directions: a DOCSIS 3.1 OFDM
    # carrier that briefly drops and returns produces two notifications
    # for a single poll of missing data, while an ID-mode channel
    # reassignment — the case this was built for — can leave the totals
    # unchanged and go undetected. See #197.
    return "none"


def format_onboarding_message(*, model: str, current: ChannelTotals) -> str:
    """Body text for the one-time onboarding notification."""
    return (
        f"{model} is online with {current.downstream} downstream and "
        f"{current.upstream} upstream bonded channels. You can auto-generate "
        f"a Lovelace dashboard for this modem via Developer Tools → Actions → "
        f"`cable_modem_monitor.generate_dashboard`."
    )
