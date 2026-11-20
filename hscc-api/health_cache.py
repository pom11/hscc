"""Cached verify snapshot behind ``GET /v1/health`` (t_7babf634).

Why this module exists
----------------------
``handle_health`` used to call ``verify.run_all()`` on the request thread, so
every health poll ran the whole fleet smoke test — real chat round-trips to
every serving GPU unit included — and the server is a ``ThreadingHTTPServer``
with no cache, no dedupe and no single-flight: N concurrent clients each
started their own 64-second verify. ``run_all`` also includes
``check_api_routes``, whose sweep curls ``/v1/health`` and ``/v1/verify``, so
one verify started two more inside the API.

The posture this module implements
----------------------------------
* ``GET /v1/health``  = the LAST cached verify snapshot (cheap, pollable).
* ``GET /v1/verify``  = run the fleet check NOW (the explicit trigger) and
  ``record()`` the result here, so the two routes cannot drift.
* TTL, stale-while-revalidate: fresh → serve immediately; stale → serve the
  stale verdict immediately *and* kick ONE background refresh; nothing cached
  anywhere (first poll of a fresh process, snapshot file absent or
  unsalvageable) → run **one** inline verify and cache it. Concurrent callers
  wait for that one verify instead of starting their own.
* **Never invent a verdict.** A verify that raises or returns something that is
  not a verdict stores nothing; the caller gets ``None`` and the health route
  says so. A failed refresh never overwrites a good snapshot.

The cache lives here, in hscc-api, **not** in the daemon. Publishing a verify
verdict into the daemon's periodic-streams dir would feed the verdict back into
``verify.check_daemon_streams`` — which requires every stream there to be a
fresh ``ok`` stream — the same reasoning recorded in ``api_server.py`` next to
the activity-marker path.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# A snapshot younger than this is served as-is; older than this is served as
# stale AND revalidated in the background. 120 s is the card's number: the iOS
# ConnectionMonitor polls /v1/health every 15 s, so a fleet verify costs at
# most one run per window instead of one run per poll.
TTL_SECONDS = 120

# How long a caller may wait for the single in-flight inline verify before it
# gives up and reports "no verdict". A normal verify is ~60 s; this bound only
# fires when a verify has wedged, and it exists so one wedged verify cannot
# tie up every health-poll thread in the process forever.
INLINE_WAIT_SECONDS = 180.0

SNAPSHOT_FILE = "health_snapshot.json"

# Guards _CACHE / _IN_FLIGHT and is the Condition waiters sleep on while a
# single verify is in flight.
_COND = threading.Condition()
_CACHE: "dict | None" = None
_IN_FLIGHT = False


# --------------------------------------------------------------------------- #
# Paths and shapes (pure helpers)
# --------------------------------------------------------------------------- #

def snapshot_path() -> str:
    """``~/.hscc/health_snapshot.json``, resolved at CALL time.

    Resolved per call (never baked into a module constant) so a relocated HOME
    or a test fixture that redirects ``~/.hscc`` takes effect without a reload —
    the convention the rest of hscc-api uses for ``~/.hscc`` paths.
    """
    return os.path.join(os.path.expanduser("~/.hscc"), SNAPSHOT_FILE)


def _coerce_epoch(value) -> "int | None":
    """A sane non-negative epoch int, or None. Booleans are not timestamps."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value if value >= 0 else None


def _normalise(result, generated_at=None) -> "dict | None":
    """The stored snapshot for a verify result, or None when there is no verdict.

    A verify result is a verdict only when it carries a *bool* ``ok`` and a
    list of ``checks``. Anything else — None, an error dict, a result whose
    verdict is still ``None`` — is not a verdict and must not be cached,
    because serving a made-up verdict is worse than serving no verdict.
    """
    if not isinstance(result, dict):
        return None
    ok = result.get("ok")
    if not isinstance(ok, bool):
        return None
    checks = result.get("checks")
    if not isinstance(checks, list):
        return None
    unverified = result.get("unverified")
    if not isinstance(unverified, list):
        unverified = []
    stamp = _coerce_epoch(generated_at)
    if stamp is None:
        stamp = int(time.time())
    return {
        "generated_at": stamp,
        "result": {"ok": ok, "checks": checks, "unverified": unverified},
    }


def _age_seconds(generated_at: int) -> int:
    """Age of a snapshot, clamped at 0 so a future-dated stamp reads fresh."""
    return max(0, int(time.time()) - int(generated_at))


def _view(snap: dict, source: str) -> dict:
    """The caller-facing shape: the verdict, how old it is, where it came from."""
    age = _age_seconds(snap["generated_at"])
    return {
        "result": snap["result"],
        "generated_at": snap["generated_at"],
        "age_seconds": age,
        "stale": age > TTL_SECONDS,
        "source": source,
    }


# --------------------------------------------------------------------------- #
# Disk persistence (atomic, best-effort)
# --------------------------------------------------------------------------- #

