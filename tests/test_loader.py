"""The BIOS must load programs larger than one DMA window.

The loader used to issue a single 4096-byte read and stop. Anything
bigger was silently truncated: the tail never reached RAM, the CPU ran
into whatever followed, and there was no error. A C program with a
stdlib and a font table exceeds 4 KB immediately.

There was a second, much higher ceiling behind it. The boot progress bar
painted one screen word per word copied, and the clear afterwards wiped
the same range -- neither bounded by the framebuffer. Past
PROGRAM_LOAD_ADDR - DISPLAY_START bytes the bar ran off the screen into
the load area and the clear zeroed the front of the program. That is
what DISPLAY_CEILING below is about, and it is why these tests compare
the whole image rather than only the tail.

Every check runs twice (docs/os_cd.md, phase 2): once through stage 1's
own loader, the one described above, and once through bios2, which loads
the whole program with one READ_DMA instead. Both must hand over the same
program, whole, on a blank screen.

    python3 tests/test_loader.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.instruction_set import NONE_REG, encode                 # noqa: E402
from emulator.machine import Machine                                  # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    DISPLAY_SIZE, DISPLAY_START, IO_SIZE, IOHeader, PROGRAM_LOAD_ADDR)
from emulator.programs import Program                                 # noqa: E402

BIOS = REPO_ROOT / "build" / "bios.bin"
WINDOW = IO_SIZE - IOHeader.USABLE_AFTER      # 4096: one DMA transfer
SENTINEL = 0xC0FFEE

# The size at which an unclamped progress bar first reaches the program
# it is loading. Derived, not typed: it moves with the display geometry.
DISPLAY_CEILING = PROGRAM_LOAD_ADDR - DISPLAY_START

# bios2 is built from its source, into a folder that lasts for the run:
# build/bios2.bin is only whatever the launcher last built.
_BUILD = tempfile.TemporaryDirectory()
with contextlib.redirect_stdout(io.StringIO()):
    BIOS2 = Program(name="bios2", source=REPO_ROOT / "firmware" / "bios2.c",
                    binary=Path(_BUILD.name) / "bios2.bin",
                    origin="BIOS2_LOAD_ADDR").ensure_built(quiet=True)
ROUTES = (("stage 1", None), ("bios2", BIOS2))


def program_of(instruction_count):
    """NOPs, then set A to the sentinel and halt. The sentinel only
    arrives if the LAST instruction was loaded."""
    body = encode("NOP") * instruction_count
    return body + encode("MOV", dst=0, src1=NONE_REG, imm=SENTINEL) + encode("HALT")


def boot(program_bytes, bios2=None, max_steps=8_000_000):
    """Power on with the program on channel 1 -- and bios2 on channel 7,
    when given -- and run to HALT."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "prog.bin"
        path.write_bytes(program_bytes)
        machine = Machine(bios_path=str(BIOS), program_path=str(path),
                          bios2_path=str(bios2) if bios2 else None)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(max_steps):
                    if machine.step() == 1:
                        break
            mem = machine.ram.mem
            end = PROGRAM_LOAD_ADDR + len(program_bytes)
            return SimpleNamespace(
                a=machine.cpu.reg.read(0), halted=machine.cpu.halted,
                loaded=bytes(mem[PROGRAM_LOAD_ADDR:end]),
                after=bytes(mem[end:end + 64]),
                screen=bytes(mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE]))
        finally:
            machine.close()


def differs_at(loaded, program):
    return next(i for i in range(len(program)) if loaded[i] != program[i])


@cases(
    ("under one window",      100),      # 816 bytes
    ("just under the window", 510),      # 4096 bytes exactly
    ("just over the window",  512),      # 4112 bytes -- the old cliff
    ("two windows",           900),      # 7216 bytes
    ("three windows",        1500),      # 12016 bytes
)
def test_program_loads_whole(label, nops):
    program = program_of(nops)
    for route, bios2 in ROUTES:
        r = boot(program, bios2)
        assert r.loaded == program, (
            f"{label} via {route} ({len(program)} bytes): image differs from the file "
            f"at byte {differs_at(r.loaded, program)}")
        assert r.halted, f"{label} via {route}: never reached HALT"
        assert r.a == SENTINEL, (
            f"{label} via {route} ({len(program)} bytes): A={r.a:#x}, want {SENTINEL:#x} -- "
            f"the final instruction did not survive the load")


def test_the_old_ceiling_is_really_gone():
    """A 5,616-byte program used to be truncated at ~4 KB and halt with a
    garbage value, silently. This is that exact case."""
    program = program_of(700)
    assert len(program) > WINDOW, "test must exceed one window to be meaningful"
    for route, bios2 in ROUTES:
        r = boot(program, bios2)
        assert r.loaded[-8:] == program[-8:], f"via {route}: the tail never arrived"
        assert r.a == SENTINEL, f"via {route}: A={r.a:#x}, want {SENTINEL:#x}"


@cases(
    ("just under the display ceiling", DISPLAY_CEILING - 4096),
    ("just over the display ceiling",  DISPLAY_CEILING + 4096),
    ("well over it",                   DISPLAY_CEILING * 3 // 2),
)
def test_a_program_bigger_than_the_screen_loads_whole(label, size):
    """The boot progress bar must not paint into the program.

    user/files.c is 169,076 bytes -- <pigeon/fs.h> plus display, input
    and string. It booted into 43 KB of zeros and ran until a RET found a
    return address nothing had pushed. The bar painted word N of the
    program at DISPLAY_START + 4N, which passes PROGRAM_LOAD_ADDR once
    the program is bigger than the gap between the two, and the clear
    that follows then zeroed everything the bar had touched.
    """
    program = program_of(size // 8 - 2)
    assert len(program) > DISPLAY_SIZE, "must exceed the framebuffer to mean anything"
    for route, bios2 in ROUTES:
        r = boot(program, bios2, max_steps=40_000_000)
        assert r.loaded == program, (
            f"{label} via {route} ({len(program)} bytes): image differs from the file at "
            f"byte {differs_at(r.loaded, program)} -- the loader wrote over the program "
            f"it was loading")
        assert r.halted, f"{label} via {route}: never reached HALT"
        assert r.a == SENTINEL, f"{label} via {route}: A={r.a:#x}, want {SENTINEL:#x}"


def test_the_screen_is_clear_when_the_program_starts():
    """A program bigger than the screen must still find a clean
    framebuffer -- and nothing zeroed past the end of it."""
    program = program_of(DISPLAY_CEILING // 8)       # comfortably over
    for route, bios2 in ROUTES:
        r = boot(program, bios2, max_steps=40_000_000)
        assert r.screen == b"\x00" * DISPLAY_SIZE, f"via {route}: the boot bar was left on screen"
        assert r.loaded == program, f"via {route}: the program did not load whole"


def test_exact_window_multiple_terminates():
    """A program that is an exact multiple of the window makes the loader
    ask for one more chunk and get zero bytes back -- it must stop there,
    not loop forever."""
    nops = (WINDOW // 8) - 2                     # exactly 4096 bytes total
    program = program_of(nops)
    assert len(program) == WINDOW
    for route, bios2 in ROUTES:
        r = boot(program, bios2)
        assert r.loaded == program and r.halted and r.a == SENTINEL, f"via {route}"


def test_load_address_is_untouched_below_the_program():
    """The loader must not scribble outside the program area."""
    program = program_of(600)
    for route, bios2 in ROUTES:
        r = boot(program, bios2)
        assert r.after == b"\x00" * 64, f"via {route}: wrote past the end of the program"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "BIOS program loader"))
