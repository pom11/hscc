"""Unit tests for hscc-api — GET /v1/logs (bounded redacted log tail, t_3a995be5).

Contract (see ios-app/Sources/HSCC/Models.swift — LogSource/LogEntry/LogsResponse):

    GET /v1/logs?source=daemon|api|worker&limit=<N>  ->  200 bare array of
    [{timestamp, level, source, line}] (LogsResponse = [LogEntry]).

Three server-side guarantees under test:
  1. BOUNDED MEMORY — ``_backing_tail`` reverse-seeks from EOF and reads only
     the trailing region, so a huge log never loads fully into memory.
  2. BOUNDED RESPONSE — ``limit`` is clamped to [1, 200]; the payload never
     exceeds ``limit`` entries.
  3. REDACTED BEFORE SERVING — Tailnet->100.64.0.1, RFC1918->10.0.0.x, other
     IPv4->[REDACTED_IP]; bearer/token/apikey/session/long-run masked.

The suite is hermetic:
  * ``_backing_tail`` is monkeypatched for the HTTP-level tests, so no test
    touches the operator's live logs;
  * the reverse-seek tail itself is exercised against a REAL temp file so the
    bounded-memory behaviour is proven, not assumed;
  * auth is exercised end-to-end over real loopback HTTP.
"""

import http.client
import json
import threading
import types
import builtins

import pytest

import api_server
import routes_logs


# --------------------------------------------------------------------------- #
# Hermetic backing fake
# --------------------------------------------------------------------------- #

@pytest.fixture
def fakes(monkeypatch):
    """Stub ``routes_logs._backing_tail`` so no test reads a live log."""
    state = {"lines": [], "picked": {}}

    def _fake_tail(path, limit):
        state["picked"][path] = limit
        return list(state["lines"])

    monkeypatch.setattr(routes_logs, "_backing_tail", _fake_tail)
    return state


@pytest.fixture
def running(tmp_path, fakes):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path),
                                          addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _req(running, token, path="/v1/logs", method="GET"):
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"raw": raw}
    return resp.status, payload


_DAEMON_LINES = [
    "[2026-09-03T16:05:17.732336+00:00] [ WARN] structured status unavailable",
    "[2026-09-03T16:05:18.685287+00:00] [ INFO] DGX check complete: ok=True",
    "[2026-09-03T16:05:18.711179+00:00] [ INFO] Watchdog: pipeline healthy",
]


# --------------------------------------------------------------------------- #
# Core contract
# --------------------------------------------------------------------------- #

def test_logs_200_bare_array_shape(running, token, fakes):
    """200 + a bare array of {timestamp, level, source, line} entries."""
    fakes["lines"] = _DAEMON_LINES
    status, payload = _req(running, token, "/v1/logs?source=daemon&limit=3")
    assert status == 200
    assert isinstance(payload, list)          # bare array, NOT an object
    assert len(payload) == 3
    for e in payload:
        assert set(e.keys()) == {"timestamp", "level", "source", "line"}
        assert e["source"] == "daemon"
        assert e["level"] in ("INFO", "WARN", "ERROR", "DEBUG")

    # Parsing extracts timestamp + level from the daemon format.
    assert payload[0]["timestamp"] == "2026-09-03T16:05:17.732336+00:00"
    assert payload[0]["level"] == "WARN"
    assert payload[1]["level"] == "INFO"


def test_logs_bounds_entries_to_limit(running, token, fakes):
    """Even if the backing over-returns, the payload never exceeds ``limit``."""
    fakes["lines"] = [f"line{i}" for i in range(50)]
    status, payload = _req(running, token, "/v1/logs?source=api&limit=5")
    assert status == 200
    assert len(payload) == 5


def test_logs_limit_capped_at_200(running, token, fakes):
    """A huge requested limit is clamped to 200 — bounded response."""
    fakes["lines"] = _DAEMON_LINES
    status, payload = _req(running, token, "/v1/logs?source=daemon&limit=9999")
    assert status == 200
    assert len(payload) <= routes_logs.MAX_LIMIT
    # The backing was asked for the clamped limit, not the raw huge number.
    picked = list(fakes["picked"].values())
    assert picked and max(picked) == routes_logs.MAX_LIMIT


def test_logs_limit_min_clamped_to_1(running, token, fakes):
    fakes["lines"] = _DAEMON_LINES
    status, payload = _req(running, token, "/v1/logs?source=daemon&limit=0")
    assert status == 200
    assert len(payload) <= 1


def test_logs_default_limit(running, token, fakes):
    """No limit -> DEFAULT_LIMIT (50)."""
    fakes["lines"] = _DAEMON_LINES
    status, payload = _req(running, token, "/v1/logs?source=daemon")
    assert status == 200
    assert max(fakes["picked"].values()) == routes_logs.DEFAULT_LIMIT


def test_logs_bad_limit_is_400(running, token, fakes):
    status, payload = _req(running, token, "/v1/logs?source=daemon&limit=abc")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_logs_bad_source_is_400(running, token, fakes):
    status, payload = _req(running, token, "/v1/logs?source=nope")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "daemon" in payload["error"]["message"]


def test_logs_missing_source_is_400(running, token, fakes):
    status, payload = _req(running, token, "/v1/logs")
    assert status == 400


def test_logs_auth_401_without_token(running, fakes):
    status, payload = _req(running, None, "/v1/logs?source=daemon")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_logs_auth_401_wrong_token(running, fakes):
    status, payload = _req(running, "bad-token", "/v1/logs?source=daemon")
    assert status == 401


