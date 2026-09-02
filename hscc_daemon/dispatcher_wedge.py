"""Detect, alert, and self-heal a silently-wedged kanban dispatcher.

The Hermes kanban dispatcher's OWN health check ("kanban dispatcher stuck")
fires whenever it sees spawnable ready work but 0 spawned — which is exactly
what it does when the fleet is legitimately AT CAPACITY (``kanban.max_in_progress``
reached). Two very different situations look identical to that check:

  * trigger 1 (healthy, at capacity): ready cards ARE being held back because
    every running worker slot is taken — correct behaviour, the dispatcher is
    fine, never page.
  * trigger 2 (genuine wedge): ready cards sit unclaimed while worker slots go
    EMPTY. The dispatcher should be spawning when slots free up but its own
    internal state is wedged, so it spawns NOTHING — silent, and `hscc status`/
    `hscc verify` stay green because nothing surfaced it.

This module is the hscc-side watchdog that tells the two apart. It is a
sibling of the operator-approved recovery automation in ``recover.py`` (the
engine-wedge watchdog) and reuses the exact same seams:

  * **detection** — ``check_dispatcher_wedge()`` (the ``dispatcher`` CHECK
    STREAM). For N consecutive detector ticks it observes a genuine stall —
    spawnable ready/review work exists on some board, that board has room under
    ``max_in_progress``, and still zero workers spawned — it writes
    ``ok: False``. That single ``ok: False`` is what turns the lights red:
    ``hscc status`` shows a FAIL row for ``dispatcher``, ``hscc verify`` fails
    the stream (via the existing ``check_daemon_streams``), and the trigger
    engine emits a ``state.dispatcher.degraded`` pseudo-event that operator
    trigger rules can page on. No new notification channel. Critically, the
 at-capacity case (trigger 1) never goes red — that is the exact false
 positive the card exists to prevent.

 To page on it, the operator adds a rule to ``~/.hscc/triggers.json``; the
 engine fires it automatically the moment ``ok`` flips (no repo change
 needed — the streams are operator-managed, and the degraded event is
 emitted generically for any red stream). Example rule:

 .. code-block:: json

     {"id": "dispatcher-wedge-detected", "trigger_type": "notify",
      "enabled": true, "cooldown_seconds": 3600,
      "condition": {"metric": "state.dispatcher", "op": "==", "value": "False"},
      "action": {"type": "notify", "message":
         "Dispatcher wedged: spawnable ready work with free worker slots"}}
 * **self-heal** — ``recover_dispatcher_wedge()`` (a daemon recovery thread).
    After M consecutive *genuine-stall detections* (the probe has already
    debounced N), and only if every guard passes, it invokes the restart action
    (default: ``launchctl kickstart <gateway>``). Guards mirror ``recover.py``:
    a cooldown window and a max-attempt cap, both persisted in
    ``~/.hscc/dispatcher_recover.json`` so they survive daemon restart; once
    the cap is hit we STOP acting and only keep alerting. ``launchctl`` is a
    MacOS label managed by ``launchctl list | grep ai.hermes``; the command is
    buildable/configurable and the actual subprocess call is injectable so
    tests run with a fake and never touch the real gateway.

Everything that touches the outside world is injectable (kanban read,
``max_in_progress`` config read, restart runner, clock, state writer), so the
suite runs against fakes with ZERO external calls and never touches live
operator state.
"""

import json
import os
import time

from .daemon_ops import log

DISPATCHER_STREAM = "dispatcher"

# ── Detect debounce (probe-level) ─────────────────────────────────────────
# N consecutive genuine-stall *ticks* before the detector DECLARES a stall and
# turns the stream red. A single transient tick (one board pausing between
# spawns while a worker writes a completion back) must never page.
DISPATCHER_DETECT_TICKS = int(
    os.environ.get("HSCC_DISPATCHER_DETECT_TICKS", "5"))

