"""Interrupts, faults and the stack pointer: phase 2 of docs/kernel.md (§13, §17).

Six instructions come after SHR: GETSP, SETSP, EI, DI, IRET and SETIV. The
CPU delivers faults and interrupts through a vector table. It pushes the
flags word and the address to return to, turns interrupts off and jumps to
the handler, and IRET undoes all four. A fault with no handler stops the
emulator exactly as it did before there were vectors. The timer's TICK
command and HID's break raise interrupts, and the machine asks them every
CLOCK_SAMPLE_INTERVAL instructions, in step() and run() alike.

Everything is run: hand-written programs on a bare CPU and on a Machine,
compiled C interrupted after every instruction, and C that abandons a
divide by zero twenty calls deep, as the kernel's exit() will.

    python3 tests/test_interrupts.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import logging
import struct
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                   # noqa: E402
from assembler.assembler import Assembler, assemble_file                # noqa: E402
from compiler.cc import compile_units                                   # noqa: E402
from emulator.cpu import CPU                                            # noqa: E402
from emulator.devices import timer as timer_device                      # noqa: E402
from emulator.devices.hid import CMD_SET_BREAK, HID                     # noqa: E402
from emulator.devices.keycodes import KEY_LCTRL, KEY_RCTRL              # noqa: E402
from emulator.devices.timer import (                                    # noqa: E402
    CMD_START, CMD_STATUS, CMD_STOP, CMD_TICK, STATUS_DONE, STATUS_RUNNING,
    STATUS_STOPPED, Timer)
from emulator.instruction_set import (                                  # noqa: E402
    INSTRUCTIONS_BY_NAME, NONE_REG, disassemble, encode)
from emulator.machine import Machine                                    # noqa: E402
from emulator.memory_map import (                                       # noqa: E402
    CH_HID, CH_TIMER, FLAG_IE, FLAG_LESS, FLAG_ZERO, HEAP_START, PROGRAM_LOAD_ADDR,
    RAM_SIZE, STACK_TOP, VEC_BAD_FETCH, VEC_BAD_OPCODE, VEC_BREAK, VEC_DIV_ZERO,
    VEC_TIMER)
from emulator.ram import RAM                                            # noqa: E402

# A Machine built with no program logs that channel 1 is empty.
logging.getLogger("emulator.machine").setLevel(logging.ERROR)

SMALL = 1 << 20                 # the bare CPU's RAM
F = 5                           # the frame pointer's register number
IRET = INSTRUCTIONS_BY_NAME["IRET"].opcode

_BUILD = tempfile.TemporaryDirectory()
BIOS = Path(_BUILD.name) / "bios.bin"
with contextlib.redirect_stdout(io.StringIO()):
    assemble_file(REPO_ROOT / "firmware" / "bios.asm", BIOS, quiet=True)


# --- running programs -------------------------------------------------------------

def assemble(text, org, ram_size):
    """Assembly -> (image, symbols). LAST_WORD is the last word of RAM: an
    instruction fetched there runs past the end of memory."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.asm"
        path.write_text(f"LAST_WORD = {ram_size - 4:#x}\n.ORG {org:#x}\n{text}")
        assembler = Assembler(str(path))
        image = assembler.assemble()
        return image, {**assembler.static_defs, **assembler.symbols}


def execute(text, how, limit=200_000, each=None):
    """Run a program to HALT three ways: "cpu", CPU.run() on a bare CPU
    with SMALL RAM, the program at 0; "step", Machine.step(); and "run",
    Machine.run(), the program at PROGRAM_LOAD_ADDR. each(cpu, symbols)
    runs before every instruction, and may raise interrupts; not with
    "run", which runs on its own."""
    machine = None
    if how == "cpu":
        image, symbols = assemble(text, 0, SMALL)
        ram = RAM(SMALL)
        ram.load_bytes(image, 0)
        cpu = CPU(ram)
        cpu.sp = SMALL - 4
        step = cpu.run
    else:
        image, symbols = assemble(text, PROGRAM_LOAD_ADDR, RAM_SIZE)
        machine = Machine(bios_path=str(BIOS))
        ram, cpu = machine.ram, machine.cpu
        ram.load_bytes(image, PROGRAM_LOAD_ADDR)
        cpu.pc = PROGRAM_LOAD_ADDR
        step = machine.step
    try:
        if how in ("run", "step, then run"):
            assert each is None, "run() takes no hook"
            stepped = 15_000 if how == "step, then run" else 0
            for _ in range(stepped):
                assert step() == 0, "halted while stepping"
            machine.run(deadline=time.time() + 60)
            assert cpu.halted, "run() stopped at its deadline, not at HALT"
            steps = stepped + machine.total_instructions
        else:
            for steps in range(limit):
                if each is not None:
                    each(cpu, symbols)
                if step() == 1:
                    break
            else:
                raise AssertionError(f"no HALT within {limit:,} instructions, PC={cpu.pc:#x}")
        return SimpleNamespace(cpu=cpu, ram=ram, sym=symbols, steps=steps, machine=machine,
                               top=ram.size - 4,
                               reg=lambda name: cpu.reg.read(name),
                               word=lambda name, i=0: ram.read_word(symbols[name] + 4 * i))
    finally:
        if machine is not None:
            machine.close()


