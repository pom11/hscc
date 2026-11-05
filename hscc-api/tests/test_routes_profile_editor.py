"""Unit tests for hscc-api per-project Profile Editor endpoints.

The suite is hermetic: it builds a scratch ``HERMES_HOME`` in a tmp dir with a
real ``<proj>-orch`` profile (config.yaml + profile.yaml) and points the
module's ``_hermes_profiles_dir()`` at it via the ``HERMES_HOME`` env var. No
test ever reads or writes the operator's live profiles. Handlers are driven
over real loopback HTTP (port 0) exactly like the other suites, so auth, the
route dispatcher, AND the real ``_backing_read`` / ``_backing_update`` run
against the scratch profile end-to-end.

Coverage per endpoint:
  * GET /v1/profile/editor/{profile}  — reads the five editable fields (model,
                                          provider, toolsets, preload_skills,
                                          description, compression) + available
                                          options; auth; 404 on unknown; 400 on
                                          traversal.
  * POST /v1/profile/editor/{profile} — missing confirm -> 409 AND no write;
                                          confirm:true -> merges ONLY the supplied
                                          fields and preserves everything else
                                          (base_url, api_key, untouched fields);
                                          400 on bad types / nothing-to-update;
                                          404 on unknown profile.
"""

import json
import threading
import types

import pytest

import api_server
import routes_profile_editor as rpe


# --------------------------------------------------------------------------- #
# Fixtures: scratch HERMES_HOME + real backing + running server
# --------------------------------------------------------------------------- #

