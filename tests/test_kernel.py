"""The kernel: phase 3 of docs/kernel.md (§9-§12, §17).

user/os/kernel.c boots on a Machine from a PigeonFS test disk and starts
user/os/bin/sh.c. The tests type at the shell through HID and read the
kernel's console back as text, with the font reader tests/test_files.py
has. Programs that end every way a program can -- returning, exit() from
deep inside, a fault, Ctrl+C -- are small stand-ins compiled here as
program files and put in /bin.

Each test runs the machine in short slices of wall-clock time until the
screen shows what it waits for, so none depends on how many instructions
a second the host manages.

    python3 tests/test_kernel.py      (or: python3 -m pytest tests/)
"""
import contextlib
import functools
import io
import logging
import re
import struct
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                   # noqa: E402
from assembler.assembler import Assembler, assemble_file                # noqa: E402
from emulator.devices.keycodes import (                                 # noqa: E402
    KEY_BACKSPACE, KEY_DELETE, KEY_DOWN, KEY_END, KEY_HOME, KEY_LCTRL, KEY_LEFT, KEY_PGDN,
    KEY_PGUP, KEY_RIGHT, KEY_TAB, KEY_UP)
from emulator.machine import Machine                                    # noqa: E402
from emulator.memory_map import (                                       # noqa: E402
    BOOT_CHANNEL, CH_CD, CH_DEBUG, CH_DISPLAY, CH_HDD, CH_TIMER, CH_USERPROG, DISPLAY_H, DISPLAY_START, DISPLAY_W, PROGRAM_LOAD_ADDR,
    BOOT_LOAD_ADDR, BOOT_RECORD, BOOT_SIGNATURE, VEC_BREAK)
from emulator.programs import Program                                   # noqa: E402
from pfs import PgfsImage                                               # noqa: E402
from test_files import text_at                                          # noqa: E402

logging.getLogger("emulator.machine").setLevel(logging.ERROR)

OS = REPO_ROOT / "user" / "os"
MiB = 1 << 20
ENTER = 0x0D
PROMPT = re.compile(r"^\S*> _$")
ROWS, COLS, ROW_H = 12, 32, 9
README = b"PigeonOS test disk\nsecond line\n"

_WORK = tempfile.TemporaryDirectory()
WORK = Path(_WORK.name)
BIOS = WORK / "bios.bin"
with contextlib.redirect_stdout(io.StringIO()):
    assemble_file(REPO_ROOT / "firmware" / "bios.asm", BIOS, quiet=True)

# Programs that end each way a program can, and that test what the kernel
# does around them.
STANDINS = {
    "deep": r'''#include <pigeon/sys.h>
int down(int n) { if (n == 0) exit(42); return down(n - 1) + 1; }
int main(void) { return down(50); }
''',
    "div0": r'''#include <pigeon/sys.h>
int zero;
int crash(int n) { if (n == 0) return 10 / zero; return crash(n - 1) + 1; }
int main(void) { return crash(20); }
''',
    "badop": r'''#include <pigeon/sys.h>
unsigned junk[4];
int main(void) {
    void (*f)(void);
    junk[0] = 0xFFFFFFFFu;
    f = (void (*)(void))(unsigned)junk;
    f();
    return 0;
}
''',
    "offend": r'''#include <pigeon/sys.h>
int main(void) { void (*f)(void); f = (void (*)(void))0x07FFFFFCu; f(); return 0; }
''',
    "spin": r'''#include <pigeon/sys.h>
int main(void) { unsigned n; n = 0u; print("spinning\n"); while (1) { n = n + 1u; } return 0; }
''',
    "nested": r'''#include <pigeon/mem.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>
int main(void) {
    unsigned *block;
    unsigned i;
    int s;
    char *a[3];
    char n[12];
    block = (unsigned *)malloc(4000u);
    for (i = 0u; i < 1000u; i++) block[i] = i * 3u + 1u;
    a[0] = "echo"; a[1] = "from-nested"; a[2] = (char *)0;
    s = exec("/bin/echo.bin", 2, a);
    for (i = 0u; i < 1000u; i++) {
        if (block[i] != i * 3u + 1u) { print("heap damaged\n"); return 1; }
    }
    itoa(s, n);
    print("echo gave ");
    print(n);
    print("\n");
    print(sys_strerror(exec("/bin/div0.bin", 1, a)));
    print("\n");
    return 5;
}
''',
    "limit": r'''#include <pigeon/string.h>
#include <pigeon/sys.h>
extern unsigned __heap_limit;
int main(void) { char n[12]; utoa(__heap_limit, n, 16u); print(n); print("\n"); return 0; }
''',
    "flip": r'''#include <pigeon/display.h>
#include <pigeon/sys.h>
int main(void) {
    disp_use_back_buffer();
    disp_clear(RED);
    disp_present();
    disp_clear(BLUE);
    return 0;
}
''',
    "keep": r'''#include <pigeon/sys.h>
int main(void) {
    int fd;
    fd = open("/docs/kept.txt", O_WRITE | O_CREATE);
    if (fd < 0) return fd;
    write(fd, "kept", 4u);
    return 0;
}
''',
    "hello": r'''#include <pigeon/sys.h>
int main(int argc, char **argv) { print("hello from "); print(argv[0]); print("\n"); return 0; }
''',
    "ownfs": r'''#include <pigeon/fs.h>
int main(void) {
    if (fs_mount(CH_HDD) < 0) return 1;
    return fs_save("/docs/own.txt", "mine\n", 5u) != 5;
}
''',
    "lines": r'''#include <pigeon/string.h>
#include <pigeon/sys.h>
int main(int argc, char **argv) {
    char n[12];
    int i;
    int count;
    count = 0;
    if (argc > 1) count = atoi(argv[1]);
    for (i = 1; i <= count; i++) { print("line "); itoa(i, n); print(n); print("\n"); }
    return 0;
}
''',
    "nobreak": r"""#include <pigeon/stdio.h>
#include <pigeon/sys.h>
static void wait_c(char *said) {
    int k;
    print(said);
    while (1) {
        k = getkey();
        if (k == 'c' || k == 'C') break;
    }
}
int main(int argc, char **argv) {
    char line[16];
    char *a[2];
    char m;
    int status;
    m = 'a';
    if (argc > 1) m = argv[1][0];
    printf("was %d\n", setbreak(0));
    if (m == 'l') {
        print("line? ");
        read(STDIN, line, 15u);
    }
    wait_c("press\n");
    print("got c\n");
    if (m == 'c') {
        a[0] = "spin";
        a[1] = (char *)0;
        status = exec("/bin/spin.bin", 1, a);
        printf("child: %s\n", sys_strerror(status));
        wait_c("again\n");
        print("got c again\n");
    }
    return setbreak(1);
}
""",
    "writer": r"""#include <pigeon/stdio.h>
#include <pigeon/sys.h>
int main(void) {
    int fd;
    int i;
    fd = open("/docs/deep.txt", O_WRITE | O_CREATE);
    write(fd, "deep", 4u);
    for (i = 1; i <= 30; i++) printf("deep %d\n", i);
    close(fd);
    return 0;
}
""",
    "spawn": r"""#include <pigeon/stdio.h>
#include <pigeon/sys.h>
int main(void) {
    char *a[2];
    a[0] = "writer";
    a[1] = (char *)0;
    printf("spawn got %d\n", exec("/bin/writer.bin", 1, a));
    return 0;
}
""",
    "ask": r'''#include <pigeon/sys.h>
int main(void) {
    char line[256];
    int n;
    print("line one\nname? ");
    n = read(STDIN, line, 255u);
    if (n > 0) write(STDOUT, line, (unsigned)n);
    return 0;
}
''',
    "term": r'''#include <pigeon/sys.h>
int main(int argc, char **argv) {
    char m;
    int i;
    if (argc < 2) return 1;
    m = argv[1][0];
    if (m == 'c') print("\x1b[31mRRR\x1b[0m\x1b[34mBBB\x1b[0m\n");
    if (m == 'i') print("\x1b[7mINV\x1b[0m\n");
    if (m == 'j') print("junk\x1b[2Jtop\n");
    if (m == 'h') print("\x1b[5;10Hhere\n");
    if (m == 'k') print("\x1b[7;1Habcdef\x1b[7;3H\x1b[K\n");
    if (m == 's') { write(STDOUT, "\x1b[3", 3u); print("2mgreen\x1b[0m\n"); }
    if (m == 'u') print("a\x1b[5qb\x1b[?25lc\n");
    if (m == 'l') print("\x1b[32mleft on\n");
    if (m == 'r') print("\x1b[2J\x1b[1;1Htop\x1b[12;1Hbottom\x1b[3;5r\x1b[3;1H1\n2\n3\n4\n5\x1b[r\x1b[6;1H");
    if (m == 'S') print("\x1b[2J\x1b[1;1Ha\nb\nc\nd\x1b[2;3r\x1b[S\x1b[r\x1b[6;1H");
    if (m == 'T') print("\x1b[2J\x1b[1;1Ha\nb\nc\nd\x1b[2;3r\x1b[T\x1b[r\x1b[6;1H");
    if (m == 'R') print("\x1b[2;4r\x1b[6;1H");
    if (m == 'o') print("a\x1b]133;A\x07b\x1b]0;title\x1b\\c\n");
    if (m == 'O') { print("d\x1b]"); for (i = 0; i < 70; i++) print("x"); print("\n"); }
    return 0;
}
''',
    "fmt": r'''#include <pigeon/stdio.h>
int main(void) {
    printf("%s has %d legs, %x\n", "pigeon", 2, 255);
    printf("[%5d|%-4s|%03u]\n", -7, "ab", 9u);
    printf("%s%s%s\n", "0123456789012345678901234567890123456789",
           "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "!");
    return 0;
}
''',
    "panic": r'''#include <pigeon/sys.h>
int main(void) {
    unsigned wrapper;
    wrapper = *(unsigned *)(SYSCALL_TABLE + SYS_GETKEY * 4u);
    *(unsigned *)(wrapper + 24u) = 0xFFu;       /* its CALL, now a bad instruction */
    *(unsigned *)(wrapper + 28u) = 0u;
    return getkey();
}
''',
}


# --- building ---------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def built(source, relocatable=True):
    source = Path(source)
    binary = WORK / "build" / f"{source.stem}{'.reloc' if relocatable else ''}.bin"
    return Program(name=source.stem, source=source, binary=binary,
                   relocatable=relocatable).ensure_built(quiet=True).read_bytes()


def kernel():
    return built(OS / "kernel.c", relocatable=False)


def standin(name):
    source = WORK / "standins" / f"{name}.c"
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(STANDINS[name])
    return built(source)


def shell_program(name):
    return built(OS / "bin" / f"{name}.c")


def make_disk(path, extra=(), shell=True, label="TEST"):
    """A PigeonFS disk with the shell and its programs in /bin, a readme in
    /docs, and `extra`: (path, bytes) pairs."""
    img = PgfsImage.mkfs(path, 2 * MiB, label=label)
    img.mkdir("/bin")
    img.mkdir("/docs")
    for name in ("sh", "ls", "cat", "echo"):
        if shell or name != "sh":
            img.write_file(f"/bin/{name}.bin", shell_program(name))
    img.write_file("/docs/readme.txt", README)
    for where, data in extra:
        parent = where.rsplit("/", 1)[0]
        if parent and not img.exists(parent):
            img.mkdir(parent, parents=True)
        img.write_file(where, data)
    img.close()
    return path


# --- driving it -------------------------------------------------------------------

def lines(rows):
    """The console's lines, with those it wrapped at COLS joined up again.
    A line of exactly COLS characters looks like a wrapped one; the tests
    print none."""
    out, current = [], ""
    for row in rows:
        current += row
        if len(row) < COLS:
            out.append(current)
            current = ""
    if current:
        out.append(current)
    return out


