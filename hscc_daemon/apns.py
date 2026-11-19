"""APNs push sender for the operator alert feed (card N2).

The main orchestrator reaches the operator's phone and Mac through Apple Push
Notification service. The durable feed (``hscc_daemon/alerts.py``) stays the
source of truth; this module is the ONE transport behind its
``alert_push.send`` seam, called once per newly created alert.

WHY THIS CLIENT
===============
APNs requires HTTP/2. The hscc venv already carries ``httpx`` (pinned in
``requirements.txt`` because Hermes' board-read chain imports it), and httpx
speaks HTTP/2 when the ``h2`` package is present — so this card adds exactly one
minimal dependency, ``h2`` (``httpx[http2]``), instead of a second HTTP stack.
The import is lazy: no config, no devices, or a test with a fake transport never
imports httpx/h2 at all. ES256 signing uses the system ``openssl`` already
present on macOS/Linux, so no crypto wheel (``cryptography``) is added.

CREDENTIALS LIVE OUTSIDE THE REPO
=================================
Everything secret comes from ``~/.hscc/apns.json`` (honouring ``HSCC_HOME`` /
``HSCC_DIR`` for tests and sandboxes):

    {
      "key_id": "ABCDE12345",              # Apple key id (10 chars)
      "team_id": "TEAM123456",             # Apple team id (10 chars)
      "key_path": "~/.hscc/AuthKey_ABCDE12345.p8",
      # or, instead of key_path:
      "keychain_service": "hscc-apns",
      "keychain_account": "AuthKey_ABCDE12345.p8",
      "topics": {"ios": "ro.parsec.hscc.ios",
                 "macos": "ro.parsec.hscc.mac"}
    }

``topics`` is optional: when absent the APNs topic is the registered device's
``bundle_id``. A string applies to every platform; a list is an allowlist (the
device's bundle id must be in it); a map names the topic per platform.

JWT
===
Token authentication: ES256 over ``{"alg":"ES256","kid":<key_id>}`` and
``{"iss":<team_id>,"iat":<now>}``. APNs accepts a token for one hour and asks
callers not to mint a new one more often than every 20 minutes; this provider
reuses it for 50 minutes and then refreshes. The private key is read per refresh
(never cached in a log or a response) and handed to ``openssl`` through a
0600 temp file, because LibreSSL cannot read the key from stdin while also
reading the message from stdin.

WHAT IS SENT
============
Alert only: ``aps.alert.title`` / ``aps.alert.body``, ``aps.sound=default`` for
``action`` / ``critical``, and ``aps.thread-id`` = the alert key when it has one.
No action buttons and no reply path — the operator answers in the main
orchestrator's pane.

PRUNING
=======
APNs answers 410 Gone for a token that no longer identifies an install, and can
answer 400 ``BadDeviceToken`` / ``DeviceTokenNotForTopic``. Those tokens are
removed from the roster (``devices.prune_tokens``). Other failures (429, 5xx,
network) keep the registration and are reported; retry belongs to a later send,
not to a loop inside this call.

NO SECRETS IN LOGS
==================
Device tokens are named by their derived id, never echoed. JWT bytes, key
material and full request URLs (which carry the token) are not logged. Transport
exceptions are reported by class name only — an httpx error's string can repeat
the URL.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import devices as device_store

log = logging.getLogger("hscc.apns")

CONFIG_FILE = "apns.json"

PRODUCTION_HOST = "https://api.push.apple.com"
DEVELOPMENT_HOST = "https://api.sandbox.push.apple.com"

REQUEST_TIMEOUT_S = 10
JWT_REFRESH_S = 50 * 60

#: APNs reasons that mean "this token is dead, remove it".
PRUNE_REASONS = frozenset({
    "BadDeviceToken",
    "DeviceTokenNotForTopic",
    "Unregistered",
})

CHECK_NAME = "APNs push (operator notifications)"

ONE_TIME_STEPS = (
    "Apple developer portal -> Keys -> new key with APNs; enable Push "
    "Notifications on app ids ro.parsec.hscc.*"
)

TEST_ALERT = {
    "id": 0,
    "title": "HSCC test notification",
    "body": "Push notifications are working.",
    "level": "action",
    "key": "hscc-test",
}


class ConfigError(Exception):
    """A config problem with an operator-readable (secret-free) sentence."""


class SigningError(Exception):
    """ES256 signing failed; the message never contains key material."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def hscc_root(hscc_dir=None) -> Path:
    if hscc_dir:
        return Path(os.path.expanduser(str(hscc_dir)))
    env = (os.environ.get("HSCC_HOME", "").strip()
           or os.environ.get("HSCC_DIR", "").strip())
    return Path(os.path.expanduser(env or "~/.hscc"))


