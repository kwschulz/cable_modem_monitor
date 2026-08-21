#!/usr/bin/env python3
"""
Docker status checker for Linux/macOS.
Verifies Docker is installed and running before executing Docker commands.

Note: Windows users should run this inside WSL2. See docs/setup/GETTING_STARTED.md.
"""

import contextlib
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time

# Color codes
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
NC = "\033[0m"


def supports_unicode():
    """Check if the terminal supports Unicode output."""
    try:
        "\u2713".encode(sys.stdout.encoding or "utf-8")
        return True
    except (UnicodeEncodeError, AttributeError):
        return False


USE_UNICODE = supports_unicode()
CHECK = "\u2713" if USE_UNICODE else "OK"
CROSS = "\u2717" if USE_UNICODE else "X"
INFO = "\u2139" if USE_UNICODE else "i"


def print_error(message):
    """Print error message in red."""
    print(f"{RED}{CROSS}{NC} {message}", file=sys.stderr)


def print_success(message):
    """Print success message in green."""
    print(f"{GREEN}{CHECK}{NC} {message}")


def print_info(message):
    """Print info message in cyan."""
    print(f"{CYAN}{INFO}{NC} {message}")


def is_running_in_container():
    """Check if we're running inside a Docker container."""
    # Check for .dockerenv file (Linux containers)
    if os.path.exists("/.dockerenv"):
        return True
    # Check for container environment variables
    if os.environ.get("REMOTE_CONTAINERS") or os.environ.get("CODESPACES"):
        return True
    # Check cgroup for docker/container indicators (Linux only)
    try:
        with open("/proc/1/cgroup") as f:
            content = f.read()
            return "docker" in content or "container" in content
    except (FileNotFoundError, PermissionError):
        pass
    return False


def cleanup_stale_docker_files():
    """Remove stale PID and socket files from crashed docker.

    Note: This function uses Linux-specific paths and commands.
    It's only called when running inside a container (always Linux).
    """
    stale_files = [
        "/var/run/docker.pid",
        "/var/run/docker.sock",
        "/var/run/containerd/containerd.pid",
    ]
    for f in stale_files:
        with contextlib.suppress(FileNotFoundError, PermissionError):
            os.remove(f)

    # Kill any zombie docker processes
    subprocess.run(["pkill", "-9", "dockerd"], capture_output=True)
    subprocess.run(["pkill", "-9", "containerd"], capture_output=True)
    time.sleep(1)


