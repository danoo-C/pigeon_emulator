"""user/graph.c -- the graphing calculator, compiled and run.

Two kinds of test here.

The arithmetic ones reach INSIDE the program. graph.c carries its own Q16
fixed point because <pigeon/math.h> is Q8 and a grapher needs the other
trade (see the file's header), and a fixed-point routine that is merely
close is worthless -- so every function is checked against the same
expression evaluated in double precision on the host. To do that the
file's main() is cut off and a harness main() put in its place, which is
also why MAIN_MARKER is asserted rather than searched for quietly: if
graph.c stops having exactly that line, these tests must fail loudly
rather than silently test nothing.

The rendering ones boot the real machine and look at the screen.

    python3 tests/test_graph.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run_module                                     # noqa: E402
from emulator.machine import Machine                               # noqa: E402
from emulator.memory_map import PROGRAM_LOAD_ADDR                  # noqa: E402
from test_libs import run                                          # noqa: E402

GRAPH_C = REPO_ROOT / "user" / "graph.c"
GRAPH_BIN = REPO_ROOT / "build" / "graph.bin"
MAIN_MARKER = "int main(void) {"

ONE = 65536.0
CURVE_RGB = b"\xff\xd0\x3f"          # CURVE, 0xFF3FD0FF, as the framebuffer stores it

# What the fixed point is allowed to be out by. The observed worst case
# across this battery is 7.6e-5 relative; anything an order of magnitude
# past that is a broken table or a lost sign, not rounding.
ABS_TOL = 6e-4
REL_TOL = 3e-4


def q16(v):
    return int(round(v * ONE))


def _harness(cases):
    """graph.c with its UI main() replaced by one that evaluates `cases`.

    Each case leaves four words behind: the value, the undefined flag,
    whether it parsed, and where the parse stopped.
    """
    source = GRAPH_C.read_text()
    assert MAIN_MARKER in source, f"{GRAPH_C} no longer declares `{MAIN_MARKER}`"
    body = source[:source.index(MAIN_MARKER)]

    exprs = ",\n".join('    "%s"' % e.replace("\\", "\\\\").replace('"', '\\"')
                       for e, _ in cases)
    xs = ",\n".join("    %d" % q16(x) for _, x in cases)
    return body + """
