"""Detect, alert, and self-heal a silently-wedged kanban dispatcher.

The Hermes kanban dispatcher's OWN health check ("kanban dispatcher stuck")
fires whenever it sees spawnable ready work but 0 spawned — which is exactly
what it does when the fleet is legitimately AT CAPACITY (``kanban.max_in_progress``
reached). Very different situations look identical to that check (trigger 3
was added the hard way on 2026-10-17):

  * trigger 1 (healthy, at capacity): ready cards ARE being held back because
    every running worker slot is taken — correct behaviour, the dispatcher is
    fine, never page.
  * trigger 2 (genuine wedge): ready cards sit unclaimed while worker slots go
    EMPTY. The dispatcher should be spawning when slots free up but its own
    internal state is wedged, so it spawns NOTHING — silent, and `hscc status`/
    `hscc verify` stay green because nothing surfaced it.
  * trigger 3 (healthy, dispatcher-design holds — t_160d306f, 2026-10-17):
    ready cards sit unclaimed with FREE slots because the dispatcher's own
    respawn guard is refusing to re-spawn them (``active_pr`` /
    ``recent_success`` / a rate-limit cooldown / the dispatch lock / memory
    pressure). The gateway still logs "dispatcher stuck" for these ticks —
    the hold only annotates the warning, it does not influence the counter —
    and on 2026-10-17 that false positive restarted the gateway three times
    in 65 minutes. Two fail-closed layers tell trigger 2 from trigger 3:
    the probe believes the dispatcher's own ``Last tick held back:`` payload
    (fresh lookback + reconcilable counts + benign names only), and the
    recovery independently re-verifies through the sanctioned ``hermes
    kanban`` CLI that every ready card is explainably held BEFORE it will
    restart. Anything the watchdog cannot explain (``blocker_auth``, unknown
    reason names, missing/stale warnings, unreadable boards) stays
    wedge-worthy — the original purpose is never disarmed.

This module is the hscc-side watchdog that tells them apart. It is a
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

Review work is spawnable ONLY if this runtime can actually dispatch it. A card
parked in ``review`` on a runtime whose dispatcher has no review lane (or with
``kanban.review_dispatch: false``) is NOT waiting on the dispatcher — it is
waiting on a human. Counting it as spawnable made the detector declare a stall
that nothing could ever clear, and the recovery restarted the gateway every
cooldown window (operator incident 2026-10-15: four gateway SIGTERMs in two
hours on ``t_25aed701``, a merged PR sitting in ``review``). So:

  * ``spawnable`` counts review rows only when the lib exposes
    ``has_spawnable_review`` AND ``review_dispatch_enabled()`` is true — the
    exact gate hermes' own ``dispatch_once`` uses for the review lane. Neither
    holds → the board's review rows are reported as ``stale_review`` (a warning
    line + stream fields), the stream stays GREEN, nothing is restarted, no
    wedge state is written. The same capability also decides the per-profile
    ROOM question (``_any_assignee_under_cap``): an assignee that is only
    waiting on undispatchable review work must not make a capped board look
    roomy, or the phantom stall returns through the other door.
  * a declared stall the runtime cannot dispatch (``stall_dispatchable: False``)
    restarts AT MOST ONCE per incident, and an incident is identified by its
    spawnable/roomy signature — see ``recover_dispatcher_wedge``.

Everything that touches the outside world is injectable (kanban read,
``max_in_progress`` config read, restart runner, clock, state writer, review
capability), so the suite runs against fakes with ZERO external calls and never
touches live operator state.
"""

import json
import os
import re
import time

from .daemon_ops import log

DISPATCHER_STREAM = "dispatcher"

# ── Benign dispatcher holds (t_160d306f) ───────────────────────────────────
# The 2026-10-17 incident: card t_4e0f0509 sat READY but held by the
# dispatcher's OWN respawn guard (an open PR on the card — ``active_pr=1``),
# while the gateway logged the "kanban dispatcher stuck" warning every tick
# (6→11→16→21→26 consecutive). This watchdog read that red warning plus the
# board's spawnable-ready predicate and restarted the gateway 3× in 65 min.
# A held-back card is NOT a wedge: the dispatcher is working correctly by
# refusing to respawn.
#
# The hold names below mirror, AS THEY EXIST, Hermes' own suppression
# vocabulary (hermes_cli/kanban_db_dispatch.py::describe_suppression, which
# feeds the "Last tick held back: <payload>" clause, and gateway/
# kanban_watchers.py which writes it):
#   * ``active_pr``       — respawn guard: recent comment carries a PR URL
#   * ``recent_success``  — respawn guard: completed run inside the guard window
#   * ``rate_limited``    — cooldown: workers hit a provider quota wall and
#     were deliberately released to ready WITHOUT counting a failure
#   * ``skipped_locked``  — another process holds the board dispatch lock
#     (the lock holder is making progress on the same board)
#   * ``memory_pressure`` — tick-level restriction (critical/elevated); a
#     restart only ADDS pressure
# ``blocker_auth`` is deliberately NOT benign: it signals a credential/quota
# failure the operator must fix (exactly the "check credentials" case this
# watchdog exists to surface) — fail CLOSED. Any unknown/new reason string is
# wedge-worthy for the same reason: a Hermes upgrade that renames or adds a
# hold must not silently silence the watchdog.
BENIGN_HOLD_REASONS = frozenset({
    "active_pr", "recent_success", "rate_limited", "skipped_locked",
    "memory_pressure",
})

# The dispatcher logs the stuck warning at most once per 300s while bad_ticks
# run; a warning older than this lookback no longer describes the CURRENT
# zero-spawn state, so it must not explain (or silence) anything.
DISPATCHER_HOLD_LOOKBACK_SECONDS = int(
    os.environ.get("HSCC_DISPATCHER_HOLD_LOOKBACK_SECONDS", "900"))
# Minimum seconds between operator notifications for the SAME explained stall
# (the gate itself re-evaluates every recovery tick; notify once, not hourly).
DISPATCHER_HOLD_NOTIFY_COOLDOWN_SECONDS = int(
    os.environ.get("HSCC_DISPATCHER_HOLD_NOTIFY_COOLDOWN_SECONDS", "3600"))

# Gateway log the dispatcher's stuck warning is written to. Module constant
# (computed at import) so the test conftest can redirect it per-test — same
# mechanism as every other ``~/.hscc`` module constant.
GATEWAY_LOG = os.path.expanduser("~/.hermes/logs/gateway.log")

# "kanban dispatcher stuck: ready queue non-empty for 11 consecutive ticks
# but 0 workers spawned. ..." — the gateway (gateway/kanban_watchers.py) and
# the CLI daemon (hermes_cli/kanban_ops.py: "...0 workers spawned
# successfully.") share this prefix. The tick count is a free token (\S+, so
# a quoted/synthetic line such as the incident report's "for N consecutive
# ticks" still parses) and optional so a truncated quote still matches.
_STUCK_WARNING_RE = re.compile(
    r"dispatcher stuck: ready queue non-empty for(?: (\S+) consecutive"
    r" ticks)? but 0 workers spawned")
_HOLD_BACK_RE = re.compile(r"Last tick held back:\s*([^.]+)\.")
# One ``name=value`` pair of describe_suppression's payload grammar
# ("active_pr=1, recent_success=2, memory_pressure=critical"). A pair that
# does NOT parse as name=value is an UNKNOWN hold → wedge-worthy.
_HOLD_PAIR_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(\S+)$")
# Leading logging-formatter timestamp (local time), e.g. 2026-10-17 12:49:30,405
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

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

# The stale-review warning carries this reason when the caller pinned
# ``review_capable=False`` without a specific diagnosis (tests, and any future
# caller that already knows the answer).
REVIEW_NOT_DISPATCHABLE_REASON = (
    "review feature not dispatchable by this runtime")

# In-memory rate-limit for the stale-review warning line. A parked review card
# is a steady state, not an event: without a cooldown this would write one
# WARNING to the daemon log every probe tick (60s) forever, which is log noise
# that buries the real signal. Bounded per-process on purpose — same convention
# as the detector streaks above: a daemon restart is allowed to warn once more.
_stale_review_warn = {"last_at": None}


def _reset_stale_review_warning():
    """Forget the stale-review warning cooldown (tests / daemon restart)."""
    _stale_review_warn["last_at"] = None



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


