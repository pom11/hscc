"""Idle autodown/autoup — config + core state module (Phase 1).

Owns ``~/.hscc/autodown.json`` (schema in docs/design/idle-autodown.md §7)
and the pure decision helpers that later phases (2-8) build the daemon loop,
teardown, wake, and CLI on top of.

Phase 1 scope ONLY: config load/save, the single activity choke point, the
kanban idle predicate, and the unit classification table. No daemon loop, no
``cycle()``, no teardown, no wake, no CLI wiring — those are phases 2-8.

Every function here is pure or operates on an injectable path/db so it is
testable without the daemon running and without touching the real ~/.hscc or
~/.hermes.
"""

import datetime
import json
import os

from .daemon_ops import log
from .state import now_iso
from .desktop import send_macos_notification
from .telegram import notify_operations

# Path to the autodown state+config file. Overridden in tests via
# monkeypatch, mirroring lifecycle.WATCHDOG_BLOCK_FILE
# (hscc_daemon/lifecycle.py:124).
AUTODOWN_FILE = os.path.expanduser("~/.hscc/autodown.json")

# Default configuration (docs/design/idle-autodown.md §7). C5: OFF by default.
# A new file starts disabled; the file is created when autodown is first
# enabled.
DEFAULT_CONFIG = {
    "enabled": False,            # C5: OFF by default
    "idle_minutes": 10,          # default 10; 0 = only via explicit wake
    "state": "up",               # one of: up | waking | down
    "last_activity_iso": None,   # advanced by every activity source (§1d)
    "down_since": None,
    "wake_source": None,
    "wake_at": None,
    "cancel_requested": False,
    "reason": "",
}

# Statuses that are terminal/parked — the only ones that do NOT count as live
# or imminent work. Derived from the exclusion list in hscc.py:112 (done,
# review, archived, blocked) with ``review`` removed: a card awaiting review is
# work a reviewer is about to pick up (design §1a), so it counts as active.
# Everything else (running, ready, review, qa, in_progress, todo, scheduled,
# triage, ...) is treated as conservative-active: "any ambiguous state counts
# as work" (§1a). Missing/unknown statuses are NOT terminal and therefore count
# as active (fail-safe — never tear down on a signal we can't positively clear).
TERMINAL_STATUSES = frozenset({"done", "archived", "blocked"})


def load_config():
    """Load ``~/.hscc/autodown.json``, failing closed to the disabled default.

    Fail-closed (C5, §8 "config corrupt"): an absent file, unreadable file, or
    corrupt/invalid JSON is treated as DISABLED (``enabled: False``), never as
    enabled — a corrupt config can never cause a teardown. The returned dict
    is always a valid, complete config.
    """
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(AUTODOWN_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    # Merge over the defaults so a partial file (or a hand-edited one with a
    # field removed) still yields a complete config. Absent/invalid values
    # fall back to the default — never to enabled.
    for key, default in DEFAULT_CONFIG.items():
        cfg[key] = data.get(key, default)
    return cfg


def save_config(cfg):
    """Persist ``cfg`` to ``~/.hscc/autodown.json`` atomically.

    Writes to a temp file then ``os.replace`` so a crash mid-write can never
    leave a corrupt (partially-written) config that would then fail closed and
    confuse the operator. Reuses the exact atomic pattern of
    state.write_state (hscc_daemon/state.py:31-35) and
    lifecycle.save_watchdog_block (hscc_daemon/lifecycle.py:143-146).
    """
    os.makedirs(os.path.dirname(AUTODOWN_FILE) or ".", exist_ok=True)
    tmp = AUTODOWN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, default=str)
    os.replace(tmp, AUTODOWN_FILE)


def record_activity(source):
    """Advance the idle timer: set ``last_activity_iso`` to now (UTC ISO 8601).

    The single choke point every activity source (§1d) calls. Recorded in the
    config file on disk. Safe to call when the config file does not exist yet:
    load_config() returns the disabled default, and we persist it (still
    disabled) with the timestamp advanced rather than crashing.
    """
    cfg = load_config()
    cfg["last_activity_iso"] = now_iso()
    cfg["wake_source"] = source
    save_config(cfg)
    return cfg


