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
from compiler.cc import compile_to_asm, origin_of              # noqa: E402
from compiler.lexer import CompileError                        # noqa: E402
from compiler.preprocess import Preprocessor                   # noqa: E402
from emulator.cpu import CPU                                   # noqa: E402
from emulator.memory_map import (BIOS2_LOAD_ADDR,              # noqa: E402
                                 PROGRAM_LOAD_ADDR, RAM_SIZE, STACK_TOP)
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
    return execute(build(source))


def execute(image: bytes):
    """Load an image at PROGRAM_LOAD_ADDR and run it to HALT."""
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


# --- the preprocessor -------------------------------------------------------

def expand(source: str) -> str:
    """Preprocessed text, without the blank lines directives leave behind."""
    text, _ = Preprocessor().process(source, "t.c")
    return "\n".join(line for line in text.splitlines() if line.strip())


@cases(
    ("a string", '#define NAME "/x"\nchar *s = "NAME"; char *t = NAME;',
     'char *s = "NAME"; char *t = "/x";'),
    ("a character", "#define A 5\nint c = 'A' + A;", "int c = 'A' + 5;"),
    ("an escaped quote", '#define N 1\nchar *s = "say \\"N\\" N"; int n = N;',
     'char *s = "say \\"N\\" N"; int n = 1;'),
    ("an escaped character", "#define N 1\nint q = '\\''; int n = N;",
     "int q = '\\''; int n = 1;"),
    ("a string a macro expands to", '#define A "B"\n#define B 1\nchar *s = A;',
     'char *s = "B";'),
    ("a parameter's name in the body", '#define SHOW(x) puts("x"), x\nSHOW(y);',
     'puts("x"), y;'),
    ("an argument", '#define ID(x) x\n#define N 1\nchar *s = ID("N");',
     'char *s = ("N");'),
)
def test_names_inside_literals_are_not_replaced(what, source, expected):
    assert expand(source) == expected, f"{what}: {expand(source)}"


def test_a_string_can_hold_a_macro_name():
    """What broke user/os/installer.c: "INSTALLER" became ""/install.bin""."""
    returns('#define GREETING "hi"\n'
            'int main(void){ char *s = "GREETING"; char *t = GREETING;'
            ' return s[1]*256 + t[1]; }',
            ord('R') * 256 + ord('i'))


def test_a_macro_argument_that_is_a_type_goes_in_as_written():
    """`(char *)` is a cast and `((char *))` isn't. Words and stars go in as
    they are; an expression is still parenthesised, and so is a name that is
    itself a macro, which may expand to one."""
    out = expand("#define CAST(t, v) ((t)(v))\n"
                 "#define TWICE(x) x*2\n"
                 "#define N 1+2\n"
                 "CAST(char *, a + b) CAST(unsigned int, x) TWICE(N) TWICE(y)")
    assert "((char *)((a + b)))" in out, out
    assert "((unsigned int)(x))" in out, out
    assert "(1+2)*2" in out and "y*2" in out, out


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


# --- signed arithmetic on an unsigned machine (docs/compiler_plan.md) --------
#
# DIV and SHR are unsigned and there is no SAR, so the compiler puts the
# sign back. Every case below returned the wrong answer, silently, before
# the fix: -20 / 2 was 2,147,483,638.

@cases(
    ("-20 / 2", -10),
    ("20 / -2", -10),
    ("-20 / -2", 10),
    ("-7 / 2", -3),                       # C truncates toward zero
    ("7 / -2", -3),
    ("-7 % 3", -1),                       # the sign follows the dividend
    ("7 % -3", 1),
    ("-7 % -3", -1),
    ("-8 >> 1", -4),
    ("-1 >> 5", -1),
    ("-1024 >> 10", -1),
    ("-3 << 2", -12),
    ("(int)0x80000000 / 2", -1073741824),  # INT_MIN, where negating overflows
)
def test_signed_arithmetic(expr, expected):
    returns(f"int main(void) {{ return {expr}; }}", expected)


