"""PigeonFS on the guest: lib/pigeon/fs.c, running on the emulator.

Every test compiles a C program together with the library and runs it
on a real Machine whose channel 2 (and sometimes channel 1) is a
temporary disk image. Then it looks at that image from the host with
tools/pfs.py. After every test the image must pass fsck.

The strongest checks compare the guest with pfs.py directly: the same
operations, done once by each, must leave byte-identical images. That
pins everything the two could disagree on -- which block the allocator
picks, which slot an entry takes, what fills the tail of a last block.

    python3 tests/test_fs.py      (or: python3 -m pytest tests/)
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

from _runner import cases, run_module                           # noqa: E402
from emulator.machine import Machine                            # noqa: E402
from emulator.memory_map import CH_HDD, PROGRAM_LOAD_ADDR, STACK_TOP  # noqa: E402
from pfs import BLOCK, PgfsImage                                # noqa: E402
from test_libs import build                                     # noqa: E402

BIOS = REPO_ROOT / "build" / "bios.bin"
MiB = 1 << 20

# The error codes, read from the header rather than retyped.
FS = {name: int(value) for name, value in re.findall(
    r"#define (FS_E\w+)\s+\((-\d+)\)", (REPO_ROOT / "lib/pigeon/fs.h").read_text())}
FS["FS_OK"] = 0
NAMES = {value: name for name, value in FS.items()}

# A Machine with nothing on channel 1 warns about it on every boot.
logging.getLogger("emulator.machine").setLevel(logging.ERROR)

PRELUDE = r"""
#include <pigeon/fs.h>
#include <pigeon/mem.h>
#include <pigeon/string.h>

#define TRY(x) if ((r = (x)) < 0) return r

/* The same bytes tests/test_pfs.py's pattern() makes. */
static void fill(unsigned char *p, unsigned n) {
    unsigned i;
    for (i = 0u; i < n; i++) p[i] = (unsigned char)((i * 7u + (i >> 9)) & 255u);
}

static int mismatches(unsigned char *p, unsigned n) {
    unsigned i;
    int bad = 0;
    for (i = 0u; i < n; i++) {
        if (p[i] != (unsigned char)((i * 7u + (i >> 9)) & 255u)) bad++;
    }
    return bad;
}

static char *numbered(char *out, char *prefix, unsigned n) {
    unsigned k = strlcpy(out, prefix, 64u);
    utoa(n, out + k, 10u);
    return out;
}
"""


def program(body):
    return PRELUDE + "\nint main(void) {\n    int r;\n" + body + "\n}\n"


def pattern(n):
    return bytes((i * 7 + i // 512) & 0xFF for i in range(n))


_build = functools.lru_cache(maxsize=None)(build)


def machine_for(disk, boot=None):
    return Machine(bios_path=str(BIOS), program_path=str(boot) if boot else None,
                   disk_path=str(disk))


def load(machine, source):
    machine.ram.load_bytes(_build(source), PROGRAM_LOAD_ADDR)
    machine.cpu.pc = PROGRAM_LOAD_ADDR


def run_fs(source, disk, boot=None, inspect=None, seconds=120):
    """Run a program to HALT and return main()'s result, signed."""
    machine = machine_for(disk, boot)
    try:
        load(machine, source)
        with contextlib.redirect_stdout(io.StringIO()):
            machine.run(deadline=time.time() + seconds)
        assert machine.cpu.halted, f"did not halt within {seconds}s (PC={machine.cpu.pc:#x})"
        assert machine.cpu.sp == STACK_TOP, f"hardware stack unbalanced: SP={machine.cpu.sp:#x}"
        if inspect is not None:
            inspect(machine)
        value = machine.cpu.reg.read(0)
        return value - (1 << 32) if value & 0x80000000 else value
    finally:
        machine.close()


def expect(got, want):
    assert got == want, (f"returned {got} ({NAMES.get(got, 'a step number')}), "
                         f"expected {want} ({NAMES.get(want, '')})")


@contextlib.contextmanager
def disks():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def formatted(d, name="disk.img", size=MiB, label=""):
    path = d / name
    PgfsImage.mkfs(path, size, label=label).close()
    return path


def blank(d, name="disk.img", size=MiB):
    path = d / name
    path.write_bytes(bytes(size))
    return path


def clean(path):
    with PgfsImage(path) as img:
        report = img.fsck()
    assert report.clean, "fsck: " + "; ".join(p.message for p in report.problems)


def host(path):
    return PgfsImage(path)


def first_difference(a: bytes, b: bytes) -> str:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return f"first difference at byte {i} (block {i // BLOCK}, offset {i % BLOCK})"
    return f"lengths differ: {len(a)} and {len(b)}"


# --- volumes -------------------------------------------------------------------

def test_format_and_mount_a_blank_disk():
    with disks() as d:
        disk = blank(d)
        expect(run_fs(program("""
            fs_volinfo vi;
            if (fs_mount(CH_HDD) != FS_ENOFS) return -100;
            TRY(fs_format(CH_HDD, "PIGEON", 0));
            TRY(fs_mount(CH_HDD));
            TRY(fs_statvfs(CH_HDD, &vi));
            if (strcmp(vi.label, "PIGEON") != 0) return -101;
            if (vi.total_blocks != 2048u || vi.block_size != 512u) return -102;
            return (int)vi.free_blocks;"""), disk), 2030)
        clean(disk)


