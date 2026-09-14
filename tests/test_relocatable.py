"""Relocatable programs: phase 1 of docs/kernel.md (§8, §17).

A C program built with cc.py --relocatable is a program file: a header, an
image built for LINK_ADDR, and the offset of every word in the image that
holds an address. compiler/program_file.py finds those words by assembling
the program twice, at two addresses, and comparing. The kernel will load a
program file anywhere, add the distance to each listed word, and call it as
entry(argc, argv). Here the host loads it the same way.

Everything is run, not just inspected. A patched program is called at
several addresses, and it has to return the right answer, give its caller
back F and a balanced hardware stack, keep its frame stack and heap after
its image and away from the fixed program region, and stop its heap at
__heap_limit.

    python3 tests/test_relocatable.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import struct
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                   # noqa: E402
from assembler.assembler import Assembler                               # noqa: E402
from compiler import cc                                                 # noqa: E402
from compiler.program_file import (LINK_ADDR, LINK_ALT, Header,         # noqa: E402
                                   ProgramFileError, addresses, build, relocate)
from emulator.cpu import CPU                                            # noqa: E402
from emulator.instruction_set import NONE_REG, encode                   # noqa: E402
from emulator.memory_map import (HEAP_START, PROGRAM_FILE_HEADER,       # noqa: E402
                                 PROGRAM_FILE_MAGIC, PROGRAM_FILE_VERSION,
                                 PROGRAM_LOAD_ADDR, RAM_SIZE, STACK_TOP)
from emulator.programs import Program                                   # noqa: E402
from emulator.ram import RAM                                            # noqa: E402

STEP_LIMIT = 3_000_000
F = 5                                   # the frame pointer's register number

# Where the harness keeps what a kernel would. Well away from the fixed
# program region and HEAP_START, which a relocated program must not touch,
# and from every address the programs here are loaded at.
RETURN_TO = 0x00800000                  # a HALT, where the program's RET lands
ARGV_AT = 0x00900000                    # the argv array, then its strings
CALLER_FRAME = 0x00A00000               # the caller's F: argc, then argv

BASES = (LINK_ADDR, 0x01040FF0, 0x00400000, 0x05000008)

# A function pointer, a table of string pointers, recursion, malloc and the
# arguments: every kind of address a C program holds.
EVERYTHING = r'''
#include <pigeon/mem.h>
#include <pigeon/string.h>
char *names[3] = {"alpha", "beta", "gamma"};
int twice(int x) { return x * 2; }
int fact(int n) { if (n <= 1) return 1; return n * fact(n - 1); }
int main(int argc, char **argv) {
    int (*f)(int) = twice;
    char *buf = (char *)malloc(32);
    strlcpy(buf, names[2], 32);
    return f(fact(5)) + (int)strlen(buf) * 1000 + argc * 100000 + argv[1][0] * 1000000;
}
'''
EVERYTHING_RETURNS = 2 * 120 + 5 * 1000 + 2 * 100000 + ord("h") * 1000000

# The frame stack in use, and the address malloc hands out.
LAYOUT = r'''
#include <pigeon/mem.h>
int depth(int n) { int here = n; if (n == 0) return 0; return depth(n - 1) + here - n; }
int main(void) { char *p = (char *)malloc(8); depth(50); return (int)p; }
'''

HEAP = r'''
#include <pigeon/mem.h>
int main(void) { return (malloc(1000) != NULL) + 2 * (malloc(1000) != NULL); }
'''

_WORK = tempfile.TemporaryDirectory()
_BUILT = {}


def build_c(folder, text, name, relocatable=True):
    """C text -> (the build's bytes, the assembly it came from)."""
    source = Path(folder) / f"{name}.c"
    source.write_text(text)
    binary = Program(name=name, source=source, binary=Path(folder) / f"{name}.bin",
                     relocatable=relocatable).ensure_built(quiet=True)
    return binary.read_bytes(), binary.with_suffix(".asm")


def built(text, name):
    """build_c, once for the whole file."""
    if name not in _BUILT:
        _BUILT[name] = build_c(_WORK.name, text, name)
    return _BUILT[name]


def symbols_of(asm, origin):
    assembler = Assembler(str(asm), origin=origin)
    assembler.assemble()
    return {**assembler.static_defs, **assembler.symbols}


def assemble_text(text, origin=None):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.asm"
        path.write_text(text)
        return Assembler(str(path), origin=origin).assemble()


def run_cpu(ram, pc):
    cpu = CPU(ram)
    cpu.pc = pc
    for _ in range(STEP_LIMIT):
        if cpu.run() == 1:
            return cpu
    raise AssertionError(f"did not halt within {STEP_LIMIT:,} instructions")


class Call:
    """A program file loaded at `base` and called as entry(argc, argv), the
    way the kernel will: the image patched, the caller's frame holding the
    arguments, and the return address on the hardware stack."""

    def __init__(self, blob, base, heap_limit=None, ram=None):
        self.image, self.header = relocate(blob, base)
        self.base = base
        ram = self.ram = ram or RAM(RAM_SIZE)
        ram.load_bytes(self.image, base)
        if heap_limit is not None:
            ram.write_word(base + self.header.heap_offset + 4, heap_limit)

    def run(self, args=("t", "hello")):
        ram = self.ram
        ram.load_bytes(encode("HALT", dst=NONE_REG, src1=NONE_REG, src2=NONE_REG, imm=0),
                       RETURN_TO)
        text = ARGV_AT + 4 * len(args)
        for index, arg in enumerate(args):
            ram.write_word(ARGV_AT + 4 * index, text)
            ram.load_bytes(arg.encode() + b"\0", text)
            text += len(arg) + 1
        ram.write_word(CALLER_FRAME, len(args))
        ram.write_word(CALLER_FRAME + 4, ARGV_AT)
        cpu = CPU(ram)
        cpu.reg.values[F] = CALLER_FRAME
        cpu.sp -= 4
        ram.write_word(cpu.sp, RETURN_TO)
        cpu.pc = self.base + self.header.entry
        for _ in range(STEP_LIMIT):
            if cpu.run() == 1:
                break
        else:
            raise AssertionError(f"did not return within {STEP_LIMIT:,} instructions")
        assert cpu.pc == RETURN_TO + 8, f"halted at {cpu.pc - 8:#x}, not back at its caller"
        self.cpu = cpu
        return self

    @property
    def a(self):
        return self.cpu.reg.read(0)

    @property
    def image_end(self):
        return self.base + self.header.image_size

    def written(self, start, end):
        """How many bytes from start to end are no longer zero."""
        region = self.ram.mem[start:end]
        return len(region) - region.count(0)


def call(blob, base, args=("t", "hello"), heap_limit=None):
    return Call(blob, base, heap_limit=heap_limit).run(args)


# --- the file ---------------------------------------------------------------------

def test_the_header_describes_the_file():
    blob, asm = built(EVERYTHING, "everything")
    header = Header.read(blob)
    symbols = symbols_of(asm, LINK_ADDR)
    assert blob[:4] == b"PGEX"
    assert struct.unpack_from("<2I", blob) == (PROGRAM_FILE_MAGIC, PROGRAM_FILE_VERSION)
    assert len(blob) == PROGRAM_FILE_HEADER + header.image_size + 4 * header.patch_count
    assert header.image_size == symbols["__image_end"] - LINK_ADDR
    assert header.entry == symbols["__start"] - LINK_ADDR == 0
    assert header.frame_size == symbols["__frame_limit"] - symbols["__frame_base"] == 262144
    assert header.heap_offset == symbols["__heap_ptr"] - LINK_ADDR
    assert symbols["__heap_limit"] == symbols["__heap_ptr"] + 4
    assert header.link == LINK_ADDR
    offsets = struct.unpack_from(f"<{header.patch_count}I", blob,
                                 PROGRAM_FILE_HEADER + header.image_size)
    assert list(offsets) == sorted(set(offsets)), "patches out of order, or listed twice"
    assert all(offset % 4 == 0 and offset + 4 <= header.image_size for offset in offsets)


@cases(*BASES)
def test_a_patched_image_is_the_build_made_for_that_address(base):
    """The comparison misses nothing: patching gives, byte for byte, what
    the assembler builds for that address."""
    blob, asm = built(EVERYTHING, "everything")
    image, _ = relocate(blob, base)
    assert image == Assembler(str(asm), origin=base).assemble(), f"differs at {base:#x}"


@cases(*BASES)
def test_it_runs_wherever_it_is_loaded(base):
    blob, _ = built(EVERYTHING, "everything")
    run = call(blob, base)
    assert run.a == EVERYTHING_RETURNS, f"returned {run.a} at {base:#x}"
    assert run.cpu.reg.read(F) == CALLER_FRAME, "the caller did not get its F back"
    assert run.cpu.sp == STACK_TOP, f"hardware stack unbalanced: SP={run.cpu.sp:#x}"
    assert run.written(PROGRAM_LOAD_ADDR, HEAP_START + 2 * 262144) == 0, \
        "it wrote to the fixed program region, or the frame stack at HEAP_START"


@cases(LINK_ADDR, 0x05000008)
def test_its_frame_stack_and_heap_come_after_its_image(base):
    blob, _ = built(LAYOUT, "layout")
    run = call(blob, base)
    heap = run.image_end + run.header.frame_size
    assert run.a == heap + 8, f"malloc gave {run.a:#x}; the heap starts at {heap:#x}"
    assert run.ram.read_word(base + run.header.heap_offset) == heap + 16
    assert run.written(run.image_end, heap) > 0, "no frames after its image"
    assert run.written(PROGRAM_LOAD_ADDR, HEAP_START + 2 * 262144) == 0


def test_a_main_without_parameters_ignores_its_arguments():
    blob, _ = built("int main(void) { int a = 3; int b = 4; return a * b; }\n", "plain")
    run = call(blob, 0x01040FF0, args=("plain", "x", "y"))
    assert run.a == 12 and run.cpu.reg.read(F) == CALLER_FRAME and run.cpu.sp == STACK_TOP


def test_it_can_be_called_from_c_through_a_function_pointer():
    """What the kernel will do: C built for PROGRAM_LOAD_ADDR calls the
    program's entry with arguments, and its own local survives the call."""
    blob, _ = built(EVERYTHING, "everything")
    base = 0x01040FF0
    caller, _ = build_c(_WORK.name, f"""
        typedef int (*entry_fn)(int argc, char **argv);
        int main(void) {{
            char *argv[2];
            int keep = 77;
            entry_fn entry = (entry_fn){base:#x}u;
            argv[0] = "t";
            argv[1] = "hello";
            if (entry(2, argv) != {EVERYTHING_RETURNS}) return 1;
            return keep;
        }}""", "caller", relocatable=False)
    ram = RAM(RAM_SIZE)
    Call(blob, base, ram=ram)
    ram.load_bytes(caller, PROGRAM_LOAD_ADDR)
    cpu = run_cpu(ram, PROGRAM_LOAD_ADDR)
    assert cpu.reg.read(0) == 77, f"main() returned {cpu.reg.read(0)}"
    assert cpu.sp == STACK_TOP


# --- the heap limit ---------------------------------------------------------------

@cases(("left at 0: mem.c's own limit", None, 3),
       ("room for exactly one", 1008, 1),
       ("a byte short of one", 1007, 0),
       ("room for exactly two", 2016, 3),
       ("a byte short of two", 2015, 1))
def test_malloc_stops_at_the_heap_limit(label, room, expected):
    """The kernel writes a program's limit into the word after __heap_ptr
    (docs/kernel.md Q7). Each malloc(1000) takes 1,008 bytes with its header."""
    blob, _ = built(HEAP, "heap")
    header = Header.read(blob)
    base = 0x01040FF0
    limit = None if room is None else base + header.image_size + header.frame_size + room
    run = call(blob, base, heap_limit=limit)
    assert run.a == expected, f"{label}: {run.a}"


def test_a_program_built_for_one_address_can_set_its_own_limit():
    """The kernel's case: built for PROGRAM_LOAD_ADDR, it declares
    __heap_limit alongside mem.c's own declaration, and sets it."""
    image, _ = build_c(_WORK.name, r'''
        #include <pigeon/mem.h>
        extern unsigned __heap_ptr;
        extern unsigned __heap_limit;
        int main(void) {
            char *first;
            __heap_limit = __heap_ptr + 108u;
            first = (char *)malloc(100);
            return (first != NULL) + 2 * (malloc(1) != NULL) + 4 * (__heap_ptr == __heap_limit);
        }''', "fixed_limit", relocatable=False)
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, PROGRAM_LOAD_ADDR)
    assert run_cpu(ram, PROGRAM_LOAD_ADDR).reg.read(0) == 5