@cases(
    ("divide", "int a; int b; a = -20; b = 2; return a / b;", -10),
    ("divide by a negative", "int a; int b; a = 20; b = -2; return a / b;", -10),
    ("modulo", "int a; int b; a = -7; b = 3; return a % b;", -1),
    ("shift by a variable", "int a; int n; a = -64; n = 3; return a >> n;", -8),
    ("shift by a zero variable", "int a; int n; a = -8; n = 0; return a >> n;", -8),
    ("compound divide", "int a; a = -20; a /= 2; return a;", -10),
    ("compound modulo", "int a; a = -7; a %= 3; return a;", -1),
    ("compound shift", "int a; a = -8; a >>= 1; return a;", -4),
    ("INT_MIN / -1", "int a; int b; a = (int)0x80000000; b = -1; return a / b;",
     -2147483648),
)
def test_signed_arithmetic_on_variables(label, body, expected):
    returns(f"int main(void) {{ {body} }}", expected)


@cases(
    ("divide", "unsigned a; a = 0xFFFFFFFEu; return (int)(a / 2u);", 0x7FFFFFFF),
    ("modulo", "unsigned a; a = 0xFFFFFFFFu; return (int)(a % 10u);", 5),
    ("shift", "unsigned a; a = 0x80000000u; return (int)(a >> 31);", 1),
)
def test_unsigned_arithmetic_is_left_alone(label, body, expected):
    """The helper and the sign-extending shift cost nothing where the types
    say they are not needed -- which is most of this machine's arithmetic."""
    returns(f"int main(void) {{ {body} }}", expected)


def test_pointer_difference_can_be_negative():
    returns("int main(void){ int v[8]; int *p; int *q; p = &v[1]; q = &v[5];"
            "                return (int)(p - q); }", -4)


# --- globals are built into the image, so their values must be known --------

@cases(
    ("a constant expression", "int a = 2*3+1;", 7),
    ("a negative number", "int a = -1;", -1),
    ("sizeof", "int a = sizeof(int) * 2;", 8),
    ("a macro that expands to an expression", "#define N (4*2)\nint a = N;", 8),
    ("a cast", "int a = (char)300;", 44),
    ("a conditional", "int a = 1 ? 9 : 4;", 9),
    ("a character", "int a = 'A';", 65),
    ("a plain literal, as ever", "int a = 7;", 7),
)
def test_global_initialisers_fold(label, declaration, expected):
    returns(declaration.replace("\\n", "\n") + "\nint main(void){ return a; }", expected)


def test_a_global_can_hold_an_address():
    returns("int v = 42;\nint *p = &v;\nint main(void){ return *p; }", 42)


def test_a_global_table_of_function_pointers():
    """Impossible before: every entry was silently zero, so calling one
    jumped to address 0."""
    returns("typedef int (*cb)(int);\n"
            "int one(int x){ return x + 1; }\n"
            "int two(int x){ return x + 2; }\n"
            "cb table[] = {one, two};\n"
            "int main(void){ return table[1](5); }", 7)


def test_a_global_array_folds_its_elements():
    returns("int t[] = {2*3+1, -1, sizeof(int)};\n"
            "int main(void){ return t[0] + t[1] + t[2]; }", 10)


# --- struct assignment copies the whole struct ------------------------------

def test_struct_assignment_copies_every_word():
    returns("struct P { int a; int b; int c; };\n"
            "int main(void){ struct P x; struct P y;\n"
            "  x.a = 1; x.b = 2; x.c = 3; y.a = 7; y.b = 8; y.c = 9;\n"
            "  y = x; return y.a * 100 + y.b * 10 + y.c; }", 123)


def test_struct_assignment_through_pointers():
    returns("struct P { int a; int c; };\n"
            "int main(void){ struct P x; struct P y; struct P *p; struct P *q;\n"
            "  x.c = 3; y.c = 9; p = &y; q = &x; *p = *q; return y.c; }", 3)


