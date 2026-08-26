---
name: mock-server
description: Replay a catalog modem's HAR against the mock server. Two modes — an automated pipeline check (auth, parse, golden comparison) and a persistent server a local Home Assistant can point at to inspect the device page and entities.
---

<!-- Master copy: skills/mock-server.md — edit there, not in .claude/skills/ -->

# Mock Server Skill

> **Invocation note**: Project-local skills in `skills/` are not registered as Skill tool
> targets — `Skill("mock-server")` will return "Unknown skill". Read this file and
> execute the steps directly. This is a Claude Code limitation, not a config gap.

`HARMockServer` replays HAR-captured responses with real auth simulation.
It speaks the same protocols as production (none, basic, form, form_nonce,
form_sjcl, url_token, HNAP), so crypto strategies do a genuine PBKDF2 and
AES round-trip rather than a stub.

**Pick the mode from what is being asked.** "Does this modem still work"
is Mode 1. Anything that means *look at it* — a device page, an entity
list, a config flow — is Mode 2. Mode 1 cannot answer Mode 2 questions:
its server lives on a random port inside the pytest process and dies with
the test, so there is nothing to point a browser or HA at.

## Mode 1 — Automated pipeline check

Runs auth, resource loading, parsing, orchestration and golden-file
comparison for one catalog modem.

```bash
.venv/bin/python -m pytest packages/cable_modem_monitor_catalog/tests/test_modems.py \
    -v -k "{manufacturer}/{model}" --no-header --tb=long
```

Three tests per modem: `test_modem_yaml_schema`, `test_modem_har_replay`
(the full orchestrated cycle), `test_modem_golden_spec_conformance`.

**On failure**, the harness writes `modem.actual.json` beside the golden.
Diff it against `modem.expected.json` and report which fields drifted and
whether the actual output looks correct — the golden may be the wrong one.

Report: auth strategy (from modem.yaml), channel counts, system_info
fields extracted, golden match, and how many test variants ran. Some
modems carry more than one HAR in `test_data/`; discovery picks all of
them up, so confirm the count rather than assuming one.

## Mode 2 — Persistent server for manual inspection

Start a server that blocks until Ctrl+C and point the local HA harness at
it. This is how you verify anything that only exists in the UI: device
card fields, entity naming, config-flow behaviour.

```bash
.venv/bin/python -m solentlabs.cable_modem_monitor_core.test_harness \
    packages/cable_modem_monitor_catalog/solentlabs/cable_modem_monitor_catalog/modems/{manufacturer}/{model} \
    --host 0.0.0.0 --port 8080
```

Flags: `--host` (default `0.0.0.0`), `--port` (default `8080`),
`--har` to pick a specific HAR when `test_data/` holds several,
`--log-level`.

Bind `0.0.0.0`, not localhost — HA runs in a container and its loopback
is its own. Give HA the WSL host address from `hostname -I`, not
`127.0.0.1`. Credentials are `admin` / `pw`; the mock server accepts
those, and no real credential ever belongs here.

**Confirm it is up with `ss -ltn | grep :8080` plus a curl at root, not by
reading the log.** The startup banner is buffered and may never reach the
redirect target, so an empty log file says nothing about whether the
server started. A `401` at root is a healthy answer for an auth'd modem.

Stop the server when the inspection is done; do not leave a port bound.

Ken drives the HA container. Stay repo-side: start the server, hand over
the address and credentials, and report what to look for.

## Notes

- No external network access is needed; everything is localhost.
- If a catalog entry is missing `modem.yaml`, `parser.yaml`,
  `test_data/modem.har`, or `test_data/modem.expected.json`, report which
  files are absent and stop rather than improvising a fixture.
