"""Unit tests for the ops endpoints (verify / daemon/status / triggers /
escalate / profiles) + fleet control (cluster up/down).

Hermetic: every backing call is stubbed via monkeypatch on the ``_backing_*``
module functions, so NO test runs a real verify scan, reads a live state dir,
or issues a real fleet up/down. Handlers are driven over real loopback HTTP
(port 0) so auth and the router are exercised end-to-end.

Coverage per endpoint:
  * reads -> 200 + expected shape + non-empty ``speak``;
  * mutations (cluster up/down) -> 409 ``confirm_required`` without ``confirm``
    AND the backing call was NOT made; ``confirm: true`` -> backing called;
    a backing failure is a non-2xx (never claims success);
  * auth enforced (401 without a token);
  * error envelope shape ({error: {code, message, speak}}).
"""

import http.client
import json
import threading
import types

import pytest

import api_server
import routes_ops


@pytest.fixture
def fakes(monkeypatch):
    state = {
        "up_calls": [],
        "down_calls": [],
        "triggers_run_calls": [],
        "escalate_run_calls": [],
    }
    b = {
        "verify": lambda: {
            "ok": True,
            "checks": [
                {"name": "plugins", "ok": True, "detail": "ok"},
                {"name": "daemon_streams", "ok": True, "detail": "ok"},
            ],
        },
        "daemon_status": lambda: {
            "daemon_running": True,
            "pid": 1234,
            "state": "running",
            "streams": {"dgx": {"ok": True, "timestamp": "t", "stream": "dgx"}},
        },
        "triggers": lambda: {
            "rules": [{"id": "r1", "trigger_type": "notify", "enabled": True}],
            "last_run": {"actions_fired": 1},
            "recent_events": [],
        },
        "triggers_run": lambda: (
            state["triggers_run_calls"].append(True)
            or {
                "rules": [{"id": "r1", "trigger_type": "notify", "enabled": True}],
                "last_run": {"actions_fired": 2, "timestamp": "t"},
                "recent_events": [{"event": "run"}],
            }
        ),
        "escalate": lambda: [
            {"task": "t_abc", "action": "escalate", "to": "architect",
             "category": "test-failure"},
        ],
        "escalate_run": lambda: (
            state["escalate_run_calls"].append(True)
            or [
                {"task": "t_abc", "action": "reassign", "to": "architect",
                 "category": "test-failure", "notified": True},
            ]
        ),
        "profiles": lambda: {
            "counts": {"researcher-a": 2, "architect": 1},
            "total_running": 3,
            "profiles": ["architect", "researcher-a"],
        },
        "cluster_up": lambda: (
            state["up_calls"].append(True)
            or {"success": True, "dry_run": False, "units": 4, "plan": [],
                "issued": []}
        ),
        "cluster_down": lambda: (
            state["down_calls"].append(True)
            or {"success": True, "output": "stopped all"}
        ),
    }
    _install(monkeypatch, b)
    return state


def _install(monkeypatch, backing: dict):
    for name, fn in backing.items():
        monkeypatch.setattr(routes_ops, f"_backing_{name}", fn)


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
# GET /v1/verify
# --------------------------------------------------------------------------- #

def test_verify_200(running, token):
    status, payload = _req(running, token, "/v1/verify")
    assert status == 200
    assert payload["ok"] is True
    assert len(payload["checks"]) == 2
    assert payload["speak"] == "All checks passed."
    assert all(c["name"] for c in payload["checks"])


def test_verify_reflects_failures(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"verify": lambda: {
        "ok": False,
        "checks": [
            {"name": "plugins", "ok": True, "detail": "ok"},
            {"name": "proxy", "ok": False, "detail": "down"},
        ],
    }})
    status, payload = _req(running, token, "/v1/verify")
    assert status == 200
    assert payload["ok"] is False
    assert "proxy" in payload["speak"]


def test_verify_auth_401(running):
    status, payload = _req(running, None, "/v1/verify")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# GET /v1/daemon/status
# --------------------------------------------------------------------------- #

def test_daemon_status_200(running, token):
    status, payload = _req(running, token, "/v1/daemon/status")
    assert status == 200
    assert payload["daemon_running"] is True
    assert payload["pid"] == 1234
    assert payload["state"] == "running"
    assert "dgx" in payload["streams"]
    assert payload["speak"]


def test_daemon_status_auth_401(running):
    status, payload = _req(running, None, "/v1/daemon/status")
    assert status == 401


# --------------------------------------------------------------------------- #
# GET /v1/triggers
# --------------------------------------------------------------------------- #

def test_triggers_200(running, token):
    status, payload = _req(running, token, "/v1/triggers")
    assert status == 200
    assert len(payload["rules"]) == 1
    assert payload["last_run"]["actions_fired"] == 1
    assert "recent_events" in payload
    assert payload["speak"]


def test_triggers_auth_401(running):
    status, payload = _req(running, None, "/v1/triggers")
    assert status == 401


# --------------------------------------------------------------------------- #
# GET /v1/escalate
# --------------------------------------------------------------------------- #

def test_escalate_200(running, token):
    status, payload = _req(running, token, "/v1/escalate")
    assert status == 200
    assert payload["count"] == 1
    assert payload["escalations"][0]["task"] == "t_abc"
    assert payload["speak"] == "1 pending escalation."


def test_escalate_none_200(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"escalate": lambda: []})
    status, payload = _req(running, token, "/v1/escalate")
    assert status == 200
    assert payload["count"] == 0
    assert payload["escalations"] == []
    assert payload["speak"] == "No escalations pending."


