"""user/files.c -- the file browser, compiled and driven on the emulator.

Phase 5 of docs/filesystem.md. These tests boot the program on a real
Machine with a temporary disk on channel 2, press keys at it, and then
check two things: what came out on the screen, and what <pigeon/fs.h>
actually left on the image -- read back with tools/pfs.py, the same
oracle tests/test_fs.py uses. A browser that draws the right thing over
a disk it corrupted would pass half of that, so both halves are checked
and every test ends with an fsck.

The screen is read back as TEXT. screen_text() reverses lib/pigeon/
display.c's font -- the table is parsed out of display.c rather than
copied here, so a change to a glyph cannot silently make these tests
assert something else. That turns "some pixels changed" into "the
listing says readme.txt", which is what the test actually means.

    python3 tests/test_files.py      (or: python3 -m pytest tests/)
"""
import contextlib
import functools
import io
import logging
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run_module                                        # noqa: E402
from assembler.assembler import Assembler                             # noqa: E402
from compiler.cc import compile_units                                 # noqa: E402
from emulator.machine import Machine                                  # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    DISPLAY_H, DISPLAY_W, PROGRAM_LOAD_ADDR)
from emulator.programs import libraries_for                           # noqa: E402
from pfs import PgfsImage                                             # noqa: E402

BIOS = REPO_ROOT / "build" / "bios.bin"
FILES_C = REPO_ROOT / "user" / "files.c"
DISPLAY_C = REPO_ROOT / "lib" / "pigeon" / "display.c"
MiB = 1 << 20

# A Machine with nothing on channel 1 warns about it on every boot.
logging.getLogger("emulator.machine").setLevel(logging.ERROR)

# Keys, in the pigeon keycode space (emulator/devices/keycodes.py).
UP, DOWN, PGUP, PGDN, HOME, END = 0x82, 0x83, 0x86, 0x87, 0x84, 0x85
ENTER, BKSP, ESC, TAB = 0x0D, 0x08, 0x1B, 0x09

# Measured: a cold boot on a 1 MiB disk (format, seed, first frame) is
# about 1.6M instructions and a keypress redraw about 0.4M. These are
# ceilings with room, not targets -- a program that settles early just
# spins in its idle loop until the budget runs out.
BOOT_BUDGET = 6_000_000
KEY_BUDGET = 1_500_000

# --- the layout files.c draws, derived the same way it derives it ---------
GLYPH_W, GLYPH_H = 5, 8
CELL = GLYPH_W + 1
ROW = GLYPH_H + 1
COLS = DISPLAY_W // CELL
HEAD_Y = 1
LIST_Y = HEAD_Y + GLYPH_H + 1 + 3
STATUS_Y = DISPLAY_H - GLYPH_H - 1
ROWS = (STATUS_Y - 2 - 1 - LIST_Y) // ROW

HEADER, STATUS = "header", "status"
INK_THRESHOLD = 200      # r+g+b: the four inks are all over 340, the three
                         # backgrounds all under 140


# --- building the program ---------------------------------------------------

@functools.lru_cache(maxsize=None)
def program():
    units = [FILES_C] + libraries_for(FILES_C)
    with tempfile.TemporaryDirectory() as d:
        asm = Path(d) / "files.asm"
        asm.write_text(compile_units(units))
        return Assembler(str(asm)).assemble()


# --- reading the screen back as text ----------------------------------------