# --- every real program -----------------------------------------------------------

@cases(*sorted(str(p.relative_to(REPO_ROOT)) for p in
               [*REPO_ROOT.glob("user/*.c"), *REPO_ROOT.glob("user/os/*.c")]))
def test_every_c_program_in_user_becomes_a_program_file(source):
    """Every address the compiler emits is one the comparison can patch."""
    folder = Path(_WORK.name) / "user"
    folder.mkdir(exist_ok=True)
    name = Path(source).stem
    binary = Program(name=name, source=REPO_ROOT / source, binary=folder / f"{name}.bin",
                     relocatable=True).ensure_built(quiet=True)
    blob = binary.read_bytes()
    assert Header.read(blob).patch_count > 0
    image, _ = relocate(blob, 0x02000008)
    assert image == Assembler(str(binary.with_suffix(".asm")), origin=0x02000008).assemble()


# --- what is refused --------------------------------------------------------------

@cases(("a word shifted from an address", ".word start >> 4\n"),
       ("a byte cut from an address", ".byte start & 0xFF\n.align 4\n"),
       ("an address not on a word", ".byte 0\n.word start\n.space 3\n"))
def test_a_value_computed_from_an_address_is_refused(label, data):
    text = f".ORG PROGRAM_LOAD_ADDR\nstart:\n    HALT\n{data}"
    image, moved = assemble_text(text, LINK_ADDR), assemble_text(text, LINK_ALT)
    try:
        addresses(image, moved, LINK_ALT - LINK_ADDR)
    except ProgramFileError as e:
        assert "computed from an address" in str(e), f"{label}: {e}"
        return
    raise AssertionError(f"{label}: accepted")


