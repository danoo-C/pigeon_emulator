"""The front ends follow the mode (docs/gac/plans/phase4_frontends.md).

The server's replies are plain functions, called here without a server, as
tests/test_serial.py does with serial_reply. The browser page's logic runs
under node, as tests/test_serial_panel.py runs the Serial panel's; the
pygame client's is display/screen_mode.py, which imports no pygame. The two
clients' logic is held to one table of cases, so they cannot drift apart.

    python3 tests/test_frontends.py      (or: python3 -m pytest tests/)
"""
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "display"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import screen_mode as SM                                              # noqa: E402
from _runner import cases, run_module                                 # noqa: E402
from emulator.devices.display_io import (                             # noqa: E402
    FRAME_FORMAT, DisplayIO, frame_reply, info_reply, preferred_reply)
from emulator.devices.vram import CMD_PREFERRED, CMD_SET_MODE, VRAM    # noqa: E402
from emulator.io_controller import IOChannel, IOController           # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    CH_VRAM, DISPLAY_H, DISPLAY_MODES, DISPLAY_SIZE, DISPLAY_START, DISPLAY_W, IO_START,
    IOHeader)
from emulator.ram import RAM                                          # noqa: E402

PAGE = REPO_ROOT / "display" / "index.html"
CLIENT = REPO_ROOT / "display" / "display.py"
WINDOW = IO_START + IOHeader.USABLE_AFTER
# Small, as in test_vram.py: a worker per CPU.
TEST_RAM = 1 << 24
TEST_VRAM = 1 << 23


def machine(vram=True):
    """RAM, its DisplayIO, and -- with video memory -- the VRAM device."""
    ram = RAM(TEST_RAM, TEST_VRAM if vram else 0)
    display = DisplayIO(ram)
    device = VRAM(ram, display, DISPLAY_MODES) if vram else None
    return ram, display, device


def set_mode_on_the_bus(ram, device, w, h):
    """SET_MODE as a guest sends it: the window, the header, the controller."""
    controller = IOController(ram)
    controller.register_channel(CH_VRAM, IOChannel(device.callback, name="VRAM"))
    struct.pack_into("<II", ram.mem, WINDOW, w, h)
    for field, value in ((IOHeader.IO_R_W, 0), (IOHeader.COMMAND, CMD_SET_MODE),
                         (IOHeader.LENGTH, 20), (IOHeader.ADDRESS, 0),
                         (IOHeader.IO_CHANNEL, CH_VRAM)):
        ram.write_word(IO_START + field, value)
    controller.update()
    assert struct.unpack_from("<I", ram.mem, WINDOW)[0] == 1, "SET_MODE refused"


# --- the server: /frame ---------------------------------------------------------------

def test_a_frame_is_the_bytes_as_they_are_in_memory_with_its_mode():
    ram, display, _ = machine()
    ram.mem[DISPLAY_START:DISPLAY_START + 4] = b"\x11\x22\x33\x44"
    display.update()
    data, headers = frame_reply(display)
    assert len(data) == DISPLAY_SIZE
    assert data[:4] == b"\x11\x22\x33\x44", "the server swizzled"
    assert headers == {"X-Pigeon-Mode": f"{DISPLAY_W},{DISPLAY_H},0"}


def test_a_frame_after_a_mode_change_is_the_new_size_and_says_so():
    ram, display, device = machine()
    set_mode_on_the_bus(ram, device, 640, 360)
    ram.write_word(ram.vram_base + device.surfaces[0].offset, 0xFF123456)
    display.update()
    data, headers = frame_reply(display)
    assert len(data) == 640 * 360 * 4
    assert headers["X-Pigeon-Mode"] == "640,360,1"
    assert data[:4] == b"\x56\x34\x12\xff"


def test_a_frame_keeps_the_mode_it_was_drawn_in():
    """Captured together: a switch after the snapshot does not relabel it."""
    ram, display, device = machine()
    display.update()
    device.set_mode(640, 360)
    data, headers = frame_reply(display)
    assert len(data) == DISPLAY_SIZE and headers["X-Pigeon-Mode"] == f"{DISPLAY_W},{DISPLAY_H},0"
    display.update()
    data, headers = frame_reply(display)
    assert len(data) == 640 * 360 * 4 and headers["X-Pigeon-Mode"] == "640,360,1"