def _read_disk() -> "dict | None":
    """The on-disk snapshot, or None when absent/unparseable/not a verdict."""
    try:
        raw = json.loads(Path(snapshot_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return _normalise(raw.get("result"), generated_at=raw.get("generated_at"))


def _write_disk(snap: dict) -> bool:
    """Write atomically (tmp file + ``os.replace``). Best-effort: False on failure.

    A snapshot dir that cannot be written (read-only volume, permissions) is
    not a health failure — the in-process copy still serves, and the next
    ``record()`` retries.
    """
    path = Path(snapshot_path())
    tmp = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


# --------------------------------------------------------------------------- #
# The verify seam
# --------------------------------------------------------------------------- #

def _default_verify():
    """Run the real fleet check. Callers inject their own for tests."""
    from hscc_daemon import verify
    return verify.run_all()


def _safe_verify(runner):
    """``runner()`` with any exception swallowed into "no verdict"."""
    try:
        return runner()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Single-flight refresh
# --------------------------------------------------------------------------- #

def _store_snapshot_locked(snap: dict) -> dict:
    """Cache + persist an already-normalised snapshot. Caller holds _COND."""
    global _CACHE
    _write_disk(snap)
    _CACHE = snap
    return snap


def _store_result_locked(result) -> "dict | None":
    """Normalise then store ``result``. Caller holds _COND. None if no verdict."""
    snap = _normalise(result)
    if snap is None:
        return None
    return _store_snapshot_locked(snap)


def _spawn_refresh_locked(runner) -> bool:
    """Start one background verify. Caller holds _COND. False if one is running."""
    global _IN_FLIGHT
    if _IN_FLIGHT:
        return False
    _IN_FLIGHT = True
    try:
        threading.Thread(
            target=_refresh_worker, args=(runner,),
            name="hscc-health-refresh", daemon=True).start()
    except (OSError, RuntimeError):
        # No thread available (thread limit, fork pressure): never leave the
        # in-flight flag set, or every waiter would block until its timeout and
        # the cache would never revalidate again.
        _IN_FLIGHT = False
        _COND.notify_all()
        return False
    return True


def _refresh_worker(runner) -> None:
    global _IN_FLIGHT
    result = _safe_verify(runner)
    with _COND:
        _IN_FLIGHT = False
        _store_result_locked(result)
        _COND.notify_all()


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #

def get_snapshot(verify=None) -> "dict | None":
    """The freshest available verify verdict, running at most ONE verify.

    ``verify`` is the verify seam (default ``hscc_daemon.verify.run_all``);
    handlers pass their own ``_backing_verify`` so tests can substitute a fake.

    Returns ``{"result": {...}, "generated_at": int, "age_seconds": int,
    "stale": bool, "source": "cache" | "inline"}``, or ``None`` when no verdict
    exists and none could be produced — the caller must then say it has no
    health, never guess one.

    Three paths, and only the third may run a verify on the calling thread:
      1. fresh snapshot  → return it, verify untouched;
      2. stale snapshot  → return it now and kick the single background refresh;
      3. nothing anywhere → one inline verify; every other caller waits for it.
    """
    global _CACHE, _IN_FLIGHT
    runner = verify or _default_verify
    with _COND:
        if _CACHE is None:
            _CACHE = _read_disk()
        while True:
            if _CACHE is not None:
                if _age_seconds(_CACHE["generated_at"]) > TTL_SECONDS:
                    _spawn_refresh_locked(runner)
                return _view(_CACHE, "cache")
            if _IN_FLIGHT:
                if not _COND.wait(INLINE_WAIT_SECONDS):
                    return None      # wedged verify: no verdict, say so
                continue
            _IN_FLIGHT = True
            break

    # Path 3 — the only place a request thread runs a verify. Exactly one
    # thread gets here; the rest waited on _COND above and share its result.
    result = None
    snap = None
    try:
        result = _safe_verify(runner)
    finally:
        with _COND:
            _IN_FLIGHT = False
            snap = _store_result_locked(result)
            _COND.notify_all()
    return _view(snap, "inline") if snap is not None else None


def request_refresh(verify=None) -> bool:
    """Kick one background verify now. False when one is already in flight."""
    with _COND:
        return _spawn_refresh_locked(verify or _default_verify)


def record(result, generated_at=None):
    """Publish ``result`` (a verify result) as the current snapshot.

    ``GET /v1/verify`` calls this so an explicit verify also refreshes the
    health screen. Returns the stored snapshot, or ``None`` when ``result``
    carries no verdict — a malformed or failed verify never replaces a good
    snapshot. Never raises.
    """
    snap = _normalise(result, generated_at=generated_at)
    if snap is None:
        return None
    with _COND:
        return _store_snapshot_locked(snap)


def reset_state() -> None:
    """Forget the in-process copy (the on-disk snapshot is left alone).

    The cache is process-global, so a test suite that exercises the health
    route must start each test from an empty in-process copy — otherwise one
    test's verdict leaks into the next and the assertions prove nothing.
    """
    global _CACHE, _IN_FLIGHT
    with _COND:
        _CACHE = None
        _IN_FLIGHT = False