def test_a_program_compiled_for_one_address_is_refused():
    """It would relocate, but its frame stack would stay at HEAP_START and
    be shared with whoever ran it."""
    with tempfile.TemporaryDirectory() as d:
        asm = Path(d) / "fixed.asm"
        asm.write_text(cc.compile_to_asm("int main(void) { return 1; }", "fixed.c"))
        try:
            build(asm)
        except ProgramFileError as e:
            assert "not after its image" in str(e), e
            return
    raise AssertionError("a fixed build became a program file")


@cases(("not a program file", lambda blob: b"\x7fELF" + blob[4:], "not a program file"),
       ("cut short", lambda blob: blob[:-4], "describes"),
       ("a byte too long", lambda blob: blob + b"\0", "describes"),
       ("a later version", lambda blob: blob[:4] + struct.pack("<I", 2) + blob[8:], "version"),
       ("too short for a header", lambda blob: blob[:16], "too short"))
def test_a_damaged_file_is_refused(label, damage, fragment):
    blob, _ = built(EVERYTHING, "everything")
    try:
        relocate(damage(blob), LINK_ADDR)
    except ProgramFileError as e:
        assert fragment in str(e), f"{label}: {e}"
        return
    raise AssertionError(f"{label}: accepted")


def test_an_address_an_instruction_cannot_start_at_is_refused():
    blob, _ = built(EVERYTHING, "everything")
    try:
        relocate(blob, 0x01000004)
    except ProgramFileError:
        return
    raise AssertionError("loaded at 0x01000004")


