"""The installer: user/os/installer.c puts the disc on the hard disk.

docs/os_cd.md, section 8. The example project's disc is built, put in the
drive, and booted on a Machine with a blank hard disk. The installer asks,
formats the disk, copies the disc onto it, makes the disk boot /boot.bin --
the graphing calculator -- and restarts. Then the hard disk boots, and the
calculator draws its curve.

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
from test_project import EXAMPLE, quiet                               # noqa: E402

MiB = 1 << 20
PROMPT = "ENTER install   ESC cancel"
INSTALLED = [
    "/boot.bin", "/pigeon.txt", "/bin/files.bin", "/bin/cube.bin", "/docs/readme.txt"]

# The example disc, built once for the whole file.
_BUILD = tempfile.TemporaryDirectory()
DISC = build_disc(read_project(EXAMPLE), Path(_BUILD.name) / "pigeonos.img",
                  Path(_BUILD.name) / "build", quiet)


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


def test_the_installer_puts_the_disc_on_the_hard_disk_and_the_disk_boots_the_calculator():
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
            assert rows[9] == "Installed 5 files.", rows
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
            calculator = on_disc["/boot.bin"]
            mem = p.machine.ram.mem
            assert bytes(mem[PROGRAM_LOAD_ADDR:PROGRAM_LOAD_ADDR + len(calculator)]) == \
                calculator, "the hard disk did not load the calculator"
            p.run(steps=12_000_000)
            assert curve_pixels(p.machine.display_io.snapshot()) > 200, \
                "the calculator did not draw its curve"


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


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the installer"))
