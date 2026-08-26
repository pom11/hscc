"""Unit tests for the kanban board-hygiene endpoints (blocked / recover / stale).

Hermetic: every backing call is stubbed via monkeypatch on the ``_backing_*``
module functions, so NO test scans the live kanban DB or mutates a real card.
Handlers are driven over real loopback HTTP (port 0) so auth and the router
are exercised end-to-end.

Coverage per endpoint:
  * reads -> 200 + expected shape + non-empty ``speak``;
  * recover (mutation) -> 409 ``confirm_required`` without ``confirm`` AND the
    backing call was NOT made; ``confirm: true`` -> backing called; unknown
    card -> 404;
  * auth enforced (401 without a token);
  * error envelope shape ({error: {code, message, speak}}).
"""

import http.client
import json
import threading
import types

import pytest

import api_server
import routes_kanban


@pytest.fixture
def fakes(monkeypatch):
    state = {
        "recover_calls": [],
    }
    b = {
        "list_blocked": lambda: _blocked_result(),
        "recover_blocked": lambda task_id, reason=None: (
            state["recover_calls"].append((task_id, reason))
            or ("default", True)
        ),
        "list_stale": lambda older_than=None, board=None: _stale_result(older_than),
    }
    _install(monkeypatch, b)
    return state


def _install(monkeypatch, backing: dict):
    for name, fn in backing.items():
        monkeypatch.setattr(routes_kanban, f"_backing_{name}", fn)


def _blocked_result():
    return {
        "boards": 1,
        "tasks": [
            {"board": "default", "id": "t_blk1", "status": "blocked",
             "assignee": "researcher-a", "age_days": 2,
             "block_kind": "needs_input", "why": "waiting on the operator",
             "title": "Fix the thing", "comments": ["comment"]},
        ],
        "errors": [],
    }


def _stale_result(older_than):
    tasks = [
        {"board": "default", "id": "t_stale1", "status": "running",
         "assignee": "worker", "age_days": 3, "title": "Old task"},
    ]
    return {"boards": 1, "tasks": tasks, "errors": []}


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


def _req(running, token, path, body=None, method="GET"):
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode("utf-8")
    else:
        raw = None
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    payload: dict
    try:
        payload = json.loads(data) if data else {}
    except ValueError:
        payload = {"raw": data}
    return resp.status, payload


# --------------------------------------------------------------------------- #
# GET /v1/kanban/blocked
# --------------------------------------------------------------------------- #

def test_blocked_200(running, token):
    status, payload = _req(running, token, "/v1/kanban/blocked")
    assert status == 200
    assert payload["count"] == 1
    assert payload["boards"] == 1
    assert payload["tasks"][0]["id"] == "t_blk1"
    assert payload["tasks"][0]["why"]
    assert "error" not in payload
    assert payload["speak"] == "1 card blocked across 1 board."


def test_blocked_none_200(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"list_blocked": lambda: {
        "boards": 1, "tasks": [], "errors": [],
    }})
    status, payload = _req(running, token, "/v1/kanban/blocked")
    assert status == 200
    assert payload["count"] == 0
    assert payload["speak"] == "No blocked cards on any board."


def test_blocked_auth_401(running):
    status, payload = _req(running, None, "/v1/kanban/blocked")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# POST /v1/kanban/blocked/{id}/recover (confirm-gated)
# --------------------------------------------------------------------------- #

def test_recover_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/kanban/blocked/t_blk1/recover",
                           body={}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["recover_calls"] == []


def test_recover_confirm_true_backs(running, token, fakes):
    status, payload = _req(running, token, "/v1/kanban/blocked/t_blk1/recover",
                           body={"confirm": True, "reason": "looks safe"},
                           method="POST")
    assert status == 200
    assert payload["id"] == "t_blk1"
    assert payload["board"] == "default"
    assert payload["reason"] == "looks safe"
    assert payload["message"]
    assert payload["speak"]
    assert fakes["recover_calls"] == [("t_blk1", "looks safe")]


def test_recover_not_blocked_404(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"recover_blocked": lambda task_id, reason=None:
                           (None, False)})
    status, payload = _req(running, token, "/v1/kanban/blocked/ghost/recover",
                           body={"confirm": True}, method="POST")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_recover_auth_401(running, fakes):
    status, payload = _req(running, None,
                           "/v1/kanban/blocked/t_blk1/recover",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["recover_calls"] == []


# --------------------------------------------------------------------------- #
# GET /v1/kanban/stale
# --------------------------------------------------------------------------- #

def test_stale_200_default(running, token):
    status, payload = _req(running, token, "/v1/kanban/stale")
    assert status == 200
    from hscc_daemon import autodown
    assert payload["older_than"] == autodown.DEFAULT_STALE_DAYS
    assert payload["count"] == 1
    assert payload["tasks"][0]["id"] == "t_stale1"
    assert payload["speak"] == "1 stale card."


def test_stale_older_than_0(running, token, fakes, monkeypatch):
    calls = {}
    _install(monkeypatch, {
        "list_stale": lambda older_than=None, board=None: (
            calls.__setitem__("older_than", older_than)
            or _stale_result(older_than)
        ),
    })
    status, payload = _req(running, token, "/v1/kanban/stale?older_than=0")
    assert status == 200
    assert payload["older_than"] == 0
    assert calls["older_than"] == 0


def test_stale_bad_older_than_400(running, token, fakes):
    status, payload = _req(running, token, "/v1/kanban/stale?older_than=-1")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_stale_none_200(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"list_stale": lambda older_than=None, board=None: {
        "boards": 1, "tasks": [], "errors": []}})
    status, payload = _req(running, token, "/v1/kanban/stale")
    assert status == 200
    assert payload["count"] == 0
    assert payload["speak"] == "No stale cards."


def test_stale_auth_401(running):
    status, payload = _req(running, None, "/v1/kanban/stale")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# recover is POST-only
# --------------------------------------------------------------------------- #

def test_recover_not_reachable_via_get(running, token, fakes):
    status, payload = _req(running, token, "/v1/kanban/blocked/t_blk1/recover",
                           method="GET")
    assert status == 405
    assert payload["error"]["code"] == "method_not_allowed"
    assert fakes["recover_calls"] == []
