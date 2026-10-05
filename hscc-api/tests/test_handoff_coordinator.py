"""Hermetic tests for :mod:`active_sessions_handoff` (t_7cc2e7d5).

The HSCC-side handoff coordinator is the HSCC consumption of the upstream
consented authorized-controller handoff seam (fork commit 77f046f089,
pom11/hermes-agent). These tests run WITHOUT the fork seam — they inject a fake
``hermes_cli.active_sessions`` (via ``primitives_factory``) and a fake registry
snapshot, so the coordinator's state machine is proven hermetically on any
deployment, including the pre-deploy one where the real seam is absent.

The cross-process probe (tests_gateway/probe_05_handoff_coordinator.py) is the
execution proof against the REAL seam code; both are part of the deliverable.
"""

from __future__ import annotations

import pytest

import active_sessions_handoff as ash
from active_sessions_handoff import (
    SEAM_UNAVAILABLE, HandoffCoordinator, HandoffError,
)


class _StubRefusal:
    def __init__(self, message: str, reason: str = "SESSION_NOT_OWNED"):
        self.reason = reason
        self._message = message

    def __str__(self):
        return self._message


class _StubLease:
    def __init__(self, lease_id="ctrl-lease-1"):
        self.lease_id = lease_id
        self.released = False

    def release(self):
        self.released = True


class _FakePrimitives:
    """A stand-in for ``hermes_cli.active_sessions`` (fork seam surface)."""

    def __init__(self, request_result=None, request_refusal=None,
                 complete_lease=None, complete_refusal=None, snapshot=None,
                 nonce="nonce-abc123"):
        self.request_result = request_result
        self.request_refusal = request_refusal
        self.complete_lease = complete_lease
        self.complete_refusal = complete_refusal
        self.snapshot = snapshot if snapshot is not None else []
        self.nonce = nonce
        self.request_calls = 0
        self.complete_calls = 0

    def request_active_session_handoff(self, session_id, *, controller,
                                       registry_home=None):
        self.request_calls += 1
        if self.request_refusal is not None:
            return None, self.request_refusal
        return self.request_result, None

    def complete_active_session_handoff(self, session_id, *, controller,
                                        nonce, surface, metadata=None,
                                        registry_home=None):
        self.complete_calls += 1
        if self.complete_refusal is not None:
            return None, self.complete_refusal
        return self.complete_lease, None

    def active_session_registry_snapshot(self, registry_home=None, *,
                                         strict=False):
        return self.snapshot


def _coordinator(fake, **overrides):
    def _factory():
        return fake
    return HandoffCoordinator(
        session_id="s1", profile="orch",
        primitives_factory=_factory,
        snapshot_fn=lambda home, strict=False: fake.snapshot,
        grant_poll_interval=0.01,
        **overrides)


def _reg_entry(handoff=None, session_id="s1"):
    entry = {"session_id": session_id, "surface": "cli", "pid": 999,
             "started_at": 1000.0}
    if handoff is not None:
        entry["handoff"] = handoff
    return entry


# --------------------------------------------------------------------------- #
# Seam availability
# --------------------------------------------------------------------------- #

def test_seam_available_true_when_primitives_present(monkeypatch):
    monkeypatch.setattr(ash, "_import_primitives", lambda: object())
    assert ash.seam_available() is True


def test_seam_available_false_when_primitives_absent(monkeypatch):
    monkeypatch.setattr(ash, "_import_primitives", lambda: None)
    assert ash.seam_available() is False


# --------------------------------------------------------------------------- #
# request()
# --------------------------------------------------------------------------- #

def test_request_raises_seam_unavailable_when_no_primitives():
    c = HandoffCoordinator(session_id="s1", profile="orch",
                           primitives_factory=lambda: None)
    with pytest.raises(HandoffError) as ei:
        c.request(registry_home=None)
    assert ei.value.code == SEAM_UNAVAILABLE


def test_request_returns_granted_false_when_pending():
    fake = _FakePrimitives(request_result={"granted": False})
    c = _coordinator(fake)
    assert c.request(registry_home=None) == {"granted": False}


def test_request_returns_granted_true_when_already_consented():
    fake = _FakePrimitives(request_result={"granted": True})
    c = _coordinator(fake)
    assert c.request(registry_home=None) == {"granted": True}


