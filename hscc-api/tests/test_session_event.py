"""Tests for the session-event wire contract (t_47f51a71).

Pins the exact JSON frame shapes the iOS decoders (t_1ff4dcbd) and history
pager (t_2776ea3c) build against, plus the seq/cursor semantics that make
reconnect gap-free (t_218cb9ec), plus the history endpoint itself.

The store half is pure Python (no I/O) and is tested directly. The endpoint
half is driven over real loopback HTTP with routes_session._registry faked to
a hermetic namespace, exactly like the A3 project-route tests.
"""

import http.client
import threading
import types

import pytest

import api_server
import routes_session
import session_event
from session_event import (
    AgentPayload,
    CardPayload,
    ErrorPayload,
    HelloPayload,
    MessagePayload,
    SystemPayload,
    ToolCallPayload,
    get_store,
    reset_stores,
)
from tests.test_api import RunningServer


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _project(name="hscc"):
    return types.SimpleNamespace(name=name)


@pytest.fixture
def fakes(monkeypatch):
    """Hermetic flightdeck registry fake so no real registry file is read."""
    fake_registry = types.SimpleNamespace(
        get_project=lambda name, path=None: _project(name=name),
        ProjectNotFoundError=type("ProjectNotFoundError", (Exception,), {}),
    )
    monkeypatch.setattr(routes_session, "_registry", fake_registry)
    monkeypatch.setattr(
        routes_session, "_registry_path", lambda ctx: "/tmp/fake-registry.yaml")
    return {"_registry": fake_registry}


@pytest.fixture
def running(tmp_path, fakes):
    srv = RunningServer(hscc_dir=str(tmp_path))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


@pytest.fixture
def clean_store():
    reset_stores()
    yield
    reset_stores()


# --------------------------------------------------------------------------- #
# Event TYPE payload shapes (the pinned wire contract)
# --------------------------------------------------------------------------- #

def test_hello_frame_shape():
    assert HelloPayload(next_seq=7).to_json() == {"next_seq": 7}


def test_message_delta_frame_shape():
    assert MessagePayload(role="assistant", delta="Hello").to_json() == {
        "role": "assistant", "delta": "Hello", "done": False,
    }
    assert MessagePayload(role="assistant", delta="", done=True).to_json() == {
        "role": "assistant", "delta": "", "done": True,
    }


def test_tool_call_start_finish_shape():
    start = ToolCallPayload(
        call_id="c1", name="kanban_create",
        status="start", args={"title": "Do a thing"}).to_json()
    assert start == {
        "call_id": "c1", "name": "kanban_create", "status": "start",
        "args": {"title": "Do a thing"},
    }
    finish = ToolCallPayload(
        call_id="c1", name="kanban_create", status="finish",
        result={"id": "t_x"}, duration_s=1.2).to_json()
    assert finish == {
        "call_id": "c1", "name": "kanban_create", "status": "finish",
        "result": {"id": "t_x"}, "duration_s": 1.2,
    }


def test_card_frame_shape():
    assert CardPayload(board="default", id="t_abc", title="Do a thing",
                       status="blocked").to_json() == {
        "board": "default", "id": "t_abc", "title": "Do a thing",
        "status": "blocked",
    }


def test_agent_frame_shape():
    assert AgentPayload(role="researcher-a", action="spawned").to_json() == {
        "role": "researcher-a", "action": "spawned",
    }
    assert AgentPayload(role="researcher-a", action="finished",
                        task="summarize").to_json() == {
        "role": "researcher-a", "action": "finished", "task": "summarize",
    }


def test_system_frame_shape():
    assert SystemPayload(kind="cron",
                         details={"job": "daily"}).to_json() == {
        "kind": "cron", "details": {"job": "daily"},
    }
    assert SystemPayload(kind="compaction").to_json() == {"kind": "compaction"}


def test_error_frame_shape():
    assert ErrorPayload(code="session_missing",
                        message="session not found").to_json() == {
        "code": "session_missing", "message": "session not found",
    }


def test_event_envelope_shape(clean_store):
    store = get_store("hscc")
    store.append("message", MessagePayload(role="assistant", delta="hi"))
    ev = store.history()["events"][0]
    assert set(ev) == {"seq", "type", "ts", "payload"}
    assert ev["seq"] == 1
    assert ev["type"] == "message"
    assert ev["payload"] == {"role": "assistant", "delta": "hi", "done": False}
    assert ev["ts"].endswith("Z")  # ISO-8601 UTC


