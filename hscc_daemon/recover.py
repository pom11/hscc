"""Auto-recovery for a wedged serving unit (operator-approved ACTING automation).

v1.14.1 shipped ``check_engine_wedge`` (E stream): it probes EVERY unit in
serving.json with a real generation and treats a 200-with-no-returned-text as a
wedge. That probe only ALERTS (via the ``state.engine_wedge.degraded`` trigger
rule). This module is the operator-approved ACTING half: when a unit has been
wedged for N consecutive checks it restarts JUST that unit, using the same
``hscc cluster stop <container_id>`` + ``hscc cluster up`` wrappers the operator
used by hand on 2026-08-27.

It is deliberately a sibling of the probe, and does NOT touch
``check_engine_wedge``'s detection logic. It CONSUMES the probe's verdict (the
latest ``state.engine_wedge`` stream's ``wedged`` list) and applies its own
guards on top.

Guards (the point of this card — not decoration):
  * fire only after ``RECOVER_CONSECUTIVE_WEDGES`` (default 3) CONSECUTIVE
    checks report the unit wedged; never on a single stall.
  * ``RECOVER_COOLDOWN_SECONDS`` (default 30 min) between recovery attempts on
    a unit, so a unit that wedges again immediately is not restart-looped.
  * ``RECOVER_MAX_ATTEMPTS`` (default 3) consecutive-attempt cap; after it we
    STOP acting, keep alerting, and record ``gave_up`` — never restart forever.
  * never act while ``~/.hscc/watchdog-block.json`` has
    ``intentional == "autodown"``, and never while a unit is still LOADING (the
    probe's ``wedged`` list already excludes loading units — respected here).
  * act on ONLY the wedged workload's container (``hscc cluster stop <id>``);
    a healthy sibling unit is never stopped.
  * reuse autodown's O_EXCL exclusivity lockfile, so a recovery and an
    autodown teardown/wake can NEVER run concurrently.
  * every action logged: which unit, why it fired, attempt number, outcome.

External side-effects are injectable (stream state, container-id resolver,
stop/up runners, lock acquire/release, intentional-window check, clock, state
file path) so tests run with fakes and ZERO real subprocess/HTTP calls and
never touch live operator state.
"""

import json
import os
import sys
import time

from .daemon_ops import log
from .state import now_iso

RECOVER_STATE_FILE = os.path.expanduser("~/.hscc/recover.json")

# N CONSECUTIVE wedge detections before the recovery fires. Default 3: the
# probe already debounces (ENGINE_WEDGE_THRESHOLD) before declaring a wedge, so
# this is a SECOND, recovery-specific debounce — several consecutive declared
# wedges across separate checks, not a single stall.
RECOVER_CONSECUTIVE_WEDGES = int(
    os.environ.get("HSCC_RECOVER_CONSECUTIVE_WEDGES", "3"))
# Min seconds between recovery attempts on the same unit (default 30 min).
RECOVER_COOLDOWN_SECONDS = int(
    os.environ.get("HSCC_RECOVER_COOLDOWN_SECONDS", "1800"))
# Max consecutive recovery attempts on a unit before we give up acting and only
# keep alerting.
RECOVER_MAX_ATTEMPTS = int(
    os.environ.get("HSCC_RECOVER_MAX_ATTEMPTS", "3"))

# How often the daemon recovery thread runs (seconds). Must match the
# engine_wedge probe cadence so each recovery "check" corresponds to one probe
# check.
RECOVER_CHECK_INTERVAL = int(
    os.environ.get("HSCC_RECOVER_CHECK_INTERVAL", "60"))

# In-memory per-unit consecutive-wedge streak, keyed by unit id (same key the
# probe uses). Lives only for the daemon process: a restart re-probes and
# re-counts, consistent with the probe's own in-memory `_engine_wedge_units`.
# This is the N-consecutive-detections guard's counter. It is NOT persisted —
# cooldown/attempts live in recover.json, which survives restart.
_recover_units = {}

# Cached loaded hscc-cluster engine (load once, reuse across ticks).
_cluster_engine = None


def _unit_key(u):
    """Stable key mirroring the probe's `health._engine_wedge_unit_key`."""
    if isinstance(u, dict):
        uid = u.get("unit") or u.get("id")
        if uid:
            return str(uid)
        node = u.get("node") or u.get("nodes") or ""
        node = node[0] if isinstance(node, (list, tuple)) else node
        return f"{node}:{u.get('port', '?')}"
    return str(u)