def _any_assignee_under_cap(kanban_db, board, multi, max_per_profile,
                            include_review=True):
    """True iff some assignee with unclaimed SPAWNABLE work is under its cap.

    ``include_review`` must be False when this runtime cannot dispatch the
    review lane: a review row is not work the dispatcher could spawn this tick,
    so its assignee must not answer the room question. Letting it do so
    re-created the same phantom stall on the OTHER side of the detector — with
    the ready-holding profile legitimately AT its per-profile cap, an unclaimed
    review row owned by an idle assignee made the board look roomy, the ready
    row kept it spawnable, and recovery restarted the gateway for a stall that
    was just correct throttling.

    Read-only. On any error, return True (fail toward "there is room"), which
    keeps this helper from silencing a real stall — the stall path still has
    its own cooldown and attempt cap.
    """
    lanes = "'ready', 'review'" if include_review else "'ready'"
    try:
        with kanban_db.connect_closing(board=board) if multi \
                else kanban_db.connect_closing() as conn:
            waiting = [r[0] for r in conn.execute(
                "SELECT DISTINCT assignee FROM tasks "
                f"WHERE status IN ({lanes}) "
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


# ── Can THIS runtime dispatch the review lane? ─────────────────────────────
def _review_dispatch_config_state():
    """``kanban.review_dispatch`` as hermes' dispatcher sees it.

    Returns ``True`` / ``False`` / ``None`` (None = unknown: the dispatch module
    or its accessor is not importable on this interpreter, or it raised).

    Deliberately reuses ``hermes_cli.kanban_db_dispatch.review_dispatch_enabled``
    instead of re-reading the config here: that function IS the gate hermes' own
    ``dispatch_once`` applies before it enumerates ANY review row
    (``review_rows = _lane_rows(conn, "review") if review_dispatch_enabled()
    else []``), so our answer is byte-for-byte the dispatcher's. A second
    implementation would be free to disagree with the dispatcher — which is the
    exact bug class this function exists to close. Note ``kanban.auto_review``
    (``kanban_decompose._review_policy``) is the *pairing* policy that decides
    who reviews a newly created card, not whether the review lane dispatches, so
    it is NOT the signal to consult here.
    """
    try:
        from hermes_cli import kanban_db_dispatch
    except Exception:  # noqa: BLE001 - no dispatch module: unknown, not "off"
        return None
    fn = getattr(kanban_db_dispatch, "review_dispatch_enabled", None)
    if fn is None:
        return None
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001 - a raising accessor is unknown too
        return None


def _review_dispatch_capable(kanban_db):
    """``(capable, reason)`` — can this runtime spawn a ``review`` card at all?

    ``capable`` is what gates review rows out of ``spawnable``; ``reason`` is the
    non-empty human string used in the warning when it is False.

    Two independent ways the answer is False, and BOTH were live on 2026-10-15
    in the sense that each one alone would have produced the phantom stall:
      * the kanban lib in use exposes no ``has_spawnable_review`` — no review
        lane to spawn into. That is the condition that makes ``_board_snapshot``
        fall back to raw SQL, and the raw-SQL predicate counting
        ``status='review'`` was the door the operator's diagnosis pointed at
        (measured 2026-10-16: on THIS box the bind in
        ``_bind_spawnable_helpers`` does succeed, so the reachable door here is
        the second one — but the fallback is one unimportable
        ``kanban_db_dispatch`` away, e.g. a daemon started with a
        ``HERMES_AGENT_PATH`` that has no dispatch module, and it must not be
        allowed to lie again).
      * ``kanban.review_dispatch`` is off (the dispatcher's own gate): review
        work is parked for a human by configuration, so no dispatcher state can
        ever clear it. This box currently has it ON.

    When the lib DOES advertise ``has_spawnable_review`` but the config gate is
    unreadable (``None``), we report CAPABLE — the same fail-toward-today's-
    semantics direction the lib's own ``review_dispatch_enabled`` takes (its
    ``except`` returns True). Narrowing on a read failure would silently disable
    review-stall detection on a runtime that can dispatch review.
    """
    if not hasattr(kanban_db, "has_spawnable_review"):
        return False, ("review feature not available in this runtime "
                       "(no has_spawnable_review in the kanban lib)")
    if _review_dispatch_config_state() is False:
        return False, "kanban.review_dispatch is off (review is human-only)"
    return True, ""


def _count_review_rows(conn):
    """Assigned, unclaimed ``review`` rows on one board (warning-only count).

    A plain count for the operator-facing warning. It is deliberately NOT a
    spawnable predicate — we never reimplement ``has_spawnable_review`` here.
    """
    return int(conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'review' "
        "AND assignee IS NOT NULL AND claim_lock IS NULL"
    ).fetchone()[0])


def _board_snapshot(kanban_db, board, multi, max_in_progress,
                    max_per_profile=None, review_capable=True,
                    review_reason=""):
    """Gather one board's dispatcher-relevant state.

    Returns a dict with:
      * ``spawnable`` — True iff the board has spawnable work this runtime can
        actually dispatch: ready work (an assigned, unclaimed task whose
        assignee is a real Hermes profile), PLUS review work only when
        ``review_capable``. Review rows that this runtime cannot spawn are NOT
        spawnable — see ``_review_dispatch_capable``.
      * ``in_progress`` — count of ``status='running'`` tasks.
      * ``roomy`` — True iff this board could still spawn (in_progress below
        the global cap, or no cap is configured).
      * ``stale_review`` / ``review_count`` — review work IS present but this
        runtime cannot dispatch it, and how many rows. Reported, never stalled.
      * ``spawnable_proven`` — False when the spawnable answer came from the
        raw-SQL fallback rather than the dispatcher's own predicate, i.e. we
        could not prove the assignee maps to a real profile. Used to mark the
        resulting stall as not proven dispatcher-fixable.
      * ``error`` — unreadable board (never crashes the whole scan).

    Where the real kanban lib exposes its ``has_spawnable_ready`` /
    ``has_spawnable_review`` we reuse them so our notion of "spawnable" is
    byte-for-byte the dispatcher's. Fall back to the same SQL predicate if the
    lib is a minimal injected stub without them — and count review work as
    spawnable there only if ``review_capable`` says this runtime has a review
    lane to spawn into.
    """
    try:
        with kanban_db.connect_closing(board=board) if multi \
                else kanban_db.connect_closing() as conn:
            proven = True
            if hasattr(kanban_db, "has_spawnable_ready"):
                ready = kanban_db.has_spawnable_ready(conn)
                review = (review_capable
                          and kanban_db.has_spawnable_review(conn))
                spawnable = bool(ready or review)
            else:
                # Raw-SQL fallback: we can see assigned+unclaimed rows but we
                # CANNOT check them against the profile registry, which is what
                # the dispatcher's own predicate does. So the answer is a guess
                # (``spawnable_proven`` False), and review rows are counted only
                # when this runtime actually has a review lane.
                proven = False
                rows = conn.execute(
                    "SELECT 1 FROM tasks WHERE status = 'ready' "
                    "AND assignee IS NOT NULL AND claim_lock IS NULL LIMIT 1"
                ).fetchone()
                spawnable = bool(rows)
                review_pending = False
            review_count = _count_review_rows(conn)
            in_progress = int(conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0])
            # Pending work for reconciliation of the dispatcher's hold counts
            # (t_160d306f). The lane set follows the SAME capability answer
            # the spawnable decision above used (t_dc04732e, the combined
            # case): when this runtime cannot dispatch the review lane, the
            # dispatcher's own tick never even enumerates review rows
            # (``review_rows = _lane_rows(conn, "review") if
            # review_dispatch_enabled() else []``), so its "Last tick held
            # back" clause can never be counting a parked review card —
            # letting such a row pad the reconciliation would exonerate a
            # live wedge (pending >= claimed) for up to the lookback window.
            # When the lane IS dispatchable a review card may legitimately
            # sit held by a respawn guard, so it counts there. Deliberately
            # counts EVERY assigned+unclaimed row in the dispatchable lanes
            # (control-plane lanes included): over-counting pending makes a
            # benign-hold EXEMPTION harder, never easier — fail closed.
            pending_lanes = "'ready', 'review'" if review_capable \
                else "'ready'"
            pending = int(conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ("
                f"{pending_lanes}) AND assignee IS NOT NULL "
                "AND claim_lock IS NULL"
            ).fetchone()[0])
    except Exception as e:  # noqa: BLE001
        label = "default" if not board else board
        return {"board": label, "spawnable": False, "in_progress": 0,
                "pending": 0, "roomy": False, "error": str(e),
                "stale_review": False,
                "review_count": 0, "review_reason": review_reason,
                "spawnable_proven": True}
    stale_review = bool(review_count) and not review_capable
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
        roomy = _any_assignee_under_cap(kanban_db, board, multi, max_per_profile,
                                        include_review=review_capable)
    return {"board": "default" if not board else board,
            "spawnable": spawnable, "in_progress": in_progress,
            "pending": pending, "roomy": roomy, "error": None,
            "stale_review": stale_review, "review_count": review_count,
            "review_reason": review_reason if stale_review else "",
            "spawnable_proven": proven}


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


