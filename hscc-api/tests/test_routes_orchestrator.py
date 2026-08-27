"""Unit tests for hscc-api Phase C2 — job-based orchestrator chat.

The suite is hermetic: the orchestrator invocation is fully stubbed via
monkeypatch on the ``_backing_resolve`` / ``_backing_invoke`` seams, so NO test
spawns a real agent, invokes an LLM, consumes tokens, or dispatches real work.
Both the project→orchestrator resolution and the hermes transport are faked.

Handlers are driven over real loopback HTTP (port 0) like the A1-A4 suites, so
auth and the route dispatcher are exercised end-to-end.

The chat POST is now JOB-BASED (Phase 2 of t_bc242def): it validates/resolves
synchronously, spawns a background thread that invokes the orchestrator, and
returns 202 with a job_id. The reply is read via ``GET /v1/orchestrator/chat/
{id}`` which reports queued/running/done + honest elapsed.

Coverage required by the card:
  * missing ``confirm`` -> 409 AND no job created / no backing called;
  * missing/empty ``prompt`` -> 400;
  * unknown project -> 400;
  * absent/null project => resolves to the ``general`` orchestrator;
  * success: POST -> 202 with job_id; GET -> running then done with the reply
    + profile/session used + honest elapsed;
  * backing failure -> job lands in a terminal error state, not a leak;
  * GET unknown job id -> 404;
  * auth still enforced (401) on both POST and GET.
"""
import json
import threading
import time
import types

import pytest

import api_server
import routes_orchestrator
from routes_orchestrator import _ChatJob, _run_job


# --------------------------------------------------------------------------- #
# Fixtures: a running server + hermetic fakes for resolve/invoke
# --------------------------------------------------------------------------- #

@pytest.fixture
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
        # Optional: block the fake invoke until this event is set (lets a test
        # observe the job in a running state). Default None = instant reply.
        "invoke_gate": None,
    }
    backing = {
        "resolve": lambda project, registry_path: (
            state["resolve_calls"].append((project, registry_path))
            or state["resolved"]
        ),
        "invoke": _make_invoke(state),
    }
    _install(monkeypatch, backing)
    return state


def _make_invoke(state):
    def invoke(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT):
        state["invoke_calls"].append((profile, session, prompt, timeout))
        gate = state["invoke_gate"]
        if gate is not None:
            gate.wait(timeout=5)          # hold the job in `running`
        if state["invoke_result"] is not None:
            reply, *_ = state["invoke_result"]
            return state["invoke_result"]
        return ("I dispatched a card for you.", profile, session)
    return invoke


def _install(monkeypatch, backing: dict):
    """Point each ``_backing_*`` module function at the given fake."""
    for name, fn in backing.items():
        monkeypatch.setattr(routes_orchestrator, f"_backing_{name}", fn)


