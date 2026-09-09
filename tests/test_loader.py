"""The BIOS must load programs larger than one DMA window.

The loader used to issue a single 4096-byte read and stop. Anything
bigger was silently truncated: the tail never reached RAM, the CPU ran
into whatever followed, and there was no error. A C program with a
stdlib and a font table exceeds 4 KB immediately.

    python3 tests/test_loader.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.instruction_set import NONE_REG, encode                 # noqa: E402
from emulator.machine import Machine                                  # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    IO_SIZE, IOHeader, PROGRAM_LOAD_ADDR)

BIOS = REPO_ROOT / "build" / "bios.bin"
WINDOW = IO_SIZE - IOHeader.USABLE_AFTER      # 4096: one DMA transfer
SENTINEL = 0xC0FFEE


def program_of(instruction_count):
    """NOPs, then set A to the sentinel and halt. The sentinel only
    arrives if the LAST instruction was loaded."""
    body = encode("NOP") * instruction_count
    return body + encode("MOV", dst=0, src1=NONE_REG, imm=SENTINEL) + encode("HALT")


def boot(program_bytes, max_steps=8_000_000):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "prog.bin"
        path.write_bytes(program_bytes)
        machine = Machine(bios_path=str(BIOS), program_path=str(path))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(max_steps):
                    if machine.step() == 1:
                        break
            loaded = bytes(machine.ram.mem[PROGRAM_LOAD_ADDR:
                                           PROGRAM_LOAD_ADDR + len(program_bytes)])
            return machine.cpu.reg.read(0), loaded, machine.cpu.halted
        finally:
            machine.close()


@cases(
    ("under one window",      100),      # 816 bytes
    ("just under the window", 510),      # 4096 bytes exactly
    ("just over the window",  512),      # 4112 bytes -- the old cliff
    ("two windows",           900),      # 7216 bytes
    ("three windows",        1500),      # 12016 bytes
)
def test_program_loads_whole(label, nops):
    program = program_of(nops)
    result, loaded, halted = boot(program)

    assert loaded == program, (
        f"{label} ({len(program)} bytes): image differs from the file at byte "
        f"{next(i for i in range(len(program)) if loaded[i] != program[i])}")
    assert halted, f"{label}: never reached HALT"
    assert result == SENTINEL, (
        f"{label} ({len(program)} bytes): A={result:#x}, want {SENTINEL:#x} -- "
        f"the final instruction did not survive the load")


def test_the_old_ceiling_is_really_gone():
    """A 5,616-byte program used to be truncated at ~4 KB and halt with a
    garbage value, silently. This is that exact case."""
    program = program_of(700)
    assert len(program) > WINDOW, "test must exceed one window to be meaningful"
    result, loaded, _ = boot(program)
    assert loaded[-8:] == program[-8:], "the tail never arrived"
    assert result == SENTINEL


def test_exact_window_multiple_terminates():
    """A program that is an exact multiple of the window makes the loader
    ask for one more chunk and get zero bytes back -- it must stop there,
    not loop forever."""
    nops = (WINDOW // 8) - 2                     # exactly 4096 bytes total
    program = program_of(nops)
    assert len(program) == WINDOW
    result, loaded, halted = boot(program)
    assert loaded == program and halted and result == SENTINEL


def test_load_address_is_untouched_below_the_program():
    """The loader must not scribble outside the program area."""
    program = program_of(600)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.bin"
        path.write_bytes(program)
        machine = Machine(bios_path=str(BIOS), program_path=str(path))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(8_000_000):
                    if machine.step() == 1:
                        break
            after = PROGRAM_LOAD_ADDR + len(program)
            tail = bytes(machine.ram.mem[after:after + 64])
            assert tail == b"\x00" * 64, "wrote past the end of the program"
        finally:
            machine.close()


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "BIOS program loader"))
