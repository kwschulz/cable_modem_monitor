"""Tests for the pure channel-bond onboarding detection logic.

Pure logic — no HA dependency. Exercises every branch of ``evaluate``
with a table plus a focused check on the onboarding message formatter.
"""

from __future__ import annotations

import pytest

from custom_components.cable_modem_monitor.channel_bond_notifier import (
    ChannelTotals,
    evaluate,
    format_onboarding_message,
)
from custom_components.cable_modem_monitor.channel_bond_storage import BondState

# ---------------------------------------------------------------------
# evaluate() — decision table
# ---------------------------------------------------------------------

_CURRENT = ChannelTotals(downstream=24, upstream=4)
_MATCHING_STATE = BondState(baseline_downstream=24, baseline_upstream=4)
_STALE_DS_STATE = BondState(baseline_downstream=23, baseline_upstream=4)
_STALE_US_STATE = BondState(baseline_downstream=24, baseline_upstream=5)
_STALE_BOTH_STATE = BondState(baseline_downstream=23, baseline_upstream=5)

# ┌──────────────────────────────┬──────────────────────┬────────────────────────┬──────────────────┬────────────┐
# │ desc                         │ stored               │ onboarding_eligible    │ recovery_active  │ expected   │
# └──────────────────────────────┴──────────────────────┴────────────────────────┴──────────────────┴────────────┘
EVAL_CASES = [
    ("recovery_with_match", _MATCHING_STATE, True, True, "none"),
    ("recovery_with_stale_state", _STALE_DS_STATE, True, True, "none"),
    ("recovery_no_stored", None, True, True, "none"),
    ("fresh_setup_no_stored", None, True, False, "onboarding"),
    ("upgraded_entry_no_stored", None, False, False, "silent_init"),
    ("onboarded_steady", _MATCHING_STATE, True, False, "none"),
    ("upgraded_steady_after_silent_init", _MATCHING_STATE, False, False, "none"),
    # Totals differing from the stored baseline is no longer an action.
    # The comparison watched bonded totals rather than entities, which is
    # the wrong value in both directions: a DOCSIS 3.1 OFDM carrier that
    # briefly drops and returns produced two notifications for a single
    # poll of missing data, while an ID-mode channel reassignment — the
    # case it was built for — can leave the totals unchanged and go
    # undetected. Removed in 3.14.1 (#197).
    ("ds_differs_not_actionable", _STALE_DS_STATE, True, False, "none"),
    ("us_differs_not_actionable", _STALE_US_STATE, True, False, "none"),
    ("both_differ_not_actionable", _STALE_BOTH_STATE, True, False, "none"),
    # Onboarding must still fire on a fresh entry whose totals happen to
    # differ from nothing at all — the stored-is-None branch is what
    # distinguishes a fresh entry, not the totals.
    ("fresh_setup_ignores_totals", None, True, False, "onboarding"),
]


@pytest.mark.parametrize(
    "desc,stored,onboarding_eligible,recovery_active,expected",
    EVAL_CASES,
    ids=[c[0] for c in EVAL_CASES],
)
def test_evaluate(desc, stored, onboarding_eligible, recovery_active, expected):
    result = evaluate(
        current=_CURRENT,
        stored=stored,
        onboarding_eligible=onboarding_eligible,
        recovery_active=recovery_active,
    )
    assert result == expected


def test_evaluate_never_returns_change():
    """No combination of inputs produces a change action.

    Belt and braces alongside the table: the action was removed rather
    than made harder to reach, so nothing should be able to surface it.
    """
    stored_options = [None, _MATCHING_STATE, _STALE_DS_STATE, _STALE_US_STATE, _STALE_BOTH_STATE]
    for stored in stored_options:
        for eligible in (True, False):
            for recovery in (True, False):
                result = evaluate(
                    current=_CURRENT,
                    stored=stored,
                    onboarding_eligible=eligible,
                    recovery_active=recovery,
                )
                assert result in {"none", "onboarding", "silent_init"}


# ---------------------------------------------------------------------
# evaluate() — zero-totals guard
#
# A (0, 0) reading means "no data yet" (booting / no_signal), never a real
# bond. It must not fire a notification and must not be persisted as a
# baseline, regardless of recovery state or whether a real baseline already
# exists. Regression: a ~1h45m outage outlived the time-boxed recovery
# window, so a transient 0-channel reading was stored as baseline and the
# subsequent recovery looked like a 0 → 24 change. (MB7621, v3.14.0-beta.11)
#
# The change action is gone as of 3.14.1 (#197), so the recovery half of
# that regression can no longer notify — but the guard still has to keep
# (0, 0) out of the Store, which is what these cases assert.
# ---------------------------------------------------------------------

_ZERO = ChannelTotals(downstream=0, upstream=0)

ZERO_GUARD_CASES = [
    ("zero_with_real_baseline", _MATCHING_STATE, True, False, "none"),
    ("zero_no_stored_eligible", None, True, False, "none"),
    ("zero_no_stored_upgraded", None, False, False, "none"),
    ("zero_during_recovery", _MATCHING_STATE, True, True, "none"),
]


@pytest.mark.parametrize(
    "desc,stored,onboarding_eligible,recovery_active,expected",
    ZERO_GUARD_CASES,
    ids=[c[0] for c in ZERO_GUARD_CASES],
)
def test_evaluate_zero_totals_never_actionable(desc, stored, onboarding_eligible, recovery_active, expected):
    result = evaluate(
        current=_ZERO,
        stored=stored,
        onboarding_eligible=onboarding_eligible,
        recovery_active=recovery_active,
    )
    assert result == expected


# ---------------------------------------------------------------------
# Message formatters — smoke checks
# ---------------------------------------------------------------------


def test_onboarding_message_includes_counts_and_service():
    message = format_onboarding_message(model="TPS-2000", current=_CURRENT)
    assert "TPS-2000" in message
    assert "24 downstream" in message
    assert "4 upstream" in message
    assert "cable_modem_monitor.generate_dashboard" in message


def test_change_message_formatter_is_gone():
    """The change formatter no longer exists (#197)."""
    from custom_components.cable_modem_monitor import channel_bond_notifier

    assert not hasattr(channel_bond_notifier, "format_change_message")
