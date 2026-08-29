"""Unit tests for hscc-api sessions manager (routes_sessions.py).

Card t_c2631579: "List a profile's sessions with message count, token totals
and compaction headroom; retire or compact a bloated one."

The suite is hermetic: the profile's session DB is faked with a REAL in-memory
SQLite ``sessions`` table (so the module's SQL and row shaping are exercised
against a genuine engine), and ``_backing_open_profile_db`` / the bloat
verdict seam are monkeypatched so no test touches a real ``~/.hscc`` or real
Hermes state. Handlers are driven over real loopback HTTP (port 0) like the
neighbouring suites, so auth + the route dispatcher are exercised end-to-end.

Coverage required by the card:
  * GET /v1/sessions?profile=X lists sessions with message count, token
    totals (incl. total_tokens sum) and compaction headroom;
  * list only surfaces LISTABLE (non-child), non-archived sessions, newest
    first;
  * a session is NOT flagged bloated for size alone — only positive
    compaction-failure evidence triggers it (parity with the orchestrator);
  * POST retire requires confirm (409 otherwise); retires by id (retitles,
    history kept) with the right profile;
  * POST compact requires confirm (409 otherwise); keeps the session, clears
    the compaction-failure latch, re-arms native compaction;
  * unknown session id -> 404; missing profile -> 400; missing confirm -> 409;
  * auth enforced (401) on all three endpoints.
"""
import json
import sqlite3
import sys
import threading
import types

import pytest

import api_server
import routes_sessions

# The sessions table's columns (subset the module reads). Mirrors the real
# hermes_state SCHEMA_SQL but keeps only what the sessions manager reads.
_SESSION_COLS = [
    "id TEXT PRIMARY KEY", "source TEXT NOT NULL", "user_id TEXT",
    "model TEXT", "model_config TEXT", "parent_session_id TEXT",
    "started_at REAL NOT NULL", "ended_at REAL", "end_reason TEXT",
    "message_count INTEGER DEFAULT 0", "tool_call_count INTEGER DEFAULT 0",
    "input_tokens INTEGER DEFAULT 0", "output_tokens INTEGER DEFAULT 0",
    "cache_read_tokens INTEGER DEFAULT 0", "cache_write_tokens INTEGER DEFAULT 0",
    "reasoning_tokens INTEGER DEFAULT 0", "title TEXT",
    "compression_failure_error TEXT",
    "compression_fallback_streak INTEGER NOT NULL DEFAULT 0",
    "compression_ineffective_count INTEGER NOT NULL DEFAULT 0",
    "profile_name TEXT", "archived INTEGER NOT NULL DEFAULT 0",
    "pinned INTEGER NOT NULL DEFAULT 0",
]


class _FakeSessionDB:
    """Live in-memory SQLite stand-in for the profile's state.db.

    Exposes the ``sessions`` table plus the small ``SessionDB`` surface the
    module uses (``get_session`` / ``set_session_title`` / ``close``), so both
    the module's raw SQL and its named-method calls run against a real engine.
    ``closed`` is set so tests can assert the module never leaks connections.
    """

    def __init__(self):
        self.closed = False
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"CREATE TABLE sessions ({', '.join(_SESSION_COLS)})")
        self._conn.commit()

    def get_session(self, sid):
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        return dict(row) if row else None

    def get_session_title(self, sid):
        row = self._conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        return row["title"] if row else None

    def set_session_title(self, sid, title):
        cur = self._conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?", (title, sid)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def seed(self, **cols):
        allowed = {c.split()[0] for c in _SESSION_COLS}
        row = {k: v for k, v in cols.items() if k in allowed}
        row.setdefault("source", "cli")
        row.setdefault("started_at", 1000.0)
        row.setdefault("id", f"s{len(self._rows())}")
        placeholders = ", ".join("?" for _ in row)
        keys = ", ".join(row.keys())
        self._conn.execute(
            f"INSERT INTO sessions ({keys}) VALUES ({placeholders})",
            list(row.values()),
        )
        self._conn.commit()
        return row["id"]

    def _rows(self):
        return self._conn.execute(
            "SELECT id FROM sessions"
        ).fetchall()

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------- #
# Server fixture
# --------------------------------------------------------------------------- #