def raise_at(label, vector):
    """A hook raising `vector` once, when PC first reaches `label`."""
    done = []

    def each(cpu, symbols):
        if not done and cpu.pc == symbols[label]:
            cpu.interrupt(vector)
            done.append(True)
    return each


def io(channel, command, length=0, address=0):
    """Assembly sending one command: the header, then the channel, which
    fires it. Uses C and D."""
    return f"""
    MOV D, #IO_START
    ADD C, D, #IO_R_W
    MWW C, #0
    ADD C, D, #IO_COMMAND
    MWW C, #{command}
    ADD C, D, #IO_LENGTH
    MWW C, #{length}
    ADD C, D, #IO_ADDRESS
    MWW C, #{address}
    MWW D, #{channel}
"""


# --- the six instructions ---------------------------------------------------------

def test_the_six_instructions_come_after_shr():
    """Appended, so every existing opcode and every binary already built
    stays the same."""
    names = ["GETSP", "SETSP", "EI", "DI", "IRET", "SETIV"]
    assert INSTRUCTIONS_BY_NAME["SHR"].opcode == 28
    assert [INSTRUCTIONS_BY_NAME[n].opcode for n in names] == list(range(29, 35))


def test_they_assemble_and_disassemble():
    image, symbols = assemble("""
    GETSP C
    SETSP D
    SETSP #0x1234
    EI
    DI
    IRET
    SETIV E
    SETIV #table
table:
""", 0, SMALL)
    assert image == (encode("GETSP", dst=2) + encode("SETSP", src1=3)
                     + encode("SETSP", imm=0x1234) + encode("EI") + encode("DI")
                     + encode("IRET") + encode("SETIV", src1=4)
                     + encode("SETIV", imm=symbols["table"]))
    for name in ("GETSP", "SETSP", "EI", "DI", "IRET", "SETIV"):
        assert f" {name} " in disassemble(encode(name), 0), disassemble(encode(name), 0)


@cases("cpu", "step")
def test_getsp_reads_the_stack_pointer_and_setsp_sets_it(how):
    r = execute("""
    GETSP A
    MOV B, #0x8000
    SETSP B
    GETSP C
    PUSH B
    SETSP #0x9000
    GETSP D
    HALT
""", how)
    assert r.reg("A") == r.top
    assert (r.reg("C"), r.reg("D"), r.cpu.sp) == (0x8000, 0x9000, 0x9000)
    assert r.ram.read_word(0x7FFC) == 0x8000, "PUSH did not use the stack pointer SETSP set"


@cases("cpu", "step", "run")
def test_setsp_gets_back_out_of_fifty_calls(how):
    """What exit() needs: remember SP, call as deep as you like, put SP back,
    and RET returns from the call that remembered it."""
    r = execute("""
    CALL guarded
    HALT
guarded:
    GETSP A
    MOV C, #saved
    MWW C, A
    MOV B, #50
    CALL down
    MOV A, #1
    RET
down:
    SUB B, B, #1
    CMP B, #0
    JZ bail
    CALL down
    RET
bail:
    GETSP E
    MOV C, #saved
    MRW D, C
    SETSP D
    MOV A, #42
    RET
saved:
    .word 0
""", how)
    assert r.reg("A") == 42
    assert r.reg("E") == r.top - 4 - 4 * 50, "the calls did not go fifty deep"
    assert r.cpu.sp == r.top, "the hardware stack is not back where it started"


# --- taking an interrupt -------------------------------------------------------------

INTERRUPTED = """
    SETIV #vectors
    MOV A, #{a}
    CMP A, #5
    EI
here:
    NOP
done:
    HALT
handler:
    GETSP B
    MRW C, B
    ADD B, B, #4
    MRW D, B
    MOV E, #1
    MOV A, #1
    CMP A, #2
    IRET
vectors:
    .word 0, 0, 0, handler, 0
"""