def last_row(rows):
    """The last line on the screen with anything on it."""
    shown = [line for line in lines(rows) if line.strip()]
    return shown[-1] if shown else ""


class Console:
    """The kernel on a Machine, its disk on channel 2: keys in, rows out."""

    def __init__(self, disk, boot_channel=None, disc=None):
        self.machine = Machine(bios_path=str(BIOS), disk_path=str(disk))
        if disc is not None:
            self.machine.cd.root = None
            self.machine.cd.insert(disc)
        self.machine.ram.load_bytes(kernel(), PROGRAM_LOAD_ADDR)
        self.machine.cpu.pc = PROGRAM_LOAD_ADDR
        if boot_channel is not None:
            self.machine.ram.write_word(BOOT_CHANNEL, boot_channel)

    @classmethod
    def on(cls, machine):
        """A console for a Machine already running the kernel, however it
        got there -- booted from an installed disk, say."""
        console = cls.__new__(cls)
        console.machine = machine
        return console

    def close(self):
        self.machine.close()

    def rows(self):
        fb = self.machine.display_io.snapshot()
        return [text_at(fb, r * ROW_H) for r in range(ROWS)]

    def run_until(self, wanted, seconds=90):
        give_up = time.time() + seconds
        while time.time() < give_up:
            with contextlib.redirect_stdout(io.StringIO()):
                self.machine.run(deadline=time.time() + 0.1)
            if wanted(self.rows()):
                return True
            if self.machine.cpu.halted:
                return False
        return False

    def ready(self):
        return self.run_until(lambda rows: PROMPT.match(last_row(rows)))

    def press(self, *codes):
        for code in codes:
            self.machine.hid.push_key(code, True)
        for code in reversed(codes):
            self.machine.hid.push_key(code, False)

    def type(self, text):
        for ch in text:
            self.press(ENTER if ch == "\n" else ord(ch))

    def command(self, line, seconds=90):
        """Type a line, wait for the next prompt, and return what was printed
        between the two."""
        before = self.rows()
        self.type(line + "\n")
        assert self.run_until(lambda rows: rows != before and PROMPT.match(last_row(rows)),
                              seconds), f"no prompt after {line!r}: {self.rows()}"
        return self.output(line)

    def output(self, line):
        shown = [text for text in lines(self.rows()) if text.strip()]
        end = len(shown) - 1
        start = max(i for i in range(end) if shown[i].endswith("> " + line))
        return shown[start + 1:end]


@contextlib.contextmanager
def booted(extra=(), shell=True, **kw):
    with tempfile.TemporaryDirectory() as t:
        disk = make_disk(Path(t) / "hdd.img", extra, shell)
        console = Console(disk, **kw)
        try:
            yield console
        finally:
            console.close()


# --- booting ----------------------------------------------------------------------

def test_the_kernel_starts_the_shell_from_the_disk():
    with booted() as c:
        assert c.ready(), c.rows()
        rows = c.rows()
        assert rows[0] == "PigeonOS" and rows[1] == "2:/> _", rows


def test_without_a_shell_the_kernel_says_why_and_stops():
    with booted(shell=False) as c:
        assert not c.run_until(lambda rows: False, seconds=20) and c.machine.cpu.halted
        assert lines(c.rows())[:2] == ["PigeonOS",
                                       "cannot start /bin/sh.bin: no such file or directory"], \
            c.rows()


def test_a_disk_with_no_filesystem_is_reported():
    with tempfile.TemporaryDirectory() as t:
        disk = Path(t) / "blank.img"
        disk.write_bytes(bytes(MiB))
        c = Console(disk)
        try:
            assert c.run_until(lambda rows: rows[1].startswith("cannot mount disk 2:")), c.rows()
            assert c.run_until(lambda rows: False, seconds=5) is False and c.machine.cpu.halted
        finally:
            c.close()


@cases(("booted from the CD", CH_CD, "6:/> _"),
       ("booted from the hard disk", CH_HDD, "2:/> _"),
       ("booted from channel 1", CH_USERPROG, "2:/> _"),
       ("started without bios2", None, "2:/> _"))
def test_the_kernel_mounts_the_disk_it_booted_from(label, channel, prompt):
    """docs/kernel.md Q8: BOOT_CHANNEL when it is the hard disk or the CD,
    and the hard disk otherwise. Both have a shell here."""
    with tempfile.TemporaryDirectory() as t:
        disk = make_disk(Path(t) / "hdd.img")
        disc = make_disk(Path(t) / "disc.img", label="DISC")
        c = Console(disk, boot_channel=channel, disc=disc)
        try:
            assert c.ready(), f"{label}: {c.rows()}"
            assert c.rows()[1] == prompt, f"{label}: {c.rows()}"
        finally:
            c.close()


# --- the shell and its programs ----------------------------------------------------------

def test_programs_get_their_arguments():
    with booted() as c:
        assert c.ready(), c.rows()
        assert c.command('echo one  "two words"   three') == ["one two words three"]


def test_ls_and_cat_read_the_disk_through_the_kernel():
    with booted() as c:
        assert c.ready(), c.rows()
        assert sorted(c.command("ls /bin")) == ["cat.bin", "echo.bin", "ls.bin", "sh.bin"]
        assert c.command("ls /") == ["bin/", "docs/"]
        assert c.command("cat /docs/readme.txt") == README.decode().splitlines()
        assert c.command("cat /docs/nothing") == ["cat: /docs/nothing: not found", "cat: exit 1"]


def test_cd_changes_the_directory_every_program_starts_in():
    with booted() as c:
        assert c.ready(), c.rows()
        assert c.command("cd /docs") == []
        assert last_row(c.rows()) == "2:/docs> _"
        assert c.command("ls") == ["readme.txt"]
        assert c.command("cat readme.txt") == README.decode().splitlines()
        assert c.command("cd /nowhere") == ["cd: not found"]
        assert c.command("cd") == [] and last_row(c.rows()) == "2:/> _"


def test_a_name_is_looked_up_in_bin_then_where_you_are():
    """docs/kernel_exec.md Q4. The .bin is added when it is missing."""
    with booted(extra=[("/docs/hello.bin", standin("hello"))]) as c:
        assert c.ready(), c.rows()
        assert c.command("hello") == ["hello: not found"]
        assert c.command("cd /docs") == []
        assert c.command("hello") == ["hello from hello"]
        assert c.command("hello.bin") == ["hello from hello.bin"]
        assert c.command("/bin/echo x") == ["x"]
        assert c.command("help")[0] == "cd DIR   go to a directory"


@cases(("returns, after exit() fifty calls deep", "deep", "deep: exit 42"),
       ("divides by zero twenty calls deep", "div0", "div0: divided by zero"),
       ("runs a bad instruction", "badop", "badop: ran a bad instruction"),
       ("runs off the end of memory", "offend", "offend: ran off the end of memory"))
def test_however_a_program_ends_the_shell_carries_on(label, name, reported):
    with booted(extra=[(f"/bin/{name}.bin", standin(name))]) as c:
        assert c.ready(), c.rows()
        assert c.command(name) == [reported], label
        assert c.command("echo still here") == ["still here"], label


def test_ctrl_c_stops_a_program_that_never_ends():
    with booted(extra=[("/bin/spin.bin", standin("spin"))]) as c:
        assert c.ready(), c.rows()
        c.type("spin\n")
        assert c.run_until(lambda rows: last_row(rows) == "spinning"), c.rows()
        c.press(KEY_LCTRL, ord("c"))
        assert c.run_until(lambda rows: PROMPT.match(last_row(rows))), c.rows()
        assert c.output("spin") == ["spinning", "^C", "spin: stopped"]


def test_a_program_runs_programs_above_itself():
    """The child goes above the parent's heap, which is intact afterwards;
    a crash two levels down ends only the child."""
    with booted(extra=[("/bin/nested.bin", standin("nested")),
                       ("/bin/div0.bin", standin("div0"))]) as c:
        assert c.ready(), c.rows()
        assert c.command("nested") == ["from-nested", "echo gave 0", "divided by zero",
                                       "nested: exit 5"]


def test_each_program_is_given_its_heap_limit():
    """docs/kernel.md Q7: exec writes __heap_limit before calling it."""
    with booted(extra=[("/bin/limit.bin", standin("limit"))]) as c:
        assert c.ready(), c.rows()
        assert c.command("limit") == ["7f00000"]


def test_the_screen_comes_back_after_a_program_draws_its_own():
    """The program page-flips to a buffer on its own heap and paints the
    screen. Afterwards the display shows the screen's memory again, with
    the console's text redrawn on it."""
    with booted(extra=[("/bin/flip.bin", standin("flip"))]) as c:
        assert c.ready(), c.rows()
        assert c.command("flip") == []
        scanout = c.machine.display_io.scanout_base
        assert scanout == DISPLAY_START, f"the display still shows {scanout:#x}"
        assert c.rows()[:2] == ["PigeonOS", "2:/> flip"], c.rows()


def test_a_file_a_program_leaves_open_is_closed_for_it():
    """One writer per file: had the kernel not closed the first run's
    handle, the second run's open would be refused as busy."""
    with tempfile.TemporaryDirectory() as t:
        disk = make_disk(Path(t) / "hdd.img", [("/bin/keep.bin", standin("keep"))])
        c = Console(disk)
        try:
            assert c.ready(), c.rows()
            assert c.command("keep") == []
            assert c.command("keep") == []
        finally:
            c.close()
        with PgfsImage(disk) as img:
            assert img.read_file("/docs/kept.txt") == b"kept"
            assert img.fsck().clean


def test_a_program_with_its_own_fs_c_leaves_the_kernel_seeing_the_disk_as_it_is():
    """A program may bundle fs.c and write behind the kernel's back. The
    kernel mounts the disk again afterwards, so its cache neither hides the
    new file nor hands the new file's blocks out again."""
    with tempfile.TemporaryDirectory() as t:
        disk = make_disk(Path(t) / "hdd.img", [("/bin/ownfs.bin", standin("ownfs")),
                                               ("/bin/keep.bin", standin("keep"))])
        c = Console(disk)
        try:
            assert c.ready(), c.rows()
            assert c.command("ls /docs") == ["readme.txt"]
            assert c.command("ownfs") == []
            assert c.command("ls /docs") == ["own.txt", "readme.txt"]
            assert c.command("cat /docs/own.txt") == ["mine"]
            assert c.command("keep") == []
        finally:
            c.close()
        with PgfsImage(disk) as img:
            assert img.read_file("/docs/own.txt") == b"mine\n"
            assert img.read_file("/docs/kept.txt") == b"kept"
            assert img.fsck().clean


def test_ctrl_c_at_the_prompt_throws_the_line_away():
    """While the console reads a line, Ctrl+C is a key: the shell isn't
    ended, and the next line runs."""
    with booted() as c:
        assert c.ready(), c.rows()
        c.type("echo never")
        c.press(KEY_LCTRL, ord("c"))
        assert c.command("echo yes") == ["yes"]
        assert "2:/> echo never^C" in lines(c.rows()), c.rows()
        assert "shell ended, starting it again" not in lines(c.rows()), c.rows()


# --- the console as a terminal: docs/phase4_plan.md step 3 -----------------------

RED, GREEN, BLUE, INK, BG = (255, 0, 0), (0, 255, 0), (0, 0, 255), (216, 216, 216), (0, 0, 0)
MAGENTA, WHITE = (255, 0, 255), (255, 255, 255)


def cell_colors(fb, row, col):
    """The colors a cell's pixels hold, as (r, g, b): the framebuffer keeps
    each pixel's bytes blue, green, red, alpha."""
    colors = set()
    for dy in range(ROW_H):
        for dx in range(6):
            i = ((row * ROW_H + dy) * DISPLAY_W + col * 6 + dx) * 4
            colors.add((fb[i + 2], fb[i + 1], fb[i]))
    return colors


