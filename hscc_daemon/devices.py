"""Registered push devices for the alert feed (card N2).

The operator's phone and Mac register once with ``POST /v1/devices`` and then
receive every *new* alert as an APNs push (``hscc_daemon/apns.py``). This module
owns the durable roster:

    ~/.hscc/devices.json   the last MAX_DEVICES registrations
    ~/.hscc/devices.lock   CLI/API/tests cross-process mutation lock

WHY A TOKEN IS STORED BUT NEVER RETURNED
========================================
APNs delivers to a device token, so the push sender needs the raw token. It is
a credential-like identifier: the API answers with a stable derived ``id`` and
never echoes the token back, and logs name the id, not the token. Tokens are
normalised to lowercase hex so re-registration with different casing refreshes
one row instead of creating two.

REGISTRATION IS A REFRESH
=========================
``POST /v1/devices`` is idempotent per ``(platform, token)``: registering again
updates ``bundle_id`` / ``env`` / ``updated_at`` in place. That is what an app
does on every launch, and it is also how a token that moved from sandbox to
production (or a rebuilt app id) is corrected. The roster is bounded and the
oldest *updated* row is evicted first.

APNs 410/BadDeviceToken pruning is :func:`prune_tokens` — deliberately a
separate mutation so a sender can remove exactly the tokens APNs rejected,
without a read-modify-write race against a fresh registration.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

try:  # POSIX only; the daemon targets macOS/Linux, but degrade honestly.
    import fcntl
except ImportError:  # pragma: no cover - Windows import path
    fcntl = None

log = logging.getLogger("hscc.devices")

DEVICES_FILE = "devices.json"
DEVICES_LOCK_FILE = "devices.lock"

#: A phone lock screen does not need an unbounded device table. 100 is far
#: beyond one operator's devices and keeps the file tiny.
MAX_DEVICES = 100

PLATFORMS = ("ios", "macos")
ENVIRONMENTS = ("production", "development")

_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")
_BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,154}$")

_THREAD_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Root / paths
# --------------------------------------------------------------------------- #

def hscc_root(hscc_dir=None) -> Path:
    """The hscc state root: explicit arg, then ``$HSCC_HOME`` / ``$HSCC_DIR``,
    then ``~/.hscc``.

    Resolved at CALL time so the API context (an explicit ``hscc_dir``) and a
    test sandbox (``HSCC_HOME``) both work without an import-order dance.
    """
    if hscc_dir:
        return Path(os.path.expanduser(str(hscc_dir)))
    env = (os.environ.get("HSCC_HOME", "").strip()
           or os.environ.get("HSCC_DIR", "").strip())
    return Path(os.path.expanduser(env or "~/.hscc"))


def store_path(hscc_dir=None) -> Path:
    return hscc_root(hscc_dir) / DEVICES_FILE


def _now() -> float:
    return time.time()


def _iso(epoch: float) -> str:
    dt = datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _clean(value, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{name} is required")
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")
    return value


def normalise_platform(platform) -> str:
    if not isinstance(platform, str):
        raise ValueError("platform must be ios or macos")
    platform = platform.strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform must be ios or macos")
    return platform


def normalise_environment(env) -> str:
    if not isinstance(env, str):
        raise ValueError("env must be production or development")
    env = env.strip().lower()
    if env not in ENVIRONMENTS:
        raise ValueError("env must be production or development")
    return env


def normalise_token(token) -> str:
    if not isinstance(token, str):
        raise ValueError("token must be a hex string")
    token = token.strip().lower()
    if not _TOKEN_RE.match(token):
        raise ValueError(
            "token must be 32-200 hex characters (the APNs device token)"
        )
    if len(token) % 2:
        raise ValueError("token must have an even number of hex characters")
    return token


def normalise_bundle_id(bundle_id) -> str:
    bundle_id = _clean(bundle_id, "bundle_id", 155)
    if not _BUNDLE_RE.match(bundle_id):
        raise ValueError(
            "bundle_id must be a reverse-DNS app id (letters, digits, . and -)"
        )
    return bundle_id


def device_id(platform: str, token: str) -> str:
    """Stable public identifier derived from the token (never the token)."""
    blob = f"{platform}:{token}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _public(device: dict) -> dict:
    """The wire shape: no token, no key material."""
    return {
        "id": device.get("id"),
        "platform": device.get("platform"),
        "bundle_id": device.get("bundle_id"),
        "env": device.get("env"),
        "registered_at": device.get("registered_at"),
        "updated_at": device.get("updated_at"),
    }


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

def _empty_state() -> dict:
    return {"version": 1, "devices": []}


def _load(path: Path) -> dict:
    """A missing/corrupt roster is empty; the next write repairs it."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError) as exc:
        log.warning("devices: cannot read %s (%r); treating as empty", path, exc)
        return _empty_state()
    if not isinstance(raw, dict):
        log.warning("devices: %s is not a JSON object; treating as empty", path)
        return _empty_state()
    devices = raw.get("devices")
    state = _empty_state()
    if isinstance(devices, list):
        state["devices"] = [
            d for d in devices
            if isinstance(d, dict) and d.get("token") and d.get("platform")
        ][-MAX_DEVICES:]
    return state


