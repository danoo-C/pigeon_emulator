"""explorer, the file explorer: docs/explorer_plan.md.

/bin/explorer.bin runs under the kernel on a test disk, the way
tests/test_edit.py runs the editor: the shell starts it, keys and clicks go
in through HID, and the screen comes back as text. The explorer lays its
rows out exactly as user/files.c does, so the reader is test_files.py's --
the font table parsed out of display.c, so a changed glyph cannot quietly
make these tests assert something else.

Two screens matter here. While the explorer draws, the screen is its own
list, read with screen_text(). While a program it started runs, and until
the key that ends the pause, the screen is the kernel's console, read with
test_kernel.py's rows().

    python3 tests/test_explorer.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                   # noqa: E402
from emulator.devices.keycodes import (                                 # noqa: E402
    KEY_BACKSPACE, KEY_DOWN, KEY_END, KEY_ESC, KEY_HOME, KEY_PGDN, KEY_UP, key_f)
from pfs import PgfsImage                                               # noqa: E402
from emulator.memory_map import DISPLAY_H                               # noqa: E402
from test_files import (                                                # noqa: E402
    GLYPH_H, HEADER, LIST_Y, ROW, ROWS, STATUS, screen_text, text_at)
from test_kernel import (                                               # noqa: E402
    ENTER, STANDINS, Console, last_row, lines, make_disk, shell_program, standin)

# What the explorer's own keys are, in the pigeon keycode space.
ESC = KEY_ESC
F2, F5 = key_f(2), key_f(5)
LEFT_BUTTON, RIGHT_BUTTON = 0, 1
WHEEL_UP, WHEEL_DOWN = 5, 6

BUILT_IN_CONF = ("EXEC            = .bin\n"
                 "/bin/img.bin -s = .bmp\n"
                 "/bin/edit.bin   = *.*\n")

# A program that says its own name and every argument it was given, so a
# test can see what a rule ran and what it appended.
STANDINS["shout"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) {
    int i;
    for (i = 0; i < argc; i++) { print(argv[i]); if (i + 1 < argc) print(" "); }
    print("\n");
    return 0;
}
"""

# A program that makes a file where it is, to show the folder is read again
# after a program ends.
STANDINS["maker"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) {
    int fd = open("made.txt", O_WRITE | O_CREATE | O_TRUNC);
    if (fd >= 0) { write(fd, "hi\n", 3u); close(fd); }
    return 0;
}
"""

# A program that leaves the current directory somewhere else, as a shell
# started from the explorer can.
STANDINS["wanderer"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) { chdir("/"); print("wandered\n"); return 0; }
"""


# --- driving it -------------------------------------------------------------------

def screen(c):
    """The explorer's rows: HEADER, 0..ROWS-1, STATUS."""
    return screen_text(c.machine.display_io.snapshot())


def body(c):
    """The list area as one string, for `in` checks."""
    rows = screen(c)
    return "\n".join(rows[r] for r in range(ROWS))


def anywhere(c):
    """Every row of text on the screen, read at every pixel offset: a menu
    is drawn where the click was, not on the list's grid, so it is not on
    any row screen_text() reads."""
    fb = c.machine.display_io.snapshot()
    found = (text_at(fb, y) for y in range(DISPLAY_H - GLYPH_H))
    return "\n".join(text for text in found if text.strip())


def wait(c, wanted, seconds=60):
    """Run until the explorer's screen is what the test is waiting for, and
    the frame is whole.

    The explorer clears the screen and draws it again, so a snapshot taken
    while it draws is half a frame: the header with no list under it, or a
    status line cut off in the middle of a word. Two things say the frame
    finished -- the status line, which every frame draws last and never
    leaves empty, and the screen not having changed since the last look,
    since the explorer draws only when something has."""
    give_up = time.time() + seconds
    before = None
    while time.time() < give_up:
        with contextlib.redirect_stdout(io.StringIO()):
            c.machine.run(deadline=time.time() + 0.1)
        rows = screen(c)
        if rows[STATUS].strip() != "" and rows == before and wanted(rows):
            return True
        before = rows
        if c.machine.cpu.halted:
            return False
    return False


def wait_console(c, wanted, seconds=60):
    """The same, for the console: what a program printed, and the pause."""
    give_up = time.time() + seconds
    while time.time() < give_up:
        with contextlib.redirect_stdout(io.StringIO()):
            c.machine.run(deadline=time.time() + 0.1)
        if wanted(c.rows()):
            return True
        if c.machine.cpu.halted:
            return False
    return False


