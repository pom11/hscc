"""RFC 6455 WebSocket framing (server side), pure stdlib — t_47f51a71.

The HSCC API server is deliberately ``http.server`` / stdlib only (no
flask/fastapi/uvicorn; see api_server.py docstring). To offer the live
session stream over WebSocket — ``/v1/projects/{name}/session/ws`` — the
bridge needs the RFC 6455 wire protocol implemented against the raw socket.
This module is that implementation: the handshake-accept computation and the
frame encoder/decoder, as pure functions with no I/O, so they are unit-testable
in isolation against known RFC-interop vectors.

Scope (a deliberately SMALL, reviewable RFC surface — enough for the chat
stream, none of which needs extensions or compression):

* ``handshake_accept(key)`` — the ``Sec-WebSocket-Accept`` value.
* ``encode_frame(opcode, payload, fin=True, rsv=0)`` — a SERVER→client frame
  (server frames are never masked).
* A streaming decoder ``FrameDecoder`` — an incremental client→server frame
  reader that consumes masked frames, handles fragmentation (continuation),
  and yields control frames (ping/pong/close) alongside the data.

Deliberately NOT implemented (out of scope, would need ext-negotiation):
  per-message deflate (permessage-deflate), extensions, big (>16 MiB) frames,
  cross-origin policy — the app is a native client, not a browser.

All functions raise :class:`WSProtocolError` on a malformed/oversize frame.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from typing import Callable, Iterator, Optional

# The RFC 6455 GUID appended to the key before SHA-1 for the accept value.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes (RFC 6455 §5.2).
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# Payload-size fences (RFC 6455 §5.2).
LEN_16BIT = 126      # 16-bit extended length follows
LEN_64BIT = 127      # 64-bit extended length follows
MAX_PAYLOAD = 16 * 1024 * 1024   # 16 MiB — generous for a chat frame, bounded.

# Frame close codes (RFC 6455 §7.4.1) — the few the server emits.
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_UNSUPPORTED = 1003
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009


class WSProtocolError(Exception):
    """A malformed, oversized, or protocol-violating frame."""


def handshake_accept(key: str) -> str:
    """Return the ``Sec-WebSocket-Accept`` header for a ``Sec-WebSocket-Key``.

    RFC 6455 §4.2.2:  base64(SHA-1(key + GUID)).
    """
    digest = hashlib.sha1((key + WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(opcode: int, payload: bytes, fin: bool = True,
                 rsv: int = 0) -> bytes:
    """Encode one server→client frame (unmasked)."""
    if rsv != 0:
        raise WSProtocolError("extensions not negotiated (rsv != 0)")
    if len(payload) > MAX_PAYLOAD:
        raise WSProtocolError(f"payload too large: {len(payload)}")

    b0 = (0x80 if fin else 0x00) | ((rsv & 0x7) << 4) | (opcode & 0x0F)
    n = len(payload)
    if n < LEN_16BIT:
        header = bytes([b0, n])
    elif n <= 0xFFFF:
        header = bytes([b0, LEN_16BIT]) + struct.pack("!H", n)
    else:
        header = bytes([b0, LEN_64BIT]) + struct.pack("!Q", n)
    return header + payload


# -- incremental decoder ---------------------------------------------------- #

class FrameDecoder:
    """Incremental stream decoder yielding complete WebSocket frames.

    Feed bytes with :meth:`feed`; it buffers partial frames and yields
    ``(opcode, payload, fin)`` tuples as they complete. Consumes MASKED
    frames (client→server must be masked per RFC 6455 §5.1); a server→client
    table flip by the test harness is not supported here (the bridge only
    ever reads client frames on this decoder).

    Fragmentation: a TEXT/BINARY frame with ``fin=False`` is followed by one
    or more CONTINUATION frames; the caller re-assembles them (see
    :func:`iter_data_messages`).
    """

    def __init__(self, max_payload: int = MAX_PAYLOAD):
        self._buf = bytearray()
        self.max_payload = max_payload

    def feed(self, data: bytes) -> Iterator[tuple[int, bytes, bool]]:
        """Consume ``data``; yield completed ``(opcode, payload, fin)`` frames."""
        self._buf.extend(data)
        while True:
            frame = self._try_read_frame()
            if frame is None:
                return
            yield frame

    def _try_read_frame(self) -> Optional[tuple[int, bytes, bool]]:
        buf = self._buf
        if len(buf) < 2:
            return None

        b0, b1 = buf[0], buf[1]

        fin = bool(b0 & 0x80)
        rsv = (b0 >> 4) & 0x7
        if rsv != 0:
            raise WSProtocolError("extensions not negotiated (rsv != 0)")
        opcode = b0 & 0x0F

        masked = bool(b1 & 0x80)
        if not masked:
            raise WSProtocolError("client frame must be masked")

        length = b1 & 0x7F
        idx = 2

        if length == LEN_16BIT:
            if len(buf) < idx + 2:
                return None
            length = struct.unpack("!H", buf[idx:idx + 2])[0]
            idx += 2
        elif length == LEN_64BIT:
            if len(buf) < idx + 8:
                return None
            length = struct.unpack("!Q", buf[idx:idx + 8])[0]
            idx += 8

        if length > self.max_payload:
            raise WSProtocolError(f"frame payload {length} exceeds max")

        # Mask key (4 bytes).
        if len(buf) < idx + 4:
            return None
        mask_key = buf[idx:idx + 4]
        idx += 4

        if len(buf) < idx + length:
            return None

        payload = bytes(buf[idx:idx + length])
        del buf[:idx + length]

        # Unmask (RFC 6455 §5.3).
        unmasked = bytes(
            byte ^ mask_key[i % 4] for i, byte in enumerate(payload)
        )
        return opcode, unmasked, fin


def iter_data_messages(decoder: FrameDecoder, data: bytes,
                       on_close: Optional[Callable[[bytes], None]] = None
                       ) -> Iterator[tuple[int, bytes]]:
    """Yield complete (opcode, payload) DATA messages from a byte stream.

    Reassembles TEXT/BINARY fragments across CONTINUATION frames, and drains
    control frames (ping/pong/close) WITHOUT yielding them as data messages.
    Control frames may be interleaved anywhere (RFC 6455 §5.4).

    When ``on_close`` is given, it is called with the close frame's payload
    the moment a close frame completes — so a server can detect the peer's
    close and reply in kind. Ping/pong are silently dropped and only finished
    data messages are yielded.
    """
    curr_opcode: Optional[int] = None
    curr: bytearray = bytearray()

    for opcode, payload, fin in decoder.feed(data):
        if opcode in (OP_PING, OP_PONG):
            continue
        if opcode == OP_CLOSE:
            if on_close is not None:
                on_close(payload)
            continue
        if opcode in (OP_TEXT, OP_BINARY):
            if curr_opcode is not None:
                raise WSProtocolError("new data frame while fragment in progress")
            if fin:
                yield opcode, payload
            else:
                curr_opcode = opcode
                curr = bytearray(payload)
            continue
        if opcode == OP_CONTINUATION:
            if curr_opcode is None:
                raise WSProtocolError("continuation frame without a data frame")
            curr.extend(payload)
            if fin:
                yield curr_opcode, bytes(curr)
                curr_opcode = None
                curr = bytearray()
            continue
        raise WSProtocolError(f"reserved opcode 0x{opcode:x}")


def parse_close_frame(payload: bytes) -> int:
    """Extract the numeric close code from a close-frame payload (default 1005)."""
    if len(payload) == 0:
        return 1005                      # no status code present (§5.5.1)
    if len(payload) < 2:
        raise WSProtocolError("close frame payload too short for a status code")
    return struct.unpack("!H", payload[:2])[0]


def make_close_frame(code: int, reason: str = "") -> bytes:
    """Encode a server close frame with the given code + reason."""
    reason_b = reason.encode("utf-8")[:120]
    return encode_frame(OP_CLOSE, struct.pack("!H", code) + reason_b)
