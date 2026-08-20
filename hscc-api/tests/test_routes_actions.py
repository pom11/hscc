"""Unit tests for hscc-api Phase A4 — mutating (confirm-gated) endpoints.

The suite is hermetic: every backing call is stubbed via monkeypatch on the
``_backing_*`` module functions, so NO test ever creates a card, merges a
branch, applies a template, or stops a container against the live
cluster/kanban/git. Handlers are driven over real loopback HTTP (loopback port
0) exactly like A1/A2/A3, so auth and the route dispatcher are exercised
end-to-end.

Coverage required by the card, per endpoint:
  * missing ``confirm`` -> 409 AND the backing function was NOT called;
  * ``confirm: true``  -> backing called with the right args;
  * missing required field -> 400;
  * auth enforced -> 401;
  * merge endpoint: a FAILED merge must NOT close the card and must not return
    2xx.
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


def _project(name="hscc", repo="/tmp/repo", board="default"):
    return types.SimpleNamespace(name=name, repo=repo, board=board)


_OUTCOME_OK = "merged wt/t_abc123 into main and pushed"


@pytest.fixture
def fakes(monkeypatch):
    """Install hermetic fakes for every ``_backing_*`` seam.

    Returns a dict keyed by the backing-function name (e.g. ``"create_task"``)
    so a test can mutate one specific fake per case (e.g. to record whether it
    was called, or to force a failure).
    """
    state = {
        "create_task_calls": [],
        "do_apply_calls": [],
        "close_calls": [],
        "template_calls": [],
        "stop_calls": [],
    }
    b = {
        "create_task": lambda board, title, assignee=None, body=None, _kdb=None: (
            state["create_task_calls"].append(
                (board, title, assignee, body)
            ) or "t_new1"
        ),
        "resolve_card": lambda card_id, ctx: (_card(cid=card_id), _project(), "wt/" + card_id),
        "is_merged": lambda repo, branch, base="main": False,
        "do_apply": lambda repo, branch, base="main": (
            state["do_apply_calls"].append((repo, branch, base)) or _OUTCOME_OK
        ),
        "close_card": lambda card_id, board: (
            state["close_calls"].append((card_id, board)) or True
        ),
        "template_apply": lambda name, force_recreate=False: (
            state["template_calls"].append((name, force_recreate))
            or {"template": name, "steps": [], "success": True}
        ),
        "stop": lambda container_id: (
            state["stop_calls"].append(container_id)
            or {"success": True, "output": "stopped"}
        ),
    }
    _install(monkeypatch, b)
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


def _post(running, token, path="/v1/cards", body=None, method="POST"):
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


# --------------------------------------------------------------------------- #
# POST /v1/cards
# --------------------------------------------------------------------------- #

def test_cards_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards",
                            body={"board": "default", "title": "Hi"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["create_task_calls"] == []  # backing NOT called


def test_cards_confirm_false_is_409(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards",
                            body={"board": "default", "title": "Hi", "confirm": False})
    assert status == 409
    assert fakes["create_task_calls"] == []


def test_cards_confirm_true_calls_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards", body={
        "board": "default", "title": "Hi", "assignee": "researcher-a",
        "body": "do it", "confirm": True,
    })
    assert status == 200
    assert payload["id"] == "t_new1"
    assert payload["message"]
    assert fakes["create_task_calls"] == [
        ("default", "Hi", "researcher-a", "do it")
    ]


def test_cards_missing_required_field_400(running, token, fakes):
    status, payload = _post(running, token, "/v1/cards",
                            body={"board": "default", "confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["create_task_calls"] == []


def test_cards_auth_401(running, fakes):
    status, payload = _post(running, token=None, path="/v1/cards",
                            body={"board": "default", "title": "Hi", "confirm": True})
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert fakes["create_task_calls"] == []


# --------------------------------------------------------------------------- #
# POST /v1/review/{card_id}/merge
# --------------------------------------------------------------------------- #

def _merge_body():
    return {"confirm": True}


def test_merge_missing_confirm_409_no_close(running, token, fakes):
    status, payload = _post(running, token, "/v1/review/t_abc123/merge", body={})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["do_apply_calls"] == []
    assert fakes["close_calls"] == []


def test_merge_confirm_true_merges_and_closes(running, token, fakes):
    status, payload = _post(running, token, "/v1/review/t_abc123/merge",
                            body=_merge_body())
    assert status == 200
    assert payload["merged"] is True
    assert payload["card_closed"] is True
    assert payload["message"]
    # Backing called: resolve + is_merged + do_apply + close.
    assert fakes["do_apply_calls"] == [("/tmp/repo", "wt/t_abc123", "main")]
    assert fakes["close_calls"] == [("t_abc123", "default")]


def test_merge_failed_merge_does_not_close_card(running, token, fakes, monkeypatch):
    """A FAILED merge must NOT close the card and must not return 2xx."""
    _install(monkeypatch, {
        "do_apply": lambda repo, branch, base="main": (
            fakes["do_apply_calls"].append((repo, branch, base))
            or "merge failed: conflict merging wt/t_abc123 into main"
        ),
    })
    status, payload = _post(running, token, "/v1/review/t_abc123/merge",
                            body=_merge_body())
    assert status == 502
    assert payload["error"]["code"] == "merge_failed"
    assert "conflict" in payload["error"]["message"]
    assert fakes["do_apply_calls"]  # merge WAS attempted
    assert fakes["close_calls"] == []  # card NOT closed


def test_merge_partial_push_failed_does_not_close(running, token, fakes, monkeypatch):
    """A merge that landed but whose push failed is PARTIAL — do not close."""
    _install(monkeypatch, {
        "do_apply": lambda repo, branch, base="main": (
            fakes["do_apply_calls"].append((repo, branch, base))
            or "merge done but push failed: run `git push origin main` by hand"
        ),
    })
    status, payload = _post(running, token, "/v1/review/t_abc123/merge",
                            body=_merge_body())
    assert status == 502
    assert payload["error"]["code"] == "merge_failed"
    assert fakes["close_calls"] == []


def test_merge_already_landed_refuses_no_close(running, token, fakes, monkeypatch):
    """Already-merged branch: refuse (no re-merge, no close) — mirror cmd_review."""
    _install(monkeypatch, {"is_merged": lambda repo, branch, base="main": True})
    status, payload = _post(running, token, "/v1/review/t_abc123/merge",
                            body=_merge_body())
    assert status == 409
    assert payload["error"]["code"] == "already_landed"
    assert fakes["do_apply_calls"] == []
    assert fakes["close_calls"] == []


def test_merge_unresolvable_card_404(running, token, fakes, monkeypatch):
    def raises(card_id, ctx):
        raise routes_actions._review_cmd.ReviewError("no card")
    _install(monkeypatch, {"resolve_card": raises})
    status, payload = _post(running, token, "/v1/review/ghost/merge",
                            body=_merge_body())
    assert status == 404
    assert payload["error"]["code"] == "not_found"
    assert fakes["close_calls"] == []


def test_merge_close_fails_warns_but_merged_true(running, token, fakes, monkeypatch):
    """Merge landed but archive failed -> 200 (merge IS done) but card_closed False."""
    _install(monkeypatch, {"close_card": lambda card_id, board: False})
    status, payload = _post(running, token, "/v1/review/t_abc123/merge",
                            body=_merge_body())
    assert status == 200
    assert payload["merged"] is True
    assert payload["card_closed"] is False
    assert payload.get("warning")


def test_merge_missing_card_id_400(running, token, fakes):
    status, payload = _post(running, token, "/v1/review//merge", body=_merge_body())
    assert status == 404  # "" card_id does not match the ?P<card_id>[^/]+ regex
    # A truly missing card_id in the path cannot hit the handler; the empty
    # path falls through as no-route. The ``missing card_id`` 400 branch is
    # exercised in a direct-handler test below.


def test_merge_missing_card_id_400_direct(running, token, fakes):
    # Direct handler call with no card_id in query -> the handler raises 400.
    # (The real route regex guarantees the dispatcher always supplies card_id,
    # but the handler must be safe on its own.)
    with pytest.raises(api_server.ApiError) as excinfo:
        routes_actions.handle_merge_card(
            running.server, running.server.ctx, {},
            json.dumps(_merge_body()).encode(),
        )
    assert excinfo.value.status == 400
    assert excinfo.value.code == "bad_request"


def test_merge_auth_401(running, fakes):
    status, payload = _post(running, token=None, path="/v1/review/t_abc123/merge",
                            body=_merge_body())
    assert status == 401
    assert fakes["do_apply_calls"] == []
    assert fakes["close_calls"] == []


# --------------------------------------------------------------------------- #
# POST /v1/template/apply
# --------------------------------------------------------------------------- #

def test_template_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/template/apply",
                            body={"name": "dev"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["template_calls"] == []


def test_template_confirm_true_calls_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/template/apply", body={
        "name": "dev", "confirm": True,
    })
    assert status == 200
    assert payload["message"]
    assert payload["success"] is True
    assert fakes["template_calls"] == [("dev", False)]


def test_template_force_recreate_flag(running, token, fakes):
    status, payload = _post(running, token, "/v1/template/apply", body={
        "name": "dev", "force_recreate": True, "confirm": True,
    })
    assert status == 200
    assert fakes["template_calls"] == [("dev", True)]


def test_template_missing_name_400(running, token, fakes):
    status, payload = _post(running, token, "/v1/template/apply",
                            body={"confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["template_calls"] == []


def test_template_blocked_not_2xx(running, token, fakes, monkeypatch):
    """A BLOCKED/partial apply (success False) must NOT be a 2xx success."""
    _install(monkeypatch, {
        "template_apply": lambda name, force_recreate=False: (
            fakes["template_calls"].append((name, force_recreate))
            or {"status": "blocked", "success": False,
                "error": "preflight check failed"}
        ),
    })
    status, payload = _post(running, token, "/v1/template/apply",
                            body={"name": "dev", "confirm": True})
    assert status == 502
    assert payload["error"]["code"] == "apply_failed"
    assert "preflight" in payload["error"]["message"]


def test_template_auth_401(running, fakes):
    status, payload = _post(running, token=None, path="/v1/template/apply",
                            body={"name": "dev", "confirm": True})
    assert status == 401
    assert fakes["template_calls"] == []


# --------------------------------------------------------------------------- #
# POST /v1/cluster/stop
# --------------------------------------------------------------------------- #

def test_stop_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cluster/stop",
                            body={"container_id": "abc"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["stop_calls"] == []


def test_stop_confirm_true_calls_backing(running, token, fakes):
    status, payload = _post(running, token, "/v1/cluster/stop", body={
        "container_id": "abc123", "confirm": True,
    })
    assert status == 200
    assert payload["message"]
    assert payload["container_id"] == "abc123"
    assert fakes["stop_calls"] == ["abc123"]


def test_stop_missing_container_id_400(running, token, fakes):
    status, payload = _post(running, token, "/v1/cluster/stop",
                            body={"confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["stop_calls"] == []


def test_stop_failure_not_2xx(running, token, fakes, monkeypatch):
    _install(monkeypatch, {
        "stop": lambda container_id: (
            fakes["stop_calls"].append(container_id)
            or {"success": False, "error": "no such container"}
        ),
    })
    status, payload = _post(running, token, "/v1/cluster/stop",
                            body={"container_id": "zzz", "confirm": True})
    assert status == 502
    assert payload["error"]["code"] == "stop_failed"
    assert "no such container" in payload["error"]["message"]


def test_stop_auth_401(running, fakes):
    status, payload = _post(running, token=None, path="/v1/cluster/stop",
                            body={"container_id": "abc", "confirm": True})
    assert status == 401
    assert fakes["stop_calls"] == []


# --------------------------------------------------------------------------- #
# Guard rails: validate/other methods never reach the mutating handlers
# --------------------------------------------------------------------------- #

def test_actions_not_reachable_via_get(running, token, fakes):
    """A GET to a mutating path must never invoke the mutating handler.

    There are two cases:
      * mutating-ONLY paths (/v1/template/apply, /v1/cluster/stop,
        /v1/review/{id}/merge) have no GET route -> 405 method_not_allowed;
      * /v1/cards has a legitimate GET read route (A3) -> that read handler,
        NOT the POST dispatch handler, serves it (200 with the read shape).
    In no case may any mutating backing call fire.
    """
    # Mutating-only paths: GET is not routed -> 405 (never the handler).
    for path in ("/v1/review/t_abc123/merge", "/v1/template/apply",
                 "/v1/cluster/stop"):
        status, payload = _post(running, token, path, body=None, method="GET")
        assert status == 405, f"{path}: expected 405 got {status}"
        assert payload["error"]["code"] == "method_not_allowed"

    # /v1/cards has a GET read route (A3) that must serve it, not dispatch.
    status, payload = _post(running, token, "/v1/cards", body=None, method="GET")
    assert status == 200
    assert "cards" in payload  # read shape, not the dispatch {id:, message:} shape

    # No mutating backing call happened anywhere.
    assert fakes["create_task_calls"] == []
    assert fakes["do_apply_calls"] == []
    assert fakes["close_calls"] == []
    assert fakes["template_calls"] == []
    assert fakes["stop_calls"] == []