@cases(("equal", 5, FLAG_ZERO, "cpu"), ("less", 3, FLAG_LESS, "cpu"),
       ("greater", 7, 0, "cpu"), ("equal", 5, FLAG_ZERO, "step"),
       ("less", 3, FLAG_LESS, "step"), ("greater", 7, 0, "step"))
def test_an_interrupt_pushes_the_flags_and_where_to_return(label, a, flags, how):
    """Taken before the instruction PC was at. The handler finds the
    address of that instruction on top of the stack, the flags word under
    it, and interrupts off."""
    seen = {}

    def each(cpu, symbols):
        raise_timer(cpu, symbols)
        if cpu.pc == symbols["handler"] + 8 * 4:        # at MOV E: after reading both words
            seen["ie"], seen["sp"] = cpu.ie, cpu.sp
    raise_timer = raise_at("here", VEC_TIMER)
    r = execute(INTERRUPTED.format(a=a), how, each=each)
    assert r.reg("C") == r.sym["here"], f"{label}: returned to {r.reg('C'):#x}"
    assert r.reg("D") == flags | FLAG_IE, f"{label}: flags word {r.reg('D'):#x}"
    assert seen == {"ie": False, "sp": r.top - 8}, f"{label}: {seen}"


@cases(("equal", 5, True, False, "cpu"), ("less", 3, False, True, "cpu"),
       ("greater", 7, False, False, "cpu"), ("equal", 5, True, False, "step"),
       ("less", 3, False, True, "step"), ("greater", 7, False, False, "step"))
def test_iret_puts_back_the_flags_interrupts_and_stack(label, a, zero, less, how):
    """The handler's own CMP leaves the flags as 1 < 2 would. IRET puts
    back the interrupted program's, and the NOP it returns to runs."""
    r = execute(INTERRUPTED.format(a=a), how, each=raise_at("here", VEC_TIMER))
    assert r.reg("E") == 1, f"{label}: the handler never ran"
    assert (r.cpu.zero_flag, r.cpu.less_flag) == (zero, less), f"{label}: flags not restored"
    assert r.cpu.ie, f"{label}: interrupts not back on"
    assert r.cpu.sp == r.top, f"{label}: stack not balanced"
    assert r.cpu.pc == r.sym["done"] + 8, f"{label}: halted at {r.cpu.pc - 8:#x}"


@cases("cpu", "step")
def test_an_interrupt_waits_while_interrupts_are_off(how):
    """Raised during a DI loop, taken right after EI: before the NOP, with
    the loop finished."""
    r = execute("""
    SETIV #vectors
    DI
    MOV B, #0
loop:
    ADD B, B, #1
    CMP B, #100
    JL loop
    EI
after:
    NOP
    HALT
handler:
    MOV C, B
    GETSP D
    MRW E, D
    IRET
vectors:
    .word 0, 0, 0, handler, 0
""", how, each=raise_at("loop", VEC_TIMER))
    assert r.reg("C") == 100, f"taken with the loop at {r.reg('C')}"
    assert r.reg("E") == r.sym["after"], "not taken before the instruction after EI"


@cases("cpu", "step")
def test_pending_interrupts_are_taken_lowest_first_and_do_not_nest(how):
    """Break and the timer raised together. The timer, the lower vector,
    goes first; break waits for its IRET, since a handler runs with
    interrupts off."""
    handler = """
    MOV C, #log_at
    MRW D, C
    MOV E, #{vector}
    MWW D, E
    ADD D, D, #4
    MWW C, D
    IRET
"""

    def both(cpu, symbols):
        if cpu.pc == symbols["here"] and not cpu.pending and not symbols.get("raised"):
            cpu.interrupt(VEC_BREAK)
            cpu.interrupt(VEC_TIMER)
            symbols["raised"] = True
    r = execute(f"""
    SETIV #vectors
    EI
here:
    NOP
    HALT
on_timer:
{handler.format(vector=VEC_TIMER)}
on_break:
{handler.format(vector=VEC_BREAK)}
vectors:
    .word 0, 0, 0, on_timer, on_break
log_at:
    .word log
log:
    .word 0, 0, 0
""", how, each=both)
    assert [r.word("log", i) for i in range(3)] == [VEC_TIMER, VEC_BREAK, 0]
    assert r.cpu.pending == 0 and r.cpu.sp == r.top