def test_request_raises_when_no_live_owner():
    fake = _FakePrimitives(
        request_refusal=_StubRefusal("No live Hermes process currently owns "
                                     "session s1; nothing to hand off."))
    c = _coordinator(fake)
    with pytest.raises(HandoffError) as ei:
        c.request(registry_home=None)
    assert ei.value.code == "SESSION_NOT_OWNED"


# --------------------------------------------------------------------------- #
# poll_for_nonce()
# --------------------------------------------------------------------------- #

def test_poll_for_nonce_returns_granted_nonce():
    fake = _FakePrimitives(snapshot=[_reg_entry(
        handoff={"controller": "hscc-app", "granted": True,
                 "nonce": "nn-1", "requested_at": 1000.0})])
    c = _coordinator(fake)
    assert c.poll_for_nonce(registry_home=None, timeout=1.0) == "nn-1"


def test_poll_for_nonce_times_out_when_owner_never_consents():
    # Owner entry exists but the ask is never granted (stays pending).
    fake = _FakePrimitives(snapshot=[_reg_entry(
        handoff={"controller": "hscc-app", "granted": False,
                 "nonce": None, "requested_at": 1000.0})])
    c = _coordinator(fake, grant_timeout=0.05)
    with pytest.raises(HandoffError) as ei:
        c.poll_for_nonce(registry_home=None, timeout=0.05)
    assert ei.value.code == "grant_timeout"


def test_poll_for_nonce_denied_when_ask_cleared_or_replaced():
    # Owner entry's handoff is no longer addressed to our controller
    # (denied / re-requested by someone else) — bail, don't wait the TTL.
    fake = _FakePrimitives(snapshot=[_reg_entry(
        handoff={"controller": "other-app", "granted": False,
                 "nonce": None, "requested_at": 1000.0})])
    c = _coordinator(fake)
    with pytest.raises(HandoffError) as ei:
        c.poll_for_nonce(registry_home=None, timeout=1.0)
    assert ei.value.code == "handoff_denied"


def test_poll_for_nonce_skips_other_sessions():
    # A different session's granted handoff must not satisfy OUR poll.
    fake = _FakePrimitives(snapshot=[
        _reg_entry(session_id="other", handoff={
            "controller": "hscc-app", "granted": True, "nonce": "nn-other",
            "requested_at": 1000.0}),
        _reg_entry(session_id="s1", handoff={
            "controller": "hscc-app", "granted": True, "nonce": "nn-s1",
            "requested_at": 1000.0}),
    ])
    c = _coordinator(fake)
    assert c.poll_for_nonce(registry_home=None, timeout=0.5) == "nn-s1"


# --------------------------------------------------------------------------- #
# complete() / release()
# --------------------------------------------------------------------------- #

def test_complete_returns_lease_on_success():
    lease = _StubLease()
    fake = _FakePrimitives(complete_lease=lease)
    c = _coordinator(fake)
    assert c.complete("nonce-abc123", registry_home=None) is lease
    assert fake.complete_calls == 1


def test_complete_raises_on_refusal():
    fake = _FakePrimitives(
        complete_refusal=_StubRefusal("Session s1 has no valid consented "
                                      "handoff for controller 'hscc-app'."))
    c = _coordinator(fake)
    with pytest.raises(HandoffError) as ei:
        c.complete("bad-nonce", registry_home=None)
    assert ei.value.code == "SESSION_NOT_OWNED"


def test_release_is_best_effort():
    released = []
    class _L:
        def release(self):
            released.append(True)
    c = _coordinator(_FakePrimitives())
    c.release(_L(), session_id="s1")
    assert released == [True]
    # A None lease is a no-op, never raises.
    c.release(None, session_id="s1")


# --------------------------------------------------------------------------- #
# orchestrate() — the full state machine
# --------------------------------------------------------------------------- #

def test_orchestrate_propagates_seam_unavailable():
    c = HandoffCoordinator(session_id="s1", profile="orch",
                           primitives_factory=lambda: None)
    with pytest.raises(HandoffError) as ei:
        c.orchestrate("hello", drive=lambda: None)
    assert ei.value.code == SEAM_UNAVAILABLE