def test_escalate_auth_401(running):
    status, payload = _req(running, None, "/v1/escalate")
    assert status == 401


# --------------------------------------------------------------------------- #
# GET /v1/profiles
# --------------------------------------------------------------------------- #

def test_profiles_200(running, token):
    status, payload = _req(running, token, "/v1/profiles")
    assert status == 200
    assert payload["total_running"] == 3
    assert payload["counts"]["researcher-a"] == 2
    assert payload["speak"]


def test_profiles_auth_401(running):
    status, payload = _req(running, None, "/v1/profiles")
    assert status == 401


# --------------------------------------------------------------------------- #
# POST /v1/cluster/up (confirm-gated)
# --------------------------------------------------------------------------- #

def test_cluster_up_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/cluster/up", body={},
                           method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["up_calls"] == []


def test_cluster_up_confirm_true_backs(running, token, fakes):
    status, payload = _req(running, token, "/v1/cluster/up",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["units"] == 4
    assert payload["message"]
    assert fakes["up_calls"] == [True]


def test_cluster_up_failure_non2xx(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"cluster_up": lambda: {"error": "no serving units"}})
    status, payload = _req(running, token, "/v1/cluster/up",
                           body={"confirm": True}, method="POST")
    assert status == 502
    assert payload["error"]["code"] == "cluster_up_failed"


def test_cluster_up_auth_401(running, fakes):
    status, payload = _req(running, None, "/v1/cluster/up",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["up_calls"] == []


# --------------------------------------------------------------------------- #
# POST /v1/cluster/down (confirm-gated)
# --------------------------------------------------------------------------- #

def test_cluster_down_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/cluster/down", body={},
                           method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["down_calls"] == []


def test_cluster_down_confirm_true_backs(running, token, fakes):
    status, payload = _req(running, token, "/v1/cluster/down",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["success"] is True
    assert payload["message"]
    assert fakes["down_calls"] == [True]


def test_cluster_down_failure_non2xx(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"cluster_down": lambda: {"error": "sparkrun missing"}})
    status, payload = _req(running, token, "/v1/cluster/down",
                           body={"confirm": True}, method="POST")
    assert status == 502
    assert payload["error"]["code"] == "cluster_down_failed"


def test_cluster_down_auth_401(running, fakes):
    status, payload = _req(running, None, "/v1/cluster/down",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["down_calls"] == []


# --------------------------------------------------------------------------- #
# POST /v1/triggers/run (confirm-gated)
# --------------------------------------------------------------------------- #

def test_triggers_run_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/triggers/run", body={},
                           method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["triggers_run_calls"] == []


def test_triggers_run_confirm_true_backs(running, token, fakes):
    status, payload = _req(running, token, "/v1/triggers/run",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["last_run"]["actions_fired"] == 2
    assert payload["recent_events"] == [{"event": "run"}]
    assert payload["speak"]
    assert fakes["triggers_run_calls"] == [True]


def test_triggers_run_failure_non2xx(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"triggers_run": lambda: None})
    status, payload = _req(running, token, "/v1/triggers/run",
                           body={"confirm": True}, method="POST")
    assert status == 502
    assert payload["error"]["code"] == "triggers_run_failed"


def test_triggers_run_auth_401(running, fakes):
    status, payload = _req(running, None, "/v1/triggers/run",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["triggers_run_calls"] == []


# --------------------------------------------------------------------------- #
# POST /v1/escalate (confirm-gated)
# --------------------------------------------------------------------------- #

def test_escalate_run_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/escalate", body={},
                           method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["escalate_run_calls"] == []


def test_escalate_run_confirm_true_backs(running, token, fakes):
    status, payload = _req(running, token, "/v1/escalate",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["count"] == 1
    assert payload["performed"] is True
    assert payload["escalations"][0]["action"] == "reassign"
    assert payload["speak"]
    assert fakes["escalate_run_calls"] == [True]


def test_escalate_run_failure_non2xx(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"escalate_run": lambda: "boom"})
    status, payload = _req(running, token, "/v1/escalate",
                           body={"confirm": True}, method="POST")
    assert status == 502
    assert payload["error"]["code"] == "escalate_failed"


def test_escalate_run_auth_401(running, fakes):
    status, payload = _req(running, None, "/v1/escalate",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["escalate_run_calls"] == []


# --------------------------------------------------------------------------- #
# cluster up/down are POST-only
# --------------------------------------------------------------------------- #

def test_cluster_up_down_not_reachable_via_get(running, token, fakes):
    for path in ("/v1/cluster/up", "/v1/cluster/down"):
        status, payload = _req(running, token, path, method="GET")
        assert status == 405, f"{path}: got {status}"
        assert payload["error"]["code"] == "method_not_allowed"
    assert fakes["up_calls"] == []
    assert fakes["down_calls"] == []


# --------------------------------------------------------------------------- #
# triggers/run + escalate are POST-only for the mutating path
# --------------------------------------------------------------------------- #

def test_triggers_run_escalate_run_not_reachable_via_get(running, token, fakes):
    # GET /v1/triggers and GET /v1/escalate are the read endpoints; the
    # mutating POST paths must reject GET (a GET must never trigger work).
    status, payload = _req(running, token, "/v1/triggers/run", method="GET")
    assert status == 405
    assert payload["error"]["code"] == "method_not_allowed"
    assert fakes["triggers_run_calls"] == []

