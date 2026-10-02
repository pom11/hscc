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
from typing import Any, Callable, Optional

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


def _is_tui_turn_lease(holder: str) -> bool:
    """True when a ``session_turn_leases`` holder is a TUI-platform lease.

    The serve's durable turn-lease holder is
    ``pid=<pid>:turn=<turn id>:platform=<platform>``
    (hermes ``agent/turn_facade_lease.py``). We only ever release a lease the
    serve holds on OUR pinned session — the TUI surface this driver owns — so
    we must not release a lease from any other surface. Extract the last
    ``:platform=`` value and match the ``tui`` prefix (defensive against a
    future ``platform=tui:<sub>``; never matches telegram / desktop / cli, etc.).
    """
    marker = ":platform="
    idx = holder.rfind(marker)
    if idx < 0:
        return False
    return holder[idx + len(marker):].startswith("tui")


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
        session_id: Optional[str] = None,
        profile: Optional[str] = None,
        registry_path: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.token = token
        self.tls = bool(tls)
        self.project = project
        self.channel = channel or secrets.token_urlsafe(16)
        # Registry path the driver resolves the named session through
        # (§3.1/§3.2). Optional; deployment supplies it so resolution targets
        # the project registry, exactly like routes_session/routes_ws do.
        self.registry_path = registry_path
        # §3.1 same-session pinning: the project's NAMED Hermes session id and
        # its owning profile. Optional so a config can be built without them
        # (the driver resolves them on start when absent); when present they
        # pin the /api/pty to that exact named session so an app message lands
        # in the SAME conversation the CLI continues (§3.3).
        self.session_id = session_id
        self.profile = profile

    @property
    def base_url(self) -> str:
        scheme = "wss" if self.tls else "ws"
        return f"{scheme}://{self.host}:{self.port}"

    def ws_path(self, path: str) -> str:
        """Relative WS request-target (path + query) for the given api path."""
        return f"{path}?token={self.token}&channel={self.channel}"

    def pty_path(self, session_id: Optional[str] = None,
                 profile: Optional[str] = None) -> str:
        """WS request-target for /api/pty, pinned to the named session (§3.3).

        The live PTY spawns a fresh chat unless it is told which session to
        resume (``?resume=<session_id>`` -> ``HERMES_TUI_RESUME``) and which
        profile to scope under (``?profile=<profile>`` -> ``HERMES_HOME``) —
        see ``hermes_cli.web_server_chat._resolve_chat_argv``. Passing our
        resolved project identity here is what makes an APP message reach the
        SAME named session the CLI continues, instead of a throwaway fresh
        session. Falls back to just token+channel when neither is known.
        """
        override_sid = session_id or self.session_id
        override_prof = profile or self.profile
        path = self.ws_path("/api/pty")
        parts = []
        if override_prof:
            from urllib.parse import quote
            parts.append(f"profile={quote(override_prof)}")
        if override_sid:
            from urllib.parse import quote
            parts.append(f"resume={quote(override_sid)}")
        if parts:
            path += "&" + "&".join(parts)
        return path

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
        # t_93f1ba4d: optional callback fired when an assistant turn is closed
        # (the messages of that serve-driven turn are now persisted in
        # state.db). The driver uses it to advance the shared store-tail
        # watermark so the disk poller does not re-translate those messages as
        # duplicates. Only used with live translation, never in backfill.
        self.on_turn_closed: Optional[Callable[[], None]] = None

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
            # t_93f1ba4d: a serve-driven turn's messages are now persisted in
            # state.db; tell the driver to claim them in the store-tail
            # watermark so the disk poller does not translate them twice.
            if self.on_turn_closed is not None:
                try:
                    self.on_turn_closed()
                except Exception:  # noqa: BLE001 — a sync failure must not
                    pass            # break the translation/append path

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
# Full-history backfill (design §3.2): seed the store from the named session's
# REAL history in state.db, so the iOS app shows the whole conversation up
# front instead of only events accumulated since the bridge connected.
#
# The store is keyed by project name (the same key routes_session's history
# endpoint and routes_ws's relay use via ``ensure_session``), and the message
# source is the ACTUAL named Hermes session resolved through the same-session
# pinning primitive (routes_orchestrator.resolve_named_session_id, §3.1):
# ``<project>-orch`` profile's state.db, session titled ``<project>``.
# --------------------------------------------------------------------------- #

