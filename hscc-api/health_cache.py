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
* **One attempt per window, verdict or not** (t_a09ce4ed). When there is nothing
  to serve *and* the attempt produced no verdict either, the next attempt on the
  request thread waits ``TTL_SECONDS``. The stamp is in-process only and is
  never persisted — a failed attempt is not a verdict and must not be written as
  if it were one. During that window the health route answers the *same* honest
  shape it always has (``{"speak": "health check unavailable"}``, no ``ok``
  key): the backoff defers a probe, it never becomes an answer. See
  :func:`get_snapshot` for why it defers rather than stops.

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

# Guards _CACHE / _IN_FLIGHT / _RETRY_AFTER and is the Condition waiters sleep
# on while a single verify is in flight.
_COND = threading.Condition()
_CACHE: "dict | None" = None
_IN_FLIGHT = False

# The no-verdict retry backoff: the monotonic instant before which the "nothing
# cached anywhere" path starts NO new verify. In-process only, and deliberately
# never written to snapshot_path() — a failed attempt is not a verdict, and a
# persisted failure would come back next boot looking like one.
_RETRY_AFTER: "float | None" = None


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
# The no-verdict retry backoff (t_a09ce4ed)
# --------------------------------------------------------------------------- #

def _clock() -> float:
    """Now, for the retry window — monotonic on purpose.

    Never ``time.time()``: a backward clock step (NTP correcting a drifted
    clock — exactly what a misconfigured offline fleet looks like) would either
    stretch the silence indefinitely or erase it. Monotonic cannot go backwards,
    so the window is bounded by what we set, not by the wall clock.
    """
    return time.monotonic()


def _backoff_open_locked() -> bool:
    """True while a recent no-verdict attempt forbids starting another one.

    Caller holds ``_COND``. Consulted ONLY on the "no snapshot anywhere" path,
    so it can never hide a verdict: with a snapshot to serve, this is never
    asked. It defers a probe; it does not answer a question.
    """
    return _RETRY_AFTER is not None and _clock() < _RETRY_AFTER


def _arm_backoff_locked() -> None:
    """Record that the attempt just made produced no verdict.

    Caller holds ``_COND``. Cleared by the next snapshot that is actually stored
    (``_store_snapshot_locked``), so the window only ever covers "we have
    nothing to serve and probing did not help".
    """
    global _RETRY_AFTER
    _RETRY_AFTER = _clock() + TTL_SECONDS


# --------------------------------------------------------------------------- #
# Single-flight refresh
# --------------------------------------------------------------------------- #

def _store_snapshot_locked(snap: dict) -> dict:
    """Cache + persist an already-normalised snapshot. Caller holds _COND.

    Storing a verdict also ends any no-verdict retry window: the backoff only
    exists while there is nothing to serve.
    """
    global _CACHE, _RETRY_AFTER
    _write_disk(snap)
    _CACHE = snap
    _RETRY_AFTER = None
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

    Four paths, and only the third may run a verify on the calling thread:
      1. fresh snapshot  → return it, verify untouched;
      2. stale snapshot  → return it now and kick the single background refresh;
      3. nothing anywhere, and no attempt within the last ``TTL_SECONDS`` → one
         inline verify; every other caller waits for it;
      4. nothing anywhere, but the last attempt produced no verdict and is
         younger than ``TTL_SECONDS`` → ``None``, WITHOUT another verify.

    Why path 4 exists (t_a09ce4ed). When no verdict is obtainable at all —
    sparkrun gone, the GPU fleet powered down, the deployed ``verify.py`` broken
    — path 3 caches nothing, so every poll re-ran a whole fleet smoke test: the
    storm this module exists to remove, surviving in exactly the case where the
    operator is already troubleshooting a broken fleet. And it nests: ``hscc
    verify`` → ``check_api_routes`` → the route sweep curls ``/v1/health`` (its
    probed set comes from ``api_route_sweep.client_routes()``, which reads the
    Swift client, and the client references that path) → path 3 → a *second*
    inline verify inside a verify. A bounded retry keeps a broken fleet probed —
    one attempt per ``TTL_SECONDS`` window, so the fix is noticed within one
    window of it happening — while removing the per-poll storm. The alternative
    considered and rejected was "no verdict means keep probing on every poll";
    what it buys is a probe every 15 s that cannot succeed, and what it costs is
    a full verify each time.

    Path 4 must never become a served verdict: it returns ``None``, and the
    health route answers ``{"speak": "health check unavailable"}`` with no
    ``ok`` key — byte-identical to what a failed attempt always answered.
    ``GET /v1/verify`` is not gated by it either: the explicit trigger runs a
    verify every single time, window open or not.
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
            if _backoff_open_locked():
                return None          # probed recently, no verdict: say so again
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
            if snap is None:
                # No verdict: remember the attempt so the next poll does not
                # spend another verify on the same broken fleet. A store that
                # did succeed already cleared the window (see
                # _store_snapshot_locked).
                _arm_backoff_locked()
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
    """Forget the in-process copy and the retry window (the snapshot file is
    left alone).

    The cache is process-global, so a test suite that exercises the health
    route must start each test from an empty in-process copy — otherwise one
    test's verdict leaks into the next and the assertions prove nothing. The
    retry window is cleared for the same reason: a test that armed it by
    failing must not leave the next test unable to probe.
    """
    global _CACHE, _IN_FLIGHT, _RETRY_AFTER
    with _COND:
        _CACHE = None
        _IN_FLIGHT = False
        _RETRY_AFTER = None