def test_before_the_first_update_and_after_clear_the_frame_is_black_at_the_mode():
    ram, display, device = machine()
    ram.mem[DISPLAY_START:DISPLAY_START + 4] = b"\xff" * 4
    data, headers = frame_reply(display)
    assert data == bytes(DISPLAY_SIZE)
    display.update()
    device.set_mode(320, 180)
    display.clear()
    data, headers = frame_reply(display)
    assert data == bytes(320 * 180 * 4) and headers["X-Pigeon-Mode"] == "320,180,1"


def test_a_machine_without_video_memory_is_generation_zero_forever():
    _, display, _ = machine(vram=False)
    display.update()
    assert frame_reply(display)[1]["X-Pigeon-Mode"] == f"{DISPLAY_W},{DISPLAY_H},0"


# --- the server: /info and /preferred ------------------------------------------------

def test_info_says_what_the_front_ends_need_to_follow_the_mode():
    _, display, device = machine()
    device.set_mode(640, 360)
    info = info_reply(display)
    assert (info["w"], info["h"], info["size"]) == (640, 360, 640 * 360 * 4)
    assert info["generation"] == 1 and info["format"] == FRAME_FORMAT == "bgra"
    assert info["modes"] == [list(m) for m in DISPLAY_MODES]
    assert info["preferred"] == [0, 0]
    assert json.loads(json.dumps(info)) == info, "not JSON"


def test_info_without_video_memory_offers_the_one_mode_it_has():
    _, display, _ = machine(vram=False)
    info = info_reply(display)
    assert info["modes"] == [[DISPLAY_W, DISPLAY_H]] and info["generation"] == 0
    assert info["preferred"] == [0, 0]


def test_preferred_is_stored_for_the_guest_and_switches_nothing():
    _, display, device = machine()
    assert preferred_reply(display, {"w": 854, "h": 480}) == (200, {"preferred": [854, 480]})
    assert device.mode == (DISPLAY_W, DISPLAY_H), "the host switched the mode"
    assert info_reply(display)["preferred"] == [854, 480]
    reply = device.callback(0, CMD_PREFERRED, 4, 0, bytearray(4))
    assert struct.unpack("<III", reply) == (854, 480, 1)


@cases(({"w": 641, "h": 360}, "not an offered mode"), ({"w": 640}, "expected"),
       ({"w": "wide", "h": 3}, "expected"), ([640, 360], "expected"))
def test_preferred_refuses_anything_but_an_offered_mode(body, expected):
    _, display, device = machine()
    status, reply = preferred_reply(display, body)
    assert status == 400 and expected in reply["error"], reply
    assert device.preferred == (0, 0, 0)


def test_preferred_without_video_memory_is_not_found():
    _, display, _ = machine(vram=False)
    assert preferred_reply(display, {"w": 640, "h": 360})[0] == 404


# --- the two clients' logic, one table of cases ----------------------------------------

MODES = (
    ("640,360,3", [640, 360, 3]),
    ("192,108,0", [192, 108, 0]),
    (None, None),
    ("", None),
    ("640,360", None),
    ("640,360,3,4", None),
    ("0,360,1", None),
    ("640,-1,1", None),
    ("a,b,c", None),
)

FITS = (   # w, h, room_w, room_h, wanted -> scale
    ((192, 108, 1900, 1000, 4), 4),     # a small mode stays at yours
    ((1280, 720, 1900, 1000, 4), 1),    # a big one comes down to what fits
    ((640, 360, 1900, 1000, 4), 2),
    ((1280, 720, 800, 600, 4), 1),      # never below 1, even when nothing fits
    ((192, 108, 1900, 1000, 16), 9),
    ((192, 108, 1900, 1000, 0), 1),
)


def test_the_pygame_client_parses_a_mode_as_the_page_does():
    for header, want in MODES:
        got = SM.parse_mode(header)
        assert (list(got) if got else None) == want, header


def test_the_pygame_client_fits_as_the_page_does():
    for args, want in FITS:
        assert SM.fit_pixel_size(*args) == want, args


def test_the_room_on_the_desktop_leaves_the_toolbar_and_the_panel_out():
    w, h = SM.room_for_screen(1920, 1080, 300, 40)
    assert w == 1920 - SM.DESKTOP_MARGIN_W - 300 and h == 1080 - SM.DESKTOP_MARGIN_H - 40


# --- the browser page, under node -----------------------------------------------------------

def page():
    return PAGE.read_text()


def between(text, start, end):
    """The source from `start` to the first `end` after it."""
    assert start in text, f"lost {start!r}"
    at = text.index(start)
    assert end in text[at:], f"lost {end!r} after {start!r}"
    return text[at:text.index(end, at)]


def screen_logic():
    return between(page(), "// ---- the screen's logic", "// ---- end of the screen's logic")


