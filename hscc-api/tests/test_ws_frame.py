"""Tests for the RFC 6455 WebSocket framing layer (t_47f51a71).

These pin the pure wire-protocol primitives — handshake-accept computation and
frame encode/decode — against known RFC-6455 interop vectors and error cases.
No I/O: the decoder is fed byte strings directly. This is the foundation the
``/v1/projects/{name}/session/ws`` endpoint (routes_ws.py) stands on, so it is
tested in isolation first.
"""

import struct

import pytest

import ws_frame
from ws_frame import (
    CLOSE_NORMAL,
    LEN_16BIT,
    LEN_64BIT,
    OP_BINARY,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    FrameDecoder,
    WSProtocolError,
    encode_frame,
    handshake_accept,
    iter_data_messages,
    make_close_frame,
    parse_close_frame,
)


# --------------------------------------------------------------------------- #
# Handshake accept
# --------------------------------------------------------------------------- #

def test_handshake_accept_rfc_vector():
    """RFC 6455 §4.2.2 worked example."""
    assert handshake_accept("dGhlIHNhbXBsZSBub25jZQ==") == \
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_handshake_accept_deterministic():
    assert handshake_accept("abcd==") == handshake_accept("abcd==")
    assert handshake_accept("abcd==") != handshake_accept("wxyz==")


# --------------------------------------------------------------------------- #
# Server frame encoding (unmasked)
# --------------------------------------------------------------------------- #

def test_encode_small_frame():
    assert encode_frame(OP_TEXT, b"hi") == bytes([0x81, 0x02]) + b"hi"


def test_encode_16bit_length():
    payload = b"x" * 200
    frame = encode_frame(OP_TEXT, payload)
    assert frame[:4] == bytes([0x81, LEN_16BIT]) + struct.pack("!H", 200)
    assert frame[4:] == payload


def test_encode_64bit_length():
    payload = b"y" * 70000
    frame = encode_frame(OP_BINARY, payload)
    assert frame[0] == 0x82
    assert frame[1] == LEN_64BIT
    assert struct.unpack("!Q", frame[2:10])[0] == 70000
    assert frame[10:] == payload


def test_encode_fin_and_rsv():
    # fin=0, rsv=0 -> b0 = opcode only.
    assert encode_frame(OP_CONTINUATION, b"", fin=False)[0] == OP_CONTINUATION
    # fin=1 -> b0 high bit set.
    assert encode_frame(OP_TEXT, b"")[0] == 0x81
    with pytest.raises(WSProtocolError, match="rsv"):
        encode_frame(OP_TEXT, b"", rsv=4)


# --------------------------------------------------------------------------- #
# Decoder basics (client frames are MASKED)
# --------------------------------------------------------------------------- #

def _mask(payload: bytes, key: bytes = b"\x01\x02\x03\x04") -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _client_frame(opcode, payload: bytes, fin=True, key=b"\x01\x02\x03\x04"):
    b0 = (0x80 if fin else 0) | opcode
    b1 = 0x80 | len(payload)  # masked
    return bytes([b0, b1]) + key + _mask(payload, key)


def test_decode_text_frame():
    dec = FrameDecoder()
    frames = list(dec.feed(_client_frame(OP_TEXT, b"hello")))
    assert frames == [(OP_TEXT, b"hello", True)]


def test_decode_unmasked_rejected():
    dec = FrameDecoder()
    with pytest.raises(WSProtocolError, match="must be masked"):
        list(dec.feed(b"\x81\x02hi"))


def test_decode_incremental_feeds():
    dec = FrameDecoder()
    data = _client_frame(OP_TEXT, b"hello world")
    out = []
    # Feed one byte at a time — the decoder must buffer & complete incrementally.
    for byte in data:
        out.extend(dec.feed(bytes([byte])))
    assert out == [(OP_TEXT, b"hello world", True)]


