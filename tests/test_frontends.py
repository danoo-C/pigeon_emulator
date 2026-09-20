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
    BANDS, FRAME_FORMAT, DisplayIO, frame_reply, frame_since, info_reply, preferred_reply)
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
    assert headers == {"X-Pigeon-Mode": f"{DISPLAY_W},{DISPLAY_H},0", "X-Pigeon-Frame": "1"}


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


# --- the server: frames that did not change (docs/gac/plans/phase8_bandwidth.md) ----------

PITCH = DISPLAY_W * 4


def paint_row(ram, row, colour):
    ram.mem[DISPLAY_START + row * PITCH:DISPLAY_START + (row + 1) * PITCH] = (
        struct.pack("<I", colour) * DISPLAY_W)


def test_the_same_picture_is_not_a_new_frame():
    ram, display, _ = machine()
    assert display.update() is True
    assert display.update() is False, "an unchanged picture made a new frame"
    paint_row(ram, 5, 0xFF112233)
    assert display.update() is True
    assert frame_reply(display)[1]["X-Pigeon-Frame"] == "2"


def test_since_the_current_frame_is_nothing():
    ram, display, _ = machine()
    display.update()
    status, data, headers = frame_since(display, 1)
    assert (status, data, headers["X-Pigeon-Frame"]) == (204, b"", "1")


def test_since_an_older_frame_is_the_rows_that_changed():
    ram, display, _ = machine()
    display.update()
    old = frame_reply(display)[0]
    paint_row(ram, 40, 0xFFFF0000)
    display.update()
    paint_row(ram, 7, 0xFF00FF00)                 # two frames later, higher up
    display.update()
    status, data, headers = frame_since(display, 1)
    assert status == 200 and headers["X-Pigeon-Rows"] == "7,40", headers
    assert len(data) == (40 - 7 + 1) * PITCH
    patched = bytearray(old)
    patched[7 * PITCH:41 * PITCH] = data
    assert bytes(patched) == frame_reply(display)[0], "the band did not make the frame"
    status, data, headers = frame_since(display, 2)
    assert headers["X-Pigeon-Rows"] == "7,7" and len(data) == PITCH


@cases(("too old", -BANDS - 5), ("from the future", 99), ("never served", -1))
def test_since_a_frame_it_cannot_answer_from_is_the_whole_frame(label, since):
    ram, display, _ = machine()
    for row in range(BANDS + 3):
        paint_row(ram, row % DISPLAY_H, 0xFF000000 | row)
        display.update()
    number = int(frame_reply(display)[1]["X-Pigeon-Frame"])
    if since < -1:
        since = number + since                      # further back than BANDS frames
    status, data, headers = frame_since(display, since)
    assert status == 200 and headers["X-Pigeon-Rows"] == f"0,{DISPLAY_H - 1}", label
    assert data == frame_reply(display)[0], label


def test_a_mode_change_is_the_whole_frame():
    ram, display, device = machine()
    display.update()
    device.set_mode(640, 360)
    display.update()
    status, data, headers = frame_since(display, 1)
    assert headers["X-Pigeon-Rows"] == "0,359" and len(data) == 640 * 360 * 4
    assert headers["X-Pigeon-Mode"] == "640,360,1"


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
    assert "pixels = whole ? toRGBA(buf) : applyBand(pixels, buf, rows.first, W)" in loop
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


# --- the mode picker (docs/gac/plans/phase6_console.md §3) ---------------------------------------

def test_the_pickers_offer_every_mode_with_the_current_one_chosen():
    modes = [list(m) for m in DISPLAY_MODES]
    got = node("console.log(JSON.stringify(modeOptions(%s, 640, 360)));" % json.dumps(modes))
    assert [o["value"] for o in got] == [f"{w}x{h}" for w, h in DISPLAY_MODES]
    assert [o["selected"] for o in got] == [m == (640, 360) for m in DISPLAY_MODES]
    assert got[2]["label"] == "640 x 360"
    items = SM.mode_items(modes, 640, 360)
    assert [(i["w"], i["h"]) for i in items] == list(DISPLAY_MODES)
    assert [i["current"] for i in items] == [o["selected"] for o in got]
    assert items[2]["name"].startswith(SM.mode_label(640, 360)) == (got[2]["label"] == "640 x 360")


def test_a_machine_without_modes_offers_the_one_it_has():
    got = node("console.log(JSON.stringify(modeOptions(null, 192, 108)));")
    assert got == [{"value": "192x108", "label": "192 x 108", "selected": True}]
    assert [(i["w"], i["h"], i["current"]) for i in SM.mode_items(None, 192, 108)] == [
        (192, 108, True)]


def test_the_pickers_only_ask_and_follow_the_mode():
    """Choosing posts /preferred and nothing else; the picker shows the
    mode whoever changed it."""
    source = page()
    assert '<select id="mode"' in source
    wiring = between(source, "function wireMode(){", "// The room the canvas has")
    assert "fetch('/preferred', {method: 'POST'" in wiring
    assert "switches at the prompt" in wiring
    set_mode = between(source, "function setMode(w, h){", "scaleInput.addEventListener")
    assert "showModes()" in set_mode and "modeNote.hidden = true" in set_mode
    client = CLIENT.read_text()
    assert 'add("Mode", self._choose_mode)' in client
    ask = between(client, "def _ask_for_mode(self, item):", "def _open_picker(")
    assert 'f"{self.base_url}/preferred"' in ask