def test_only_c_is_built_relocatable():
    with tempfile.TemporaryDirectory() as d:
        source = Path(d) / "tool.asm"
        source.write_text(".ORG PROGRAM_LOAD_ADDR\n    HALT\n")
        try:
            Program(name="tool", source=source, binary=Path(d) / "tool.bin",
                    relocatable=True).ensure_built(quiet=True)
        except ValueError as e:
            assert "only C" in str(e), e
            return
    raise AssertionError("assembly was built relocatable")


# --- the assembler ----------------------------------------------------------------

def test_a_definition_can_use_a_label():
    """`__frame_base = __image_end` needs a label's address, which the
    assembler has only after layout. Definitions may use each other, too."""
    image = assemble_text(".ORG 0x1000\nDOUBLE = AFTER * 2\nAFTER = end + 4\n"
                          ".word DOUBLE\nend:\n")
    assert struct.unpack("<I", image)[0] == (0x1004 + 4) * 2


@cases(("used to size .space", "SIZE = end\n.space SIZE\nend:\n", "uses a label"),
       ("naming nothing", ".ORG 0x1000\nX = nowhere + 1\n.word X\n", "Undefined symbol: nowhere"))
def test_a_definition_that_cannot_be_settled_says_why(label, text, fragment):
    try:
        assemble_text(text)
    except ValueError as e:
        assert fragment in str(e), f"{label}: {e}"
        assert "Line" in str(e), f"{label}: no line in {e}"
        return
    raise AssertionError(f"{label}: assembled")


