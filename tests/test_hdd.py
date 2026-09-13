"""The HDD's DMA commands: READ_DMA (6) and WRITE_DMA (7).

Phase 6 of docs/filesystem.md. Until now every byte to or from a disk
went through the 4 KB IO window and a compiled memcpy -- about 4,350
instructions a block. These two commands let the device copy straight to
and from guest RAM, which makes the device, not the bus, the thing that
can write anywhere. Most of what follows is about where it REFUSES to.

Two facts shaped the commands, and both are pinned down here rather than
left in a comment:

- They are sent with R/W = 0. IOController copies a device's reply back
  into the window only for R/W = 0, so a command sent the other way could
  never say how many bytes it moved. The device reads its parameters out
  of RAM itself.
- An HDD built without RAM answers them like any unknown command, with
  LENGTH zero bytes. That, next to the 4-byte reply of one that knows
  them, is how lib/pigeon/fs.c tells the two apart at run time.

    python3 tests/test_hdd.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import logging
import struct
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.devices.cd import CD                                    # noqa: E402
from emulator.devices.hdd import (                                    # noqa: E402
    CMD_GET_SIZE, CMD_READ, CMD_READ_DMA, CMD_WRITE, CMD_WRITE_DMA, DMA_PARAMS,
    DMA_REFUSED, HDD)
from emulator.io_controller import (                                  # noqa: E402
    ERR_NO_SUCH_CHANNEL, IOChannel, IOController)
from emulator.memory_map import (                                     # noqa: E402
    DISPLAY_START, HEAP_START, IO_START, PROGRAM_LOAD_ADDR, RAM_SIZE, IOHeader)
from emulator.ram import RAM                                          # noqa: E402

WINDOW_BASE = IO_START + IOHeader.USABLE_AFTER
WINDOW = 4096
BUF = HEAP_START                        # an ordinary guest address


def pattern(n):
    return bytes((i * 7 + i // 512) & 0xFF for i in range(n))


class disk:
    """A temporary image on an HDD with (or without) a RAM to copy into."""

    def __init__(self, data=b"", with_ram=True):
        self.data = data
        self.with_ram = with_ram

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "hdd.img"
        self.path.write_bytes(self.data)
        self.ram = RAM(RAM_SIZE)
        self.hdd = HDD(str(self.path), ram=self.ram if self.with_ram else None)
        return self

    def __exit__(self, *exc):
        self.hdd.close()
        self._tmp.cleanup()
        return False

    def dma(self, command, address, count, offset=0):
        """One DMA command, as IOController delivers an R/W = 0 command."""
        struct.pack_into("<II", self.ram.mem, WINDOW_BASE, address, count)
        reply = self.hdd.callback(0, command, DMA_PARAMS, offset, bytearray(DMA_PARAMS))
        assert len(reply) == 4, f"a DMA reply is one word, got {len(reply)} bytes"
        return struct.unpack("<I", reply)[0]


# --- reading -----------------------------------------------------------------------

@cases(("one block", 512), ("exactly a window", WINDOW), ("three windows and a bit", 3 * WINDOW + 100))
def test_a_read_lands_in_ram_as_one_command_of_any_length(label, size):
    with disk(pattern(size)) as d:
        assert d.dma(CMD_READ_DMA, BUF, size) == size, label
        assert bytes(d.ram.mem[BUF:BUF + size]) == pattern(size), label


def test_a_read_past_the_end_is_short_and_the_rest_of_the_range_zero_filled():
    """The guest's buffer is fully defined either way. And RAM keeps its
    size: a slice assignment of fewer bytes than the slice SHRINKS a
    bytearray, which would slide every address above the buffer."""
    with disk(pattern(1000)) as d:
        d.ram.mem[BUF:BUF + 3000] = b"\xAA" * 3000
        assert d.dma(CMD_READ_DMA, BUF, 3000) == 1000
        assert bytes(d.ram.mem[BUF:BUF + 1000]) == pattern(1000)
        assert bytes(d.ram.mem[BUF + 1000:BUF + 3000]) == bytes(2000), "tail not zero-filled"
        assert len(d.ram.mem) == RAM_SIZE, "RAM changed size"


def test_a_read_wholly_past_the_end_moves_nothing_and_zeroes_the_range():
    with disk(pattern(100)) as d:
        d.ram.mem[BUF:BUF + 64] = b"\xAA" * 64
        assert d.dma(CMD_READ_DMA, BUF, 64, offset=5000) == 0
        assert bytes(d.ram.mem[BUF:BUF + 64]) == bytes(64)
        assert len(d.ram.mem) == RAM_SIZE


# --- writing -------------------------------------------------------------------------

def test_a_write_lands_on_the_disk_from_ram():
    with disk(bytes(1024)) as d:
        d.ram.mem[BUF:BUF + 10000] = pattern(10000)
        assert d.dma(CMD_WRITE_DMA, BUF, 10000, offset=512) == 10000
        on_disk = d.path.read_bytes()
        assert on_disk[:512] == bytes(512), "wrote before its offset"
        assert on_disk[512:10512] == pattern(10000)


def test_a_zero_length_transfer_answers_zero_and_touches_nothing():
    """That is the probe lib/pigeon/fs.c sends: a count of 0 at the
    program's own address."""
    with disk(pattern(256)) as d:
        assert d.dma(CMD_READ_DMA, PROGRAM_LOAD_ADDR, 0) == 0
        assert d.dma(CMD_WRITE_DMA, PROGRAM_LOAD_ADDR, 0) == 0
        assert d.path.read_bytes() == pattern(256)


