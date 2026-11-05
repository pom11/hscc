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


# --------------------------------------------------------------------------- #
# HERMES_HOME resolution (t_dbae1837 — audit t_8893ab89 row 32)
#
# Hermes exports HERMES_HOME=<root>/profiles/<name> for every profile session,
# and the old resolver joined "profiles" onto it: <root>/profiles/<name>/profiles
# — empty, so the Profile Library listed ZERO profiles. These tests fabricate
# that layout under a throwaway HOME (never the operator's real ~/.hermes) and
# pin that the ROOT's profiles dir is what gets enumerated.
# --------------------------------------------------------------------------- #

@pytest.fixture
def leaked_home(tmp_path, monkeypatch):
    """A scratch Hermes ROOT + a profile env that leaks into the API process.

    Layout (the real fleet layout, including the double-nest an earlier leak
    left behind):

        <root>/profiles/hscc-orch/config.yaml        <- the ONLY real profile
        <root>/profiles/backend-engineer/            <- the running profile
        <root>/profiles/backend-engineer/profiles/ghost/  <- the double-nest

    ``HERMES_HOME`` points at the running profile dir, i.e. exactly what Hermes
    exports. HOME is pinned to tmp_path so the ``~/.hermes`` default candidate
    is also inside the fixture, never the operator's real home.
    """
    home = tmp_path / "real-home"
    root = home / ".hermes"
    orch = root / "profiles" / "hscc-orch"
    orch.mkdir(parents=True)
    (orch / "config.yaml").write_text(
        "model:\n  default: hscc-model\n  provider: custom\n")
    (orch / "description.txt").write_text("Routes HSCC work.\n")

    leaked = root / "profiles" / "backend-engineer"
    ghost = leaked / "profiles" / "ghost"          # the double-nest
    ghost.mkdir(parents=True)
    (ghost / "config.yaml").write_text("model:\n  default: phantom\n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(leaked))
    return {"home": home, "root": root, "orch": orch, "leaked": leaked,
            "ghost": ghost}


def _listing_names(profiles_dir):
    return [p.name for p in sorted(profiles_dir.iterdir()) if p.is_dir()]


def test_profiles_dir_ignores_a_profile_scoped_env(leaked_home):
    """A profile-exported HERMES_HOME still resolves the ROOT's profiles/."""
    got = routes_profile._hermes_profiles_dir()
    assert got == leaked_home["root"] / "profiles"
    # The double-nest is not what gets listed — and the real profile is.
    assert _listing_names(got) == ["backend-engineer", "hscc-orch"]
    assert "ghost" not in _listing_names(got)


def test_double_nest_is_skipped_even_when_it_exists(leaked_home):
    """The double-nest exists here, and is still ruled out (t_7a16aed0 class).

    A resolver that merely preferred the grandparent and stopped would pass the
    test above by accident; this one puts a real ``profiles/`` directory inside
    the profile dir (the state this fleet is actually in) and requires the
    resolver to reject it because its parent is named ``profiles``.
    """
    assert (leaked_home["leaked"] / "profiles").is_dir()   # fixture sanity
    got = routes_profile._hermes_profiles_dir()
    assert got != leaked_home["leaked"] / "profiles"
    assert (got / "hscc-orch").is_dir()


def test_list_route_enumerates_real_profiles_under_a_leaked_env(leaked_home,
                                                                monkeypatch,
                                                                tmp_path):
    """The bug's operator-visible symptom: the library listed 0 profiles.

    Unlike the rest of this file, this drives the REAL ``_backing_list`` (no
    backing stub) over loopback HTTP, so the whole read path — resolver,
    directory walk and config.yaml model read — runs against the scratch tree.
    """
    import threading
    import http.client

    srv = api_server.create_server(hscc_dir=str(tmp_path), addr=("127.0.0.1", 0))
    host, port = srv.server_address[:2]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/v1/profile/list", headers={
            "Authorization": "Bearer " + api_server.load_token(srv.ctx.hscc_dir)})
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()

    assert resp.status == 200
    names = [p["name"] for p in payload["profiles"]]
    assert "hscc-orch" in names           # the real profile is listed
    assert "ghost" not in names           # the double-nest is not
    assert payload["count"] == len(names) >= 1
    assert "0 profile" not in payload["speak"]


def test_unset_env_resolves_the_default_root(tmp_path, monkeypatch):
    """No HERMES_HOME at all (the shipped launchd unit) → ~/.hermes/profiles."""
    home = tmp_path / "plain-home"
    (home / ".hermes" / "profiles" / "solo").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert routes_profile._hermes_profiles_dir() == home / ".hermes" / "profiles"


def test_no_profiles_dir_anywhere_falls_back_to_the_default_root(tmp_path,
                                                                 monkeypatch):
    """Nothing installed → the default root, never a $HERMES_HOME join."""
    home = tmp_path / "bare-home"
    # A HERMES_HOME that carries no profiles/ child anywhere near it: the
    # resolver must not invent `$HERMES_HOME/profiles`.
    stray = home / "scratch" / "hermes-workdir"
    stray.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(stray))
    got = routes_profile._hermes_profiles_dir()
    assert got == home / ".hermes" / "profiles"
    assert got != stray / "profiles"


def test_resolver_never_reads_the_env_directly():
    """Structural pin: the resolver must go through the shared ladder.

    The whole class of bug is a private ``os.environ["HERMES_HOME"]`` join in
    one route (audit t_8893ab89 rows 32-34). A future edit that reintroduces it
    in either file fails here even if the author also adds a passing test.
    """
    import inspect

    import routes_profile_editor
    for mod in (routes_profile, routes_profile_editor):
        src = inspect.getsource(mod)
        assert 'environ.get("HERMES_HOME"' not in src, mod.__name__
        assert 'environ["HERMES_HOME"]' not in src, mod.__name__
    # ...and the ladder is the shared one, not a copy.
    assert "hermes_homes" in inspect.getsource(routes_profile.hermes_root_dir)
    # ...and the profile-dir filter is the shared predicate, never inline.
    src = inspect.getsource(routes_profile.hermes_root_dir)
    assert "is_profile_dir" in src
    assert 'basename(parent) == "profiles"' not in src


def test_profile_dir_candidate_is_skipped_via_the_shared_predicate(
        tmp_path, monkeypatch):
    """Only the ROOT wins; the profile-scoped candidate is dropped by the rule.

    The profile dir carries a real ``profiles/`` child (the double-nest), so a
    resolver that stopped filtering would return that phantom tree instead.
    The predicate is instrumented to prove this resolver calls it.
    """
    from hscc_daemon import hermes_python

    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "worker"
    (profile / "profiles" / "ghost").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "plain-home"))
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert routes_profile.hermes_root_dir() == root

    seen = []
    real = hermes_python.is_profile_dir
    monkeypatch.setattr(hermes_python, "is_profile_dir",
                        lambda home: seen.append(str(home)) or real(home))
    assert routes_profile.hermes_root_dir() == root
    assert str(profile) in seen