def test_an_origin_given_to_the_assembler_overrides_org():
    image = assemble_text(".ORG PROGRAM_LOAD_ADDR\nstart:\n.word start\n", origin=0x40000)
    assert struct.unpack("<I", image)[0] == 0x40000


# --- cc.py --relocatable ----------------------------------------------------------

def test_cc_py_builds_a_program_file_on_the_command_line():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        source = d / "args.c"
        source.write_text("int main(int argc, char **argv) { return argc * 10 + argv[0][0]; }\n")
        output = d / "out" / "args.bin"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cc.main([str(source), "--relocatable", "-o", str(output)]) == 0
        blob = output.read_bytes()
        assert blob[:4] == b"PGEX" and "a program file" in out.getvalue(), out.getvalue()
        assert output.with_suffix(".asm").is_file(), "the assembly was not kept beside it"
        assert call(blob, 0x01040FF0, args=("a", "b")).a == 2 * 10 + ord("a")

        for argv in (["--relocatable", "-S", str(source)],
                     ["--relocatable", "--org", "BIOS2_LOAD_ADDR", str(source)],
                     ["--project", str(source), "--relocatable"]):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    cc.main(argv)
                except SystemExit as e:
                    assert e.code == 2, argv
                else:
                    raise AssertionError(f"{argv} was accepted")


def test_a_program_with_a_variadic_function_relocates():
    """Phase 4 step 1: the extra arguments' slots move with the frame stack."""
    source = ("#include <pigeon/stdarg.h>\n"
              "int sum(int count, ...) { va_list ap; int t = 0; va_start(ap, count);"
              " while (count > 0) { t = t + va_arg(ap, int); count--; } return t; }\n"
              "int main(int argc, char **argv) { return sum(8, 1, 2, 3, 4, 5, 6, 7, argc); }\n")
    blob, _ = built(source, "variadic")
    for base in BASES:
        assert call(blob, base).a == 28 + 2, f"at {base:#x}"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "relocatable programs"))