# --- where it refuses --------------------------------------------------------------------

@cases(
    ("the IO header",            IO_START, 16),
    ("the framebuffer",          DISPLAY_START, 16),
    ("one byte below the program", PROGRAM_LOAD_ADDR - 1, 16),
    ("off the end of RAM",       RAM_SIZE - 10, 20),
    ("a 32-bit wrap",            0xFFFFFFF0, 0x20),
)
def test_a_range_outside_guest_ram_is_refused_both_ways(label, address, count):
    with disk(pattern(4096)) as d:
        header = bytes(d.ram.mem[IO_START:IO_START + WINDOW_BASE - IO_START])
        assert d.dma(CMD_READ_DMA, address, count) == DMA_REFUSED, f"read: {label}"
        assert d.dma(CMD_WRITE_DMA, address, count) == DMA_REFUSED, f"write: {label}"
        assert len(d.ram.mem) == RAM_SIZE, f"{label}: RAM changed size"
        assert d.path.read_bytes() == pattern(4096), f"{label}: the disk changed"
        assert bytes(d.ram.mem[IO_START:WINDOW_BASE]) == header, f"{label}: header written"


def test_the_last_bytes_of_ram_are_reachable():
    with disk(pattern(16)) as d:
        assert d.dma(CMD_READ_DMA, RAM_SIZE - 16, 16) == 16
        assert bytes(d.ram.mem[RAM_SIZE - 16:]) == pattern(16)
        assert len(d.ram.mem) == RAM_SIZE


# --- detection -------------------------------------------------------------------------

def test_an_hdd_without_ram_does_not_know_the_commands():
    """The other half of detection: older hardware answers LENGTH zero
    bytes, as it answers any command it has never heard of."""
    with disk(pattern(64), with_ram=False) as d:
        for command in (CMD_READ_DMA, CMD_WRITE_DMA):
            struct.pack_into("<II", d.ram.mem, WINDOW_BASE, BUF, 64)
            assert d.hdd.callback(0, command, DMA_PARAMS, 0, bytearray(DMA_PARAMS)) == bytes(8)
        assert d.path.read_bytes() == pattern(64)


def fire(ram, controller, channel, read_write, command, length, address=0):
    ram.write_word(IO_START + IOHeader.IO_R_W, read_write)
    ram.write_word(IO_START + IOHeader.COMMAND, command)
    ram.write_word(IO_START + IOHeader.LENGTH, length)
    ram.write_word(IO_START + IOHeader.ADDRESS, address)
    ram.write_word(IO_START + IOHeader.IO_CHANNEL, channel)
    controller.update()
    return ram.read_word(IO_START + IOHeader.RETURN_DATA)


