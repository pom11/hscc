"""Install/uninstall/service management for the HSCC daemon."""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .cli_theme import esc, make_console, make_status_panel
# The ONE unit name, shared with systemd-setup.sh, restart_daemon.sh and the
# migration script (t_d8f13693). Import it — never re-type the literal here:
# hscc-bootstrap/tests/test_unit_name_drift.py pins every reader to this file.
from .unit_name import LEGACY_UNIT_NAME, UNIT_NAME


HSCC_DIR = os.path.expanduser("~/.hscc")
PID_FILE = os.path.join(HSCC_DIR, "daemon.pid")
LOG_FILE = os.path.join(HSCC_DIR, "daemon.log")
PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")
PLIST_FILE = os.path.join(PLIST_DIR, "com.hermes.hscc_daemon.plist")
SYSTEMD_USER_DIR = Path(os.path.expanduser("~/.config/systemd/user"))
SYSTEMD_UNIT_FILE = SYSTEMD_USER_DIR / UNIT_NAME
SYSTEMD_UNIT_NAME = UNIT_NAME
#: The retired name (written by systemd-setup.sh before t_d8f13693). Nothing
#: here WRITES it; uninstall purges it so a box cannot keep it enabled.
LEGACY_SYSTEMD_UNIT_NAME = LEGACY_UNIT_NAME
LEGACY_SYSTEMD_UNIT_FILE = SYSTEMD_USER_DIR / LEGACY_UNIT_NAME

PLIST_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.hscc_daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{PYTHON_PATH}</string>
        <string>-m</string>
        <string>hscc_daemon</string>
        <string>start-daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{PKG_DIR}</string>
    <key>StandardOutPath</key>
    <string>{HOMEDIR}/.hscc/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>{HOMEDIR}/.hscc/daemon.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{PATH_ENV}</string>
        <key>PYTHONPATH</key>
        <string>{PLUGINS_DIR}</string>
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
ExecStart={PYTHON_PATH} -m hscc_daemon start-daemon
WorkingDirectory={PKG_DIR}
Environment=PATH={PATH_ENV}
Environment=PYTHONPATH={PLUGINS_DIR}
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


