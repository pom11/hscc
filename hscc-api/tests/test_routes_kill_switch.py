"""Unit tests for the kill-switch endpoints (routes_kanban running/kill).

Hermetic: every backing call is stubbed via monkeypatch on the ``_backing_*``
module functions, so NO test scans the live kanban DB or signals a real
process. Handlers are driven over real loopback HTTP (port 0).

Coverage per endpoint:
  * GET  /v1/kanban/running -> 200 + expected shape + speak, names each running
    task with pid + host_local.
  * POST /v1/kanban/task/{id}/kill -> 409 confirm_required WITHOUT confirm AND
    the backing kill was NOT called; confirm:true -> backing called; unknown
    task -> 404; auth enforced (401).
  * The kill response reports what actually died (termination dict surfaced).
"""

import http.client
import json
import threading
import types

import pytest

import api_server
import routes_kanban


def _kill_result(task_id="t_run1", pid=4242, host_local=True, terminated=True,
                 sigkill=False):
    return {
        "found": True,
        "task": {"board": "default", "id": task_id, "title": "Runaway train",
                 "assignee": "worker", "pid": pid, "host_local": host_local},
        "termination": {
            "prev_pid": pid, "host_local": host_local,
            "termination_attempted": True, "terminated": terminated,
            "sigkill": sigkill,
        },
    }


@pytest.fixture
def fakes(monkeypatch):
    state = {"kill_calls": []}
    b = {
        "list_running": lambda: {
            "boards": ["default"],
            "tasks": [
                {"board": "default", "id": "t_run1", "title": "Runaway train",
                 "assignee": "worker", "status": "running", "pid": 4242,
                 "host_local": True, "started_at": None},
            ],
            "errors": [],
            "count": 1,
        },
        "kill_running": lambda task_id: (
            state["kill_calls"].append(task_id) or _kill_result(task_id)
        ),
    }
    _install(monkeypatch, b)
    return state


def _install(monkeypatch, backing: dict):
    for name, fn in backing.items():
        monkeypatch.setattr(routes_kanban, f"_backing_{name}", fn)


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
# GET /v1/kanban/running
# --------------------------------------------------------------------------- #

def test_running_200(running, token):
    status, payload = _req(running, token, "/v1/kanban/running")
    assert status == 200
    assert payload["count"] == 1
    t = payload["tasks"][0]
    assert t["id"] == "t_run1"
    assert t["pid"] == 4242
    assert t["host_local"] is True
    assert payload["speak"] == "1 running task."


def test_running_none_200(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"list_running": lambda: {
        "boards": [], "tasks": [], "errors": [], "count": 0}})
    status, payload = _req(running, token, "/v1/kanban/running")
    assert status == 200
    assert payload["count"] == 0
    assert payload["speak"] == "No running tasks."


def test_running_auth_401(running):
    status, payload = _req(running, None, "/v1/kanban/running")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# POST /v1/kanban/task/{id}/kill (confirm-gated)
# --------------------------------------------------------------------------- #

def test_kill_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/kanban/task/t_run1/kill",
                           body={}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["kill_calls"] == []


def test_kill_confirm_true_backs_and_reports(running, token, fakes):
    status, payload = _req(running, token, "/v1/kanban/task/t_run1/kill",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["id"] == "t_run1"
    assert fakes["kill_calls"] == ["t_run1"]
    # reports what actually died:
    assert payload["task"]["pid"] == 4242
    assert payload["termination"]["terminated"] is True
    assert payload["termination"]["sigkill"] is False
    assert payload["speak"] == "Stopped task t_run1 (pid 4242)."


def test_kill_sigkill_reported(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"kill_running": lambda task_id:
                           _kill_result(sigkill=True)})
    status, payload = _req(running, token, "/v1/kanban/task/t_run1/kill",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["termination"]["sigkill"] is True
    assert "force-kill" in payload["speak"]


def test_kill_not_running_404(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"kill_running": lambda task_id: {
        "found": False, "task": None, "termination": None}})
    status, payload = _req(running, token, "/v1/kanban/task/ghost/kill",
                           body={"confirm": True}, method="POST")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_kill_missing_task_id_400(running, token, fakes):
    # no task_id captured -> handled with 400 by the handler
    status, payload = _req(running, token, "/v1/kanban/task//kill",
                           body={"confirm": True}, method="POST")
    assert status in (400, 404)


def test_kill_auth_401(running, fakes):
    status, payload = _req(running, None, "/v1/kanban/task/t_run1/kill",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["kill_calls"] == []


def test_kill_get_not_allowed(running, token, fakes):
    status, payload = _req(running, token, "/v1/kanban/task/t_run1/kill",
                           method="GET")
    assert status == 405
    assert fakes["kill_calls"] == []


# --------------------------------------------------------------------------- #
# the board-pin observable (t_147201fa) — the panel must say what it cannot stop
# --------------------------------------------------------------------------- #

_PIN_MSG = ("kanban board-pin conflict: HERMES_KANBAN_DB='/pins/alpha/kanban.db' "
            "outranks the explicit board='beta' this caller asked for")


def test_running_surfaces_pin_conflicts(running, token, fakes, monkeypatch):
    """The listing passes the conflict through, per row and per panel.

    A row shown as un-killable without a reason is the trap the card names; the
    panel now carries the sentence the verb will raise.
    """
    _install(monkeypatch, {"list_running": lambda: {
        "boards": ["alpha", "beta"],
        "tasks": [
            {"board": "alpha", "id": "t_a", "title": "mine", "assignee": "worker",
             "status": "running", "pid": 1, "host_local": True,
             "started_at": None, "killable": True, "pin_conflict": None},
            {"board": "beta", "id": "t_b", "title": "not mine",
             "assignee": "worker", "status": "running", "pid": 2,
             "host_local": True, "started_at": None, "killable": False,
             "pin_conflict": _PIN_MSG},
        ],
        "errors": [], "pin_conflicts": [_PIN_MSG], "count": 2,
    }})
    status, payload = _req(running, token, "/v1/kanban/running")
    assert status == 200
    assert payload["count"] == 2
    assert payload["pin_conflicts"] == [_PIN_MSG], payload
    by_id = {t["id"]: t for t in payload["tasks"]}
    assert by_id["t_a"]["killable"] is True
    assert by_id["t_b"]["killable"] is False
    assert by_id["t_b"]["pin_conflict"] == _PIN_MSG


def test_kill_refused_under_a_pin_is_a_failed_kill_naming_both(
        running, token, fakes, monkeypatch):
    """The refusal must arrive as a failed kill naming both boards — never as
    404 "not running" (the card IS running; we refuse to touch it) and never as
    a 200 that reads like a stopped worker.
    """
    from hscc_daemon import board_pin

    def refusing(task_id):
        raise board_pin.BoardPinConflict(_PIN_MSG)

    _install(monkeypatch, {"kill_running": refusing})
    status, payload = _req(running, token, "/v1/kanban/task/t_b/kill",
                           body={"confirm": True}, method="POST")
    assert status == 502, payload
    assert payload["error"]["code"] == "kill_failed"
    assert "beta" in payload["error"]["message"], payload
    assert "/pins/alpha/kanban.db" in payload["error"]["message"], payload