#define TN %d
static char *T_EXPR[TN] = {
%s
};
static int T_X[TN] = {
%s
};
int main(void) {
    int *out = (int *)malloc(TN * 16);
    int i;
    for (i = 0; i < TN; i++) {
        if (compile_expr(T_EXPR[i]) == 0) {
            out[i*4] = 0; out[i*4+1] = 0; out[i*4+2] = 0; out[i*4+3] = p_errpos;
            continue;
        }
        out[i*4] = eval(T_X[i]);
        out[i*4+1] = ev_bad;
        out[i*4+2] = 1;
        out[i*4+3] = 0;
    }
    return (int)out;
}
""" % (len(cases), exprs, xs)


def evaluate(cases):
    """[(expression, x)] -> [(value, undefined, parsed, error_position)]."""
    cpu = run(_harness(cases), "display.c", "input.c", "math.c", "mem.c")
    base = cpu.reg.read(0)
    out = []
    for i in range(len(cases)):
        raw = cpu.ram.read_word(base + i * 16)
        out.append((
            (raw - (1 << 32) if raw >= (1 << 31) else raw) / ONE,
            cpu.ram.read_word(base + i * 16 + 4),
            cpu.ram.read_word(base + i * 16 + 8),
            cpu.ram.read_word(base + i * 16 + 12),
        ))
    return out


# --- arithmetic -------------------------------------------------------------

# (as typed into the bar, the same thing in Python, x)
AGAINST_FLOAT = [
    ("2sin(x)",     "2*math.sin(x)",       1.0),
    ("sin(x)",      "math.sin(x)",         0.7),
    ("sin(x)",      "math.sin(x)",        -3.9),
    ("sin(x)",      "math.sin(x)",        40.0),   # far outside one turn
    ("cos(x)",      "math.cos(x)",         2.4),
    ("tan(x)",      "math.tan(x)",         1.0),
    ("sqrt(x)",     "math.sqrt(x)",        2.0),
    ("sqrt(x)",     "math.sqrt(x)",        0.25),
    ("sqrt(x)",     "math.sqrt(x)",      900.0),
    ("ln(x)",       "math.log(x)",        10.0),
    ("ln(x)",       "math.log(x)",         0.125),
    ("log(x)",      "math.log10(x)",    1000.0),
    ("exp(x)",      "math.exp(x)",         2.0),
    ("exp(x)",      "math.exp(x)",        -3.0),
    ("atan(x)",     "math.atan(x)",        0.5),
    ("atan(x)",     "math.atan(x)",        7.0),
    ("asin(x)",     "math.asin(x)",        0.5),
    ("acos(x)",     "math.acos(x)",       -0.75),
    ("pi",          "math.pi",             0.0),
    ("e",           "math.e",              0.0),
    ("1.1",         "1.1",                 0.0),
    ("3.14159",     "3.14159",             0.0),
    ("x^0.5",       "x**0.5",              9.0),
    ("exp(-x^2)",   "math.exp(-x**2)",     1.2),
    ("sqrt(1-x^2)", "math.sqrt(1-x**2)",   0.6),
    ("sin(x*2)*2+(x^2*1.1-x^2)", "math.sin(x*2)*2+(x**2*1.1-x**2)",  3.0),
    ("sin(x*2)*2+(x^2*1.1-x^2)", "math.sin(x*2)*2+(x**2*1.1-x**2)", -7.5),
]

# Things that must come out EXACT, because they are about how the parser
# groups the input rather than about how well Q16 approximates anything.
EXACT = [
    ("1+2*3",      7.0,    0.0),
    ("(1+2)*3",    9.0,    0.0),
    ("2^3^2",    512.0,    0.0),     # right-associative, not (2^3)^2 = 64
    ("-2^2",      -4.0,    0.0),     # unary minus is looser than ^
    ("2^-2",       0.25,   0.0),
    ("10-3-2",     5.0,    0.0),     # left-associative
    ("100/8/2",    6.25,   0.0),
    ("7%3",        1.0,    0.0),
    ("-7%3",      -1.0,    0.0),     # sign follows the dividend, as C does
    ("2x",         6.5,    3.25),    # juxtaposition is a multiply
    ("3(x+1)",    -4.5,   -2.5),
    ("(x+1)(x-1)", 15.0,   4.0),
    ("x^2",      144.0,   12.0),
    ("x^3",      -64.0,   -4.0),     # a negative base needs the integer path
    ("x^-1",       0.125,  8.0),
    ("floor(x)",  -3.0,   -2.5),
    ("ceil(x)",   -2.0,   -2.5),
    ("round(x)",  -2.0,   -2.5),
    ("sign(x)",   -1.0,   -2.5),
    ("min(x,2)",   2.0,    5.0),
    ("max(x,2)",   2.0,   -5.0),
    ("0.0625",     0.0625, 0.0),
]

UNDEFINED = ["1/0", "sqrt(0-1)", "ln(0)", "x%0", "(0-2)^0.5"]

# (as typed, where the parser should stop)
BAD_SYNTAX = [
    ("sin(x",   5),      # at the end, where the ) is missing
    ("2+",      2),
    ("(x+1",    4),
    ("wat(x)",  0),      # at the name, not past it
    ("min(x)",  5),      # where the second argument should start
    ("1 2 )",   4),
    ("",        0),
]


def test_arithmetic_matches_floating_point():
    got = evaluate([(c, x) for c, _, x in AGAINST_FLOAT])
    for (c, py, x), (value, bad, parsed, _) in zip(AGAINST_FLOAT, got):
        assert parsed, f"{c!r} did not parse"
        assert not bad, f"{c!r} at x={x} came back undefined"
        want = eval(py, {"math": math, "x": x})
        slack = max(ABS_TOL, abs(want) * REL_TOL)
        assert abs(value - want) <= slack, (
            f"{c!r} at x={x}: got {value!r}, want {want!r} "
            f"(off by {abs(value - want):.2e}, allowed {slack:.2e})")


def test_grouping_and_precedence_are_exact():
    got = evaluate([(c, x) for c, _, x in EXACT])
    for (c, want, x), (value, bad, parsed, _) in zip(EXACT, got):
        assert parsed, f"{c!r} did not parse"
        assert not bad, f"{c!r} came back undefined"
        assert value == want, f"{c!r} at x={x}: got {value!r}, want {want!r}"


def test_out_of_domain_samples_are_flagged_not_guessed():
    """A pole or a bad domain has to break the curve, not plot a zero."""
    got = evaluate([(c, 1.0) for c in UNDEFINED])
    for c, (_, bad, parsed, _) in zip(UNDEFINED, got):
        assert parsed, f"{c!r} should parse -- it is only undefined at this x"
        assert bad, f"{c!r} was not flagged as undefined"


def test_syntax_errors_report_where_they_stopped():
    """The bar underlines the character the parser gave up on, so the
    position matters as much as the refusal."""
    got = evaluate([(c, 0.0) for c, _ in BAD_SYNTAX])
    for (c, at), (_, _, parsed, pos) in zip(BAD_SYNTAX, got):
        assert not parsed, f"{c!r} parsed, and should not have"
        assert pos == at, f"{c!r}: error reported at {pos}, expected {at}"


def test_a_number_keeps_four_decimal_places():
    """Past the fourth digit the fraction's numerator would overflow the
    32-bit divide that turns it into Q16, so the rest is dropped."""
    (value, _, _, _), = evaluate([("0.0001", 0.0)])
    assert abs(value - 0.0001) < 2e-5


# --- on the machine ---------------------------------------------------------

def boot_and_render(setup, budget=12_000_000):
    """Boot graph.bin through the real BIOS, let it draw, then act."""
    assert GRAPH_BIN.exists(), "build/graph.bin is missing; run start_emulator.py graph"
    machine = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"),
                      program_path=str(GRAPH_BIN))
    shots = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            while machine.cpu.pc < PROGRAM_LOAD_ADDR:
                if machine.step() == 1:
                    break
            for _ in range(budget):
                if machine.step() == 1:
                    break
            shots.append(machine.display_io.snapshot())
            setup(machine.hid)
            for _ in range(budget):
                if machine.step() == 1:
                    break
            shots.append(machine.display_io.snapshot())
        return shots
    finally:
        machine.close()


def curve_pixels(fb):
    return sum(1 for i in range(0, len(fb), 4) if fb[i:i + 3] == CURVE_RGB)


def test_it_draws_the_default_curve():
    before, _ = boot_and_render(lambda hid: None)
    assert curve_pixels(before) > 200, "the plot area is empty"


def test_zooming_redraws_the_curve_differently():
    def zoom_in(hid):
        for _ in range(6):
            hid.push_key(0x82, True)            # KEY_UP
            hid.push_key(0x82, False)

    before, after = boot_and_render(zoom_in)
    assert curve_pixels(after) > 100, "the curve vanished when zoomed"
    assert before != after, "the screen did not change when zoomed"


if __name__ == "__main__":
    raise SystemExit(run_module(globals(), "graphing calculator"))
