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
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                   # noqa: E402
from assembler.assembler import assemble_file                           # noqa: E402
from emulator.devices.keycodes import KEY_LCTRL                         # noqa: E402
from emulator.machine import Machine                                    # noqa: E402
from emulator.memory_map import (                                       # noqa: E402
    BOOT_CHANNEL, CH_CD, CH_HDD, CH_USERPROG, DISPLAY_START, PROGRAM_LOAD_ADDR)
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
            assert c.command("ls /docs") == ["readme.txt", "own.txt"]
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


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the kernel"))
