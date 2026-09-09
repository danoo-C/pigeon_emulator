"""HID: the FIFO buffer, the real-time buffer, and the four gaps.

Each test_gap_* corresponds to a numbered gap in the plan and reproduces
the original failure, so the fixes stay fixed.

    python3 tests/test_input.py      (or: python3 -m pytest tests/)
"""
import json
import re
import sys
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
