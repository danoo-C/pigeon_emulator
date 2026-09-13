"""<pigeon/cd.h> on the guest: lib/pigeon/cd.c, running on the emulator.

Phase 5 of docs/cd-drive.md. Every test compiles a C program with the
library and runs it on a real Machine whose CD drive holds a temporary
file, reusing tests/test_fs.py's harness -- which already knows how to put
a disc in the drive, because phase 1 needed it for read-only volumes.

The library is small; what it has to get right is the edges between it
and everything around it. A read that crosses the 4 KB IO window. A
channel that is a disk, or the timer, rather than a drive. A save that
fills the disk halfway. A disc swapped while it is being copied. Error
codes from two libraries coming back through one call. Those are what
these tests are about.

    python3 tests/test_cdlib.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    CH_CD, CH_DISPLAY, CH_HID, CH_TIMER, STACK_TOP)
from test_fs import (                                                 # noqa: E402
    FS, MiB, PRELUDE, blank, clean, disc_image, disks, formatted, host, load,
    machine_for, pattern, run_fs, timer_zero_status)
from test_libs import build                                           # noqa: E402

CD = {name: int(value) for name, value in re.findall(
    r"#define (CD_E\w+)\s+\((-\d+)\)", (REPO_ROOT / "lib/pigeon/cd.h").read_text())}
CD["CD_OK"] = 0
NAMES = {**{v: k for k, v in FS.items()}, **{v: k for k, v in CD.items()}}

CD_PRELUDE = "#include <pigeon/cd.h>\n" + PRELUDE


def program(body):
    return CD_PRELUDE + "\nint main(void) {\n    int r;\n" + body + "\n}\n"


def describe(code):
    """CD codes are unambiguous. -1 to -19 is also where these programs
    number their own failing steps, so an FS name there may be neither."""
    if code == 0 or (code in NAMES and code <= -101):
        return NAMES[code]
    if code in NAMES:
        return f"{NAMES[code]}, or step {code}"
    return f"step {code}" if code < 0 else "a count"


def expect(got, want):
    assert got == want, f"returned {got} ({describe(got)}), expected {want} ({describe(want)})"


def raw_disc(d, name="demo.bin", data=b""):
    path = d / name
    path.write_bytes(data)
    return path


def run_machine(source, disk, disc=None, before=None, after=None, seconds=120):
    """run_fs with a hand on the machine: before(machine) runs after the
    program is loaded and before it starts, for a test that has to change
    what is in the drive, or wrap a callback, first; after(machine) runs
    once it has halted."""
    machine = machine_for(disk, disc=disc)
    try:
        load(machine, source)
        if before is not None:
            before(machine)
        with contextlib.redirect_stdout(io.StringIO()):
            machine.run(deadline=time.time() + seconds)
        assert machine.cpu.halted, f"did not halt within {seconds}s (PC={machine.cpu.pc:#x})"
        assert machine.cpu.sp == STACK_TOP, f"hardware stack unbalanced: SP={machine.cpu.sp:#x}"
        if after is not None:
            after(machine)
        value = machine.cpu.reg.read(0)
        return value - (1 << 32) if value & 0x80000000 else value
    finally:
        machine.close()


# --- the error codes -----------------------------------------------------------

def test_cd_error_codes_can_never_be_mistaken_for_fs_codes():
    """cd_save() returns a CD_* or an FS_* code through the same int. The
    design's first draft numbered CD errors -1 to -4 -- which are exactly
    FS_ENOENT to FS_ENOTDIR."""
    cd_codes = {v for k, v in CD.items() if k != "CD_OK"}
    fs_codes = {v for k, v in FS.items() if k != "FS_OK"}
    assert cd_codes, "no CD_E* codes found in cd.h"
    assert not cd_codes & fs_codes, f"collisions: {sorted(cd_codes & fs_codes)}"
    assert max(cd_codes) <= -101, "CD codes left their own range"


def test_a_program_that_includes_only_cd_h_can_pass_null():
    """cd.h's own example is cd_save(CH_CD, NULL). Every unit is
    preprocessed on its own, so NULL has to come from cd.h itself."""
    build("#include <pigeon/cd.h>\n"
          "int main(void) { return cd_save(CH_CD, NULL); }\n")


# --- reading --------------------------------------------------------------------

def test_a_disc_reads_back_in_pieces_that_cross_the_io_window():
    """1000, 777, 4097 and then 5000: the third needs two IO commands, and
    the last runs off the end of the disc and comes back short."""
    with disks() as d:
        disc = raw_disc(d, data=pattern(10000))
        expect(run_fs(program("""
            unsigned char *buf = (unsigned char *)malloc(10000u);
            unsigned sizes[4];
            unsigned off = 0u;
            unsigned k = 0u;
            int n;

            if (buf == NULL) return -1;
            sizes[0] = 1000u; sizes[1] = 777u; sizes[2] = 4097u; sizes[3] = 5000u;
            for (;;) {
                n = cd_read(CH_CD, off, buf + off, sizes[k]);
                if (n < 0) return n;
                off = off + (unsigned)n;
                if ((unsigned)n < sizes[k]) break;
                k++;
            }
            if (mismatches(buf, off) != 0) return -2;
            return (int)off;"""), blank(d), disc=disc), 10000)


def test_reads_come_back_short_at_the_end_and_empty_past_it():
    with disks() as d:
        disc = raw_disc(d, data=pattern(100))
        expect(run_fs(program("""
            unsigned char buf[600];
            if (cd_read(CH_CD, 0u, buf, 512u) != 100) return -1;
            if (cd_read(CH_CD, 60u, buf + 60, 512u) != 40) return -2;
            if (cd_read(CH_CD, 100u, buf, 512u) != 0) return -3;
            if (cd_read(CH_CD, 5000u, buf, 512u) != 0) return -4;
            if (cd_read(CH_CD, 0u, buf, 0u) != 0) return -5;
            if (cd_read(CH_CD, 0u, NULL, 4u) != CD_EINVAL) return -6;
            if (mismatches(buf, 100u) != 0) return -7;
            return 0;"""), blank(d), disc=disc), 0)


def test_info_describes_the_disc_that_is_in():
    with disks() as d:
        disc = raw_disc(d, "demo.bin", pattern(1234))
        expect(run_fs(program("""
            cd_info_t info;
            r = cd_info(CH_CD, &info);
            if (r != CD_OK) return r;
            if (info.present != 1u) return -1;
            if (info.size != 1234u) return -2;
            if (info.generation != 1u) return -3;
            if (strcmp(info.name, "demo.bin") != 0) return -4;
            if (cd_present(CH_CD) != 1) return -5;
            if (cd_generation(CH_CD) != 1u) return -6;
            return 0;"""), blank(d), disc=disc), 0)


def test_an_empty_drive_says_so_everywhere():
    """A disc goes in and comes out before the program starts, so the
    drive is empty at generation 2. That is what shows cd_info() really
    fills *out on CD_ENODISC: on a drive that has never held a disc every
    field is 0, which a memset alone would also produce."""
    with disks() as d:
        disc = raw_disc(d, data=b"x" * 64)
        expect(run_machine(program("""
            cd_info_t info;
            char label[16];
            unsigned char buf[8];

            info.present = 99u;
            if (cd_info(CH_CD, &info) != CD_ENODISC) return -1;
            if (info.present != 0u || info.size != 0u) return -2;
            if (info.generation != 2u) return -8;        /* still filled in */
            if (cd_generation(CH_CD) != 2u) return -9;
            if (cd_present(CH_CD) != 0) return -3;
            if (cd_has_fs(CH_CD) != 0) return -4;
            if (cd_read(CH_CD, 0u, buf, 8u) != CD_ENODISC) return -5;
            if (cd_label(CH_CD, label, 16u) != CD_ENODISC) return -6;
            if (label[0] != 0) return -7;
            TRY(fs_mount(CH_HDD));
            return cd_save(CH_CD, NULL);"""), formatted(d), disc=disc,
            before=lambda m: m.cd.eject()), CD["CD_ENODISC"])


@cases(("an empty channel", "7", None), ("a disk", "CH_HDD", None),
       ("the timer", "CH_TIMER", CH_TIMER), ("HID", "CH_HID", CH_HID),
       ("the display", "CH_DISPLAY", CH_DISPLAY), ("channel 0", "0", None))
def test_channels_that_are_not_drives(label, channel, refused):
    """A disk answers command 8 with zeros and an empty channel with
    0xFFFFFFFF, so neither passes for a drive -- and the probe has to be
    sent to find that out.

    The timer, HID and the display are different: they are refused BEFORE
    anything is sent, the rule fs.c follows for the same reason. Command 8
    means nothing on any of the three today, so judging by the outcome
    alone cannot see the rule -- with the guard deleted, every one of them
    still answers "not a drive", and an earlier version of this test passed
    that way. So for those three it is asserted as nothing reaching the
    device at all."""
    sent = []
    statuses = []

    def watch(machine):
        if refused is None:
            return
        device = machine.io_controller.channels[refused]
        original = device.callback

        def recording(read_write, command, length, address, data):
            sent.append(command)
            return original(read_write, command, length, address, data)

        device.callback = recording

    with disks() as d:
        disc = raw_disc(d, data=b"x" * 64)
        expect(run_machine(program(f"""
            cd_info_t info;
            unsigned char buf[8];
            if (cd_present({channel}) != 0) return -1;
            if (cd_has_fs({channel}) != 0) return -2;
            if (cd_generation({channel}) != 0u) return -3;
            if (cd_read({channel}, 0u, buf, 8u) != CD_ENODEV) return -4;
            return cd_info({channel}, &info);"""), formatted(d), disc=disc,
            before=watch, after=lambda m: statuses.append(timer_zero_status(m))),
            CD["CD_ENODEV"])
    assert statuses == [0], f"{label}: timer 0 was started"
    assert sent == [], f"{label}: commands reached a device that is known not to be a drive: {sent}"


# --- filesystems on discs ------------------------------------------------------------

def test_has_fs_tells_an_image_from_a_raw_disc():
    with disks() as d:
        image = disc_image(d, label="DISC")
        expect(run_fs(program("""
            char label[16];
            if (cd_has_fs(CH_CD) != 1) return -1;
            r = cd_label(CH_CD, label, 16u);
            if (r != 4) return r < 0 ? r : -2;
            if (strcmp(label, "DISC") != 0) return -3;
            return 0;"""), blank(d), disc=image), 0)

        disc = raw_disc(d, data=pattern(4096))
        expect(run_fs(program("""
            char label[16];
            if (cd_has_fs(CH_CD) != 0) return -1;
            return cd_label(CH_CD, label, 16u);"""), blank(d), disc=disc), CD["CD_ENOFS"])


def test_a_label_is_cut_the_way_strlcpy_cuts():
    with disks() as d:
        image = disc_image(d, label="DISC")
        expect(run_fs(program("""
            char out[3];
            r = cd_label(CH_CD, out, 3u);
            if (strcmp(out, "DI") != 0) return -1;
            return r;                        /* the length it TRIED to copy */"""),
               blank(d), disc=image), 4)


# --- saving ---------------------------------------------------------------------------

def test_save_copies_a_raw_disc_under_its_own_name():
    with disks() as d:
        disc = raw_disc(d, "prog.bin", pattern(10000))
        disk = formatted(d)
        expect(run_fs(program("""
            TRY(fs_mount(CH_HDD));
            return cd_save(CH_CD, NULL);"""), disk, disc=disc), 10000)
        with host(disk) as img:
            assert img.read_file("/prog.bin") == pattern(10000)
        clean(disk)


@cases(("an exact window", 4096), ("windows and a tail", 12345), ("an empty disc", 0))
def test_save_to_a_path(label, size):
    with disks() as d:
        disc = raw_disc(d, "whatever.bin", pattern(size))
        disk = formatted(d)
        expect(run_fs(program("""
            TRY(fs_mount(CH_HDD));
            TRY(fs_mkdir("/copies"));
            return cd_save(CH_CD, "/copies/x.bin");"""), disk, disc=disc), size)
        with host(disk) as img:
            assert img.read_file("/copies/x.bin") == pattern(size), label
        clean(disk)


def test_saving_onto_the_disc_itself_passes_fs_erofs_through():
    """With only the disc mounted, the current volume IS the disc. The
    refusal comes from fs.c, and has to arrive as FS_EROFS, not as some
    CD code it happens to share a number with."""
    with disks() as d:
        image = disc_image(d)
        before = image.read_bytes()
        expect(run_fs(program("""
            TRY(fs_mount(CH_CD));
            return cd_save(CH_CD, "copy.bin");"""), blank(d), disc=image), FS["FS_EROFS"])
        assert image.read_bytes() == before


def test_a_save_that_fills_the_disk_leaves_nothing_behind():
    """A copy quietly shorter than its disc looks complete to everything
    that opens it later, so a full disk removes it -- and every block it
    took comes back."""
    with disks() as d:
        disc = raw_disc(d, "huge.bin", pattern(400 * 1024))
        disk = formatted(d, size=256 * 1024)
        with host(disk) as img:
            free_before = img.free_count()
        expect(run_fs(program("""
            TRY(fs_mount(CH_HDD));
            return cd_save(CH_CD, "huge.bin");"""), disk, disc=disc), FS["FS_ENOSPC"])
        with host(disk) as img:
            assert "huge.bin" not in {e.name for e in img.listdir("/")}, "partial copy left"
            assert img.free_count() == free_before, "blocks were not given back"
        clean(disk)


def test_a_disc_swapped_during_a_save_is_caught_and_the_copy_removed():
    """The front ends can eject at any moment. The swap is made from inside
    the device callback, after the second READ, which is exactly as sudden
    as a button press between two guest instructions."""
    with disks() as d:
        first = raw_disc(d, "first.bin", pattern(10000))
        second = raw_disc(d, "second.bin", bytes(10000))
        disk = formatted(d)
        reads = []

        def wrap(machine):
            channel = machine.io_controller.channels[CH_CD]
            original = channel.callback

            def swapping(read_write, command, length, address, data):
                reply = original(read_write, command, length, address, data)
                if command == 2:
                    reads.append(address)
                    if len(reads) == 2:
                        machine.cd.insert(second)
                return reply

            channel.callback = swapping

        got = run_machine(program("""
            TRY(fs_mount(CH_HDD));
            return cd_save(CH_CD, "copy.bin");"""), disk, disc=first, before=wrap)

        assert len(reads) >= 2, f"the swap never happened ({len(reads)} reads)"
        expect(got, CD["CD_ECHANGED"])
        with host(disk) as img:
            assert "copy.bin" not in {e.name for e in img.listdir("/")}, "mixed copy left"
        clean(disk)


def test_strerror_names_both_kinds_of_error():
    with disks() as d:
        expect(run_fs(program("""
            if (strcmp(cd_strerror(0), "ok") != 0) return -1;
            if (strcmp(cd_strerror(CD_ENODISC), "no disc in the drive") != 0) return -2;
            if (strcmp(cd_strerror(CD_ECHANGED), "the disc was changed") != 0) return -3;
            if (strcmp(cd_strerror(FS_ENOSPC), fs_strerror(FS_ENOSPC)) != 0) return -4;
            if (strcmp(cd_strerror(FS_EROFS), "read-only disk") != 0) return -5;
            return 0;"""), blank(d)), 0)


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "<pigeon/cd.h> on the guest (lib/pigeon/cd.c)"))
