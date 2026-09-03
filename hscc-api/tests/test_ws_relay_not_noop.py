"""Guard: the WS send path must never accept an operator message and drop it.

This is a regression test for a shipped outage. ``routes_ws._default_relay``
used to be ``return False`` — a no-op. The Chat tab in the app sends over this
socket, so every message the operator typed was appended to the store (which
echoed it back, making the UI look like it worked) and then discarded. The
orchestrator never saw it, the cluster sat idle, and no reply ever came.

Everything was green while that was true, because the existing tests asserted
the hook was *called*. Calling a no-op is indistinguishable from working if you
only assert the call. So these tests assert the OUTCOME instead: either the
orchestrator is actually reached, or the operator is told in the transcript.
A no-op relay fails both.
"""

import json
import time
import types

import pytest

import routes_ws
import session_event
from session_event import TYPE_ERROR, TYPE_MESSAGE, get_store, reset_stores


@pytest.fixture(autouse=True)
def clean_stores():
    reset_stores()
    yield
    reset_stores()


def _wait_for(project, predicate, timeout=5.0):
    """Poll the store until predicate(events) holds. The relay runs on a
    background thread, so the assertion cannot be made synchronously."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = get_store(project).history()["events"]
        if predicate(events):
            return events
        time.sleep(0.02)
    return get_store(project).history()["events"]


def _types(events):
    return [e["type"] for e in events]


def _install_backing(monkeypatch, invoke):
    """Redirect the relay's orchestrator backing to a fake, keeping job machinery real.

    Since t_68432c2d the relay (routes_ws._default_relay) runs its turn as a
    real :class:`routes_orchestrator._ChatJob` through THAT module's own job
    machinery (_new_job / _run_job / _job_dict) — so it can be cancelled by a
    ``stop`` frame. A bare fake module can no longer stand in for the whole
    ``routes_orchestrator`` namespace (it would lack those job functions and the
    relay would fail). We therefore keep the REAL module and patch only the
    process-bound pieces (_backing_resolve + _backing_invoke) plus the registry,
    preserving the tests' intent (assert the OUTCOME, not the call).

    ``invoke(profile, session, text)`` is the test's simple 3-arg fake; the
    relay calls it through a wrapper that supplies the job kwargs and forwards
    an ``on_spawn`` handle so the cancellable path has something to retain.
    """
    import sys
    import routes_orchestrator as ro
    # Make sure no stale fake module is shadowing the real one when the relay
    # thread does ``import routes_orchestrator``.
    monkeypatch.setitem(sys.modules, "routes_orchestrator", ro)

    def _fake_backing_resolve(project, path):
        # resolve_name currently reads the registry; keep tests hermetic. The
        # session id comes from routes_ws._registry.ensure_session (faked to the
        # project name) so REPORT "orch"/"hscc" consistently.
        return {"profile": "orch", "session": "hscc"}

    def _fake_backing_invoke(profile, session, text, timeout=None,
                             image_data=None, image_mime=None,
                             cancel_evt=None, on_spawn=None):
        # Give the cancellable job machinery a retained handle (the test's
        # simple fake never spawns a real subprocess, but a stop path must have
        # something to terminate/kill). A no-op proc is enough.
        if on_spawn is not None:
            on_spawn(_NoopProc())
        # A test that wants to exercise the STOP path needs the cancel event, so
        # pass it through when the user's invoke is written to accept it
        # (cancel-aware fakes that block until cancelled, like a real subprocess).
        try:
            import inspect
            _accepts_cancel = "cancel_evt" in inspect.signature(invoke).parameters
        except (TypeError, ValueError):
            _accepts_cancel = False
        if _accepts_cancel:
            return invoke(profile, session, text, cancel_evt=cancel_evt)
        return invoke(profile, session, text)

    monkeypatch.setattr(ro, "_registry_path", lambda ctx: "/dev/null")
    monkeypatch.setattr(ro, "_backing_resolve", _fake_backing_resolve)
    monkeypatch.setattr(ro, "_backing_invoke", _fake_backing_invoke)
    # The relay persists/resolves the session via routes_ws's registry
    # (ensure_session). Fake it so NO test writes to the real ~/.flightdeck
    # registry. Idempotent default = project name.
    monkeypatch.setattr(
        routes_ws, "_registry",
        types.SimpleNamespace(ensure_session=lambda name, path=None: name))
    monkeypatch.setattr(routes_ws, "_registry_path", lambda ctx: "/dev/null")
    return ro


class _NoopProc:
    """A stand-in retained subprocess handle for the cancellable job path.

    The stop machinery calls ``terminate()``/``kill()`` on the retained Popen;
    in these relay tests no real subprocess is ever spawned, so a no-op handle
    satisfies the contract without touching the OS.
    """

    def poll(self):
        return None

    def wait(self, timeout=None):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass


def test_relay_actually_reaches_the_orchestrator(monkeypatch):
    """The message must arrive at the backing invoke, with the operator's text."""
    seen = []

    def invoke(profile, session, text):
        seen.append((profile, session, text))
        return ("acknowledged", profile, session)

    _install_backing(monkeypatch, invoke)

    assert routes_ws._default_relay("hscc", "restart the orchestrator") is True

    events = _wait_for("hscc", lambda evs: any(e["type"] == TYPE_MESSAGE for e in evs))
    # The relay resolves the session through routes_ws's registry (ensure_session),
    # which the fake maps deterministically to the project name, NOT through the
    # _backing_resolve seam (whose "s1" is an unrelated placeholder). "hscc" is
    # the durable, registry-persisted id — exactly what survives a reinstall.
    assert seen == [("orch", "hscc", "restart the orchestrator")], (
        "relay did not reach the orchestrator: %r" % (seen,))
    replies = [e for e in events if e["type"] == TYPE_MESSAGE]
    assert replies, "no assistant reply appended to the store"
    assert replies[-1]["payload"]["role"] == "assistant"
    assert replies[-1]["payload"]["delta"] == "acknowledged"
    assert replies[-1]["payload"]["done"] is True