# Backfill high-water mark, keyed by project. Backfill is idempotent on
# (session id + store high-water mark) — the design's literal key — so a
# reconnect that has NOT advanced the store does not re-append the same
# history and seq stays contiguous. On a genuinely new session id (rotation)
# or an advanced store, backfill runs again.
_BACKFILL_HW: dict[str, dict] = {}
_BACKFILL_LOCK = threading.Lock()


def reset_backfill_state() -> None:
    """Drop the backfill high-water marks (test isolation only)."""
    global _BACKFILL_HW
    with _BACKFILL_LOCK:
        _BACKFILL_HW = {}


# --------------------------------------------------------------------------- #
# Live store-tail watermark (t_93f1ba4d).
#
# The live feed's OTHER half: while ``/api/events`` fans out ONLY the serve's
# own pty activity (chat_ws.py:622-654 — a pure /api/pub fan-out), the operator
# drives the same named session in a SEPARATE CLI REPL whose frames go straight
# to the shared state.db and NEVER reach the serve fan-out. The per-project
# store therefore freezes at the mount-time backfill watermark.
#
# To make the app reflect CLI-driven frames 1:1 (the operator's hard
# requirement), the driver ALSO runs a store-tail poller that reads the
# session's ON-DISK messages table for NEW rows (id > watermark) and translates
# them exactly like the backfill/FrameTranslator does. The watermark below is
# the merge point shared by BOTH the disk poller and the native events loop so
# they never double-translate the same underlying message:
#
#   * the store-tail poller advances it as it translates disk rows;
#   * the events loop, after finalizing a NATIVE serve-driven turn (whose
#     messages were written to state.db by the serve), advances it to the
#     session's current max message id — "claiming" those messages so the disk
#     poller does not re-translate them as duplicates.
#
# Keyed by project (the same key the store uses), so each project's tail is
# independent. The value is the highest state.db message ``id`` already
# translated into that project's store.
# --------------------------------------------------------------------------- #
_TAIL_HW: dict[str, int] = {}
_TAIL_LOCK = threading.Lock()

# Poll cadence for the CLI-driven store-tail poller (t_93f1ba4d). 2s is
# snappy enough for the app to feel live while cheap (a COUNT-based read every
# 2s per mounted project is trivial; the event store's own high-water gate the
# acceptance check relies on). Tunable for tests.
_TAIL_POLL_INTERVAL_S = 2.0


def reset_tail_state() -> None:
    """Drop the store-tail watermarks (test isolation only)."""
    global _TAIL_HW
    with _TAIL_LOCK:
        _TAIL_HW = {}


def _tail_watermark(project: str) -> int:
    """The highest on-disk message id already translated for ``project``."""
    with _TAIL_LOCK:
        return _TAIL_HW.get(project, 0)


def _mark_tail_seen(project: str, msg_id: int) -> None:
    """Advance ``project``'s tail watermark to at least ``msg_id`` (monotonic)."""
    with _TAIL_LOCK:
        cur = _TAIL_HW.get(project, 0)
        if msg_id > cur:
            _TAIL_HW[project] = msg_id