def test_detection_has_four_distinguishable_answers_on_the_real_bus():
    """Exactly what fs.c's probe reads: a DMA disk 4, a disk without DMA 8,
    the CD drive 8, an empty channel 0xFFFFFFFF."""
    ram = RAM(RAM_SIZE)
    controller = IOController(ram)
    with tempfile.TemporaryDirectory() as tmp:
        new = Path(tmp) / "new.img"
        old = Path(tmp) / "old.img"
        new.write_bytes(bytes(4096))
        old.write_bytes(bytes(4096))
        dma_disk, plain_disk = HDD(str(new), ram=ram), HDD(str(old))
        cd = CD(root=None, dirs=[])
        controller.register_channel(2, IOChannel(dma_disk.callback, name="HDD"))
        controller.register_channel(3, IOChannel(plain_disk.callback, name="OLD"))
        controller.register_channel(6, IOChannel(cd.callback, name="CD"))
        try:
            def probe(channel):
                struct.pack_into("<II", ram.mem, WINDOW_BASE, PROGRAM_LOAD_ADDR, 0)
                return fire(ram, controller, channel, 0, CMD_READ_DMA, DMA_PARAMS)
            assert probe(2) == 4
            assert probe(3) == 8
            assert probe(6) == 8
            assert probe(7) == ERR_NO_SUCH_CHANNEL
        finally:
            dma_disk.close(); plain_disk.close(); cd.close()


def test_why_the_commands_are_sent_with_rw_zero():
    """With R/W = 1 the transfer still happens and RETURN_DATA still says 4
    -- but the controller never copies the reply back, so the window still
    holds the parameters and the guest cannot learn how much moved. The
    day this test fails, R/W = 1 has become usable."""
    ram = RAM(RAM_SIZE)
    controller = IOController(ram)
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "hdd.img"
        image.write_bytes(pattern(100))
        hdd = HDD(str(image), ram=ram)
        controller.register_channel(2, IOChannel(hdd.callback, name="HDD"))
        try:
            struct.pack_into("<II", ram.mem, WINDOW_BASE, BUF, 500)
            assert fire(ram, controller, 2, 1, CMD_READ_DMA, DMA_PARAMS) == 4
            assert ram.read_word(WINDOW_BASE) == BUF, "the reply was copied back after all"
            assert bytes(ram.mem[BUF:BUF + 100]) == pattern(100)

            struct.pack_into("<II", ram.mem, WINDOW_BASE, BUF, 500)
            assert fire(ram, controller, 2, 0, CMD_READ_DMA, DMA_PARAMS) == 4
            assert ram.read_word(WINDOW_BASE) == 100, "R/W = 0 did not bring the count back"
        finally:
            hdd.close()


# --- nothing else moved ----------------------------------------------------------------------

def test_the_window_commands_are_unchanged():
    with disk(pattern(1000)) as d:
        assert d.hdd.callback(0, CMD_GET_SIZE, 8, 0, bytearray(8)) == (1000).to_bytes(8, "little")
        assert d.hdd.callback(0, CMD_READ, 512, 800, bytearray(512)) == pattern(1000)[800:]
        d.hdd.callback(1, CMD_WRITE, 4, 0, bytearray(b"ABCD"))
        assert d.path.read_bytes()[:4] == b"ABCD"


def test_both_of_a_machines_disks_can_do_dma():
    from emulator.machine import Machine

    with tempfile.TemporaryDirectory() as tmp:
        program = Path(tmp) / "p.bin"
        program.write_bytes(bytes(8))
        machine = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"),
                          program_path=str(program), disk_path=str(Path(tmp) / "hdd.img"))
        try:
            assert machine.hdd.ram is machine.ram
            assert machine.user_prog.ram is machine.ram
        finally:
            machine.close()


# --- what lib/pigeon/fs.c does with them ---------------------------------------------
#
# The library's own tests (test_fs.py, and test_fs_nodma.py for disks
# without DMA) prove the RESULTS are identical either way. These prove the
# fast path is actually TAKEN -- a fast path that silently never runs
# would pass every one of those.

logging.getLogger("emulator.machine").setLevel(logging.ERROR)
logging.getLogger("emulator.devices.hdd").setLevel(logging.ERROR)

FS_PRELUDE = r"""
#include <pigeon/fs.h>
#include <pigeon/mem.h>
#include <pigeon/string.h>
#define TRY(x) if ((r = (x)) < 0) return r
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
"""


def run_recording(body, with_dma=True):
    """Run a program against a freshly formatted disk; return what main()
    returned and every command number the disk was sent, in order."""
    from pfs import PgfsImage
    from test_libs import build
    from emulator.machine import Machine

    commands = []
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "hdd.img"
        PgfsImage.mkfs(image, 1 << 20).close()
        machine = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"),
                          disk_path=str(image))
        if not with_dma:
            machine.hdd.ram = None
        channel = machine.io_controller.channels[2]
        original = channel.callback

        def recording(read_write, command, length, address, data):
            commands.append(command)
            return original(read_write, command, length, address, data)

        channel.callback = recording
        try:
            machine.ram.load_bytes(
                build(FS_PRELUDE + "int main(void) {\n    int r;\n" + body + "\n}\n"),
                PROGRAM_LOAD_ADDR)
            machine.cpu.pc = PROGRAM_LOAD_ADDR
            with contextlib.redirect_stdout(io.StringIO()):
                machine.run(deadline=time.time() + 60)
            assert machine.cpu.halted, "did not halt"
            value = machine.cpu.reg.read(0)
        finally:
            machine.close()
    return (value - (1 << 32) if value & 0x80000000 else value), commands


