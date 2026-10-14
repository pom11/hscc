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
"""

from __future__ import annotations

import json
import os
import time

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


def note_heal(node, now=None, window=None):
    """Record one heal ACTION against `node`. Returns a dict:

        {"tripped": bool, "count": int, "suspended_s": float}

    `tripped` is True when this action crossed the threshold, i.e. the caller
    must stop healing this node and raise the operator alert. The threshold is
    read from HEAL_BREAKER_MAX at call time (never passed in), so there is one
    source of truth and tests override it by monkeypatching the constant.
    """
    now = _now() if now is None else now
    window = HEAL_BREAKER_WINDOW_S if window is None else float(window)
    data = _load()
    key = str(node or "?")
    strikes = _recent(data.get(key), now, window)
    strikes.append(now)
    data[key] = strikes
    _save(data)
    over = len(strikes) > HEAL_BREAKER_MAX
    return {"tripped": over, "count": len(strikes),
            "suspended_s": HEAL_BREAKER_SUSPEND_S if over else 0.0}


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
    if len(strikes) <= HEAL_BREAKER_MAX:
        return 0.0
    # Oldest surviving strike defines when the count drops back under the cap.
    return max(0.0, (min(strikes) + window) - now)


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
