"""Unit tests for hscc-api (Phase A1): server skeleton, auth, bind/config
resolution, JSON error contract, and /v1/ping.

The suite is hermetic: servers bind loopback port 0 (ephemeral), tokens are
generated in tmp_path dirs, no fixed public port or live tailnet is used.
"""

import http.client
import json
import os
import re
import socket
import stat
import threading

import pytest

import api_server
from api_server import (
    ApiError,
    DEFAULT_BIND,
    DEFAULT_PORT,
    MAX_BODY_BYTES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class RunningServer:
    """A live server bound to loopback port 0, torn down by the fixture."""

    def __init__(self, hscc_dir, **kwargs):
        self.server = api_server.create_server(
            hscc_dir=hscc_dir, addr=("127.0.0.1", 0), **kwargs
        )
        self.host, self.port = self.server.server_address[:2]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method="GET", path="/v1/ping", token=None, body=None,
                content_type="application/json"):
        """Return (status, parsed_json). ``token`` overrides the real token."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = content_type
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        else:
            data = None
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw
        return resp.status, payload


@pytest.fixture
def running(tmp_path):
    srv = RunningServer(hscc_dir=str(tmp_path))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    """The real token for the running fixture's server."""
    return api_server.load_token(running.server.ctx.hscc_dir)


# ---------------------------------------------------------------------------
# Token generation + file mode
# ---------------------------------------------------------------------------

def _mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_token_generated_on_first_run(hscc_dir):
    tok = api_server.load_token(hscc_dir)
    token_path = os.path.join(hscc_dir, "api-token")
    # A fresh, empty dir generates and creates the file.
    assert os.path.exists(token_path)
    assert tok == open(token_path).read().strip()
    # Mode is 0600 (owner read/write only) — never group/other readable.
    assert _mode_of(token_path) == 0o600


def test_token_reused_when_file_exists(hscc_dir):
    """Existing non-empty token file is read, not regenerated."""
    token_path = os.path.join(hscc_dir, "api-token")
    os.makedirs(hscc_dir, exist_ok=True)
    with open(token_path, "w") as fh:
        fh.write("preset-token-value\n")
    assert api_server.load_token(hscc_dir) == "preset-token-value"


def test_token_fail_closed_on_empty(hscc_dir):
    """Empty token file -> refuse, never fall back to no-auth."""
    token_path = os.path.join(hscc_dir, "api-token")
    os.makedirs(hscc_dir, exist_ok=True)
    open(token_path, "w").close()  # empty file
    with pytest.raises(RuntimeError, match="empty"):
        api_server.load_token(hscc_dir)


def test_token_fail_closed_on_unreadable(hscc_dir):
    """Unreadable token file -> refuse via load_token."""
    token_path = os.path.join(hscc_dir, "api-token")
    os.makedirs(hscc_dir, exist_ok=True)
    with open(token_path, "w") as fh:
        fh.write("secret\n")
    os.chmod(token_path, 0)  # no permissions
    with pytest.raises(RuntimeError):
        api_server.load_token(hscc_dir)


def test_create_server_fail_closed_on_empty_token(hscc_dir):
    """create_server() must refuse to start on an empty token file."""
    token_path = os.path.join(hscc_dir, "api-token")
    os.makedirs(hscc_dir, exist_ok=True)
    open(token_path, "w").close()
    with pytest.raises(RuntimeError, match="empty"):
        api_server.create_server(hscc_dir=hscc_dir)


# ---------------------------------------------------------------------------
# Auth (over real HTTP)
# ---------------------------------------------------------------------------

def test_auth_missing_token_401(running):
    status, payload = running.request(token=None)
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert "error" in payload and "speak" in payload["error"]


def test_auth_wrong_token_401(running):
    status, payload = running.request(token="totally-wrong-token")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_auth_accepts_correct_token(running, token):
    status, payload = running.request(token=token)
    assert status == 200
    assert payload.get("ok") is True


def test_auth_rejects_non_bearer_scheme(running, token):
    status, payload = running.request(token="Basic " + token)
    assert status == 401


def test_compare_digest_not_plain_eq():
    # Gabfill: constant-time comparison, and a wrong token never validates.
    assert api_server.token_valid("abc", "abc") is True
    assert api_server.token_valid("abc", "abd") is False
    assert api_server.token_valid("", "abc") is False
    assert api_server.token_valid("abc", "") is False
    assert api_server.token_valid(None, "abc") is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Bind / config resolution
# ---------------------------------------------------------------------------

def test_default_bind_loopback():
    assert api_server.resolve_bind(DEFAULT_BIND) == "127.0.0.1"


def test_bind_refuses_zero():
    for bad in ("0.0.0.0", "::"):
        with pytest.raises(RuntimeError, match="must never be reachable"):
            api_server.resolve_bind(bad)


def test_config_refuses_zero(hscc_dir):
    """Even if api.json requests 0.0.0.0, resolve_config refuses it."""
    os.makedirs(hscc_dir, exist_ok=True)
    with open(os.path.join(hscc_dir, "api.json"), "w") as fh:
        json.dump({"bind": "0.0.0.0"}, fh)
    with pytest.raises(RuntimeError, match="must never be reachable"):
        api_server.resolve_config(hscc_dir=hscc_dir)