def row_of(console, text):
    return next(r for r, row in enumerate(console.rows()) if row.startswith(text))


@contextlib.contextmanager
def terminal():
    """The shell with a colorless prompt. Its built-in one is red (sh.c), and
    these tests read the ink of the row under a program's output, which is a
    prompt: they are about the console and what the kernel resets, not about
    what the shell paints its prompt."""
    with booted(extra=[("/bin/term.bin", standin("term")),
                       ("/etc/shell_header.conf", b"``CWD``> ")]) as c:
        assert c.ready(), c.rows()
        yield c


def test_colors_are_drawn_and_kept_through_a_scroll():
    with terminal() as c:
        assert c.command("term c") == ["RRRBBB"]
        row = row_of(c, "RRRBBB")
        fb = c.machine.display_io.snapshot()
        assert cell_colors(fb, row, 0) == {RED, BG} and cell_colors(fb, row, 3) == {BLUE, BG}
        for i in range(5):
            c.command(f"echo {i}")
        moved = row_of(c, "RRRBBB")
        assert moved < row, "the screen did not scroll"
        fb = c.machine.display_io.snapshot()
        assert cell_colors(fb, moved, 0) == {RED, BG} and cell_colors(fb, moved, 3) == {BLUE, BG}
        assert cell_colors(fb, moved + 1, 0) <= {INK, BG}, "the color outlived ESC [ 0 m"


def test_inverse_fills_the_cell_with_the_ink():
    with terminal() as c:
        c.type("term i\n")
        assert c.run_until(lambda rows: PROMPT.match(last_row(rows))), c.rows()
        fb = c.machine.display_io.snapshot()
        row = next(r for r in range(ROWS) if r > 1 and cell_colors(fb, r, 0) == {INK, BG}
                   and cell_colors(fb, r, 3) == {BG})
        lit = sum(1 for dy in range(ROW_H) for dx in range(6)
                  if (fb[((row * ROW_H + dy) * DISPLAY_W + dx) * 4 + 2]) == 216)
        assert lit > 30, f"cell 0 of row {row} is not inverse: {lit} lit pixels"


def test_clearing_the_screen_puts_the_cursor_home():
    with terminal() as c:
        c.type("term j\n")
        assert c.run_until(lambda rows: rows[0] == "top" and PROMPT.match(last_row(rows))), c.rows()
        assert c.rows()[1] == "2:/> _" and "junk" not in "".join(c.rows())


def test_the_cursor_moves_and_a_line_clears_to_its_end():
    with terminal() as c:
        c.command("term h")
        assert c.rows()[4] == "         here", c.rows()
        c.command("term k")
        assert c.rows()[6] == "ab", c.rows()


def test_a_sequence_split_across_two_writes_still_works():
    with terminal() as c:
        assert c.command("term s") == ["green"]
        fb = c.machine.display_io.snapshot()
        assert cell_colors(fb, row_of(c, "green"), 0) == {GREEN, BG}


def test_sequences_the_console_doesnt_know_are_dropped():
    with terminal() as c:
        assert c.command("term u") == ["abc"]


def test_a_color_left_on_by_a_program_is_reset_after_it():
    with terminal() as c:
        c.command("term l")
        fb = c.machine.display_io.snapshot()
        row = row_of(c, "left on")
        assert cell_colors(fb, row, 0) == {GREEN, BG}
        assert cell_colors(fb, row + 1, 0) <= {INK, BG}, "the prompt came out green"


# --- scrolling: docs/phase4b_plan.md step 1 ----------------------------------------

def test_a_scroll_region_scrolls_only_its_rows():
    """Rows 3 to 5 take five lines; the rows around them stay, and ESC [ r
    gives the shell the whole screen back."""
    with terminal() as c:
        c.type("term r\n")
        assert c.run_until(lambda rows: rows[5] == "2:/> _"), c.rows()
        rows = c.rows()
        assert rows[0] == "top" and rows[11] == "bottom", rows
        assert rows[2:5] == ["3", "4", "5"], rows
        assert rows[1] == "" and rows[6:11] == [""] * 5, rows


@cases(("up, ESC [ S", "S", ["a", "c", "", "d"]),
       ("down, ESC [ T", "T", ["a", "", "b", "d"]))
def test_a_region_scrolls_when_asked(label, mode, shown):
    with terminal() as c:
        c.type(f"term {mode}\n")
        assert c.run_until(lambda rows: rows[5] == "2:/> _"), f"{label}: {c.rows()}"
        assert c.rows()[:4] == shown, f"{label}: {c.rows()}"


def test_a_scroll_region_left_set_is_undone_after_the_program():
    """term R sets rows 2 to 4 and ends. Kept, the shell's output would stop
    scrolling at the bottom row and write over itself there."""
    with terminal() as c:
        c.type("term R\n")
        assert c.run_until(lambda rows: rows[5] == "2:/> _"), c.rows()
        for i in range(6):
            c.command(f"echo {i}")
        assert c.rows()[9:12] == ["2:/> echo 5", "5", "2:/> _"], c.rows()


def kernel_symbols():
    """The kernel's labels, from the assembly its build left beside it."""
    kernel()
    asm = Assembler(str(WORK / "build" / "kernel.asm"))
    with contextlib.redirect_stdout(io.StringIO()):
        asm.assemble()
    return asm.symbols


def test_a_console_scroll_costs_a_few_thousand_instructions():
    """Counted, not timed: one con_newline on the bottom row, from its first
    instruction to its return. Redrawing the screen instead cost up to a
    million (docs/phase4b_plan.md §1)."""
    entry = kernel_symbols()["con_newline"]
    with booted() as c:
        assert c.ready(), c.rows()
        for i in range(5):
            c.command(f"echo {i}")
        assert c.rows()[11] == "2:/> _", c.rows()
        cpu = c.machine.cpu
        c.press(ENTER)
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(1_000_000):
                if cpu.pc == entry:
                    break
                c.machine.step()
            assert cpu.pc == entry, "Enter never reached con_newline"
            sp, steps = cpu.sp, 0
            while cpu.sp <= sp and steps < 2_000_000:
                c.machine.step()
                steps += 1
        assert c.rows()[9:12] == ["4", "2:/>", ""], c.rows()
        assert steps < 20_000, f"a console scroll took {steps:,} instructions"


# --- line editing and history: docs/phase4b_plan.md step 2 ------------------------

def edited(c, keys, line):
    """Keys pressed at the prompt -- text typed, a key code, or a chord such
    as (KEY_LCTRL, ord("a")) -- then Enter: what the line printed, found on
    the screen by the line as it should read."""
    before = c.rows()
    for key in keys:
        if isinstance(key, str):
            c.type(key)
        elif isinstance(key, tuple):
            c.press(*key)
        else:
            c.press(key)
    c.press(ENTER)
    assert c.run_until(lambda rows: rows != before and PROMPT.match(last_row(rows))), c.rows()
    return c.output(line)


def ctrl(letter):
    return (KEY_LCTRL, ord(letter))


@cases(("typing in the middle", ["echo hllo", KEY_LEFT, KEY_LEFT, KEY_LEFT, "e"], "echo hello", ["hello"]),
       ("Backspace in the middle", ["echo helxlo", KEY_LEFT, KEY_LEFT, KEY_BACKSPACE], "echo hello", ["hello"]),
       ("Delete under the cursor", ["echo hexllo", KEY_LEFT, KEY_LEFT, KEY_LEFT, KEY_LEFT, KEY_DELETE],
        "echo hello", ["hello"]),
       ("Home and End", ["cho hi", KEY_HOME, "e", KEY_END, "!"], "echo hi!", ["hi!"]),
       ("Ctrl+A and Ctrl+E", ["cho hi", ctrl("a"), "e", ctrl("e"), "!"], "echo hi!", ["hi!"]),
       ("Ctrl+U throwing the line away", ["garbage", ctrl("u"), "echo ok"], "echo ok", ["ok"]),
       ("moving past either end", ["echo ab", KEY_RIGHT, KEY_HOME, KEY_LEFT, KEY_END, "c"], "echo abc",
        ["abc"]),
       ("deleting past either end", ["echo ab", KEY_DELETE, KEY_HOME, KEY_BACKSPACE, KEY_END, "c"],
        "echo abc", ["abc"]),
       ("Ctrl with a letter typing nothing", ["echo a", ctrl("z"), "b"], "echo ab", ["ab"]))
def test_the_line_is_edited_where_the_cursor_is(label, keys, line, printed):
    with booted() as c:
        assert c.ready(), c.rows()
        assert edited(c, keys, line) == printed, f"{label}: {c.rows()}"


def test_a_long_line_wraps_and_is_edited_on_its_second_row():
    word = "abcdefghijklmnopqrstuvwxyz0123456789"
    with booted() as c:
        assert c.ready(), c.rows()
        keys = ["echo " + word, KEY_LEFT, KEY_LEFT, KEY_LEFT, "!", KEY_END, "?"]
        typed = word[:-3] + "!" + word[-3:] + "?"
        assert edited(c, keys, "echo " + typed) == [typed], c.rows()


def test_a_line_typed_on_the_bottom_row_scrolls_the_screen_under_it():
    """A two-line prompt on the bottom row: the line wraps, the screen
    scrolls under it, and it is edited back on the row that moved up."""
    with booted(extra=[("/etc/shell_header.conf", b'"``CWD``\\n> "')]) as c:
        assert c.ready(), c.rows()
        for i in range(3):
            c.command(f"echo {i}")
        assert c.rows()[10:] == ["2:/", "> _"], c.rows()
        keys = ["echo " + "z" * 40] + [KEY_LEFT] * 20 + ["Q"]
        typed = "z" * 20 + "Q" + "z" * 20
        # what it printed, then the next prompt's first row
        assert edited(c, keys, "echo " + typed) == [typed, "2:/"], c.rows()
        shown = lines(c.rows())
        at = shown.index("> echo " + typed)
        assert shown[at - 1] == "2:/", shown


def test_up_and_down_walk_through_the_lines_typed():
    """Empty lines and a line repeating the one before aren't kept, Up
    stops at the oldest, and Down past the newest gives back the typing."""
    with booted() as c:
        assert c.ready(), c.rows()
        for line in ("echo one", "echo two", "echo two"):
            c.command(line)
        before = c.rows()
        c.press(ENTER)
        assert c.run_until(lambda rows: rows != before and last_row(rows) == "2:/> _"), c.rows()

        def shows(line):
            assert c.run_until(lambda rows: last_row(rows) == f"2:/> {line}_"), (line, c.rows())

        c.type("ec")
        c.press(KEY_UP)
        shows("echo two")
        c.press(KEY_UP)
        shows("echo one")
        c.press(KEY_UP)
        c.press(KEY_DOWN)
        shows("echo two")
        c.press(KEY_DOWN)
        shows("ec")
        assert edited(c, [KEY_DOWN, "ho back"], "echo back") == ["back"]


def test_ctrl_l_moves_the_prompt_to_the_top_by_its_mark():
    """The disc's kind of prompt: a blank line, then two rows. The blank row
    at the mark is skipped."""
    with booted(extra=[("/etc/shell_header.conf", b'"\\n``CWD``\\n> "')]) as c:
        assert c.ready(), c.rows()
        c.command("echo hi")
        c.type("echo x")
        c.press(*ctrl("l"))
        assert c.run_until(lambda rows: rows[:2] == ["2:/", "> echo x_"]), c.rows()
        assert c.rows()[2:] == [""] * 10, c.rows()
        assert edited(c, [], "echo x") == ["x", "2:/"]