def test_the_guest_formats_exactly_as_pfs_does():
    """Byte for byte, the whole image: the superblock, every FAT block,
    the root directory, and the zeros everywhere else."""
    with disks() as d:
        guest = blank(d, "guest.img")
        expect(run_fs(program('return fs_format(CH_HDD, "PIGEON", 0);'), guest), 0)
        oracle = blank(d, "host.img")
        PgfsImage.mkfs(oracle, label="PIGEON").close()
        a, b = guest.read_bytes(), oracle.read_bytes()
        assert a == b, first_difference(a, b)


def test_format_refuses_a_disk_holding_files():
    """A disk lives until it is deliberately reformatted."""
    with disks() as d:
        disk = formatted(d)
        with host(disk) as img:
            img.write_file("/keep", b"precious")
        expect(run_fs(program("return fs_format(CH_HDD, \"\", 0);"), disk), FS["FS_ENOTBLANK"])
        with host(disk) as img:
            assert img.read_file("/keep") == b"precious"
        expect(run_fs(program("return fs_format(CH_HDD, \"NEW\", FS_FORMAT_FORCE);"), disk), 0)
        with host(disk) as img:
            assert img.listdir("/") == [] and img.label == "NEW"
        clean(disk)


def test_the_boot_disk_is_refused():
    """Channel 1 is the program's own .bin. It has no superblock, so
    mounting it fails, and formatting it -- unforced -- is refused. The
    file on the host must come through untouched."""
    with disks() as d:
        boot = d / "program.bin"
        image = pattern(20 * BLOCK)
        boot.write_bytes(image)
        disk = formatted(d)
        expect(run_fs(program("""
            if (fs_mount(CH_USERPROG) != FS_ENOFS) return -100;
            return fs_format(CH_USERPROG, "", 0);"""), disk, boot=boot), FS["FS_ENOTBLANK"])
        assert boot.read_bytes() == image, "the program image was modified"


def timer_zero_status(machine):
    return struct.unpack_from("<I", machine.timer.callback(0, 5, 8, 0, bytearray(8)))[0]


@cases(("HID", "CH_HID"), ("the timer", "CH_TIMER"), ("the display", "CH_DISPLAY"),
       ("an empty channel", "7"), ("channel 0", "0"),
       ("channel 1 with nothing on it", "CH_USERPROG"))
def test_channels_that_are_not_disks(label, channel):
    """GET_SIZE is command 1, which is START on the timer: probing the
    timer channel would start timer 0. The known non-disks are refused
    before anything is sent."""
    statuses = []
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program(f"""
            if (fs_format({channel}, "", FS_FORMAT_FORCE) != FS_ENODEV) return -100;
            return fs_mount({channel});"""), disk,
               inspect=lambda m: statuses.append(timer_zero_status(m))), FS["FS_ENODEV"])
    assert statuses == [0], "timer 0 was started"


@cases(("version", 4, 2), ("block size", 8, 1024), ("more blocks than the disk", 12, 4096),
       ("FAT size", 20, 3), ("root outside the data", 64 + 36, 1))
def test_a_corrupt_superblock_is_refused(label, offset, value):
    with disks() as d:
        disk = formatted(d)
        raw = bytearray(disk.read_bytes())
        struct.pack_into("<I", raw, offset, value)
        disk.write_bytes(raw)
        expect(run_fs(program("return fs_mount(CH_HDD);"), disk), FS["FS_ECORRUPT"])


def test_mount_unmount_and_sync():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            fs_volinfo vi;
            int fd;
            TRY(fs_mount(CH_HDD));
            TRY(fs_mount(CH_HDD));                       /* twice is fine */
            TRY(fs_save("/f", "x", 1));
            fd = fs_open("/f", FS_READ);
            TRY(fd);
            if (fs_unmount(CH_HDD) != FS_EBUSY) return -100;
            TRY(fs_close(fd));
            TRY(fs_sync(CH_HDD));
            TRY(fs_unmount(CH_HDD));
            if (fs_open("/f", FS_READ) != FS_ENODEV) return -101;
            if (fs_statvfs(CH_HDD, &vi) != FS_ENODEV) return -102;
            if (fs_sync(CH_HDD) != FS_ENODEV) return -103;
            if (fs_unmount(CH_HDD) != FS_ENODEV) return -104;
            TRY(fs_mount(CH_HDD));
            return fs_load("/f", &vi, 4);"""), disk), 1)
        clean(disk)


# --- files ---------------------------------------------------------------------

@cases(0, 1, 511, 512, 513, 4095, 4096, 4097, 100_000)
def test_round_trip(size):
    """Every block edge and IO-window edge. The guest writes and reads
    back; then the host reads the same bytes."""
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program(f"""
            unsigned char *buf = (unsigned char *)malloc({size}u + 1u);
            unsigned char *back = (unsigned char *)malloc({size}u + 1u);
            fill(buf, {size}u);
            TRY(fs_mount(CH_HDD));
            TRY(fs_save("/f", buf, {size}u));
            memset(back, 0, {size}u + 1u);
            if (fs_load("/f", back, {size}u + 1u) != {size}) return -100;
            return mismatches(back, {size}u);"""), disk), 0)
        with host(disk) as img:
            assert img.read_file("/f") == pattern(size)
        clean(disk)


def test_writes_and_reads_in_uneven_pieces():
    """700-byte writes and 300-byte reads straddle every block boundary,
    so every byte goes through the partial-block path of the cache."""
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            unsigned char *buf = (unsigned char *)malloc(5000u);
            unsigned char *back = (unsigned char *)malloc(5000u);
            unsigned done;
            int fd;
            fill(buf, 5000u);
            TRY(fs_mount(CH_HDD));
            fd = fs_open("/f", FS_WRITE | FS_CREATE);
            TRY(fd);
            for (done = 0u; done < 5000u; done = done + 700u) {
                TRY(fs_write(fd, buf + done, done + 700u > 5000u ? 5000u - done : 700u));
            }
            TRY(fs_close(fd));
            fd = fs_open("/f", FS_READ);
            TRY(fd);
            for (done = 0u; done < 5000u; done = done + 300u) {
                TRY(fs_read(fd, back + done, 300u));
            }
            if (fs_read(fd, back, 10u) != 0) return -100;
            return mismatches(back, 5000u);"""), disk), 0)
        with host(disk) as img:
            assert img.read_file("/f") == pattern(5000)
        clean(disk)


