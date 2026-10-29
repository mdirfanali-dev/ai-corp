"""Draws the AICorp icon and writes desktop/assets/aicorp.ico. Pure Python, no extra packages.

The idea: a company org chart that is also the letter A. One amber node on top (the founder: warm, the point the eye goes to), two
lighter nodes below (the AI staff), joined by lines. Deep blue (trust, calm) behind it; few shapes, so it still reads at 16 px.

Run: python make_icon.py   (the generated .ico is committed; this script only exists so the icon can be reproduced or changed)
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZES = (256, 64, 48, 32, 24, 16)
BG_TOP, BG_BOTTOM = (38, 98, 208), (16, 40, 112)       # trust blue -> deep navy
NODE = (255, 255, 255)
FOUNDER = (255, 184, 48)                               # warm amber: the one thing the eye lands on
LINE = (214, 232, 255)


def inside_round_square(x: float, y: float, r: float = 0.22) -> bool:
    if not (0 <= x <= 1 and 0 <= y <= 1):
        return False
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


TOP, LEFT, RIGHT = (0.5, 0.26), (0.27, 0.74), (0.73, 0.74)


def colour_at(x: float, y: float):
    """Colour of the picture at one point, or None for transparent."""
    if not inside_round_square(x, y):
        return None
    d_top = ((x - TOP[0]) ** 2 + (y - TOP[1]) ** 2) ** 0.5
    if d_top <= 0.135:
        return FOUNDER
    if min(((x - LEFT[0]) ** 2 + (y - LEFT[1]) ** 2) ** 0.5, ((x - RIGHT[0]) ** 2 + (y - RIGHT[1]) ** 2) ** 0.5) <= 0.105:
        return NODE
    legs = min(seg_dist(x, y, *TOP, *LEFT), seg_dist(x, y, *TOP, *RIGHT))
    bar = seg_dist(x, y, *LEFT, *RIGHT)
    if legs <= 0.036 or bar <= 0.036:
        return LINE
    return tuple(BG_TOP[k] + (BG_BOTTOM[k] - BG_TOP[k]) * y for k in range(3))


def render(size: int, ss: int = 4) -> bytes:
    rows = []
    for j in range(size):
        row = bytearray([0])                               # PNG filter type 0
        for i in range(size):
            r = g = b = a = 0.0
            for sj in range(ss):
                for si in range(ss):
                    col = colour_at((i + (si + 0.5) / ss) / size, (j + (sj + 0.5) / ss) / size)
                    if col:
                        r, g, b, a = r + col[0], g + col[1], b + col[2], a + 1
            n = ss * ss
            row += bytes([int(r / a) if a else 0, int(g / a) if a else 0, int(b / a) if a else 0, int(255 * a / n)])
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main() -> None:
    images = [(s, render(s)) for s in SIZES]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    body = b""
    for size, png in images:
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(body))
        body += png
    target = Path(__file__).with_name("aicorp.ico")
    target.write_bytes(out + body)
    print(f"wrote {target.name} ({len(out + body)} bytes, sizes {SIZES})")


if __name__ == "__main__":
    main()