def test_ctrl_l_without_a_mark_moves_the_line_itself_to_the_top():
    with booted(extra=[("/bin/ask.bin", standin("ask"))]) as c:
        assert c.ready(), c.rows()
        c.type("ask\n")
        assert c.run_until(lambda rows: last_row(rows) == "name? _"), c.rows()
        c.type("ab")
        c.press(*ctrl("l"))
        assert c.run_until(lambda rows: rows[0] == "name? ab_"), c.rows()
        c.press(ENTER)
        assert c.run_until(lambda rows: PROMPT.match(last_row(rows))), c.rows()
        assert lines(c.rows())[:2] == ["name? ab", "ab"], c.rows()


# --- Tab completion: docs/phase4b_plan.md step 3 ----------------------------------

@cases(("a command from /bin", ["ec", KEY_TAB, "hi"], "echo hi", ["hi"]),
       ("a built-in", ["hel", KEY_TAB], "help",
        ["cd DIR   go to a directory", "exit     end the shell", "help     this",
         "Anything else runs a program;", "ls /bin shows them."]),
       ("a file", ["cat /docs/re", KEY_TAB], "cat /docs/readme.txt", ["PigeonOS test disk", "second line"]),
       ("a directory, with its /", ["ls /do", KEY_TAB], "ls /docs/", ["readme.txt"]),
       ("in the middle of a line", ["cat /do x", KEY_LEFT, KEY_LEFT, KEY_TAB, "re", KEY_TAB, KEY_END,
                                   KEY_BACKSPACE, KEY_BACKSPACE], "cat /docs/readme.txt",
        ["PigeonOS test disk", "second line"]),
       ("no match leaving the word alone", ["echo zz", KEY_TAB], "echo zz", ["zz"]))
def test_tab_completes_the_word_before_the_cursor(label, keys, line, printed):
    with booted() as c:
        assert c.ready(), c.rows()
        assert edited(c, keys, line) == printed, f"{label}: {c.rows()}"


def test_a_second_tab_lists_the_names_that_match_under_the_line():
    extra = [("/docs/alpha1.txt", b"one\n"), ("/docs/alpha2.txt", b"two\n")]
    with booted(extra=extra) as c:
        assert c.ready(), c.rows()
        c.type("cat /docs/al")
        c.press(KEY_TAB)
        assert c.run_until(lambda rows: rows[1] == "2:/> cat /docs/alpha_"), c.rows()
        c.press(KEY_TAB)
        # the cursor is drawn last: wait for it too, or catch it half drawn
        assert c.run_until(lambda rows: rows[2] == "alpha1.txt  alpha2.txt"
                           and rows[1] == "2:/> cat /docs/alpha_"), c.rows()
        c.type("2")
        assert c.run_until(lambda rows: rows[1] == "2:/> cat /docs/alpha2_" and rows[2] == ""), c.rows()
        assert edited(c, [KEY_TAB], "cat /docs/alpha2.txt") == ["two"]


def test_the_commands_listed_include_the_built_ins_in_order():
    with booted(extra=with_commands()) as c:
        assert c.ready(), c.rows()
        c.type("c")
        c.press(KEY_TAB)
        c.press(KEY_TAB)
        assert c.run_until(lambda rows: rows[2] == "cat    cd     clear  cp"), c.rows()


def test_a_long_list_scrolls_the_line_up_and_counts_what_doesnt_fit():
    extra = [(f"/docs/f{i:02}.txt", b"") for i in range(40)]
    with booted(extra=extra) as c:
        assert c.ready(), c.rows()
        for i in range(5):
            c.command(f"echo {i}")
        assert c.rows()[11] == "2:/> _", c.rows()
        c.type("cat /docs/f")
        c.press(KEY_TAB)
        c.press(KEY_TAB)
        assert c.run_until(lambda rows: rows[11] == "and 10 more"
                           and rows[0] == "2:/> cat /docs/f_"), c.rows()
        rows = c.rows()
        assert rows[1] == "f00.txt  f01.txt  f02.txt" and rows[10] == "f27.txt  f28.txt  f29.txt", rows


def test_a_name_with_a_space_comes_back_in_quotes():
    with booted(extra=[("/docs/my notes.txt", b"quoted\n")]) as c:
        assert c.ready(), c.rows()
        assert edited(c, ["cat /docs/my", KEY_TAB], 'cat "/docs/my notes.txt"') == ["quoted"]


def test_a_program_that_names_no_commands_completes_file_names_only():
    """ask never calls setcomplete, and the shell's setting isn't passed on."""
    with booted(extra=[("/bin/ask.bin", standin("ask"))]) as c:
        assert c.ready(), c.rows()
        c.type("ask\n")
        assert c.run_until(lambda rows: last_row(rows) == "name? _"), c.rows()
        c.type("ech")
        c.press(KEY_TAB)
        c.type(" /do")
        c.press(KEY_TAB)
        assert c.run_until(lambda rows: last_row(rows) == "name? ech /docs/_"), c.rows()
        c.press(ENTER)
        assert c.run_until(lambda rows: PROMPT.match(last_row(rows))), c.rows()
        shown = lines(c.rows())
        assert shown[shown.index("name? ech /docs/") + 1] == "ech /docs/", shown


# --- scrollback and the wheel: docs/phase4b_plan.md step 4 ------------------------

def marker(fb, text):
    """Whether `text` is the scrollback marker: inverse cells ending row 0,
    with the cell before them not inverse."""
    def inverse(col):
        lit = sum(1 for dy in range(ROW_H) for dx in range(6)
                  if fb[(dy * DISPLAY_W + col * 6 + dx) * 4 + 2] == 216)
        return lit > 30
    start = COLS - len(text)
    return all(inverse(col) for col in range(start, COLS)) and not inverse(start - 1)


def shows_marker(c, text):
    """For run_until: the marker is drawn after the rows, so wait for it too."""
    return marker(c.machine.display_io.snapshot(), text)


def notch(c, button):
    c.machine.hid.push_mouse_event(button, True)
    c.machine.hid.push_mouse_event(button, False)


def run_lines(c, count):
    """`lines COUNT`, waited for by the screen it leaves: its last 11 lines
    and the prompt. c.command can't find the command once it scrolls off."""
    c.type(f"lines {count}\n")
    assert c.run_until(lambda rows: rows[0] == f"line {count - 10}" and rows[11] == "2:/> _",
                       seconds=180), c.rows()


@contextlib.contextmanager
def scrolled_off(count):
    """`lines COUNT` run, so that many rows and more have scrolled off. The
    scrollback then holds PigeonOS, the command, and the lines before the
    screen's: row k of it is line k - 1."""
    with booted(extra=[("/bin/lines.bin", standin("lines"))] + with_commands()) as c:
        assert c.ready(), c.rows()
        run_lines(c, count)
        yield c


def test_pgup_and_pgdn_look_back_and_a_key_comes_back():
    with scrolled_off(30) as c:
        assert c.rows()[0] == "line 20", c.rows()
        c.press(KEY_PGUP)
        assert c.run_until(lambda rows: rows[0].startswith("line 9") and rows[11] == "line 20"
                           and shows_marker(c, "-11")), c.rows()
        c.press(KEY_PGUP)                          # 21 rows is all there are
        assert c.run_until(lambda rows: rows[0].startswith("PigeonOS") and shows_marker(c, "-21")), \
            c.rows()
        c.press(KEY_PGDN)                          # 10 rows back
        assert c.run_until(lambda rows: rows[0].startswith("line 10")), c.rows()
        c.type("x")
        assert c.run_until(lambda rows: rows[0] == "line 20" and last_row(rows) == "2:/> x_"), c.rows()
        assert not marker(c.machine.display_io.snapshot(), "-10")


def test_the_wheel_moves_the_view_three_rows_a_notch():
    with scrolled_off(30) as c:
        notch(c, 5)
        notch(c, 5)
        assert c.run_until(lambda rows: rows[0].startswith("line 14") and rows[11] == "line 25"
                           and shows_marker(c, "-6")), c.rows()
        notch(c, 6)
        notch(c, 6)
        assert c.run_until(lambda rows: rows[0] == "line 20" and rows[11] == "2:/> _"), c.rows()


def test_the_scrollback_keeps_the_last_hundred_rows():
    with scrolled_off(130) as c:
        for _ in range(10):
            c.press(KEY_PGUP)
        assert c.run_until(lambda rows: rows[0].startswith("line 20") and shows_marker(c, "-100")), \
            c.rows()


def test_esc_bracket_2_j_keeps_the_scrollback_and_clear_empties_it():
    """How many rows the scrollback holds is read from the kernel's memory:
    on the screen, a PgUp into an empty scrollback and a key typed after it
    end the same way as a PgUp and a key after a full one."""
    back_count = kernel_symbols()["__g_back_count"]
    with scrolled_off(30) as c:
        assert c.machine.ram.read_word(back_count) == 21
        c.type("clear\n")
        assert c.run_until(lambda rows: rows[0] == "2:/> _"), c.rows()
        assert c.machine.ram.read_word(back_count) == 0, "clear left the scrollback"
        c.press(KEY_PGUP)
        c.type("x")
        assert c.run_until(lambda rows: rows[0] == "2:/> x_"), c.rows()
    with booted(extra=[("/bin/lines.bin", standin("lines")), ("/bin/term.bin", standin("term"))]) as c:
        assert c.ready(), c.rows()
        run_lines(c, 30)
        c.type("term j\n")
        assert c.run_until(lambda rows: rows[0] == "top" and PROMPT.match(last_row(rows))), c.rows()
        c.press(KEY_PGUP)
        assert c.run_until(lambda rows: rows[11] == "top" and rows[0] != ""), c.rows()


# --- break per program: docs/phase4b_plan.md step 5 -------------------------------

@cases(("straight after setbreak", "nobreak", False),
       ("after reading a line, which used to turn break back on", "nobreak line", True))
def test_a_program_that_turns_break_off_gets_ctrl_c_as_a_key(label, command, reads):
    """nobreak waits for Ctrl+C with getkey, which reads characters; the
    same keys stay in the event queue, where read() would find them. So
    each case presses Ctrl+C once, after anything that reads a line."""
    with booted(extra=[("/bin/nobreak.bin", standin("nobreak"))]) as c:
        assert c.ready(), c.rows()
        c.type(command + "\n")
        if reads:
            assert c.run_until(lambda rows: last_row(rows) == "line? _"), f"{label}: {c.rows()}"
            c.type("x\n")
        assert c.run_until(lambda rows: last_row(rows) == "press"), f"{label}: {c.rows()}"
        c.press(*ctrl("c"))
        assert c.run_until(lambda rows: PROMPT.match(last_row(rows))), c.rows()
        shown = lines(c.rows())
        assert "was 1" in shown and "got c" in shown, shown
        assert not any("stopped" in line for line in shown), shown


def test_break_is_on_for_a_child_and_off_again_for_its_parent():
    extra = [("/bin/nobreak.bin", standin("nobreak")), ("/bin/spin.bin", standin("spin"))]
    with booted(extra=extra) as c:
        assert c.ready(), c.rows()
        c.type("nobreak child\n")
        assert c.run_until(lambda rows: last_row(rows) == "press"), c.rows()
        c.press(*ctrl("c"))
        assert c.run_until(lambda rows: last_row(rows) == "spinning"), c.rows()
        c.press(*ctrl("c"))                        # the child: break is on
        assert c.run_until(lambda rows: last_row(rows) == "again"), c.rows()
        assert "child: stopped" in lines(c.rows()), c.rows()
        c.press(*ctrl("c"))                        # the parent again: a key
        assert c.run_until(lambda rows: PROMPT.match(last_row(rows))), c.rows()
        assert "got c again" in lines(c.rows()), c.rows()
        c.type("spin\n")                           # the next program: break on
        assert c.run_until(lambda rows: last_row(rows) == "spinning"), c.rows()
        c.press(*ctrl("c"))
        assert c.run_until(lambda rows: PROMPT.match(last_row(rows))), c.rows()
        assert "spin: stopped" in lines(c.rows()), c.rows()