@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    """A scratch HERMES_HOME with a real ``hscc-orch`` profile.

    Mirrors the real layout: config.yaml carries the model block, toolsets
    list, skills.preload and compression; profile.yaml carries the routing
    description. ``HERMES_HOME`` redirects the module's ``_hermes_profiles_dir()``
    so no live profile is ever touched.
    """
    home = tmp_path / "hermes-home"
    pdir = home / "profiles" / "hscc-orch"
    pdir.mkdir(parents=True)

    # skill seeds for the preload picker (global + profile-local + nested)
    (home / "skills").mkdir(parents=True)
    sk = home / "skills" / "auto" / "hermes-agent"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("# hermes-agent\n")
    local_sk = pdir / "skills" / "ios" / "swift"
    local_sk.mkdir(parents=True)
    (local_sk / "SKILL.md").write_text("# swift\n")
    nested = home / "skills" / "mlops" / "inference" / "vllm"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("# vllm\n")

    (pdir / "config.yaml").write_text(
        "toolsets:\n"
        "- terminal\n"
        "- file\n"
        "skills:\n"
        "  preload:\n"
        "  - hermes-agent\n"
        "model:\n"
        "  default: hscc-model\n"
        "  provider: custom\n"
        "  base_url: http://10.0.0.244:8000/v1\n"
        "  api_key: sk-sparkrun\n"
        "compression:\n"
        "  threshold: 0.8\n"
        "  threshold_tokens: 100000\n"
    )
    (pdir / "profile.yaml").write_text(
        "description: HSCC orchestrator routes work.\n"
        "description_auto: false\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return {"home": home, "pdir": pdir}


@pytest.fixture
def running(tmp_path, profile_home):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path), addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]

    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _req(running, token, path, body=None, method=None):
    """Drive one HTTP request; return (status, payload dict)."""
    import http.client

    conn = http.client.HTTPConnection(running.host, running.port, timeout=5)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    raw = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode("utf-8")
    m = method or ("POST" if body is not None else "GET")
    conn.request(m, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    payload = json.loads(data.decode("utf-8"))
    return resp.status, payload


# --------------------------------------------------------------------------- #
# GET /v1/profile/editor/{profile}
# --------------------------------------------------------------------------- #

def test_get_reads_editable_fields(running, token, profile_home):
    status, body = _req(running, token, "/v1/profile/editor/hscc-orch")
    assert status == 200
    assert body["profile"] == "hscc-orch"
    assert body["model"] == "hscc-model"
    assert body["provider"] == "custom"
    assert body["toolsets"] == ["terminal", "file"]
    assert body["preload_skills"] == ["hermes-agent"]
    assert body["description"] == "HSCC orchestrator routes work."
    assert body["compression"]["threshold"] == 0.8
    assert body["compression"]["threshold_tokens"] == 100000
    assert "toolsets_all" in body and "terminal" in body["toolsets_all"]
    # picker options include global + profile-local skills (bare leaf names)
    assert "hermes-agent" in body["skills_all"]
    assert "swift" in body["skills_all"]
    assert "vllm" in body["skills_all"]
    assert "speak" in body


def test_get_requires_auth(running, token):
    status, body = _req(running, None, "/v1/profile/editor/hscc-orch")
    assert status in (401, 403)


def test_get_unknown_profile_404(running, token):
    status, body = _req(running, token, "/v1/profile/editor/nope-orch")
    assert status == 404
    assert body["error"]["code"] == "not_found"


def test_get_rejects_traversal(running, token):
    status, body = _req(running, token, "/v1/profile/editor/..%2F..%2Fetc")
    assert status == 400
    assert body["error"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# POST /v1/profile/editor/{profile}
# --------------------------------------------------------------------------- #

def test_put_requires_confirm_no_write(running, token, profile_home):
    before = (profile_home["pdir"] / "config.yaml").read_text()
    status, body = _req(running, token, "/v1/profile/editor/hscc-orch",
                        body={"model": "other-model"})
    assert status == 409
    assert body["error"]["code"] == "confirm_required"
    assert (profile_home["pdir"] / "config.yaml").read_text() == before


def test_put_model_merges_preserves_rest(running, token, profile_home):
    """Updating just the model preserves toolsets/skills/compression/base_url."""
    status, body = _req(running, token, "/v1/profile/editor/hscc-orch",
                        body={"model": "new-model", "confirm": True})
    assert status == 200
    assert body["model"] == "new-model"
    # untouched fields preserved
    assert body["toolsets"] == ["terminal", "file"]
    assert body["preload_skills"] == ["hermes-agent"]
    assert body["description"] == "HSCC orchestrator routes work."
    assert body["compression"]["threshold"] == 0.8
    # protected keys not clobbered
    cfg = (profile_home["pdir"] / "config.yaml").read_text()
    assert "base_url: http://10.0.0.244:8000/v1" in cfg
    assert "api_key: sk-sparkrun" in cfg


def test_put_toolsets_and_skills(running, token, profile_home):
    status, body = _req(running, token, "/v1/profile/editor/hscc-orch",
                        body={"toolsets": ["terminal", "file", "web"],
                              "preload_skills": ["hermes-agent", "swift"],
                              "confirm": True})
    assert status == 200
    assert body["toolsets"] == ["terminal", "file", "web"]
    assert body["preload_skills"] == ["hermes-agent", "swift"]
    assert body["model"] == "hscc-model"  # preserved


def test_put_description(running, token, profile_home):
    status, body = _req(running, token, "/v1/profile/editor/hscc-orch",
                        body={"description": "Now it routes iOS work.",
                              "confirm": True})
    assert status == 200
    assert body["description"] == "Now it routes iOS work."
    meta = (profile_home["pdir"] / "profile.yaml").read_text()
    assert "Now it routes iOS work." in meta
    assert "description_auto: false" in meta


def test_put_compression(running, token, profile_home):
    status, body = _req(running, token, "/v1/profile/editor/hscc-orch",
                        body={"compression": {"threshold_tokens": 50000},
                              "confirm": True})
    assert status == 200
    assert body["compression"]["threshold_tokens"] == 50000
    assert body["compression"]["threshold"] == 0.8  # preserved


def test_put_rejects_non_list_toolsets(running, token, profile_home):
    status, body = _req(running, token, "/v1/profile/editor/hscc-orch",
                        body={"toolsets": "terminal", "confirm": True})
    assert status == 400
    assert body["error"]["code"] == "bad_request"


def test_put_nothing_to_update(running, token, profile_home):
    status, body = _req(running, token, "/v1/profile/editor/hscc-orch",
                        body={"confirm": True})
    assert status == 400
    assert body["error"]["code"] == "bad_request"


def test_put_unknown_profile_404(running, token):
    status, body = _req(running, token, "/v1/profile/editor/nope-orch",
                        body={"model": "x", "confirm": True})
    assert status == 404


# --------------------------------------------------------------------------- #
# HERMES_HOME resolution (t_dbae1837 — audit t_8893ab89 rows 33 + 34)
#
# The editor is the sharper case: it WRITES config.yaml / profile.yaml through
# the same resolver. Under a profile-scoped HERMES_HOME the old join resolved
# <root>/profiles/<name>/profiles/<proj>-orch, so a real profile 404'd and a
# PUT would have created a double-nested phantom profile.
# --------------------------------------------------------------------------- #

@pytest.fixture
def leaked_profile_home(tmp_path, monkeypatch):
    """ROOT with a real profile, plus a leaked profile env + double-nest.

    ``HERMES_HOME`` points at the running profile dir — exactly what Hermes
    exports (the shape of the t_7a16aed0 leak class) — and that dir carries a
    ``profiles/`` child of its own so a resolver that merely prefers the
    grandparent cannot pass by accident.
    """
    home = tmp_path / "real-home"
    root = home / ".hermes"
    pdir = root / "profiles" / "hscc-orch"
    pdir.mkdir(parents=True)
    (pdir / "config.yaml").write_text(
        "model:\n  default: hscc-model\n  provider: custom\n"
        "  base_url: http://10.0.0.244:8000/v1\n")
    (pdir / "profile.yaml").write_text("description: Routes HSCC work.\n")

    # GLOBAL skills live at the ROOT; the leaked profile has its own smaller set.
    (root / "skills" / "auto" / "hermes-agent").mkdir(parents=True)
    (root / "skills" / "auto" / "hermes-agent" / "SKILL.md").write_text("# x\n")

    leaked = root / "profiles" / "backend-engineer"
    (leaked / "skills" / "only-local").mkdir(parents=True)
    (leaked / "skills" / "only-local" / "SKILL.md").write_text("# y\n")
    phantom = leaked / "profiles" / "hscc-orch"      # the double-nest
    phantom.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(leaked))
    return {"home": home, "root": root, "pdir": pdir, "leaked": leaked,
            "phantom": phantom}


@pytest.fixture
def leaked_running(tmp_path, leaked_profile_home):
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
def leaked_token(leaked_running):
    return api_server.load_token(leaked_running.server.ctx.hscc_dir)


def test_editor_reads_the_root_profile_under_a_leaked_env(leaked_running,
                                                          leaked_token,
                                                          leaked_profile_home):
    status, body = _req(leaked_running, leaked_token,
                        "/v1/profile/editor/hscc-orch")
    assert status == 200, body            # was 404 before the fix
    assert body["model"] == "hscc-model"
    assert body["description"] == "Routes HSCC work."


def test_editor_global_skills_survive_a_leaked_env(leaked_running, leaked_token,
                                                   leaked_profile_home):
    """Audit row 34: the GLOBAL half of the preload picker vanished.

    ``skills_all`` must include the ROOT's skills AND the profile's own; the
    old env join scanned <profile>/skills as if it were the global dir, so
    root-level skills disappeared from the picker.
    """
    status, body = _req(leaked_running, leaked_token,
                        "/v1/profile/editor/hscc-orch")
    assert status == 200
    assert "hermes-agent" in body["skills_all"]     # ROOT skills (the lost half)


def test_editor_writes_land_in_the_root_profile(leaked_running, leaked_token,
                                                leaked_profile_home):
    """A PUT edits ROOT/profiles/hscc-orch and creates no phantom profile."""
    status, body = _req(leaked_running, leaked_token,
                        "/v1/profile/editor/hscc-orch",
                        body={"model": "new-model", "confirm": True})
    assert status == 200
    assert body["model"] == "new-model"
    cfg = (leaked_profile_home["pdir"] / "config.yaml").read_text()
    assert "new-model" in cfg
    assert "base_url: http://10.0.0.244:8000/v1" in cfg   # merge, not clobber
    # Nothing was written into the double-nest.
    assert list(leaked_profile_home["phantom"].iterdir()) == []
    assert not (leaked_profile_home["leaked"] / "profiles" / "hscc-orch" /
                "config.yaml").exists()


def test_editor_profiles_dir_is_the_shared_resolver(leaked_profile_home):
    """One resolver for both routes — no private copy that can drift."""
    import routes_profile
    assert rpe._hermes_profiles_dir() == routes_profile._hermes_profiles_dir()
    assert rpe._hermes_root_dir() == routes_profile.hermes_root_dir()

