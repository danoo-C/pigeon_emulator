"""HID: the FIFO buffer, the real-time buffer, and the four gaps.

Each test_gap_* corresponds to a numbered gap in the plan and reproduces
the original failure, so the fixes stay fixed.

    python3 tests/test_input.py      (or: python3 -m pytest tests/)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.devices import keycodes as K                            # noqa: E402
from emulator.devices.hid import (                                    # noqa: E402
    CMD_GET_KEY_BITMAP, CMD_GET_KEY_EVENT, CMD_GET_KEY_STATE,
    CMD_GET_KEYBOARD, CMD_GET_MOUSE_BUTTONS, CMD_GET_MOUSE_EVENT,
    CMD_GET_MOUSE_POS, CMD_NOP, HID, KEY_BITMAP_BYTES)


def read(hid, command, address=0, length=4):
    return hid.callback(0, command, length, address, bytearray())


def bits_set(bitmap):
    return [i for i in range(len(bitmap) * 8) if bitmap[i >> 3] >> (i & 7) & 1]


# --- the four gaps ----------------------------------------------------------

@cases(("right", K.KEY_RIGHT), ("up", K.KEY_UP), ("f1", K.KEY_F1), ("left", K.KEY_LEFT))
def test_gap1_named_keys_survive_intact(name, code):
    """Gap 1: push_key used to mask to a byte, so pygame's Right arrow
    (1073741903) reached the guest as 79 -- the letter 'O'. Named keys now
    live in 0x80-0x9F and arrive unchanged."""
    hid = HID()
    hid.push_key(code)
    assert read(hid, CMD_GET_KEYBOARD, length=1)[0] == code
    assert not K.is_printable(code), f"{name} must not collide with ASCII"


def test_gap1_out_of_range_codes_are_rejected_not_truncated():
    """A front end that forgets to translate must fail loudly. Masking is
    what produced plausible-but-wrong letters."""
    hid = HID()
    hid.push_key(1073741903)                      # raw pygame K_RIGHT
    assert read(hid, CMD_GET_KEYBOARD, length=1)[0] == 0, "queue should be empty"
    assert bits_set(hid.key_bitmap()) == [], "state must not record it either"


def test_gap3_key_release_is_delivered():
    """Gap 3: only KEYDOWN was forwarded, so hold-to-move was impossible."""
    hid = HID()
    hid.push_key(K.KEY_LEFT, pressed=True)
    assert hid.key_is_down(K.KEY_LEFT)
    hid.push_key(K.KEY_LEFT, pressed=False)
    assert not hid.key_is_down(K.KEY_LEFT)


def test_gap4_mouse_button_mask_tracks_edges():
    """Gap 4: nothing ever posted /mouse_buttons and push_mouse_event did
    not update the mask, so CMD_GET_MOUSE_BUTTONS always returned 0."""
    hid = HID()
    assert read(hid, CMD_GET_MOUSE_BUTTONS, length=1)[0] == 0
    hid.push_mouse_event(0, True)
    assert read(hid, CMD_GET_MOUSE_BUTTONS, length=1)[0] == 0b001
    hid.push_mouse_event(1, True)
    assert read(hid, CMD_GET_MOUSE_BUTTONS, length=1)[0] == 0b011
    hid.push_mouse_event(0, False)
    assert read(hid, CMD_GET_MOUSE_BUTTONS, length=1)[0] == 0b010


# --- the real-time buffer ---------------------------------------------------

def test_realtime_bitmap_is_a_snapshot_not_a_queue():
    """Reading it twice gives the same answer -- that is the whole point."""
    hid = HID()
    for code in (ord('w'), ord('a'), K.KEY_UP):
        hid.push_key(code, pressed=True)
    first = read(hid, CMD_GET_KEY_BITMAP)
    second = read(hid, CMD_GET_KEY_BITMAP)
    assert first == second, "a real-time read must not consume anything"
    assert len(first) == KEY_BITMAP_BYTES
    assert bits_set(first) == sorted([ord('a'), ord('w'), K.KEY_UP])


def test_realtime_single_key_query_uses_the_address_field():
    hid = HID()
    hid.push_key(K.KEY_DOWN, pressed=True)
    assert read(hid, CMD_GET_KEY_STATE, address=K.KEY_DOWN)[0] == 1
    assert read(hid, CMD_GET_KEY_STATE, address=K.KEY_UP)[0] == 0


def test_realtime_mouse_position_packing():
    hid = HID()
    hid.set_mouse_pos(37, 42)
    packed = int.from_bytes(read(hid, CMD_GET_MOUSE_POS), "little")
    assert (packed >> 16, packed & 0xFFFF) == (37, 42)


# --- the FIFO buffer --------------------------------------------------------

def test_fifo_preserves_order_and_both_edges():
    hid = HID()
    hid.push_key(ord('x'), pressed=True)
    hid.push_key(ord('x'), pressed=False)
    hid.push_key(K.KEY_ESC, pressed=True)

    expected = [(True, ord('x')), (False, ord('x')), (True, K.KEY_ESC)]
    for want_press, want_code in expected:
        flags, code = read(hid, CMD_GET_KEY_EVENT, length=2)
        assert flags & 0x80, "VALID bit missing on a real event"
        assert bool(flags & 0x40) is want_press
        assert code == want_code
    assert read(hid, CMD_GET_KEY_EVENT, length=2) == b"\x00\x00", "should be drained"


def test_fifo_survives_a_press_release_between_polls():
    """The reason the FIFO exists: a fast tap must not vanish."""
    hid = HID()
    hid.push_key(ord('q'), pressed=True)
    hid.push_key(ord('q'), pressed=False)
    assert not hid.key_is_down(ord('q')), "real-time state correctly shows it is up"
    flags, code = read(hid, CMD_GET_KEY_EVENT, length=2)
    assert flags & 0x40 and code == ord('q'), "but the FIFO still has the press"


def test_key_event_distinguishes_code_zero_from_empty():
    """The character FIFO returns 0x00 for 'empty', which a real key code
    of 0 is indistinguishable from. The 2-byte event form has a VALID bit."""
    hid = HID()
    hid.push_key(0, pressed=True)
    flags, code = read(hid, CMD_GET_KEY_EVENT, length=2)
    assert flags & 0x80 and code == 0
    assert read(hid, CMD_GET_KEY_EVENT, length=2)[0] & 0x80 == 0, "empty has no VALID"


# --- protocol hygiene -------------------------------------------------------

@cases(CMD_GET_KEYBOARD, CMD_GET_MOUSE_EVENT, CMD_GET_KEY_EVENT)
def test_a_write_does_not_drain_a_fifo(command):
    """io_controller discards a write's return value, so popping on a
    write silently loses input."""
    hid = HID()
    hid.push_key(ord('z'), pressed=True)
    hid.push_mouse_event(0, True)
    assert hid.callback(1, command, 4, 0, bytearray()) == b""
    assert len(hid._key_queue) == 1 and len(hid._mouse_event_queue) == 1


@cases((CMD_GET_MOUSE_POS, 4), (CMD_GET_MOUSE_BUTTONS, 1),
       (CMD_GET_KEY_EVENT, 2), (CMD_GET_KEY_BITMAP, KEY_BITMAP_BYTES),
       (CMD_GET_KEY_STATE, 1), (CMD_GET_KEYBOARD, 1), (CMD_GET_MOUSE_EVENT, 1))
def test_reply_size_is_fixed_regardless_of_length(command, expected):
    """Tying the reply to `length` is how a word-sized read of the key
    FIFO silently pops four keys and looks at one."""
    hid = HID()
    for length in (0, 1, 4, 64):
        assert len(hid.callback(0, command, length, 0, bytearray())) == expected


def test_nop_still_echoes_length_zero_bytes():
    assert HID().callback(0, CMD_NOP, 8, 0, bytearray()) == b"\x00" * 8


def test_queue_overflow_drops_the_oldest():
    hid = HID()
    for i in range(300):
        hid.push_key(i & 0x7F, pressed=True)
    assert len(hid._key_queue) == 256, "bounded"


# --- the two keycode tables must agree --------------------------------------

def test_javascript_keycode_table_matches_python():
    """display/index.html mirrors keycodes.py in JS. Two hand-maintained
    tables drift; this stops them."""
    html = (REPO_ROOT / "display" / "index.html").read_text()
    block = re.search(r"const KEYS = \{(.*?)\};", html, re.DOTALL)
    assert block, "could not find the KEYS table in index.html"

    js = dict(re.findall(r"([A-Za-z][\w]*|'\s')\s*:\s*(0x[0-9A-Fa-f]+)", block.group(1)))
    js = {name: int(value, 16) for name, value in js.items()}
    assert js, "parsed an empty JS table"

    # browser event.code name -> the keycodes.py constant it must equal
    expected = {
        "ArrowLeft": K.KEY_LEFT, "ArrowRight": K.KEY_RIGHT,
        "ArrowUp": K.KEY_UP, "ArrowDown": K.KEY_DOWN,
        "Home": K.KEY_HOME, "End": K.KEY_END,
        "PageUp": K.KEY_PGUP, "PageDown": K.KEY_PGDN,
        "Insert": K.KEY_INSERT, "Delete": K.KEY_DELETE,
        "Backspace": K.KEY_BACKSPACE, "Tab": K.KEY_TAB,
        "Enter": K.KEY_ENTER, "Escape": K.KEY_ESC,
        "ShiftLeft": K.KEY_LSHIFT, "ShiftRight": K.KEY_RSHIFT,
        "ControlLeft": K.KEY_LCTRL, "ControlRight": K.KEY_RCTRL,
        "AltLeft": K.KEY_LALT, "AltRight": K.KEY_RALT,
    }
    expected.update({f"F{n}": K.key_f(n) for n in range(1, 13)})

    for name, want in expected.items():
        assert name in js, f"index.html is missing {name}"
        assert js[name] == want, (
            f"index.html has {name}=0x{js[name]:02X}, keycodes.py says 0x{want:02X}")


def _node():
    """The `node` binary, or None. Browser-side logic is otherwise
    untestable from here, but node is not a dependency of this project --
    skip rather than fail when it is absent."""
    return shutil.which("node")


def _browser_script():
    return (REPO_ROOT / "display" / "index.html").read_text()


def test_the_browser_page_is_valid_javascript():
    """A syntax error in the page is silent: the browser logs it to a
    console nobody is watching and the screen simply stays blank."""
    node = _node()
    if node is None:
        print("      (node not installed -- browser script not checked)")
        return
    script = re.search(r"<script>(.*?)</script>", _browser_script(), re.DOTALL)
    assert script, "could not find the script block in index.html"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script.group(1))
        path = f.name
    try:
        done = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert done.returncode == 0, f"index.html has a syntax error:\n{done.stderr}"
    finally:
        os.unlink(path)


def _outbox_source():
    """The page's post()/drain() pair, lifted out so node can run it."""
    html = _browser_script()
    assert "const outbox = [];" in html, (
        "index.html no longer has the outbox that serialises HID posts -- "
        "without it several requests are in flight at once and arrive in "
        "any order, which the guest reads as the pointer jumping")
    start = html.index("const outbox = [];")
    end = html.index("function drain(){", start)
    end = html.index("\n    }\n", end) + len("\n    }\n")
    return html[start:end]