# --- paging and more: docs/phase4b_plan.md step 6 ---------------------------------

def inverse_cell(fb, row, col):
    lit = sum(1 for dy in range(ROW_H) for dx in range(6)
              if fb[((row * ROW_H + dy) * DISPLAY_W + col * 6 + dx) * 4 + 2] == 216)
    return lit > 30


def paused(c):
    """-- more --: ten inverse cells starting the bottom row."""
    fb = c.machine.display_io.snapshot()
    return all(inverse_cell(fb, 11, col) for col in range(10))


def with_more(*extra):
    return [("/bin/more.bin", shell_program("more"))] + list(extra)


def test_more_pages_a_file_with_space_enter_and_q():
    text = "".join(f"row {i}\n" for i in range(1, 31)).encode()
    with booted(extra=with_more(("/docs/long.txt", text))) as c:
        assert c.ready(), c.rows()
        c.type("more /docs/long.txt\n")
        assert c.run_until(lambda rows: rows[0] == "row 1" and rows[10] == "row 11" and paused(c)), c.rows()
        c.type(" ")
        assert c.run_until(lambda rows: rows[10] == "row 22" and paused(c)), c.rows()
        c.press(ENTER)
        assert c.run_until(lambda rows: rows[10] == "row 23" and paused(c)), c.rows()
        c.type("q")
        assert c.run_until(lambda rows: rows[10] == "row 23" and rows[11] == "2:/> _"), c.rows()


def test_more_pages_a_command_with_more_entries_than_fit():
    extra = with_more(("/bin/lines.bin", standin("lines")), ("/bin/term.bin", standin("term")))
    with booted(extra=extra + with_commands()) as c:
        assert c.ready(), c.rows()
        c.type("more ls /bin\n")
        assert c.run_until(lambda rows: rows[0] == "cat.bin" and rows[10] == "rmdir.bin" and paused(c)), \
            c.rows()
        c.type(" ")
        assert c.run_until(lambda rows: rows[9:12] == ["sh.bin", "term.bin", "2:/> _"]), c.rows()


def test_pgup_looks_back_while_more_waits():
    with booted(extra=with_more(("/bin/lines.bin", standin("lines")))) as c:
        assert c.ready(), c.rows()
        c.type("more lines 30\n")
        assert c.run_until(lambda rows: rows[10] == "line 11" and paused(c)), c.rows()
        c.press(KEY_PGUP)                          # two rows are all that scrolled off
        assert c.run_until(lambda rows: rows[0].startswith("PigeonOS") and shows_marker(c, "-2")), c.rows()
        c.type(" ")
        assert c.run_until(lambda rows: rows[10] == "line 22" and paused(c)), c.rows()
        c.type("q")
        assert c.run_until(lambda rows: rows[11] == "2:/> _"), c.rows()
        c.type("lines 15\n")                     # paging ended with more
        assert c.run_until(lambda rows: rows[10] == "line 15" and rows[11] == "2:/> _"), c.rows()


def test_ctrl_c_at_more_stops_the_command_writing():
    with booted(extra=with_more(("/bin/lines.bin", standin("lines")))) as c:
        assert c.ready(), c.rows()
        c.type("more lines 30\n")
        assert c.run_until(lambda rows: rows[10] == "line 11" and paused(c)), c.rows()
        c.press(*ctrl("c"))
        assert c.run_until(lambda rows: rows[11] == "2:/> _"), c.rows()
        shown = lines(c.rows())
        assert shown[-4:-1] == ["^C", "more: lines: stopped", "more: exit 1"], shown


def test_q_ends_the_command_and_what_it_ran_and_closes_their_files():
    """spawn runs writer, which opens a file and prints; q ends both, and
    the kernel keeps no file open for either."""
    handles = kernel_symbols()["__g_handle_depth"]
    extra = with_more(("/bin/spawn.bin", standin("spawn")), ("/bin/writer.bin", standin("writer")))
    with booted(extra=extra) as c:
        assert c.ready(), c.rows()
        c.type("more spawn\n")
        assert c.run_until(lambda rows: rows[10] == "deep 11" and paused(c)), c.rows()
        c.type("q")
        assert c.run_until(lambda rows: rows[11] == "2:/> _"), c.rows()
        assert [c.machine.ram.read_word(handles + 4 * i) for i in range(16)] == [0] * 16
        assert not any("spawn got" in line or "stopped" in line for line in lines(c.rows())), c.rows()
        assert c.command("echo ok") == ["ok"]


@cases(("a file", "more /docs/readme.txt", ["PigeonOS test disk", "second line"]),
       ("a command", "more echo hi", ["hi"]),
       ("a word that is neither", "more nosuch", ["more: nosuch: not found", "more: exit 1"]),
       ("no words", "more", ["usage: more FILE... or more COMMAND ARGS...", "more: exit 1"]))
def test_more_takes_a_file_as_a_file_and_anything_else_as_a_command(label, line, printed):
    with booted(extra=with_more()) as c:
        assert c.ready(), c.rows()
        assert c.command(line) == printed, f"{label}: {c.rows()}"


@cases(("a known one and one ended by ESC \\", "o", ["abc"]),
       ("one never ended, given up after 64 characters", "O", ["d" + "x" * 6]))
def test_esc_bracket_sequences_are_not_printed(label, mode, printed):
    with terminal() as c:
        assert c.command(f"term {mode}") == printed, f"{label}: {c.rows()}"


# --- printf, the prompt, file commands and ls: docs/phase4_plan.md steps 2, 5-7 --

FILE_COMMANDS = ("mkdir", "rmdir", "rm", "mv", "cp", "clear")


def with_commands(extra=()):
    return [(f"/bin/{name}.bin", shell_program(name)) for name in FILE_COMMANDS] + list(extra)


def test_printf_reaches_the_console_through_the_kernel():
    with booted(extra=[("/bin/fmt.bin", standin("fmt"))]) as c:
        assert c.ready(), c.rows()
        assert c.command("fmt") == ["pigeon has 2 legs, ff", "[   -7|ab  |009]",
                                    "0123456789" * 4 + "ABCDEFGHIJKLMNOPQRSTUVWXYZ!"]


@cases(("the directory, then a line break", b"PGS ``CWD``\\n|-> ", ["PGS 2:/", "|-> _"]),
       ("quotes around it, and a line break at the end", b'"Q> "\n', ["Q> _"]),
       ("a backslash", b"a\\\\b> ", ["a\\b> _"]),
       ("a name it doesn't know, as written", b"``NOPE``> ", ["``NOPE``> _"]),
       ("the last status", b"``STATUS``> ", ["0> _"]))
def test_the_prompt_comes_from_etc_shell_header_conf(label, text, shown):
    with booted(extra=[("/etc/shell_header.conf", text)]) as c:
        assert c.ready(), f"{label}: {c.rows()}"
        assert lines(c.rows())[1:1 + len(shown)] == shown, f"{label}: {c.rows()}"


def test_the_prompt_shows_colors_and_the_status_of_the_last_program():
    """``STATUS`` in the ink before it; ``CSTATUS`` white for 0, magenta for a
    program's exit value, red for an error: here E_NOTPROG, -20."""
    extra = [("/etc/shell_header.conf", b"``RED``R``RESET````STATUS``:``CSTATUS``> "),
             ("/bin/bad.bin", b"not a program")]
    with booted(extra=extra) as c:
        assert c.ready(), c.rows()
        for line, shown, col, ink in ((None, "R0:0> _", 3, WHITE),
                                      ("cat /docs/nothing", "R1:1> _", 3, MAGENTA),
                                      ("bad", "R-20:-20> _", 5, RED)):
            if line is not None:
                c.command(line)
            rows = c.rows()
            assert last_row(rows) == shown, c.rows()
            row = max(r for r, text in enumerate(rows) if text.strip())
            fb = c.machine.display_io.snapshot()
            assert cell_colors(fb, row, 0) == {RED, BG}, line
            assert cell_colors(fb, row, 1) - {BG} == {INK}, line
            assert cell_colors(fb, row, col) - {BG} == {ink}, line


def test_the_second_line_is_the_first_prompt_and_the_first_line_every_one_after():
    """Each line quoted, with the '\\r' a Windows editor leaves and blank lines
    at the end; the first line starts with a line break."""
    text = b'"\\n``CWD``> "\r\n"start> "\n\n'
    with booted(extra=[("/etc/shell_header.conf", text)]) as c:
        assert c.ready(), c.rows()
        assert c.rows()[:2] == ["PigeonOS", "start> _"], c.rows()
        assert c.command("echo hi") == ["hi"]
        assert c.rows()[1:5] == ["start> echo hi", "hi", "", "2:/> _"], c.rows()
        c.command("cd /docs")
        assert c.rows()[4:7] == ["2:/> cd /docs", "", "2:/docs> _"], c.rows()


@cases(("a line over 255 bytes", b"x" * 300, "has a line over 255 bytes"),
       ("a second line over 255 bytes", b"A> \n" + b"x" * 256, "has a line over 255 bytes"),
       ("three lines", b"A> \nB> \nC> ", "has more than 2 lines"),
       ("over 1024 bytes", b"A> \n" * 300, "is over 1024 bytes"))
def test_a_prompt_file_it_refuses_leaves_the_built_in_prompt(label, text, said):
    with booted(extra=[("/etc/shell_header.conf", text)]) as c:
        assert c.ready(), f"{label}: {c.rows()}"
        assert lines(c.rows())[1:3] == [f"sh: /etc/shell_header.conf {said}",
                                        "2:/> _"], f"{label}: {c.rows()}"


def test_mkdir_cp_mv_rm_and_rmdir_change_the_disk():
    with tempfile.TemporaryDirectory() as t:
        disk = make_disk(Path(t) / "hdd.img", with_commands())
        c = Console(disk)
        try:
            assert c.ready(), c.rows()
            assert c.command("mkdir /docs/new /tmp") == []
            assert c.command("cp /docs/readme.txt /docs/new") == []
            assert c.command("cp /docs/readme.txt /docs/copy.txt") == []
            assert c.command("mv /docs/copy.txt /tmp") == []
            assert c.command("mv /tmp/copy.txt /tmp/moved.txt") == []
            assert c.command("ls /docs/new /tmp") == ["/docs/new:", "readme.txt",
                                                     "/tmp:", "moved.txt"]
            assert c.command("rm /tmp/moved.txt") == []
            assert c.command("rmdir /tmp") == []
        finally:
            c.close()
        with PgfsImage(disk) as img:
            assert img.read_file("/docs/new/readme.txt") == README
            assert not img.exists("/tmp") and not img.exists("/docs/copy.txt")
            assert img.fsck().clean


@cases(("a directory that isn't empty", "rmdir /docs", ["rmdir: /docs: not empty", "rmdir: exit 1"]),
       ("a file that isn't there", "rm /docs/nothing", ["rm: /docs/nothing: not found", "rm: exit 1"]),
       ("copying a directory", "cp /docs /x", ["cp: /docs: is a directory", "cp: exit 1"]),
       ("moving what isn't there", "mv /nothing /docs", ["mv: /nothing: not found", "mv: exit 1"]))
def test_file_commands_say_why_they_refuse(label, line, said):
    with booted(extra=with_commands()) as c:
        assert c.ready(), c.rows()
        assert c.command(line) == said, label


