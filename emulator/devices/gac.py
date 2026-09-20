"""The graphics accelerator: drawing done by the host, on surfaces in video
memory or in RAM (docs/gac/plans/phase3_gac.md, design in
docs/gac/design.md §5.3).

A guest clearing a 1280 x 720 screen a store at a time runs 3.7 million
instructions; this device does it with one slice assignment per row. That
ratio is what makes a big screen affordable, and text is where it matters
most: a full console redraw at 720p is about 34 seconds of guest code and
about 12 ms here.

  callback(read_write, command, length, address, data)

  cmd  name         window in                                   reply
  0    NOP          --                                          0
  1    INFO         --                                          magic, features, window bytes
  2    FILL         dst, x, y, w, h, colour                     1 / 0
  3    FRAME        dst, x, y, w, h, colour                     1 / 0
  4    BLIT         src, sx, sy, dst, dx, dy, w, h              1 / 0
  5    BLIT_SCALED  src, sx, sy, sw, sh, dst, dx, dy, dw, dh    1 / 0
  6    LINE         dst, x0, y0, x1, y1, colour                 1 / 0
  7    CIRCLE       dst, cx, cy, r, colour                      1 / 0
  8    DISC         dst, cx, cy, r, colour                      1 / 0
  9    SET_FONT     address, glyph_w, glyph_h, cell_w, cell_h,  1 / 0
                    first, count
  10   TEXT         dst, x, y, fg, bg, length, then the bytes   1 / 0
  11   SCROLL       dst, x, y, w, h, dy, bg                     1 / 0
  12   BATCH        count, then records: cmd, nwords, args...   ran, refused
  13   DAMAGE       --                                          0 (reserved)
  14   BLIT_ALPHA   src, sx, sy, dst, dx, dy, w, h, alpha       1 / 0
                    alpha 0..255, or SRC_ALPHA (256) for each
                    source pixel's own
  15   RAM_SURFACE  address, w, h                               handle, or 0
  16   RAM_FREE     -- (the handle in ADDRESS)                  1 / 0

Every argument is a little-endian word in the data window, read with R/W 0
as CH_VRAM's are. Coordinates are signed and everything is clipped here, in
the device: whatever the guest asks for, nothing is written outside the
surface it names. 0 means the surface was not there (or, for LINE, CIRCLE
and DISC, was absurdly big, or a scaled blit's source was not wholly in
its surface); a shape clipped to nothing is still a 1.

**Surfaces.** A handle is VRAM's: 0 is the screen of the current mode, and
the rest are what CH_VRAM's ALLOC handed out. Their geometry is looked up
in the VRAM device on every command and never copied, so a FREE or a mode
change cannot leave the GAC drawing into memory handed to someone else. Or
a handle is a RAM surface, 0x80000000 and up, registered with RAM_SURFACE
and checked once, then: wholly inside RAM and at or above
PROGRAM_LOAD_ADDR, or exactly DISPLAY_START with no more than a screen of
bytes -- the power-on screen, which the kernel console draws on.

**The same pixels as lib/pigeon/display.c.** Phase 5 routes that library
here, so each primitive is its algorithm, not a similar one: the same
Bresenham, the same midpoint circle, spans for a disc, the 5x7 font's top
bits. Where display.c stores a pixel twice -- a disc's rows, a circle's
octant joins, a frame's corners -- this draws it once: identical while
colours are opaque, and right once they blend. tests/test_gac.py runs
display.c itself and compares the bytes.

**BATCH** checks every record's shape before drawing anything -- a record
it does not know, a BATCH inside it, or one running off the window refuses
the lot -- then runs them in order, skipping and counting any that fail.

**Text** is drawn a pixel row at a time across the whole string: the
glyphs' row masks, precomputed by SET_FONT, joined into one big integer,
and `(dst & ~mask) | (fg & mask)`. Eight big-integer operations a line of
text, not eight per glyph.

**Blending.** Every command with a colour blends on its alpha byte:
0xFF... and 0x00... are stored exactly as before, and anything between
lays the colour over what is there,
`d' = (d * (255 - a) + s * a + 127) / 255` per channel, rounded to nearest
so a colour over itself is itself. Alpha 0 is stored, not "invisible":
the screen ignores alpha (docs/gac/plans/phase4_frontends.md), and code
has always cleared to 0 meaning black -- bios2's disp_clear(0) did
nothing at all while alpha 0 drew nothing (docs/gac/plans/
phase5_display_lib.md). The one "nothing" is TEXT's background, where
alpha 0 means no background. The destination keeps its own alpha
byte -- the scanout ignores alpha, and three channels is a cheaper table
than four. A blend has to read every byte it writes, so it cannot be a
slice assignment; per channel it is a 256-entry `bytes.translate` table
instead, built once per command. BLIT copies bytes verbatim, alpha and
all; BLIT_ALPHA lays a whole rectangle over another at one alpha, with the
arithmetic done on a row at a time as one big integer (SWAR). BLIT_ALPHA at
SRC_ALPHA takes each source pixel's own alpha instead: no table and no
single multiply fits that, but almost none of a real sprite needs one --
97.6% of an antialiased one is opaque or wholly transparent -- so
src_alpha_row cuts each row into a margin it skips, a core it copies and a
rim it blends (docs/gac/plans/phase9_srcalpha.md).

The GAC writes the surface buffers directly, as CH_DISPLAY's FILL does. It
does not set ram.vram_dirty: that flag means "written through the
aperture, damage unknown", and Phase 8 has the GAC report its own damage.
"""
import logging
import struct
import weakref
from typing import Dict, List, Optional, Tuple

