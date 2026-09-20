"""Video memory mapped above RAM (docs/gac/phase1_aperture.md).

    python3 tests/test_vram_aperture.py      (or: python3 -m pytest tests/)
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                             # noqa: E402
from emulator.cpu import CPU                                      # noqa: E402
from emulator.instruction_set import NONE_REG, encode             # noqa: E402
from emulator.memory_map import IO_START                          # noqa: E402
from emulator.ram import RAM                                      # noqa: E402

# Small on purpose: the same arithmetic as 128 MB + 16 MB, without
# allocating it in every worker. A 1 GB machine is checked by hand
# (phase1_aperture.md, "As built").
SIZE = 0x10000
VRAM = 0x1000
BASE = SIZE          # the aperture starts where RAM ends


def machine():
    return RAM(SIZE, vram_size=VRAM)


# --- no video memory: the RAM from before -------------------------------------

def test_without_vram_nothing_changes():
    ram = RAM(SIZE)
    assert ram.mask == SIZE - 1 and ram.vram_base == SIZE and len(ram.vram) == 0
    ram.write_word(SIZE + 8, 0x11223344)          # wraps to 8, as it always did
    assert ram.read_word(8) == 0x11223344
    assert ram.vram_dirty is False


def test_without_vram_a_word_at_the_top_still_wraps():
    """REFACTORING.md 1.1, the reason no-VRAM has to keep its mask."""
    ram = RAM(1024)
    ram.write_word(1022, 0xDEADBEEF)
    assert len(ram.mem) == 1024
    assert ram.read_word(1022) == 0xDEADBEEF
    assert ram.mem[0] == 0xAD and ram.mem[1] == 0xDE


# --- the aperture -------------------------------------------------------------

def test_the_space_doubles_and_the_aperture_starts_where_ram_ends():
    ram = machine()
    assert ram.vram_base == SIZE and ram.mask == 2 * SIZE - 1
    assert len(ram.mem) == SIZE and len(ram.vram) == VRAM


def test_a_word_goes_to_video_memory_not_ram():
    ram = machine()
    ram.write_word(BASE + 16, 0xFF00FF00)
    assert ram.read_word(BASE + 16) == 0xFF00FF00
    assert ram.vram[16:20] == bytes((0x00, 0xFF, 0x00, 0xFF))
    assert ram.mem == bytes(SIZE), "the aperture wrote into RAM"


def test_a_byte_goes_to_video_memory_not_ram():
    ram = machine()
    ram.write_byte(BASE + 3, 0x1AB)
    assert ram.read_byte(BASE + 3) == 0xAB and ram.vram[3] == 0xAB
    assert ram.mem == bytes(SIZE)


def test_ram_below_the_aperture_is_still_ram():
    ram = machine()
    ram.write_word(SIZE - 4, 0x01020304)
    ram.write_byte(0x100, 9)
    assert ram.read_word(SIZE - 4) == 0x01020304 and ram.read_byte(0x100) == 9
    assert ram.vram == bytes(VRAM)


@cases(("word", 4), ("byte", 1))
def test_past_the_end_of_video_memory_reads_zero_and_drops_writes(kind, width):
    ram = machine()
    where = BASE + VRAM + 64
    if width == 4:
        ram.write_word(where, 0xFFFFFFFF)
        assert ram.read_word(where) == 0
    else:
        ram.write_byte(where, 0xFF)
        assert ram.read_byte(where) == 0
    assert ram.mem == bytes(SIZE) and ram.vram == bytes(VRAM)
    assert len(ram.mem) == SIZE and len(ram.vram) == VRAM


def test_a_word_across_the_end_of_video_memory_keeps_the_part_inside():
    ram = machine()
    ram.write_word(BASE + VRAM - 2, 0xAABBCCDD)
    assert ram.vram[-2:] == bytes((0xDD, 0xCC))
    assert ram.read_word(BASE + VRAM - 2) == 0x0000CCDD
    assert len(ram.vram) == VRAM


def test_a_word_across_the_top_of_ram_splits_between_the_two():
    ram = machine()
    ram.write_word(SIZE - 2, 0xAABBCCDD)
    assert ram.mem[-2:] == bytes((0xDD, 0xCC))
    assert ram.vram[:2] == bytes((0xBB, 0xAA))
    assert ram.read_word(SIZE - 2) == 0xAABBCCDD
    assert len(ram.mem) == SIZE


def test_a_word_across_the_top_of_the_space_wraps_to_zero():
    ram = RAM(SIZE, vram_size=SIZE)                # VRAM fills the aperture
    top = 2 * SIZE
    ram.write_word(top - 2, 0xAABBCCDD)
    assert ram.vram[-2:] == bytes((0xDD, 0xCC))
    assert ram.mem[:2] == bytes((0xBB, 0xAA))
    assert ram.read_word(top - 2) == 0xAABBCCDD


def test_the_space_itself_wraps():
    ram = machine()
    ram.write_word(2 * SIZE + 12, 0x5A5A5A5A)      # one whole space up
    assert ram.read_word(12) == 0x5A5A5A5A


# --- the flags ------------------------------------------------------------------

@cases("word", "byte")
def test_an_aperture_write_sets_vram_dirty_and_not_io_pending(kind):
    ram = machine()
    if kind == "word":
        ram.write_word(BASE, 1)
    else:
        ram.write_byte(BASE, 1)
    assert ram.vram_dirty is True and ram.io_pending is False


def test_a_ram_write_does_not_set_vram_dirty():
    ram = machine()
    ram.write_word(0x200, 1)
    ram.write_byte(0x200, 1)
    assert ram.vram_dirty is False


def test_the_io_window_still_arms_io_pending():
    ram = machine()
    ram.write_byte(IO_START, 1)
    assert ram.io_pending is True


# --- the rest of RAM's interface -------------------------------------------------

def test_nothing_loads_into_video_memory():
    ram = machine()
    try:
        ram.load_bytes(b"\x01" * 8, BASE)
    except ValueError:
        assert ram.vram == bytes(VRAM)
        return
    raise AssertionError("load_bytes wrote into the aperture")


def test_a_dump_is_ram_only():
    ram = machine()
    assert len(ram.dump_ram()) == SIZE


@cases(
    (SIZE, SIZE + 4, "fit in the aperture"),
    (SIZE, -1, "fit in the aperture"),
    (1 << 32, 16, "at most 2 GB"),
)
def test_bad_sizes_are_refused(size, vram, expected):
    try:
        RAM(size, vram)
    except ValueError as e:
        assert expected in str(e), e
        return
    raise AssertionError(f"RAM({size}, {vram}) should have been refused")


# --- through the CPU ----------------------------------------------------------------

def run(*instructions, limit=100):
    ram = machine()
    ram.load_bytes(b"".join(instructions), 0)
    cpu = CPU(ram)
    for _ in range(limit):
        if cpu.run() == 1:
            return cpu
    raise AssertionError(f"did not halt within {limit} instructions")


def test_a_guest_draws_a_pixel_with_one_store():
    cpu = run(encode("MOV", dst=0, src1=NONE_REG, imm=BASE + 40),
              encode("MOV", dst=1, src1=NONE_REG, imm=0xFFFF0000),
              encode("MWW", dst=0, src1=1),
              encode("MRW", dst=2, src1=0),
              encode("HALT"))
    assert cpu.ram.vram[40:44] == bytes((0, 0, 0xFF, 0xFF))
    assert cpu.reg.read(2) == 0xFFFF0000


def test_a_guest_writes_bytes_into_the_aperture_too():
    cpu = run(encode("MOV", dst=0, src1=NONE_REG, imm=BASE + 7),
              encode("MW", dst=0, src1=NONE_REG, imm=0x42),
              encode("MR", dst=1, src1=0),
              encode("HALT"))
    assert cpu.ram.vram[7] == 0x42 and cpu.reg.read(1) == 0x42


def test_jumping_into_video_memory_is_a_fetch_fault():
    """No handler installed, so the fault stops the machine with its message."""
    ram = machine()
    ram.load_bytes(encode("JMP", src1=NONE_REG, imm=BASE), 0)
    cpu = CPU(ram)
    cpu.run()
    try:
        cpu.run()
    except RuntimeError as e:
        assert "Fetch past end of memory" in str(e), e
        return
    raise AssertionError("executing video memory should fault")


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "video memory aperture"))
