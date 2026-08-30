"""Tests for the outbound hermes gateway driver (t_776e294a).

Covers the client framing (masked encode + unmasked server decode, cross-
checked against the repo's server-side ws_frame implementation), the
:class:`FrameTranslator` mapping real captured native frames into session_event
store frames, and the :class:`GatewayDriver` WS-relay hook integration with
routes_ws.

The translation tests use representative native frames whose shapes are taken
VERBATIM from the real isolated live-probe corpus at
``tests_gateway/probe_03_tool_frames.jsonl`` (see
``tests_gateway/FINDINGS_gateway_protocol.md``). The driver never connects to
the live operator gateway in tests — transport is unit-tested with local
socketpair sockets only.
"""

import json
import socket
import threading
import types

import pytest

import gateway_driver as gd
import routes_ws  # noqa: F401  (importable for the relay-hook assertions)
import ws_frame as ws_frame_server
from session_event import get_store, reset_stores
from gateway_driver import FrameTranslator, GatewayConfig, GatewayDriver


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def clean_store():
    reset_stores()
    yield
    reset_stores()


@pytest.fixture
def translator(clean_store):
    return FrameTranslator(get_store("hscc"))


@pytest.fixture
def socket_pair():
    """A connected (a, b) socketpair for client-side decode tests."""
    a, b = socket.socketpair()
    yield a, b
    a.close()
    b.close()


@pytest.fixture
def send_pty(clean_store):
    """A driver whose _pty is a real socketpair-backed _WSClient (send path).

    Returns a SimpleNamespace with ``drv`` (the GatewayDriver), ``peer`` (the
    other end of the socketpair, to observe what the driver sent), and
    ``decoder`` (a server-side FrameDecoder to unmask the client's frames).
    """
    cfg = GatewayConfig(host="127.0.0.1", port=1, token="t", project="hscc")
    drv = GatewayDriver(cfg)
    sock, peer = socket.socketpair()
    pty = gd._WSClient("127.0.0.1", 0, "/api/pty")
    pty._sock = sock
    drv._pty = pty
    drv._alive = True
    ns = types.SimpleNamespace(
        drv=drv, peer=peer, decoder=ws_frame_server.FrameDecoder())
    yield ns
    sock.close()
    peer.close()


# --------------------------------------------------------------------------- #
# Native -> session_event helpers
# --------------------------------------------------------------------------- #

def _msg(type_, payload=None, sid="8eb874e8"):
    """Build a native event notification in the captured envelope shape."""
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": type_, "session_id": sid},
    }
    if payload is not None:
        frame["params"]["payload"] = payload
    return frame


def _jsonrpc_result(id_, result):
    """A native JSON-RPC RESPONSE (must be ignored by the translator)."""
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _count(store):
    return store.history(limit=0)["next_seq"] - 1


# --------------------------------------------------------------------------- #
# FrameTranslator: native -> session_event (real corpus shapes)
# --------------------------------------------------------------------------- #

def test_jsonrpc_responses_are_ignored(translator):
    """Native responses (id+result, no method:event) never reach the store."""
    translator.on_frame(_jsonrpc_result("r1", {"sessions": []}))
    translator.on_frame(_jsonrpc_result("r2", {"config": {}}))
    translator.on_frame({"no": "shape"})
    translator.on_frame("not a dict")
    assert _count(get_store("hscc")) == 0


def test_message_stream_produces_contract_frames(translator):
    """message.start + deltas -> message role=assistant deltas + done=True close."""
    translator.on_frame(_msg("message.start"))
    translator.on_frame(_msg("message.delta", {"text": "Hello"}))
    translator.on_frame(_msg("message.delta", {"text": " world"}))
    # A tool.start closes the assistant turn (the native feed has no message.end).
    translator.on_frame(_msg("tool.start", {
        "tool_id": "call_abc", "name": "read_file", "context": "some/file"}))
    store = get_store("hscc")
    evs = store.history()["events"]
    assert [e["type"] for e in evs] == ["message", "message", "message", "tool_call"]
    messages = [e["payload"] for e in evs if e["type"] == "message"]
    assert messages[0] == {"role": "assistant", "delta": "Hello", "done": False}
    assert messages[1] == {"role": "assistant", "delta": " world", "done": False}
    # The final close marker finalizes the streaming assistant row.
    assert messages[2] == {"role": "assistant", "delta": "", "done": True}
    tool = [e["payload"] for e in evs if e["type"] == "tool_call"][0]
    assert tool["status"] == "start"
    assert tool["call_id"] == "call_abc"
    assert tool["name"] == "read_file"


