"""HSCC API — outbound gateway driver to ``hermes serve`` (t_776e294a).

This is the "other half" of the WebSocket bridge: while ``routes_ws.py``
relays an app client against the shared per-project store, this module
connects the project OUT to a running ``hermes serve`` gateway and translates
its native event stream into store frames, so the app's live chat shows the
real orchestrator session (model replies, tool calls, errors).

Two upstream connections are maintained (discovered + proven by the isolated
live-probe in ``tests_gateway/``):

* ``/api/pty``  — a terminal WebSocket that spawns/reuses the hermes TUI chat
  session. Outbound: this is where user messages are typed (char-by-char +
  ``\\r`` to submit — the native TUI only submits on CR, never LF). Inbound:
  raw ANSI terminal bytes (the rendering), which are NOT translated.
* ``/api/events`` — on the SAME ``?channel=``, receives the dispatcher's JSON
  event notifications verbatim (the tool-call + message feed). This is what
  this driver translates into ``session_event`` envelopes.

The whole thing is deliberately pure-stdlib. ``ws_frame.py`` is server-side
(reads masked client frames, encodes unmasked). A gateway driver needs the
CLIENT side (mask its outbound frames per RFC 6455 §5.1, decode UNMASKED
server frames), so the client framing lives here rather than in the tested
server module.

Auth: the upstream gateway is reached with ``?token=`` (the gateway's session
token) on both sockets; both must share ``?channel=<channel>`` or the fan-out
won't reach the events subscriber.

Isolation/safety (the hard constraint this task was given): the driver NEVER
probes or restarts the live operator gateway (port 9119) and never writes its
state. It connects to a gateway whose host/port/token are supplied explicitly
in :class:`GatewayConfig` and which the operator has opted to attach.

Threading model: ``GatewayDriver`` owns two reader threads (one per upstream
socket). Only the events-reader thread calls the translator and appends to the
store; the pty thread only drains ANSI bytes. ``send_user_message`` is called
from the WS endpoint thread and writes to the pty socket (guarded by a lock so
two operators can't interleave mid-message).

The translation core, :class:`FrameTranslator`, is pure/stateful and is unit
tested directly against the captured corpus shapes in
``tests/test_gateway_driver.py`` and the real ``tests_gateway/probe_03_tool_frames.jsonl``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Optional

# The repo's server-side framing (used for the shared opcode constants + the
# handshake accept computation; the client path re-implements masking/decode).
import ws_frame  # noqa: E402

from session_event import (  # noqa: E402
    TYPE_ERROR,
    TYPE_MESSAGE,
    TYPE_SYSTEM,
    TYPE_TOOL_CALL,
    ErrorPayload,
    MessagePayload,
    SystemPayload,
    ToolCallPayload,
    get_store,
)

log = logging.getLogger("hscc-api.gateway")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class GatewayConfig:
    """Connection config for one upstream ``hermes serve`` gateway.

    All fields are explicit — the driver never auto-discovers or connects to
    the live operator gateway on its own. ``channel`` defaults to a random
    value so each driver instance gets its own fan-out scope (multiple drivers
    can coexist without cross-talk).
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 9119,
        token: str = "",
        tls: bool = False,
        project: str = "hscc",
        channel: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.token = token
        self.tls = bool(tls)
        self.project = project
        self.channel = channel or secrets.token_urlsafe(16)

    @property
    def base_url(self) -> str:
        scheme = "wss" if self.tls else "ws"
        return f"{scheme}://{self.host}:{self.port}"

    def ws_path(self, path: str) -> str:
        """Relative WS request-target (path + query) for the given api path."""
        return f"{path}?token={self.token}&channel={self.channel}"

    def ws_url(self, path: str) -> str:
        """Absolute ws(s) URL (scheme + host + path + query)."""
        return f"{self.base_url}{self.ws_path(path)}"

    def to_dict(self) -> dict:
        """Non-secret view (token redacted) for logging / admin surface."""
        return {
            "host": self.host,
            "port": self.port,
            "tls": self.tls,
            "project": self.project,
            # channel is NOT a secret; token is — keep channel for duo-debug.
            "channel": self.channel,
        }


# --------------------------------------------------------------------------- #
# Minimal RFC 6455 CLIENT framing (pure stdlib, standalone).
# ws_frame is server-side; the client must mask outbound + decode unmasked.
# --------------------------------------------------------------------------- #

def _client_handshake(path: str, host_header: str) -> tuple[str, bytes]:
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    return key, request.encode("ascii")


