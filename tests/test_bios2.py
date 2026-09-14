"""The two-stage BIOS: stage 1, the firmware device, and bios2.

Phases 1 to 3 of docs/os_cd.md. The BIOS has 1 KB and no room for a font,
so it loads a second stage, bios2, off a read-only HDD on channel 7 with
one READ_DMA, and jumps to it.

Stage 1 is tested with a tiny stand-in for bios2 -- NOPs and a sentinel --
which proves it ran by what it leaves in A. What must NOT change matters
as much. With no bios2 the BIOS boots channel 1 exactly as before, and
every test that builds a Machine without one boots that way. So does a
bios2 that is empty, claims to be too big, or arrives short.

The real bios2, firmware/bios2.c, is tested on a Machine with keys pushed
through HID and its screen read back as text, with the font reader
tests/test_files.py already has. Disks and discs that can boot carry a
stand-in boot sector, which proves it ran, and from which channel, by
what it leaves in A.

Both stages are built here from their sources rather than taken from
build/, which only the launcher rebuilds: these tests are about the
sources.

    python3 tests/test_bios2.py      (or: python3 -m pytest tests/)
"""
import contextlib
import functools
import io
import logging
import struct
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, real_clock, run_module                     # noqa: E402
from assembler.assembler import Assembler, assemble_file              # noqa: E402
from emulator.cli import build_bios2_if_stale, in_program, second_stage  # noqa: E402
from emulator.config import load_config                               # noqa: E402
from emulator.devices.hdd import (                                    # noqa: E402
    CMD_GET_SIZE, CMD_READ, CMD_READ_DMA, CMD_TRUNCATE, CMD_WRITE, CMD_WRITE_DMA,
    DMA_REFUSED, HDD)
from emulator.devices.timer import CMD_STATUS, STATUS_STOPPED         # noqa: E402
from emulator.instruction_set import NONE_REG, encode                 # noqa: E402
from emulator.machine import Machine                                  # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    BIOS2_LOAD_ADDR, BIOS2_MAX, BIOS_MAX, BOOT_BLOCK, BOOT_CHANNEL, BOOT_CODE,
    BOOT_LOAD_ADDR, BOOT_RECORD, BOOT_SIGNATURE, CH_BIOS2, CH_CD, CH_HDD, CH_USERPROG,
    DISPLAY_H, DISPLAY_SIZE, IO_START, PROGRAM_LOAD_ADDR, PROGRAM_MAX_SIZE, STACK_TOP,
    IOHeader)
from emulator.programs import Program                                 # noqa: E402
from emulator.ram import RAM                                          # noqa: E402
from pfs import PgfsImage                                             # noqa: E402
from test_files import ROW, text_at                                   # noqa: E402

# An empty channel 1, an empty channel 7 and a refused transfer each log a
# warning, and these tests do all three on purpose.
for _name in ("emulator.machine", "emulator.io_controller", "emulator.devices.hdd"):
    logging.getLogger(_name).setLevel(logging.ERROR)

_BUILD = tempfile.TemporaryDirectory()
BIOS = Path(_BUILD.name) / "bios.bin"
with contextlib.redirect_stdout(io.StringIO()):
    assemble_file(REPO_ROOT / "firmware" / "bios.asm", BIOS, quiet=True)
    BIOS2 = Program(name="bios2", source=REPO_ROOT / "firmware" / "bios2.c",
                    binary=Path(_BUILD.name) / "bios2.bin",
                    origin="BIOS2_LOAD_ADDR").ensure_built(quiet=True).read_bytes()

# bios2's countdown, read from its source so these tests follow a change to it.
COUNTDOWN_MS = next(int(line.split()[2].rstrip("u"))
                    for line in (REPO_ROOT / "firmware" / "bios2.c").read_text().splitlines()
                    if line.startswith("#define COUNTDOWN_MS"))
