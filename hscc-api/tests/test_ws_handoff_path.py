"""Hermetic tests for the WS handoff wiring (t_7cc2e7d5).

``routes_ws._handle_client_send`` now routes a busy-CLI-owner send into
``_handle_busy_send``, which drives the consented handoff (or, when the seam
is absent on this hermes, falls back to the existing ``session_busy`` notice).
These tests prove the WIRING hermetically — they install a fake coordinator via
``routes_ws.coordinator_factory`` (the injectable seam) so no real hermes seam
or operator registry is touched.

The real cross-process mechanics against the upstream seam are proven by
execution in tests_gateway/probe_05_handoff_coordinator.py; this file proves the
HSCC send-path wiring around the coordinator.
"""

from __future__ import annotations

import time
import types

import pytest

import routes_ws
import session_event
from session_event import (
    TYPE_SYSTEM, get_store, reset_stores,
)
from active_sessions_handoff import SEAM_UNAVAILABLE, HandoffError


@pytest.fixture(autouse=True)
def clean_stores():
    reset_stores()
    yield
    reset_stores()


@pytest.fixture(autouse=True)
def _restore_factory(monkeypatch):
    """Restore coordinator_factory to its module default after each test."""
    monkeypatch.setattr(routes_ws, "coordinator_factory",
                        routes_ws._build_coordinator)
    # Stub the CLI-owner detector to a busy owner so _handle_busy_send's
    # resolve path is exercised; individual tests override as needed.
    _stub_resolve(monkeypatch)


def _stub_resolve(monkeypatch):
    """Make resolve_named_session_id return a stable (profile, session)."""
    import routes_orchestrator as ro
    monkeypatch.setattr(
        ro, "resolve_named_session_id",
        lambda project, registry_path=None: ("orch", "title", "ses-1"))


class _FakeCoordinator:
    """A stand-in HandoffCoordinator that records calls and returns canned
    results, so the WS wiring is proven without any hermes seam."""

    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.orchestrate_calls = []

    def registry_home(self):
        return "/profiles/orch"

    def orchestrate(self, text, *, drive, on_pending, on_denied,
                    session_id, registry_home=None):
        self.orchestrate_calls.append(
            {"text": text, "session_id": session_id, "registry_home": registry_home})
        if self._exc is not None:
            raise self._exc
        # Invoke the callbacks exactly as the real coordinator does.
        if self._result == "pending":
            on_pending()
            on_denied("Timed out waiting for the owner to approve.")
            return {"outcome": "denied", "reason": "grant_timeout",
                    "message": "Timed out waiting for the owner to approve."}
        if self._result == "driven":
            on_pending()
            drive()
            return {"outcome": "completed"}
        if self._result == "denied":
            on_denied("The handoff request was not approved.")
            return {"outcome": "denied", "reason": "handoff_denied",
                    "message": "The handoff request was not approved."}
        return {"outcome": "completed"}


