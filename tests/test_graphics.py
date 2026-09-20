"""graphics, the drawing program: docs/graphics_plan.md.

/bin/graphics.bin draws under the kernel like any other program, so the
harness is test_kernel's console. A picture lasts only as long as the
program that drew it -- the kernel paints its console back over the screen
the moment one ends -- so every test that looks at pixels runs its command
with -wait and takes the screen while it is held.

    python3 tests/test_graphics.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import struct
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                   # noqa: E402
from emulator.devices.display_io import CMD_FILL                        # noqa: E402
from emulator.devices.keycodes import KEY_ESC                           # noqa: E402
from emulator.memory_map import CH_DISPLAY, DISPLAY_H, DISPLAY_W        # noqa: E402
from test_kernel import (                                               # noqa: E402
    PROMPT, booted, last_row, lines, shell_program)

BLACK = "0xFF000000"
MAGENTA, MAGENTA_RGB = "0xFFFF00FF", (255, 0, 255)
GREEN, GREEN_RGB = "0xFF00FF00", (0, 255, 0)

# The last thing every held command draws, in a colour no test uses: once
# this corner pixel is on the screen, everything before it has been drawn.
MARK, MARK_RGB = "0xFF010203", (1, 2, 3)


def with_graphics(*extra):
    return [("/bin/graphics.bin", shell_program("graphics"))] + list(extra)


def rgb(fb, x, y):
    """A pixel's colour. The framebuffer keeps each one blue, green, red,
    alpha -- cell_colors in test_kernel reads it the same way."""
    i = (y * DISPLAY_W + x) * 4
    return (fb[i + 2], fb[i + 1], fb[i])


def lit(fb, colour):
    return sum(1 for y in range(DISPLAY_H) for x in range(DISPLAY_W)
               if rgb(fb, x, y) == colour)


@contextlib.contextmanager
def at_the_prompt(extra=()):
    with booted(extra=with_graphics(*extra)) as c:
        assert c.ready(), c.rows()
        yield c


def type_in(c, text, seconds=30):
    """Type a line, waiting for the kernel to take each handful of keys.

    The HID's key-event queue holds 256 edges and drops the oldest when it
    is full, two edges a character -- so a command over about 120 characters
    pushed in one go loses the keys the kernel has not read yet, and these
    commands are longer than that. Running the machine for a fixed time
    between handfuls is not enough either: under a loaded parallel run a
    wall-clock slice can execute almost nothing. So this waits until the
    queue is actually empty.
    """
    give_up = time.time() + seconds
    for i in range(0, len(text), 16):
        c.type(text[i:i + 16])
        while len(c.machine.hid._key_event_queue) > 0 and time.time() < give_up:
            with contextlib.redirect_stdout(io.StringIO()):
                c.machine.run(deadline=time.time() + 0.05)


def press_until(c, key, wanted, seconds=60):
    """Press `key` until something happens on the screen.

    -wait empties the key queue before it listens, and a test cannot see the
    moment it starts: a press that lands in the microseconds between the last
    shape and the drain is thrown away with the Enter that started the
    command. A person would press again, so this does.
    """
    give_up = time.time() + seconds
    while time.time() < give_up:
        c.press(key)
        again = time.time() + 1.0
        while time.time() < again:
            with contextlib.redirect_stdout(io.StringIO()):
                c.machine.run(deadline=time.time() + 0.1)
            if wanted():
                return True
            if c.machine.cpu.halted:
                return False
    return False


def said(shown, text):
    """Is `text` among the lines? The console is 32 columns wide and a line
    exactly that long reads as a wrapped one, so its message can arrive
    joined to the next."""
    return text in "".join(shown)


def printed(c, line):
    """What the command `line` printed, the prompt back.

    test_kernel's Console.output() matches the whole echoed command, which
    these commands are too long for: the console wraps one over several
    rows, and a row that ends a character short of the width reads as a
    line of its own, so lines() cannot join them up again. The echo's last
    row is still the end of the command, so that is what this matches --
    and a message that happens to end in the same word is not a whole
    suffix of the command.
    """
    shown = [text for text in lines(c.rows()) if text.strip()]
    end = len(shown) - 1                       # the prompt, waiting for the next
    head = shown[end][:-1]                     # "2:/> ", whatever the disk is
    for i in range(end - 1, -1, -1):
        tail = shown[i][len(head):] if shown[i].startswith(head) else shown[i]
        if tail and line.endswith(tail):
            return shown[i + 1:end]
    raise AssertionError(f"{line!r} was never echoed: {shown}")


def run(c, line, seconds=90):
    """Type a command, wait for the prompt, and return what it printed."""
    before = c.rows()
    type_in(c, line + "\n")
    assert c.run_until(lambda rows: rows != before and PROMPT.match(last_row(rows)),
                       seconds), f"no prompt after {line!r}: {c.rows()}"
    return printed(c, line)


def hold(c, ops, key=KEY_ESC, seconds=90):
    """Run `graphics OPS`, holding the screen at the end: what was drawn, and
    what the command printed once `key` let it go."""
    return held(c, f"graphics {ops} -px {DISPLAY_W - 1} {DISPLAY_H - 1} {MARK} -wait",
                key, seconds)


def held(c, line, key=KEY_ESC, seconds=90):
    """Run a command that draws the mark last and then holds the screen: what
    was on it, and what was printed once `key` let it go.

    The mark is the last thing drawn and -wait keeps it there, so the screen
    can be watched from outside without racing the drawing: a picture that is
    up stays up until a key goes down. Scripts hold it the same way.
    """
    type_in(c, line + "\n")
    screen = None
    give_up = time.time() + seconds
    while time.time() < give_up:
        with contextlib.redirect_stdout(io.StringIO()):
            c.machine.run(deadline=time.time() + 0.1)
        fb = c.machine.display_io.snapshot()
        if rgb(fb, DISPLAY_W - 1, DISPLAY_H - 1) == MARK_RGB:
            screen = fb
            break
        assert not c.machine.cpu.halted, f"the machine stopped: {c.rows()}"
    assert screen is not None, f"nothing was drawn: {c.rows()}"
    assert press_until(c, key, lambda: PROMPT.match(last_row(c.rows()))), \
        f"the wait never ended: {c.rows()}"
    return screen, printed(c, line)


def fills(c):
    """The colour of every framebuffer fill asked of the display device.
    The console's own redraw is one, so tests look for their colour in the
    list rather than at its length."""
    seen = []
    channel = c.machine.io_controller.channels[CH_DISPLAY]
    real = channel.callback

    def spy(read_write, command, length, address, data):
        if command == CMD_FILL:
            seen.append(int.from_bytes(bytes(data[:4]), "little"))
        return real(read_write, command, length, address, data)

    channel.callback = spy
    return seen


# --- one operation at a time ------------------------------------------------------

def test_a_pixel_is_set_where_it_says():
    with at_the_prompt() as c:
        fb, _ = hold(c, f"-clear {BLACK} -px 10 20 {MAGENTA}")
        assert rgb(fb, 10, 20) == MAGENTA_RGB
        assert lit(fb, MAGENTA_RGB) == 1, "a pixel is one pixel"


def test_clear_fills_the_whole_screen():
    with at_the_prompt() as c:
        fb, _ = hold(c, f"-clear {MAGENTA}")
        assert rgb(fb, 0, 0) == MAGENTA_RGB and rgb(fb, DISPLAY_W - 1, 0) == MAGENTA_RGB
        # every pixel but the mark in the corner
        assert lit(fb, MAGENTA_RGB) == DISPLAY_W * DISPLAY_H - 1


def test_a_rect_is_filled_and_a_frame_is_not():
    with at_the_prompt() as c:
        fb, _ = hold(c, f"-clear {BLACK} -rect 10 10 20 15 {MAGENTA}"
                        f" -frame 50 10 20 15 {GREEN}")
        assert lit(fb, MAGENTA_RGB) == 20 * 15
        assert rgb(fb, 20, 17) == MAGENTA_RGB, "the middle of a rect is filled"
        assert lit(fb, GREEN_RGB) == 2 * 20 + 2 * 15 - 4, "a frame is its perimeter"
        assert rgb(fb, 60, 17) != GREEN_RGB, "the middle of a frame is not"


def test_a_line_reaches_both_of_its_ends():
    with at_the_prompt() as c:
        fb, _ = hold(c, f"-clear {BLACK} -line 4 4 60 40 {MAGENTA}")
        assert rgb(fb, 4, 4) == MAGENTA_RGB and rgb(fb, 60, 40) == MAGENTA_RGB


def test_a_circle_is_an_outline_and_a_disc_is_filled():
    with at_the_prompt() as c:
        fb, _ = hold(c, f"-clear {BLACK} -circle 40 54 20 {MAGENTA}"
                        f" -disc 140 54 20 {GREEN}")
        assert rgb(fb, 60, 54) == MAGENTA_RGB and rgb(fb, 40, 54) != MAGENTA_RGB
        assert rgb(fb, 160, 54) == GREEN_RGB and rgb(fb, 140, 54) == GREEN_RGB


def test_text_is_drawn_in_the_font():
    with at_the_prompt() as c:
        fb, _ = hold(c, f"-clear {BLACK} -text 4 40 Hi {MAGENTA}")
        drawn = lit(fb, MAGENTA_RGB)
        assert 6 <= drawn <= 2 * 8 * 5, f"two glyphs drew {drawn} pixels"


def test_a_shape_off_the_edge_is_clipped_not_refused():
    """The library clips the far edges; graphics clips the near ones, which
    are unsigned by the time they reach it and would otherwise wrap."""
    with at_the_prompt() as c:
        fb, printed = hold(c, f"-clear {BLACK} -rect -5 -5 20 20 {MAGENTA}"
                              f" -disc 0 54 10 {GREEN}")
        assert printed == ["27"], printed
        assert lit(fb, MAGENTA_RGB) == 15 * 15, "the corner that is on the screen"
        assert rgb(fb, 0, 54) == GREEN_RGB and rgb(fb, 9, 54) == GREEN_RGB


# --- many in one call -------------------------------------------------------------

def test_a_whole_list_is_drawn_in_order():
    """Five operations, one program load: the reason the argument list takes
    as many as you write."""
    with at_the_prompt() as c:
        fb, _ = hold(c, f"-clear {BLACK} -rect 0 0 192 108 {GREEN}"
                        f" -disc 96 54 20 {MAGENTA} -text 4 100 Pigeon {BLACK}")
        assert rgb(fb, 96, 54) == MAGENTA_RGB, "the disc is over the rect"
        assert rgb(fb, 4, 4) == GREEN_RGB, "the rect is over the clear"
        assert lit(fb, MAGENTA_RGB) > 1000, "a disc of r=20 is about 1,257 pixels"


# --- a list with a mistake in it --------------------------------------------------

def test_a_mistake_anywhere_draws_nothing_at_all():
    """The list is checked before the first pixel, so the clear that starts
    it never happens: a typo in the last shape does not leave you with the
    first four."""
    with at_the_prompt() as c:
        seen = fills(c)
        shown = run(c, f"graphics -clear {MAGENTA} -rect 0 0 50 50 {GREEN}"
                         f" -disc 96 54 20 nonsense")
        assert said(shown, "graphics: not a colour: nonsense"), shown
        assert 0xFFFF00FF not in seen, "the screen was cleared before the check"


def test_a_mistake_is_found_before_a_wait_that_comes_first():
    """-wait ahead of the mistake would hold the screen for ever if the list
    were drawn as it was read."""
    with at_the_prompt() as c:
        shown = run(c, f"graphics -clear {MAGENTA} -wait -rect 0 0 50 50", seconds=30)
        assert said(shown, "graphics: -rect wants 5 arguments"), shown


def test_what_each_kind_of_mistake_says():
    with at_the_prompt() as c:
        assert said(run(c, "graphics -blob 1 2"),
                    "graphics: no such operation: -blob"), c.rows()
        assert said(run(c, f"graphics -px 10 1o {MAGENTA}"),
                    "graphics: not a number: 1o"), c.rows()
        assert said(run(c, "graphics -disc 96 54 20"),
                    "graphics: -disc wants 4 arguments"), c.rows()
        assert said(run(c, "graphics -clear"),
                    "graphics: -clear wants 1 argument"), c.rows()
        assert said(run(c, f"graphics -px -5 -5 {MAGENTA} extra"),
                    "graphics: no such operation: extra"), c.rows()


def test_with_nothing_to_draw_it_says_how():
    with at_the_prompt() as c:
        shown = run(c, "graphics")
        assert shown[0] == "usage: graphics OPERATION...", shown
        assert any("-wait" in text for text in shown), shown


# --- -f: an image, at a place and a size (docs/graphics_plan.md Q1) ---------------

def solid_bmp(w, h, colour):
    """A BMP of one colour, as an editor saves one: 24-bit, bottom-up."""
    r, g, b = colour
    stride = (w * 3 + 3) & ~3
    row = bytes((b, g, r)) * w
    pixels = (row + bytes(stride - len(row))) * h
    head = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54) + head + pixels


LOGO = ("/logo.bmp", solid_bmp(4, 4, MAGENTA_RGB))


def test_an_image_is_drawn_at_the_place_and_size_it_says():
    with at_the_prompt([LOGO]) as c:
        fb, _ = hold(c, f"-clear {BLACK} -f /logo.bmp 10 10 20 20 STRETCH")
        assert rgb(fb, 10, 10) == MAGENTA_RGB and rgb(fb, 29, 29) == MAGENTA_RGB
        assert rgb(fb, 9, 9) == (0, 0, 0) and rgb(fb, 30, 30) == (0, 0, 0)
        assert lit(fb, MAGENTA_RGB) == 20 * 20, "STRETCH fills the size it was given"


def test_crop_keeps_the_image_its_own_size():
    """The same 4x4 image in the same 20x20 box: CROP centres it on black
    rather than scaling it (<pigeon/bmp.h>)."""
    with at_the_prompt([LOGO]) as c:
        fb, _ = hold(c, f"-clear {BLACK} -f /logo.bmp 10 10 20 20 CROP")
        assert lit(fb, MAGENTA_RGB) == 4 * 4
        assert rgb(fb, 18, 18) == MAGENTA_RGB, "and in the middle of the box"


def test_an_image_off_the_edge_is_clipped():
    with at_the_prompt([LOGO]) as c:
        fb, _ = hold(c, f"-clear {BLACK} -f /logo.bmp -2 -2 4 4 CROP_TOP_LEFT")
        assert lit(fb, MAGENTA_RGB) == 2 * 2, "the corner that is on the screen"
        assert rgb(fb, 0, 0) == MAGENTA_RGB and rgb(fb, 2, 2) == (0, 0, 0)


def test_what_a_wrong_image_says_and_that_nothing_is_drawn():
    with at_the_prompt([LOGO, ("/notes.txt", b"not a picture")]) as c:
        seen = fills(c)
        assert said(run(c, f"graphics -clear {MAGENTA} -f /nope.bmp 0 0 4 4 CROP"),
                    "graphics: /nope.bmp: not found"), c.rows()
        assert said(run(c, f"graphics -clear {MAGENTA} -f /notes.txt 0 0 4 4 CROP"),
                    "graphics: /notes.txt: not a BMP"), c.rows()
        assert said(run(c, f"graphics -clear {MAGENTA} -f /logo.bmp 0 0 4 4 SQUISH"),
                    "graphics: not a mode: SQUISH"), c.rows()
        assert said(run(c, f"graphics -clear {MAGENTA} -f /logo.bmp 0 0 0 4 CROP"),
                    "graphics: not a width: 0"), c.rows()
        assert said(run(c, f"graphics -clear {MAGENTA} -f /logo.bmp 0 0 4 0 CROP"),
                    "graphics: not a height: 0"), c.rows()
        assert 0xFFFF00FF not in seen, "the screen was cleared before the check"


# --- -wait ------------------------------------------------------------------------

def test_wait_holds_the_screen_until_a_key_and_says_which():
    with at_the_prompt() as c:
        fb, printed = hold(c, f"-clear {MAGENTA}", key=KEY_ESC)
        assert printed == ["27"], printed                 # KEY_ESC
        assert rgb(fb, 96, 54) == MAGENTA_RGB, "it was still up when the key went down"


def test_the_key_that_ended_the_wait_is_the_one_reported():
    with at_the_prompt() as c:
        _, printed = hold(c, f"-clear {BLACK}", key=ord("g"))
        assert printed == ["103"], printed


def test_the_console_comes_back_when_the_program_ends():
    """The kernel redraws its console after every program, so a picture that
    is not held is gone the moment graphics returns -- what -wait is for."""
    with at_the_prompt() as c:
        assert run(c, f"graphics -clear {MAGENTA} -disc 96 54 20 {GREEN}") == []
        fb = c.machine.display_io.snapshot()
        assert rgb(fb, 96, 54) != GREEN_RGB and rgb(fb, 4, 54) != MAGENTA_RGB


if __name__ == "__main__":
    sys.exit(run_module(cases(globals())))