def test_unknown_event_type_raises(clean_store):
    store = get_store("x")
    with pytest.raises(ValueError, match="unknown event type"):
        store.append("bogus", HelloPayload(next_seq=1))


# --------------------------------------------------------------------------- #
# Store seq/cursor semantics (gap-free reconnect contract)
# --------------------------------------------------------------------------- #

def test_seq_contiguous_and_per_project(clean_store):
    s1, s2 = get_store("alpha"), get_store("beta")
    assert s1.append("message", MessagePayload(role="a", delta="1")) == 1
    assert s1.append("message", MessagePayload(role="a", delta="2")) == 2
    # A different project starts its OWN sequence at 1.
    assert s2.append("message", MessagePayload(role="a", delta="x")) == 1
    assert s1.next_seq == 3
    assert s2.next_seq == 2


def test_history_newest_page(clean_store):
    s = get_store("alpha")
    for i in range(1, 6):
        s.append("message", MessagePayload(role="assistant", delta=f"d{i}"))
    data = s.history(limit=200)
    assert [e["seq"] for e in data["events"]] == [1, 2, 3, 4, 5]
    assert data["oldest_seq"] == 1
    assert data["next_seq"] == 6
    assert data["next_before"] is None  # no older page


def test_history_before_and_paging(clean_store):
    s = get_store("alpha")
    for i in range(1, 6):
        s.append("message", MessagePayload(role="assistant", delta=f"d{i}"))
    # Page back from seq 5: events with seq < 5 -> [1,2,3,4]; newest `limit` 2.
    page = s.history(before=5, limit=2)
    assert [e["seq"] for e in page["events"]] == [3, 4]
    assert page["next_before"] == 3  # next OLDER page is everything before 3
    page2 = s.history(before=3, limit=2)
    assert [e["seq"] for e in page2["events"]] == [1, 2]
    assert page2["next_before"] is None


def test_history_continuity_to_live_stream(clean_store):
    """History up to `s` + live from `s+1` = no gap, no duplicate."""
    s = get_store("alpha")
    for i in range(1, 4):
        s.append("message", MessagePayload(role="assistant", delta=f"d{i}"))
    # Client has rendered up to seq 2; asks for the strict older bound.
    assert [e["seq"] for e in s.history(before=3)["events"]] == [1, 2]
    assert s.next_seq == 4
    # The live bridge continues appending seq 4, 5... same contiguous space.
    s.append("message", MessagePayload(role="assistant", delta="d4"))
    s.append("message", MessagePayload(role="assistant", delta="d5"))
    assert [e["seq"] for e in s.history()["events"]] == [1, 2, 3, 4, 5]


def test_ring_eviction_keeps_contiguous_seq(clean_store):
    store = session_event.SessionEventStore(capacity=3)
    for i in range(1, 7):
        store.append("message", MessagePayload(role="assistant", delta=f"d{i}"))
    data = store.history()
    assert [e["seq"] for e in data["events"]] == [4, 5, 6]
    assert data["oldest_seq"] == 4
    assert data["next_seq"] == 7
    # Eviction must not break next_seq monotonicity.
    assert store.append("message",
                        MessagePayload(role="assistant", delta="d7")) == 7


def test_limit_clamped_and_zero(clean_store):
    s = get_store("alpha")
    for i in range(1, 6):
        s.append("message", MessagePayload(role="assistant", delta=f"d{i}"))
    page = s.history(limit=3)
    assert [e["seq"] for e in page["events"]] == [3, 4, 5]
    assert page["next_before"] == 3
    meta = s.history(limit=0)
    assert meta["events"] == []
    assert meta["next_seq"] == 6


# --------------------------------------------------------------------------- #
# Live-stream subscription + gap-free resume (the WS bridge backend)
# --------------------------------------------------------------------------- #

def test_append_fans_out_to_subscribers(clean_store):
    s = get_store("alpha")
    got = []
    s.subscribe(lambda ev: got.append(ev.seq))
    s.append("message", MessagePayload(role="assistant", delta="a"))
    s.append("message", MessagePayload(role="assistant", delta="b"))
    assert got == [1, 2]


def test_unsubscribe_stops_fanout(clean_store):
    s = get_store("alpha")
    got = []
    listener = _store_listener(got)
    s.subscribe(listener)
    s.append("message", MessagePayload(role="assistant", delta="a"))
    assert got == [1]
    s.unsubscribe(listener)
    s.append("message", MessagePayload(role="assistant", delta="b"))
    assert got == [1]  # no further fan-out after unsubscribe


