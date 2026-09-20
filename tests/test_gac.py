"""The graphics accelerator (docs/gac/plans/phase3_gac.md).

The test that matters most is the first one: lib/pigeon/display.c draws a
scene on a bare CPU, the GAC draws the same scene, and the two framebuffers
must be equal byte for byte. Phase 5 routes display.c's calls here, and a
pixel that moved would show in every screenshot test in the suite.

    python3 tests/test_gac.py      (or: python3 -m pytest tests/)
"""
import random
import re
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_libs                                                      # noqa: E402
from _runner import cases, run_module                                 # noqa: E402
from emulator.devices.display_io import DisplayIO                     # noqa: E402
from emulator.devices.gac import (                                    # noqa: E402
    CMD_BATCH, CMD_BLIT, CMD_BLIT_ALPHA, CMD_BLIT_SCALED, CMD_CIRCLE, CMD_DAMAGE, CMD_DISC, CMD_FILL,
    CMD_FRAME, CMD_INFO, CMD_LINE, CMD_NOP, CMD_RAM_FREE, CMD_RAM_SURFACE, CMD_SCROLL,
    CMD_SET_FONT, CMD_TEXT, FEATURE_BLEND, FEATURE_SRC_ALPHA, FEATURE_TEXT, GAC,
    GAC_MAGIC, MAX_EXTENT, MAX_ROW_PIXELS, RAM_HANDLE, SRC_ALPHA, WINDOW_BYTES,
    blend_rows, src_alpha_row)
from emulator.devices.vram import CMD_ALLOC, CMD_FREE, VRAM                # noqa: E402
from emulator.io_controller import IOChannel, IOController           # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    CH_GAC, DISPLAY_H, DISPLAY_MODES, DISPLAY_SIZE, DISPLAY_START, DISPLAY_W, HEAP_START,
    IO_START, IOHeader, PROGRAM_LOAD_ADDR)
from emulator.ram import RAM                                          # noqa: E402

W, H = DISPLAY_W, DISPLAY_H
WINDOW = IO_START + IOHeader.USABLE_AFTER
# Small, as in test_vram.py: a worker per CPU, and 128 MB RAMs copied per
# case are how the suite once ran the host out of memory.
TEST_RAM = 1 << 24
TEST_VRAM = 1 << 23
FONT_AT = HEAP_START

_SRC = (REPO_ROOT / "lib" / "pigeon" / "display.c").read_text()
_TABLE = _SRC[_SRC.index("FONT[] = {"):_SRC.index("};", _SRC.index("FONT[] = {"))]
FONT = bytes(int(h, 16) for h in re.findall(r"0x([0-9A-Fa-f]{2})", _TABLE))
assert len(FONT) == 95 * 8, len(FONT)


class Rig:
    """A RAM with video memory, its VRAM device and a GAC, the font in
    RAM, and a RAM surface over DISPLAY_START -- the power-on screen."""

    def __init__(self, mode=(W, H)):
        self.ram = RAM(TEST_RAM, TEST_VRAM)
        self.vram = VRAM(self.ram, DisplayIO(self.ram), DISPLAY_MODES, mode)
        self.gac = GAC(self.ram, self.vram)
        self.ram.mem[FONT_AT:FONT_AT + len(FONT)] = FONT
        self.screen = self.words(CMD_RAM_SURFACE, DISPLAY_START, W, H)[0]
        assert self.screen, "the power-on screen was refused as a RAM surface"

    def call(self, command, *words, tail=b"", address=0):
        """Arguments in the window, R/W 0, as the guest sends them. Words
        may be negative: they are packed as the guest's two's complement."""
        data = b"".join(struct.pack("<I", w & 0xFFFFFFFF) for w in words) + tail
        self.ram.mem[WINDOW:WINDOW + len(data)] = data
        return self.gac.callback(0, command, 4, address, bytearray(4))

    def words(self, command, *words, **kw):
        reply = self.call(command, *words, **kw)
        return struct.unpack(f"<{len(reply) // 4}I", reply)

    def ok(self, command, *words, **kw):
        return self.words(command, *words, **kw) == (1,)

    def font(self):
        assert self.ok(CMD_SET_FONT, FONT_AT, 5, 8, 6, 8, 0x20, 95)

    def text(self, dst, x, y, s, fg, bg=0):
        data = s.encode()
        return self.ok(CMD_TEXT, dst, x, y, fg, bg, len(data), tail=data)

    def fb(self):
        return bytes(self.ram.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE])


def pixel(fb, x, y, w=W):
    return struct.unpack_from("<I", fb, (y * w + x) * 4)[0]


# --- the equality test ------------------------------------------------------------

SCENE = [
    ("clear", 0xFF101018),
    ("rect", 10, 10, 30, 20, 0xFFFF0000),
    ("rect", 180, 100, 40, 40, 0xFF00FF00),               # runs off the far corner
    ("hline", 0, 50, W, 0xFF0000FF),
    ("vline", 100, 0, H, 0xFFFFFF00),
    ("frame", 5, 60, 50, 30, 0xFFFFFFFF),
    ("frame", 150, 80, 60, 50, 0xFF00FFFF),                # runs off the edges
    ("frame", 60, 5, 1, 1, 0xFFFF00FF),
    ("line", 0, 0, W - 1, H - 1, 0xFFFFFFFF),
    ("line", W - 1, 0, 0, H - 1, 0xFF808080),
    ("line", -30, 20, 250, 90, 0xFFFF8000),                 # both ends off screen
    ("line", 90, -20, 60, 130, 0xFF00FF80),                 # steep, off top and bottom
    ("line", 120, 40, 120, 40, 0xFFFFFFFF),                 # a single point
    ("circle", 96, 54, 30, 0xFFFF0000),
    ("circle", 0, 0, 25, 0xFF00FF00),                       # three quarters off
    ("circle", 150, 30, 0, 0xFFFFFFFF),
    ("disc", 170, 20, 18, 0xFF4080FF),
    ("disc", -5, 100, 20, 0xFFFF40FF),                      # off the left and bottom
    ("disc", 40, 40, 1, 0xFFFFFFFF),
    ("text", 2, 2, "Hello, pigeon! gjpqy_", 0xFFFFFFFF),
    ("text", -8, 95, "off the left edge", 0xFFFFC000),
    ("text", 160, 70, "and the right", 0xFF80FF80),
    ("scroll", 30, 40, -7, 0xFF202040),
    ("scroll", 60, 30, 5, 0xFF402020),
    ("scroll", 100, 8, 20, 0xFF000000),                     # further than the band
]


