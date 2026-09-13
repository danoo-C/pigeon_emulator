"""user/disc.c -- the CD drive demo, compiled and driven on the emulator.

Phase 6 of docs/cd-drive.md. Each test boots the program on a real
Machine, puts a disc in its drive (or leaves it empty), presses keys, and
checks two things: what the screen says, read back as text, and what
landed on the hard disk image, read back with tools/pfs.py.

disc.c lays its screen out exactly as user/files.c does, so
tests/test_files.py's screen_text() reads it -- that reader reverses the
font parsed out of display.c, and turns "some pixels changed" into "the
header says [PGFS]".

The test that matters most here is the one no key is pressed in: a disc
put in from the host while the program runs has to show up on its own,
because that is how the display's buttons reach it.

    python3 tests/test_disc.py      (or: python3 -m pytest tests/)
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
from emulator.memory_map import PROGRAM_LOAD_ADDR                     # noqa: E402
from emulator.programs import libraries_for                           # noqa: E402
from pfs import PgfsImage                                             # noqa: E402
from test_files import (                                              # noqa: E402
    DOWN, END, ESC, HEADER, HOME, ROWS, STATUS, body, screen_text)

BIOS = REPO_ROOT / "build" / "bios.bin"
DISC_C = REPO_ROOT / "user" / "disc.c"
MiB = 1 << 20

logging.getLogger("emulator.machine").setLevel(logging.ERROR)

# Ceilings, not targets: boot formats a 1 MiB disk and reads the drive.
BOOT_BUDGET = 6_000_000
KEY_BUDGET = 1_500_000
COPY_BUDGET = 8_000_000


@functools.lru_cache(maxsize=None)
def program():
    units = [DISC_C] + libraries_for(DISC_C)
    with tempfile.TemporaryDirectory() as d:
        asm = Path(d) / "disc.asm"
        asm.write_text(compile_units(units))
        return Assembler(str(asm)).assemble()


def pattern(n):
    return bytes((i * 7 + i // 512) & 0xFF for i in range(n))


class Session:
    """disc.c on a Machine, with keys going in and frames coming out."""

    def __init__(self, disk, disc=None, boot=BOOT_BUDGET):
        self.machine = Machine(bios_path=str(BIOS), disk_path=str(disk))
        self.machine.cd.root = None           # temporary discs live outside the repo
        if disc is not None:
            self.machine.cd.insert(disc)
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
        for key in keys:
            code = key if isinstance(key, int) else ord(key)
            self.machine.hid.push_key(code, True)
            self.machine.hid.push_key(code, False)
        return self.step(budget)

    @property
    def lines(self):
        return screen_text(self.machine.display_io.snapshot())

    @property
    def body(self):
        return body(self.machine.display_io.snapshot())

    def close(self):
        self.machine.close()


@contextlib.contextmanager
def running(disc_data=None, disc_name="demo.bin", image=None, disk_size=MiB):
    """disc.c with a blank disk on channel 2 and, optionally, a disc in the
    drive: raw bytes, or a PigeonFS image built by image(path)."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        disk = d / "hdd.img"
        disk.write_bytes(bytes(disk_size))
        disc = None
        if image is not None:
            disc = d / disc_name
            image(disc)
        elif disc_data is not None:
            disc = d / disc_name
            disc.write_bytes(disc_data)
        session = Session(disk, disc)
        try:
            yield session, disk, d
        finally:
            session.close()


def pgfs(files, label="DISC", dirs=()):
    def build(path):
        PgfsImage.mkfs(path, MiB, label=label).close()
        with PgfsImage(path) as img:
            for where in dirs:
                img.mkdir(where)
            for where, data in files:
                img.write_file(where, data)
    return build


def clean(path):
    with PgfsImage(path) as img:
        report = img.fsck()
    assert report.clean, "fsck: " + "; ".join(p.message for p in report.problems)


# --- an empty drive -----------------------------------------------------------------

def test_an_empty_drive_says_so_and_a_blank_disk_is_formatted():
    with running() as (s, disk, _):
        lines = s.lines
        assert lines[HEADER].startswith("CD DRIVE"), lines[HEADER]
        assert lines[HEADER].endswith("[empty]"), lines[HEADER]
        assert "no disc in the drive" in s.body, s.body
        assert "formatted" in lines[STATUS], lines[STATUS]
        clean(disk)


def test_a_disc_put_in_while_it_runs_shows_up_with_no_key_pressed():
    """That is how the display's Load buttons reach this program at all."""
    with running() as (s, _, d):
        disc = d / "late.bin"
        disc.write_bytes(bytes(range(48)))
        s.machine.cd.insert(disc)
        s.step(KEY_BUDGET)
        lines = s.lines
        assert lines[HEADER].startswith("late.bin"), lines[HEADER]
        assert lines[HEADER].endswith("[raw]"), lines[HEADER]
        assert "the disc changed" in lines[STATUS], lines[STATUS]


def test_copy_with_nothing_in_the_drive_says_so():
    with running() as (s, _, _d):
        s.press("c")
        assert "nothing to copy" in s.lines[STATUS], s.lines[STATUS]


# --- a raw disc ----------------------------------------------------------------------

def test_a_binary_disc_is_shown_as_a_hex_dump():
    with running(disc_data=bytes(range(64))) as (s, _, _d):
        lines = s.lines
        assert lines[0].startswith(" 64 B"), lines[0]
        assert lines[0].endswith("hex"), lines[0]
        assert lines[1].startswith("000000  00 01 02 03 04 05"), lines[1]
        assert lines[2].startswith("000006  06 07 08 09 0a 0b"), lines[2]