def listing(c):
    """The names in the list, as they are drawn and in that order, without
    their sizes: "readme.txt              1234 B" is "readme.txt", and a
    directory keeps the '/' the explorer puts on it."""
    out = []
    for r in range(ROWS):
        row = screen(c)[r].strip()
        if not row:
            continue
        if row.startswith(">"):
            row = row[1:].strip()
        out.append(row.rsplit("  ", 1)[0].strip())
    return out


def row_of(c, text):
    """The list row `text` is drawn on: a menu is on the same rows, since it
    snaps to them."""
    rows = screen(c)
    for r in range(ROWS):
        if text in rows[r]:
            return r
    return None


def selected(c):
    """The name on the row the explorer marks with '>'."""
    for r in range(ROWS):
        row = screen(c)[r]
        if row.strip().startswith(">"):
            return row.strip()[1:].strip().rsplit("  ", 1)[0].strip()
    return None


def click(c, row, button=LEFT_BUTTON, col=2):
    """A click on list row `row`, counted from the first one drawn."""
    c.machine.hid.set_mouse_pos(col * 6 + 2, LIST_Y + row * ROW + 3)
    c.machine.hid.push_mouse_event(button, True)
    c.machine.hid.push_mouse_event(button, False)


def click_at(c, x, y, button=LEFT_BUTTON):
    c.machine.hid.set_mouse_pos(x, y)
    c.machine.hid.push_mouse_event(button, True)
    c.machine.hid.push_mouse_event(button, False)


def notch(c, button):
    c.machine.hid.push_mouse_event(button, True)
    c.machine.hid.push_mouse_event(button, False)


@contextlib.contextmanager
def explorer(files=(), conf=BUILT_IN_CONF, at=None, programs=()):
    """The kernel on a disk holding the explorer, `files` and the standins
    `programs` name, with the explorer started at the prompt -- in `at` when
    it is given."""
    extra = [("/bin/explorer.bin", shell_program("explorer"))]
    for name in programs:
        extra.append((f"/bin/{name}.bin", standin(name)))
    if conf is not None:
        extra.append(("/etc/explorer.conf", conf.encode()))
    extra.extend(files)
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "hdd.img"
        console = Console(make_disk(path, extra))
        try:
            assert console.ready(), console.rows()
            console.type(f"explorer {at}\n" if at else "explorer\n")
            assert wait(console, lambda rows: rows[HEADER].startswith("2:")), \
                f"the explorer never drew its list: {console.rows()}"
            console.disk = path
            yield console
        finally:
            console.close()


def on_disk(c, path):
    """What the disk holds at `path`, the machine stopped first; None for
    nothing -- as tests/test_edit.py reads a save back."""
    c.close()
    with PgfsImage(c.disk) as img:
        return img.read_file(path) if img.exists(path) else None


# --- the list ----------------------------------------------------------------------

def test_the_explorer_lists_the_folder_it_was_started_in():
    with explorer() as c:
        assert screen(c)[HEADER].startswith("2:/"), screen(c)[HEADER]
        assert "bin/" in listing(c) and "docs/" in listing(c), listing(c)
        assert selected(c) == "bin/", listing(c)


def test_directories_come_first_then_names_and_the_root_has_no_parent():
    files = [("/docs/b.txt", b"b\n"), ("/docs/a.txt", b"a\n"), ("/docs/sub/c.txt", b"c\n")]
    with explorer(files, at="/docs") as c:
        assert listing(c) == ["..", "sub/", "a.txt", "b.txt", "readme.txt"], listing(c)
    with explorer(files) as c:
        assert ".." not in listing(c), listing(c)


def test_the_arrows_move_and_enter_goes_into_a_folder_and_backspace_back():
    with explorer([("/docs/note.txt", b"one\n")]) as c:
        c.press(KEY_DOWN)
        assert wait(c, lambda rows: rows[1].strip().startswith(">")), body(c)
        assert selected(c) == "docs/", listing(c)
        c.press(ENTER)
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)
        assert listing(c) == ["..", "note.txt", "readme.txt"], listing(c)
        c.press(KEY_BACKSPACE)
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/ ")
                    or rows[HEADER].startswith("2:/2") or rows[HEADER].startswith("2:/")
                    and "docs" not in rows[HEADER]), screen(c)[HEADER]
        assert "docs/" in listing(c), listing(c)


