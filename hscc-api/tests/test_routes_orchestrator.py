"""Unit tests for hscc-api Phase C2 — POST /v1/orchestrator/chat.

The suite is hermetic: the orchestrator invocation is fully stubbed via
monkeypatch on the ``_backing_resolve`` / ``_backing_invoke`` seams, so NO test
spawns a real agent, invokes an LLM, consumes tokens, or dispatches real work.
Both the project→orchestrator resolution and the hermes transport are faked.

Handlers are driven over real loopback HTTP (port 0) like the A1-A4 suites, so
auth and the route dispatcher are exercised end-to-end.

Coverage required by the card:
  * missing ``confirm`` -> 409 AND the backing call was NOT made;
  * missing ``prompt`` -> 400;
  * unknown project -> 400;
  * absent project => resolves to the ``general`` orchestrator;
  * success path returns the reply + profile/session used;
  * backing failure -> clean 5xx, no traceback leak;
  * auth still enforced (401).
"""

import json
import types

import pytest

import api_server
import routes_orchestrator


# --------------------------------------------------------------------------- #
# Fixtures: a running server + hermetic fakes for resolve/invoke
# --------------------------------------------------------------------------- #

@ pytest.fixture
def fakes(monkeypatch):
    """Install hermetic fakes; returns a dict of the call-records + seams.

    ``state`` exposes the invocation records so a test can assert whether the
    backing was called (and with what). The registries used by the real
    repository are never touched — ``_backing_resolve`` and ``_backing_invoke``
    are both replaced, so nothing reads disk, spawns an agent, or burns tokens.
    """
    state = {
        "resolved": {"project": "hscc", "profile": "hscc-orch",
                     "session": "hscc", "board": "hscc",
                     "repo": "/tmp/hscc"},
        "resolve_calls": [],
        "invoke_calls": [],
        # The real transport reports back the profile/session it used, so the
        # fake echoes the profile/session it was invoked with.
        "invoke_result": None,
    }
    backing = {
        "resolve": lambda project, registry_path: (
            state["resolve_calls"].append((project, registry_path))
            or state["resolved"]
        ),
        "invoke": lambda profile, session, prompt, timeout=180.0: (
            state["invoke_calls"].append((profile, session, prompt, timeout))
            or (state["invoke_result"]
                or ("I dispatched a card for you.", profile, session))
        ),
    }
    _install(monkeypatch, backing)
    return state


def _install(monkeypatch, backing: dict):
    """Point each ``_backing_*`` module function at the given fake."""
    for name, fn in backing.items():
        monkeypatch.setattr(routes_orchestrator, f"_backing_{name}", fn)


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


def _post(running, token, path="/v1/orchestrator/chat", body=None, method="POST"):
    import http.client

    conn = http.client.HTTPConnection(running.host, running.port, timeout=5)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode("utf-8")
    else:
        headers["Content-Type"] = "application/json"
        raw = b""
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
# Confirm gate
# --------------------------------------------------------------------------- #

def test_chat_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "go build X",
    })
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["resolve_calls"] == []   # resolver NOT consulted
    assert fakes["invoke_calls"] == []    # orchestrator NOT messaged


def test_chat_confirm_false_is_409(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "go build X", "confirm": False,
    })
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["invoke_calls"] == []


# --------------------------------------------------------------------------- #
# Validation (400)
# --------------------------------------------------------------------------- #

def test_chat_missing_prompt_400(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "confirm": True,
    })
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "prompt" in payload["error"]["message"]
    assert fakes["invoke_calls"] == []


def test_chat_empty_prompt_400(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "   ", "confirm": True,
    })
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["invoke_calls"] == []


# --------------------------------------------------------------------------- #
# Resolution (project -> orchestrator)
# --------------------------------------------------------------------------- #

def test_chat_unknown_project_400(running, token, fakes, monkeypatch):
    def raises(project, registry_path):
        fakes["resolve_calls"].append((project, registry_path))
        raise routes_orchestrator.UnknownProjectError(
            "unknown project 'bogus': not in the registry and not the "
            "'general' sentinel"
        )
    _install(monkeypatch, {"resolve": raises})
    status, payload = _post(running, token, body={
        "project": "bogus", "prompt": "hi", "confirm": True,
    })
    assert status == 400
    assert payload["error"]["code"] == "unknown_project"
    assert "bogus" in payload["error"]["message"]
    assert fakes["invoke_calls"] == []  # never got that far


