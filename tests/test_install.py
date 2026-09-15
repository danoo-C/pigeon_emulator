"""The installer: user/os/installer.c puts the disc on the hard disk.

docs/os_cd.md, section 8. The example project's disc is built, put in the
drive, and booted on a Machine with a blank hard disk. The installer asks,
formats the disk, copies the disc onto it, makes the disk boot /boot.bin --
the kernel -- and restarts. Then the hard disk boots, the kernel starts
the shell, and the shell runs the graphing calculator, which draws its
curve and comes back on Esc.

Everything is driven the way a person would: keys pushed through HID, the
screen read back as text.

    python3 tests/test_install.py      (or: python3 -m pytest tests/)
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

from _runner import run_module                                        # noqa: E402
from compiler.project import build_disc, read_project                 # noqa: E402
from emulator.memory_map import BOOT_BLOCK, BOOT_CODE, PROGRAM_LOAD_ADDR  # noqa: E402
from pfs import PgfsImage                                             # noqa: E402
from test_bios2 import ENTER, ESC, power_on                           # noqa: E402
from test_graph import curve_pixels                                   # noqa: E402
from test_kernel import BG, BLUE, GREEN, INK, WHITE, Console, cell_colors, last_row  # noqa: E402
from test_project import EXAMPLE, quiet                               # noqa: E402

MiB = 1 << 20
PROMPT = "ENTER install   ESC cancel"
INSTALLED = ["/boot.bin", "/pigeon.txt", "/docs/readme.txt", "/etc/shell_header.conf"] + [
    f"/bin/{name}.bin" for name in ("sh", "ls", "cat", "echo", "mkdir", "rmdir", "rm", "mv",
                                    "cp", "clear", "more", "edit", "graph", "cube", "files", "corrupter")]

# The example disc, built once for the whole file.
_BUILD = tempfile.TemporaryDirectory()
DISC = build_disc(read_project(EXAMPLE), Path(_BUILD.name) / "pigeonos.img",
                  Path(_BUILD.name) / "build", quiet)


def serial(p, prefix=""):
    """The lines on the debug port that start with `prefix`."""
    return [line for line in p.machine.debug.since(0).data.decode().splitlines()
            if line.startswith(prefix)]


def keys_row(text):
    return lambda rows: rows[11] == text


def run_to(machine, address, steps):
    """Step until PC reaches `address`; True if it did."""
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(steps):
            if machine.cpu.pc == address:
                return True
            if machine.step() == 1:
                return False
    return machine.cpu.pc == address


def test_the_installer_puts_the_disc_on_the_hard_disk_and_the_disk_boots_the_shell():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        disk = t / "hdd.img"
        disk.write_bytes(bytes(4 * MiB))              # blank, as the emulator makes one
        with PgfsImage(DISC) as src:
            on_disc = {path: src.read_file(path) for path in INSTALLED}

        with power_on(t, disk=disk, disc=DISC, keys=[ENTER]) as p:
            # bios2 boots the CD, since the blank disk can't boot, into the installer
            assert p.run_until(keys_row(PROMPT), steps=20_000_000), p.rows()
            p.key(ENTER)
            assert p.run_until(keys_row("ENTER restart"), steps=80_000_000, every=50_000), \
                p.rows()
            rows = p.rows()
            assert rows[9] == f"Installed {len(INSTALLED)} files.", rows
            assert rows[10] == "The hard disk boots /boot.bin.", rows

            # the hard disk, from the host, while the installer waits
            with PgfsImage(disk) as img:
                assert img.label == "PIGEONOS"
                assert not img.exists("/install.bin"), "the installer copied itself"
                for path, data in on_disc.items():
                    assert img.read_file(path) == data, f"{path} differs from the disc"
                boot = img.stat("/boot.bin")
                assert img.boot_record() == (boot.first, boot.size), img.boot_record()
                assert img.fsck().clean
            assert disk.read_bytes()[BOOT_CODE:BOOT_BLOCK] == \
                DISC.read_bytes()[BOOT_CODE:BOOT_BLOCK], "not the disc's boot sector"

            # what it said on the debug port (docs/phase5b_plan.md step 3)
            said = serial(p, "[installer]")
            assert said[:5] == [
                "[installer] disc on channel 6: PigeonOS 0.1",
                "[installer] hard disk: 4096 K",
                f"[installer] {len(INSTALLED)} files to copy; /boot.bin will boot",
                "[installer] Enter: installing",
                "[installer] formatting the hard disk as PIGEONOS",
            ], said
            copied = [line.removeprefix("[installer] copying ") for line in said[5:-2]]
            assert copied[0] == "/boot.bin" and sorted(copied) == sorted(INSTALLED), said
            assert said[-2:] == [
                f"[installer] boot record: /boot.bin, block {boot.first}, {boot.size} bytes",
                f"[installer] installed {len(INSTALLED)} files; Enter restarts",
            ], said

            # restart: bios2 again, and the hard disk now comes first
            p.key(ENTER)
            assert p.run_until(lambda rows: rows[4] == "  Hard disk  PIGEONOS"
                               and rows[7].startswith("Booting Hard disk"),
                               steps=20_000_000), p.rows()
            p.key(ENTER)
            # Compared at the hand-over, before it runs: a C program's globals
            # live in its image, so running changes the bytes.
            assert run_to(p.machine, PROGRAM_LOAD_ADDR, steps=6_000_000), \
                f"the hard disk never handed over: {p.rows()}"
            kernel = on_disc["/boot.bin"]
            mem = p.machine.ram.mem
            assert bytes(mem[PROGRAM_LOAD_ADDR:PROGRAM_LOAD_ADDR + len(kernel)]) == \
                kernel, "the hard disk did not load the kernel"

            # the kernel starts the shell; the shell runs the calculator
            # with the disc's prompt, /etc/shell_header.conf: its second line
            # first, then its first, which starts with a blank line
            shell = Console.on(p.machine)
            assert shell.ready(), shell.rows()
            everything = serial(p)
            assert everything.count("[bios] bios2") == 2 and "[installer] restarting" in everything
            assert serial(p, "[kernel]")[:3] == [
                f"[kernel] started, {len(kernel)} bytes at 0x00020000",
                "[kernel] mounted channel 2, PIGEONOS",
                "[kernel] exec /bin/sh.bin at 0x01000000, depth 1",
            ], everything
            assert shell.rows()[:3] == ["PigeonOS", "|-(PGS)-[2:/]-(0)", "|-> _"], shell.rows()
            fb = p.machine.display_io.snapshot()
            for col, ink in ((0, GREEN), (3, BLUE), (9, INK), (15, WHITE)):    # ``CSTATUS``: 0 in white
                assert cell_colors(fb, 1, col) - {BG} == {ink}, (col, cell_colors(fb, 1, col))
            shell.type("graph\n")
            assert shell.run_until(
                lambda rows: curve_pixels(p.machine.display_io.snapshot()) > 200), \
                "the calculator did not draw its curve"
            shell.press(ESC)
            assert shell.run_until(lambda rows: last_row(rows) == "|-> _"), shell.rows()
            assert shell.rows()[1:6] == ["|-(PGS)-[2:/]-(0)", "|-> graph", "",
                                         "|-(PGS)-[2:/]-(0)", "|-> _"], shell.rows()


def test_esc_cancels_and_leaves_the_hard_disk_as_it_was():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        disk = t / "hdd.img"
        PgfsImage.mkfs(disk, 4 * MiB, label="KEEP").close()
        with PgfsImage(disk) as img:
            img.write_file("/precious.txt", b"not to be erased")
        before = disk.read_bytes()
        with power_on(t, disk=disk, disc=DISC, keys=[ENTER]) as p:
            assert p.run_until(keys_row(PROMPT), steps=20_000_000), p.rows()
            p.key(ESC)
            assert p.run(steps=3_000_000), "the installer did not stop"
            assert p.rows()[9] == "Cancelled: nothing was changed.", p.rows()
            said = serial(p, "[installer]")
            assert said[-1] == "[installer] Esc: cancelled, nothing written", said
            assert not any("formatting" in line or "copying" in line for line in said), said
        assert disk.read_bytes() == before, "cancelling changed the hard disk"


def test_a_hard_disk_too_small_is_reported():
    """256 K holds the calculator but not everything else on the disc."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        disk = t / "hdd.img"
        disk.write_bytes(bytes(256 * 1024))
        with power_on(t, disk=disk, disc=DISC, keys=[ENTER]) as p:
            assert p.run_until(keys_row(PROMPT), steps=20_000_000), p.rows()
            assert p.rows()[3] == "Hard disk: 256 K", p.rows()
            p.key(ENTER)
            assert p.run(steps=80_000_000), f"the installer did not stop: {p.rows()}"
            assert p.rows()[9] == "The install failed:", p.rows()
            said = serial(p, "[installer]")
            assert said[-1] == f"[installer] failed: {p.rows()[10]}", said
            assert said[-2].endswith(f": {p.rows()[10]}") and said[-2].split()[1].startswith("/"), \
                "the file that didn't fit is not named"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the installer"))