def random_scene(seed, n=60):
    """Shapes where display.c and the GAC agree on what the numbers mean:
    lines, circles, discs and text anywhere, and rects and frames starting
    on screen -- display.c's are unsigned, so one starting off the left
    wraps and draws nothing, where the GAC draws its visible part (Q1)."""
    rng = random.Random(seed)
    colour = lambda: 0xFF000000 | rng.randrange(1 << 24)
    near = lambda size: rng.randrange(-40, size + 40)
    scene = [("clear", colour())]
    for _ in range(n):
        kind = rng.choice(("rect", "frame", "line", "circle", "disc", "text", "scroll"))
        if kind in ("rect", "frame"):
            scene.append((kind, rng.randrange(W), rng.randrange(H),
                          rng.randrange(80), rng.randrange(60), colour()))
        elif kind == "line":
            scene.append((kind, near(W), near(H), near(W), near(H), colour()))
        elif kind in ("circle", "disc"):
            scene.append((kind, near(W), near(H), rng.randrange(40), colour()))
        elif kind == "text":
            s = "".join(chr(rng.randrange(0x20, 0x7F)) for _ in range(rng.randrange(1, 20)))
            s = s.replace("\\", "/").replace('"', "'")
            scene.append((kind, near(W), near(H), s, colour()))
        else:
            y = rng.randrange(H)
            scene.append((kind, y, rng.randrange(1, H - y + 1), rng.randrange(-30, 31),
                          colour()))
    return scene


def c_program(scene):
    calls = {
        "clear": "disp_clear(0x%Xu);",
        "rect": "disp_rect(%d, %d, %d, %d, 0x%Xu);",
        "hline": "disp_hline(%d, %d, %d, 0x%Xu);",
        "vline": "disp_vline(%d, %d, %d, 0x%Xu);",
        "frame": "disp_frame(%d, %d, %d, %d, 0x%Xu);",
        "line": "disp_line(%d, %d, %d, %d, 0x%Xu);",
        "circle": "disp_circle(%d, %d, %d, 0x%Xu);",
        "disc": "disp_disc(%d, %d, %d, 0x%Xu);",
        "scroll": "disp_scroll(%d, %d, %d, 0x%Xu);",
    }
    body = []
    for op in scene:
        if op[0] == "text":
            _, x, y, s, c = op
            body.append('disp_text((unsigned)%d, (unsigned)%d, "%s", 0x%Xu);' % (x, y, s, c))
        else:
            body.append(calls[op[0]] % op[1:])
    return test_libs.DISPLAY + "int main(void){\n" + "\n".join(body) + "\nreturn 0; }\n"


def gac_draw(rig, scene):
    rig.font()
    s = rig.screen
    for op in scene:
        kind, args = op[0], op[1:]
        if kind == "clear":
            ok = rig.ok(CMD_FILL, s, 0, 0, W, H, args[0])
        elif kind == "rect":
            ok = rig.ok(CMD_FILL, s, *args)
        elif kind == "hline":
            x, y, w, c = args
            ok = rig.ok(CMD_FILL, s, x, y, w, 1, c)
        elif kind == "vline":
            x, y, h, c = args
            ok = rig.ok(CMD_FILL, s, x, y, 1, h, c)
        elif kind == "frame":
            ok = rig.ok(CMD_FRAME, s, *args)
        elif kind == "line":
            ok = rig.ok(CMD_LINE, s, *args)
        elif kind in ("circle", "disc"):
            ok = rig.ok(CMD_CIRCLE if kind == "circle" else CMD_DISC, s, *args)
        elif kind == "text":
            x, y, text, c = args
            ok = rig.text(s, x, y, text, c)
        else:
            y, h, dy, bg = args
            ok = rig.ok(CMD_SCROLL, s, 0, y, W, h, dy, bg)
        assert ok, f"the GAC refused {op}"