def test_a_fragmented_file_reads_back_whole():
    """Appending after another file was written leaves a gap in the
    chain. A whole-file read moves contiguous runs in one command, so it
    must notice where a run ends."""
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            unsigned char *buf = (unsigned char *)malloc(2048u);
            unsigned char *back = (unsigned char *)malloc(2048u);
            int fd;
            fill(buf, 2048u);
            TRY(fs_mount(CH_HDD));
            TRY(fs_save("/a", buf, 512u));
            TRY(fs_save("/b", buf, 512u));
            fd = fs_open("/a", FS_WRITE | FS_APPEND);
            TRY(fd);
            TRY(fs_write(fd, buf + 512, 1536u));
            TRY(fs_close(fd));
            if (fs_load("/a", back, 2048u) != 2048) return -100;
            return mismatches(back, 2048u);"""), disk), 0)
        with host(disk) as img:
            assert img._chain(img.stat("/a").first) == [18, 20, 21, 22]
            assert img.read_file("/a") == pattern(2048)
        clean(disk)


def test_append_seek_and_tell():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            char b[8];
            int fd;
            TRY(fs_mount(CH_HDD));
            TRY(fs_save("/log", "hello", 5u));
            fd = fs_open("/log", FS_WRITE | FS_APPEND);
            TRY(fd);
            TRY(fs_write(fd, " world", 6u));
            TRY(fs_close(fd));
            fd = fs_open("/log", FS_READ);
            TRY(fd);
            if (fs_seek(fd, -5, FS_SEEK_END) != 6) return -100;
            memset(b, 0, 8u);
            if (fs_read(fd, b, 7u) != 5) return -101;
            if (strcmp(b, "world") != 0) return -102;
            if (fs_tell(fd) != 11) return -103;
            if (fs_seek(fd, 1, FS_SEEK_END) != FS_EINVAL) return -104;
            if (fs_seek(fd, -12, FS_SEEK_CUR) != FS_EINVAL) return -105;
            if (fs_seek(fd, 0, 7) != FS_EINVAL) return -106;
            if (fs_seek(fd, 2, FS_SEEK_SET) != 2) return -107;
            if (fs_read(fd, b, 3u) != 3 || b[0] != 'l') return -108;
            return fs_close(fd);"""), disk), 0)
        with host(disk) as img:
            assert img.read_file("/log") == b"hello world"
        clean(disk)


def test_overwriting_the_middle_keeps_the_rest():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            unsigned char *buf = (unsigned char *)malloc(1500u);
            int fd;
            fill(buf, 1500u);
            TRY(fs_mount(CH_HDD));
            TRY(fs_save("/f", buf, 1500u));
            fd = fs_open("/f", FS_WRITE);
            TRY(fd);
            TRY(fs_seek(fd, 700, FS_SEEK_SET));
            TRY(fs_write(fd, "XXXXXXXXXX", 10u));
            return fs_close(fd);"""), disk), 0)
        expected = bytearray(pattern(1500))
        expected[700:710] = b"X" * 10
        with host(disk) as img:
            assert img.read_file("/f") == bytes(expected)
        clean(disk)


def test_truncating_on_open_frees_the_blocks():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            unsigned char *buf = (unsigned char *)malloc(3000u);
            fs_volinfo vi;
            unsigned before;
            TRY(fs_mount(CH_HDD));
            TRY(fs_statvfs(CH_HDD, &vi));
            before = vi.free_blocks;
            TRY(fs_save("/f", buf, 3000u));
            TRY(fs_close(fs_open("/f", FS_WRITE | FS_TRUNC)));
            TRY(fs_statvfs(CH_HDD, &vi));
            return (int)(before - vi.free_blocks);"""), disk), 0)
        with host(disk) as img:
            assert img.stat("/f").size == 0
        clean(disk)


