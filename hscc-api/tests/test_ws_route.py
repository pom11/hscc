"""Integration tests for the live session WebSocket endpoint (t_47f51a71).

Drive the real server over loopback with the independent ``websockets``
client (a distinct RFC 6455 implementation), cross-validating our handshake
and framing against an off-the-shelf client. Covers: auth rejection of
unauthenticated upgrades, the hello frame, history replay on resume,
live fan-out from store appends, gap-free/duplicate-free reconnect against a
shared seq space, and the inbound send path.

Because the WS session runs synchronously in the server's connection thread,
tests drive the client via ``asyncio.run`` per test and append to the store
from the test thread — the store fan-out reaches the live WS client in it.
"""

import asyncio
import json

import pytest
import types
import websockets

import api_server
import routes_ws  # noqa: F401  (registers the WS route at import)
import session_event
from session_event import MessagePayload, get_store, reset_stores
from tests.test_api import RunningServer


# --------------------------------------------------------------------------- #
# Fixtures (mirror test_session_event so this file is standalone)
# --------------------------------------------------------------------------- #

def _project(name="hscc"):
    return types.SimpleNamespace(name=name)


@pytest.fixture
def fakes(monkeypatch):
    fake_registry = types.SimpleNamespace(
        get_project=lambda name, path=None: _project(name=name),
        ProjectNotFoundError=type("ProjectNotFoundError", (Exception,), {}),
    )
    monkeypatch.setattr(routes_ws, "_registry", fake_registry)
    monkeypatch.setattr(
        routes_ws, "_registry_path", lambda ctx: "/tmp/fake-registry.yaml")
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


def _ws_uri(running, project="hscc", after=None):
    base = f"ws://{running.host}:{running.port}/v1/projects/{project}/session/ws"
    if after is not None:
        base += f"?after={after}"
    return base


def _connect(running, token, project="hscc", after=None):
    """Return a websockets client for the session WS endpoint."""
    headers = {"Authorization": f"Bearer {token}"}
    return websockets.connect(
        _ws_uri(running, project, after),
        additional_headers=headers,
        ping_interval=None,       # deterministic: only client-initiated pings
    )


async def _recv(ws):
    """Receive and parse the next server frame as JSON."""
    return json.loads(await ws.recv())


# --------------------------------------------------------------------------- #
# Auth: reject unauthenticated / bad-token upgrades
# --------------------------------------------------------------------------- #

def test_unauthenticated_upgrade_rejected(running):
    """No bearer token -> HTTP 401, never upgraded."""
    async def go():
        try:
            async with websockets.connect(
                _ws_uri(running), ping_interval=None,
            ) as ws:
                await ws.recv()
                pytest.fail("unauthenticated upgrade should have been rejected")
        except websockets.exceptions.InvalidStatus as exc:
            assert exc.response.status_code == 401
    asyncio.run(go())


def test_bad_token_upgrade_rejected(running):
    async def go():
        try:
            async with websockets.connect(
                _ws_uri(running),
                additional_headers={"Authorization": "Bearer wrong-token"},
                ping_interval=None,
            ) as ws:
                await ws.recv()
                pytest.fail("bad-token upgrade should have been rejected")
        except websockets.exceptions.InvalidStatus as exc:
            assert exc.response.status_code == 401
    asyncio.run(go())


def test_unknown_project_404_before_upgrade(running, token, fakes, monkeypatch,
                                            clean_store):
    def raises(name, path=None):
        raise fakes["_registry"].ProjectNotFoundError
    monkeypatch.setattr(fakes["_registry"], "get_project", raises)

    async def go():
        try:
            async with websockets.connect(
                _ws_uri(running, project="bogus"),
                additional_headers={"Authorization": f"Bearer {token}"},
                ping_interval=None,
            ) as ws:
                await ws.recv()
                pytest.fail("unknown project should not upgrade")
        except websockets.exceptions.InvalidStatus as exc:
            assert exc.response.status_code == 404
    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Hello + live replay
# --------------------------------------------------------------------------- #

def test_hello_then_live_stream(running, token, fakes, clean_store):
    """Fresh connect: hello, then live events as they're appended."""
    async def go():
        async with _connect(running, token) as ws:
            hello = await _recv(ws)
            assert hello["type"] == "hello"
            assert hello["payload"]["next_seq"] == 1
            boundary = hello["seq"]
            assert boundary == 1

            # Append live from the test thread; the WS must fan it out.
            store = get_store("hscc")
            store.append("message",
                         MessagePayload(role="assistant", delta="Hello "))
            store.append("message",
                         MessagePayload(role="assistant", delta="world"))

            frames = [await _recv(ws), await _recv(ws)]
            assert [f["seq"] for f in frames] == [1, 2]
            assert [f["type"] for f in frames] == ["message", "message"]
            payloads = [f["payload"] for f in frames]
            assert payloads[0]["delta"] == "Hello "
            assert payloads[1]["delta"] == "world"
            assert all(f["seq"] >= boundary for f in frames)
    asyncio.run(go())