def test_resolve_config_defaults(hscc_dir):
    cfg = api_server.resolve_config(hscc_dir=hscc_dir)
    assert cfg == {"host": "127.0.0.1", "port": DEFAULT_PORT}


def test_config_file_overrides(hscc_dir):
    os.makedirs(hscc_dir, exist_ok=True)
    with open(os.path.join(hscc_dir, "api.json"), "w") as fh:
        json.dump({"bind": "10.0.0.5", "port": 9999}, fh)
    cfg = api_server.resolve_config(hscc_dir=hscc_dir)
    assert cfg == {"host": "10.0.0.5", "port": 9999}


def test_override_beats_config(hscc_dir):
    os.makedirs(hscc_dir, exist_ok=True)
    with open(os.path.join(hscc_dir, "api.json"), "w") as fh:
        json.dump({"bind": "10.0.0.5", "port": 9999}, fh)
    cfg = api_server.resolve_config(
        hscc_dir=hscc_dir, bind_override="loopback", port_override=1234
    )
    assert cfg == {"host": "127.0.0.1", "port": 1234}


def test_explicit_ip_bind():
    assert api_server.resolve_bind("192.168.1.10") == "192.168.1.10"


def test_bind_rejects_garbage(hscc_dir):
    with pytest.raises(RuntimeError, match="invalid bind"):
        api_server.resolve_bind("not-an-ip")


def test_tailscale_bind_fails_cleanly_when_no_ip(monkeypatch):
    """tailscale bind with no discoverable IP -> clear error, no widening."""
    monkeypatch.setattr(api_server, "_find_tailnet_ip", lambda: None)
    with pytest.raises(RuntimeError, match="[Tt]ailscale|tailnet"):
        api_server.resolve_bind("tailscale")


def test_tailscale_bind_uses_discovered_ip(monkeypatch):
    monkeypatch.setattr(api_server, "_find_tailnet_ip", lambda: "100.64.0.1")
    assert api_server.resolve_bind("tailscale") == "100.64.0.1"


# ---------------------------------------------------------------------------
# Error contract / routes
# ---------------------------------------------------------------------------

def test_unknown_route_404_shape(running, token):
    status, payload = running.request(path="/v1/nope", token=token)
    assert status == 404
    assert set(payload["error"]) == {"code", "message", "speak"}
    assert payload["error"]["code"] == "not_found"
    assert isinstance(payload["error"]["speak"], str)


def test_outside_v1_404(running, token):
    status, _ = running.request(path="/healthz", token=token)
    assert status == 404


def test_method_not_allowed(running, token):
    """POST /v1/ping (a GET-only route) -> 405 with the error shape."""
    status, payload = running.request(method="POST", path="/v1/ping", token=token)
    assert status == 405
    assert payload["error"]["code"] == "method_not_allowed"


def test_oversized_body_400(running, token):
    """A declared Content-Length over 1 MiB -> 400, rejected before the body
    is read. We craft the request with a raw socket so we can declare an
    oversized Content-Length without actually pushing 1 MiB+ across the wire
    (the server must reject on the declared length, not after buffering it)."""
    big_len = MAX_BODY_BYTES + 1
    request = (
        f"POST /v1/ping HTTP/1.1\r\n"
        f"Host: {running.host}:{running.port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {big_len}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode()
    sock = socket.create_connection((running.host, running.port), timeout=5)
    try:
        sock.sendall(request)
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    status_line = head.decode().splitlines()[0]
    assert " 400 " in status_line
    payload = json.loads(body)
    assert payload["error"]["code"] == "bad_request"
    assert "too large" in payload["error"]["message"]


def test_ping_returns_expected_shape(running, token):
    status, payload = running.request(path="/v1/ping", token=token)
    assert status == 200
    assert payload["ok"] is True
    assert payload["service"] == "hscc-api"
    assert "version" in payload
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_confirm_required_shape_exists():
    """The 409 confirm_required error shape must exist now (A4 uses it)."""
    err = ApiError(409, "confirm_required", "confirm: true is required")
    d = err.to_dict()
    assert d == {
        "error": {
            "code": "confirm_required",
            "message": "confirm: true is required",
            "speak": "confirm: true is required",
        }
    }


def test_error_never_leaks_token(running):
    """A wrong token must NOT appear anywhere in the 401 response body."""
    bogus = "leak-me-please-secret"
    status, payload = running.request(token=bogus)
    assert status == 401
    assert bogus not in json.dumps(payload)


# ---------------------------------------------------------------------------
# 500 internal error path (unhandled exception in a handler)
# ---------------------------------------------------------------------------

def test_internal_error_is_sanitized(running, token):
    """An unhandled exception -> 500 internal_error, no traceback leak."""
    def boom(server, ctx, query, body):
        raise ValueError("secret internals: db password = xyz")
    api_server.ROUTES.append(("GET", re.compile(r"^/v1/_boom$"), boom))
    try:
        status, payload = running.request(path="/v1/_boom", token=token)
        assert status == 500
        assert payload["error"]["code"] == "internal_error"
        assert "db password" not in json.dumps(payload)
        assert "xyz" not in json.dumps(payload)
        assert "Traceback" not in json.dumps(payload)
    finally:
        api_server.ROUTES.pop()