HARNESS_TOP = """
var HID = 'http://hid';
var inFlight = 0, maxInFlight = 0;
var sent = [], resolvers = [];
globalThis.fetch = function (url, opts) {
  inFlight++; if (inFlight > maxInFlight) maxInFlight = inFlight;
  sent.push(url.slice(HID.length) + ' ' + opts.body);
  return new Promise(function (res) {
    resolvers.push(function () { inFlight--; res({}); });
  });
};
"""

HARNESS_BOTTOM = """
post('/mouse_pos',   {x:1, y:1});
post('/mouse_pos',   {x:2, y:2});
post('/mouse_event', {button:0, pressed:true});
post('/mouse_pos',   {x:3, y:3});
post('/mouse_pos',   {x:4, y:4});
post('/key',         {code:65, pressed:true});
post('/mouse_event', {button:0, pressed:false});

(async function () {
  while (resolvers.length) {
    resolvers.shift()();
    await new Promise(r => setTimeout(r, 0));
  }
  console.log(JSON.stringify({maxInFlight: maxInFlight, sent: sent}));
})();
"""


def _run_outbox():
    node = _node()
    if node is None:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(HARNESS_TOP + _outbox_source() + HARNESS_BOTTOM)
        path = f.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


def test_the_browser_sends_one_request_at_a_time():
    """fetch() does not preserve order, and the guest reads a drag as the
    difference between successive positions -- so a stale /mouse_pos
    arriving after a fresh one is one jump across the screen. Keeping a
    single request in flight is what rules that out.

    This is the difference between the two front ends: the pygame client
    posts synchronously from its event loop and is ordered for free.
    """
    result = _run_outbox()
    if result is None:
        print("      (node not installed -- browser outbox not checked)")
        return
    assert result["maxInFlight"] == 1, (
        f"{result['maxInFlight']} requests were in flight at once; "
        "they can then arrive in any order")