def _save(path: Path, state: dict) -> None:
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
    """Exclusive cross-process lock around one read-modify-write."""

    def __init__(self, root: Path):
        self._path = root / DEVICES_LOCK_FILE
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


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def register(platform, token, bundle_id, env, hscc_dir=None,
             now=None) -> dict:
    """Register or refresh one device.

    Returns ``{"created": bool, "device": {...public...}, "count": n}``.
    Raises ``ValueError`` for a malformed field (the route maps it to 400).
    """
    platform = normalise_platform(platform)
    token = normalise_token(token)
    bundle_id = normalise_bundle_id(bundle_id)
    env = normalise_environment(env)
    now = _now() if now is None else float(now)

    root = hscc_root(hscc_dir)
    path = store_path(hscc_dir)
    ident = device_id(platform, token)

    with _store_lock(root):
        state = _load(path)
        devices = state["devices"]
        existing = None
        for entry in devices:
            if (entry.get("platform") == platform
                    and str(entry.get("token", "")).lower() == token):
                existing = entry
                break

        if existing is None:
            entry = {
                "id": ident,
                "platform": platform,
                "token": token,
                "bundle_id": bundle_id,
                "env": env,
                "registered_at": _iso(now),
                "updated_at": _iso(now),
            }
            devices.append(entry)
            created = True
        else:
            existing.update({
                "id": ident,
                "bundle_id": bundle_id,
                "env": env,
                "updated_at": _iso(now),
            })
            entry = existing
            created = False

        if len(devices) > MAX_DEVICES:
            devices.sort(key=lambda d: str(d.get("updated_at") or ""))
            del devices[: len(devices) - MAX_DEVICES]
        state["devices"] = devices
        _save(path, state)

    return {"created": created, "device": _public(entry),
            "count": len(devices)}


def load(hscc_dir=None) -> list:
    """All registrations (raw entries, including tokens) for the sender."""
    return list(_load(store_path(hscc_dir))["devices"])


def public_list(hscc_dir=None) -> dict:
    devices = [_public(d) for d in load(hscc_dir)]
    devices.sort(key=lambda d: str(d.get("updated_at") or ""), reverse=True)
    return {"devices": devices, "count": len(devices)}


def prune_tokens(tokens, hscc_dir=None) -> int:
    """Remove every device whose token is in ``tokens``; returns the count.

    A no-op for an empty/unreadable roster. Matching is case-insensitive and
    scoped to the raw token, so a stale registration cannot survive merely
    because the app re-registered it under another bundle id.
    """
    wanted = {str(t).strip().lower() for t in (tokens or []) if t}
    if not wanted:
        return 0
    root = hscc_root(hscc_dir)
    path = store_path(hscc_dir)
    removed = 0
    with _store_lock(root):
        state = _load(path)
        kept = []
        for entry in state["devices"]:
            if str(entry.get("token", "")).lower() in wanted:
                removed += 1
            else:
                kept.append(entry)
        if removed:
            state["devices"] = kept
            _save(path, state)
    return removed