def _bind_spawnable_helpers(kanban_db):
    """Bind the canonical spawnable predicates onto a real Hermes kanban lib.

    ``has_spawnable_ready``/``has_spawnable_review`` live in
    ``hermes_cli.kanban_db_dispatch``; ``hermes_cli.kanban_db`` only re-exports
    them through a removal-scheduled compat shim whose stated date
    (2026-09-14) has passed and which emits HermesPluginCompatWarning on every
    ``hasattr`` / attribute access. Binding them into the module's ``__dict__``
    (exactly as ``autodown._load_kanban_db_or_default`` does for ``connect`` /
    ``connect_closing``) makes ``hasattr(kanban_db, ...)`` and
    ``kanban_db.has_spawnable_*`` resolve from ``__dict__`` and never touch the
    shim — so ``_board_snapshot`` keeps reusing the dispatcher's own notion of
    spawnable, and the compat warnings stop. Idempotent and cheap: after the
    first bind the names are already in ``__dict__`` and this is a no-op.

    Three cases:
      * old Hermes (pre-split): ``has_spawnable_ready`` is a NATIVE ``__dict__``
        member (never a warning) — already present, left untouched.
      * current Hermes (v2026.9.14): only reachable via the shim — bound here
        from ``kanban_db_dispatch`` so ``__dict__`` holds the real callables.
      * hermes_cli unreachable (test-only interpreter / no Hermes): nothing to
        bind; ``_board_snapshot`` then falls back to the injected fake's own
        methods, then the SQL predicate. This is NOT a silent narrowing — on
        the same interpreter ``_load_kanban_db_or_default`` returns None and
        the whole probe goes fail-safe ``unreachable``, never the SQL fallback
        with a real board.
    """
    try:
        from hermes_cli import kanban_db_dispatch
    except Exception:  # noqa: BLE001 - no hermes_cli: nothing to bind
        return kanban_db
    for _n in ("has_spawnable_ready", "has_spawnable_review"):
        if _n not in vars(kanban_db) and hasattr(kanban_db_dispatch, _n):
            setattr(kanban_db, _n, getattr(kanban_db_dispatch, _n))
    return kanban_db


# ── In-memory detector state (lives for the daemon process) ────────────────
# Mirrors ``_engine_wedge_units`` (health.py) / ``_recover_units`` (recover.py):
# a daemon restart re-probes fresh, so in-memory streaks are correct to reset.
_detector = {
    "stall_ticks": 0,      # consecutive genuine-stall ticks (N debounce)
    "last_running_total": None,  # previous tick's fleet-wide running count
    "declared": False,     # a genuine stall has been DECLARED (stream is red)
    "declared_since": None,  # epoch clock of first declaring tick
    "peak_stall_ticks": 0,
    "benign_hold_ticks": 0,    # consecutive ticks explained by a benign hold
    "last_hold_explained": False,
}

# M-debounce for the recovery: how many consecutive RED ticks (declared
# detections) the recovery needs before it considers restarting.
_recovery_detection_streak = 0

# In-memory throttle for benign-hold operator notifications (t_160d306f): the
# recovery loop ticks every DISPATCHER_RECOVER_CHECK_INTERVAL seconds, so an
# ongoing hold must notify ONCE per DISPATCHER_HOLD_NOTIFY_COOLDOWN_SECONDS,
# not every minute. In-memory matches the streak counters' lifetime: a daemon
# restart re-notifies once, which is the correct "I'm still here" ping.
_hold_notify_at = None


def _capture_kanban(kanban_db=None, max_in_progress=None,
                    max_per_profile=None, review_capable=None):
    """Read dispatcher-relevant state across every board (read-only).

    Returns a dict that is the input to the stall decision:
      ``{boards: [...], spawnable: [...], roomy_spawnable: [...],
      at_capacity: [...], total_running: int, max_in_progress: int|None,
      errors: [...], stale_review: bool, review_count: int,
      review_reason: str, stall_dispatchable: bool}``.
    ``max_in_progress`` is read from config when not passed (injectable).
    ``review_capable`` overrides the runtime-capability probe (injectable, for
    tests); when None it is resolved ONCE per pass via
    ``_review_dispatch_capable`` so every board sees the same answer.
    If the kanban lib cannot be reached at all, returns ``{"unreachable":...}``
    so the caller can decide (fail safe: write ok True, do NOT false-alarm).
    """
    if kanban_db is None:
        kanban_db = _load_kanban_db_or_default()
        if kanban_db is not None:
            # Production path: bind the canonical spawnable predicates onto the
            # real Hermes lib so ``_board_snapshot``'s hasattr/attribute access
            # resolves from ``__dict__`` (never the expired compat shim). Only
            # done for the LOADED real lib — an injected test fake keeps its own
            # methods untouched (its ``__dict__`` would shadow its class methods).
            _bind_spawnable_helpers(kanban_db)
    if kanban_db is None:
        return {"unreachable": _kanban_unreachable_reason(),
                "boards": [], "spawnable": [], "roomy_spawnable": [],
                "at_capacity": [], "total_running": 0,
                "max_in_progress": None, "errors": [],
                "stale_review": False, "review_count": 0,
                "review_reason": "", "stall_dispatchable": True}
    if review_capable is None:
        review_capable, review_reason = _review_dispatch_capable(kanban_db)
    else:
        review_reason = "" if review_capable else REVIEW_NOT_DISPATCHABLE_REASON
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
                "max_in_progress": max_in_progress, "errors": [],
                "stale_review": False, "review_count": 0,
                "review_reason": "", "stall_dispatchable": True}
    # Pass 1: every board's state. total_running must be COMPLETE before any
    # board's roominess is judged, because the cap is fleet-wide (see below),
    # so the roomy/at_capacity split happens in pass 2.
    snaps, errors = [], []
    for board in boards:
        snap = _board_snapshot(kanban_db, board, multi, max_in_progress,
                               max_per_profile, review_capable=review_capable,
                               review_reason=review_reason)
        if snap["error"]:
            errors.append(f"{snap['board']}: {snap['error']}")
            continue
        snaps.append(snap)
    total_running = sum(s["in_progress"] for s in snaps)
    total_pending = sum(s.get("pending", 0) for s in snaps)

    # THE CAP IS GLOBAL, NOT PER BOARD (A1, false dispatcher stall
    # 2026-10-14). hermes enforces kanban.max_in_progress ACROSS ALL BOARDS
    # (hermes_cli/kanban.py, "#33488"), but this check judged each board
    # against the cap on its OWN running count. With max_in_progress=3 and 3
    # tasks running fleet-wide, the gateway was correctly holding further work
    # while every board still looked roomy (each had <3 of its own), so the
    # probe paged "GENUINE STALL … roomy=['hscc'], total_running=3" all
    # morning and recovery eventually gave up. A board is roomy only if the
    # FLEET has a slot too: `in_progress < cap` per board AND
    # `total_running < cap` overall. The per-profile cap check is unchanged —
    # it answers a different question (could this board's waiting profiles
    # spawn even with a free slot).
    global_at_capacity = (max_in_progress is not None
                          and total_running >= max_in_progress)

    spawnable, roomy_spawnable, at_capacity = [], [], []
    for snap in snaps:
        if not snap["spawnable"]:
            continue
        spawnable.append(snap["board"])
        if snap["roomy"] and not global_at_capacity:
            roomy_spawnable.append(snap["board"])
        else:
            # A board blocked by the fleet-wide cap genuinely cannot spawn,
            # so it belongs in at_capacity; `global_at_capacity` says WHY.
            at_capacity.append(snap["board"])
    # Is a stall on these boards something a gateway restart could fix? Only if
    # at least one board we call spawnable was PROVEN so by the dispatcher's own
    # predicate. When every spawnable answer came from the raw-SQL fallback we
    # cannot rule out that the waiting assignees are not real profiles at all —
    # the dispatcher is then correctly idle, and restarting it changes nothing.
    # That is the "stall not dispatcher-fixable" class the restart latch keys on.
    stall_dispatchable = True
    if roomy_spawnable:
        stall_dispatchable = any(
            s["spawnable_proven"] for s in snaps
            if s["board"] in roomy_spawnable)
    stale_boards = [s["board"] for s in snaps if s["stale_review"]]
    return {
        "boards": ["default" if b is None else b for b in boards],
        "spawnable": spawnable, "roomy_spawnable": roomy_spawnable,
        "at_capacity": at_capacity, "total_running": total_running,
        "total_pending": total_pending,
        "max_in_progress": max_in_progress, "errors": errors,
        "global_at_capacity": global_at_capacity,
        # Review work this runtime cannot dispatch: reported separately so an
        # operator sees "N review cards parked" without it ever reading as a
        # stall (it is waiting on a human, not on the dispatcher).
        "stale_review": bool(stale_boards),
        "stale_review_boards": stale_boards,
        "review_count": sum(s["review_count"] for s in snaps
                            if s["stale_review"]),
        "review_reason": review_reason,
        "stall_dispatchable": stall_dispatchable,
    }