from ..memory_map import (
    DISPLAY_SIZE, DISPLAY_START, IO_SIZE, IO_START, IOHeader, PROGRAM_LOAD_ADDR,
)

log = logging.getLogger(__name__)

CMD_NOP = 0
CMD_INFO = 1
CMD_FILL = 2
CMD_FRAME = 3
CMD_BLIT = 4
CMD_BLIT_SCALED = 5
CMD_LINE = 6
CMD_CIRCLE = 7
CMD_DISC = 8
CMD_SET_FONT = 9
CMD_TEXT = 10
CMD_SCROLL = 11
CMD_BATCH = 12
CMD_DAMAGE = 13
CMD_BLIT_ALPHA = 14
CMD_RAM_SURFACE = 15
CMD_RAM_FREE = 16

#: "PGGA" in byte order, the convention of CH_VRAM's VRAM_MAGIC.
GAC_MAGIC = 0x41474750
FEATURE_TEXT = 1
FEATURE_BLEND = 2
FEATURE_SRC_ALPHA = 4
#: BLIT_ALPHA's alpha word, meaning "each source pixel's own alpha" instead
#: of one alpha for the whole rectangle. 256 because every alpha above 255
#: was refused before this existed, so an emulator built without it answers
#: 0 rather than not knowing the command at all (docs/gac/plans/
#: phase9_srcalpha.md §3).
SRC_ALPHA = 256
#: RAM surfaces' handles start here, so they can never be a VRAM handle.
RAM_HANDLE = 0x80000000
#: The longest line, the biggest radius: a guest asking for a line 2**31
#: pixels long would hold the emulator for hours, walking pixels nobody
#: sees. 131,072 is a hundred times the widest screen.
MAX_EXTENT = 1 << 17

_WINDOW_BASE = IO_START + IOHeader.USABLE_AFTER
WINDOW_BYTES = IO_SIZE - IOHeader.USABLE_AFTER
_OK, _NO = struct.pack("<I", 1), struct.pack("<I", 0)

# Each drawing command's arguments: the struct format, and so the word
# count BATCH checks a record against. TEXT's string follows its six words.
_ARGS = {
    CMD_FILL: "<IiiiiI",
    CMD_FRAME: "<IiiiiI",
    CMD_BLIT: "<IiiIiiii",
    CMD_BLIT_SCALED: "<IiiiiIiiii",
    CMD_LINE: "<IiiiiI",
    CMD_CIRCLE: "<IiiiI",
    CMD_DISC: "<IiiiI",
    CMD_SET_FONT: "<IIIIIII",
    CMD_TEXT: "<IiiIII",
    CMD_SCROLL: "<IiiiiiI",
    CMD_BLIT_ALPHA: "<IiiIiiiiI",
}
_STRUCTS = {cmd: struct.Struct(fmt) for cmd, fmt in _ARGS.items()}


class Target:
    """A surface resolved for drawing: its buffer, where it starts in it,
    and its size in pixels. The pitch is always width * 4."""
    __slots__ = ("buf", "base", "w", "h")

    def __init__(self, buf, base: int, w: int, h: int):
        self.buf, self.base, self.w, self.h = buf, base, w, h

    def at(self, x: int, y: int) -> int:
        return self.base + (y * self.w + x) * 4


