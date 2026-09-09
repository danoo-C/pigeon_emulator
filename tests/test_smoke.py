"""CPU behaviour plus a regression test for each bug the audit found.

Every test named test_bug_* corresponds to a numbered finding in
REFACTORING.md and reproduces the original failure. They are here so the
fixes stay fixed.

    python3 tests/test_smoke.py      (or: python3 -m pytest tests/)
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run_module                                    # noqa: E402
from emulator.cpu import CPU                                      # noqa: E402
from emulator.instruction_set import NONE_REG, encode             # noqa: E402
from emulator.io_controller import (                              # noqa: E402
    ERR_NO_SUCH_CHANNEL, IOChannel, IOController)
from emulator.memory_map import IO_START, IOHeader, RAM_SIZE, STACK_TOP  # noqa: E402
from emulator.ram import RAM                                      # noqa: E402

TEST_RAM = 0x10000


def run(*instructions, ram_size=TEST_RAM, limit=1000):
    """Assemble a program from encode() calls, run it to HALT, return the CPU."""
    ram = RAM(ram_size)
    ram.load_bytes(b"".join(instructions), 0)
    cpu = CPU(ram)
    for _ in range(limit):
        if cpu.run() == 1:
            return cpu
    raise AssertionError(f"did not halt within {limit} instructions")


# --- basic execution --------------------------------------------------------

def test_arithmetic():
    cpu = run(encode("MOV", dst=0, src1=NONE_REG, imm=5),
              encode("ADD", dst=1, src1=0, src2=NONE_REG, imm=3),
              encode("MUL", dst=2, src1=1, src2=NONE_REG, imm=4),
              encode("HALT"))
    assert (cpu.reg.read(0), cpu.reg.read(1), cpu.reg.read(2)) == (5, 8, 32)


def test_sub_wraps_unsigned():
    """Registers are unsigned 32-bit: 0 - 1 wraps, it does not go negative."""
    cpu = run(encode("SUB", dst=0, src1=0, src2=NONE_REG, imm=1), encode("HALT"))
    assert cpu.reg.read(0) == 0xFFFFFFFF


def test_cmp_and_conditional_jump():
    # A = 0; loop: A += 1; CMP A, 10; JL loop; HALT
    cpu = run(encode("MOV", dst=0, src1=NONE_REG, imm=0),
              encode("ADD", dst=0, src1=0, src2=NONE_REG, imm=1),
              encode("CMP", src1=0, src2=NONE_REG, imm=10),
              encode("JL", src1=NONE_REG, imm=8),
              encode("HALT"))
    assert cpu.reg.read(0) == 10
    assert cpu.zero_flag and not cpu.less_flag


def test_push_pop_is_lifo():
    cpu = run(encode("MOV", dst=0, src1=NONE_REG, imm=11),
              encode("MOV", dst=1, src1=NONE_REG, imm=22),
              encode("PUSH", src1=0), encode("PUSH", src1=1),
              encode("POP", dst=2), encode("POP", dst=3),
              encode("HALT"))
    assert (cpu.reg.read(2), cpu.reg.read(3)) == (22, 11)
    assert cpu.sp == STACK_TOP, "stack not balanced"


def test_memory_round_trip():
    cpu = run(encode("MOV", dst=0, src1=NONE_REG, imm=0x2000),
              encode("MOV", dst=1, src1=NONE_REG, imm=0xDEADBEEF),
              encode("MWW", dst=0, src1=1),
              encode("MRW", dst=2, src1=0),
              encode("HALT"))
    assert cpu.reg.read(2) == 0xDEADBEEF


def test_call_ret_round_trip():
    """New in this refactor: CALL pushes the return address, RET pops it."""
    prog = (encode("CALL", src1=NONE_REG, imm=0x20)
            + encode("MOV", dst=1, src1=NONE_REG, imm=99)
            + encode("HALT")).ljust(0x20, b"\x00")
    prog += encode("MOV", dst=0, src1=NONE_REG, imm=7) + encode("RET")
    ram = RAM(TEST_RAM)
    ram.load_bytes(prog, 0)
    cpu = CPU(ram)
    for _ in range(50):
        if cpu.run() == 1:
            break
    assert (cpu.reg.read(0), cpu.reg.read(1)) == (7, 99)
    assert cpu.sp == STACK_TOP, "CALL/RET left the stack unbalanced"


def test_shifts():
    cpu = run(encode("MOV", dst=0, src1=NONE_REG, imm=5),
              encode("SHL", dst=1, src1=0, src2=NONE_REG, imm=3),
              encode("SHR", dst=2, src1=1, src2=NONE_REG, imm=2),
              encode("SHL", dst=3, src1=0, src2=NONE_REG, imm=40),
              encode("HALT"))
    assert (cpu.reg.read(1), cpu.reg.read(2), cpu.reg.read(3)) == (40, 10, 0)


# --- regressions for the audit's findings -----------------------------------

def test_bug_1_1_word_write_does_not_grow_ram():
    """REFACTORING.md 1.1: slice assignment at the top of memory used to
    resize the bytearray, making RAM larger than RAM_SIZE."""
    ram = RAM(1024)
    ram.write_word(1022, 0xDEADBEEF)
    assert len(ram.mem) == 1024, f"RAM grew to {len(ram.mem)}"
    assert ram.read_word(1022) == 0xDEADBEEF, "wrapped word did not round-trip"


def test_bug_1_1b_ram_size_must_be_power_of_two():
    """self.mask = size - 1 silently depends on it."""
    try:
        RAM(1000)
    except ValueError:
        return
    raise AssertionError("RAM(1000) should have been rejected")


def _io_with(channel_id, callback):
    ram = RAM(RAM_SIZE)
    io = IOController(ram)
    if callback is not None:
        io.register_channel(channel_id, IOChannel(callback, "TEST"))
    return ram, io


def test_bug_1_2_device_returning_none_does_not_crash():
    """REFACTORING.md 1.2: the controller called len() on the callback's
    result unconditionally, so a device returning None took the emulator
    down with a TypeError."""
    ram, io = _io_with(4, lambda rw, cmd, length, addr, data: None)
    ram.write_word(IO_START + IOHeader.IO_CHANNEL, 4)
    io.update()  # must not raise
    assert ram.read_word(IO_START + IOHeader.RETURN_DATA) == 0


def test_bug_1_2b_timer_never_returns_none():
    from emulator.devices.timer import CMD_NOP, Timer
    timer = Timer()
    assert timer.callback(0, CMD_NOP, 4, 0, bytearray()) is not None
    assert timer.callback(0, 99, 4, 0, bytearray()) is not None, "unknown command"


def test_bug_1_3_unknown_channel_reports_instead_of_raising():
    """REFACTORING.md 1.3: an unregistered channel raised a bare KeyError."""
    ram, io = _io_with(4, None)
    ram.write_word(IO_START + IOHeader.IO_CHANNEL, 9)
    io.update()  # must not raise
    assert ram.read_word(IO_START + IOHeader.RETURN_DATA) == ERR_NO_SUCH_CHANNEL
    assert ram.read_word(IO_START + IOHeader.IO_CHANNEL) == 0, "channel not cleared"


def test_bug_1_5_register_beyond_the_cpu_is_rejected():
    """REFACTORING.md 1.5: the assembler encoded A-Z while the CPU has A-F,
    so `MOV G, #1` assembled cleanly and died at runtime."""
    from assembler.assembler import REG_NAME_TO_IDX
    from emulator.memory_map import REGISTER_COUNT
    assert len(REG_NAME_TO_IDX) == REGISTER_COUNT
    assert "G" not in REG_NAME_TO_IDX


def test_io_pending_flag_is_armed_by_both_write_paths():
    """The main loop relies on this instead of polling every instruction.
    A guest can select a channel with a byte write (MW), not just MWW."""
    ram = RAM(RAM_SIZE)
    assert not ram.io_pending
    ram.write_word(IO_START, 1)
    assert ram.io_pending, "write_word into the IO window did not arm the flag"
    ram.io_pending = False
    ram.write_byte(IO_START, 1)
    assert ram.io_pending, "write_byte into the IO window did not arm the flag"
    ram.io_pending = False
    ram.write_word(0x20000, 1)
    assert not ram.io_pending, "a write outside the IO window armed the flag"


def test_io_controller_clears_pending_after_its_own_writes():
    """update() writes to RAM itself, which re-arms io_pending. If it did
    not clear the flag last, every instruction would re-run the command."""
    ram, io = _io_with(4, lambda rw, cmd, length, addr, data: b"\x00\x00\x00\x00")
    ram.write_word(IO_START + IOHeader.IO_CHANNEL, 4)
    io.update()
    assert not ram.io_pending, "controller left io_pending armed -> infinite retrigger"


def test_opcodes_0_to_24_are_unchanged():
    """New instructions must be APPENDED. Inserting one renumbers every
    later opcode and invalidates every assembled binary."""
    from emulator.instruction_set import INSTRUCTIONS_BY_OPCODE as table
    expected = ("NOP MOV ADD SUB MUL DIV OR AND XOR NOT JMP MR MW MRW MWW "
                "CMP JZ JNZ JL JG JLE JGE HALT PUSH POP").split()
    assert [table[op].name for op in range(25)] == expected


def test_display_conversion_swaps_r_and_b():
    from emulator.devices.display_io import DisplayIO
    from emulator.memory_map import DISPLAY_SIZE
    display = DisplayIO(RAM(RAM_SIZE))
    data = bytes(range(256)) * (DISPLAY_SIZE // 256)
    reference = bytearray(len(data))
    for i in range(0, len(data), 4):
        reference[i:i + 4] = (data[i + 2], data[i + 1], data[i], data[i + 3])
    assert display._convert_to_rgba(data) == bytes(reference)


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "emulator smoke tests"))