# ── Recovery debounce + guards (recovery-level, mirrors recover.py) ───────
# M consecutive genuine-stall DETECTIONS (each a tick where the probe declares
# a stall) before the heavy action (restart) is considered at all.
DISPATCHER_RESTART_AFTER_DETECTIONS = int(
    os.environ.get("HSCC_DISPATCHER_RESTART_AFTER", "3"))
# Min seconds between restarts (default 30 min) — never restart-loop a gateway
# that wedges again immediately.
DISPATCHER_RECOVER_COOLDOWN_SECONDS = int(
    os.environ.get("HSCC_DISPATCHER_RECOVER_COOLDOWN_SECONDS", "1800"))
# Max consecutive restarts before we GIVE UP acting and only keep alerting.
DISPATCHER_RECOVER_MAX_ATTEMPTS = int(
    os.environ.get("HSCC_DISPATCHER_RECOVER_MAX_ATTEMPTS", "3"))

# How often the daemon dispatcher recovery thread runs (seconds). Must match
# the dispatcher probe cadence so each recovery "detection" corresponds to one
# probe check.
DISPATCHER_RECOVER_CHECK_INTERVAL = int(
    os.environ.get("HSCC_DISPATCHER_RECOVER_CHECK_INTERVAL", "60"))

# Persisted cooldown/attempt state. Computed at RUNTIME via expanduser so the
# conftest ``_isolate_hscc`` redirect (which patches os.path.expanduser) moves
# every test onto a per-test tmp path automatically.
DISPATCHER_RECOVER_STATE_FILE = os.path.expanduser(
    "~/.hscc/dispatcher_recover.json")


def _recover_state_path():
    """The recover-state file, resolved at CALL time via expanduser.

    Resolving on every call (rather than once at import) means conftest's
    ``_isolate_hscc`` os.path.expanduser redirect redirects it per-test
    automatically, so no test can ever read/write the operator's real
    `~/.hscc/dispatcher_recover.json`. Kept identical to the module constant
    when running outside the test harness.
    """
    return os.path.expanduser("~/.hscc/dispatcher_recover.json")


def _gateway_label():
    """Return the Hermes gateway's macOS launchd label.

    The gateway service is registered under LaunchAgent
    ``ai.hermes.gateway`` (that is the label ``launchctl list`` shows and the
    one ``launchctl kickstart -k gui/<uid>/<label>`` targets). Configurable via
    env so an alternative registration name can be used without a code change.
    """
    return os.environ.get("HSCC_DISPATCHER_GATEWAY_LABEL", "ai.hermes.gateway")


def _default_restart_cmd():
    """The ``launchctl kickstart -k`` command that restarts the gateway.

    ``-k`` kills the running instance and relaunches it under launchd, which
    is exactly the "restart the gateway so the dispatcher rebuilds its state"
    intent. The uid is resolved at runtime because tests run under a different
    uid than the operator would not — the label + uid are stable for the
    daemon's owner. The command is exposed for transparency and for tests to
    assert against; the actual subprocess runner is ``_run_restart``.
    """
    uid = os.getuid()
    label = _gateway_label()
    return ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]


def _run_restart(cmd=None):
    """Run the restart command (or a provided one) and return its outcome.

    Returns ``{"success": bool, "cmd": [...], "error": str|None}``. Never
    raises. Tests inject a fake ``restart_fn`` so this is only reached in
    production (or when a test explicitly wants to exercise the parser path
    with a stubbed ``subprocess.run``).
    """
    cmd = cmd if cmd is not None else _default_restart_cmd()
    try:
        import subprocess
        proc = subprocess.run(
            [str(c) for c in cmd], capture_output=True, text=True, timeout=30)
        ok = proc.returncode == 0
        return {"success": ok, "cmd": list(cmd),
                "output": (proc.stdout or "") + (proc.stderr or "")}
    except Exception as e:  # noqa: BLE001 - never let a restart crash the daemon
        return {"success": False, "cmd": list(cmd), "error": str(e)}


