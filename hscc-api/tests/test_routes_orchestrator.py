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
import os
import sys
import tempfile
import threading
import time
import types

import pytest

import api_server
import routes_orchestrator
from routes_orchestrator import _ChatJob, _new_job, _reap_jobs, _run_job


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
    def invoke(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT,
               **kwargs):   # image_data/image_mime forwarded by _run_job
        state["invoke_calls"].append((profile, session, prompt, timeout))
        state.setdefault("invoke_images", []).append(
            (kwargs.get("image_data"), kwargs.get("image_mime")))
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
# Image attachment (t_a779c06f) — validation
# --------------------------------------------------------------------------- #

def test_chat_image_data_without_mime_400(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
        "image_data": "aGVsbG8=",
    })
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "together" in payload["error"]["message"]
    assert fakes["invoke_calls"] == []
    assert not routes_orchestrator._jobs


def test_chat_image_mime_without_data_400(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
        "image_mime": "image/png",
    })
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "together" in payload["error"]["message"]
    assert fakes["invoke_calls"] == []


def test_chat_image_bad_base64_400(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
        "image_data": "not@@base64!!", "image_mime": "image/png",
    })
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "not valid base64" in payload["error"]["message"]
    assert fakes["invoke_calls"] == []


def test_chat_image_non_image_mime_400(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
        "image_data": "aGVsbG8=", "image_mime": "text/plain",
    })
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "image/*" in payload["error"]["message"]
    assert fakes["invoke_calls"] == []


def test_chat_image_empty_400(running, token, fakes):
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
        "image_data": "", "image_mime": "image/png",
    })
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "empty" in payload["error"]["message"]


def test_chat_image_oversized_400(monkeypatch):
    """Just over the decoded cap is rejected BEFORE any job is created. Tested
    at the validation seam (not over HTTP) because shipping a ~27 MB base64 body
    over loopback breaks the synchronous test client (BrokenPipe when the
    server rejects mid-body) — the HTTP path is already covered by the other 400
    cases."""
    monkeypatch.setattr(routes_orchestrator, "_MAX_IMAGE_BYTES", 1024)
    big = b"x" * 1025
    import base64 as _b64
    with pytest.raises(routes_orchestrator.ApiError) as ei:
        routes_orchestrator._validate_image({
            "image_data": _b64.b64encode(big).decode(),
            "image_mime": "image/png",
        })
    assert ei.value.status == 400
    assert "exceeds" in ei.value.message
    assert "MiB" in ei.value.message


def test_chat_image_success_runs_job_with_image(running, token, fakes):
    """A valid image attachment is forwarded to the backing invoke, which the
    transport then turns into ``--image <file>``. The 202 + job flow stays the
    same as a plain chat."""
    import base64 as _b64
    png_data = b"\x89PNG\r\n\x1a\n" + b"fakepixels" * 10
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "what went wrong on the dashboard",
        "confirm": True,
        "image_data": _b64.b64encode(png_data).decode(),
        "image_mime": "image/png",
    })
    assert status == 202
    job_id = payload["job_id"]
    done = _poll_done(running, token, job_id)
    assert done["status"] == "done"
    assert done["reply"] == "I dispatched a card for you."
    # The fake invoke saw our exact image bytes + mime.
    assert fakes["invoke_images"] == [(png_data, "image/png")]
    assert fakes["invoke_calls"] == [
        ("hscc-orch", "hscc", "what went wrong on the dashboard",
         routes_orchestrator._DEFAULT_TIMEOUT)
    ]


def test_chat_image_success_normalizes_mime(running, token, fakes):
    """MIME case is normalized to lowercase before it reaches the invoke."""
    import base64 as _b64
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
        "image_data": _b64.b64encode(b"jpegdata").decode(),
        "image_mime": "image/JPEG",
    })
    assert status == 202
    done = _poll_done(running, token, payload["job_id"])
    assert done["status"] == "done"
    assert fakes["invoke_images"] == [(b"jpegdata", "image/jpeg")]


def test_chat_plain_has_no_image(running, token, fakes):
    """A plain (no image) chat must NOT be mistaken for one carrying an image —
    regression guard so existing clients are unaffected."""
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hello", "confirm": True,
    })
    assert status == 202
    done = _poll_done(running, token, payload["job_id"])
    assert done["status"] == "done"
    assert fakes["invoke_images"] == [(None, None)]


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
    def timed_out(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT,
                  **kwargs):
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
    def timed_out(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT,
                  **kwargs):
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
    def unavail(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT,
                **kwargs):
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
    def failed(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT,
               **kwargs):
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
    def boom(profile, session, prompt, timeout=routes_orchestrator._DEFAULT_TIMEOUT,
             **kwargs):
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
        lambda p, s, q, timeout=routes_orchestrator._DEFAULT_TIMEOUT,
               **kwargs: ("the answer", p, s),
    )
    _run_job(job)
    d = routes_orchestrator._job_dict(job)
    assert d["status"] == "done"
    assert d["reply"] == "the answer"
    assert d["speak"].startswith("hscc-orch says:")
    assert d["elapsed"] >= 0.0


# --------------------------------------------------------------------------- #
# Job-store reaping (t_2bb97a26): terminal jobs must not leak unboundedly.
# --------------------------------------------------------------------------- #

def test_reap_evicts_terminal_job_past_retention():
    """A terminal job finished longer ago than the retention window is reaped."""
    old = _ChatJob("chat-1", "hscc", "hscc-orch", "hscc", "hi")
    old.finished_at = time.time() - routes_orchestrator._JOB_RETENTION_SECONDS - 1
    fresh = _ChatJob("chat-2", "hscc", "hscc-orch", "hscc", "hi")
    fresh.finished_at = time.time()
    with routes_orchestrator._jobs_lock:
        routes_orchestrator._jobs.clear()
        routes_orchestrator._jobs["chat-1"] = old
        routes_orchestrator._jobs["chat-2"] = fresh
        _reap_jobs()
        assert "chat-1" not in routes_orchestrator._jobs   # past window -> gone
        assert "chat-2" in routes_orchestrator._jobs       # in window -> kept


def test_reap_keeps_in_retention_and_live_jobs():
    """No terminal job inside the window, and no live job, is ever reaped."""
    oldish = _ChatJob("chat-1", "hscc", "hscc-orch", "hscc", "hi")
    oldish.finished_at = time.time() - routes_orchestrator._JOB_RETENTION_SECONDS + 1
    live = _ChatJob("chat-2", "hscc", "hscc-orch", "hscc", "hi")  # queued, no finished_at
    with routes_orchestrator._jobs_lock:
        routes_orchestrator._jobs.clear()
        routes_orchestrator._jobs["chat-1"] = oldish
        routes_orchestrator._jobs["chat-2"] = live
        _reap_jobs()
        assert "chat-1" in routes_orchestrator._jobs   # inside window -> kept
        assert "chat-2" in routes_orchestrator._jobs   # live -> always kept


