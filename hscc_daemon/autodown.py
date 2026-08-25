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
import sys

from .daemon_ops import log
from .state import now_iso
from .desktop import send_macos_notification
from .telegram import notify_operations

# Path to the autodown state+config file. Overridden in tests via
# monkeypatch, mirroring lifecycle.WATCHDOG_BLOCK_FILE
# (hscc_daemon/lifecycle.py:124).
AUTODOWN_FILE = os.path.expanduser("~/.hscc/autodown.json")

# Atomic O_EXCL lockfile guarding the teardown/autoup critical sections (§8
# double-teardown row). Only ONE teardown OR autoup may run at a time — they
# are mutually exclusive, not just teardown-vs-teardown. A second caller
# returns a ``busy`` result instead of proceeding concurrently. Overridden in
# tests via monkeypatch, mirroring AUTODOWN_FILE.
AUTODOWN_LOCK = os.path.expanduser("~/.hscc/autodown.lock")

# Default configuration (docs/design/idle-autodown.md §7). C5: OFF by default.
# A new file starts disabled; the file is created when autodown is first
# enabled.
DEFAULT_CONFIG = {
    "enabled": False,            # C5: OFF by default
    "idle_minutes": 10,          # default 10; 0 = only via explicit wake
    "state": "up",               # one of: up | waking | down | error
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


# Nominal location of the Hermes agent source tree, which holds
# ``hermes_cli/kanban_db.py``. Mirrors flightdeck/core/kanban.py:77
# (``_HERMES_AGENT_PATH``) and lifecycle.py:62 / install.py:88,102 which build
# the same ``~/.hermes/hermes-agent`` tree. Overridable via env so unusual
# installs and tests can point at it (same override flightdeck respects).
_HERMES_AGENT_PATH = "~/.hermes/hermes-agent"

# Last outcome of resolving the Hermes kanban library. Surfaced by
# ``hscc autodown status`` so an operator is never left wondering why the
# kanban interlock never clears. ``ok`` is None until first resolution, then
# True/False; ``reason`` describes a failure; ``warned`` guards the
# once-per-process log (not one line per daemon tick).
_KANBAN_LOAD = {"ok": None, "reason": "", "warned": False}


def _note_kanban_unavailable(reason):
    """Record that the kanban interlock is unevaluable and log it ONCE.

    Updates the module-level ``_KANBAN_LOAD`` so ``hscc autodown status`` can
    show the truth, and emits a single WARN log line (not one per daemon tick)
    via ``daemon_ops.log``. Does not raise — the caller still fails safe (the
    interlock stays active).
    """
    _KANBAN_LOAD["ok"] = False
    _KANBAN_LOAD["reason"] = reason
    if not _KANBAN_LOAD["warned"]:
        _KANBAN_LOAD["warned"] = True
        try:
            log(f"Autodown kanban interlock unevaluable: {reason}", "WARN")
        except Exception:
            pass  # logging must never break the fail-safe


def _load_kanban_db_or_default():
    """Import Hermes' ``hermes_cli.kanban_db`` lazily, or None on failure.

    Kept in its own function so ``_has_active_work`` (and this module) never
    depend on Hermes being installed at import time, and so tests can inject.
    Resolves the Hermes runtime dir and puts it on ``sys.path`` only for the
    duration of this one import — mirroring flightdeck/core/kanban.py:86-111
    — because ``hermes_cli`` lives under ~/.hermes/hermes-agent and is never
    on the daemon's ``sys.path``. Honours ``HOME`` (via expanduser) and the
    ``HERMES_AGENT_PATH`` override the way flightdeck does.

    On any failure it returns None WITHOUT raising; the caller
    (``_has_active_work``) then fails safe to True (never treat an
    unevaluable interlock as idle). The failure is recorded + logged once via
    ``_note_kanban_unavailable`` so ``status`` surfaces it.
    """
    hermes_path = os.path.expanduser(
        os.environ.get("HERMES_AGENT_PATH", _HERMES_AGENT_PATH))
    if not os.path.isdir(hermes_path):
        _note_kanban_unavailable(
            f"Hermes agent source not found at {hermes_path!r} — cannot import "
            f"hermes_cli.kanban_db. Set HERMES_AGENT_PATH if it lives elsewhere.")
        return None
    if hermes_path not in sys.path:
        sys.path.insert(0, hermes_path)
    try:
        from hermes_cli import kanban_db  # noqa: PLC0415
    except Exception as e:
        _note_kanban_unavailable(
            f"could not import hermes_cli.kanban_db from {hermes_path!r}: {e}")
        return None
    _KANBAN_LOAD["ok"] = True
    _KANBAN_LOAD["reason"] = ""
    return kanban_db


def kanban_check_state():
    """Report the kanban interlock resolution for ``hscc autodown status``.

    Returns None when the daemon has not yet evaluated it, otherwise a dict
    ``{ok: bool, reason: str}`` describing the last resolution. This lets an
    operator see — from ``status`` — that the kanban interlock is unevaluable
    and why, instead of guessing why autodown never fires.
    """
    if _KANBAN_LOAD.get("ok") is None:
        return None
    return {"ok": _KANBAN_LOAD["ok"], "reason": _KANBAN_LOAD["reason"]}



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
        is not confirmed down (state is ``waking``/``up``/``error``/missing) — an
        in-progress or failed transition. The layer should be up: finish the
        wake (or resume supervision). Never leave it parked.
    ``healthy``
        No intentional autodown block — the watchdog supervises normally. A
        wake-failure ``state: \"error\"`` lands here once its block is cleared:
        the watchdog supervises and can heal whatever is actually there.

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
    """True iff every present keepalive unit's HEAD node answers healthy (§1f/§6.4).

    A keepalive unit may span multiple nodes (tensor-parallel across two GPUs);
    only the HEAD node (the span primary) serves an HTTP API — TP peers expose
    no endpoint of their own and must NOT be probed individually. So we probe
    the head (first node) of each unit, not every node, reusing how
    ``health.check_workers`` (health.py:1002) already judges the same units:
    it computes ``_tp_peer_nodes`` (health.py:407, via cmdlib's
    ``serving_unit_scoreboard`` — the single source of truth) and treats any
    NON-primary span member as healthy without a standalone probe. A node that
    is a TP peer this run is skipped; every remaining (head) node must answer
    ``/health``.

    Keepalive units are NO LONGER exempt from teardown (C4 reversed) — fleet
    down takes the whole serving layer incl. keepalive units. The ``keepalive_ok``
    INTERLOCK is still a separate "don't shut down mid-problem" gate: if a
    keepalive unit is itself unhealthy we abort, so we do not tear the whole
    fleet down while a worker is mid-flight relying on it (§6.4). Fail-safe:
    any inability to load serving, resolve the unit set, resolve tp-peers, or
    probe a unit's head ⇒ False (abort teardown) AND log why — we never tear
    down on an unverifiable keepalive signal. With no keepalive units present,
    there is nothing to protect ⇒ True.
    """
    try:
        from . import serving
        from .util import http_check
        from .health import _tp_peer_nodes
    except Exception as e:
        log(f"Autodown keepalive check unavailable: {e}", "WARN")
        return False
    try:
        units = serving.keepalive_units(serving.load_serving())
    except Exception as e:
        log(f"Autodown keepalive: cannot resolve unit set: {e} — aborting "
            f"teardown", "WARN")
        return False
    if not units:
        return True  # no keepalive units to be unhealthy
    try:
        # Non-primary members of multi-node/tp spans serve through their head
        # and expose no endpoint of their own — reusing check_workers' exact
        # health judgment for the same units.
        tp_peers = _tp_peer_nodes()
    except Exception as e:
        log(f"Autodown keepalive: tp-peer resolution failed: {e} — aborting "
            f"teardown", "WARN")
        return False
    for u in units:
        node = u["node"]
        if node in tp_peers:
            # TP peer serves through the unit's head — no standalone endpoint,
            # so it cannot independently be down. Do not probe it.
            continue
        try:
            url = f"http://{node}:{u['port']}/health"
            if not http_check(url, timeout=5).get("ok"):
                log(f"Autodown keepalive: unit {u['id']} head "
                    f"{node}:{u['port']} unhealthy — aborting teardown", "WARN")
                return False
        except Exception as e:
            log(f"Autodown keepalive: probe of {u['id']} "
                f"({node}:{u['port']}) failed: {e} — aborting teardown", "WARN")
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
    # 6.4 Keepalive health INTERLOCK (§1f) — keepalive units are teardown
    #     targets now (C4 reversed) but a sick one aborts: do not tear the whole
    #     fleet down mid-problem while a worker relies on it.
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
    4a. ``state == error`` ⇒ a wake failed to determine any units; autoup has
       cleared the intentional block and released the layer to the watchdog.
       Do nothing (no re-latch, no teardown, no wake seam) — the watchdog
       owns supervision until serving.json is repaired.
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
        # §8 self-heal: while down, re-assert the intentional watchdog block
        # EVERY cycle so a deleted/corrupt/reset block file can never let the
        # watchdog resurrect a deliberately-down layer (C2).
        try:
            _self_heal_intentional_block()
        except Exception as e:
            # Defensive — a broken block read must not break the cycle.
            log(f"Autodown self-heal block error: {e}", "ERROR")
        # Wake seam (§4): if an activity event arrived since we went down,
        # bring the serving layer back up via autoup(). autoup() is Phase 5;
        # call it lazily (missing ⇒ no-op, raising ⇒ caught + logged), mirroring
        # how _invoke_teardown handles the Phase 4 seam.
        if _fresh_activity_since_down(cfg):
            _invoke_autoup()
        return
    if state == "error":
        # §8 residual: a wake failed to determine ANY units to start and
        # autoup has cleared the intentional block, releasing the layer to
        # ordinary watchdog supervision. cycle() must NOT re-latch the block
        # (that would re-suppress the watchdog with nothing running), must NOT
        # tear down (there is nothing to tear down and the layer may be down),
        # and must NOT run the wake seam (there are no units to start until
        # serving.json is fixed). The watchdog owns supervision; do nothing.
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


# ---------------------------------------------------------------------------
# Autodown lockfile — atomic O_EXCL mutex between teardown() and autoup()
# ---------------------------------------------------------------------------

def _lock_stale_seconds():
    """How old a lock may be before it is presumed abandoned (§8).

    A teardown or autoup may legitimately hold the lock for up to the wake
    readiness grace window (a model load, ``VLLM_LOAD_GRACE_MINUTES``, default
    20 min) — the longest bounded critical section. Any lock OLDER than that
    window plus a fixed margin is presumed abandoned by a dead/blocked holder
    and is broken, so a crashed process can NEVER deadlock the daemon forever.
    Read at call time so monkeypatching lifecycle is respected.
    """
    from . import lifecycle
    grace = getattr(lifecycle, "VLLM_LOAD_GRACE_MINUTES", 20)
    return int(grace) * 60 + 300   # 20m model-load window + 5m margin


def _acquire_lock(now=None):
    """Atomically acquire the autodown O_EXCL lockfile (§8).

    Returns True on success (the lock is now held by this process), or False
    if another teardown/autoup holds it (busy). A stale lock — older than
    ``_lock_stale_seconds()``, i.e. abandoned by a dead/blocked holder — is
    broken (unlinked) and acquire is retried once before giving up, so the
    daemon can never deadlock forever.
    """
    import time
    now = now if now is not None else time.time()
    # The lock's parent dir (~/.hscc) may not exist on a fresh machine; create
    # it lazily like save_config does, so os.open(O_CREAT) never raises.
    try:
        os.makedirs(os.path.dirname(AUTODOWN_LOCK) or ".", exist_ok=True)
    except OSError:
        return False
    try:
        fd = os.open(AUTODOWN_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        # Already held — is it stale (abandoned)?
        try:
            age = now - os.path.getmtime(AUTODOWN_LOCK)
        except OSError:
            age = None
        if age is not None and age > _lock_stale_seconds():
            # Presumed abandoned by a dead/blocked holder — break it and retry
            # once. If the retry still races another acquirer, report busy.
            try:
                os.unlink(AUTODOWN_LOCK)
            except OSError:
                return False
            try:
                fd = os.open(AUTODOWN_LOCK,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                return False
        else:
            return False  # live lock held by a concurrent teardown/autoup
    # Record the holder's pid + acquire time for diagnostics / staleness.
    try:
        os.write(fd, f"pid={os.getpid()} acquired={now}".encode())
    except OSError:
        pass
    os.close(fd)
    return True


def _release_lock():
    """Release the autodown lockfile if it exists. Never raises.

    Called on EVERY exit path (success and failure/abort alike) via the
    callers' ``finally``, so a lock is never leaked — a leaked lock would
    otherwise wedge the daemon (the exact failure §8 guards against).
    """
    try:
        if os.path.exists(AUTODOWN_LOCK):
            os.unlink(AUTODOWN_LOCK)
    except OSError:
        pass


def _build_teardown_plan(serving):
    """Full-fleet teardown plan — the ENTIRE serving layer goes down (§3).

    **Fleet down powers EVERYTHING down** — orchestrator AND keepalive AND
    non-keepalive workers. This REVERSES the earlier C4 keepalive exemption
    (operator decision): powering the whole serving layer down is the point of
    the feature. The raw stop string is built by the shared
    ``serving.fleet_down_cmd()`` (``sparkrun stop --all``) — the single source
    of truth autodown shares with the ``hscc cluster down`` CLI wrapper — so
    autodown no longer constructs its own per-unit sparkrun stop strings.

    Returns a single-entry list (the one fleet-down stop), whose entry carries
    the full verify set so teardown can confirm EVERY unit's head node went
    down::

        [{"kind": "fleet", "cmd": ["sparkrun","stop","--all"],
          "verify": [{"nodes": [...], "port": 8000, "unit_id": "orch"}, ...]}]

    ``serving`` is the parsed serving.json dict (None/absent ⇒ [], fail-safe:
    stop nothing).
    """
    if not isinstance(serving, dict):
        return []
    from .serving import fleet_up_plan, fleet_down_cmd
    verify = fleet_up_plan(serving)   # every unit, in fleet-up order
    if not verify:
        return []
    return [{
        "kind": "fleet",
        "nodes": sorted({n for e in verify for n in e["nodes"]}),
        "port": None,
        "unit_id": "fleet",
        "cmd": fleet_down_cmd(),
        "verify": [
            {"nodes": e["nodes"], "port": e["port"], "unit_id": e["unit_id"]}
            for e in verify
        ],
    }]


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
             http_check_fn=None, lock_now=None):
    """Execute the idle teardown sequence (§3/§5), under the autodown lock.

    Entry point wraps the real sequence in the autodown O_EXCL lockfile so
    teardown is mutually exclusive with autoup (a ``hscc autodown wake`` must
    never race an in-flight teardown) and with a second teardown (§8
    double-teardown). The lock is released on EVERY exit path (success,
    abort, busy, failure) so it can never leak and wedge the daemon.

    Two gates run before any stop is issued:
      * ``state == "down"`` ⇒ the layer is already down — re-issuing stops
        would be pointless and would race a concurrent wake. Return ``busy``.
      * another teardown/autoup holds the lock ⇒ ``busy``.

    The sequence itself (inside the lock) is ordered per §3/§5 (C2), and each
    step is logged via daemon_ops.log:

      1. Re-verify idle (§6 last-line guard): re-run the full ``_is_idle``
         conjunction. If anything changed since the timer decided ⇒ ABORT,
         NO stops issued.
      1a. Build the teardown plan. An EMPTY plan means we cannot determine
          what to tear down (serving.json missing/corrupt ⇒ ``load_serving``
          returned None) ⇒ ABORT before writing the block. Never record
          ``down`` having stopped nothing (§8 — no silent half-state).
      1b. Assert the C4 keepalive invariant: no keepalive node may appear in
          the teardown node set. If a future co-located config would make us
          stop a keepalive unit, ABORT loudly rather than trusting topology.
      2. Write the watchdog block BEFORE stopping anything (§3.2, C2):
         ``blocked:true``, reason §3.2, ``blocked_at: now``, and the NEW field
         ``intentional: "autodown"``. Block first, THEN stop — stopping first
         lets the watchdog resurrect units mid-teardown (has actually happened).
      3. Stop the ENTIRE fleet — ONE `sparkrun stop --all` (built by the shared
         serving.fleet_down_cmd(), the wrapper's source of truth). Powers ALL
         serving units down — orchestrator AND keepalive AND non-keepalive
         workers (reverses the earlier C4 keepalive exemption).
      4. Verify down: confirm every unit's head port no longer responds (§3.4).
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
    (``down`` | ``aborted`` | ``busy`` | ``cancelled`` | ``failed`` |
    ``no-targets``) plus ``issued`` (the list of stop commands actually
    issued) and the original ``plan``.
    """
    if not _acquire_lock(now=lock_now):
        log("Autodown teardown: autodown.lock held by another teardown/autoup "
            "— returning busy (no stops issued)")
        return {"result": "busy", "issued": [], "plan": []}
    try:
        return _teardown_locked(serving_path=serving_path,
                                run_cmd_fn=run_cmd_fn, kanban_db=kanban_db,
                                agents_file=agents_file, now=now,
                                keepalive_ok=keepalive_ok,
                                http_check_fn=http_check_fn)
    finally:
        _release_lock()


def _teardown_locked(serving_path=None, run_cmd_fn=None, kanban_db=None,
                     agents_file=None, now=None, keepalive_ok=None,
                     http_check_fn=None):
    """The teardown sequence itself, run while holding the autodown lock.

    Split out of ``teardown()`` so the O_EXCL lock acquisition + release live
    in exactly two places (the wrapper's begin / ``finally``) and every gate
    and early-return here runs under the lock. All existing behavior
    (ordering, block-before-stop, cancel, rollback) is unchanged; only the
    state gate, empty-plan abort, and keepalive-invariant abort (§8) are new.
    """
    from . import lifecycle   # noqa: F401  (used via module)
    from . import serving as serving_mod
    from .util import http_check as _util_http_check, run_cmd as _util_run_cmd

    run_cmd = run_cmd_fn or _util_run_cmd
    http_check = http_check_fn or _util_http_check
    serving = serving_mod.load_serving(serving_path)

    # -- 0. State gate (§8 double-teardown) -------------------------------
    # Re-check state UNDER the lock so the check is atomic with a concurrent
    # autoup (which also holds the lock to write its result). If the layer is
    # already recorded down, there is nothing to tear down — re-issuing stops
    # would be pointless and would race a wake already in progress.
    cfg = load_config()
    if cfg.get("state") == "down":
        log("Autodown teardown: state is already \"down\" — returning busy "
            "(no stops issued)")
        _notify("Autodown teardown skipped: serving layer is already down",
                "HSCC Autodown", priority="normal")
        return {"result": "busy", "issued": [], "plan": []}

    # -- 1. Re-verify idle (§6 last-line guard) ---------------------------
    # Re-run the FULL idle conjunction. If anything changed since the timer
    # decided (a card arrived, an agent went busy, the window reset, a
    # keepalive unit went sick), ABORT — no stops issued at all.
    if not _is_idle(cfg, kanban_db=kanban_db, agents_file=agents_file,
                    now=now, keepalive_ok=keepalive_ok):
        msg = "Autodown teardown ABORTED: idle predicate no longer holds " \
              "(work/activity arrived)".strip()
        log(msg, "ERROR")
        _notify(msg, "HSCC Autodown Aborted", priority="high")
        return {"result": "aborted", "issued": [], "plan": []}

    # Build the teardown plan (the ENTIRE fleet — all units go down).
    plan = _build_teardown_plan(serving)

    # -- 1a. EMPTY plan ⇒ abort before writing the block (§8) -------------
    # An empty plan means we could not determine what to tear down
    # (serving.json missing/corrupt ⇒ load_serving returned None ⇒
    # _build_teardown_plan returned []). Issuing ZERO stops yet recording
    # state:"down" would mark the still-running orchestrator down with the
    # block latched — a silent half-state (audit F4). ABORT instead, before
    # touching the block, so the watchdog keeps supervising reality.
    if not plan:
        msg = ("Autodown teardown ABORTED: empty teardown plan — cannot "
               "determine what to tear down (serving.json missing/corrupt?)")
        log(msg, "ERROR")
        _notify(msg, "HSCC Autodown Aborted", priority="high")
        return {"result": "no-targets", "issued": [], "plan": []}

    # -- 1b. (removed) C4 keepalive-overlap abort guard ---------------------
    # Previously this aborted when a keepalive node appeared in the teardown
    # set. With the fleet-down decision (PART 2) keepalive units are NO LONGER
    # exempt — ``sparkrun stop --all`` takes the ENTIRE serving layer down,
    # including keepalive units. The old guard would abort on EVERY teardown
    # (the fleet stop always touches keepalive nodes), so it is removed. The
    # ``keepalive_ok`` INTERLOCK (a sick fleet aborts teardown) is kept — see
    # _default_keepalive_ok — that is a "don't shut down mid-problem" signal,
    # not an exemption.

    # -- 2. Write the watchdog block BEFORE stopping anything (§3.2, C2) ----
    # Snapshot the current block so a failure/cancel can roll it back and hand
    # supervision back to the watchdog untouched (§3/§8). Block first, THEN
    # stop — stopping first would let the watchdog resurrect units mid-teardown.
    block = lifecycle.load_watchdog_block()
    original_block = dict(block)
    block["blocked"] = True
    block["reason"] = WATCHDOG_TEARDOWN_REASON
    block["blocked_at"] = now_iso()
    block["intentional"] = "autodown"    # new field (§5)
    lifecycle.save_watchdog_block(block)
    log("Autodown: watchdog block written (intentional teardown)")

    # -- 3. Stop the ENTIRE fleet — ONE `sparkrun stop --all` ---------------
    # The plan is a single fleet-down stop covering ALL units (orchestrator +
    # keepalive + non-keepalive workers). The command was built by the shared
    # ``serving.fleet_down_cmd()`` — the wrapper's source of truth — so
    # autodown no longer builds raw per-unit sparkrun stop strings. Cancel is
    # still honored: re-check ``cancel_requested`` before issuing the stop, and
    # if set, roll back + record + return cancelled.
    issued = []
    for entry in plan:   # exactly one entry (the fleet stop)
        # Re-check cancel_requested BEFORE the stop (§6 manual abort).
        if load_config().get("cancel_requested"):
            _rollback_block(original_block)
            _record_failure("teardown cancelled by operator")
            log("Autodown teardown CANCELLED mid-way; block rolled back")
            _notify("HSCC autodown: teardown CANCELLED mid-way — block rolled "
                    "back so the watchdog resumes supervision",
                    "HSCC Autodown Cancelled", priority="normal")
            return {"result": "cancelled", "issued": issued, "plan": plan}
        res = run_cmd(entry["cmd"], timeout=180)
        issued.append({"kind": entry["kind"], "nodes": entry["nodes"],
                       "port": entry["port"], "cmd": entry["cmd"],
                       "ok": bool(res.get("ok"))})
        if not res.get("ok"):
            return _handle_stop_failure(entry, res, original_block, issued,
                                        plan)
        log(f"Autodown: stopped {entry['kind']} unit {entry['unit_id']}")

    # -- 4. Verify down (§3.4) — confirm every unit's head no longer responds.
    # Best-effort confirmation: the issued ``sparkrun stop --all`` is
    # authoritative; a still-responding probe is a warning, not a reason to
    # refuse to record the intentional down (the block gates the watchdog
    # either way). Probes each unit's HEAD node (nodes[0]) — multi-node/TP
    # spans serve through their head only, reusing the fleet-up plan's shape.
    all_down = True
    verify = plan[0]["verify"] if plan else []
    for v in verify:
        if not _probe_down(v["nodes"][0], v["port"], http_check):
            all_down = False
            log(f"Autodown verify-down: WARN {v['nodes'][0]}:"
                f"{v['port']} still responding after stop", "WARN")
    if all_down:
        log("Autodown: all serving units verified down")
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
            "(entire fleet — orchestrator AND keepalive workers stopped)",
            "HSCC Autodown — Serving Down", priority="high")
    return {"result": "down", "issued": issued, "plan": plan}


def _cmd_error_text(res, cap=500):
    """Combine a command's stdout + stderr for the reason/log (PART 3).

    ``res`` is a run_cmd result dict. Stdout and stderr are both captured so a
    failure is diagnosable without a manual probe — the recorded reason was
    previously empty because only ``output`` (stdout) was read and the failing
    ``sparkrun`` command emitted its diagnostic on stderr. Returns a single
    trimmed string (capped at ``cap`` chars) with the non-empty parts joined by
    a newline; empty if both are missing.
    """
    parts = []
    for k in ("output", "stderr"):
        v = res.get(k)
        if v:
            parts.append(str(v))
    return "\n".join(parts)[:cap]


def _handle_stop_failure(entry, res, original_block, issued, plan):
    """Roll back + record + notify on a failed stop (§8 teardown-fails).

    A stop failure must NOT leave a half-torn cluster with the block latched:
    roll the block back (clear intentional) so the watchdog resumes and can
    heal the remaining units, record the failure in autodown.json (state is
    reality — not fully down), and notify.
    """
    _rollback_block(original_block)
    out = _cmd_error_text(res)
    _record_failure(
        f"teardown failed stopping {entry['kind']} unit {entry['unit_id']}: {out}")
    log(f"Autodown teardown FAILED at {entry['kind']} unit "
        f"{entry['unit_id']} ({out}); block rolled back — watchdog resuming",
        "ERROR")
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


def _build_wake_plan(serving):
    """Ordered start commands for EVERY serving.json unit (orchestrator FIRST,
    then workers sorted by id) — the full fleet-up set (§4).

    **Fleet-up restores the WHOLE serving layer**, including keepalive units.
    It delegates to the shared ``serving.fleet_up_plan()`` builder — the same
    single source of truth the ``hscc cluster up`` CLI wrapper uses — so
    autodown no longer constructs its own raw per-unit sparkrun start strings
    and does not invent a new start mechanism. Orchestrator FIRST (there is no
    in-flight request when waking from zero), then workers. This is the exact
    set ``_build_teardown_plan``'s verify set derives from, so what comes UP
    equals what went DOWN.

    Each entry: ``{"kind", "nodes", "port", "unit_id", "cmd"}`` (similar shape
    to the old wake plan, with ``keepalive`` retained on the entry).
    """
    if not isinstance(serving, dict):
        return []
    from .serving import fleet_up_plan
    return fleet_up_plan(serving)


def _all_units_ready(plan, http_check_fn):
    """True iff EVERY unit in ``plan`` answers healthy on its port (§4.4).

    A unit is ready when ``GET http://<nodes[0]>:<port>/health`` returns ok.
    ``plan`` is the FULL fleet-up set (orchestrator + keepalive + non-keepalive
    workers); each unit's HEAD node (nodes[0]) is probed — multi-node/TP spans
    serve through their head only, so a TP peer is never probed standalone.
    An unreachable/probe-error unit is NOT ready.
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


def _record_wake_failure(msg, state="up"):
    """Persist a wake failure into autodown.json (§8 wake-fails).

    Sets ``state`` to reflect reality — the layer is NOT confirmed up — and
    records ``reason`` so ``hscc autodown status`` is an honest report an
    operator can act on. ``wake_source``/``wake_at`` are KEPT so the operator
    can still see what triggered the wake.

    The ``state`` written is a judgment call per failure path:
      * "up" (default) — the start-failed / readiness-timeout paths DID
        start (or partially start) units, so the layer is best described as
        "not confirmed down; the block is cleared and the watchdog owns
        supervision" (the §8 ``up-or-error`` semantic).
      * "error" — the empty-wake-plan path started NOTHING, so claiming
        ``up`` would be an aspiration, not reality. ``"error"`` is the honest
        label (layer not up; autodown has released it to the watchdog).
    """
    cfg = load_config()
    cfg["state"] = state
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
           notify=True, lock_now=None):
    """Bring the serving layer back up (§4/§4.5/§5), under the autodown lock.

    Entry point wraps the real sequence in the autodown O_EXCL lockfile so
    autoup is mutually exclusive with teardown (a wake must never race an
    in-flight teardown) and with a second autoup (§8 double-teardown). The
    lock is released on EVERY exit path so it can never leak and wedge the
    daemon.

    Two gates run before any start is issued:
      * ``state == "up"`` ⇒ the layer is already up — re-issuing starts would
        be pointless. Return ``busy``.
      * another teardown/autoup holds the lock ⇒ ``busy``.

    Idempotent: if ``autodown.json.state`` is already ``waking`` we return an
    ``already-waking`` result WITHOUT starting anything, so two wake triggers
    can never start two parallel wakes.

    The sequence itself (inside the lock) is per §4/§4.5/§5, and each step is
    logged via daemon_ops.log:

      1. Mark ``state: "waking"`` (idempotent guard on entry).
      1a. Build the wake plan. An EMPTY plan means we cannot determine what
          to start (serving.json missing/corrupt ⇒ ``load_serving`` returned
          None). This is a FAILURE, not a success: do NOT clear the block and
          do NOT claim ``up`` when zero units were started/confirmed (§8,
          audit F7). Record the failure loudly and leave the block latched.
      2. Record ``wake_source`` + ``wake_at``.
      3. Start the serving layer back up — EVERY unit in serving.json:
         orchestrator FIRST (§4.3), then workers. The wake plan comes from the
         shared ``serving.fleet_up_plan()`` — the same source of truth
         ``hscc cluster up`` uses — so the full set that went DOWN (orchestrator
         AND keepalive AND non-keepalive workers) comes back UP. Keepalive
         units are NO LONGER exempt on the way up: fleet-up restores the whole
         layer.
      4. Wait for readiness — poll each unit's port until healthy or timeout
         (§4.4, ``VLLM_LOAD_GRACE_MINUTES`` window; injectable probe + clock).
      5. Clear the watchdog block ONLY after serving is confirmed up (§4.5):
         ``blocked: false``, remove ``intentional``, clear ``failures``. Order
         is critical — clearing before units are ready would let the first
         watchdog tick see a not-yet-ready cluster and latch the breaker.
      6. Set ``state: "up"``, clear ``wake`` bookkeeping.
      7. Notify operator (desktop + ops Telegram; both CPU-side).

    Failure handling (§8 wake-fails) — if a start fails OR readiness times out,
    do NOT silently leave ``state:"waking"`` forever with the block latched
    (an invisible wedge). We: record the failure, clear ``intentional`` so the
    watchdog resumes and can heal, notify loudly, and leave ``state: "up"``
    (the §8 ``up-or-error`` semantic: the layer is not confirmed down) with a
    clear ``reason`` an operator can act on.

    The empty-plan failure (§1a) follows the SAME principle: with NOTHING
    started, the layer is down and nothing supervises it if the block stays
    latched — the worst half-state (per §8, "favor UP and supervised over
    down and unsupervised"). It clears ``intentional`` too, letting the
    watchdog resume and heal whatever is actually there, and records an
    honest ``state: "error"`` (NOT ``"up"`` — nothing was started) with the
    reason.

    Returns a result dict with ``result`` in
    (``up`` | ``busy`` | ``already-waking`` | ``start-failed`` | ``not-ready`` |
    ``no-units``) plus ``started`` (the units issued start) and ``ready`` (the
    units confirmed healthy).

    All external side-effects are injectable, so running this NEVER touches the
    live cluster.
    """
    if not _acquire_lock(now=lock_now):
        log("Autodown autoup: autodown.lock held by another teardown/autoup "
            "— returning busy (no starts issued)")
        return {"result": "busy", "started": [], "ready": []}
    try:
        return _autoup_locked(serving_path=serving_path, run_cmd_fn=run_cmd_fn,
                              http_check_fn=http_check_fn, clock=clock,
                              sleep_fn=sleep_fn,
                              wake_grace_minutes=wake_grace_minutes, now=now,
                              notify=notify)
    finally:
        _release_lock()


def _autoup_locked(serving_path=None, run_cmd_fn=None, http_check_fn=None,
                   clock=None, sleep_fn=None, wake_grace_minutes=None, now=None,
                   notify=True):
    """The wake sequence itself, run while holding the autodown lock.

    Split out of ``autoup()`` so the O_EXCL lock acquisition + release live in
    exactly two places (the wrapper's begin / ``finally``) and every gate and
    early-return here runs under the lock. All existing behavior (idempotent
    waking guard, start order, block-clear-after-ready, failure recovery) is
    unchanged; only the ``state=="up"`` gate and the empty-plan failure (§8)
    are new.
    """
    from . import serving as serving_mod

    run_cmd = run_cmd_fn or _util_run_cmd()
    http_check = http_check_fn or _util_http_check_probe()
    serving = serving_mod.load_serving(serving_path)
    ts = now or now_iso()

    # -- 0. State gate (§8 double-teardown) -------------------------------
    # If the layer is already recorded up, there is nothing to wake — re-issue
    # starts would be pointless. Check UNDER the lock so it is atomic with a
    # concurrent teardown (which also holds the lock to write its "down").
    cfg = load_config()
    if cfg.get("state") == "up":
        log("Autodown autoup: state is already \"up\" — returning busy "
            "(no starts issued)")
        return {"result": "busy", "started": [], "ready": []}

    # -- 1. Mark waking — idempotent guard --------------------------------
    if cfg.get("state") == "waking":
        # Another wake is already in flight. No-op: never start two parallel
        # wakes from one trigger set.
        log("Autodown autoup: already waking — no-op (wake in flight)")
        return {"result": "already-waking", "started": [], "ready": []}
    cfg["state"] = "waking"
    save_config(cfg)

    # Build the wake plan — the FULL fleet (all units incl. keepalive).
    plan = _build_wake_plan(serving)

    # -- 1a. EMPTY plan ⇒ failure, not success (§8 / audit F7 / residual) --
    # An empty wake plan means we could not determine what to start
    # (serving.json missing/corrupt ⇒ load_serving returned None). Reporting
    # state:"up" while starting nothing would be a vacuous success (audit F7).
    # This is a FAILURE, and it must NOT leave the watchdog suppressed with
    # nothing running (the v1.9.0 residual): clearing ``intentional`` so
    # ordinary supervision resumes and the watchdog can heal whatever is
    # actually there, recording the failure loudly, and setting an HONEST
    # ``state: "error"`` — NOT ``"up"``, since zero units were started, and
    # NOT ``"down"``, which cycle()'s self-heal would immediately re-latch
    # and re-suppress the watchdog (undoing this fix). "error" means "the
    # layer is not up; autodown has released it to the watchdog".
    if not plan:
        # The block was latched intentional (as teardown left it); clear it so
        # the watchdog resumes supervision. State → "error" (honest: nothing
        # started), reason records the failure for status + operator action.
        _clear_intentional_block(
            reason="autodown: wake failed — empty wake plan; watchdog resuming")
        msg = ("autodown: wake FAILED — empty wake plan; cannot determine "
               "what to start (serving.json missing/corrupt?)")
        _record_wake_failure(msg, state="error")
        _notify(msg, "HSCC Autodown Wake Failed", priority="critical")
        log(msg, "ERROR")
        return {"result": "no-units", "started": [], "ready": []}

    # -- 2. Record the wake trigger (§4.2) ---------------------------------
    cfg["wake_source"] = cfg.get("wake_source") or "cycle"
    cfg["wake_at"] = ts
    save_config(cfg)
    log("Autodown autoup: state=waking, wake recorded")

    # -- 3. Start the serving layer back up (orchestrator FIRST, §4.3) -----
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
    out = _cmd_error_text(res)
    msg = (f"autodown: wake FAILED starting {entry['kind']} unit "
           f"{entry['unit_id']}: {out}")
    # Clear intentional so the watchdog resumes supervision + can heal.
    _clear_intentional_block(reason="autodown: wake failed — watchdog resuming")
    _record_wake_failure(msg)
    log("Autodown autoup FAILED at " + f"{entry['kind']} unit "
        f"{entry['unit_id']} ({out}); state=up, intentional cleared", "ERROR")
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
# (hscc-api/api_server.py::_do_stamp_http_activity). This lives at
# ~/.hscc/activity.json — OUTSIDE ~/.hscc/state/ — deliberately: activity is
# event-driven (updated only on a request), not a periodic stream, so it must
# not sit in the daemon-streams dir that verify.py::check_daemon_streams
# treats as requiring fresh ok:true entries. state/ means exactly "periodic
# streams the daemon refreshes". Override in tests.
HTTP_ACTIVITY_STATE = os.path.expanduser("~/.hscc/activity.json")

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


# ---------------------------------------------------------------------------
# Phase 8 — daemon-start recovery + self-healing intentional block
#           (§8 "daemon dies while down" / "while waking" /
#            "watchdog-block file corrupt/missing")
# ---------------------------------------------------------------------------


def _assert_intentional_block():
    """(Re)write the watchdog block as an intentional autodown (§8/§3.2).

    Loads the current block, sets ``blocked: true`` + ``intentional:
    \"autodown\"`` with the teardown reason and a fresh ``blocked_at``, and
    persists it — so the (new) daemon's watchdog backs off instead of
    resurrecting an intentionally-down serving layer (C2). Idempotent:
    re-asserting an already-correct block is a harmless rewrite. Never raises
    (load/save are self-contained).
    """
    from . import lifecycle
    block = lifecycle.load_watchdog_block()
    block["blocked"] = True
    block["intentional"] = "autodown"
    block["reason"] = WATCHDOG_TEARDOWN_REASON
    block["blocked_at"] = now_iso()
    lifecycle.save_watchdog_block(block)
    return block


def _self_heal_intentional_block():
    """Re-assert the intentional block only when it is missing/corrupt (§8).

    Called EVERY cycle while ``state == \"down\"``. Returns True when it rewrote
    the block (it was deleted, corrupt, or reset — ``intentional != \"autodown\"``
    OR ``blocked`` is False), False when the block was already correctly asserted
    (``blocked: true`` AND ``intentional: \"autodown\"``). This self-healing means
    a lost OR half-cleared block file can never let the watchdog resurrect a
    deliberately-down layer.

    The ``blocked`` clause is the defense-in-depth for FINDING 2 (§8 forbids the
    silent half-state): the watchdog's backoff-elapsed path historically popped
    ``blocked_at``/``reason`` but NOT ``intentional``, leaving a ``blocked: False
    + intentional: "autodown"`` block. A block with ``blocked`` False but
    ``intentional == "autodown"`` is NOT already-asserted — it is a split-brain
    wedge that would let the next watchdog tick resurrect the orchestrator. Treat
    it as needing re-assert.
    """
    from . import lifecycle
    block = lifecycle.load_watchdog_block()
    if block.get("intentional") == "autodown" and block.get("blocked"):
        return False  # already correctly asserted — leave it untouched
    _assert_intentional_block()
    return True


def resume_from_restart():
    """Recover autodown state ONCE on daemon startup (§8).

    Read ``~/.hscc/autodown.json`` and reconcile reality with the new
    daemon's supervision. Never touch the serving layer when disarmed.

    - ``enabled == false`` ⇒ do nothing at all (C5 — never act when disarmed).
    - ``state == \"down\"`` ⇒ the layer is intentionally down but NOTHING
      supervises it after a daemon restart. Re-assert the watchdog block
      (``blocked: true, intentional: \"autodown\"``) so the new daemon's
      watchdog doesn't resurrect it, and resume monitoring (the normal cycle
      loop takes over). The serving layer STAYS down — that was the operator's
      intent; wake still works on the next event.
    - ``state == \"waking\"`` ⇒ a wake may or may not have finished. Clear the
      stale ``\"waking\"`` (it would trip ``autoup``'s already-waking guard) and
      RE-RUN autoup — idempotent (``--ensure`` on already-running units is a
      no-op) — to finish the wake. SAFE = finish the wake.
    - ``state == \"up\"`` (or unknown) ⇒ do nothing.
    - ``state == \"error\"`` ⇒ a wake failed to determine units; autoup already
      cleared the intentional block and released the layer to the watchdog.
      Do nothing on restart either (do NOT re-latch) — the watchdog owns
      supervision until serving.json is repaired.

    Fully defensive: if anything here raises, callers that need to guarantee
    the daemon boots use ``resume_from_restart_defensive()`` — a broken
    autodown must never stop the daemon starting (§8 guiding principle).
    """
    cfg = load_config()
    if not cfg.get("enabled"):
        # Disarmed — do nothing at all. Never re-assert the block, never wake.
        return
    state = cfg.get("state")
    if state == "down":
        _assert_intentional_block()
        log("Autodown startup: state=down → watchdog block re-asserted "
            "(intentional autodown); monitoring resumed, serving stays down")
    elif state == "waking":
        # A wake may or may not have finished after the daemon died. Clear the
        # stale ``waking`` (riding on the dead daemon's in-flight wake) so
        # autoup's already-waking guard doesn't no-op it, then re-run autoup
        # to finish the wake. ``--ensure`` makes re-running idempotent.
        cfg = load_config()
        cfg["state"] = "up"
        save_config(cfg)
        autoup()
        log("Autodown startup: state=waking → autoup re-run to finish the wake")
    # state == "up" / "error" / unknown: nothing to recover. For "error",
    # the intentional block was already cleared by autoup's failure handling;
    # re-asserting it here would re-suppress the watchdog with nothing running.


def resume_from_restart_defensive():
    """Startup hook for daemon_ops.run_daemon_loop — never blocks the boot (§8).

    Wraps ``resume_from_restart`` so ANY exception is logged and swallowed: the
    daemon must start even if autodown is broken. This is the ONLY function
    ``daemon_ops.run_daemon_loop`` calls at startup.
    """
    try:
        resume_from_restart()
    except Exception as e:
        log(f"Autodown resume_from_restart error — daemon starting anyway: {e}",
            "ERROR")