def test_struct_copies_into_a_declaration():
    returns("struct P { int a; int c; };\n"
            "int main(void){ struct P x; x.a = 1; x.c = 3;\n"
            "  { struct P y = x; return y.c; } }", 3)


def test_struct_assignment_of_a_global_and_a_member():
    returns("struct Inner { int u; int v; };\n"
            "struct Outer { int k; struct Inner in; };\n"
            "struct Outer g;\n"
            "int main(void){ struct Inner i; i.u = 4; i.v = 5;\n"
            "  g.in = i; return g.in.u * 10 + g.in.v; }", 45)


def test_a_big_struct_copies_in_a_loop():
    returns("struct Big { int a[10]; };\n"
            "int main(void){ struct Big x; struct Big y; int i;\n"
            "  for (i = 0; i < 10; i++) { x.a[i] = i * 2; y.a[i] = 0; }\n"
            "  y = x; return y.a[9] + y.a[0]; }", 18)


def test_a_struct_copy_leaves_its_neighbours_alone():
    returns("struct P { int a; int b; int c; };\n"
            "int main(void){ int before; struct P x; struct P y; int after;\n"
            "  before = 11; after = 22; x.a = 1; x.b = 2; x.c = 3;\n"
            "  y = x; return before + after + y.c; }", 36)


# --- char is signed, and says so (docs/compiler_plan.md C7) -----------------
#
# MR loads a byte zero-extended, so `char c = -1;` used to compare equal to
# 255 and (int)c was 255. unsigned char is untouched and still costs one
# instruction.

@cases(
    ("assigned a negative", "char c; c = -1; return (int)c;", -1),
    ("compared with a negative", "char c; c = -1; return c == -1;", 1),
    ("tested for less than zero", "char c; c = -1; if (c < 0) return 5; return 9;", 5),
    ("at the positive edge", "char c; c = 127; return (int)c;", 127),
    ("cast from a bigger number", "return (char)200;", -56),
    ("cast that also narrows", "return (char)300;", 44),
    ("in arithmetic", "char a; char b; a = -10; b = 3; return a + b;", -7),
    ("in an array", "char v[4]; v[0] = -3; return (int)v[0];", -3),
    ("through a pointer", "char v[4]; char *p; v[1] = -9; p = v; return (int)p[1];", -9),
)
def test_char_is_signed(label, body, expected):
    returns(f"int main(void) {{ {body} }}", expected)


def test_a_char_struct_member_and_parameter_are_signed():
    returns("struct S { char a; char b; };\n"
            "int f(char c){ return (int)c; }\n"
            "int main(void){ struct S s; s.b = -7; return f(s.b); }", -7)


def test_a_global_char_is_signed():
    returns("char g = -5;\nint main(void){ return (int)g; }", -5)


@cases(
    ("holds 128 and above", "unsigned char c; c = 200; return (int)c;", 200),
    ("compares as unsigned", "unsigned char c; c = 200; return c > 100;", 1),
    ("cast stays in range", "return (int)(unsigned char)200;", 200),
)
def test_unsigned_char_is_not_touched(label, body, expected):
    returns(f"int main(void) {{ {body} }}", expected)


def test_a_string_scan_still_stops_at_the_terminator():
    """The loop everything on this machine is built on: sign extension must
    not change what counts as the end of a string."""
    returns("int main(void){ char v[4]; int n; v[0] = 65; v[1] = 0; n = 0;\n"
            "  while (v[n]) n++; return n; }", 1)


# --- `char *p = "..."` is a pointer, `char v[] = "..."` is an array (C8) ----

def test_a_global_string_pointer_is_a_pointer():
    """It used to become char[3]: the parser could not tell `char *p` from
    `char p[]`, so the image held the characters where the pointer goes."""
    returns('char *p = "hi";\nint main(void){ return (int)sizeof(p) * 100 + p[0]; }',
            4 * 100 + 104)