def test_orchestrate_full_cycle_when_consent_already_granted():
    """Owner already consented in an earlier round-trip (request returns
    granted=True): the coordinator skips the wait, completes immediately, drives,
    and releases."""
    lease = _StubLease()
    fake = _FakePrimitives(
        request_result={"granted": True},
        complete_lease=lease,
        snapshot=[_reg_entry(handoff={
            "controller": "hscc-app", "granted": True, "nonce": "nn-1",
            "requested_at": 1000.0})])
    c = _coordinator(fake, grant_timeout=0.05)
    events = []

    def on_pending():
        events.append("pending")

    def on_denied(m):
        events.append(("denied", m))

    drove = []
    result = c.orchestrate("hello", drive=lambda: drove.append(1),
                           on_pending=on_pending, on_denied=on_denied,
                           registry_home=None)
    assert result["outcome"] == "completed"
    assert drove == [1]
    assert events == []          # already granted → no pending emitted
    assert lease.released is True  # lease handed back
    assert fake.complete_calls == 1


def test_orchestrate_calls_on_working_when_turn_starts():
    """Once the owner consents and the lease transfers, ``on_working`` fires
    right before ``drive()`` — so the app flips away from the awaiting-approval
    banner while the orchestrated turn runs (acceptance bullet)."""
    lease = _StubLease()
    # Request returns granted=True (owner already consented in an earlier
    # round-trip) so the ask is moot and it goes straight to drive.
    fake = _FakePrimitives(
        request_result={"granted": True},
        complete_lease=lease,
        snapshot=[_reg_entry(handoff={
            "controller": "hscc-app", "granted": True, "nonce": "nn-1",
            "requested_at": 1000.0})])
    c = _coordinator(fake, grant_timeout=0.05)
    order = []

    def on_pending():
        order.append("pending")

    def on_working():
        order.append("working")

    def drive():
        order.append("drive")

    result = c.orchestrate("hello", drive=drive,
                           on_pending=on_pending, on_working=on_working,
                           registry_home=None)
    assert result["outcome"] == "completed"
    # working must fire immediately before drive (and after complete) so the
    # indicator is live for the whole orchestrated turn.
    assert "working" in order, "on_working never fired on the driven path"
    assert order.index("working") == order.index("drive") - 1, order


def test_orchestrate_denied_never_calls_on_working():
    """A denied/timeout path must NOT emit a working frame (nothing drove)."""
    fake = _FakePrimitives(
        request_result={"granted": False},
        snapshot=[_reg_entry(handoff={
            "controller": "hscc-app", "granted": False, "nonce": None,
            "requested_at": 1000.0})])
    c = _coordinator(fake, grant_timeout=0.05)
    worked = []

    def on_working():
        worked.append(True)

    result = c.orchestrate("hello", drive=lambda: None, on_working=on_working,
                           registry_home=None)
    assert result["outcome"] == "denied"
    assert worked == [], "on_working must not fire when the turn never drove"


def test_orchestrate_denied_when_owner_times_out():
    """Owner never consents: the coordinator surfaces a denied notice and does
    NOT drive the turn or hold a lease."""
    fake = _FakePrimitives(
        request_result={"granted": False},
        snapshot=[_reg_entry(handoff={
            "controller": "hscc-app", "granted": False, "nonce": None,
            "requested_at": 1000.0})])
    c = _coordinator(fake, grant_timeout=0.05)
    events = []

    def on_pending():
        events.append("pending")

    def on_denied(m):
        events.append(("denied", m))

    drove = []
    result = c.orchestrate("hello", drive=lambda: drove.append(1),
                           on_pending=on_pending, on_denied=on_denied,
                           registry_home=None)
    assert result["outcome"] == "denied"
    assert result["reason"] == "grant_timeout"
    assert events == ["pending", ("denied", "Timed out waiting for the owner to approve.")]
    assert drove == []                              # never drove
    assert fake.complete_calls == 0                 # never completed


def test_orchestrate_denied_when_no_live_owner():
    fake = _FakePrimitives(
        request_refusal=_StubRefusal("No live Hermes process currently owns "
                                     "session s1; nothing to hand off."))
    c = _coordinator(fake)
    events = []
    result = c.orchestrate("hello", drive=lambda: None,
                           on_denied=lambda m: events.append(m),
                           registry_home=None)
    assert result["outcome"] == "denied"
    assert result["reason"] == "SESSION_NOT_OWNED"
    assert events                       # surfaced a denied notice