def _has_active_work(kanban_db=None):
    """True if ANY kanban task is in a state meaning work is live or imminent.

    The idle predicate of design §1a. True ⇒ NOT idle ⇒ never tear down. False
    only when the board is genuinely quiet (every task is in a terminal/parked
    status: done / archived / blocked).

    ``kanban_db`` is an injectable Hermes kanban library (an object exposing a
    ``connect_closing()`` context manager yielding a sqlite connection), so
    tests never touch the real ~/.hermes/kanban.db. When omitted, it imports
    the Hermes ``hermes_cli.kanban_db`` module lazily (same logic path as the
    fleet's flightdeck/core/kanban.py::_load_kanban_db). Reads the live +
    parked tables only, never ``archived`` cards. Any DB read failure returns
    True (conservative — treat unreadable as active so we never tear down on a
    signal we could not verify).
    """
    if kanban_db is None:
        kanban_db = _load_kanban_db_or_default()
    if kanban_db is None:
        # Could not reach Hermes' kanban lib — fail safe, do not consider idle.
        return True

    try:
        with kanban_db.connect_closing() as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks "
                "WHERE status IS NULL "
                "   OR status NOT IN ('done', 'archived', 'blocked') "
                "LIMIT 1"
            ).fetchone()
    except Exception:
        # Unreadable / missing DB ⇒ treat as active (safe: never tear down).
        return True
    return row is not None


def _load_kanban_db_or_default():
    """Import Hermes' ``hermes_cli.kanban_db`` lazily, or None on failure.

    Kept in its own function so ``_has_active_work`` (and this module) never
    depend on Hermes being installed at import time, and so tests can inject.
    Mirrors flightdeck/core/kanban.py::_load_kanban_db (line 86) which does the
    same deferred import.
    """
    try:
        from hermes_cli import kanban_db  # noqa: PLC0415
    except Exception:
        return None
    return kanban_db


def classify(idle_state_block, autodown_state):
    """Classify the serving layer from the watchdog block + autodown state.

    The unit classification decision table of design §5. Pure function: given
    the watchdog block dict (from lifecycle.load_watchdog_block) and the
    autodown config dict (from load_config), classify the (non-keepalive)
    serving layer as one of:

    ``expected_down``
        The layer is intentionally down by autodown: the watchdog block is set
        with ``intentional == "autodown"`` and autodown state is ``"down"``.
        The watchdog must NOT resurrect it.
    ``should_be_up``
        The block is latched with ``intentional == "autodown"`` but the layer
        is not confirmed down (state is ``waking``/``up``/missing) — an
        in-progress or failed transition. The layer should be up: finish the
        wake (or resume supervision). Never leave it parked.
    ``healthy``
        No intentional autodown block — the watchdog supervises normally.

    Later phases extend this per-unit (which specific units are in the
    teardown set); Phase 1 provides the layer-level decision table the
    intentional-aware watchdog fork consults.
    """
    autodown_state = autodown_state or {}
    idle_state_block = idle_state_block or {}

    blocked = bool(idle_state_block.get("blocked"))
    intentional = idle_state_block.get("intentional")
    ad_state = autodown_state.get("state", "up")

    if blocked and intentional == "autodown" and ad_state == "down":
        return "expected_down"
    if blocked and intentional == "autodown":
        # Block latched but layer not confirmed down (waking/up/missing).
        return "should_be_up"
    return "healthy"


# ---------------------------------------------------------------------------
# Phase 3 — cycle() idle evaluation + safety interlocks (§1, §6)
# ---------------------------------------------------------------------------

# Default path to the fleet agents.json (health.py:551). Overridable in tests
# so cycle() never reads the real ~/.hscc.
AGENTS_FILE = os.path.expanduser("~/.hscc/agents.json")


