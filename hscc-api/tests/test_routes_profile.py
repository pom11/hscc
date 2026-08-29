"""Unit tests for hscc-api Profile Library endpoints.

The suite is hermetic: every backing call is stubbed via monkeypatch on the
``_backing_*`` functions, so NO test ever runs a real ``hermes profile
install/export`` against the live operator's profiles. Handlers are driven
over real loopback HTTP (port 0) exactly like the other suites, so auth and
the route dispatcher are exercised end-to-end.

Coverage per endpoint:
  * GET /v1/profile/list          — structured browse + speak + auth + degrade.
  * POST /v1/profile/install      — missing confirm -> 409 AND no backing call;
                                    confirm:true -> backing called with args;
                                    missing source -> 400; failed install -> 502.
  * POST /v1/profile/export       — missing confirm -> 409 no backing;
                                    confirm:true -> backing called; 400/502.
  * GET /v1/profile/export/{file} — path-safety (rejects /, ..), 404, serves
                                    raw bytes via the __raw_bytes__ escape.
"""

import json
import types

import pytest

import api_server
import routes_profile


# --------------------------------------------------------------------------- #
# Fixtures: running server + hermetic fakes for every backing call
# --------------------------------------------------------------------------- #

@ pytest.fixture
def fakes(monkeypatch, tmp_path):
    """Install hermetic fakes; never touch the live hermes profiles."""
    state = {
        "list_calls": [],
        "install_calls": [],
        "export_calls": [],
    }
    b = {
        "list": lambda: (
            state["list_calls"].append(1)
            or [
                {"name": "flutter-engineer", "model": "worker-model",
                 "description": "Builds Flutter apps.",
                 "is_distribution": True, "source": "github.com/acme/flutter",
                 "version": "1.0.0"},
                {"name": "bc-al-engineer", "model": "worker-model",
                 "description": None, "is_distribution": False,
                 "source": None, "version": None},
            ]
        ),
        "install": lambda source, name=None: (
            state["install_calls"].append((source, name))
            or {"installed": True, "name": name or "flutter-engineer",
                "source": source}
        ),
        "export": lambda profile, ctx: (
            state["export_calls"].append((profile, ctx))
            or {"profile": profile, "filename": f"{profile}.tar.gz",
                "path": str(tmp_path / "profile-exports" / f"{profile}.tar.gz"),
                "size": 1234}
        ),
    }
    _install(monkeypatch, b)
    # Point the export dir at the tmp hscc dir for download tests.
    export_dir = tmp_path / "profile-exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "flutter.tar.gz").write_bytes(b"fake-gzip-bytes")
    return state


def _install(monkeypatch, backing: dict):
    for name, fn in backing.items():
        monkeypatch.setattr(routes_profile, f"_backing_{name}", fn)


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


def _req(running, token, path, body=None, method=None):
    """Drive one HTTP request; return (status, payload).

    ``payload`` is the decoded JSON dict for JSON responses, or a raw bytes
    object when the response is binary (Content-Type not application/json).
    """
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
    m = method or ("POST" if body is not None else "GET")
    conn.request(m, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    ctype = resp.getheader("Content-Type", "")
    conn.close()
    if data and "application/json" in ctype:
        try:
            return resp.status, json.loads(data)
        except ValueError:
            return resp.status, {"raw": data}
    return resp.status, data


# --------------------------------------------------------------------------- #
# GET /v1/profile/list
# --------------------------------------------------------------------------- #

def test_list_browse(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/list")
    assert status == 200
    assert payload["count"] == 2
    assert payload["profiles"][0]["name"] == "flutter-engineer"
    assert payload["profiles"][0]["is_distribution"] is True
    assert payload["profiles"][0]["source"] == "github.com/acme/flutter"
    assert payload["profiles"][1]["name"] == "bc-al-engineer"
    assert payload["profiles"][1]["is_distribution"] is False
    assert "installed" in payload["speak"]


def test_list_auth_401(running, fakes):
    status, payload = _req(running, token=None, path="/v1/profile/list")
    assert status == 401
    assert fakes["list_calls"] == []


def test_list_degrade_on_backing_error(running, token, fakes, monkeypatch):
    def boom():
        raise RuntimeError("boom")
    _install(monkeypatch, {"list": boom})
    status, payload = _req(running, token, "/v1/profile/list")
    assert status == 200
    assert payload["profiles"] == []
    assert "unavailable" in payload["speak"]


# --------------------------------------------------------------------------- #
# POST /v1/profile/install
# --------------------------------------------------------------------------- #

def test_install_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/install",
                           body={"source": "github.com/acme/flutter"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["install_calls"] == []


def test_install_confirm_true_calls_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/install", body={
        "source": "github.com/acme/flutter", "confirm": True,
    })
    assert status == 200
    assert payload["installed"] is True
    assert payload["name"] == "flutter-engineer"
    assert payload["message"]
    # Source passed; name None (not supplied) -> the manifest decides.
    assert fakes["install_calls"] == [("github.com/acme/flutter", None)]


def test_install_with_name_override(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/install", body={
        "source": "github.com/acme/flutter", "name": "my-flutter",
        "confirm": True,
    })
    assert status == 200
    assert fakes["install_calls"] == [("github.com/acme/flutter", "my-flutter")]


