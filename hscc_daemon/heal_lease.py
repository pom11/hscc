"""Durable single-flight lease for repair actions (F2, HEAL_CASCADE 2026-10-14).

WHY THIS EXISTS
---------------
On 2026-10-14 one wedged unit triggered FOUR uncoordinated repair paths inside
three minutes — engine-wedge recovery (+``hscc cluster up``), ``check_workers``
relaunch, the proxy relaunch, and a whole-template ``--force-recreate``. None
knew about the others. Together they destroyed every in-progress model load on
four nodes and two of them hard-hung until a manual power-cycle.

The pre-existing lock (``autodown.AUTODOWN_LOCK``) does not cover this: it is
held only by autodown teardown/wake and by engine-wedge recovery, so worker
relaunch and the proxy relaunch never see it. Its staleness rule — "a LIVE
holder is never broken, no matter its age" — is correct for a lock whose holder
is a *process*, and exactly wrong here: our holders are **threads inside one
daemon process**, so a thread that dies or leaks the lease while the daemon
lives would wedge every repair for the rest of the daemon's life.

So this is a LEASE with a bounded TTL, not a lock:

  * durable — a file under ~/.hscc, so a daemon restart does NOT reset who is
    repairing (the in-memory state that caused part of the incident does);
  * bounded — it expires after ``HEAL_LEASE_TTL_S`` whatever happened to the
    holder, so a leaked lease self-heals instead of disabling repairs forever.
    This is where it differs from ``autodown``'s lock, whose rule is "a LIVE
    holder is never broken, no matter its age" — right for a process that could
    be a long-lived teardown, wrong for a thread inside a daemon that never
    exits. The TTL is advisory and clock-based: a lease can outlive a fast
    repair if the holder is killed before ``release``, and the cost is one
    skipped 60s tick rather than a lost repair;
  * cooperative — a second mechanism that finds it held LOGS and backs off for
    that tick; it never blocks, never queues, and never kills the in-flight
    repair. Healing is a supervisory loop: skipping one tick is cheap, two
    concurrent repairs are not.
  * scope-tagged — the holder records WHICH mechanism and WHICH node/unit, so
    the log says what is actually in flight ("cluster up for 10.0.0.x"), and
    the F5 circuit breaker can count actions per node from the same record.

Deliberately NOT a threading.Lock / multiprocessing lock: the four actors are
threads and subprocesses started from different modules, and the point of the
exercise is that the record survives the process that wrote it.
"""

from __future__ import annotations

import json
import os
import time

# Lease file. Same directory as every other durable daemon artefact
# (~/.hscc/worker_relaunch.json, watchdog-block.json, autodown.lock).
HEAL_LEASE_FILE = os.path.expanduser("~/.hscc/heal-lease.json")

# How long a lease is honoured without its holder renewing or releasing.
# Must exceed the SLOWEST legitimate repair so a healthy in-flight repair is
# never mistaken for a leak — `sparkrun proxy start` runs a full SSH endpoint
# discovery sweep and is given 90s by its caller; a template apply recreating
# four units is slower still. The window is generous on purpose: the cost of
# expiring early is back to two repairs at once (the incident), the cost of
# expiring late is one skipped 60s tick.
HEAL_LEASE_TTL_S = float(os.environ.get("HSCC_HEAL_LEASE_TTL_S", "600"))


def _now():
    return time.time()


def read_lease(now=None):
    """Return the lease record if one is CURRENTLY held, else None.

    Expired leases read as absent (and are left on disk for the next writer to
    overwrite) — callers must never treat an expired lease as held.
    """
    now = _now() if now is None else now
    try:
        with open(HEAL_LEASE_FILE) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    try:
        until = float(rec.get("until", 0.0))
    except (TypeError, ValueError):
        return None
    if until <= now:
        return None
    return rec


def held_by(who=None, now=None):
    """True if the lease is currently held (optionally only by `who`).

    `who` lets a mechanism re-enter for the action it already owns without
    being blocked by its own lease.
    """
    rec = read_lease(now)
    if rec is None:
        return False
    if who is not None and rec.get("who") == who:
        return True
    return who is None


def acquire(who, subject, ttl=None, now=None):
    """Try to take the repair lease. Returns (acquired, holder_record).

    On success the caller OWNS the lease and must call ``release`` in a
    ``finally`` — same discipline as autodown's lock, whose release-on-every-
    exit-path rule this follows. On failure, ``holder_record`` is whatever is
    in flight (for the log), and the caller must back off this tick.

    Race-safe by construction: written with O_CREAT|O_EXCL, so two acquirers
    in the same instant cannot both win. A loser re-reads; if the winner's
    record is there it backs off, which is the whole point.
    """
    now = _now() if now is None else now
    ttl = HEAL_LEASE_TTL_S if ttl is None else float(ttl)
    try:
        os.makedirs(os.path.dirname(HEAL_LEASE_FILE) or ".", exist_ok=True)
    except OSError:
        pass
    rec = {"who": who, "subject": subject,
           "acquired": now, "until": now + ttl,
           "pid": os.getpid()}
    payload = json.dumps(rec, sort_keys=True).encode()
    for _ in range(2):
        try:
            fd = os.open(HEAL_LEASE_FILE,
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            current = read_lease(now)
            if current is not None:
                if who is not None and current.get("who") == who:
                    # Own lease still live (a previous step of the same repair):
                    # refresh it rather than treating ourselves as a blocker.
                    _write(rec, payload)
                    return True, rec
                return False, current
            # Present but EXPIRED — a leaked lease must not disable repairs.
            try:
                os.unlink(HEAL_LEASE_FILE)
            except OSError:
                return False, None
            continue
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True, rec
    return False, read_lease(now)


def _write(rec, payload=None):
    """Best-effort overwrite (used to refresh our own lease)."""
    payload = payload if payload is not None else json.dumps(
        rec, sort_keys=True).encode()
    tmp = HEAL_LEASE_FILE + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
        os.replace(tmp, HEAL_LEASE_FILE)
    except OSError:
        pass


def renew(ttl=None, now=None):
    """Extend a lease we hold (a long repair should keep its claim)."""
    now = _now() if now is None else now
    ttl = HEAL_LEASE_TTL_S if ttl is None else float(ttl)
    rec = read_lease(now)
    if rec is None:
        return False
    rec["until"] = now + ttl
    _write(rec)
    return True


def release(who=None, now=None):
    """Drop the lease. If `who` is given, only drop it when WE hold it, so a
    late ``finally`` can never release someone else's fresh lease. Never
    raises — mirrors ``autodown._release_lock``."""
    now = _now() if now is None else now
    try:
        with open(HEAL_LEASE_FILE) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return
    if who is not None and isinstance(rec, dict) and rec.get("who") != who:
        return
    try:
        os.unlink(HEAL_LEASE_FILE)
    except OSError:
        pass


def describe(rec):
    """One-line human description for a back-off log."""
    if not rec:
        return "another repair"
    return "{} ({})".format(rec.get("who") or "?", rec.get("subject") or "?")
