"""daemon_install.py — launchd auto-start installer for the flightdeck daemon.

Optional: the daemon itself never installs this. ``flightdeck daemon install``
must be run EXPLICITLY by the operator and gated by ``--apply`` — it changes
what auto-starts at login on this machine, which is exactly the kind of
persistent-system change that needs a human's yes first (even though the daemon
it installs is read-only).

The installed plist runs the daemon's loop in the FOREGROUND
(``flightdeck daemon --start-daemon`` internal path) so launchd supervises it
directly; it never forks a child that would leave launchd tracking the wrong
process. This mirrors ``hscc_daemon/install.py``.

This module has NO ``build_subparser``/``run`` hooks, so cli.py's auto-discovery
does not treat it as a command — it is a helper pulled in only by
``daemon install`` / ``daemon uninstall``.

NOTE: In the test suite and manual verification for this card, ``install`` is
NOT run — doing so would write ``~/Library/LaunchAgents/...`` and call
``launchctl`` on the operator's machine. The generation logic is unit-tested;
the side-effecting install is gated behind ``--apply``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..core import daemon as d

PLIST_LABEL = "com.flightdeck.daemon"

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.flightdeck.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{PYBIN}</string>
        <string>-m</string>
        <string>flightdeck.cli</string>
        <string>daemon</string>
        <string>--start-daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{HOME}</string>
    <key>StandardOutPath</key>
    <string>{LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>{LOG_FILE}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{PATH_ENV}</string>
        <key>HOME</key>
        <string>{HOME}</string>
        <key>PYTHONDONTWRITEBYTECODE</key>
        <string>1</string>
    </dict>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>ExitTimeOut</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def _resolve_python() -> str:
    """Resolve a real python interpreter for the service unit (like hscc)."""
    home = os.path.expanduser("~")
    candidates = [
        os.environ.get("FLIGHTDECK_PYBIN", ""),
        os.path.join(home, ".hermes/hermes-agent/venv/bin/python"),
        shutil.which("python3") or "",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "/usr/bin/python3"  # last-resort default


def _path_env(home: str) -> str:
    """A minimal PATH for the supervised daemon."""
    return (
        os.path.join(home, ".hermes/hermes-agent/venv/bin")
        + ":" + os.path.join(home, ".local/bin")
        + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )


def plist_dir() -> str:
    return os.path.expanduser("~/Library/LaunchAgents")


def plist_file() -> str:
    return os.path.join(plist_dir(), f"{PLIST_LABEL}.plist")


def generate_plist() -> str:
    """The launchd plist with resolved absolute paths (never ``~``)."""
    home = os.path.expanduser("~")
    return _PLIST_TEMPLATE.format(
        PYBIN=_resolve_python(),
        HOME=home,
        LOG_FILE=d.LOG_FILE,
        PATH_ENV=_path_env(home),
    )


def _stop_running_daemon() -> None:
    """Stop any running daemon instance (shared by install/uninstall)."""
    pid = d.get_pid()
    if pid is not None:
        print(f"  Stopping running daemon (PID {pid})")
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
        except OSError:
            pass
        d.write_stopped()


def cmd_install(args: argparse.Namespace, registry_path: str) -> int:
    """Install the launchd auto-start service (requires ``--apply``)."""
    if not getattr(args, "apply", False):
        print("dry-run: `flightdeck daemon install` would write a launchd plist")
        print(f"  to {plist_file()} and `launchctl load` it (auto-start at login).")
        print("  Pass --apply to perform the install.")
        return 0
    if sys.platform != "darwin":
        print(f"error: launchd install is macOS-only (this host: {sys.platform})")
        return 2

    _stop_running_daemon()
    target = Path(plist_file())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generate_plist())
    print(f"  Plist installed: {target}")
    try:
        cp = subprocess.run(
            ["launchctl", "load", str(target)],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0:
            print("  Loaded into launchd")
        else:
            print(f"  launchctl load returned {cp.returncode}: {cp.stderr.strip()}")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  launchctl load failed: {exc}")
    print("\n  flightdeck daemon is now managed by launchd (auto-start at login).")
    print(f"  Check: launchctl list | grep {PLIST_LABEL}")
    return 0


def cmd_uninstall(args: argparse.Namespace, registry_path: str) -> int:
    """Remove the launchd auto-start service."""
    _stop_running_daemon()
    target = Path(plist_file())
    if not target.exists():
        print("  No plist found — nothing to remove")
        return 0
    try:
        subprocess.run(
            ["launchctl", "unload", str(target)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  launchctl unload failed: {exc}")
    target.unlink()
    print(f"  Plist removed: {target}")
    print("  flightdeck daemon uninstalled (no auto-start at login)")
    return 0