def test_install_missing_source_400(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/install",
                           body={"confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["install_calls"] == []


def test_install_failed_502(running, token, fakes, monkeypatch):
    def fail(source, name=None):
        fakes["install_calls"].append((source, name))
        raise api_server.ApiError(
            502, "install_failed", "clone failed", "Profile install failed.")
    _install(monkeypatch, {"install": fail})
    status, payload = _req(running, token, "/v1/profile/install", body={
        "source": "github.com/acme/flutter", "confirm": True,
    })
    assert status == 502
    assert payload["error"]["code"] == "install_failed"


def test_install_auth_401(running, fakes):
    status, payload = _req(running, token=None, path="/v1/profile/install",
                           body={"source": "x", "confirm": True})
    assert status == 401
    assert fakes["install_calls"] == []


# --------------------------------------------------------------------------- #
# POST /v1/profile/export
# --------------------------------------------------------------------------- #

def test_export_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/export",
                           body={"profile": "flutter-engineer"})
    assert status == 409
    assert fakes["export_calls"] == []


def test_export_confirm_true_calls_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/export", body={
        "profile": "flutter-engineer", "confirm": True,
    })
    assert status == 200
    assert payload["profile"] == "flutter-engineer"
    assert payload["filename"] == "flutter-engineer.tar.gz"
    assert payload["path"]
    assert payload["size"] == 1234
    assert payload["message"]
    assert fakes["export_calls"] == [("flutter-engineer", running.server.ctx)]


def test_export_missing_profile_400(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/export",
                           body={"confirm": True})
    assert status == 400
    assert fakes["export_calls"] == []


def test_export_invalid_name_400(fakes):
    # The name validator guards _backing_export BEFORE any shell-out, so an
    # operator can never direct the export tarball outside the export dir via
    # a path separator in the profile name.
    assert not routes_profile._valid_profile_name("../evil")
    assert not routes_profile._valid_profile_name("a/b")
    assert not routes_profile._valid_profile_name("")
    assert not routes_profile._valid_profile_name("a b c")
    assert routes_profile._valid_profile_name("flutter-engineer")
    assert routes_profile._valid_profile_name("bc_al_engineer")
    assert fakes["export_calls"] == []


def test_export_failed_502(running, token, fakes, monkeypatch):
    def fail(profile, ctx):
        fakes["export_calls"].append((profile, ctx))
        raise api_server.ApiError(502, "export_failed", "tar failed",
                                  "Profile export failed.")
    _install(monkeypatch, {"export": fail})
    status, payload = _req(running, token, "/v1/profile/export", body={
        "profile": "flutter-engineer", "confirm": True,
    })
    assert status == 502
    assert payload["error"]["code"] == "export_failed"


def test_export_auth_401(running, fakes):
    status, payload = _req(running, token=None, path="/v1/profile/export",
                           body={"profile": "x", "confirm": True})
    assert status == 401
    assert fakes["export_calls"] == []


# --------------------------------------------------------------------------- #
# GET /v1/profile/export/{file}  (raw binary download)
# --------------------------------------------------------------------------- #

def test_export_download_serves_bytes(running, token):
    status, data = _req(running, token, "/v1/profile/export/flutter.tar.gz")
    assert status == 200
    assert data == b"fake-gzip-bytes"


def test_export_download_missing_404(running, token):
    status, data = _req(running, token, "/v1/profile/export/nope.tar.gz")
    assert status == 404
    assert isinstance(data, dict)
    assert data["error"]["code"] == "not_found"


def test_export_download_path_traversal_rejected(running, token):
    # A slash or .. must never escape the export dir.
    status, data = _req(running, token, "/v1/profile/export/..%2fapi.json")
    assert status in (400, 404)
    if isinstance(data, dict):
        assert data["error"]["code"] in ("bad_request", "not_found")


def test_export_download_auth_401(running):
    status, data = _req(running, token=None, path="/v1/profile/export/flutter.tar.gz")
    assert status == 401


# --------------------------------------------------------------------------- #
# Guard rails: mutating paths never served via GET
# --------------------------------------------------------------------------- #

def test_install_not_via_get(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/install")
    assert status == 405
    assert payload["error"]["code"] == "method_not_allowed"
    assert fakes["install_calls"] == []


def test_export_not_via_get(running, token, fakes):
    status, payload = _req(running, token, "/v1/profile/export")
    assert status == 405
    assert fakes["export_calls"] == []