def differences(a, b):
    out = []
    for i in range(0, len(a), 4):
        if a[i:i + 4] != b[i:i + 4]:
            out.append(((i // 4) % W, (i // 4) // W))
    return out


@cases(("fixed", None), ("random 1", 1), ("random 2", 2))
def test_the_gac_draws_exactly_what_display_c_draws(label, seed):
    scene = SCENE if seed is None else random_scene(seed)
    cpu = test_libs.run(c_program(scene), "display.c")
    software = test_libs.framebuffer(cpu.ram)
    del cpu
    rig = Rig()
    gac_draw(rig, scene)
    diff = differences(software, rig.fb())
    assert not diff, f"{label}: {len(diff)} pixels differ, the first at {diff[:5]}"


# --- the device ---------------------------------------------------------------------

def test_info_answers_the_magic_first():
    assert Rig().words(CMD_INFO) == (
        GAC_MAGIC, FEATURE_TEXT | FEATURE_BLEND | FEATURE_SRC_ALPHA, WINDOW_BYTES)


@cases(CMD_NOP, CMD_DAMAGE)
def test_nop_and_damage_answer_zero(command):
    assert Rig().words(command) == (0,)


def test_an_unknown_command_is_survivable():
    assert Rig().call(99) == b""


# --- surfaces -----------------------------------------------------------------------

@cases(
    ("the power-on screen", DISPLAY_START, W, H, True),
    ("the heap", HEAP_START, 64, 64, True),
    ("bigger than a screen at DISPLAY_START", DISPLAY_START, W, H + 1, False),
    ("the IO window", IO_START, 16, 16, False),
    ("the BIOS", 0, 16, 16, False),
    ("under the program", PROGRAM_LOAD_ADDR - 64, 16, 16, False),
    ("past the end of RAM", TEST_RAM - 64, 16, 16, False),
    ("unaligned", HEAP_START + 2, 16, 16, False),
    ("no pixels", HEAP_START, 0, 16, False),
)
def test_a_ram_surface_is_checked_once_when_registered(label, address, w, h, allowed):
    handle = Rig().words(CMD_RAM_SURFACE, address, w, h)[0]
    assert bool(handle) == allowed, label
    assert not handle or handle & RAM_HANDLE, label


def test_ram_free_forgets_the_surface():
    rig = Rig()
    assert rig.words(CMD_RAM_FREE, address=rig.screen) == (1,)
    assert rig.words(CMD_RAM_FREE, address=rig.screen) == (0,)
    assert not rig.ok(CMD_FILL, rig.screen, 0, 0, 4, 4, 0xFFFFFFFF)


def alloc(rig, w, h):
    """A VRAM surface, as a guest gets one from CH_VRAM: (handle, offset)."""
    rig.ram.mem[WINDOW:WINDOW + 8] = struct.pack("<II", w, h)
    handle, offset, _ = struct.unpack("<III", rig.vram.callback(0, CMD_ALLOC, 4, 0,
                                                                bytearray(4)))
    return handle, offset


def test_a_vram_surface_is_drawn_through_its_handle():
    rig = Rig()
    handle, offset = alloc(rig, 10, 10)
    assert rig.ok(CMD_FILL, handle, 0, 0, 10, 10, 0xFF112233)
    assert rig.ram.vram[offset:offset + 4] == b"\x33\x22\x11\xff"
    assert rig.ram.vram[offset + 400:offset + 404] == bytes(4), "wrote past the surface"


def test_a_freed_vram_surface_is_refused():
    rig = Rig()
    handle, _ = alloc(rig, 10, 10)
    rig.vram.callback(0, CMD_FREE, 4, handle, bytearray(4))
    assert not rig.ok(CMD_FILL, handle, 0, 0, 10, 10, 0xFFFFFFFF)
    assert rig.ram.vram == bytes(TEST_VRAM)


def test_the_screen_is_handle_zero_and_follows_the_mode():
    rig = Rig(mode=(320, 180))
    assert rig.ok(CMD_FILL, 0, 0, 0, 320, 180, 0xFFFFFFFF)
    screen = rig.vram.surfaces[0]
    assert rig.ram.vram[screen.offset:screen.end] == b"\xff" * (320 * 180 * 4)
    assert rig.ram.vram[screen.end:screen.end + 16] == bytes(16)


@cases(CMD_FILL, CMD_FRAME, CMD_LINE)
def test_an_unknown_handle_is_refused_and_draws_nothing(command):
    rig = Rig()
    before = rig.fb()
    assert not rig.ok(command, 12345, 0, 0, 10, 10, 0xFFFFFFFF)
    assert not rig.ok(command, RAM_HANDLE + 999, 0, 0, 10, 10, 0xFFFFFFFF)
    assert rig.fb() == before


# --- clipping -----------------------------------------------------------------------

@cases((-5, -5, 20, 20), (W - 10, H - 10, 50, 50), (-100, 10, 50, 10), (10, H, 10, 10),
       (-(1 << 31), -(1 << 31), 1 << 31, 1 << 31))
def test_a_fill_draws_exactly_its_visible_part(x, y, w, h):
    """Signed coordinates, clipped (Q1): the visible part, nothing else."""
    rig = Rig()
    assert rig.ok(CMD_FILL, rig.screen, x, y, w, h, 0xFFFFFFFF)
    fb = rig.fb()
    for py in range(H):
        for px in range(W):
            inside = x <= px < x + w and y <= py < y + h
            assert (pixel(fb, px, py) == 0xFFFFFFFF) == inside, (px, py)


def test_nothing_is_written_outside_the_surface():
    """Everything off every edge of a small heap surface, and the bytes
    around it untouched."""
    rig = Rig()
    rig.font()
    surface = rig.words(CMD_RAM_SURFACE, HEAP_START + 4096, 8, 8)[0]
    before = bytes(rig.ram.mem)
    colour = 0xFFABCDEF
    rig.ok(CMD_FILL, surface, -10, -10, 100, 100, colour)
    rig.ok(CMD_FRAME, surface, -3, -3, 20, 20, colour)
    rig.ok(CMD_LINE, surface, -50, -40, 60, 70, colour)
    rig.ok(CMD_CIRCLE, surface, 4, 4, 30, colour)
    rig.ok(CMD_DISC, surface, 4, 4, 30, colour)
    rig.text(surface, -3, -3, "WWWWWWWW", colour, 0xFF000000)
    rig.ok(CMD_SCROLL, surface, -5, -5, 30, 30, 3, colour)
    rig.ok(CMD_BLIT, rig.screen, 0, 0, surface, -4, -4, 50, 50)
    after = bytes(rig.ram.mem)
    start, end = HEAP_START + 4096, HEAP_START + 4096 + 8 * 8 * 4
    io_end = IO_START + IOHeader.USABLE_AFTER + WINDOW_BYTES   # the calls' own arguments
    assert after[:IO_START] == before[:IO_START]
    assert after[io_end:start] == before[io_end:start] and after[end:] == before[end:]


@cases(CMD_LINE, CMD_CIRCLE, CMD_DISC)
def test_an_absurdly_big_shape_is_refused(command):
    rig = Rig()
    far = MAX_EXTENT + 1
    args = (0, 0, far, 0, 0xFFFFFFFF) if command == CMD_LINE else (0, 0, far, 0xFFFFFFFF)
    assert not rig.ok(command, rig.screen, *args)
    assert rig.ok(CMD_LINE if command == CMD_LINE else command, rig.screen,
                  *((0, 0, MAX_EXTENT, 0, 0xFFFFFFFF) if command == CMD_LINE
                    else (0, 0, 200, 0xFFFFFFFF)))


def test_a_negative_radius_draws_nothing():
    rig = Rig()
    assert rig.ok(CMD_DISC, rig.screen, 50, 50, -3, 0xFFFFFFFF)
    assert rig.fb() == bytes(DISPLAY_SIZE)


# --- each pixel once ----------------------------------------------------------------

def spy(rig):
    """Every pixel offset an Ink writes, in order: for "each pixel once"."""
    from emulator.devices import gac as gac_module
    seen = []
    real = gac_module.Ink

    class Counting(real):
        def span(self, buf, offset, pixels):
            seen.extend(range(offset, offset + pixels * 4, 4))
            super().span(buf, offset, pixels)

        def pixel(self, buf, offset):
            seen.append(offset)
            super().pixel(buf, offset)

    gac_module.Ink = Counting
    return seen, lambda: setattr(gac_module, "Ink", real)


@cases(("frame", CMD_FRAME, (20, 20, 30, 15)), ("circle", CMD_CIRCLE, (60, 50, 25)),
       ("disc", CMD_DISC, (60, 50, 25)), ("small circle", CMD_CIRCLE, (60, 50, 1)))
def test_each_pixel_is_drawn_once(label, command, args):
    """display.c stores these pixels twice; blending them twice would darken
    them, so the GAC must not."""
    rig = Rig()
    seen, restore = spy(rig)
    try:
        rig.ok(command, rig.screen, *args, 0xFFFFFFFF)
    finally:
        restore()
    assert seen and len(seen) == len(set(seen)), f"{label}: a pixel drawn twice"


# --- copies ----------------------------------------------------------------------------

def paint_gradient(rig, surface_address, w, h):
    for y in range(h):
        for x in range(w):
            struct.pack_into("<I", rig.ram.mem, surface_address + (y * w + x) * 4,
                             0xFF000000 | (y << 8) | x)


@cases((5, 3), (-5, 3), (3, -5), (-3, -4), (0, 7))
def test_a_blit_onto_itself_is_overlap_safe(dx, dy):
    rig = Rig()
    paint_gradient(rig, DISPLAY_START, W, H)
    before = rig.fb()
    assert rig.ok(CMD_BLIT, rig.screen, 20, 20, rig.screen, 20 + dx, 20 + dy, 40, 30)
    fb = rig.fb()
    for y in range(30):
        for x in range(40):
            assert pixel(fb, 20 + dx + x, 20 + dy + y) == pixel(before, 20 + x, 20 + y)


def test_a_blit_moves_ram_into_vram_and_back():
    rig = Rig(mode=(320, 180))
    paint_gradient(rig, DISPLAY_START, W, H)
    assert rig.ok(CMD_BLIT, rig.screen, 0, 0, 0, 10, 10, W, H)
    screen = rig.vram.surfaces[0]
    at = screen.offset + (10 * 320 + 10) * 4
    assert rig.ram.vram[at:at + 4] == rig.ram.mem[DISPLAY_START:DISPLAY_START + 4]
    rig.ram.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE] = bytes(DISPLAY_SIZE)
    assert rig.ok(CMD_BLIT, 0, 10, 10, rig.screen, 0, 0, W, H)
    assert pixel(rig.fb(), 5, 7) == 0xFF000000 | (7 << 8) | 5


def test_a_scaled_blit_uses_bmp_cs_mapping():
    rig = Rig()
    src = rig.words(CMD_RAM_SURFACE, HEAP_START + 8192, 7, 5)[0]
    paint_gradient(rig, HEAP_START + 8192, 7, 5)
    assert rig.ok(CMD_BLIT_SCALED, src, 0, 0, 7, 5, rig.screen, 3, 4, 20, 13)
    fb = rig.fb()
    for j in range(13):
        for i in range(20):
            sx, sy = i * 7 // 20, j * 5 // 13
            assert pixel(fb, 3 + i, 4 + j) == 0xFF000000 | (sy << 8) | sx, (i, j)
    assert pixel(fb, 23, 4) == 0 and pixel(fb, 3, 17) == 0


def test_a_scaled_blit_needs_its_whole_source():
    rig = Rig()
    assert not rig.ok(CMD_BLIT_SCALED, rig.screen, W - 5, 0, 10, 10, rig.screen, 0, 0, 20, 20)


# --- text ------------------------------------------------------------------------------

def test_text_needs_a_font():
    rig = Rig()
    assert not rig.text(rig.screen, 0, 0, "A", 0xFFFFFFFF)


@cases((9, 8, 6, 8, 0x20, 95), (5, 8, 4, 8, 0x20, 95), (5, 8, 6, 8, 0x20, 0),
       (5, 8, 6, 8, 0xF0, 95))
def test_a_bad_font_is_refused(glyph_w, glyph_h, cell_w, cell_h, first, count):
    rig = Rig()
    assert not rig.ok(CMD_SET_FONT, FONT_AT, glyph_w, glyph_h, cell_w, cell_h, first, count)


def test_a_font_past_the_end_of_ram_is_refused():
    assert not Rig().ok(CMD_SET_FONT, TEST_RAM - 16, 5, 8, 6, 8, 0x20, 95)


def test_a_background_fills_the_cell_and_alpha_zero_leaves_it():
    """Q3: bg with alpha 0 is 'no background'."""
    rig = Rig()
    rig.font()
    rig.ok(CMD_FILL, rig.screen, 0, 0, W, H, 0xFF444444)
    assert rig.text(rig.screen, 0, 0, "I", 0xFFFFFFFF, 0x00FF0000)
    assert pixel(rig.fb(), 0, 0) == 0xFF444444, "alpha-0 background painted"
    assert rig.text(rig.screen, 10, 0, "I", 0xFFFFFFFF, 0xFFFF0000)
    fb = rig.fb()
    assert pixel(fb, 10, 0) == 0xFFFF0000 and pixel(fb, 15, 7) == 0xFFFF0000
    assert pixel(fb, 12, 0) == 0xFFFFFFFF, "the I's top bar"
    assert pixel(fb, 16, 0) == 0xFF444444, "painted past the cell"


def test_characters_outside_the_font_advance_but_draw_nothing():
    rig = Rig()
    rig.font()
    assert rig.text(rig.screen, 0, 0, "\x01A", 0xFFFFFFFF, 0xFF0000FF)
    fb = rig.fb()
    assert pixel(fb, 0, 0) == 0xFF0000FF and pixel(fb, 6 + 1, 3) == 0xFFFFFFFF


def test_text_longer_than_the_window_is_refused():
    rig = Rig()
    rig.font()
    assert not rig.ok(CMD_TEXT, rig.screen, 0, 0, 0xFFFFFFFF, 0, WINDOW_BYTES, tail=b"A" * 8)


# --- batch -----------------------------------------------------------------------------

def record(command, *words, tail=b""):
    tail += bytes(-len(tail) % 4)
    body = b"".join(struct.pack("<I", w & 0xFFFFFFFF) for w in words) + tail
    return struct.pack("<II", command, len(body) // 4) + body


def send_batch(rig, *records, count=None):
    data = struct.pack("<I", len(records) if count is None else count) + b"".join(records)
    rig.ram.mem[WINDOW:WINDOW + len(data)] = data
    return struct.unpack("<II", rig.gac.callback(0, CMD_BATCH, 4, 0, bytearray(4)))


def test_a_batch_runs_in_order():
    rig = Rig()
    rig.font()
    s = rig.screen
    assert send_batch(rig,
                      record(CMD_FILL, s, 0, 0, W, H, 0xFF000080),
                      record(CMD_FILL, s, 10, 10, 20, 20, 0xFFFF0000),
                      record(CMD_TEXT, s, 12, 12, 0xFFFFFFFF, 0, 3, tail=b"abc"),
                      record(CMD_DISC, s, 100, 50, 10, 0xFF00FF00)) == (4, 0)
    fb = rig.fb()
    assert pixel(fb, 0, 0) == 0xFF000080 and pixel(fb, 29, 29) == 0xFFFF0000
    assert pixel(fb, 100, 50) == 0xFF00FF00


def test_a_bad_handle_in_a_batch_is_skipped_and_counted():
    rig = Rig()
    s = rig.screen
    assert send_batch(rig,
                      record(CMD_FILL, 777, 0, 0, 5, 5, 0xFFFFFFFF),
                      record(CMD_FILL, s, 0, 0, 5, 5, 0xFFFF0000)) == (1, 1)
    assert pixel(rig.fb(), 0, 0) == 0xFFFF0000


@cases(
    ("an unknown command", lambda s: record(99, 1, 2)),
    ("a batch in a batch", lambda s: record(CMD_BATCH, 0)),
    ("too few words", lambda s: record(CMD_FILL, s, 0, 0, 5, 5)),
    ("too many words", lambda s: record(CMD_FILL, s, 0, 0, 5, 5, 0xFFFFFFFF, 7)),
    ("text shorter than it says", lambda s: record(CMD_TEXT, s, 0, 0, 0xFFFFFFFF, 0, 40,
                                                   tail=b"abc")),
)
def test_a_malformed_batch_draws_nothing(label, bad):
    rig = Rig()
    rig.font()
    s = rig.screen
    reply = send_batch(rig, record(CMD_FILL, s, 0, 0, W, H, 0xFFFFFFFF), bad(s))
    assert reply == (0, 2), label
    assert rig.fb() == bytes(DISPLAY_SIZE), f"{label}: drew something"


def test_a_batch_running_off_the_window_draws_nothing():
    rig = Rig()
    s = rig.screen
    assert send_batch(rig, record(CMD_FILL, s, 0, 0, 5, 5, 0xFFFFFFFF), count=600) == (0, 600)
    assert rig.fb() == bytes(DISPLAY_SIZE)


# --- blending (3b) -----------------------------------------------------------------------

def blend(d, s, a):
    """The formula, one channel of one pixel: the reference everything is
    checked against."""
    return (d * (255 - a) + s * a + 127) // 255


def blend_pixel(dst, colour):
    """A 0xAARRGGBB colour over a destination pixel word: B, G and R
    blended, the destination's own alpha kept."""
    a = colour >> 24
    out = dst & 0xFF000000
    for shift in (0, 8, 16):
        out |= blend((dst >> shift) & 0xFF, (colour >> shift) & 0xFF, a) << shift
    return out


def noise(rig, address, pixels, seed):
    rng = random.Random(seed)
    rig.ram.mem[address:address + pixels * 4] = bytes(rng.randrange(256)
                                                      for _ in range(pixels * 4))


@cases(0x80FF0000, 0x01123456, 0xFE00FF7F, 0x40FFFFFF)
def test_a_translucent_fill_matches_the_formula_on_every_pixel(colour):
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, colour)
    before = rig.fb()
    assert rig.ok(CMD_FILL, rig.screen, 20, 10, 100, 60, colour)
    fb = rig.fb()
    for y in range(H):
        for x in range(W):
            want = pixel(before, x, y)
            if 20 <= x < 120 and 10 <= y < 70:
                want = blend_pixel(want, colour)
            assert pixel(fb, x, y) == want, (x, y)


def test_alpha_zero_is_stored_as_it_is_not_invisible():
    """0 is what code has always cleared to, meaning black, and the screen
    ignores alpha (phase 4): an alpha-0 colour is stored like an opaque one
    (docs/gac/plans/phase5_display_lib.md, As built)."""
    rig = Rig()
    rig.font()
    noise(rig, DISPLAY_START, W * H, 7)
    for command, args in ((CMD_FILL, (0, 0, W, H)), (CMD_FRAME, (5, 5, 50, 50)),
                          (CMD_LINE, (0, 0, W, H)), (CMD_DISC, (50, 50, 20))):
        assert rig.ok(command, rig.screen, *args, 0x00123456), command
    assert pixel(rig.fb(), 0, 0) == 0x00123456 and pixel(rig.fb(), 50, 50) == 0x00123456
    assert rig.ok(CMD_FILL, rig.screen, 0, 0, W, H, 0)
    assert rig.fb() == bytes(DISPLAY_SIZE), "a clear to 0 did not zero the screen"


def test_a_text_background_with_alpha_zero_is_still_no_background():
    rig = Rig()
    rig.font()
    noise(rig, DISPLAY_START, W * H, 8)
    before = rig.fb()
    assert rig.text(rig.screen, 0, 0, " ", 0xFFFFFFFF, 0x00FFFFFF)
    assert rig.fb() == before


def test_a_colour_over_itself_is_itself():
    """Rounding to nearest: blending never drifts a colour it already is."""
    rig = Rig()
    rig.ok(CMD_FILL, rig.screen, 0, 0, W, H, 0xFF3A6EA5)
    for alpha in (1, 77, 128, 254):
        rig.ok(CMD_FILL, rig.screen, 0, 0, W, H, (alpha << 24) | 0x3A6EA5)
    assert pixel(rig.fb(), 50, 50) == 0xFF3A6EA5


@cases(("frame", CMD_FRAME, (20, 20, 40, 30)), ("circle", CMD_CIRCLE, (90, 54, 30)),
       ("disc", CMD_DISC, (90, 54, 30)), ("line", CMD_LINE, (0, 0, 150, 90)),
       ("tiny disc", CMD_DISC, (10, 10, 1)))
def test_a_translucent_shape_blends_each_pixel_exactly_once(label, command, args):
    """display.c stores these pixels twice; blended twice they would come
    out darker than the rest of the shape."""
    rig = Rig()
    rig.ok(CMD_FILL, rig.screen, 0, 0, W, H, 0xFF000000)
    assert rig.ok(command, rig.screen, *args, 0x80FFFFFF)
    once = blend_pixel(0xFF000000, 0x80FFFFFF)
    fb = rig.fb()
    seen = {pixel(fb, x, y) for y in range(H) for x in range(W)}
    assert seen == {0xFF000000, once}, f"{label}: {sorted(hex(v) for v in seen)}"


def test_translucent_text_lays_its_ink_over_its_background():
    rig = Rig()
    rig.font()
    rig.ok(CMD_FILL, rig.screen, 0, 0, W, H, 0xFF204080)
    assert rig.text(rig.screen, 0, 0, "I", 0x80FFFFFF, 0x80000000)
    fb = rig.fb()
    under = blend_pixel(0xFF204080, 0x80000000)
    assert pixel(fb, 0, 0) == under, "the cell's background"
    assert pixel(fb, 2, 0) == blend_pixel(under, 0x80FFFFFF), "the I's top bar"
    assert pixel(fb, 6, 0) == 0xFF204080, "past the cell"


def test_a_translucent_scroll_background_blends():
    rig = Rig()
    rig.ok(CMD_FILL, rig.screen, 0, 0, W, H, 0xFF000000)
    assert rig.ok(CMD_SCROLL, rig.screen, 0, 0, W, 20, -5, 0x80FF0000)
    fb = rig.fb()
    assert pixel(fb, 0, 17) == blend_pixel(0xFF000000, 0x80FF0000)
    assert pixel(fb, 0, 10) == 0xFF000000


def test_the_destinations_alpha_byte_is_kept():
    rig = Rig()
    rig.ram.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE] = (
        struct.pack("<I", 0x7F102030) * (W * H))
    rig.ok(CMD_FILL, rig.screen, 0, 0, W, H, 0x80FFFFFF)
    assert pixel(rig.fb(), 3, 3) == blend_pixel(0x7F102030, 0x80FFFFFF)
    assert pixel(rig.fb(), 3, 3) >> 24 == 0x7F


@cases(1, 64, 128, 200, 254)
def test_blit_alpha_matches_the_formula_byte_for_byte(alpha):
    rig = Rig()
    src = rig.words(CMD_RAM_SURFACE, HEAP_START + 8192, 60, 40)[0]
    noise(rig, HEAP_START + 8192, 60 * 40, alpha)
    noise(rig, DISPLAY_START, W * H, alpha + 1000)
    before = rig.fb()
    image = bytes(rig.ram.mem[HEAP_START + 8192:HEAP_START + 8192 + 60 * 40 * 4])
    assert rig.ok(CMD_BLIT_ALPHA, src, 0, 0, rig.screen, 30, 20, 60, 40, alpha)
    fb = rig.fb()
    for y in range(H):
        for x in range(W):
            want = pixel(before, x, y)
            if 30 <= x < 90 and 20 <= y < 60:
                s = pixel(image, x - 30, y - 20, w=60)
                want = blend_pixel(want, (alpha << 24) | (s & 0xFFFFFF))
            assert pixel(fb, x, y) == want, (x, y)


def test_blend_rows_is_exact_across_a_row_wider_than_its_constants():
    rng = random.Random(3)
    n = (MAX_ROW_PIXELS + 37) * 4
    dst = bytes(rng.randrange(256) for _ in range(MAX_ROW_PIXELS * 4))
    src = bytes(rng.randrange(256) for _ in range(MAX_ROW_PIXELS * 4))
    got = blend_rows(dst, src, 99)
    for i in range(0, len(dst), 4):
        for c in range(3):
            assert got[i + c] == blend(dst[i + c], src[i + c], 99), (i, c)
        assert got[i + 3] == dst[i + 3]
    rig = Rig()
    wide = rig.words(CMD_RAM_SURFACE, HEAP_START + 8192, n // 4, 1)[0]
    assert rig.ok(CMD_BLIT_ALPHA, wide, 0, 0, wide, 1, 0, n // 4, 1, 128), "in pieces"


def test_blit_alpha_at_the_ends_of_its_range():
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 11)
    before = rig.fb()
    assert rig.ok(CMD_BLIT_ALPHA, rig.screen, 0, 0, rig.screen, 5, 5, 50, 50, 0)
    assert rig.fb() == before, "alpha 0 changed something"
    assert rig.ok(CMD_BLIT_ALPHA, rig.screen, 0, 0, rig.screen, 100, 5, 50, 50, 255)
    assert pixel(rig.fb(), 100, 5) == pixel(before, 0, 0), "alpha 255 is a blit"
    # 256 is SRC_ALPHA, which this GAC has; 257 and up are still refused.
    assert rig.ok(CMD_BLIT_ALPHA, rig.screen, 0, 0, rig.screen, 5, 5, 50, 50, SRC_ALPHA)
    assert not rig.ok(CMD_BLIT_ALPHA, rig.screen, 0, 0, rig.screen, 5, 5, 50, 50, 257)


def test_blit_alpha_onto_itself_reads_each_row_before_writing_it():
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 12)
    before = rig.fb()
    assert rig.ok(CMD_BLIT_ALPHA, rig.screen, 10, 10, rig.screen, 10, 13, 40, 20, 128)
    fb = rig.fb()
    for y in range(20):
        for x in range(40):
            s = pixel(before, 10 + x, 10 + y)
            want = blend_pixel(pixel(before, 10 + x, 13 + y), (128 << 24) | (s & 0xFFFFFF))
            assert pixel(fb, 10 + x, 13 + y) == want, (x, y)


# --- on the bus and in the machine ------------------------------------------------------

def test_a_guest_fills_through_the_io_controller():
    rig = Rig()
    controller = IOController(rig.ram)
    controller.register_channel(CH_GAC, IOChannel(rig.gac.callback, name="GAC"))
    struct.pack_into("<IiiiiI", rig.ram.mem, WINDOW, rig.screen, 0, 0, 4, 4, 0xFF00FF00)
    for field, value in ((IOHeader.IO_R_W, 0), (IOHeader.COMMAND, CMD_FILL),
                         (IOHeader.LENGTH, 4), (IOHeader.ADDRESS, 0),
                         (IOHeader.IO_CHANNEL, CH_GAC)):
        rig.ram.write_word(IO_START + field, value)
    controller.update()
    assert rig.ram.read_word(IO_START + IOHeader.RETURN_DATA) == 4
    assert pixel(rig.fb(), 3, 3) == 0xFF00FF00


def test_a_machine_has_the_gac_only_with_video_memory():
    import tempfile
    from emulator.machine import Machine
    bios = REPO_ROOT / "build" / "bios.bin"
    with tempfile.TemporaryDirectory() as d:
        for vram_size, present in ((1 << 20, True), (0, False)):
            machine = Machine(bios_path=str(bios), disk_path=str(Path(d) / "hdd.img"),
                              vram_size=vram_size)
            try:
                assert (machine.gac is not None) == present
                assert (CH_GAC in machine.io_controller.channels) == present
            finally:
                machine.close()


# --- BLIT_ALPHA at SRC_ALPHA: each source pixel's own alpha -----------------------
#
# docs/gac/plans/phase9_srcalpha.md. src_alpha_row cuts each row into a
# transparent margin it skips, a solid core it copies and a soft rim it
# blends, so the tests below are mostly about those three agreeing with the
# one reference: blend_pixel, the source word used as its own colour.


def src_alpha_reference(dst_fb, src_pixels, w, h, dx, dy, sw):
    """What SRC_ALPHA must produce, a pixel at a time."""
    out = bytearray(dst_fb)
    for y in range(h):
        for x in range(w):
            s = src_pixels[y * sw + x]
            if (s >> 24) == 0:
                continue
            d = pixel(dst_fb, dx + x, dy + y)
            struct.pack_into("<I", out, ((dy + y) * W + dx + x) * 4,
                             blend_pixel(d, s))
    return bytes(out)


def put_sprite(rig, at, pixels):
    rig.ram.mem[at:at + len(pixels) * 4] = b"".join(
        struct.pack("<I", p) for p in pixels)


SPRITE_AT = PROGRAM_LOAD_ADDR + 0x8000


def sprite_surface(rig, pixels, w, h):
    put_sprite(rig, SPRITE_AT, pixels)
    handle = rig.words(CMD_RAM_SURFACE, SPRITE_AT, w, h)[0]
    assert handle
    return handle


def disc_pixels(w, h):
    """An antialiased disc: opaque core, soft rim, clear outside -- the
    picture the design is built around."""
    out = []
    cx, cy, r = w / 2, h / 2, min(w, h) / 2 - 2
    for y in range(h):
        for x in range(w):
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            cover = max(0.0, min(1.0, r + 1 - dist))
            out.append((int(cover * 255) << 24) | 0x2080E0)
    return out


def gradient_pixels(w, h):
    """A partial alpha on nearly every pixel: no core to copy, no margin to
    skip. The case that falls back to the plain loop."""
    return [(((x * 255) // max(1, w - 1)) << 24) | 0x101018
            for y in range(h) for x in range(w)]


def noise_pixels(w, h, seed=3):
    rng = random.Random(seed)
    return [rng.randrange(1 << 32) for _ in range(w * h)]


def flat_pixels(w, h, alpha):
    return [(alpha << 24) | 0x30A050] * (w * h)


@cases(("disc", disc_pixels), ("gradient", gradient_pixels), ("noise", noise_pixels),
       ("opaque", lambda w, h: flat_pixels(w, h, 255)),
       ("clear", lambda w, h: flat_pixels(w, h, 0)),
       ("nearly opaque", lambda w, h: flat_pixels(w, h, 254)))
def test_src_alpha_matches_the_reference_on_every_picture(name, make):
    sw, sh = 48, 32
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 21)
    pixels = make(sw, sh)
    src = sprite_surface(rig, pixels, sw, sh)
    before = rig.fb()
    assert rig.ok(CMD_BLIT_ALPHA, src, 0, 0, rig.screen, 20, 30, sw, sh, SRC_ALPHA)
    assert rig.fb() == src_alpha_reference(before, pixels, sw, sh, 20, 30, sw), name


def test_src_alpha_leaves_a_clear_sprite_and_its_margin_untouched():
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 22)
    before = rig.fb()
    src = sprite_surface(rig, flat_pixels(30, 20, 0), 30, 20)
    assert rig.ok(CMD_BLIT_ALPHA, src, 0, 0, rig.screen, 4, 4, 30, 20, SRC_ALPHA)
    assert rig.fb() == before


def test_src_alpha_copies_an_opaque_sprite_but_keeps_the_destination_alpha():
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 23)
    before = rig.fb()
    pixels = flat_pixels(16, 8, 255)
    src = sprite_surface(rig, pixels, 16, 8)
    assert rig.ok(CMD_BLIT_ALPHA, src, 0, 0, rig.screen, 7, 9, 16, 8, SRC_ALPHA)
    fb = rig.fb()
    for y in range(8):
        for x in range(16):
            got = pixel(fb, 7 + x, 9 + y)
            assert got & 0xFFFFFF == 0x30A050
            assert got >> 24 == pixel(before, 7 + x, 9 + y) >> 24, "alpha was not kept"