def test_reap_is_triggered_by_new_job_opportunistically():
    """``_new_job`` pays down aged terminal jobs as a side effect."""
    old = _ChatJob("chat-1", "hscc", "hscc-orch", "hscc", "hi")
    old.finished_at = time.time() - routes_orchestrator._JOB_RETENTION_SECONDS - 1
    with routes_orchestrator._jobs_lock:
        routes_orchestrator._jobs.clear()
        routes_orchestrator._jobs["chat-1"] = old
    _new_job("hscc", "hscc-orch", "hscc", "hi")
    with routes_orchestrator._jobs_lock:
        # The stale job was reaped as a side effect of the new submission; only
        # the freshly created queued job (and nothing else) remains.
        ids = set(routes_orchestrator._jobs.keys())
    assert "chat-1" not in ids
    assert len(ids) == 1


def test_reap_cap_trims_oldest_terminal_never_live(monkeypatch):
    """Over the hard bound, the OLDEST terminal jobs are trimmed, never live."""
    monkeypatch.setattr(routes_orchestrator, "_JOBS_MAX", 3)
    older = _ChatJob("chat-1", "hscc", "hscc-orch", "hscc", "hi")
    older.finished_at = time.time() - 1000
    newer = _ChatJob("chat-2", "hscc", "hscc-orch", "hscc", "hi")
    newer.finished_at = time.time() - 100
    live = _ChatJob("chat-3", "hscc", "hscc-orch", "hscc", "hi")  # no finished_at
    with routes_orchestrator._jobs_lock:
        routes_orchestrator._jobs.clear()
        routes_orchestrator._jobs["chat-1"] = older
        routes_orchestrator._jobs["chat-2"] = newer
        routes_orchestrator._jobs["chat-3"] = live
        routes_orchestrator._jobs["chat-4"] = \
            _ChatJob("chat-4", "hscc", "hscc-orch", "hscc", "hi")
        _reap_jobs()
        assert "chat-1" not in routes_orchestrator._jobs   # oldest terminal trimmed
        assert "chat-2" in routes_orchestrator._jobs
        assert "chat-3" in routes_orchestrator._jobs       # live never trimmed
        assert "chat-4" in routes_orchestrator._jobs


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
    string. We capture the argv by faking subprocess.Popen and assert the prompt
    arrives as a single element, so a prompt like ``uname; rm -rf /`` cannot be
    executed by a shell.

    The seam changed from ``subprocess.run`` to ``subprocess.Popen`` so the
    running process can be retained and terminated by a stop (t_68432c2d); the
    fake therefore provides the Popen surface ``_backing_invoke`` consumes.
    """
    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["kw"] = kw
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self):
            return ("the orchestrator reply\n", "")

        def poll(self):
            return self.returncode

        def terminate(self):  # noqa: D401
            pass

        def kill(self):  # noqa: D401
            pass

    import subprocess as _sp
    monkeypatch.setattr(_sp, "Popen", _FakeProc)
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
    # _backing_invoke uses Popen now; the timeout is enforced by its own loop.
    assert "timeout" not in captured["kw"]
    # --continue carries the named session so the thread persists.
    assert "hscc" in argv
    # The call opened stdout/stderr as pipes so output can be drained.
    assert captured["kw"]["stdout"] == _sp.PIPE
    assert captured["kw"]["stderr"] == _sp.PIPE


def test_backing_invoke_timeout_raises(monkeypatch):
    import subprocess as _sp

    class _SlowProc:
        def __init__(self, *a, **k):
            self.returncode = None

        def wait(self, timeout=None):
            raise _sp.TimeoutExpired(cmd=["hermes"], timeout=1)

        def poll(self):
            return None

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(_sp, "Popen", _SlowProc)
    with pytest.raises(routes_orchestrator._OrchestratorTimeout):
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi", timeout=1)


def _popen(monkeypatch, stdout="", stderr="", returncode=0, spawn_exc=None):
    """Install a fake ``subprocess.Popen`` returning a controllable proc.

    The production seam changed from ``subprocess.run`` to ``subprocess.Popen``
    (t_68432c2d) so a running turn can be retained + terminated by a stop.
    These tests therefore install a Popen fake whose instances model a process:
    ``wait()`` returns immediately, ``communicate()`` yields the configured
    stdout/stderr, and ``poll()`` reports the returncode. ``spawn_exc`` (e.g.
    ``FileNotFoundError``) makes construction raise, modelling a missing
    binary.
    """
    import subprocess as _sp

    class _FakePopen:
        def __init__(self, argv=None, **kw):
            self.argv = argv
            self.kw = kw
            self.returncode = returncode
            self._out = stdout
            self._err = stderr
            if spawn_exc is not None:
                raise spawn_exc

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self):
            return (self._out, self._err)

        def poll(self):
            return self.returncode

        def terminate(self):  # noqa: D401
            self.returncode = -15

        def kill(self):  # noqa: D401
            self.returncode = -9

    monkeypatch.setattr(_sp, "Popen", _FakePopen)
    return _FakePopen


def test_backing_invoke_session_not_found_raises_unavailable(monkeypatch):
    _popen(monkeypatch, returncode=1,
           stderr="Session not found: hscc\nUse a session ID from a previous "
                  "CLI run.")
    with pytest.raises(routes_orchestrator._OrchestratorUnavailable) as ei:
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")
    assert "not ready" in str(ei.value)


def test_backing_invoke_session_not_found_returncode_0_still_unavailable(monkeypatch):
    """Even with a 0 exit, an explicit 'Session not found' is a missing session.

    Preserves the original combinator: a literal missing-session signal is
    reported as unavailable regardless of the exit code.
    """
    _popen(monkeypatch, returncode=0, stderr="Session not found: hscc")
    with pytest.raises(routes_orchestrator._OrchestratorUnavailable):
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")


def test_backing_invoke_model_unreachable_names_itself(monkeypatch):
    """A nonzero exit that is NOT a missing session must name the real failure.

    The card's incident: the operator's message failed with "session 'hscc'
    not ready" WHILE that session existed — the real cause (e.g. the model
    endpoint unreachable) was discarded behind the blanket returncode check.
    Now any nonzero exit without an explicit 'Session not found' stderr
    becomes a distinct invocation error carrying the ACTUAL stderr tail, so
    the operator sees the true cause and is never sent chasing a ghost.
    """
    _popen(monkeypatch, returncode=1, stderr=(
        "openai.APIConnectionError: Failed to connect to the model "
        "endpoint at api.openai.com:443\nConnection refused"))
    with pytest.raises(routes_orchestrator._OrchestratorInvocationError) as ei:
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")
    msg = str(ei.value)
    # The REAL failure names itself — not a fabricated "not ready".
    assert "not ready" not in msg
    assert "APIConnectionError" in msg
    assert "Connection refused" in msg
    # And it is NOT misreported as an unavailable SESSION.
    assert not isinstance(ei.value, routes_orchestrator._OrchestratorUnavailable)


def test_backing_invoke_nonzero_exit_carries_bounded_stderr_tail(monkeypatch):
    """A crash's real stderr tail is surfaced, bounded, never discarded."""
    big_err = "\n".join(f"line {i} of an internal stack" for i in range(200))
    _popen(monkeypatch, returncode=2, stderr=big_err)
    with pytest.raises(routes_orchestrator._OrchestratorInvocationError) as ei:
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")
    msg = str(ei.value)
    assert "exit 2" in msg
    assert "stderr tail:" in msg
    # Bounded: the tail is capped, not the whole 200-line stack.
    assert len(msg) < 2000
    # The real tail (latest lines) is present, not the head.
    assert "line 199 of an internal stack" in msg


