"""Pure-stdlib QR code encoder (byte mode, error-correction level M).

HSCC is pure-stdlib (see the README badge) — `qrcode`/`Pillow` are NOT
available and must not be added. This module implements QR encoding from
scratch, restricted to what the connection-settings use case needs:

  * byte mode only
  * error-correction level M
  * the smallest version that fits the payload (versions 1..10 supported;
    level-M byte-mode capacity covers everything up to ~214 bytes)
  * all 8 data masks with standard-Nayuki penalty scoring so the chosen code
    is the most scannable one

The module exposes a pure encoder (`make_qr`) plus two terminal renderers
(unicode half-block and ASCII fallback). Nothing here ever knows about auth
tokens, hostnames, or the CLI — the payload is passed in as opaque bytes, and
the caller decides what to encode.

Reference: ISO/IEC 18004. The version/capacity/ECC-block figures below are the
standard published values for EC level M.
"""

from __future__ import annotations

__all__ = ["make_qr", "render_unicode", "render_ascii", "render_text",
           "REED_SOLOMON_EXP", "REED_SOLOMON_LOG"]

# ---------------------------------------------------------------------------
# Field tables
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ECC block structure per version, EC level M.
#
# Each entry: list of (data_codewords, ec_codewords) per block.
# All blocks within one version share the SAME ec_codewords count (level M).
# (v, data_per_block, ec_per_block, num_blocks) — we store the per-block shape
# for the common case (uniform block sizes) and, for the two versions with
# mixed block sizes, an explicit list.
#
# We use a compact tuple table: (version, blocks) where blocks is a tuple of
# (data_count, ec_count, repeat). Undefined versions beyond 10 are refused.
_BLOCK_TABLE_M = {
    1:  ((16, 10, 1),),
    2:  ((28, 16, 1),),
    3:  ((44, 26, 1),),
    4:  ((32, 18, 2),),
    5:  ((43, 24, 2),),
    6:  ((27, 16, 4),),
    7:  ((31, 18, 4),),
    8:  ((38, 22, 2), (39, 22, 2)),
    9:  ((36, 22, 3), (37, 22, 2)),
    10: ((43, 26, 4), (44, 26, 1)),
}

# Total number of data codewords for each version, level M (precomputed from
# _BLOCK_TABLE_M for the "smallest version" search).
_DATA_CODEWORDS_M = {
    v: sum(d * r for (d, _e, r) in shapes)
    for v, shapes in _BLOCK_TABLE_M.items()
}

# Alignment-pattern center positions per version (ISO 18004 table 1).
_ALIGNMENT_POS = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50],
}

# EC level → 2-bit format-information code. M = 00.
_FORMAT_EC_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}

_EC_LEVEL_FMT = _FORMAT_EC_BITS["M"]


# ---------------------------------------------------------------------------
# Reed-Solomon over GF(2^8)
# ---------------------------------------------------------------------------

def _reed_solomon_init():
    """Precompute GF(256) exp/log tables (generator polynomial 0x11D)."""
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


REED_SOLOMON_EXP, REED_SOLOMON_LOG = _reed_solomon_init()


def _rs_mul(x, y):
    if x == 0 or y == 0:
        return 0
    return REED_SOLOMON_EXP[REED_SOLOMON_LOG[x] + REED_SOLOMON_LOG[y]]