def test_src_alpha_with_the_opaque_core_broken_by_a_gap():
    """find/rfind bracket the opaque pixels; a clear one between them means
    the core is not one run, and the whole span must go through the loop."""
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 24)
    row = [0xFF112233, 0xFF112233, 0x00112233, 0x80112233, 0xFF112233, 0x40112233]
    pixels = row * 4
    src = sprite_surface(rig, pixels, len(row), 4)
    before = rig.fb()
    assert rig.ok(CMD_BLIT_ALPHA, src, 0, 0, rig.screen, 3, 2, len(row), 4, SRC_ALPHA)
    assert rig.fb() == src_alpha_reference(before, pixels, len(row), 4, 3, 2, len(row))


@cases(([0x80112233], "one soft pixel"),
       ([0xFF112233], "one opaque pixel"),
       ([0x00112233], "one clear pixel"),
       ([0x01112233, 0xFE445566], "a rim with no core"),
       ([0x00112233, 0xFF445566, 0x00778899], "a core with clear on both sides"),
       ([0xFF112233, 0x00445566], "opaque then clear"))
def test_src_alpha_on_the_awkward_little_rows(row, name):
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 25)
    src = sprite_surface(rig, row, len(row), 1)
    before = rig.fb()
    assert rig.ok(CMD_BLIT_ALPHA, src, 0, 0, rig.screen, 11, 6, len(row), 1, SRC_ALPHA)
    assert rig.fb() == src_alpha_reference(before, row, len(row), 1, 11, 6, len(row)), name