def test_backing_invoke_nonzero_exit_no_stderr(monkeypatch):
    """A nonzero exit with empty stderr still names itself, not 'not ready'."""
    _popen(monkeypatch, returncode=1)
    with pytest.raises(routes_orchestrator._OrchestratorInvocationError) as ei:
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")
    msg = str(ei.value)
    assert "not ready" not in msg
    assert "no stderr captured" in msg


def test_backing_invoke_strips_channel_notice_lines(monkeypatch):
    """`-Q` can emit a cwd-restore notice on stdout BEFORE the reply; the parsed
    reply must not be polluted by it (observed real transport, t_bc242def)."""
    _popen(monkeypatch, stdout=("↪ restored workspace dir: /Users/desac\n\n"
                                "the actual reply here\n"))
    reply, _, _ = routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")
    assert "restored workspace" not in reply
    assert reply == "the actual reply here"


def test_backing_invoke_strips_tirith_preamble(monkeypatch):
    """t_5ed5dfa8 Bug 2: the tirith security preamble must NOT pollute the reply.

    Observed real leak — the reply was ``"⚠ tirith security scanner enabled
    but not available — command scanning will use pattern matching only\n
    IDLETEST"``, i.e. the Hermes startup warning reached the chat transcript and
    would be spoken aloud by Siri intents. `_cprint` -> stdout when no prompt
    app is active (cli.py:7018-7021). `_backing_invoke` must strip it.
    """
    _popen(monkeypatch, stdout=(
        "⚠ tirith security scanner enabled but not available — command "
        "scanning will use pattern matching only\nIDLETEST\n"))
    reply, _, _ = routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")
    assert "tirith" not in reply
    assert "scanner" not in reply
    assert reply == "IDLETEST"


def test_backing_invoke_strips_resume_banner_defensively(monkeypatch):
    """t_5ed5dfa8 Bug 2: the resume banner is stripped if it ever lands on stdout.

    Ordinarily `hermes chat -Q` sends the ``↻ Resumed session ...`` banner to
    stderr (cli_agent_setup_mixin.py:312-315), but we strip it defensively in
    case a config change routes it to stdout — a distinctive line that no model
    reply naturally matches.
    """
    _popen(monkeypatch, stdout=(
        "↻ Resumed session seed-hscc \"hscc\" (21 user messages, 266 total "
        "messages)\nthe answer\n"))
    reply, _, _ = routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")
    assert "Resumed session" not in reply
    assert reply == "the answer"


def test_notice_line_does_not_strip_warning_shaped_reply_content():
    """t_5ed5dfa8 Bug 2 anti-over-strip: a model answer that merely looks like a
    warning (mentions a scanner/session, or starts with '⚠') must PASS THROUGH.

    We anchor on the exact known Hermes preamble shapes, so legitimate reply
    content that resembles a warning is never removed. Each of these is a
    plausible model answer and must survive:
    """
    not_notices = [
        "⚠ The session you asked about was archived.",
        "the tirith scanner is not available on this host, so commands are safe",
        "Resumed session handling is documented in the API.md design doc",
    ]
    for line in not_notices:
        assert not routes_orchestrator._is_notice_line(line), repr(line)


def test_notice_line_strips_known_preamble_shapes():
    """Known Hermes startup lines are stripped; near-misses are not."""
    assert routes_orchestrator._is_notice_line(
        "↪ restored workspace dir: /Users/desac"
    )
    assert routes_orchestrator._is_notice_line(
        "⚠ tirith security scanner enabled but not available — command "
        "scanning will use pattern matching only"
    )
    assert routes_orchestrator._is_notice_line(
        "↻ Resumed session seed-hscc \"hscc\" (21 user messages, 266 total messages)"
    )
    # A resume-shaped line WITHOUT the message counts is NOT the banner -> keep.
    assert not routes_orchestrator._is_notice_line(
        "↻ Resumed session seed-hscc"
    )


def test_backing_invoke_empty_reply_raises(monkeypatch):
    _popen(monkeypatch, stdout="   \n")
    with pytest.raises(routes_orchestrator._OrchestratorInvocationError):
        routes_orchestrator._backing_invoke("hscc-orch", "hscc", "hi")


def test_backing_invoke_missing_hermes_raises_unavailable(monkeypatch):
    _popen(monkeypatch, spawn_exc=FileNotFoundError("hermes"))
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


# --------------------------------------------------------------------------- #
# t_5ed5dfa8 Bug 1: honest busy-reporting when the orchestrator profile is busy
# --------------------------------------------------------------------------- #

def test_busy_notice_singular_and_plural():
    """The busy notice reads grammatically for 1 task and for many."""
    one = routes_orchestrator._busy_notice("hscc-orch", 1)
    many = routes_orchestrator._busy_notice("hscc-orch", 3)
    assert "1 kanban task" in one and "tasks" not in one
    assert "3 kanban tasks" in many
    assert "hscc-orch is busy" in one
    assert "queued behind" in one
    # Must read correctly in both: "with N task(s) running" avoids a plural-verb.
    assert "1 kanban task running" in one
    assert "3 kanban tasks running" in many


def test_backing_busy_tasks_counts_only_assigned_running(monkeypatch, tmp_path):
    """Counts exactly the boards' `running` tasks assigned to the profile.

    Injects a fake ``hermes_cli`` module exposing a ``kanban_db.list_boards()``
    that points at real temp SQLite files, so the seam's real SQL runs against
    controlled data — no live ~/.hermes is touched.
    """
    import sqlite3

    boards = []
    for slug, rows in [
        ("hscc", [("t_a", "hscc-orch", "running")]),
        ("flosana", [("t_b", "some-other", "running"),
                     ("t_c", "hscc-orch", "done")]),
    ]:
        path = tmp_path / f"{slug}.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE tasks (id TEXT, assignee TEXT, status TEXT)")
        conn.executemany(
            "INSERT INTO tasks (id, assignee, status) VALUES (?,?,?)", rows
        )
        conn.commit()
        conn.close()
        boards.append({"slug": slug, "db_path": str(path)})

    fake_kanban = types.SimpleNamespace(list_boards=lambda: boards)
    fake_hermes_cli = types.SimpleNamespace(kanban_db=fake_kanban)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)

    count = routes_orchestrator._backing_busy_tasks("hscc-orch")
    # hscc has 1 running hscc-orch task; flosana's running task is other-assignee
    # and its done hscc-orch task is not running -> only 1 counts.
    assert count == 1

    # A profile with no running tasks anywhere -> 0 (idle).
    assert routes_orchestrator._backing_busy_tasks("ghost-profile") == 0