def test_tool_start_finish_pairing(translator):
    """Native tool.start + tool.complete -> one tool_call pair sharing call_id."""
    translator.on_frame(_msg("tool.start", {
        "tool_id": "call_xyz", "name": "terminal", "context": "echo hi"}))
    translator.on_frame(_msg("tool.complete", {
        "tool_id": "call_xyz", "name": "terminal",
        "args": {"command": "echo hi"},
        "result": {"output": "hi", "exit_code": 0},
        "duration_s": 0.5,
    }))
    store = get_store("hscc")
    tools = [e["payload"] for e in store.history()["events"] if e["type"] == "tool_call"]
    assert len(tools) == 2
    assert tools[0]["status"] == "start"
    assert tools[1]["status"] == "finish"
    assert tools[0]["call_id"] == tools[1]["call_id"] == "call_xyz"
    assert tools[1]["args"] == {"command": "echo hi"}
    assert tools[1]["result"] == {"output": "hi", "exit_code": 0}
    assert tools[1]["duration_s"] == 0.5


def test_error_frame_maps_to_error(translator):
    translator.on_frame(_msg("error", {"message": "provider down"}))
    evs = get_store("hscc").history()["events"]
    assert [e["type"] for e in evs] == ["error"]
    assert evs[0]["payload"]["code"] == "gateway_error"
    assert evs[0]["payload"]["message"] == "provider down"


def test_gateway_and_status_map_to_system(translator):
    translator.on_frame(_msg("gateway.ready", {"skin": {}}))
    translator.on_frame(_msg("status.update", {"kind": "lifecycle", "text": "x"}))
    evs = get_store("hscc").history()["events"]
    assert [e["payload"]["kind"] for e in evs] == ["gateway", "status"]
    assert all(e["type"] == "system" for e in evs)


def test_interim_and_thinking_are_not_translated(translator):
    """Interim snapshots + reasoning streams are not chat text — dropped."""
    translator.on_frame(_msg("message.start"))
    translator.on_frame(_msg("message.interim",
                             {"text": "snapshot", "already_streamed": True}))
    translator.on_frame(_msg("thinking.delta", {"text": "hmm"}))
    translator.on_frame(_msg("reasoning.available", {"id": "r"}))
    translator.on_frame(_msg("sessions.changed"))
    translator.on_frame(_msg("tool.generating", {"name": "read_file"}))
    translator.flush()
    store = get_store("hscc")
    # message.start itself carries no text (deltas do), all interim/thinking is
    # dropped, and flush() emits only the done=True close marker -> 1 frame.
    evs = store.history()["events"]
    assert [e["type"] for e in evs] == ["message"]
    assert evs[0]["payload"] == {"role": "assistant", "delta": "", "done": True}


def test_flush_closes_open_assistant(translator):
    """flush() emits the done=True marker for an open assistant row."""
    translator.on_frame(_msg("message.delta", {"text": "partial"}))
    translator.flush()
    evs = get_store("hscc").history()["events"]
    assert [e["payload"]["done"] for e in evs] == [False, True]
    assert evs[1]["payload"]["delta"] == ""


def test_real_corpus_end_to_end(tmp_path, clean_store):
    """Feed the FULL captured corpus (real isolated probe frames) and check the
    store produced the expected translated event mix. Pins driver behavior
    against genuine upstream traffic, not just hand-written frames."""
    import pathlib
    probe = pathlib.Path(__file__).resolve().parent.parent / "tests_gateway" / \
        "probe_03_tool_frames.jsonl"
    if not probe.exists():
        pytest.skip("captured corpus not present")
    translator = FrameTranslator(get_store("hscc"))
    n = 0
    for line in probe.open():
        translator.on_frame(json.loads(line))
        n += 1
    translator.flush()
    store = get_store("hscc")
    # 284 native event lines -> 253 store events (no eviction: cap 2000).
    assert n == 284
    assert store.history(limit=0)["next_seq"] - 1 == 253
    from collections import Counter
    # Pull ALL retained events (history defaults to a 200-page; bump the limit).
    all_events = store.history(limit=2000)["events"]
    types = Counter(e["type"] for e in all_events)
    assert types["message"] == 240
    assert types["tool_call"] == 11
    assert types["system"] == 2
    # Tool calls pair: 6 starts + 5 finishes (one start had no finish in corpus).
    statuses = Counter(e["payload"]["status"]
                       for e in all_events if e["type"] == "tool_call")
    assert statuses == {"start": 6, "finish": 5}


# --------------------------------------------------------------------------- #
# Client framing: masked encode + unmasked server decode (cross-check ws_frame)
# --------------------------------------------------------------------------- #