@cases((-6, -4), (W - 5, 3), (3, H - 4), (-40, -40), (W + 10, 5), (5, H + 10))
def test_src_alpha_clips_like_every_other_command(dx, dy):
    """Nothing outside the surface is written, whatever the numbers."""
    sw, sh = 20, 16
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 26)
    pixels = disc_pixels(sw, sh)
    src = sprite_surface(rig, pixels, sw, sh)
    guard_before = bytes(rig.ram.mem[DISPLAY_START + DISPLAY_SIZE:
                                     DISPLAY_START + DISPLAY_SIZE + 64])
    before = rig.fb()
    assert rig.ok(CMD_BLIT_ALPHA, src, 0, 0, rig.screen, dx, dy, sw, sh, SRC_ALPHA)
    fb = rig.fb()
    assert rig.ram.mem[DISPLAY_START + DISPLAY_SIZE:
                       DISPLAY_START + DISPLAY_SIZE + 64] == guard_before
    for y in range(H):
        for x in range(W):
            sx, sy = x - dx, y - dy
            want = pixel(before, x, y)
            if 0 <= sx < sw and 0 <= sy < sh:
                want = blend_pixel(want, pixels[sy * sw + sx])
            assert pixel(fb, x, y) == want, (x, y)


@cases(3, -3)
def test_src_alpha_onto_itself_reads_each_row_before_writing_it(dy):
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 27)
    before = rig.fb()
    w, h, sx, sy = 40, 20, 10, 30
    assert rig.ok(CMD_BLIT_ALPHA, rig.screen, sx, sy, rig.screen, sx, sy + dy,
                  w, h, SRC_ALPHA)
    fb = rig.fb()
    for y in range(h):
        for x in range(w):
            s = pixel(before, sx + x, sy + y)
            want = blend_pixel(pixel(before, sx + x, sy + y + dy), s)
            assert pixel(fb, sx + x, sy + y + dy) == want, (x, y)


