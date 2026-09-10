"""pigeon-cc: compile C, run it on the emulator, check the answer.

These are execution tests, not parser tests. Every case compiles to
assembly, assembles to a real image, loads it at PROGRAM_LOAD_ADDR and
runs it on the CPU -- so a pass means the whole chain works, including
the calling convention.

    python3 tests/test_compiler.py      (or: python3 -m pytest tests/)
"""
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                          # noqa: E402
from assembler.assembler import Assembler                      # noqa: E402
from compiler.cc import compile_to_asm                         # noqa: E402
from compiler.lexer import CompileError                        # noqa: E402
from emulator.cpu import CPU                                   # noqa: E402
from emulator.memory_map import (PROGRAM_LOAD_ADDR, RAM_SIZE,  # noqa: E402
                                 STACK_TOP)
from emulator.ram import RAM                                   # noqa: E402

STEP_LIMIT = 3_000_000


def build(source: str) -> bytes:
    """C text -> a loadable image."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.asm"
        path.write_text(compile_to_asm(source, "t.c"))
        return Assembler(str(path)).assemble()


def run(source: str):
    """Compile, load, execute. Returns the CPU at HALT."""
    image = build(source)
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, PROGRAM_LOAD_ADDR)
    cpu = CPU(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    for _ in range(STEP_LIMIT):
        if cpu.run() == 1:
            return cpu
    raise AssertionError(f"did not halt within {STEP_LIMIT:,} instructions")


def returns(source: str, expected: int):
    cpu = run(source)
    got = cpu.reg.read(0)
    assert got == (expected & 0xFFFFFFFF), (
        f"main() returned {got} ({got:#x}), expected "
        f"{expected & 0xFFFFFFFF} ({expected & 0xFFFFFFFF:#x})")
    # A leaked hardware stack means a PUSH without its POP -- the expression
    # evaluator's invariant, and a silent corruptor if it ever slips.
    assert cpu.sp == STACK_TOP, (
        f"hardware stack unbalanced: SP={cpu.sp:#x}, expected {STACK_TOP:#x}")


# --- expressions ------------------------------------------------------------

@cases(
    ("2+3*4-6/2", 11),
    ("(2+3)*4", 20),
    ("17 % 5", 2),
    ("100 % 7", 2),
    ("(0xF0|0x0F)&0xFF^0x11", 0xEE),
    ("(1<<8) >> 2", 64),
    ("~0", -1),
    ("!0", 1),
    ("!5", 0),
    ("-7", -7),
    ("5 == 5", 1),
    ("5 != 5", 0),
    ("3 < 5", 1),
    ("5 <= 5", 1),
    ("(1 && 1) + (1 || 0) + (0 && 1)", 2),
    ("1 ? 100 : 200", 100),
    ("0 ? 100 : 200", 200),
)
def test_expression(expr, expected):
    returns(f"int main(void) {{ return {expr}; }}", expected)


def test_sizeof():
    returns("int main(void){ return sizeof(int)*100 + sizeof(char)*10 + sizeof(int*); }",
            4 * 100 + 1 * 10 + 4)


# --- signedness, which this machine gets wrong by default -------------------

def test_signed_comparison():
    """CMP is unsigned; signed operands need the sign-bit bias. Without it
    -5 < 0 is false, because -5 is 0xFFFFFFFB."""
    returns("int main(void){ int a = -5; return a < 0 ? 1 : 0; }", 1)
    returns("int main(void){ int a = -5; int b = 3; return a < b ? 1 : 0; }", 1)
    returns("int main(void){ int a = -10; int b = -5; return a < b ? 1 : 0; }", 1)


def test_unsigned_comparison_does_not_get_biased():
    """A large unsigned value must stay large, not read as negative."""
    returns("int main(void){ unsigned a = 4294967290u; return a > 5 ? 1 : 0; }", 1)


def test_unsigned_wraps():
    returns("int main(void){ unsigned a = 0; a = a - 1; return a; }", 0xFFFFFFFF)


# --- control flow -----------------------------------------------------------

@cases(
    ("if taken",     "int a=3; if(a<5) return 1; return 2;", 1),
    ("else taken",   "int a=9; if(a<5) return 1; else return 2; ", 2),
    ("while",        "int i=0,s=0; while(i<10){ s+=i; i++; } return s;", 45),
    ("do-while runs once", "int i=99; do { i=1; } while(0); return i;", 1),
    ("for",          "int s=0; for(int i=1;i<=10;i++) s+=i; return s;", 55),
    ("break",        "int i; for(i=0;i<100;i++) if(i==7) break; return i;", 7),
    ("continue",     "int s=0; for(int i=0;i<10;i++){ if(i%2) continue; s+=i; } return s;", 20),
    ("nested loops", "int n=0; for(int i=0;i<5;i++) for(int j=0;j<5;j++) n++; return n;", 25),
    ("empty for",    "int i=0; for(;;){ i++; if(i>4) break; } return i;", 5),
)
def test_control_flow(label, body, expected):
    returns(f"int main(void) {{ {body} }}", expected)


def test_short_circuit_does_not_evaluate_the_right_side():
    """If && evaluated both sides this would divide by zero, which on this
    machine is a Python exception that kills the emulator."""
    returns("int main(void){ int a = 0; return (a && 1/a) || 1; }", 1)
    returns("int main(void){ int a = 1; return (a || 1/0) ? 7 : 8; }", 7)


# --- variables --------------------------------------------------------------

@cases(
    ("multi declarator", "int i=0,s=0; while(i<10){ s+=i; i++; } return s;", 45),
    ("postfix",   "int i=5; int j=i++; return j*10+i;", 56),
    ("prefix",    "int i=5; int j=++i; return j*10+i;", 66),
    ("postfix--", "int i=5; int j=i--; return j*10+i;", 54),
    ("compound",  "int a=10; a+=5; a*=2; a-=6; a/=3; return a;", 8),
    ("compound bits", "int a=0xF0; a|=0x0F; a&=0xFE; a^=0x01; return a;", 0xFF),
    ("shadowing", "int a=1; { int a=2; } return a;", 1),
    ("chained assign", "int a; int b; a = b = 7; return a+b;", 14),
)
def test_variables(label, body, expected):
    returns(f"int main(void) {{ {body} }}", expected)


def test_globals():
    returns("int g; int main(void){ g = 99; return g; }", 99)
    returns("int g = 77; int main(void){ return g; }", 77)
    returns("int a = 1; int b = 2; int main(void){ return a + b; }", 3)


def test_char_is_one_byte():
    returns("char c; int main(void){ c = 'A'; return c; }", 65)
    returns("int main(void){ char c = 'z'; return c; }", ord('z'))


# --- functions and the calling convention -----------------------------------

def test_call_and_arguments():
    returns("int add(int a, int b){ return a+b; } int main(void){ return add(20,22); }", 42)
    returns("int f(int a,int b,int c,int d){ return a*1000+b*100+c*10+d; }"
            "int main(void){ return f(1,2,3,4); }", 1234)


def test_recursion():
    returns("int fact(int n){ if(n<=1) return 1; return n*fact(n-1); }"
            "int main(void){ return fact(6); }", 720)
    returns("int fib(int n){ if(n<2) return n; return fib(n-1)+fib(n-2); }"
            "int main(void){ return fib(12); }", 144)


def test_mutual_recursion():
    """Requires functions to be visible before they are defined."""
    returns("int odd(int n);"
            "int even(int n){ if(n==0) return 1; return odd(n-1); }"
            "int odd(int n){ if(n==0) return 0; return even(n-1); }"
            "int main(void){ return even(10); }", 1)


def test_nested_calls_do_not_corrupt_the_frame():
    """A call inside an argument expression is where a frame-pointer bug
    would show up: the inner call moves F while the outer is mid-setup."""
    returns("int sq(int x){ return x*x; } int main(void){ return sq(sq(3)); }", 81)
    returns("int f(int x){ return x+1; } int main(void){ return f(1)*f(2)+f(3); }", 10)
    returns("int add(int a,int b){ return a+b; }"
            "int main(void){ return add(add(1,2), add(3,4)); }", 10)


TWO = "int two(int a, int b){ return a*100 + b; } int id(int x){ return x; }"


@cases(
    ("nested in the last argument",  "two(7, id(3))", 703),
    ("nested in the first argument", "two(id(7), 3)", 703),
    ("nested in both",               "two(id(7), id(3))", 703),
    ("no nesting at all",            "two(7, 3)", 703),
    ("an argument that is an expression containing a call",
     "two(id(7), id(1) + id(2))", 703),
)
def test_a_nested_call_does_not_overwrite_an_earlier_argument(label, expr, expected):
    """A nested call does not only clobber registers -- it writes ITS
    arguments to the SAME frame slots the enclosing call is filling in,
    because both compute them from F plus the caller's frame size.

    `two(7, id(3))` returned 303: 7 went into slot 0, then id's own
    argument overwrote it, so `two` received (3, 3). The test above only
    passed by luck -- in `add(add(1,2), add(3,4))` the inner call's first
    argument happened to equal what the slot was supposed to hold.
    """
    returns(f"{TWO} int main(void){{ return {expr}; }}", expected)


def test_three_nested_arguments_all_survive():
    returns("int three(int a,int b,int c){ return a*10000 + b*100 + c; }"
            "int id(int x){ return x; }"
            "int main(void){ return three(id(1), id(2), id(3)); }", 10203)


def test_deep_recursion_keeps_the_frame_pointer_straight():
    returns("int down(int n){ if(n==0) return 0; return 1 + down(n-1); }"
            "int main(void){ return down(200); }", 200)


def test_function_named_like_a_register():
    """A-F are register names in the assembler, which refuses the
    ambiguity; the compiler mangles just those."""
    returns("int f(int x){ return x+1; } int main(void){ return f(41); }", 42)
    returns("int a(void){ return 7; } int main(void){ return a(); }", 7)


def test_void_function():
    returns("int g; void bump(void){ g = g + 1; } "
            "int main(void){ g = 0; bump(); bump(); return g; }", 2)


# --- pointers, arrays, strings ----------------------------------------------

@cases(
    ("address-of and deref", "int a=42; int *p=&a; return *p;", 42),
    ("store through pointer", "int a=1; int *p=&a; *p=99; return a;", 99),
    ("pointer arithmetic",  "int v[3]; int *p=v; *p=10; *(p+1)=20; return *(p+1);", 20),
    ("array subscript",     "int v[4]; v[2]=7; return v[2];", 7),
    ("array loop",          "int v[5]; for(int i=0;i<5;i++) v[i]=i*i;"
                            " int s=0; for(int i=0;i<5;i++) s+=v[i]; return s;", 30),
    ("char array",          "char b[4]; b[0]='A'; b[1]='B'; return b[0]+b[1];", 131),
    ("pointer difference",  "int v[8]; int *a=v; int *b=&v[5]; return b-a;", 5),
    ("pointer comparison",  "int v[4]; int *p=v; return p < v+2 ? 1 : 0;", 1),
    ("pointer increment",   "int v[3]; v[0]=1; v[1]=2; int *p=v; p++; return *p;", 2),
    ("int to pointer cast", "int a=5; unsigned n=(unsigned)&a; int *p=(int*)n; return *p;", 5),
)
def test_pointers_and_arrays(label, body, expected):
    returns(f"int main(void) {{ {body} }}", expected)


def test_array_decays_when_passed():
    returns("int sum(int *a, int n){ int s=0; for(int i=0;i<n;i++) s+=a[i]; return s; }"
            "int main(void){ int v[3]; v[0]=1; v[1]=2; v[2]=3; return sum(v,3); }", 6)


def test_global_array():
    returns("int g[4]; int main(void){ g[1]=5; g[3]=g[1]*2; return g[1]+g[3]; }", 15)


def test_string_literals():
    returns('int main(void){ char *s = "hi"; return s[0]; }', ord('h'))
    returns('int main(void){ char *s = "pigeon"; int n=0; while(s[n]) n++; return n; }', 6)
    returns('int main(void){ char *s = "AB"; return s[0]*100 + s[1] + s[2]; }',
            ord('A') * 100 + ord('B'))


def test_volatile_mmio():
    """The pattern the input library depends on: a device register at a
    fixed address, written and read through a volatile pointer."""
    returns("int main(void){ volatile unsigned *p = (volatile unsigned *)0x1418;"
            " *p = 123; return *p; }", 123)


# --- structs ----------------------------------------------------------------

def test_struct_members():
    returns("struct P { int x; int y; };"
            "int main(void){ struct P p; p.x=3; p.y=4; return p.x*p.y; }", 12)


def test_struct_through_pointer():
    returns("struct P { int x; int y; };"
            "int main(void){ struct P p; struct P *q=&p; q->x=7; q->y=6; return p.x*p.y; }", 42)


def test_struct_passed_by_pointer():
    returns("struct P { int x; int y; };"
            "int area(struct P *p){ return p->x * p->y; }"
            "int main(void){ struct P p; p.x=6; p.y=7; return area(&p); }", 42)


def test_struct_member_offsets():
    """Members are laid out in declaration order, each aligned to its own
    size, so a char does not force the next int out of alignment."""
    returns("struct S { char a; int b; char c; };"
            "int main(void){ struct S s; s.a=1; s.b=2; s.c=3;"
            " return s.a*100 + s.b*10 + s.c; }", 123)


def test_array_of_structs():
    returns("struct P { int x; int y; };"
            "int main(void){ struct P v[3]; for(int i=0;i<3;i++){ v[i].x=i; v[i].y=i*2; }"
            " return v[2].x*10 + v[2].y; }", 24)


# --- function pointers ------------------------------------------------------

def test_function_pointer():
    returns("int add1(int x){ return x+1; }"
            "int main(void){ int (*f)(int) = add1; return f(41); }", 42)


def test_function_pointer_as_argument():
    """CALL takes a register, so indirect calls need no special support."""
    returns("int inc(int x){ return x+1; } int dbl(int x){ return x*2; }"
            "int apply(int (*g)(int), int v){ return g(v); }"
            "int main(void){ return apply(inc,10)*100 + apply(dbl,10); }", 1120)


def test_function_pointer_reassigned():
    returns("int a(void){ return 1; } int b(void){ return 2; }"
            "int main(void){ int (*f)(void) = a; int s = f(); f = b; return s*10 + f(); }", 12)


# --- diagnostics ------------------------------------------------------------

@cases(
    ("int main(void){ return undefined_thing; }", "not declared"),
    ("int main(void){ float f; return 0; }", "floating"),
    ("int main(void){ return 1", "expected"),
    ("int main(void){ break; }", "outside a loop"),
    ("int f(int a){return a;} int main(void){ return f(1,2); }", "argument"),
    ("int main(void){ int a; int a; return 0; }", "already declared"),
    ("int main(void){ 1 = 2; return 0; }", "cannot assign"),
    ("int f(void){} int f(void){} int main(void){return 0;}", "defined twice"),
    ("void v(void){} int main(void){ return v(); }", "void"),
    ("int other(void){ return 0; }", "no main"),
    ("int main(void){ goto x; }", "goto"),
    ("int main(void){ switch(1){} }", "switch"),
)
def test_rejects(source, fragment):
    try:
        build(source)
    except CompileError as e:
        assert fragment in str(e), f"expected {fragment!r} in: {e}"
        return
    raise AssertionError(f"should have been rejected: {source!r}")


def test_errors_carry_a_source_position():
    try:
        build("int main(void) {\n    int a;\n    return nope;\n}\n")
    except CompileError as e:
        assert e.line == 3, f"expected line 3, got line {e.line}: {e}"
        return
    raise AssertionError("should have failed")


# --- output shape -----------------------------------------------------------

def test_image_is_small():
    """The frame stack and heap are addresses past the end of the image,
    not `.space` reservations -- reserving them put 256 KB of zeros in
    every binary and made the BIOS copy it on every boot."""
    image = build("int main(void){ return 0; }")
    assert len(image) < 1024, f"trivial program produced {len(image)} bytes"


def test_generated_assembly_carries_the_c_source():
    asm = compile_to_asm("int main(void) {\n    return 42;\n}\n", "t.c")
    assert "; 2: return 42;" in asm, "source lines should be interleaved as comments"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "pigeon-cc"))