def test_fs_c_moves_every_block_by_dma_on_a_disk_that_has_it():
    result, commands = run_recording("""
        unsigned char *buf = (unsigned char *)malloc(20000u);
        TRY(fs_mount(CH_HDD));
        fill(buf, 20000u);
        if (fs_save("/big", buf, 20000u) != 20000) return -1;
        memset(buf, 0, 20000u);
        if (fs_load("/big", buf, 20000u) != 20000) return -2;
        return mismatches(buf, 20000u);""")
    assert result == 0, f"the round trip came back wrong ({result})"
    assert CMD_WRITE_DMA in commands, "fs.c never wrote by DMA"
    # GET_SIZE and MEDIA at mount; everything else by DMA. A window READ
    # or WRITE here would mean a block went the slow way on a disk that
    # did not need it to.
    assert set(commands) <= {CMD_GET_SIZE, CMD_READ_DMA, CMD_WRITE_DMA, 8}, sorted(set(commands))


def test_a_buffer_dma_refuses_falls_back_to_the_window():
    """The framebuffer is below the program, so the disk refuses to DMA
    into or out of it -- and loading a picture straight onto the screen is a
    perfectly good thing to do. Both directions have to fall back and still
    get every byte right."""
    result, commands = run_recording("""
        unsigned char *screen = (unsigned char *)DISPLAY_START;
        unsigned char *back = (unsigned char *)malloc(4096u);
        TRY(fs_mount(CH_HDD));
        fill(screen, 4096u);
        if (fs_save("/pic", screen, 4096u) != 4096) return -1;
        if (fs_load("/pic", back, 4096u) != 4096) return -2;
        if (mismatches(back, 4096u) != 0) return -3;
        memset(screen, 0, 4096u);
        if (fs_load("/pic", screen, 4096u) != 4096) return -4;
        return mismatches(screen, 4096u);""")
    assert result == 0, f"a fallback got a byte wrong ({result})"
    assert CMD_WRITE in commands, "the refused write never fell back to the window"
    assert CMD_READ in commands, "the refused read never fell back to the window"


def _after_marker(commands):
    """Commands sent after the last FLUSH -- the programs below call
    fs_sync() as a marker between writing a file and reading it back."""
    last = len(commands) - 1 - commands[::-1].index(5)
    return commands[last + 1:]


LONG_READ = """
    unsigned char *buf = (unsigned char *)malloc(102400u);
    TRY(fs_mount(CH_HDD));
    fill(buf, 102400u);
    if (fs_save("/long", buf, 102400u) != 102400) return -1;
    TRY(fs_sync(CH_HDD));
    memset(buf, 0, 102400u);
    if (fs_load("/long", buf, 102400u) != 102400) return -2;
    return mismatches(buf, 102400u);"""


def test_one_dma_command_carries_a_long_contiguous_run():
    """200 blocks, contiguous on a fresh disk. Through the window that is
    at least 25 transfers of 8 blocks; with DMA the whole run is one."""
    result, commands = run_recording(LONG_READ)
    assert result == 0
    reads = _after_marker(commands)
    assert reads.count(CMD_READ) == 0, "a block of the long read used the window"
    assert 1 <= reads.count(CMD_READ_DMA) <= 3, f"{reads.count(CMD_READ_DMA)} DMA reads for one run"


def test_without_dma_the_same_read_takes_a_window_at_a_time():
    """The contrast, and the proof the run cap still holds where it must:
    no window transfer may carry more than 8 blocks."""
    result, commands = run_recording(LONG_READ, with_dma=False)
    assert result == 0
    reads = _after_marker(commands)
    assert CMD_READ_DMA not in reads and CMD_WRITE_DMA not in commands
    assert reads.count(CMD_READ) >= 25, f"only {reads.count(CMD_READ)} window reads for 200 blocks"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "HDD DMA commands"))