def test_logs_post_is_405(running, token, fakes):
    status, payload = _req(running, token, "/v1/logs", method="POST")
    assert status == 405


def test_logs_unreadable_log_degrades_to_empty(running, token, monkeypatch):
    """An absent/unreadable log -> 200 with an empty array (honest tail)."""
    monkeypatch.setattr(routes_logs, "_backing_tail", lambda path, limit: [])
    status, payload = _req(running, token, "/v1/logs?source=daemon")
    assert status == 200
    assert payload == []


# --------------------------------------------------------------------------- #
# Redaction (server-side, before serving)
# --------------------------------------------------------------------------- #

def test_redact_tailnet_host_to_placeholder():
    """Tailnet CGNAT (100.64.0.0/10) -> 100.64.0.1."""
    line = "connecting to worker at 100.122.7.9:8080"
    assert "100.64.0.1" in routes_logs._redact(line)
    assert "100.122.7.9" not in routes_logs._redact(line)


def test_redact_rfc1918_to_placeholder():
    """LAN (10/8, 172.16/12, 192.168/16) -> 10.0.0.x."""
    for ip in ("10.0.0.42", "172.20.1.2", "192.168.88.247"):
        out = routes_logs._redact(f"served from {ip}:8788")
        assert "10.0.0.x" in out
        assert ip not in out


def test_redact_other_ipv4():
    assert "[REDACTED_IP]" in routes_logs._redact("public 8.8.8.8 here")


def test_redact_placeholder_not_remasked():
    """The 100.64.0.1 placeholder is NOT re-masked into [REDACTED_IP]."""
    out = routes_logs._redact("host 100.64.0.1 ready")
    assert "100.64.0.1" in out
    assert "[REDACTED_IP]" not in out


def test_redact_bearer_and_tokens():
    # A real bearer header value must never survive; only the redacted form.
    out = routes_logs._redact("Authorization: Bearer abcDEF123xyz456.more")
    assert "abcDEF123xyz456.more" not in out
    assert "Bearer ***" in out

    out = routes_logs._redact("Authorization: Bearer abc")
    # Too short to be a cred — leave it; only 6+ char runs are masked.
    assert "abc" in out

    out = routes_logs._redact("token=abcDEF123 secret=hunter2 api_key=sekret")
    assert "abcDEF123" not in out
    assert "hunter2" not in out
    assert "sekret" not in out
    assert "token=***" in out and "secret=***" in out and "api_key=***" in out


def test_redact_session_and_long_run():
    out = routes_logs._redact("job sess_7f3aX92 running")
    assert "sess_***" in out and "sess_7f3aX92" not in out

    out = routes_logs._redact("payload 01234567890123456789hash long run")
    assert "01234567890123456789hash" not in out
    assert "***" in out


def test_served_entries_are_redacted(running, token, fakes):
    """The HTTP payload is already redacted — not just the client-side filter."""
    fakes["lines"] = [
        "[2026-09-03T16:05:00+00:00] [ INFO] hit 100.122.7.9 token=sk-secret-abcdef long-run-sessionid-but-not-short",
    ]
    _, payload = _req(running, token, "/v1/logs?source=daemon&limit=1")
    entry = payload[0]["line"]
    assert "100.122.7.9" not in entry
    assert "sk-secret-abcdef" not in entry
    assert "100.64.0.1" in entry or "***" in entry


# --------------------------------------------------------------------------- #
# _backing_tail — bounded reverse-seek (proven against a real temp file)
# --------------------------------------------------------------------------- #

def _write_tmp_log(lines):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".log")
    with __import__("os").fdopen(fd, "w") as f:
        for ln in lines:
            f.write(ln + "\n")
    return path


def test_backing_tail_returns_last_n_lines():
    path = _write_tmp_log([f"line{i}" for i in range(100)])
    try:
        got = routes_logs._backing_tail(path, 7)
        assert got == [f"line{i}" for i in range(93, 100)]
    finally:
        import os
        os.unlink(path)


def test_backing_tail_does_not_read_whole_file(monkeypatch):
    """Bounded-memory proof: a huge file is never fully read into memory.

    We assert the backing only ever issues reads whose total bytes stay well
    below the file size, by replacing the file object's read() with an
    instrumented one that records how much it read and refuses to read in
    arbitrarily large (unbounded) chunks.
    """
    path = _write_tmp_log([f"line{i}" for i in range(100000)])  # ~800 KB
    try:
        reads = []

        class _Probe:
            def __init__(self, f):
                self._f = f
            def seek(self, *a):
                self._f.seek(*a)
            def read(self, n=0):
                data = self._f.read(n)
                reads.append(n if n else len(data))
                return data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self._f.close()
                return False
            def __getattr__(self, k):
                return getattr(self._f, k)

        orig_open = builtins.open
        def probe_open(path2, mode, *a, **k):
            return _Probe(orig_open(path2, mode, *a, **k))
        monkeypatch.setattr(builtins, "open", probe_open)

        got = routes_logs._backing_tail(path, 5)
        assert got == [f"line{i}" for i in range(99995, 100000)]
        # Every read was a bounded 16 KiB chunk (never the whole 800 KB file),
        # and total bytes read is far below the file size.
        assert reads and max(reads) <= 16 * 1024
        assert sum(reads) < 200 * 1024
    finally:
        import os
        os.unlink(path)


def test_backing_tail_absent_file_returns_empty():
    assert routes_logs._backing_tail("/nonexistent/nope.log", 10) == []