def _mask(payload: bytes, mask_key: bytes) -> bytes:
    """Mask client→server payload (RFC 6455 §5.3)."""
    return bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))


def _encode_client_frame(opcode: int, payload: bytes, mask_key: bytes) -> bytes:
    """Encode one MASKED client→server frame."""
    if len(payload) > ws_frame.MAX_PAYLOAD:
        raise ws_frame.WSProtocolError(f"payload too large: {len(payload)}")
    b0 = 0x80 | (opcode & 0x0F)  # fin=True, rsv=0
    n = len(payload)
    if n < ws_frame.LEN_16BIT:
        header = bytes([b0, 0x80 | n])
    elif n <= 0xFFFF:
        header = bytes([b0, 0x80 | ws_frame.LEN_16BIT]) + struct.pack("!H", n)
    else:
        header = bytes([b0, 0x80 | ws_frame.LEN_64BIT]) + struct.pack("!Q", n)
    header += mask_key
    return header + _mask(payload, mask_key)


def _read_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes or raise ConnectionError on EOF/error."""
    chunks = bytearray()
    while len(chunks) < n:
        try:
            data = sock.recv(n - len(chunks))
        except socket.timeout:
            # The socket's individual-read timeout expired — NOT a disconnect.
            # Recursor loops catch this and keep waiting.
            if chunks:
                raise ConnectionError("upstream closed mid-frame (timeout)")
            raise
        if not data:
            raise ConnectionError("upstream closed during read")
        chunks.extend(data)
    return bytes(chunks)


class _WSClient:
    """A single outbound WebSocket connection (handshake + masked send +
    unmasked receive). Blocking, thread-based, pure stdlib."""

    def __init__(self, host: str, port: int, path: str, *,
                 tls: bool = False, connect_timeout: float = 8.0):
        self._host = host
        self._port = port
        self._path = path
        self._tls = tls
        self._connect_timeout = connect_timeout
        self._sock: Optional[socket.socket] = None
        self._mask_key = secrets.token_bytes(4)
        self._write_lock = threading.Lock()

    # -- connect ----------------------------------------------------------- #

    def connect(self) -> None:
        """Open TCP, perform the TLS upgrade if requested, and the WS handshake."""
        if self._tls:
            import ssl
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # loopback/tailnet; no public PKI
            sock = socket.create_connection(
                (self._host, self._port), timeout=self._connect_timeout)
            self._sock = context.wrap_socket(sock, server_hostname=self._host)
        else:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=self._connect_timeout)
        self._sock.settimeout(30.0)
        self._do_handshake()

    def _do_handshake(self) -> None:
        sock = self._sock
        assert sock is not None
        key, request = _client_handshake(self._path, f"{self._host}:{self._port}")
        sock.sendall(request)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("upstream closed during handshake")
            head += chunk
        headers, _, rest = head.partition(b"\r\n\r\n")
        status_line = headers.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101" not in status_line:
            # Surface the body snippet (auth failures carry a JSON error).
            raise ConnectionError(
                f"upstream rejected WebSocket upgrade: {status_line} | {rest[:200]!r}")
        expected = ws_frame.handshake_accept(key)
        for line in headers.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-accept:"):
                got = line.split(b":", 1)[1].strip().decode("ascii", "replace")
                if not _const_eq(got, expected):
                    raise ConnectionError("upstream Sec-WebSocket-Accept mismatch")
                return
        raise ConnectionError("no Sec-WebSocket-Accept in upgrade response")

    # -- send -------------------------------------------------------------- #

    def send_text(self, text: str) -> None:
        """Send a text frame (masked). Raises ConnectionError when not connected."""
        sock = self._sock
        if sock is None:
            raise ConnectionError("gateway not connected")
        frame = _encode_client_frame(
            ws_frame.OP_TEXT, text.encode("utf-8"), self._mask_key)
        with self._write_lock:
            try:
                sock.sendall(frame)
            except OSError as exc:
                raise ConnectionError(f"gateway send failed: {exc}") from exc

    # -- receive ----------------------------------------------------------- #

    def read_frame(self) -> tuple[int, bytes]:
        """Read one complete data frame from the server (unmasked).

        Returns ``(opcode, payload)`` for TEXT/BINARY data. Control frames
        (ping/pong) are answered transparently; a close frame raises
        ConnectionError. Raises ConnectionError on EOF/error.
        """
        sock = self._sock
        if sock is None:
            raise ConnectionError("gateway not connected")
        while True:
            opcode, payload = self._read_one_data_frame(sock)
            if opcode == ws_frame.OP_PING:
                self.send_bytes_raw(ws_frame.encode_frame(ws_frame.OP_PONG, payload))
                continue
            if opcode == ws_frame.OP_PONG:
                continue
            if opcode == ws_frame.OP_CLOSE:
                raise ConnectionError("upstream sent close frame")
            return opcode, payload

    def _read_one_data_frame(self, sock) -> tuple[int, bytes]:
        b0 = _read_exact(sock, 1)[0]
        b1 = _read_exact(sock, 1)[0]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        if b1 & 0x80:
            raise ws_frame.WSProtocolError("server frame must not be masked")
        length = b1 & 0x7F
        if length == ws_frame.LEN_16BIT:
            length = struct.unpack("!H", _read_exact(sock, 2))[0]
        elif length == ws_frame.LEN_64BIT:
            length = struct.unpack("!Q", _read_exact(sock, 8))[0]
        if length > ws_frame.MAX_PAYLOAD:
            raise ws_frame.WSProtocolError(f"frame payload {length} exceeds max")
        payload = _read_exact(sock, length) if length else b""
        if fin:
            return opcode, payload
        # Fragmented: reassemble continuation frames.
        chunks = [payload]
        while not fin:
            b0 = _read_exact(sock, 1)[0]
            b1 = _read_exact(sock, 1)[0]
            fin = bool(b0 & 0x80)
            cop = b0 & 0x0F
            if cop != ws_frame.OP_CONTINUATION:
                raise ws_frame.WSProtocolError("expected continuation frame")
            clen = b1 & 0x7F
            if clen == ws_frame.LEN_16BIT:
                clen = struct.unpack("!H", _read_exact(sock, 2))[0]
            elif clen == ws_frame.LEN_64BIT:
                clen = struct.unpack("!Q", _read_exact(sock, 8))[0]
            chunks.append(_read_exact(sock, clen) if clen else b"")
        return opcode, b"".join(chunks)

    def send_bytes_raw(self, data: bytes) -> None:
        with self._write_lock:
            try:
                self._sock.sendall(data)
            except OSError as exc:
                raise ConnectionError(f"gateway send failed: {exc}") from exc

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _const_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    return sum(x != y for x, y in zip(a.encode(), b.encode())) == 0


# --------------------------------------------------------------------------- #
# Translation core: native `/api/events` frame -> session_event store appends.
# Pure + stateful; unit-tested directly against the captured corpus.
# --------------------------------------------------------------------------- #

class FrameTranslator:
    """Translate a stream of native ``hermes serve`` event frames into
    ``session_event`` store appends.

    The native feed (via ``/api/events``) is the dispatcher's writes verbatim:
    JSON-RPC responses and event notifications:
    ``{"jsonrpc":"2.0","method":"event","params":{"type":...,"session_id":...,"payload":{...}}}``

    Only ``method == "event"`` notifications are translated; JSON-RPC responses
    (with ``id``/``result``) are ignored. See ``tests_gateway/FINDINGS_gateway_protocol.md``
    for the captured schema.

    Assistant-message buffering: the native feed has no "message.end" marker,
    so the assistant turn is closed (``done=True``) when (a) a ``tool.start``
    arrives (the model paused text generation), (b) a new ``message.start``
    arrives, or (c) :meth:`flush` is called on stop. Each ``message.delta`` is
    streamed as its own ``message role=assistant read, done=False`` frame, so
    the app renders a streaming row.
    """

    def __init__(self, store):
        self._store = store
        self._in_assistant = False      # an assistant message turn is open
        self._msg_buf: list[str] = []   # pending assistant text (unused with streaming)

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _pv(native: dict) -> dict:
        """Return the ``params`` dict of a native event notification."""
        return native.get("params") or {}

    def _append(self, type_: str, payload: Any) -> int:
        return self._store.append(type_, payload)

    # -- public entry ------------------------------------------------------- #

    def on_frame(self, native: Any) -> None:
        """Process one native frame (a parsed JSON dict)."""
        if not isinstance(native, dict):
            return
        if native.get("method") != "event":
            return  # JSON-RPC response, not an event notification
        params = native.get("params")
        if not isinstance(params, dict):
            return
        etype = params.get("type")
        payload = params.get("payload") or {}
        handler = _DISPATCH.get(etype)
        if handler is None:
            log.debug("gateway: ignoring native event type %r", etype)
            return
        try:
            handler(self, payload)
        except Exception as exc:  # a malformed native frame must not kill the thread
            log.warning("gateway: translation error for %s: %s", etype, exc)

    # -- translation rules -------------------------------------------------- #

    def on_message_start(self, payload: dict) -> None:
        """A new assistant turn began — finalize any open buffer first."""
        self._close_assistant()
        self._in_assistant = True

    def on_message_delta(self, payload: dict) -> None:
        if not self._in_assistant:
            self._in_assistant = True
        text = payload.get("text") or ""
        self._append(
            TYPE_MESSAGE,
            MessagePayload(role="assistant", delta=text, done=False),
        )

    def on_message_interim(self, payload: dict) -> None:
        # Native interim full-text snapshot; the individual deltas are the
        # authoritative stream. Ignored (no translation).
        return

    def on_tool_start(self, payload: dict) -> None:
        self._close_assistant()
        self._append(
            TYPE_TOOL_CALL,
            ToolCallPayload(
                call_id=str(payload.get("tool_id") or ""),
                name=str(payload.get("name") or "tool"),
                status="start",
                args=_args_for_start(payload),
            ),
        )

    def on_tool_complete(self, payload: dict) -> None:
        self._append(
            TYPE_TOOL_CALL,
            ToolCallPayload(
                call_id=str(payload.get("tool_id") or ""),
                name=str(payload.get("name") or "tool"),
                status="finish",
                args=_as_dict(payload.get("args")),
                result=payload.get("result"),
                duration_s=_as_float(payload.get("duration_s")),
            ),
        )

    def on_error(self, payload: dict) -> None:
        self._close_assistant()
        self._append(
            TYPE_ERROR,
            ErrorPayload(
                code="gateway_error",
                message=str(payload.get("message") or "gateway error"),
            ),
        )

    def on_system(self, payload: dict, kind: str) -> None:
        self._append(
            TYPE_SYSTEM,
            SystemPayload(kind=kind, details=_as_dict(payload)),
        )

    # -- assistant turn lifecycle ------------------------------------------- #

    def _close_assistant(self) -> None:
        """Emit the ``done=True`` frame that finalizes the open assistant row.

        Only emits when an assistant message turn is open (a row is streaming).
        The trailing frame carries an empty delta: it is the finalization marker,
        not text — the iOS row decoder finalizes the streaming row without
        adding content.
        """
        if self._in_assistant:
            self._in_assistant = False
            self._append(
                TYPE_MESSAGE,
                MessagePayload(role="assistant", delta="", done=True),
            )

    def flush(self) -> None:
        """Close any in-flight assistant message (call on driver stop/teardown)."""
        self._close_assistant()


# The dispatch table for native event type -> translation method name.
_DISPATCH = {
    "message.start": FrameTranslator.on_message_start,
    "message.delta": FrameTranslator.on_message_delta,
    "message.interim": FrameTranslator.on_message_interim,
    "tool.start": FrameTranslator.on_tool_start,
    "tool.complete": FrameTranslator.on_tool_complete,
    # tool.generating is a pre-call hint; tool.start/complete carry the payload.
    "error": FrameTranslator.on_error,
    "status.update": lambda self, p: self.on_system(p, "status"),
    "gateway.ready": lambda self, p: self.on_system(p, "gateway"),
}


def _args_for_start(payload: dict) -> dict:
    """Best-effort args from a tool.start frame.

    The native ``tool.start`` carries only ``context`` (a short preview), not
    the full ``args`` (those arrive on ``tool.complete``). We surface context
    as ``args`` so the "start" chip has something to show; ``tool.complete``
    overrides with the real args.
    """
    ctx = payload.get("context")
    if isinstance(ctx, dict):
        return dict(ctx)
    if ctx is not None:
        return {"context": ctx}
    return {}


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# The driver: an in-process background coordinator owning the two upstream
# WebSocket connections and their reader threads.
# --------------------------------------------------------------------------- #

class GatewayDriver:
    """Owns the /api/pty + /api/events connections to one ``hermes serve``
    gateway and fans translated frames into a project store.

    Start it with :meth:`start`; it opens both upstream sockets and spawns a
    reader thread per socket. Operators relay messages via
    :meth:`send_user_message`. Shut down with :meth:`stop`.
    """

    _PTY_PATH = "/api/pty"
    _EVENTS_PATH = "/api/events"

    def __init__(self, config: GatewayConfig):
        self.config = config
        self._translator = FrameTranslator(get_store(config.project))
        self._pty: Optional[_WSClient] = None
        self._events: Optional[_WSClient] = None
        self._threads: list[threading.Thread] = []
        self._send_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._alive = False
        self._relay_hook = None

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        """Connect both upstream sockets and start the reader threads.

        Raises ConnectionError if either upstream connection cannot be
        established (e.g. the gateway is down or the token is rejected).
        """
        if self._alive:
            return
        pty, events = self._connect_upstreams()
        self._pty = pty
        self._events = events
        self._stop_evt.clear()
        self._alive = True

        # Install the WS-relay hook so operator sends reach this driver. The
        # hook narrows to this driver's project; other projects (with their own
        # driver) would install their own. Imported lazily to keep the two
        # modules from forming a hard import cycle at module load.
        import routes_ws as _routes_ws

        project = self.config.project

        def _relay(proj: str, text: str) -> bool:
            if proj != project:
                return False
            return self.send_user_message(text)

        self._relay_hook = _relay
        _routes_ws.relay_user_message = _relay

        ev_thread = threading.Thread(
            target=self._run_events_loop, name="gateway-events",
            daemon=True)
        pty_thread = threading.Thread(
            target=self._run_pty_loop, name="gateway-pty",
            daemon=True)
        self._threads = [ev_thread, pty_thread]
        ev_thread.start()
        pty_thread.start()

    def _connect_upstreams(self) -> tuple:
        """Open the /api/pty and /api/events connections (no-op gateway probe).

        Separated from :meth:`start` so tests can inject fake transport
        clients. Returns ``(pty, events)`` both connected. On failure the
        partially-opened pty is closed and :class:`ConnectionError` raised.
        """
        pty = _WSClient(self.config.host, self.config.port,
                        self.config.ws_path(self._PTY_PATH))
        events = _WSClient(self.config.host, self.config.port,
                           self.config.ws_path(self._EVENTS_PATH))
        pty.connect()
        try:
            events.connect()
        except ConnectionError:
            pty.close()
            raise
        return pty, events

    def _run_events_loop(self) -> None:
        """Read /api/events and translate each notification into the store.

        ``socket.timeout`` (an idle gap with no upstream frames) is treated as
        a keep-alive opportunity, not a disconnect — the loop keeps waiting so
        a quiet gateway doesn't kill the feed.
        """
        try:
            events = self._events
            while not self._stop_evt.is_set() and events is not None:
                try:
                    opcode, payload = events.read_frame()
                except socket.timeout:
                    continue
                if opcode != ws_frame.OP_TEXT:
                    continue
                try:
                    frame = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self._translator.on_frame(frame)
        except ConnectionError as exc:
            log.warning("gateway: events feed disconnected: %s", exc)
        finally:
            self._translator.flush()

    def _run_pty_loop(self) -> None:
        """Drain /api/pty bytes (ANSI terminal rendering) — not translated.

        The PTY socket also carries the user's own keystrokes if the operator
        were typing directly; the driver does not attempt to parse the raw
        terminal bytes for events (they are a rendering, not a data feed).
        """
        try:
            pty = self._pty
            while not self._stop_evt.is_set() and pty is not None:
                try:
                    opcode, payload = pty.read_frame()
                except socket.timeout:
                    continue
                del opcode, payload  # ANSI bytes ignored for event translation
        except ConnectionError as exc:
            log.debug("gateway: pty feed closed: %s", exc)

    # -- operator relay ----------------------------------------------------- #

    def send_user_message(self, text: str) -> bool:
        """Relay an operator message into the gateway's chat session.

        Returns True when the relay succeeded (the message was written to the
        PTY socket). The user echo is appended by the caller
        (``routes_ws._handle_client_send``); the forwarding here goes to hermes.

        Matching the proven native behavior, the message is typed char-by-char
        and submitted with ``\\r`` (CR) — LF never submits in the TUI.
        """
        text = (text or "").strip()
        if not text:
            return False
        pty = self._pty
        if pty is None or self._stop_evt.is_set():
            return False
        try:
            with self._send_lock:
                for ch in text:
                    pty.send_text(ch)
                    time.sleep(0.01)
                time.sleep(0.2)
                pty.send_text("\r")
            return True
        except ConnectionError as exc:
            log.warning("gateway: send_user_message failed: %s", exc)
            return False

    # -- teardown ----------------------------------------------------------- #

    def stop(self) -> None:
        """Close both upstream sockets and stop the reader threads."""
        if not self._alive:
            return
        self._alive = False
        self._stop_evt.set()
        # Restore the default (no-op) WS relay hook for our project.
        if self._relay_hook is not None:
            import routes_ws as _routes_ws
            if _routes_ws.relay_user_message is self._relay_hook:
                _routes_ws.relay_user_message = _routes_ws._default_relay
            self._relay_hook = None
        for ws in (self._pty, self._events):
            if ws is not None:
                ws.close()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads = []
        self._pty = None
        self._events = None