def try_start_docker_in_docker():
    """Try to start the Docker daemon inside a container (docker-in-docker).

    Note: This function uses Linux-specific paths and commands.
    It's only called when running inside a container (always Linux).
    """
    print_info("Attempting to start Docker daemon inside container...")
    print("")

    # Clean up stale files from previous crashed docker
    cleanup_stale_docker_files()

    # Common locations for docker-in-docker startup scripts
    startup_commands = [
        # devcontainers docker-in-docker feature uses this
        ["/usr/local/share/docker-init.sh"],
        # Alternative: start dockerd directly
        ["dockerd", "--host=unix:///var/run/docker.sock"],
        # Some setups use service
        ["service", "docker", "start"],
    ]

    for cmd in startup_commands:
        # Check if command exists (which() for PATH, exists() for absolute paths)
        cmd_path = cmd[0]
        if not (shutil.which(cmd_path) or os.path.exists(cmd_path)):
            continue
        try:
            # docker-init.sh and dockerd run forever, so background them
            if cmd_path in ("dockerd", "/usr/local/share/docker-init.sh"):
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Wait for daemon to start (can take 20-30s on slow systems)
                print("   Waiting for Docker daemon", end="", flush=True)
                for _ in range(30):
                    time.sleep(1)
                    print(".", end="", flush=True)
                    result = subprocess.run(
                        ["docker", "info"],
                        capture_output=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        print()
                        return True
                print()
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    # Verify docker is now working
                    check = subprocess.run(
                        ["docker", "info"],
                        capture_output=True,
                        timeout=10,
                    )
                    if check.returncode == 0:
                        return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue

    return False


def get_docker_install_instructions(system):
    """Get platform-specific Docker installation instructions."""
    if system == "Darwin":
        return [
            "  Download: https://desktop.docker.com/mac/main/amd64/Docker.dmg",
            "",
            "  Or install via Homebrew:",
            f"    {CYAN}brew install --cask docker{NC}",
        ]
    # Linux
    return [
        "  Ubuntu/Debian: sudo apt install docker.io",
        "  Fedora/RHEL: sudo dnf install docker",
        "  Or download Docker Desktop: https://docs.docker.com/desktop/install/linux-install/",
    ]


def _install_docker_macos():
    """Install Docker Desktop on macOS using Homebrew."""
    print("")
    print("Installing Docker Desktop via Homebrew...")
    print("")
    result = subprocess.run(["brew", "install", "--cask", "docker"], check=False)
    if result.returncode == 0:
        print("")
        print(f"{GREEN}{CHECK}{NC} Docker Desktop installed!")
        print(f"{YELLOW}Please start Docker Desktop from Applications.{NC}")
        print("")


def prompt_docker_install(system):
    """Prompt user to install Docker and launch installer if confirmed."""
    if system != "Darwin":
        return False

    print("")
    print(f"{CYAN}Would you like to install Docker Desktop now? (uses Homebrew){NC}")
    print("")
    try:
        response = input("Install Docker Desktop? [y/N]: ").strip().lower()
        if response in ("y", "yes"):
            _install_docker_macos()
            return True
    except (EOFError, KeyboardInterrupt):
        print("")
    except FileNotFoundError:
        print(f"{YELLOW}Homebrew not found. Please install Docker Desktop manually.{NC}")
        print("")
    return False


def get_docker_start_instructions(system):
    """Get platform-specific instructions for starting Docker."""
    if system == "Darwin":
        return [
            "macOS:",
            "  1. Open Applications folder",
            "  2. Launch Docker Desktop",
            "  3. Wait for the Docker whale icon in menu bar to show 'Running'",
        ]
    # Linux
    return [
        "Linux:",
        "  Option 1 (Docker Desktop):",
        "    - Launch Docker Desktop application",
        "  Option 2 (Docker Engine):",
        "    - sudo systemctl start docker",
        "    - Or: sudo service docker start",
    ]


def is_wsl():
    """Check if running inside WSL (Windows Subsystem for Linux)."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except (FileNotFoundError, PermissionError):
        return False


def find_docker_desktop_exe():
    """Find Docker Desktop executable path in WSL."""
    # Common installation paths
    paths = [
        "/mnt/c/Program Files/Docker/Docker/Docker Desktop.exe",
        "/mnt/c/Program Files (x86)/Docker/Docker/Docker Desktop.exe",
    ]
    for path in paths:
        if os.path.exists(path):
            return path
    return None


def is_docker_desktop_running():
    """Check if Docker Desktop is already running as a Windows process."""
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq Docker Desktop.exe"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "Docker Desktop.exe" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _wait_for_docker_ready(timeout_seconds=60, engine_up_grace=0):
    """Poll for Docker to become ready. True if ready within the deadline."""
    print("  Waiting for Docker to be ready", end="", flush=True)

    # Socket provisioning starts when the engine comes up, not when this
    # wait starts. With engine_up_grace, the first sighting of the engine
    # socket without this distro's docker.sock extends the deadline so
    # provisioning gets at least that long from engine-up. docker info
    # keeps being polled throughout: an engine that appears early keeps
    # the full original budget, and setups that reach the daemon some
    # other way (DOCKER_HOST, a non-default context) still succeed.
    deadline = timeout_seconds
    elapsed = 0
    engine_seen = False
    while elapsed < deadline:
        time.sleep(1)
        elapsed += 1
        print(".", end="", flush=True)
        if engine_up_grace and not engine_seen and _engine_up_but_socket_missing():
            engine_seen = True
            deadline = max(deadline, elapsed + engine_up_grace)
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                print("")
                print(f"{GREEN}{CHECK}{NC} Docker is ready!")
                print("")
                return True
        except subprocess.TimeoutExpired:
            continue

    # Timeout - Docker didn't become ready
    print("")
    print(f"  {YELLOW}Docker Desktop is still starting...{NC}")
    print("  Please wait a moment and try your command again.")
    print("")
    return False


# Docker Desktop's WSL integration: the engine runs in the `docker-desktop`
# distro and exposes this socket; a per-distro proxy then creates
# /var/run/docker.sock inside each integrated distro.
ENGINE_SOCK = "/mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock"
DISTRO_SOCK = "/var/run/docker.sock"
PROXY_BIN = "/mnt/wsl/docker-desktop/docker-desktop-user-distro"


def _engine_up_but_socket_missing():
    """True when Docker's engine is up but this distro's docker.sock is absent."""
    return os.path.exists(ENGINE_SOCK) and not os.path.exists(DISTRO_SOCK)


def _find_orphaned_proxy_pids():
    """PIDs of distro proxies still running after the engine went away."""
    if os.path.exists(ENGINE_SOCK):
        return []
    try:
        result = subprocess.run(
            ["pgrep", "-f", "docker-desktop-user-distro proxy"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    return [pid for pid in result.stdout.split() if pid.isdigit()]


def _cleanup_stale_proxy(pids):
    """Kill orphaned distro proxies and remove the dead socket they hold.

    A proxy launched by an earlier recovery outlives Docker Desktop by design
    (setsid, detached). Once Desktop exits, the proxy's upstream engine socket
    is gone but its /var/run/docker.sock remains — every docker command then
    hangs on the dead socket, and the leftover can block Desktop's next
    provisioning. Both proxy and socket are root-owned, so this goes through
    sudo. PIDs are collected beforehand and killed by number — a pkill -f
    inside the sudo shell would match its own command line.
    """
    print("  A leftover Docker proxy from a previous session is holding a")
    print("  dead /var/run/docker.sock; docker commands hang on it.")
    print("  Cleaning it up (may prompt for sudo)...")
    inner = "kill " + " ".join(pids) + " 2>/dev/null; rm -f " + shlex.quote(DISTRO_SOCK)
    try:
        subprocess.run(["sudo", "bash", "-c", inner], check=False)
    except (FileNotFoundError, OSError):
        return False
    if os.path.exists(DISTRO_SOCK):
        return False
    print_success("Stale proxy cleared")
    print("")
    return True


def _to_windows_path(linux_path):
    """Convert a /mnt/<drive>/... WSL path to its Windows form, or None."""
    parts = linux_path.split("/")
    if len(parts) >= 3 and parts[1] == "mnt" and len(parts[2]) == 1:
        return "\\".join([parts[2].upper() + ":"] + parts[3:])
    return None


def _wait_for_socket(timeout_seconds=15):
    """Poll for /var/run/docker.sock, then confirm the daemon answers."""
    print("  Waiting for Docker socket", end="", flush=True)
    for _ in range(timeout_seconds):
        time.sleep(1)
        print(".", end="", flush=True)
        if not os.path.exists(DISTRO_SOCK):
            continue
        try:
            result = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            print("")
            print(f"{GREEN}{CHECK}{NC} Docker is ready!")
            print("")
            return True
    print("")
    return False


def _start_distro_proxy(docker_exe):
    """Recreate /var/run/docker.sock by launching Docker's per-distro proxy.

    The engine is already up; only this distro's socket is missing — either a
    Docker Desktop update wiped the WSL-integration config, or provisioning
    never completed within the grace period.
    Starting the proxy directly is far less disruptive than restarting the
    backend distro. The proxy binds a socket under root-owned /var/run and runs
    for the life of the session, so it must go through sudo and stay detached.
    """
    if not os.access(PROXY_BIN, os.X_OK):
        return False
    # The proxy requires Docker Desktop's Windows resources dir as a positional
    # argument; it sits beside the Docker Desktop.exe we already located.
    resources_linux = os.path.join(os.path.dirname(docker_exe), "resources")
    resources_win = _to_windows_path(resources_linux)
    if not os.path.isdir(resources_linux) or resources_win is None:
        return False

    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
    # Mirror Docker Desktop's own invocation. setsid + background keeps the
    # long-lived proxy alive after this script exits; sudo runs on the terminal
    # so it can prompt for a password if one is needed.
    inner = (
        "setsid "
        + shlex.quote(PROXY_BIN)
        + " proxy"
        + " --distro-name "
        + shlex.quote(distro)
        + " --docker-desktop-root /mnt/wsl/docker-desktop "
        + shlex.quote(resources_win)
        + " >/tmp/docker-distro-proxy.log 2>&1 </dev/null &"
    )
    print("")
    print("  Reconnecting Docker to this WSL distro...")
    print("  (sudo: the proxy binds a socket in root-owned /var/run)")
    try:
        subprocess.run(["sudo", "bash", "-c", inner], check=False)
    except (FileNotFoundError, OSError):
        return False
    return _wait_for_socket()


def _repair_docker_engine_wsl():
    """Restart only Docker's backend distro to re-attach the engine socket.

    Returns True if Docker becomes ready. Terminating the `docker-desktop`
    distro is safe from inside another distro: it leaves this session alive
    and lets the running Docker Desktop re-provision the engine, which
    recreates /var/run/docker.sock. We deliberately do NOT kill the Docker
    Desktop GUI process, since force-killing it can tear down a working
    engine without re-attaching the socket.
    """
    print("")
    print("  Restarting Docker's backend distro...", end="", flush=True)
    result = subprocess.run(
        ["wsl.exe", "--terminate", "docker-desktop"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(" failed")
        return False
    print(" done")
    # The re-provisioned engine can come up well before this distro's
    # socket does; grant the same engine-up grace as the cold-start path
    # instead of burning the whole wait on guaranteed-failing docker info.
    return _wait_for_docker_ready(60, engine_up_grace=120)


def _reconnect_engine_socket(docker_exe, grace_wait):
    """Reconnect this distro's socket to an already-up engine. True if ready.

    With grace_wait, first give a running Docker Desktop time to provision
    the socket itself — the same socket state occurs mid-startup on a cold
    boot, and waiting avoids the sudo proxy (and its password prompt) when
    Desktop is about to finish on its own.
    """
    print("  Docker's engine is up but not yet connected to this WSL distro.")
    if grace_wait and is_docker_desktop_running():
        print("  Docker Desktop is running and may still be provisioning it.")
        # Cold-boot provisioning routinely runs well past 30s. Match the
        # patience already granted to the engine so Desktop can finish on
        # its own instead of falling through to the sudo proxy.
        if _wait_for_socket(120):
            return True
    print("  Desktop hasn't provisioned it — starting the proxy directly.")
    return _start_distro_proxy(docker_exe)


def _clear_stale_proxy_if_any():
    """Detect and clear an orphaned distro proxy holding a dead socket.

    Inverse of the engine-up-socket-missing failure: this distro still has a
    docker.sock but the engine behind it is gone — the signature of a proxy
    left over from an earlier recovery after Docker Desktop exited. Clearing
    it stops docker commands hanging and lets Desktop re-provision cleanly.
    Only fires when an orphaned proxy process is confirmed — a plain Docker
    Engine socket is left alone.
    """
    if os.path.exists(DISTRO_SOCK) and not os.path.exists(ENGINE_SOCK):
        pids = _find_orphaned_proxy_pids()
        if pids:
            _cleanup_stale_proxy(pids)


def _repair_stuck_desktop(docker_exe):
    """Repair a running-but-unresponsive Desktop. True if ready, else "handled"."""
    print(f"  {YELLOW}Docker Desktop is running but not responding.{NC}")
    print("")
    print("  This usually means the engine did not re-attach")
    print("  /var/run/docker.sock to this distro. It commonly happens")
    print("  after a Docker Desktop update. Restarting Docker's backend")
    print("  distro re-provisions the engine and recreates the socket.")
    print("")
    try:
        response = input("  Restart Docker's backend to repair it? [Y/n]: ").strip().lower()
        if response in ("", "y", "yes"):
            if _repair_docker_engine_wsl():
                return True
            # The restart can leave the engine up with this distro's
            # socket still missing; the proxy reconnect is the surgical
            # fix for exactly that state, so try it before the heavy
            # full-reset guidance. The repair wait already granted the
            # provisioning grace from engine-up, so skip a second one.
            if _engine_up_but_socket_missing() and _reconnect_engine_socket(docker_exe, grace_wait=False):
                return True
    except (EOFError, KeyboardInterrupt):
        print("")

    # Repair declined or did not take. Fall back to the full WSL restart,
    # which the script cannot run itself without killing this distro.
    print("")
    print("  If that did not fix it, do a full reset from a Windows")
    print("  PowerShell or CMD prompt:")
    print("")
    print("    1. wsl --shutdown")
    print("       (restarts all WSL distros, including this one)")
    print("    2. Fully quit Docker Desktop from the system tray, then")
    print("       relaunch it and wait for 'Running'")
    print("    3. If it still fails: Docker Desktop > Settings >")
    print("       Resources > WSL Integration > enable this distro >")
    print("       Apply & Restart")
    print("")
    print("  Then re-open your terminal and try the command again.")
    print("")
    # Targeted guidance already shown; tell the caller to skip the
    # generic "start Docker Desktop" trailer (it is already running).
    return "handled"


def _prompt_start_docker_wsl(docker_exe):
    """Start/repair Docker for WSL. True if ready, "handled" if guidance shown, else False."""
    # Clear a stale proxy first, then continue into the start/repair flow.
    _clear_stale_proxy_if_any()

    # Most common post-update failure: the engine is up but this distro's
    # /var/run/docker.sock was never recreated. Reconnect it by starting the
    # per-distro proxy directly — the surgical fix, far cheaper than restarting
    # the backend distro. No prompt: this is meant to self-heal.
    # Falls through to the heavier recovery paths if the reconnect fails.
    if _engine_up_but_socket_missing() and _reconnect_engine_socket(docker_exe, grace_wait=True):
        return True

    # Docker Desktop already running but not responding gets the repair flow.
    if is_docker_desktop_running():
        return _repair_stuck_desktop(docker_exe)

    print(f"  {CYAN}Would you like to start Docker Desktop now?{NC}")
    print("")
    try:
        response = input("  Start Docker Desktop? [Y/n]: ").strip().lower()
        if response in ("", "y", "yes"):
            print("")
            print("  Starting Docker Desktop...", end="", flush=True)
            subprocess.Popen(
                [docker_exe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(" started")
            # A cold start (first launch after reboot) has been observed to
            # need more than 120s before the engine answers. The engine-up
            # grace lets a late engine finish provisioning without stacking
            # a second full wait after this one.
            if _wait_for_docker_ready(180, engine_up_grace=120):
                return True
            if _engine_up_but_socket_missing():
                # The wait already granted provisioning its grace from
                # engine-up; go straight to the proxy reconnect.
                return _reconnect_engine_socket(docker_exe, grace_wait=False)
            return False
    except (EOFError, KeyboardInterrupt):
        print("")
    return False


def prompt_start_docker(system):
    """Prompt user to start Docker Desktop automatically."""
    if system == "Darwin":
        print(f"  {CYAN}Would you like to start Docker Desktop now?{NC}")
        print("")
        try:
            response = input("  Start Docker Desktop? [Y/n]: ").strip().lower()
            if response in ("", "y", "yes"):
                print("")
                print("  Starting Docker Desktop...", end="", flush=True)
                subprocess.Popen(
                    ["open", "-a", "Docker"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(" started")
                # Cold starts regularly need more than 60s (see WSL path).
                return _wait_for_docker_ready(120)
        except (EOFError, KeyboardInterrupt):
            print("")
    elif system == "Linux" and is_wsl():
        docker_exe = find_docker_desktop_exe()
        if docker_exe:
            return _prompt_start_docker_wsl(docker_exe)
    return False


def check_docker_installed(system):
    """Check if Docker is installed. Returns True if installed."""
    if shutil.which("docker"):
        return True

    print(f"{RED}{CROSS}{NC} Docker is not installed")
    print("")
    print("  Please install Docker Desktop:")
    print("")
    for line in get_docker_install_instructions(system):
        print(f"  {line}")
    print("")

    # Offer to install Docker automatically
    prompt_docker_install(system)
    return False


def _handle_docker_timeout(system):
    """Recover from a hung `docker info`. Returns 0 if Docker is ready, 1 otherwise.

    A hang (rather than a fast connection error) is the signature of a dead
    socket — typically a stale distro proxy — so run the same recovery flow
    as a failed check instead of punting.
    """
    if is_running_in_container():
        print("  Docker may be starting up. Please wait a moment and try again.")
        print("")
        return 1
    outcome = prompt_start_docker(system)
    if outcome is True:
        return 0
    if outcome != "handled":
        print("  Docker may be starting up. Please wait a moment and try again.")
        print("")
    return 1


def check_docker_running(system):
    """Check if Docker daemon is running. Returns 0 if running, 1 otherwise."""
    # Show checking status
    print("  Checking Docker...", end="", flush=True)

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            # Clear the "Checking..." line and show success
            print(f"\r{GREEN}{CHECK}{NC} Docker is running       ")
            return 0

        # Docker command failed
        raise subprocess.CalledProcessError(result.returncode, "docker info")

    except subprocess.TimeoutExpired:
        print(f"\r{RED}{CROSS}{NC} Docker command timed out")
        print("")
        return _handle_docker_timeout(system)

    except subprocess.CalledProcessError:
        print(f"\r{RED}{CROSS}{NC} Docker is not ready     ")
        print("")

        # Different guidance for container vs host environment
        if is_running_in_container():
            # Try to start docker-in-docker daemon
            if try_start_docker_in_docker():
                print_success("Docker daemon started inside container")
                return 0
            print("  You're running inside a Dev Container.")
            print("")
            print("  The Docker daemon inside the container failed to start.")
            print("  Try rebuilding the container:")
            print("")
            print(f"    {CYAN}Ctrl+Shift+P → 'Dev Containers: Rebuild Container'{NC}")
            print("")
            return 1

        # Try to start Docker Desktop
        outcome = prompt_start_docker(system)
        if outcome is True:
            return 0  # Docker is now running

        # The WSL "running but not responding" path prints its own targeted
        # guidance; only show the generic start instructions otherwise.
        if outcome != "handled":
            print("  Please start Docker Desktop:")
            print("")
            for line in get_docker_start_instructions(system):
                print(f"  {line}")
            print("")
            print("  Then try your command again.")
            print("")
        return 1

    except FileNotFoundError:
        print(f"\r{RED}{CROSS}{NC} Docker command not found in PATH")
        print("")
        return 1


def check_docker():
    """Check if Docker is installed and running.

    Returns 0 if Docker is ready, 1 otherwise.
    """
    system = platform.system()

    # Check if Docker is installed
    if not check_docker_installed(system):
        return 1

    # Check if Docker daemon is running
    return check_docker_running(system)


def print_separator():
    """Print a visual separator with timestamp for VSCode task panel."""
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"─── {timestamp} ───────────────────────────────────────────────")


def clear_screen():
    """Clear the terminal screen."""
    # ANSI escape sequence works on Linux/macOS/WSL2
    print("\033[2J\033[H", end="", flush=True)


def main():
    """Main entry point."""
    clear_screen()
    print_separator()
    sys.exit(check_docker())


if __name__ == "__main__":
    main()
