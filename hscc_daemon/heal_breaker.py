"""Per-node heal circuit breaker (F5, HEAL_CASCADE 2026-10-14).

WHY THIS EXISTS
---------------
On 2026-10-14 the fleet was destroyed by healing, not by the fault. One wedged
unit produced FOUR repair mechanisms firing every ~60s for ~35 minutes: the log
records the same worker being auto-healed at 07:21:45, 07:32:40 and 07:43:19
while other paths relaunched it every minute on top of that. Nothing counted
how many times a node had been attacked in a row, so an unbounded repair loop
ran against hardware that was already degrading — and .244/.247 hard-hung and
needed a manual power-cycle.

A repair loop that never gives up is not persistence, it is the fault. F2's
lease makes repairs *sequential*; this makes them *finite per node*: after
HEAL_BREAKER_MAX actions on one node inside HEAL_BREAKER_WINDOW_S, hscc stops
healing that node, tells the operator loudly, and leaves it alone until the
window passes (or the operator clears it).

DURABLE, NOT IN-MEMORY
----------------------
The incident's in-memory flaw: `_worker_down_streak` and
`_worker_last_autoheal` reset when the daemon restarts, so a crash-looping
daemon got a fresh strike count every time (see docs/audits/HEAL_CASCADE_*.md,
F2). This file lives under ~/.hscc beside the heal lease, so a restart — even
one caused by the healing itself — does not reset the count. Verified by
reading it back with the module's memory cleared (test_durability).

Alert de-duplication: the strike ledger and the *notification* cooldown are
separate. Latching must alert exactly once, not once per suppressed tick —
the 2026-10-08 NAS rule fired 178 macOS notifications in 11 minutes by getting
that wrong (trigger.py:22).

A PAUSED NODE IS NOT AN OUTAGE (t_e6ee7e60)
-------------------------------------------
``hscc node pause`` parks one node on purpose and edits no cluster setting, so a
parked node's units are simply down with nothing anywhere the repair paths look
saying "deliberate". This module read that as a fault: every down tick appended
a strike here, and after HEAL_BREAKER_MAX of them inside the window the breaker
latched, emitted ``heal_breaker.tripped`` at ``critical`` and pushed a desktop
notification about a node the operator parked themselves — while leaving that
node holding a slate of strikes that outlived the pause and kept it unhealable
after the resume.

``note_heal()`` is the strike/trip decision seam, so the node-pause overlay is
consulted there and nowhere else: while the overlay holds the node, no strike is
appended, the ledger is not opened for writing at all, and ``tripped`` is False
— which means no latch, no event and no notification. The overlay is READ,
never written; this module still writes exactly one file, its own ledger.

ASK ``covering``, NEVER ``paused``

``node_pauses.paused(host)`` is host-keyed: the other nodes of a
tensor-parallel unit — stopped as one thing — appear only in the record's
``span_nodes``. A seam that asked ``paused()`` would keep striking the rest of
a span the operator parked. ``covering()`` follows ``span_nodes``, which is what
the parent module pins for every skip seam; the watchdog seam (t_4ecbd9d0) makes
the same pin, and a test here reddens if ``covering()`` is ever swapped for
``paused()``.

WHAT IS DELIBERATELY UNCHANGED

``suspended()`` does NOT ask the overlay. It reports the breaker's own timeout,
and a node already in timeout when it got parked stays in timeout: making it
answer 0 for a paused node would hand a repair path that never consults the
overlay a green light ON a parked node — over-suppression, the same class of bug
as the false page it would remove. Only ACCRUAL is suppressed, so a pause can
neither trip the breaker nor lengthen a suspension (suppressed ticks write
nothing, so the oldest strike — what the expiry is computed from — never moves),
and a pause on one node never disarms the breaker for another. The fail-open
direction is inherited from ``node_pauses.load()`` (a missing or malformed
overlay reads as "no pauses", loudly WARNed): a broken overlay file can never
freeze the fleet's ability to repair itself.
"""

from __future__ import annotations

import json
import os
import time

# t_e6ee7e60: the durable node-pause overlay. Stdlib-only and it imports nothing
# from this package at module level (its daemon_ops import is lazy, inside
# _warn), so this cannot join the health<->lifecycle cycle — the same argument
# lifecycle.py makes for the watchdog seam.
from . import node_pauses

# Durable strike ledger + latch, one file, beside heal-lease.json.
HEAL_BREAKER_FILE = os.path.expanduser("~/.hscc/heal-breaker.json")