def test_copying_onto_a_read_only_disc_is_refused():
    with tempfile.TemporaryDirectory() as t:
        disk = make_disk(Path(t) / "hdd.img")
        disc = make_disk(Path(t) / "disc.img", with_commands(), label="DISC")
        c = Console(disk, boot_channel=CH_CD, disc=disc)
        try:
            assert c.ready(), c.rows()
            assert c.command("cp /docs/readme.txt /docs/again.txt") == [
                "cp: /docs/again.txt: read-only disk", "cp: exit 1"]
        finally:
            c.close()


def test_clear_clears_the_screen():
    with booted(extra=with_commands()) as c:
        assert c.ready(), c.rows()
        c.command("echo one")
        c.type("clear\n")
        assert c.run_until(lambda rows: rows[0] == "2:/> _" and not any(rows[1:])), c.rows()


def test_ls_sorts_by_name_and_l_shows_sizes():
    extra = [("/docs/zeta.txt", b"z" * 1234), ("/docs/alpha.txt", b""), ("/docs/Mid/x", b"x")]
    with booted(extra=extra) as c:
        assert c.ready(), c.rows()
        assert c.command("ls /docs") == ["Mid/", "alpha.txt", "readme.txt", "zeta.txt"]
        assert c.command("ls -l /docs") == ["   <dir> Mid/", "       0 alpha.txt",
                                            f"{len(README):8} readme.txt", "    1234 zeta.txt"]
        assert c.command("ls -x") == ["ls: unknown option -x", "ls: exit 1"]


# --- Ctrl+C only while a program runs: docs/phase4_plan.md step 10 ----------------

def test_a_break_left_pending_while_the_kernel_was_busy_ends_no_program():
    """A Ctrl+C the machine took while the kernel was busy -- raised here
    straight into the CPU while the shell waits in read() -- is no program's:
    the command typed next runs, and the shell carries on."""
    with booted() as c:
        assert c.ready(), c.rows()
        c.machine.cpu.interrupt(VEC_BREAK)
        assert c.command("echo still running") == ["still running"]
        assert "shell ended, starting it again" not in lines(c.rows()), c.rows()


@cases(("text", b"not a program at all"),
       ("a header with the wrong length", None))
def test_a_file_that_is_not_a_program_is_refused(label, data):
    if data is None:
        data = standin("hello")[:-4]            # one patch offset short
    with booted(extra=[("/bin/junk.bin", data)]) as c:
        assert c.ready(), c.rows()
        assert c.command("junk") == ["junk: not a program"], label


def test_exit_ends_the_shell_and_the_kernel_starts_it_again():
    with booted() as c:
        assert c.ready(), c.rows()
        assert c.command("exit") == ["shell ended, starting it again"]
        assert c.command("echo back") == ["back"]


def test_a_fault_in_the_kernel_is_a_panic_that_stops_the_machine():
    """Nothing is protected: the stand-in overwrites the wrapper getkey() goes
    through, and the fault lands in kernel code, which can't be abandoned."""
    with booted(extra=[("/bin/panic.bin", standin("panic"))]) as c:
        assert c.ready(), c.rows()
        c.type("panic\n")
        assert not c.run_until(lambda rows: False, seconds=20) and c.machine.cpu.halted
        panic = [line for line in lines(c.rows()) if line.startswith("kernel panic:")]
        assert len(panic) == 1 and panic[0].startswith(
            "kernel panic: ran a bad instruction at 0x"), c.rows()


# --- the debug port: docs/phase5b_plan.md step 4 ------------------------------------

def serial(c):
    """The kernel's lines on the debug port."""
    return c.machine.debug.since(0).data.decode().splitlines()


EXEC_LINE = re.compile(r"^\[kernel\] exec (\S+) at 0x([0-9A-F]{8}), depth (\d+)$")


def execs(said):
    """(path, address, depth) for each exec line."""
    return [(m[1], int(m[2], 16), int(m[3])) for m in map(EXEC_LINE.match, said) if m]


def test_the_kernel_logs_starting_mounting_and_the_shell():
    with booted() as c:
        assert c.ready(), c.rows()
        assert serial(c) == ["[kernel] started at 0x00020000", "[kernel] mounted channel 2, TEST",
                             "[kernel] no /etc/boot.conf: starting /bin/sh.bin",
                             "[kernel] exec /bin/sh.bin at 0x01000000, depth 1"], serial(c)


def test_the_kernel_logs_why_it_stops_at_boot():
    with booted(shell=False) as c:
        assert not c.run_until(lambda rows: False, seconds=20) and c.machine.cpu.halted
        assert serial(c)[2:] == ["[kernel] no /etc/boot.conf: starting /bin/sh.bin",
                                 "[kernel] exec /bin/sh.bin: no such file or directory",
                                 "[kernel] cannot start /bin/sh.bin: no such file or directory"], \
            serial(c)
    with tempfile.TemporaryDirectory() as t:
        disk = Path(t) / "blank.img"
        disk.write_bytes(bytes(MiB))
        c = Console(disk)
        try:
            assert not c.run_until(lambda rows: False, seconds=20) and c.machine.cpu.halted
            said = serial(c)
        finally:
            c.close()
    assert said[0] == "[kernel] started at 0x00020000", said
    assert said[1].startswith("[kernel] cannot mount channel 2: ") and len(said) == 2, said


def test_each_exec_is_logged_with_how_it_ended():
    """A return and exit() told apart, a fault two levels down with where it
    happened, Ctrl+C -- and nothing the programs print."""
    extra = [(f"/bin/{name}.bin", standin(name)) for name in ("deep", "div0", "nested", "spin")]
    with booted(extra=extra) as c:
        assert c.ready(), c.rows()
        start = len(serial(c))
        assert c.command("echo hi") == ["hi"]
        c.command("deep")
        c.command("nested")
        c.type("spin\n")
        assert c.run_until(lambda rows: last_row(rows) == "spinning"), c.rows()
        c.press(KEY_LCTRL, ord("c"))
        assert c.ready(), c.rows()
        said = serial(c)[start:]
    started = execs(said)
    assert [(path, depth) for path, _, depth in started] == [
        ("/bin/echo.bin", 2), ("/bin/deep.bin", 2), ("/bin/nested.bin", 2),
        ("/bin/echo.bin", 3), ("/bin/div0.bin", 3), ("/bin/spin.bin", 2)], said
    assert all(address > 0x01000000 for _, address, _ in started), said
    ended = [line for line in said if not EXEC_LINE.match(line)]
    fault = re.match(r"^\[kernel\] /bin/div0\.bin ended: divided by zero at 0x([0-9A-F]{8})$",
                     ended[3])
    div0_at = started[4][1]
    assert fault and div0_at <= int(fault[1], 16) < div0_at + len(standin("div0")), said
    assert ended[:3] + ended[4:] == [
        "[kernel] /bin/echo.bin ended: 0", "[kernel] /bin/deep.bin ended: exit 42",
        "[kernel] /bin/echo.bin ended: 0", "[kernel] /bin/nested.bin ended: 5",
        "[kernel] /bin/spin.bin ended: Ctrl+C"], said
    assert all(line.startswith("[kernel] ") for line in said), said


def test_q_and_ctrl_c_at_more_are_how_the_command_ended():
    with booted(extra=with_more(("/bin/lines.bin", standin("lines")))) as c:
        assert c.ready(), c.rows()
        for keys, how in ((("q",), "q at -- more --"), (ctrl("c"), "Ctrl+C")):
            start = len(serial(c))
            c.type("more lines 30\n")
            assert c.run_until(lambda rows: "line 11" in rows and paused(c)), c.rows()
            if keys == ("q",):
                c.type("q")
            else:
                c.press(*keys)
            assert c.ready(), c.rows()
            said = serial(c)[start:]
            assert [(path, depth) for path, _, depth in execs(said)] == [
                ("/bin/more.bin", 2), ("/bin/lines.bin", 3)], said
            assert said[2] == f"[kernel] /bin/lines.bin ended: {how}", said
            assert said[3].startswith("[kernel] /bin/more.bin ended: ") and len(said) == 4, said


def test_a_panic_is_logged_before_the_machine_stops():
    with booted(extra=[("/bin/panic.bin", standin("panic"))]) as c:
        assert c.ready(), c.rows()
        c.type("panic\n")
        assert not c.run_until(lambda rows: False, seconds=20) and c.machine.cpu.halted
        said = serial(c)
    assert said[-2].startswith("[kernel] exec /bin/panic.bin at "), said
    assert re.match(r"^\[kernel\] panic: ran a bad instruction at 0x[0-9A-F]{8}$", said[-1]), said


def test_a_program_that_cannot_start_is_logged_and_exit_still_restarts_the_shell():
    """k_exec left `started` at 0 after a program failed to start, so a shell
    that exited next was taken for one that never started, and the kernel
    stopped with "cannot start /bin/sh.bin: ok" (docs/phase5b_plan.md §1)."""
    with booted(extra=[("/bin/junk.bin", b"not a program at all\n")]) as c:
        assert c.ready(), c.rows()
        assert c.command("junk") == ["junk: not a program"]
        assert c.command("exit") == ["shell ended, starting it again"]
        assert c.command("echo back") == ["back"]
        said = serial(c)
    assert "[kernel] exec /bin/junk.bin: not a program" in said, said
    i = said.index("[kernel] the shell ended; starting it again")
    assert said[i - 1] == "[kernel] /bin/sh.bin ended: 0", said
    assert said[i + 1] == "[kernel] exec /bin/sh.bin at 0x01000000, depth 1", said


@cases(("booted from the hard disk", CH_HDD, "[kernel] started, 229528 bytes at 0x00020000"),
       ("from channel 1, with a disk's record left behind", CH_USERPROG,
        "[kernel] started at 0x00020000"))
def test_the_kernel_takes_its_size_from_the_record_of_the_disk_it_booted(label, channel, first):
    """bios2 leaves a booted disk's block 0 at BOOT_LOAD_ADDR, boot record
    and all. Booted from channel 1, the record there is an earlier boot's."""
    with tempfile.TemporaryDirectory() as t:
        c = Console(make_disk(Path(t) / "hdd.img"), boot_channel=channel)
        try:
            struct.pack_into("<III", c.machine.ram.mem, BOOT_LOAD_ADDR + BOOT_RECORD,
                             BOOT_SIGNATURE, 7, 229528)
            assert c.ready(), f"{label}: {c.rows()}"
            assert serial(c)[0] == first, f"{label}: {serial(c)}"
        finally:
            c.close()


def first_exec_of_echo(port):
    """Instructions from k_exec's first to its return, for the first echo
    typed, with the debug port on the bus or not."""
    entry = kernel_symbols()["k_exec"]
    with booted() as c:
        if not port:
            del c.machine.io_controller.channels[CH_DEBUG]
        assert c.ready(), c.rows()
        cpu = c.machine.cpu
        c.type("echo hi\n")
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(5_000_000):
                if cpu.pc == entry:
                    break
                c.machine.step()
            assert cpu.pc == entry, "the shell never called exec"
            sp, steps = cpu.sp, 0
            while cpu.sp <= sp and steps < 5_000_000:
                c.machine.step()
                steps += 1
        return steps


def test_an_exec_costs_its_two_lines_and_no_more():
    """Counted, not timed: 118,130 with the port and 107,904 without, so
    10,226 for its two lines (docs/phase5b_plan.md §8)."""
    with_port, without = first_exec_of_echo(True), first_exec_of_echo(False)
    assert with_port - without < 12_000, \
        f"logging cost {with_port - without:,} instructions ({with_port:,} against {without:,})"



# --- exec_out: a program's output into a buffer (docs/pgs_plan.md 4.5) ------------

STANDINS["talker"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) { print("hello from talker\n"); return 0; }
"""

STANDINS["exiter"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) { print("leaving\n"); return 3; }
"""

