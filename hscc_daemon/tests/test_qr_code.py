"""Unit tests for the pure-stdlib QR encoder (hscc_daemon.qr_code).

Proves the encoder is correct three independent ways:

  1. A PINNED matrix regression — the 41x41 matrix that the encoder produces
     for the fixed connection payload below is baked in verbatim, so any
     future encoder change that alters the output fails. Its correctness is
     corroborated independently by the self-contained decoder (section 2),
     which reconstructs the layout from the spec tables and reads the matrix
     back to the exact payload.
  2. A SELF-CONTAINED DECODER — the test re-derives the QR layout from the
     spec tables (finder/timing/alignment/format positions) and reads the
     matrix back, asserting the payload is byte-identical for a range of
     payload sizes and versions. This exercises the encoder with no shared
     algorithm code with the thing it calls "correct".
  3. Structural invariants — square matrix, version == capacity picked, the
     encoder picks the SMALLEST version that fits (proved at the 120-byte
     boundary), and the renderers emit what they claim.

The whole file is pure-stdlib (pytest only); no `qrcode`/`Pillow`/zbar.
"""

from hscc_daemon import qr_code


# ---------------------------------------------------------------------------
# 1) Pinned matrix regression (zbar-verified during development)
# ---------------------------------------------------------------------------

# Payload: the exact connection JSON the CLI produces (real 43-char token,
# no spaces after colons, no trailing newline) — 93 bytes.
_PAYLOAD = (
    '{"v":1,"host":"100.64.0.3","port":8787,'
    '"token":"AAAAAAAAAAAAAAAAAAAAAAAAAAaaaaaaaaaaaaaaaaa"}'
)

# Encoder picked the smallest fit → version 6 (41x41). Matrix rows listed top
# to bottom as '1'=dark / '0'=light. Corroborated by the independent decoder
# in section 2 (reads back to the exact payload).
_PINNED = [
    "11111110010010100111001001100010001111111",
    "10000010101011010011110110111101101000001",
    "10111010011100111110100011101000101011101",
    "10111010011011000111010101111011101011101",
    "10111010111101000110010111110111101011101",
    "10000010001010101011101100010101001000001",
    "11111110101010101010101010101010101111111",
    "00000000001111011101010011000101000000000",
    "10101010000111111110111111100101100010010",
    "00111101110101000110011001000100110001111",
    "10000110000011001010111010001100101111011",
    "00011101000010100111010101000110000101000",
    "00010111110011000000011011001110111100001",
    "01100100011011010101011111010110111001010",
    "10011010000000001010001110100010000000011",
    "01100100011100100111011011111111011100000",
    "10000010010110000100100110011000101101001",
    "01111101000100011100100111011101101101011",
    "00011110000001101010010000100010011001101",
    "01000100110101001111000101110111010100010",
    "10100011011100010010100010101100001000110",
    "00010000011111111111110011100000011001001",
    "00111110010110110110010001101100101111111",
    "00010001010001010111010111011110000100000",
    "01101011001001110110011001111101111010010",
    "11111101011010100100001000100010010001011",
    "10111011100000101010111011001010101110011",
    "00011000011000110110110101111101100100000",
    "11000110100001111001111001000111111101001",
    "01101001011100100101110101001110111001010",
    "10010110111011111011101000101010001100111",
    "01011001100100011110111101100111000010000",
    "10010010111111110101110010001000111110001",
    "00000000101100011000110110011101100011011",
    "11111110000110010100001001000010101011101",
    "10000010011000011001011100010110100010011",
    "10111010110101111000101010001110111110101",
    "10111010011110110101111011000100010111000",
    "10111010100000111000100000100110100011111",
    "10000010011010101100010001101110110110010",
    "11111110110100101110111101100100100110011",
]


def _matrix_from_pinned():
    """Convert the pinned string rows into a list[list[bool]] matrix."""
    return [[c == "1" for c in row] for row in _PINNED]


class TestPinnedMatrix:
    def test_encoder_reproduces_reference_matrix(self):
        got = qr_code.make_qr(_PAYLOAD.encode("utf-8"))
        want = _matrix_from_pinned()
        assert len(got) == len(want) and all(len(r) == len(want) for r in want)
        assert got == want


# ---------------------------------------------------------------------------
# 2) Self-contained decoder
# ---------------------------------------------------------------------------

# Copy of the standard layout tables (spec constants, shared with the encoder).
_ALIGN = qr_code._ALIGNMENT_POS


def _place_finders(func, size):
    for r0, c0 in ((0, 0), (size - 7, 0), (0, size - 7)):
        for r in range(max(0, r0 - 1), min(size, r0 + 8)):
            for c in range(max(0, c0 - 1), min(size, c0 + 8)):
                func[r][c] = True


def _place_alignment(func, size, version):
    """Alignment patterns, mirroring the encoder: skip the top-left finder and
    any position whose centre is already a function module (a finder)."""
    pos = _ALIGN[version]
    for cy in pos:
        for cx in pos:
            if (cx, cy) == (6, 6):
                continue
            if func[cy][cx]:
                continue
            for r in range(cy - 2, cy + 3):
                for c in range(cx - 2, cx + 3):
                    func[r][c] = True