def test_a_global_string_pointer_can_be_reassigned():
    returns('char *p = "hi";\nchar *q = "XY";\n'
            'int main(void){ p = q; return (int)p[0]; }', 88)


def test_a_global_char_array_still_holds_its_own_bytes():
    returns('char v[] = "hi";\n'
            'int main(void){ v[0] = 88; return (int)sizeof(v) * 100 + v[0]; }',
            3 * 100 + 88)


def test_an_unsized_array_still_takes_its_length_from_the_initialiser():
    returns("int t[] = {1,2,3};\nint main(void){ return (int)sizeof(t) + t[2]; }", 15)


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
    # Silent miscompiles until 2026-09-20, now refused (docs/compiler_plan.md)
    ("struct P { int a; };\nint f(struct P p){ return p.a; }\nint main(void){ return 0; }",
     "by value"),
    ("struct P { int a; };\nstruct P mk(void){ struct P r; return r; }\n"
     "int main(void){ return 0; }", "return"),
    ("int a = 3;\nint b = a;\nint main(void){ return b; }", "compile-time constant"),
    ("int a = 1 / 0;\nint main(void){ return a; }", "division by zero"),
    ("struct P { int a; };\nint main(void){ struct P x; struct P y; struct P z;\n"
     "  z = (y = x); return 0; }", "whole statement"),
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


# --- where the program runs: cc.py --org ----------------------------------

