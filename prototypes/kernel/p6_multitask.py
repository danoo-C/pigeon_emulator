"""P6 (docs/kernel.md §16): preemptive multitasking. A kernel starts three relocated programs,
each with its own image, frame stack, heap and hardware stack, and a timer
interrupt switches between them with GETSP/SETSP. The kernel's print is
deliberately not atomic; with DI/EI around it lines stay whole, without,
they don't."""
import re
import struct

from proto import *  # noqa: F403
from emulator.memory_map import HEAP_START

SAVE = "\n".join(f"    PUSH {r}" for r in "ABCDEF")
RESTORE = "\n".join(f"    POP {r}" for r in "FEDCBA")

KERNEL = r'''
#define SYSTAB 0x15818u
#define NT 4
extern int task_exit;
extern int irq_switch;
extern int kinit_ivt;
extern int k_ei;
extern int k_di;
extern int w_puts;
extern int w_puts_unlocked;
unsigned vectors[8];
unsigned irqframes[512];
unsigned task_sp[NT];
unsigned entries[NT];
unsigned args[8];
int alive[NT];
int results[NT];
unsigned runs_of[NT];
int cur;
int alive_count;
unsigned switches;
unsigned locked;
char console[16384];
unsigned con_len;

void k_puts(char *s) {
    unsigned n;
    while (*s) {
        n = con_len;
        console[n] = *s;
        con_len = n + 1u;
        s++;
    }
}

int sched_next(void) {
    int n;
    int k;
    n = cur;
    for (k = 0; k < NT; k++) {
        n = n + 1;
        if (n == NT) n = 0;
        if (alive[n]) break;
    }
    cur = n;
    switches = switches + 1u;
    runs_of[n] = runs_of[n] + 1u;
    return n;
}

int on_task_exit(int code) {
    results[cur] = code;
    alive[cur] = 0;
    alive_count = alive_count - 1;
    return sched_next();
}

int main(void) {
    unsigned *t;
    unsigned *sp;
    int i;
    t = (unsigned *)SYSTAB;
    if (locked) t[0] = (unsigned)&w_puts;
    else t[0] = (unsigned)&w_puts_unlocked;
    vectors[3] = (unsigned)&irq_switch;
    ((void (*)(void))&kinit_ivt)();
    alive[0] = 1;
    cur = 0;
    alive_count = 0;
    for (i = 1; i < NT; i++) {
        if (entries[i] != 0u) {
            sp = (unsigned *)(0x06000000u + (unsigned)i * 0x00100000u);
            sp--; *sp = (unsigned)&task_exit;
            sp--; *sp = 4u;
            sp--; *sp = entries[i];
            sp--; *sp = 0u;
            sp--; *sp = 0u;
            sp--; *sp = 0u;
            sp--; *sp = 0u;
            sp--; *sp = 0u;
            sp--; *sp = (unsigned)&args[i * 2];
            task_sp[i] = (unsigned)sp;
            alive[i] = 1;
            alive_count = alive_count + 1;
        }
    }
    ((void (*)(void))&k_ei)();
    while (alive_count > 0) { }
    ((void (*)(void))&k_di)();
    return 0;
}
'''

KASM = f"""
kinit_ivt:
    SETIV #__g_vectors
    RET
k_ei:
    EI
    RET
k_di:
    DI
    RET
w_puts:
    DI
    CALL k_puts
    EI
    RET
w_puts_unlocked:
    CALL k_puts
    RET
irq_switch:
{SAVE}
    GETSP A
    MOV C, #__g_cur
    MRW B, C
    SHL B, B, #2
    ADD B, B, #__g_task_sp
    MWW B, A
    MOV F, #__g_irqframes
    CALL sched_next
    SHL A, A, #2
    ADD A, A, #__g_task_sp
    MRW B, A
    SETSP B
{RESTORE}
    IRET
task_exit:
    DI
    MOV F, #__g_irqframes
    MWW F, A
    CALL on_task_exit
    SHL A, A, #2
    ADD A, A, #__g_task_sp
    MRW B, A
    SETSP B
{RESTORE}
    IRET
"""

TASK_H = r'''
#define SYSTAB 0x15818u
typedef void (*puts_fn)(char *);
void sys_puts(char *s) { ((puts_fn)(*(unsigned *)SYSTAB))(s); }
void say(char tag, unsigned v) {
    char line[10];
    line[0] = tag;
    line[1] = ' ';
    line[2] = (char)('0' + (v / 10000u) % 10u);
    line[3] = (char)('0' + (v / 1000u) % 10u);
    line[4] = (char)('0' + (v / 100u) % 10u);
    line[5] = (char)('0' + (v / 10u) % 10u);
    line[6] = (char)('0' + v % 10u);
    line[7] = '\n';
    line[8] = 0;
    sys_puts(line);
}
'''

TASK_A = r'''
#include <pigeon/mem.h>
int main(int argc, char **argv) {
    char *mark;
    unsigned i;
    unsigned j;
    unsigned count;
    int sum;
    mark = (char *)malloc(3000u);
    count = 0u;
    sum = 0;
    for (i = 0u; i < 3000u; i++) mark[i] = 0;
    for (i = 2u; i < 3000u; i++) {
        if (mark[i] == 0) {
            count++;
            sum = sum + (int)i;
            for (j = i + i; j < 3000u; j = j + i) mark[j] = 1;
            if (count % 40u == 0u) say('A', count);
        }
    }
    say('A', count);
    return sum + argc * 1000000;
}
''' + TASK_H

