#!/usr/bin/env python3
"""Write user/os/etc/bmp/badge.bmp: a sprite with a genuinely soft edge.

    python3 tools/make_badge.py

The disc that `graphics -sprite` draws. It exists because nothing else in
/etc/bmp has an alpha channel -- pigeon.bmp and eye-mask.bmp are 24-bit, and
a mask is binary -- so there was no picture on the machine whose edge was
soft enough to show what GAC_SRC_ALPHA does
(docs/gac/plans/phase9_srcalpha.md).

A 32-bit BMP with the usual bit fields, which is what <pigeon/bmp.h> reads
and what an editor saves. Kept as a generator rather than a hand-drawn file
so the shape is readable and can be changed without a paint program.
"""
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "user" / "os" / "etc" / "bmp" / "badge.bmp"
SIZE = 64
BGRA_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)


def coverage(x, y, cx, cy, r):
    """How much of this pixel the disc covers, 0.0 to 1.0: the distance to
    the edge, clamped to one pixel either side. One pixel of softness is
    what an antialiased edge is, and it is what the device's fast path is
    built for -- a thin rim, an opaque core."""
    dist = ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5
    return max(0.0, min(1.0, r - dist + 0.5))


def pixels():
    """Top row first: (b, g, r, a) a pixel."""
    cx = cy = SIZE / 2.0
    out = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            outer = coverage(x, y, cx, cy, SIZE / 2.0 - 1.0)
            inner = coverage(x, y, cx, cy, SIZE / 2.0 - 6.0)
            # A blue disc with a lighter ring around it, so a wrong blend
            # shows as a colour and not only as a hard edge.
            r, g, b = (0x30, 0x80, 0xE0) if inner > 0.5 else (0xA0, 0xD0, 0xFF)
            row.append((b, g, r, int(round(outer * 255))))
        out.append(row)
    return out


def bmp(rows):
    body = b"".join(bytes(v for px in row for v in px) for row in reversed(rows))
    info = struct.pack("<IiiHHIIiiII", 108, SIZE, SIZE, 1, 32, 3, len(body),
                       2835, 2835, 0, 0)
    fields = struct.pack("<IIII", *BGRA_MASKS) + b"BGRs" + bytes(108 - 40 - 20)
    offset = 14 + len(info) + len(fields)
    return (struct.pack("<2sIHHI", b"BM", offset + len(body), 0, 0, offset)
            + info + fields + body)


def main():
    data = bmp(pixels())
    OUT.write_bytes(data)
    soft = sum(1 for row in pixels() for px in row if 0 < px[3] < 255)
    print(f"{OUT.relative_to(REPO_ROOT)}: {len(data):,} B, {SIZE}x{SIZE}, "
          f"{soft} pixels with a partial alpha "
          f"({100.0 * soft / (SIZE * SIZE):.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