@cases("cpu", "step")
def test_an_interrupt_with_no_handler_stops_the_emulator(how):
    try:
        execute("""
    EI
here:
    NOP
    HALT
""", how, each=raise_at("here", VEC_TIMER))
    except RuntimeError as e:
        assert str(e).startswith(f"Interrupt {VEC_TIMER} at PC="), e
        return
    raise AssertionError("an interrupt with no vector table was taken")


def test_the_cpu_dump_shows_the_interrupt_state():
    cpu = CPU(RAM(SMALL))
    cpu.ie, cpu.ivt = True, 0x1234
    cpu.interrupt(VEC_TIMER)
    dump = cpu.dump()
    for part in ("IE=True", "IV=0x00001234", f"PENDING={1 << VEC_TIMER:#x}"):
        assert part in dump, dump


# --- faults ---------------------------------------------------------------------

FAULTS = (
    ("DIV by a register holding 0", "    MOV B, #0\nat:\n    DIV C, A, B\n", VEC_DIV_ZERO,
     ZeroDivisionError, "Division by zero at PC={at:#06x}"),
    ("DIV by #0", "at:\n    DIV C, A, #0\n", VEC_DIV_ZERO,
     ZeroDivisionError, "Division by zero at PC={at:#06x}"),
    ("an unknown opcode", "at:\n    .word 0xFF, 0\n", VEC_BAD_OPCODE,
     RuntimeError, "Unknown opcode 255 at PC={at:#06x}"),
    ("a fetch past the end of memory", "    JMP #LAST_WORD\n", VEC_BAD_FETCH,
     RuntimeError, "Fetch past end of memory at PC={at:#06x}"),
)


def fault_address(label, symbols):
    return symbols["LAST_WORD"] if "fetch" in label else symbols["at"]


@cases(*[(label, body, vector, how) for label, body, vector, _, _ in FAULTS
         for how in ("cpu", "step", "run")])
def test_a_fault_enters_its_handler_with_interrupts_off(label, body, vector, how):
    """The handler finds the faulting instruction's own address on the
    stack, and the flags word under it, interrupts off in it. A DIV by 0
    writes nothing."""
    r = execute(f"""
    SETIV #vectors
    MOV A, #10
    MOV C, #77
{body}
    HALT
h0:
    MOV A, #100
    JMP record
h1:
    MOV A, #101
    JMP record
h2:
    MOV A, #102
    JMP record
record:
    GETSP D
    MRW E, D
    ADD D, D, #4
    MRW F, D
    HALT
vectors:
    .word h0, h1, h2, 0, 0
""", how)
    assert r.reg("A") == 100 + vector, f"{label}: vector {r.reg('A') - 100} taken"
    assert r.reg("E") == fault_address(label, r.sym), f"{label}: returns to {r.reg('E'):#x}"
    assert r.reg("F") == 0, f"{label}: flags word {r.reg('F'):#x}"
    assert r.reg("C") == 77, f"{label}: the faulting DIV wrote its result"
    assert not r.cpu.ie and r.cpu.sp == r.top - 8


@cases(*[(label, body, error, message, table, how)
         for label, body, _, error, message in FAULTS
         for table in ("no vector table", "a table of zeros")
         for how in ("cpu", "step", "run")])
def test_a_fault_with_no_handler_stops_the_emulator_as_before(label, body, error, message,
                                                             table, how):
    """Every program built before there were vectors behaves the same: the
    same exception, with the faulting instruction's address."""
    setiv = "    SETIV #zeros\n" if table == "a table of zeros" else ""
    text = f"""{setiv}    MOV A, #10
{body}
    HALT
zeros:
    .word 0, 0, 0, 0, 0
"""
    symbols = assemble(text, 0 if how == "cpu" else PROGRAM_LOAD_ADDR,
                       SMALL if how == "cpu" else RAM_SIZE)[1]
    try:
        execute(text, how)
    except error as e:
        expected = message.format(at=fault_address(label, symbols))
        assert str(e) == expected, f"{label}, {table}: {e!s}, want {expected}"
        return
    raise AssertionError(f"{label}, {table}: no {error.__name__}")


@cases("cpu", "step", "run")
def test_a_handler_can_fix_the_cause_and_retry(how):
    """The return address is the faulting instruction, so IRET runs the
    DIV again, with the divisor the handler put right. The table's address
    comes from a register, where every other program here gives it as #."""
    r = execute("""
    MOV D, #vectors
    SETIV D
    MOV A, #84
    MOV B, #0
    DIV C, A, B
    HALT
fix:
    MOV B, #2
    IRET
vectors:
    .word fix, 0, 0, 0, 0
""", how)
    assert r.reg("C") == 42 and r.cpu.sp == r.top


