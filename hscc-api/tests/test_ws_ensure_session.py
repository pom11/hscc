"""Guard: the WS relay must ensure the Hermes session exists before relaying.

Regression test for the WS chat gap. The REST chat handler
(``routes_orchestrator``) calls ``_ensure_session_exists(profile, session)``
which CREATEs the real Hermes session row in the profile's state.db on first
use (a brand-new project has NO chat session yet, so ``hermes chat -Q
--continue <session>`` fails with ``orchestrator_unavailable``). The WS relay
(``routes_ws._default_relay``) — the path the iOS app uses for chat — only
called the REGISTRY ``ensure_session`` (which just persists the session NAME),
so chatting a project whose Hermes session is missing failed instead of
creating it.

These tests assert the OUTCOME the relay must deliver: the create-helper is
invoked with the resolved profile + session BEFORE the message is relayed.
Without the fix the helper is never called, so ``created`` stays empty and the
test fails.
"""

import json
import time
import types

import pytest

import routes_ws
from session_event import TYPE_MESSAGE, get_store, reset_stores

from test_ws_relay_not_noop import _install_backing, _wait_for


@pytest.fixture(autouse=True)
def clean_stores():
    reset_stores()
    yield
    reset_stores()


def test_relay_ensures_session_exists_before_invoking(monkeypatch):
    """A project with no Hermes session must get one created, then relayed.

    Drives the WS relay entry point (``routes_ws._default_relay``) for a
    project whose session is missing, with the orchestrator backing faked so
    the relay's real job machinery runs hermetically. The create-helper is
    faked to RECORD the profile + session it was asked to ensure, standing in
    for ``routes_orchestrator._ensure_session_exists`` (the real one cannot be
    run in tests — it writes to a live profile's state.db). Asserting the
    helper was called is the point: that is the OUTCOME the relay must deliver.
    Without the fix the helper is never called and this test fails.
    """
    import routes_orchestrator as ro
    seen_invokes = []
    created = []

    def invoke(profile, session, text):
        seen_invokes.append((profile, session, text))
        return (f"reply to {text}", profile, session)

    _install_backing(monkeypatch, invoke)

    # Fake the real create-helper (it mutates a live profile's state.db, which
    # tests must never touch). Record that it was asked to ensure the session.
    def _fake_ensure(profile, session):
        created.append((profile, session))
        return {"created_session": "fake-new", "profile": profile,
                "title": session}

    monkeypatch.setattr(ro, "_ensure_session_exists", _fake_ensure)

    assert routes_ws._default_relay("hscc", "first message") is True

    events = _wait_for(
        "hscc",
        lambda evs: any(e["type"] == TYPE_MESSAGE
                        and e["payload"].get("role") == "assistant"
                        for e in evs))
    assert events, "relay produced no transcript events"

    # The create-helper must have been called BEFORE the invoke, with the SAME
    # profile + session the message was relayed on. Without the fix these lists
    # would be empty (the helper is never called).
    assert created, (
        "the WS relay never ensured the Hermes session exists — a project "
        "with no session would fail instead of being created: %r" % (created,))
    assert created[0][0] == "orch"
    assert created[0][1] == "hscc"
    assert seen_invokes, "message was never relayed after ensuring the session"
    assert seen_invokes[0] == ("orch", "hscc", "first message")

    # The message must actually have been relayed (session created AND relayed).
    replies = [e for e in events if e["type"] == TYPE_MESSAGE
               and e["payload"].get("role") == "assistant"]
    assert replies, "no assistant reply appended to the store"
    assert replies[-1]["payload"]["delta"] == "reply to first message"