# ── Kanban snapshot (read-only; reuses autodown's board seams) ─────────────
def _load_kanban_db_or_default():
    """Import Hermes' kanban lib lazily (same seam autodown uses), or None."""
    from . import autodown
    return autodown._load_kanban_db_or_default()


def _enum_board_names(kanban_db):
    """Ordered board slugs (reuse autodown's SINGLE enumeration)."""
    from . import autodown
    return autodown._enum_board_names(kanban_db)


def _any_assignee_under_cap(kanban_db, board, multi, max_per_profile):
    """True iff some assignee with unclaimed ready/review work is under its cap.

    Read-only. On any error, return True (fail toward "there is room"), which
    keeps this helper from silencing a real stall — the stall path still has
    its own cooldown and attempt cap.
    """
    try:
        with kanban_db.connect_closing(board=board) if multi \
                else kanban_db.connect_closing() as conn:
            waiting = [r[0] for r in conn.execute(
                "SELECT DISTINCT assignee FROM tasks "
                "WHERE status IN ('ready', 'review') "
                "AND assignee IS NOT NULL AND claim_lock IS NULL"
            ).fetchall()]
            if not waiting:
                return True
            running = dict(conn.execute(
                "SELECT assignee, COUNT(*) FROM tasks "
                "WHERE status = 'running' AND assignee IS NOT NULL "
                "GROUP BY assignee"
            ).fetchall())
    except Exception:  # noqa: BLE001
        return True
    return any(running.get(a, 0) < max_per_profile for a in waiting)


def _read_max_per_profile():
    """Read ``kanban.max_in_progress_per_profile`` from the operator's config.

    Returns None when unset/unreadable, meaning "no per-profile cap".
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        v = (cfg.get("kanban") or {}).get("max_in_progress_per_profile")
        if v is None:
            return None
        return int(v)
    except Exception:  # noqa: BLE001
        return None


def _board_snapshot(kanban_db, board, multi, max_in_progress,
                    max_per_profile=None):
    """Gather one board's dispatcher-relevant state.

    Returns a dict with:
      * ``spawnable`` — True iff the board has spawnable ready OR review work
        (an assigned, unclaimed task whose assignee is a real Hermes profile).
      * ``in_progress`` — count of ``status='running'`` tasks.
      * ``roomy`` — True iff this board could still spawn (in_progress below
        the global cap, or no cap is configured).
      * ``error`` — unreadable board (never crashes the whole scan).

    Where the real kanban lib exposes its ``has_spawnable_ready`` /
    ``has_spawnable_review`` we reuse them so our notion of "spawnable" is
    byte-for-byte the dispatcher's. Fall back to the same SQL predicate if the
    lib is a minimal injected stub without them.
    """
    try:
        with kanban_db.connect_closing(board=board) if multi \
                else kanban_db.connect_closing() as conn:
            if hasattr(kanban_db, "has_spawnable_ready"):
                spawnable = (kanban_db.has_spawnable_ready(conn)
                             or kanban_db.has_spawnable_review(conn))
            else:
                rows = conn.execute(
                    "SELECT 1 FROM tasks WHERE status = 'ready' "
                    "AND assignee IS NOT NULL AND claim_lock IS NULL LIMIT 1"
                ).fetchone()
                rows_review = conn.execute(
                    "SELECT 1 FROM tasks WHERE status = 'review' "
                    "AND assignee IS NOT NULL AND claim_lock IS NULL LIMIT 1"
                ).fetchone()
                spawnable = bool(rows or rows_review)
            in_progress = int(conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0])
    except Exception as e:  # noqa: BLE001
        label = "default" if not board else board
        return {"board": label, "spawnable": False, "in_progress": 0,
                "roomy": False, "error": str(e)}
    roomy = (max_in_progress is None
             or in_progress < max_in_progress)
    # A board with global room can still be unable to spawn ANYTHING when every
    # profile holding ready work is at its own per-profile cap. Treating that as
    # a wedge is a false alarm — the dispatcher is behaving exactly as
    # configured. Observed live: board running=5 under a global cap of 6, all
    # ready work assigned to ios-engineer which was at its per-profile cap of 3,
    # so the detector declared a "GENUINE STALL" every 5s against a healthy
    # dispatcher.
    if roomy and spawnable and max_per_profile is not None:
        roomy = _any_assignee_under_cap(kanban_db, board, multi, max_per_profile)
    return {"board": "default" if not board else board,
            "spawnable": spawnable, "in_progress": in_progress,
            "roomy": roomy, "error": None}


def _read_max_in_progress():
    """Read ``kanban.max_in_progress`` from the operator's hermes config.

    Returns None when unset or unreadable (no cap configured ⇒ boards are never
    capacity-blocked). Deliberately lenient: a config we cannot read just means
    "assume room", which errs toward reporting a stall — the recovery guard's
    cooldown/attempt-cap still protect against acting on a fluke.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        v = (cfg.get("kanban") or {}).get("max_in_progress")
        if v is None:
            return None
        v = int(v)
        return v if v > 0 else None
    except Exception:  # noqa: BLE001
        return None