def test_a_hex_row_shows_all_six_characters():
    """The sixth character of the ASCII column is in the screen's last
    column. With one more space in the row it would land where the
    string's terminator lives, and be drawn off the edge -- invisible, and
    easy to never notice, which is how it was first written."""
    with running(disc_data=b"\x00ABCDE" + bytes(58)) as (s, _, _d):
        assert s.lines[1] == "000000  00 41 42 43 44 45 .ABCDE", s.lines[1]


def test_a_text_disc_is_shown_as_text_and_t_switches_to_hex():
    with running(disc_data=b"hello from a disc\nsecond line\n") as (s, _, _d):
        lines = s.lines
        assert lines[0].endswith("text"), lines[0]
        assert lines[1] == "hello from a disc", lines[1]
        assert lines[2] == "second line", lines[2]

        s.press("t")
        lines = s.lines
        assert lines[0].endswith("hex"), lines[0]
        assert lines[1] == "000000  68 65 6c 6c 6f 20 hello", lines[1]


def test_the_hex_dump_scrolls_and_stops_at_the_end():
    size = 600
    with running(disc_data=pattern(size)) as (s, _, _d):
        s.press(DOWN)
        assert s.lines[1].startswith("000006"), s.lines[1]
        s.press(END)
        rows = [s.lines[r] for r in range(1, ROWS)]
        last = [r for r in rows if r][-1]
        # 600 bytes is 100 rows of six; the last row starts at 594.
        assert last.startswith("000252"), f"END did not reach the last row: {last!r}"
        s.press(DOWN)
        assert [s.lines[r] for r in range(1, ROWS)] == rows, "scrolled past the end"
        s.press(HOME)
        assert s.lines[1].startswith("000000"), s.lines[1]


def test_c_copies_a_raw_disc_onto_the_disk():
    with running(disc_data=pattern(10000), disc_name="prog.bin") as (s, disk, _d):
        s.press("c", budget=COPY_BUDGET)
        status = s.lines[STATUS]
        assert status.startswith("copied 10000 B to 2:/prog.bin"), status
        with PgfsImage(disk) as img:
            assert img.read_file("/prog.bin") == pattern(10000)
        clean(disk)


# --- a filesystem disc ----------------------------------------------------------------

TREE = pgfs(
    files=(("/readme.txt", b"hi\n"),
           ("/games/pong.bin", pattern(700)),
           ("/games/deep/hidden.bin", pattern(300))),
    dirs=("/games", "/games/deep"))


def test_a_filesystem_disc_lists_its_tree_two_levels_deep():
    with running(image=TREE, disc_name="games.img") as (s, _, _d):
        lines = s.lines
        assert lines[HEADER].startswith("games.img"), lines[HEADER]
        assert lines[HEADER].endswith("[PGFS]"), lines[HEADER]
        assert lines[0].startswith(" label DISC"), lines[0]

        text = s.body
        assert re.search(r"^ readme\.txt\s+3 B$", text, re.M), text
        assert re.search(r"^ games/$", text, re.M), text
        assert re.search(r"^   pong\.bin\s+700 B$", text, re.M), text
        assert re.search(r"^   deep/$", text, re.M), text
        assert "hidden.bin" not in text, "listed deeper than two levels"
        s.press(HOME)                 # the boot message lasts until a key
        assert "c copy" in s.lines[STATUS], s.lines[STATUS]


def test_c_copies_a_filesystem_disc_as_a_whole_tree():
    """The listing stops at two levels; the copy must not."""
    with running(image=TREE, disc_name="games.img") as (s, disk, _d):
        s.press("c", budget=COPY_BUDGET)
        status = s.lines[STATUS]
        assert status.startswith("copied 3 files to 2:/DISC"), status
        with PgfsImage(disk) as img:
            assert img.read_file("/DISC/readme.txt") == b"hi\n"
            assert img.read_file("/DISC/games/pong.bin") == pattern(700)
            assert img.read_file("/DISC/games/deep/hidden.bin") == pattern(300)
        clean(disk)


def test_a_swapped_filesystem_disc_is_reread_not_served_from_the_old_volume():
    """A volume left mounted across a swap would still hold the old disc's
    cached blocks. The new disc's tree has to be the one on screen."""
    other = pgfs(files=(("/only-on-b.txt", b"b\n"),), label="BEE")
    with running(image=TREE, disc_name="a.img") as (s, _, d):
        assert "readme.txt" in s.body
        disc_b = d / "b.img"
        other(disc_b)
        s.machine.cd.insert(disc_b)
        s.step(KEY_BUDGET)
        text = s.body
        assert "only-on-b.txt" in text, text
        assert "readme.txt" not in text, "the old disc's tree is still showing"
        assert s.lines[0].startswith(" label BEE"), s.lines[0]


# --- ejecting and quitting ---------------------------------------------------------------

def test_e_ejects_the_disc_from_inside_the_machine():
    with running(image=TREE, disc_name="games.img") as (s, _, _d):
        s.press("e")
        assert s.machine.cd.status()["present"] is False, "the host still sees a disc"
        lines = s.lines
        assert lines[HEADER].endswith("[empty]"), lines[HEADER]
        assert "ejected" in lines[STATUS], lines[STATUS]


def test_esc_quits_and_leaves_the_disk_clean():
    with running(disc_data=pattern(100)) as (s, disk, _d):
        assert s.press(ESC, budget=3_000_000), "ESC did not end the program"
        clean(disk)


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the CD drive demo (user/disc.c)"))