def _kanban_unreachable_reason():
    try:
        from . import autodown
        return autodown._KANBAN_LOAD.get("reason") or "kanban lib unreachable"
    except Exception:  # noqa: BLE001
        return "kanban lib unreachable"


# ── Layer 1: the dispatcher's own hold evidence (gateway.log) ──────────────
def parse_hold_clause(text):
    """Parse a ``Last tick held back: <payload>`` clause into (reasons, benign).

    ``reasons`` is the list of ``(name, value)`` pairs of Hermes'
    ``describe_suppression`` grammar; ``benign`` is True ONLY when the clause
    carries at least one pair AND every pair names a reason in
    ``BENIGN_HOLD_REASONS``. Unknown names, unparseable pairs and an absent
    clause (the bare zero-spawn warning — the dispatcher holding back NOTHING
    while spawning nothing) all return ``benign=False``: fail CLOSED, a hold
    the watchdog does not recognise stays wedge-worthy.
    """
    m = _HOLD_BACK_RE.search(text or "")
    if not m:
        return [], False
    reasons = []
    for part in m.group(1).split(","):
        pm = _HOLD_PAIR_RE.match(part.strip())
        if not pm:
            # Garbage inside the clause: unknown vocabulary → not benign.
            return [(m.group(1).strip(), "?")], False
        reasons.append((pm.group(1), pm.group(2)))
    if not reasons:
        return [], False
    benign = all(name in BENIGN_HOLD_REASONS for name, _v in reasons)
    return reasons, benign


def _log_line_ts(line):
    """Epoch seconds of a leading ``YYYY-MM-DD HH:MM:SS,mmm`` stamp, or None."""
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        import datetime
        return datetime.datetime.strptime(
            m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _read_gateway_log_tail(path=None, max_bytes=262_144):
    """Last ``max_bytes`` of the gateway log as lines, or None if unreadable.

    Read-only, bounded: the stuck warning is rate-limited to once per 300s, so
    256 KB of tail always spans the lookback window. A missing/unreadable log
    returns None — the caller then has NO hold evidence and (fail closed) a
    stall is judged on board state alone.
    """
    path = path if path is not None else GATEWAY_LOG
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-max_bytes, os.SEEK_END)
            except OSError:  # file smaller than the window
                f.seek(0)
            chunk = f.read().decode("utf-8", errors="replace")
        return chunk.splitlines()
    except OSError:
        return None


def _last_stuck_warning(lines, now=None):
    """The most recent 'dispatcher stuck' warning line stamped at or before
    ``now``, or None. Scans backwards: the newest applicable warning is the
    one that describes the current zero-spawn state.

    Fail-closed on stamps: a line WITHOUT a parseable timestamp, or stamped
    in the future (log replay / clock jump), is SKIPPED — the gateway's
    formatter always stamps every record, so an undated "warning" is not
    evidence of anything and must not exonerate a live wedge.
    """
    for line in reversed(lines or []):
        if not _STUCK_WARNING_RE.search(line):
            continue
        ts = _log_line_ts(line)
        if ts is None:
            continue
        if now is not None and ts > now + 60:
            continue
        return line
    return None


def classify_dispatcher_hold(now=None, log_path=None):
    """Layer-1 evidence: does the dispatcher's own last stuck warning explain
    the zero-spawn tick with BENIGN holds?

    Returns ``{"classified": bool, "benign": bool, "reasons": [...],
    "age_s": int|None}``:

      * no recent stuck warning inside ``DISPATCHER_HOLD_LOOKBACK_SECONDS``
        ⇒ ``classified=False`` — no hold evidence either way; the board-state
        decision stands unchanged (a wedged dispatcher stops writing, so a
        stale warning ages out and CANNOT mask a real wedge).
      * warning with all-benign hold names ⇒ ``classified=True, benign=True``
        — a HELD card, not a wedge (t_160d306f: ``active_pr=1`` restarted the
        gateway three times in 65 minutes).
      * warning with an unknown/non-benign name (``blocker_auth``, future
        vocabulary) or no hold clause at all ⇒ ``classified=True,
        benign=False`` — wedge-worthy, exactly as before this fix.

    Never raises; an unreadable log degrades to ``classified=False``.
    """
    if now is None:
        now = time.time()
    lines = _read_gateway_log_tail(log_path)
    if lines is None:
        return {"classified": False, "benign": False, "reasons": [],
                "age_s": None}
    line = _last_stuck_warning(lines, now=now)
    if line is None:
        return {"classified": False, "benign": False, "reasons": [],
                "age_s": None}
    ts = _log_line_ts(line)
    age = int(now - ts) if ts is not None else None
    if age is not None and age > DISPATCHER_HOLD_LOOKBACK_SECONDS:
        # The warning predates the current state (log stalled / dispatcher
        # wedged and stopped writing): it cannot exonerate a live wedge.
        return {"classified": False, "benign": False, "reasons": [],
                "age_s": age}
    reasons, benign = parse_hold_clause(line)
    return {"classified": True, "benign": benign, "reasons": reasons,
            "age_s": age}


# ── The stall decision ─────────────────────────────────────────────────────
def evaluate_stall_tick(capture, detector_state, now, hold_info=None):
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
        (``total_running`` did not increase since the previous tick), AND
      * t_160d306f: the dispatcher's own hold evidence (``hold_info`` from
        ``classify_dispatcher_hold``) does NOT explain the zero-spawn tick
        with BENIGN holds. A card held by ``active_pr`` / ``recent_success`` /
        a cooldown is the dispatcher working as designed — holding it back is
        not a wedge (the 2026-10-17 incident restarted the gateway 3× for
        exactly this).

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

    # t_160d306f benign-hold exemption — every condition must hold, or the
    # tick is judged exactly as before (fail closed):
    #   * the dispatcher's last stuck warning is FRESH (inside the lookback —
    #     a wedged dispatcher stops writing, so its exemption expires on its
    #     own) and every hold name in it is benign;
    #   * the claimed holds add up: the dispatcher says it held back >=1 task
    #     this tick and the boards really do have >= claimed pending cards.
    #     A hold clause borrowed from a stale/other state cannot reconcile
    #     against a queue that no longer carries the work;
    #   * a hold that PERSISTS while the warning stays fresh exempts for as
    #     long as it lasts — the incident's hours-long active_pr=1 is exactly
    #     the healthy steady state this exists to protect. The recovery-side
    #     board-state gate (layer 2) independently re-verifies the hold
    #     against the cards' own respawn_guarded events before ANY restart,
    #     so a dispatcher frozen while CLAIMING a benign hold still escalates
    #     when the cards show no such hold.
    explained = False
    if hold_info and hold_info.get("classified") and hold_info.get("benign"):
        claimed = sum(int(v) for _n, v in (hold_info.get("reasons") or [])
                      if v.isdigit())
        pending = int(capture.get("total_pending", 0) or 0)
        explained = bool(claimed) and pending >= claimed
        detector_state["benign_hold_ticks"] = (
            detector_state.get("benign_hold_ticks", 0) + 1 if explained else 0)
    else:
        detector_state["benign_hold_ticks"] = 0
    detector_state["last_hold_explained"] = explained

    if roomy and not spawned and not explained:
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
    if was_declared and (not roomy or spawned or explained):
        # Stall cleared: a worker spawned (dispatcher working again), the work
        # drained / every board filled up (nothing left to spawn), or a benign
        # dispatcher hold arrived and explains the zero-spawn (t_160d306f).
        detector_state["declared"] = False
        detector_state["declared_since"] = None
        detector_state["stall_ticks"] = 0
        detector_state["peak_stall_ticks"] = 0
        return -1, False
    if was_declared:
        # Still mid-stall this tick.
        return 0, True
    return 0, False


