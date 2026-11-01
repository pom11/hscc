"""Doctor check: are the out-of-daemon cron watchers still firing? (GAP-B-4)

``dep_pr_watcher`` (Hermes cron ``hscc-dep-watcher``, daily) and
``escalate_watcher`` (Hermes cron ``hscc-escalate-watcher``, */15) run outside
the daemon, so no daemon stream sees them: when Hermes cron stops firing them,
nothing in HSCC notices. Each pass stamps itself into ``~/.hscc/state`` via the
daemon's own state writer (``hscc_daemon/cron_watchers.py``); this check reads
those stamps and warns (non-fatal) when one is older than 3x its cadence.

Stdlib-only, like every other hscc-bootstrap module: preflight runs before the
runtime plugin set is guaranteed to exist. The stamp format is the daemon's
plain JSON (``state.write_state``), so reading it needs nothing but json — the
cadence table is mirrored here as ``WATCHERS`` (kept byte-for-byte in sync by
``hscc_daemon/tests/test_cron_watchers.py``).

Verdict convention (mirrors the repo's warn style):
  * fresh stamps (or a watcher that was never configured -- no stamp file) ⇒
    ok=True. "not configured" is reported cleanly, never as a warning.
  * stale / failing / bad-stamp ⇒ ok=False with a WARN-prefixed detail and
    fatal=False: a dead watcher is a loud yellow line, never a failed install.
"""

import datetime
import json
import os
from typing import Any, Dict, Mapping, Optional

CHECK_NAME = "cron watchers (dep_pr/escalate)"

# Mirror of hscc_daemon/cron_watchers.py WATCHERS (stream -> cadence seconds).
# The daemon-side module is the SOT; the sync-pinned test keeps them equal.
WATCHERS = {
    "dep_pr_watcher": 86400,
    "escalate_watcher": 900,
}
# Mirror of the cadence-override env names (same sync-pinned test).
CADENCE_ENV = {
    "dep_pr_watcher": "HSCC_DEP_WATCHER_CADENCE_S",
    "escalate_watcher": "HSCC_ESCALATE_CADENCE_S",
}
# Mirror of the installer paths (same sync-pinned test).
INSTALL_SCRIPT = {
    "dep_pr_watcher": "scripts/install_dep_watcher.sh",
    "escalate_watcher": "scripts/install_escalate_watcher.sh",
}
MISS_FACTOR = 3


def state_dir(hscc_dir: Optional[str] = None) -> str:
    base = hscc_dir or os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")
    return os.path.join(base, "state")


def _read_stamp(directory: str, stream: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(directory, stream + ".json")
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # A truncated/corrupt stamp is itself evidence of a broken writer —
        # surface it as a bad stamp, not as "not configured".
        return {"timestamp": None}
    return data if isinstance(data, dict) else {"timestamp": None}


def _install_marker(hscc_base: str, stream: str) -> Optional[Dict[str, Any]]:
    """Installer marker row (written by install_*.sh via cron_watchers)."""
    path = os.path.join(hscc_base, "watcher-installs.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    row = data.get(stream)
    return row if isinstance(row, dict) else None


def _age_seconds(state, now):
    raw = state.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return (now - ts).total_seconds()


def evaluate(hscc_dir: Optional[str] = None, *, now=None,
             env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Verdict rows for both watchers. Pure read; never raises per-watcher.

    Returns {name, ok, fatal, detail, fix, rows}. ok=False whenever any
    watcher is stale/failing/bad-stamp; fatal is ALWAYS False (GAP-B-4:
    warn-only -- the bootstrap must never hard-stop over an out-of-band cron).
    """
    directory = state_dir(hscc_dir)
    hscc_base = os.path.dirname(directory)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    env = dict(os.environ) if env is None else env

    rows = []
    problems = []
    notes = []
    for stream, base_cadence in WATCHERS.items():
        cadence = base_cadence
        raw_c = env.get(CADENCE_ENV[stream])
        if raw_c:
            try:
                val = int(float(raw_c))
                if val > 0:
                    cadence = val
            except (TypeError, ValueError):
                pass
        window = MISS_FACTOR * cadence
        state = _read_stamp(directory, stream)

        if state is None:
            # No stamp: an install marker separates "registered but never
            # completed a pass" (the 17-failure incident class -- warn) from
            # "never configured" (clean, no nag).
            if _install_marker(hscc_base, stream):
                rows.append({"stream": stream, "status": "never-ran",
                             "age_s": None, "window_s": window})
                problems.append(
                    f"WARN: {stream} was installed but has never completed a "
                    f"pass -- check `hermes cron list` for failing runs")
                continue
            rows.append({"stream": stream, "status": "not-configured",
                         "age_s": None, "window_s": window})
            notes.append(f"{stream}: not configured (no stamp; run "
                         f"{INSTALL_SCRIPT[stream]} if it should run)")
            continue

        age = _age_seconds(state, now)
        if age is None:
            rows.append({"stream": stream, "status": "bad-stamp",
                         "age_s": None, "window_s": window})
            problems.append(f"WARN: {stream}: stamp has no parseable "
                            f"timestamp -- liveness cannot be judged")
            continue

        rows.append({"stream": stream, "status": None, "age_s": int(age),
                     "window_s": window})
        if age > window:
            rows[-1]["status"] = "stale"
            problems.append(
                f"WARN: {stream} last ran {int(age / 60)}m ago (window "
                f"{int(window / 60)}m) -- the Hermes cron job is not firing")
        elif state.get("ok") is False:
            rows[-1]["status"] = "failing"
            err = str(state.get("error") or "last pass failed")
            problems.append(f"WARN: {stream} last pass failed: {err[:160]}")
        else:
            rows[-1]["status"] = "ok"
            notes.append(f"{stream} fresh ({int(age)}s)")

    if problems:
        return {
            "name": CHECK_NAME, "ok": False, "fatal": False, "rows": rows,
            "detail": "; ".join(problems + notes),
            "fix": ("check `hermes cron list` (job paused? ticker down? the "
                    "script moved out of <bound home>/scripts?), then "
                    "`hermes cron run <job> && hermes cron tick` to prove a "
                    "pass; reinstall with scripts/install_dep_watcher.sh / "
                    "scripts/install_escalate_watcher.sh if the job is gone"),
        }
    return {
        "name": CHECK_NAME, "ok": True, "fatal": False, "rows": rows,
        "detail": "; ".join(notes) if notes else "no watchers configured",
        "fix": "",
    }


def check(hscc_dir: Optional[str] = None, *, now=None,
          env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Doctor-shaped check dict (name/ok/detail/fix/fatal + rows)."""
    return evaluate(hscc_dir=hscc_dir, now=now, env=env)
