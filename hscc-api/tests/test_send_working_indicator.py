"""t_f109e7ea — app→session: working indicator + honest CLI-held-session notice.

Guards the two HSCC-side fixes for the operator's report ("no indicator that
the orchestrator is working; app message neither drives a reply nor appears
anywhere"):

1. A WS ``send`` emits an immediate ``system kind="working"`` indicator before
   the (possibly slow) reply, so the app always shows the turn start — no more
   silent gap between the operator's line and the assistant reply.

2. When the project's named session is owned by the operator's interactive CLI
   REPL, HSCC does NOT pretend "working" and silently drive nothing (the
   confirmed hermes behaviour: any second driver is refused 4090/SESSION_NOT_OWNED
   before admission, so no turn, no reply, and the message never reaches
   state.db). Instead it appends a ``system kind="session_busy"`` notice — the
   operator learns the message WAS received but the machine session is in use —
   and it does NOT relay into a session whose turn another surface drives.

Hermetic: the relay is stubbed to a real-reply fake and ``detect_active_cli_owner``
is controlled directly, so no test touches the operator's live registry, state.db,
or hermes serve (the same seam test_ws_relay_not_noop.py establishes).
"""

import json
import time
import types

import pytest

import routes_orchestrator as ro
import routes_ws
import session_event
from session_event import TYPE_SYSTEM, get_store, reset_stores


@pytest.fixture(autouse=True)
def clean_stores():
    reset_stores()
    yield
    reset_stores()


def _install_relay(monkeypatch):
    """Stub the orchestrator backing to a real-reply fake; return the seen calls.

    Keeps the REST relay path real (_new_job/_run_job) but fakes the process
    boundary, exactly like test_ws_relay_not_noop.py. Returns a list that each
    relay invocation appends (profile, session, text) to.
    """
    import sys
    monkeypatch.setitem(sys.modules, "routes_orchestrator", ro)

    def _fake_backing_resolve(project, path):
        return {"profile": "orch", "session": "hscc"}

    def _fake_backing_invoke(profile, session, text, timeout=None,
                             image_data=None, image_mime=None,
                             cancel_evt=None, on_spawn=None):
        if on_spawn is not None:
            on_spawn(_NoopProc())
        return (f"reply to {text}", profile, session)

    monkeypatch.setattr(ro, "_registry_path", lambda ctx: "/dev/null")
    monkeypatch.setattr(ro, "_backing_resolve", _fake_backing_resolve)
    monkeypatch.setattr(ro, "_backing_invoke", _fake_backing_invoke)
    monkeypatch.setattr(
        routes_ws, "_registry",
        types.SimpleNamespace(ensure_session=lambda name, path=None: name))
    monkeypatch.setattr(routes_ws, "_registry_path", lambda ctx: "/dev/null")
    return []


class _NoopProc:
    def poll(self):
        return None

    def wait(self, timeout=None):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass


def _events(project="hscc"):
    return get_store(project).history()["events"]


def _system_kinds(events):
    return [e["payload"]["kind"] for e in events if e["type"] == TYPE_SYSTEM]