def test_backing_busy_tasks_failsafe_to_busy(monkeypatch, tmp_path):
    """Unreadable boards -> fail safe toward busy (>=1), never idle."""
    fake_kanban = types.SimpleNamespace(
        list_boards=lambda: [{"db_path": str(tmp_path / "missing.db")}]
    )
    fake_hermes_cli = types.SimpleNamespace(kanban_db=fake_kanban)
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    count = routes_orchestrator._backing_busy_tasks("hscc-orch")
    assert count >= 1


def test_backing_busy_tasks_import_failure_failsafe(monkeypatch):
    """If hermes_cli.kanban_db cannot be imported -> fail safe busy (>=1).

    ``_backing_busy_tasks`` imports ``hermes_cli`` lazily inside the function;
    if that import raises (Hermes not reachable from the API plugin env), it
    must fail SAFE toward busy so we never claim an orchestrator is idle when we
    cannot verify.
    """
    def boom():
        raise ImportError("no hermes_cli")
    fake_mod = types.SimpleNamespace()
    fake_mod.list_boards = boom
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_mod)
    count = routes_orchestrator._backing_busy_tasks("hscc-orch")
    assert count >= 1


def test_chat_post_includes_notice_when_profile_busy(running, token, fakes, monkeypatch):
    """A busy orchestrator -> the 202 POST carries the honest notice, and a
    LIVE (still-running) poll shows it too. The t_a8e9b7ff fix: the notice is
    DROPPED once the job reaches a terminal state — a finished job must never
    carry "busy right now ... poll this job" (that was the lying notice)."""
    monkeypatch.setattr(routes_orchestrator, "_backing_busy_tasks",
                        lambda profile: 2)
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "go build X", "confirm": True,
    })
    assert status == 202
    assert payload["notice"]
    assert "hscc-orch" in payload["notice"]
    assert "2 kanban tasks" in payload["notice"]

    # HOLD the fake invoke on a gate so the job stays LIVE (running) — the
    # notice must ride on a live poll, because the operator is still waiting.
    gate = threading.Event()
    fakes["invoke_gate"] = gate
    # Re-submit so the gate takes effect on a LIVE job we can observe.
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "go build X", "confirm": True,
    })
    job_id = payload["job_id"]
    live = {}
    deadline = time.time() + 2
    while time.time() < deadline:
        _, live = _get(running, token, f"/v1/orchestrator/chat/{job_id}")
        if live["status"] == "running":
            break
        time.sleep(0.005)
    assert live["status"] == "running"
    assert live["notice"] == payload["notice"]   # live poll keeps the notice

    # Release the gate -> the job goes DONE; the notice MUST be dropped now.
    gate.set()
    faked = _poll_done(running, token, job_id)
    assert faked["status"] == "done"
    assert "notice" not in faked          # no stale "busy / poll this job"
    assert "reply" in faked               # the outcome speaks for itself


def test_chat_post_omits_notice_when_profile_idle(running, token, fakes, monkeypatch):
    """An idle orchestrator profile -> no notice (the common fast path)."""
    monkeypatch.setattr(routes_orchestrator, "_backing_busy_tasks",
                        lambda profile: 0)
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "hi", "confirm": True,
    })
    assert status == 202
    assert "notice" not in payload
    faked = _poll_done(running, token, payload["job_id"])
    assert "notice" not in faked
    assert faked["status"] == "done"


# --------------------------------------------------------------------------- #
# t_a8e9b7ff Bug 1: session-bloat guard — detect pre-wedge, rotate preserving
# continuity, surface health, all non-destructive.
# --------------------------------------------------------------------------- #

def _ctx(hscc_dir, config=None):
    return types.SimpleNamespace(config=config or {}, hscc_dir=hscc_dir)


class _FakeProfileDB:
    """Hermetic stand-in for the orchestrator profile's state.db (no hermes).

    Implements exactly the ``SessionDB`` surface ``_guard_session_bloat`` /
    ``_session_health`` / ``_rotate_session`` use, backed by an in-memory
    ``sessions`` table carrying the columns the guard reads. This keeps the
    bloat-guard tests fully self-contained — no dependency on a real
    ``hermes_state``/``hermes_cli`` install on the test path — while exercising
    the guard's real decision + rotation code end-to-end.
    """

    def __init__(self, rows):
        self.closed = False
        self.rows = {}           # id -> row dict (mutable)
        for r in rows:
            self.rows[r["id"]] = dict(r)
            self.rows[r["id"]].setdefault("compression_failure_error", None)
            self.rows[r["id"]].setdefault("compression_fallback_streak", 0)
            self.rows[r["id"]].setdefault("compression_ineffective_count", 0)

    # --- interface the guard uses (name-for-name with SessionDB) ---------- #
    def get_session_by_title(self, title):
        for r in self.rows.values():
            if r.get("title") == title:
                return dict(r)
        return None

    def resolve_session_by_title(self, title):
        match = None
        for r in self.rows.values():
            if r.get("title") == title:
                if match is None or r.get("started_at", 0) > match.get("started_at", 0):
                    match = r
        return match["id"] if match else None

    def get_session(self, sid):
        r = self.rows.get(sid)
        return dict(r) if r else None

    def get_session_title(self, sid):
        r = self.rows.get(sid)
        return r.get("title") if r else None

    def set_session_title(self, sid, title):
        if sid not in self.rows:
            raise ValueError(f"_FakeProfileDB: no session {sid!r}")
        self.rows[sid]["title"] = title
        return True

    def create_session(self, sid, source="cli", model=None, profile_name=None):
        self.rows[sid] = {
            "id": sid, "source": source, "title": None,
            "message_count": 0, "input_tokens": 0,
            "compression_failure_error": None,
            "compression_fallback_streak": 0,
            "compression_ineffective_count": 0,
            "model": model, "profile_name": profile_name,
            "started_at": 0,
        }
        return sid

    def close(self):
        self.closed = True


def _seed_session_db(tmp_path, title, messages=0, input_tokens=0,
                     comp_fail=None, comp_streak=0, comp_ineff=0, model=None):
    """Build a temp :class:`_FakeProfileDB` with ONE titled session carrying the
    given real signals.

    Returns ``(fake_db, sid)``. Installs ``fake_db`` as the stub for
    ``_open_profile_session_db`` and returns a *factory* that hands out a FRESH
    ``_FakeProfileDB`` (sharing the same row store) per call when installed via
    :func:`_install_fake_profile_db`. ``fake_db`` is passed to the test for
    direct inspection; it shares ``rows`` with fresh copies, so state written by
    the guard is visible through it.
    """
    sid = f"seed_{title}_{messages}_{id(_seed_session_db)}"
    base = {
        "id": sid, "source": "cli", "title": title, "profile_name": f"{title}-orch",
        "message_count": messages, "input_tokens": input_tokens,
        "compression_failure_error": comp_fail,
        "compression_fallback_streak": comp_streak,
        "compression_ineffective_count": comp_ineff,
        "model": model or "orchestrator-model", "started_at": 0,
    }
    fake = _FakeProfileDB([base])
    return fake, sid