def _stall_signature(capture):
    """Stable identity of a stall: the boards that have spawnable work + room.

    Two ticks share a signature iff the dispatcher faces the same situation, so
    "we already restarted for this" is answerable without trusting that the
    stream stayed red across the interval. Sorted so board enumeration order
    cannot make the same incident look new.
    """
    return "|".join(sorted(
        set(capture.get("spawnable") or [])
        | {f"roomy:{b}" for b in (capture.get("roomy_spawnable") or [])}))


def _maybe_warn_stale_review(capture, now):
    """One warning per cooldown window for review work this runtime cannot spawn.

    A card parked in ``review`` on a runtime with no review lane (or with
    ``kanban.review_dispatch: false``) is waiting on a HUMAN. That is worth
    telling the operator about, and worth having on the stream so ``hscc
    status`` can show it — but it is NOT a dispatcher stall: no dispatcher state
    and no gateway restart can clear it, and treating it as one cost four
    gateway restarts on 2026-10-15. So this path writes a log line and stream
    fields ONLY — never ``ok: False``, never the wedge/recovery state file,
    never a restart. Rate-limited because a parked card is a steady state.
    """
    if not capture.get("stale_review"):
        return False
    last = _stale_review_warn.get("last_at")
    if last is not None and (now - last) < DISPATCHER_RECOVER_COOLDOWN_SECONDS:
        return False
    _stale_review_warn["last_at"] = now
    boards = capture.get("stale_review_boards") or []
    log(f"Dispatcher-wedge check: {capture.get('review_count', 0)} review "
        f"card(s) not dispatchable by this runtime — review feature not "
        f"enabled ({capture.get('review_reason') or 'unknown reason'}) on "
        f"board(s) {boards}. NOT a dispatcher stall: nothing to spawn, nothing "
        f"to restart. Close/approve the cards, or enable the review lane.",
        "WARNING")
    return True


def check_dispatcher_wedge(kanban_db=None, max_in_progress=None, now=None,
                           write_state_fn=None, max_per_profile=None,
                           review_capable=None, hold_fn=None):
    """The dispatcher-wedge probe (the ``dispatcher`` CHECK STREAM).

    Runs on the daemon's periodic cadence (see ``PERIODIC_INTERVALS``). Reads
    kanban state, makes one stall-tick decision, and writes the ``dispatcher``
    state stream. Returns True (ok) unless a genuine stall is currently
    DECLARED — matching how the other health checks return their ``ok``.

    The stream's ``ok`` is what propagates everywhere: ``hscc status`` FAIL
    row, ``hscc verify`` red, ``state.dispatcher.degraded`` trigger event.

    Injectable: ``kanban_db`` (fake lib), ``max_in_progress`` (avoids reading
    the operator's real config in tests), ``now`` (clock), ``write_state_fn``
    (avoids touching the real state dir), ``review_capable`` (pins the
    runtime's review-dispatch capability instead of probing it), ``hold_fn``
    (layer-1 hold evidence; defaults to ``classify_dispatcher_hold`` reading
    the gateway log).
    """
    from .state import now_iso, write_state
    write_state_fn = write_state_fn or write_state
    if now is None:
        now = time.time()

    capture = _capture_kanban(kanban_db=kanban_db,
                              max_per_profile=max_per_profile,
                              max_in_progress=max_in_progress,
                              review_capable=review_capable)

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

    # Review cards this runtime cannot spawn are reported here, and ONLY here:
    # before the stall decision, so they never influence it, and with no state
    # mutation beyond the rate-limit timestamp.
    warned = _maybe_warn_stale_review(capture, now)

    # Layer 1 (t_160d306f): ask the dispatcher itself, from its own log,
    # whether the last zero-spawn tick was a benign hold (active_pr,
    # recent_success, cooldowns). Fail closed — see classify_dispatcher_hold.
    hold_info = (hold_fn or classify_dispatcher_hold)(now=now)

    change, stall = evaluate_stall_tick(capture, _detector, now,
                                        hold_info=hold_info)

    declared = _detector["declared"]
    details = {
        "boards": capture["boards"],
        "spawnable": capture["spawnable"],
        "roomy_spawnable": capture["roomy_spawnable"],
        "at_capacity": capture["at_capacity"],
        "total_running": capture["total_running"],
        "max_in_progress": capture["max_in_progress"],
        # Exposed on the stream (not just the capture) so an operator reading
        # `hscc status`/`verify` can tell a fleet-wide cap from a board cap
        # without re-deriving it. A1.
        "global_at_capacity": capture.get("global_at_capacity", False),
        "stall_ticks": _detector["stall_ticks"],
        "errors": capture["errors"],
        # t_160d306f: what the dispatcher said, and whether we believed it.
        "hold_explained": _detector.get("last_hold_explained", False),
        "hold_reasons": [f"{n}={v}" for n, v in
                         (hold_info or {}).get("reasons", [])],
        # Review work present but NOT dispatchable by this runtime: reported so
        # it is visible, deliberately kept out of `spawnable` so it can never
        # declare a stall. `stall_dispatchable` tells the recovery whether a
        # restart could plausibly help at all (see recover_dispatcher_wedge).
        "stale_review": capture.get("stale_review", False),
        "stale_review_boards": capture.get("stale_review_boards", []),
        "review_count": capture.get("review_count", 0),
        "review_reason": capture.get("review_reason", ""),
        "stall_dispatchable": capture.get("stall_dispatchable", True),
        "stall_signature": _stall_signature(capture),
    }

    if change == -1:
        # A declared stall cleared — report the duration in the recovery end
        # of the logs/message only; here we just go green.
        log("Dispatcher-wedge check: genuine stall CLEARED (work drained / "
            "boards at capacity / benign dispatcher hold) — stream back to OK")

    if not declared:
        msg = ("dispatcher healthy — no genuine stall (%s)"
               % _healthy_reason(capture, details))
        if warned:
            msg += (" — %s review card(s) not dispatchable by this runtime "
                    "(review feature not enabled)"
                    % capture.get("review_count", 0))
        write_state_fn(DISPATCHER_STREAM, {
            "ok": True, **details,
            "last_check": now_iso(),
            "message": msg,
        })
        if details["hold_explained"]:
            log(f"Dispatcher-wedge check: OK — zero spawns explained by "
                f"benign dispatcher hold "
                f"{details['hold_reasons']} (held != wedged, t_160d306f)")
        else:
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
                   ">= %ss — dispatcher is wedged%s" % (
                       DISPATCHER_DETECT_TICKS,
                       "" if capture.get("stall_dispatchable", True) else
                       " (NOT proven dispatcher-fixable: the spawnable answer "
                       "came from the raw-SQL fallback, so a gateway restart "
                       "may not help)"),
    })
    log(f"Dispatcher-wedge check: GENUINE STALL detected — spawnable="
        f"{capture['spawnable']}, roomy={capture['roomy_spawnable']}, "
        f"total_running={capture['total_running']}, "
        f"dispatchable={capture.get('stall_dispatchable', True)}", "ERROR")
    return False


def _healthy_reason(capture, details=None):
    # t_160d306f: a benign dispatcher hold is its own healthy reason and wins
    # over the stale board-derived branches — "no spawnable work" would be
    # flatly wrong when the queue IS non-empty and simply held back.
    if details and details.get("hold_explained"):
        return (f"zero spawns explained by benign dispatcher hold "
                f"{details.get('hold_reasons') or []} (held != wedged)")
    if not capture.get("spawnable"):
        return "no spawnable ready work (correctly idle)"
    if not capture.get("roomy_spawnable"):
        # A1 renamed this to "at_capacity (global)" because the cap is
        # fleet-wide. The condition is deliberately NOT ``and not
        # global_at_capacity``: total_running >= any board's in_progress, so a
        # board that is at capacity forces the fleet to be at capacity too —
        # a board-at-capacity-but-fleet-has-room state is unreachable, and
        # keeping a branch for it would be dead code that reads like a case.
        return ("all spawnable boards at capacity — at_capacity (global) "
                f"(total_running={capture.get('total_running')} >= "
                f"max_in_progress={capture.get('max_in_progress')})")
    return "worker recently spawned"