def config_path(hscc_dir=None) -> Path:
    return hscc_root(hscc_dir) / CONFIG_FILE


def _topic_map(value):
    """Normalise ``topics`` to a string / allowlist / per-platform map."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, list):
        out = [str(v).strip() for v in value if isinstance(v, str) and v.strip()]
        return out or None
    if isinstance(value, dict):
        out = {}
        for platform in device_store.PLATFORMS:
            raw = value.get(platform)
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            if isinstance(raw, str) and raw.strip():
                out[platform] = raw.strip()
        return out or None
    raise ConfigError("topics must be a string, a list or a per-platform object")


def load_config(hscc_dir=None):
    """The normalised APNs config, or ``None`` when no file exists.

    Raises :class:`ConfigError` for an unreadable/malformed document. Error
    messages name the field, never echo its value.
    """
    path = config_path(hscc_dir)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot read {path.name} ({type(exc).__name__})") from exc
    except ValueError as exc:
        raise ConfigError(f"{path.name} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must be a JSON object")

    key_id = raw.get("key_id")
    team_id = raw.get("team_id")
    if not isinstance(key_id, str) or not key_id.strip():
        raise ConfigError("key_id is required in apns.json")
    if not isinstance(team_id, str) or not team_id.strip():
        raise ConfigError("team_id is required in apns.json")

    keychain = raw.get("keychain") if isinstance(raw.get("keychain"), dict) else {}
    cfg = {
        "key_id": key_id.strip(),
        "team_id": team_id.strip(),
        "key_path": (raw.get("key_path") or "").strip() or None,
        "keychain_service": ((raw.get("keychain_service")
                              or keychain.get("service") or "").strip()
                             or None),
        "keychain_account": ((raw.get("keychain_account")
                              or keychain.get("account") or "").strip()
                             or None),
        "topics": _topic_map(raw.get("topics", raw.get("topic"))),
    }
    if not cfg["key_path"] and not cfg["keychain_service"]:
        raise ConfigError(
            "apns.json needs key_path or keychain_service for the .p8 key"
        )
    return cfg


def key_source(cfg) -> str:
    """Human name for where the key comes from (never the key itself)."""
    if cfg.get("key_path"):
        return "key file"
    return "keychain item"


def load_key_material(cfg) -> bytes:
    """The .p8 PEM bytes: from ``key_path`` or the configured keychain item."""
    path = cfg.get("key_path")
    if path:
        try:
            return Path(os.path.expanduser(path)).read_bytes()
        except OSError as exc:
            if not cfg.get("keychain_service"):
                raise ConfigError(
                    f"the configured APNs key file is not readable "
                    f"({type(exc).__name__})"
                ) from exc
    service = cfg.get("keychain_service")
    if service:
        account = cfg.get("keychain_account") or cfg["key_id"]
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service,
                 "-a", account, "-w"],
                capture_output=True, timeout=REQUEST_TIMEOUT_S,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
            raise ConfigError(
                f"cannot read the APNs keychain item ({type(exc).__name__})"
            ) from exc
        if result.returncode != 0 or not result.stdout.strip():
            raise ConfigError(
                "the configured APNs keychain item was not found or is empty"
            )
        return result.stdout
    raise ConfigError("no APNs key source configured")


# --------------------------------------------------------------------------- #
# JWT (ES256)
# --------------------------------------------------------------------------- #

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _read_der_len(data: bytes, pos: int):
    if pos >= len(data):
        raise SigningError("truncated ECDSA signature")
    first = data[pos]
    pos += 1
    if first < 0x80:
        return first, pos
    count = first & 0x7F
    if count == 0 or count > 4 or pos + count > len(data):
        raise SigningError("malformed ECDSA signature length")
    return int.from_bytes(data[pos:pos + count], "big"), pos + count


def _read_der_int(data: bytes, pos: int):
    if pos >= len(data) or data[pos] != 0x02:
        raise SigningError("malformed ECDSA signature integer")
    pos += 1
    length, pos = _read_der_len(data, pos)
    if length <= 0 or pos + length > len(data):
        raise SigningError("truncated ECDSA signature integer")
    return int.from_bytes(data[pos:pos + length], "big"), pos + length


def der_to_raw_signature(der: bytes) -> bytes:
    """Convert an OpenSSL DER ECDSA signature to APNs' 64-byte R||S form."""
    if not der or der[0] != 0x30:
        raise SigningError("OpenSSL did not return an ECDSA signature")
    length, pos = _read_der_len(der, 1)
    if pos + length > len(der):
        raise SigningError("truncated ECDSA signature")
    r, pos = _read_der_int(der, pos)
    s, pos = _read_der_int(der, pos)
    out = bytearray()
    for value in (r, s):
        if value < 0 or value.bit_length() > 256:
            raise SigningError("ECDSA signature integer out of range")
        out.extend(value.to_bytes(32, "big"))
    return bytes(out)


