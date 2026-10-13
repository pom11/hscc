"""launchd unit for the HSCC read-only API (REPL-injection 2026-10-13).

OPT-IN, exactly like the daemon's own unit (hscc daemon install): the
bootstrap offers `hscc api install` — nothing is loaded without it. One
consequence the operator called out: after a REBOOT the iPhone would find
no API at all (the current PID-file start does not survive login). KeepAlive
here covers crash respin AND boot; the unit runs the real entrypoint
(`python3 -m api_server serve`) so the bind rules — 127.0.0.1 only, token
required — are unchanged, not weakened.

Honest scope note: `hscc api status` reads its PID file, so a launchd-run
instance looks stopped there; use `launchctl print gui/$UID/com.hscc.api`
for the unit (same known asymmetry as the daemon unit).

Pure-text module (build/recommend only) so the plist shape is unit-testable
without launchctl: the install path is `plutil -lint` + a read-back compare,
mirroring tests/test_install.py's fake-launchctl pattern.
"""
from __future__ import annotations

import os
import plistlib
import shlex

API_LABEL = "com.hscc.api"
API_PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{API_LABEL}.plist")


def build_api_plist(python_path: str, hscc_api_dir: str, *,
                    log_path: str | None = None) -> dict:
    """Plist dict for the API under launchd.

    Runs ``<python> -m api_server serve`` with cwd=hscc_api_dir (the module
    lives there; api_server itself enforces the loopback bind + token).
    """
    return {
        "Label": API_LABEL,
        "ProgramArguments": [python_path, "-m", "api_server", "serve"],
        "WorkingDirectory": hscc_api_dir,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_path or os.path.expanduser("~/.hscc/logs/api.log"),
        "StandardErrorPath": log_path or os.path.expanduser("~/.hscc/logs/api.err.log"),
        "EnvironmentVariables": {
            "HOME": os.path.expanduser("~"),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:"
                    + os.path.pathsep.join(
                        p for p in (os.environ.get("PATH", "").split(os.path.pathsep))
                        if p),
        },
    }


def recommend_api_python() -> str | None:
    """Interpreter for the API unit, resolved at install time.

    Same rule as bootstrap._api_python: hermes' venv (which carries the
    websockets/starlette deps the API needs), never the system python3.
    Returns None when no venv interpreter exists — the caller reports it
    rather than writing a plist that could never start.
    """
    hermes_home = os.environ.get(
        "HERMES_HOME", os.path.expanduser(os.path.join("~", ".hermes")))
    cand = os.path.join(hermes_home, "hermes-agent", "venv", "bin", "python3")
    return cand if os.path.exists(cand) else None


def install_api_plist(hscc_api_dir: str, python_path: str | None = None) -> dict:
    """Write the LaunchAgent plist. NEVER loads it — `launchctl bootstrap`
    stays a deliberate manual step (an opt-in unit must not become live from
    a file write). Idempotent: a byte-identical re-install is a no-op.
    """
    py = python_path or recommend_api_python()
    if not py:
        raise RuntimeError(
            "no Hermes venv python found (HERMES_HOME/hermes-agent/venv/bin/python3) "
            "— pass python_path explicitly or install Hermes first")
    data = build_api_plist(py, os.path.abspath(hscc_api_dir))
    os.makedirs(os.path.dirname(API_PLIST_PATH), exist_ok=True)
    os.makedirs(os.path.expanduser("~/.hscc/logs"), exist_ok=True)
    new_bytes = plistlib.dumps(data, sort_keys=False)
    try:
        with open(API_PLIST_PATH, "rb") as fh:
            if fh.read() == new_bytes:
                return {"path": API_PLIST_PATH, "changed": False, "python": py}
    except FileNotFoundError:
        pass
    tmp = API_PLIST_PATH + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(new_bytes)
    os.replace(tmp, API_PLIST_PATH)
    return {"path": API_PLIST_PATH, "changed": True, "python": py}


def uninstall_api_plist() -> dict:
    """Remove the plist file (manual `launchctl bootout` first)."""
    try:
        os.remove(API_PLIST_PATH)
        return {"path": API_PLIST_PATH, "removed": True}
    except FileNotFoundError:
        return {"path": API_PLIST_PATH, "removed": False}


def plist_snippet(hscc_api_dir: str) -> str:
    """Copy-paste text for the manual route (report item 5)."""
    py = recommend_api_python() or "python3  # <-- set the Hermes venv python"
    args = " ".join(shlex.quote(a) for a in build_api_plist(py, hscc_api_dir)["ProgramArguments"])
    return (
        "# Save as ~/Library/LaunchAgents/%s.plist, then:\n"
        "#   plutil -lint ~/Library/LaunchAgents/%s.plist\n"
        "#   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/%s.plist\n"
        "#   launchctl print gui/$UID/%s | head -20\n"
        "# ProgramArguments line: %s  (WorkingDirectory: %s)\n"
        % (API_LABEL, API_LABEL, API_LABEL, API_LABEL, args, hscc_api_dir)
    )