def _content_to_text(content: Any) -> str:
    """Best-effort text of a stored message ``content`` column.

    Most content is a plain str; a multimodal user message may be a list of
    {type,text} blocks (e.g. an image + caption). Flatten the text blocks for
    the store frame; anything non-textual degrades to a bounded repr.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "\n".join(parts)
        return "[image/content]"
    if content is None:
        return ""
    return str(content)


def _epoch_to_iso(ts: Any) -> Optional[str]:
    """Best-effort epoch-seconds -> ISO-8601 UTC, or None when invalid.

    ``state.db`` messages carry a numeric ``timestamp`` (epoch seconds);
    the store's ``ts`` is ISO-8601. History frames should preserve the original
    message time so iOS renders real times, but a malformed timestamp
    degrades to None -> the store stamps wall clock (still valid).
    """
    try:
        f = float(ts)
    except (TypeError, ValueError):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(f))


def _tool_call_from_history(msg: dict) -> ToolCallPayload:
    """Translate a ``role==\"tool\"`` message row into a tool_call frame.

    The named session's stored tool message carries the completed tool's
    identity (``tool_call_id``, ``tool_name``, ``content`` = its result). We
    emit a single ``status=\"finish\"`` frame (we only have the completed
    outcome, not the pre-call start). Best-effort: any missing field degrades
    to an empty string so the frame shape stays decodable — never crashes a
    backfill over one malformed row.
    """
    call_id = str(msg.get("tool_call_id") or "")
    name = str(msg.get("tool_name") or "tool")
    args = {}
    # The stored tool_calls column often carries the call's input arguments
    # ({name, arguments/input, id, ...}). Surface the first call's input as
    # the args preview when available.
    tc = msg.get("tool_calls")
    if isinstance(tc, list) and tc and isinstance(tc[0], dict):
        inp = tc[0].get("input", tc[0].get("arguments"))
        if isinstance(inp, dict):
            args = inp
        elif isinstance(inp, str) and inp:
            from json import loads
            try:
                parsed = loads(inp)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                args = parsed
    return ToolCallPayload(
        call_id=call_id,
        name=name,
        status="finish",
        args=args,
        result=_content_to_text(msg.get("content")),
    )


def _translate_history_to_store(store, messages: list) -> int:
    """Translate a session's stored messages into store frames; returns count.

    One ``role=\"user\"`` / ``role=\"assistant\"`` message -> a single
    ``message`` frame with the full text and ``done=True`` (the terminal
    frame the live feed would have emitted — a complete turn is a finalised
    row). A ``role=\"tool\"`` message -> one ``tool_call status=\"finish\"``
    frame; ``role=\"system\"`` -> one ``system`` frame. Each frame keeps the
    original message timestamp so the iOS transcript shows real times, and
    seq is assigned sequentially by the store, so backfilled history and the
    live feed share ONE contiguous seq space (the reconnect contract).
    """
    count = 0
    for m in messages:
        if not isinstance(m, dict) or not m.get("role"):
            continue   # a malformed row should never abort the backfill
        ts = _epoch_to_iso(m.get("timestamp"))
        role = m.get("role")
        if role in ("user", "assistant"):
            store.append(TYPE_MESSAGE, MessagePayload(
                role=role, delta=_content_to_text(m.get("content")), done=True),
                ts=ts)
            count += 1
        elif role == "tool":
            store.append(TYPE_TOOL_CALL, _tool_call_from_history(m), ts=ts)
            count += 1
        elif role == "system":
            store.append(TYPE_SYSTEM, SystemPayload(
                kind="session_history",
                details={"text": _content_to_text(m.get("content"))}),
                ts=ts)
            count += 1
    return count


def backfill_named_session(project: str, registry_path: Optional[str] = None) -> dict:
    """Backfill a project's SessionEventStore from its named session's history.

    The §3.2 primitive: resolve the project's named Hermes session id via the
    same-session pinning resolver, read its FULL message history from the
    owning `<project>-orch` profile's state.db, and translate it into store
    frames (translated like the live feed, so history and live share one
    contract + one seq space).

    IDEMPOTENT (design §3.2): keyed on (session id + store high-water mark).
    When the project has already been backfilled to the CURRENT store
    high-water for the SAME session id, this is a no-op — no duplicate frames,
    seq stays contiguous. A reconnect that hasn't advanced the store therefore
    does not duplicate; a rotation to a new session id or an advanced store
    runs again.

    FAIL-SAFE (design §4): any failure here is reported, never raised — the
    caller (GatewayDriver.start) must "log + continue live, don't drop the
    live stream". The return dict ``skipped`` names each early-return reason
    so the caller can log it honestly.

    Returns ``{"project", "backfilled", "session"|None, "skipped"|None}``.
    """
    from routes_orchestrator import (
        _open_profile_session_db,
        resolve_named_session_id,
    )

    store = get_store(project)
    try:
        profile, _title, session_id = resolve_named_session_id(
            project, registry_path=registry_path)
    except Exception as exc:  # noqa: BLE001 — fail-safe, never raise
        log.warning("gateway: backfill %s failed to resolve session: %r",
                    project, exc)
        return {"project": project, "backfilled": 0, "session": None,
                "skipped": "resolve_failed"}
    if session_id is None:
        # The project has not started a named session yet — nothing to backfill.
        return {"project": project, "backfilled": 0, "session": None,
                "skipped": "no_session"}

    # Idempotency check: same session id + store high-water unchanged => the
    # history is already seeded to the current head; a reconnect must not dup.
    with _BACKFILL_LOCK:
        rec = _BACKFILL_HW.get(project)
    if rec is not None and rec.get("session_id") == session_id \
            and rec.get("store_next_seq") == store.next_seq:
        return {"project": project, "backfilled": 0, "session": session_id,
                "skipped": "already_backfilled"}

    db = _open_profile_session_db(profile, read_only=True)
    if db is None:
        # Unreachable opener: the resolver already reported no-session, so we
        # simply skip — nothing to backfill (the honest no-session result).
        log.warning("gateway: backfill %s: state.db for %s unreachable",
                    project, profile)
        return {"project": project, "backfilled": 0, "session": session_id,
                "skipped": "no_session"}
    try:
        messages = db.get_messages(session_id)
        count = _translate_history_to_store(store, messages)
        with _BACKFILL_LOCK:
            _BACKFILL_HW[project] = {
                "session_id": session_id,
                "store_next_seq": store.next_seq,
            }
        # Seed the store-tail watermark to the last message id backfilled, so
        # the live tail poller (t_93f1ba4d) only ever picks up messages AFTER
        # this history — it must not re-translate the whole transcript it just
        # seeded (that would duplicate every frame).
        _mark_tail_seen(project, _max_message_id(messages))
        return {"project": project, "backfilled": count, "session": session_id}
    except Exception as exc:  # noqa: BLE001 — fail-safe: never break connect
        log.warning("gateway: backfill %s failed (live continues): %r",
                    project, exc)
        return {"project": project, "backfilled": 0, "session": session_id,
                "skipped": "backfill_failed"}
    finally:
        try:
            db.close()
        except Exception:
            pass


def _max_message_id(messages: list) -> int:
    """Highest ``id`` among a list of session message dicts (0 when none)."""
    highest = 0
    for m in messages:
        if isinstance(m, dict):
            try:
                mid = int(m.get("id") or 0)
            except (TypeError, ValueError):
                mid = 0
            if mid > highest:
                highest = mid
    return highest


def tail_named_session(project: str, registry_path: Optional[str] = None,
                       after_id: Optional[int] = None) -> dict:
    """Poll the project's named session on-disk frames and translate NEW ones.

    THE §3.3 live source for externally-written (CLI-driven) frames. While the
    driver's ``/api/events`` feed captures only the serve's OWN pty activity,
    the operator's CLI REPL writes its frames directly to the shared state.db —
    the same source the backfill reads. This function tails that table: it
    resolves the project's named session (same-session pinning, §3.1), reads
    ONLY the messages with ``id > <watermark>`` (the shared watermark that
    backfill seeds and the native events loop advances too), and translates
    them into the store exactly like the backfill does. So whatever the CLI
    writes appears in the store — and the app — as it happens, without
    duplicating messages the native serve feed already put there.

    ``after_id`` overrides the shared watermark cursor (used by isolated tests
    to drive a deterministic sequence); production passes ``None`` and the
    shared watermark is authoritative.

    Idempotent + thread-safe by the shared watermark: the tail only ever
    translates rows with ``id`` strictly above the current watermark, and
    advances it atomically. Two pollers (or the events loop's syncer) calling
    concurrently cannot double-translate.

    FAIL-SAFE (design §4, mirroring the backfill): any failure here is
    reported, never raised — the caller's live loop must never drop on a
    transient DB lock. Returns ``skipped`` naming the early-return reason.

    Returns {"project", "tail_appended", "session"|None, "skipped"|None,
    "high_water"}.
    """
    from routes_orchestrator import (
        _open_profile_session_db,
        resolve_named_session_id,
    )

    store = get_store(project)
    try:
        profile, _title, session_id = resolve_named_session_id(
            project, registry_path=registry_path)
    except Exception as exc:  # noqa: BLE001 — fail-safe, never raise
        log.debug("gateway: store-tail %s failed to resolve session: %r",
                  project, exc)
        return {"project": project, "tail_appended": 0, "session": None,
                "skipped": "resolve_failed", "high_water": _tail_watermark(project)}
    if session_id is None:
        # The project has not started a named session — nothing to tail.
        return {"project": project, "tail_appended": 0, "session": None,
                "skipped": "no_session", "high_water": _tail_watermark(project)}

    db = _open_profile_session_db(profile, read_only=True)
    if db is None:
        return {"project": project, "tail_appended": 0, "session": session_id,
                "skipped": "no_session", "high_water": _tail_watermark(project)}
    try:
        cursor = after_id if after_id is not None else _tail_watermark(project)
        new_messages = db.get_messages(session_id, after_id=cursor)
        # Defensive: only translate rows strictly above the cursor (get_messages
        # already applies ``id > after_id`` in SQL, but belt-and-suspenders
        # against a cursor that raced backwards).
        new_messages = [m for m in new_messages
                        if isinstance(m, dict) and m.get("id", 0) > cursor]
        count = _translate_history_to_store(store, new_messages)
        if new_messages:
            _mark_tail_seen(project, _max_message_id(new_messages))
        return {"project": project, "tail_appended": count,
                "session": session_id,
                "high_water": _tail_watermark(project)}
    except Exception as exc:  # noqa: BLE001 — fail-safe: never break the loop
        log.warning("gateway: store-tail %s failed (live continues): %r",
                    project, exc)
        return {"project": project, "tail_appended": 0, "session": session_id,
                "skipped": "tail_failed", "high_water": _tail_watermark(project)}
    finally:
        try:
            db.close()
        except Exception:
            pass

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
        self._interrupt_hook = None
        # §3.1 same-session pinning — the project's resolved NAMED Hermes
        # session id + owning profile. Populated by :meth:`_resolve_named_session`
        # on start (or taken straight from the config when supplied); the
        # /api/pty is pinned to this session so app messages land in it (§3.3).
        self._session_id: Optional[str] = config.session_id
        self._session_profile: Optional[str] = config.profile

    # -- lifecycle ---------------------------------------------------------- #

    def _resolve_named_session(self) -> None:
        """Resolve the project's named Hermes session (same-session pinning, §3.1).

        Sets :attr:`_session_id` / :attr:`_session_profile` from the project's
        ACTUAL named session in state.db (via ``resolve_named_session_id``) when
        the config did not already supply them, so both the PTY pinning (§3.3)
        and the backfill (§3.2) agree with the CLI chat path on which session a
        project is. Best-effort: any failure leaves them ``None`` (the driver
        still connects — backfill is skipped and the PTY spawns a fresh session,
        which is the pre-existing behavior, not a regression).
        """
        if self._session_id is not None and self._session_profile is not None:
            return   # already pinned via config
        try:
            from routes_orchestrator import resolve_named_session_id
            profile, _title, session_id = resolve_named_session_id(
                self.config.project,
                registry_path=getattr(self.config, "registry_path", None))
            if self._session_profile is None:
                self._session_profile = profile
            if self._session_id is None:
                self._session_id = session_id
            if self._session_id:
                log.info("gateway: %s pinned to named session %s (profile %s)",
                         self.config.project, self._session_id,
                         self._session_profile)
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            log.warning("gateway: could not resolve named session for %s: %r",
                        self.config.project, exc)

    def _backfill(self) -> None:
        """Backfill the store from the named session's history (§3.2).

        Log-only on failure (and after ``_resolve_named_session`` this can
        itself fail via the resolve step): a backfill that fails must NEVER
        drop the live stream (design §4). ``backfill_named_session`` is
        idempotent, so calling it on every start is safe — a reconnect with an
        unchanged store/high-water is a no-op.
        """
        try:
            result = backfill_named_session(
                self.config.project,
                registry_path=getattr(self.config, "registry_path", None))
        except Exception as exc:  # noqa: BLE001 — never break start()
            log.warning("gateway: backfill %s raised (live continues): %r",
                        self.config.project, exc)
            return
        skipped = result.get("skipped")
        if skipped:
            log.debug("gateway: backfill %s skipped (%s)",
                      self.config.project, skipped)
        else:
            log.info("gateway: backfilled %s with %d history frames for session %s",
                     self.config.project, result.get("backfilled", 0),
                     result.get("session"))

    def start(self) -> None:
        """Connect both upstream sockets and start the reader threads.

        Order matters (the session-continuity contract, design §3.2/§3.3):

          1. Resolve the project's NAMED Hermes session (§3.1) so we know which
             session this project is and can pin / backfill it;
          2. Backfill the store from that session's history (§3.2) BEFORE the
             live feed opens, so iOS shows the whole conversation immediately;
          3. Open /api/pty pinned to that named session (§3.3) + /api/events;
          4. Start the live reader threads.

        The backfill is log-only: a failure (e.g. state.db locked) logs and
        continues — it must never drop the live stream (design §4); a later
        reconnect retries it (idempotent).

        Raises ConnectionError if either upstream connection cannot be
        established (e.g. the gateway is down or the token is rejected).
        """
        if self._alive:
            return
        self._resolve_named_session()
        self._backfill()
        pty, events = self._connect_upstreams()
        self._pty = pty
        self._events = events
        self._stop_evt.clear()
        self._alive = True
        # t_93f1ba4d: wire the events-loop's turn-complete callback so that
        # serve-driven turns it translates are claimed in the shared store-tail
        # watermark — preventing the disk poller from re-translating them.
        self._translator.on_turn_closed = self._sync_tail_watermark

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

        # Install the interrupt hook so a WS ``stop`` frame reaches this
        # driver's PTY (Ctrl-C) when it owns the project (t_68432c2d). The
        # default — no driver — falls back to cancelling the in-flight relay
        # job, which is correct only when no GatewayDriver is attached.
        def _interrupt(proj: str) -> bool:
            if proj != project:
                return False
            return self.interrupt()

        self._interrupt_hook = _interrupt
        _routes_ws.interrupt_user_turn = _interrupt

        ev_thread = threading.Thread(
            target=self._run_events_loop, name="gateway-events",
            daemon=True)
        pty_thread = threading.Thread(
            target=self._run_pty_loop, name="gateway-pty",
            daemon=True)
        # t_93f1ba4d: the store-tail poller — the live source for CLI-driven
        # frames the /api/events fan-out never sees (the operator's SEPARATE
        # CLI REPL writes them straight to state.db). It does NOT need the
        # upstream sockets; it tails the session's on-disk message rows.
        tail_thread = threading.Thread(
            target=self._run_store_tail_loop, name="gateway-store-tail",
            daemon=True)
        self._threads = [ev_thread, pty_thread, tail_thread]
        ev_thread.start()
        pty_thread.start()
        tail_thread.start()

    def _connect_upstreams(self) -> tuple:
        """Open the /api/pty and /api/events connections (no-op gateway probe).

        Read :meth:`start` for the ordering contract. The /api/pty path is
        PINNED to the project's named session (``?profile=<profile>&resume=
        <session_id>``, §3.3) so an app message lands in the SAME conversation
        the CLI continues; /api/events stays plain token+channel (the feed is
        per-channel; the pty publishes this project's session to it).

        Separated from :meth:`start` so tests can inject fake transport
        clients. Returns ``(pty, events)`` both connected. On failure the
        partially-opened pty is closed and :class:`ConnectionError` raised.
        """
        pty = _WSClient(self.config.host, self.config.port,
                        self.config.pty_path(self._session_id,
                                             self._session_profile))
        events = _WSClient(self.config.host, self.config.port,
                           self.config.ws_path(self._EVENTS_PATH))
        pty.connect()
        try:
            events.connect()
        except ConnectionError:
            pty.close()
            raise
        return pty, events

    def _run_store_tail_loop(self) -> None:
        """Poll the session's on-disk frames, translating NEW CLI-driven ones.

        t_93f1ba4d: the live source for externally-written frames. The
        ``/api/events`` feed fans out ONLY the serve's own pty activity, so
        frames the operator's SEPARATE CLI REPL writes straight to state.db
        would otherwise freeze the store at the mount-time backfill watermark.
        This loop re-reads the session's message table every
        :data:`_TAIL_POLL_INTERVAL_S` seconds and appends any NEW rows (id >
        the shared store-tail watermark) to the store, translated exactly like
        the backfill. ``tail_named_session`` is itself fail-safe (never raises)
        and thread-safe (the watermark makes it idempotent), so a transient DB
        lock is a logged skip, never a crash.
        """
        # Idle before the first poll so backfill (which seeds the watermark
        # with the full history) always runs first — the poll must start from
        # AFTER the seeded history, never re-translate it.
        self._sleep_interruptible(_TAIL_POLL_INTERVAL_S)
        while not self._stop_evt.is_set():
            try:
                tail_named_session(
                    self.config.project,
                    registry_path=getattr(self.config, "registry_path", None))
            except Exception as exc:  # noqa: BLE001 — belt-and-suspenders
                log.debug("gateway: store-tail loop %s raised: %r",
                          self.config.project, exc)
            self._sleep_interruptible(_TAIL_POLL_INTERVAL_S)

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep, returning early on ``_stop_evt`` so stop() is prompt."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stop_evt.is_set():
            time.sleep(0.1)

    def _sync_tail_watermark(self) -> None:
        """Advance the store-tail watermark to the session's current on-disk
        max message id.

        t_93f1ba4d dedup half: after the native ``/api/events`` feed has
        translated a serve-driven turn to ``done``, that turn's messages are
        persisted in state.db (the serve's own dispatcher wrote them). By
        advancing the shared watermark to the session's current max id we
        ``claim`` those messages so the store-tail poller does not re-translate
        them — it only picks up rows the native feed never saw (genuinely
        CLI-driven frames). Best-effort + fail-safe: a transient DB read that
        fails is a clean no-op (the next poll re-syncs).
        """
        sid, prof = self._session_id, self._session_profile
        project = self.config.project
        if not sid or not prof:
            return
        try:
            from routes_orchestrator import _open_profile_session_db
            db = _open_profile_session_db(prof, read_only=True)
            if db is None:
                return
            try:
                latest = db.get_messages(sid, latest=True, limit=1)
                if latest:
                    _mark_tail_seen(
                        project, _max_message_id(latest))
            finally:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001 — never break the events loop
            log.debug("gateway: store-tail sync %s failed: %r", project, exc)

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

    def interrupt(self) -> bool:
        """Send an interrupt (Ctrl-C, then CR) to abort the running turn.

        t_68432c2d: the live PTY path's cancel. ``hermes chat`` in the TUI
        treats a Ctrl-C byte (0x03) as the abort keystroke; the trailing CR
        submits the interrupt like the native TUI does. Safe when idle (a
        stray Ctrl-C is harmless), never raises, and returns True once the
        bytes are written (or False when the PTY is gone / stopping).
        """
        pty = self._pty
        if pty is None or self._stop_evt.is_set():
            return False
        try:
            with self._send_lock:
                pty.send_text("\x03")
                time.sleep(0.1)
                pty.send_text("\r")
            return True
        except ConnectionError as exc:
            log.warning("gateway: interrupt failed: %s", exc)
            return False

    # -- teardown ----------------------------------------------------------- #

    def _release_turn_lease(self) -> None:
        """Release the serve's durable session-turn lease on our pinned session.

        While this driver holds a PTY open on the project's named session, the
        upstream ``hermes serve`` keeps a ROLLING durable turn-lease row in the
        profile's ``session_turn_leases`` table (holder
        ``pid=<serve pid>:turn=<turn id>:platform=tui``). If we did nothing, an
        immediate ``hermes chat --continue <project>`` on the operator's CLI
        would be refused SESSION_NOT_OWNED ("this chat is open in another
        Hermes window") until the serve restarted or released. On stop() we
        clear that row so the handoff is immediate.

        Reuses the SAME state.db seam as the backfill (``_open_profile_session_db``,
        §3.2) — no second transport to the fleet — and releases through hermes'
        own first-class primitive (``SessionDB.release_session_turn_lease``),
        which re-derives the conversation key on the write connection and is
        identity-checked + idempotent.

        SAFETY / SCOPING: only a ``platform=tui`` lease on OUR conversation
        (the lineage key of ``self._session_id``) is released. A lease from any
        other surface on this conversation is left untouched, as are all other
        conversations. ``release_session_turn_lease`` re-checks the holder at
        delete time, so even a takeover that lands between our read and the
        delete is a safe no-op (we never delete another process's lease).

        Best-effort and idempotent by construction: no pinned session, no row,
        an already-released / non-TUI lease, an unreachable profile DB, or any
        exception here is a clean no-op that never raises from stop().
        """
        session_id = self._session_id
        profile = self._session_profile
        if not session_id or not profile:
            return
        try:
            from routes_orchestrator import _open_profile_session_db
            db = _open_profile_session_db(profile, read_only=False)
        except Exception:  # noqa: BLE001 — best-effort, never break stop()
            return
        if db is None:
            return
        try:
            # hermes' own key derivation (compression-parent walk to the
            # conversation/lineage root) — the row the serve's acquire used.
            key = db._session_turn_lease_key(session_id)
            row = db._read_one(
                "SELECT holder FROM session_turn_leases WHERE conversation_id = ?",
                (key,))
            holder = row[0] if row is not None else None
            if holder and _is_tui_turn_lease(holder):
                # First-class, identity-checked, idempotent release; a no-op if
                # the holder already changed or the row is gone.
                db.release_session_turn_lease(session_id, holder)
                log.info("gateway: released TUI turn lease on session %s "
                         "(project %s, profile %s)",
                         session_id, self.config.project, profile)
            elif holder:
                log.debug("gateway: not releasing %s turn lease on %s (not a "
                          "TUI holder: %r)", self.config.project, session_id, holder)
        except Exception as exc:  # noqa: BLE001 — never break stop()
            log.warning("gateway: failed to release turn lease on %s: %r",
                        session_id, exc)
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        """Close both upstream sockets, stop the reader threads, and release the
        pinned session's turn lease for an immediate CLI handoff."""
        if not self._alive:
            return
        self._alive = False
        self._stop_evt.set()
        # Release the serve's durable turn lease on our pinned session so the
        # operator's CLI `--continue` succeeds immediately (t_4bcfd5cf).
        # Idempotent + best-effort; must never raise.
        self._release_turn_lease()
        # Restore the default (no-op) WS relay hook for our project.
        if self._relay_hook is not None:
            import routes_ws as _routes_ws
            if _routes_ws.relay_user_message is self._relay_hook:
                _routes_ws.relay_user_message = _routes_ws._default_relay
            self._relay_hook = None
        # Restore the default interrupt hook (cancels the relay job) too.
        if self._interrupt_hook is not None:
            import routes_ws as _routes_ws
            if _routes_ws.interrupt_user_turn is self._interrupt_hook:
                _routes_ws.interrupt_user_turn = _routes_ws._interrupt_relay
            self._interrupt_hook = None
        for ws in (self._pty, self._events):
            if ws is not None:
                ws.close()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads = []
        self._pty = None
        self._events = None