class Ink:
    """How a colour lands on a surface: `span` for a run of pixels, `pixel`
    for one, `over` for what a row of bytes becomes under it. This one is
    opaque -- the colour's bytes are stored. `ink()` picks which kind a
    colour needs, so no primitive knows which it has."""
    __slots__ = ("word",)

    def __init__(self, colour: int):
        self.word = (colour & 0xFFFFFFFF).to_bytes(4, "little")

    def span(self, buf, offset: int, pixels: int) -> None:
        buf[offset:offset + pixels * 4] = self.word * pixels

    def pixel(self, buf, offset: int) -> None:
        buf[offset:offset + 4] = self.word

    def over(self, row: bytes) -> bytes:
        return self.word * (len(row) // 4)


class ClearInk:
    """Nothing lands: TEXT's background when its alpha is 0."""
    __slots__ = ()

    def span(self, buf, offset: int, pixels: int) -> None:
        pass

    def pixel(self, buf, offset: int) -> None:
        pass

    def over(self, row: bytes) -> bytes:
        return bytes(row)


def blend_table(source: int, alpha: int) -> bytes:
    """What each destination byte becomes with `source` laid over it at
    `alpha`: one channel's whole blend as a 256-entry translate table."""
    keep = 255 - alpha
    return bytes((d * keep + source * alpha + 127) // 255 for d in range(256))


class BlendInk:
    """0 < alpha < 255: the colour over what is there. One translate table
    per channel, B, G and R, since a fixed colour makes the blend a map of
    one byte to one byte; the alpha byte is left alone."""
    __slots__ = ("tables",)

    def __init__(self, colour: int):
        alpha = (colour >> 24) & 0xFF
        self.tables = (blend_table(colour & 0xFF, alpha),
                       blend_table((colour >> 8) & 0xFF, alpha),
                       blend_table((colour >> 16) & 0xFF, alpha))

    def over(self, row) -> bytes:
        out = bytearray(row)
        for channel, table in enumerate(self.tables):
            out[channel::4] = out[channel::4].translate(table)
        return bytes(out)

    def span(self, buf, offset: int, pixels: int) -> None:
        end = offset + pixels * 4
        buf[offset:end] = self.over(buf[offset:end])

    def pixel(self, buf, offset: int) -> None:
        b, g, r = self.tables
        buf[offset] = b[buf[offset]]
        buf[offset + 1] = g[buf[offset + 1]]
        buf[offset + 2] = r[buf[offset + 2]]


_CLEAR = ClearInk()


def ink(colour: int):
    """The ink a colour needs. Stored first, and untouched: every colour in
    the tree is 0xFF... or a plain 0, and none of them gets one step
    slower. Only an alpha from 1 to 254 blends."""
    alpha = (colour >> 24) & 0xFF
    if alpha == 0xFF or alpha == 0:
        return Ink(colour)
    return BlendInk(colour)


# SWAR constants for BLIT_ALPHA, for rows of up to MAX_ROW_PIXELS: a lane
# mask with a zero byte after each lane byte, so each product has 16 bits
# of room, and the constants the division needs, in every lane.
MAX_ROW_PIXELS = 1 << 12
_LANES = int.from_bytes(b"\xff\x00" * (MAX_ROW_PIXELS * 2), "little")
_ROUND = int.from_bytes(b"\x7f\x00" * (MAX_ROW_PIXELS * 2), "little")
_ONES = int.from_bytes(b"\x01\x00" * (MAX_ROW_PIXELS * 2), "little")
_ALPHA = int.from_bytes(b"\x00\x00\x00\xff" * MAX_ROW_PIXELS, "little")


def blend_rows(dst: bytes, src: bytes, alpha: int) -> bytes:
    """`src` over `dst` at one alpha, every byte at once: the bytes split
    into even and odd lanes, each lane set multiplied by one big-integer
    multiply, and divided by 255 as (x + 1 + (x >> 8)) >> 8 -- exact for
    every x a blend can make, 0..65152. The alpha bytes are dst's. Byte for
    byte what blend_table's formula gives."""
    n = len(dst)
    whole = (1 << (n * 8)) - 1
    lanes, rounding, ones = _LANES & whole, _ROUND & whole, _ONES & whole
    d = int.from_bytes(dst, "little")
    s = int.from_bytes(src, "little")
    out = 0
    for shift in (0, 8):
        x = ((d >> shift) & lanes) * (255 - alpha) + ((s >> shift) & lanes) * alpha + rounding
        out |= (((x + ones + ((x >> 8) & lanes)) >> 8) & lanes) << shift
    keep = _ALPHA & whole
    return ((out & ~keep) | (d & keep)).to_bytes(n, "little")


def blend_span(buf, at: int, src, x0: int, x1: int) -> None:
    """Pixels x0 to x1 of `src` over the row at `at`, each on its own alpha:
    blend_rows' formula a pixel at a time. This is the slow loop, and the
    point of src_alpha_row is to hand it as few pixels as possible."""
    for p in range(x0 * 4, x1 * 4, 4):
        a = src[p + 3]
        if a == 0:
            continue
        o = at + p
        if a == 255:
            buf[o:o + 3] = src[p:p + 3]
            continue
        inv = 255 - a
        for c in range(3):
            x = buf[o + c] * inv + src[p + c] * a + 127
            buf[o + c] = (x + 1 + (x >> 8)) >> 8


def src_alpha_row(buf, at: int, src) -> None:
    """One row of `src` over the row at `at`, each pixel on its own alpha.

    Not a blend of every pixel: a real sprite is 97.6% opaque or wholly
    transparent (docs/gac/plans/phase9_srcalpha.md §1), so the row is first
    cut into three by string searches over its alpha bytes, all at C speed.
    A transparent margin is not touched at all, a solid core is the slice
    copy BLIT does, and only the soft rim goes through blend_span. Measured
    at 0.39 ms for a 128 x 128 antialiased sprite against 5.65 ms for the
    naive loop, and 4.90 ms against 213 ms for a whole 720p screen.

    Where there is no solid core -- a gradient, a soft shadow -- the whole
    ink span goes to blend_span and this is the naive loop, no worse."""
    a = src[3::4]
    w = len(a)
    lo = w - len(a.lstrip(b"\x00"))
    if lo == w:                        # nothing on this row has any alpha
        return
    hi = len(a.rstrip(b"\x00"))
    first = a.find(b"\xff", lo, hi)
    if first >= 0:
        last = a.rfind(b"\xff", lo, hi) + 1
        if a.count(b"\xff", first, last) == last - first:
            # One unbroken run of opaque pixels: copy it, and put the
            # destination's own alpha bytes back -- every blend on this
            # device leaves them alone.
            s, e = first * 4, last * 4
            keep = buf[at + s + 3:at + e:4]
            buf[at + s:at + e] = src[s:e]
            buf[at + s + 3:at + e:4] = keep
            blend_span(buf, at, src, lo, first)
            blend_span(buf, at, src, last, hi)
            return
    blend_span(buf, at, src, lo, hi)


class Font:
    """SET_FONT's font, with every glyph row's mask precomputed as the
    bytes of one cell row: FF FF FF FF where there is ink, zeros where
    there is not. A cell row past the glyph's rows is all zeros."""

    def __init__(self, data: bytes, glyph_w: int, glyph_h: int, cell_w: int,
                 cell_h: int, first: int, count: int):
        self.cell_w, self.cell_h, self.first, self.count = cell_w, cell_h, first, count
        ink, none = b"\xff" * 4, bytes(4)
        blank = bytes(cell_w * 4)
        self.rows: List[List[bytes]] = []
        for g in range(count):
            glyph = []
            for r in range(cell_h):
                if r >= glyph_h:
                    glyph.append(blank)
                    continue
                bits = data[g * glyph_h + r]
                glyph.append(b"".join(ink if c < glyph_w and bits & (0x80 >> c) else none
                                      for c in range(cell_w)))
            self.rows.append(glyph)
        self.blank = [blank] * cell_h

    def row_mask(self, text: bytes, r: int) -> bytes:
        rows, first, count, blank = self.rows, self.first, self.count, self.blank[0]
        return b"".join([rows[c - first][r] if first <= c < first + count else blank
                         for c in text])


class GAC:
    def __init__(self, ram, vram):
        self.ram = ram
        self.vram = vram
        # Weak, as DisplayIO's link to the VRAM device is: the two point at
        # each other, and a cycle would keep the machine's RAM alive.
        vram.gac = weakref.proxy(self)
        # handle -> (address, w, h, owner): the owner as CH_VRAM's OWNER
        # said when it was registered, so FREE_OWNED frees these too.
        self.ram_surfaces: Dict[int, Tuple[int, int, int, int]] = {}
        self._next_ram = RAM_HANDLE + 1
        self.font: Optional[Font] = None

    # --- surfaces ---------------------------------------------------------

    def target(self, handle: int) -> Optional[Target]:
        if handle & RAM_HANDLE:
            found = self.ram_surfaces.get(handle)
            if found is None:
                return None
            address, w, h, _ = found
            return Target(self.ram.mem, address, w, h)
        surface = self.vram.surfaces.get(handle)
        if surface is None:
            return None
        return Target(self.ram.vram, surface.offset, surface.width, surface.height)

    def ram_surface(self, address: int, w: int, h: int) -> int:
        """Register a rectangle of RAM to draw on; 0 if it is not one the
        GAC may write. The rule is CH_DISPLAY's _valid_base and the HDD's
        DMA rule, so no surface can reach the BIOS, the IO window, or past
        the end of RAM -- where a slice assignment grows the bytearray."""
        size = w * h * 4
        if w <= 0 or h <= 0 or address & 3 or address + size > self.ram.size:
            return 0
        if address < PROGRAM_LOAD_ADDR and not (address == DISPLAY_START
                                                and size <= DISPLAY_SIZE):
            return 0
        handle = self._next_ram
        self._next_ram = handle + 1 if handle + 1 < 0xFFFFFFFF else RAM_HANDLE + 1
        self.ram_surfaces[handle] = (address, w, h, self.vram.owner)
        return handle

    def free_owned(self, owner: int) -> List[int]:
        """Forget every RAM surface registered by `owner` or deeper."""
        gone = [h for h, s in self.ram_surfaces.items() if s[3] >= owner]
        for handle in gone:
            del self.ram_surfaces[handle]
        return gone

    # --- shapes -------------------------------------------------------------

    @staticmethod
    def _rect(t: Target, ink: Ink, x: int, y: int, w: int, h: int) -> None:
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + w, t.w), min(y + h, t.h)
        if x0 >= x1 or y0 >= y1:
            return
        if x0 == 0 and x1 == t.w:
            ink.span(t.buf, t.at(0, y0), (y1 - y0) * t.w)     # whole rows: one slice
            return
        n = x1 - x0
        for row in range(y0, y1):
            ink.span(t.buf, t.at(x0, row), n)

    def _frame(self, t: Target, ink: Ink, x: int, y: int, w: int, h: int) -> None:
        """disp_frame's outline, each pixel once: the top and bottom rows
        whole, the sides without the corners."""
        if w <= 0 or h <= 0:
            return
        self._rect(t, ink, x, y, w, 1)
        if h > 1:
            self._rect(t, ink, x, y + h - 1, w, 1)
        if h > 2:
            self._rect(t, ink, x, y + 1, 1, h - 2)
            if w > 1:
                self._rect(t, ink, x + w - 1, y + 1, 1, h - 2)

    @staticmethod
    def _points(t: Target, ink: Ink, points) -> None:
        w, h, buf, base = t.w, t.h, t.buf, t.base
        for x, y in points:
            if 0 <= x < w and 0 <= y < h:
                ink.pixel(buf, base + (y * w + x) * 4)

    @staticmethod
    def _line_points(x0: int, y0: int, x1: int, y1: int) -> List[Tuple[int, int]]:
        """disp_line's Bresenham, step for step."""
        dx, dy = x1 - x0, y1 - y0
        sx = sy = 1
        if dx < 0:
            dx, sx = -dx, -1
        if dy < 0:
            dy, sy = -dy, -1
        err = dx - dy
        points = []
        while True:
            points.append((x0, y0))
            if x0 == x1 and y0 == y1:
                return points
            e2 = err + err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    @staticmethod
    def _octants(r: int):
        """disp_circle's midpoint loop: each (x, y) of the first octant."""
        x, y, err = r, 0, 1 - r
        while x >= y:
            yield x, y
            y += 1
            if err < 0:
                err += 2 * y + 1
            else:
                x -= 1
                err += 2 * (y - x) + 1

    def _circle(self, t: Target, ink: Ink, cx: int, cy: int, r: int) -> None:
        points = set()
        for x, y in self._octants(r):
            points.update(((cx + x, cy + y), (cx + y, cy + x), (cx - y, cy + x),
                           (cx - x, cy + y), (cx - x, cy - y), (cx - y, cy - x),
                           (cx + y, cy - x), (cx + x, cy - y)))
        self._points(t, ink, points)

    def _disc(self, t: Target, ink: Ink, cx: int, cy: int, r: int) -> None:
        """disp_disc's spans, each row once at its widest: every span of a
        row is centred on cx, so the widest covers the rest."""
        half: Dict[int, int] = {}
        for x, y in self._octants(r):
            for row, reach in ((cy + y, x), (cy - y, x), (cy + x, y), (cy - x, y)):
                if half.get(row, -1) < reach:
                    half[row] = reach
        for row, reach in half.items():
            self._rect(t, ink, cx - reach, row, 2 * reach + 1, 1)

    # --- copies ---------------------------------------------------------------

    @staticmethod
    def _clip_copy(src: Target, sx: int, sy: int, dst: Target, dx: int, dy: int,
                   w: int, h: int):
        """Shrink a copy until it fits both surfaces; None if nothing is left."""
        lo = min(sx, dx)
        if lo < 0:
            sx, dx, w = sx - lo, dx - lo, w + lo
        lo = min(sy, dy)
        if lo < 0:
            sy, dy, h = sy - lo, dy - lo, h + lo
        w = min(w, src.w - sx, dst.w - dx)
        h = min(h, src.h - sy, dst.h - dy)
        if w <= 0 or h <= 0:
            return None
        return sx, sy, dx, dy, w, h

    def _blit(self, src: Target, sx: int, sy: int, dst: Target, dx: int, dy: int,
              w: int, h: int) -> None:
        clipped = self._clip_copy(src, sx, sy, dst, dx, dy, w, h)
        if clipped is None:
            return
        sx, sy, dx, dy, w, h = clipped
        sbuf, dbuf = src.buf, dst.buf
        if w == src.w == dst.w:                        # whole rows: one slice
            s, d = src.at(0, sy), dst.at(0, dy)
            dbuf[d:d + w * h * 4] = sbuf[s:s + w * h * 4]
            return
        n = w * 4
        rows = range(h)
        if sbuf is dbuf and dst.at(dx, dy) > src.at(sx, sy):
            rows = reversed(rows)      # moving down: the last row first
        for i in rows:
            s, d = src.at(sx, sy + i), dst.at(dx, dy + i)
            dbuf[d:d + n] = sbuf[s:s + n]

    def _blit_alpha(self, src: Target, sx: int, sy: int, dst: Target, dx: int, dy: int,
                    w: int, h: int, alpha: int) -> bool:
        """BLIT, laid over the destination at one alpha -- or, at SRC_ALPHA,
        on each source pixel's own."""
        if alpha == SRC_ALPHA:
            return self._blit_src_alpha(src, sx, sy, dst, dx, dy, w, h)
        if alpha > 255:
            return False
        if alpha == 255:
            self._blit(src, sx, sy, dst, dx, dy, w, h)
            return True
        clipped = self._clip_copy(src, sx, sy, dst, dx, dy, w, h)
        if alpha == 0 or clipped is None:
            return True
        sx, sy, dx, dy, w, h = clipped
        sbuf, dbuf = src.buf, dst.buf
        rows = range(h)
        if sbuf is dbuf and dst.at(dx, dy) > src.at(sx, sy):
            rows = reversed(rows)
        for i in rows:
            s, d = src.at(sx, sy + i), dst.at(dx, dy + i)
            # A row wider than the SWAR constants goes in pieces.
            for part in range(0, w, MAX_ROW_PIXELS):
                n = min(MAX_ROW_PIXELS, w - part) * 4
                ps, pd = s + part * 4, d + part * 4
                dbuf[pd:pd + n] = blend_rows(dbuf[pd:pd + n], sbuf[ps:ps + n], alpha)
        return True

    def _blit_src_alpha(self, src: Target, sx: int, sy: int, dst: Target,
                        dx: int, dy: int, w: int, h: int) -> bool:
        """BLIT_ALPHA at SRC_ALPHA: every pixel on the alpha it carries.
        Always a 1 -- a sprite clipped to nothing is still done."""
        clipped = self._clip_copy(src, sx, sy, dst, dx, dy, w, h)
        if clipped is None:
            return True
        sx, sy, dx, dy, w, h = clipped
        sbuf, dbuf = src.buf, dst.buf
        n = w * 4
        rows = range(h)
        if sbuf is dbuf and dst.at(dx, dy) > src.at(sx, sy):
            rows = reversed(rows)
        for i in rows:
            s, d = src.at(sx, sy + i), dst.at(dx, dy + i)
            # The whole source row first: one copy, and overlapping source
            # and destination in one buffer cannot then read what this row
            # has already written.
            src_alpha_row(dbuf, d, sbuf[s:s + n])
        return True

    def _blit_scaled(self, src: Target, sx: int, sy: int, sw: int, sh: int,
                     dst: Target, dx: int, dy: int, dw: int, dh: int) -> bool:
        """Nearest neighbour with bmp.c's mapping, i * size / n: destination
        pixel i of n takes source pixel i * size / n. The destination clips;
        the source must lie wholly inside its surface, since clipping it
        would change which pixels the mapping picks."""
        if sx < 0 or sy < 0 or sw <= 0 or sh <= 0 or sx + sw > src.w or sy + sh > src.h:
            return False
        cols = range(max(0, -dx), min(dw, dst.w - dx))
        rows = range(max(0, -dy), min(dh, dst.h - dy))
        if not cols or not rows:
            return True
        picks = [sx + i * sw // dw for i in cols]
        x0 = dx + cols[0]
        n = len(cols) * 4
        sbuf, dbuf = src.buf, dst.buf
        last_row, last = None, b""
        for j in rows:
            row = sy + j * sh // dh
            if row != last_row:
                base = src.at(0, row)
                line = sbuf[base:base + src.w * 4]
                last = b"".join([line[p * 4:p * 4 + 4] for p in picks])
                last_row = row
            d = dst.at(x0, dy + j)
            dbuf[d:d + n] = last
        return True

    def _scroll(self, t: Target, x: int, y: int, w: int, h: int, dy: int,
                bg: int) -> None:
        """disp_scroll for a band: move its rows by dy, fill what is left."""
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + w, t.w), min(y + h, t.h)
        if x0 >= x1 or y0 >= y1 or dy == 0:
            return
        h = y1 - y0
        n = abs(dy)
        paint = ink(bg)
        if n >= h:
            self._rect(t, paint, x0, y0, x1 - x0, h)
            return
        kept = h - n
        if dy < 0:
            self._blit(t, x0, y0 + n, t, x0, y0, x1 - x0, kept)
            self._rect(t, paint, x0, y0 + kept, x1 - x0, n)
        else:
            self._blit(t, x0, y0, t, x0, y0 + n, x1 - x0, kept)
            self._rect(t, paint, x0, y0, x1 - x0, n)

    # --- text -----------------------------------------------------------------

    def _text(self, t: Target, x: int, y: int, fg: int, bg: int, text: bytes) -> None:
        font = self.font
        cw = font.cell_w
        # The visible columns of the string, in pixels from its left edge.
        left = max(0, -x)
        right = min(len(text) * cw, t.w - x)
        if left >= right:
            return
        lo, hi = left * 4, right * 4
        width = hi - lo
        # A background with alpha 0 is no background (Q3 of phase 3): the
        # one place alpha 0 means "nothing" rather than a colour to store.
        fg_ink = ink(fg)
        bg_ink = _CLEAR if (bg >> 24) & 0xFF == 0 else ink(bg)
        # An opaque colour's row is the same on every row: built once.
        fg_row = (int.from_bytes(fg_ink.over(bytes(width)), "little")
                  if isinstance(fg_ink, Ink) else None)
        buf = t.buf
        for r in range(font.cell_h):
            row = y + r
            if not 0 <= row < t.h:
                continue
            mask = int.from_bytes(font.row_mask(text, r)[lo:hi], "little")
            o = t.at(x + left, row)
            # The background over what is there (alpha 0: what is there),
            # then the ink over that, and the mask picks between the two.
            under = bg_ink.over(buf[o:o + width])
            over = fg_row if fg_row is not None else int.from_bytes(fg_ink.over(under),
                                                                    "little")
            under = int.from_bytes(under, "little")
            buf[o:o + width] = ((under & ~mask) | (over & mask)).to_bytes(width, "little")

    def _set_font(self, address: int, glyph_w: int, glyph_h: int, cell_w: int,
                  cell_h: int, first: int, count: int) -> bool:
        if not (0 < glyph_w <= 8 and 0 < glyph_h and cell_w >= glyph_w and cell_h >= glyph_h
                and 0 < count and first + count <= 256 and cell_w * cell_h <= 4096):
            return False
        size = count * glyph_h
        if address + size > self.ram.size:
            return False
        self.font = Font(bytes(self.ram.mem[address:address + size]),
                         glyph_w, glyph_h, cell_w, cell_h, first, count)
        return True

    # --- running a command ------------------------------------------------------

    def run(self, command: int, args) -> bool:
        """One drawing command on its argument words. False if a surface it
        names is not there, or its arguments are ones the GAC refuses."""
        a = _STRUCTS[command].unpack_from(args, 0)
        if command == CMD_SET_FONT:
            return self._set_font(*a)
        t = self.target(a[0])
        if t is None:
            return False
        if command == CMD_FILL:
            self._rect(t, ink(a[5]), *a[1:5])
        elif command == CMD_FRAME:
            self._frame(t, ink(a[5]), *a[1:5])
        elif command == CMD_LINE:
            x0, y0, x1, y1 = a[1:5]
            if max(abs(x1 - x0), abs(y1 - y0)) > MAX_EXTENT:
                return False
            self._points(t, ink(a[5]), self._line_points(x0, y0, x1, y1))
        elif command in (CMD_CIRCLE, CMD_DISC):
            _, cx, cy, r, colour = a
            if r > MAX_EXTENT:
                return False
            if r >= 0:
                (self._circle if command == CMD_CIRCLE else self._disc)(
                    t, ink(colour), cx, cy, r)
        elif command == CMD_SCROLL:
            self._scroll(t, *a[1:])
        elif command == CMD_TEXT:
            _, x, y, fg, bg, length = a
            size = _STRUCTS[CMD_TEXT].size
            if self.font is None or size + length > len(args):
                return False
            self._text(t, x, y, fg, bg, bytes(args[size:size + length]))
        elif command == CMD_BLIT:
            dst = self.target(a[3])
            if dst is None:
                return False
            self._blit(t, a[1], a[2], dst, *a[4:])
        elif command == CMD_BLIT_SCALED:
            dst = self.target(a[5])
            if dst is None:
                return False
            return self._blit_scaled(t, *a[1:5], dst, *a[6:])
        elif command == CMD_BLIT_ALPHA:
            dst = self.target(a[3])
            if dst is None:
                return False
            return self._blit_alpha(t, a[1], a[2], dst, *a[4:])
        return True

    def batch(self, window) -> Tuple[int, int]:
        """Check every record's shape, then run them: (ran, refused)."""
        count = struct.unpack_from("<I", window, 0)[0]
        at, records = 4, []
        for _ in range(count):
            if at + 8 > len(window):
                return 0, count
            cmd, nwords = struct.unpack_from("<II", window, at)
            args = at + 8
            end = args + nwords * 4
            if cmd not in _STRUCTS or end > len(window):
                return 0, count
            need = _STRUCTS[cmd].size
            if cmd == CMD_TEXT and nwords * 4 >= need:
                length = struct.unpack_from("<I", window, args + 20)[0]
                need += (length + 3) & ~3
            if nwords * 4 != need:
                return 0, count
            records.append((cmd, window[args:end]))
            at = end
        refused = sum(1 for cmd, args in records if not self.run(cmd, args))
        return len(records) - refused, refused

    # --- the IO bus device ----------------------------------------------------------

    def callback(self, read_write: int, command: int, length: int, address: int,
                 data: bytearray) -> bytes:
        """IOController-compatible callback. See the module docstring."""
        # A copy: 4 KB costs well under a microsecond, and nothing a command
        # draws can then change the arguments it is still reading.
        window = bytes(self.ram.mem[_WINDOW_BASE:_WINDOW_BASE + WINDOW_BYTES])
        if command in _STRUCTS:
            return _OK if self.run(command, window) else _NO
        if command == CMD_BATCH:
            return struct.pack("<II", *self.batch(window))
        if command == CMD_RAM_SURFACE:
            return struct.pack("<I", self.ram_surface(*struct.unpack_from("<III", window)))
        if command == CMD_RAM_FREE:
            return _OK if self.ram_surfaces.pop(address, None) is not None else _NO
        if command == CMD_INFO:
            return struct.pack("<III", GAC_MAGIC,
                               FEATURE_TEXT | FEATURE_BLEND | FEATURE_SRC_ALPHA,
                               WINDOW_BYTES)
        if command in (CMD_NOP, CMD_DAMAGE):
            return _NO
        log.warning("GAC: unknown command %d", command)
        return b""