def test_replay_existing_history_on_connect(running, token, fakes,
                                            clean_store):
    """Connect with after=s: replay stored (s, boundary], then live."""
    async def go():
        store = get_store("hscc")
        for i in range(1, 4):
            store.append("message",
                         MessagePayload(role="assistant", delta=f"d{i}"))
        # resume from seq 1 -> replay 2,3 (boundary=4)
        async with _connect(running, token, after=1) as ws:
            hello = await _recv(ws)
            assert hello["payload"]["next_seq"] == 4
            replay = [await _recv(ws), await _recv(ws)]
            assert [f["seq"] for f in replay] == [2, 3]
            # Live continues at 4.
            store.append("message",
                         MessagePayload(role="assistant", delta="d4"))
            live = await _recv(ws)
            assert live["seq"] == 4
    asyncio.run(go())


def test_resume_is_gap_free_and_duplicate_free(running, token, fakes,
                                               clean_store):
    """Reconnect: history up to s + WS from s = no gap, no repeat."""
    async def go():
        store = get_store("hscc")
        for i in range(1, 5):
            store.append("message",
                         MessagePayload(role="assistant", delta=f"d{i}"))
        # Client rendered seqs 1..3, reconnects with after=3.
        async with _connect(running, token, after=3) as ws:
            hello = await _recv(ws)
            assert hello["payload"]["next_seq"] == 5
            # Only seq 4 replays (nothing between 3 and boundary=5 except 4).
            replay = await _recv(ws)
            assert replay["seq"] == 4
            store.append("message",
                         MessagePayload(role="assistant", delta="d5"))
            live = await _recv(ws)
            assert live["seq"] == 5
            # Exactly the missing seqs, in order, once each.
    asyncio.run(go())


def test_resume_beyond_boundary_yields_only_live(running, token, fakes,
                                                 clean_store):
    """after == boundary: no replay, live stream starts immediately."""
    async def go():
        store = get_store("hscc")
        store.append("message",
                     MessagePayload(role="assistant", delta="d1"))
        async with _connect(running, token, after=2) as ws:
            hello = await _recv(ws)
            assert hello["payload"]["next_seq"] == 2
            store.append("message",
                         MessagePayload(role="assistant", delta="d2"))
            live = await _recv(ws)
            assert live["seq"] == 2
    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Inbound: relay app messages into the session
# --------------------------------------------------------------------------- #

def test_inbound_send_echoes_user_message(running, token, fakes, clean_store):
    """A client send text is appended to the store and fanned back out."""
    async def go():
        async with _connect(running, token) as ws:
            hello = await _recv(ws)
            boundary = hello["seq"]
            await ws.send(json.dumps({"kind": "send", "text": "deploy now"}))
            echo = await _recv(ws)
            assert echo["type"] == "message"
            assert echo["payload"]["role"] == "user"
            assert echo["payload"]["delta"] == "deploy now"
            assert echo["seq"] == 1
            # And it persisted into the shared store (history agrees).
            data = get_store("hscc").history()
            assert data["events"][0]["payload"] == {
                "role": "user", "delta": "deploy now", "done": True}
    asyncio.run(go())


def test_bad_inbound_json_gets_error_frame(running, token, fakes,
                                           clean_store):
    async def go():
        async with _connect(running, token) as ws:
            hello = await _recv(ws)
            await ws.send("this is not json")
            err = await _recv(ws)
            assert err["type"] == "error"
            assert err["payload"]["code"] == "bad_frame"
    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Multi-client fan-out
# --------------------------------------------------------------------------- #

def test_fanout_to_multiple_connections(running, token, fakes, clean_store):
    """Two clients both receive the same live event, once each."""
    async def go():
        async with _connect(running, token) as ws1:
            async with _connect(running, token) as ws2:
                assert (await _recv(ws1))["type"] == "hello"
                assert (await _recv(ws2))["type"] == "hello"
                get_store("hscc").append(
                    "message",
                    MessagePayload(role="assistant", delta="hi"))
                f1 = await _recv(ws1)
                f2 = await _recv(ws2)
                assert f1 == f2
                assert f1["seq"] == 1
    asyncio.run(go())