COUNTDOWN_S = -(-COUNTDOWN_MS // 1000)      # as bios2 shows it: whole seconds, rounded up

PROGRAM = 0xC0FFEE              # what a channel-1 program leaves in A
STAGE2 = 0xB1052                # what the stand-in bios2 leaves in A
SECTOR = 0x5EC7000              # the stand-in boot sector leaves this plus its channel
WINDOW = 4096
WINDOW_BASE = IO_START + IOHeader.USABLE_AFTER
SMALL_RAM = 1 << 18             # a power of two past PROGRAM_LOAD_ADDR, for device tests
MiB = 1 << 20

# Keys, in the pigeon keycode space (emulator/devices/keycodes.py).
ESC, ENTER, UP, DOWN = 0x1B, 0x0D, 0x82, 0x83

# bios2's rows, as firmware/bios2.c lays them out.
SCREEN_ROWS = DISPLAY_H // ROW
R_TITLE, R_RAM, R_PROGRAM, R_DISK, R_CD, R_STATUS, R_KEYS = 0, 1, 3, 4, 5, 7, 11
COUNTDOWN_KEYS = "ESC menu    ENTER boot now"
MENU_KEYS = "UP DOWN choose   ENTER boot"


def image(nops, sentinel):
    """NOPs, then the sentinel into A, then HALT. The same bytes run at
    any address, as bios2 or as a program, and the sentinel only arrives
    if the last instruction was loaded."""
    return (encode("NOP") * nops + encode("MOV", dst=0, src1=NONE_REG, imm=sentinel)
            + encode("HALT"))


def recorder(commands, answer):
    def recording(read_write, command, length, address, data):
        commands.append(command)
        return answer(read_write, command, length, address, data)
    return recording


def claims_size(size):
    def fake(real):
        def answer(read_write, command, length, address, data):
            if command == CMD_GET_SIZE:
                return size.to_bytes(8, "little")
            return real(read_write, command, length, address, data)
        return answer
    return fake


def reports_moved(value_for):
    """The transfer really happens; what comes back is value_for(bytes moved)."""
    def fake(real):
        def answer(read_write, command, length, address, data):
            reply = real(read_write, command, length, address, data)
            if command == CMD_READ_DMA:
                return struct.pack("<I", value_for(struct.unpack("<I", reply)[0]))
            return reply
        return answer
    return fake


# --- stage 1 ----------------------------------------------------------------------

def boot(program=None, bios2=None, fakes=None, max_steps=8_000_000):
    """Power on a Machine with a stand-in bios2 and run it to HALT.

    fakes maps a channel to fake(real_callback) -> callback, for a test
    that needs a device to misbehave. Every command reaching channel 7 is
    recorded either way."""
    fakes = fakes or {}
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        paths = {}
        for name, data in (("program", program), ("bios2", bios2)):
            if data is not None:
                paths[name] = t / f"{name}.bin"
                paths[name].write_bytes(data)
        machine = Machine(bios_path=str(BIOS), disk_path=str(t / "hdd.img"),
                          program_path=str(paths["program"]) if "program" in paths else None,
                          bios2_path=str(paths["bios2"]) if "bios2" in paths else None)
        sent = []
        try:
            channel = machine.io_controller.channels.get(CH_BIOS2)
            if channel is not None:
                answer = fakes[CH_BIOS2](channel.callback) if CH_BIOS2 in fakes else channel.callback
                channel.callback = recorder(sent, answer)
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(max_steps):
                    if machine.step() == 1:
                        break
            assert machine.cpu.halted, f"never halted (PC={machine.cpu.pc:#x})"
            mem = machine.ram.mem
            return SimpleNamespace(
                a=machine.cpu.reg.read(0), sent=sent,
                registered=CH_BIOS2 in machine.io_controller.channels,
                at_bios2=bytes(mem[BIOS2_LOAD_ADDR:BIOS2_LOAD_ADDR + len(bios2 or b"")]),
                at_program=bytes(mem[PROGRAM_LOAD_ADDR:PROGRAM_LOAD_ADDR + len(program or b"")]))
        finally:
            machine.close()


@cases(("under one window", 100),        # 816 bytes
       ("just over the window", 512),    # 4,112 bytes
       ("100 KB", 12_500),
       ("1 MB", 131_000))
def test_the_bios_loads_bios2_whole_and_jumps_to_it(label, nops):
    """One GET_SIZE and one READ_DMA, whatever the size: DMA has no 4 KB
    window, so there is no loop to get wrong."""
    bios2 = image(nops, STAGE2)
    r = boot(bios2=bios2)
    assert r.a == STAGE2, f"{label}: A={r.a:#x}, want {STAGE2:#x}"
    assert r.at_bios2 == bios2, f"{label}: bios2 in RAM differs from the file"
    assert r.sent == [CMD_GET_SIZE, CMD_READ_DMA], f"{label}: channel 7 got {r.sent}"


def test_bios2_comes_before_a_program_on_channel_1():
    """bios2 decides what boots, so the BIOS must not load the program too."""
    program = image(10, PROGRAM)
    r = boot(program=program, bios2=image(10, STAGE2))
    assert r.a == STAGE2, f"A={r.a:#x}: the program ran instead of bios2"
    assert r.at_program == bytes(len(program)), "the BIOS loaded the program as well"


@cases(("no firmware device", None), ("an empty bios2 file", b""))
def test_without_a_bios2_the_program_on_channel_1_boots_as_before(label, bios2):
    """700 NOPs is 5,616 bytes, over one IO window -- test_loader.py's old
    cliff -- so the channel-1 loader's chunk loop runs as well."""
    program = image(700, PROGRAM)
    r = boot(program=program, bios2=bios2)
    assert r.a == PROGRAM, f"{label}: A={r.a:#x}, want {PROGRAM:#x}"
    assert r.at_program == program, f"{label}: the program did not load whole"
    if bios2 is None:
        assert not r.registered, "channel 7 is registered with no bios2 to put on it"
    else:
        assert r.sent == [CMD_GET_SIZE], f"{label}: channel 7 got {r.sent}"


@cases(("a size over BIOS2_MAX", claims_size(BIOS2_MAX + 1), [CMD_GET_SIZE]),
       ("a size with the top bit set", claims_size(0x80000000), [CMD_GET_SIZE]),
       ("a short transfer", reports_moved(lambda n: n - 4), [CMD_GET_SIZE, CMD_READ_DMA]),
       ("a refused transfer", reports_moved(lambda n: DMA_REFUSED),
        [CMD_GET_SIZE, CMD_READ_DMA]))
def test_a_bios2_that_cannot_be_trusted_falls_back_to_channel_1(label, fake, sent):
    """Half a bios2 must never run. The top-bit case pins the size check
    as unsigned: read as signed, 0x80000000 is negative, passes as small,
    and the BIOS would go on to send READ_DMA."""
    program = image(10, PROGRAM)
    r = boot(program=program, bios2=image(10, STAGE2), fakes={CH_BIOS2: fake})
    assert r.a == PROGRAM, f"{label}: A={r.a:#x}, want {PROGRAM:#x}"
    assert r.sent == sent, f"{label}: channel 7 got {r.sent}, want {sent}"


def test_the_bios_still_fits_in_its_kilobyte():
    size = BIOS.stat().st_size
    assert size <= BIOS_MAX, f"the BIOS is {size} bytes, over {BIOS_MAX}"


# --- bios2: a Machine with keys going in and rows coming out -------------------------

@functools.lru_cache(maxsize=None)
def stand_in_sector():
    """A boot sector that proves it ran, and from where: it leaves
    SECTOR + the channel bios2 wrote to BOOT_CHANNEL in A, and halts."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sector.asm"
        path.write_text(f".ORG BOOT_ENTRY\n    MOV B #BOOT_CHANNEL\n    MRW A B\n"
                        f"    ADD A A #{SECTOR}\n    HALT\n")
        return Assembler(str(path)).assemble()


def bootable_image(path, label="PIGEONOS", signed=True, size=MiB):
    """A PigeonFS image with the stand-in boot sector in block 0. Written
    after pfs.py is done with it, because its superblock writes rebuild
    block 0 and would erase the sector."""
    PgfsImage.mkfs(path, size, label=label).close()
    raw = bytearray(path.read_bytes())
    if signed:
        struct.pack_into("<I", raw, BOOT_RECORD, BOOT_SIGNATURE)
    code = stand_in_sector()
    raw[BOOT_CODE:BOOT_CODE + len(code)] = code
    path.write_bytes(raw)
    return path


class Power:
    """The real bios2 on a Machine."""

    def __init__(self, folder, program=None, disk=None, disc=None, fakes=None, keys=()):
        folder = Path(folder)
        firmware = folder / "bios2.bin"
        firmware.write_bytes(BIOS2)
        program_path = None
        if program is not None:
            program_path = folder / "program.bin"
            program_path.write_bytes(program)
        self.machine = Machine(bios_path=str(BIOS), bios2_path=str(firmware),
                               program_path=str(program_path) if program_path else None,
                               disk_path=str(disk or folder / "hdd.img"))
        if disc is not None:
            self.machine.cd.root = None
            self.machine.cd.insert(disc)
        self.sent1 = []
        channel = self.machine.io_controller.channels.get(CH_USERPROG)
        if channel is not None:
            fake = (fakes or {}).get(CH_USERPROG)
            channel.callback = recorder(
                self.sent1, fake(channel.callback) if fake else channel.callback)
        for code in keys:
            self.key(code)

    def key(self, code):
        self.machine.hid.push_key(code, True)
        self.machine.hid.push_key(code, False)

    def run(self, steps=8_000_000):
        """Run up to `steps` instructions; True once it has halted."""
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(steps):
                if self.machine.step() == 1:
                    return True
        return False

    def run_until(self, wanted, steps=6_000_000, every=20_000):
        """Run until wanted(rows) holds on two looks in a row, `every`
        instructions apart. One look is not enough: it can catch the
        screen halfway through a redraw, a status line cleared and half
        written. False if it halted first, or never settled."""
        previous = None
        for _ in range(0, steps, every):
            if self.run(every):
                return False
            rows = self.rows()
            if rows == previous and wanted(rows):
                return True
            previous = rows
        return False

    def press(self, *codes, steps=1_000_000):
        for code in codes:
            self.key(code)
        return self.run(steps)

    def rows(self):
        fb = self.machine.display_io.snapshot()
        return [text_at(fb, 1 + r * ROW) for r in range(SCREEN_ROWS)]

    @property
    def a(self):
        return self.machine.cpu.reg.read(0)


@contextlib.contextmanager
def power_on(folder, **kw):
    power = Power(folder, **kw)
    try:
        yield power
    finally:
        power.machine.close()


def status_is(text):
    return lambda rows: rows[R_STATUS] == text


# --- bios2: the countdown ----------------------------------------------------------

@real_clock
def test_the_boot_screen_lists_the_devices_and_counts_down():
    """Waits for the keys row, which bios2 draws last. Two matching looks
    are not enough on their own here: while bios2 clears the empty keys
    row to BG, the pixels do not change, and the screen looks settled
    with that row still to come."""
    with tempfile.TemporaryDirectory() as t, power_on(t, program=image(100, PROGRAM)) as p:
        assert p.run_until(lambda rows: rows[R_KEYS] == COUNTDOWN_KEYS
                           and rows[R_STATUS].startswith("Booting")), p.rows()
        rows = p.rows()
    assert rows[R_TITLE] == "PIGEON BIOS", rows
    assert rows[R_RAM] == "128 MB RAM", rows
    assert rows[R_PROGRAM] == "  Program    816 bytes", rows
    assert rows[R_DISK] == "  Hard disk  no boot sector", rows
    assert rows[R_CD] == "  CD         no disc", rows
    assert rows[R_STATUS] in (f"Booting Program in {COUNTDOWN_S}",
                              f"Booting Program in {COUNTDOWN_S - 1}"), rows


@real_clock
def test_with_no_key_the_first_bootable_device_boots_when_the_countdown_ends():
    with tempfile.TemporaryDirectory() as t, power_on(t, program=image(10, PROGRAM)) as p:
        started = time.time()
        # Bounded by the clock, not by a number of instructions, which would
        # cover more or fewer seconds with the speed of the host.
        while not p.run(100_000):
            assert time.time() < started + COUNTDOWN_S + 30, "never booted"
        waited = time.time() - started
        assert p.a == PROGRAM, f"A={p.a:#x}"
    assert waited >= COUNTDOWN_MS / 1000 - 0.1, \
        f"booted after {waited:.2f} s, before the {COUNTDOWN_MS} ms countdown ran out"


@real_clock
def test_enter_during_the_countdown_boots_at_once():
    with tempfile.TemporaryDirectory() as t, \
            power_on(t, program=image(10, PROGRAM), keys=[ENTER]) as p:
        started = time.time()
        assert p.run(), "never booted"
        waited = time.time() - started
        assert p.a == PROGRAM, f"A={p.a:#x}"
    assert waited < 1.5, f"Enter took {waited:.2f} s to boot: the countdown ran anyway"


@cases(("under one window", 100),
       ("over the display ceiling", 17_000),                  # 136 KB
       ("exactly PROGRAM_MAX_SIZE", PROGRAM_MAX_SIZE // 8 - 2))
def test_a_program_on_channel_1_arrives_in_one_transfer(label, nops):
    program = image(nops, PROGRAM)
    with tempfile.TemporaryDirectory() as t, power_on(t, program=program, keys=[ENTER]) as p:
        assert p.run(steps=3_000_000), f"{label}: never booted"
        assert p.a == PROGRAM, f"{label}: A={p.a:#x}"
        loaded = bytes(p.machine.ram.mem[PROGRAM_LOAD_ADDR:PROGRAM_LOAD_ADDR + len(program)])
        assert loaded == program, f"{label}: the program did not load whole"
        assert p.sent1.count(CMD_READ_DMA) == 1 and CMD_READ not in p.sent1, \
            f"{label}: channel 1 got {p.sent1}"


def test_a_program_on_channel_1_finds_channel_1_in_boot_channel():
    """docs/kernel.md Q8. The word still holds what booted before -- here the
    CD, as after the installer restarts -- and a kernel started from
    channel 1 must not take it for the disk it came from."""
    program = (encode("MOV", dst=1, src1=NONE_REG, imm=BOOT_CHANNEL)
               + encode("MRW", dst=0, src1=1) + encode("HALT"))
    with tempfile.TemporaryDirectory() as t, power_on(t, program=program, keys=[ENTER]) as p:
        p.machine.ram.write_word(BOOT_CHANNEL, CH_CD)
        assert p.run(steps=3_000_000), "never booted"
        assert p.a == CH_USERPROG, f"BOOT_CHANNEL holds {p.a}"


def test_the_program_finds_a_blank_screen_empty_queues_and_a_stopped_timer():
    """The menu's keys are the menu's. bios2 read ENTER, but ENTER's two
    edges and an 'x' typed after it are still queued when it hands over,
    and none of them may reach the program."""
    with tempfile.TemporaryDirectory() as t, \
            power_on(t, program=image(10, PROGRAM), keys=[ENTER, ord("x")]) as p:
        assert p.run() and p.a == PROGRAM, "never booted"
        hid, machine = p.machine.hid, p.machine
        assert hid.pop_key() == 0, "a typed key reached the program"
        assert hid.pop_key_event() == b"\x00\x00", "a key edge reached the program"
        status = struct.unpack_from("<I", machine.timer.callback(0, CMD_STATUS, 8, 1,
                                                                 bytearray(8)))[0]
        assert status == STATUS_STOPPED, f"bios2 left timer 1 in status {status}"
        assert machine.display_io.snapshot() == bytes(DISPLAY_SIZE), "the screen was not cleared"
        # A CALL, not a jump: the program runs a few return addresses down.
        assert STACK_TOP - 32 <= machine.cpu.sp < STACK_TOP, f"SP={machine.cpu.sp:#x}"


# --- bios2: the menu ---------------------------------------------------------------

def test_esc_opens_the_menu_and_enter_boots_the_one_chosen():
    """The disk's boot sector runs from BOOT_ENTRY with its whole block 0
    copied to BOOT_LOAD_ADDR and its channel at BOOT_CHANNEL."""
    with tempfile.TemporaryDirectory() as t:
        disk = bootable_image(Path(t) / "disk.img")
        with power_on(t, program=image(10, PROGRAM), disk=disk, keys=[ESC]) as p:
            assert p.run_until(lambda rows: rows[R_STATUS] == "BOOT MENU"
                               and rows[R_KEYS] == MENU_KEYS), p.rows()
            rows = p.rows()
            assert rows[R_PROGRAM] == "> Program    96 bytes", rows
            assert rows[R_DISK] == "  Hard disk  PIGEONOS", rows
            assert rows[R_KEYS] == MENU_KEYS, rows
            assert not p.press(DOWN)
            assert p.rows()[R_DISK] == "> Hard disk  PIGEONOS", p.rows()
            assert p.press(ENTER), "the disk did not boot"
            assert p.a == SECTOR + CH_HDD, f"A={p.a:#x}, want {SECTOR + CH_HDD:#x}"
            mem = p.machine.ram.mem
            assert bytes(mem[BOOT_LOAD_ADDR:BOOT_LOAD_ADDR + BOOT_BLOCK]) == \
                disk.read_bytes()[:BOOT_BLOCK], "block 0 was not copied whole"
            assert struct.unpack_from("<I", mem, BOOT_CHANNEL)[0] == CH_HDD


@real_clock
def test_a_disc_with_a_boot_sector_counts_down_and_boots_from_the_cd():
    with tempfile.TemporaryDirectory() as t:
        disc = bootable_image(Path(t) / "disc.img", label="INSTALL")
        with power_on(t, disc=disc) as p:
            assert p.run_until(status_is(f"Booting CD in {COUNTDOWN_S}")), p.rows()
            assert p.rows()[R_CD] == "  CD         INSTALL", p.rows()
            assert p.press(ENTER), "the disc did not boot"
            assert p.a == SECTOR + CH_CD, f"A={p.a:#x}, want {SECTOR + CH_CD:#x}"


@cases(("nothing on channel 1", lambda d: {}, R_PROGRAM, "Program    none"),
       ("a program over PROGRAM_MAX_SIZE", lambda d: {"program": bytes(PROGRAM_MAX_SIZE + 8)},
        R_PROGRAM, "Program    too big"),
       ("a PigeonFS disk with no boot record",
        lambda d: {"disk": bootable_image(d / "disk.img", signed=False)},
        R_DISK, "Hard disk  no boot sector"),
       ("a bootable disk with no label",
        lambda d: {"disk": bootable_image(d / "disk.img", label="")},
        R_DISK, "Hard disk  bootable"),
       ("an empty drive", lambda d: {}, R_CD, "CD         no disc"),
       ("a disc of raw bytes", lambda d: {"disc": raw_disc(d / "disc.bin")},
        R_CD, "CD         no boot sector"))
def test_each_device_says_what_it_holds(label, prepare, row, detail):
    """Read in the menu, past the "> " that marks the selected row."""
    with tempfile.TemporaryDirectory() as t, \
            power_on(t, keys=[ESC], **prepare(Path(t))) as p:
        assert p.run_until(lambda rows: rows[R_STATUS] in ("BOOT MENU", "Nothing to boot")), \
            f"{label}: {p.rows()}"
        assert p.rows()[row][2:] == detail, f"{label}: {p.rows()[row]!r}"


def raw_disc(path):
    path.write_bytes(bytes(4096))
    return path


def test_with_nothing_to_boot_the_menu_waits_and_notices_a_disc_going_in():
    """No key is pressed after the disc goes in: the drive's generation
    counter moving is what redraws the menu and selects the CD."""
    with tempfile.TemporaryDirectory() as t:
        disc = bootable_image(Path(t) / "disc.img", label="INSTALL")
        with power_on(t) as p:
            assert p.run_until(status_is("Nothing to boot")), p.rows()
            assert not p.run(300_000), "halted with nothing to boot"
            p.machine.cd.root = None
            p.machine.cd.insert(disc)
            assert p.run_until(lambda rows: rows[R_CD] == "> CD         INSTALL"), p.rows()
            assert p.rows()[R_STATUS] == "BOOT MENU", p.rows()
            assert p.press(ENTER), "the disc did not boot"
            assert p.a == SECTOR + CH_CD, f"A={p.a:#x}"


def test_enter_on_a_device_that_cannot_boot_says_so():
    with tempfile.TemporaryDirectory() as t, power_on(t, keys=[ENTER]) as p:
        assert p.run_until(status_is("Program: can't boot")), p.rows()
        assert not p.run(300_000), "halted"


@cases(("a short transfer", reports_moved(lambda n: n - 4)),
       ("a refused transfer", reports_moved(lambda n: DMA_REFUSED)))
def test_a_program_that_arrives_short_is_not_run_and_the_menu_says_so(label, fake):
    """The short case copies the whole program and only reports less, so
    running it anyway would still halt with the sentinel. It must not."""
    with tempfile.TemporaryDirectory() as t, \
            power_on(t, program=image(10, PROGRAM), fakes={CH_USERPROG: fake},
                     keys=[ENTER]) as p:
        assert p.run_until(status_is("Program: load failed")), f"{label}: {p.rows()}"
        assert not p.run(300_000), f"{label}: halted -- the program ran"


# --- the firmware device -------------------------------------------------------

def test_the_machine_refuses_a_bios2_too_big_to_fit():
    """Past BIOS2_MAX a second stage runs into the hardware stack."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        firmware = t / "bios2.bin"
        with open(firmware, "wb") as f:
            f.truncate(BIOS2_MAX)             # sparse: nothing is written
        Machine(bios_path=str(BIOS), disk_path=str(t / "hdd.img"),
                bios2_path=str(firmware)).close()
        with open(firmware, "r+b") as f:
            f.truncate(BIOS2_MAX + 1)
        try:
            Machine(bios_path=str(BIOS), disk_path=str(t / "hdd.img"),
                    bios2_path=str(firmware))
        except ValueError as e:
            assert str(BIOS2_MAX) in str(e), str(e)
        else:
            raise AssertionError("a bios2 over BIOS2_MAX was accepted")


def test_a_missing_bios2_is_an_error_not_a_new_file():
    """An ordinary HDD creates a missing image, as 4 MiB of zeros. That is
    right for a disk and wrong for firmware."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        missing = t / "nowhere" / "bios2.bin"
        try:
            Machine(bios_path=str(BIOS), disk_path=str(t / "hdd.img"),
                    bios2_path=str(missing))
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("a missing bios2 was accepted")
        assert not missing.parent.exists(), "a folder was created for it"


@cases(("WRITE", 1, CMD_WRITE, b""),
       ("WRITE sent with R/W 0", 0, CMD_WRITE, b""),
       ("TRUNCATE", 0, CMD_TRUNCATE, b""),
       ("WRITE_DMA", 0, CMD_WRITE_DMA, struct.pack("<I", DMA_REFUSED)))
def test_the_firmware_device_refuses_every_write(label, read_write, command, refusal):
    """Each command is first sent to an ordinary HDD, to show it really
    changes the file there -- TRUNCATE to ADDRESS 0 empties it, WRITE_DMA
    copies RAM over it -- and then to the read-only one."""
    content = bytes(range(256)) * 8
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        for readonly in (False, True):
            path = t / f"bios2-{readonly}.bin"
            path.write_bytes(content)
            ram = RAM(SMALL_RAM)
            ram.mem[PROGRAM_LOAD_ADDR:PROGRAM_LOAD_ADDR + 64] = b"\xAA" * 64
            struct.pack_into("<II", ram.mem, WINDOW_BASE, PROGRAM_LOAD_ADDR, 64)
            device = HDD(path, ram=ram, readonly=readonly)
            try:
                reply = device.callback(read_write, command, 64, 0, bytearray(b"\x55" * 64))
            finally:
                device.close()
            if not readonly:
                assert path.read_bytes() != content, f"{label} changes nothing even on a disk"
                continue
            assert reply == refusal, f"{label}: answered {reply!r}, want {refusal!r}"
            assert path.read_bytes() == content, f"{label} changed the firmware file"


def test_the_firmware_device_still_reads():
    content = bytes(range(256)) * 20
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "bios2.bin"
        path.write_bytes(content)
        ram = RAM(SMALL_RAM)
        device = HDD(path, ram=ram, readonly=True)
        try:
            assert device.callback(0, CMD_GET_SIZE, 8, 0, bytearray(8)) == \
                len(content).to_bytes(8, "little")
            assert device.callback(0, CMD_READ, 100, 1000, bytearray(100)) == content[1000:1100]
            struct.pack_into("<II", ram.mem, WINDOW_BASE, PROGRAM_LOAD_ADDR, len(content))
            assert device.callback(0, CMD_READ_DMA, 8, 0, bytearray(8)) == \
                struct.pack("<I", len(content))
        finally:
            device.close()
        assert bytes(ram.mem[PROGRAM_LOAD_ADDR:PROGRAM_LOAD_ADDR + len(content)]) == content


# --- the launcher ----------------------------------------------------------------

def test_the_launcher_uses_a_bios2_only_when_there_is_one():
    """With auto_build off nothing is built. The configured file is
    optional; one named with --bios2 is not."""
    with tempfile.TemporaryDirectory() as t:
        firmware = Path(t) / "bios2.bin"
        config = load_config().override(bios2_binary=str(firmware), auto_build=False)
        assert second_stage(config, explicit=False) is None
        try:
            second_stage(config, explicit=True)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("--bios2 naming a missing file was accepted")
        firmware.write_bytes(image(1, STAGE2))
        assert second_stage(config, explicit=False) == firmware
        assert second_stage(config, explicit=True) == firmware


def test_the_launcher_builds_bios2_from_its_source():
    """As it builds the BIOS: when the build is missing or out of date.
    A file named with --bios2 is never built over."""
    with tempfile.TemporaryDirectory() as t:
        firmware = Path(t) / "bios2.bin"
        config = load_config().override(bios2_binary=str(firmware), auto_build=True)
        try:
            second_stage(config, explicit=True)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("--bios2 naming a missing file was accepted")
        assert not firmware.exists(), "--bios2 built over the file it named"
        with contextlib.redirect_stdout(io.StringIO()):
            assert second_stage(config, explicit=False) == firmware
        assert firmware.read_bytes() == BIOS2, "the launcher's build differs from this file's"
        assert build_bios2_if_stale(config) is False, "rebuilt a build that was up to date"


@cases(("just below the program", PROGRAM_LOAD_ADDR - 8, False),
       ("its first instruction", PROGRAM_LOAD_ADDR, True),
       ("its last instruction", PROGRAM_LOAD_ADDR + PROGRAM_MAX_SIZE - 8, True),
       ("just past it", PROGRAM_LOAD_ADDR + PROGRAM_MAX_SIZE, False),
       ("bios2", BIOS2_LOAD_ADDR, False))
def test_the_debugger_steps_only_inside_the_program(label, pc, steps):
    """It used to step anywhere at or above PROGRAM_LOAD_ADDR, which would
    now single-step through the whole of bios2."""
    assert in_program(pc) == steps, f"{label} ({pc:#x})"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the two-stage BIOS"))