def _resolve_python():
    """Resolve a real python interpreter for the service unit (C1).

    Prefer the Hermes venv python (has the daemon's deps), then PATH python3,
    then known locations. Never returns a path that doesn't exist — a hardcoded
    /usr/local/bin/python3 would respawn-loop on Homebrew-only/Spark hosts."""
    hmdir = os.path.expanduser("~")
    candidates = [
        os.environ.get("HSCC_PYBIN", ""),
        os.path.join(hmdir, ".hermes/hermes-agent/venv/bin/python"),
        shutil.which("python3") or "",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "/usr/bin/python3"  # last-resort default


def _service_manager():
    """Pick the auto-start mechanism for this host."""
    if sys.platform == "darwin":
        return "launchd"
    if shutil.which("systemctl"):
        return "systemd"
    return "none"


def _console(**kwargs):
    """Themed Console (theme auto-detects); ``file=`` lets tests capture."""
    return make_console(**kwargs)


def _stop_running_daemon():
    """Stop any running daemon instance (shared by install/uninstall)."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        _console().print(f"  Stopping existing daemon (PID {pid})")
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
    python_path = _resolve_python()
    pkg_dir = str(Path(__file__).resolve().parent)
    plugins_dir = str(Path(__file__).resolve().parent.parent)  # plugins/
    return PLIST_CONTENT.format(
        PYTHON_PATH=python_path,
        PKG_DIR=pkg_dir,
        HOMEDIR=hmdir,
        PATH_ENV=_daemon_path_env(hmdir),
        PLUGINS_DIR=plugins_dir,
    )


def generate_systemd_unit():
    """Generate the systemd --user unit with resolved paths.

    THE raw-text emitter. Everything that puts a unit file on disk calls this —
    ``hscc install`` (``_install_systemd``) and ``systemd-setup.sh`` (through
    :func:`emit_systemd_unit`). Neither may build the file from a rendered
    console view: ``cmd_plist`` prints through Rich, which SOFT-WRAPS long lines,
    and a redirected copy of that output is a unit file systemd cannot parse.
    Measured on this checkout before t_d8f13693 — the Rich render folds
    ``ExecStart=... -m hscc_daemon start-daemon`` across a newline, so systemd
    (which keeps only text up to the first ``=`` per line, and skips a line with
    no ``=``) got ``ExecStart=<python> -m hscc_daemon`` with ``start-daemon``
    dropped on the floor, and the PATH line lost its ``~/.local/bin`` entry.
    A node installed that way runs the wrong command under a name nobody probes.
    """
    hmdir = os.path.expanduser("~")
    python_path = _resolve_python()
    pkg_dir = str(Path(__file__).resolve().parent)
    plugins_dir = str(Path(__file__).resolve().parent.parent)  # plugins/
    return SYSTEMD_UNIT_CONTENT.format(
        PYTHON_PATH=python_path,
        PKG_DIR=pkg_dir,
        HOMEDIR=hmdir,
        PATH_ENV=_daemon_path_env(hmdir),
        PLUGINS_DIR=plugins_dir,
    )


# ── systemd --user: the ONE emitter + the legacy purge (t_d8f13693) ─────────
#
# ``systemd-setup.sh`` used to generate its unit with `hscc.py plist > UNIT`
# (a rendered console view — see generate_systemd_unit's docstring) AND under a
# different name. Both are fixed by routing the shell helper through these two
# functions, so there is exactly one place that writes a unit file and exactly
# one name it writes.

def systemd_unit_paths(dest_dir=None):
    """(unit_name, unit_file, legacy_name, legacy_file) for this host.

    ``dest_dir`` overrides ``SYSTEMD_USER_DIR`` (tests point it at a tmp dir);
    read via the module global so a monkeypatched test cannot aim at the
    operator's real ``~/.config/systemd/user``.
    """
    base = Path(dest_dir) if dest_dir else Path(SYSTEMD_USER_DIR)
    return (SYSTEMD_UNIT_NAME, base / SYSTEMD_UNIT_NAME,
            LEGACY_SYSTEMD_UNIT_NAME, base / LEGACY_SYSTEMD_UNIT_NAME)


def emit_systemd_unit(dest_dir=None, *, dry_run=False):
    """Write the canonical unit file and report the names a caller must handle.

    Returns ``{"unit_name", "unit_file", "legacy_name", "legacy_file",
    "changed"}``. This is the function ``systemd-setup.sh`` calls (and
    ``_install_systemd`` calls), so the shell helper and `hscc install` cannot
    drift on content or name. Writes only the unit file — starting/enabling it
    stays with the caller, which also owns the twin-avoidance ordering.
    ``dry_run=True`` reports what WOULD change and writes nothing.
    """
    name, unit_file, legacy_name, legacy_file = systemd_unit_paths(dest_dir)
    body = generate_systemd_unit()
    changed = not (unit_file.exists() and unit_file.read_text() == body)
    if changed and not dry_run:
        unit_file.parent.mkdir(parents=True, exist_ok=True)
        unit_file.write_text(body)
    return {"unit_name": name, "unit_file": str(unit_file),
            "legacy_name": legacy_name, "legacy_file": str(legacy_file),
            "changed": bool(changed and not dry_run)}


def purge_legacy_systemd_unit(dest_dir=None, *, dry_run=False, runner=None):
    """Disable + remove the retired ``hscc_daemon.service`` if this host has one.

    Without this, a node installed by the old ``systemd-setup.sh`` keeps the
    legacy unit ENABLED after a re-install: two enabled units for one daemon is
    the supervisor-level twin, and `systemctl --user disable` of the new name
    would leave the old one running at next login.

    ``runner`` is the systemctl seam (``subprocess.run`` by default; tests
    inject a recorder) — never a hand-rolled transport. Returns a dict naming
    what was found and what was done; a legacy unit that was not ours to remove
    is reported, never silently kept.
    """
    run = runner or subprocess.run
    name, _unit_file, legacy_name, legacy_file = systemd_unit_paths(dest_dir)

    def probe(verb):
        res = run(["systemctl", "--user", verb, legacy_name],
                  capture_output=True, text=True, timeout=15)
        return (res.stdout or "").strip()

    enabled = probe("is-enabled")
    active = probe("is-active")
    present = Path(legacy_file).exists()
    found = bool(present or enabled in ("enabled", "enabled-runtime", "static",
                                        "masked", "generated") or active == "active")
    if not found:
        return {"legacy_name": legacy_name, "found": False, "changed": False,
                "enabled": enabled, "active": active, "detail": "no legacy unit"}
    if dry_run:
        return {"legacy_name": legacy_name, "found": True, "changed": False,
                "enabled": enabled, "active": active, "present": present,
                "detail": "dry-run: would disable --now + remove"}

    steps = []
    if active == "active" or active == "activating":
        run(["systemctl", "--user", "disable", "--now", legacy_name],
            capture_output=True, text=True, timeout=20)
        steps.append("disable --now")
    elif enabled.startswith("enabled"):
        run(["systemctl", "--user", "disable", legacy_name],
            capture_output=True, text=True, timeout=20)
        steps.append("disable")
    if present:
        try:
            Path(legacy_file).unlink()
            steps.append("remove file")
        except OSError as e:
            return {"legacy_name": legacy_name, "found": True, "changed": False,
                    "enabled": enabled, "active": active, "present": True,
                    "detail": f"could not remove {legacy_file}: {e}"}
    run(["systemctl", "--user", "daemon-reload"],
        capture_output=True, text=True, timeout=15)
    after_rc, after = probe("is-enabled")
    return {"legacy_name": legacy_name, "found": True, "changed": True,
            "enabled": enabled, "active": active, "present": present,
            "after": after, "steps": steps,
            "detail": f"retired {legacy_name} ({'; '.join(steps) or 'already inert'}); "
                      f"only {name} remains"}


def _install_launchd():
    Path(PLIST_DIR).mkdir(parents=True, exist_ok=True)
    plist_file = Path(PLIST_FILE)
    plist_file.write_text(generate_plist())
    _console().print(f"  Plist installed: {esc(plist_file)}")

    result = subprocess.run(
        ["launchctl", "load", str(plist_file)],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        _console().print(make_status_panel(
            "Loaded into launchd — hscc_daemon is now managed by launchd.",
            status="ok", title="install",
        ))
        _console().print(f"  Check status: launchctl list | grep hscc")
    else:
        _console().print("  launchctl load failed, starting manually...")
        # Call the actual start function (imported later)
        _console().print(f"  To load on next boot: launchctl load {plist_file}")


def _install_systemd():
    # ONE unit name, ONE emitter: emit_systemd_unit() is also what
    # systemd-setup.sh calls, so the two install paths cannot write different
    # files under different names (t_d8f13693).
    emitted = emit_systemd_unit()
    unit_file = Path(emitted["unit_file"])
    _console().print(f"  Unit installed: {esc(unit_file)}")

    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, text=True, timeout=10)

    # Retire the pre-t_d8f13693 name BEFORE enabling the canonical one: a box
    # that still carries hscc_daemon.service would otherwise end up with TWO
    # enabled units for one daemon (the supervisor-level twin), and a later
    # `disable` of one name would leave the other running at next login.
    retired = purge_legacy_systemd_unit()
    if retired["found"]:
        _console().print(f"  Legacy unit: {esc(retired['detail'])}")

    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0:
        _console().print(make_status_panel(
            "Enabled + started via systemd --user — hscc_daemon is now "
            "managed by systemd.",
            status="ok", title="install",
        ))
        _console().print(f"  Check status: systemctl --user status {SYSTEMD_UNIT_NAME}")
        _console().print("  Tip: run `loginctl enable-linger $USER` so it survives logout.")
    else:
        # stderr is launchctl's own text — DATA (may quote paths with brackets).
        _console().print(f"  systemctl enable failed ({esc(result.stderr.strip())}), starting manually...")
        _console().print(f"  To enable later: systemctl --user enable --now {SYSTEMD_UNIT_NAME}")


def _uninstall_launchd():
    plist_file = Path(PLIST_FILE)
    if plist_file.exists():
        subprocess.run(["launchctl", "unload", str(plist_file)],
                       capture_output=True, text=True, timeout=10)
        plist_file.unlink()
        _console().print(make_status_panel(
            f"Plist removed: {esc(plist_file)} — hscc_daemon uninstalled.",
            status="ok", title="uninstall",
        ))
    else:
        _console().print("  No plist found — nothing to remove")


def _uninstall_systemd():
    subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME],
                   capture_output=True, text=True, timeout=15)
    if Path(SYSTEMD_UNIT_FILE).exists():
        Path(SYSTEMD_UNIT_FILE).unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, text=True, timeout=10)
        _console().print(make_status_panel(
            f"Unit removed: {esc(SYSTEMD_UNIT_FILE)} — hscc_daemon uninstalled.",
            status="ok", title="uninstall",
        ))
    else:
        _console().print("  No unit found — nothing to remove")
    # The retired name is uninstalled too: leaving hscc_daemon.service enabled
    # behind an `hscc uninstall` would mean the daemon is still started at next
    # login by a unit nothing in the repo writes any more.
    retired = purge_legacy_systemd_unit()
    if retired["found"]:
        _console().print(f"  Legacy unit: {esc(retired['detail'])}")


# ── CLI Command Wrappers ──────────────────────────────────────────────────

def cmd_unit_emit(argv=None):
    """Internal entry point: write the canonical systemd --user unit (t_d8f13693).

    ``systemd-setup.sh`` calls exactly this — ``hscc.py unit-emit --json`` — and
    parses the unit names out of the JSON. It used to run ``hscc.py plist >
    UNIT_FILE``, which redirected a *rendered Rich console view* into a unit
    file: box-drawing lines, and long assignments soft-wrapped so systemd's
    parser dropped ``start-daemon`` from ``ExecStart`` and truncated the
    ``PATH=`` value (see generate_systemd_unit's docstring). A view for humans
    is not a file format.

    ``--unit-dir DIR`` overrides the destination (tests; never used by the
    setup helper). ``--json`` is the default shape and is accepted explicitly
    so the caller can be unambiguous. Exit 0 on success, 1 on a write error —
    the caller is a ``set -e`` script that must not enable a unit it could not
    write. No service call happens here: enable/start order belongs to the
    caller, which owns never leaving two units active.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    dest = None
    if "--unit-dir" in argv:
        i = argv.index("--unit-dir")
        if i + 1 >= len(argv):
            print("unit-emit: --unit-dir needs a directory", file=sys.stderr)
            return 1
        dest = argv[i + 1]
    try:
        emitted = emit_systemd_unit(dest)
        retired = purge_legacy_systemd_unit(dest, dry_run="--dry-run" in argv)
    except OSError as e:
        print(f"unit-emit: could not write the unit file: {e}", file=sys.stderr)
        return 1
    import json as _json
    payload = dict(emitted)
    payload["legacy"] = retired
    payload["dry_run"] = "--dry-run" in argv
    print(_json.dumps(payload, sort_keys=True))
    return 0


def cmd_plist():
    """Generate and display the auto-start service definition."""
    from .daemon_ops import get_pid, save_pid, write_stopped, run_daemon_loop
    mgr = _service_manager()
    console = _console()
    if mgr == "systemd":
        unit = generate_systemd_unit()
        console.print(make_status_panel(
            "Systemd unit (copy to "
            + f"{esc(SYSTEMD_UNIT_FILE)})",
            status="", title="plist", color="accent",
        ))
        # The unit/plist bodies embed the HOME-derived python + path — DATA.
        console.print(esc(unit))
        console.print(f"  # systemctl --user daemon-reload && systemctl --user enable --now {SYSTEMD_UNIT_NAME}")
        return
    plist = generate_plist()
    console.print(make_status_panel(
        f"Launchd plist (copy to {esc(PLIST_FILE)})",
        status="", title="plist", color="accent",
    ))
    console.print(esc(plist))
    console.print(f"  # launchctl load {esc(PLIST_FILE)}")


def cmd_install():
    """Install the auto-start service and start the daemon."""
    mgr = _service_manager()
    _console().print(f"Installing hscc_daemon ({mgr}) service...")
    _stop_running_daemon()

    if mgr == "launchd":
        _install_launchd()
    elif mgr == "systemd":
        _install_systemd()
    else:
        _console().print("  No service manager (launchd/systemd) found — starting as a")
        _console().print("  background process (will NOT auto-start on boot).")


def cmd_uninstall():
    """Remove the auto-start service and stop the daemon."""
    _stop_running_daemon()
    mgr = _service_manager()
    if mgr == "launchd":
        _uninstall_launchd()
    elif mgr == "systemd":
        _uninstall_systemd()
    else:
        _console().print("  No service manager — daemon stopped (nothing installed to remove)")