def test_src_alpha_refuses_a_surface_that_is_not_there():
    rig = Rig()
    assert not rig.ok(CMD_BLIT_ALPHA, 0x4242, 0, 0, rig.screen, 0, 0, 4, 4, SRC_ALPHA)
    assert not rig.ok(CMD_BLIT_ALPHA, rig.screen, 0, 0, 0x4242, 0, 0, 4, 4, SRC_ALPHA)


def test_src_alpha_inside_a_batch():
    rig = Rig()
    noise(rig, DISPLAY_START, W * H, 28)
    pixels = disc_pixels(12, 12)
    src = sprite_surface(rig, pixels, 12, 12)
    before = rig.fb()
    record = struct.pack("<II", CMD_BLIT_ALPHA, 9) + struct.pack(
        "<IiiIiiiiI", src, 0, 0, rig.screen, 8, 8, 12, 12, SRC_ALPHA)
    assert rig.words(CMD_BATCH, 1, tail=record) == (1, 0), "ran, refused"
    assert rig.fb() == src_alpha_reference(before, pixels, 12, 12, 8, 8, 12)


def test_src_alpha_row_is_exactly_the_per_pixel_formula():
    """The row function itself, against the formula, on rows built to hit
    each of its branches -- including ones no sprite above produces."""
    rng = random.Random(29)
    for trial in range(600):
        w = rng.randrange(1, 20)
        kind = trial % 5
        if kind == 0:
            alphas = [rng.choice([0, 255]) for _ in range(w)]
        elif kind == 1:
            alphas = [0] * w
        elif kind == 2:
            alphas = [255] * w
        elif kind == 3:
            alphas = [rng.randrange(256) for _ in range(w)]
        else:
            alphas = [rng.choice([0, 255, 255, rng.randrange(1, 255)]) for _ in range(w)]
        src = bytearray()
        for a in alphas:
            src += struct.pack("<I", (a << 24) | rng.randrange(1 << 24))
        dst = bytes(rng.randrange(256) for _ in range(w * 4))
        got = bytearray(dst)
        src_alpha_row(got, 0, src)
        want = bytearray(dst)
        for i in range(w):
            s = struct.unpack_from("<I", src, i * 4)[0]
            d = struct.unpack_from("<I", dst, i * 4)[0]
            struct.pack_into("<I", want, i * 4, blend_pixel(d, s))
        assert bytes(got) == bytes(want), (w, alphas)


