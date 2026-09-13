"""The boot sector, firmware/boot.asm, and making a disk boot with tools/pfs.py.

Phase 4 of docs/os_cd.md. bios2 copies a bootable disk's block 0 to
BOOT_LOAD_ADDR and calls the boot sector in it. The boot sector reads the
boot record beside it, loads the contiguous file it names to
PROGRAM_LOAD_ADDR through the IO window, and jumps there. When it cannot,
it returns to bios2, which says so. PgfsImage.make_bootable writes the
record and the sector, once it has checked the file can be booted.

Every boot here goes the whole way -- stage 1, bios2, the boot sector,
the program -- from the hard disk and from the CD.

    python3 tests/test_boot.py      (or: python3 -m pytest tests/)
"""
import struct
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    BOOT_BLOCK, BOOT_CODE, BOOT_RECORD, BOOT_SIGNATURE, CH_CD, CH_HDD, PROGRAM_LOAD_ADDR,
    PROGRAM_MAX_SIZE)
from pfs import BLOCK, PgfsError, PgfsImage, boot_sector              # noqa: E402
from test_bios2 import (                                              # noqa: E402
    ENTER, PROGRAM, R_STATUS, R_TITLE, image, power_on, status_is)
from test_fs import FS, expect, run_fs                                # noqa: E402
from test_fs import program as fs_program                             # noqa: E402
from test_pfs import cli                                              # noqa: E402

MiB = 1 << 20
SECTOR = boot_sector()


def bootable(path, program, size=4 * MiB, label="PIGEONOS", before=None):
    """A fresh image holding `program` as /boot.bin, made to boot it.
    before(img) writes whatever should come first on the disk."""
    PgfsImage.mkfs(path, size, label=label).close()
    with PgfsImage(path) as img:
        if before is not None:
            before(img)
        img.write_file("/boot.bin", program)
        img.make_bootable("/boot.bin", SECTOR)
    return path


def raises(code, fn, *args):
    try:
        fn(*args)
    except PgfsError as e:
        assert e.code == code, f"expected {code}, got {e.code}: {e}"
        return
    raise AssertionError(f"expected {code}, but nothing was raised")


def test_the_boot_sector_fits_in_block_0():
    room = BOOT_BLOCK - BOOT_CODE
    assert len(SECTOR) <= room, f"the boot sector is {len(SECTOR)} bytes; there are {room}"


# --- booting all the way ---------------------------------------------------------