def test_client_frame_roundtrip_against_server_decoder():
    """A client-encoded (masked) frame decodes cleanly with the server-side
    FrameDecoder, and the payload is unmasked back to the original."""
    mask = b"\x01\x02\x03\x04"
    for text in ["", "hi", "x" * 200]:
        payload = text.encode("utf-8")
        framed = gd._encode_client_frame(ws_frame_server.OP_TEXT, payload, mask)
        decoder = ws_frame_server.FrameDecoder()
        frames = list(decoder.feed(framed))
        assert len(frames) == 1
        opcode, got, fin = frames[0]
        assert opcode == ws_frame_server.OP_TEXT
        assert fin is True
        assert got == payload


def test_client_reads_server_frame_via_local_sockets(socket_pair):
    """A real socketpair round-trip: server-side encode -> client-side decode."""
    a, b = socket_pair
    client = gd._WSClient("127.0.0.1", 0, "/api/events")
    client._sock = a  # inject the accepting side as the client's socket
    text = {"jsonrpc": "2.0", "method": "event", "params": {"type": "x"}}
    server_frame = ws_frame_server.encode_frame(
        ws_frame_server.OP_TEXT, json.dumps(text).encode())
    b.sendall(server_frame)
    opcode, payload = client.read_frame()
    assert opcode == ws_frame_server.OP_TEXT
    assert json.loads(payload) == text


def test_client_answers_ping_with_pong(socket_pair):
    a, b = socket_pair
    client = gd._WSClient("127.0.0.1", 0, "/api/events")
    client._sock = a
    b.sendall(ws_frame_server.encode_frame(ws_frame_server.OP_PING, b"hello-ping"))
    # Client auto-answers ping and moves on; send a text after it.
    b.sendall(ws_frame_server.encode_frame(
        ws_frame_server.OP_TEXT, json.dumps({"t": 1}).encode()))
    opcode, payload = client.read_frame()
    assert json.loads(payload) == {"t": 1}


# --------------------------------------------------------------------------- #
# GatewayDriver relay-hook integration with routes_ws
# --------------------------------------------------------------------------- #

def test_driver_installs_and_removes_relay_hook(monkeypatch):
    """start() wires routes_ws.relay_user_message; stop() restores the no-op."""
    cfg = GatewayConfig(host="127.0.0.1", port=1, token="t", project="hscc")
    drv = GatewayDriver(cfg)

    fake_pty = gd._WSClient("127.0.0.1", 0, "/api/pty")
    fake_events = gd._WSClient("127.0.0.1", 0, "/api/events")
    monkeypatch.setattr(drv, "_connect_upstreams",
                        lambda: (fake_pty, fake_events))
    # Never actually block on real sockets in the reader threads.
    monkeypatch.setattr(drv, "_run_events_loop", lambda: None)
    monkeypatch.setattr(drv, "_run_pty_loop", lambda: None)

    assert routes_ws.relay_user_message("hscc", "x") is False  # no-op before
    drv.start()
    try:
        assert routes_ws.relay_user_message is drv._relay_hook
        # The hook narrows to the driver's project; other projects fall through.
        # (Calling send_user_message would hit the fake unconnected socket, so
        # we assert the hook identity rather than a live call.)
    finally:
        drv.stop()
    assert routes_ws.relay_user_message("hscc", "x") is False  # restored


def test_send_user_message_types_into_pty(send_pty):
    """send_user_message types the text char-by-char and submits with CR.

    The pty is backed by a real socketpair; we decode the client's masked
    frames on the peer with the server-side decoder and assert the exact
    sequence the driver sends.
    """
    drv = send_pty.drv
    peer = send_pty.peer
    decoder = send_pty.decoder

    fragments = []

    def _reader():
        peer.settimeout(2.0)
        try:
            while True:
                data = peer.recv(65536)
                if not data:
                    return
                for opcode, payload, fin in decoder.feed(data):
                    if opcode == ws_frame_server.OP_TEXT:
                        fragments.append(payload.decode("utf-8"))
        except OSError:
            return

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    assert drv.send_user_message("hi") is True
    # Allow the per-char pacing to complete.
    t.join(timeout=3)
    text = "".join(fragments)
    assert text.endswith("\r")
    assert text[:-1] == "hi"


def test_send_user_message_rejects_empty(send_pty):
    """Blank input yields False without touching the socket."""
    assert send_pty.drv.send_user_message("   ") is False


def test_send_user_message_fails_when_not_connected():
    """Not started -> no pty socket -> relay fails, does not raise."""
    cfg = GatewayConfig(host="127.0.0.1", port=1, token="t", project="hscc")
    drv = GatewayDriver(cfg)
    assert drv.send_user_message("hello") is False