@cases(*[(label, bad, target, how)
         for label, bad, target in (
             ("an unknown opcode", "    .word 0xFF, 0", "bad"),
             ("a DIV by 0", "    DIV A, A, #0", "bad"),
             ("past the end of memory", "    NOP", "LAST_WORD"))
         for how in ("cpu", "step", "run")])
def test_a_fault_on_a_fault_handlers_first_instruction_stops_the_emulator(label, bad, target,
                                                                         how):
    """A vector pointing at no code would otherwise enter its handler over
    and over, pushing eight bytes each time."""
    try:
        execute(f"""
    SETIV #vectors
    MOV B, #0
    DIV A, A, B
    HALT
bad:
{bad}
    HALT
vectors:
    .word {target}, {target}, {target}, 0, 0
""", how)
    except RuntimeError as e:
        assert str(e).startswith("Double fault: "), f"{label}: {e}"
        return
    raise AssertionError(f"{label}: no double fault")


@cases("cpu", "step", "run")
def test_a_fault_later_in_a_handler_is_delivered(how):
    """Only the first instruction counts as a double fault: a handler that
    faults further in is entered again."""
    r = execute("""
    SETIV #vectors
    MOV E, #0
    DIV A, A, #0
    HALT
handler:
    ADD E, E, #1
    CMP E, #2
    JZ done
    DIV A, A, #0
done:
    HALT
vectors:
    .word handler, 0, 0, 0, 0
""", how)
    assert r.reg("E") == 2 and r.cpu.sp == r.top - 16


# --- compiled C -----------------------------------------------------------------

def build_c(c_text, asm_text):
    """C compiled for PROGRAM_LOAD_ADDR, with hand-written routines among
    its code, which C reaches as `extern int name;` cast to a function."""
    with tempfile.TemporaryDirectory() as d:
        source = Path(d) / "k.c"
        source.write_text(c_text)
        asm = compile_units([source])
        marker = asm.index("; --- static data ---")
        path = Path(d) / "k.asm"
        path.write_text(asm[:marker] + asm_text.strip("\n") + "\n\n" + asm[marker:])
        assembler = Assembler(str(path))
        return assembler.assemble(), {**assembler.static_defs, **assembler.symbols}