def test_a_program_built_for_another_address_runs_there():
    """cc.py --org (docs/os_cd.md): the code and data move, and every
    absolute address in them moves too -- a global table, a string, a call
    through a function pointer. Built for BIOS2_LOAD_ADDR and run there, it
    gives the same answer, and nothing lands at PROGRAM_LOAD_ADDR."""
    source = """
        int table[3] = {5, 7, 11};
        int twice(int x) { return x + x; }
        int main(void) {
            int (*f)(int);
            char *word;
            f = twice;
            word = "pigeon";
            return f(table[2]) + word[1];
        }"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.asm"
        path.write_text(compile_to_asm(source, "t.c", origin="BIOS2_LOAD_ADDR"))
        image = Assembler(str(path)).assemble()
    assert image != build(source), "the origin changed nothing"
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, BIOS2_LOAD_ADDR)
    cpu = CPU(ram)
    cpu.pc = BIOS2_LOAD_ADDR
    for _ in range(STEP_LIMIT):
        if cpu.run() == 1:
            break
    else:
        raise AssertionError(f"did not halt within {STEP_LIMIT:,} instructions")
    assert cpu.reg.read(0) == 22 + ord("i"), f"main() returned {cpu.reg.read(0)}"
    assert cpu.sp == STACK_TOP, f"hardware stack unbalanced: SP={cpu.sp:#x}"
    assert bytes(ram.mem[PROGRAM_LOAD_ADDR:PROGRAM_LOAD_ADDR + len(image)]) == \
        bytes(len(image)), "something was written where the program would normally be"


@cases(("BIOS2_LOAD_ADDR", "BIOS2_LOAD_ADDR"),
       ("PROGRAM_LOAD_ADDR", "PROGRAM_LOAD_ADDR"),
       ("0x07000000", "0x7000000"),
       ("131072", "0x20000"))
def test_org_takes_a_number_or_a_memory_map_name(text, emitted):
    """A name stays a name, so the assembly says what it means."""
    assert origin_of(text) == emitted


@cases(("not a name", "nowhere"),
       ("not where an instruction can start", "0x20004"),
       ("past the end of RAM", "0x08000000"),
       ("negative", "-8"))
def test_org_refuses_what_is_not_an_address(label, text):
    try:
        origin_of(text)
    except ValueError:
        return
    raise AssertionError(f"{label}: --org {text} was accepted")


# --- variadic functions --------------------------------------------------------

SUM = ("#include <pigeon/stdarg.h>\n"
       "int sum(int count, ...) {\n"
       "    va_list ap; int total = 0;\n"
       "    va_start(ap, count);\n"
       "    while (count > 0) { total = total + va_arg(ap, int); count--; }\n"
       "    va_end(ap);\n"
       "    return total;\n"
       "}\n")


@cases(("no extra arguments", "sum(0)", 0),
       ("one", "sum(1, 5)", 5),
       ("eight, the most", "sum(8, 1, 2, 3, 4, 5, 6, 7, 8)", 36),
       ("negative ones", "sum(3, -1, -2, 10)", 7))
def test_a_variadic_function_takes_up_to_eight_extra_arguments(label, call, expected):
    returns(SUM + f"int main(void) {{ return {call}; }}", expected)


def test_extra_arguments_can_be_chars_and_pointers():
    returns("#include <pigeon/stdarg.h>\n"
            "int pick(char *kinds, ...) {\n"
            "    va_list ap; int total = 0;\n"
            "    va_start(ap, kinds);\n"
            "    while (*kinds) {\n"
            "        if (*kinds == 'c') total = total + va_arg(ap, char);\n"
            "        else if (*kinds == 's') total = total + *va_arg(ap, char *);\n"
            "        else total = total + va_arg(ap, int);\n"
            "        kinds++;\n"
            "    }\n"
            "    return total;\n"
            "}\n"
            "int main(void) { char c = 'A'; return pick(\"csi\", c, \"B\", 1000); }",
            65 + 66 + 1000)


def test_a_va_list_can_be_passed_on():
    returns("#include <pigeon/stdarg.h>\n"
            "int vsum(int count, va_list ap) {\n"
            "    int t = 0;\n"
            "    while (count > 0) { t = t + va_arg(ap, int); count--; }\n"
            "    return t;\n"
            "}\n"
            "int sum(int count, ...) { va_list ap; int t; va_start(ap, count);"
            " t = vsum(count, ap); va_end(ap); return t; }\n"
            "int main(void) { return sum(4, 10, 20, 30, 40); }", 100)


def test_a_variadic_function_through_a_pointer():
    returns(SUM + "int main(void) { int (*f)(int, ...) = sum; return f(3, 1, 2, 3) * 10 + f(1, 9); }",
            69)


def test_extra_arguments_that_call_functions_themselves():
    """The staging path: a later argument calls something."""
    returns(SUM + "int id(int x) { return x; }\n"
            "int main(void) { return sum(3, id(1), sum(2, id(2), 3), id(4)); }", 10)


def test_recursion_through_a_variadic_function():
    returns("#include <pigeon/stdarg.h>\n"
            "int down(int n, ...) {\n"
            "    va_list ap; int carried;\n"
            "    va_start(ap, n);\n"
            "    carried = va_arg(ap, int);\n"
            "    if (n == 0) return carried;\n"
            "    return down(n - 1, carried + n);\n"
            "}\n"
            "int main(void) { return down(10, 0); }", 55)


def test_neither_locals_nor_calls_overwrite_the_extra_arguments():
    """The eight slots are reserved: the locals start after them, and so do
    the frames of the function's own calls."""
    returns("#include <pigeon/stdarg.h>\n"
            "int three(int a, int b, int c) { return a + b + c; }\n"
            "int f(int n, ...) {\n"
            "    int a; int b; int t = 0; int i; va_list ap;\n"
            "    a = 7; b = 9;\n"
            "    t = three(100, 200, 300);\n"
            "    va_start(ap, n);\n"
            "    for (i = 0; i < n; i++) t = t + va_arg(ap, int);\n"
            "    return t + a * 1000 + b * 10000;\n"
            "}\n"
            "int main(void) { return f(8, 1, 2, 3, 4, 5, 6, 7, 8); }",
            600 + 36 + 7000 + 90000)