# ── Cluster wrapper hooks (call the hscc wrappers, never raw sparkrun) ───
def _load_cluster_engine():
    """Load the hscc-cluster plugin's hscc.py as a library.

    Same mechanism as ``hscc_daemon/hscc.py:_load_cluster_engine``. The
    recovery acts through the ops wrapper's ``cmd_cluster_status`` /
    ``cmd_stop`` / ``cmd_cluster_up`` functions (which shell out to sparkrun),
    the way autodown does — it never builds raw sparkrun commands. Returns the
    module, or None (callers abort: without the wrappers we cannot act safely).
    """
    global _cluster_engine
    if _cluster_engine is not None:
        return _cluster_engine
    from pathlib import Path
    import importlib.util
    cluster_dir = Path(__file__).resolve().parent.parent / "hscc-cluster"
    cluster_hscc = cluster_dir / "hscc.py"
    if not cluster_hscc.is_file():
        return None
    if str(cluster_dir) not in sys.path:
        sys.path.insert(0, str(cluster_dir))
    spec = importlib.util.spec_from_file_location(
        "hscc_cluster_engine", str(cluster_hscc))
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _cluster_engine = mod
    return mod


def _resolve_container_id(status_result, unit):
    """Best-effort map a wedged unit to its sparkrun container id.

    `hscc cluster status` reports workloads keyed by job/recipe name
    (``cmd_cluster_status()`` returns a ``workloads`` list with ``name`` +
    ``container_id``). A unit's container is the workload whose name contains
    the unit's concrete model id or recipe filename stem (the stable identity
    the operator sees in `hscc cluster status`).

    SAFETY: a unique match is required. If zero OR multiple workloads match
    (ambiguous), return None — the caller then records "cannot resolve
    container" and issues NO stop. A healthy sibling unit is never at risk of
    being stopped by a misidentified container.
    """
    workloads = (status_result or {}).get("workloads") or []
    if not workloads:
        return None
    needle = None
    model = (unit.get("model") or "").strip() if isinstance(unit, dict) else ""
    if model:
        needle = model
    else:
        recipe = (unit.get("recipe") or "").strip() if isinstance(unit, dict) else ""
        if recipe:
            needle = os.path.splitext(os.path.basename(recipe))[0]
    if not needle:
        return None
    matches = [w for w in workloads if needle in (w.get("name") or "")]
    if len(matches) == 1:
        cid = (matches[0].get("container_id") or "").strip()
        if cid and cid != "?":
            return cid
    return None


# ── Persistent recovery state (recover.json): cooldown + attempt cap ─────
def _load_recover_state(state_file=None):
    """Load persistent recovery state from ``state_file`` (or default)."""
    path = state_file or RECOVER_STATE_FILE
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("units"), dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"units": {}}


def _save_recover_state(state, state_file=None):
    """Atomically persist recovery state (tmp + os.replace). Never raises."""
    path = state_file or RECOVER_STATE_FILE
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, path)
    except OSError:
        pass