TASK_B = TASK_H + r'''
int fib(int n) {
    if (n < 2) return n;
    return fib(n - 1) + fib(n - 2);
}
int main(int argc, char **argv) {
    unsigned k;
    int total;
    total = 0;
    for (k = 0u; k < 12u; k++) {
        total = total + fib(13);
        say('B', k);
    }
    return total + argc * 1000000;
}
'''

TASK_C = TASK_H + r'''
int main(int argc, char **argv) {
    int v[120];
    int i;
    int j;
    int t;
    unsigned seed;
    int sum;
    seed = 12345u;
    for (i = 0; i < 120; i++) {
        seed = seed * 1103515245u + 12345u;
        v[i] = (int)((seed >> 16) & 1023u) - 512;
    }
    for (i = 0; i < 120; i++) {
        for (j = 0; j + 1 < 120 - i; j++) {
            if (v[j] > v[j + 1]) {
                t = v[j];
                v[j] = v[j + 1];
                v[j + 1] = t;
            }
        }
        if (i % 10 == 0) say('C', (unsigned)i);
    }
    sum = 0;
    for (i = 0; i + 1 < 120; i++) {
        if (v[i] > v[i + 1]) sum = sum + 1000000;
    }
    for (i = 0; i < 120; i++) sum = sum * 7 + v[i];
    return sum + argc;
}
'''

# TASK_A puts its includes first; the header must follow them.
TASK_A = TASK_A.replace("#include <pigeon/mem.h>\n", "#include <pigeon/mem.h>\n" + TASK_H, 1)
TASK_A = TASK_A[:TASK_A.rindex(TASK_H)]


def expected():
    primes = [p for p in range(2, 3000) if all(p % q for q in range(2, int(p ** 0.5) + 1))]
    a = sum(primes) + 1 * 1000000

    def fib(n):
        return n if n < 2 else fib(n - 1) + fib(n - 2)
    b = 12 * fib(13) + 2 * 1000000
    seed, v = 12345, []
    for _ in range(120):
        seed = (seed * 1103515245 + 12345) & 0xFFFFFFFF
        v.append(((seed >> 16) & 1023) - 512)
    v.sort()
    s = 0
    for x in v:
        s = (s * 7 + x) & 0xFFFFFFFF
    c = (s + 3) & 0xFFFFFFFF
    lines = {"A": len(primes) // 40 + 1, "B": 12, "C": 12}
    return [a & 0xFFFFFFFF, b, c], lines


kimg, ksym = build_fixed(KERNEL, KASM)
blobs = {1: build_app(TASK_A), 2: build_app(TASK_B), 3: build_app(TASK_C)}
want_results, want_lines = expected()


def attempt(k, locked=True):
    ram = RAM(RAM_SIZE)
    ram.load_bytes(kimg, PROGRAM_LOAD_ADDR)
    for i, blob in blobs.items():
        info = load_app(ram, blob, 0x01000000 * i)
        ram.write_word(ksym["__g_entries"] + 4 * i, info["entry"])
        ram.write_word(ksym["__g_args"] + 8 * i, i)
        ram.write_word(ksym["__g_args"] + 8 * i + 4, 0)
    ram.write_word(ksym["__g_locked"], 1 if locked else 0)
    cpu = new_cpu(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    n = run(cpu, tick=every(k), limit=200_000_000)
    length = ram.read_word(ksym["__g_con_len"])
    text = bytes(ram.mem[ksym["__g_console"]:ksym["__g_console"] + length]).decode("latin-1")
    results = [ram.read_word(ksym["__g_results"] + 4 * i) for i in (1, 2, 3)]
    runs = [ram.read_word(ksym["__g_runs_of"] + 4 * i) for i in (1, 2, 3)]
    lines = text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")
    whole = [l for l in lines if re.fullmatch(r"[ABC] \d{5}", l)]
    counts = {t: sum(1 for l in whole if l[0] == t) for t in "ABC"}
    tags = [l[0] for l in whole]
    changes = sum(1 for x, y in zip(tags, tags[1:]) if x != y)
    return {
        "results_ok": results == want_results,
        "lines_whole": len(whole) == len(lines) and counts == want_lines,
        "broken_lines": len(lines) - len(whole),
        "chars": f"{length}/{8 * sum(want_lines.values())}",
        "tag_changes": changes,
        "turns_per_task": runs,
        "switches": ram.read_word(ksym["__g_switches"]),
        "stack_ok": cpu.sp == STACK_TOP and cpu.reg.read(5) == HEAP_START,
        "instructions": n,
    }


print("expected results", [hex(r) for r in want_results], "lines", want_lines,
      "program files", {i: len(b) for i, b in blobs.items()})
for k in (1, 5, 37, 500, 20000):
    print(f"locked print, switch every {k:>5}:", attempt(k))
for k in (3, 7):
    print(f"NEGATIVE CONTROL unlocked print, switch every {k}:", attempt(k, locked=False))