# ── In-memory detector state (lives for the daemon process) ────────────────
# Mirrors ``_engine_wedge_units`` (health.py) / ``_recover_units`` (recover.py):
# a daemon restart re-probes fresh, so in-memory streaks are correct to reset.
_detector = {
    "stall_ticks": 0,      # consecutive genuine-stall ticks (N debounce)
    "last_running_total": None,  # previous tick's fleet-wide running count
    "declared": False,     # a genuine stall has been DECLARED (stream is red)
    "declared_since": None,  # epoch clock of first declaring tick
    "peak_stall_ticks": 0,
}

# M-debounce for the recovery: how many consecutive RED ticks (declared
# detections) the recovery needs before it considers restarting.
_recovery_detection_streak = 0


def _capture_kanban(kanban_db=None, max_in_progress=None,
                    max_per_profile=None):
    """Read dispatcher-relevant state across every board (read-only).

    Returns a dict that is the input to the stall decision:
      ``{boards: [...], spawnable: [...], roomy_spawnable: [...],
      at_capacity: [...], total_running: int, max_in_progress: int|None,
      errors: [...]}``.
    ``max_in_progress`` is read from config when not passed (injectable).
    If the kanban lib cannot be reached at all, returns ``{"unreachable":...}``
    so the caller can decide (fail safe: write ok True, do NOT false-alarm).
    """
    kanban_db = kanban_db or _load_kanban_db_or_default()
    if kanban_db is None:
        return {"unreachable": _kanban_unreachable_reason(),
                "boards": [], "spawnable": [], "roomy_spawnable": [],
                "at_capacity": [], "total_running": 0,
                "max_in_progress": None, "errors": []}
    if max_in_progress is None:
        max_in_progress = _read_max_in_progress()
    if max_per_profile is None:
        max_per_profile = _read_max_per_profile()
    try:
        boards, multi = _enum_board_names(kanban_db)
    except Exception as e:  # noqa: BLE001
        return {"unreachable": f"could not enumerate boards: {e}",
                "boards": [], "spawnable": [], "roomy_spawnable": [],
                "at_capacity": [], "total_running": 0,
                "max_in_progress": max_in_progress, "errors": []}
    spawnable, roomy_spawnable, at_capacity, errors = [], [], [], []
    total_running = 0
    for board in boards:
        snap = _board_snapshot(kanban_db, board, multi, max_in_progress,
                               max_per_profile)
        total_running += snap["in_progress"]
        if snap["error"]:
            errors.append(f"{snap['board']}: {snap['error']}")
            continue
        if snap["spawnable"]:
            spawnable.append(snap["board"])
            if snap["roomy"]:
                roomy_spawnable.append(snap["board"])
            else:
                at_capacity.append(snap["board"])
    return {
        "boards": ["default" if b is None else b for b in boards],
        "spawnable": spawnable, "roomy_spawnable": roomy_spawnable,
        "at_capacity": at_capacity, "total_running": total_running,
        "max_in_progress": max_in_progress, "errors": errors,
    }