def _install_fake_profile_db(monkeypatch, fake_db):
    """Point ``_open_profile_session_db`` at a FRESH ``_FakeProfileDB`` per call.

    The guard CLOSES the SessionDB it opens (both the no-rotation and rotate
    paths), so the stub must hand out a NEW connection each call. Each fresh
    instance shares ``rows`` with the original ``fake_db``, so mutations the
    guard makes are visible both through the instance the guard used and
    through ``fake_db`` for later assertions. ``read_only`` is captured but
    ignored (the fake is a read/write in-memory store) — what matters for the
    test is which path the guard takes, not the underlying SQLite mode.
    """
    store = fake_db.rows

    def _open(profile, read_only=False):
        # Share the SAME mutable ``rows`` store object across every connection
        # (mirroring how the real SessionDB instances read/write one underlying
        # state.db file). A naive ``_FakeProfileDB([dict(r) for r in ...])``
        # would hand each connection deep-shallow COPIES of the row dicts, so a
        # ``set_session_title`` / ``create_session`` mutation on one connection
        # would never be visible through ``fake_db`` — silently breaking the
        # rotation assertions. Bypassing ``__init__`` and aliasing ``rows``
        # makes every mutation propagate to the shared store the test inspects.
        db = _FakeProfileDB.__new__(_FakeProfileDB)
        db.closed = False
        db.rows = store
        return db

    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db", _open)


def test_bloat_verdict_hard_signals():
    """The compression-failure fields (real signals) trigger a last-resort
    rotate — the ONLY rotation trigger (t_a8e9b7ff step 3)."""
    row_healthy = {"input_tokens": 1000, "compression_failure_error": None,
                   "compression_fallback_streak": 0,
                   "compression_ineffective_count": 0}
    assert routes_orchestrator._session_bloat_verdict(row_healthy) == (False, "")

    # Each failure signal alone is enough (even small input_tokens).
    for field, value in [
        ("compression_failure_error", "model context length exceeded"),
        ("compression_fallback_streak", 1),   # >= 1 fires
        ("compression_ineffective_count", 2),
    ]:
        row = dict(row_healthy, **{field: value})
        bloated, reason = routes_orchestrator._session_bloat_verdict(row)
        assert bloated is True
        assert "compression" in reason


def test_bloat_verdict_never_rotates_on_size_alone():
    """Size-driven rotation is GONE (t_a8e9b7ff step 2/3): a LARGE session is
    never rotated merely for being large — ``input_tokens`` is a CUMULATIVE
    counter (never reset by compaction), so cumulative size says nothing about
    current health, and with the 100K compaction cap in place a large-but-healthy
    session is normal and must not be rotated."""
    row = {"input_tokens": int(routes_orchestrator._ORCH_CONTEXT_WINDOW * 54),
           "compression_failure_error": None,
           "compression_fallback_streak": 0,
           "compression_ineffective_count": 0}
    assert routes_orchestrator._session_bloat_verdict(row) == (False, "")

    # Even absurd cumulative tokens do NOT rotate absent failure evidence.
    row_absurd = dict(row, input_tokens=50_000_000)
    assert routes_orchestrator._session_bloat_verdict(row_absurd) == (False, "")


def test_busy_notice_dropped_on_terminal_error_job():
    """Never 'poll' on a terminal ERROR job either — the notice is dropped."""
    job = _ChatJob("chat-1", "hscc", "hscc-orch", "hscc", "hi",
                   notice="hscc-orch is busy right now ... poll this job")
    routes_orchestrator._finish_job_error(job, "timeout", "did not reply")
    d = routes_orchestrator._job_dict(job)
    assert d["status"] == "timeout"
    assert "notice" not in d          # no stale "poll this job" on a terminal
    assert d["error"]["code"] == "orchestrator_timeout"


def test_guard_session_bloat_rotates_on_failure_evidence(tmp_path, monkeypatch):
    """Full guard: when compaction has POSITIVELY failed, the session is retired
    (non-destructive) and a fresh `<project>` session replaces it, so
    `--continue <project>` resolves clean. Rotation here is the LAST RESORT —
    it fires on failure evidence, never on size alone."""
    fake, old_id = _seed_session_db(
        tmp_path, "hscc",
        messages=280,
        input_tokens=int(routes_orchestrator._ORCH_CONTEXT_WINDOW * 40),  # ~10.5M
        comp_streak=1,
    )
    _install_fake_profile_db(monkeypatch, fake)
    ctx = _ctx(str(tmp_path), {})
    rotation = routes_orchestrator._guard_session_bloat(ctx, "hscc-orch", "hscc")
    assert rotation is not None
    assert rotation["retired_session"] == old_id
    assert rotation["title"] == "hscc"
    assert rotation["reason"]
    # Non-destructive: the old session still exists, retitled.
    old = fake.get_session(old_id)
    assert old is not None
    assert old["title"].startswith("hscc-retired-")
    # Fresh session now owns the `<project>` title and is resolvable.
    new_id = fake.resolve_session_by_title("hscc")
    assert new_id != old_id
    assert fake.get_session(new_id)["message_count"] == 0


def test_guard_session_bloat_noop_when_healthy(tmp_path, monkeypatch):
    """A healthy session is NOT rotated (no false positives)."""
    fake, old_id = _seed_session_db(tmp_path, "hscc", messages=4,
                                    input_tokens=51566)
    _install_fake_profile_db(monkeypatch, fake)
    ctx = _ctx(str(tmp_path), {})
    rotation = routes_orchestrator._guard_session_bloat(ctx, "hscc-orch", "hscc")
    assert rotation is None
    # Original session untouched, still titled `<project>`.
    assert fake.resolve_session_by_title("hscc") == old_id
    assert fake.get_session(old_id)["title"] == "hscc"


def test_guard_session_bloat_noop_on_large_healthy_session(tmp_path, monkeypatch):
    """A LARGE session with NO failure evidence is NOT rotated (t_a8e9b7ff
    step 2/3). With the 100K compaction cap in place, a big-but-healthy session
    is normal — this is the exact case the old 30x cumulative trigger would
    have wrongly rotated."""
    fake, old_id = _seed_session_db(
        tmp_path, "hscc", messages=278,
        input_tokens=int(routes_orchestrator._ORCH_CONTEXT_WINDOW * 54),
    )
    _install_fake_profile_db(monkeypatch, fake)
    ctx = _ctx(str(tmp_path), {})
    rotation = routes_orchestrator._guard_session_bloat(ctx, "hscc-orch", "hscc")
    assert rotation is None
    assert fake.resolve_session_by_title("hscc") == old_id
    assert fake.get_session(old_id)["title"] == "hscc"