@pytest.fixture
def running(tmp_path, fakes):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path), addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture(autouse=True)
def _clear_jobs():
    """Start each test with an empty job store.

    ``_jobs`` is a module-global (the server is long-lived in production, so the
    store intentionally persists across connections there). Here we clear it
    before every test so assertions like ``not routes_orchestrator._jobs`` and
    per-test job ids are deterministic regardless of run order.
    """
    with routes_orchestrator._jobs_lock:
        routes_orchestrator._jobs.clear()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _request(running, token, method, path, body=None, timeout=5):
    import http.client

    conn = http.client.HTTPConnection(running.host, running.port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        payload: dict = json.loads(data) if data else {}
    except ValueError:
        payload = {"raw": data}
    return resp.status, payload


def _post(running, token, path="/v1/orchestrator/chat", body=None):
    return _request(running, token, "POST", path, body=body)


def _get(running, token, path):
    return _request(running, token, "GET", path)


def _poll_done(running, token, job_id, timeout=5.0):
    """GET the job until it reaches a terminal state; return (status, payload)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = _get(running, token, f"/v1/orchestrator/chat/{job_id}")
        assert status == 200
        if payload.get("status") in ("done", "timeout", "unavailable", "error"):
            return payload
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


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
    assert not routes_orchestrator._jobs   # no job created


def test_chat_confirm_false_is_409(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "go build X", "confirm": False,
    })
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["invoke_calls"] == []
    assert not routes_orchestrator._jobs


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
    assert not routes_orchestrator._jobs


def test_chat_empty_prompt_400(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "   ", "confirm": True,
    })
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert fakes["invoke_calls"] == []
    assert not routes_orchestrator._jobs


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
    assert not routes_orchestrator._jobs


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
    assert not routes_orchestrator._jobs


# --------------------------------------------------------------------------- #
# POST returns a job; GET reports running -> done with honest elapsed
# --------------------------------------------------------------------------- #

def test_chat_absent_project_resolves_general(running, token, fakes):
    """No project in the body -> resolve with None -> the general orchestrator."""
    fakes["resolved"] = {"project": "general", "profile": "general-orch",
                         "session": "general", "board": "default", "repo": None}
    status, payload = _post(running, token, body={
        "prompt": "hi", "confirm": True,
    })
    assert status == 202
    assert payload["status"] == "queued"
    assert payload["job_id"]
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
    assert status == 202
    assert payload["profile"] == "general-orch"
    assert fakes["resolve_calls"][0][0] is None


def test_chat_resolves_named_project(running, token, fakes):
    """A named project resolves to its <project>-orch profile + named session."""
    project, profile, session = "hscc", "hscc-orch", "hscc"
    fakes["resolved"] = {"project": project, "profile": profile,
                         "session": session, "board": "hscc", "repo": "/tmp/hscc"}
    status, payload = _post(running, token, body={
        "project": project, "prompt": "go build X", "confirm": True,
    })
    assert status == 202
    faked = _poll_done(running, token, payload["job_id"])
    assert fakes["invoke_calls"][0][0] == profile
    assert fakes["invoke_calls"][0][1] == session
    assert payload["job_id"] and faked["job_id"] == payload["job_id"]
    assert faked["status"] == "done"


def test_chat_success_full_job_cycle(running, token, fakes):
    """POST -> 202; GET shows an honest lifecycle and the reply when done."""
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "go build X", "confirm": True,
    })
    assert status == 202
    job_id = payload["job_id"]
    assert payload["status"] in ("queued", "running")
    assert payload["elapsed"] == 0.0

    faked = _poll_done(running, token, job_id)
    assert faked["status"] == "done"
    assert faked["reply"] == "I dispatched a card for you."
    assert faked["profile"] == "hscc-orch"
    assert faked["session"] == "hscc"
    assert faked["speak"]
    assert faked["elapsed"] >= 0.0
    # The orchestrator was messaged with the right args exactly once.
    assert fakes["invoke_calls"] == [
        ("hscc-orch", "hscc", "go build X", routes_orchestrator._DEFAULT_TIMEOUT)
    ]


def test_chat_shortens_long_reply_in_speak(running, token, fakes):
    long_reply = "word " * 40  # 200 chars
    fakes["invoke_result"] = (long_reply, "hscc-orch", "hscc")
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    faked = _poll_done(running, token, payload["job_id"])
    assert faked["reply"] == long_reply
    assert len(faked["speak"]) < len(long_reply)


def test_get_job_reports_running_while_invoke_blocks(running, token, fakes):
    """Hold the fake invoke on a gate so we can observe a `running` job."""
    gate = threading.Event()
    fakes["invoke_gate"] = gate
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    job_id = payload["job_id"]
    # Give the worker a moment to flip queued -> running.
    running_payload = {}
    deadline = time.time() + 2
    while time.time() < deadline:
        _, running_payload = _get(running, token, f"/v1/orchestrator/chat/{job_id}")
        if running_payload["status"] == "running":
            break
        time.sleep(0.005)
    assert running_payload["status"] == "running"
    assert "reply" not in running_payload
    assert running_payload["elapsed"] >= 0.0
    # Release the gate -> job reaches done.
    gate.set()
    faked = _poll_done(running, token, job_id)
    assert faked["status"] == "done"
    assert faked["reply"] == "I dispatched a card for you."


# --------------------------------------------------------------------------- #
# Backing failures -> job lands in a terminal error state (no leak)
# --------------------------------------------------------------------------- #

def test_chat_backing_timeout_job_error(running, token, fakes, monkeypatch):
    def timed_out(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT):
        fakes["invoke_calls"].append((profile, session, prompt, timeout))
        raise routes_orchestrator._OrchestratorTimeout("did not reply in 180s")
    _install(monkeypatch, {"invoke": timed_out})
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    faked = _poll_done(running, token, payload["job_id"])
    assert faked["status"] == "timeout"
    assert faked["error"]["code"] == "orchestrator_timeout"
    assert "Traceback" not in str(faked)


def test_timeout_job_elapsed_frozen_at_termination(running, token, fakes, monkeypatch):
    """t_023d4c4c Bug 2: a terminal job's elapsed is frozen at termination.

    Regression for the contradiction where status/error said 'did not reply
    within 180s' while elapsed reported ~50 min. The failure was that _job_dict
    computed elapsed for error states as live ``time.time() - submitted_at``
    (i.e. at POLL time), so a late GET after a timeout reported the minutes
    spent polling, not the seconds the job actually ran. Now any terminal state
    freezes elapsed at ``finished_at - submitted_at``. We prove it by marking a
    job timed-out, then reading it again long after (simulating a late poll):
    elapsed must still equal finished_at - submitted_at, and must match the
    timeout the message describes — NOT the wall time between submission and
    the late read.
    """
    def timed_out(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT):
        fakes["invoke_calls"].append((profile, session, prompt, timeout))
        raise routes_orchestrator._OrchestratorTimeout("did not reply in 180s")
    _install(monkeypatch, {"invoke": timed_out})
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    job_id = payload["job_id"]
    faked = _poll_done(running, token, job_id)
    assert faked["status"] == "timeout"
    terminated_elapsed = faked["elapsed"]

    with routes_orchestrator._jobs_lock:
        job = routes_orchestrator._jobs[job_id]
        frozen = job.finished_at - job.submitted_at

    # Simulate an operator polling LATE — well after the job terminated. The
    # frozen value must NOT have grown into the polling window.
    time.sleep(0.05)
    late = _get(running, token, f"/v1/orchestrator/chat/{job_id}")[1]
    assert late["status"] == "timeout"
    assert late["elapsed"] == terminated_elapsed == round(frozen, 3)
    # And it agrees with the message — the timeout the message names (~180s in
    # the fake), not ~50 min of post-termination polling.
    assert late["elapsed"] < 5.0


def test_chat_backing_unavailable_job_error(running, token, fakes, monkeypatch):
    def unavail(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT):
        fakes["invoke_calls"].append((profile, session, prompt, timeout))
        raise routes_orchestrator._OrchestratorUnavailable(
            "orchestrator session 'hscc' not ready"
        )
    _install(monkeypatch, {"invoke": unavail})
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    faked = _poll_done(running, token, payload["job_id"])
    assert faked["status"] == "unavailable"
    assert faked["error"]["code"] == "orchestrator_unavailable"
    assert "Traceback" not in str(faked)


def test_chat_backing_invocation_error_job_error(running, token, fakes, monkeypatch):
    def failed(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT):
        fakes["invoke_calls"].append((profile, session, prompt, timeout))
        raise routes_orchestrator._OrchestratorInvocationError("empty reply")
    _install(monkeypatch, {"invoke": failed})
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    faked = _poll_done(running, token, payload["job_id"])
    assert faked["status"] == "error"
    assert faked["error"]["code"] == "orchestrator_error"
    assert "Traceback" not in str(faked)


def test_chat_unexpected_backing_exception_job_error(running, token, fakes, monkeypatch):
    """A truly unexpected exception must become a clean job error, not a leak."""
    def boom(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT):
        fakes["invoke_calls"].append((profile, session, prompt, timeout))
        raise RuntimeError("secret-token-in-detail")
    _install(monkeypatch, {"invoke": boom})
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    faked = _poll_done(running, token, payload["job_id"])
    assert faked["status"] == "error"
    assert faked["error"]["code"] == "orchestrator_error"
    # The internal detail must NOT echo the exception (no secret leak).
    assert "secret-token-in-detail" not in str(faked)


# --------------------------------------------------------------------------- #
# GET /v1/orchestrator/chat/{id} — 404 for unknown, auth on GET
# --------------------------------------------------------------------------- #

def test_get_unknown_job_404(running, token):
    status, payload = _get(running, token, "/v1/orchestrator/chat/nope-123")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_get_job_auth_401(running, fakes):
    status, _ = _get(running, token=None, path="/v1/orchestrator/chat/chat-1")
    assert status == 401


def test_post_auth_401(running, fakes):
    status, _ = _post(running, token=None, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 401
    assert fakes["invoke_calls"] == []
    assert fakes["resolve_calls"] == []


# --------------------------------------------------------------------------- #
# Direct _run_job unit tests (deterministic, no HTTP)
# --------------------------------------------------------------------------- #

def test_run_job_done_sets_reply_and_speak(monkeypatch):
    job = _ChatJob("chat-99", "hscc", "hscc-orch", "hscc", "hi")
    monkeypatch.setattr(
        routes_orchestrator, "_backing_invoke",
        lambda p, s, q, timeout=routes_orchestrator._DEFAULT_TIMEOUT: ("the answer", p, s),
    )
    _run_job(job)
    d = routes_orchestrator._job_dict(job)
    assert d["status"] == "done"
    assert d["reply"] == "the answer"
    assert d["speak"].startswith("hscc-orch says:")
    assert d["elapsed"] >= 0.0


def test_run_job_done_then_get_is_idempotent(running, token, fakes):
    """A completed job stays done and stable across repeated GETs."""
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    job_id = payload["job_id"]
    first = _poll_done(running, token, job_id)
    second = _poll_done(running, token, job_id)
    assert first["status"] == "done" and second["status"] == "done"
    assert first["reply"] == second["reply"]
    # elapsed is frozen at completion, so both reads report the same value.
    assert first["elapsed"] == second["elapsed"]


def test_get_on_a_given_job_is_readonly(running, token, fakes):
    """GET never invokes the orchestrator — it only reports a prior job."""
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    _poll_done(running, token, payload["job_id"])
    _get(running, token, f"/v1/orchestrator/chat/{payload['job_id']}")
    # The worker invoked exactly once (the POST's background thread); GETs
    # added nothing.
    assert len(fakes["invoke_calls"]) == 1


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
    assert "timeout" in captured["kw"] and captured["kw"]["timeout"] == routes_orchestrator._DEFAULT_TIMEOUT
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


def test_backing_invoke_strips_channel_notice_lines(monkeypatch):
    """`-Q` can emit a cwd-restore notice on stdout BEFORE the reply; the parsed
    reply must not be polluted by it (observed real transport, t_bc242def)."""
    import subprocess as _sp

    def with_notice(*a, **k):
        _p = types.SimpleNamespace(returncode=0)
        _p.stderr = ""
        _p.stdout = ("↪ restored workspace dir: /Users/desac\n\n"
                     "the actual reply here\n")
        return _p
    monkeypatch.setattr(_sp, "run", with_notice)
    reply, _, _ = routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")
    assert "restored workspace" not in reply
    assert reply == "the actual reply here"


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


# --------------------------------------------------------------------------- #
# t_023d4c4c Bug 1: configurable timeout via chat_timeout in ~/.hscc/api.json
# --------------------------------------------------------------------------- #

def _ctx_with_config(hscc_dir, config_dict):
    """A minimal ctx stub matching the fields _chat_timeout reads."""
    return types.SimpleNamespace(config=config_dict, hscc_dir=hscc_dir)


def test_chat_timeout_default_when_unconfigured(tmp_path):
    """No api.json, no config dict -> the module default (600s)."""
    ctx = _ctx_with_config(str(tmp_path), {})
    assert routes_orchestrator._chat_timeout(ctx) == \
        routes_orchestrator._DEFAULT_TIMEOUT == 600.0


def test_chat_timeout_reads_api_json(tmp_path):
    """A chat_timeout key in ~/.hscc/api.json overrides the default."""
    (tmp_path / "api.json").write_text(json.dumps({"chat_timeout": 900}))
    ctx = _ctx_with_config(str(tmp_path), {})
    assert routes_orchestrator._chat_timeout(ctx) == 900.0


def test_chat_timeout_config_dict_highest_precedence(tmp_path):
    """An explicit config-dict value beats the api.json file."""
    (tmp_path / "api.json").write_text(json.dumps({"chat_timeout": 900}))
    ctx = _ctx_with_config(str(tmp_path), {"chat_timeout": 40})
    assert routes_orchestrator._chat_timeout(ctx) == 40.0


def test_chat_timeout_rejects_bad_values(tmp_path):
    """A malformed chat_timeout is a hard config error, not a silent guess."""
    ctx = _ctx_with_config(str(tmp_path), {"chat_timeout": "soon"})
    with pytest.raises(RuntimeError):
        routes_orchestrator._chat_timeout(ctx)
    ctx2 = _ctx_with_config(str(tmp_path), {"chat_timeout": 0})
    with pytest.raises(RuntimeError):
        routes_orchestrator._chat_timeout(ctx2)
    ctx3 = _ctx_with_config(str(tmp_path), {"chat_timeout": -5})
    with pytest.raises(RuntimeError):
        routes_orchestrator._chat_timeout(ctx3)


def test_chat_uses_configured_timeout_in_invoke(tmp_path, fakes):
    """A configured chat_timeout flows all the way into the backing invoke."""
    (tmp_path / "api.json").write_text(json.dumps({"chat_timeout": 42.0}))
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path), addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    try:
        tok = api_server.load_token(tmp_path)
        status, payload = _post(srv, tok, body={
            "project": "hscc", "prompt": "hi", "confirm": True,
        })
        assert status == 202
        _poll_done(srv, tok, payload["job_id"])
        # The recorded invoke was called with the CONFIGURED timeout (42s),
        # not the 600s default.
        assert fakes["invoke_calls"][0][3] == 42.0
    finally:
        srv.server.shutdown()
        srv.server.server_close()