def test_chat_project_without_board_400(running, token, fakes, monkeypatch):
    """A registry project with no board cannot route an orchestrator -> 400."""
    def raises(project, registry_path):
        fakes["resolve_calls"].append((project, registry_path))
        raise routes_orchestrator.OrchestratorError(
            "project 'x' has no 'board' in the registry"
        )
    _install(monkeypatch, {"resolve": raises})
    status, payload = _post(running, token, body={
        "project": "x", "prompt": "hi", "confirm": True,
    })
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "board" in payload["error"]["message"]


def test_chat_absent_project_resolves_general(running, token, fakes):
    """No project in the body -> resolve with None -> the 'general' orchestrator."""
    fakes["resolved"] = {"project": "general", "profile": "general-orch",
                         "session": "general", "board": "default", "repo": None}
    status, payload = _post(running, token, body={
        "prompt": "hi", "confirm": True,
    })
    assert status == 200
    # resolver was consulted with None (project absent -> general)
    assert fakes["resolve_calls"][0][0] is None
    assert payload["profile"] == "general-orch"
    assert payload["session"] == "general"


def test_chat_null_project_resolves_general(running, token, fakes):
    """Explicit null project -> the 'general' orchestrator."""
    fakes["resolved"] = {"project": "general", "profile": "general-orch",
                         "session": "general", "board": "default", "repo": None}
    status, payload = _post(running, token, body={
        "project": None, "prompt": "hi", "confirm": True,
    })
    assert status == 200
    assert fakes["resolve_calls"][0][0] is None
    assert payload["profile"] == "general-orch"
    assert payload["session"] == "general"


def test_chat_resolves_named_project(running, token, fakes):
    """A named project resolves to its <project>-orch profile + named session."""
    project, profile, session = "hscc", "hscc-orch", "hscc"
    fakes["resolved"] = {"project": project, "profile": profile,
                         "session": session, "board": "hscc", "repo": "/tmp/hscc"}
    status, payload = _post(running, token, body={
        "project": project, "prompt": "go build X", "confirm": True,
    })
    assert status == 200
    assert fakes["invoke_calls"][0][0] == profile
    assert fakes["invoke_calls"][0][1] == session
    assert payload["profile"] == profile
    assert payload["session"] == session


# --------------------------------------------------------------------------- #
# Success path
# --------------------------------------------------------------------------- #

def test_chat_success_returns_reply_and_identity(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "go build X", "confirm": True,
    })
    assert status == 200
    assert payload["reply"] == "I dispatched a card for you."
    assert payload["profile"] == "hscc-orch"
    assert payload["session"] == "hscc"
    assert payload["speak"]
    # The orchestrator was messaged with the right args.
    assert fakes["invoke_calls"] == [
        ("hscc-orch", "hscc", "go build X", routes_orchestrator._DEFAULT_TIMEOUT)
    ]


def test_chat_shortens_long_reply_in_speak(running, token, fakes):
    long_reply = "word " * 40  # 200 chars
    fakes["invoke_result"] = (long_reply, "hscc-orch", "hscc")
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 200
    assert payload["reply"] == long_reply          # full reply preserved
    assert len(payload["speak"]) < len(long_reply)  # speak is the short summary


# --------------------------------------------------------------------------- #
# Backing failures -> clean 5xx (no traceback leak, no server crash)
# --------------------------------------------------------------------------- #

def test_chat_backing_timeout_504(running, token, fakes, monkeypatch):
    def timed_out(profile, session, prompt, timeout=180.0):
        fakes["invoke_calls"].append((profile, session, prompt, timeout))
        raise routes_orchestrator._OrchestratorTimeout("did not reply in 180s")
    _install(monkeypatch, {"invoke": timed_out})
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 504
    assert payload["error"]["code"] == "orchestrator_timeout"
    # No traceback is leaked into the response body.
    assert "Traceback" not in str(payload)


