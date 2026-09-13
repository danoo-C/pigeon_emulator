"""The two-stage BIOS: stage 1, the firmware device, and bios2.

Phases 1 and 2 of docs/os_cd.md. The BIOS has 1 KB and no room for a font,
so it loads a second stage, bios2, off a read-only HDD on channel 7 with
one READ_DMA, and jumps to it.

Stage 1 is tested with a tiny stand-in for bios2 -- NOPs and a sentinel --
which proves it ran by what it leaves in A. What must NOT change matters
as much. With no bios2 the BIOS boots channel 1 exactly as before, and
every test that builds a Machine without one boots that way. So does a
bios2 that is empty, claims to be too big, or arrives short.

The real bios2, firmware/bios2.c, is tested too. In phase 2 it has no
screen: it boots the program on channel 1 with one READ_DMA, and when it
cannot, it halts with the reason in A.

Both are built here from their sources rather than taken from build/,
which only the launcher rebuilds: these tests are about the sources.

    python3 tests/test_bios2.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import logging
import struct
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from assembler.assembler import assemble_file                         # noqa: E402
from emulator.cli import build_bios2_if_stale, in_program, second_stage  # noqa: E402
from emulator.config import load_config                               # noqa: E402
from emulator.devices.hdd import (                                    # noqa: E402
    CMD_GET_SIZE, CMD_READ, CMD_READ_DMA, CMD_TRUNCATE, CMD_WRITE, CMD_WRITE_DMA,
    DMA_REFUSED, HDD)
from emulator.instruction_set import NONE_REG, encode                 # noqa: E402
from emulator.machine import Machine                                  # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    BIOS2_LOAD_ADDR, BIOS2_MAX, BIOS_MAX, CH_BIOS2, CH_USERPROG, DISPLAY_SIZE,
    DISPLAY_START, IO_START, PROGRAM_LOAD_ADDR, PROGRAM_MAX_SIZE, STACK_TOP, IOHeader)
from emulator.programs import Program                                 # noqa: E402
from emulator.ram import RAM                                          # noqa: E402

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

PROGRAM = 0xC0FFEE              # what a channel-1 program leaves in A
STAGE2 = 0xB1052                # what the stand-in bios2 leaves in A
NOTHING_TO_BOOT = 1             # bios2's reasons for halting, from firmware/bios2.c
TRANSFER_FAILED = 2
WINDOW = 4096
WINDOW_BASE = IO_START + IOHeader.USABLE_AFTER
SMALL_RAM = 1 << 18             # a power of two past PROGRAM_LOAD_ADDR, for device tests


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


def boot(program=None, bios2=None, fakes=None, before=None, max_steps=8_000_000):
    """Power on a Machine and run it to HALT.

    fakes maps a channel to fake(real_callback) -> callback, for a test
    that needs a device to misbehave. Every command reaching channel 1 or
    channel 7 is recorded either way. before(machine) runs at power-on,
    before the first instruction."""
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
        sent = {CH_USERPROG: [], CH_BIOS2: []}
        try:
            for number, commands in sent.items():
                channel = machine.io_controller.channels.get(number)
                if channel is not None:
                    answer = fakes[number](channel.callback) if number in fakes else channel.callback
                    channel.callback = recorder(commands, answer)
            if before is not None:
                before(machine)
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(max_steps):
                    if machine.step() == 1:
                        break
            assert machine.cpu.halted, f"never halted (PC={machine.cpu.pc:#x})"
            mem = machine.ram.mem
            return SimpleNamespace(
                a=machine.cpu.reg.read(0), sp=machine.cpu.sp,
                sent=sent[CH_BIOS2], sent1=sent[CH_USERPROG],
                registered=CH_BIOS2 in machine.io_controller.channels,
                at_bios2=bytes(mem[BIOS2_LOAD_ADDR:BIOS2_LOAD_ADDR + len(bios2 or b"")]),
                at_program=bytes(mem[PROGRAM_LOAD_ADDR:PROGRAM_LOAD_ADDR + len(program or b"")]),
                screen=bytes(mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE]))
        finally:
            machine.close()


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


# --- stage 1: loading bios2 ------------------------------------------------------

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


# --- stage 1: falling back -------------------------------------------------------

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


# --- bios2, the real one ----------------------------------------------------------

def dirty_screen(machine):
    """Paint the framebuffer at power-on, so a blank screen afterwards
    proves something cleared it."""
    machine.ram.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE] = b"\xFF" * DISPLAY_SIZE


@cases(("under one window", 100),
       ("over the display ceiling", 17_000),                  # 136 KB
       ("exactly PROGRAM_MAX_SIZE", PROGRAM_MAX_SIZE // 8 - 2))
def test_bios2_boots_a_program_on_channel_1(label, nops):
    """One GET_SIZE and one READ_DMA on channel 1, however big the
    program, then a blank screen and a CALL into it."""
    program = image(nops, PROGRAM)
    r = boot(program=program, bios2=BIOS2, before=dirty_screen, max_steps=2_000_000)
    assert r.a == PROGRAM, f"{label}: A={r.a:#x}, want {PROGRAM:#x}"
    assert r.at_program == program, f"{label}: the program did not load whole"
    assert r.sent1 == [CMD_GET_SIZE, CMD_READ_DMA], f"{label}: channel 1 got {r.sent1}"
    assert r.screen == bytes(DISPLAY_SIZE), f"{label}: the screen was not cleared"
    # C cannot jump, so the program runs on top of two return addresses:
    # the startup code's call to main, and main's call into the program.
    assert r.sp == STACK_TOP - 8, f"{label}: SP={r.sp:#x}, want {STACK_TOP - 8:#x}"


@cases(("channel 1 empty", None),
       ("an empty program file", b""),
       ("a program over PROGRAM_MAX_SIZE", bytes(PROGRAM_MAX_SIZE + 8)))
def test_bios2_halts_when_there_is_nothing_to_boot(label, program):
    """PROGRAM_MAX_SIZE is where bios2's own frame stack begins, at
    HEAP_START: a bigger program would be copied over it."""
    r = boot(program=program, bios2=BIOS2)
    assert r.a == NOTHING_TO_BOOT, f"{label}: A={r.a:#x}, want {NOTHING_TO_BOOT}"
    assert CMD_READ_DMA not in r.sent1, f"{label}: bios2 started a transfer anyway"


@cases(("a short transfer", reports_moved(lambda n: n - 4)),
       ("a refused transfer", reports_moved(lambda n: DMA_REFUSED)))
def test_bios2_does_not_run_a_program_that_arrived_short(label, fake):
    """The short case copies the whole program and only reports less, so
    running it anyway would still reach the sentinel. It must not."""
    r = boot(program=image(10, PROGRAM), bios2=BIOS2, fakes={CH_USERPROG: fake})
    assert r.a == TRANSFER_FAILED, f"{label}: A={r.a:#x}, want {TRANSFER_FAILED}"


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
