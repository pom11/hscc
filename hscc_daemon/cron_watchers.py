"""Liveness stamps + staleness verdicts for the Hermes-cron watchers.

Audit B / GAP-B-4: the two watchers that live OUTSIDE the daemon --
``scripts/dep_pr_watcher.py`` (Hermes cron job ``hscc-dep-watcher``, daily) and
``scripts/escalate_watcher_run.py`` (Hermes cron job ``hscc-escalate-watcher``,
*/15) -- leave no trace when they run and no gap at all when they stop running.
Both are ``--no-agent`` jobs whose only record is Hermes' own run history, so a
job that stops firing is discovered only when a dep-bump PR goes unverified for
days. That exact failure already happened once: the dep-watcher failed 17
consecutive daily runs before anyone looked (t_3aadd2c0, recorded in
``scripts/install_dep_watcher.sh``).

Fix shape deliberately copied from the daemon streams, not invented:

  * Every pass calls :func:`stamp`, which stamps through the daemon's OWN
    ``state.write_state()`` -- same files under ``~/.hscc/state``, same
    ``timestamp`` field, same atomic tmp+replace, same ``persist_disabled()``
    honour. There is exactly ONE liveness stamp format on this host.
  * The stamp moves on EVERY pass -- idle passes included. "Ran, nothing to do"
    must advance it, or a healthy idle watcher is indistinguishable from a dead
    one. A pass whose upstream probe failed still stamps (ok=True) with the
    failure in the payload: the pass itself happened.
  * Verdicts are WARN-shaped, mirroring the repo's verify warn convention
    (verify.py ``check_proxy_router_settings``: ``ok: True`` + a ``WARN:``-
    prefixed detail). A dead watcher is a loud yellow line, not a RED check.

The watchers are NOT moved into the daemon here -- that is a separate decision
(audit B left it open). This module only makes their silence observable.

Cadence source of truth lives HERE (``WATCHERS``), mirroring the installer
defaults. A watcher whose schedule is customised via env
(``HSCC_DEP_WATCHER_SCHEDULE`` / ``HSCC_ESCALATE_SCHEDULE``) should point the
staleness window at the real cadence with ``HSCC_DEP_WATCHER_CADENCE_S`` /
``HSCC_ESCALATE_CADENCE_S`` (seconds) so a custom schedule is not judged
against the installer default.

Stdlib-only (same rule as the daemon's ``state.py``, which this module loads
standalone by path when the package import fails -- ``scripts/`` is not a
deployed plugin package, and dep_pr_watcher.py deliberately imports nothing
from ``hscc_daemon`` at module scope).
"""

import datetime
import importlib.util
import json
import os

# Staleness = this many missed passes before a watcher is declared stale.
# >= 3x cadence per GAP-B-4: one missed pass is normal (a slow tick, a laptop
# asleep across the 08:00 mark), so the window has to absorb it; two misses
# already read stale at 3x.
MISS_FACTOR = 3

# Each watcher's SOT row. ``stream`` is the stamp file name under
# ~/.hscc/state (and the verify-stream exclusion key), ``cron_job`` the Hermes
# cron job name the installer registers, ``cadence_s`` the installer default
# schedule in seconds, ``cadence_env`` the env override for the cadence (see
# the module docstring), ``install_script`` how to (re)install it.
WATCHERS = {
    "dep_pr_watcher": {
        "cron_job": "hscc-dep-watcher",
        "script": "dep_pr_watcher.py",
        "cadence_s": 86400,           # daily (`0 8 * * *`)
        "cadence_env": "HSCC_DEP_WATCHER_CADENCE_S",
        "install_script": "scripts/install_dep_watcher.sh",
    },
    "escalate_watcher": {
        "cron_job": "hscc-escalate-watcher",
        "script": "escalate_watcher_run.py",
        "cadence_s": 900,             # every 15 min (`*/15 * * * *`)
        "cadence_env": "HSCC_ESCALATE_CADENCE_S",
        "install_script": "scripts/install_escalate_watcher.sh",
    },
}

# Names in stable rendering order.
STREAMS = tuple(WATCHERS)

# Statuses that mean "nothing to warn about".
HEALTHY_STATUSES = ("ok", "not-configured")