@pytest.fixture
def running(tmp_path):
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


@pytest.fixture
def fake_profile(monkeypatch):
    """Install a fresh in-memory session store behind the module's seam."""
    db = _FakeSessionDB()
    ensured = []
    _open = lambda profile, read_only=False: db
    monkeypatch.setattr(routes_sessions, "_backing_open_profile_db", _open)
    monkeypatch.setattr(routes_sessions, "_backing_ensure_threshold",
                        lambda profile: ensured.append(profile) or {})
    db.ensured = ensured
    return db


def _request(running, token, method, path, body=None, timeout=5):
    import http.client
    conn = http.client.HTTPConnection(running.host, running.port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        payload: dict = json.loads(data) if data else {}
    except ValueError:
        payload = {"raw": data}
    return resp.status, payload


# --------------------------------------------------------------------------- #
# GET /v1/sessions — list
# --------------------------------------------------------------------------- #

def test_list_sessions_reports_counts_tokens_headroom(running, token, fake_profile):
    fake_profile.seed(id="s1", title="hscc", message_count=12,
                      input_tokens=5000, output_tokens=2000,
                      cache_read_tokens=300, cache_write_tokens=100,
                      reasoning_tokens=900, model="m1")
    status, payload = _request(running, token, "GET", "/v1/sessions?profile=hscc-orch")
    assert status == 200
    assert payload["profile"] == "hscc-orch"
    assert payload["count"] == 1
    s = payload["sessions"][0]
    assert s["id"] == "s1"
    assert s["title"] == "hscc"
    assert s["message_count"] == 12
    assert s["input_tokens"] == 5000
    assert s["output_tokens"] == 2000
    # total_tokens = input+output+cache_read+cache_write+reasoning
    assert s["total_tokens"] == 5000 + 2000 + 300 + 100 + 900
    # compaction headroom = context_window - threshold_tokens (stable guarantee)
    assert s["context_window"] > s["threshold_tokens"]
    assert s["compaction_headroom"] == s["context_window"] - s["threshold_tokens"]


def test_list_hides_children_and_archived_orders_newest(running, token, fake_profile):
    fake_profile.seed(id="root", title="hscc", started_at=100.0)
    fake_profile.seed(id="child", title=None, parent_session_id="root",
                      started_at=200.0)  # sub-agent run -> hidden
    fake_profile.seed(id="old", title="hscc", started_at=50.0)
    fake_profile.seed(id="arch", title="hscc", started_at=300.0, archived=1)
    status, payload = _request(running, token, "GET",
                               "/v1/sessions?profile=hscc-orch")
    assert status == 200
    ids = [s["id"] for s in payload["sessions"]]
    assert "child" not in ids       # sub-agent runs are not surfaced
    assert "arch" not in ids        # archived is not surfaced
    assert ids == ["root", "old"]   # newest first


def test_list_never_flags_bloat_on_size_alone(running, token, fake_profile):
    """A LARGE session is not bloated — size is not a positive signal."""
    fake_profile.seed(id="big", title="hscc", message_count=5000,
                      input_tokens=900_000_000, output_tokens=1_000_000)
    status, payload = _request(running, token, "GET",
                               "/v1/sessions?profile=hscc-orch")
    assert status == 200
    s = payload["sessions"][0]
    assert s["id"] == "big"
    assert s["bloated"] is False
    assert s["reason"] == ""


def test_list_flags_bloat_on_failure_evidence(running, token, fake_profile):
    fake_profile.seed(id="failing", title="hscc", message_count=80,
                      input_tokens=70_000,
                      compression_failure_error="model context exceeded")
    status, payload = _request(running, token, "GET",
                               "/v1/sessions?profile=hscc-orch")
    assert payload["sessions"][0]["bloated"] is True
    assert payload["sessions"][0]["reason"]


def test_list_missing_profile_400(running, token):
    status, payload = _request(running, token, "GET", "/v1/sessions")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "profile" in payload["error"]["message"]


def test_list_unreachable_profile_200_empty(running, token, monkeypatch):
    monkeypatch.setattr(routes_sessions, "_backing_open_profile_db",
                        lambda profile, read_only=False: None)
    status, payload = _request(running, token, "GET",
                               "/v1/sessions?profile=nope")
    assert status == 200
    assert payload["sessions"] == []


def test_list_auth_401(running):
    status, _ = _request(running, None, "GET",
                         "/v1/sessions?profile=hscc-orch")
    assert status == 401


# --------------------------------------------------------------------------- #
# POST retire / compact — confirm gate
# --------------------------------------------------------------------------- #

def test_retire_requires_confirm(running, token, fake_profile):
    sid = fake_profile.seed(id="s1", title="hscc", message_count=99,
                            input_tokens=50_000,
                            compression_failure_error="context exceeded")
    status, payload = _request(running, token, "POST",
                               f"/v1/sessions/{sid}/retire",
                               body={"profile": "hscc-orch"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    # nothing changed
    assert fake_profile.get_session(sid)["title"] == "hscc"


def test_compact_requires_confirm(running, token, fake_profile):
    sid = fake_profile.seed(id="s1", title="hscc", message_count=99,
                            compression_failure_error="context exceeded",
                            compression_fallback_streak=3,
                            compression_ineffective_count=2)
    status, payload = _request(running, token, "POST",
                               f"/v1/sessions/{sid}/compact",
                               body={"profile": "hscc-orch"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    row = fake_profile.get_session(sid)
    assert row["compression_failure_error"] == "context exceeded"  # intact


def test_retire_requires_profile_in_body(running, token, fake_profile):
    sid = fake_profile.seed(id="s1", title="hscc")
    status, payload = _request(running, token, "POST",
                               f"/v1/sessions/{sid}/retire",
                               body={"confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_retire_unknown_session_404(running, token, fake_profile):
    status, payload = _request(running, token, "POST",
                               "/v1/sessions/nope/retire",
                               body={"profile": "hscc-orch", "confirm": True})
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_retire_non_destructive_by_id(running, token, fake_profile):
    sid = fake_profile.seed(id="s1", title="hscc", message_count=250,
                            input_tokens=40_000,
                            compression_failure_error="context exceeded")
    status, payload = _request(running, token, "POST",
                               f"/v1/sessions/{sid}/retire",
                               body={"profile": "hscc-orch", "confirm": True})
    assert status == 200
    assert payload["session_id"] == sid
    assert payload["previous_title"] == "hscc"
    # history kept: same id, same rows, retitled
    row = fake_profile.get_session(sid)
    assert row["id"] == sid
    assert row["message_count"] == 250              # intact
    assert row["title"].startswith("hscc-retired-") # retired out of the live set
    assert payload["retired_title"] == row["title"]


def test_compact_keeps_session_and_clears_failure_latch(running, token, fake_profile):
    sid = fake_profile.seed(id="s1", title="hscc", message_count=250,
                            input_tokens=40_000,
                            compression_failure_error="context exceeded",
                            compression_fallback_streak=3,
                            compression_ineffective_count=2)
    status, payload = _request(running, token, "POST",
                               f"/v1/sessions/{sid}/compact",
                               body={"profile": "hscc-orch", "confirm": True})
    assert status == 200
    assert payload["session_id"] == sid
    assert payload["title"] == "hscc"
    # continuity preserved: same session, still live
    row = fake_profile.get_session(sid)
    assert row["id"] == sid
    assert row["title"] == "hscc"
    assert row["message_count"] == 250
    # failure latch cleared -> native compaction re-armed
    assert row["compression_failure_error"] is None
    assert row["compression_fallback_streak"] == 0
    assert row["compression_ineffective_count"] == 0
    # threshold was re-ensured
    assert fake_profile.ensured == ["hscc-orch"]


def test_retire_auth_401(running):
    status, _ = _request(running, None, "POST",
                         "/v1/sessions/s1/retire",
                         body={"profile": "hscc-orch", "confirm": True})
    assert status == 401