def test_open_refusals():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            fs_stat_t st;
            char b[4];
            int fd;
            TRY(fs_mount(CH_HDD));
            TRY(fs_mkdir("/d"));
            TRY(fs_save("/f", "x", 1u));
            if (fs_open("/missing", FS_WRITE) != FS_ENOENT) return -100;
            if (fs_open("/missing", FS_READ | FS_CREATE) != FS_EINVAL) return -101;
            if (fs_open("/f", 0) != FS_EINVAL) return -102;
            if (fs_open("/d", FS_READ) != FS_EISDIR) return -103;
            if (fs_open("/", FS_READ) != FS_EISDIR) return -104;
            if (fs_open("/nodir/f", FS_WRITE | FS_CREATE) != FS_ENOENT) return -105;
            if (fs_open("/f/x", FS_WRITE | FS_CREATE) != FS_ENOTDIR) return -106;
            fd = fs_open("/f", FS_READ);
            TRY(fd);
            if (fs_write(fd, "y", 1u) != FS_EBADF) return -107;
            if (fs_readdir(fd, &st) != FS_EBADF) return -108;
            if (fs_closedir(fd) != FS_EBADF) return -109;
            TRY(fs_close(fd));
            if (fs_close(fd) != FS_EBADF) return -110;
            if (fs_read(99, b, 1u) != FS_EBADF) return -111;
            if (fs_read(-1, b, 1u) != FS_EBADF) return -112;
            return 0;"""), disk), 0)
        clean(disk)


def test_one_writer_any_number_of_readers():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            int w;
            int a;
            int b;
            TRY(fs_mount(CH_HDD));
            w = fs_open("/f", FS_WRITE | FS_CREATE);
            TRY(w);
            if (fs_open("/f", FS_WRITE) != FS_EBUSY) return -100;
            a = fs_open("/f", FS_READ);
            b = fs_open("/f", FS_READ);
            if (a < 0 || b < 0) return -101;
            TRY(fs_close(w));
            if (fs_open("/f", FS_WRITE | FS_TRUNC) != FS_EBUSY) return -102;
            if (fs_remove("/f") != FS_EBUSY) return -103;
            if (fs_rename("/f", "/g") != FS_EBUSY) return -104;
            TRY(fs_close(a));
            TRY(fs_close(b));
            return fs_remove("/f");"""), disk), 0)
        clean(disk)


def test_eight_handles_and_no_more():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            int fds[8];
            int i;
            TRY(fs_mount(CH_HDD));
            TRY(fs_save("/f", "x", 1u));
            for (i = 0; i < 8; i++) {
                fds[i] = fs_open("/f", FS_READ);
                TRY(fds[i]);
            }
            if (fs_open("/f", FS_READ) != FS_EMFILE) return -100;
            if (fs_opendir("/") != FS_EMFILE) return -101;     /* the same table */
            TRY(fs_close(fds[3]));
            TRY(fs_close(fs_open("/f", FS_READ)));
            return 0;"""), disk), 0)


def test_names_and_path_lengths():
    long_path = "/" + "/".join(["p" * 31] * 8)          # 256 bytes
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program(f"""
            TRY(fs_mount(CH_HDD));
            if (fs_save("/{'n' * 31}", "a", 1u) != 1) return -100;
            if (fs_save("/{'n' * 32}", "a", 1u) != FS_ENAMETOOLONG) return -101;
            if (fs_save("/a:b", "a", 1u) != FS_EINVAL) return -102;
            if (fs_save("{long_path}", "a", 1u) != FS_ENAMETOOLONG) return -103;
            if (fs_save("/my file.txt", "sp", 2u) != 2) return -104;
            return 0;"""), disk), 0)
        with host(disk) as img:
            assert sorted(e.name for e in img.listdir("/")) == ["my file.txt", "n" * 31]
        clean(disk)


# --- directories ----------------------------------------------------------------

def test_directories_readdir_and_stat():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            fs_stat_t st;
            int dh;
            int files = 0;
            int dirs = 0;
            int bytes = 0;
            TRY(fs_mount(CH_HDD));
            TRY(fs_mkdir("/a"));
            TRY(fs_mkdir("/a/b"));
            TRY(fs_save("/a/f1", "12345", 5u));
            TRY(fs_save("/a/f2", "", 0u));
            if (fs_mkdir("/a") != FS_EEXIST) return -100;
            if (fs_mkdir("/") != FS_EEXIST) return -101;
            if (fs_mkdir("/x/y") != FS_ENOENT) return -102;
            if (fs_mkdir("/a/f1/z") != FS_ENOTDIR) return -103;
            if (fs_opendir("/a/f1") != FS_ENOTDIR) return -104;
            dh = fs_opendir("/a");
            TRY(dh);
            while ((r = fs_readdir(dh, &st)) == 1) {
                if (st.type == FS_TYPE_DIR) dirs++;
                else { files++; bytes = bytes + (int)st.size; }
            }
            if (r != 0) return r;
            TRY(fs_closedir(dh));
            TRY(fs_stat("/a/b", &st));
            if (st.type != FS_TYPE_DIR || st.size != 512u || strcmp(st.name, "b") != 0) return -105;
            TRY(fs_stat("/", &st));
            if (st.type != FS_TYPE_DIR) return -106;
            if (fs_stat("/a/nope", &st) != FS_ENOENT) return -107;
            return files * 100 + dirs * 10 + bytes;"""), disk), 215)
        clean(disk)