def run_c(image, symbols, every=0, globals_=(), limit=30_000_000):
    """On a bare CPU, raising the timer interrupt after every `every`-th
    instruction that runs with interrupts on. IRET never counts, so the
    instruction it returns to runs before the next interrupt."""
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, PROGRAM_LOAD_ADDR)
    for name, value in globals_:
        ram.write_word(symbols[f"__g_{name}"], value)
    cpu = CPU(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    mem, counted, raised = ram.mem, 0, 0
    for _ in range(limit):
        opcode = mem[cpu.pc]
        if cpu.run() == 1:
            break
        if every and cpu.ie and opcode != IRET:
            counted += 1
            if counted % every == 0:
                cpu.interrupt(VEC_TIMER)
                raised += 1
    else:
        raise AssertionError(f"no HALT within {limit:,} instructions")
    return SimpleNamespace(cpu=cpu, a=cpu.reg.read(0), raised=raised,
                           word=lambda name: ram.read_word(symbols[f"__g_{name}"]))


# docs/kernel.md P2, smaller: a sort, a sieve, recursion and function
# pointers, all comparing and jumping, with a handler whose own C compares.
WORKLOAD = r'''
extern int irq_entry;
extern int k_enable;
extern int k_disable;
unsigned vectors[5];
unsigned irqframes[256];
unsigned ticks;
unsigned interrupted;

void on_tick(void) {
    unsigned i;
    unsigned x;
    x = 0u;
    ticks = ticks + 1u;
    for (i = 0u; i < 3u; i++) {
        if (i != 1u) x = x + i;
    }
}

int sieve(unsigned n) {
    char mark[120];
    unsigned i;
    unsigned j;
    int count;
    count = 0;
    for (i = 0u; i < n; i++) mark[i] = 0;
    for (i = 2u; i < n; i++) {
        if (mark[i] == 0) {
            count++;
            for (j = i + i; j < n; j = j + i) mark[j] = 1;
        }
    }
    return count;
}

int fib(int n) {
    if (n < 2) return n;
    return fib(n - 1) + fib(n - 2);
}

void sort(int *v, int n) {
    int i;
    int j;
    int t;
    for (i = 0; i < n; i++) {
        for (j = 0; j + 1 < n - i; j++) {
            if (v[j] > v[j + 1]) {
                t = v[j];
                v[j] = v[j + 1];
                v[j + 1] = t;
            }
        }
    }
}

int twice(int x) { return x * 2; }
int negate(int x) { return -x; }

int main(void) {
    int v[16];
    int i;
    int sum;
    int (*f)(int);
    vectors[3] = (unsigned)&irq_entry;
    if (interrupted) ((void (*)(void))&k_enable)();
    sum = 0;
    for (i = 0; i < 16; i++) v[i] = ((i * 7919) % 97) - 48;
    sort(v, 16);
    for (i = 0; i + 1 < 16; i++) {
        if (v[i] > v[i + 1]) sum = sum + 1000000;
    }
    for (i = 0; i < 16; i++) sum = sum * 3 + v[i] * (i + 1);
    sum = sum + sieve(120u) * 1000 + fib(9);
    for (i = 0; i < 10; i++) {
        if (i & 1) f = negate;
        else f = twice;
        sum = sum + f(i - 5);
    }
    ((void (*)(void))&k_disable)();
    return sum;
}
'''

SAVE = "\n".join(f"    PUSH {r}" for r in "ABCDEF")
RESTORE = "\n".join(f"    POP {r}" for r in "FEDCBA")
WORKLOAD_ASM = f"""
k_enable:
    SETIV #__g_vectors
    EI
    RET
k_disable:
    DI
    RET
irq_entry:
{SAVE}
    MOV F, #__g_irqframes
    CALL on_tick
{RESTORE}
    IRET
"""

_WORKLOAD = []


def workload():
    if not _WORKLOAD:
        image, symbols = build_c(WORKLOAD, WORKLOAD_ASM)
        _WORKLOAD.extend([image, symbols, run_c(image, symbols).a])
    return _WORKLOAD


@cases(1, 2, 7, 101)
def test_compiled_c_gets_the_same_answer_interrupted_after_every_nth_instruction(every):
    """An interrupt between any two instructions, including a CMP and its
    jump, changes nothing the program can see: its answer, F, and the
    hardware stack are what they are with no interrupts at all."""
    image, symbols, expected = workload()
    r = run_c(image, symbols, every=every, globals_=[("interrupted", 1)])
    assert r.raised > 50, f"only {r.raised} interrupts"
    assert r.word("ticks") == r.raised, f"{r.word('ticks')} handled of {r.raised} raised"
    assert r.a == expected, f"every {every}: {r.a:#x}, want {expected:#x}"
    assert r.cpu.sp == STACK_TOP and r.cpu.reg.read(F) == HEAP_START


def test_c_abandons_a_divide_by_zero_twenty_calls_deep():
    """docs/kernel.md §9's exit and fault path, in miniature. run_guarded
    remembers SP and F with GETSP; the fault handler puts them back with
    SETSP, and returns from run_guarded as if the call had returned 999.
    Then the same routine runs a function that returns normally."""
    image, symbols = build_c(r'''
extern int run_guarded;
extern int abandon;
unsigned vectors[5];
unsigned guarded;
unsigned saved_sp;
unsigned saved_f;

int deep(int n, int d) {
    if (n == 0) return 100 / d;
    return deep(n - 1, d) + 1;
}
int crash(void) { return deep(20, 0); }
int fine(void) { return deep(20, 5); }

int main(void) {
    int a;
    int b;
    vectors[0] = (unsigned)&abandon;
    guarded = (unsigned)crash;
    a = ((int (*)(void))&run_guarded)();
    guarded = (unsigned)fine;
    b = ((int (*)(void))&run_guarded)();
    return a * 1000 + b;
}
''', """
run_guarded:
    SETIV #__g_vectors
    GETSP A
    MOV C, #__g_saved_sp
    MWW C, A
    MOV C, #__g_saved_f
    MWW C, F
    MOV C, #__g_guarded
    MRW E, C
    CALL E
    RET
abandon:
    MOV C, #__g_saved_sp
    MRW A, C
    SETSP A
    MOV C, #__g_saved_f
    MRW F, C
    MOV A, #999
    RET
""")
    r = run_c(image, symbols)
    assert r.a == 999 * 1000 + 40, f"main returned {r.a}"
    assert r.cpu.sp == STACK_TOP, "the abandoned calls left the hardware stack unbalanced"
    assert r.cpu.reg.read(F) == HEAP_START, "F not restored"


# --- the timer ---------------------------------------------------------------------

@contextlib.contextmanager
def clock_at(now):
    """The timer device reads now[0], which the test moves."""
    saved = timer_device.clock
    timer_device.clock = lambda: now[0]
    try:
        yield
    finally:
        timer_device.clock = saved


def status(timer, address=1):
    return struct.unpack("<II", timer.callback(0, CMD_STATUS, 0, address, bytearray()))


def test_a_ticking_timer_is_due_once_a_period_until_stopped():
    """Binary fractions of a second, so the arithmetic is exact."""
    now = [100.0]
    with clock_at(now):
        timer = Timer()
        reply = timer.callback(0, CMD_TICK, 250, 1, bytearray())
        assert struct.unpack("<II", reply) == (STATUS_RUNNING, 250)
        assert not timer.poll()
        now[0] += 0.125
        assert not timer.poll() and status(timer) == (STATUS_RUNNING, 125)
        now[0] += 0.125
        assert timer.poll(), "not due after one period"
        assert not timer.poll(), "due twice for one tick"
        assert status(timer) == (STATUS_RUNNING, 250), "never finishes"
        now[0] += 1.0
        assert timer.poll() and not timer.poll(), "four missed ticks are one interrupt"
        timer.callback(0, CMD_STOP, 0, 1, bytearray())
        now[0] += 1.0
        assert not timer.poll() and status(timer) == (STATUS_STOPPED, 0)


def test_start_makes_a_ticking_timer_a_one_shot_again():
    now = [0.0]
    with clock_at(now):
        timer = Timer()
        timer.callback(0, CMD_TICK, 250, 1, bytearray())
        timer.callback(0, CMD_START, 500, 1, bytearray())
        now[0] += 1.0
        assert not timer.poll() and status(timer) == (STATUS_DONE, 0)


def test_polling_reads_the_clock_only_while_a_timer_ticks():
    """So a machine with no ticking timer reads no clock between
    instructions -- and a test's stepping clock doesn't move."""
    reads = []
    saved = timer_device.clock
    timer_device.clock = lambda: reads.append(1) or 7.0
    try:
        timer = Timer()
        timer.callback(0, CMD_START, 100, 1, bytearray())
        before = len(reads)
        assert not timer.poll() and len(reads) == before
    finally:
        timer_device.clock = saved


TICKS = f"""
    MOV B, #0
    SETIV #vectors
{io(CH_TIMER, CMD_TICK, 10, 1)}
    EI
spin:
    ADD B, B, #1
    MOV C, #ticks
    MRW A, C
    CMP A, #5
    JL spin
    DI
{io(CH_TIMER, CMD_STOP, 0, 1)}
    HALT
tick:
    PUSH A
    PUSH C
    PUSH D
    MOV C, #ticks
    MRW A, C
    SHL D, A, #2
    ADD D, D, #seen
    MWW D, B
    ADD A, A, #1
    MWW C, A
    POP D
    POP C
    POP A
    IRET
vectors:
    .word 0, 0, 0, tick, 0
ticks:
    .word 0
seen:
    .word 0, 0, 0, 0, 0
"""


def test_step_and_run_take_the_timers_interrupts_at_the_same_instructions():
    """docs/kernel.md Q5: a program with a timer must behave the same in the
    debugger. The handler records how far the loop had got at each tick.
    Stepping part of the way and then running must agree too: the two share
    one countdown to the next poll."""
    stepped = execute(TICKS, "step", limit=1_000_000)
    seen = [stepped.word("seen", i) for i in range(5)]
    assert all(b > a for a, b in zip(seen, seen[1:])), f"ticks out of order: {seen}"
    for how in ("run", "step, then run"):
        r = execute(TICKS, how)
        assert [r.word("seen", i) for i in range(5)] == seen, f"{how}: {r.word('seen')}"
        assert r.steps == stepped.steps, f"{how} took {r.steps:,}, step() {stepped.steps:,}"
        assert r.cpu.sp == r.top and r.machine.timer.timers[1].status == STATUS_STOPPED


def test_a_stopped_timer_interrupts_no_more():
    """Three ticks, then STOP, then 90,000 instructions: nine more polls.
    The handler records the spin counter, which is 0 until then. A tick
    raised before the STOP may still arrive, before the spin begins."""
    r = execute(f"""
    MOV B, #0
    SETIV #vectors
{io(CH_TIMER, CMD_TICK, 10, 1)}
    EI
wait:
    MOV C, #ticks
    MRW A, C
    CMP A, #3
    JL wait
    DI
{io(CH_TIMER, CMD_STOP, 0, 1)}
    EI
spin:
    ADD B, B, #1
    CMP B, #30000
    JL spin
    HALT
tick:
    PUSH A
    PUSH C
    MOV C, #ticks
    MRW A, C
    ADD A, A, #1
    MWW C, A
    MOV C, #spun
    MWW C, B
    POP C
    POP A
    IRET
vectors:
    .word 0, 0, 0, tick, 0
ticks:
    .word 0
spun:
    .word 0
""", "step", limit=1_000_000)
    assert r.word("ticks") in (3, 4), f"{r.word('ticks')} ticks"
    assert r.word("spun") == 0, f"a tick arrived {r.word('spun')} loops into the spin"


# --- break ---------------------------------------------------------------------------

def press(hid, *codes):
    for code in codes:
        hid.push_key(code, True)
    for code in reversed(codes):
        hid.push_key(code, False)


def keys_in(hid):
    queued = []
    while (code := hid.pop_key()) != 0:
        queued.append(code)
    return queued


def set_break(hid, on):
    return hid.callback(0, CMD_SET_BREAK, 0, int(on), bytearray())


def test_ctrl_c_is_a_key_until_break_is_on():
    hid = HID()
    press(hid, KEY_LCTRL, ord("c"))
    assert not hid.take_break()
    assert keys_in(hid) == [KEY_LCTRL, ord("c")]


@cases(("left Ctrl, c", KEY_LCTRL, "c"), ("right Ctrl, c", KEY_RCTRL, "c"),
       ("left Ctrl, C", KEY_LCTRL, "C"))
def test_with_break_on_ctrl_c_is_the_break_and_not_a_key(label, ctrl, letter):
    """Neither its press nor its release reaches the guest."""
    hid = HID()
    assert set_break(hid, True) == b"\x01"
    press(hid, ctrl, ord(letter))
    assert hid.take_break(), f"{label}: no break"
    assert not hid.take_break(), f"{label}: one Ctrl+C, two breaks"
    assert keys_in(hid) == [ctrl], label
    events = []
    while (event := hid.pop_key_event()) != b"\x00\x00":
        events.append(event[1])
    assert events == [ctrl, ctrl], f"{label}: key events {events}"
    assert not hid.key_is_down(ord(letter))


def test_with_break_on_c_alone_is_still_a_key():
    hid = HID()
    set_break(hid, True)
    press(hid, ord("c"))
    assert not hid.take_break() and keys_in(hid) == [ord("c")]


def test_turning_break_off_drops_a_break_not_yet_taken():
    hid = HID()
    set_break(hid, True)
    press(hid, KEY_LCTRL, ord("c"))
    assert set_break(hid, False) == b"\x00"
    assert not hid.take_break()
    press(hid, KEY_LCTRL, ord("c"))
    assert not hid.take_break() and keys_in(hid) == [KEY_LCTRL, KEY_LCTRL, ord("c")]


BREAKABLE = f"""
    SETIV #vectors
{{set_break}}
    EI
spin:
    MOV C, #broken
    MRW A, C
    CMP A, #0
    JZ spin
    HALT
on_break:
    MOV C, #broken
    MWW C, #1
    IRET
vectors:
    .word 0, 0, 0, 0, on_break
broken:
    .word 0
"""


@cases(("break on", True), ("break off", False))
def test_ctrl_c_on_a_machine_interrupts_a_spinning_program(label, on):
    """The key is pushed from outside, as the HTTP server would, and the
    program is interrupted within one poll."""
    text = BREAKABLE.format(set_break=io(CH_HID, CMD_SET_BREAK, 0, 1) if on else "")
    image, symbols = assemble(text, PROGRAM_LOAD_ADDR, RAM_SIZE)
    with Machine(bios_path=str(BIOS)) as machine:
        machine.ram.load_bytes(image, PROGRAM_LOAD_ADDR)
        machine.cpu.pc = PROGRAM_LOAD_ADDR
        for _ in range(1000):
            machine.step()
        press(machine.hid, KEY_LCTRL, ord("c"))
        halted = False
        for _ in range(3 * 10_000):
            if machine.step() == 1:
                halted = True
                break
        assert halted == on, f"{label}: halted={halted}"
        assert (machine.ram.read_word(symbols["broken"]) == 1) == on, label
        assert keys_in(machine.hid) == ([KEY_LCTRL] if on else [KEY_LCTRL, ord("c")]), label


if __name__ == "__main__":
    sys.exit(run_module(globals(), "interrupts, faults and the stack pointer"))