def test_guard_session_bloat_hard_failure_signal(tmp_path, monkeypatch):
    """A session whose compression is failing rotates even at modest tokens."""
    fake, old_id = _seed_session_db(
        tmp_path, "hscc", messages=120, input_tokens=4_000_000,
        comp_streak=2)
    _install_fake_profile_db(monkeypatch, fake)
    ctx = _ctx(str(tmp_path), {})
    rotation = routes_orchestrator._guard_session_bloat(ctx, "hscc-orch", "hscc")
    assert rotation is not None
    assert rotation["retired_session"] == old_id
    assert "compression" in rotation["reason"]


def test_guard_session_bloat_failsafe_disabled(tmp_path, monkeypatch):
    """session_guard.enabled=False -> the guard never runs (no rotation) EVEN
    with positive failure evidence present — the fail-safe gate is absolute."""
    fake, old_id = _seed_session_db(
        tmp_path, "hscc", messages=999,
        input_tokens=int(routes_orchestrator._ORCH_CONTEXT_WINDOW * 500),
        comp_streak=5,
    )
    _install_fake_profile_db(monkeypatch, fake)
    ctx = _ctx(str(tmp_path), {"session_guard": {"enabled": False}})
    assert routes_orchestrator._guard_session_bloat(ctx, "hscc-orch", "hscc") is None
    assert fake.resolve_session_by_title("hscc") == old_id


def test_guard_session_bloat_failsafe_unreadable(monkeypatch):
    """Can't open the profile state.db -> fail-safe no-rotation, never a crash."""
    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db",
                        lambda profile, read_only=False: None)
    ctx = _ctx("/nonexistent", {})
    assert routes_orchestrator._guard_session_bloat(ctx, "hscc-orch", "hscc") is None


def test_session_health_readonly_reports_signals(tmp_path, monkeypatch):
    """_session_health surfaces the rotation signals + cap WITHOUT rotating."""
    fake, old_id = _seed_session_db(
        tmp_path, "hscc", messages=278,
        input_tokens=int(routes_orchestrator._ORCH_CONTEXT_WINDOW * 54),
        comp_fail="model context length exceeded",
    )
    _install_fake_profile_db(monkeypatch, fake)
    ctx = _ctx(str(tmp_path), {})
    health = routes_orchestrator._session_health(ctx, "hscc-orch", "hscc")
    assert health is not None
    assert health["bloated"] is True
    assert health["compaction_at_risk"] is True     # the compaction alert
    assert health["messages"] == 278
    assert health["input_tokens"] > 0
    assert health["context_window"] == routes_orchestrator._ORCH_CONTEXT_WINDOW
    assert health["threshold_tokens"] == \
        routes_orchestrator.SESSION_COMPACTION_THRESHOLD_TOKENS == 100000
    # Read-only: the session is UNAFFECTED (still titled `<project>`, no retire).
    assert fake.resolve_session_by_title("hscc") == old_id
    assert fake.get_session(old_id)["title"] == "hscc"


def test_session_health_not_at_risk_for_large_healthy(tmp_path, monkeypatch):
    """A large-but-healthy session does NOT trip the compaction alert — raw
    cumulative size is not failure evidence (t_a8e9b7ff)."""
    fake, old_id = _seed_session_db(
        tmp_path, "hscc", messages=278,
        input_tokens=int(routes_orchestrator._ORCH_CONTEXT_WINDOW * 54),
    )
    _install_fake_profile_db(monkeypatch, fake)
    ctx = _ctx(str(tmp_path), {})
    health = routes_orchestrator._session_health(ctx, "hscc-orch", "hscc")
    assert health is not None
    assert health["bloated"] is False
    assert health["compaction_at_risk"] is False
    assert health["threshold_tokens"] == 100000


def test_session_guard_config_precedence_and_validation(tmp_path):
    """session_guard config: defaults, file, inline, and hard-error on garbage."""
    # Defaults when nothing configured.
    enabled, ctx_win = routes_orchestrator._session_guard_config(
        _ctx(str(tmp_path), {}))
    assert enabled is True
    assert ctx_win == routes_orchestrator._ORCH_CONTEXT_WINDOW == 262144

    # Inline config dict wins.
    cfg = {"session_guard": {"enabled": False, "context_window": 131072}}
    enabled, ctx_win = routes_orchestrator._session_guard_config(
        _ctx(str(tmp_path), cfg))
    assert enabled is False and ctx_win == 131072

    # Malformed -> hard error.
    with pytest.raises(RuntimeError):
        routes_orchestrator._session_guard_config(
            _ctx(str(tmp_path), {"session_guard": {"context_window": "huge"}}))


# ---- _ensure_compaction_threshold (t_a8e9b7ff step 2) -------------------- #
# The ensure mechanism lazily imports ``hermes_cli.profiles`` and
# ``utils.atomic_yaml_write`` (the same real Hermes modules the CLI's
# ``config set`` path uses). The hermetic test path (p313) has neither, so we
# inject FAKE modules into ``sys.modules`` — matching exactly how the rest of
# this suite fakes the SessionDB — to exercise the ensure logic against a real
# temp ``config.yaml`` on disk. ``atomic_yaml_write`` doubles as the real writer
# so we verify the file actually lands and parses back.

class _FakeProfilesModule:
    """Stand-in for ``hermes_cli.profiles`` (profile dir resolution).

    When ``roster`` (a list of profile names, t_c03fd5ae) is empty/None it
    keeps the original single-root behaviour (``get_profile_dir`` -> ``root``,
    ``profile_exists`` -> True, ``list_profiles`` -> []) so the existing
    single-profile ensure + guard tests are unchanged. When ``roster`` is
    given, it resolves each profile to its OWN subdir under ``root`` and
    ``list_profiles`` enumerates the roster — exercising the role-profile sweep
    against isolated fake profile dirs, never the live ``~/.hermes/profiles``.
    """
    def __init__(self, root, roster=None):
        self._root = root
        self._roster = list(roster or [])
    def normalize_profile_name(self, name):
        return name
    def validate_profile_name(self, name):
        return None
    def profile_exists(self, name):
        if self._roster:
            return name in self._roster
        return True
    def get_profile_dir(self, name):
        if self._roster:
            return self._root / name
        return self._root
    def list_profiles(self):
        if not self._roster:
            return []
        return [types.SimpleNamespace(name=n, path=self._root / n)
                for n in self._roster]


