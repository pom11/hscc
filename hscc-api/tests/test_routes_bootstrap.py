"""Unit tests for the new-project bootstrap wizard endpoints.

Hermetic: every backing call is stubbed via monkeypatch on the ``_backing_*``
module functions, so NO test initializes a real git repo, reads a live registry,
invokes the profile generator, or opens a live profile session DB. Handlers are
driven over real loopback HTTP (port 0) so auth and the router are exercised
end-to-end.

Coverage required by the card (the phone-driven guided flow):
  * GET  /v1/projects/new/plan — READ-ONLY plan: valid name/repo -> 200 with
    the steps that WILL run; invalid name / registry collision -> 200 with
    ``ready=false`` + a blocker (the wizard shows the operator why it cannot
    proceed, without mutating anything);
  * POST /v1/projects/new — confirm-gated (409 ``confirm_required`` without
    ``confirm: true`` AND no backing mutation made); missing name/repo -> 400;
    an already-registered name -> 409 ``project_exists`` (never overwrites);
    success -> every step (repo, board, orchestrator, session) runs and reports;
    a failed orchestrator/session step degrades to a partial (202), never a
    fabricated full success;
  * auth enforced (401 without a token) on both endpoints.
"""

import http.client
import json
import threading
import types

import pytest

import api_server
import routes_bootstrap


def _make_plan(registry_has=False, name_valid=True, name_error=None):
    """A plan fake shape matching the real ``_backing_plan`` return."""
    collisions = {
        "registry_has_project": registry_has,
        "repo_is_git": False,
        "board_exists": False,
        "profile_exists": False,
    }
    blockers = []
    if not name_valid:
        blockers.append(name_error or "invalid name")
    if registry_has:
        blockers.append("already registered")
    return {
        "name": "acme",
        "repo": "/tmp/acme",
        "ready": (name_valid and not registry_has),
        "validations": {"name_valid": name_valid, "name_error": name_error,
                        "repo_given": True},
        "collisions": collisions,
        "blockers": blockers,
        "steps": ["repo", "board", "orchestrator", "session"],
        "would_create": {"profile": "acme-orch", "session": "acme",
                         "board": "acme"},
    }


@pytest.fixture
def fakes(monkeypatch):
    """Install hermetic fake backing; returns mutable + call-record state."""
    state = {
        "plan_registry_has": False,
        "plan_calls": [],
        "create_calls": [],
        "orch_calls": [],
        "session_calls": [],
        # Optional forced failures (set to an Exception to raise / truthy
        # 'bool' to return a step-failure from create_project).
        "create_ok": True,
        "orch_fail": None,     # if truthy, ensure_orchestrator raises
        "session_fail": None,  # if truthy, ensure_session raises
    }

    def plan(name, repo, registry_path=None):
        state["plan_calls"].append((name, repo, registry_path))
        name_valid = bool(name) and name.replace("-", "").replace("_", "") \
            .isalnum() and name[0].islower()
        return _make_plan(registry_has=state["plan_registry_has"],
                          name_valid=name_valid,
                          name_error=None if name_valid else "invalid name")

    def create_project(name, repo, registry_path, github, private):
        state["create_calls"].append(
            (name, repo, registry_path, github, private))
        steps = [
            {"id": "repo", "status": "ok", "detail": "repo created"},
            {"id": "board", "status": "ok", "detail": "board acme"},
            {"id": "registry", "status": "ok", "detail": "registered acme"},
        ]
        return {"steps": steps, "repo": repo, "ok": state["create_ok"],
                "retry": None}

    def ensure_orchestrator(name, registry_path, base_identity=""):
        state["orch_calls"].append((name, registry_path, base_identity))
        if state["orch_fail"]:
            raise RuntimeError("profile ensure boom")
        return {"profile": f"{name}-orch", "session": name, "board": name,
                "repo": "/tmp/acme", "project": name, "changed": True}

    def ensure_session(profile, session):
        state["session_calls"].append((profile, session))
        if state["session_fail"]:
            raise RuntimeError("session ensure boom")
        return {"created_session": "20260829_first_abc123",
                "profile": profile, "title": session}

    for name, fn in (
            ("plan", plan),
            ("create_project", create_project),
            ("ensure_orchestrator", ensure_orchestrator),
            ("ensure_session", ensure_session)):
        monkeypatch.setattr(routes_bootstrap, f"_backing_{name}", fn)
    return state


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


PLAN_PATH = "/v1/projects/new/plan?name=acme&repo=/tmp/acme"


# --------------------------------------------------------------------------- #
# GET /v1/projects/new/plan (read-only)
# --------------------------------------------------------------------------- #

def test_plan_ready_200(running, token, fakes):
    status, payload = _req(running, token, PLAN_PATH)
    assert status == 200
    assert payload["ready"] is True
    assert payload["steps"] == ["repo", "board", "orchestrator", "session"]
    assert payload["validations"]["name_valid"] is True
    assert payload["blockers"] == []
    assert payload["speak"]
    # Plan is READ-ONLY: no create/seeding backing was touched.
    assert fakes["create_calls"] == []
    assert fakes["orch_calls"] == []
    assert fakes["session_calls"] == []


def test_plan_invalid_name_not_ready(running, token, fakes):
    status, payload = _req(running, token,
                           "/v1/projects/new/plan?name=Bad%20Name&repo=/tmp/x")
    assert status == 200
    assert payload["ready"] is False
    assert payload["blockers"], "a blocker names the invalid name"
    assert payload["speak"]


