"""Persistent alert feed — the store behind ``hscc notify`` / ``/v1/alerts``.

The main orchestrator (design: ``docs/design/2026-11-19-main-orchestrator-design.md``)
must reach the operator when something needs a human: a LIVE publish waiting
for approval, a break it could not fix, or a release it shipped. The transport
is push (the hscc iOS/macOS apps), but the SOURCE OF TRUTH is one small feed
on disk that every surface shares:

    hscc notify "<title>" "<body>" [--level info|action|critical] [--key K]
        -> POST /v1/alerts -> this module -> ~/.hscc/alerts.json
    GET  /v1/alerts?since=<id>  -> the apps poll the same file
    POST /v1/alerts/<id>/ack    -> mark seen and clear its dedupe slot

WHY A FILE AND NOT AN IN-MEMORY QUEUE
=====================================
The API server is its own process (``hscc_daemon/api_launchd.py`` starts
``python -m api_server serve``) and ``hscc notify`` is a third one. The feed
must survive an API restart and be visible to the daemon that will send push,
so it is a bounded JSON document written atomically under ``~/.hscc``. The
file is deliberately small (the last ``MAX_ALERTS`` entries): this is a feed
for a phone lock screen, not an audit log.

THE THREE RULES
===============
1. **Dedupe.** A keyed alert is one CONDITION, not one call. Posting the same
   ``key`` with the same title/body/level while its alert is still in the feed
   and not acknowledged does not create a second alert — the caller gets the
   existing one back with ``deduped: true``. New content on the same key is a
   changed condition and alerts again (subject to rule 2). Acknowledging an
   alert clears its key's slot, so a condition that recurs after the operator
   has seen it can alert again — that is the "or it is cleared" half.
2. **Rate limit.** Per key and globally. A key may emit at most once per
   ``PER_KEY_MIN_INTERVAL_S``; the whole feed may emit at most
   ``GLOBAL_MAX_PER_WINDOW`` in ``GLOBAL_WINDOW_S``. Suppression is not an
   error: the caller gets ``rate_limited: true`` and ``retry_after_s`` so a
   looping producer cannot turn into notification spam (an anti-goal in the
   design) and does not need to treat "quiet" as failure.
3. **Honest ordering.** Ids are monotonic integers, never reused, persisted
   across restarts. ``GET ?since=<id>`` is the apps' paging read: strictly
   greater ids, ascending, bounded by ``limit``.

``None`` IS NOT A VALUE. An alert that was never acknowledged carries
``acked: false`` and ``acked_at: null``; clients must not read that as "an
empty timestamp". The bounded store evicts the oldest alerts by id, period: the
feed has one order, and pretending an old unacknowledged alert is newer than a
recent one would be a lie.

PUSH IS A SEAM, NOT THIS CARD. ``hscc_daemon/alert_push.py`` is the APNs
sender (card N2). This module calls it once per CREATED alert, after the file is
safely written, and records its result on the response. A sender failure never
undoes or hides the alert.

The store is safe against two writers (CLI + API) on one host: mutations take
an exclusive ``fcntl`` lock on ``alerts.lock`` and replace the JSON atomically.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

try:  # POSIX only; the daemon targets macOS/Linux, but degrade honestly.
    import fcntl
except ImportError:  # pragma: no cover - Windows import path
    fcntl = None

log = logging.getLogger("hscc.alerts")

#: File names inside the hscc root.
ALERTS_FILE = "alerts.json"
ALERTS_LOCK_FILE = "alerts.lock"

#: Bounded feed. Oldest entries are dropped first; ids are never reused.
MAX_ALERTS = 500

#: Per-field caps. The API body cap is 1 MiB, but a phone notification is not
#: a document: refusing a giant title/body is kinder than silently truncating
#: one (a truncated alert can change its meaning).
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 4000
MAX_KEY_CHARS = 200

#: Levels the apps know how to render. `action` is "a human decision is
#: needed", `critical` is "something is broken now". Closed set — an unknown
#: level would reach a client that has no bucket for it.
LEVELS = ("info", "action", "critical")
DEFAULT_LEVEL = "info"

#: Rate limits (rule 2). Kept module-level so tests can pin a clock and so an
#: operator-facing doc can quote one number.
PER_KEY_MIN_INTERVAL_S = 60.0
GLOBAL_WINDOW_S = 60.0
GLOBAL_MAX_PER_WINDOW = 30

#: How many per-key rate stamps to retain (a long-lived daemon sees many
#: distinct keys over days; the stamps are tiny but not unbounded).
RATE_ENTRIES_MAX = 1000

#: The in-process half of the lock. The file lock handles CLI-vs-API; this
#: handles two threads inside one process (the API serves requests on threads).
_THREAD_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Root / paths
# --------------------------------------------------------------------------- #

def hscc_root(hscc_dir=None) -> Path:
    """The hscc state root: explicit arg, then ``$HSCC_HOME``, then ``~/.hscc``.

    Resolved at CALL time, not import time, so a temp ``HSCC_HOME`` (the test
    harness) and an explicit ``ctx.hscc_dir`` (the API) both work without a
    prefix import dance. Production passes neither and lands on ``~/.hscc``.
    """
    if hscc_dir:
        return Path(os.path.expanduser(str(hscc_dir)))
    env = os.environ.get("HSCC_HOME", "").strip()
    return Path(os.path.expanduser(env or "~/.hscc"))


def store_path(hscc_dir=None) -> Path:
    """The alert feed document (``<root>/alerts.json``)."""
    return hscc_root(hscc_dir) / ALERTS_FILE


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #

def _now() -> float:
    return time.time()


def _iso(epoch: float) -> str:
    """Second-resolution UTC in the house ``...Z`` form."""
    dt = datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _signature(title: str, body: str, level: str) -> str:
    """What "its content changed" means, byte for byte.

    The key identifies the CONDITION; this digest identifies the MESSAGE. A
    title/body/level edit is a change worth re-alerting for; a timestamp the
    producer appended to an otherwise identical body is not our business to
    normalise (a caller that wants stable content should send stable content).
    """
    blob = json.dumps([title, body, level], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _clean_text(value, name: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")
    return value


def normalise_level(level) -> str:
    """Validate/lower-case a level; ``None`` means the default."""
    if level is None or (isinstance(level, str) and not level.strip()):
        return DEFAULT_LEVEL
    if not isinstance(level, str):
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    level = level.strip().lower()
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    return level


def _normalise_key(key):
    if key is None:
        return None
    key = _clean_text(key, "key", MAX_KEY_CHARS)
    return key or None


def _empty_state() -> dict:
    return {"version": 1, "next_id": 1, "alerts": [], "dedupe": {}, "rate": {}}


def _load(path: Path) -> dict:
    """Read the feed; a missing/corrupt file is an EMPTY feed, never a crash.

    A corrupt document has already lost its usefulness; refusing to alert (or
    refusing to serve GET) because of it would turn one broken file into a
    silent notification outage. The next successful write replaces it, and the
    warning names the path so the operator can look.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError) as exc:
        log.warning("alerts: cannot read %s (%r); treating as empty", path, exc)
        return _empty_state()
    if not isinstance(raw, dict):
        log.warning("alerts: %s is not a JSON object; treating as empty", path)
        return _empty_state()

    state = _empty_state()
    try:
        state["next_id"] = max(1, int(raw.get("next_id", 1)))
    except (TypeError, ValueError):
        state["next_id"] = 1

    alerts = raw.get("alerts")
    if isinstance(alerts, list):
        state["alerts"] = [a for a in alerts if isinstance(a, dict)][-MAX_ALERTS:]
    dedupe = raw.get("dedupe")
    if isinstance(dedupe, dict):
        state["dedupe"] = {
            k: v for k, v in dedupe.items()
            if isinstance(k, str) and isinstance(v, dict)
        }
    rate = raw.get("rate")
    if isinstance(rate, dict):
        state["rate"] = {
            k: v for k, v in rate.items()
            if isinstance(k, str) and isinstance(v, (int, float))
        }
    return state


