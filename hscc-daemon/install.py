"""Install/uninstall/service management for the HSCC daemon."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


HSCC_DIR = os.path.expanduser("~/.hscc")
PID_FILE = os.path.join(HSCC_DIR, "daemon.pid")
LOG_FILE = os.path.join(HSCC_DIR, "daemon.log")
PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")
PLIST_FILE = os.path.join(PLIST_DIR, "com.hermes.hscc-daemon.plist")
SYSTEMD_USER_DIR = Path(os.path.expanduser("~/.config/systemd/user"))
SYSTEMD_UNIT_FILE = SYSTEMD_USER_DIR / "com.hermes.hscc-daemon.service"
SYSTEMD_UNIT_NAME = "com.hermes.hscc-daemon.service"

PLIST_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.hscc-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{PYTHON_PATH}</string>
        <string>{SCRIPT_PATH}</string>
        <string>start-daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{HOMEDIR}</string>
    <key>StandardOutPath</key>
    <string>{HOMEDIR}/.hscc/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>{HOMEDIR}/.hscc/daemon.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{PATH_ENV}</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>WatchPaths</key>
    <array>
        <string>{HOMEDIR}/.hscc/events.jsonl</string>
    </array>
    <key>ExitTimeOut</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
"""

SYSTEMD_UNIT_CONTENT = """[Unit]
Description=HSCC Monitoring Daemon (Hermes Spark Cluster Control)
After=network.target

[Service]
Type=simple
ExecStart={PYTHON_PATH} {SCRIPT_PATH} start-daemon
WorkingDirectory={HOMEDIR}
Environment=PATH={PATH_ENV}
Restart=on-failure
RestartSec=30
StandardOutput=append:{HOMEDIR}/.hscc/daemon.log
StandardError=append:{HOMEDIR}/.hscc/daemon.log

[Install]
WantedBy=default.target
"""


def _daemon_path_env(hmdir):
    """Minimal PATH for the supervised daemon."""
    return (os.path.join(hmdir, ".hermes/hermes-agent/venv/bin")
            + ":" + os.path.join(hmdir, ".local/bin")
            + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")


def _service_manager():
    """Pick the auto-start mechanism for this host."""
    if sys.platform == "darwin":
        return "launchd"
    if shutil.which("systemctl"):
        return "systemd"
    return "none"


def _stop_running_daemon():
    """Stop any running daemon instance (shared by install/uninstall)."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        print(f"  Stopping existing daemon (PID {pid})")
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
        except OSError:
            pass
        _write_stopped()
    except (FileNotFoundError, ValueError):
        pass


def _write_stopped():
    """Remove PID file."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def generate_plist():
    """Generate the Launchd plist with resolved paths."""
    hmdir = os.path.expanduser("~")
    python_path = shutil.which("python3") or "/usr/bin/python3"
    return PLIST_CONTENT.format(
        PYTHON_PATH=python_path,
        SCRIPT_PATH=os.path.abspath(__file__),
        HOMEDIR=hmdir,
        PATH_ENV=_daemon_path_env(hmdir),
    )


def generate_systemd_unit():
    """Generate the systemd --user unit with resolved paths."""
    hmdir = os.path.expanduser("~")
    python_path = shutil.which("python3") or "/usr/bin/python3"
    return SYSTEMD_UNIT_CONTENT.format(
        PYTHON_PATH=python_path,
        SCRIPT_PATH=os.path.abspath(__file__),
        HOMEDIR=hmdir,
        PATH_ENV=_daemon_path_env(hmdir),
    )


def _install_launchd():
    Path(PLIST_DIR).mkdir(parents=True, exist_ok=True)
    plist_file = Path(PLIST_FILE)
    plist_file.write_text(generate_plist())
    print(f"  Plist installed: {plist_file}")

    result = subprocess.run(
        ["launchctl", "load", str(plist_file)],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        print("  Loaded into launchd")
        print(f"\n  hscc-daemon is now managed by launchd.")
        print(f"  To check status: launchctl list | grep hscc")
    else:
        print(f"  launchctl load failed, starting manually...")
        # Call the actual start function (imported later)
        print(f"  To load on next boot: launchctl load {plist_file}")


def _install_systemd():
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    unit_file = Path(SYSTEMD_UNIT_FILE)
    unit_file.write_text(generate_systemd_unit())
    print(f"  Unit installed: {unit_file}")

    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, text=True, timeout=10)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0:
        print("  Enabled + started via systemd --user")
        print(f"\n  hscc-daemon is now managed by systemd.")
        print(f"  To check status: systemctl --user status {SYSTEMD_UNIT_NAME}")
        print(f"  Tip: run `loginctl enable-linger $USER` so it survives logout.")
    else:
        print(f"  systemctl enable failed ({result.stderr.strip()}), starting manually...")
        print(f"  To enable later: systemctl --user enable --now {SYSTEMD_UNIT_NAME}")


def _uninstall_launchd():
    plist_file = Path(PLIST_FILE)
    if plist_file.exists():
        subprocess.run(["launchctl", "unload", str(plist_file)],
                       capture_output=True, text=True, timeout=10)
        plist_file.unlink()
        print(f"  Plist removed: {plist_file}")
        print("  hscc-daemon uninstalled")
    else:
        print("  No plist found — nothing to remove")


def _uninstall_systemd():
    subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME],
                   capture_output=True, text=True, timeout=15)
    if Path(SYSTEMD_UNIT_FILE).exists():
        Path(SYSTEMD_UNIT_FILE).unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, text=True, timeout=10)
        print(f"  Unit removed: {SYSTEMD_UNIT_FILE}")
        print("  hscc-daemon uninstalled")
    else:
        print("  No unit found — nothing to remove")


# ── CLI Command Wrappers ──────────────────────────────────────────────────

def cmd_plist():
    """Generate and display the auto-start service definition."""
    from .daemon_ops import get_pid, save_pid, write_stopped, run_daemon_loop
    mgr = _service_manager()
    if mgr == "systemd":
        unit = generate_systemd_unit()
        print(unit)
        print(f"\n# To install: write to {SYSTEMD_UNIT_FILE}")
        print(f"#   systemctl --user daemon-reload && systemctl --user enable --now {SYSTEMD_UNIT_NAME}")
        return
    plist = generate_plist()
    print(plist)
    print(f"\n# To install: write to {PLIST_FILE}")
    print(f"#   launchctl load {PLIST_FILE}")


def cmd_install():
    """Install the auto-start service and start the daemon."""
    mgr = _service_manager()
    print(f"Installing hscc-daemon ({mgr}) service...")
    _stop_running_daemon()

    if mgr == "launchd":
        _install_launchd()
    elif mgr == "systemd":
        _install_systemd()
    else:
        print("  No service manager (launchd/systemd) found — starting as a")
        print("  background process (will NOT auto-start on boot).")


def cmd_uninstall():
    """Remove the auto-start service and stop the daemon."""
    _stop_running_daemon()
    mgr = _service_manager()
    if mgr == "launchd":
        _uninstall_launchd()
    elif mgr == "systemd":
        _uninstall_systemd()
    else:
        print("  No service manager — daemon stopped (nothing installed to remove)")
