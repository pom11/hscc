"""Unit tests for hscc-api live agent activity feed (routes_activity.py).

Card t_308fce41: "iOS + api: live agent activity feed". A flight recorder
across the fleet — who is running, which tool they just called, on which
card; tap a line to trace.

The suite is hermetic: the running-card source and each profile's state.db
are faked with REAL in-memory SQLite (so the module's SQL and row shaping run
against a genuine engine), and ``_backing_running_tasks`` /
``_backing_open_profile_db`` are monkeypatched so no test reads the live kanban
board or a live Hermes state.db. Handlers are driven over real loopback HTTP
(port 0) so auth + the route dispatcher are exercised end-to-end.

Coverage required by the card:
  * GET /v1/activity/feed returns running-card entries ("running" kind) for
    every running card, even a profile with no tool calls in the window;
  * a running profile's recent tool calls appear as "tool_call" entries tied
    to its card (tool name parsed from tool_calls JSON function.name and the
    tool_name column), newest first, limit-respecting;
  * profiles with no state.db degrade gracefully (no crash, still listed as
    running);
  * limit validation (bad / negative -> 400; cap at 200);
  * speak present + no agents running -> honest empty feed;
  * auth enforced (401) on the endpoint.
"""
import json
import sqlite3
import sys
import threading
import types

import pytest

import api_server
import routes_activity

# The messages table's columns (subset the module reads). Mirrors the real
# hermes state.db schema but keeps only what the activity feed queries.
_MESSAGE_COLS = [
    "id INTEGER PRIMARY KEY AUTOINCREMENT",
    "session_id TEXT NOT NULL",
    "role TEXT NOT NULL",
    "content TEXT",
    "tool_call_id TEXT",
    "tool_calls TEXT",
    "tool_name TEXT",
    "timestamp REAL NOT NULL",
]


class _FakeSessionDB:
    """Live in-memory SQLite stand-in for a profile's state.db.

    Exposes a ``messages`` table (the only table the feed queries) plus the
    small ``SessionDB`` surface the module uses (``close``). ``closed`` is set
    so tests can assert the module never leaks connections.
    """

    def __init__(self):
        self.closed = False
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"CREATE TABLE messages ({', '.join(_MESSAGE_COLS)})")
        self._conn.commit()
        self._seq = 0

    def seed_message(self, *, role="assistant", tool_name=None, tool_calls=None,
                     timestamp=1000.0, session_id="sid1"):
        self._seq += 1
        cur = self._conn.execute(
            "INSERT INTO messages (session_id, role, tool_name, tool_calls, "
            "timestamp) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, tool_name,
             json.dumps(tool_calls) if tool_calls is not None else None,
             float(timestamp)),
        )
        self._conn.commit()
        return cur.lastrowid

    def close(self):
        self.closed = True


def _fake_db_with_tools(*rows):
    """A _FakeSessionDB pre-seeded with the given messages rows."""
    db = _FakeSessionDB()
    for kw in rows:
        db.seed_message(**kw)
    return db


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
def fake_backing(monkeypatch):
    """Install fake running-cards + per-profile state.db seams.

    ``tasks`` is the running-card list the endpoint should see; ``dbs`` maps
    profile name -> _FakeSessionDB (or None to simulate an unresolvable
    profile's state.db).
    """
    state = types.SimpleNamespace(tasks=[], dbs={})

    monkeypatch.setattr(routes_activity, "_backing_running_tasks",
                        lambda: {"boards": ["hscc"], "tasks": state.tasks,
                                 "errors": [], "count": len(state.tasks)})
    monkeypatch.setattr(routes_activity, "_backing_open_profile_db",
                        lambda profile, read_only=True: state.dbs.get(profile))

    state.set_tasks = lambda tasks_: setattr(state, "tasks", tasks_)
    state.set_db = lambda profile, db: state.dbs.__setitem__(profile, db)
    return state


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


_A_CARD = {
    "board": "hscc",
    "id": "t_abc",
    "title": "build the widget",
    "assignee": "ios-engineer",
    "status": "running",
    "pid": 1234,
    "host_local": True,
    "started_at": "2026-08-29T10:00:00+00:00",
}

# Two more running cards (distinct assignees) for feed-saturation tests.
_B_CARD = {
    "board": "hscc",
    "id": "t_def",
    "title": "fix the API",
    "assignee": "backend-engineer",
    "status": "running",
    "pid": 2345,
    "host_local": True,
    "started_at": "2026-08-29T11:00:00+00:00",
}

_C_CARD = {
    "board": "hscc",
    "id": "t_ghi",
    "title": "tune the cluster",
    "assignee": "orchestrator",
    "status": "running",
    "pid": 3456,
    "host_local": False,
    "started_at": "2026-08-29T12:00:00+00:00",
}


# --------------------------------------------------------------------------- #
# GET /v1/activity/feed
# --------------------------------------------------------------------------- #

def test_feed_shows_running_card_with_no_tool_calls(running, token, fake_backing):
    fake_backing.set_tasks([_A_CARD])
    status, payload = _request(running, token, "GET", "/v1/activity/feed")
    assert status == 200
    assert payload["running_count"] == 1
    assert payload["count"] == 1
    assert payload["profiles"] == ["ios-engineer"]
    e = payload["entries"][0]
    assert e["kind"] == "running"
    assert e["profile"] == "ios-engineer"
    assert e["card_id"] == "t_abc"
    assert e["card_title"] == "build the widget"
    assert e["tool"] is None
    assert "speak" in payload