class Recorder:
    """A bytearray that remembers which bytes were written to it. Blending
    a clear pixel comes to the destination unchanged, so skipping the
    transparent margin cannot be seen in the pixels -- only here."""

    def __init__(self, data):
        self.data = bytearray(data)
        self.written = set()

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.written.update(range(*key.indices(len(self.data)))
                            if isinstance(key, slice) else (key,))
        self.data[key] = value


def test_src_alpha_skips_the_transparent_margin_instead_of_blending_it():
    """The performance claim of the whole phase: a clear pixel costs
    nothing, because it is never touched (phase9_srcalpha.md §2)."""
    row = [0x00112233, 0x00112233, 0x80445566, 0xFF778899, 0x00AABBCC]
    src = b"".join(struct.pack("<I", p) for p in row)
    buf = Recorder(bytes(len(row) * 4))
    src_alpha_row(buf, 0, bytearray(src))
    assert buf.written, "nothing was drawn at all"
    assert not (buf.written & set(range(0, 8))), "the leading clear pixels were written"
    assert not (buf.written & set(range(16, 20))), "the trailing clear pixel was written"
    # The blended pixel's alpha byte is never touched. The copied one's is
    # written by the slice and then put straight back, which is why the
    # value, not the write, is what the other tests pin.
    assert 11 not in buf.written
    assert buf.data[15] == 0


def test_src_alpha_skips_a_wholly_clear_row_without_reading_the_destination():
    src = bytearray(struct.pack("<I", 0x00112233) * 6)
    buf = Recorder(bytes(24))
    src_alpha_row(buf, 0, src)
    assert buf.written == set()


def test_src_alpha_row_writes_nothing_outside_the_row_it_is_given():
    buf = bytearray(b"\xAA" * 64)
    src = bytearray(struct.pack("<I", 0x80102030) * 4)
    src_alpha_row(buf, 16, src)
    assert buf[:16] == b"\xAA" * 16
    assert buf[32:] == b"\xAA" * 32


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "graphics accelerator"))