def _save(path: Path, state: dict) -> None:
    """Write the feed atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


class _store_lock:
    """Exclusive CLI-vs-API lock around one read-modify-write.

    ``fcntl.flock`` is advisory and per-open-file; the API and CLI are separate
    processes on the same host, which is exactly the case this covers. The
    in-process thread lock is acquired first so two request threads cannot
    interleave before either reaches the kernel lock.
    """

    def __init__(self, root: Path):
        self._path = root / ALERTS_LOCK_FILE
        self._fh = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _THREAD_LOCK.acquire()
        try:
            self._fh = open(self._path, "a+")
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except BaseException:
            _THREAD_LOCK.release()
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fh is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                finally:
                    self._fh.close()
                    self._fh = None
        finally:
            _THREAD_LOCK.release()
        return False


def _prune(state: dict, now: float) -> None:
    """Bound every map in the document (rule: a feed, never a log)."""
    alerts = state.get("alerts") or []
    if len(alerts) > MAX_ALERTS:
        del alerts[: len(alerts) - MAX_ALERTS]

    live_ids = {a.get("id") for a in alerts if isinstance(a.get("id"), int)}
    dedupe = state.get("dedupe") or {}
    for key in list(dedupe):
        entry = dedupe.get(key) or {}
        if entry.get("alert_id") not in live_ids:
            dedupe.pop(key, None)

    rate = state.get("rate") or {}
    cutoff = now - max(3600.0, PER_KEY_MIN_INTERVAL_S * 10)
    for key in list(rate):
        try:
            if float(rate[key]) < cutoff:
                rate.pop(key, None)
        except (TypeError, ValueError):
            rate.pop(key, None)
    if len(rate) > RATE_ENTRIES_MAX:
        newest = sorted(rate.items(), key=lambda kv: float(kv[1]),
                        reverse=True)[:RATE_ENTRIES_MAX]
        state["rate"] = dict(newest)
    else:
        state["rate"] = rate
    state["dedupe"] = dedupe
    state["alerts"] = alerts


def _find_alert(state: dict, alert_id):
    for alert in state.get("alerts") or []:
        if alert.get("id") == alert_id:
            return alert
    return None


def _public(alert):
    """The wire shape. Internal fields (epoch) are not part of the contract."""
    if not isinstance(alert, dict):
        return None
    return {
        "id": alert.get("id"),
        "ts": alert.get("ts"),
        "title": alert.get("title"),
        "body": alert.get("body"),
        "level": alert.get("level", DEFAULT_LEVEL),
        "key": alert.get("key"),
        "acked": bool(alert.get("acked")),
        "acked_at": alert.get("acked_at"),
    }


def _global_retry_after(state: dict, now: float):
    """Seconds until the global window frees a slot, or None when it is free."""
    recent = [
        a for a in (state.get("alerts") or [])
        if isinstance(a.get("epoch"), (int, float))
        and now - float(a["epoch"]) < GLOBAL_WINDOW_S
    ]
    if len(recent) < GLOBAL_MAX_PER_WINDOW:
        return None
    oldest = min(float(a["epoch"]) for a in recent)
    return max(0.0, oldest + GLOBAL_WINDOW_S - now)


def _push(alert: dict, hscc_dir=None) -> dict:
    """Offer one created alert to the sender seam; never raise.

    A dead sender must not turn a persisted alert into a failed request — the
    operator still sees it on the next ``GET /v1/alerts``. The result is
    reported to the caller so an operator can tell "queued but no push
    transport" from "pushed". ``hscc_dir`` carries the caller's sandbox root
    into the sender so its roster/config read the same ``~/.hscc`` the alert
    was written under.
    """
    try:
        from . import alert_push
        send = alert_push.send
        # N1's documented seam was ``send(alert)``; N2 adds an optional
        # ``hscc_dir`` so a sandboxed API/test resolves its own roster. Keep a
        # single-argument hook (or an old monkeypatch) working.
        try:
            import inspect
            params = inspect.signature(send).parameters
            accepts_dir = "hscc_dir" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in params.values())
        except (TypeError, ValueError):
            accepts_dir = False
        result = (send(dict(alert), hscc_dir=hscc_dir) if accepts_dir
                  else send(dict(alert)))
    except Exception as exc:  # noqa: BLE001 - a seam must not break the feed
        log.warning("alerts: push sender failed: %r", exc)
        return {"sent": False, "reason": "sender_error"}
    if isinstance(result, dict):
        return dict(result)
    return {"sent": bool(result), "reason": "sent" if result else "no_transport"}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def post_alert(title, body="", level=None, key=None, hscc_dir=None,
               source=None, now=None) -> dict:
    """Create, dedupe or rate-limit one alert.

    Returns a dict::

        {
          "created": bool, "deduped": bool, "rate_limited": bool,
          "reason": "created"|"duplicate"|"rate_limited_key"|...,
          "alert": {...} | None,     # the existing one when deduped
          "retry_after_s": float|None,
          "push": {...}              # only on created
        }

    ``source`` is accepted for callers that later want per-source reporting;
    it is not part of the public alert shape today (the feed is one stream).
    """
    title = _clean_text(title, "title", MAX_TITLE_CHARS, required=True)
    body = _clean_text(body, "body", MAX_BODY_CHARS)
    level = normalise_level(level)
    key = _normalise_key(key)
    now = _now() if now is None else float(now)

    root = hscc_root(hscc_dir)
    path = store_path(hscc_dir)
    signature = _signature(title, body, level)

    with _store_lock(root):
        state = _load(path)
        _prune(state, now)

        if key:
            entry = state["dedupe"].get(key)
            if entry and entry.get("signature") == signature:
                existing = _find_alert(state, entry.get("alert_id"))
                if existing is not None and not existing.get("acked"):
                    return {
                        "created": False,
                        "deduped": True,
                        "rate_limited": False,
                        "reason": "duplicate",
                        "alert": _public(existing),
                        "retry_after_s": None,
                    }
                # Acknowledged (cleared) or evicted: the slot no longer blocks.
                state["dedupe"].pop(key, None)

        retry_after = None
        reason = None
        if key:
            last = state["rate"].get(key)
            if last is not None:
                remaining = PER_KEY_MIN_INTERVAL_S - (now - float(last))
                if remaining > 0:
                    retry_after = remaining
                    reason = "rate_limited_key"
        if reason is None:
            global_wait = _global_retry_after(state, now)
            if global_wait is not None:
                retry_after = global_wait
                reason = "rate_limited_global"

        if reason is not None:
            return {
                "created": False,
                "deduped": False,
                "rate_limited": True,
                "reason": reason,
                "alert": None,
                "retry_after_s": round(max(0.0, retry_after or 0.0), 3),
            }

        alert_id = int(state.get("next_id") or 1)
        state["next_id"] = alert_id + 1
        alert = {
            "id": alert_id,
            "ts": _iso(now),
            "epoch": now,
            "title": title,
            "body": body,
            "level": level,
            "key": key,
            "acked": False,
            "acked_at": None,
        }
        state["alerts"].append(alert)
        if key:
            state["dedupe"][key] = {
                "signature": signature,
                "alert_id": alert_id,
                "emitted_epoch": now,
            }
            state["rate"][key] = now
        _prune(state, now)
        _save(path, state)

    push = _push(alert, hscc_dir)
    return {
        "created": True,
        "deduped": False,
        "rate_limited": False,
        "reason": "created",
        "alert": _public(alert),
        "retry_after_s": None,
        "push": push,
    }


def list_alerts(since=None, limit=100, hscc_dir=None) -> dict:
    """The apps' paging read.

    ``since`` is exclusive and optional. Without it the feed answers with the
    most recent ``limit`` alerts (ascending), so a first launch can render a
    screenful without knowing an id. With it, only ids greater than ``since``
    are returned — still ascending — and ``has_more`` says whether the caller
    should ask again. ``latest_id`` is the newest id the STORE holds (not just
    the page), which is what a client advances its cursor from after a full
    drain.
    """
    if since is not None:
        try:
            since = int(since)
        except (TypeError, ValueError):
            raise ValueError("since must be an integer alert id")
        if since < 0:
            raise ValueError("since must be a non-negative alert id")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    limit = min(limit, MAX_ALERTS)

    state = _load(store_path(hscc_dir))
    alerts = sorted(
        (a for a in (state.get("alerts") or []) if isinstance(a.get("id"), int)),
        key=lambda a: a["id"],
    )
    if since is None:
        page = alerts[-limit:] if alerts else []
        has_more = len(alerts) > len(page)
    else:
        matched = [a for a in alerts if a["id"] > since]
        page = matched[:limit]
        has_more = len(matched) > len(page)

    cursor = page[-1]["id"] if page else since
    return {
        "alerts": [_public(a) for a in page],
        "count": len(page),
        "since": since,
        "limit": limit,
        "oldest_id": page[0]["id"] if page else None,
        "latest_id": alerts[-1]["id"] if alerts else None,
        "next_since": cursor,
        "has_more": has_more,
    }


def ack_alert(alert_id, hscc_dir=None, now=None):
    """Mark one alert acknowledged and clear its dedupe slot.

    Returns the updated public alert, or ``None`` when the id is not in the
    bounded feed (404 at the route). Idempotent: a second ack keeps the first
    ``acked_at`` so the timestamp means "when the operator saw it", not "when
    the app last retried".
    """
    try:
        alert_id = int(alert_id)
    except (TypeError, ValueError):
        raise ValueError("alert id must be an integer")
    now = _now() if now is None else float(now)
    root = hscc_root(hscc_dir)
    path = store_path(hscc_dir)

    with _store_lock(root):
        state = _load(path)
        alert = _find_alert(state, alert_id)
        if alert is None:
            return None
        if not alert.get("acked"):
            alert["acked"] = True
            alert["acked_at"] = _iso(now)
        for key, entry in list(state["dedupe"].items()):
            if entry.get("alert_id") == alert_id:
                state["dedupe"].pop(key, None)
        _save(path, state)
        return _public(alert)


def clear_key(key, hscc_dir=None) -> bool:
    """Drop a key's dedupe slot explicitly ("or it is cleared").

    The route does not expose this yet; acking the alert is the operator-facing
    clear. Kept as a library primitive so a future ``hscc notify --clear`` can
    reuse the one implementation rather than editing the store shape.
    """
    key = _normalise_key(key)
    if not key:
        return False
    root = hscc_root(hscc_dir)
    path = store_path(hscc_dir)
    with _store_lock(root):
        state = _load(path)
        existed = state["dedupe"].pop(key, None) is not None
        if existed:
            _save(path, state)
    return existed
