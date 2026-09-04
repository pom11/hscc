"""Unit tests for hscc-api card-actions endpoints.

Covers the four card-actions (mutating, confirm-gated) endpoints added in
Phase A4:
  * POST   /v1/cards/{id}/comment -> add_card_comment
  * POST   /v1/cards/{id}/block   -> block_card
  * POST   /v1/cards/{id}/close   -> close_card
  * PATCH  /v1/cards/{id}         -> edit_card (assignee only)

The suite is hermetic: every backing call is stubbed via monkeypatch on the
``routes_actions._backing_*`` module functions, and card resolution (``find_card``)
is stubbed too, so NO test ever touches the live board. Handlers are driven over
real loopback HTTP (loopback port 0) exactly like ``test_routes_actions.py``.

Coverage required per endpoint:
  * missing ``confirm`` -> 409 AND the backing function was NOT called;
  * ``confirm: true``   -> backing called with the right args, correct payload;
  * missing required field -> 400;
  * auth enforced -> 401;
  * card not found -> 404 ``not_found`` (find_card stubbed to return None).
"""

import json
import types

import pytest

import api_server
import routes_actions


# --------------------------------------------------------------------------- #
# Fixtures: a running server + hermetic fakes for every backing call
# --------------------------------------------------------------------------- #

def _card(cid="t_abc123", title="Do a thing", status="review", board="default",
          branch="wt/t_abc123", body=""):
    return {
        "id": cid, "title": title, "status": status, "board": board,
        "branch": branch, "body": body, "created_at": 1000,
        "workspace_path": "/tmp/repo",
    }


@pytest.fixture
def fakes(monkeypatch):
    """Install hermetic fakes for every ``_backing_*`` seam plus ``find_card``.

    Returns a dict keyed by the backing-function name so a test can mutate one
    specific fake per case (e.g. to record whether it was called, or to force
    a failure). ``find_card`` is stubbed to return the default card so every
    route resolves by default; tests that need a "not found" case override it.
    """
    state = {
        "find_card_calls": [],
        "add_comment_calls": [],
        "block_calls": [],
        "complete_calls": [],
        "edit_calls": [],
    }
    b = {
        # Card resolution is not a _backing_* seam; handled via find_card.
        "add_comment": lambda card_id, body, author=None, _kdb=None: (
            state["add_comment_calls"].append((card_id, body, author))
            or 42
        ),
        "block_card": lambda card_id, reason=None, kind=None, _kdb=None: (
            state["block_calls"].append((card_id, reason, kind))
            or True
        ),
        "complete_card": lambda card_id, result=None, _kdb=None: (
            state["complete_calls"].append((card_id, result))
            or True
        ),
        "edit_card": lambda card_id, assignee=None, _kdb=None: (
            state["edit_calls"].append((card_id, assignee))
            or True
        ),
    }
    _install(monkeypatch, b)
    # Default card resolution: every route finds the card unless a test
    # overrides find_card to return None.
    monkeypatch.setattr(
        routes_actions._kanban, "find_card",
        lambda card_id, _kdb=None, boards=None: (
            state["find_card_calls"].append(card_id) or _card(cid=card_id)
        ),
    )
    return state


def _install(monkeypatch, backing: dict):
    """Point each ``_backing_*`` module function at the given fake."""
    for name, fn in backing.items():
        monkeypatch.setattr(routes_actions, f"_backing_{name}", fn)


@pytest.fixture
def running(tmp_path, fakes):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path), addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    import threading

    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _request(running, token, path, body=None, method="POST"):
    import http.client

    conn = http.client.HTTPConnection(running.host, running.port, timeout=5)
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
    try:
        payload: dict = json.loads(data) if data else {}
    except ValueError:
        payload = {"raw": data}
    return resp.status, payload


def _post(running, token, path, body=None):
    return _request(running, token, path, body=body, method="POST")


# --------------------------------------------------------------------------- #
# POST /v1/cards/{card_id}/comment
# --------------------------------------------------------------------------- #

def test_comment_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards/t_abc123/comment",
                            body={"body": "looks good"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["add_comment_calls"] == []  # backing NOT called


def test_comment_confirm_true_calls_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards/t_abc123/comment",
                            body={"body": "looks good", "author": "researcher-a",
                                  "confirm": True})
    assert status == 200
    assert payload["id"] == "t_abc123"
    assert payload["comment_id"] == 42
    assert payload["message"]
    assert fakes["add_comment_calls"] == [("t_abc123", "looks good", "researcher-a")]


