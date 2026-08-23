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