def test_the_browser_collapses_stale_positions_but_keeps_every_edge():
    """Only the newest position means anything, so a backlog of them is
    worse than useless -- it arrives after the gesture is over. Button and
    key edges are the opposite: each one is discrete and two in a row do
    not mean the same as one."""
    result = _run_outbox()
    if result is None:
        print("      (node not installed -- browser outbox not checked)")
        return
    sent = result["sent"]

    assert sent == [
        '/mouse_pos {"x":1,"y":1}',
        '/mouse_pos {"x":2,"y":2}',
        '/mouse_event {"button":0,"pressed":true}',
        '/mouse_pos {"x":4,"y":4}',          # (3,3) collapsed into it
        '/key {"code":65,"pressed":true}',
        '/mouse_event {"button":0,"pressed":false}',
    ], f"unexpected delivery:\n  " + "\n  ".join(sent)


def test_the_browser_anchors_a_click_to_a_position():
    """mousedown used to carry no position, so a press landed on whatever
    the last throttled mousemove had reported -- up to a frame of travel
    away, and the whole of that gap became the first drag delta."""
    html = _browser_script()
    down = re.search(r"canvas\.addEventListener\('mousedown'.*?\}\);", html, re.DOTALL)
    assert down, "could not find the mousedown handler"
    body = down.group(0)
    assert "/mouse_pos" in body, "mousedown does not post a position"
    assert body.index("/mouse_pos") < body.index("/mouse_event"), (
        "mousedown posts the button before the position it happened at")