STANDINS["errtalk"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) {
    print("out\n");
    write(STDERR, "err\n", 4u);
    return 0;
}
"""

STANDINS["relay"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) {
    char *child[2];
    print("relay says hi\n");
    child[0] = "talker";
    child[1] = (char *)0;
    exec("/bin/talker.bin", 1, child);
    return 0;
}
"""

STANDINS["spammer"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) {
    int i;
    for (i = 0; i < 100; i++) print("0123456789\n");
    return 0;
}
"""

# cap SIZE PROGRAM [ARGS...] -- runs it captured and says what it caught, on
# one line, with the newlines in the value shown as '|'. A bare name is
# /bin/<name>.bin, as the shell resolves one, so a test's command line stays
# under the console's 32 columns: one exactly that wide reads as a wrapped
# line and Console.output() cannot find it.
STANDINS["cap"] = r"""#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>
char buf[9000];
char path[264];
int main(int argc, char **argv) {
    char *p;
    int status;
    unsigned size;
    if (argc < 3) { print("usage: cap SIZE PROGRAM\n"); return 1; }
    size = (unsigned)atoi(argv[1]);
    if (strchr(argv[2], '/') == (char *)0) {
        strlcpy(path, "/bin/", sizeof(path));
        strlcat(path, argv[2], sizeof(path));
        strlcat(path, ".bin", sizeof(path));
    } else {
        strlcpy(path, argv[2], sizeof(path));
    }
    status = exec_out(path, argc - 2, argv + 2, buf, size);
    for (p = buf; *p != 0; p++) {
        if (*p == 10) *p = '|';
    }
    printf("got [%s] status %d len %u\n", buf, status, strlen(buf));
    return 0;
}
"""


def catching(*names):
    """A kernel on a disk with cap and the standins named."""
    return booted(extra=[(f"/bin/{name}.bin", standin(name)) for name in ("cap",) + names])


def ran(c, line):
    """Type a line, wait for the prompt, and give the console back as one run
    of text with every row padded to the full width -- as stopped() does, so
    a line that wrapped at a space reads whole."""
    c.command(line)
    return "".join(row.ljust(COLS) for row in c.rows())


def test_exec_out_takes_the_output_instead_of_printing_it():
    with catching("talker") as c:
        assert c.ready(), c.rows()
        text = ran(c, "cap 256 talker")
        assert "got [hello from talker|] status 0 len 18" in text, c.rows()
        assert text.count("hello from talker") == 1, "it was printed as well as captured"


def test_exec_out_still_returns_the_status():
    with catching("exiter", "div0") as c:
        assert c.ready(), c.rows()
        assert "got [leaving|] status 3 len 8" in ran(c, "cap 256 exiter"), c.rows()
        assert "got [] status -100 len 0" in ran(c, "cap 256 div0"), c.rows()


def test_a_grandchilds_output_is_captured_too():
    with catching("relay", "talker") as c:
        assert c.ready(), c.rows()
        text = ran(c, "cap 256 relay")
        assert "got [relay says hi|hello from talker|] status 0 len 32" in text, c.rows()
        assert text.count("relay says hi") == 1, "it was printed as well as captured"


def test_output_past_the_buffer_is_dropped_and_the_length_says_so():
    with catching("spammer") as c:
        assert c.ready(), c.rows()
        text = ran(c, "cap 32 spammer")
        assert "got [0123456789|0123456789|012345678] status 0 len 31" in text, c.rows()


def test_stderr_still_reaches_the_console_while_stdout_is_captured():
    with catching("errtalk") as c:
        assert c.ready(), c.rows()
        assert c.command("cap 256 errtalk") == ["err", "got [out|] status 0 len 4"], c.rows()


def test_a_capture_inside_a_capture_comes_back_to_the_outer_one():
    """The inner cap's own line is captured by the outer one, which is only
    true if exec_out put the outer capture back when the inner ended."""
    with catching("talker") as c:
        assert c.ready(), c.rows()
        text = ran(c, "cap 256 cap 256 talker")
        assert "got [got [hello from talker|] status 0 len 18|] status 0 len 4" in text, c.rows()
        assert text.count("hello from talker") == 1, "a line escaped a capture"


def test_a_capture_is_over_when_the_program_it_covered_ends():
    with catching("talker") as c:
        assert c.ready(), c.rows()
        ran(c, "cap 256 talker")
        assert c.command("echo hi") == ["hi"], c.rows()
        assert "got [hello from talker|] status 0 len 18" in ran(c, "cap 256 talker"), c.rows()


@cases(("no room", "0"), ("only a terminator", "1"))
def test_a_capture_with_no_room_is_refused_and_the_program_never_runs(label, size):
    with catching("talker") as c:
        assert c.ready(), c.rows()
        text = ran(c, f"cap {size} talker")
        assert "got [] status -9 len 0" in text, c.rows()
        assert "hello from talker" not in text, "it ran anyway"


def test_a_program_that_cannot_start_is_reported_as_exec_reports_it():
    with catching("talker") as c:
        assert c.ready(), c.rows()
        assert "got [] status -1 len 0" in ran(c, "cap 256 nope"), c.rows()
        said = serial(c)
    assert "[kernel] exec /bin/nope.bin: no such file or directory" in said, said


# --- boot.conf, the splash and the startup program: docs/phase6_plan.md -----------

STANDINS["splasharg"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) { print("splash got "); if (argc > 1) print(argv[1]); print("\n"); return 0; }
"""

WITH_SPLASH = "splash = /bin/splash.bin\nsplash_ms = 1234\nstartup = /bin/sh.bin\n"


@contextlib.contextmanager
def booted_with(conf, extra=()):
    """The kernel on a test disk with /etc/boot.conf holding `conf`, or none."""
    with tempfile.TemporaryDirectory() as t:
        files = list(extra)
        if conf is not None:
            files.append(("/etc/boot.conf", conf.encode() if isinstance(conf, str) else conf))
        console = Console(make_disk(Path(t) / "hdd.img", files))
        try:
            yield console
        finally:
            console.close()


def stopped(c):
    """Run until the kernel stops: the console as one run of text, every row
    padded to the full width so a message wrapped at a space reads whole,
    and the port's lines."""
    assert not c.run_until(lambda rows: False, seconds=30) and c.machine.cpu.halted, c.rows()
    return "".join(row.ljust(COLS) for row in c.rows()).rstrip(), serial(c)


BANNER = "PigeonOS".ljust(COLS)


def test_boot_conf_runs_the_splash_with_its_length_then_the_banner_then_the_shell():
    with booted_with(WITH_SPLASH, [("/bin/splash.bin", standin("splasharg"))]) as c:
        assert c.ready(), c.rows()
        assert c.rows()[:3] == ["splash got 1234", "PigeonOS", "2:/> _"], c.rows()
        said = serial(c)
    assert said[2] == "[kernel] boot.conf: splash /bin/splash.bin for 1234 ms, startup /bin/sh.bin", said
    assert [(path, depth) for path, _, depth in execs(said)] == [("/bin/splash.bin", 1), ("/bin/sh.bin", 1)]
    assert said[4] == "[kernel] /bin/splash.bin ended: 0", said


@cases(("comments, blank lines, tabs and CRLF",
        "# what boots\r\n\r\n\tsplash = /bin/splash.bin   # first\r\nstartup=/bin/sh.bin\r\n",
        ["splash got 2500", "PigeonOS", "2:/> _"]),
       ("no splash", "startup = /bin/sh.bin\n", ["PigeonOS", "2:/> _"]))
def test_boot_conf_takes_comments_spaces_and_defaults(label, conf, rows):
    with booted_with(conf, [("/bin/splash.bin", standin("splasharg"))]) as c:
        assert c.ready(), f"{label}: {c.rows()}"
        assert c.rows()[:len(rows)] == rows, f"{label}: {c.rows()}"


OVER = "# " + "x" * 1030 + "\nstartup = /bin/sh.bin\n"


@cases(("an unknown key", "splash_mss = 5\nstartup = /bin/sh.bin\n", "/etc/boot.conf:1: unknown key splash_mss"),
       ("a key given twice", "startup = /bin/sh.bin\nstartup = /bin/sh.bin\n", "/etc/boot.conf:2: startup given twice"),
       ("a line with no =", "startup /bin/sh.bin\n", "/etc/boot.conf:1: expected key = value"),
       ("a key with no value", "# first\nsplash =\nstartup = /bin/sh.bin\n", "/etc/boot.conf:2: no value for splash"),
       ("splash_ms not a number", "splash = /bin/splash.bin\nsplash_ms = soon\nstartup = /bin/sh.bin\n",
        "/etc/boot.conf:2: splash_ms must be 1 to 60000, not soon"),
       ("splash_ms with a unit", "splash = /bin/splash.bin\nsplash_ms = 1s\nstartup = /bin/sh.bin\n",
        "/etc/boot.conf:2: splash_ms must be 1 to 60000, not 1s"),
       ("splash_ms 0", "splash = /bin/splash.bin\nsplash_ms = 0\nstartup = /bin/sh.bin\n",
        "/etc/boot.conf:2: splash_ms must be 1 to 60000, not 0"),
       ("splash_ms over a minute", "splash = /bin/splash.bin\nsplash_ms = 60001\nstartup = /bin/sh.bin\n",
        "/etc/boot.conf:2: splash_ms must be 1 to 60000, not 60001"),
       ("splash_ms with no splash", "splash_ms = 100\nstartup = /bin/sh.bin\n", "/etc/boot.conf: splash_ms without splash"),
       ("no startup", "splash = /bin/splash.bin\n", "/etc/boot.conf: no startup"),
       ("over 1,024 bytes", OVER, "/etc/boot.conf: is over 1024 bytes"))
def test_a_mistake_in_boot_conf_stops_boot_and_says_where(label, conf, message):
    with booted_with(conf, [("/bin/splash.bin", standin("splasharg"))]) as c:
        shown, said = stopped(c)
    assert shown == BANNER + message, f"{label}: {shown!r}"
    assert said[-1] == f"[kernel] {message}", f"{label}: {said}"
    assert execs(said) == [], f"{label}: something ran: {said}"


@cases(("missing", None, r"cannot start /bin/splash\.bin: no such file or directory"),
       ("not a program", b"not a program at all\n", r"cannot start /bin/splash\.bin: not a program"),
       ("faulting", "div0", r"/bin/splash\.bin: divided by zero at 0x[0-9a-fA-F]+"))
def test_a_splash_that_cannot_start_or_faults_stops_boot(label, splash, message):
    extra = []
    if splash is not None:
        extra = [("/bin/splash.bin", standin(splash) if isinstance(splash, str) else splash)]
    with booted_with("splash = /bin/splash.bin\nstartup = /bin/sh.bin\n", extra) as c:
        shown, said = stopped(c)
    assert re.fullmatch(re.escape(BANNER) + message, shown), f"{label}: {shown!r}"
    assert "/bin/sh.bin" not in [path for path, _, _ in execs(said)], f"{label}: the shell started"


def test_ctrl_c_ends_the_splash_and_boot_carries_on():
    with booted_with("splash = /bin/spin.bin\nstartup = /bin/sh.bin\n", [("/bin/spin.bin", standin("spin"))]) as c:
        assert c.run_until(lambda rows: rows[0] == "spinning"), c.rows()
        c.press(KEY_LCTRL, ord("c"))
        assert c.ready(), c.rows()
        assert c.rows()[:4] == ["spinning", "^C", "PigeonOS", "2:/> _"], c.rows()
        assert "[kernel] /bin/spin.bin ended: Ctrl+C" in serial(c)


def test_a_startup_program_other_than_the_shell_is_started_again_when_it_ends():
    with booted_with("startup = /bin/hello.bin\n", [("/bin/hello.bin", standin("hello"))]) as c:
        assert c.run_until(lambda rows: lines(rows).count("hello ended, starting it again") >= 2), c.rows()
        assert lines(c.rows())[:4] == ["PigeonOS", "hello from hello", "hello ended, starting it again",
                                       "hello from hello"], c.rows()
        said = serial(c)
    assert [path for path, _, _ in execs(said)][:2] == ["/bin/hello.bin", "/bin/hello.bin"], said
    assert "[kernel] /bin/hello.bin ended; starting it again" in said