# More than this many heal ACTIONS on ONE node within the window trips it.
# NOT the incident's 3 template-recreates: the same number counts the gentle
# relaunches too, and a legitimate `cluster up` recovery can fan out across
# several units on one node in a single tick — that is recovery, not a storm.
# 6 sits above a one-time fan-out and below the incident's pace (one unit alone
# crash-looping hits 6 in ~15 min at the 180s relaunch cooldown, and the log
# shows one node taking dozens of actions in 35 minutes).
HEAL_BREAKER_MAX = int(os.environ.get("HSCC_HEAL_BREAKER_MAX", "6"))

# Rolling window for counting actions.
HEAL_BREAKER_WINDOW_S = float(os.environ.get("HSCC_HEAL_BREAKER_WINDOW_S", "900"))

# After latching, how long to keep refusing heals. The window itself is the
# suspension: once the strike records age out, healing resumes on its own —
# "relentless, not infinite", matching lifecycle.WATCHDOG_BACKOFF_MINUTES.
HEAL_BREAKER_SUSPEND_S = float(os.environ.get("HSCC_HEAL_BREAKER_SUSPEND_S", "900"))

# Repeat-suppression for the operator alert (see module docstring).
HEAL_BREAKER_NOTIFY_COOLDOWN_S = float(
    os.environ.get("HSCC_HEAL_BREAKER_NOTIFY_COOLDOWN_S", "1800"))


def _now():
    return time.time()


def _load():
    try:
        with open(HEAL_BREAKER_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    try:
        os.makedirs(os.path.dirname(HEAL_BREAKER_FILE) or ".", exist_ok=True)
    except OSError:
        pass
    tmp = HEAL_BREAKER_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, sort_keys=True)
        os.replace(tmp, HEAL_BREAKER_FILE)
    except OSError:
        pass


def _recent(strikes, now, window):
    """Strike timestamps still inside the window (a corrupt entry drops the
    whole record rather than letting a bad timestamp pin a node shut)."""
    out = []
    for s in strikes or []:
        try:
            t = float(s)
        except (TypeError, ValueError):
            return []
        if now - t <= window:
            out.append(t)
    return out


def _pausing(key, directory=None):
    """The pause entry that parks ``key``, or ``None`` when nothing does.

    ``covering`` (this node, or any span it belongs to), never ``paused`` —
    see the module docstring; the difference is only visible on the sibling
    nodes of a tensor-parallel span, and those are exactly the nodes a
    host-keyed check would go on striking.

    ``directory`` is ``node_pauses``' own overlay-root seam, and the reason the
    root is a parameter rather than an env var: the CLI writes the overlay and
    the daemon loop reads it, so a threaded root is one root both processes can
    agree on. ``None`` is the default ``~/.hscc``, which is what the daemon's
    callers pass.

    No try/except on purpose: ``node_pauses``' read path is documented never to
    raise (a missing, unreadable or malformed overlay reads as "no pauses" and
    WARNs), and this seam inherits that fail-open direction instead of
    re-deciding it. A guard that could raise into ``check_workers`` would be a
    worse failure mode than the pause it is looking for.
    """
    return node_pauses.covering(key, directory=directory)


def note_heal(node, now=None, window=None, directory=None):
    """Record one heal ACTION against `node`. Returns a dict:

        {"tripped": bool, "count": int, "suspended_s": float,
         "paused": bool, "paused_reason": str}

    `tripped` is True when this action crossed the threshold, i.e. the caller
    must stop healing this node and raise the operator alert. The threshold is
    read from HEAL_BREAKER_MAX at call time (never passed in), so there is one
    source of truth and tests override it by monkeypatching the constant.

    A node the pause overlay is holding accrues NOTHING: no strike, no ledger
    write, `tripped` False (module docstring — a pause is not an outage). The
    two `paused` keys are always present so the caller can log *why* the tick
    was free without reading the overlay itself. `count` on that path is the
    existing in-window slate, unchanged, so the caller's log line stays truthful.

    `directory` relocates only the OVERLAY lookup (the test seam); the ledger
    stays at HEAL_BREAKER_FILE, which is the file the daemon restart reads back.
    """
    now = _now() if now is None else now
    window = HEAL_BREAKER_WINDOW_S if window is None else float(window)
    key = str(node or "?")
    pause = _pausing(key, directory=directory)
    if pause is not None:
        # Deliberately no `_save`: the ledger is the durable memory a daemon
        # restart depends on, and a parked node must not leave a write on every
        # suppressed tick (its own test asserts the file is never created).
        return {"tripped": False,
                "count": len(_recent(_load().get(key), now, window)),
                "suspended_s": 0.0,
                "paused": True,
                "paused_reason": (pause.get("reason") or "")}
    data = _load()
    strikes = _recent(data.get(key), now, window)
    strikes.append(now)
    data[key] = strikes
    _save(data)
    over = len(strikes) > HEAL_BREAKER_MAX
    return {"tripped": over, "count": len(strikes),
            "suspended_s": HEAL_BREAKER_SUSPEND_S if over else 0.0,
            "paused": False, "paused_reason": ""}


