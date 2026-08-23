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

    Hardening: the dict on disk is ALWAYS the complete §7 schema. ``cfg`` is
    merged over ``DEFAULT_CONFIG`` and only the merged result is persisted, so
    a partial dict (from a patched loader, a hand-edit, or a future caller)
    can never write a config file missing keys. ``DEFAULT_CONFIG`` is copied
    fresh each call so the caller's dict is not mutated.
    """
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    os.makedirs(os.path.dirname(AUTODOWN_FILE) or ".", exist_ok=True)
    tmp = AUTODOWN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(merged, f, indent=2, default=str)
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


def cycle(kanban_db=None, agents_file=None, now=None, keepalive_ok=None,
          probes=None):
    """Idle autodown decision function (Phase 3, §1/§6; Phase 6 probes §1d).

    Called each daemon tick by ``daemon_ops.run_autodown_loop``
    (daemon_ops.py:307-317). Pure-ish and testable: every external input
    (kanban DB, agents.json path, clock) is injectable, and with no args it
    reads the real config from disk and uses the real clock — no running daemon
    required.

    Decision order:
    1. Disabled ⇒ do nothing (§7 C5, fail-closed).
    2. Phase 6 activity probes ⇒ run FIRST (after the disabled guard) so fresh
       inbound activity resets ``last_activity_iso`` before the window is
       evaluated. Each probe stamps via ``record_activity`` — the single choke
       point (§1d). A raising/broken probe is caught + logged and never breaks
       the cycle.
       ``probes`` (an injectable sequence of zero-arg callables returning True
       if they stamped) defaults to the three real sources: HTTP API request,
       new kanban card / task activity, inbound Telegram. Pass ``probes=[]`` to
       run the pure idle evaluation without any source polling.
    3. ``state`` in (``down``, ``waking``) ⇒ wake is Phase 5. Return without
       touching the serving layer.
    4. ``state == down`` ⇒ wake seam (§4): if an activity event arrived since
       we went down, bring the serving layer back up via autoup() (Phase 5,
       called lazily).
    5. ``state == up`` ⇒ evaluate the full idle conjunction (§1/§6). Any single
       false, or any unverifiable signal, ⇒ NOT idle ⇒ return without teardown
       (fail-safe direction is mandatory).
    6. All clear ⇒ teardown the serving layer (Phase 4, called lazily).
    """
    cfg = load_config()
    if not cfg.get("enabled"):
        # §7 C5: OFF by default. Do nothing, never touch the serving layer.
        return

    # Phase 6 activity probes — run after the disabled guard so fresh inbound
    # activity resets last_activity_iso before the window is evaluated. A
    # broken probe is caught + logged, never breaks the cycle.
    if probes is None:
        probes = _default_probes(kanban_db)
    for probe in probes:
        try:
            probe()
        except Exception as e:
            # A broken probe must never break the cycle — log + continue.
            log(f"Autodown activity probe error: {e}", "ERROR")

    # Reload AFTER probes: record_activity (called by a probe) advances
    # last_activity_iso on disk, which the window math and wake seam below must
    # see.
    cfg = load_config()
    state = cfg.get("state")
    if state == "waking":
        # A wake is already in flight (autoup set state=waking). Do NOT start
        # a second parallel wake — return and let the in-flight one finish.
        return
    if state == "down":
        # Wake seam (§4): if an activity event arrived since we went down,
        # bring the serving layer back up via autoup(). autoup() is Phase 5;
        # call it lazily (missing ⇒ no-op, raising ⇒ caught + logged), mirroring
        # how _invoke_teardown handles the Phase 4 seam.
        if _fresh_activity_since_down(cfg):
            _invoke_autoup()
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


# ---------------------------------------------------------------------------
# Phase 5 — autoup() wake sequence + cycle wake seam (§4, §5/§4.5, §8)
# ---------------------------------------------------------------------------

# Default interval between readiness polls during a wake (§4.4). Injectable in
# tests so they never sleep.
READY_POLL_INTERVAL = 5


def _fresh_activity_since_down(cfg):
    """True iff an activity event arrived AFTER the cluster went down (§4).

    The wake seam's trigger: ``last_activity_iso`` vs ``down_since`` —
    ``record_activity()`` (Phase 1) is the single choke point every activity
    source stamps, and ``down_since`` is when teardown confirmed the layer down
    (§3.5). Any activity stamped after that point means a fresh inbound event
    wants serving back up.

    Fail-safe (never auto-wake on an unverifiable signal): if either timestamp
    is absent/unparseable we return False — we only auto-wake on a positively
    verifiable fresh-activity signal.
    """
    down = _parse_iso(cfg.get("down_since"))
    last = _parse_iso(cfg.get("last_activity_iso"))
    if down is None or last is None:
        return False  # can't verify fresh activity ⇒ don't auto-wake
    return last > down


def _invoke_autoup():
    """Drive ``autoup()`` lazily — the Phase 5 seam.

    Mirrors ``_invoke_teardown`` (autodown.py:353): if autoup is not yet
    defined (Phase 5 missing) it is a no-op; a raising autoup is caught + logged
    so the cycle keeps running.
    """
    autoup = globals().get("autoup")
    if autoup is None:
        return  # Phase 5 not implemented yet → no wake to run
    try:
        autoup()
    except Exception as e:
        log(f"Autodown autoup error: {e}", "ERROR")

# The readiness deadline for a wake reuses the watchdog's model-load grace
# window (lifecycle.py:125, VLLM_LOAD_GRACE_MINUTES default 20 — model load is
# genuinely slow). Read at CALL time via a function so monkeypatching the env /
# lifecycle attribute is respected, matching how lifecycle reads it.
def _wake_ready_grace_minutes():
    from . import lifecycle
    return getattr(lifecycle, "VLLM_LOAD_GRACE_MINUTES", 20)


def _unit_start_cmd(u, serving):
    """`sparkrun run <recipe> --cluster hscc --hosts <nodes> --port <port>
    --no-follow --ensure` for one serving unit (§4.3).

    Mirrors the real orchestrator start form ``serving.VLLM_START_CMD``
    (serving.py:151-153). The recipe is the unit's OWN recipe
    (``serving.orchestrator_recipe`` for the orchestrator, ``u.recipe`` for a
    worker), falling back to the orchestrator recipe global so a unit missing a
    recipe can still be started. ``--hosts`` carries the EXACT per-unit node
    list from serving.json so the set that comes UP matches what went DOWN.
    """
    from .serving import orchestrator_recipe, serving_port
    from .serving import VLLM_RECIPE, HSCC_CLUSTER
    if u.get("role") == "orchestrator":
        recipe = orchestrator_recipe(serving) or VLLM_RECIPE
    else:
        recipe = u.get("recipe") or VLLM_RECIPE
    nodes = [n for n in (u.get("nodes") or []) if n]
    port = u.get("port") or serving_port(serving)
    return ["sparkrun", "run", recipe, "--cluster", HSCC_CLUSTER,
            "--hosts", ",".join(nodes), "--port", str(port),
            "--no-follow", "--ensure"]


def _build_wake_plan(serving):
    """Ordered start commands for the NON-keepalive serving units (§4).

    The EXACT mirror of ``_build_teardown_plan`` (autodown.py:427) but in WAKE
    order: orchestrator unit FIRST, then non-keepalive workers (§4.3 — reverse
    of teardown; there is no in-flight request when waking from zero). It picks
    out the SAME unit set teardown stopped, so what comes UP equals exactly what
    went DOWN. Keepalive units (C4) are NEVER in the set.

    Each entry: ``{"kind", "nodes", "port", "unit_id", "cmd"}`` (same shape as
    the teardown plan so the round-trip teardown→autoup is symmetric).
    """
    if not isinstance(serving, dict):
        return []
    from .serving import serving_port
    orch = []
    workers = []
    for u in (serving.get("units", []) or []):
        if not isinstance(u, dict):
            continue
        nodes = [n for n in (u.get("nodes") or []) if n]
        if not nodes:
            continue  # no nodes ⇒ nothing to start
        role = u.get("role")
        unit_id = u.get("id") or ",".join(nodes)
        port = u.get("port") or serving_port(serving)
        if role == "orchestrator":
            orch.append({"kind": "orchestrator", "nodes": nodes,
                         "port": port, "unit_id": unit_id,
                         "cmd": _unit_start_cmd(u, serving)})
        elif role == "worker" and not u.get("keepalive"):
            workers.append({"kind": "worker", "nodes": nodes,
                            "port": port, "unit_id": unit_id,
                            "cmd": _unit_start_cmd(u, serving)})
        # keepalive worker / unknown role ⇒ never in the wake set.
    workers.sort(key=lambda e: e["unit_id"])
    return orch + workers


def _all_units_ready(plan, http_check_fn):
    """True iff EVERY unit in ``plan`` answers healthy on its port (§4.4).

    A unit is ready when ``GET http://<nodes[0]>:<port>/health`` returns ok.
    Keepalive units are not in ``plan`` and are not probed here (they were never
    stopped, so they are not being woken). An unreachable/probe-error unit is
    NOT ready.
    """
    for entry in plan:
        try:
            res = http_check_fn(
                f"http://{entry['nodes'][0]}:{entry['port']}/health", timeout=5)
            if not res.get("ok"):
                return False
        except Exception:
            return False
    return True


def _wait_ready(plan, http_check_fn=None, clock=None, timeout_seconds=None,
                sleep_fn=None):
    """Poll each unit's port until healthy or the readiness deadline (§4.4).

    Returns ``(ready_unit_ids, ok)``: ``ready_unit_ids`` is the ordered list of
    units confirmed healthy; ``ok`` is False when the deadline passed with some
    unit still not ready (readiness timeout, §8 wake-fails).

    Injectable so tests never sleep: ``clock()`` returns the current time
    (default time.monotonic), ``sleep_fn(seconds)`` is the poll wait (default
    ``time.sleep``; tests pass a no-op / advancing fake), ``http_check_fn`` is
    the health probe (default util.http_check), and ``timeout_seconds`` caps
    the window (default ``VLLM_LOAD_GRACE_MINUTES * 60``).
    """
    import time
    http_check = http_check_fn or _util_http_check_probe()
    clock = clock or time.monotonic
    sleep_fn = sleep_fn or time.sleep
    if timeout_seconds is None:
        timeout_seconds = _wake_ready_grace_minutes() * 60
    deadline = clock() + timeout_seconds

    ready = []
    # Units whose readiness probe raised — logged ONCE each so a broken probe
    # is visible instead of silently busy-spinning the full grace window. A
    # set (not a bool) gives per-unit diagnostics while staying bounded: every
    # probe that raises every round logs exactly one line, never a flood.
    logged_raises = set()
    while True:
        for entry in plan:
            if entry["unit_id"] in ready:
                continue
            try:
                res = http_check(
                    f"http://{entry['nodes'][0]}:{entry['port']}/health",
                    timeout=5)
                if res.get("ok"):
                    ready.append(entry["unit_id"])
            except Exception as e:
                # Not ready this round — keep polling. But surface the first
                # failure per unit so a probe that raises EVERY round does not
                # look like "the model is just slow" for the whole 20-minute
                # grace window with zero diagnostics (real defect fixed here).
                if entry["unit_id"] not in logged_raises:
                    log(
                        "Autodown _wait_ready: readiness probe for "
                        f"{entry['unit_id']} raised: {e}",
                        "WARN",
                    )
                    logged_raises.add(entry["unit_id"])
        if all(e["unit_id"] in ready for e in plan):
            return ready, True
        if clock() >= deadline:
            return ready, False
        sleep_fn(READY_POLL_INTERVAL)


def _util_http_check_probe():
    """Deferred import of util.http_check (avoids import churn at module top)."""
    from .util import http_check
    return http_check


def _clear_intentional_block(reason=None):
    """Clear the watchdog's intentional-autodown block so it resumes supervision.

    Sets ``blocked: false``, removes ``intentional``, and clears ``failures``,
    restoring the watchdog to ordinary supervision (§4.5 / §5 transition
    "Serving verified up → clear block"). Used on BOTH a successful wake (after
    readiness confirmed) and a failed wake (§8: clear ``intentional`` so the
    watchdog resumes and can heal).
    """
    from . import lifecycle
    block = lifecycle.load_watchdog_block()
    block["blocked"] = False
    block.pop("intentional", None)
    block.setdefault("failures", []).clear()
    if reason:
        block["reason"] = reason
    lifecycle.save_watchdog_block(block)
    return block


def _record_wake_failure(msg):
    """Persist a wake failure into autodown.json (§8 wake-fails).

    Sets ``state`` to reflect reality — the layer is NOT confirmed up — and
    records ``reason`` so ``hscc autodown status`` is an honest report an
    operator can act on. ``wake_source``/``wake_at`` are KEPT so the operator
    can still see what triggered the wake.
    """
    cfg = load_config()
    cfg["state"] = "up"
    cfg["reason"] = msg
    save_config(cfg)


def _record_wake_success():
    """Persist the resumed-up state into autodown.json (§4.6).

    Sets ``state: \"up\"`` and clears the ``wake`` bookkeeping (``wake_source``,
    ``wake_at``) — the wake is done, the trigger no longer needs to be surfaced.
    """
    cfg = load_config()
    cfg["state"] = "up"
    cfg["wake_source"] = None
    cfg["wake_at"] = None
    cfg["reason"] = ""
    save_config(cfg)


def autoup(serving_path=None, run_cmd_fn=None, http_check_fn=None,
           clock=None, sleep_fn=None, wake_grace_minutes=None, now=None,
           notify=True):
    """Bring the serving layer back up (§4/§4.5/§5) + handle failure (§8).

    Idempotent: if ``autodown.json.state`` is already ``waking`` we return a
    ``already-waking`` result WITHOUT starting anything, so two wake triggers
    can never start two parallel wakes.

    Sequence (§4):
      1. Mark ``state: \"waking\"`` (idempotent guard on entry).
      2. Record ``wake_source`` + ``wake_at``.
      3. Start the serving layer: the units teardown() stopped, orchestrator
         FIRST (§4.3) — the exact reverse of teardown order. The wake plan is
         built from the same unit table ``_build_teardown_plan`` derives from,
         so the set that comes UP equals exactly the set that went DOWN.
         Keepalive units were never stopped, so they are never started here.
      4. Wait for readiness — poll each unit's port until healthy or timeout
         (§4.4, ``VLLM_LOAD_GRACE_MINUTES`` window; injectable probe + clock).
      5. Clear the watchdog block ONLY after serving is confirmed up (§4.5):
         ``blocked: false``, remove ``intentional``, clear ``failures``. Order
         is critical — clearing before units are ready would let the first
         watchdog tick see a not-yet-ready cluster and latch the breaker.
      6. Set ``state: \"up\"``, clear ``wake`` bookkeeping.
      7. Notify operator (desktop + ops Telegram; both CPU-side).

    Failure handling (§8 wake-fails) — if a start fails OR readiness times out,
    do NOT silently leave ``state:\"waking\"`` forever with the block latched
    (an invisible wedge). We: record the failure, clear ``intentional`` so the
    watchdog resumes and can heal, notify loudly, and leave ``state: \"up\"``
    (reality-ish: the layer is not confirmed down) with a clear ``reason`` an
    operator can act on.

    Returns a result dict with ``result`` in
    (``up`` | ``already-waking`` | ``start-failed`` | ``not-ready``) plus
    ``started`` (the units issued start) and ``ready`` (the units confirmed
    healthy).

    All external side-effects are injectable, so running this NEVER touches the
    live cluster.
    """
    from . import serving as serving_mod

    run_cmd = run_cmd_fn or _util_run_cmd()
    http_check = http_check_fn or _util_http_check_probe()
    serving = serving_mod.load_serving(serving_path)
    ts = now or now_iso()

    # -- 1. Mark waking — idempotent guard --------------------------------
    cfg = load_config()
    if cfg.get("state") == "waking":
        # Another wake is already in flight. No-op: never start two parallel
        # wakes from one trigger set.
        log("Autodown autoup: already waking — no-op (wake in flight)")
        return {"result": "already-waking", "started": [], "ready": []}
    cfg["state"] = "waking"
    save_config(cfg)

    # -- 2. Record the wake trigger (§4.2) ---------------------------------
    cfg["wake_source"] = cfg.get("wake_source") or "cycle"
    cfg["wake_at"] = ts
    save_config(cfg)
    log("Autodown autoup: state=waking, wake recorded")

    # -- 3. Start the serving layer back up (orchestrator FIRST, §4.3) -----
    plan = _build_wake_plan(serving)
    started = []
    for entry in plan:
        res = run_cmd(entry["cmd"], timeout=30)
        started.append({"kind": entry["kind"], "nodes": entry["nodes"],
                        "port": entry["port"], "cmd": entry["cmd"],
                        "ok": bool(res.get("ok"))})
        if not res.get("ok"):
            return _handle_start_failure(entry, res, notify=notify)
        log(f"Autodown autoup: started {entry['kind']} unit {entry['unit_id']}")

    # -- 4. Wait for readiness (§4.4) ---------------------------------------
    ready, ok = _wait_ready(plan, http_check_fn=http_check, clock=clock,
                            timeout_seconds=(
                                wake_grace_minutes * 60
                                if wake_grace_minutes is not None else None),
                            sleep_fn=sleep_fn)
    if not ok:
        msg = ("autodown: wake READINESS TIMEOUT after "
               f"{_wake_ready_grace_minutes()}m — units not all healthy")
        return _handle_wake_timeout(plan, ready, msg, notify=notify)

    # -- 5. Clear the watchdog block ONLY after serving confirmed up (§4.5) -
    # Order is critical: clearing before units are ready would let the very
    # first watchdog tick see a not-yet-ready cluster and latch the breaker.
    # By construction we are here only after _all units_ answered healthy.
    _clear_intentional_block(reason="serving layer up (autodown wake complete)")
    log("Autodown autoup: watchdog block cleared after readiness confirmed")

    # -- 6. Set state up + clear wake bookkeeping (§4.6) --------------------
    _record_wake_success()

    # -- 7. Notify operator (§4.7) ------------------------------------------
    if notify:
        _notify("HSCC serving layer is back UP (idle autodown wake complete)",
                "HSCC Autodown — Serving Up", priority="normal")
    return {"result": "up", "started": started,
            "ready": [e["unit_id"] for e in plan]}


def _util_run_cmd():
    """Deferred import of util.run_cmd (avoids import churn at module top)."""
    from .util import run_cmd
    return run_cmd


def _handle_start_failure(entry, res, notify=True):
    """Wake-fails path when a ``sparkrun run`` returns non-ok (§8).

    Do NOT leave ``state:\"waking\"`` forever with the block latched. Clear the
    ``intentional`` marker so the watchdog resumes and can heal, record the
    failure, notify loudly, and leave ``state: \"up\"`` (not confirmed down) with
    a reason an operator can act on. ``wake_source``/``wake_at`` are kept so the
    operator can see the trigger.
    """
    out = (res.get("output") or "")[:200]
    msg = (f"autodown: wake FAILED starting {entry['kind']} unit "
           f"{entry['unit_id']}: {out}")
    # Clear intentional so the watchdog resumes supervision + can heal.
    _clear_intentional_block(reason="autodown: wake failed — watchdog resuming")
    _record_wake_failure(msg)
    log("Autodown autoup FAILED at " + f"{entry['kind']} unit "
        f"{entry['unit_id']}; state=up, intentional cleared", "ERROR")
    if notify:
        _notify(f"HSCC autodown: wake FAILED starting {entry['kind']} unit "
                f"{entry['unit_id']} — serving layer NOT up, watchdog resuming",
                "HSCC Autodown Wake Failed", priority="critical")
    return {"result": "start-failed", "failed_at": entry["unit_id"],
            "started": [], "ready": []}


def _handle_wake_timeout(plan, ready, msg, notify=True):
    """Wake-fails path when readiness times out (§8).

    Same principle: do NOT leave ``state:\"waking\"`` forever with the block
    latched (invisible wedge). Clear ``intentional`` so the watchdog resumes and
    can heal whatever failed to come up, record the failure, notify loudly, and
    leave ``state: \"up\"`` with a reason. ``wake_source``/``wake_at`` kept so the
    operator can see the trigger.
    """
    _clear_intentional_block(reason="autodown: wake readiness timeout — "
                                    "watchdog resuming")
    _record_wake_failure(msg)
    log(f"Autodown autoup READINESS TIMEOUT; state=up, intentional cleared "
        f"(ready={ready})", "ERROR")
    if notify:
        _notify("HSCC autodown: wake READINESS TIMEOUT — serving NOT confirmed "
                "up; watchdog resuming to heal",
                "HSCC Autodown Wake Timeout", priority="critical")
    return {"result": "not-ready", "ready": ready, "plan": plan}


# ---------------------------------------------------------------------------
# Phase 6 — activity-source probes into record_activity (§1d)
# ---------------------------------------------------------------------------
#
# Each probe detects one of the §1d activity sources and, when it fires, calls
# ``record_activity(source)`` — the single choke point that advances
# ``last_activity_iso`` on disk (resetting the idle window). cycle() runs all
# three every tick (autodown.py:399-421), each wrapped in try/except so a
# broken probe can never break the cycle. All probes are fail-safe: missing /
# unreadable signals never fabricate activity.

# §1d.1 — the HSCC API server writes an authenticated-request timestamp here
# (via state.write_state('activity', ...) in hscc-api/api_server.py). Override
# in tests.
HTTP_ACTIVITY_STATE = os.path.expanduser("~/.hscc/state/activity.json")

# §1d.2 — the Hermes gateway log. The design correction: we do NOT edit
# ~/.hermes-tg/mcp_server.py (external, untracked by git). Instead we observe
# its effect indirectly through Hermes' OWN gateway log, which writes an
# ``inbound message: platform=telegram`` line for every inbound Telegram
# message. Read-only, cheap to poll. Overridable in tests.
GATEWAY_LOG = os.path.expanduser("~/.hermes/logs/gateway.log")
TELEGRAM_MARKER = "inbound message: platform=telegram"
# Byte offset up to which the gateway log has been scanned for the marker,
# persisted so a restarted daemon does not re-stamp old mail as fresh.
TELEGRAM_OFFSET_FILE = os.path.expanduser(
    "~/.hscc/state/telegram_probe.offset")


def _read_activity_ts(activity_file=None):
    """Best-effort parse of the API activity file's ``timestamp`` (§1d.1).

    Returns an aware datetime, or None if the file is missing/unreadable/not a
    dict/no timestamp. Never raises.
    """
    path = activity_file or HTTP_ACTIVITY_STATE
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    ts = data.get("timestamp")
    return _parse_iso(ts) if ts else None


def probe_http_activity(activity_file=None):
    """Stamp ``record_activity(\"http\")`` when the API server logged a request
    newer than our last activity (§1d.1).

    Called each cycle. Compares the API state file's ``timestamp`` (written on
    every AUTHENTICATED request) against the config's ``last_activity_iso``;
    stamps only when the API activity is NEWER than what we last recorded, so a
    steady stream of requests keeps the window rolling without redundant writes.

    Returns True if it stamped. Fail-safe: no file / unparseable timestamp ⇒
    do NOT stamp (never fabricate API activity from an unreadable signal).
    """
    ts = _read_activity_ts(activity_file)
    if ts is None:
        return False
    cfg = load_config()
    last = _parse_iso(cfg.get("last_activity_iso"))
    if last is not None and ts <= last:
        return False   # no NEW API activity since we last recorded
    record_activity("http")
    return True


def probe_kanban_activity(kanban_db=None):
    """Stamp ``record_activity(\"kanban\")`` when the board has live/imminent
    work (§1d.3: \"new kanban card / task is activity\").

    The fleet drives work through the kanban DB; a board with any
    running/ready/review/qa/... card is the operator working. We stamp whenever
    the board has live/imminent work, which resets the idle timer while a
    pipeline is in flight — an active board keeps the cluster awake, and a card
    that arrives while DOWN is detected by the wake seam (a fresh last_activity
    beats down_since) and triggers autoup.

    Counting activity only when there IS work (vs. a transition detector)
    is intentional and safe: the idle interlock §6.1 already blocks teardown
    while work is active, so the probe's only real effect is keeping the
    window rolled while work runs and firing the wake seam on a new card.

    NOTE the failure semantics differ from ``_has_active_work`` (the teardown
    predicate, which returns True on an unreadable DB — conservative for
    teardown). For an ACTIVITY signal that polarity is backwards: stamping on an
    unreadable DB would fabricate perpetual activity from a dead board. So we
    query the DB directly and only stamp when the board is POSITIVELY readable
    AND has active work; an unreadable board returns False (no stamp).
    """
    if kanban_db is None:
        kb = _load_kanban_db_or_default()
        if kb is None:
            return False   # can't read the board ⇒ can't verify activity
        kanban_db = kb
    try:
        with kanban_db.connect_closing() as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks "
                "WHERE status IS NULL "
                "   OR status NOT IN ('done', 'archived', 'blocked') "
                "LIMIT 1"
            ).fetchone()
    except Exception:
        # Unreadable board ⇒ cannot positively confirm activity ⇒ no stamp.
        return False
    if row is not None:
        record_activity("kanban")
        return True
    return False


def _load_telegram_offset(offset_file=None):
    """Read the last-scanned byte offset of the gateway log, or None."""
    path = offset_file or TELEGRAM_OFFSET_FILE
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _save_telegram_offset(offset, offset_file=None):
    """Atomically persist the gateway-log scan offset (best-effort)."""
    path = offset_file or TELEGRAM_OFFSET_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(offset))
        os.replace(tmp, path)
    except OSError:
        pass   # best-effort; a missing offset just re-baselines next poll


def probe_telegram_activity(gateway_log=None, offset_file=None):
    """Stamp ``record_activity(\"telegram\")`` on NEW inbound Telegram messages
    (§1d.2 — design correction).

    We do NOT edit ~/.hermes-tg/mcp_server.py (external, untracked by git —
    an edit there is silently lost on rebuild and is a trap, not a design). We
    observe Telegram inbound traffic indirectly: the Hermes gateway writes an
    ``inbound message: platform=telegram`` line to ~/.hermes/logs/gateway.log
    for every inbound Telegram message. This probe scans that log from the last
    scanned byte offset; any NEW marker line = fresh inbound Telegram ⇒ stamp.

    The offset is persisted in ~/.hscc/state/telegram_probe.offset so a
    restarted daemon does not re-stamp old mail. Log rotation (size shrinking /
    truncation) is handled by re-baselining from offset 0.

    Returns True if it stamped. Fail-safe: missing log or unreadable offset ⇒
    baseline-reset, no stamp (never fabricate Telegram activity).
    """
    log_path = gateway_log or GATEWAY_LOG
    offset_path = offset_file or TELEGRAM_OFFSET_FILE
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return False   # no log ⇒ no telegram signal
    offset = _load_telegram_offset(offset_path)
    if offset is None:
        # First poll (or lost offset): baseline at current EOF — do NOT treat
        # the log's existing content as fresh inbound activity.
        _save_telegram_offset(size, offset_path)
        return False
    if size < offset:
        offset = 0    # log rotated/truncated — re-baseline from the start
    if size == offset:
        return False  # nothing new since last scan
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
    except OSError:
        return False
    count = chunk.count(TELEGRAM_MARKER.encode())
    _save_telegram_offset(size, offset_path)
    if count > 0:
        record_activity("telegram")
        return True
    return False


def _default_probes(kanban_db=None):
    """Build the default cycle() probe closures (§1d).

    probe_kanban_activity needs the injectable kanban_db (tests pass a fake);
    the others read module-level paths. Each closure takes NO args and returns
    True if it stamped, so cycle() can run them uniformly.
    """
    return [
        lambda: probe_http_activity(),
        lambda: probe_kanban_activity(kanban_db),
        lambda: probe_telegram_activity(),
    ]