def _wait_for(project, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        evs = _events(project)
        if predicate(evs):
            return evs
        time.sleep(0.02)
    return _events(project)


# --------------------------------------------------------------------------- #
# Working indicator
# --------------------------------------------------------------------------- #

def test_send_emits_working_indicator_then_reply(monkeypatch):
    """A send with no CLI owner appends user echo + a working indicator, then
    the relay produces an assistant reply — the app sees the turn start AND the
    outcome."""
    _install_relay(monkeypatch)
    monkeypatch.setattr(
        ro, "detect_active_cli_owner", lambda project, registry_path=None: None)

    sock = types.SimpleNamespace(sendall=lambda b: None)
    monkeypatch.setattr(routes_ws, "_send_text", lambda s, t: None, raising=True)

    routes_ws._handle_client_send(sock, "hscc", {"text": "status please"})

    # The relay runs on a background thread; wait for the reply to land.
    evs = _wait_for("hscc", lambda evs: any(
        e["type"] == "message" and e["payload"].get("role") == "assistant"
        for e in evs))

    # The working indicator is emitted synchronously in the send path, so it is
    # present immediately (not gated on the slow reply).
    kinds = _system_kinds(evs)
    assert "working" in kinds, "no working indicator emitted: %r" % (kinds,)

    # Order: user echo -> working indicator -> assistant reply.
    order = [e["type"] for e in evs]
    assert order.index("message") < order.index("system"), order
    user_idx = [i for i, e in enumerate(evs)
                if e["type"] == "message" and e["payload"]["role"] == "user"]
    work_idx = [i for i, e in enumerate(evs)
                if e["type"] == TYPE_SYSTEM and e["payload"]["kind"] == "working"]
    assert user_idx and work_idx, order
    assert user_idx[0] < work_idx[0], "working indicator before the user echo is wrong"


def test_send_does_not_fabricate_working_when_cli_holds_session(monkeypatch):
    """When the session is CLI-owned, the send shows the honest session_busy
    notice and does NOT emit a working indicator nor relay into the session."""
    seen = []
    _install_relay(monkeypatch)
    monkeypatch.setattr(
        ro, "detect_active_cli_owner",
        lambda project, registry_path=None: {"surface": "cli", "session_id": "s1"})
    # Record whether the relay hook is invoked.
    monkeypatch.setattr(routes_ws, "relay_user_message",
                        lambda project, text: seen.append((project, text)))

    sock = types.SimpleNamespace(sendall=lambda b: None)
    monkeypatch.setattr(routes_ws, "_send_text", lambda s, t: None, raising=True)

    routes_ws._handle_client_send(sock, "hscc", {"text": "hello from app"})

    evs = _events("hscc")
    kinds = _system_kinds(evs)
    # The message was received (user echo) and the operator is told plainly.
    assert any(e["payload"]["role"] == "user" for e in evs)
    assert "session_busy" in kinds, "no session_busy notice: %r" % (kinds,)
    # We must NOT pretend "working" (nothing is working) nor drive a second
    # surface into a session whose turn the CLI owns.
    assert "working" not in kinds, kinds
    assert seen == [], "relayed into a CLI-held session: %r" % (seen,)


def test_send_still_relays_when_detector_is_fail_safe(monkeypatch):
    """If the CLI-owner detection fails (returns None — "not provably owned"),
    the send still relays: a detection failure must never drop the operator's
    message."""
    seen = []
    _install_relay(monkeypatch)
    monkeypatch.setattr(ro, "detect_active_cli_owner",
                        lambda project, registry_path=None: None)

    sock = types.SimpleNamespace(sendall=lambda b: None)
    monkeypatch.setattr(routes_ws, "_send_text", lambda s, t: None, raising=True)
    monkeypatch.setattr(routes_ws, "relay_user_message",
                        lambda project, text: seen.append((project, text)))

    routes_ws._handle_client_send(sock, "hscc", {"text": "keep going"})

    assert seen == [("hscc", "keep going")]


# --------------------------------------------------------------------------- #
# detect_active_cli_owner unit tests (surface matching)
# --------------------------------------------------------------------------- #

def test_detector_matches_only_live_cli_surface(monkeypatch):
    """Only an entry on the project's OWN session with surface==cli (lowercased)
    is reported as an owner; other surfaces / sessions are None."""
    def _fake_resolve(project, registry_path=None):
        return ("hscc-orch", "hscc", "sess-1")

    monkeypatch.setattr(ro, "resolve_named_session_id", _fake_resolve)
    monkeypatch.setattr(ro, "_active_session_cli_owner", lambda profile, sid: {
        "session_id": sid, "surface": "cli", "lease_id": "L1"})

    owner = ro.detect_active_cli_owner("hscc")
    assert owner is not None
    assert owner["lease_id"] == "L1"
    assert ro.detect_active_cli_owner("hscc")["surface"] == "cli"


def test_detector_fail_safe_returns_none(monkeypatch):
    """A resolution/read failure must never raise out of the send path."""
    def _boom(project, registry_path=None):
        raise RuntimeError("state.db busy")
    monkeypatch.setattr(ro, "resolve_named_session_id", _boom)
    assert ro.detect_active_cli_owner("hscc") is None


# --------------------------------------------------------------------------- #
# Streamed assistant reply (driver path) coexists with the working indicator
# --------------------------------------------------------------------------- #

def test_driver_streamed_reply_follows_working_indicator(monkeypatch):
    """The driver (serve) path: send -> working indicator -> streamed assistant
    deltas -> done. The app sees the turn start and the fluent reply."""
    _install_relay(monkeypatch)
    monkeypatch.setattr(
        ro, "detect_active_cli_owner", lambda project, registry_path=None: None)

    sock = types.SimpleNamespace(sendall=lambda b: None)
    monkeypatch.setattr(routes_ws, "_send_text", lambda s, t: None, raising=True)

    # Simulate the GatewayDriver's events-loop translating native deltas into
    # the store (the driver path's reply), NOT a single REST reply.
    driver_path = {"called": False}
    def _driver_relay(project, text):
        driver_path["called"] = True
        from gateway_driver import FrameTranslator
        tr = FrameTranslator(get_store(project))
        tr.on_frame({"jsonrpc": "2.0", "method": "event",
                     "params": {"type": "message.start",
                                "session_id": "s1", "payload": {}}})
        tr.on_frame({"jsonrpc": "2.0", "method": "event",
                     "params": {"type": "message.delta",
                                "session_id": "s1",
                                "payload": {"text": "Right"}}})
        tr.on_frame({"jsonrpc": "2.0", "method": "event",
                     "params": {"type": "message.delta",
                                "session_id": "s1",
                                "payload": {"text": " away."}}})
        tr.flush()
    monkeypatch.setattr(routes_ws, "relay_user_message", _driver_relay)

    routes_ws._handle_client_send(sock, "hscc", {"text": "what do you see?"})

    evs = _events("hscc")
    kinds = _system_kinds(evs)
    assert driver_path["called"] is True
    assert "working" in kinds, kinds
    assert any(e["type"] == "message" and e["payload"]["role"] == "assistant"
               and e["payload"]["delta"] == "Right" for e in evs)
    assert any(e["type"] == "message" and e["payload"]["role"] == "assistant"
               and e["payload"]["delta"] == " away." for e in evs)
    # The working indicator precedes the reply in the transcript.
    work_idx = next(i for i, e in enumerate(evs)
                    if e["type"] == TYPE_SYSTEM and e["payload"]["kind"] == "working")
    reply_idx = next(i for i, e in enumerate(evs)
                     if e["type"] == "message" and e["payload"]["role"] == "assistant")
    assert work_idx < reply_idx, evs