def node(script):
    """The page's screen logic and then `script`, under node; the last line
    it prints, as JSON. node is not optional here, as for the Serial panel."""
    binary = shutil.which("node")
    assert binary, "node is needed to run the page's logic (docs/phase5c_plan.md §1)"
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(screen_logic() + "\n" + script)
        path = f.name
    try:
        done = subprocess.run([binary, path], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


def test_the_page_parses_a_mode():
    got = node("console.log(JSON.stringify(%s.map(h => { const m = parseMode(h); "
               "return m && [m.w, m.h, m.generation]; })));"
               % json.dumps([header for header, _ in MODES]))
    assert got == [want for _, want in MODES]


def test_the_page_fits_a_scale():
    got = node("console.log(JSON.stringify(%s.map(a => fitScale(...a))));"
               % json.dumps([list(args) for args, _ in FITS]))
    assert got == [want for _, want in FITS]


def test_the_page_swaps_red_and_blue_and_makes_every_pixel_opaque():
    """What the server did before this phase -- test_smoke.py's reference,
    moved here with the swizzle -- plus the alpha the screen now ignores."""
    data = bytes(range(256)) * 16
    got = node("const b = Buffer.from(%r, 'base64');"
               "const ab = b.buffer.slice(b.byteOffset, b.byteOffset + b.length);"
               "console.log(JSON.stringify(Array.from(toRGBA(ab))));"
               % base64.b64encode(data).decode())
    want = []
    for i in range(0, len(data), 4):
        want += [data[i + 2], data[i + 1], data[i], 0xFF]
    assert got == want


# --- the wiring, read from the source -------------------------------------------------------

def test_the_page_follows_the_mode_on_the_frame_that_brings_it():
    """No DOM under node, so the wiring is checked by reading it, as the
    Serial panel's is (tests/test_serial_panel.py)."""
    loop = between(page(), "async function loop(){", "// Translate a browser event")
    assert "parseMode(resp.headers.get('X-Pigeon-Mode'))" in loop
    assert "setMode(mode.w, mode.h)" in loop
    assert "new ImageData(toRGBA(buf), W, H)" in loop
    size = between(page(), "function sizeCanvas(){", "function setMode(")
    assert "fitScale(W, H, roomW, roomH," in size
    assert "window.addEventListener('resize', sizeCanvas)" in page()
    assert "canvas.width = W * Number(" not in page(), "a scale that ignores the room"


def test_the_pygame_client_follows_the_mode_on_the_frame_that_brings_it():
    source = CLIENT.read_text()
    assert 'SM.parse_mode(resp.headers.get("X-Pigeon-Mode"))' in source
    assert "self._set_mode(mode)" in source
    render = between(source, "def _render(self", "if self.serial_open:")
    assert '"BGRA").convert()' in render, "the swizzle, or dropping alpha, is gone"
    resize = between(source, "def _resize_window(self):", "screen_w =")
    assert "self._fitting_pixel_size()" in resize


# --- end to end ---------------------------------------------------------------------------------

def test_a_mode_change_on_the_bus_changes_what_the_page_draws():
    """The phase's "done when": a guest switches to 640 x 360, the next
    /frame carries it, and the page's own logic makes of that a 640 x 360
    picture with the right colours."""
    ram, display, device = machine()
    display.update()
    assert frame_reply(display)[1]["X-Pigeon-Mode"] == f"{DISPLAY_W},{DISPLAY_H},0"
    set_mode_on_the_bus(ram, device, 640, 360)
    screen = ram.vram_base + device.surfaces[0].offset
    ram.write_word(screen, 0xFFFF0000)                               # red, top left
    ram.write_word(screen + (359 * 640 + 639) * 4, 0x000000FF)       # blue, alpha 0
    display.update()
    data, headers = frame_reply(display)
    got = node("const b = Buffer.from(%r, 'base64');"
               "const ab = b.buffer.slice(b.byteOffset, b.byteOffset + b.length);"
               "const m = parseMode(%r); const px = toRGBA(ab);"
               "const last = px.length - 4;"
               "console.log(JSON.stringify({w: m.w, h: m.h, ok: px.length === m.w * m.h * 4,"
               " first: Array.from(px.slice(0, 4)), last: Array.from(px.slice(last))}));"
               % (base64.b64encode(data).decode(), headers["X-Pigeon-Mode"]))
    assert got == {"w": 640, "h": 360, "ok": True,
                   "first": [255, 0, 0, 255], "last": [0, 0, 255, 255]}


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "front ends"))