# ── Layer 2: belt-and-braces board-state gate (sanctioned hermes CLI) ──────
# Runs ONLY on the restart path of ``recover_dispatcher_wedge``. Consults the
# board through the sanctioned ``hermes kanban`` CLI — never a direct DB read,
# never Hermes' internals — and answers one question: could a human reading
# ``kanban list``/``kanban show`` right now EXPLAIN every ready card's zero
# spawn (cap reached, per-profile cap, active PR, cooldown)? If yes, this is
# not a wedge and the restart must not happen.
#
# Fail CLOSED in every direction: an unreachable CLI, an unparseable payload,
# a board whose ready cards cannot be listed, or a card with no hold evidence
# all return "not explained" → the restart proceeds exactly as before.
_GATE_CLI_TIMEOUT_SECONDS = 20


def _hermes_cli_argv():
    """Argv prefix for the sanctioned hermes CLI (resolved at CALL time).

    Deferred import of ``lifecycle.find_hermes_bin`` keeps this module
    importable without the binary on PATH (and test-injectable). Returns None
    when hermes cannot be found.
    """
    try:
        from .lifecycle import find_hermes_bin
        bin_path = find_hermes_bin()
    except Exception:  # noqa: BLE001
        bin_path = None
    return [bin_path, "kanban"] if bin_path else None


def _gate_cli_json(cli_fn, argv):
    """Run the hermes kanban CLI and parse its JSON stdout, or None.

    Any failure (non-zero exit, timeout, bad JSON) returns None — the caller
    treats that as "unexplained" (fail closed).
    """
    try:
        proc = cli_fn(argv)
    except Exception:  # noqa: BLE001
        return None
    if proc is None or getattr(proc, "returncode", 1) != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except Exception:  # noqa: BLE001
        return None