def test_a_directory_grows_past_eight_entries():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            char name[64];
            fs_stat_t st;
            unsigned i;
            int dh;
            int n = 0;
            TRY(fs_mount(CH_HDD));
            TRY(fs_mkdir("/d"));
            for (i = 0u; i < 20u; i++) TRY(fs_save(numbered(name, "/d/f", i), "", 0u));
            dh = fs_opendir("/d");
            TRY(dh);
            while (fs_readdir(dh, &st) == 1) n++;
            TRY(fs_closedir(dh));
            return n;"""), disk), 20)
        with host(disk) as img:
            assert img.stat("/d").size == 3 * BLOCK
            assert len(img.listdir("/d")) == 20
        clean(disk)


def test_removing_everything_returns_every_block():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            unsigned char *buf = (unsigned char *)malloc(5000u);
            char name[64];
            fs_volinfo vi;
            unsigned before;
            unsigned i;
            TRY(fs_mount(CH_HDD));
            TRY(fs_statvfs(CH_HDD, &vi));
            before = vi.free_blocks;
            TRY(fs_mkdir("/x"));
            TRY(fs_mkdir("/x/y"));
            for (i = 0u; i < 12u; i++) TRY(fs_save(numbered(name, "/x/y/f", i), buf, i * 300u));
            TRY(fs_save("/x/top", buf, 5000u));
            for (i = 0u; i < 12u; i++) TRY(fs_remove(numbered(name, "/x/y/f", i)));
            TRY(fs_rmdir("/x/y"));
            TRY(fs_remove("/x/top"));
            TRY(fs_rmdir("/x"));
            TRY(fs_statvfs(CH_HDD, &vi));
            return (int)(before - vi.free_blocks);"""), disk), 0)
        with host(disk) as img:
            assert img.listdir("/") == []
        clean(disk)


def test_remove_and_rmdir_refusals():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            int dh;
            TRY(fs_mount(CH_HDD));
            TRY(fs_mkdir("/d"));
            TRY(fs_save("/d/f", "x", 1u));
            if (fs_rmdir("/d") != FS_ENOTEMPTY) return -100;
            if (fs_rmdir("/d/f") != FS_ENOTDIR) return -101;
            if (fs_remove("/d") != FS_EISDIR) return -102;
            if (fs_rmdir("/") != FS_EBUSY) return -103;
            if (fs_remove("/") != FS_EISDIR) return -104;
            if (fs_remove("/nope") != FS_ENOENT) return -105;
            dh = fs_opendir("/d");
            TRY(dh);
            TRY(fs_remove("/d/f"));
            if (fs_rmdir("/d") != FS_EBUSY) return -106;    /* a handle is open on it */
            TRY(fs_closedir(dh));
            return fs_rmdir("/d");"""), disk), 0)
        clean(disk)


def test_rename():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            TRY(fs_mount(CH_HDD));
            TRY(fs_mkdir("/a"));
            TRY(fs_mkdir("/b"));
            TRY(fs_save("/a/f", "data", 4u));
            TRY(fs_rename("/a/f", "/a/g"));
            TRY(fs_rename("/a/g", "/b/h"));
            TRY(fs_mkdir("/a/sub"));
            TRY(fs_save("/a/sub/deep", "deep", 4u));
            TRY(fs_rename("/a", "/b/a2"));                  /* a directory, contents and all */
            if (fs_rename("/b", "/b/a2/inside") != FS_EINVAL) return -100;
            if (fs_rename("/b", "/b/./a2/../a2/x") != FS_EINVAL) return -101;
            TRY(fs_save("/c", "", 0u));
            if (fs_rename("/c", "/b/h") != FS_EEXIST) return -102;
            if (fs_rename("/", "/z") != FS_EBUSY) return -103;
            if (fs_rename("/missing", "/z") != FS_ENOENT) return -104;
            if (fs_rename("/c", "/missing/z") != FS_ENOENT) return -105;
            if (fs_rename("/c", "/bad:name") != FS_EINVAL) return -106;
            return fs_rename("/c", "/./c");                 /* to itself: nothing to do */"""),
            disk), 0)
        with host(disk) as img:
            assert img.read_file("/b/h") == b"data"
            assert img.read_file("/b/a2/sub/deep") == b"deep"
            assert not img.exists("/a") and img.exists("/c")
        clean(disk)


# --- the current directory ------------------------------------------------------