# ── The guarded recovery pass ────────────────────────────────────────────
def recover_engine_wedge(stream_state=None, resolve_container=None,
                         status_fn=None, stop_fn=None, up_fn=None,
                         lock_acquire=None, lock_release=None,
                         intentional_fn=None, now=None, state_file=None):
    """One guarded recovery pass. Returns a dict (never raises).

    Consumes ``stream_state`` (the latest ``state.engine_wedge`` stream; if
    None it is read from disk), and for every unit the probe declared wedged,
    applies the guards then — if all pass — restarts ONLY that unit's container
    via the hscc wrappers.

    Injectable hooks (defaults go to the real cluster wrappers / autodown
    lock / lifecycle block, so production just calls
    ``recover_engine_wedge()`` and tests pass fakes without touching the live
    cluster):

      * ``stream_state``  — the engine_wedge stream dict (default: read_state).
      * ``status_fn``     — ``hscc cluster status`` wrapper → {workloads:[...]}.
      * ``resolve_container(status_result, unit) -> str|None`` — container id
        for a wedged unit (default: ``_resolve_container_id``).
      * ``stop_fn(cid)``  — ``hscc cluster stop <cid>`` wrapper → result dict.
      * ``up_fn()``       — ``hscc cluster up`` wrapper → result dict.
      * ``lock_acquire``/``lock_release`` — autodown's O_EXCL lock functions
        (defaults: ``autodown._acquire_lock`` / ``_release_lock``).
      * ``intentional_fn`` — True iff an intentional autodown is in effect
        (default: read ``lifecycle.load_watchdog_block``).
      * ``now``           — wall clock for cooldown math (default: time.time).
      * ``state_file``    — persist path for cooldown/attempts (default:
        ``RECOVER_STATE_FILE``).

    Returns ``{"result": ...}`` where result is one of:
      ``skipped`` (intentional autodown / lock busy),
      ``none-wedged`` (no unit crossed the N-consecutive threshold),
      ``recovered`` (one or more units restarted), or
      ``gave-up`` (attempt cap reached — stopped acting).
    """
    now = now if now is not None else time.time()

    # Read the probe's verdict (the latest engine_wedge stream).
    if stream_state is None:
        from .state import read_state as _read
        from .health import ENGINE_WEDGE_STREAM, check_engine_wedge  # noqa
        stream_state = _read(ENGINE_WEDGE_STREAM)
    stream = stream_state or {}

    # -- Guard: intentional autodown -------------------------------------
    # Never act while an intentional autodown is in effect — the serving layer
    # (or a wake) is operator-managed; an automated restart would fight it.
    if intentional_fn is None:
        from .lifecycle import load_watchdog_block as _lwb
        def _default_intentional():
            block = _lwb()
            return bool(block) and block.get("intentional") == "autodown"
        intentional_fn = _default_intentional
    if intentional_fn():
        log("Engine-wedge recovery: intentional autodown in effect — skipping")
        return {"result": "skipped", "reason": "intentional autodown",
                "actions": []}

    wedged = stream.get("wedged") or []
    wedged_keys = {_unit_key(u) for u in wedged}

    # Update the in-memory N-consecutive streak: increment every wedged unit;
    # reset every unit the probe reported in ANY other status this tick.
    for key in wedged_keys:
        _recover_units.setdefault(key, {"wedge_streak": 0})["wedge_streak"] += 1
    reported_other = set()
    for lst in ("ok_units", "loading", "down"):
        for u in (stream.get(lst) or []):
            reported_other.add(_unit_key(u))
    for key in reported_other:
        _recover_units.setdefault(key, {"wedge_streak": 0})["wedge_streak"] = 0

    # Candidate units that crossed the N-consecutive threshold THIS tick.
    candidates = []
    for u in wedged:
        key = _unit_key(u)
        streak = _recover_units.get(key, {}).get("wedge_streak", 0)
        if streak >= RECOVER_CONSECUTIVE_WEDGES:
            candidates.append((key, u, streak))

    if not candidates:
        return {"result": "none-wedged",
                "message": "no unit has N consecutive wedge detections yet",
                "actions": []}

    # -- Guard: exclusivity lock ------------------------------------------
    # Reuse autodown's O_EXCL lockfile so a recovery can never run concurrently
    # with an autodown teardown/wake (they hold the same lock).
    if lock_acquire is None or lock_release is None:
        from . import autodown as _ad
        lock_acquire = _ad._acquire_lock
        lock_release = _ad._release_lock
    if not lock_acquire(now=now):
        log("Engine-wedge recovery: autodown lock held (teardown/wake in "
            "flight) — skipping this pass")
        return {"result": "skipped", "reason": "lock held", "actions": []}
    try:
        return _recover_locked(candidates, stream=stream,
                               resolve_container=resolve_container,
                               status_fn=status_fn, stop_fn=stop_fn,
                               up_fn=up_fn, now=now, state_file=state_file)
    finally:
        lock_release()


