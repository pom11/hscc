"""tmux-first write path (REPL-injection 2026-10-13).

The operator's rule: an iPhone message must type into the SAME REPL they
have open in the terminal. When the project's REPL pane exists in tmux the
WS send path delivers via repl_inject; the serve PTY is the fallback used
ONLY when no tmux REPL exists. Every routing decision must be visible in
the transcript (details.via) — the operator must be able to tell from the
event stream which path carried their message.

Pane resolution is stubbed at the seam (routes_ws._tmux_pane_for) or below
(resolve_named_session_id + repl_inject.resolve_pane); nothing here touches
a real tmux. The repl_inject module additionally refuses its raw seams
under pytest (see test_repl_inject), so even a forgotten stub cannot type
into a live pane.
"""

import time
import types

import pytest

import repl_inject
import routes_ws
from session_event import TYPE_SYSTEM, get_store, reset_stores

PANE = {"pane": "%1", "target": "proj-sess:0.0", "tty": "/dev/ttys003",
        "profile": "proj-orch", "cmd": "hermes -p proj-orch chat"}


@pytest.fixture(autouse=True)
def clean_stores():
    reset_stores()
    yield
    reset_stores()


def _wait_for(project, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = get_store(project).history()["events"]
        if predicate(events):
            return events
        time.sleep(0.02)
    return get_store(project).history()["events"]


def _sys_events(events):
    return [e for e in events if e["type"] == TYPE_SYSTEM]


def _send(project, text):
    sock = types.SimpleNamespace(sendall=lambda b: None)
    routes_ws._handle_client_send(sock, project, {"text": text})


# --------------------------------------------------------------------------- #
# routing: tmux pane found -> inject, NEVER serve relay
# --------------------------------------------------------------------------- #

def test_pane_found_injects_and_never_uses_serve_relay(monkeypatch):
    injects = []
    monkeypatch.setattr(routes_ws, "_tmux_pane_for", lambda p: (PANE, None))

    def fake_deliver(project, text, pane):
        injects.append((project, text, pane))
        # emulate the real receipt frame the worker appends
        from session_event import SystemPayload
        get_store(project).append(TYPE_SYSTEM, SystemPayload(
            kind="working", details={"text": text, "via": "tmux",
                                     "pane": pane["target"], "busy": False,
                                     "mode": "plain", "queued": False}))

    monkeypatch.setattr(routes_ws, "_tmux_deliver", fake_deliver)
    relay_calls = []
    monkeypatch.setattr(routes_ws, "relay_user_message",
                        lambda p, t: relay_calls.append((p, t)) or True)

    _send("proj", "hello from iphone")

    events = _wait_for("proj", lambda ev: injects)
    assert injects == [("proj", "hello from iphone", PANE)]
    assert relay_calls == []                       # serve path NOT taken
    # user echo still present (1:1 transcript)
    assert any(e["payload"].get("role") == "user" for e in events)
    w = [e for e in _sys_events(events)
         if e["payload"].get("kind") == "working"]
    assert w and w[-1]["payload"]["details"]["via"] == "tmux"
    assert w[-1]["payload"]["details"]["pane"] == "proj-sess:0.0"


# --------------------------------------------------------------------------- #
# routing: no pane -> serve fallback, labeled via=serve
# --------------------------------------------------------------------------- #

def test_no_pane_falls_back_to_serve_and_says_so(monkeypatch):
    monkeypatch.setattr(routes_ws, "_tmux_pane_for", lambda p: (None, None))
    import routes_orchestrator as _ro
    monkeypatch.setattr(_ro, "detect_active_cli_owner",
                        lambda p, rp=None: None)
    calls = []
    monkeypatch.setattr(routes_ws, "relay_user_message",
                        lambda p, t: calls.append((p, t)) or True)

    _send("proj", "no terminal open")

    events = _wait_for("proj", lambda ev: calls)
    assert calls == [("proj", "no terminal open")]
    w = [e for e in _sys_events(events)
         if e["payload"].get("kind") == "working"]
    assert w and w[-1]["payload"]["details"]["via"] == "serve"


# --------------------------------------------------------------------------- #
# routing: AMBIGUOUS panes -> refuse honestly, NEVER serve-fallback
# --------------------------------------------------------------------------- #

def test_ambiguous_pane_refuses_and_never_falls_back(monkeypatch):
    monkeypatch.setattr(routes_ws, "_tmux_pane_for",
                        lambda p: (None, "2 tmux panes run a REPL for 'proj-orch'"))
    calls = []
    monkeypatch.setattr(routes_ws, "relay_user_message",
                        lambda p, t: calls.append((p, t)) or True)

    _send("proj", "which pane?")

    events = _wait_for("proj", lambda ev:
                       any(e["payload"].get("kind") == "tmux_ambiguous"
                           for e in _sys_events(ev)) or bool(calls))
    assert calls == []                             # NEVER silent serve fallback
    amb = [e for e in _sys_events(events)
           if e["payload"].get("kind") == "tmux_ambiguous"]
    assert amb and "did not guess" in amb[0]["payload"]["details"]["message"]


# --------------------------------------------------------------------------- #
# _tmux_pane_for itself: project -> profile -> pane, tri-state
# --------------------------------------------------------------------------- #

def test_pane_for_uses_named_session_profile(monkeypatch):
    import routes_orchestrator as _ro
    monkeypatch.setattr(_ro, "resolve_named_session_id",
                        lambda p, registry_path=None: ("proj-orch", "proj", "sid"))
    resolved = []

    def fake_resolve(profile, panes=None, pane_override=None):
        resolved.append(profile)
        return PANE
    monkeypatch.setattr(repl_inject, "resolve_pane", fake_resolve)
    pane, amb = routes_ws._tmux_pane_for("proj")
    assert resolved == ["proj-orch"] and pane is PANE and amb is None


def test_pane_for_no_profile_is_clean_fallback(monkeypatch):
    import routes_orchestrator as _ro
    monkeypatch.setattr(_ro, "resolve_named_session_id",
                        lambda p, registry_path=None: (None, None, None))
    assert routes_ws._tmux_pane_for("proj") == (None, None)


def test_pane_for_no_panes_is_clean_fallback(monkeypatch):
    import routes_orchestrator as _ro
    monkeypatch.setattr(_ro, "resolve_named_session_id",
                        lambda p, registry_path=None: ("proj-orch", "proj", "sid"))

    def raise_none(profile, panes=None, pane_override=None):
        raise repl_inject.InjectError("no_panes", "no tmux pane ...")
    monkeypatch.setattr(repl_inject, "resolve_pane", raise_none)
    assert routes_ws._tmux_pane_for("proj") == (None, None)


def test_pane_for_ambiguous_propagates_message(monkeypatch):
    import routes_orchestrator as _ro
    monkeypatch.setattr(_ro, "resolve_named_session_id",
                        lambda p, registry_path=None: ("proj-orch", "proj", "sid"))

    def raise_amb(profile, panes=None, pane_override=None):
        raise repl_inject.InjectError("ambiguous", "2 tmux pane ...")
    monkeypatch.setattr(repl_inject, "resolve_pane", raise_amb)
    pane, amb = routes_ws._tmux_pane_for("proj")
    assert pane is None and amb and "2 tmux pane" in amb


def test_pane_for_resolution_failure_never_raises(monkeypatch):
    """Any surprise (UnknownProjectError etc.) degrades to clean fallback —
    the socket loop must never die over a routing probe."""
    import routes_orchestrator as _ro

    def boom(p, registry_path=None):
        raise RuntimeError("registry exploded")
    monkeypatch.setattr(_ro, "resolve_named_session_id", boom)
    assert routes_ws._tmux_pane_for("proj") == (None, None)


# --------------------------------------------------------------------------- #
# _tmux_deliver: receipt frame + honest failure frame
# --------------------------------------------------------------------------- #

def test_deliver_appends_receipt_with_via(monkeypatch):
    monkeypatch.setattr(repl_inject, "deliver",
                        lambda pane, text, mode="auto", sleep_fn=None: {
                            "pane": "%1", "target": "s:0.0", "profile": "p",
                            "busy": True, "mode": "queue",
                            "sent_text": "/queue " + text})
    routes_ws._tmux_deliver("proj", "hi", PANE)
    evs = _sys_events(get_store("proj").history()["events"])
    w = [e for e in evs if e["payload"].get("kind") == "working"]
    assert w[-1]["payload"]["details"]["via"] == "tmux"
    assert w[-1]["payload"]["details"]["queued"] is True
    assert w[-1]["payload"]["details"]["mode"] == "queue"


def test_deliver_failure_is_honest_not_silent(monkeypatch):
    def boom(pane, text, mode="auto", sleep_fn=None):
        raise repl_inject.InjectError("tmux", "paste-buffer failed: pane gone")
    monkeypatch.setattr(repl_inject, "deliver", boom)
    routes_ws._tmux_deliver("proj", "hi", PANE)
    evs = _sys_events(get_store("proj").history()["events"])
    fail = [e for e in evs if e["payload"].get("kind") == "tmux_delivery_failed"]
    assert fail and fail[0]["payload"]["details"]["via"] == "tmux"
    assert "pane gone" in fail[0]["payload"]["details"]["message"]
    # and NOT the busy notice / NOT a hanging working frame
    assert not [e for e in evs if e["payload"].get("kind") == "working"]