def test_home_end_and_the_page_keys_move_through_a_long_folder():
    files = [(f"/many/f{i:02}.txt", b"x\n") for i in range(20)]
    with explorer(files, at="/many") as c:
        c.press(KEY_END)
        assert wait(c, lambda rows: "f19.txt" in "\n".join(rows[r] for r in range(ROWS))), body(c)
        assert selected(c) == "f19.txt", body(c)
        c.press(KEY_HOME)
        assert wait(c, lambda rows: rows[0].strip().startswith(">")), body(c)
        assert selected(c) == "..", body(c)
        c.press(KEY_PGDN)
        assert wait(c, lambda rows: selected(c) == "f08.txt", 30), body(c)


def test_an_empty_folder_says_so():
    with explorer([("/empty/gone.txt", b"x\n")], at="/empty") as c:
        c.press(KEY_DOWN)                       # ".." is the only entry
        assert listing(c) == ["..", "gone.txt"], listing(c)


# --- the rules -----------------------------------------------------------------------

def open_selected(c, name):
    """Move to `name` and press Enter."""
    for _ in range(ROWS * 2):
        if selected(c) == name:
            break
        c.press(KEY_DOWN)
        wait(c, lambda rows: True, 5)
    assert selected(c) == name, listing(c)
    c.press(ENTER)


def test_a_rule_runs_its_command_with_the_file_added_to_the_end():
    conf = "/bin/shout.bin -s = .txt\n"
    with explorer([("/docs/note.txt", b"one\n")], conf=conf, programs=["shout"]) as c:
        open_selected(c, "docs/")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)
        open_selected(c, "note.txt")
        assert wait_console(c, lambda rows: "[ press any key ]" in "".join(rows)), c.rows()
        assert "/bin/shout.bin -s note.txt" in lines(c.rows()), c.rows()
        c.type("x")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)


def test_exec_runs_the_file_itself_as_the_prompt_would():
    with explorer([("/bin/shout.bin", standin("shout"))], conf="EXEC = .bin\n") as c:
        open_selected(c, "bin/")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/bin")), body(c)
        open_selected(c, "shout.bin")
        assert wait_console(c, lambda rows: "[ press any key ]" in "".join(rows)), c.rows()
        assert "shout.bin" in lines(c.rows()), c.rows()


def test_the_first_matching_rule_wins_and_star_catches_the_rest():
    conf = ("/bin/shout.bin first = .txt\n"
            "/bin/shout.bin never = .txt\n"
            "/bin/shout.bin rest  = *.*\n")
    files = [("/docs/note.txt", b"x\n"), ("/docs/pic.bmp", b"x\n")]
    with explorer(files, conf=conf, programs=["shout"]) as c:
        open_selected(c, "docs/")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)
        open_selected(c, "note.txt")
        assert wait_console(c, lambda rows: "[ press any key ]" in "".join(rows)), c.rows()
        assert "/bin/shout.bin first note.txt" in lines(c.rows()), c.rows()
        c.type("x")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)
        open_selected(c, "pic.bmp")
        assert wait_console(c, lambda rows: "rest pic.bmp" in "".join(rows)), c.rows()
        assert "/bin/shout.bin rest pic.bmp" in lines(c.rows()), c.rows()


def test_a_file_no_rule_matches_is_refused_on_the_status_line():
    with explorer([("/docs/note.zzz", b"x\n")], conf="EXEC = .bin\n") as c:
        open_selected(c, "docs/")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)
        open_selected(c, "note.zzz")
        assert wait(c, lambda rows: "no rule" in rows[STATUS]), screen(c)[STATUS]
        assert screen(c)[STATUS].startswith("no rule for note.zzz"), screen(c)[STATUS]


@cases(("no equals", "EXEC .bin\n", "expected command"),
       ("no command", "= .bin\n", "no command"),
       ("no pattern", "EXEC =\n", "no pattern"),
       ("not a pattern", "EXEC = bin\n", "not a pattern"),
       ("after the catch-all", "/bin/edit.bin = *.*\nEXEC = .bin\n", "after *.*"))