def _install_fake_hermes_modules(monkeypatch, tmp_path, wrote=None, roster=None):
    """Inject fake ``hermes_cli.profiles`` + ``utils`` into ``sys.modules`` so
    ``_ensure_compaction_threshold`` / ``_ensure_role_profiles`` run against
    ``tmp_path``'s config files.

    ``wrote`` (optional list) collects every path ``atomic_yaml_write`` wrote to
    (monkeypatched from the real writer, so tests can assert the write actually
    landed and parse the file back through ``yaml.safe_load`` — i.e. verify via
    the same ``config.yaml`` path ``agent_init`` reads, not just the return
    dict). ``roster`` (optional, t_c03fd5ae) seeds the fake profile set for the
    role-profile sweep. ``monkeypatch.setitem`` tracks the sys.modules entries
    so they are restored after each test — the global module registry is never
    left polluted for later tests.
    """
    import sys as _sys
    fake_profiles = _FakeProfilesModule(tmp_path, roster)
    fake_utils = types.ModuleType("utils")

    def _atomic_write(path, data, **kw):
        import os as _os
        import yaml as _yaml
        if wrote is not None:
            wrote.append(str(path))
        _os.makedirs(_os.path.dirname(str(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _yaml.dump(data, f, sort_keys=False, default_flow_style=False)
    fake_utils.atomic_yaml_write = _atomic_write

    # Build a fake ``hermes_cli`` package with a ``profiles`` submodule.
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.profiles = fake_profiles
    fake_hermes_cli.__path__ = []

    monkeypatch.setitem(_sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(_sys.modules, "hermes_cli.profiles", fake_profiles)
    monkeypatch.setitem(_sys.modules, "utils", fake_utils)
    return fake_profiles


def test_ensure_compaction_threshold_writes_when_missing(tmp_path, monkeypatch):
    """Ensure sets compression.threshold_tokens on a profile with NO config
    yet, and the value LANDS in config.yaml (agent_init's read path)."""
    wrote = []
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote)
    res = routes_orchestrator._ensure_compaction_threshold("hscc-orch")
    assert res is not None
    assert res["set"] is True
    assert res["threshold_tokens"] == 100000
    assert res["previous"] is None
    # The write actually landed on disk (agent_init reads this exact file).
    cfg_path = tmp_path / "config.yaml"
    assert str(cfg_path) in wrote
    import yaml as _yaml
    parsed = _yaml.safe_load(cfg_path.read_text())
    assert parsed["compression"]["threshold_tokens"] == 100000


def test_ensure_compaction_threshold_idempotent_noop(tmp_path, monkeypatch):
    """Ensure is a NO-OP when the profile already has threshold_tokens == the
    constant — it never clobbers an operator value already <= 100000."""
    import yaml as _yaml
    (tmp_path / "config.yaml").write_text(
        "compression:\n  threshold_tokens: 100000\n")
    wrote = []
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote)
    res = routes_orchestrator._ensure_compaction_threshold("hscc-orch")
    assert res is None                     # no-op
    assert wrote == []                     # nothing written
    # File unchanged (operator value preserved).
    parsed = _yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert parsed["compression"]["threshold_tokens"] == 100000


def test_ensure_compaction_threshold_preserves_lower_operator_value(
        tmp_path, monkeypatch):
    """An operator value BELOW the constant (e.g. 60000) is PRESERVED — a lower
    cap is strictly better and must never be clobbered."""
    import yaml as _yaml
    (tmp_path / "config.yaml").write_text(
        "compression:\n  threshold_tokens: 60000\n")
    wrote = []
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote)
    res = routes_orchestrator._ensure_compaction_threshold("hscc-orch")
    assert res is None
    assert wrote == []
    parsed = _yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert parsed["compression"]["threshold_tokens"] == 60000


def test_ensure_compaction_threshold_lowers_over_cap_value(tmp_path, monkeypatch):
    """An operator value ABOVE the constant (e.g. 250000, past the wedge floor)
    is LOWERED to the constant — compaction must fire early enough to leave
    headroom, not at/over the 196608 ratio floor."""
    import yaml as _yaml
    (tmp_path / "config.yaml").write_text(
        "compression:\n  threshold_tokens: 250000\n")
    wrote = []
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote)
    res = routes_orchestrator._ensure_compaction_threshold("hscc-orch")
    assert res is not None
    assert res["set"] is True
    assert res["previous"] == 250000
    parsed = _yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert parsed["compression"]["threshold_tokens"] == 100000


def test_ensure_compaction_threshold_preserves_other_config(tmp_path, monkeypatch):
    """Write sets/replaces ONLY compression.threshold_tokens; all other config
    keys (e.g. model, memory directives) are preserved untouched."""
    import yaml as _yaml
    (tmp_path / "config.yaml").write_text(
        "model: orchestrator-model\nchat_timeout: 600\n")
    wrote = []
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote)
    res = routes_orchestrator._ensure_compaction_threshold("hscc-orch")
    assert res is not None and res["set"] is True
    parsed = _yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert parsed["compression"]["threshold_tokens"] == 100000
    assert parsed["model"] == "orchestrator-model"   # untouched
    assert parsed["chat_timeout"] == 600             # untouched


# ---- _ensure_role_profiles (t_c03fd5ae) ---------------------------------- #
# The role-profile sweep discovers every non-orchestrator profile from the
# profile root (never hardcoded) and runs each through the existing
# _ensure_compaction_threshold — so new profiles get the threshold on the next
# run, idempotence is inherited, lower operator values are preserved, and orch
# profiles are skipped. All against a fake roster rooted at tmp_path — never
# the live ~/.hermes/profiles.

def _seed_cfg(root, name, text):
    """Create ``root/<name>/config.yaml`` (with parents) so a role profile has
    a home dir before the sweep runs against it."""
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "config.yaml").write_text(text, encoding="utf-8")


def test_ensure_role_profiles_covers_new_profile(tmp_path, monkeypatch):
    """THE point: a NEW profile appearing under the profile root gets the
    threshold on the next ensure run. Roster mixes already-ensured roles, an
    under-cap operator value, and a brand-new profile with no config."""
    import yaml as _yaml
    # coder: already at 100000 (idempotence target)
    _seed_cfg(tmp_path, "coder", "compression:\n  threshold_tokens: 100000\n")
    # qa: operator set BELOW the constant — must be preserved
    _seed_cfg(tmp_path, "qa", "compression:\n  threshold_tokens: 60000\n")
    # worker: role profile already configured with OTHER keys, no compression
    _seed_cfg(tmp_path, "worker", "model: worker-role\nchat_timeout: 300\n")
    # NEW role profile — no config.yaml at all (regressed to the wedge state)
    roster = ["coder", "qa", "worker", "new-role", "hscc-orch"]
    wrote = []
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote, roster=roster)

    res = routes_orchestrator._ensure_role_profiles()
    assert res is not None
    # new-role + worker (no cap yet) got the threshold;
    # coder/qa unchanged; hscc-orch skipped
    assert res["set"] == ["new-role", "worker"]
    assert res["unchanged"] == ["coder", "qa"]
    assert res["orchestrators"] == ["hscc-orch"]
    assert res["role_profiles"] == 4

    # The new role profile's config.yaml landed on disk (agent_init's read path).
    assert str(tmp_path / "new-role" / "config.yaml") in wrote
    assert _yaml.safe_load(
        (tmp_path / "new-role" / "config.yaml").read_text()
    )["compression"]["threshold_tokens"] == 100000
    # worker's OTHER keys survived its ensure (only compression touched) ...
    worker_parsed = _yaml.safe_load((tmp_path / "worker" / "config.yaml").read_text())
    assert worker_parsed["compression"]["threshold_tokens"] == 100000
    assert worker_parsed["model"] == "worker-role"        # untouched
    assert worker_parsed["chat_timeout"] == 300           # untouched
    # qa's lower operator value preserved, coder already-ensured value intact.
    assert _yaml.safe_load((tmp_path / "qa" / "config.yaml").read_text()
                           )["compression"]["threshold_tokens"] == 60000
    assert _yaml.safe_load((tmp_path / "coder" / "config.yaml").read_text()
                           )["compression"]["threshold_tokens"] == 100000
    # hscc-orch was SKIPPED by the sweep — its (missing) config untouched.
    assert not (tmp_path / "hscc-orch" / "config.yaml").exists()


