"""edit, the mini nano: docs/phase4b_plan.md step 7.

/bin/edit.bin runs under the kernel on a test disk, as the shell's
programs do in tests/test_kernel.py. Keys and clicks go in through HID, and
the screen is read back with the font reader. The title bar, the shortcuts
and the cursor are inverse, so each cell that reads as no glyph is read
again with its colours swapped. What a save wrote is read from the disk
image once the machine has stopped.

    python3 tests/test_edit.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                   # noqa: E402
from emulator.devices.keycodes import (                                 # noqa: E402
    KEY_DOWN, KEY_END, KEY_HOME, KEY_LCTRL, KEY_RIGHT)
from emulator.memory_map import DISPLAY_W                               # noqa: E402
from pfs import BLOCK, PgfsImage                                        # noqa: E402
from test_files import INK_THRESHOLD, text_at                           # noqa: E402
from test_kernel import (                                               # noqa: E402
    COLS, ENTER, ROW_H, ROWS, Console, kernel_symbols, make_disk, shell_program)

NOTE = b"one\ntwo\nthree\n"


# --- driving it -------------------------------------------------------------------

def chord(letter):
    return (KEY_LCTRL, ord(letter))


def keys(c, *items):
    """Text typed, a key code pressed, or a chord such as chord("o")."""
    for item in items:
        if isinstance(item, str):
            c.type(item)
        elif isinstance(item, tuple):
            c.press(*item)
        else:
            c.press(item)


def screen(c):
    """The rows as a person reads them. A cell that reads as no glyph is read
    again with its colours swapped, which is how inverse cells come back."""
    fb = c.machine.display_io.snapshot()
    swapped = bytearray(len(fb))
    for i in range(0, len(fb), 4):
        if fb[i] + fb[i + 1] + fb[i + 2] <= INK_THRESHOLD:
            swapped[i:i + 3] = b"\xd8\xd8\xd8"
    swapped = bytes(swapped)
    rows = []
    for r in range(ROWS):
        plain = text_at(fb, r * ROW_H).ljust(COLS)
        other = text_at(swapped, r * ROW_H).ljust(COLS)
        rows.append("".join(o if p == "?" else p for p, o in zip(plain, other)).rstrip())
    return rows


def inverse(fb, row, col):
    lit = 0
    for dy in range(ROW_H):
        for dx in range(6):
            i = ((row * ROW_H + dy) * DISPLAY_W + col * 6 + dx) * 4
            if fb[i] + fb[i + 1] + fb[i + 2] > INK_THRESHOLD:
                lit += 1
    return lit > 30


def cursor_at(c):
    """The cursor: the one inverse cell on the text rows, as (row, column)."""
    fb = c.machine.display_io.snapshot()
    found = [(r, col) for r in range(1, 10) for col in range(COLS) if inverse(fb, r, col)]
    return found[0] if len(found) == 1 else found


def wait(c, check, what=""):
    assert c.run_until(lambda rows: check(screen(c))), (what, screen(c))


class Session:
    """A machine booted from a test disk, and what the disk holds after."""

    def __init__(self, disk):
        self.disk = disk
        self.console = Console(disk)
        self.closed = False

    def close(self):
        if not self.closed:
            self.console.close()
            self.closed = True

    def file(self, path):
        """What the disk holds at path, the machine stopped first; None for nothing."""
        self.close()
        with PgfsImage(self.disk) as img:
            return img.read_file(path) if img.exists(path) else None


@contextlib.contextmanager
def disk_with(extra=(), fill=False):
    with tempfile.TemporaryDirectory() as t:
        disk = make_disk(Path(t) / "hdd.img", [("/bin/edit.bin", shell_program("edit"))] + list(extra))
        if fill:
            with PgfsImage(disk) as img:            # all but two blocks
                img.write_file("/fill.bin", bytes((img.free_count() - 2) * BLOCK))
        session = Session(disk)
        try:
            assert session.console.ready(), session.console.rows()
            yield session
        finally:
            session.close()


def open_edit(session, path):
    c = session.console
    c.type(f"edit {path}\n")
    wait(c, lambda rows: rows[11] == "^O Save ^X Exit ^K Cut ^W Find", "the shortcuts")
    return c


def left(c):
    """edit ended: its screen cleared, the prompt at the top."""
    assert c.run_until(lambda rows: rows[0] == "2:/> _"), c.rows()


# --- editing and saving -------------------------------------------------------------

def test_open_move_type_in_the_middle_save_and_leave():
    with disk_with([("/docs/note.txt", NOTE)]) as s:
        c = open_edit(s, "/docs/note.txt")
        wait(c, lambda rows: rows[1:4] == ["one", "two", "three"] and rows[10].strip() == "[ Read 3 lines ]")
        assert screen(c)[0] == " edit /docs/note.txt", screen(c)
        assert cursor_at(c) == (1, 0)
        keys(c, KEY_DOWN, KEY_RIGHT, KEY_RIGHT, "X")
        wait(c, lambda rows: rows[2] == "twXo" and rows[0].endswith("Modified") and rows[10] == ""
             and cursor_at(c) == (2, 3))
        keys(c, chord("o"))
        wait(c, lambda rows: rows[10].strip() == "[ Wrote 3 lines ]" and rows[0] == " edit /docs/note.txt")
        keys(c, chord("x"))
        left(c)
        assert s.file("/docs/note.txt") == b"one\ntwXo\nthree\n"
        assert s.file("/docs/note.txt~") is None


@cases(("Y saves, and edit ends", "y", b"aone\ntwo\nthree\n", True),
       ("N leaves the file as it was, and edit ends", "n", NOTE, True),
       ("^C stays in edit", chord("c"), NOTE, False))
def test_leaving_with_changes_asks_first(label, answer, saved, ends):
    with disk_with([("/docs/note.txt", NOTE)]) as s:
        c = open_edit(s, "/docs/note.txt")
        keys(c, "a", chord("x"))
        wait(c, lambda rows: rows[10].strip() == "Save modified buffer? Y N", label)
        keys(c, answer)
        if not ends:
            wait(c, lambda rows: rows[10].strip() == "[ Cancelled ]" and rows[1] == "aone", label)
            keys(c, chord("x"), "n")
        left(c)
        assert s.file("/docs/note.txt") == saved, label


def test_cut_lines_gather_and_paste_above_the_cursor():
    with disk_with([("/docs/abcd.txt", b"a\nb\nc\nd\n")]) as s:
        c = open_edit(s, "/docs/abcd.txt")
        keys(c, chord("k"), chord("k"), KEY_DOWN, chord("u"))
        wait(c, lambda rows: rows[1:6] == ["c", "a", "b", "d", ""] and cursor_at(c) == (4, 0))
        keys(c, chord("o"), chord("x"))
        left(c)
        assert s.file("/docs/abcd.txt") == b"c\na\nb\nd\n"


def test_a_new_file_is_made_on_the_first_save():
    with disk_with() as s:
        c = open_edit(s, "/docs/new.txt")
        wait(c, lambda rows: rows[10].strip() == "[ New File ]")
        keys(c, "hi", ENTER, chord("s"))
        wait(c, lambda rows: rows[10].strip() == "[ Wrote 1 line ]" and cursor_at(c) == (2, 0))
        keys(c, chord("x"))
        left(c)
        assert s.file("/docs/new.txt") == b"hi\n"


def test_a_save_onto_a_full_disk_leaves_the_old_file():
    """The note is three blocks and two are free, so FILE~ can't be written."""
    note = b"".join(b"a line of the note %04d\n" % i for i in range(3 * BLOCK // 24 + 1))
    with disk_with([("/docs/note.txt", note)], fill=True) as s:
        c = open_edit(s, "/docs/note.txt")
        wait(c, lambda rows: rows[1] == "a line of the note 0000")
        keys(c, "x", chord("o"))
        wait(c, lambda rows: rows[10].strip() == "[ Not saved: disk full ]" and rows[0].endswith("Modified"))
        keys(c, chord("x"), "n")
        left(c)
        assert s.file("/docs/note.txt") == note
        assert s.file("/docs/note.txt~") is None


# --- finding, moving, and the rest of the keys ---------------------------------------

def test_find_goes_forward_wraps_and_says_when_it_isnt_there():
    with disk_with([("/docs/fruit.txt", b"apple\nbanana\ncherry\nbanana split\n")]) as s:
        c = open_edit(s, "/docs/fruit.txt")

        def find(text):
            keys(c, chord("w"))
            wait(c, lambda rows: rows[10].startswith("Find:"), text)
            keys(c, text, ENTER)

        find("banana")
        wait(c, lambda rows: cursor_at(c) == (2, 0))
        find("banana")
        wait(c, lambda rows: cursor_at(c) == (4, 0))
        find("banana")
        wait(c, lambda rows: cursor_at(c) == (2, 0) and rows[10].strip() == "[ Search Wrapped ]")
        find("kiwi")
        wait(c, lambda rows: rows[10].strip() == '[ "kiwi" not found ]' and cursor_at(c) == (2, 0))
        keys(c, chord("w"))
        wait(c, lambda rows: rows[10].startswith("Find:"))
        keys(c, "x", chord("c"))
        wait(c, lambda rows: rows[10].strip() == "[ Cancelled ]" and cursor_at(c) == (2, 0))


def test_a_long_line_moves_sideways_with_the_cursor():
    long = "0123456789" * 5
    with disk_with([("/docs/long.txt", (long + "\nshort\n").encode())]) as s:
        c = open_edit(s, "/docs/long.txt")
        wait(c, lambda rows: rows[1] == long[:32] and rows[2] == "short")
        keys(c, KEY_END)
        wait(c, lambda rows: rows[1] == "$" + long[25:] and cursor_at(c) == (1, 26))
        keys(c, KEY_DOWN)
        wait(c, lambda rows: rows[1] == long[:32] and cursor_at(c) == (2, 5))
        keys(c, KEY_HOME)
        wait(c, lambda rows: cursor_at(c) == (2, 0))


def test_ctrl_c_says_where_the_cursor_is_and_ctrl_g_shows_the_keys():
    with disk_with([("/docs/note.txt", NOTE)]) as s:
        c = open_edit(s, "/docs/note.txt")
        keys(c, KEY_DOWN, KEY_RIGHT, KEY_RIGHT, chord("c"))
        wait(c, lambda rows: rows[10].strip() == "[ line 2 of 4, col 3 ]" and rows[1] == "one")
        keys(c, chord("g"))
        wait(c, lambda rows: rows[0].strip() == "edit: the keys" and rows[3] == " ^K  cut a line  ^U  paste")
        keys(c, "q")                                # any key comes back, and types nothing
        wait(c, lambda rows: rows[1:4] == ["one", "two", "three"] and cursor_at(c) == (2, 2))
        keys(c, chord("x"))
        left(c)
        assert s.file("/docs/note.txt") == NOTE


# --- the mouse ------------------------------------------------------------------------

def notch(c, button):
    c.machine.hid.push_mouse_event(button, True)
    c.machine.hid.push_mouse_event(button, False)


def click(c, row, col):
    c.machine.hid.set_mouse_pos(col * 6 + 2, row * 9 + 3)
    c.machine.hid.push_mouse_event(0, True)
    c.machine.hid.push_mouse_event(0, False)


def test_the_wheel_scrolls_the_text_and_keeps_the_cursor_on_it():
    text = "".join(f"line {i}\n" for i in range(1, 31)).encode()
    with disk_with([("/docs/lines.txt", text)]) as s:
        c = open_edit(s, "/docs/lines.txt")
        wait(c, lambda rows: rows[1] == "line 1" and rows[9] == "line 9")
        notch(c, 6)                                 # down: the cursor would be above the screen
        wait(c, lambda rows: rows[1] == "line 4" and rows[9] == "line 12" and cursor_at(c) == (1, 0))
        notch(c, 5)
        wait(c, lambda rows: rows[1] == "line 1" and rows[9] == "line 9" and cursor_at(c) == (4, 0))


@cases(("in a line", 3, 3, (3, 3)),
       ("past a line's end", 2, 20, (2, 6)),
       ("below the last line", 8, 2, (4, 2)),
       ("on the title bar", 0, 5, (1, 0)),
       ("on the shortcuts", 11, 5, (1, 0)))
def test_a_click_puts_the_cursor_where_it_lands(label, row, col, cursor):
    with disk_with([("/docs/four.txt", b"line 1\nline 2\nline 3\nline 4")]) as s:
        c = open_edit(s, "/docs/four.txt")
        wait(c, lambda rows: rows[4] == "line 4" and cursor_at(c) == (1, 0))
        click(c, row, col)
        keys(c, chord("c"))                         # and a message, to know the click was taken
        said = f"[ line {cursor[0]} of 4, col {cursor[1] + 1} ]"
        wait(c, lambda rows: rows[10].strip() == said and cursor_at(c) == cursor, label)


# --- what it refuses, and what it costs ------------------------------------------------

@cases(("no name", "edit", ["usage: edit FILE", "edit: exit 1"]),
       ("a directory", "edit /docs", ["edit: /docs: is a directory", "edit: exit 1"]),
       ("a zero byte", "edit /docs/zero.bin",
        ["edit: /docs/zero.bin: holds a zero byte, so isn't text", "edit: exit 1"]),
       ("over 64 KB", "edit /docs/big.txt", ["edit: /docs/big.txt: over 64 KB", "edit: exit 1"]))
def test_what_edit_refuses_to_open(label, line, printed):
    extra = [("/docs/zero.bin", b"text\0more"), ("/docs/big.txt", b"x" * 65537)]
    with disk_with(extra) as s:
        assert s.console.command(line) == printed, (label, s.console.rows())


def test_typing_a_character_draws_only_the_rest_of_its_line():
    """Counted in instructions: the one write edit makes for a key, from the
    kernel's first instruction of it to its return."""
    entry = kernel_symbols()["k_write"]
    with disk_with([("/docs/hello.txt", b"hello there, world\n")]) as s:
        c = open_edit(s, "/docs/hello.txt")
        keys(c, KEY_RIGHT, KEY_RIGHT, "a")          # modified now, so the title bar isn't in the count
        wait(c, lambda rows: rows[1] == "heallo there, world" and rows[0].endswith("Modified")
             and cursor_at(c) == (1, 3))
        cpu = c.machine.cpu
        c.type("b")
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3_000_000):
                if cpu.pc == entry:
                    break
                c.machine.step()
            assert cpu.pc == entry, "the key was never drawn"
            sp, steps = cpu.sp, 0
            while cpu.sp <= sp and steps < 5_000_000:
                c.machine.step()
                steps += 1
        wait(c, lambda rows: rows[1] == "heabllo there, world" and cursor_at(c) == (1, 4))
        assert steps < 250_000, f"one key took {steps:,} instructions to draw"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "edit"))