def _kanban_unreachable_reason():
    try:
        from . import autodown
        return autodown._KANBAN_LOAD.get("reason") or "kanban lib unreachable"
    except Exception:  # noqa: BLE001
        return "kanban lib unreachable"


# ── The stall decision ─────────────────────────────────────────────────────
def evaluate_stall_tick(capture, detector_state, now):
    """One detector-tick's stall decision, as a pure update of state.

    Returns ``(declared_change, stall_tick)`` where ``declared_change`` is
    ``+1`` (stall newly DECLARED this tick), ``0`` (no change to the declared
    state), or ``-1`` (a previously-declared stall CLEARED this tick). Mutates
    ``detector_state`` in place (the in-memory streak counters), mirroring how
    the probe/recovery maintain per-process streaks.

    The genuine-stall predicate (the whole point of this card):
      * some board has spawnable ready/review work AND that board has room
        under ``max_in_progress`` (``roomy_spawnable`` non-empty), AND
      * the fleet spawned ZERO new workers this tick
        (``total_running`` did not increase since the previous tick).

    Everything else is NOT a stall: no spawnable work (correctly idle), or all
    spawnable boards are AT capacity (trigger 1 — healthy, never page), or a
    worker actually got spawned (dispatcher is working).
    """
    stall_ticks = detector_state.get("stall_ticks", 0)
    was_declared = detector_state.get("declared", False)
    last_running = detector_state.get("last_running_total")

    roomy = bool(capture.get("roomy_spawnable"))
    spawned = False
    if last_running is not None:
        spawned = capture.get("total_running", 0) > last_running
    detector_state["last_running_total"] = capture.get("total_running", 0)

    if roomy and not spawned:
        stall_ticks += 1
    else:
        stall_ticks = 0
    detector_state["stall_ticks"] = stall_ticks
    detector_state["peak_stall_ticks"] = max(
        detector_state.get("peak_stall_ticks", 0), stall_ticks)

    if not was_declared and stall_ticks >= DISPATCHER_DETECT_TICKS:
        detector_state["declared"] = True
        detector_state["declared_since"] = now
        return 1, True
    if was_declared and (not roomy or spawned):
        # Stall cleared: a worker spawned (dispatcher working again), or the
        # work drained / every board filled up (nothing left to spawn).
        detector_state["declared"] = False
        detector_state["declared_since"] = None
        detector_state["stall_ticks"] = 0
        detector_state["peak_stall_ticks"] = 0
        return -1, False
    if was_declared:
        # Still mid-stall this tick.
        return 0, True
    return 0, False