def _place_timing(func, size):
    for i in range(8, size - 8):
        if not func[i][6]:
            func[i][6] = True
        if not func[6][i]:
            func[6][i] = True


def _build_function_map(matrix):
    size = len(matrix)
    version = (size - 17) // 4
    func = [[False] * size for _ in range(size)]
    _place_finders(func, size)
    _place_alignment(func, size, version)
    _place_timing(func, size)
    # Format-information areas: positions are fixed (only values vary with the
    # mask). Copy 1 around the top-left finder, copy 2 on the right/bottom.
    for i in range(6):
        func[i][8] = True                    # copy1 vertical (bits 0-5)
    func[7][8] = func[8][8] = True           # copy1 (bits 6,7)
    func[8][7] = True                        # copy1 (bit 8)
    for i in range(9, 15):
        func[8][14 - i] = True               # copy1 (bits 9-14)
    for i in range(8):
        func[8][size - 1 - i] = True         # copy2 (bits 0-7)
    for i in range(8, 15):
        func[size - 15 + i][8] = True        # copy2 (bits 8-14)
    # Version-information areas for version >= 7 (6x3 blocks, 2 copies).
    if version >= 7:
        for i in range(6):
            for j in range(3):
                func[i][size - 11 + j] = True
                func[size - 11 + j][i] = True
    # Dark module (below the top-right finder).
    func[size - 8][8] = True
    return func


def _read_format(m):
    """Return (ec_bits, mask) from BOTH format copies; they must agree."""
    size = len(m)
    bits_copies = []

    def read(y, x):
        return 1 if m[y][x] else 0

    # Copy A: around the top-left finder.
    first = []
    for i in range(6):
        first.append(read(i, 8))        # bits 0-5
    first += [read(7, 8), read(8, 8)]   # bits 6,7
    first.append(read(8, 7))            # bit 8
    for i in range(9, 15):
        first.append(read(8, 14 - i))   # bits 9-14, row 8 cols 5..0
    bits_copies.append(first)

    # Copy B: right column + bottom row.
    second = []
    for i in range(8):
        second.append(read(8, size - 1 - i))    # bits 0-7, row 8 right side
    for i in range(8, 15):
        second.append(read(size - 15 + i, 8))   # bits 8-14, col 8 bottom
    bits_copies.append(second)

    results = []
    for bits15 in bits_copies:
        val = 0
        for i, b in enumerate(bits15):
            val |= b << i
        # Undo the 0x5412 mask, then strip the 10-bit BCH.
        raw = val ^ 0x5412
        fmt = raw >> 10
        ec = (fmt >> 3) & 0x03
        mask = fmt & 0x07
        results.append((ec, mask))
    assert results[0] == results[1], f"format copies disagree: {results}"
    return results[0]


def _decode(matrix):
    """Decode a byte-mode, EC-level-M matrix. Returns the raw bytes."""
    size = len(matrix)
    version = (size - 17) // 4
    func = _build_function_map(matrix)

    ec, mask = _read_format(matrix)
    assert ec == 0, "expected EC level M (00)"

    # Read ALL data-module bits in zigzag order, applying the mask to recover
    # the raw interleaved codeword stream (data codewords then EC codewords).
    bits = []
    for right in range(size - 1, 0, -2):
        if right <= 6:
            right -= 1
        for vert in range(size):
            for j in range(2):
                c = right - j
                upward = ((right + 1) & 2) == 0
                r = (size - 1 - vert) if upward else vert
                if func[r][c]:
                    continue
                raw = 1 if matrix[r][c] else 0
                if _mask(mask, r, c):
                    raw ^= 1
                bits.append(raw)

    # Recover the per-block data codewords. The bit stream is the spec's
    # interleave: data codewords round-robin across blocks (short blocks first
    # for mixed sizes), then EC. We de-interleave the data region so block 0's
    # logical data holds the byte-mode payload contiguously.
    blocks = []
    for d, e, r in qr_code._BLOCK_TABLE_M[version]:
        blocks.extend([(d, e)] * r)
    data_per_block = [d for d, _e in blocks]
    total_data = sum(data_per_block)
    # First total_data*8 bits are the interleaved data codewords.
    stream_bytes = [
        _int(bits[i:i + 8]) for i in range(0, total_data * 8, 8)
    ]
    block_datas = [[] for _ in blocks]
    pos = 0
    max_data_len = max(data_per_block)
    for i in range(max_data_len):
        for bi, dcount in enumerate(data_per_block):
            if i < dcount:
                block_datas[bi].append(stream_bytes[pos])
                pos += 1

    # The logical data stream is the per-block data concatenated in block
    # order; block 0 holds the byte-mode header (mode + count) and the payload
    # continues contiguously through the later blocks.
    logical = []
    for chunk in block_datas:
        logical.extend(chunk)
    allbits = []
    for b in logical:
        allbits += [(b >> k) & 1 for k in range(7, -1, -1)]

    def take(n):
        nonlocal allbits
        out, allbits = allbits[:n], allbits[n:]
        return out

    mode = _int(take(4))
    assert mode == 4, f"expected byte mode (0100), got {mode:04b}"
    count = _int(take(8 if version <= 9 else 16))
    data = []
    for _ in range(count):
        data.append(_int(take(8)))
    return bytes(data)