def _recover_locked(candidates, stream=None, resolve_container=None,
                    status_fn=None, stop_fn=None, up_fn=None, now=None,
                    state_file=None):
    """The recovery sequence, run while holding the autodown O_EXCL lock.

    Split out of ``recover_engine_wedge`` so the O_EXCL lock acquisition +
    release live in exactly one wrapper (begin / ``finally``), mirroring
    ``autodown.teardown``. All gates and per-unit logic live here, under the
    lock — so no concurrent autodown/wake can interleave with the stops.

    Each candidate unit is handled independently, so a healthy sibling unit is
    never stopped (only each wedged unit's own container is stopped, then one
    fleet ``hscc cluster up`` relaunches the wedged/missing units).
    """
    stream = stream or {}
    now = now if now is not None else time.time()
    state = _load_recover_state(state_file)
    loading_keys = {_unit_key(u) for u in (stream.get("loading") or [])}
    healthy_keys = {_unit_key(u) for u in (stream.get("ok_units") or [])}

    if resolve_container is None:
        resolve_container = _resolve_container_id
    if status_fn is None or stop_fn is None or up_fn is None:
        cl = _load_cluster_engine()
        if cl is None:
            log("Engine-wedge recovery: cannot load hscc-cluster wrappers — "
                "no stop issued")
            return {"result": "failed", "reason": "cluster engine unavailable",
                    "actions": []}
        if status_fn is None:
            status_fn = cl.cmd_cluster_status
        if stop_fn is None:
            stop_fn = cl.cmd_stop
        if up_fn is None:
            up_fn = cl.cmd_cluster_up

    # Resolve container ids for all candidate units in ONE status call, so a
    # healthy sibling is never in the stop set.
    status_result = status_fn()
    by_key = {}
    for key, u, streak in candidates:
        cid = resolve_container(status_result, u)
        by_key[key] = cid
        log(f"Engine-wedge recovery: unit {key!r} streak={streak} — "
            f"resolved container_id={cid!r}")

    actions = []
    stop_ids = []
    gave_up_any = False
    acted_any = bool(candidates)

    for key, u, streak in candidates:
        unit_state = state["units"].setdefault(key, {
            "attempts": 0, "last_recovery_at": 0.0, "gave_up": False,
            "gave_up_at": None,
        })

        # A unit we just recovered that is now healthy gets a fresh attempt
        # budget + cleared cooldown (it healed), so a LATER distinct failure
        # starts from zero, not from a stale gave-up.
        if key in healthy_keys:
            unit_state["attempts"] = 0
            unit_state["gave_up"] = False
            unit_state["gave_up_at"] = None

        # -- Guard: max consecutive-attempt cap (gave up) -------------------
        if unit_state["gave_up"]:
            log(f"Engine-wedge recovery: unit {key!r} GAVE UP after "
                f"{unit_state['attempts']} attempts — keeping alerting, not "
                "acting")
            actions.append({"unit": key, "action": "skip",
                            "reason": "gave_up",
                            "attempt": unit_state["attempts"]})
            continue

        # -- Guard: cooldown window ------------------------------------------
        last = unit_state.get("last_recovery_at") or 0.0
        since = now - last
        if last and since < RECOVER_COOLDOWN_SECONDS:
            log(f"Engine-wedge recovery: unit {key!r} inside cooldown "
                f"({since:.0f}s < {RECOVER_COOLDOWN_SECONDS}s) — suppressing")
            actions.append({"unit": key, "action": "skip",
                            "reason": "cooldown",
                            "attempt": unit_state["attempts"]})
            continue

        # -- Guard: a loading unit is not a wedged unit ----------------------
        # The probe's `wedged` list already excludes loading units, so this is
        # belt-and-braces: refuse to act on a unit the stream ALSO marks as
        # loading.
        if key in loading_keys:
            actions.append({"unit": key, "action": "skip",
                            "reason": "loading",
                            "attempt": unit_state["attempts"]})
            continue

        # -- Attempt cap check (pre-increment) --------------------------------
        if unit_state["attempts"] >= RECOVER_MAX_ATTEMPTS:
            unit_state["gave_up"] = True
            unit_state["gave_up_at"] = now_iso()
            log(f"Engine-wedge recovery: unit {key!r} reached "
                f"{RECOVER_MAX_ATTEMPTS} attempts — GIVING UP (stop acting, "
                "keep alerting)")
            gave_up_any = True
            actions.append({"unit": key, "action": "gave_up",
                            "reason": "max_attempts",
                            "attempt": unit_state["attempts"]})
            continue

        # -- Act: stop ONLY the wedged unit's container, then fleet up -------
        cid = by_key[key]
        if not cid:
            log(f"Engine-wedge recovery: unit {key!r} — no container_id "
                "resolved; no stop issued (fail-safe)")
            actions.append({"unit": key, "action": "skip",
                            "reason": "no_container_id",
                            "attempt": unit_state["attempts"]})
            continue

        stop_result = stop_fn(cid)
        stop_ok = bool(stop_result) and (
            stop_result.get("success", stop_result.get("ok", False)))
        unit_state["attempts"] += 1
        unit_state["last_recovery_at"] = now
        actions.append({"unit": key, "action": "stop",
                        "container_id": cid, "attempt": unit_state["attempts"],
                        "stop_ok": stop_ok,
                        "output": (stop_result or {}).get("output", "")})
        log(f"Engine-wedge recovery: stopped unit {key!r} "
            f"(container {cid!r}, attempt {unit_state['attempts']}, "
            f"stop_ok={stop_ok})")
        if cid not in stop_ids:
            stop_ids.append(cid)

    # One fleet `hscc cluster up` — re-derives every unit's serve command and
    # carries --served-model-name, so aliases survive (exactly what the operator
    # ran by hand). Only run when we actually stopped something.
    up_out = None
    if stop_ids and up_fn:
        log("Engine-wedge recovery: relaunching missing units via "
            "`hscc cluster up`")
        up_result = up_fn()
        up_ok = bool(up_result) and (
            up_result.get("success", up_result.get("ok", False)))
        up_out = {"ok": up_ok, "output": (up_result or {}).get("output", "")}
        actions.append({"action": "up", "result": up_out})
    elif stop_ids and not up_fn:
        log("Engine-wedge recovery: up_fn unavailable — units stopped but not "
            "relaunched")
        up_out = {"ok": False, "reason": "up_fn unavailable"}

    _save_recover_state(state, state_file)

    if gave_up_any:
        result = "gave-up"
    elif any(a.get("action") == "stop" for a in actions):
        result = "recovered"
    elif acted_any:
        # Candidates crossed the N-consecutive threshold but every one was
        # suppressed by a guard (cooldown / gave_up / loading / no container).
        result = "suppressed"
    else:
        result = "none-wedged"
    return {"result": result, "actions": actions, "up": up_out}
