"""The display device: the scanout base register and the framebuffer fill.

These are the two operations that used to be guest loops over every pixel
-- 87% of the 3D cube's executed instructions. Moving them into the
machine means a bad pointer now reaches RAM at hardware speed, so most of
what follows is about what the device REFUSES to do.

    python3 tests/test_display.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.devices.display_io import (                             # noqa: E402
    CMD_FILL, CMD_GET_BASE, CMD_INFO, CMD_NOP, CMD_SET_BASE, DisplayIO)
from emulator.memory_map import (                                     # noqa: E402
    DISPLAY_H, DISPLAY_SIZE, DISPLAY_START, DISPLAY_W, HEAP_START,
    IO_START, PROGRAM_LOAD_ADDR, RAM_SIZE)
from emulator.ram import RAM                                          # noqa: E402


def device():
    return DisplayIO(RAM(RAM_SIZE))


def call(display, command, address=0, data=b"", read_write=0):
    return display.callback(read_write, command, len(data) or 4, address,
                            bytearray(data))


def word(reply):
    return int.from_bytes(reply[:4], "little")


# --- what the guest can ask ------------------------------------------------

def test_info_reports_the_real_geometry():
    """A program compiled for a different screen size must be able to find
    out, because a fill writes the DEVICE's idea of a screen into a buffer
    the guest sized from its own."""
    reply = call(device(), CMD_INFO)
    assert len(reply) == 12
    assert word(reply) == DISPLAY_W
    assert word(reply[4:]) == DISPLAY_H
    assert word(reply[8:]) == DISPLAY_SIZE


def test_base_starts_at_the_hardware_framebuffer():
    display = device()
    assert display.scanout_base == DISPLAY_START
    assert word(call(display, CMD_GET_BASE)) == DISPLAY_START


def test_setting_a_base_moves_what_the_screen_reads():
    display = device()
    display.ram.mem[HEAP_START:HEAP_START + 4] = b"\xef\xbe\xad\xde"

    assert word(call(display, CMD_SET_BASE, address=HEAP_START)) == 1
    assert display.scanout_base == HEAP_START
    assert display.snapshot()[:4] == b"\xef\xbe\xad\xde"
    assert len(display.snapshot()) == DISPLAY_SIZE


def test_flipping_back_to_the_framebuffer_is_allowed():
    """Both surfaces of a page flip must be reachable, and one of them is
    DISPLAY_START -- which is below PROGRAM_LOAD_ADDR and would otherwise
    be refused."""
    display = device()
    call(display, CMD_SET_BASE, address=HEAP_START)
    assert word(call(display, CMD_SET_BASE, address=DISPLAY_START)) == 1
    assert display.scanout_base == DISPLAY_START


def test_nop_answers():
    assert word(call(device(), CMD_NOP)) == 0


# --- what it refuses -------------------------------------------------------

@cases(
    ("null -- malloc failed and the guest never checked", 0),
    ("unaligned by one", HEAP_START + 1),
    ("unaligned by three", HEAP_START + 3),
    ("the IO header", IO_START),
    ("partway into the framebuffer", DISPLAY_START + 4),
    ("below the program", PROGRAM_LOAD_ADDR - 4),
    ("far past the end of RAM", RAM_SIZE),
    ("one screen short of the end", RAM_SIZE - DISPLAY_SIZE + 4),
)
def test_a_bad_base_is_refused_and_the_old_one_kept(label, base):
    display = device()
    call(display, CMD_SET_BASE, address=HEAP_START)      # a good one first

    with contextlib.redirect_stderr(io.StringIO()):
        assert word(call(display, CMD_SET_BASE, address=base)) == 0, label
    assert display.scanout_base == HEAP_START, (
        f"{label}: the device took {base:#x} and the guest is now drawing "
        f"into a buffer nobody is watching")


def test_the_last_valid_base_is_exactly_one_screen_from_the_end():
    """The boundary the fill guard depends on, pinned from both sides."""
    display = device()
    assert word(call(display, CMD_SET_BASE, address=RAM_SIZE - DISPLAY_SIZE)) == 1
    with contextlib.redirect_stderr(io.StringIO()):
        assert word(call(display, CMD_SET_BASE,
                         address=RAM_SIZE - DISPLAY_SIZE + 4)) == 0


# --- the fill --------------------------------------------------------------

def test_fill_paints_exactly_one_screen():
    display = device()
    call(display, CMD_FILL, address=HEAP_START,
         data=(0xFF00FF00).to_bytes(4, "little"), read_write=1)

    mem = display.ram.mem
    assert mem[HEAP_START:HEAP_START + 4] == b"\x00\xff\x00\xff"
    assert mem[HEAP_START + DISPLAY_SIZE - 4:HEAP_START + DISPLAY_SIZE] \
        == b"\x00\xff\x00\xff"
    assert mem[HEAP_START + DISPLAY_SIZE:HEAP_START + DISPLAY_SIZE + 4] \
        == b"\x00\x00\x00\x00", "the fill ran past the end of the screen"


def test_fill_never_resizes_ram():
    """The one that matters most.

    A slice assignment past the end of a bytearray does not raise and does
    not clamp the write -- it GROWS the bytearray, pushing the address
    space past RAM_SIZE and breaking every `addr & mask` in the machine.
    ram.py carries the same warning from a previous life. One comparison
    in _valid_base is all that stands between a guest pointer and that.
    """
    display = device()
    for base in (RAM_SIZE - 4, RAM_SIZE - DISPLAY_SIZE + 4, RAM_SIZE * 2):
        with contextlib.redirect_stderr(io.StringIO()):
            call(display, CMD_FILL, address=base,
                 data=(0xFFFFFFFF).to_bytes(4, "little"), read_write=1)
        assert len(display.ram.mem) == RAM_SIZE, (
            f"filling at {base:#x} resized RAM to {len(display.ram.mem):,} bytes")


def test_a_refused_fill_writes_nothing():
    display = device()
    before = bytes(display.ram.mem[DISPLAY_START:DISPLAY_START + 64])
    with contextlib.redirect_stderr(io.StringIO()):
        call(display, CMD_FILL, address=DISPLAY_START + 4,   # unaligned
             data=(0xFFFFFFFF).to_bytes(4, "little"), read_write=1)
    assert bytes(display.ram.mem[DISPLAY_START:DISPLAY_START + 64]) == before


def test_changing_colour_repaints():
    """The pattern is cached between calls because disp_clear() passes the
    same colour every frame; a new colour must still take effect."""
    display = device()
    for colour in (0xFF112233, 0xFF112233, 0xFFAABBCC):
        call(display, CMD_FILL, address=HEAP_START,
             data=colour.to_bytes(4, "little"), read_write=1)
        assert display.ram.mem[HEAP_START:HEAP_START + 4] \
            == colour.to_bytes(4, "little")


def test_an_unknown_command_is_survivable():
    with contextlib.redirect_stderr(io.StringIO()):
        assert call(device(), 99) == b""


# --- through a real machine ------------------------------------------------
#
# The bare CPU in test_libs.py has no IO controller, so the display library
# stays on its software path there. These are the tests that exercise the
# hardware path the library actually takes in front of a user.

def boot(program, steps=6_000_000):
    """Run the BIOS, then the program, collecting each scanout base change.

    The BIOS phase is bounded by the WALL CLOCK, not by a step count. It
    spins two real seconds on the timer, so the number of instructions
    that takes is a property of how fast the host interpreter is -- about
    1.9M under CPython and 12.4M under PyPy. A fixed step budget silently
    encodes one of those and fails on anything faster.
    """
    import time
    from emulator.machine import Machine
    machine = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"),
                      program_path=str(REPO_ROOT / "build" / program))
    with contextlib.redirect_stdout(io.StringIO()):
        give_up = time.time() + 30
        while machine.cpu.pc < PROGRAM_LOAD_ADDR:
            for _ in range(100_000):            # chunked: the clock costs more
                if machine.step() == 1 or machine.cpu.pc >= PROGRAM_LOAD_ADDR:
                    break                       # than an instruction does
            assert time.time() < give_up, f"{program}: the BIOS never handed over"

        bases = []
        for _ in range(steps):
            if machine.step() == 1:
                break
            if not bases or machine.display_io.scanout_base != bases[-1]:
                bases.append(machine.display_io.scanout_base)
    machine.close()
    return machine, bases


def test_the_cube_alternates_between_two_surfaces():
    """A page flip, not a one-way move: the base must come back."""
    cube = REPO_ROOT / "build" / "cube.bin"
    if not cube.exists():
        return                                  # nothing built; test_libs pins this
    machine, bases = boot("cube.bin")
    assert len(bases) >= 3, f"the cube never flipped -- bases seen: {bases}"
    assert len(set(bases)) == 2, (
        f"expected exactly two surfaces, saw {sorted(set(bases))}")
    assert DISPLAY_START in bases, "the hardware framebuffer is never scanned out"
    assert bases[0] != bases[1] and bases[1] != bases[2], "it stopped alternating"


def test_a_program_that_never_flips_is_left_alone():
    """screen.asm and friends draw straight at DISPLAY_START and know
    nothing about CH_DISPLAY. They must keep working untouched."""
    screen = REPO_ROOT / "build" / "screen.bin"
    if not screen.exists():
        return
    machine, bases = boot("screen.bin", steps=2_000_000)
    assert machine.display_io.scanout_base == DISPLAY_START
    assert bases == [DISPLAY_START]


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "display device"))