def state_dir():
    """Where the stamps live -- must match ``hscc_daemon.state.STATE_DIR``.

    ``HSCC_DIR`` (the dir holding ``cluster.json``, honoured the same way by
    the bootstrap doctor) relocates it wholesale; otherwise ``~/.hscc/state``
    through ``expanduser``, which already follows ``HOME`` in a redirected-home
    test. A test that monkeypatches ``hscc_daemon.state.STATE_DIR`` still wins:
    :func:`_state_writer` prefers the package module when importable, and
    :func:`read_stamp` asks that module for the directory when it is present.
    """
    try:
        s = _state_writer()
    except Exception:
        pass
    else:
        return s.STATE_DIR
    base = os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")
    return os.path.join(base, "state")


def _state_writer():
    """Return the canonical state module (write_state / read_state / STATE_DIR).

    Prefers the real ``hscc_daemon.state`` package module -- the deployed
    runtime, and any test that patched its ``STATE_DIR``. Falls back to loading
    the sibling ``state.py`` standalone by path, for a ``scripts/``-only
    checkout where ``hscc_daemon`` is not importable. Raises ImportError if
    neither is reachable; callers decide how loud to be.
    """
    try:
        from hscc_daemon import state as _s
        return _s
    except ImportError:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), "hscc_daemon", "state.py")
        spec = importlib.util.spec_from_file_location(
            "hscc_cron_watcher_state", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _hscc_base():
    """The dir one level above the stamp dir, so ONE test seam moves both.

    Prefers ``dirname(state_dir())`` -- which follows ``hscc_daemon.state``'s
    ``STATE_DIR`` when a test patched it -- and falls back to ``HSCC_DIR`` /
    ``~/.hscc`` when the state module is not importable.
    """
    try:
        return os.path.dirname(state_dir())
    except Exception:
        pass
    return os.environ.get("HSCC_DIR") or os.path.expanduser("~/.hscc")


def install_marker_path():
    """Path of the install marker file (ONE LEVEL ABOVE the state dir).

    Deliberately OUTSIDE ~/.hscc/state: everything in the state dir is globbed
    by ``verify.check_daemon_streams`` as a daemon stream, and the marker is
    not a stream -- it is the record that an installer registered the cron job.
    """
    return os.path.join(_hscc_base(), "watcher-installs.json")


def mark_installed(stream, schedule=None):
    """Record that install_<...>_watcher.sh registered this cron job.

    Called by the installer, not by the watcher. The marker lets the doctor
    tell "installed, but the job has never completed a pass" (the
    17-consecutive-failures incident class, t_3aadd2c0) from "never
    configured". Best-effort; returns True when the marker was written.
    """
    if stream not in WATCHERS:
        return False
    path = install_marker_path()
    data = {}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[stream] = {
        "cron_job": WATCHERS[stream]["cron_job"],
        "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "schedule": schedule or "",
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def mark_removed(stream):
    """Drop a stream's install marker (installer --uninstall). Best-effort."""
    path = install_marker_path()
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict) or stream not in data:
            return True
        data.pop(stream, None)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, ValueError):
        return False