def _pygame_client():
    """display/display.py, or None when pygame is not installed.

    pygame is an optional client dependency (requirements-client.txt) and
    the emulator core deliberately does not need it, so the suite has to
    survive its absence -- but it must not pass quietly on a machine that
    HAS it and is broken.
    """
    try:
        import pygame                                        # noqa: F401
    except ImportError:
        return None
    sys.path.insert(0, str(REPO_ROOT / "display"))
    import display as client
    return client


def test_shifted_characters_survive_the_pygame_client():
    """Shift+9 must reach the guest as '(', not as '9'.

    pygame's event.key is the PHYSICAL key -- K_9 whether or not shift is
    held, and K_9 == 57 == ord('9') -- so a client that reads only
    event.key can never deliver a shifted character: no parentheses, no
    '*', '^' or '+', no capitals. event.unicode is the field that knows
    about shift AND about the keyboard layout, which matters because '('
    is Shift+9 on a US layout and Shift+8 on a Slovak one.
    """
    client = _pygame_client()
    if client is None:
        print("      (pygame not installed -- pygame client not checked)")
        return
    import pygame

    # (physical key, what the layout produced, what the guest must get)
    cases = [
        (pygame.K_9, "(", ord("(")),      # US
        (pygame.K_0, ")", ord(")")),      # US
        (pygame.K_8, "(", ord("(")),      # Slovak: same physical key, other glyph
        (pygame.K_9, ")", ord(")")),      # Slovak
        (pygame.K_8, "*", ord("*")),
        (pygame.K_6, "^", ord("^")),
        (pygame.K_a, "A", ord("A")),
        (pygame.K_9, "9", ord("9")),      # unshifted still works
        (pygame.K_a, "a", ord("a")),
    ]
    for key, typed, want in cases:
        got = client.to_pigeon_key(key, typed)
        assert got == want, (
            f"typing {typed!r} on physical key {key} reached the guest as "
            f"{got!r}, expected {want!r} ({chr(want)!r})")