def _parse_iso(ts):
    """Parse an ISO 8601 timestamp into an aware datetime, or None on failure.

    Fail-safe helper for the elapsed-window measure (§1c): a value we cannot
    parse is NOT idle. ``fromisoformat`` accepts the ``+00:00`` / ``Z`` / offset
    forms produced by ``now_iso()``.
    """
    try:
        return datetime.datetime.fromisoformat(ts)
    except (ValueError, TypeError, AttributeError):
        return None


def _window_elapsed(cfg, now=None):
    """True when ``now - last_activity_iso >= idle_minutes`` (§1c/§6.3).

    Fail-safe in both ambiguous directions:
    - A NULL/absent ``last_activity_iso`` does NOT count as \"infinitely idle\"
      (§1e warm-up/first-boot guard). We treat it as \"activity just now\" — stamp
      it with ``now`` and return False — so a fresh install can never
      immediately tear down.
    - An unparseable timestamp ⇒ not idle (never tear down on a signal we
      cannot verify).
    - ``idle_minutes <= 0`` (§7: \"0 = only via explicit wake/never auto\") ⇒
      never auto-teardown.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    idle_minutes = cfg.get("idle_minutes") or 0
    last = cfg.get("last_activity_iso")
    if not last:
        # Warm-up guard: NULL/absent ⇒ activity \"just now\". Stamp it.
        cfg["last_activity_iso"] = now.isoformat()
        try:
            save_config(cfg)
        except Exception:
            pass  # stamping best-effort; fail-safe regardless
        return False
    if idle_minutes <= 0:
        return False
    last_dt = _parse_iso(last)
    if last_dt is None:
        # Unparseable timestamp ⇒ NOT idle.
        return False
    elapsed_min = (now - last_dt).total_seconds() / 60.0
    return elapsed_min >= idle_minutes


def _agents_idle(agents_file=None):
    """True when EVERY enabled agent in agents.json is ``idle`` (§1b/§6.2).

    Idle requires no enabled agent to be mid-turn (working) or failed — the
    same status vocabulary ``health.check_heartbeat`` tallies (health.py:561,
    statuses ``idle``/``working``/``failed``). Disabled agents are not running,
    so they cannot be mid-work and do not gate idle.

    Fail-safe (mirrors §1a): an unreadable / missing / corrupt agents.json, or
    an unexpected shape, means we cannot positively verify all agents are idle
    ⇒ False (NOT idle, never tear down).
    """
    path = agents_file or AGENTS_FILE
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    agents = data.get("agents", [])
    if not isinstance(agents, list):
        return False
    for a in agents:
        if not isinstance(a, dict):
            continue
        if not a.get("enabled", True):
            continue  # disabled agent never gates idle
        if a.get("status") != "idle":
            # working / failed / anything-else ⇒ NOT idle.
            return False
    return True


def _default_keepalive_ok():
    """True iff every present keepalive unit answers healthy (§1f/§6.4).

    Keepalive units are exempt from teardown (C4), but if one is itself
    unhealthy we abort — do not tear down the orchestrator while a worker is
    mid-flight relying on it (§6.4). Fail-safe: any inability to load serving,
    resolve the unit set, or probe a port ⇒ False (abort) — we never tear down
    on an unverifiable keepalive signal. With no keepalive units present, there
    is nothing to protect ⇒ True.
    """
    try:
        from . import serving
        from .util import http_check
    except Exception:
        return False
    try:
        units = serving.keepalive_units(serving.load_serving())
    except Exception:
        return False
    if not units:
        return True  # no keepalive units to be unhealthy
    for u in units:
        try:
            url = f"http://{u['node']}:{u['port']}/health"
            if not http_check(url, timeout=5).get("ok"):
                return False
        except Exception:
            return False
    return True


def _is_idle(cfg, kanban_db=None, agents_file=None, now=None,
             keepalive_ok=None):
    """Evaluate the FULL idle conjunction (§1/§6) — all must hold.

    ``True`` only when every interlock positively clears. Any single false, or
    any signal we could not verify, ⇒ ``False`` (not idle, no teardown).
    Small helpers are injectable (Phase 1 ``_has_active_work(kanban_db=...)``,
    ``agents_file`` for §1b, ``now`` for the clock, ``keepalive_ok`` for §6.4)
    so cycle() is unit-testable without the daemon or the real ~/.hscc /
    ~/.hermes.
    """
    # 6.1 Kanban work (§1a) — _has_active_work True ⇒ active ⇒ not idle.
    if _has_active_work(kanban_db):
        return False
    # 6.2 Agent liveness (§1b) — every enabled agent must be idle.
    if not _agents_idle(agents_file):
        return False
    # 6.3 Elapsed window (§1c/§6.3).
    if not _window_elapsed(cfg, now):
        return False
    # 6.4 Keepalive health (§1f) — keepalive units exempt from teardown but a
    #     sick one aborts (don't tear the orchestrator out from under a worker).
    kh = keepalive_ok or _default_keepalive_ok
    try:
        if not kh():
            return False
    except Exception:
        return False  # fail-safe: unverifiable keepalive health ⇒ not idle
    return True


def _invoke_teardown():
    """Drive ``teardown()`` lazily — the Phase 4 seam.

    Phase 2 calls ``cycle`` the same way (``getattr``, missing ⇒ no-op, raising
    ⇒ caught + logged). ``teardown`` does not exist until Phase 4, so when it is
    absent cycle's idle decision simply ends the call. The full stop-order /
    watchdog-block sequence is Phase 4's job.
    """
    teardown = globals().get("teardown")
    if teardown is None:
        return  # Phase 4 not implemented yet → no teardown to run
    try:
        teardown()
    except Exception as e:
        log(f"Autodown teardown error: {e}", "ERROR")


def cycle(kanban_db=None, agents_file=None, now=None, keepalive_ok=None):
    """Idle autodown decision function (Phase 3, §1/§6).

    Called each daemon tick by ``daemon_ops.run_autodown_loop``
    (daemon_ops.py:307-317). Pure-ish and testable: every external input
    (kanban DB, agents.json path, clock) is injectable, and with no args it
    reads the real config from disk and uses the real clock — no running daemon
    required.

    Decision order:
    1. Disabled ⇒ do nothing (§7 C5, fail-closed).
    2. ``state`` in (``down``, ``waking``) ⇒ wake is Phase 5, NOT this phase.
       Return without touching anything. Clear seam for Phase 5 to hook.
    3. ``state == up`` ⇒ evaluate the full idle conjunction (§1/§6). Any single
       false, or any unverifiable signal, ⇒ NOT idle ⇒ return without teardown
       (fail-safe direction is mandatory).
    4. All clear ⇒ teardown the serving layer. ``teardown()`` is Phase 4 and
       does not exist yet — called lazily via ``_invoke_teardown`` (missing ⇒
       no-op, raising ⇒ caught + logged).
    """
    cfg = load_config()
    if not cfg.get("enabled"):
        # §7 C5: OFF by default. Do nothing, never touch the serving layer.
        return
    state = cfg.get("state")
    if state in ("down", "waking"):
        # Phase 5 owns wake/autoup. This phase does NOT touch anything.
        return
    # state == "up": evaluate the full idle predicate (§1/§6).
    if not _is_idle(cfg, kanban_db=kanban_db, agents_file=agents_file,
                    now=now, keepalive_ok=keepalive_ok):
        # Not idle — no teardown.
        return
    # All interlocks clear ⇒ teardown (Phase 4, called lazily).
    _invoke_teardown()


# ---------------------------------------------------------------------------
# Phase 4 — teardown sequence + watchdog block coordination (§3, §5)
# ---------------------------------------------------------------------------

# Reason string written into the watchdog block on intentional teardown (§3.2).
WATCHDOG_TEARDOWN_REASON = "autodown: intentional idle teardown"


def _worker_stop_cmd(nodes):
    """`sparkrun stop --hosts <nodes>` for one non-keepalive unit (§3.3).

    Mirrors the real orchestrator stop form ``serving.VLLM_STOP_CMD``
    (serving.py:150): ``sparkrun stop --hosts <node>``. We pass the EXACT
    per-unit node list from serving.json (never a catch-all that could touch
    keepalive nodes, C4). The positional port form from the design prose isn't
    used because the actual command in code carries no port.
    """
    return ["sparkrun", "stop", "--hosts", ",".join(nodes)]


def _build_teardown_plan(serving):
    """Ordered stop commands for the NON-keepalive serving units (§3).

    Returns a list of dicts, one entry per unit to stop::

        {"kind": "worker"|"orchestrator", "nodes": [...], "port": int,
         "unit_id": str, "cmd": [str, ...]}

    Order is critical: non-keepalive worker units FIRST (sorted by unit id for
    determinism), the orchestrator unit LAST (§3.3) so nothing is mid-request
    into a stopped orchestrator. Keepalive units (C4 — ``serving.keepalive_units``,
    serving.py:172) are NEVER in the set — filtered out explicitly.

    ``serving`` is the parsed serving.json dict (None/absent ⇒ empty plan,
    fail-safe: stop nothing). ``port`` is kept per entry for the verify-down
    probe in step 4 of teardown().
    """
    if not isinstance(serving, dict):
        return []
    from .serving import serving_port
    workers = []
    orch = []
    for u in (serving.get("units", []) or []):
        if not isinstance(u, dict):
            continue
        nodes = [n for n in (u.get("nodes") or []) if n]
        if not nodes:
            continue  # no nodes ⇒ nothing to stop
        role = u.get("role")
        port = u.get("port") or serving_port(serving)
        unit_id = u.get("id") or ",".join(nodes)
        if role == "orchestrator":
            # The orchestrator unit is never keepalive — stop it LAST.
            orch.append({"kind": "orchestrator", "nodes": nodes, "port": port,
                         "unit_id": unit_id, "cmd": _worker_stop_cmd(nodes)})
        elif role == "worker" and not u.get("keepalive"):
            # NON-keepalive worker ⇒ teardown target. Keepalive workers are
            # exempt (C4) and never appear here.
            workers.append({"kind": "worker", "nodes": nodes, "port": port,
                            "unit_id": unit_id, "cmd": _worker_stop_cmd(nodes)})
        # keepalive worker / unknown role ⇒ never in the teardown set.
    workers.sort(key=lambda e: e["unit_id"])
    return workers + orch


def _probe_down(node, port, http_check_fn):
    """True iff ``node:port`` no longer responds (verify-down, §3.4)."""
    try:
        res = http_check_fn(f"http://{node}:{port}/health", timeout=5)
        # Down means NOT ok — the unit is no longer answering. A probe that
        # itself errors (returns not-ok) counts as down for verification.
        return not bool(res.get("ok"))
    except Exception:
        # Unreachable / probe error ⇒ treat as down (the unit is not answering).
        return True


def _record_failure(cfg_msg):
    """Persist a teardown failure/cancel into autodown.json (§8).

    Sets ``state`` to reflect reality — the layer is NOT fully down — so
    ``classify()`` (autodown.py:191) routes the watchdog to ``should_be_up``
    (resume supervision / heal) rather than a silently-broken ``expected_down``.
    """
    cfg = load_config()
    cfg["state"] = "up"
    cfg["down_since"] = None
    cfg["reason"] = cfg_msg
    save_config(cfg)


def _notify(msg, title, priority="normal"):
    """Best-effort operator notify (desktop + ops Telegram). Never raises."""
    try:
        notify_operations(msg)
    except Exception:
        pass
    try:
        send_macos_notification(title, msg, priority=priority)
    except Exception:
        pass


def _rollback_block(original_block):
    """Restore the pre-teardown watchdog block (§3/§8 rollback).

    Wipes the ``intentional`` marker and returns ``blocked``/``reason``/
    ``blocked_at`` to their pre-teardown values (as the watchdog left them), so
    the watchdog resumes ordinary supervision and can heal whatever partial
    state a failed/cancelled teardown left behind. A half-down cluster with a
    latched intentional block is the worst possible state — never leave it.
    """
    from . import lifecycle
    lifecycle.save_watchdog_block(original_block)


def teardown(serving_path=None, run_cmd_fn=None, kanban_db=None,
             agents_file=None, now=None, keepalive_ok=None,
             http_check_fn=None):
    """Execute the idle teardown sequence (§3/§5).

    Runs in the autodown thread. Order is critical (C2) and each step is
    logged via daemon_ops.log:

      1. Re-verify idle (§6 last-line guard): re-run the full ``_is_idle``
         conjunction. If anything changed since the timer decided ⇒ ABORT,
         NO stops issued.
      2. Write the watchdog block BEFORE stopping anything (§3.2, C2):
         ``blocked:true``, reason §3.2, ``blocked_at: now``, and the NEW field
         ``intentional: "autodown"``. Block first, THEN stop — stopping first
         lets the watchdog resurrect units mid-teardown (has actually happened).
      3. Stop non-keepalive units — workers first, orchestrator LAST (§3.3),
         using the exact per-unit node list from serving.json. Keepalive units
         (C4, serving.py:172) are NEVER in the set. No catch-all sparkrun stop.
      4. Verify down: confirm the stopped ports no longer respond (§3.4).
      5. Record state: ``autodown.json`` ⇒ ``state:"down"``, ``down_since``,
         ``reason``. (``intentional:"autodown"`` lives in the WATCHDOG BLOCK
         file only — one source of truth per fact.)
      6. Notify the operator (desktop + ops Telegram; both CPU-side).

    All external side-effects are injectable (serving.json path, command
    runner, health probe, kanban/agents/clock/keepalive inputs) so tests run
    with fakes and ZERO real commands — this never touches the live cluster.

    ``cancel_requested`` in autodown.json is re-checked BEFORE each stop; if
    set, we stop cleanly, roll the block back, and report ``cancelled`` (§6).

    Returns a result dict with ``result`` in
    (``down`` | ``aborted`` | ``cancelled`` | ``failed``) plus
    ``issued`` (the list of stop commands actually issued) and the original
    ``plan``.
    """
    from . import lifecycle   # noqa: F401  (used via module)
    from . import serving as serving_mod
    from .util import http_check as _util_http_check, run_cmd as _util_run_cmd

    run_cmd = run_cmd_fn or _util_run_cmd
    http_check = http_check_fn or _util_http_check
    serving = serving_mod.load_serving(serving_path)

    # -- 1. Re-verify idle (§6 last-line guard) ---------------------------
    # Re-run the FULL idle conjunction. If anything changed since the timer
    # decided (a card arrived, an agent went busy, the window reset, a
    # keepalive unit went sick), ABORT — no stops issued at all.
    cfg = load_config()
    if not _is_idle(cfg, kanban_db=kanban_db, agents_file=agents_file,
                    now=now, keepalive_ok=keepalive_ok):
        msg = "Autodown teardown ABORTED: idle predicate no longer holds " \
              "(work/activity arrived)".strip()
        log(msg, "ERROR")
        _notify(msg, "HSCC Autodown Aborted", priority="high")
        return {"result": "aborted", "issued": [], "plan": []}

    # Build the teardown set (non-keepalive units only; keepalive excluded).
    plan = _build_teardown_plan(serving)

    # -- 2. Write the watchdog block BEFORE stopping anything (§3.2, C2) ----
    # Snapshot the current block so a failure/cancel can roll it back and hand
    # supervision back to the watchdog untouched (§3/§8). Block first, THEN
    # stop — stopping first would let the watchdog resurrect units mid-teardown.
    block = lifecycle.load_watchdog_block()
    original_block = dict(block)
    block["blocked"] = True
    block["reason"] = WATCHDOG_TEARDOWN_REASON
    block["blocked_at"] = now_iso()
    block["intentional"] = "autodown"    # NEW field (§5)
    lifecycle.save_watchdog_block(block)
    log("Autodown: watchdog block written (intentional teardown)")

    # -- 3. Stop non-keepalive units — workers first, orchestrator LAST -----
    issued = []
    for entry in plan:
        # Re-check cancel_requested BEFORE each stop (§6 manual abort).
        if load_config().get("cancel_requested"):
            _rollback_block(original_block)
            _record_failure("teardown cancelled by operator")
            log("Autodown teardown CANCELLED mid-way; block rolled back")
            _notify("HSCC autodown: teardown CANCELLED mid-way — block rolled "
                    "back so the watchdog resumes supervision",
                    "HSCC Autodown Cancelled", priority="normal")
            return {"result": "cancelled", "issued": issued, "plan": plan}
        res = run_cmd(entry["cmd"], timeout=30)
        issued.append({"kind": entry["kind"], "nodes": entry["nodes"],
                       "port": entry["port"], "cmd": entry["cmd"],
                       "ok": bool(res.get("ok"))})
        if not res.get("ok"):
            return _handle_stop_failure(entry, res, original_block, issued,
                                        plan)
        log(f"Autodown: stopped {entry['kind']} unit {entry['unit_id']}")

    # -- 4. Verify down (§3.4) — confirm stopped ports no longer respond. ---
    # Best-effort confirmation: the issued ``sparkrun stop`` is authoritative;
    # a still-responding probe is a warning, not a reason to refuse to record
    # the intentional down (the block gates the watchdog either way).
    all_down = True
    for entry in plan:
        if not _probe_down(entry["nodes"][0], entry["port"], http_check):
            all_down = False
            log(f"Autodown verify-down: WARN {entry['nodes'][0]}:"
                f"{entry['port']} still responding after stop", "WARN")
    if all_down:
        log("Autodown: all non-keepalive units verified down")
    else:
        log("Autodown: verify-down found some ports still responding (warned)",
            "WARN")

    # -- 5. Record state: down (§3.5) ---------------------------------------
    cfg = load_config()
    cfg["state"] = "down"
    cfg["down_since"] = now_iso()
    cfg["reason"] = "autodown: intentional idle teardown"
    cfg["cancel_requested"] = False
    # NOTE: no ``intentional`` field here — that marker belongs to the
    # watchdog-block file only (one source of truth per fact, §3.5).
    save_config(cfg)
    log("Autodown: recorded state=down")

    # -- 6. Notify the operator (desktop + ops Telegram) --------------------
    _notify("HSCC serving layer brought DOWN by idle autodown "
            "(non-keepalive units stopped; keepalive units left up)",
            "HSCC Autodown — Serving Down", priority="high")
    return {"result": "down", "issued": issued, "plan": plan}


def _handle_stop_failure(entry, res, original_block, issued, plan):
    """Roll back + record + notify on a failed stop (§8 teardown-fails).

    A stop failure must NOT leave a half-torn cluster with the block latched:
    roll the block back (clear intentional) so the watchdog resumes and can
    heal the remaining units, record the failure in autodown.json (state is
    reality — not fully down), and notify.
    """
    _rollback_block(original_block)
    out = (res.get("output") or "")[:200]
    _record_failure(
        f"teardown failed stopping {entry['kind']} unit {entry['unit_id']}: {out}")
    log(f"Autodown teardown FAILED at {entry['kind']} unit "
        f"{entry['unit_id']}; block rolled back — watchdog resuming", "ERROR")
    _notify(f"HSCC autodown: teardown FAILED stopping {entry['kind']} unit "
            f"{entry['unit_id']} — block rolled back so the watchdog can heal "
            f"the remaining units",
            "HSCC Autodown Teardown Failed", priority="high")
    return {"result": "failed", "failed_at": entry["unit_id"],
            "issued": issued, "plan": plan}