def test_the_current_directory():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            char cwd[64];
            char buf[16];
            TRY(fs_mount(CH_HDD));
            if (fs_getcwd(cwd, 64u) != 3 || strcmp(cwd, "2:/") != 0) return -100;
            TRY(fs_mkdir("/a"));
            TRY(fs_mkdir("a/b"));                        /* relative, from the root */
            TRY(fs_chdir("/a/b"));
            fs_getcwd(cwd, 64u);
            if (strcmp(cwd, "2:/a/b") != 0) return -101;
            TRY(fs_save("f", "rel", 3u));                 /* /a/b/f */
            TRY(fs_chdir(".."));
            fs_getcwd(cwd, 64u);
            if (strcmp(cwd, "2:/a") != 0) return -102;
            if (fs_load("b/f", buf, 16u) != 3) return -103;
            if (fs_load("./b/../b//f", buf, 16u) != 3) return -104;
            if (fs_load("2:a/b/f", buf, 16u) != 3) return -105;   /* a prefix means the root */
            TRY(fs_chdir("../../.."));                    /* .. at the root stays there */
            fs_getcwd(cwd, 64u);
            if (strcmp(cwd, "2:/") != 0) return -106;
            if (fs_chdir("/a/b/f") != FS_ENOTDIR) return -107;
            if (fs_chdir("/nope") != FS_ENOENT) return -108;
            if (fs_chdir("3:/") != FS_ENODEV) return -109;
            TRY(fs_chdir("/a/b"));
            if (fs_rmdir("/a/b") != FS_EBUSY) return -110;
            if (fs_rename("/a", "/z") != FS_EBUSY) return -111;    /* above the cwd */
            if (fs_rename("/a/b", "/z") != FS_EBUSY) return -112;
            if (fs_getcwd(cwd, 6u) != FS_E2BIG) return -113;
            TRY(fs_chdir("/"));
            return fs_rename("/a", "/z");"""), disk), 0)
        with host(disk) as img:
            assert img.read_file("/z/b/f") == b"rel"
        clean(disk)


def test_two_volumes():
    """Channel 1 can hold a PigeonFS image too. Paths name the volume by
    channel, and the current directory can move between them."""
    with disks() as d:
        disk = formatted(d, "disk.img")
        boot = formatted(d, "boot.img")
        expect(run_fs(program("""
            char cwd[16];
            TRY(fs_mount(CH_HDD));
            TRY(fs_mount(CH_USERPROG));
            TRY(fs_save("2:/x", "two", 3u));
            TRY(fs_save("1:/x", "one", 3u));
            TRY(fs_save("/y", "default", 7u));           /* the first mounted: channel 2 */
            if (fs_rename("1:/x", "2:/z") != FS_EXDEV) return -100;
            TRY(fs_chdir("1:/"));
            TRY(fs_save("rel", "r", 1u));
            if (fs_getcwd(cwd, 16u) != 3 || strcmp(cwd, "1:/") != 0) return -101;
            TRY(fs_unmount(CH_USERPROG));                /* the current volume goes */
            if (fs_getcwd(cwd, 16u) < 0 || strcmp(cwd, "2:/") != 0) return -102;
            if (fs_open("1:/x", FS_READ) != FS_ENODEV) return -103;
            return 0;"""), disk, boot=boot), 0)
        with host(disk) as img:
            assert img.read_file("/x") == b"two" and img.read_file("/y") == b"default"
        with host(boot) as img:
            assert img.read_file("/x") == b"one" and img.read_file("/rel") == b"r"
        clean(disk)
        clean(boot)


# --- lines and whole files ------------------------------------------------------

def test_lines():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            unsigned char *big = (unsigned char *)malloc(700u);
            char line[10];
            char wide[1000];
            int fd;
            int steps = 0;
            TRY(fs_mount(CH_HDD));
            fd = fs_open("/t.txt", FS_WRITE | FS_CREATE);
            TRY(fd);
            TRY(fs_puts(fd, "one\\n"));
            TRY(fs_puts(fd, "\\n"));
            TRY(fs_puts(fd, "a line longer than ten\\n"));
            TRY(fs_puts(fd, "last"));
            TRY(fs_close(fd));
            fd = fs_open("/t.txt", FS_READ);
            TRY(fd);
            if (fs_gets(fd, line, 1u) != FS_EINVAL) return -100;
            if (fs_gets(fd, line, 10u) == 4 && strcmp(line, "one\\n") == 0) steps++;
            if (fs_gets(fd, line, 10u) == 1 && strcmp(line, "\\n") == 0) steps++;
            if (fs_gets(fd, line, 10u) == 9 && strcmp(line, "a line lo") == 0) steps++;
            if (fs_gets(fd, line, 10u) == 9 && strcmp(line, "nger than") == 0) steps++;
            if (fs_gets(fd, line, 10u) == 5 && strcmp(line, " ten\\n") == 0) steps++;
            if (fs_gets(fd, line, 10u) == 4 && strcmp(line, "last") == 0) steps++;
            if (fs_gets(fd, line, 10u) == 0 && line[0] == 0) steps++;
            TRY(fs_close(fd));
            /* a line that crosses a block boundary */
            memset(big, 'a', 600u);
            big[600] = '\\n';
            big[601] = 'b';
            big[602] = '\\n';
            TRY(fs_save("/wide.txt", big, 603u));
            fd = fs_open("/wide.txt", FS_READ);
            TRY(fd);
            if (fs_gets(fd, wide, 1000u) == 601 && wide[599] == 'a' && wide[600] == '\\n') steps++;
            if (fs_gets(fd, wide, 1000u) == 2 && strcmp(wide, "b\\n") == 0) steps++;
            TRY(fs_close(fd));
            return steps;"""), disk), 9)
        with host(disk) as img:
            assert img.read_file("/t.txt") == b"one\n\na line longer than ten\nlast"
        clean(disk)