def check_dispatcher_wedge(kanban_db=None, max_in_progress=None, now=None,
                           write_state_fn=None, max_per_profile=None):
    """The dispatcher-wedge probe (the ``dispatcher`` CHECK STREAM).

    Runs on the daemon's periodic cadence (see ``PERIODIC_INTERVALS``). Reads
    kanban state, makes one stall-tick decision, and writes the ``dispatcher``
    state stream. Returns True (ok) unless a genuine stall is currently
    DECLARED — matching how the other health checks return their ``ok``.

    The stream's ``ok`` is what propagates everywhere: ``hscc status`` FAIL
    row, ``hscc verify`` red, ``state.dispatcher.degraded`` trigger event.

    Injectable: ``kanban_db`` (fake lib), ``max_in_progress`` (avoids reading
    the operator's real config in tests), ``now`` (clock), ``write_state_fn``
    (avoids touching the real state dir).
    """
    from .state import now_iso, write_state
    write_state_fn = write_state_fn or write_state
    if now is None:
        now = time.time()

    capture = _capture_kanban(kanban_db=kanban_db,
                              max_per_profile=max_per_profile,
                              max_in_progress=max_in_progress)

    if capture.get("unreachable"):
        # Cannot read the board DBs — fail SAFE (do not page on our own
        # inability to read) but be transparent in the stream. This is the one
        # place a red stream would be a false alarm, so stay green.
        write_state_fn(DISPATCHER_STREAM, {
            "ok": True, "unreadable": capture["unreachable"],
            "last_check": now_iso(), "message": "cannot read kanban state — "
            "skipping detection (fail-safe)"})
        log(f"Dispatcher-wedge check: kanban unreadable "
            f"({capture['unreachable']}) — not declaring a stall")
        return True

    change, stall = evaluate_stall_tick(capture, _detector, now)

    declared = _detector["declared"]
    details = {
        "boards": capture["boards"],
        "spawnable": capture["spawnable"],
        "roomy_spawnable": capture["roomy_spawnable"],
        "at_capacity": capture["at_capacity"],
        "total_running": capture["total_running"],
        "max_in_progress": capture["max_in_progress"],
        "stall_ticks": _detector["stall_ticks"],
        "errors": capture["errors"],
    }

    if change == -1:
        # A declared stall cleared — report the duration in the recovery end
        # of the logs/message only; here we just go green.
        log("Dispatcher-wedge check: genuine stall CLEARED (work drained / "
            "boards at capacity) — stream back to OK")

    if not declared:
        write_state_fn(DISPATCHER_STREAM, {
            "ok": True, **details,
            "last_check": now_iso(),
            "message": "dispatcher healthy — no genuine stall "
                       "(%s)" % _healthy_reason(capture),
        })
        log(f"Dispatcher-wedge check: OK — "
            f"total_running={capture['total_running']}, "
            f"spawnable={capture['spawnable']}, "
            f"roomy={capture['roomy_spawnable']}")
        return True

    # A genuine stall is DECLARED: surface it. This is what turns the lights
    # red everywhere.
    stalled_for = None
    if declared and _detector.get("declared_since"):
        stalled_for = round(now - _detector["declared_since"])
    write_state_fn(DISPATCHER_STREAM, {
        "ok": False, **details, "declared": True,
        "stalled_for_s": stalled_for,
        "last_check": now_iso(),
        "message": "GENUINE STALL: spawnable ready work with free worker "
                   "slots, but 0 workers spawned for "
                   ">= %ss — dispatcher is wedged" % DISPATCHER_DETECT_TICKS,
    })
    log(f"Dispatcher-wedge check: GENUINE STALL detected — spawnable="
        f"{capture['spawnable']}, roomy={capture['roomy_spawnable']}, "
        f"total_running={capture['total_running']}", "ERROR")
    return False


def _healthy_reason(capture):
    if not capture.get("spawnable"):
        return "no spawnable ready work (correctly idle)"
    if not capture.get("roomy_spawnable"):
        return (f"all spawnable boards at capacity "
                f"(max_in_progress={capture.get('max_in_progress')})")
    return "worker recently spawned"


# ── Guarded recovery (self-heal) ───────────────────────────────────────────
def _load_recover_state(state_file=None):
    path = state_file or _recover_state_path()
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _save_recover_state(state, state_file=None):
    path = state_file or _recover_state_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError:
        pass