def test_named_keys_beat_the_character_they_carry():
    """Return, Escape, Backspace and Tab carry a control character in
    event.unicode, and Space carries a printable one. The named mapping
    has to win, or Return would arrive as 0x0D by a different route and
    Space would depend on which branch ran first."""
    client = _pygame_client()
    if client is None:
        print("      (pygame not installed -- pygame client not checked)")
        return
    import pygame

    for key, carried, want in [
        (pygame.K_RETURN, "\r", K.KEY_ENTER),
        (pygame.K_ESCAPE, "\x1b", K.KEY_ESC),
        (pygame.K_BACKSPACE, "\x08", K.KEY_BACKSPACE),
        (pygame.K_TAB, "\t", K.KEY_TAB),
        (pygame.K_SPACE, " ", K.KEY_SPACE),
        (pygame.K_LEFT, "", K.KEY_LEFT),
    ]:
        assert client.to_pigeon_key(key, carried) == want


def test_the_pygame_client_drops_what_it_cannot_encode():
    """The key FIFO is one byte wide and the HID device REJECTS anything
    above 0xFF, so a layout that produces a non-ASCII character must be
    dropped here rather than truncated into a plausible wrong letter."""
    client = _pygame_client()
    if client is None:
        print("      (pygame not installed -- pygame client not checked)")
        return
    assert client.to_pigeon_key(225, "\u00e1") is None       # a-acute
    assert client.to_pigeon_key(0x11B, "\u010d") is None     # c-caron


def test_pigeon_keycodes_do_not_collide():
    codes = sorted(K.NAMED_KEYS.values())
    assert len(codes) == len(set(codes)), "duplicate keycode"
    assert all(0 <= c <= K.KEY_MAX for c in codes)
    named_non_ascii = [c for c in codes if not K.is_printable(c) and c >= 0x80]
    assert all(0x80 <= c <= 0x9F or c == K.KEY_DELETE for c in named_non_ascii)


# --- the guest's view, through the real IO bus -------------------------------

def test_guest_reads_input_through_the_io_controller():
    """End to end: program the IO header, fire channel 3, read the reply --
    exactly the sequence compiler/design/07-input.md specifies."""
    from emulator.io_controller import IOChannel, IOController
    from emulator.memory_map import CH_HID, IO_START, IOHeader, RAM_SIZE
    from emulator.ram import RAM

    ram = RAM(RAM_SIZE)
    io = IOController(ram)
    hid = HID()
    io.register_channel(CH_HID, IOChannel(hid.callback, "HID"))

    hid.set_mouse_pos(11, 22)
    hid.push_key(K.KEY_UP, pressed=True)

    def guest_read(command, address=0):
        ram.write_word(IO_START + IOHeader.IO_R_W, 0)
        ram.write_word(IO_START + IOHeader.COMMAND, command)
        ram.write_word(IO_START + IOHeader.LENGTH, 4)
        ram.write_word(IO_START + IOHeader.ADDRESS, address)
        ram.write_word(IO_START + IOHeader.IO_CHANNEL, CH_HID)
        io.update()
        base = IO_START + IOHeader.USABLE_AFTER
        length = ram.read_word(IO_START + IOHeader.RETURN_DATA)
        return bytes(ram.mem[base:base + length])

    packed = int.from_bytes(guest_read(CMD_GET_MOUSE_POS), "little")
    assert (packed >> 16, packed & 0xFFFF) == (11, 22)
    assert guest_read(CMD_GET_KEY_STATE, address=K.KEY_UP)[0] == 1
    assert len(guest_read(CMD_GET_KEY_BITMAP)) == KEY_BITMAP_BYTES
    assert bits_set(guest_read(CMD_GET_KEY_BITMAP)) == [K.KEY_UP]


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "HID input"))