def test_relay_failure_is_surfaced_not_swallowed(monkeypatch):
    """If the orchestrator is unreachable the operator must SEE that.

    A silent drop is the exact bug this file exists to prevent, so an
    exception has to become a visible error event, not a log line.

    Since t_68432c2d the relay runs the turn as a real ``_ChatJob`` and a
    generic invocation failure lands the job in its documented ``error`` state
    (``_run_job``'s cleanup), which the relay then folds into the transcript as
    an ``orchestrator_error`` — the code changed from the old ``relay_failed``
    (which fired when the relay caught the exception itself). The raw
    exception text is not echoed (that detail is deliberately not leaked to the
    client); what matters is a visible, coherent error.
    """
    def invoke(profile, session, text):
        raise RuntimeError("connection refused")

    _install_backing(monkeypatch, invoke)

    assert routes_ws._default_relay("hscc", "hello") is True

    events = _wait_for("hscc", lambda evs: any(e["type"] == TYPE_ERROR for e in evs))
    errors = [e for e in events if e["type"] == TYPE_ERROR]
    assert errors, "relay failure was swallowed: %r" % (_types(events),)
    assert errors[-1]["payload"]["code"] == "orchestrator_error"
    assert json.dumps(errors[-1]["payload"]).find("connection refused") == -1
    # The operator is left with the job's coherent headline, never a raw trace.
    assert "orchestrator" in errors[-1]["payload"]["message"].lower()


def test_client_send_path_produces_a_reply_end_to_end(monkeypatch):
    """Drive the real inbound handler, not just the relay function.

    This is the shape of the actual outage: _handle_client_send appended the
    user's line and called a hook that did nothing, so the transcript ended
    with the user's own message and nothing ever followed it."""
    def invoke(profile, session, text):
        return ("on it", profile, session)

    _install_backing(monkeypatch, invoke)

    sent = []
    sock = types.SimpleNamespace(sendall=lambda b: sent.append(b))
    monkeypatch.setattr(routes_ws, "_send_text",
                        lambda s, t: sent.append(t), raising=True)

    routes_ws._handle_client_send(sock, "hscc", {"text": "status please"})

    events = _wait_for(
        "hscc",
        lambda evs: any(e["type"] == TYPE_MESSAGE
                        and e["payload"]["role"] == "assistant" for e in evs))
    roles = [e["payload"]["role"] for e in events if e["type"] == TYPE_MESSAGE]
    assert roles[:1] == ["user"], "operator's own line missing: %r" % (roles,)
    assert "assistant" in roles, (
        "send path accepted the message and produced no reply — the relay is "
        "a no-op again: %r" % (roles,))