def read_install_marker(stream):
    """Install marker row for a stream, or None."""
    try:
        with open(install_marker_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    row = data.get(stream)
    return row if isinstance(row, dict) else None


def cadence_s(stream, env=None):
    """Watcher cadence in seconds: env override, else the installer default."""
    row = WATCHERS[stream]
    raw = (os.environ if env is None else env).get(row["cadence_env"])
    if raw:
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            val = 0
        if val > 0:
            return val
    return row["cadence_s"]


def stale_window_s(stream, env=None):
    """Seconds after which a stamp is stale: MISS_FACTOR x cadence."""
    return MISS_FACTOR * cadence_s(stream, env=env)


def _parse_ts(value):
    """Parse a stamp timestamp; None when absent or unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts


def stamp(stream, **fields):
    """Record one watcher pass through the canonical state writer.

    Never raises: a stamp failure must never break -- or silently un-stamp --
    the watcher's real work, and silent no-op is exactly the GAP-B-4 failure
    mode this closes. Returns True when the stamp landed, False otherwise
    (callers print the difference; they never act on it).

    Honours ``state.persist_disabled_active()``: inside an ad-hoc ``hscc
    check`` run there is no real pass to record, so nothing is written.
    """
    row = WATCHERS.get(stream)
    if row is None:
        return False
    try:
        s = _state_writer()
        if getattr(s, "persist_disabled_active", lambda: False)():
            return False
        s.write_state(stream, {
            "ok": True,
            "watcher": stream,
            "cron_job": row["cron_job"],
            **fields,
        })
        return True
    except Exception:
        return False


def stamp_failure(stream, error):
    """Record a pass that raised: the stamp MOVES, ok=False carries the fault.

    A watcher that crashes mid-pass is the single most important thing to
    notice, and "crashed" must not look like "never ran" -- so this writes too,
    with ok=False and the error text. Returns True when the stamp landed.
    """
    row = WATCHERS.get(stream)
    if row is None:
        return False
    try:
        s = _state_writer()
        s.write_state(stream, {
            "ok": False,
            "watcher": stream,
            "cron_job": row["cron_job"],
            "error": str(error)[:400],
        })
        return True
    except Exception:
        return False


def read_stamp(stream):
    """Read a watcher's stamp dict from the state dir; None if absent/broken."""
    path = os.path.join(state_dir(), stream + ".json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def verdict(stream, *, now=None, state=None, env=None):
    """Liveness verdict for one watcher -- RAW status, no surface-specific ok.

    Returns {stream, status, warn, detail, age_s, window_s} where ``status`` is:

      ``ok``               stamp fresh -- healthy.
      ``not-configured``   no stamp file and no install marker -- the watcher
                           is off. Clean, NOT a warning: an operator who never
                           installed it must not be nagged every doctor run.
      ``never-ran``        the installer registered the cron job (install
                           marker present) but no pass ever completed -- the
                           17-consecutive-failures incident class. Warns.
      ``stale``            stamp older than :func:`stale_window_s` -- the cron
                           job stopped firing. The GAP-B-4 case.
      ``failing``          stamp fresh but the last pass recorded ok=False.
      ``bad-stamp``        stamp exists, timestamp unparseable.

    ``warn`` is True for every non-healthy status and ``detail`` carries the
    repo's ``WARN:`` prefix there, so any surface printing the row reads it as
    a warning.

    Consumers map status onto their own ok convention on purpose: the doctor
    row renders non-healthy as a NON-FATAL failure (ok=False, fatal=False --
    doctor's warning shape) while ``hscc verify`` keeps ok=True with the WARN
    detail, so a dead watcher never turns the cluster smoke test RED.
    """
    if stream not in WATCHERS:
        raise KeyError(stream)
    row = WATCHERS[stream]
    window = stale_window_s(stream, env=env)
    if state is None:
        state = read_stamp(stream)

    base = {"stream": stream, "age_s": None, "window_s": window}

    if state is None:
        # No stamp. Two very different reasons, and conflating them is how the
        # 17-consecutive-failures incident stayed invisible: an installer
        # marker means the job WAS registered, so silence means it never
        # completed a pass (a warning); no marker means it was never installed
        # (clean, no warning).
        marker = read_install_marker(stream)
        if marker:
            return {**base, "status": "never-ran", "warn": True,
                    "detail": (f"WARN: {stream} ({row['cron_job']}) was "
                               f"installed ({row['install_script']}) but has "
                               f"never completed a pass -- check "
                               f"`hermes cron list` for failing runs")}
        return {**base, "status": "not-configured", "warn": False,
                "detail": (f"{stream}: not configured (no stamp; install with "
                           f"{row['install_script']} if it should run)")}

    ts = _parse_ts(state.get("timestamp"))
    if ts is None:
        return {**base, "status": "bad-stamp", "warn": True,
                "detail": (f"WARN: {stream}: stamp carries no parseable "
                           f"timestamp -- liveness cannot be judged")}

    now = now or datetime.datetime.now(datetime.timezone.utc)
    age = (now - ts).total_seconds()
    base["age_s"] = int(age)

    if age > window:
        return {**base, "status": "stale", "warn": True,
                "detail": (f"WARN: {stream} ({row['cron_job']}) last ran "
                           f"{int(age / 60)}m ago -- past the "
                           f"{int(window / 60)}m staleness window; the cron "
                           f"job is not firing (check `hermes cron list`; "
                           f"cadence env {row['cadence_env']})")}
    if state.get("ok") is False:
        err = str(state.get("error") or state.get("message") or "last pass failed")
        return {**base, "status": "failing", "warn": True,
                "detail": (f"WARN: {stream} ({row['cron_job']}) ran recently "
                           f"but its last pass reported a failure: {err[:200]}")}
    return {**base, "status": "ok", "warn": False,
            "detail": (f"{stream} ({row['cron_job']}) fresh "
                       f"({int(age)}s old, window {window}s)")}


def all_verdicts(*, now=None, env=None):
    """Verdicts for every registered watcher, in stable order."""
    return [verdict(s, now=now, env=env) for s in STREAMS]


def fmt_age(seconds):
    """Human age: '38m', '4.2h', '2.1d'."""
    if seconds is None:
        return "never"
    if seconds < 5400:
        return f"{max(0, int(seconds / 60))}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