def test_ensure_role_profiles_idempotent_noop(tmp_path, monkeypatch):
    """When every role profile is already at 100000 the sweep is a NO-OP: it
    writes nothing and reports everything unchanged."""
    for name in ("coder", "reviewer", "worker"):
        _seed_cfg(tmp_path, name, "compression:\n  threshold_tokens: 100000\n")
    roster = ["coder", "reviewer", "worker", "hscc-orch"]
    wrote = []
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote, roster=roster)

    res = routes_orchestrator._ensure_role_profiles()
    assert res is not None
    assert res["set"] == []
    assert res["unchanged"] == ["coder", "reviewer", "worker"]
    assert res["orchestrators"] == ["hscc-orch"]
    assert wrote == []                    # nothing written anywhere


def test_ensure_role_profiles_preserves_lower_operator_value(tmp_path, monkeypatch):
    """An operator value BELOW the constant (e.g. 60000) on a role profile is
    PRESERVED, not raised — never clobber a strictly-better lower cap."""
    import yaml as _yaml
    _seed_cfg(tmp_path, "backend-engineer",
              "compression:\n  threshold_tokens: 60000\n")
    roster = ["backend-engineer"]
    wrote = []
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote, roster=roster)

    res = routes_orchestrator._ensure_role_profiles()
    assert res["set"] == []
    assert res["unchanged"] == ["backend-engineer"]
    assert wrote == []
    assert _yaml.safe_load(
        (tmp_path / "backend-engineer" / "config.yaml").read_text()
    )["compression"]["threshold_tokens"] == 60000


def test_ensure_role_profiles_skips_orchestrators(tmp_path, monkeypatch):
    """Orch profiles are NOT touched by the role sweep — their behaviour stays
    byte-identical; they are already covered by the per-project ensure path.
    A role profile alongside IS still ensured."""
    import yaml as _yaml
    _seed_cfg(tmp_path, "hscc-orch", "model: orchestrator-model\n")
    roster = ["hscc-orch", "general-orch", "worker"]
    wrote = []
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote, roster=roster)

    res = routes_orchestrator._ensure_role_profiles()
    assert res["orchestrators"] == ["general-orch", "hscc-orch"]
    assert res["set"] == ["worker"]
    # hscc-orch was NOT ensured by the sweep — its config has no threshold cap.
    assert "compression" not in _yaml.safe_load(
        (tmp_path / "hscc-orch" / "config.yaml").read_text())
    assert (tmp_path / "general-orch" / "config.yaml").exists() is False


def test_guard_session_bloat_ensures_compaction_threshold(tmp_path, monkeypatch):
    """_guard_session_bloat's PRIMARY action is to ENSURE the compaction cap on
    the profile's config.yaml (the real fix), independent of the session DB. We
    capture it by requesting the threshold on the resolved profile dir."""
    import sys as _sys
    wrote = []
    fake_profiles = _install_fake_hermes_modules(monkeypatch, tmp_path, wrote)
    fake, old_id = _seed_session_db(tmp_path, "hscc", messages=4,
                                    input_tokens=51566)
    _install_fake_profile_db(monkeypatch, fake)
    ctx = _ctx(str(tmp_path), {})
    res = routes_orchestrator._guard_session_bloat(ctx, "hscc-orch", "hscc")
    assert res is None                      # healthy session -> no rotation
    # But the ensure layer fired: the cap landed on the profile's config.yaml.
    cfg_path = tmp_path / "config.yaml"
    assert str(cfg_path) in wrote
    import yaml as _yaml
    parsed = _yaml.safe_load(cfg_path.read_text())
    assert parsed["compression"]["threshold_tokens"] == 100000


def test_guard_session_bloat_also_sweeps_role_profiles(tmp_path, monkeypatch):
    """Wiring (t_c03fd5ae): _guard_session_bloat sweeps the ROLE profiles too,
    not just the resolved orch profile — so the role-profile extension actually
    runs on the guard's trigger, not only when called directly."""
    import yaml as _yaml
    wrote = []
    # hscc-orch is the chat target; coder is a role profile with no cap yet.
    # general-orch is another orchestrator that the sweep must skip.
    roster = ["hscc-orch", "general-orch", "coder"]
    _install_fake_hermes_modules(monkeypatch, tmp_path, wrote, roster=roster)
    fake, old_id = _seed_session_db(tmp_path, "hscc", messages=4,
                                    input_tokens=51566)
    _install_fake_profile_db(monkeypatch, fake)
    ctx = _ctx(str(tmp_path), {})

    res = routes_orchestrator._guard_session_bloat(ctx, "hscc-orch", "hscc")
    assert res is None                      # healthy session -> no rotation

    # The orch profile's config.yaml (chat target) got the ensure ...
    orch_cfg = tmp_path / "hscc-orch" / "config.yaml"
    assert str(orch_cfg) in wrote
    # ... AND the role profile coder got it (the extension actually fired on
    # the guard path).
    role_cfg = tmp_path / "coder" / "config.yaml"
    assert str(role_cfg) in wrote
    assert _yaml.safe_load(
        role_cfg.read_text())["compression"]["threshold_tokens"] == 100000
    # general-orch was NOT touched by the role sweep (orch behaviour intact).
    assert not (tmp_path / "general-orch" / "config.yaml").exists()


def test_chat_post_surfaces_session_rotation(running, token, fakes, monkeypatch):
    """When the guard rotates, the 202 POST carries `session_rotation` so the
    operator sees the session was retired + recreated (chat continues on the
    fresh one, same name)."""
    monkeypatch.setattr(
        routes_orchestrator, "_guard_session_bloat",
        lambda ctx, profile, session: {
            "profile": "hscc-orch", "title": "hscc",
            "retired_session": "seed-hscc",
            "retired_title": "hscc-retired-20260827-070000",
            "session": "20260827_rot_abcd12",
            "reason": "context compression is failing",
        })
    status, payload = _post(running, token, body={
        "project": "hscc", "prompt": "go build X", "confirm": True,
    })
    assert status == 202
    rot = payload["session_rotation"]
    assert rot["retired_session"] == "seed-hscc"
    assert rot["session"] == "20260827_rot_abcd12"
    assert rot["reason"]
    # The background job still continues the (same-named) `<project>` session.
    _poll_done(running, token, payload["job_id"])
    assert fakes["invoke_calls"][0][1] == "hscc"   # session name unchanged