def test_a_conf_line_that_cannot_be_read_is_said_and_the_rest_still_works(label, conf, said):
    with explorer(conf=conf + "/bin/edit.bin = *.*\n") as c:
        assert said in screen(c)[STATUS], screen(c)[STATUS]
        assert "bin/" in listing(c), listing(c)


def test_with_no_conf_file_the_built_in_rules_are_used_and_it_says_so():
    with explorer([("/bin/shout.bin", standin("shout"))], conf=None) as c:
        assert "no /etc/explorer.conf" in screen(c)[STATUS], screen(c)[STATUS]
        open_selected(c, "bin/")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/bin")), body(c)
        open_selected(c, "shout.bin")                   # .bin is EXEC, built in
        assert wait_console(c, lambda rows: "[ press any key ]" in "".join(rows)), c.rows()
        assert "shout.bin" in lines(c.rows()), c.rows()


# --- running a program, and coming back ------------------------------------------------

def test_the_folder_is_read_again_after_a_program_and_the_key_ends_the_pause():
    conf = "/bin/maker.bin = .txt\n"
    with explorer([("/docs/note.txt", b"x\n")], conf=conf, programs=["maker"]) as c:
        open_selected(c, "docs/")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)
        open_selected(c, "note.txt")
        assert wait_console(c, lambda rows: "[ press any key ]" in "".join(rows)), c.rows()
        c.type("x")
        assert wait(c, lambda rows: "made.txt" in "\n".join(rows[r] for r in range(ROWS))), body(c)


def test_a_program_that_wanders_off_leaves_the_explorer_where_it_was():
    conf = "/bin/wanderer.bin = .txt\n"
    with explorer([("/docs/note.txt", b"x\n")], conf=conf, programs=["wanderer"]) as c:
        open_selected(c, "docs/")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)
        open_selected(c, "note.txt")
        # The pause, not the program's own line: the kernel empties the key
        # queue when a program ends, so a key pressed while it still runs is
        # thrown away and the pause would wait for one that never comes.
        assert wait_console(c, lambda rows: "[ press any key ]" in "".join(rows)), c.rows()
        assert "wandered" in "".join(c.rows()), c.rows()
        c.type("x")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), screen(c)[HEADER]


def test_a_program_that_will_not_start_says_why():
    conf = "/bin/nope.bin = .txt\n"
    with explorer([("/docs/note.txt", b"x\n")], conf=conf) as c:
        open_selected(c, "docs/")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)
        open_selected(c, "note.txt")
        assert wait(c, lambda rows: "not found" in rows[STATUS]), screen(c)[STATUS]
        assert screen(c)[STATUS].startswith("/bin/nope.bin:"), screen(c)[STATUS]


# --- the mouse --------------------------------------------------------------------------

def test_a_click_selects_and_a_second_click_on_it_opens_it():
    with explorer([("/docs/note.txt", b"x\n")]) as c:
        click(c, 1)                                     # docs/
        assert wait(c, lambda rows: rows[1].strip().startswith(">")), body(c)
        assert selected(c) == "docs/", listing(c)
        click(c, 1)
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), body(c)


def test_the_wheel_scrolls_the_list():
    """A notch is three rows, so three of them take the list past its top."""
    files = [(f"/many/f{i:02}.txt", b"x\n") for i in range(20)]
    with explorer(files, at="/many") as c:
        assert listing(c)[0] == "..", listing(c)
        for _ in range(3):
            notch(c, WHEEL_DOWN)
        assert wait(c, lambda rows: "f08.txt" in "\n".join(rows[r] for r in range(ROWS))), body(c)
        assert selected(c) == "f08.txt", body(c)
        assert ".." not in listing(c), f"the list never scrolled: {listing(c)}"
        for _ in range(4):
            notch(c, WHEEL_UP)
        assert wait(c, lambda rows: rows[0].strip().startswith(">..")), body(c)


# --- the menus ----------------------------------------------------------------------------

def test_a_click_on_empty_space_opens_the_folder_menu():
    with explorer() as c:
        click(c, ROWS - 1)                              # below the entries
        assert wait(c, lambda rows: "New file" in anywhere(c)), anywhere(c)
        assert "New folder" in anywhere(c) and "Shell here" in anywhere(c), anywhere(c)
        c.press(ESC)
        assert wait(c, lambda rows: "New file" not in anywhere(c)), anywhere(c)


