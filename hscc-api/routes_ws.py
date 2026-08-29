"""HSCC API — live session WebSocket relay endpoint (t_47f51a71).

``GET /v1/projects/{name}/session/ws`` — RFC 6455 upgrade over which the iOS
app receives the project's live event stream: the SAME per-project seq space
as the history endpoint (routes_session.py), so reconnect is gap-free and
duplicate-free by appending to the shared store.

This is the relay half of the WebSocket-bridge card. It is server-side, pure
stdlib, and testable in isolation: it bridges an app client to the shared
in-memory ``SessionEventStore`` that the bridge relay appends to. Increment 3
(separate commit, this card) adds the "other half" — the driver that connects
out to ``hermes serve`` and translates its native event stream into store
frames. That driver is intentionally NOT coupled here: the WS endpoint only
talks to the store, so it can be driven hermetically in tests.

Wire contract (frames are session_event envelopes; all server→client text)::

    hello            {next_seq: N}   first frame; live frames begin at seq N
    message/tool_call/card/agent/system/error   the replayed + live events

Client→server (text JSON):
    {"kind": "send", "text": "..."}   a message to relay into the session.
    For increment 2 the operator's message is appended to the store as a
    ``message`` role="user" event (visible echo, same seq space); increment 3
    replaces the inline stub with the real ``hermes serve`` relay.

Auth: the upgrade carries the same ``Authorization: Bearer <token>`` header as
every other /v1 endpoint. ``ApiHandler._authorize`` runs BEFORE the upgrade in
_api_server._route, so an unauthenticated upgrade is rejected with a 401 JSON
response and is never upgraded (the handshake only happens after auth).

Framing: pure-stdlib RFC 6455 from ws_frame. The session loop is a single
writer thread that drains the live-event queue and polls the socket with
``select`` so inbound client frames and control frames (ping/pong) are serviced
even while the stream is busy.
"""

from __future__ import annotations

import json
import logging
import re
import select
import socket
import sys
import time
from pathlib import Path
from queue import Empty

from api_server import ApiError, register_ws_route  # noqa: E402
from session_event import (  # noqa: E402
    TYPE_HELLO,
    TYPE_MESSAGE,
    HelloPayload,
    MessagePayload,
    get_store,
)
import ws_frame  # noqa: E402

log = logging.getLogger("hscc-api.ws")

# Make the relocated flightdeck importable, exactly like routes_session/routes_project.
_PROJECT_DIR = Path(__file__).resolve().parent.parent / "hscc-project"
if _PROJECT_DIR.is_dir() and str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from flightdeck.core import registry as _registry  # noqa: E402
from routes_project import _registry_path  # noqa: E402

# Sentinel pushed onto a live queue when a connection should stop streaming
# (e.g. the peer closed and the WS loop must exit its queue.get).
_SENTINEL = object()

# Seconds the live loop blocks on the event queue per wake; bounds how long
# inbound/ping frames wait while the stream is otherwise quiet.
_POLL_INTERVAL = 0.5


# --------------------------------------------------------------------------- #
# Session framing over the raw socket
# --------------------------------------------------------------------------- #

def _send_text(sock: socket.socket, text: str) -> None:
    """Send one complete text frame (server→client, never masked)."""
    sock.sendall(ws_frame.encode_frame(ws_frame.OP_TEXT, text.encode("utf-8")))


def _send_hello(sock: socket.socket, boundary: int) -> None:
    """Send the opening hello envelope announcing where live frames begin."""
    pay = HelloPayload(next_seq=boundary).to_json()
    frame = {
        "seq": boundary, "type": TYPE_HELLO, "ts": _now_iso(), "payload": pay,
    }
    _send_text(sock, json.dumps(frame))


def _send_event(sock: socket.socket, ev) -> None:
    """Send a stored :class:`session_event.Event` as its wire envelope."""
    _send_text(sock, json.dumps(ev.to_json()))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# Inbound client messages
# --------------------------------------------------------------------------- #

def _handle_client_send(sock: socket.socket, project: str, payload: dict) -> None:
    """Relay an operator message from the app into the session transcript.

    Increment 2: append the message to the store as a ``message``
    role="user" event. The store fan-out then broadcasts it back to every live
    subscriber (including the sender), so the operator sees their own line in
    the transcript with the same seq semantics as everything else. Increment 3
    replaces this with the real relay into ``hermes serve``; the ack/echo shape
    is unchanged.
    """
    text = payload.get("text")
    if text is None or not isinstance(text, str) or not text.strip():
        _send_text(sock, json.dumps({
            "seq": 0, "type": "error", "ts": _now_iso(),
            "payload": {"code": "bad_send", "message": "send requires non-empty text"},
        }))
        return
    get_store(project).append(
        TYPE_MESSAGE, MessagePayload(role="user", delta=text.strip(), done=True))


def _process_inbound(sock: socket.socket, project: str, opcode, payload):
    """Handle one decoded client data frame (text only)."""
    if opcode == ws_frame.OP_TEXT:
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _send_text(sock, json.dumps({
                "seq": 0, "type": "error", "ts": _now_iso(),
                "payload": {"code": "bad_frame",
                            "message": "expected a JSON text frame"},
            }))
            return
        if isinstance(obj, dict) and obj.get("kind") == "send":
            _handle_client_send(sock, project, obj)
    # Binary frames are ignored (no application-defined binary protocol yet).


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #

def handle_session_ws(server, ctx, query, handler):
    """Upgrade and run the live session stream for a project.

    ``handler`` is the :class:`ApiHandler` that received the GET — it carries
    the request headers (Sec-WebSocket-Key for the handshake) and the raw
    socket we frame over. This blocks for the life of the connection; the WS
    route dispatch in api_server returns right after (no JSON response).
    """
    name = query.get("name")
    if name is None or not str(name).strip():
        raise ApiError(400, "bad_request", "missing project name")

    # Canonicalize the project (unknown -> 404) exactly like the history route.
    try:
        _registry.get_project(name, path=_registry_path(ctx))
    except _registry.ProjectNotFoundError:
        raise ApiError(
            404, "not_found", f"no project named {name!r}",
            f"Project {name} was not found.",
        )

    # Resume cursor: the seq the client has already rendered. Optional; when
    # absent, replay everything still retained in the store.
    after = 0
    raw_after = query.get("after")
    if raw_after is not None:
        try:
            after = int(raw_after)
        except (TypeError, ValueError):
            raise ApiError(400, "bad_request", "after must be an integer")
        if after < 0:
            raise ApiError(400, "bad_request", "after must be >= 0")

    sock = handler.connection
    sock.settimeout(5.0)  # guard: a silent hanging peer must not pin the thread

    try:
        _handshake(handler, sock)
    except (OSError, BrokenPipeError):
        return

    # Subscribe BEFORE reading the boundary so no event is dropped (gap-free).
    store = get_store(name)
    boundary, replay, live_q = store.snapshot_and_subscribe(after=after)

    try:
        # Ensure the connection is not keep-alive-reused after the protocol
        # switch; the framework must close after our session.
        handler.close_connection = True

        _send_hello(sock, boundary)
        for ev in replay:
            _send_event(sock, ev)

        _live_loop(sock, name, boundary, live_q)
    finally:
        store.unsubscribe(live_q.put)
        try:
            sock.close()
        except OSError:
            pass


def _handshake(handler, sock: socket.socket) -> None:
    """Perform the RFC 6455 server handshake (101) against the request."""
    key = handler.headers.get("Sec-WebSocket-Key", "")
    accept = ws_frame.handshake_accept(key)
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    sock.sendall(response.encode("ascii"))


def _live_loop(sock: socket.socket, project: str, boundary: int, live_q) -> None:
    """Stream live events; service inbound + control frames; until closed.

    Single writer: this thread is the only one sending on ``sock``. It drains
    the live-event queue (from the store fan-out) and wakes frequently enough
    to also read inbound client frames and answer pings via ``select``. Events
    with ``seq < boundary`` are skipped — the replay snapshot delivered up to
    ``boundary - 1``, so only events appended DURING the later replay go here
    to be deduped. Events with ``seq >= boundary`` are genuinely live and are
    streamed.
    """
    decoder = ws_frame.FrameDecoder()

    while True:
        # 1) Drain whatever is already queued (non-blocking) and send it.
        while True:
            try:
                ev = live_q.get_nowait()
            except Empty:
                break
            if ev is _SENTINEL:
                return
            if ev.seq < boundary:
                continue  # already in the replay snapshot (race dedupe)
            _send_event(sock, ev)

        # 2) Service inbound/control frames buffered on the socket.
        if not _drain_socket(sock, project, decoder):
            return  # peer closed / protocol error

        # 3) Block briefly for the next live event; on timeout, loop to poll.
        try:
            ev = live_q.get(timeout=_POLL_INTERVAL)
        except Empty:
            continue
        if ev is _SENTINEL:
            return
        if ev.seq < boundary:
            continue
        _send_event(sock, ev)


def _drain_socket(sock: socket.socket, project: str, decoder) -> bool:
    """Read any pending frames; return False when the peer hung up.

    Answers pings with pongs, forwards sends, and watches for close frames.
    Returns True to keep streaming, False when the connection is done.
    """
    try:
        r, _, _ = select.select([sock], [], [], 0.0)
    except (OSError, ValueError):
        return False
    if not r:
        return True  # nothing readable right now
    try:
        data = sock.recv(65536)
    except (OSError, socket.timeout):
        return False
    if not data:
        return False  # peer closed the TCP connection

    close_payload: list = []

    def _on_close(payload):
        close_payload.append(payload)

    for opcode, payload in ws_frame.iter_data_messages(
            decoder, data, on_close=_on_close):
        if opcode == ws_frame.OP_PING:
            try:
                sock.sendall(
                    ws_frame.encode_frame(ws_frame.OP_PONG, payload))
            except OSError:
                return False
        else:
            _process_inbound(sock, project, opcode, payload)

    if close_payload:
        # Peer asked to close: reply in kind so the close handshake completes
        # cleanly (echo the code), then drop the stream.
        try:
            code = ws_frame.parse_close_frame(close_payload[0])
            sock.sendall(ws_frame.make_close_frame(code))
        except OSError:
            pass
        return False
    return True


# Register the live stream route.
register_ws_route(
    r"^/v1/projects/(?P<name>[^/]+)/session/ws$",
    handle_session_ws,
)