def _sign_es256(key_material: bytes, signing_input: bytes) -> bytes:
    """Sign with the system OpenSSL (no crypto wheel in the venv).

    The key goes to a 0600 temp file because ``openssl dgst`` needs a path for
    ``-sign`` while the message comes from stdin. The file is always removed.
    """
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="hscc-apns-key-", suffix=".p8")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key_material)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", tmp_name],
            input=signing_input, capture_output=True,
            timeout=REQUEST_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        raise SigningError(
            f"cannot sign the APNs token ({type(exc).__name__})"
        ) from exc
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    if result.returncode != 0 or not result.stdout:
        raise SigningError("openssl could not sign with the configured APNs key")
    return der_to_raw_signature(result.stdout)


class TokenProvider:
    """ES256 JWT provider with APNs' reuse window.

    ``signer`` / ``key_loader`` / ``now`` are injectable so tests pin the JWT
    shape and refresh boundary without openssl, a key file or a clock.
    """

    def __init__(self, cfg, *, signer=None, key_loader=None, now=None):
        self.cfg = cfg
        self._signer = signer or _sign_es256
        self._key_loader = key_loader or load_key_material
        self._now = now or time.time
        self._token = None
        self._iat = 0

    def token(self) -> str:
        current = float(self._now())
        if (self._token
                and 0 <= current - self._iat < JWT_REFRESH_S):
            return self._token

        header = _b64url(json.dumps(
            {"alg": "ES256", "kid": self.cfg["key_id"]},
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8"))
        iat = int(current)
        claims = _b64url(json.dumps(
            {"iss": self.cfg["team_id"], "iat": iat},
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8"))
        signing_input = f"{header}.{claims}".encode("ascii")
        signature = self._signer(self._key_loader(self.cfg), signing_input)
        if len(signature) != 64:
            raise SigningError("ES256 signature must be 64 bytes")
        self._token = f"{header}.{claims}.{_b64url(signature)}"
        self._iat = current
        return self._token


# --------------------------------------------------------------------------- #
# Transport / payload
# --------------------------------------------------------------------------- #

def _default_transport():
    """An HTTP/2-capable client; lazy so fake transports never need httpx."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - venv is built from reqs
        raise ConfigError(
            "httpx is not installed; install requirements.txt"
        ) from exc
    try:
        return httpx.Client(http2=True, timeout=REQUEST_TIMEOUT_S)
    except ImportError as exc:  # missing h2
        raise ConfigError(
            "HTTP/2 needs the 'h2' package (httpx[http2]); install "
            "requirements.txt"
        ) from exc


def _host_for(cfg, env: str) -> str:
    hosts = cfg.get("hosts") if isinstance(cfg.get("hosts"), dict) else {}
    if env == "development":
        return str(hosts.get("development") or DEVELOPMENT_HOST)
    return str(hosts.get("production") or PRODUCTION_HOST)


def _topic_for(cfg, device: dict):
    topics = cfg.get("topics")
    bundle = device.get("bundle_id")
    if topics is None:
        return bundle
    if isinstance(topics, str):
        return topics
    if isinstance(topics, list):
        if bundle in topics:
            return bundle
        return topics[0] if len(topics) == 1 else None
    if isinstance(topics, dict):
        return topics.get(device.get("platform")) or (
            topics.get("default") if isinstance(topics.get("default"), str)
            else None)
    return None


def build_payload(alert: dict) -> dict:
    """The APNs JSON body: alert title/body, sound for action/critical, thread."""
    aps = {
        "alert": {
            "title": str(alert.get("title") or ""),
            "body": str(alert.get("body") or ""),
        },
    }
    if str(alert.get("level") or "").lower() in ("action", "critical"):
        aps["sound"] = "default"
    key = alert.get("key")
    if key:
        aps["thread-id"] = str(key)
    return {"aps": aps}


def _response_reason(response):
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
        try:
            data = json.loads(response.content.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return data.get("reason") if isinstance(data, dict) else None


def _is_prunable(status: int, reason) -> bool:
    return status == 410 or reason in PRUNE_REASONS


def _send_one(client, cfg, provider, device: dict, payload: bytes) -> dict:
    """Push one alert to one device. Returns a token-free outcome dict."""
    platform = device.get("platform")
    ident = device.get("id") or "?"
    try:
        token = device_store.normalise_token(device.get("token"))
        env = device_store.normalise_environment(device.get("env"))
    except ValueError:
        return {"platform": platform, "device_id": ident, "ok": False,
                "status": None, "reason": "invalid_registration",
                "token": None}

    topic = _topic_for(cfg, device)
    if not topic:
        return {"platform": platform, "device_id": ident, "ok": False,
                "status": None, "reason": "topic_not_allowed", "token": token}

    url = f"{_host_for(cfg, env)}/3/device/{token}"
    headers = {
        "authorization": f"bearer {provider.token()}",
        "apns-topic": topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "content-type": "application/json",
    }
    try:
        response = client.post(url, headers=headers, content=payload)
    except Exception as exc:  # noqa: BLE001 - transport failure is data
        log.warning("apns: %s device %s transport error (%s)",
                    platform, ident, type(exc).__name__)
        return {"platform": platform, "device_id": ident, "ok": False,
                "status": None, "reason": "transport_error", "token": token}

    status = int(getattr(response, "status_code", 0) or 0)
    reason = _response_reason(response)
    ok = status == 200
    if ok:
        log.info("apns: alert -> %s device %s (HTTP 200)", platform, ident)
    else:
        log.warning("apns: %s device %s rejected (HTTP %s %s)",
                    platform, ident, status, reason or "no reason")
    return {"platform": platform, "device_id": ident, "ok": ok,
            "status": status, "reason": reason, "token": token}


def _send(alert: dict, *, hscc_dir=None, transport=None, now=None) -> dict:
    cfg = load_config(hscc_dir)
    if cfg is None:
        return {"sent": False, "reason": "no_config",
                "detail": "~/.hscc/apns.json is not configured"}

    entries = device_store.load(hscc_dir)
    if not entries:
        return {"sent": False, "reason": "no_devices", "devices": 0,
                "detail": "no push devices registered yet"}

    provider = TokenProvider(cfg, now=now)
    payload = json.dumps(build_payload(alert), ensure_ascii=False).encode("utf-8")

    client = transport
    owned = client is None
    if client is None:
        client = _default_transport()

    results = []
    try:
        for entry in entries:
            results.append(_send_one(client, cfg, provider, entry, payload))
    finally:
        if owned:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    prunable = [r["token"] for r in results
                if r.get("token") and _is_prunable(r.get("status") or 0,
                                                   r.get("reason"))]
    pruned = device_store.prune_tokens(prunable, hscc_dir) if prunable else 0

    sent = sum(1 for r in results if r.get("ok"))
    failed = len(results) - sent - len(prunable)
    outcome = {
        "sent": sent > 0,
        "reason": "sent" if sent else ("pruned" if pruned else "all_failed"),
        "devices": len(entries),
        "sent_count": sent,
        "failed_count": max(0, failed),
        "pruned": pruned,
        "results": [
            {k: v for k, v in r.items() if k != "token"} for r in results
        ],
    }
    return outcome


def send_alert(alert: dict, *, hscc_dir=None, transport=None, now=None) -> dict:
    """Push one created alert to every registered device; never raises.

    The return value is JSON-able and contains no tokens, JWTs or key material.
    """
    try:
        return _send(alert or {}, hscc_dir=hscc_dir, transport=transport,
                     now=now)
    except ConfigError as exc:
        return {"sent": False, "reason": "config_error", "detail": str(exc)}
    except SigningError as exc:
        return {"sent": False, "reason": "signing_error", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a push must never fail the alert
        log.warning("apns: send failed (%s)", type(exc).__name__)
        return {"sent": False, "reason": "sender_error",
                "detail": type(exc).__name__}


def send_test(*, hscc_dir=None, transport=None, now=None) -> dict:
    """Send the ``hscc notify test`` push through the same sender."""
    result = send_alert(TEST_ALERT, hscc_dir=hscc_dir, transport=transport,
                        now=now)
    result["test"] = True
    return result


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #

def doctor_check(hscc_dir=None) -> dict:
    """WARN (ok=False, fatal=False) when push cannot work.

    Missing APNs configuration is a normal fresh-install state — the one-time
    Apple portal steps are the operator's — so the row is never fatal. A
    configured key that cannot be read is also a warning, and the detail says
    which half failed without echoing any credential.
    """
    path = config_path(hscc_dir)
    if not path.exists():
        return {
            "name": CHECK_NAME,
            "ok": False,
            "fatal": False,
            "detail": "no APNs key configured — push to the apps is off",
            "fix": ("One-time: " + ONE_TIME_STEPS + "; save the .p8 outside "
                    "the repo (chmod 600) and write ~/.hscc/apns.json with "
                    "key_id, team_id and key_path (or keychain_service/"
                    "keychain_account)."),
        }
    try:
        cfg = load_config(hscc_dir)
    except ConfigError as exc:
        return {
            "name": CHECK_NAME,
            "ok": False,
            "fatal": False,
            "detail": f"APNs config is not usable — {exc}",
            "fix": ("Fix ~/.hscc/apns.json (key_id, team_id, key_path or "
                    "keychain_service). " + ONE_TIME_STEPS + "."),
        }

    try:
        load_key_material(cfg)
    except ConfigError as exc:
        return {
            "name": CHECK_NAME,
            "ok": False,
            "fatal": False,
            "detail": f"APNs key configured but unusable — {exc}",
            "fix": ("Put the .p8 where key_path points (or create the "
                    "keychain item), then re-run. " + ONE_TIME_STEPS + "."),
        }

    try:
        count = len(device_store.load(hscc_dir))
    except Exception:  # noqa: BLE001 - roster trouble must not crash doctor
        count = 0
    detail = (f"APNs key configured ({key_source(cfg)}); "
              f"{count} device{'s' if count != 1 else ''} registered")
    if count == 0:
        detail += " (the apps register on launch)"
    return {"name": CHECK_NAME, "ok": True, "fatal": False,
            "detail": detail, "fix": ""}