def test_whole_files_and_messages():
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            char b[16];
            char *p;
            unsigned n = 0u;
            TRY(fs_mount(CH_HDD));
            TRY(fs_save("/f", "pigeon", 6u));
            if (fs_load("/f", b, 5u) != FS_E2BIG) return -100;
            if (fs_load("/f", b, 6u) != 6) return -101;
            p = (char *)fs_load_alloc("/f", &n);
            if (p == NULL || n != 6u || memcmp(p, "pigeon", 6u) != 0) return -102;
            free(p);
            if (fs_load_alloc("/missing", &n) != NULL) return -103;
            TRY(fs_save("/f", "pi", 2u));                 /* replace */
            if (fs_load("/f", b, 16u) != 2) return -104;
            if (fs_load("/missing", b, 16u) != FS_ENOENT) return -105;
            if (strcmp(fs_strerror(FS_ENOENT), "no such file or directory") != 0) return -106;
            if (strlen(fs_strerror(-99)) == 0u) return -107;
            return 0;"""), disk), 0)
        with host(disk) as img:
            assert img.read_file("/f") == b"pi"
        clean(disk)


def test_a_full_disk():
    """16 blocks leaves 13 for files. A save that needs 14 writes 13 and
    reports FS_ENOSPC; nothing leaks, and deleting gives them back."""
    with disks() as d:
        disk = formatted(d, size=16 * BLOCK)
        expect(run_fs(program("""
            unsigned char *buf = (unsigned char *)malloc(8192u);
            fs_volinfo vi;
            fill(buf, 8192u);
            TRY(fs_mount(CH_HDD));
            if (fs_save("/big", buf, 7168u) != FS_ENOSPC) return -100;
            TRY(fs_statvfs(CH_HDD, &vi));
            if (vi.free_blocks != 0u) return -101;
            if (fs_mkdir("/d") != FS_ENOSPC) return -102;
            TRY(fs_remove("/big"));
            TRY(fs_statvfs(CH_HDD, &vi));
            if (vi.free_blocks != 13u) return -103;
            TRY(fs_save("/big", buf, 6656u));             /* exactly 13 blocks */
            return 0;"""), disk), 0)
        with host(disk) as img:
            assert img.read_file("/big") == pattern(6656) and img.free_count() == 0
        clean(disk)


# --- against the host, and over time ---------------------------------------------

def test_the_guest_reads_what_the_host_wrote():
    with disks() as d:
        disk = formatted(d)
        with host(disk) as img:
            img.mkdir("/docs")
            img.write_file("/docs/in.bin", pattern(3000))
        expect(run_fs(program("""
            unsigned char *back = (unsigned char *)malloc(3000u);
            fs_stat_t st;
            TRY(fs_mount(CH_HDD));
            TRY(fs_stat("/docs/in.bin", &st));
            if (st.size != 3000u) return -100;
            if (fs_load("/docs/in.bin", back, 3000u) != 3000) return -101;
            return mismatches(back, 3000u);"""), disk), 0)


def test_the_same_operations_leave_the_same_bytes():
    """The oracle test. One sequence -- mkdir, saves, a remove, next-fit
    reuse, a rename into a directory, enough files to grow it, a
    replacement, a directory moved -- done by the guest on one image and
    by pfs.py on another. The images must match byte for byte."""
    with disks() as d:
        guest = formatted(d, "guest.img")
        oracle = formatted(d, "host.img")
        expect(run_fs(program("""
            unsigned char *buf = (unsigned char *)malloc(4000u);
            char name[64];
            unsigned i;
            fill(buf, 4000u);
            TRY(fs_mount(CH_HDD));
            TRY(fs_mkdir("/docs"));
            TRY(fs_save("/docs/a.bin", buf, 3000u));
            TRY(fs_save("/b.bin", buf, 600u));
            TRY(fs_remove("/docs/a.bin"));
            TRY(fs_save("/c.bin", buf, 1000u));
            TRY(fs_rename("/c.bin", "/docs/c.bin"));
            for (i = 0u; i < 10u; i++) TRY(fs_save(numbered(name, "/docs/f", i), buf, i * 100u));
            TRY(fs_save("/b.bin", buf, 1500u));
            TRY(fs_mkdir("/docs/sub"));
            TRY(fs_rename("/docs/sub", "/sub2"));
            return 0;"""), guest), 0)
        with host(oracle) as img:
            img.mkdir("/docs")
            img.write_file("/docs/a.bin", pattern(3000))
            img.write_file("/b.bin", pattern(600))
            img.remove("/docs/a.bin")
            img.write_file("/c.bin", pattern(1000))
            img.rename("/c.bin", "/docs/c.bin")
            for i in range(10):
                img.write_file(f"/docs/f{i}", pattern(i * 100))
            img.write_file("/b.bin", pattern(1500))
            img.mkdir("/docs/sub")
            img.rename("/docs/sub", "/sub2")
        a, b = guest.read_bytes(), oracle.read_bytes()
        assert a == b, first_difference(a, b)
        clean(guest)


def test_reused_blocks_leave_the_same_bytes_too():
    """The same oracle comparison, on a 32-block disk small enough that
    next-fit wraps around into blocks that held other files. On a fresh
    disk every block starts as zeros, so three mistakes stay invisible,
    and this makes each one show:

      - a directory growing into a used block without zeroing it first
        (its old bytes would read as directory entries);
      - a short write into a used block without zeroing the rest
        (pfs.py pads a file's last block with zeros);
      - a multi-block write that assumes its blocks are contiguous. /e
        needs six blocks, gets 10-12 and 23-25, and /b sits in between.
    """
    def guest_ops():
        return program("""
            unsigned char *buf = (unsigned char *)malloc(5120u);
            char name[64];
            unsigned i;
            fill(buf, 5120u);
            TRY(fs_mount(CH_HDD));
            TRY(fs_save("/a", buf, 5120u));               /* blocks 3-12 */
            TRY(fs_save("/b", buf, 5120u));               /* 13-22 */
            TRY(fs_save("/c", buf, 2560u));               /* 23-27 */
            TRY(fs_remove("/a"));
            TRY(fs_mkdir("/d"));                          /* 28 */
            for (i = 0u; i < 9u; i++) TRY(fs_save(numbered(name, "/d/f", i), buf, 300u));
            TRY(fs_remove("/c"));
            TRY(fs_save("/e", buf, 3072u));
            return 0;""")

    with disks() as d:
        guest = formatted(d, "guest.img", size=32 * BLOCK)
        oracle = formatted(d, "host.img", size=32 * BLOCK)
        expect(run_fs(guest_ops(), guest), 0)
        with host(oracle) as img:
            img.write_file("/a", pattern(5120))
            img.write_file("/b", pattern(5120))
            img.write_file("/c", pattern(2560))
            img.remove("/a")
            img.mkdir("/d")
            for i in range(9):
                img.write_file(f"/d/f{i}", pattern(300))
            img.remove("/c")
            img.write_file("/e", pattern(3072))
            assert img._chain(img.stat("/e").first) == [10, 11, 12, 23, 24, 25], \
                "the scenario no longer fragments /e; the test has lost its point"
        a, b = guest.read_bytes(), oracle.read_bytes()
        assert a == b, first_difference(a, b)
        with host(guest) as img:
            assert img.read_file("/b") == pattern(5120)
        clean(guest)


def test_files_survive_a_new_machine():
    """What one run saves, the next run finds: the disk outlives the
    program and the Machine."""
    with disks() as d:
        disk = formatted(d)
        expect(run_fs(program("""
            unsigned char *buf = (unsigned char *)malloc(2000u);
            fill(buf, 2000u);
            TRY(fs_mount(CH_HDD));
            TRY(fs_mkdir("/saves"));
            return fs_save("/saves/game", buf, 2000u);"""), disk), 2000)
        expect(run_fs(program("""
            unsigned char *back = (unsigned char *)malloc(2000u);
            TRY(fs_mount(CH_HDD));
            if (fs_load("/saves/game", back, 2000u) != 2000) return -100;
            return mismatches(back, 2000u);"""), disk), 0)
        clean(disk)


# The renames go both ways between two directories, out of /d into the
# root and then back. A rename's new name and the clearing of its old one
# land in different directory blocks, and a flush writes those in cache
# slot order. So if the flush between the two steps went missing, one
# direction or the other would write the clear first and leave the file
# with no name at all -- whichever slots the blocks happen to be in.
CRASH = program("""
    unsigned char *buf = (unsigned char *)malloc(6000u);
    fill(buf, 6000u);
    TRY(fs_mount(CH_HDD));
    TRY(fs_mkdir("/d"));
    TRY(fs_save("/d/a", buf, 6000u));
    TRY(fs_save("/d/a", buf, 2500u));             /* truncate, free, write */
    TRY(fs_rename("/d/a", "/b"));                 /* into the root */
    TRY(fs_rename("/b", "/d/c"));                 /* and back into /d */
    TRY(fs_rename("/d/c", "/e"));
    TRY(fs_rmdir("/d"));
    return 0;""")
RENAMED = ("/d/a", "/b", "/d/c", "/e")          # every name the 2500-byte file has


def disk_states(disk):
    """Run CRASH to the end, keeping a copy of the disk after every write
    it makes. Stopping the emulator can only ever land between two
    writes, so this is every state a crash could leave behind."""
    states = []
    machine = machine_for(disk)
    try:
        channel = machine.io_controller.channels[CH_HDD]
        device = channel.callback

        def recording(read_write, command, length, address, data):
            reply = device(read_write, command, length, address, data)
            if command == 3:                    # HDD WRITE
                states.append(disk.read_bytes())
            return reply

        channel.callback = recording
        load(machine, CRASH)
        with contextlib.redirect_stdout(io.StringIO()):
            machine.run(deadline=time.time() + 120)
        assert machine.cpu.halted and machine.cpu.reg.read(0) == 0, "CRASH itself failed"
    finally:
        machine.close()
    return states


def every_file(img, path="/"):
    for entry in img.listdir(path):
        child = path.rstrip("/") + "/" + entry.name
        if entry.is_dir:
            yield from every_file(img, child)
        else:
            yield child


def test_every_crash_point_is_repairable():
    """Section 6.3's promise, checked after every single write rather than
    at a few sampled moments -- the write that matters can be one of
    dozens, and a sample misses it. A crash may leave leaked blocks, an
    overlong chain, or a file under two names, all of which fsck
    --repair fixes. It must never leave a cross-link, or an entry
    pointing into free space, and every file must still be readable.

    Repairable is not enough on its own: a file left with no name is
    only leaked blocks to fsck, which it happily frees. So once the
    renamed file exists, it must exist in every later state too."""
    with disks() as d:
        states = disk_states(formatted(d))
        assert len(states) > 20, f"only {len(states)} writes: CRASH no longer exercises much"
        scratch = d / "state.img"
        committed = False
        for i, state in enumerate(states):
            scratch.write_bytes(state)
            with host(scratch) as img:
                report = img.fsck(repair=True)
                assert not report.unrepaired, (
                    f"after write {i + 1} of {len(states)}: " +
                    "; ".join(p.message for p in report.unrepaired))
                assert img.fsck().clean
                for path in every_file(img):
                    img.read_file(path)
                present = any(img.exists(p) and not img.stat(p).is_dir
                              and img.read_file(p) == pattern(2500) for p in RENAMED)
            if present:
                committed = True
            assert present or not committed, (
                f"after write {i + 1} of {len(states)}: the file being renamed "
                f"has no name at all")
        assert committed, "the 2500-byte file never appeared"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "PigeonFS on the guest (lib/pigeon/fs.c)"))
