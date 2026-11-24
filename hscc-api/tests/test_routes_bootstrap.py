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


def _make_plan(name="acme", repo="/tmp/acme", registry_has=False,
               name_valid=True, name_error=None):
    """A plan fake shape matching the real ``_backing_plan`` return.

    ``name``/``repo`` are ECHOED, not hardcoded: a fake that answers
    ``acme``/``/tmp/acme`` whatever it was asked about cannot show a handler that
    handed it a truncated one, and the payload assertions then pass on a request
    the server would have answered for a different project.
    """
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
        "name": name,
        "repo": repo,
        "ready": (name_valid and not registry_has),
        "validations": {"name_valid": name_valid, "name_error": name_error,
                        "repo_given": bool(repo)},
        "collisions": collisions,
        "blockers": blockers,
        "steps": [] if registry_has else ["repo", "board", "orchestrator",
                                          "session"],
        "would_create": ({"profile": f"{name}-orch", "session": name,
                          "board": name} if name else {}),
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
        # The validity question goes to the PRODUCTION predicate, not a private
        # heuristic: a fake with its own idea of a valid name can disagree with
        # the handler's and the pair then proves nothing about either.
        name_error = routes_bootstrap._validate_name(name)
        return _make_plan(name=name, repo=repo,
                          registry_has=state["plan_registry_has"],
                          name_valid=name_error is None,
                          name_error=name_error)

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
# GET /v1/projects/new/plan — the query shape the DISPATCHER actually passes
# --------------------------------------------------------------------------- #
# ``api_server._parse_path`` runs ``parse_qs`` and collapses every key to
# ``v[-1]``, and ``_dispatch`` merges matched path groups into that same dict, so
# a handler is handed ONE SCALAR STRING per key. The three plan cases above pass
# either way, because the ``fakes`` plan returns a hardcoded ``acme``/``/tmp/acme``
# no matter what it is asked about: the payload could not show the difference.
# These two read the BACKING's own recorded arguments, which is the only place
# the truncation was visible. Under the previous
# ``query.get("name", [""])[0]`` both are red with ``('a', '/')`` / ``('d', '')``.

def test_the_plan_validates_the_whole_name_not_its_first_character(
        running, token, fakes):
    """``?name=acme`` must reach the validation as ``acme``, not ``a``.

    The name is also the profile, session and board the confirm will mint, so a
    one-character read is not a cosmetic slip: the review screen shows a plan for
    a different project than the operator typed.
    """
    status, payload = _req(running, token, PLAN_PATH)
    assert status == 200
    assert [call[:2] for call in fakes["plan_calls"]] == [("acme", "/tmp/acme")], (
        "the handler handed the backing a truncated name/repo — it is indexing a "
        "scalar query value as a list")
    assert payload["ready"] is True


def test_a_name_that_is_valid_only_in_full_is_never_ready(running, token, fakes):
    """The case the truncation got WRONG, not merely different.

    ``demo/x`` is an invalid project name, but its first character ``d`` is a
    valid one, so the old read answered ``ready: true`` with a plan to mint
    ``d-orch`` — the wizard green-lights a project the confirm would reject.
    """
    status, payload = _req(running, token,
                           "/v1/projects/new/plan?name=demo%2Fx&repo=/tmp/x")
    assert status == 200
    assert [call[:2] for call in fakes["plan_calls"]] == [("demo/x", "/tmp/x")]
    assert payload["ready"] is False, (
        "an invalid name was reported ready: the plan validated one character")
    assert payload["validations"]["name_valid"] is False
    assert any("lowercase" in b for b in payload["blockers"])


def test_a_blank_plan_param_is_the_same_request_as_no_param(running, token,
                                                           fakes):
    """``parse_qs`` drops blanks BEFORE any handler sees them.

    So ``?repo=`` is not an empty-string scalar the handler must survive — it is
    the key being absent. Pinned here because the plan handler's own list-indexing
    read turned that absent value into the filesystem root; the same rule is
    pinned for /v1/cluster/history in test_cluster_history_parity.py.
    """
    for path in ("/v1/projects/new/plan?name=acme",
                 "/v1/projects/new/plan?name=acme&repo=",
                 "/v1/projects/new/plan?name=acme&repo"):
        fakes["plan_calls"].clear()
        status, payload = _req(running, token, path)
        assert status == 200, (path, payload)
        assert [call[:2] for call in fakes["plan_calls"]] == [("acme", "")], path
        assert payload["validations"]["repo_given"] is False, path


def test_a_missing_name_is_a_blocker_not_a_500(running, token, fakes):
    """The degraded promise: the plan answers 200 with the reason, never an error.

    ``name`` absent means ``query.get("name")`` is absent too, and the old
    ``[""][0]`` default happened to survive that — but only by accident, and the
    sibling ``""`` case raised. The wizard's review screen needs the blocker text.
    """
    status, payload = _req(running, token, "/v1/projects/new/plan")
    assert status == 200
    assert payload["ready"] is False
    assert payload["blockers"] == ["name is required"]
    assert payload["speak"].startswith("1 blocker")


def test_the_plan_degrades_to_speak_only_when_the_backing_fails(
        running, token, fakes, monkeypatch):
    """The honest degradation stays: a failing backing is a 200 with ``speak``
    alone — no ``ready``, so a caller cannot read a missing key as a go-ahead."""
    def boom(name, repo, registry_path=None):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(routes_bootstrap, "_backing_plan", boom)
    status, payload = _req(running, token, PLAN_PATH)
    assert status == 200
    assert set(payload) == {"speak"}
    assert payload["speak"] == "Project plan unavailable."


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