def test_unsubscribe_is_idempotent(clean_store):
    s = get_store("alpha")
    got = []
    listener = _store_listener(got)
    s.subscribe(listener)
    s.unsubscribe(listener)
    s.unsubscribe(listener)  # must not raise
    s.append("message", MessagePayload(role="assistant", delta="a"))
    assert got == []


def _store_listener(got):
    def _l(ev):
        got.append(ev.seq)
    return _l


def test_listener_receives_fully_stamped_event(clean_store):
    s = get_store("alpha")
    seen = {}
    s.subscribe(lambda ev: seen.update(seq=ev.seq, type=ev.type,
                                       ts=ev.ts, payload=ev.payload))
    s.append("message", MessagePayload(role="assistant", delta="hi"))
    assert seen["seq"] == 1
    assert seen["type"] == "message"
    assert seen["ts"].endswith("Z")
    assert seen["payload"].to_json() == {"role": "assistant", "delta": "hi",
                                         "done": False}


def test_fanout_does_not_block_append_on_slow_listener(clean_store):
    """A throwing listener is swallowed; the append still returns the seq."""
    s = get_store("alpha")
    s.subscribe(lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
    assert s.append("message", MessagePayload(role="assistant", delta="a")) == 1


def test_snapshot_and_subscribe_replay_with_resume_cursor(clean_store):
    s = get_store("alpha")
    for i in range(1, 6):
        s.append("message", MessagePayload(role="assistant", delta=f"d{i}"))
    # Client has rendered up to seq 2; resume from 3.
    boundary, replay, q = s.snapshot_and_subscribe(after=2)
    assert boundary == 6
    assert [e.seq for e in replay] == [3, 4, 5]
    # Live events after the boundary land on the queue only.
    s.append("message", MessagePayload(role="assistant", delta="d6"))
    s.append("message", MessagePayload(role="assistant", delta="d7"))
    live = [q.get_nowait().seq for _ in range(2)]
    assert live == [6, 7]
    s.unsubscribe(q.put)


def test_snapshot_keeps_next_seq_monotonic_across_resume(clean_store):
    """Repeated subscribes never perturb the shared seq space."""
    s = get_store("alpha")
    s.append("message", MessagePayload(role="assistant", delta="d1"))
    store = s
    first = store.snapshot_and_subscribe(after=0)
    second = store.snapshot_and_subscribe(after=first[0] - 1)
    assert second[0] == first[0] == 2
    store.unsubscribe(first[2].put)
    store.unsubscribe(second[2].put)


# --------------------------------------------------------------------------- #
# History endpoint over HTTP
# --------------------------------------------------------------------------- #

def test_endpoint_returns_frames(running, token, fakes, clean_store):
    store = get_store("hscc")
    store.append("message", MessagePayload(role="assistant", delta="hello"))
    store.append("tool_call", ToolCallPayload(
        call_id="c1", name="kanban_create", status="start",
        args={"title": "Do a thing"}))
    status, payload = running.request(
        path="/v1/projects/hscc/session/events", token=token)
    assert status == 200
    assert payload["project"] == "hscc"
    assert [e["seq"] for e in payload["events"]] == [1, 2]
    assert payload["next_seq"] == 3
    assert payload["oldest_seq"] == 1
    assert isinstance(payload["speak"], str) and payload["speak"]
    assert payload["events"][0]["payload"] == {
        "role": "assistant", "delta": "hello", "done": False,
    }


def test_endpoint_before_cursor(running, token, fakes, clean_store):
    store = get_store("hscc")
    for i in range(1, 4):
        store.append("message", MessagePayload(role="assistant", delta=f"d{i}"))
    status, payload = running.request(
        path="/v1/projects/hscc/session/events?before=3&limit=1", token=token)
    assert status == 200
    assert [e["seq"] for e in payload["events"]] == [2]
    assert payload["next_before"] == 2
    assert payload["next_seq"] == 4


def test_endpoint_auth_401(running):
    status, _ = running.request(path="/v1/projects/hscc/session/events")
    assert status == 401


def test_endpoint_unknown_project_404(running, token, fakes, monkeypatch,
                                      clean_store):
    def raises(name, path=None):
        raise fakes["_registry"].ProjectNotFoundError
    monkeypatch.setattr(fakes["_registry"], "get_project", raises)
    status, payload = running.request(
        path="/v1/projects/bogus/session/events", token=token)
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_endpoint_bad_params_400(running, token, fakes, clean_store):
    status, _ = running.request(
        path="/v1/projects/hscc/session/events?before=abc", token=token)
    assert status == 400
    status, _ = running.request(
        path="/v1/projects/hscc/session/events?limit=-1", token=token)
    assert status == 400
