"""A QR code for a short text (the Phone Camera's address), drawn by SprigCast itself: no library to ship.

Byte mode, error correction M (a code can lose about 15% and still read), versions 1 to 10 (up to 213 bytes).
Written from the QR code standard (ISO/IEC 18004); the tests read every code back with OpenCV's reader.
"""

from __future__ import annotations

# Error correction M per version: (codewords per block of error correction, [(blocks, data codewords each)...]).
_BLOCKS_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}
_M_BITS = 0  # error correction M in the format bits

# GF(256) with x^8 + x^4 + x^3 + x^2 + 1
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    gen = [1]
    for i in range(degree):  # (x - a^0)(x - a^1)...(x - a^(degree-1))
        gen = [a ^ _mul(b, _EXP[i]) for a, b in zip(gen + [0], [0] + gen)]
    rem = list(data) + [0] * degree
    for i in range(len(data)):
        factor = rem[i]
        if factor:
            for j, g in enumerate(gen):
                rem[i + j] ^= _mul(g, factor)
    return rem[len(data):]


def _capacity(version: int) -> int:
    return sum(n * size for n, size in _BLOCKS_M[version][1])


def _version_for(n: int) -> int:
    for v in range(1, 11):
        header = 4 + (8 if v < 10 else 16)
        if header + 8 * n <= 8 * _capacity(v):
            return v
    raise ValueError(f"too long for a QR code here: {n} bytes")


def _codewords(data: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in range(length - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), 8 if version < 10 else 16)
    for byte in data:
        put(byte, 8)
    room = 8 * _capacity(version)
    put(0, min(4, room - len(bits)))
    put(0, -len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0
    while len(words) < _capacity(version):
        words.append((0xEC, 0x11)[pad % 2])
        pad += 1
    ec_len, groups = _BLOCKS_M[version]
    blocks, at = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[at:at + size])
            at += size
    ecs = [_rs_remainder(b, ec_len) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_len):
        out += [e[i] for e in ecs]
    return out


def _mask_bit(mask: int, x: int, y: int) -> bool:
    return [
        (x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0, (x // 3 + y // 2) % 2 == 0,
        x * y % 2 + x * y % 3 == 0, (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0,
    ][mask]


class _Grid:
    def __init__(self, version: int):
        self.version = version
        self.size = n = 17 + 4 * version
        self.dark = [[False] * n for _ in range(n)]
        self.fixed = [[False] * n for _ in range(n)]

    def set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def patterns(self) -> None:
        n = self.size
        for i in range(n):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        d = max(abs(dx), abs(dy))
                        self.set(x, y, d not in (2, 4))
        spots = _ALIGN[self.version]
        for i, ax in enumerate(spots):
            for j, ay in enumerate(spots):
                if (i, j) in ((0, 0), (0, len(spots) - 1), (len(spots) - 1, 0)):
                    continue  # under a finder
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)  # reserved now, written for real once the mask is known
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = bool((bits >> i) & 1)
                a, b = n - 11 + i % 3, i // 3
                self.set(a, b, bit)
                self.set(b, a, bit)

    def format_bits(self, mask: int) -> None:
        n = self.size
        data = _M_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        def bit(i: int) -> bool:
            return bool((bits >> i) & 1)

        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, n - 15 + i, bit(i))
        self.set(8, n - 8, True)

    def place(self, words: list[int]) -> None:
        n, i, total = self.size, 0, len(words) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fixed[y][x]:
                        if i < total:
                            self.dark[y][x] = bool((words[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def masked(self, mask: int) -> list[list[bool]]:
        n = self.size
        return [[self.dark[y][x] ^ (not self.fixed[y][x] and _mask_bit(mask, x, y)) for x in range(n)]
                for y in range(n)]


def _penalty(m: list[list[bool]]) -> int:
    n, score = len(m), 0
    for lines in (m, [list(col) for col in zip(*m)]):
        for line in lines:
            run = 1
            for a, b in zip(line, line[1:]):
                if a == b:
                    run += 1
                else:
                    score += run - 2 if run >= 5 else 0
                    run = 1
            score += run - 2 if run >= 5 else 0
    for y in range(n - 1):
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))
    score += abs(dark * 20 - n * n * 10) // (n * n) * 10
    return score


def encode(text: str) -> list[list[bool]]:
    """The code for `text` (UTF-8) as rows of modules, True for dark, without the quiet zone around it."""
    data = text.encode("utf-8")
    version = _version_for(len(data))
    words = _codewords(data, version)
    best = None
    for mask in range(8):
        grid = _Grid(version)
        grid.patterns()
        grid.place(words)
        grid.format_bits(mask)
        m = grid.masked(mask)
        score = _penalty(m)
        if best is None or score < best[0]:
            best = (score, m)
    return best[1]
