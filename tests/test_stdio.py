"""<pigeon/stdio.h>: printf and friends, step 2 of docs/phase4_plan.md.

snprintf is compiled with its libraries and run on a bare CPU, and what it
wrote is compared with Python's % formatting of the same values. printf
needs the kernel: with none it must return -1, which is checked here;
through the kernel it is in tests/test_kernel.py.

    python3 tests/test_stdio.py      (or: python3 -m pytest tests/)
"""
import functools
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run_module                                          # noqa: E402
from assembler.assembler import Assembler                               # noqa: E402
from compiler.cc import compile_units                                   # noqa: E402
from emulator.cpu import CPU                                            # noqa: E402
from emulator.memory_map import PROGRAM_LOAD_ADDR, RAM_SIZE, STACK_TOP  # noqa: E402
from emulator.programs import libraries_for                             # noqa: E402
from emulator.ram import RAM                                            # noqa: E402

SLOT = 80           # bytes of out[] each case writes into

# (format, the C arguments, the Python arguments)
CASES = [
    ("%d", ["42"], (42,)),
    ("%d", ["-42"], (-42,)),
    ("%d", ["0"], (0,)),
    ("%d", ["(int)0x80000000u"], (-2147483648,)),
    ("%i", ["7"], (7,)),
    ("%u", ["4000000000u"], (4000000000,)),
    ("%x", ["255"], (255,)),
    ("%x", ["0xDEADBEEFu"], (0xDEADBEEF,)),
    ("%X", ["48879"], (48879,)),
    ("%o", ["8"], (8,)),
    ("[%5d]", ["42"], (42,)),
    ("[%-5d]", ["42"], (42,)),
    ("[%05d]", ["-42"], (-42,)),
    ("[%08x]", ["0xBEEF"], (0xBEEF,)),
    ("%c%c", ["'o'", "'k'"], ("o", "k")),
    ("%s", ['"hello"'], ("hello",)),
    ("[%.3s]", ['"hello"'], ("hello",)),
    ("[%-8s]", ['"ab"'], ("ab",)),
    ("[%8s]", ['"ab"'], ("ab",)),
    ("100%%", [], ()),
    ("%s=%d, %x", ['"k"', "10", "255"], ("k", 10, 255)),
    ("%d%d%d%d%d%d%d%d", [str(i) for i in range(1, 9)], tuple(range(1, 9))),
]


@functools.lru_cache(maxsize=None)
def run_c(text):
    """C text, compiled with the libraries its includes name, run to HALT on
    a bare CPU: (cpu, ram, symbols)."""
    with tempfile.TemporaryDirectory() as d:
        source = Path(d) / "t.c"
        source.write_text(text)
        asm = Path(d) / "t.asm"
        asm.write_text(compile_units([source, *libraries_for(source)]))
        assembler = Assembler(str(asm))
        image = assembler.assemble()
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, PROGRAM_LOAD_ADDR)
    cpu = CPU(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    for _ in range(20_000_000):
        if cpu.run() == 1:
            break
    else:
        raise AssertionError("did not halt")
    assert cpu.sp == STACK_TOP, "hardware stack unbalanced"
    return cpu, ram, {**assembler.static_defs, **assembler.symbols}


def c_string(ram, address, limit=SLOT):
    data = bytes(ram.mem[address:address + limit])
    return data.split(b"\0", 1)[0].decode("latin-1")


def run_cases():
    calls = "\n".join(
        f"    lens[{i}] = snprintf(out + {i * SLOT}, {SLOT}u, \"{fmt}\"{''.join(', ' + a for a in args)});"
        for i, (fmt, args, _) in enumerate(CASES))
    return run_c(f"#include <pigeon/stdio.h>\nchar out[{len(CASES) * SLOT}];\n"
                 f"int lens[{len(CASES)}];\nint main(void) {{\n{calls}\n    return 0;\n}}\n")


def test_snprintf_formats_as_c_does():
    _, ram, symbols = run_cases()
    for i, (fmt, _, py_args) in enumerate(CASES):
        expected = fmt % py_args
        got = c_string(ram, symbols["__g_out"] + i * SLOT)
        length = ram.read_word(symbols["__g_lens"] + 4 * i)
        assert (got, length) == (expected, len(expected)), f"{fmt!r}: {got!r} ({length})"


def test_snprintf_cuts_to_the_buffer_and_returns_what_it_wanted():
    _, ram, symbols = run_c(
        "#include <pigeon/stdio.h>\n"
        "char small[5]; char one[1]; char none[2]; int a; int b; int c;\n"
        "int main(void) {\n"
        "    none[0] = 'x';\n"
        "    a = snprintf(small, 5u, \"%s\", \"hello world\");\n"
        "    b = snprintf(one, 1u, \"%d\", 12345);\n"
        "    c = snprintf(none, 0u, \"abc\");\n"
        "    return 0;\n"
        "}\n")
    word = lambda name: ram.read_word(symbols[f"__g_{name}"])
    assert (c_string(ram, symbols["__g_small"], 5), word("a")) == ("hell", 11)
    assert (c_string(ram, symbols["__g_one"], 1), word("b")) == ("", 5)
    assert (ram.mem[symbols["__g_none"]], word("c")) == (ord("x"), 3), "size 0 wrote anyway"


def test_with_no_kernel_printf_puts_and_putchar_return_minus_one():
    """The system-call table is empty on a bare CPU: nothing may call through it."""
    cpu, _, _ = run_c(
        "#include <pigeon/stdio.h>\n"
        "int main(void) {\n"
        "    return (printf(\"x %d\", 1) == -1) + 10 * (puts(\"b\") == -1)\n"
        "        + 100 * (putchar('a') == -1);\n"
        "}\n")
    assert cpu.reg.read(0) == 111


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "printf and friends"))