def test_comment_missing_required_field_400(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards/t_abc123/comment",
                            body={"confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["add_comment_calls"] == []


def test_comment_missing_author_400(running, token, fakes):
    """Body present but author absent -> 400 (the DB hard-requires author)."""
    status, payload = _post(running, token, "/v1/cards/t_abc123/comment",
                            body={"body": "looks good", "confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["add_comment_calls"] == []  # backing NOT called


def test_comment_auth_401(running, fakes):
    status, payload = _post(running, token=None, path="/v1/cards/t_abc123/comment",
                            body={"body": "hi", "confirm": True})
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert fakes["add_comment_calls"] == []


def test_comment_card_not_found_404(running, token, fakes, monkeypatch):
    monkeypatch.setattr(routes_actions._kanban, "find_card",
                        lambda card_id, _kdb=None, boards=None: None)
    status, payload = _post(running, token, "/v1/cards/ghost/comment",
                            body={"body": "hi", "author": "r1", "confirm": True})
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    assert fakes["add_comment_calls"] == []


def test_comment_backing_failure_502(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"add_comment": lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("db locked"))})
    status, payload = _post(running, token, "/v1/cards/t_abc123/comment",
                            body={"body": "hi", "author": "r1", "confirm": True})
    assert status == 502
    assert payload["error"]["code"] == "comment_failed"


# --------------------------------------------------------------------------- #
# POST /v1/cards/{card_id}/block
# --------------------------------------------------------------------------- #

def test_block_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards/t_abc123/block",
                            body={"reason": "waiting on deps"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["block_calls"] == []  # backing NOT called


def test_block_confirm_true_calls_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards/t_abc123/block",
                            body={"reason": "waiting on deps", "kind": "external",
                                  "confirm": True})
    assert status == 200
    assert payload["id"] == "t_abc123"
    assert payload["blocked"] is True
    assert payload["message"]
    assert fakes["block_calls"] == [("t_abc123", "waiting on deps", "external")]


def test_block_missing_required_field_400(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards/t_abc123/block",
                            body={"confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["block_calls"] == []


def test_block_auth_401(running, fakes):
    status, payload = _post(running, token=None, path="/v1/cards/t_abc123/block",
                            body={"reason": "r", "confirm": True})
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert fakes["block_calls"] == []


def test_block_card_not_found_404(running, token, fakes, monkeypatch):
    monkeypatch.setattr(routes_actions._kanban, "find_card",
                        lambda card_id, _kdb=None, boards=None: None)
    status, payload = _post(running, token, "/v1/cards/ghost/block",
                            body={"reason": "r", "confirm": True})
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    assert fakes["block_calls"] == []


# --------------------------------------------------------------------------- #
# POST /v1/cards/{card_id}/close
# --------------------------------------------------------------------------- #

def test_close_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards/t_abc123/close",
                            body={"result": "all done"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["complete_calls"] == []  # backing NOT called


def test_close_confirm_true_calls_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards/t_abc123/close",
                            body={"result": "all done", "confirm": True})
    assert status == 200
    assert payload["id"] == "t_abc123"
    assert payload["closed"] is True
    assert payload["message"]
    assert fakes["complete_calls"] == [("t_abc123", "all done")]


def test_close_auth_401(running, fakes):
    status, payload = _post(running, token=None, path="/v1/cards/t_abc123/close",
                            body={"confirm": True})
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert fakes["complete_calls"] == []


def test_close_card_not_found_404(running, token, fakes, monkeypatch):
    monkeypatch.setattr(routes_actions._kanban, "find_card",
                        lambda card_id, _kdb=None, boards=None: None)
    status, payload = _post(running, token, "/v1/cards/ghost/close",
                            body={"confirm": True})
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    assert fakes["complete_calls"] == []


# --------------------------------------------------------------------------- #
# PATCH /v1/cards/{card_id}  (edit — assignee only)
# --------------------------------------------------------------------------- #

def test_edit_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _request(running, token, "/v1/cards/t_abc123",
                               body={"assignee": "researcher-b"}, method="PATCH")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["edit_calls"] == []  # backing NOT called


def test_edit_confirm_true_calls_backing(running, token, fakes):
    status, payload = _request(running, token, "/v1/cards/t_abc123",
                               body={"assignee": "researcher-b", "confirm": True},
                               method="PATCH")
    assert status == 200
    assert payload["id"] == "t_abc123"
    assert payload["edited"] is True
    assert payload["assignee"] == "researcher-b"
    assert payload["message"]
    assert fakes["edit_calls"] == [("t_abc123", "researcher-b")]


def test_edit_missing_required_field_400(running, token, fakes):
    status, payload = _request(running, token, "/v1/cards/t_abc123",
                               body={"confirm": True}, method="PATCH")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["edit_calls"] == []


def test_edit_auth_401(running, fakes):
    status, payload = _request(running, token=None, path="/v1/cards/t_abc123",
                               body={"assignee": "researcher-b", "confirm": True},
                               method="PATCH")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert fakes["edit_calls"] == []


def test_edit_card_not_found_404(running, token, fakes, monkeypatch):
    monkeypatch.setattr(routes_actions._kanban, "find_card",
                        lambda card_id, _kdb=None, boards=None: None)
    status, payload = _request(running, token, "/v1/cards/ghost",
                               body={"assignee": "researcher-b", "confirm": True},
                               method="PATCH")
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    assert fakes["edit_calls"] == []