@cases(("hard disk, under one window", CH_HDD, 100),
       ("hard disk, just over the window", CH_HDD, 512),               # 4,112 bytes
       ("hard disk, an exact multiple of the window", CH_HDD, 1022),   # 8,192 bytes
       ("hard disk, exactly PROGRAM_MAX_SIZE", CH_HDD, PROGRAM_MAX_SIZE // 8 - 2),
       ("CD, just over the window", CH_CD, 512),
       ("CD, an exact multiple of the window", CH_CD, 1022),
       ("CD, 100 KB", CH_CD, 12_500))
def test_a_disk_boots_its_program_the_whole_way(label, channel, nops):
    """The program arrives whole, runs, and nothing is written past its
    end: each chunk copies only what is left of the file."""
    program = image(nops, PROGRAM)
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        disk = bootable(t / "boot.img", program)
        where = {"disk": disk} if channel == CH_HDD else {"disc": disk}
        with power_on(t, keys=[ENTER], **where) as p:
            assert p.run(steps=8_000_000), f"{label}: never booted: {p.rows()}"
            assert p.a == PROGRAM, f"{label}: A={p.a:#x}, want {PROGRAM:#x}"
            mem = p.machine.ram.mem
            end = PROGRAM_LOAD_ADDR + len(program)
            assert bytes(mem[PROGRAM_LOAD_ADDR:end]) == program, f"{label}: not whole"
            if len(program) < PROGRAM_MAX_SIZE:        # past that is bios2's frame stack
                assert bytes(mem[end:end + 64]) == bytes(64), f"{label}: wrote past the end"


def test_a_file_further_into_the_disk_boots_too():
    """The boot sector reads the first block from the record, not a number
    it assumes: here twenty blocks of another file come first."""
    program = image(600, PROGRAM)
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        disk = bootable(t / "boot.img", program,
                        before=lambda img: img.write_file("/first.bin", bytes(20 * BLOCK)))
        with PgfsImage(disk) as img:
            first, size = img.boot_record()
        assert first > 20 and size == len(program), (first, size)
        with power_on(t, disk=disk, keys=[ENTER]) as p:
            assert p.run(steps=4_000_000) and p.a == PROGRAM, p.rows()


@cases(("a size of 0", 8, 0),
       ("a size over PROGRAM_MAX_SIZE", 8, PROGRAM_MAX_SIZE + 1),
       ("a file past the end of the disk", 4, 0x7FFFF))
def test_a_boot_record_the_sector_cannot_load_comes_back_to_the_menu(label, offset, value):
    """The boot sector returns to bios2 rather than jumping into what it
    could not load, and bios2 draws its screen again and says why."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        disk = bootable(t / "boot.img", image(100, PROGRAM))
        raw = bytearray(disk.read_bytes())
        struct.pack_into("<I", raw, BOOT_RECORD + offset, value)
        disk.write_bytes(raw)
        with power_on(t, disk=disk, keys=[ENTER]) as p:
            assert p.run_until(status_is("Hard disk: boot failed")), f"{label}: {p.rows()}"
            assert p.rows()[R_TITLE] == "PIGEON BIOS", f"{label}: the frame was not redrawn"
            assert not p.run(300_000), f"{label}: halted"


# --- making a disk bootable --------------------------------------------------------

def fragmented(img):
    """/c.bin in two pieces: the allocator searches on from where it last
    stopped, runs off the end of this 32-block image, and wraps into the
    hole /a.bin left."""
    img.write_file("/a.bin", bytes(10 * BLOCK))
    img.write_file("/b.bin", bytes(10 * BLOCK))
    img.remove("/a.bin")
    img.write_file("/c.bin", bytes(15 * BLOCK))
    return "/c.bin"


@cases(("a directory", lambda img: img.mkdir("/d") or "/d", SECTOR, "EISDIR"),
       ("a file that is not there", lambda img: "/nope.bin", SECTOR, "ENOENT"),
       ("an empty file", lambda img: img.write_file("/e.bin", b"") or "/e.bin", SECTOR, "EINVAL"),
       ("a file in pieces", fragmented, SECTOR, "EINVAL"),
       ("a boot sector too big", lambda img: img.write_file("/f.bin", b"x") or "/f.bin",
        bytes(BOOT_BLOCK - BOOT_CODE + 1), "EINVAL"))
def test_make_bootable_refuses_what_cannot_boot(label, prepare, sector, code):
    """And writes nothing when it refuses."""
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "disk.img"
        PgfsImage.mkfs(path, 32 * BLOCK).close()
        with PgfsImage(path) as img:
            target = prepare(img)
            raises(code, img.make_bootable, target, sector)
            assert img.boot_record() is None, f"{label}: a record was written anyway"


def test_a_file_over_program_max_size_is_refused():
    with tempfile.TemporaryDirectory() as t:
        with PgfsImage.mkfs(Path(t) / "disk.img", 4 * MiB) as img:
            img.write_file("/big.bin", bytes(PROGRAM_MAX_SIZE + 1))
            raises("EINVAL", img.make_bootable, "/big.bin", SECTOR)


def boot_bytes(path):
    raw = path.read_bytes()[:BOOT_BLOCK]
    return raw[BOOT_RECORD:BOOT_RECORD + 12], raw[BOOT_CODE:]


def test_the_boot_record_survives_later_writes_from_pfs():
    """pfs.py used to rebuild block 0 from zeros on every write that
    touched the superblock, which erased the record and the sector."""
    with tempfile.TemporaryDirectory() as t:
        disk = bootable(Path(t) / "boot.img", image(100, PROGRAM))
        before = boot_bytes(disk)
        with PgfsImage(disk) as img:
            img.mkdir("/docs")
            img.write_file("/docs/a.txt", b"hello")
            img.rename("/docs/a.txt", "/docs/b.txt")
            img.remove("/docs/b.txt")
            assert img.fsck().clean
        assert boot_bytes(disk) == before, "a pfs.py write erased the boot record or sector"


def test_the_boot_record_survives_writes_from_the_guest():
    """fs.c updates block 0 in place, so the guest keeps the record too."""
    with tempfile.TemporaryDirectory() as t:
        disk = bootable(Path(t) / "boot.img", image(100, PROGRAM))
        before = boot_bytes(disk)
        expect(run_fs(fs_program("""
            TRY(fs_mount(CH_HDD));
            TRY(fs_mkdir("/saves"));
            TRY(fs_save("/saves/score", "12345", 5u));
            TRY(fs_remove("/saves/score"));
            return 0;"""), disk), 0)
        assert boot_bytes(disk) == before, "a guest write erased the boot record or sector"


def test_pfs_boot_on_the_command_line():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        disk = t / "disk.img"
        (t / "demo.bin").write_bytes(image(100, PROGRAM))
        assert cli("mkfs", "--image", disk, "--label", "DEMO")[0] == 0
        assert cli("put", "--image", disk, t / "demo.bin", "/demo.bin")[0] == 0
        code, out, _ = cli("boot", "--image", disk, "/demo.bin")
        assert code == 0 and b"boots /demo.bin" in out, out
        code, out, _ = cli("info", "--image", disk)
        assert code == 0 and b"boot " in out, out
        with PgfsImage(disk) as img:
            first, size = img.boot_record()
            assert size == 816 and img.stat("/demo.bin").first == first
        assert disk.read_bytes()[BOOT_CODE:BOOT_CODE + len(SECTOR)] == SECTOR
        code, _, err = cli("boot", "--image", disk, "/nope.bin")
        assert code == 1 and "no such file" in err, err
        assert struct.unpack_from("<I", disk.read_bytes(), BOOT_RECORD)[0] == BOOT_SIGNATURE


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the boot sector"))