def test_new_folder_makes_one():
    with explorer([("/docs/note.txt", b"x\n")], at="/docs") as c:
        click(c, ROWS - 1)
        assert wait(c, lambda rows: "New folder" in anywhere(c)), anywhere(c)
        click(c, row_of(c, "New folder"), col=3)
        assert wait(c, lambda rows: "new folder" in body(c)), body(c)
        c.type("plans\n")
        assert wait(c, lambda rows: "plans" in "\n".join(rows[r] for r in range(ROWS))), body(c)


def test_a_right_click_opens_the_entry_menu_and_rename_renames():
    with explorer([("/docs/note.txt", b"one\n")], at="/docs") as c:
        click(c, 1, button=RIGHT_BUTTON)                # note.txt
        assert wait(c, lambda rows: "Rename" in anywhere(c)), anywhere(c)
        assert "Open with" in anywhere(c) and "Delete" in anywhere(c), anywhere(c)
        c.press(KEY_DOWN)
        c.press(KEY_DOWN)
        c.press(ENTER)                                  # Rename...
        assert wait(c, lambda rows: "rename to" in body(c)), body(c)
        for _ in range(len("note.txt")):
            c.press(KEY_BACKSPACE)
        c.type("kept.txt\n")
        assert wait(c, lambda rows: "kept.txt" in "\n".join(rows[r] for r in range(ROWS))), body(c)
        assert on_disk(c, "/docs/kept.txt") == b"one\n"


def test_delete_asks_first_and_n_leaves_the_file_alone():
    with explorer([("/docs/note.txt", b"one\n")], at="/docs") as c:
        c.press(KEY_DOWN)                               # note.txt
        assert wait(c, lambda rows: selected(c) == "note.txt", 30), body(c)
        c.type("x")
        assert wait(c, lambda rows: "delete note.txt? y/n" in rows[STATUS]), screen(c)[STATUS]
        c.type("n")
        assert wait(c, lambda rows: "note.txt" in "\n".join(rows[r] for r in range(ROWS))), body(c)
        c.type("x")
        assert wait(c, lambda rows: "delete note.txt? y/n" in rows[STATUS]), screen(c)[STATUS]
        c.type("y")
        assert wait(c, lambda rows: "note.txt" not in "\n".join(rows[r] for r in range(ROWS))), body(c)


def test_open_with_runs_what_you_type_with_the_file_on_the_end():
    with explorer([("/docs/note.txt", b"x\n")], programs=["shout"], at="/docs") as c:
        c.press(KEY_DOWN)
        assert wait(c, lambda rows: selected(c) == "note.txt", 30), body(c)
        c.type("o")
        assert wait(c, lambda rows: "open with" in body(c)), body(c)
        c.type("shout -v\n")
        assert wait_console(c, lambda rows: "[ press any key ]" in "".join(rows)), c.rows()
        assert "shout -v note.txt" in lines(c.rows()), c.rows()


def test_shell_here_starts_the_shell_in_the_folder_and_exit_comes_back():
    with explorer([("/docs/note.txt", b"x\n")], at="/docs") as c:
        click(c, ROWS - 1)
        assert wait(c, lambda rows: "Shell here" in anywhere(c)), anywhere(c)
        c.press(KEY_DOWN)
        c.press(KEY_DOWN)
        c.press(ENTER)
        assert wait_console(c, lambda rows: any("2:/docs>" in row for row in rows)), c.rows()
        assert c.command("ls") == ["note.txt", "readme.txt"], c.rows()
        c.type("exit\n")
        assert wait_console(c, lambda rows: "[ press any key ]" in "".join(rows)), c.rows()
        c.type("x")
        assert wait(c, lambda rows: rows[HEADER].startswith("2:/docs")), screen(c)[HEADER]


# --- leaving -------------------------------------------------------------------------------

def test_esc_quits_and_the_shell_is_there_again():
    with explorer() as c:
        c.press(ESC)
        assert wait_console(c, lambda rows: any("2:/>" in row for row in rows)), c.rows()


def test_the_keys_are_on_a_screen_of_their_own():
    with explorer() as c:
        c.type("?")
        assert wait(c, lambda rows: "up/down enter  move, open" in body(c)), body(c)
        c.type("q")                                     # any key comes back
        assert wait(c, lambda rows: "bin/" in body(c)), body(c)


if __name__ == "__main__":
    run_module(__name__)
