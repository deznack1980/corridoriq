"""Minimal QR Code encoder (stdlib only) for public referral URLs.

Byte mode, versions 1-6, error-correction level M (falling back to L for
longer text), automatic mask selection. Output is an SVG. Follows the
well-known reference construction (finder/timing/alignment patterns, BCH
format bits, Reed-Solomon over GF(256) with polynomial 0x11D, block
interleaving, zig-zag placement, eight masks with penalty scoring).

The QR payload is only the URL it is given; callers pass a public referral
URL, never credentials or customer data.
"""

from __future__ import annotations

# (ecc codewords per block, number of blocks) by version, for L and M.
_ECC = {
    "L": {1: (7, 1), 2: (10, 1), 3: (15, 1), 4: (20, 1), 5: (26, 1), 6: (18, 2)},
    "M": {1: (10, 1), 2: (16, 1), 3: (26, 1), 4: (18, 2), 5: (24, 2), 6: (16, 4)},
}
_FORMAT_BITS = {"L": 1, "M": 0}
MAX_VERSION = 6


def _raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
    return result


def _data_capacity(ver: int, ecl: str) -> int:
    ecc, blocks = _ECC[ecl][ver]
    return _raw_modules(ver) // 8 - ecc * blocks


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _encode_data(payload: bytes, ver: int, ecl: str) -> list[int]:
    bits: list[int] = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)                 # byte mode
    put(len(payload), 8)           # character count (versions 1-9)
    for b in payload:
        put(b, 8)
    cap_bits = _data_capacity(ver, ecl) * 8
    put(0, min(4, cap_bits - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < cap_bits:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    return [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]


def _add_ecc_and_interleave(data: list[int], ver: int, ecl: str) -> list[int]:
    ecc_len, num_blocks = _ECC[ecl][ver]
    raw = _raw_modules(ver) // 8
    num_short = num_blocks - raw % num_blocks
    short_len = raw // num_blocks
    div = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(num_blocks):
        n = short_len - ecc_len + (0 if i < num_short else 1)
        dat = data[k:k + n]
        k += n
        ecc = _rs_remainder(dat, div)
        if i < num_short:
            dat = dat + [0]
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= num_short:
                out.append(blk[i])
    return out


class _Matrix:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def setf(self, x, y, dark):
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def function_patterns(self):
        s = self.size
        for i in range(s):
            self.setf(6, i, i % 2 == 0)
            self.setf(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self.setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        if self.ver >= 2:
            p = s - 7
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    self.setf(p + dx, p + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits("M", 0)  # reserve; overwritten after masking

    def format_bits(self, ecl, mask):
        data = _FORMAT_BITS[ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        s = self.size
        for i in range(6):
            self.setf(8, i, bit(i))
        self.setf(8, 7, bit(6))
        self.setf(8, 8, bit(7))
        self.setf(7, 8, bit(8))
        for i in range(9, 15):
            self.setf(14 - i, 8, bit(i))
        for i in range(8):
            self.setf(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.setf(8, s - 15 + i, bit(i))
        self.setf(8, s - 8, True)

    def codewords(self, data: list[int]):
        s, i = self.size, 0
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = s - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.mod[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        for y in range(self.size):
            for x in range(self.size):
                if self.fn[y][x]:
                    continue
                inv = (
                    (x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0,
                    (x // 3 + y // 2) % 2 == 0, x * y % 2 + x * y % 3 == 0,
                    (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0,
                )[mask]
                if inv:
                    self.mod[y][x] = not self.mod[y][x]

    def penalty(self) -> int:
        s, m, score = self.size, self.mod, 0
        lines = [m[y] for y in range(s)] + [[m[y][x] for y in range(s)] for x in range(s)]
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
            if run >= 5:
                score += run - 2
            txt = "".join("1" if v else "0" for v in line)
            for pat in ("10111010000", "00001011101"):
                score += 40 * txt.count(pat)
        for y in range(s - 1):
            for x in range(s - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(v for row in m for v in row)
        total = s * s
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return score + max(k, 0) * 10


def matrix(text: str) -> list[list[bool]]:
    payload = text.encode("utf-8")
    for ecl in ("M", "L"):
        for ver in range(1, MAX_VERSION + 1):
            if len(payload) + 2 <= _data_capacity(ver, ecl) and len(payload) <= 255:
                return _build(payload, ver, ecl)
    raise ValueError("text too long for a version 1-6 QR code")


def _build(payload: bytes, ver: int, ecl: str) -> list[list[bool]]:
    codewords = _add_ecc_and_interleave(_encode_data(payload, ver, ecl), ver, ecl)
    best, best_score = None, None
    for mask in range(8):
        mtx = _Matrix(ver)
        mtx.function_patterns()
        mtx.codewords(codewords)
        mtx.apply_mask(mask)
        mtx.format_bits(ecl, mask)
        score = mtx.penalty()
        if best_score is None or score < best_score:
            best, best_score = mtx, score
    return best.mod


def svg(text: str, *, scale: int = 8, border: int = 4) -> str:
    mod = matrix(text)
    n = len(mod)
    dim = (n + border * 2) * scale
    path = "".join(f"M{(x + border) * scale},{(y + border) * scale}h{scale}v{scale}h-{scale}z"
                   for y in range(n) for x in range(n) if mod[y][x])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" height="{dim}" viewBox="0 0 {dim} {dim}" '
            f'shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')
