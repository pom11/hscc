"""Telegram notification module for the HSCC daemon."""

import os
import json
import urllib.parse
import urllib.request
import ssl
import glob


ENV_FILE = os.path.expanduser("~/.hermes/.env")
OPS_CHAT_ID = os.environ.get("HSCC_OPS_CHAT_ID", "0")
OPS_THREAD_ID = os.environ.get("HSCC_OPS_THREAD_ID", "140")  # Operations topic
TELEGRAM_NOTIFY_TIMEOUT = 10


def _env_file_value(key, path=ENV_FILE):
    """Return the last non-empty `key=value` from ~/.hermes/.env, or None.

    The bot token may be duplicated across lines; last non-empty wins. The
    value is never printed or logged — only used to authenticate the API call.
    """
    value = None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    v = v.strip().strip('"').strip("'")
                    if v:
                        value = v
    except (FileNotFoundError, OSError):
        return None
    return value


def _telegram_ssl_context():
    """SSL context using a CA bundle that verifies api.telegram.org on this host.

    The daemon's stdlib Python default trust store can reject the chain; the
    certifi bundle in the Hermes venv verifies cleanly. Verification stays ON.
    """
    candidates = [os.environ.get("SSL_CERT_FILE"), os.environ.get("REQUESTS_CA_BUNDLE")]
    candidates += glob.glob(os.path.expanduser(
        "~/.hermes/hermes-agent/venv/lib/python*/site-packages/certifi/cacert.pem"))
    for ca in candidates:
        if ca and os.path.exists(ca):
            try:
                return ssl.create_default_context(cafile=ca)
            except Exception:
                continue
    return ssl.create_default_context()


def notify_operations(text):
    """Post a message to the Operations Telegram topic. Returns True on success.

    Best-effort: missing token or any network error is swallowed (the daemon
    must never crash on a failed notification). Token read from ~/.hermes/.env.
    """
    token = (os.environ.get("TELEGRAM_BOT_TOKEN")
             or _env_file_value("TELEGRAM_BOT_TOKEN"))
    if not token or not OPS_CHAT_ID:
        return False
    payload = {"chat_id": OPS_CHAT_ID, "text": text}
    if OPS_THREAD_ID:
        payload["message_thread_id"] = OPS_THREAD_ID
    data = urllib.parse.urlencode(payload).encode()
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TELEGRAM_NOTIFY_TIMEOUT,
                                    context=_telegram_ssl_context()) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        # log is imported from the main hscc module at runtime
        try:
            from . import log as _log
            _log(f"Operations notify failed: {e}", "WARN")
        except ImportError:
            pass
        return False