# --- bands of changed rows, in the clients (docs/gac/plans/phase8_bandwidth.md) ----------------

ROWS_HEADERS = (
    ("0,107", [0, 107]),
    ("7,7", [7, 7]),
    (None, None),
    ("", None),
    ("9,3", None),                  # last before first
    ("-1,5", None),
    ("1,2,3", None),
)


def test_both_clients_read_a_band_header_alike():
    got = node("console.log(JSON.stringify(%s.map(h => { const r = parseRows(h); "
               "return r && [r.first, r.last]; })));" % json.dumps([h for h, _ in ROWS_HEADERS]))
    assert got == [want for _, want in ROWS_HEADERS]
    for header, want in ROWS_HEADERS:
        got = SM.parse_rows(header)
        assert (list(got) if got else None) == want, header


def test_a_band_put_into_the_old_frame_is_the_new_frame_in_both_clients():
    """The server's own band, from frame_since, applied by each client's
    logic: the page's (which swizzles to R,G,B,A) and pygame's (which keeps
    B,G,R,A) -- each must come out as the whole new frame."""
    ram, display, _ = machine()
    ram.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE] = bytes(range(256)) * (DISPLAY_SIZE // 256)
    display.update()
    old = frame_reply(display)[0]
    ram.mem[DISPLAY_START + 30 * PITCH:DISPLAY_START + 33 * PITCH] = b"\x11\x22\x33\x44" * (3 * DISPLAY_W)
    display.update()
    new = frame_reply(display)[0]
    status, band, headers = frame_since(display, 1)
    first, last = SM.parse_rows(headers["X-Pigeon-Rows"])
    assert (first, last) == (30, 32)
    assert bytes(SM.apply_band(bytearray(old), band, first, DISPLAY_W)) == new
    got = node("const f = b => { const x = Buffer.from(b, 'base64'); "
               "return x.buffer.slice(x.byteOffset, x.byteOffset + x.length); };"
               "const pixels = toRGBA(f(%r));"
               "applyBand(pixels, f(%r), %d, %d);"
               "console.log(JSON.stringify(Buffer.from(pixels.buffer).toString('base64')));"
               % (base64.b64encode(old).decode(), base64.b64encode(band).decode(), first, DISPLAY_W))
    want = bytearray(new)
    want[0::4], want[2::4] = new[2::4], new[0::4]
    want[3::4] = b"\xff" * (len(new) // 4)
    assert base64.b64decode(got) == bytes(want)


def test_both_clients_ask_for_what_changed_and_build_on_what_they_hold():
    loop = between(page(), "async function loop(){", "// Translate a browser event")
    assert "`/frame?since=${frameNo}`" in loop and "resp.status !== 204" in loop
    assert "parseRows(resp.headers.get('X-Pigeon-Rows'))" in loop
    set_mode = between(page(), "function setMode(w, h){", "scaleInput.addEventListener")
    assert "pixels = null" in set_mode and "frameNo = null" in set_mode
    client = CLIENT.read_text()
    fetch = between(client, "def _fetch_loop(self):", "def _set_display_connected")
    assert '"?since={number}"' in fetch and "resp.status_code != 204" in fetch
    assert "SM.apply_band(held, band, rows[0], w)" in fetch
    assert "rest = 1.0 / self.fps" in fetch, "the fetch loop is not paced"


def test_the_real_server_sends_nothing_for_a_picture_that_did_not_change():
    """End to end, over HTTP: an unchanged screen is a 204 with no body; one
    changed row is one row."""
    import time
    import requests
    from emulator.machine import Machine
    with tempfile.TemporaryDirectory() as d:
        m = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"),
                    disk_path=str(Path(d) / "hdd.img"))
        try:
            m.start_servers(host="127.0.0.1", display_port=18820, hid_port=18821, cd_port=18822)
            base = "http://127.0.0.1:18820"
            for _ in range(100):
                try:
                    requests.get(base + "/info", timeout=1)
                    break
                except Exception:
                    time.sleep(0.05)
            m.display_io.update()
            whole = requests.get(base + "/frame", timeout=5)
            number = whole.headers["X-Pigeon-Frame"]
            m.display_io.update()                         # nothing changed
            same = requests.get(f"{base}/frame?since={number}", timeout=5)
            assert same.status_code == 204 and same.content == b""
            m.ram.mem[DISPLAY_START + 50 * PITCH:DISPLAY_START + 51 * PITCH] = b"\xff" * PITCH
            m.display_io.update()
            one = requests.get(f"{base}/frame?since={number}", timeout=5)
            assert one.headers["X-Pigeon-Rows"] == "50,50" and len(one.content) == PITCH
        finally:
            m.close()


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