def test_feed_tool_call_tied_to_card(running, token, fake_backing):
    fake_backing.set_tasks([_A_CARD])
    db = _fake_db_with_tools(
        dict(tool_name="terminal", timestamp=2000.0),
        dict(tool_name=None,
             tool_calls=[{"function": {"name": "kanban_show"}}],
             timestamp=1900.0),
    )
    fake_backing.set_db("ios-engineer", db)
    status, payload = _request(running, token, "GET", "/v1/activity/feed")
    assert status == 200
    kinds = [e["kind"] for e in payload["entries"]]
    assert "tool_call" in kinds
    # The tool_call entries are tied to the running card.
    tc = [e for e in payload["entries"] if e["kind"] == "tool_call"]
    assert tc[0]["card_id"] == "t_abc"
    assert tc[0]["card_title"] == "build the widget"
    assert tc[0]["session_id"] == "sid1"
    # Newest first: the timestamp-2000 tool call leads the tool_call group.
    tool_names = [e["tool"] for e in tc]
    assert tool_names[0] == "terminal"       # tool_name column wins
    assert tool_names[1] == "kanban_show"    # parsed from tool_calls JSON
    # speak is present and meaningful.
    assert payload["speak"]


def test_feed_tool_name_from_tool_calls_json_shortens_namespace(running, token,
                                                                fake_backing):
    fake_backing.set_tasks([_A_CARD])
    db = _fake_db_with_tools(
        dict(tool_calls=[{"function": {"name": "and_this_is_a_very.long.namespace"}}],
             timestamp=2000.0),
    )
    fake_backing.set_db("ios-engineer", db)
    status, payload = _request(running, token, "GET", "/v1/activity/feed")
    tc = [e for e in payload["entries"] if e["kind"] == "tool_call"]
    assert tc[0]["tool"] == "and_this_is_a_very"  # dotted names reduced to head


def test_feed_profile_without_state_db_still_shows_running(running, token,
                                                           fake_backing):
    """An unresolvable profile's state.db must not drop its running row."""
    fake_backing.set_tasks([_A_CARD])
    fake_backing.set_db("ios-engineer", None)  # no state.db
    status, payload = _request(running, token, "GET", "/v1/activity/feed")
    assert status == 200
    assert payload["running_count"] == 1
    assert payload["count"] == 1
    assert payload["entries"][0]["kind"] == "running"


def test_feed_empty_when_no_one_running(running, token, fake_backing):
    fake_backing.set_tasks([])
    status, payload = _request(running, token, "GET", "/v1/activity/feed")
    assert status == 200
    assert payload["running_count"] == 0
    assert payload["count"] == 0
    assert payload["entries"] == []
    assert payload["speak"] == "No agents currently running."


def test_feed_limit_caps_entries(running, token, fake_backing):
    fake_backing.set_tasks([_A_CARD])
    db = _fake_db_with_tools(
        *[dict(tool_name=f"tool_{i}", timestamp=float(1000 + i))
          for i in range(10)],
    )
    fake_backing.set_db("ios-engineer", db)
    status, payload = _request(running, token, "GET",
                               "/v1/activity/feed?limit=3")
    assert status == 200
    # `limit` caps TOOL-CALL entries only; the single running row is always
    # present, so count = 1 running + <=3 tool = <= 4.
    assert payload["count"] <= 4
    assert len(payload["entries"]) <= 4
    assert len([e for e in payload["entries"] if e["kind"] == "running"]) == 1


def test_feed_running_rows_survive_limit_cap(running, token, fake_backing):
    """A saturated timeline must not truncate running rows.

    Regression for the on-screen count disagreement: ``speak``/``running_count``
    count all running tasks, so if ``limit`` could drop the "running" rows the
    operator would see a header claiming "N running tasks" next to a list with
    fewer (or zero) Running badges. Every running row must always be present.
    """
    # 3 running cards; each profile floods the timeline with tool calls so the
    # cap would previously have pushed all running rows off the page.
    cards = [_A_CARD, _B_CARD, _C_CARD]
    fake_backing.set_tasks(cards)
    for card in cards:
        db = _fake_db_with_tools(
            *[dict(tool_name=f"tool_{i}", timestamp=float(1000 + i))
              for i in range(20)],
        )
        fake_backing.set_db(card["assignee"], db)
    status, payload = _request(running, token, "GET",
                               "/v1/activity/feed?limit=10")
    assert status == 200
    visible_running = [e for e in payload["entries"] if e["kind"] == "running"]
    # Every running card's row is present, so running_count never over-states
    # what the operator actually sees.
    assert len(visible_running) == payload["running_count"] == 3


def test_feed_limit_bad_400(running, token, fake_backing):
    status, payload = _request(running, token, "GET",
                               "/v1/activity/feed?limit=abc")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_feed_limit_negative_400(running, token, fake_backing):
    status, payload = _request(running, token, "GET",
                               "/v1/activity/feed?limit=-5")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_feed_auth_401(running):
    status, _ = _request(running, None, "GET", "/v1/activity/feed")
    assert status == 401


def test_feed_closes_profile_dbs(running, token, fake_backing):
    fake_backing.set_tasks([_A_CARD])
    db = _fake_db_with_tools(dict(tool_name="terminal", timestamp=1000.0))
    fake_backing.set_db("ios-engineer", db)
    _request(running, token, "GET", "/v1/activity/feed")
    assert db.closed is True