@cases(("missing", "startup = /bin/nothing.bin\n", "cannot start /bin/nothing.bin: no such file or directory"),
       ("not a program", "startup = /docs/readme.txt\n", "cannot start /docs/readme.txt: not a program"))
def test_a_startup_program_that_cannot_start_stops_boot(label, conf, message):
    with booted_with(conf) as c:
        shown, said = stopped(c)
    assert shown == BANNER + message, f"{label}: {shown!r}"
    assert said[-1] == f"[kernel] {message}", f"{label}: {said}"


PIGEON_BMP = REPO_ROOT / "user" / "os" / "etc" / "bmp" / "pigeon.bmp"
EYE_MASK_BMP = REPO_ROOT / "user" / "os" / "etc" / "bmp" / "eye-mask.bmp"
YELLOW = 0xFFFFFF00


def splash_files(ms, image=True, eyes=True):
    """The real splash, /etc/bmp/pigeon.bmp and eye-mask.bmp unless left out,
    and a boot.conf showing it for `ms` -- or, for None, one that doesn't name
    it at all, so it can be run from the prompt with no argument."""
    extra = [("/bin/splash.bin", shell_program("splash"))]
    if image:
        extra.append(("/etc/bmp/pigeon.bmp", PIGEON_BMP.read_bytes()))
    if eyes:
        extra.append(("/etc/bmp/eye-mask.bmp", EYE_MASK_BMP.read_bytes()))
    if ms is None:
        return "startup = /bin/sh.bin\n", extra
    return f"splash = /bin/splash.bin\nsplash_ms = {ms}\nstartup = /bin/sh.bin\n", extra


def watch_splash(c, key_at=None, every=False):
    """As the splash waits: how often it looks at its timer, and what the
    screen was at the first look -- or, with `every`, at each look. With
    `key_at`, a key goes down at that look."""
    seen = {"looks": 0, "screen": None, "screens": []}
    channel = c.machine.io_controller.channels[CH_TIMER]
    real = channel.callback

    def spy(read_write, command, length, address, data):
        if command == 5 and address == 1:
            seen["looks"] += 1
            if seen["screen"] is None:
                seen["screen"] = c.machine.display_io.snapshot()
            if every:
                seen["screens"].append(c.machine.display_io.snapshot())
            if seen["looks"] == key_at:
                c.machine.hid.push_key(ord("x"), True)
                c.machine.hid.push_key(ord("x"), False)
        return real(read_write, command, length, address, data)
    channel.callback = spy
    return seen


def pigeon_screen():
    """The screen's bytes with pigeon.bmp stretched onto it, decoded in Python."""
    from test_bmp import STRETCH, reference
    pixels = reference(PIGEON_BMP.read_bytes(), DISPLAY_W, DISPLAY_H, STRETCH)
    return struct.pack(f"<{DISPLAY_W * DISPLAY_H}I", *pixels)


def test_the_splash_draws_pigeon_bmp_and_holds_it_for_its_length():
    conf, extra = splash_files(2500)
    with booted_with(conf, extra) as c:
        seen = watch_splash(c)
        assert c.ready(), c.rows()
        assert c.rows()[:2] == ["PigeonOS", "2:/> _"], c.rows()
        said = serial(c)
    assert seen["screen"] == pigeon_screen(), "the screen wasn't pigeon.bmp while the splash waited"
    assert 40 <= seen["looks"] <= 60, f"{seen['looks']} looks at the timer for 2,500 ms on the stepping clock"
    assert "[kernel] /bin/splash.bin ended: 0" in said, said


def blend(base, t):
    """The splash's blend of a pixel toward yellow by t/256, in Python."""
    channels = [((base >> s & 255) * (256 - t) + (YELLOW >> s & 255) * t) >> 8 for s in (16, 8, 0)]
    return 0xFF000000 | channels[0] << 16 | channels[1] << 8 | channels[2]


def eye_amounts(screens):
    """How far the eyes are blended toward yellow, 0 to 256, in each screen
    that has them drawn: outside eye-mask.bmp's white the screen must be the
    pigeon, and every eye pixel its own colour blended by the one amount."""
    from test_bmp import STRETCH, reference
    size = DISPLAY_W * DISPLAY_H
    pigeon = reference(PIGEON_BMP.read_bytes(), DISPLAY_W, DISPLAY_H, STRETCH)
    mask = reference(EYE_MASK_BMP.read_bytes(), DISPLAY_W, DISPLAY_H, STRETCH)
    eyes = [i for i in range(size) if (mask[i] >> 8 & 255) >= 128]
    assert len(eyes) == 28, len(eyes)
    others = [i for i in range(size) if (mask[i] >> 8 & 255) < 128]
    amounts = []
    for screen in screens:
        words = struct.unpack(f"<{size}I", screen)
        assert all(words[i] == pigeon[i] for i in others), "a pixel outside the eyes changed"
        if all(words[i] == pigeon[i] for i in eyes):
            continue                             # before the first flash is drawn
        found = [t for t in range(257) if all(words[i] == blend(pigeon[i], t) for i in eyes)]
        assert found, "the eyes aren't one blend of their own colours and yellow"
        amounts.append(found[0])
    return amounts


def flash_turns(amounts):
    """How often the eyes turned from brightening to dimming, or back."""
    steps = [b - a for a, b in zip(amounts, amounts[1:]) if b != a]
    return sum(1 for a, b in zip(steps, steps[1:]) if (a > 0) != (b > 0))


def test_the_eyes_flash_yellow_smoothly_and_nothing_else_moves():
    """At every look the eyes are one blend of their own colours and yellow,
    which over 2.5 s comes near both ends, and nothing else on the screen
    moves."""
    conf, extra = splash_files(2500)
    with booted_with(conf, extra) as c:
        seen = watch_splash(c, every=True)
        assert c.ready(), c.rows()
    amounts = eye_amounts(seen["screens"])
    assert len(set(amounts)) >= 10, f"only {sorted(set(amounts))}: not a smooth flash"
    assert max(amounts) >= 230 and min(amounts) <= 26, f"from {min(amounts)} to {max(amounts)} of 256"
    turns = flash_turns(amounts)
    assert turns >= 3, f"the brightness turned {turns} times in 2.5 s: not a flash a second ({amounts})"


def test_without_the_mask_the_pigeon_still_shows_with_its_eyes_still():
    conf, extra = splash_files(2500, eyes=False)
    with booted_with(conf, extra) as c:
        seen = watch_splash(c, every=True)
        assert c.ready(), c.rows()
        screen = "".join(row.ljust(COLS) for row in c.rows())
        said = serial(c)
    assert all(s == pigeon_screen() for s in seen["screens"]), "the screen wasn't the still pigeon"
    assert screen.startswith("splash: /etc/bmp/eye-mask.bmp: not found".ljust(2 * COLS) + BANNER + "2:/> _"), \
        repr(screen)
    assert "[kernel] /bin/splash.bin ended: 0" in said, said


def test_the_splash_holds_the_image_for_the_milliseconds_boot_conf_gives():
    """A minute: 1,200 looks at 50 ms each on the stepping clock."""
    conf, extra = splash_files(60000)
    with booted_with(conf, extra) as c:
        seen = watch_splash(c)
        assert c.ready(), c.rows()
    assert 1100 <= seen["looks"] <= 1300, f"{seen['looks']} looks: not the 60,000 ms boot.conf gave"


def test_a_key_ends_the_splash_early_and_never_reaches_the_shell():
    conf, extra = splash_files(60000)
    with booted_with(conf, extra) as c:
        seen = watch_splash(c, key_at=1)
        assert c.ready(), c.rows()
        assert c.rows()[:2] == ["PigeonOS", "2:/> _"], "the key reached the shell: " + repr(c.rows())
    assert seen["screen"] == pigeon_screen()
    assert seen["looks"] <= 3, f"{seen['looks']} looks at the timer after the key"


def test_with_no_argument_the_splash_has_no_countdown_and_waits_for_a_key():
    """Run from the prompt with no length: it holds the pigeon well past the
    longest countdown there is, 60,000 ms, and only a key ends it -- which the
    kernel then keeps from the shell, as it does the key that cuts boot's
    splash short."""
    conf, extra = splash_files(None)
    with booted_with(conf, extra) as c:
        assert c.ready(), c.rows()
        seen = watch_splash(c, key_at=1400)      # about 70 s on the stepping clock
        assert c.command("splash") == [], c.rows()
        assert last_row(c.rows()) == "2:/> _", "the key reached the shell: " + repr(c.rows())
        said = serial(c)
    assert seen["screen"] == pigeon_screen(), "the screen wasn't pigeon.bmp while the splash waited"
    assert 1400 <= seen["looks"] <= 1410, f"{seen['looks']} looks: it counted down after all"
    assert "[kernel] /bin/splash.bin ended: 0" in said, said


def test_with_no_countdown_the_eyes_go_on_flashing():
    """Without a length the timer is started again for each flash, so the eyes
    must keep brightening and dimming: about three flashes over the 3 s here."""
    conf, extra = splash_files(None)
    with booted_with(conf, extra) as c:
        assert c.ready(), c.rows()
        seen = watch_splash(c, key_at=60, every=True)
        assert c.command("splash") == [], c.rows()
    amounts = eye_amounts(seen["screens"])
    assert max(amounts) >= 230 and min(amounts) <= 26, f"from {min(amounts)} to {max(amounts)} of 256"
    turns = flash_turns(amounts)
    assert turns >= 3, f"the brightness turned {turns} times in 3 s: the flashes stopped ({amounts})"


def test_a_splash_image_that_will_not_load_is_reported_and_boot_carries_on():
    conf, extra = splash_files(2500, image=False)
    with booted_with(conf, extra) as c:
        assert c.ready(), c.rows()
        screen = "".join(row.ljust(COLS) for row in c.rows())
        said = serial(c)
    assert screen.startswith("splash: /etc/bmp/pigeon.bmp: not found".ljust(2 * COLS) + BANNER + "2:/> _"), \
        repr(screen)
    assert "[kernel] /bin/splash.bin ended: 1" in said, said


def test_the_splash_loads_and_draws_in_a_few_million_instructions_and_waits_cheaply():
    """Counted, not timed: from exec until the first look at the timer is
    loading the program, the pigeon and the eye mask and drawing, 3,387,860;
    each look after that, with the eyes drawn again, is 6,757."""
    entry = kernel_symbols()["k_exec"]
    conf, extra = splash_files(2500)
    with booted_with(conf, extra) as c:
        done, looks = [0], []
        channel = c.machine.io_controller.channels[CH_TIMER]
        real = channel.callback

        def spy(read_write, command, length, address, data):
            if command == 5 and address == 1:
                looks.append(done[0])
            return real(read_write, command, length, address, data)
        channel.callback = spy
        cpu = c.machine.cpu
        with contextlib.redirect_stdout(io.StringIO()):
            while cpu.pc != entry and done[0] < 10_000_000:
                c.machine.step()
                done[0] += 1
            start = done[0]
            while b"splash.bin ended" not in c.machine.debug.since(0).data and done[0] < 20_000_000:
                for _ in range(10_000):
                    c.machine.step()
                    done[0] += 1
    to_draw = looks[0] - start
    per_look = (looks[-1] - looks[0]) / (len(looks) - 1)
    assert to_draw < 4_400_000, f"{to_draw:,} instructions to load and draw"
    assert per_look < 9_000, f"{per_look:,.0f} instructions a look"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the kernel"))