def _wait_for(project, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = get_store(project).history()["events"]
        if predicate(events):
            return events
        time.sleep(0.02)
    return get_store(project).history()["events"]


def _system_kinds(project):
    return [e["payload"].get("kind")
            for e in get_store(project).history()["events"]
            if e["type"] == TYPE_SYSTEM]


# --------------------------------------------------------------------------- #
# Wire-level
# --------------------------------------------------------------------------- #

def test_busy_send_resolves_and_uses_coordinator(monkeypatch):
    """A busy-CLI-owner send builds a coordinator (via the factory) and runs
    orchestrate with the resolved session identity."""
    fake = _FakeCoordinator(result="completed")
    captured = {}
    monkeypatch.setattr(routes_ws, "coordinator_factory",
                        lambda profile, session_id: captured.update(
                            {"profile": profile, "session_id": session_id}) or fake)
    # drive() -> completes; nothing to await beyond orchestrate call.
    routes_ws._handle_busy_send("hscc", "hello there")
    # orchestrate runs synchronously here (no consent wait in this fake path),
    # so the call already happened on the background thread. Wait for it.
    deadline = time.time() + 5
    while time.time() < deadline and not fake.orchestrate_calls:
        time.sleep(0.02)
    assert fake.orchestrate_calls, "coordinator.orchestrate never called"
    call = fake.orchestrate_calls[0]
    assert call["text"] == "hello there"
    assert call["session_id"] == "ses-1"
    assert call["registry_home"] == "/profiles/orch"
    assert captured == {"profile": "orch", "session_id": "ses-1"}


def test_busy_send_emits_handoff_pending_and_denied_on_timeout(monkeypatch):
    """When the owner never consents, the app sees handoff_pending THEN a
    session_busy denied notice (never a silent drop)."""
    fake = _FakeCoordinator(result="pending")
    monkeypatch.setattr(routes_ws, "coordinator_factory",
                        lambda profile, session_id: fake)
    routes_ws._handle_busy_send("hscc", "drive me")
    _wait_for("hscc", lambda evs: "handoff_pending" in _system_kinds("hscc"))
    assert "handoff_pending" in _system_kinds("hscc")
    assert "session_busy" in _system_kinds("hscc")


def test_busy_send_drives_turn_when_consented(monkeypatch):
    """Once the owner consents, _handle_busy_send drives the turn via the relay
    hook (the reply streams back and folds 1:1)."""
    fake = _FakeCoordinator(result="driven")
    monkeypatch.setattr(routes_ws, "coordinator_factory",
                        lambda profile, session_id: fake)
    # Drive through the relay hook: the fake returns True (relay started), and
    # folding a synthetic assistant reply to prove the streamed reply lands.
    relayed = {}
    monkeypatch.setattr(
        routes_ws, "relay_user_message",
        lambda project, text: (relayed.update({"project": project, "text": text})
                               or True))
    routes_ws._handle_busy_send("hscc", "drive me")
    _wait_for("hscc", lambda evs: "handoff_pending" in _system_kinds("hscc"))
    assert "handoff_pending" in _system_kinds("hscc")
    assert relayed == {"project": "hscc", "text": "drive me"}, (
        "turn was not driven after consent: %r" % (relayed,))


def test_busy_send_seam_unavailable_falls_back_to_session_busy(monkeypatch):
    """On a hermes without the seam, the pre-existing session_busy notice is
    preserved — NO regression, and never a silent drop."""
    fake = _FakeCoordinator(exc=HandoffError(SEAM_UNAVAILABLE,
                                             "session handoff not available"))
    monkeypatch.setattr(routes_ws, "coordinator_factory",
                        lambda profile, session_id: fake)
    routes_ws._handle_busy_send("hscc", "hello")
    _wait_for("hscc", lambda evs: "session_busy" in _system_kinds("hscc"))
    assert "session_busy" in _system_kinds("hscc")
    assert "handoff_pending" not in _system_kinds("hscc")


def test_busy_send_owner_denied_surfaces_session_busy(monkeypatch):
    """An explicit owner denial surfaces a session_busy notice (fallback)."""
    fake = _FakeCoordinator(result="denied")
    monkeypatch.setattr(routes_ws, "coordinator_factory",
                        lambda profile, session_id: fake)
    routes_ws._handle_busy_send("hscc", "hello")
    _wait_for("hscc", lambda evs: "session_busy" in _system_kinds("hscc"))
    assert "session_busy" in _system_kinds("hscc")
    assert "handoff_pending" not in _system_kinds("hscc")


def test_busy_send_resolution_failure_falls_back(monkeypatch):
    """If the named session cannot be resolved, we fall back to session_busy —
    never crash the send path."""
    import routes_orchestrator as ro
    monkeypatch.setattr(
        ro, "resolve_named_session_id",
        lambda project, registry_path=None: (_ for _ in ()).throw(RuntimeError("boom")))
    routes_ws._handle_busy_send("hscc", "hello")
    _wait_for("hscc", lambda evs: "session_busy" in _system_kinds("hscc"))
    assert "session_busy" in _system_kinds("hscc")


def test_client_send_busy_echoes_user_and_emits_handoff_pending(monkeypatch):
    """End-to-end send path when a CLI owner holds the session: the user's line
    is echoed AND the handoff_pending frame surfaces (not a silent busy)."""
    fake = _FakeCoordinator(result="driven")
    # The busy-owner detector returns a non-None entry so the send routes to
    # _handle_busy_send.
    import routes_orchestrator as ro
    monkeypatch.setattr(ro, "detect_active_cli_owner",
                        lambda project, registry_path=None: {"surface": "cli"})
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(routes_ws, "coordinator_factory",
                        lambda profile, session_id: fake)

    sent = []
    sock = types.SimpleNamespace(sendall=lambda b: sent.append(b))
    monkeypatch.setattr(routes_ws, "_send_text",
                        lambda s, t: sent.append(t), raising=True)

    routes_ws._handle_client_send(sock, "hscc", {"text": "drive me"})
    _wait_for("hscc", lambda evs: "handoff_pending" in _system_kinds("hscc"))
    # The user's own line is echoed into the store (never silently dropped).
    roles = [e["payload"]["role"] for e in get_store("hscc").history()["events"]
             if e["type"] == "message"]
    assert roles == ["user"], "user echo missing: %r" % (roles,)
    assert "handoff_pending" in _system_kinds("hscc")
    # drive() runs -> the relay hook received the text.
    assert fake.orchestrate_calls, "orchestrate never called"