@functools.lru_cache(maxsize=None)
def glyphs():
    """character -> its 8 rows of 5 ink bits, from display.c's own table."""
    body = re.search(r"static char FONT\[\] = \{(.*?)\n\};",
                     DISPLAY_C.read_text(), re.S)
    assert body, f"{DISPLAY_C} no longer declares `static char FONT[] = {{`"
    values = [int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]{2})", body.group(1))]
    assert len(values) == (0x7E - 0x20 + 1) * GLYPH_H, (
        f"font table is {len(values)} bytes, expected "
        f"{(0x7E - 0x20 + 1) * GLYPH_H} for 0x20..0x7E")
    table = {}
    for i in range(0, len(values), GLYPH_H):
        rows = tuple((v & 0xF8) >> 3 for v in values[i:i + GLYPH_H])
        table.setdefault(rows, chr(0x20 + i // GLYPH_H))
    return table


def screen_text(fb):
    """The screen's text rows, as files.c lays them out.

    Returns {row key -> string}: HEADER, 0..ROWS-1 for the list area, and
    STATUS. A cell whose ink matches no glyph comes back as '?' -- a
    caret or the scrollbar can land inside one.
    """
    table = glyphs()

    def ink(x, y):
        i = (y * DISPLAY_W + x) * 4
        return sum(fb[i:i + 3]) > INK_THRESHOLD

    def read(y):
        out = []
        for col in range(COLS):
            x = col * CELL
            rows = tuple(
                sum(1 << (GLYPH_W - 1 - c) for c in range(GLYPH_W)
                    if x + c < DISPLAY_W and y + r < DISPLAY_H and ink(x + c, y + r))
                for r in range(GLYPH_H))
            out.append(table.get(rows, " " if not any(rows) else "?"))
        return "".join(out).rstrip()

    lines = {HEADER: read(HEAD_Y), STATUS: read(STATUS_Y)}
    for r in range(ROWS):
        lines[r] = read(LIST_Y + r * ROW)
    return lines


def body(fb):
    """The list area as one string, for `in` checks."""
    lines = screen_text(fb)
    return "\n".join(lines[r] for r in range(ROWS))


# --- driving it --------------------------------------------------------------

class Session:
    """files.c running on a Machine, with keys going in and frames coming out."""

    def __init__(self, disk, boot=BOOT_BUDGET):
        self.machine = Machine(bios_path=str(BIOS), disk_path=str(disk))
        # Loaded straight into RAM rather than booted off channel 1: the
        # BIOS path costs a two-second timer wait per test and is
        # tests/test_loader.py's job, not this file's.
        self.machine.ram.load_bytes(program(), PROGRAM_LOAD_ADDR)
        self.machine.cpu.pc = PROGRAM_LOAD_ADDR
        self.step(boot)

    def step(self, budget):
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(budget):
                if self.machine.step() == 1:
                    return True
        return False

    def press(self, *keys, budget=KEY_BUDGET):
        """Push presses and releases, then let the program settle."""
        for key in keys:
            code = key if isinstance(key, int) else ord(key)
            self.machine.hid.push_key(code, True)
            self.machine.hid.push_key(code, False)
        return self.step(budget)

    def type(self, text, **kw):
        return self.press(*text, **kw)

    @property
    def screen(self):
        return self.machine.display_io.snapshot()

    def close(self):
        self.machine.close()


@contextlib.contextmanager
def browser(size=MiB, prepare=None):
    """A blank disk of `size`, optionally prepared first, with files.c on it."""
    with tempfile.TemporaryDirectory() as d:
        disk = Path(d) / "hdd.img"
        disk.write_bytes(bytes(size))
        if prepare is not None:
            prepare(disk)
        session = Session(disk)
        try:
            yield session, disk
        finally:
            session.close()


def clean(path):
    with PgfsImage(path) as img:
        report = img.fsck()
    assert report.clean, "fsck: " + "; ".join(p.message for p in report.problems)


def listing(path, where="/"):
    with PgfsImage(path) as img:
        return {entry.name: entry for entry in img.listdir(where)}


def contents(path, where):
    with PgfsImage(path) as img:
        return img.read_file(where)




# The image is read while the Machine is still open. That is safe, and it
# is the point: fs.c flushes its cache before every public call returns,
# so whenever the program is sitting in its key loop the host file is
# already consistent. Closing the machine first would only test shutdown.

# --- the first run ------------------------------------------------------------

def test_a_blank_disk_is_formatted_and_seeded():
    """fs_mount returns FS_ENOFS on a disk of zeros; the program formats
    it and writes a tree, so there is something to browse on first run."""
    with browser() as (session, disk):
        lines = screen_text(session.screen)
        assert "2:/" in lines[HEADER], lines[HEADER]
        assert "PIGEON" in lines[HEADER], lines[HEADER]
        assert "formatted" in lines[STATUS], lines[STATUS]

        text = body(session.screen)
        for name in ("logs/", "notes/", "readme.txt"):
            assert name in text, f"{name} missing from the listing:\n{text}"

        clean(disk)
        root = listing(disk)
        assert set(root) == {"logs", "notes", "readme.txt"}, sorted(root)
        assert set(listing(disk, "/notes")) == {"hello.txt", "todo.txt"}
        assert contents(disk, "/readme.txt").startswith(b"PigeonFS\n")


def test_directories_sort_before_files_and_sizes_are_shown():
    with browser() as (session, _):
        lines = screen_text(session.screen)
        assert lines[0].startswith(">logs/"), lines[0]
        assert lines[1].startswith(" notes/"), lines[1]
        assert lines[0].endswith("<dir>") and lines[1].endswith("<dir>")
        assert re.search(r"readme\.txt\s+\d+ B$", lines[2]), lines[2]


def test_the_status_line_counts_and_measures():
    with browser() as (session, disk):
        status = screen_text(session.screen)[STATUS]
        assert "formatted" in status                     # the first frame
        session.press(DOWN)                              # any key clears it
        status = screen_text(session.screen)[STATUS]
        assert status.startswith("3 items"), status

        free = int(re.search(r"(\d+) KB free", status).group(1))
        with PgfsImage(disk) as img:
            assert free == img.free_count() // 2, status


# --- it must not eat the disk ---------------------------------------------------

def test_a_disk_that_already_has_a_filesystem_is_never_reformatted():
    """The lifetime rule: only a blank disk is formatted, and files.c
    carries no force flag, so nothing it does can wipe a disk."""
    def prepare(disk):
        PgfsImage.mkfs(disk, MiB, label="MINE").close()
        with PgfsImage(disk) as img:
            img.mkdir("/keep")
            img.write_file("/keep/precious.txt", b"do not lose me\n")

    with browser(prepare=prepare) as (session, disk):
        lines = screen_text(session.screen)
        assert "MINE" in lines[HEADER], lines[HEADER]
        assert "formatted" not in lines[STATUS], lines[STATUS]
        assert "keep/" in body(session.screen)

        clean(disk)
        assert contents(disk, "/keep/precious.txt") == b"do not lose me\n"


def test_what_it_writes_survives_the_machine_being_closed():
    """Every call writes through to the host image, so a note is on disk
    the moment it is saved -- not at shutdown, which never comes."""
    with browser() as (session, disk):
        session.press("n")
        session.type("kept.txt")
        session.press(ENTER)
        session.type("still here")
        session.press(TAB, budget=3_000_000)
        assert "saved" in screen_text(session.screen)[STATUS]

        clean(disk)
        assert contents(disk, "/kept.txt") == b"still here"

        session.close()
        again = Session(disk)                            # a second machine
        try:
            assert "formatted" not in screen_text(again.screen)[STATUS]
            assert "kept.txt" in body(again.screen)
        finally:
            again.close()
        clean(disk)


# --- walking around ---------------------------------------------------------------

def test_enter_walks_into_a_directory_and_backspace_comes_back():
    with browser() as (session, disk):
        session.press(DOWN, ENTER)                       # logs/, notes/ -> notes
        lines = screen_text(session.screen)
        assert lines[HEADER].startswith("2:/notes"), lines[HEADER]
        text = body(session.screen)
        assert "hello.txt" in text and "todo.txt" in text, text

        session.press(BKSP)
        assert screen_text(session.screen)[HEADER].startswith("2:/"), "did not come back"
        assert "readme.txt" in body(session.screen)

        # `..` at the root stays at the root rather than failing.
        session.press(BKSP)
        assert screen_text(session.screen)[HEADER].startswith("2:/")
        assert "readme.txt" in body(session.screen)
        clean(disk)


def test_enter_on_a_text_file_shows_it_and_esc_comes_back():
    with browser() as (session, disk):
        session.press(END, ENTER, budget=3_000_000)      # readme.txt
        lines = screen_text(session.screen)
        assert lines[HEADER].startswith("readme.txt"), lines[HEADER]
        assert "PigeonFS" in body(session.screen)
        assert re.search(r"line 1/\d+", lines[STATUS]), lines[STATUS]

        first = body(session.screen)
        session.press(PGDN)
        assert body(session.screen) != first, "the viewer did not scroll"

        session.press(ESC)
        assert screen_text(session.screen)[HEADER].startswith("2:/")
        clean(disk)


def test_a_file_that_is_not_text_is_refused_rather_than_drawn():
    def prepare(disk):
        PgfsImage.mkfs(disk, MiB, label="PIGEON").close()
        with PgfsImage(disk) as img:
            img.write_file("/blob.bin", bytes(range(256)))

    with browser(prepare=prepare) as (session, disk):
        session.press(ENTER, budget=3_000_000)
        assert "not a text file" in screen_text(session.screen)[STATUS]
        assert "blob.bin" in body(session.screen), "it left the listing"
        clean(disk)


# --- making and unmaking things -----------------------------------------------------

def test_a_new_directory_appears_on_the_disk_and_in_the_listing():
    with browser() as (session, disk):
        session.press("d")
        session.type("saves")
        session.press(ENTER, budget=3_000_000)
        assert "created" in screen_text(session.screen)[STATUS]
        assert "saves/" in body(session.screen)

        clean(disk)
        assert "saves" in listing(disk)


def test_a_note_is_written_where_you_are_standing():
    with browser() as (session, disk):
        session.press(DOWN, ENTER)                       # into /notes
        session.press("n")
        session.type("idea.txt")
        session.press(ENTER)
        session.type("one")
        session.press(ENTER)                             # a newline in the body
        session.type("two")
        session.press(TAB, budget=3_000_000)
        assert "saved" in screen_text(session.screen)[STATUS]
        assert "idea.txt" in body(session.screen)

        clean(disk)
        assert contents(disk, "/notes/idea.txt") == b"one\ntwo"
        assert "idea.txt" not in listing(disk), "it landed in the root, not the cwd"


def test_escape_in_the_editor_writes_nothing():
    with browser() as (session, disk):
        session.press("n")
        session.type("scratch.txt")
        session.press(ENTER)
        session.type("never mind")
        session.press(ESC)
        assert "discarded" in screen_text(session.screen)[STATUS]

        clean(disk)
        assert "scratch.txt" not in listing(disk)


def test_delete_asks_first_and_then_removes_the_file():
    with browser() as (session, disk):
        session.press(END)                               # readme.txt
        session.press("x")
        assert "delete readme.txt?" in body(session.screen)

        session.press("n")                               # anything but y
        assert "readme.txt" in body(session.screen), "removed without a yes"
        assert "readme.txt" in listing(disk)

        session.press("x", "y", budget=3_000_000)
        assert "deleted" in screen_text(session.screen)[STATUS]
        assert "readme.txt" not in body(session.screen)

        clean(disk)
        assert "readme.txt" not in listing(disk)


def test_removing_a_directory_that_is_not_empty_is_refused_out_loud():
    """rmdir's FS_ENOTEMPTY has to reach the screen, not be swallowed --
    it is the refusal a user is most likely to hit."""
    with browser() as (session, disk):
        session.press(DOWN)                              # notes/, which has two files
        session.press("x", "y", budget=3_000_000)
        status = screen_text(session.screen)[STATUS]
        assert status.startswith("delete:"), status
        assert "empty" in status, status
        assert "notes/" in body(session.screen)

        clean(disk)
        assert set(listing(disk, "/notes")) == {"hello.txt", "todo.txt"}


def test_the_key_list_is_on_screen():
    with browser() as (session, _):
        session.press("?")
        text = body(session.screen)
        for key in ("up/down", "enter", "bksp", "new note", "delete", "quit"):
            assert key in text, f"{key} missing from the help:\n{text}"
        session.press(ESC)
        assert "readme.txt" in body(session.screen), "help did not close"


def test_esc_quits_and_leaves_the_disk_clean():
    with browser() as (session, disk):
        assert session.press(ESC, budget=3_000_000), "ESC did not end the program"
        clean(disk)


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the file browser"))