def test_decode_16bit_length_masked():
    payload = b"z" * 300
    frame = (bytes([0x81, 0x80 | LEN_16BIT]) + struct.pack("!H", 300)
             + b"\x01\x02\x03\x04" + _mask(payload))
    dec = FrameDecoder()
    assert list(dec.feed(frame)) == [(OP_TEXT, payload, True)]


def test_decode_64bit_length_masked():
    payload = b"q" * 70000
    frame = (bytes([0x82, 0x80 | LEN_64BIT]) + struct.pack("!Q", 70000)
             + b"\x01\x02\x03\x04" + _mask(payload))
    dec = FrameDecoder()
    assert list(dec.feed(frame)) == [(OP_BINARY, payload, True)]


def test_decode_oversize_rejected():
    dec = FrameDecoder(max_payload=10)
    frame = bytes([0x81, 0x80 | LEN_16BIT]) + struct.pack("!H", 20) + \
        b"\x00" * 4 + _mask(b"x" * 20)
    with pytest.raises(WSProtocolError, match="exceeds max"):
        list(dec.feed(frame))


# --------------------------------------------------------------------------- #
# Fragmentation + control frames
# --------------------------------------------------------------------------- #

def test_message_reassembly_across_fragments():
    frames = [
        _client_frame(OP_TEXT, b"hel", fin=False),
        _client_frame(OP_CONTINUATION, b"lo ", fin=False),
        _client_frame(OP_CONTINUATION, b"world", fin=True),
    ]
    dec = FrameDecoder()
    msgs = list(iter_data_messages(dec, b"".join(frames)))
    assert msgs == [(OP_TEXT, b"hello world")]


def test_control_frame_interleaved_dropped_from_data():
    frames = [
        _client_frame(OP_TEXT, b"abc", fin=False),
        _client_frame(OP_PING, b"ping"),
        _client_frame(OP_CONTINUATION, b"def", fin=True),
        _client_frame(OP_CLOSE, b""),
    ]
    dec = FrameDecoder()
    msgs = list(iter_data_messages(dec, b"".join(frames)))
    # ping + close are dropped from the data stream; only the text survives.
    assert msgs == [(OP_TEXT, b"abcdef")]


def test_continuation_without_start_rejected():
    dec = FrameDecoder()
    with pytest.raises(WSProtocolError, match="continuation frame without"):
        list(iter_data_messages(dec, _client_frame(OP_CONTINUATION, b"x")))

def test_new_frame_while_fragment_in_progress_rejected():
    dec = FrameDecoder()
    frames = [
        _client_frame(OP_TEXT, b"hel", fin=False),
        _client_frame(OP_TEXT, b"world", fin=True),  # not a continuation
    ]
    with pytest.raises(WSProtocolError, match="while fragment"):
        list(iter_data_messages(dec, b"".join(frames)))


def test_reserved_opcode_rejected():
    dec = FrameDecoder()
    with pytest.raises(WSProtocolError, match="reserved opcode"):
        list(iter_data_messages(dec, _client_frame(0x3, b"x")))


def test_rsv_bits_rejected():
    dec = FrameDecoder()
    # b0 with rsv=4 set.
    frame = bytes([(0x80 | (4 << 4) | OP_TEXT), 0x82]) + b"\x00" * 4 + \
        _mask(b"x")
    with pytest.raises(WSProtocolError, match="rsv"):
        list(dec.feed(frame))


# --------------------------------------------------------------------------- #
# Close-frame helpers
# --------------------------------------------------------------------------- #

def test_parse_close_frame_empty_is_1005():
    assert parse_close_frame(b"") == 1005


def test_parse_close_frame_with_code():
    assert parse_close_frame(struct.pack("!H", CLOSE_NORMAL)) == CLOSE_NORMAL


def test_close_frame_roundtrip():
    # A client→server close frame (masked) with a status code + reason.
    payload = struct.pack("!H", CLOSE_NORMAL) + b"bye"
    frame = _client_frame(OP_CLOSE, payload)
    dec = FrameDecoder()
    ops = list(dec.feed(frame))
    assert ops[0][0] == OP_CLOSE
    assert parse_close_frame(ops[0][1]) == CLOSE_NORMAL
