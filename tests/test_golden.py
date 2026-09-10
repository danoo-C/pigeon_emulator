"""The assembler must keep producing byte-identical output.

These three binaries were assembled by the original toolchain before any
of this refactor. They are the regression gate for the assembler rewrite
and for the CALL/RET/SHL/SHR opcodes not disturbing opcodes 0-24.

If one fails, read the diff before touching the fixture -- re-blessing a
golden should be a deliberate act, not a way to get back to green.

    python3 tests/test_golden.py      (or: python3 -m pytest tests/)
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module          # noqa: E402
from assembler.assembler import Assembler      # noqa: E402

# These pin the ASSEMBLER, not the programs. bios_v1 is a frozen copy of
# the pre-chunked-loader BIOS kept purely as a fixture: firmware/bios.asm
# is free to evolve (and has), but the assembler must keep turning that
# exact source into those exact bytes forever.
#
# screen and check are NOT frozen copies, and that is a weakness worth
# knowing about: both are live programs that assemble DISPLAY_W and
# DISPLAY_H into immediates, so changing the screen resolution changes
# their bytes and forces a re-bless. That happened when the display went
# from 100x100 to 192x108 -- verified first as same size, same opcodes,
# same instruction count, only the geometry immediates moving. Freezing
# copies the way bios_v1 is frozen would make this gate hold across a
# resolution change instead of bending to it.
GOLDEN = {
    "bios_v1": ("tests/golden/bios_v1.asm", "tests/golden/bios_v1.bin"),
    "screen": ("user/screen.asm", "tests/golden/screen.bin"),
    "check": ("user/checkerboard.asm", "tests/golden/check.bin"),
}


@cases("bios_v1", "screen", "check")
def test_assembles_byte_for_byte(name):
    source, golden = GOLDEN[name]
    built = Assembler(str(REPO_ROOT / source)).assemble()
    expected = (REPO_ROOT / golden).read_bytes()

    assert len(built) == len(expected), (
        f"{source}: built {len(built)} bytes, golden has {len(expected)}")
    for offset in range(0, len(expected), 8):
        got, want = built[offset:offset + 8], expected[offset:offset + 8]
        assert got == want, (
            f"{source}: instruction at 0x{offset:04x} differs\n"
            f"      built:  {got.hex(' ')}\n"
            f"      golden: {want.hex(' ')}")


@cases("user/sincos.asm", "user/ui.asm", "assembler/example.asm",
       "firmware/bios.asm")
def test_assembles_without_error(source):
    """No golden for these. sincos.bin and ui.bin drifted from their
    sources; firmware/bios.asm evolves (bios_v1 is its frozen stand-in).
    example.asm exercises the two-operand shorthand (`ADD C #1`)."""
    assert Assembler(str(REPO_ROOT / source)).assemble()


def test_no_source_has_drifted_from_the_memory_map():
    """Every .asm must agree with emulator/memory_map.py.

    user/ui.asm used to fail this: it hardcoded DISPLAY_START = 0x1218,
    two layout generations stale, and drew into the IO region instead of
    the framebuffer.
    """
    # firmware/ and user/ only: docs/ideas/ holds design sketches that are
    # deliberately not buildable (FFS.asm uses an MMW that never existed).
    drifted = {}
    for folder in ("firmware", "user"):
        for source in sorted((REPO_ROOT / folder).glob("*.asm")):
            asm = Assembler(str(source))
            asm.assemble()
            if asm.warnings:
                drifted[source.name] = asm.warnings
    assert not drifted, f"sources disagree with the memory map: {drifted}"


def test_shorthand_matches_the_explicit_form(tmp_path=None):
    """`ADD C #1` must encode exactly like `ADD C C #1`."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        short, long = Path(d) / "s.asm", Path(d) / "l.asm"
        short.write_text("ADD C #1\nSUB D #2\nHALT\n")
        long.write_text("ADD C C #1\nSUB D D #2\nHALT\n")
        assert Assembler(str(short)).assemble() == Assembler(str(long)).assemble()


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "assembler golden tests"))