def test_plan_registry_collision_not_ready(running, token, fakes):
    fakes["plan_registry_has"] = True
    status, payload = _req(running, token, PLAN_PATH)
    assert status == 200
    assert payload["ready"] is False
    assert payload["collisions"]["registry_has_project"] is True
    assert any("already registered" in b for b in payload["blockers"])


# --------------------------------------------------------------------------- #
# POST /v1/projects/new — confirm gate
# --------------------------------------------------------------------------- #

def test_create_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/projects/new",
                           body={"name": "acme", "repo": "/tmp/acme"},
                           method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["create_calls"] == []
    assert fakes["orch_calls"] == []
    assert fakes["session_calls"] == []


def test_create_confirm_false_is_409(running, token, fakes):
    status, _ = _req(running, token, "/v1/projects/new",
                     body={"name": "acme", "repo": "/tmp/acme",
                           "confirm": False}, method="POST")
    assert status == 409


# --------------------------------------------------------------------------- #
# POST /v1/projects/new — validation 400s
# --------------------------------------------------------------------------- #

def test_create_missing_name_400(running, token, fakes):
    status, payload = _req(running, token, "/v1/projects/new",
                           body={"repo": "/tmp/acme", "confirm": True},
                           method="POST")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "name" in payload["error"]["message"]


def test_create_invalid_name_400(running, token, fakes):
    status, payload = _req(running, token, "/v1/projects/new",
                           body={"name": "Bad Name!", "repo": "/tmp/acme",
                                 "confirm": True}, method="POST")
    assert status == 400
    assert "name" in payload["error"]["message"].lower()


def test_create_missing_repo_400(running, token, fakes):
    status, payload = _req(running, token, "/v1/projects/new",
                           body={"name": "acme", "confirm": True},
                           method="POST")
    assert status == 400
    assert "repo" in payload["error"]["message"]


# --------------------------------------------------------------------------- #
# POST /v1/projects/new — already-registered guard
# --------------------------------------------------------------------------- #

def test_create_already_registered_409(running, token, fakes):
    fakes["plan_registry_has"] = True
    status, payload = _req(running, token, "/v1/projects/new",
                           body={"name": "acme", "repo": "/tmp/acme",
                                 "confirm": True}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "project_exists"
    # The collision is caught BEFORE any mutation runs.
    assert fakes["create_calls"] == []
    assert fakes["orch_calls"] == []
    assert fakes["session_calls"] == []


# --------------------------------------------------------------------------- #
# POST /v1/projects/new — success
# --------------------------------------------------------------------------- #

def test_create_success_runs_all_steps(running, token, fakes):
    status, payload = _req(running, token, "/v1/projects/new",
                           body={"name": "acme", "repo": "/tmp/acme",
                                 "confirm": True}, method="POST")
    assert status == 200
    assert payload["ok"] is True
    assert payload["name"] == "acme"
    assert payload["profile"] == "acme-orch"
    assert payload["session"] == "acme"
    # create_project + orchestrator + session all called.
    assert len(fakes["create_calls"]) == 1
    assert fakes["create_calls"][0][0] == "acme"
    assert len(fakes["orch_calls"]) == 1
    assert fakes["orch_calls"][0][0] == "acme"
    assert len(fakes["session_calls"]) == 1
    assert fakes["session_calls"][0] == ("acme-orch", "acme")
    # Per-step report includes the orchestrator + session steps.
    ids = [s["id"] for s in payload["steps"]]
    assert "orchestrator" in ids and "session" in ids
    assert payload["speak"]


def test_create_forwards_github_flags(running, token, fakes):
    _req(running, token, "/v1/projects/new",
         body={"name": "acme", "repo": "/tmp/acme", "confirm": True,
               "github": True, "private": True}, method="POST")
    _, repo, _rg, github, private = fakes["create_calls"][0]
    assert github is True
    assert private is True


# --------------------------------------------------------------------------- #
# POST /v1/projects/new — partial failures
# --------------------------------------------------------------------------- #

def test_create_orchestrator_failure_partial(running, token, fakes):
    fakes["orch_fail"] = True
    status, payload = _req(running, token, "/v1/projects/new",
                           body={"name": "acme", "repo": "/tmp/acme",
                                 "confirm": True}, method="POST")
    assert status == 202  # partial, not a fabricated full success
    assert payload["ok"] is False
    by_id = {s["id"]: s for s in payload["steps"]}
    assert by_id["orchestrator"]["status"] == "failed"


def test_create_session_failure_partial(running, token, fakes):
    fakes["session_fail"] = True
    status, payload = _req(running, token, "/v1/projects/new",
                           body={"name": "acme", "repo": "/tmp/acme",
                                 "confirm": True}, method="POST")
    assert status == 202
    assert payload["ok"] is False
    assert {s["id"]: s["status"] for s in payload["steps"]}["session"] == \
        "failed"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def test_plan_requires_auth(running, fakes):
    status, _ = _req(running, None, PLAN_PATH)
    assert status == 401


def test_create_requires_auth(running, fakes):
    status, _ = _req(running, None, "/v1/projects/new",
                     body={"name": "acme", "repo": "/tmp/acme",
                           "confirm": True}, method="POST")
    assert status == 401