def recover_dispatcher_wedge(stream_state=None, restart_fn=None, now=None,
                             state_file=None):
    """One guarded self-heal pass for a wedged dispatcher. Never raises.

    Consumes ``stream_state`` (the latest ``dispatcher`` stream; if None it is
    read from the state dir) and restarts the gateway — but ONLY after:

      * the probe has DECLARED a genuine stall (``ok: False``) for M
        consecutive detections (``DISPATCHER_RESTART_AFTER_DETECTIONS``),
      * outside the cooldown window,
      * under the max-attempt cap.

    Once the cap is hit we STOP acting and keep alerting, exactly like
    ``recover.py``. The restart is injectable (``restart_fn``); production
    defaults to ``_run_restart`` (the real ``launchctl kickstart``), tests pass
    a fake so no live gateway is ever touched.

    Returns ``{"result": ...}`` where result is one of ``skipped``
    (intentional), ``none-wedged`` (no declared stall / below M), ``suppressed``
    (declared but held by cooldown/cap), ``recovered`` (restarted), or
    ``gave-up`` (attempt cap reached).
    """
    global _recovery_detection_streak
    if now is None:
        now = time.time()

    if stream_state is None:
        from .state import read_state
        stream_state = read_state(DISPATCHER_STREAM)
    stream = stream_state or {}

    # The probe's verdict: only a DECLARED genuine stall (ok: False) counts.
    if stream.get("ok") is not False:
        _recovery_detection_streak = 0
        return {"result": "none-wedged",
                "message": "dispatcher stream not red — nothing to recover",
                "actions": []}

    # M-consecutive-detections debounce: the stall must be confirmed red on
    # several consecutive daemon ticks before the heavy restart is considered.
    _recovery_detection_streak += 1
    if _recovery_detection_streak < DISPATCHER_RESTART_AFTER_DETECTIONS:
        return {"result": "none-wedged",
                "message": (f"stall declared but only "
                            f"{_recovery_detection_streak}/{DISPATCHER_RESTART_AFTER_DETECTIONS} "
                            "consecutive detections — waiting"),
                "actions": []}

    state = _load_recover_state(state_file)
    if state.get("gave_up"):
        log("Dispatcher-wedge recovery: GAVE UP — keeping alerting, not acting")
        return {"result": "gave-up",
                "message": "attempt cap reached — keeping alerting, not acting",
                "actions": []}

    last = state.get("last_recovery_at") or 0.0
    if last and (now - last) < DISPATCHER_RECOVER_COOLDOWN_SECONDS:
        since = now - last
        log(f"Dispatcher-wedge recovery: inside cooldown "
            f"({since:.0f}s < {DISPATCHER_RECOVER_COOLDOWN_SECONDS}s) — "
            "suppressing")
        return {"result": "suppressed", "reason": "cooldown", "actions": []}

    attempts = state.get("attempts", 0)
    if attempts >= DISPATCHER_RECOVER_MAX_ATTEMPTS:
        state["gave_up"] = True
        _save_recover_state(state, state_file)
        log(f"Dispatcher-wedge recovery: reached {attempts} attempts — GIVING "
            "UP (stop acting, keep alerting)")
        return {"result": "gave-up", "reason": "max_attempts",
                "attempts": attempts, "actions": []}

    # -- Act: restart the gateway -------------------------------------------
    restart_fn = restart_fn if restart_fn is not None else _run_restart
    try:
        outcome = restart_fn()
    except Exception as e:  # noqa: BLE001 - recovery must NEVER raise
        # A raising restart runner (e.g. launchctl missing) is a failed
        # restart, recorded and swallowed — never crash the daemon thread.
        outcome = {"success": False, "error": str(e)}
    attempts += 1
    state["attempts"] = attempts
    state["last_recovery_at"] = now
    _save_recover_state(state, state_file)
    # Reset the M-debounce so a restart requires M fresh red ticks again; the
    # cooldown then prevents a same-stall second restart.
    _recovery_detection_streak = 0

    action = {"action": "restart", "attempt": attempts,
              "success": bool(outcome.get("success"))}
    if outcome.get("error"):
        action["error"] = outcome["error"]
    elif outcome.get("output"):
        action["output"] = outcome["output"]
    ok = bool(outcome.get("success"))
    log(f"Dispatcher-wedge recovery: restart invoked (attempt {attempts}, "
        f"success={ok})")
    return {"result": "recovered", "restart_ok": ok, "actions": [action]}