def _suspended_from(strikes, now, window):
    """Seconds left of suspension for an already-windowed strike list.

    The ONE suspension arithmetic: ``suspended()`` and ``snapshot()`` both call
    it, so the CLI, the agent tool and the heal paths cannot disagree about
    when a node resumes.
    """
    if len(strikes) <= HEAL_BREAKER_MAX:
        return 0.0
    # Oldest surviving strike defines when the count drops back under the cap.
    return max(0.0, (min(strikes) + window) - now)


def suspended(node, now=None, window=None):
    """Seconds left of suspension for `node`, or 0.0 when healing is allowed.

    Self-clearing by construction: strikes age out of the window, so this
    returns to 0 without any operator action. It never needs a "reset" to avoid
    wedging a node forever — which is the failure mode a latch without an
    expiry would give us.
    """
    now = _now() if now is None else now
    window = HEAL_BREAKER_WINDOW_S if window is None else float(window)
    strikes = _recent(_load().get(str(node or "?")), now, window)
    return _suspended_from(strikes, now, window)


def snapshot(now=None, window=None):
    """Read-only, machine-readable view of the strike ledger.

    This is the ONE state read behind both ``hscc heal-status`` (prose) and the
    ``heal_status`` agent tool (JSON): same file, same rolling window, same cap
    and — through :func:`_suspended_from` — the same suspension arithmetic. It
    never writes the ledger, never resets an alert and never consults the
    pause overlay; clearing strikes (:func:`clear`) is deliberately not part of
    this surface.

    Every real node key gets one row. ``strikes`` counts the ones still inside
    the window (what the breaker will act on), ``recorded`` keeps the raw list
    length because the file prunes only on the next strike, and the ages are
    seconds since each surviving strike. ``file_age_s`` is how long ago the
    ledger was written (``None`` when absent), so a reader can tell "no strikes
    recorded" from "this host has no ledger yet".
    """
    now = _now() if now is None else now
    window = HEAL_BREAKER_WINDOW_S if window is None else float(window)
    data = _load()
    nodes = []
    for key in sorted(data):
        raw = data.get(key)
        if key.startswith("_") or not isinstance(raw, list):
            continue
        strikes = _recent(raw, now, window)
        suspended_s = _suspended_from(strikes, now, window)
        ages = sorted(now - t for t in strikes)
        nodes.append({
            "node": key,
            "strikes": len(strikes),
            "recorded": len(raw),
            "strike_ages_s": ages,
            "oldest_age_s": ages[-1] if ages else None,
            "newest_age_s": ages[0] if ages else None,
            "suspended": suspended_s > 0.0,
            "suspended_s": suspended_s,
        })
    try:
        file_age_s = max(0.0, now - os.stat(HEAL_BREAKER_FILE).st_mtime)
    except OSError:
        file_age_s = None
    return {
        "file": HEAL_BREAKER_FILE,
        "window_s": window,
        "max_strikes": HEAL_BREAKER_MAX,
        "file_age_s": file_age_s,
        "count": len(nodes),
        "nodes": nodes,
    }


def strike_count(node, now=None, window=None):
    now = _now() if now is None else now
    window = HEAL_BREAKER_WINDOW_S if window is None else float(window)
    return len(_recent(_load().get(str(node or "?")), now, window))


def clear(node, reason="", now=None):
    """Operator override: drop a node's strikes immediately."""
    data = _load()
    if data.pop(str(node or "?"), None) is not None:
        data.setdefault("_cleared", {})[str(node)] = {
            "at": _now() if now is None else now, "reason": reason}
        _save(data)
        return True
    return False


def should_alert(node, now=None, cooldown=None):
    """True once per latch, at most once per `cooldown`, per node.

    Callers raise the alert when this returns True. Separate from the strike
    ledger on purpose (module docstring): suppression ticks must not re-notify.
    """
    now = _now() if now is None else now
    cooldown = (HEAL_BREAKER_NOTIFY_COOLDOWN_S if cooldown is None
                else float(cooldown))
    data = _load()
    alerts = data.get("_alerted") or {}
    last = alerts.get(str(node or "?"))
    try:
        last_ts = float(last) if last is not None else None
    except (TypeError, ValueError):
        last_ts = None
    if last_ts is not None and now - last_ts < cooldown:
        return False
    alerts[str(node or "?")] = now
    data["_alerted"] = alerts
    _save(data)
    return True


def reset_alert(node):
    """Forget the alert stamp so the NEXT latch can notify again (used when a
    node recovers and later trips afresh)."""
    data = _load()
    alerts = data.get("_alerted") or {}
    if alerts.pop(str(node or "?"), None) is not None:
        data["_alerted"] = alerts
        _save(data)
        return True
    return False
