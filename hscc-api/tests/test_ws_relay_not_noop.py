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
    """Stand in for routes_orchestrator's REST backing."""
    fake = types.ModuleType("routes_orchestrator")
    fake._registry_path = lambda path: "/dev/null"
    fake._backing_resolve = lambda project, path: {
        "profile": "orch", "session": "s1"}
    fake._backing_invoke = invoke
    monkeypatch.setitem(__import__("sys").modules, "routes_orchestrator", fake)
    # The relay now persists/resolves the session via routes_ws's registry
    # (ensure_session). Fake it so NO test writes to the real ~/.flightdeck
    # registry. Idempotent default = project name.
    monkeypatch.setattr(
        routes_ws, "_registry",
        types.SimpleNamespace(ensure_session=lambda name, path=None: name))
    monkeypatch.setattr(routes_ws, "_registry_path", lambda ctx: "/dev/null")
    return fake


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
    exception has to become a visible error event, not a log line."""
    def invoke(profile, session, text):
        raise RuntimeError("connection refused")

    _install_backing(monkeypatch, invoke)

    assert routes_ws._default_relay("hscc", "hello") is True

    events = _wait_for("hscc", lambda evs: any(e["type"] == TYPE_ERROR for e in evs))
    errors = [e for e in events if e["type"] == TYPE_ERROR]
    assert errors, "relay failure was swallowed: %r" % (_types(events),)
    assert errors[-1]["payload"]["code"] == "relay_failed"
    assert "connection refused" in errors[-1]["payload"]["message"]


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