def _default_gate_cli_runner(argv):
    """Default runner for the board-state gate: list-form subprocess, no shell."""
    import subprocess
    try:
        return subprocess.run(list(argv), capture_output=True, text=True,
                              timeout=_GATE_CLI_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001
        return None


def _board_ready_facts(cli_fn, board, kanban_db, max_in_progress,
                       max_per_profile, now, nonspawnable=None):
    """Explain the zero-spawn state of ONE spawnable board.

    Returns ``(explained: bool, why: str)``. ``explained`` is True ONLY when
    every spawnable ready card is accounted for:

      * the fleet is at ``max_in_progress`` ⇒ every board is explained by the
        global cap (checked by the caller); or
      * each ready card's assignee is at its per-profile cap; or
      * the card's recent events carry a benign ``respawn_guarded`` /
        released-to-ready hold (``active_pr`` / ``recent_success`` /
        ``rate_limited``) inside the guard windows — the board-side twin of
        the ``describe_suppression`` clause, read via ``hermes kanban show``; or
      * the card is not spawnable at all (its assignee is not a Hermes
        profile — the dispatcher's own ``has_spawnable_ready`` decides).

    Anything else — a plain ready card with no hold evidence, a board whose
    list cannot be read — returns False (wedge stays actionable).

    ``nonspawnable`` (optional list, filled in place) is the audit trail for
    the THIRD kind of explanation: cards the dispatcher idles on because their
    assignee is definitely not a Hermes profile. Those hold the restart (a
    restart never fixes a typo'd name) but they are NOT a benign dispatcher
    hold — the caller notifies the operator about them, because "no worker will
    ever run this card" is exactly the silent no-dispatch failure mode this
    watchdog exists to surface (t_ab960663, PR #43 review).
    """
    list_argv = (["--board", board] if board and board != "default" else []) \
        + ["list", "--status", "ready", "--json"]
    ready = _gate_cli_json(cli_fn, list_argv)
    if not isinstance(ready, list):
        return False, f"ready list for board '{board}' unreadable via CLI"
    if not ready:
        # The probe saw spawnable ready work but the CLI lists none: state
        # moved or the two disagree — do not trust the explanation.
        return False, f"board '{board}': probe saw spawnable work, CLI listed none"

    capped = _capped_assignees(kanban_db, board, max_per_profile)
    unexplained = []
    hold_evidence = []
    dead_lanes = []
    for task in ready:
        assignee = task.get("assignee")
        if assignee is None:
            # Unassigned ready work: the dispatcher would apply the default
            # assignee and spawn — holding it back needs no guard. Unexplained.
            unexplained.append(task.get("id") or "?")
            continue
        if _assignee_is_nonspawnable(assignee):
            # Control-plane lane (not a Hermes profile): the dispatcher
            # correctly idles on it — the dispatcher's own
            # skipped_nonspawnable bucket. Explainable, but recorded: a name
            # that no profile matches may also just be a typo, and the
            # watchdog must not go quiet on that.
            dead_lanes.append({"board": board or "default",
                               "card": task.get("id") or "?",
                               "assignee": assignee})
            continue
        if capped is not None and assignee in capped:
            continue  # per-profile cap — explainable
        status, why = _card_hold_from_cli(cli_fn, board, task.get("id"), now)
        if status == "spawnable-hold":
            hold_evidence.append(f"{task.get('id')}: {why}")
            continue
        unexplained.append(f"{task.get('id') or '?'} ({why})")
    if unexplained:
        return False, (f"board '{board}': unexplained ready cards: "
                       + ", ".join(unexplained[:5]))
    # Only an EXPLAINED board contributes: when a board is unexplained the
    # caller restarts anyway, and its partial findings would be noise.
    if nonspawnable is not None:
        nonspawnable.extend(dead_lanes)
    dead = ", ".join("%s=%s" % (d["card"], d["assignee"])
                     for d in dead_lanes[:5])
    if dead_lanes and len(dead_lanes) == len(ready):
        # Nothing on this board is runnable at all — say so in the evidence
        # line rather than hiding it behind the generic "explainably held".
        why = (f"board '{board}': all {len(ready)} ready cards have "
               f"non-spawnable assignees [{dead}] — no worker can ever "
               "claim them")
    else:
        why = (f"board '{board}': all {len(ready)} ready cards explainably "
               "held")
        if dead_lanes:
            why += f" [non-spawnable assignee: {dead}]"
    if hold_evidence:
        why += " [" + "; ".join(hold_evidence[:5]) + "]"
    return True, why


def _assignee_is_nonspawnable(assignee):
    """True iff ``assignee`` is DEFINITELY not a dispatchable Hermes profile.

    Mirrors the dispatcher's ``skipped_nonspawnable`` bucket (control-plane
    lanes like ``orion-cc``). Unknown (``hermes_cli.profiles`` unimportable —
    test interpreter, no Hermes on PATH) returns False = "assume spawnable",
    which keeps the stricter hold-evidence requirement in force (fail closed).
    """
    try:
        from hermes_cli.profiles import profile_exists
    except Exception:  # noqa: BLE001
        return False
    try:
        return not bool(profile_exists(assignee))
    except Exception:  # noqa: BLE001
        return False


def _capped_assignees(kanban_db, board, max_per_profile):
    """Assignees at their per-profile cap on one board, or None if unknown.

    Mirrors the dispatcher's per-profile rule: an assignee with ready work
    whose running count is >= ``max_per_profile``. None (no cap configured /
    unreadable) means "no per-profile explanation available".
    """
    if max_per_profile is None:
        return None
    try:
        multi = bool(board) and board != "default"
        with (kanban_db.connect_closing(board=board) if multi
              else kanban_db.connect_closing()) as conn:
            rows = conn.execute(
                "SELECT assignee, COUNT(*) AS n FROM tasks "
                "WHERE status = 'running' AND assignee IS NOT NULL "
                "GROUP BY assignee").fetchall()
    except Exception:  # noqa: BLE001
        return None
    return {r[0] for r in rows if int(r[1]) >= max_per_profile}


def _card_hold_from_cli(cli_fn, board, task_id, now):
    """Classify one ready card via ``hermes kanban show --json``.

    Returns ``(status, why)``:

      * ``("spawnable-hold", reason)`` — a benign hold event (respawn_guarded
        active_pr/recent_success, or a rate_limited release) within its
        Hermes-side guard window. Windows mirror the dispatcher's own
        (3600 s success, 86400 s PR).
      * ``("unexplained", why)`` — no hold evidence (fail closed).
    """
    if not task_id:
        return "unexplained", "no id"
    show_argv = (["--board", board] if board and board != "default" else []) \
        + ["show", task_id, "--json"]
    data = _gate_cli_json(cli_fn, show_argv)
    if not isinstance(data, dict):
        return "unexplained", "show unreadable"
    # Guard windows as Hermes defines them (kanban_db_dispatch.py).
    _SUCCESS_WINDOW = 3600
    _PR_WINDOW = 86400
    # A worker that bailed on a provider quota wall requeues with a
    # rate-limited sentinel in last_failure_error (kanban_db_dispatch.py's
    # dead-worker classification) — the dispatcher deliberately does NOT
    # respawn into the same wall, which is a cooldown hold, not a wedge.
    lfe = ((data.get("task") or {}).get("last_failure_error") or "").lower()
    if ("rate" in lfe and "limit" in lfe) or "tempfail" in lfe \
            or "quota" in lfe:
        return "spawnable-hold", "rate_limited(last_failure_error)"
    for ev in (data.get("events") or []):
        kind = ev.get("kind")
        payload = ev.get("payload") or {}
        age = now - float(ev.get("created_at") or 0)
        if kind == "respawn_guarded":
            reason = payload.get("reason")
            if reason in ("active_pr", "recent_success") and age <= _PR_WINDOW:
                return "spawnable-hold", f"respawn_guarded:{reason}"
            if reason == "blocker_auth":
                return "unexplained", "blocker_auth"
            if reason:
                return "unexplained", f"respawn_guarded:{reason}"
        if kind == "rate_limited" and age <= _SUCCESS_WINDOW:
            return "spawnable-hold", "rate_limited"
    return "unexplained", "no recent hold event"


class GateVerdict(tuple):
    """The gate's ``(explained, detail)`` pair, plus the structured findings.

    A ``tuple`` subclass on purpose: the gate's documented contract is a
    2-tuple and every existing caller/test unpacks it as such (including the
    injectable fakes in the tests), so this adds information without changing
    the shape. ``nonspawnable`` is a tuple of
    ``{"board":…, "card":…, "assignee":…}`` dicts naming the ready cards the
    dispatcher idles on because their assignee is DEFINITELY not a Hermes
    profile (t_ab960663). Suppression and notification are two different
    questions: those cards legitimately suppress the restart — restarting the
    gateway never fixes a typo'd profile name — but they must NOT make the
    watchdog go quiet, because "no worker will ever run this card" is the
    silent no-dispatch failure this daemon exists to surface.
    """

    # Class-level default so a plain 2-tuple (what the injectable test gates
    # return) and a GateVerdict are read the same way by callers.
    _nonspawnable = ()

    def __new__(cls, explained, detail, nonspawnable=()):
        obj = tuple.__new__(cls, (bool(explained), str(detail)))
        obj._nonspawnable = tuple(nonspawnable or ())
        return obj

    @property
    def nonspawnable(self):
        return self._nonspawnable


def _gate_nonspawnable(verdict):
    """The non-spawnable findings carried by a gate verdict ([] for a plain
    2-tuple, so custom/injected gates keep working unchanged)."""
    return list(getattr(verdict, "nonspawnable", ()) or [])


def board_state_explains_stall(stream, kanban_db=None, max_in_progress=None,
                               max_per_profile=None, cli_fn=None, now=None):
    """Layer-2 verdict for a DECLARED stall: do the boards themselves prove
    the dispatcher is merely holding work back (NOT a wedge)?

    Returns a :class:`GateVerdict` — a 2-tuple ``(explained: bool,
    detail: str)`` that also carries ``.nonspawnable``, the ready cards whose
    assignee is not a Hermes profile at all. Fail CLOSED: any doubt
    (missing CLI, unreadable payload, a board we cannot fully account for)
    returns False so the restart proceeds. ``cli_fn`` is injectable (tests
    stub the CLI); production defaults to ``_default_gate_cli_runner``.
    """
    if now is None:
        now = time.time()
    base = _hermes_cli_argv()
    if base is None:
        return False, "hermes CLI not found — gate cannot verify board state"
    # Callers (and tests) pass board-level argv; the hermes/kanban prefix is
    # ALWAYS bound here so an injected fake sees exactly what a human would
    # type after `hermes kanban`.
    _runner = cli_fn or _default_gate_cli_runner

    def run_cli(argv):
        return _runner(base + list(argv))

    if kanban_db is None:
        kanban_db = _load_kanban_db_or_default()
        if kanban_db is not None:
            _bind_spawnable_helpers(kanban_db)
    if kanban_db is None:
        return False, "kanban lib unreachable — board state unverifiable"
    if max_in_progress is None:
        max_in_progress = _read_max_in_progress()
    if max_per_profile is None:
        max_per_profile = _read_max_per_profile()

    # The global cap explains EVERY board at once: the dispatcher is correctly
    # refusing to exceed max_in_progress.
    total_running = int((stream or {}).get("total_running", 0) or 0)
    if (max_in_progress is not None
            and total_running >= max_in_progress):
        return True, (f"fleet at capacity: total_running={total_running} >= "
                      f"max_in_progress={max_in_progress}")

    spawnable = (stream or {}).get("spawnable") or []
    if not spawnable:
        # Red stream that names no spawnable board: nothing to explain, keep
        # the restart path (fail closed — the stream is our only witness).
        return GateVerdict(False, "stream names no spawnable board")
    reasons = []
    nonspawnable = []
    for board in spawnable:
        explained, why = _board_ready_facts(
            run_cli, board, kanban_db, max_in_progress, max_per_profile, now,
            nonspawnable)
        reasons.append(why)
        if not explained:
            return GateVerdict(False, "; ".join(reasons))
    return GateVerdict(True, "; ".join(reasons), nonspawnable)


def _notify_hold(msg, title="HSCC Dispatcher Hold (no restart)"):
    """Best-effort operator notify for an explained-but-stalled dispatcher.
    Never raises; mirrors autodown._notify."""
    try:
        from .desktop import send_macos_notification
        send_macos_notification(title, msg, priority="normal")
    except Exception:  # noqa: BLE001
        pass


def _notify_nonspawnable(msg):
    """Operator notify for the config-typo class (t_ab960663). Same single
    notify channel as ``_notify_hold`` — a different TITLE only, so the
    operator can tell 'dispatcher is holding, sit tight' apart from 'a ready
    card will never be run by anyone' without opening the message."""
    return _notify_hold(msg, title="HSCC Dispatcher: unrunnable ready card")


def _nonspawnable_message(dead):
    """The operator-facing line for ready cards no worker will ever claim.

    ``dead`` is the gate's list of ``{"board", "card", "assignee"}`` findings.
    Names every card/board/assignee (the whole point of the ping is that the
    operator can fix the assignment without opening a terminal), states the
    class of failure (a profile that does not exist — a config typo, not a
    dispatcher fault) and that NO restart was attempted.
    """
    if len(dead) == 1:
        d = dead[0]
        body = (f"ready card {d['card']} on board {d['board']} has "
                f"non-spawnable assignee {d['assignee']!r}: no worker will "
                "ever run it")
    else:
        body = ("%d ready cards have non-spawnable assignees, so no worker "
                "will ever run them: %s" % (
                    len(dead),
                    "; ".join(f"{d['card']} on board {d['board']} -> "
                              f"{d['assignee']!r}" for d in dead[:10])))
    return (body + " — that assignee names no Hermes profile (a config typo "
            "or a deleted profile, not a dispatcher wedge), so NO gateway "
            "restart was attempted: fix the card's assignee "
            "(`hermes kanban assign` / the board UI) or create the profile. "
            "A restart can never fix this.")


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
                             state_file=None, board_gate_fn=None,
                             notify_fn=None):
    """One guarded self-heal pass for a wedged dispatcher. Never raises.

    Consumes ``stream_state`` (the latest ``dispatcher`` stream; if None it is
    read from the state dir) and restarts the gateway — but ONLY after:

      * the probe has DECLARED a genuine stall (``ok: False``) for M
        consecutive detections (``DISPATCHER_RESTART_AFTER_DETECTIONS``),
      * outside the cooldown window,
      * under the max-attempt cap,
      * t_160d306f: the board-state gate (``board_state_explains_stall``, via
        the sanctioned hermes CLI) cannot explain every ready card as a benign
        hold (cap reached / per-profile cap / active PR / cooldown). If it CAN
        — the dispatcher is holding, not wedging — the restart is suppressed
        and the operator is notified with the reason instead. The gate fails
        CLOSED: a gate that cannot verify the boards lets the restart through.
      * the stall has not already been paid for (the per-incident latch below).

    Once the cap is hit we STOP acting and keep alerting, exactly like
    ``recover.py``. The restart is injectable (``restart_fn``); production
    defaults to ``_run_restart`` (the real ``launchctl kickstart``), tests pass
    a fake so no live gateway is ever touched.

    PER-INCIDENT RESTART LATCH (2026-10-15 gateway restart loop)
      The cooldown + attempt cap bound restarts *within a run of red ticks*, and
      the green-clear below renews that budget. Together they let a stall the
      runtime CANNOT fix restart the gateway forever: the cooldown expired, a
      tick went green (the fleet briefly spawned something else) and renewed the
      budget, the next red re-armed it — so one merged PR parked in ``review``
      that nothing could ever spawn restarted the gateway 4 times in 2 hours.
      Fix: every restart records ``last_incident = {"signature", "at",
      "dispatchable"}`` in the recover state, where ``signature`` is the stall's
      spawnable/roomy board set (``_stall_signature``). Semantics chosen:

      1. Applies ONLY to a stall the runtime cannot dispatch
         (``stall_dispatchable: False``) — i.e. we know no dispatcher action can
         clear it. A restart is allowed ONCE (the first one, in case a restart is
         what registers a missing lane), and after that the signature is
         remembered and never restarted again.
      2. Same ``signature`` as ``last_incident`` → never restart again, EVEN IF
         the stream went green in between. A green tick means "something else
         spawned", not "this stall is resolved"; that re-arming is precisely what
         looped the gateway. A DIFFERENT non-dispatchable signature is likewise
         refused: a restart cannot fix a stall this runtime cannot dispatch.
      3. A stall the runtime CAN dispatch is NOT latched at all: today's
         cooldown + max-attempt semantics apply byte-for-byte, so a real wedge
         keeps the recovery behaviour libV4 has always had (A1's green-renewal
         included). ``last_incident`` is still overwritten on each restart, as
         bookkeeping.
      4. A green tick clears ``gave_up``/``attempts`` (A1's renewal, kept for
         both classes) but deliberately does NOT clear ``last_incident`` — that
         is the whole point of items 1-2.

    Returns ``{"result": ...}`` where result is one of ``skipped``
    (intentional), ``none-wedged`` (no declared stall / below M), ``suppressed``
    (declared but held by cooldown/cap/benign-hold gate/incident latch),
    ``recovered``
    (restarted), or ``gave-up`` (attempt cap reached).
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
        # A CONFIRMED-HEALTHY stream clears the attempt budget (A1, 2026-10-14).
        # Once the attempt cap set ``gave_up`` it persisted forever: the very
        # next wedge — possibly weeks later, after the operator fixed the real
        # problem — could never self-heal, and un-sticking it meant hand-editing
        # ``~/.hscc/dispatcher_recover.json``. ``gave_up`` bounds restarts WITHIN
        # one incident; "the stream is green again" means that incident is over,
        # so the budget is renewed.
        #
        # What the green does NOT renew is ``last_incident`` — see the latch
        # semantics in this docstring. The green cleared the *attempt budget*,
        # not the memory that we already restarted for this signature.
        #
        # Only a REAL green clears it. The unreachable-kanban path writes
        # ``ok: True`` as a fail-safe (we did not observe health, we just
        # refused to page on our own read failure) — treating that as a
        # recovery would renew the budget precisely when we know least.
        if stream.get("ok") is True and "unreadable" not in stream:
            state = _load_recover_state(state_file)
            if state.get("gave_up") or state.get("attempts"):
                # Keep `last_recovery_at`: the cooldown protects a restart that
                # just happened, and the probe's green is not proof it worked.
                # Keep `last_incident` too: it is the restart latch.
                state.pop("gave_up", None)
                state.pop("attempts", None)
                state["resolved_at"] = now
                _save_recover_state(state, state_file)
                log("Dispatcher-wedge recovery: stream green — wedge "
                    "resolved, attempt budget renewed (incident latch kept)")
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

    # -- Per-incident restart latch ------------------------------------------
    # See the latch semantics in this docstring (items 1-3). This check runs
    # BEFORE the cooldown so a latched incident reports the real reason it is
    # not acting rather than a cooldown that is not what is holding it.
    #
    # A DISPATCHABLE stall is never latched (item 3): the loop below is exactly
    # today's cooldown + max-attempt behaviour.
    signature = stream.get("stall_signature")
    dispatchable = stream.get("stall_dispatchable", True)
    last_incident = state.get("last_incident") or {}
    if not dispatchable:
        paid_for = (last_incident.get("signature") == signature
                    if signature else False)
        # A restart that was itself spent on an unfixable stall proved it did
        # not fix the class, so no further unfixable stall gets one.
        spent_on_unfixable = bool(last_incident) and \
            not last_incident.get("dispatchable", True)
        if paid_for or spent_on_unfixable:
            log("Dispatcher-wedge recovery: stall is NOT dispatcher-fixable "
                f"({stream.get('review_reason') or 'see stream'}) and a restart "
                f"was already spent ({last_incident.get('at')}) — NOT "
                "restarting again; keeping alerting")
            return {"result": "suppressed", "reason": "not_dispatcher_fixable",
                    "signature": signature, "actions": []}

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

    # -- t_160d306f belt-and-braces: is this really a wedge? ----------------
    # Every guard above passed and we are about to kick launchd. One more
    # check FIRST: ask the boards themselves (sanctioned hermes CLI) whether
    # every ready card is explainably held — cap reached, per-profile cap,
    # active PR, cooldown. A held-back card is the dispatcher working as
    # designed; restarting it would (and did, 2026-10-17) churn the gateway
    # every cooldown window for nothing. Explainable ⇒ log + notify, NO
    # restart. Unverifiable ⇒ fail closed and restart (the original purpose).
    gate_fn = board_gate_fn or board_state_explains_stall
    try:
        gate_verdict = gate_fn(stream)
        explained, gate_detail = gate_verdict
    except Exception as e:  # noqa: BLE001 - a broken gate never blocks action
        gate_verdict = None
        explained, gate_detail = False, f"gate error: {e}"
    if explained:
        global _hold_notify_at
        # t_ab960663 (PR #43 review): suppression and notification are two
        # different questions. A ready card whose assignee is not a Hermes
        # profile at all DOES suppress the restart — `launchctl kickstart`
        # has never once fixed a typo'd profile name — but going quiet about
        # it was the bug: "no worker will ever run this card" is precisely the
        # silent no-dispatch failure this watchdog exists to surface. When the
        # gate names such cards, the operator ping is the DISTINCT config-typo
        # message (naming card/board/assignee), not the generic "dispatcher is
        # only holding work back" line — on a mixed board the generic text
        # would bury the one finding the operator can actually act on. The
        # full board evidence stays in the log line.
        dead = _gate_nonspawnable(gate_verdict)
        if dead:
            msg = _nonspawnable_message(dead)
            reason = "nonspawnable_assignee"
            log("Dispatcher-wedge recovery: SUPPRESSED — " + msg
                + f" (full board evidence: {gate_detail})")
        else:
            msg = (f"Dispatcher stream is red but the board says the "
                   f"dispatcher is only HOLDING work back (not wedged) — no "
                   f"gateway restart. Evidence: {gate_detail}")
            reason = "benign_dispatcher_hold"
            log("Dispatcher-wedge recovery: SUPPRESSED — " + msg)
        if _hold_notify_at is None or (
                now - _hold_notify_at
                >= DISPATCHER_HOLD_NOTIFY_COOLDOWN_SECONDS):
            _hold_notify_at = now
            # One throttled notify channel (``notify_fn`` in tests,
            # ``_notify_hold`` → desktop sender in production). The
            # non-spawnable class only changes the title, never the path.
            notify = notify_fn or (
                _notify_nonspawnable if dead else _notify_hold)
            try:
                notify(msg)
            except Exception:  # noqa: BLE001 - notify is best-effort
                pass
        result = {"result": "suppressed", "reason": reason,
                  "detail": gate_detail, "actions": []}
        if dead:
            result["nonspawnable"] = dead
        return result

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
    # The incident latch: remember WHOSE fault we restarted for, so neither a
    # cooldown expiry nor a green tick can make us restart for the same stall
    # again (latch semantics 1-3 in this docstring).
    state["last_incident"] = {"signature": signature or "unknown",
                              "at": now, "dispatchable": bool(dispatchable)}
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
        f"success={ok}) for stall signature {signature!r} "
        f"(dispatchable={dispatchable})")
    return {"result": "recovered", "restart_ok": ok, "actions": [action]}