def test_chat_backing_unavailable_503(running, token, fakes, monkeypatch):
    def unavail(profile, session, prompt, timeout=180.0):
        fakes["invoke_calls"].append((profile, session, prompt, timeout))
        raise routes_orchestrator._OrchestratorUnavailable(
            "orchestrator session 'hscc' not ready"
        )
    _install(monkeypatch, {"invoke": unavail})
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 503
    assert payload["error"]["code"] == "orchestrator_unavailable"
    assert "Traceback" not in str(payload)


def test_chat_backing_invocation_error_502(running, token, fakes, monkeypatch):
    def failed(profile, session, prompt, timeout=180.0):
        fakes["invoke_calls"].append((profile, session, prompt, timeout))
        raise routes_orchestrator._OrchestratorInvocationError("empty reply")
    _install(monkeypatch, {"invoke": failed})
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 502
    assert payload["error"]["code"] == "orchestrator_error"
    assert "Traceback" not in str(payload)


def test_chat_unexpected_backing_exception_500(running, token, fakes, monkeypatch):
    """A truly unexpected exception must become a clean 500, not a crash/leak."""
    def boom(profile, session, prompt, timeout=180.0):
        fakes["invoke_calls"].append((profile, session, prompt, timeout))
        raise RuntimeError("secret-token-in-detail")
    _install(monkeypatch, {"invoke": boom})
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 500
    assert payload["error"]["code"] == "internal_error"
    # The internal message must NOT echo the exception detail (no secret leak).
    assert "secret-token-in-detail" not in str(payload)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def test_chat_auth_401(running, fakes):
    status, payload = _post(running, token=None, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert fakes["invoke_calls"] == []
    assert fakes["resolve_calls"] == []


# --------------------------------------------------------------------------- #
# The invocation seam runs hermes as an argv LIST (no shell injection)
# --------------------------------------------------------------------------- #

def test_backing_invoke_passes_argv_as_list(monkeypatch):
    """The real _backing_invoke must shell out with an argv LIST — never a
    string. We capture the argv by faking subprocess.run and assert the prompt
    arrives as a single element, so a prompt like ``uname; rm -rf /`` cannot be
    executed by a shell."""
    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            self.returncode = 0
            self.stdout = "the orchestrator reply\n"
            self.stderr = ""

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", _FakeProc)
    reply, profile, session = routes_orchestrator._backing_invoke(
        "hscc-orch", "hscc", "uname; rm -rf /"
    )
    argv = captured["argv"]
    assert reply == "the orchestrator reply"
    assert argv[0] == "hermes"
    assert "-p" in argv and "hscc-orch" in argv
    assert "--continue" in argv
    # The dangerous prompt is a SINGLE argv element, never interpolated.
    assert "uname; rm -rf /" in argv
    assert "timeout" in captured["kw"] and captured["kw"]["timeout"] == 180.0
    # --continue carries the named session so the thread persists.
    assert "hscc" in argv


def test_backing_invoke_timeout_raises(monkeypatch):
    import subprocess as _sp

    def raise_timeout(*a, **k):
        raise _sp.TimeoutExpired(cmd=["hermes"], timeout=1)
    monkeypatch.setattr(_sp, "run", raise_timeout)
    with pytest.raises(routes_orchestrator._OrchestratorTimeout):
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi", timeout=1)


def test_backing_invoke_session_not_found_raises_unavailable(monkeypatch):
    import subprocess as _sp

    def notfound(*a, **k):
        _p = types.SimpleNamespace(returncode=1)
        _p.stderr = "Session not found: hscc\nUse a session ID from a previous CLI run."
        _p.stdout = ""
        return _p
    monkeypatch.setattr(_sp, "run", notfound)
    with pytest.raises(routes_orchestrator._OrchestratorUnavailable):
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")


def test_backing_invoke_empty_reply_raises(monkeypatch):
    import subprocess as _sp

    def empty(*a, **k):
        _p = types.SimpleNamespace(returncode=0)
        _p.stderr = ""
        _p.stdout = "   \n"
        return _p
    monkeypatch.setattr(_sp, "run", empty)
    with pytest.raises(routes_orchestrator._OrchestratorInvocationError):
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")


def test_backing_invoke_missing_hermes_raises_unavailable(monkeypatch):
    import subprocess as _sp

    def missing(*a, **k):
        raise FileNotFoundError("hermes")
    monkeypatch.setattr(_sp, "run", missing)
    with pytest.raises(routes_orchestrator._OrchestratorUnavailable):
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")
