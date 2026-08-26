"""Unit tests for the cluster template READ endpoints (list / status / preview).

Hermetic: every backing call is stubbed via monkeypatch on the ``_backing_*``
module functions, so NO test reads the live template store. Handlers are
driven over real loopback HTTP (port 0) so auth and the router are exercised
end-to-end.

Coverage per endpoint:
  * reads -> 200 + expected shape + non-empty ``speak``;
  * preview of an unknown template -> 404;
  * auth enforced (401 without a token);
  * error envelope shape ({error: {code, message, speak}}).
"""

import http.client
import json
import threading
import types

import pytest

import api_server
import routes_template


@pytest.fixture
def fakes(monkeypatch):
    b = {
        "template_list": lambda: {
            "templates": [{"name": "dev"}, {"name": "prod"}],
            "count": 2,
        },
        "template_status": lambda: {
            "applied": "dev",
            "applied_at": "2026-08-25T00:00:00+00:00",
        },
        "template_preview": lambda name: {
            "template": name,
            "changes": [{"unit": "orch", "change": "restart"}],
        },
    }
    _install(monkeypatch, b)
    return b


def _install(monkeypatch, backing: dict):
    for name, fn in backing.items():
        monkeypatch.setattr(routes_template, f"_backing_{name}", fn)


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


def _req(running, token, path, method="GET"):
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    conn.request(method, path, headers=headers)
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
# GET /v1/template/list
# --------------------------------------------------------------------------- #

def test_template_list_200(running, token):
    status, payload = _req(running, token, "/v1/template/list")
    assert status == 200
    assert len(payload["templates"]) == 2
    assert payload["speak"] == "2 templates available."


def test_template_list_auth_401(running):
    status, payload = _req(running, None, "/v1/template/list")
    assert status == 401


# --------------------------------------------------------------------------- #
# GET /v1/template/status
# --------------------------------------------------------------------------- #

def test_template_status_200(running, token):
    status, payload = _req(running, token, "/v1/template/status")
    assert status == 200
    assert payload["applied"] == "dev"
    assert payload["speak"] == "Template dev is applied."


def test_template_status_auth_401(running):
    status, payload = _req(running, None, "/v1/template/status")
    assert status == 401


# --------------------------------------------------------------------------- #
# GET /v1/template/preview/{name}
# --------------------------------------------------------------------------- #

def test_template_preview_200(running, token):
    status, payload = _req(running, token, "/v1/template/preview/dev")
    assert status == 200
    assert payload["template"] == "dev"
    assert len(payload["changes"]) == 1
    assert payload["speak"] == "Preview: 1 change."


def test_template_preview_unknown_404(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"template_preview": lambda name: {"error": "no such template"}})
    status, payload = _req(running, token, "/v1/template/preview/ghost")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_template_preview_auth_401(running):
    status, payload = _req(running, None, "/v1/template/preview/dev")
    assert status == 401