def test_module_hook_defaults_to_the_real_relay():
    """GatewayDriver overrides this at runtime; the DEFAULT must still work.

    The outage happened with no driver attached, which is the common case for
    the app talking to a plain API server."""
    assert routes_ws.relay_user_message is routes_ws._default_relay


def test_stop_kind_while_turn_in_flight_cancels_and_acks(monkeypatch):
    """A WS `{"kind":"stop"}` client frame cancels an in-flight relay turn and
    lands a machine-visible "Turn stopped by operator." notice in the transcript
    — the stop button the operator presses mid-hermes-run."""
    import routes_orchestrator as ro
    import ws_frame

    cancel_evt_holder = {}

    def invoke(profile, session, text, cancel_evt=None):
        # Block like a real hermes subprocess until the stop sets this event,
        # then report the standard orchestration cancellation.
        cancel_evt_holder["evt"] = cancel_evt
        assert cancel_evt is not None
        cancel_evt.wait(timeout=5)
        raise ro._OrchestratorCancelled("cancelled by operator")

    _install_backing(monkeypatch, invoke)

    sock = types.SimpleNamespace(sendall=lambda b: None)
    acks = []
    monkeypatch.setattr(routes_ws, "_send_text", lambda s, t: acks.append(t),
                        raising=True)

    # Fire the turn (worker thread blocks inside invoke until cancelled).
    routes_ws._handle_client_send(sock, "hscc", {"text": "long running task"})

    # Wait until we KNOW a real in-flight job exists for "hscc".
    _wait_for("hscc", lambda evs: any(
        e["type"] == TYPE_MESSAGE and e["payload"]["role"] == "user"
        for e in evs))
    deadline = time.time() + 5
    while time.time() < deadline and "evt" not in cancel_evt_holder:
        time.sleep(0.005)
    assert "evt" in cancel_evt_holder, "the relay never accepted the turn"

    # Operator presses stop.
    routes_ws._process_inbound(sock, "hscc", ws_frame.OP_TEXT,
                               b'{"kind":"stop"}')

    # The in-flight process is cancelled AND the transcript is updated so the
    # operator sees the interruption instead of a hang.
    _wait_for("hscc", lambda evs: any(
        e["type"] == TYPE_MESSAGE and e["payload"]["role"] == "system"
        and "stopped" in (e["payload"].get("delta") or "").lower()
        for e in evs))
    acks = [a for a in acks if isinstance(a, str)]
    stop_ack = None
    for raw in acks:
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if obj.get("type") == "ack" and obj.get("payload", {}).get("kind") == "stop":
            stop_ack = obj
    assert stop_ack is not None, "no stop ack was sent: %r" % (acks,)
    assert stop_ack["payload"]["stopped"] is True, (
        "stop acked stopped=False — the in-flight process was not cancelled: "
        "%r" % (stop_ack,))


def test_stop_kind_with_nothing_in_flight_is_noop(monkeypatch):
    """Stopping when no relay turn is running acks cleanly (stopped: False) and
    does NOT append a bogus notice."""
    import ws_frame

    def invoke(profile, session, text):
        return ("ok", profile, session)

    _install_backing(monkeypatch, invoke)

    sock = types.SimpleNamespace(sendall=lambda b: None)
    acks = []
    monkeypatch.setattr(routes_ws, "_send_text", lambda s, t: acks.append(t),
                        raising=True)

    routes_ws._process_inbound(sock, "hscc", ws_frame.OP_TEXT,
                               b'{"kind":"stop"}')

    stop_ack = None
    for raw in acks:
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if obj.get("type") == "ack" and obj.get("payload", {}).get("kind") == "stop":
            stop_ack = obj
    assert stop_ack is not None, "no stop ack was sent"
    assert stop_ack["payload"]["stopped"] is False
    # No notice claiming we stopped something that wasn't running.
    events = get_store("hscc").history()["events"]
    assert not any(
        e["type"] == TYPE_MESSAGE and e["payload"].get("role") == "system"
        and "stopped" in (e["payload"].get("delta") or "").lower()
        for e in events), "spurious stop notice for idle session"