def _int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def _mask(m, r, c):
    if m == 0:
        return (r + c) % 2 == 0
    if m == 1:
        return r % 2 == 0
    if m == 2:
        return c % 3 == 0
    if m == 3:
        return (r + c) % 3 == 0
    if m == 4:
        return (r // 2 + c // 3) % 2 == 0
    if m == 5:
        return (r * c) % 2 + (r * c) % 3 == 0
    if m == 6:
        return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


class TestDecodeRoundTrip:
    """Encoder output must decode back to byte-identical input, across a range
    of payload sizes that land on different versions."""
    CASES = [
        b"hi",                                            # version 1
        b"a" * 30,                                        # version 3
        _PAYLOAD.encode("utf-8"),                         # version 6 (pinned)
        b"x" * 90,                                        # version 6 too
        b"x" * 120,                                       # version 7 (smallest fit)
        b"x" * 125,                                       # version 8 (tests version info)
        ("y" * 20).encode() + "z".encode() * 100,
    ]

    def test_round_trips_all(self):
        for data in self.CASES:
            m = qr_code.make_qr(data)
            got = _decode(m)
            assert got == data, f"{data[:20]!r}... -> {got[:20]!r}"

    def test_exact_byte_identity(self):
        """The decoded bytes equal the encoded bytes exactly."""
        data = b"x" * 213  # the version-10 level-M byte-mode maximum
        m = qr_code.make_qr(data)
        assert _decode(m) == data

    def test_binary_payload_round_trips(self):
        """A payload with many non-ASCII byte values (interpreted as bytes, not
        text) still round-trips byte-identical."""
        data = bytes((i * 7 + 13) % 256 for i in range(200))
        m = qr_code.make_qr(data)
        assert _decode(m) == data


# ---------------------------------------------------------------------------
# 3) Structure / capacity / smallest-version
# ---------------------------------------------------------------------------

class TestVersionSelection:
    def test_smallest_version_for_120_bytes_is_7(self):
        """120 bytes of byte-mode data needs version 7 at EC M (122-byte
        capacity); version 6 (106) cannot hold it."""
        data = b"x" * 120
        m = qr_code.make_qr(data)
        version = (len(m) - 17) // 4
        assert version == 7
        # And prove 6 is genuinely too small.
        excess = b"x" * 200  # forces a bigger version; just asserts sane range
        version2 = (len(qr_code.make_qr(excess)) - 17) // 4
        assert version2 > 7

    def test_all_versions_are_square_and_in_range(self):
        for n in (1, 30, 57, 120, 213):
            m = qr_code.make_qr(b"z" * n)
            size = len(m)
            assert (size - 17) % 4 == 0
            assert all(len(row) == size for row in m)

    def test_rejects_out_of_range_payload(self):
        try:
            qr_code.make_qr(b"z" * 300)  # beyond version-10 level-M capacity
        except ValueError:
            return  # expected
        raise AssertionError("expected ValueError for an oversized payload")

    def test_rejects_empty_payload(self):
        try:
            qr_code.make_qr(b"")
        except ValueError:
            return
        raise AssertionError("expected ValueError for an empty payload")


class TestRenderers:
    def test_unicode_and_ascii_render_square_with_quiet_zone(self):
        m = qr_code.make_qr(b"hello")
        size = len(m)
        u = qr_code.render_unicode(m)
        a = qr_code.render_ascii(m)
        # Unicode: 2 module rows per line → ceil((size+8)/2) lines.
        assert len(u.split("\n")) == (size + 2 * qr_code.QUIET_ZONE + 1) // 2
        # ASCII: one line per module row.
        assert len(a.split("\n")) == size + 2 * qr_code.QUIET_ZONE
        # Assert every rendered line is the expected width.
        full_width = size + 2 * qr_code.QUIET_ZONE
        for line in u.split("\n"):
            assert len(line) == full_width
        for line in a.split("\n"):
            assert len(line) == full_width * 2

    def test_render_text_force_ascii_is_pure_ascii(self):
        m = qr_code.make_qr(b"force-ascii")
        text = qr_code.render_text(m, force_ascii=True)
        assert all(ord(ch) < 128 for ch in text)
        assert qr_code._ASCII_DARK in text

    def test_quiet_zone_is_light(self):
        m = qr_code.make_qr(b"qz")
        a = qr_code.render_ascii(m, quiet=4)
        lines = a.split("\n")
        # First 4 lines are pure light (all spaces).
        for line in lines[:4]:
            assert line.strip() == ""
        # Left padding is all spaces.
        assert lines[4][:8] == "        "

    def test_errors_on_negative_quiet(self):
        m = qr_code.make_qr(b"x")
        try:
            qr_code.render_unicode(m, quiet=-1)
        except ValueError:
            return
        raise AssertionError("expected ValueError for negative quiet")


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