def _rs_compute_divisor(degree):
    """Monic generator polynomial of the given degree."""
    result = [0] * (degree - 1) + [1]
    root = 1
    for _i in range(degree):
        for j in range(len(result)):
            result[j] = _rs_mul(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _rs_mul(root, 0x02)
    return result


def _rs_compute_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i in range(len(result)):
            result[i] ^= _rs_mul(divisor[i], factor)
    return result


# ---------------------------------------------------------------------------
# Public encoder
# ---------------------------------------------------------------------------

def _pick_version(data_len: int) -> int:
    """Smallest version (1..10) whose level-M byte-mode capacity fits data_len."""
    for v in range(1, 11):
        # Byte-mode overhead: 4-bit mode + 8-bit char count; terminator ≤ 4 bits.
        data_bits = _DATA_CODEWORDS_M[v] * 8
        if 12 + 8 * data_len <= data_bits:
            return v
    raise ValueError(
        f"payload of {data_len} bytes exceeds version-10 level-M capacity "
        f"({_DATA_CODEWORDS_M[10] - 1} bytes max byte-mode)"
    )


def make_qr(data: bytes, ecl: str = "M") -> list:
    """Encode ``data`` (byte mode) into a QR module matrix.

    Returns a square ``list[list[bool]]`` — ``matrix[y][x]`` is True for a
    dark module (row first, then column). The quiet zone is NOT included; the
    caller adds it at render time. ``ecl`` must be "M" (only level supported).
    """
    if ecl != "M":
        raise ValueError("HSCC QR encoder supports only error-correction level M")
    if isinstance(data, str):
        data = data.encode("utf-8")
    data = bytes(data)
    if not data:
        raise ValueError("cannot encode an empty payload")

    size = _pick_version(len(data)) * 4 + 17
    return _QrCode(size, data).modules


class _QrCode:
    """Internal builder for one QR code (already the chosen version)."""

    def __init__(self, size: int, data: bytes):
        self.size = size
        self.version = (size - 17) // 4
        self.modules = [[False] * size for _ in range(size)]
        self.is_function = [[False] * size for _ in range(size)]

        # 1) Encode data + ECC into the interleaved codeword bitstream.
        data_codewords = self._encode_data(data)
        all_codewords = self._add_ecc_and_interleave(data_codewords, data)

        # 2) Draw function patterns, then the raw (unmasked) data bits.
        self._draw_function_patterns()
        self._draw_data_codewords(all_codewords)

        # 3) Try every mask on the FULL grid; keep the lowest penalty. Each
        #    mask XORs the data modules (mask is its own inverse), and the
        #    format bits are redrawn for that mask before scoring.
        best = (0, 0)  # (penalty, mask)
        for mask in range(8):
            self._apply_mask_function(bits=None, mask=mask)
            self._draw_format_bits(mask)
            score = self._evaluate_penalty()
            if mask == 0 or score < best[0]:
                best = (score, mask)
            self._apply_mask_function(bits=None, mask=mask)  # undo mask

        # 4) Apply the winning mask and leave its format bits in place.
        self._apply_mask_function(bits=None, mask=best[1])
        self._draw_format_bits(best[1])

    # -- function patterns ------------------------------------------------

    def _set_function_module(self, y, x, is_dark):
        self.modules[y][x] = is_dark
        self.is_function[y][x] = True

    def _draw_function_patterns(self):
        size = self.size
        already = self.is_function  # shortcut (module already a function module)

        # Finder patterns + separators in the three corners (9x9 each).
        # (x, y) = (col, row): top-left, top-right, bottom-left.
        for fx, fy in ((0, 0), (size - 7, 0), (0, size - 7)):
            self._draw_finder_pattern(fx, fy)

        # Alignment patterns — skip the top-left one (already a finder) and
        # any position already occupied by a function module.
        positions = _ALIGNMENT_POS[self.version]
        for cy in positions:
            for cx in positions:
                if (cx, cy) == (6, 6):
                    continue
                if already[cy][cx]:
                    continue
                self._draw_alignment_pattern(cx, cy)

        # Timing patterns — only in the run between the finder separators,
        # and never overwriting an existing function module.
        for r in range(8, size - 8):
            if not already[r][6]:
                self._set_function_module(r, 6, r % 2 == 0)
        for c in range(8, size - 8):
            if not already[6][c]:
                self._set_function_module(6, c, c % 2 == 0)

        # Dark module (below the top-right finder).
        self._set_function_module(size - 8, 8, True)
        # Reserve the format and version info areas (marks them function so
        # data never lands there). Format bits are (re)drawn per-mask by the
        # caller, so the mask-0 values written here are just a placeholder;
        # version bits are value-independent and final for v>=7.
        self._draw_format_bits(0)
        self._draw_version()

    def _draw_finder_pattern(self, x, y):
        for dy in range(-1, 8):
            if not (0 <= y + dy < self.size):
                continue
            for dx in range(-1, 8):
                if not (0 <= x + dx < self.size):
                    continue
                if (
                    (0 <= dy <= 6 and dx in (0, 6))
                    or (0 <= dx <= 6 and dy in (0, 6))
                    or (2 <= dy <= 4 and 2 <= dx <= 4)
                ):
                    self._set_function_module(y + dy, x + dx, True)
                else:
                    self._set_function_module(y + dy, x + dx, False)

    def _draw_alignment_pattern(self, cx, cy):
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set_function_module(
                    cy + dy, cx + dx, max(abs(dx), abs(dy)) != 1
                )

    def _draw_format_bits(self, mask):
        """Draw the 15 format bits (EC level M + mask) in both copies."""
        size = self.size
        data = (_FORMAT_EC_BITS["M"] << 3) | mask
        rem = data
        for _i in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412  # 15 bits, masked
        assert 0 <= bits < (1 << 15)

        # Copy 1 — around the top-left finder.
        for i in range(0, 6):
            self._set_function_module(i, 8, self._get_bit(bits, i))
        self._set_function_module(7, 8, self._get_bit(bits, 6))
        self._set_function_module(8, 8, self._get_bit(bits, 7))
        self._set_function_module(8, 7, self._get_bit(bits, 8))
        for i in range(9, 15):
            self._set_function_module(8, 15 - i - 1, self._get_bit(bits, i))

        # Copy 2 — right column + bottom row.
        for i in range(0, 8):
            self._set_function_module(8, size - 1 - i, self._get_bit(bits, i))
        for i in range(8, 15):
            self._set_function_module(size - 15 + i, 8, self._get_bit(bits, i))

        # Fixed dark module (below the top-right finder) — always dark.
        self._set_function_module(size - 8, 8, True)

    def _draw_version(self):
        """Draw the 18 version bits, if version >= 7 (both copies)."""
        if self.version < 7:
            return
        rem = self.version
        for _i in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem  # 18 bits
        assert bits >> 18 == 0

        for i in range(18):
            bit = self._get_bit(bits, i)
            a = self.size - 11 + i % 3
            b = i // 3
            self._set_function_module(b, a, bit)     # top-right
            self._set_function_module(a, b, bit)     # bottom-left

    @staticmethod
    def _get_bit(x, i):
        """Bit ``i`` of ``x``, 0 = least significant."""
        return ((x >> i) & 1) != 0

    # -- data codewords ---------------------------------------------------

    def _encode_data(self, data: bytes) -> bytes:
        """Byte-mode bit encoding: mode + count + data + terminator + padding."""
        size = self.version
        capacity = _DATA_CODEWORDS_M[size]

        # char count bits: 8 for versions 1-9, 16 for 10+ (we cap at 10).
        cc_bits = 8 if size <= 9 else 16
        bits = []
        # mode indicator for byte mode = 0100
        bits += [0, 1, 0, 0]
        # character count
        for i in range(cc_bits - 1, -1, -1):
            bits.append((len(data) >> i) & 1)
        # data
        for b in data:
            for i in range(7, -1, -1):
                bits.append((b >> i) & 1)

        # Terminator: up to 4 zero bits, but only as many as fit.
        max_bits = capacity * 8
        bits += [0] * min(4, max_bits - len(bits))

        # Pad to a byte boundary, then pad bytes 0xEC / 0x11 alternately.
        bits += [0] * ((-len(bits)) % 8)
        pad = 0xEC
        while len(bits) < max_bits:
            for i in range(7, -1, -1):
                bits.append((pad >> i) & 1)
            pad ^= 0xEC ^ 0x11  # toggle between 0xEC and 0x11

        # Convert the (final, full) bitstring into codewords.
        codewords = bytearray()
        for i in range(0, max_bits, 8):
            byte = 0
            for j in range(8):
                byte = (byte << 1) | bits[i + j]
            codewords.append(byte)
        return bytes(codewords)

    def _add_ecc_and_interleave(self, data_codewords: bytes, _orig_data: bytes):
        """Split data into blocks, compute ECC per block, interleave all."""
        shapes = _BLOCK_TABLE_M[self.version]
        # Expand (data, ec, repeat) into a flat list of (data_count, ec_count).
        blocks = []
        for d, e, r in shapes:
            blocks.extend([(d, e)] * r)
        ec_len = blocks[0][1]

        # Split the data codewords into per-block slices in the same order the
        # spec expects. Blocks with more data come first (mixed-size versions).
        data_per_block = [d for d, _e in blocks]
        offset = 0
        split = []
        divisor = _rs_compute_divisor(ec_len)
        for dcount in data_per_block:
            chunk = list(data_codewords[offset:offset + dcount])
            offset += dcount
            ecc = _rs_compute_remainder(chunk, divisor)
            split.append((chunk, ecc))

        result = []
        max_data_len = max(d for d, _e in blocks)
        for i in range(max_data_len):
            for chunk, _ecc in split:
                if i < len(chunk):
                    result.append(chunk[i])
        for i in range(ec_len):
            for _chunk, ecc in split:
                result.append(ecc[i])
        return bytes(result)

    # -- matrix fill ------------------------------------------------------

    def _num_data_modules(self) -> int:
        """Total number of modules available for data (raw modules − function)."""
        return sum(
            1 for y in range(self.size) for x in range(self.size)
            if not self.is_function[y][x]
        )

    def _draw_data_codewords(self, all_codewords: bytes):
        """Write the interleaved codeword bitstream into the data modules."""
        max_bits = self._num_data_modules()
        data_bits = []
        for b in all_codewords:
            for i in range(7, -1, -1):
                data_bits.append((b >> i) & 1)
        # Truncate any excess, or pad with remainder bits (zeros) to fill the
        # data-module capacity exactly.
        if len(data_bits) > max_bits:
            data_bits = data_bits[:max_bits]
        elif len(data_bits) < max_bits:
            data_bits += [0] * (max_bits - len(data_bits))
        self._apply_mask_function(bits=data_bits, mask=-1, write=True)

    def _apply_mask_function(self, bits, mask, write=False):
        """Apply a data mask (or write raw bits when ``write``).

        When ``write`` is False, toggles data modules with the mask (used both
        to apply a mask and, with the same values, to undo it). When ``write``
        is True, writes ``bits`` into the data-module zigzag and does NOT
        treat them as mask-application (used once for the final codewords).
        """
        size = self.size
        i = 0
        for right in range(size - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (size - 1 - vert) if upward else vert
                    if not self.is_function[y][x]:
                        if write:
                            self.modules[y][x] = bool(bits[i])
                            i += 1
                        else:
                            self.modules[y][x] ^= self._mask_bit(mask, x, y)

    @staticmethod
    def _mask_bit(mask, x, y):
        """The 8 standard data-mask predicates."""
        if mask == 0:
            return (x + y) % 2 == 0
        if mask == 1:
            return y % 2 == 0
        if mask == 2:
            return x % 3 == 0
        if mask == 3:
            return (x + y) % 3 == 0
        if mask == 4:
            return (y // 2 + x // 3) % 2 == 0
        if mask == 5:
            return (x * y) % 2 + (x * y) % 3 == 0
        if mask == 6:
            return ((x * y) % 2 + (x * y) % 3) % 2 == 0
        if mask == 7:
            return ((x + y) % 2 + (x * y) % 3) % 2 == 0
        raise ValueError(f"invalid mask {mask}")

    # -- penalty scoring --------------------------------------------------

    def _evaluate_penalty(self) -> int:
        size = self.size
        m = self.modules
        result = 0

        # N1: 3 + 1 per extra for runs of 5+ same-color modules (rows + cols).
        for row in range(size):
            run = 0
            prev = None
            for col in range(size):
                cur = m[row][col]
                if cur == prev:
                    run += 1
                    if run == 5:
                        result += 3
                    elif run > 5:
                        result += 1
                else:
                    run = 1
                prev = cur
        for col in range(size):
            run = 0
            prev = None
            for row in range(size):
                cur = m[row][col]
                if cur == prev:
                    run += 1
                    if run == 5:
                        result += 3
                    elif run > 5:
                        result += 1
                else:
                    run = 1
                prev = cur

        # N2: 3 for each 2x2 block of the same color.
        for y in range(size - 1):
            for x in range(size - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    result += 3

        # N3: 40 for each finder-like pattern 1011101 with 0000 on a side.
        for y in range(size):
            for x in range(size - 6):
                if (
                    m[y][x] and not m[y][x + 1] and m[y][x + 2]
                    and m[y][x + 3] and m[y][x + 4] and not m[y][x + 5]
                    and m[y][x + 6]
                ):
                    if self._finder_quiet(m, y, x, "row"):
                        result += 40
        for y in range(size - 6):
            for x in range(size):
                if (
                    m[y][x] and not m[y + 1][x] and m[y + 2][x]
                    and m[y + 3][x] and m[y + 4][x] and not m[y + 5][x]
                    and m[y + 6][x]
                ):
                    if self._finder_quiet(m, y, x, "col"):
                        result += 40

        # N4: 10 for each 5% step away from a 50/50 dark/light balance.
        dark = sum(1 for row in m for c in row if c)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total  # ceil
        result += k * 10

        return result

    @staticmethod
    def _finder_quiet(m, y, x, axis):
        """N3 rule: require 4 light modules immediately before or after the
        finder-like run in the same row/column."""
        size = len(m)
        before_ok = False
        after_ok = False
        if axis == "row":
            if x >= 4 and not any(m[y][x - 1 - i] for i in range(4)):
                before_ok = True
            if x + 10 < size and not any(m[y][x + 7 + i] for i in range(4)):
                after_ok = True
            # Also the run could be shifted; treat as → before_ok is the leading
            # side. We only need to check one adjacent run of 4 zeros.
            if x >= 4 and before_ok:
                return True
            if x + 10 < size and after_ok:
                return True
            return False
        # column
        if y >= 4 and not any(m[y - 1 - i][x] for i in range(4)):
            before_ok = True
        if y + 10 < size and not any(m[y + 7 + i][x] for i in range(4)):
            after_ok = True
        return before_ok or after_ok


# ---------------------------------------------------------------------------
# Terminal rendering
# ---------------------------------------------------------------------------

QUIET_ZONE = 4

# Unicode half-block characters.
_UPPER = "\u2580"   # ▀ upper half block
_LOWER = "\u2584"   # ▄ lower half block
_FULL = "\u2588"    # █ full block
# Pure-ASCII characters for the fallback (safe on encodings without unicode).
_ASCII_DARK = "#"


def _add_quiet_zone(matrix, quiet: int) -> list:
    """Return a copy of ``matrix`` surrounded by a ``quiet``-module light zone."""
    if quiet < 0:
        raise ValueError("quiet zone must be non-negative")
    size = len(matrix)
    new_size = size + 2 * quiet
    out = [[False] * new_size for _ in range(new_size)]
    for y in range(size):
        for x in range(size):
            out[y + quiet][x + quiet] = matrix[y][x]
    return out


def render_unicode(matrix, quiet: int = QUIET_ZONE) -> str:
    """Render a module matrix as unicode half-block lines (two rows per line).

    The QR is dark-on-light; a dark module is rendered filled. Each terminal
    line holds two module rows (upper row = U+2580, lower row = U+2584, both
    dark = U+2588), keeping the code square in a normal terminal. A ``quiet``
    light zone (default 4 modules) is added on all four sides.
    """
    m = _add_quiet_zone(matrix, quiet)
    size = len(m)
    lines = []
    upper_row = [""] * size
    lower_row = [""] * size
    for y in range(0, size, 2):
        upper = m[y]
        lower = m[y + 1] if y + 1 < size else [False] * size
        seg = []
        for x in range(size):
            top, bot = upper[x], lower[x]
            if top and bot:
                seg.append(_FULL)
            elif top:
                seg.append(_UPPER)
            elif bot:
                seg.append(_LOWER)
            else:
                seg.append(" ")
        lines.append("".join(seg))
    return "\n".join(lines)


def render_ascii(matrix, quiet: int = QUIET_ZONE) -> str:
    """Render a module matrix in pure-ASCII (each module = two chars wide).

    Light → two spaces, dark → two ASCII full blocks. One textual line per
    module row (half the vertical density of unicode), used when the terminal
    cannot render the unicode half-blocks.
    """
    m = _add_quiet_zone(matrix, quiet)
    size = len(m)
    lines = []
    for y in range(size):
        seg = []
        for x in range(size):
            seg.append("  " if not m[y][x] else _ASCII_DARK * 2)
        lines.append("".join(seg))
    return "\n".join(lines)


def _stdout_loves_unicode() -> bool:
    """True if the current stdout stream should be able to render the
    unicode half-blocks (UTF-8 or a Unicode-aware encoding)."""
    try:
        import sys
        enc = (sys.stdout.encoding or "").lower()
    except AttributeError:
        enc = ""
    if not enc:
        # Non-tty / unknown encoding: assume UTF-8 capable.
        return True
    return "utf" in enc or "unicode" in enc


def render_text(matrix, *, force_ascii: bool = False, quiet: int = QUIET_ZONE) -> str:
    """Render ``matrix`` for stdout, choosing unicode or ASCII automatically.

    ``force_ascii=True`` forces the ASCII fallback (used to prove the ASCII
    path in tests independent of the host encoding).
    """
    if force_ascii or not _stdout_loves_unicode():
        return render_ascii(matrix, quiet=quiet)
    return render_unicode(matrix, quiet=quiet)