@cases(("nine extra arguments", SUM + "int main(void) { return sum(1, 1, 2, 3, 4, 5, 6, 7, 8, 9); }",
        "up to 8 more"),
       ("a struct as an extra argument",
        SUM + "struct P { int a; int b; };\nint main(void) { struct P p; return sum(1, p); }",
        "extra argument"),
       ("too few named arguments", SUM + "int main(void) { return sum(); }", "argument"),
       ("'...' with nothing before it", "int f(...) { return 0; }\nint main(void) { return 0; }",
        "needs a named parameter"))
def test_variadic_calls_the_compiler_refuses(label, source, fragment):
    try:
        build(source)
    except CompileError as e:
        assert fragment in str(e), f"{label}: expected {fragment!r} in: {e}"
        return
    raise AssertionError(f"{label}: should have been refused")


# --- hand-written assembly: #asm ----------------------------------------------

ADD_TEN = r'''
extern int add_ten;
unsigned seen;
int triple(int x) { return x * 3; }
int main(void) {
    seen = 5u;
    return ((int (*)(void))&add_ten)();
}
'''

# Reads a C global, and calls a C function the way C does: its argument
# at [F], where add_ten's own caller already moved F.
ADD_TEN_ASM = """
add_ten:
    MOV C, #__g_seen
    MRW A, C
    ADD A, A, #10
    MWW F, A
    CALL triple
    RET
"""


def in_folder(files, main="main.c"):
    """Write {name: text} into a folder and compile `main` from there, so
    #asm paths resolve the way they do on disk. Returns (image, error)."""
    with tempfile.TemporaryDirectory() as d:
        for name, text in files.items():
            path = Path(d) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        source = Path(d) / main
        try:
            text = compile_to_asm(source.read_text(), str(source))
        except CompileError as e:
            return None, e
        asm = Path(d) / "out.asm"
        asm.write_text(text)
        return Assembler(str(asm)).assemble(), None


def test_asm_routines_and_c_share_labels():
    image, error = in_folder({"main.c": '#asm "entry.asm"\n' + ADD_TEN,
                              "entry.asm": ADD_TEN_ASM})
    assert error is None, error
    cpu = execute(image)
    assert cpu.reg.read(0) == (5 + 10) * 3 and cpu.sp == STACK_TOP


def test_an_asm_path_is_relative_to_the_file_that_names_it():
    image, error = in_folder({"main.c": '#include "kernel/k.h"\n' + ADD_TEN,
                              "kernel/k.h": '#asm "k.asm"\n',
                              "kernel/k.asm": ADD_TEN_ASM})
    assert error is None, error
    assert execute(image).reg.read(0) == 45


def test_assembly_named_twice_is_placed_once():
    """Otherwise its labels would be duplicates, which the assembler refuses."""
    image, error = in_folder({"main.c": '#asm "entry.asm"\n#include "again.h"\n' + ADD_TEN,
                              "again.h": '#asm "entry.asm"\n',
                              "entry.asm": ADD_TEN_ASM})
    assert error is None, error
    assert execute(image).reg.read(0) == 45


def test_missing_assembly_is_reported_at_its_line():
    _, error = in_folder({"main.c": '/* the kernel */\n#asm "missing.asm"\n' + ADD_TEN})
    assert error is not None, "compiled without its assembly"
    assert str(error).endswith("main.c:2:1: cannot find assembly 'missing.asm'"), str(error)


def test_editing_the_assembly_makes_a_build_stale():
    import os
    from emulator.programs import Program
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "main.c").write_text('#asm "entry.asm"\n' + ADD_TEN)
        (d / "entry.asm").write_text(ADD_TEN_ASM)
        program = Program(name="main", source=d / "main.c", binary=d / "build" / "main.bin")
        program.ensure_built(quiet=True)
        assert not program.stale
        later = (d / "build" / "main.bin").stat().st_mtime + 10
        os.utime(d / "entry.asm", (later, later))
        assert program.stale, "an edited #asm file did not make the build stale"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "pigeon-cc"))
