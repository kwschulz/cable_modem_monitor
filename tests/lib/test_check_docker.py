"""Tests for scripts/dev/check-docker.py — the WSL Docker recovery flow.

Coverage breakdown per docs/CODE_REVIEW.md § Test File Standards:
- Path/state helpers (_to_windows_path, _engine_up_but_socket_missing) —
  table-driven inline.
- _reconnect_engine_socket grace behaviour — table-driven with stubbed
  Desktop/socket probes; asserts when the grace poll runs and when the
  sudo proxy launch is reached (no real Docker state is touched).
- _wait_for_docker_ready engine-up grace — table-driven with stubbed
  clock and probes; asserts the deadline extends from engine-up without
  ever shrinking the original budget, and that docker info keeps being
  polled while the engine gap persists.
- _prompt_start_docker_wsl decision flow — scenario table covering the
  surgical pre-launch reconnect, the stuck-Desktop backend repair (with
  the engine-gap proxy fallback when the repair leaves the socket
  missing), and the cold-start paths, including the slow-cold-boot
  regression: the readiness wait grants the provisioning grace from
  engine-up, so recovery afterwards goes straight to the proxy reconnect
  (grace_wait=False) instead of stacking a second grace before sudo.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the checker module by file path — it lives in scripts/dev/, not in
# any package (and its filename is not a valid module name), so we cannot
# import it normally.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "check-docker.py"
_spec = importlib.util.spec_from_file_location("check_docker", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["check_docker"] = _mod
_spec.loader.exec_module(_mod)


# ---------------------------------------------------------------------------
# _to_windows_path — table-driven
# ---------------------------------------------------------------------------

# fmt: off
_WINDOWS_PATH_CASES = [
    # description, linux_path, expected
    ("drive-mounted program files", "/mnt/c/Program Files/Docker/resources", "C:\\Program Files\\Docker\\resources"),
    ("other drive letter",          "/mnt/d/tools/docker",                   "D:\\tools\\docker"),
    ("plain linux path",            "/home/user/docker",                     None),
    ("wsl shared mount, not a drive", "/mnt/wsl/docker-desktop",             None),
]
# fmt: on


@pytest.mark.parametrize(
    ("linux_path", "expected"),
    [(c[1], c[2]) for c in _WINDOWS_PATH_CASES],
    ids=[c[0] for c in _WINDOWS_PATH_CASES],
)
def test_to_windows_path(linux_path, expected):
    """Drive-mounted WSL paths convert; everything else returns None."""
    assert _mod._to_windows_path(linux_path) == expected


# ---------------------------------------------------------------------------
# _engine_up_but_socket_missing — table-driven
# ---------------------------------------------------------------------------

# fmt: off
_SOCKET_STATE_CASES = [
    # description, engine_sock_exists, distro_sock_exists, expected
    ("engine up, socket missing (the repair trigger)", True,  False, True),
    ("healthy: both present",                          True,  True,  False),
    ("engine down, stale socket",                      False, True,  False),
    ("nothing running",                                False, False, False),
]
# fmt: on


@pytest.mark.parametrize(
    ("engine_exists", "distro_exists", "expected"),
    [(c[1], c[2], c[3]) for c in _SOCKET_STATE_CASES],
    ids=[c[0] for c in _SOCKET_STATE_CASES],
)
def test_engine_up_but_socket_missing(monkeypatch, engine_exists, distro_exists, expected):
    """Only the engine-up/socket-absent combination triggers the repair path."""
    states = {_mod.ENGINE_SOCK: engine_exists, _mod.DISTRO_SOCK: distro_exists}
    monkeypatch.setattr(_mod.os.path, "exists", lambda path: states.get(path, False))
    assert _mod._engine_up_but_socket_missing() is expected


# ---------------------------------------------------------------------------
# _reconnect_engine_socket — grace-period behaviour
# ---------------------------------------------------------------------------

# fmt: off
_RECONNECT_CASES = [
    # description, grace_wait, desktop_running, socket_appears_in_grace,
    #   expect_grace_polled, expect_proxy_launched
    ("grace: Desktop provisions the socket itself", True,  True,  True,  True,  False),
    ("grace: Desktop never provisions, proxy runs", True,  True,  False, True,  True),
    ("grace requested but Desktop not running",     True,  False, False, False, True),
    ("no grace: straight to the proxy",             False, True,  True,  False, True),
]
# fmt: on


@pytest.mark.parametrize(
    ("grace_wait", "desktop_running", "socket_appears", "grace_polled", "proxy_launched"),
    [(c[1], c[2], c[3], c[4], c[5]) for c in _RECONNECT_CASES],
    ids=[c[0] for c in _RECONNECT_CASES],
)
def test_reconnect_engine_socket_grace(
    monkeypatch, grace_wait, desktop_running, socket_appears, grace_polled, proxy_launched
):
    """The grace poll runs only when requested with Desktop up; the sudo proxy is the fallback."""
    calls = {"grace": 0, "proxy": 0}

    def fake_wait_for_socket(timeout_seconds=15):
        calls["grace"] += 1
        return socket_appears

    def fake_start_proxy(docker_exe):
        calls["proxy"] += 1
        return True

    monkeypatch.setattr(_mod, "is_docker_desktop_running", lambda: desktop_running)
    monkeypatch.setattr(_mod, "_wait_for_socket", fake_wait_for_socket)
    monkeypatch.setattr(_mod, "_start_distro_proxy", fake_start_proxy)

    assert _mod._reconnect_engine_socket("/fake/Docker Desktop.exe", grace_wait) is True
    assert (calls["grace"] > 0) is grace_polled
    assert (calls["proxy"] > 0) is proxy_launched


# ---------------------------------------------------------------------------
# _wait_for_docker_ready — engine-up grace deadline behaviour
# ---------------------------------------------------------------------------

# fmt: off
_WAIT_READY_CASES = [
    # description, timeout, grace, gap_from_tick, ready_at_tick, expected, expected_ticks
    ("ready within the plain timeout",           5, 0,  None, 2,    True,  2),
    ("plain timeout expires",                    5, 0,  None, None, False, 5),
    ("grace requested, engine never appears",    5, 10, None, None, False, 5),
    ("early engine keeps the full budget",       5, 2,  1,    4,    True,  4),
    ("late engine extends the deadline",         5, 10, 4,    8,    True,  8),
    ("engine-up grace expires",                  5, 3,  4,    None, False, 7),
    ("daemon reachable while the gap persists",  5, 10, 1,    2,    True,  2),
]
# fmt: on


@pytest.mark.parametrize(
    ("timeout", "grace", "gap_from", "ready_at", "expected", "ticks"),
    [(c[1], c[2], c[3], c[4], c[5], c[6]) for c in _WAIT_READY_CASES],
    ids=[c[0] for c in _WAIT_READY_CASES],
)
def test_wait_for_docker_ready_grace(monkeypatch, timeout, grace, gap_from, ready_at, expected, ticks):
    """The deadline extends from engine-up only; docker info is polled throughout."""
    tick = {"n": 0}

    class _Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_sleep(seconds):
        tick["n"] += 1

    def fake_run(cmd, **kwargs):
        ready = ready_at is not None and tick["n"] >= ready_at
        return _Result(0 if ready else 1)

    monkeypatch.setattr(_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(
        _mod,
        "_engine_up_but_socket_missing",
        lambda: gap_from is not None and tick["n"] >= gap_from,
    )
    monkeypatch.setattr(_mod.subprocess, "run", fake_run)

    assert _mod._wait_for_docker_ready(timeout, engine_up_grace=grace) is expected
    assert tick["n"] == ticks


# ---------------------------------------------------------------------------
# _prompt_start_docker_wsl — decision-flow scenarios
# ---------------------------------------------------------------------------

# Each scenario stubs every Docker/Desktop probe, then asserts the outcome
# plus the observable side effects: Desktop launches, readiness-wait
# (timeout, engine_up_grace) arguments, and reconnect grace_wait values.
# grace_wait is True only where no readiness wait has run yet (the surgical
# pre-launch reconnect); after a wait, its engine-up grace has already been
# spent, so recovery goes straight to the proxy (grace_wait=False).
_FLOW_SCENARIOS = [
    {
        "id": "surgical pre-launch reconnect heals without starting Desktop",
        "pre_gap": True,
        "desktop_running": False,
        "answer": "y",
        "wait_ready": True,
        "gap_after_launch": False,
        "repair_ok": True,
        "gap_after_repair": False,
        "expected": True,
        "exp_popen": 0,
        "exp_waits": [],
        "exp_grace": [True],
    },
    {
        "id": "stuck Desktop: backend repair accepted",
        "pre_gap": False,
        "desktop_running": True,
        "answer": "y",
        "wait_ready": True,
        "gap_after_launch": False,
        "repair_ok": True,
        "gap_after_repair": False,
        "expected": True,
        "exp_popen": 0,
        "exp_waits": [],
        "exp_grace": [],
    },
    {
        "id": "stuck Desktop: repair leaves the engine gap, proxy reconnects",
        "pre_gap": False,
        "desktop_running": True,
        "answer": "y",
        "wait_ready": True,
        "gap_after_launch": False,
        "repair_ok": False,
        "gap_after_repair": True,
        "expected": True,
        "exp_popen": 0,
        "exp_waits": [],
        "exp_grace": [False],
    },
    {
        "id": "stuck Desktop: repair fails with no engine gap, guidance shown",
        "pre_gap": False,
        "desktop_running": True,
        "answer": "y",
        "wait_ready": True,
        "gap_after_launch": False,
        "repair_ok": False,
        "gap_after_repair": False,
        "expected": "handled",
        "exp_popen": 0,
        "exp_waits": [],
        "exp_grace": [],
    },
    {
        "id": "stuck Desktop: repair declined, guidance shown",
        "pre_gap": False,
        "desktop_running": True,
        "answer": "n",
        "wait_ready": True,
        "gap_after_launch": False,
        "repair_ok": True,
        "gap_after_repair": False,
        "expected": "handled",
        "exp_popen": 0,
        "exp_waits": [],
        "exp_grace": [],
    },
    {
        "id": "cold start ready within the wait",
        "pre_gap": False,
        "desktop_running": False,
        "answer": "y",
        "wait_ready": True,
        "gap_after_launch": False,
        "repair_ok": True,
        "gap_after_repair": False,
        "expected": True,
        "exp_popen": 1,
        "exp_waits": [(180, 120)],
        "exp_grace": [],
    },
    {
        "id": "slow cold start: wait expires with engine up, proxy reconnects",
        "pre_gap": False,
        "desktop_running": False,
        "answer": "y",
        "wait_ready": False,
        "gap_after_launch": True,
        "repair_ok": True,
        "gap_after_repair": False,
        "expected": True,
        "exp_popen": 1,
        "exp_waits": [(180, 120)],
        "exp_grace": [False],
    },
    {
        "id": "cold start fails and engine never appears",
        "pre_gap": False,
        "desktop_running": False,
        "answer": "y",
        "wait_ready": False,
        "gap_after_launch": False,
        "repair_ok": True,
        "gap_after_repair": False,
        "expected": False,
        "exp_popen": 1,
        "exp_waits": [(180, 120)],
        "exp_grace": [],
    },
    {
        "id": "user declines the Desktop start",
        "pre_gap": False,
        "desktop_running": False,
        "answer": "n",
        "wait_ready": True,
        "gap_after_launch": False,
        "repair_ok": True,
        "gap_after_repair": False,
        "expected": False,
        "exp_popen": 0,
        "exp_waits": [],
        "exp_grace": [],
    },
]


@pytest.mark.parametrize("scenario", _FLOW_SCENARIOS, ids=[str(s["id"]) for s in _FLOW_SCENARIOS])
def test_prompt_start_docker_wsl_flow(monkeypatch, scenario):
    """Each Docker/Desktop state resolves to the right outcome via the right recovery path."""
    counts = {"popen": 0, "repair": 0}
    waits: list[tuple[int, int]] = []
    grace: list[bool] = []
    state = {"launched": False, "repaired": False}

    def fake_engine_gap():
        if state["repaired"]:
            return scenario["gap_after_repair"]
        if state["launched"]:
            return scenario["gap_after_launch"]
        return scenario["pre_gap"]

    def fake_wait_for_ready(timeout_seconds=60, engine_up_grace=0):
        waits.append((timeout_seconds, engine_up_grace))
        return scenario["wait_ready"]

    def fake_reconnect(docker_exe, grace_wait):
        grace.append(grace_wait)
        return True

    def fake_repair():
        counts["repair"] += 1
        state["repaired"] = True
        return scenario["repair_ok"]

    class FakePopen:
        def __init__(self, *args, **kwargs):
            counts["popen"] += 1
            state["launched"] = True

    monkeypatch.setattr(_mod, "_clear_stale_proxy_if_any", lambda: None)
    monkeypatch.setattr(_mod, "_engine_up_but_socket_missing", fake_engine_gap)
    monkeypatch.setattr(_mod, "_wait_for_docker_ready", fake_wait_for_ready)
    monkeypatch.setattr(_mod, "_reconnect_engine_socket", fake_reconnect)
    monkeypatch.setattr(_mod, "_repair_docker_engine_wsl", fake_repair)
    monkeypatch.setattr(_mod, "is_docker_desktop_running", lambda: scenario["desktop_running"])
    monkeypatch.setattr(_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr("builtins.input", lambda prompt="": scenario["answer"])

    result = _mod._prompt_start_docker_wsl("/fake/Docker Desktop.exe")

    assert result == scenario["expected"]
    assert counts["popen"] == scenario["exp_popen"]
    assert waits == scenario["exp_waits"]
    assert grace == scenario["exp_grace"]
