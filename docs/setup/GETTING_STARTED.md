# Getting Started

The supported development path is **WSL2 + VS Code Remote WSL** on Windows,
or native on macOS/Linux. A Dev Container option is also available — see
[Dev Container (optional)](#dev-container-optional) below.

---

## TL;DR

```bash
git clone https://github.com/solentlabs/cable_modem_monitor.git
cd cable_modem_monitor
git lfs install      # required for HAR test fixtures
./scripts/setup.sh   # creates .venv and installs all dependencies
code .
```

When VS Code opens, run:

```bash
make validate
```

If anything in the setup fails: `./scripts/verify-setup.sh`.

---

## Prerequisites

| Tool | All platforms | Windows-specific |
|------|---------------|------------------|
| Git | required | required |
| [Git LFS](https://git-lfs.com/) | required (HAR fixtures) | required |
| Python 3.12 | required | required (install inside WSL2) |
| VS Code | required | required (installed on Windows side) |
| Docker Desktop | required for HA testing | WSL2 backend enabled |
| WSL2 + Ubuntu | — | required (`wsl --install` from PowerShell as admin) |

**Windows users:** clone the repo *inside* WSL2 (e.g. `~/projects/`),
not on the Windows filesystem (`/mnt/c/`). The 9P bridge is dramatically
slower and breaks file watchers.

---

## After opening in VS Code

| Notification | What to do |
|--------------|------------|
| "Install recommended extensions?" | Click **Install** — all required. |
| "GitLens" / "CodeQL" extras | Optional; dismiss if not needed. |

---

## Validation

```bash
make validate       # Ruff + Black + quick tests (~30s)
make validate-ci    # Local CI mirror: lint, types, tests, regression (2-5 min); run before push
make test           # All three test suites (Core, Catalog, HA)
```

Or use the Testing panel (beaker icon) — pytest tests auto-discover.
If they don't appear: refresh the panel, or check the Python
interpreter in the bottom-left status bar.

> CodeQL tests live in `cable-modem-monitor-ql/tests/` and don't appear
> in the Testing panel. Run them via GitHub Actions or `codeql test run`.

---

## Home Assistant container management

VS Code tasks (`Ctrl+Shift+P` → **Tasks: Run Task**):

| Task | Purpose |
|------|---------|
| **🚀 HA: Start** | Start HA with info logging (restarts if already running) |
| **🚀 HA: Start (Debug)** | Start HA with debug logging for all integration namespaces |
| **⏹️ HA: Stop** | Stop the HA container |
| **📋 HA: View Logs** | Tail HA logs in real time |
| **🗑️ HA: Clean All Data (Reset)** | Wipe HA's local test config (users, integrations, settings) |

After starting, open <http://localhost:8123>.

> **Code changes need a full HA restart, not an entry reload.** Run
> **🚀 HA: Start** (or `make docker-restart` from the terminal — same
> thing). The dev harness installs Core editable
> (`scripts/dev/ha-entrypoint.sh` runs `pip install -e`) and mounts
> `custom_components` live, so a running HA holds the modules it already
> imported. Reloading the integration entry re-runs setup on that stale
> code — only a restart re-imports your edits.
>
> **A changed `services.yaml` default won't appear in an action form you
> have already used.** Developer Tools → Actions saves your last-used
> values per action and replays them over the service definition, so the
> form keeps showing the old value no matter how many times you restart.
> Clear the saved data to see the new default: select the action, switch
> to YAML mode, delete the `data:` block, then switch back to UI mode.
>
> Two related symptoms come from the same cause. Removing a field from
> `services.yaml` while saved data still references it leaves HA unable
> to render the form in UI mode, so it silently falls back to YAML mode —
> which looks like a broken form. And every *optional* field draws as an
> inactive control until you tick it, so a greyed toggle means "not
> overriding the default," not "off"; mark a field `required: true` if
> its default should be visible in the form.

---

## Daily workflow

```bash
git checkout -b feature/my-change
# edit, run tests via Testing panel or `make test-quick`
make validate
git commit -am "..."
git push -u origin feature/my-change
```

Pre-commit hooks auto-format with Black and lint with Ruff. See
[CONTRIBUTING.md](../../CONTRIBUTING.md) for PR guidelines and commit
message conventions.

---

## Git worktrees

Worktrees share the main repo's `.venv` automatically. Every dev script
(`run-python.sh`, `run-pyright.sh`, `activate_venv.sh`, pre-commit
hooks) calls `scripts/dev/resolve-venv.sh`, which:

1. Looks for `.venv` in the current directory.
2. Falls back to the main worktree via `git rev-parse --git-common-dir`.

Create one anywhere convenient (a sibling directory, or your team's
preferred worktree location):

```bash
git worktree add ../my-feature feature/v3.14.0
cd ../my-feature
```

A `.venv` symlink is created on first use (gitignored). VS Code,
Pylance, and the test runner all work without setup.

---

## Windows / WSL2 setup

If you're on Windows and don't yet have WSL2:

```powershell
# PowerShell as Administrator
wsl --install
# Restart when prompted; Ubuntu launches automatically afterward.
```

In Ubuntu:

```bash
cat /etc/os-release      # confirm Ubuntu 22.04 or 24.04

# Install Docker (or use Docker Desktop with WSL2 backend)
sudo apt install docker.io
sudo usermod -aG docker $USER
exit                     # log back in to apply group

# Verify
docker run hello-world
```

Then clone the repo under `~/projects/` and run `code .` — VS Code
detects WSL2, installs Remote-WSL, and opens the folder. The
bottom-left status bar should read **"WSL: Ubuntu"**.

### WSL2 memory tuning

If WSL2 uses too much RAM, create `C:\Users\<username>\.wslconfig`:

```ini
[wsl2]
memory=8GB
processors=4
```

Then `wsl --shutdown`.

---

## Dev Container (optional)

A Dev Container configuration is available for contributors who prefer a
fully isolated environment. It requires Docker and the
[Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers).

```bash
# After cloning, open VS Code and click "Reopen in Container"
# First build takes 2–5 minutes; subsequent opens are instant.
```

The container installs all dependencies automatically via
`.devcontainer/post-create.sh`. If tools like `ruff` or `black` are missing
after a container rebuild, run the **Fix Dev Container** VS Code task.

---

## Troubleshooting

### Container won't start

```bash
docker ps                # is Docker running?
```

If not, start Docker Desktop. Then in VS Code: `F1` → **Dev Containers:
Rebuild Container**.

### Port 8123 already in use

```bash
docker ps
docker stop ha-cable-modem-test
```

Or run the **⏹️ HA: Stop** task.

### HAR files appear empty / `JSONDecodeError`

HAR fixtures are stored in Git LFS. If a file is ~130 bytes starting
with `version https://git-lfs.`, LFS hasn't fetched the content:

```bash
git lfs install   # once per machine
git lfs pull
```

### Pre-commit hooks failing

```bash
make format                          # auto-fix
make lint                            # see what's wrong
pre-commit install --install-hooks   # update hooks
```

### Permission denied on scripts (Linux/WSL2)

```bash
chmod +x scripts/*.sh scripts/dev/*.sh
```

### Docker permission denied

```bash
groups                               # should include 'docker'
sudo usermod -aG docker $USER
exit                                 # log back in
```

### Tests not appearing in Testing panel

1. Check Python interpreter in bottom-left status bar
   (should be `/usr/local/bin/python3.12` inside the container).
2. Refresh: Testing panel → refresh icon, or `F1` → **Python: Refresh
   Tests**.

---

## Getting help

- Integration-level issues: [TROUBLESHOOTING.md](../TROUBLESHOOTING.md)
- Workflow / PR guidelines: [CONTRIBUTING.md](../../CONTRIBUTING.md)
- Bug report: [open an issue](https://github.com/solentlabs/cable_modem_monitor/issues)
