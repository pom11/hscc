"""Unit tests for the cluster template READ endpoints (list / status / preview / validate).

Hermetic: every backing call is stubbed via monkeypatch on the ``_backing_*``
module functions, so NO test reads the live template store. Handlers are
driven over real loopback HTTP (port 0) so auth and the router are exercised
end-to-end.

Coverage per endpoint:
  * reads -> 200 + expected shape + non-empty ``speak``;
  * preview of an unknown template -> 404;
  * validate of an unknown template -> 200 (the CLI's ``ok:false`` verdict), and
    the route reuses the CLI's ``validate`` argv (never a second validator);
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
        "template_validate": lambda name, structural_only=False: {
            "template": name,
            "structural": {"ok": True, "errors": [], "warnings": []},
            "placement": {"ok": True, "errors": [], "warnings": []},
            "ok": True,
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


def test_template_list_backing_error_dict_degrades(running, token, monkeypatch):
    """Backing error dict -> clean 200 with only 'speak', never a raw 'error' key."""
    _install(monkeypatch, {"template_list": lambda: {"error": "hscc-cluster plugin not found"}})
    status, payload = _req(running, token, "/v1/template/list")
    assert status == 200
    assert payload == {"speak": "Template list unavailable."}
    assert "error" not in payload


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


def test_template_status_backing_error_dict_degrades(running, token, monkeypatch):
    """Backing error dict -> clean 200 with only 'speak', never a raw 'error' key."""
    _install(monkeypatch, {"template_status": lambda: {"error": "hscc-cluster plugin not found"}})
    status, payload = _req(running, token, "/v1/template/status")
    assert status == 200
    assert payload == {"speak": "Template status unavailable."}
    assert "error" not in payload


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


# --------------------------------------------------------------------------- #
# GET /v1/template/validate/{name}
# --------------------------------------------------------------------------- #

def test_template_validate_200(running, token):
    status, payload = _req(running, token, "/v1/template/validate/dev")
    assert status == 200
    assert payload["template"] == "dev"
    assert payload["ok"] is True
    assert payload["structural"]["ok"] is True
    assert payload["speak"] == "Template dev is deployable."


def test_template_validate_auth_401(running):
    status, payload = _req(running, None, "/v1/template/validate/dev")
    assert status == 401


def test_template_validate_missing_name_400():
    """A blank/whitespace name -> 400, not a fabricated verdict.

    Driven at the handler because the router's ``(?P<name>[^/]+)`` regex can
    never yield an empty segment (an empty ``/v1/template/validate/`` path
    matches nothing and 404s upstream) — the guard is the same defensive branch
    ``preview`` carries, exercised directly.
    """
    with pytest.raises(api_server.ApiError) as exc:
        routes_template.handle_template_validate(None, None, {"name": "   "}, None)
    assert exc.value.status == 400
    assert exc.value.code == "bad_request"


def test_template_validate_not_found_is_a_verdict_not_404(running, token, monkeypatch):
    """A missing template is the CLI's ``ok:false`` verdict (structural error
    'Template not found'), NOT an HTTP 404 — the route returns the same verdict
    ``hscc template validate`` prints. Contrast preview, which 404s."""
    _install(monkeypatch, {
        "template_validate": lambda name, structural_only=False: {
            "template": name,
            "structural": {"ok": False, "errors": [f"Template not found: {name}"],
                           "warnings": []},
            "placement": {"ok": True, "errors": [], "warnings": []},
            "ok": False,
        },
    })
    status, payload = _req(running, token, "/v1/template/validate/ghost")
    assert status == 200
    assert payload["ok"] is False
    assert payload["structural"]["errors"] == ["Template not found: ghost"]
    assert payload["speak"] == "Template ghost is not deployable: 1 error."


def test_template_validate_backing_error_dict_degrades(running, token, monkeypatch):
    """Backing error dict (plugin missing) -> clean 200 with only 'speak',
    never a raw 'error' key leaking to the client."""
    _install(monkeypatch,
             {"template_validate": lambda name, structural_only=False:
                 {"error": "hscc-cluster plugin not found"}})
    status, payload = _req(running, token, "/v1/template/validate/dev")
    assert status == 200
    assert payload == {"speak": "Template validation unavailable."}
    assert "error" not in payload


def test_template_validate_structural_only_forwards_cli_flag(running, token, monkeypatch):
    """The ``structural-only`` query flag is forwarded as a boolean to the
    backing seam — the CLI's ``--structural-only`` half of the same argv.
    Proves the route reuses the CLI verdict path, never a second validator."""
    seen = {}

    def _spy(name, structural_only=False):
        seen["name"] = name
        seen["structural_only"] = structural_only
        return {"template": name,
                "structural": {"ok": True, "errors": [], "warnings": []},
                "placement": {"ok": True, "errors": [], "warnings": [],
                              "skipped": True},
                "ok": True}

    _install(monkeypatch, {"template_validate": _spy})
    status, payload = _req(running, token,
                           "/v1/template/validate/dev?structural-only=1")
    assert status == 200
    assert seen == {"name": "dev", "structural_only": True}


def test_backing_template_validate_reuses_cli_argv(monkeypatch):
    """The backing seam builds the exact argv the CLI verb takes and hands it to
    ``cmd_cluster_template`` — no direct ``validate_template`` import (audit §5)."""
    argvs = []

    def _fake_cmd(argv):
        argvs.append(argv)
        return {"template": argv[1], "ok": True}

    monkeypatch.setattr(routes_template, "_load_template_cli", lambda: _fake_cmd)
    routes_template._backing_template_validate("dev")
    routes_template._backing_template_validate("dev", structural_only=True)
    assert argvs == [["validate", "dev"],
                     ["validate", "dev", "--structural-only"]]


def test_backing_template_validate_plugin_missing(monkeypatch):
    """No cluster plugin -> the honest error dict the handler degrades on."""
    monkeypatch.setattr(routes_template, "_load_template_cli", lambda: None)
    assert routes_template._backing_template_validate("dev") == {
        "error": "hscc-cluster plugin not found"}
