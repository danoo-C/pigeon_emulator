"""Assembler data directives, and the pass-1/pass-2 anti-drift guard.

The compiler needs static data: initialised globals, string literals,
lookup tables. None of it existed -- the size model was a hardcoded
"8 bytes per line" in both passes.

    python3 tests/test_directives.py      (or: python3 -m pytest tests/)
"""
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                  # noqa: E402
from assembler.assembler import Assembler, Item                        # noqa: E402


def assemble(source):
    """Assemble a source string, returning (bytes, assembler)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.asm"
        path.write_text(source)
        asm = Assembler(str(path))
        return asm.assemble(), asm


def rejects(source, expected_fragment):
    try:
        assemble(source)
    except Exception as e:
        assert expected_fragment in str(e), f"expected {expected_fragment!r} in: {e}"
        return
    raise AssertionError(f"should have been rejected: {source!r}")


# --- the anti-drift guard ---------------------------------------------------

def test_the_drift_guard_actually_fires():
    """If this check never runs it will rot, and a size mismatch would
    silently move every later label."""
    class Liar(Item):
        def place(self, asm, addr):
            return 4                      # claims 4 bytes...
        def emit(self, asm):
            return b"\x00\x00"            # ...emits 2

    asm = Assembler.__new__(Assembler)
    asm.org_addr = 0
    asm.symbols = {}
    items = [Liar(1, ".fake")]
    asm._layout(items)
    try:
        asm._emit(items)
    except AssertionError as e:
        assert "pass 1 sized" in str(e)
        return
    raise AssertionError("a size mismatch went undetected")


def test_image_length_matches_the_laid_out_address_range():
    binary, asm = assemble(".byte 1,2,3\n.align 8\nMOV A, #1\nHALT\n")
    assert len(binary) == asm.end_addr - asm.org_addr


# --- sizes and bytes --------------------------------------------------------

@cases(
    (".byte 1,2,3",        b"\x01\x02\x03"),
    (".byte 0xFF",         b"\xff"),
    (".byte -1",           b"\xff"),
    (".word 0x1234",       b"\x34\x12\x00\x00"),
    (".word 1,2",          b"\x01\x00\x00\x00\x02\x00\x00\x00"),
    (".word -1",           b"\xff\xff\xff\xff"),
    ('.ascii "AB"',        b"AB"),
    ('.asciz "AB"',        b"AB\x00"),
    ('.ascii "a", "b"',    b"ab"),
    ('.asciz "a", "b"',    b"a\x00b\x00"),
    (".space 0",           b""),
    (".space 5",           b"\x00" * 5),
)
def test_directive_emits(source, expected):
    binary, _ = assemble(source + "\n")
    assert binary == expected, f"{source} -> {binary.hex(' ')}"


@cases(
    ('.asciz "a\\nb"',   b"a\nb\x00"),
    ('.asciz "\\t\\r"',  b"\t\r\x00"),
    ('.asciz "\\x41"',   b"A\x00"),
    ('.asciz "\\0"',     b"\x00\x00"),
    ('.asciz "\\\\"',    b"\\\x00"),
    ('.asciz "a\\"b"',   b'a"b\x00'),
    ('.asciz "\\101"',   b"A\x00"),
)
def test_string_escapes(source, expected):
    binary, _ = assemble(source + "\n")
    assert binary == expected, f"{source} -> {binary.hex(' ')}"


def test_semicolon_inside_a_string_is_not_a_comment():
    binary, _ = assemble('.asciz "a;b"\n')
    assert binary == b"a;b\x00"


def test_comment_after_a_directive_is_still_a_comment():
    binary, _ = assemble('.byte 65 ; this is a comment\n')
    assert binary == b"A"


def test_colon_inside_a_string_defines_no_label():
    binary, asm = assemble('.asciz "a: b"\n')
    assert binary == b"a: b\x00"
    assert asm.symbols == {}


def test_equals_inside_a_string_is_not_a_static_def():
    binary, asm = assemble('.asciz "a=b"\n')
    assert binary == b"a=b\x00"
    assert asm.static_defs == {}


# --- alignment --------------------------------------------------------------

@cases((0, 8, 0), (1, 8, 7), (3, 4, 1), (4, 4, 0), (1, 1, 0))
def test_align_pads_to_the_boundary(prefix_bytes, boundary, expected_pad):
    binary, _ = assemble(f".space {prefix_bytes}\n.align {boundary}\n")
    assert len(binary) == prefix_bytes + expected_pad


def test_align_lets_code_follow_data():
    binary, asm = assemble('.byte 1,2,3\n.align 8\nCODE: HALT\n')
    assert asm.symbols["CODE"] == 8, "code should start on an 8-byte boundary"
    assert len(binary) == 16


# --- labels -----------------------------------------------------------------

def test_labels_address_the_data_that_follows():
    _, asm = assemble('MSG: .asciz "hi"\nNUM: .word 7\nEND:\n')
    assert asm.symbols == {"MSG": 0, "NUM": 3, "END": 7}


def test_label_before_align_points_at_the_padding():
    """Matches GNU as: item order decides, so put the label after .align
    if you want the aligned address."""
    _, asm = assemble('.byte 1\nBEFORE: .align 8\nAFTER: .word 0\n')
    assert asm.symbols["BEFORE"] == 1 and asm.symbols["AFTER"] == 8


def test_dotted_local_labels_work():
    """Compilers emit .L1, .L2. These used to define a symbol that could
    never be referenced -- ast.parse rejects the dot."""
    binary, asm = assemble(".L1:\n  HALT\n  JMP .L1\n")
    assert asm.symbols[".L1"] == 0
    assert int.from_bytes(binary[12:16], "little") == 0


def test_forward_reference_to_data():
    binary, asm = assemble("  MRW A, #TABLE\n  HALT\nTABLE: .word 0xCAFE\n")
    assert int.from_bytes(binary[4:8], "little") == asm.symbols["TABLE"] == 16


# --- errors, not silent wrong answers ---------------------------------------

@cases(
    (".byte 256\n",              "out of range"),
    (".byte 1,,2\n",             "empty operand"),
    (".byte\n",                  "at least one value"),
    (".align 3\n",               "power-of-two"),
    (".align 0\n",               "power-of-two"),
    ('.asciz "oops\n',           "unterminated"),
    ('.asciz "a\\q"\n',          "unknown escape"),
    ('.asciz "\\x4"\n',          "two hex digits"),
    (".unknown 1\n",             "Unknown directive"),
    ("x: HALT\nx: HALT\n",       "Duplicate label"),
    (".byte 1\nMOV A, #1\n",     "not a multiple of 8"),
    ("HALT\n.ORG 0x100\n",       ".ORG must come before"),
    ("A:\n HALT\n JMP A\n",      "both a register and a label"),
)
def test_rejected(source, fragment):
    rejects(source, fragment)


# --- .ORG -------------------------------------------------------------------

@cases((".ORG 0x20000", 0x20000), (".ORG PROGRAM_LOAD_ADDR", 0x20000),
       (".ORG HEAP_START", 0x120000))
def test_org_accepts_expressions(directive, expected):
    """`.ORG PROGRAM_LOAD_ADDR` used to fail a literal-only regex and be
    silently dropped, leaving the origin at 0 and every label wrong."""
    _, asm = assemble(f"{directive}\nhere:\n  HALT\n")
    assert asm.org_addr == expected and asm.symbols["here"] == expected


# --- error messages ---------------------------------------------------------

def test_errors_name_the_real_source_line():
    """Line numbers used to index the preprocessed list, so a bad line 9
    was reported as 'Line 2'."""
    source = "; comment\n\n; another\n\n.ORG 0x0\n\nHALT\n\nBOGUS A, B\n"
    try:
        assemble(source)
    except Exception as e:
        assert "Line 9" in str(e), f"expected line 9, got: {e}"
        return
    raise AssertionError("should have failed")


# --- the one that proves it end to end --------------------------------------

def test_guest_reads_assembled_data_through_the_real_loader():
    """Data must land where its label says, all the way through DMA.

    A word table and a string, read back by the CPU into registers.
    """
    from emulator.cpu import CPU
    from emulator.memory_map import PROGRAM_LOAD_ADDR, RAM_SIZE
    from emulator.ram import RAM

    binary, _ = assemble("""
.ORG PROGRAM_LOAD_ADDR
        MRW A, #TABLE          ; first word of the table
        MOV B, #TABLE
        ADD B, B, #8
        MRW B, B               ; third word
        MR  C, #MSG            ; first byte of the string
        MOV D, #MSG
        ADD D, D, #2
        MR  D, D               ; third byte
        HALT
TABLE:  .word 0x11111111, 0x22222222, 0x33333333
MSG:    .asciz "pigeon"
""")
    ram = RAM(RAM_SIZE)
    ram.load_bytes(binary, PROGRAM_LOAD_ADDR)
    cpu = CPU(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    for _ in range(100):
        if cpu.run() == 1:
            break
    assert cpu.reg.read(0) == 0x11111111, f"A={cpu.reg.read(0):#x}"
    assert cpu.reg.read(1) == 0x33333333, f"B={cpu.reg.read(1):#x}"
    assert cpu.reg.read(2) == ord('p'), f"C={cpu.reg.read(2)}"
    assert cpu.reg.read(3) == ord('g'), f"D={cpu.reg.read(3)}"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "assembler directives"))
