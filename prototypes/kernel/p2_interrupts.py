"""P2 (docs/kernel.md §16): compiled C keeps its answer with an interrupt between every pair of
its instructions -- and two negative controls show what the design guards
against: flags not restored by IRET, and a handler that uses the
interrupted code's frame pointer."""
from proto import *  # noqa: F403
from emulator.memory_map import HEAP_START

C = r'''
extern int irq_entry;
extern int irq_entry_noframe;
extern int k_enable;
extern int k_disable;
unsigned vectors[8];
unsigned irqframes[256];
unsigned ticks;
unsigned mode;

void on_tick(void) {
    unsigned i;
    unsigned x;
    x = 0u;
    ticks = ticks + 1u;
    for (i = 0u; i < 3u; i++) {
        if (i != 1u) x = x + i;
    }
    if (x > 1000u) x = 0u;
}

int sieve(unsigned n) {
    char mark[400];
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

int workload(void) {
    int v[40];
    int i;
    int sum;
    int (*f)(int);
    sum = 0;
    for (i = 0; i < 40; i++) v[i] = ((i * 7919) % 97) - 48;
    sort(v, 40);
    for (i = 0; i + 1 < 40; i++) {
        if (v[i] > v[i + 1]) sum = sum + 1000000;
    }
    for (i = 0; i < 40; i++) sum = sum * 3 + v[i] * (i + 1);
    sum = sum + sieve(400u) * 1000;
    sum = sum + fib(12);
    for (i = 0; i < 20; i++) {
        if (i & 1) f = negate;
        else f = twice;
        sum = sum + f(i - 10);
    }
    return sum;
}

int main(void) {
    int r;
    vectors[3] = (unsigned)&irq_entry;
    if (mode == 2u) vectors[3] = (unsigned)&irq_entry_noframe;
    if (mode != 0u) ((void (*)(void))&k_enable)();
    r = workload();
    ((void (*)(void))&k_disable)();
    return r;
}
'''

SAVE = "\n".join(f"    PUSH {r}" for r in "ABCDEF")
RESTORE = "\n".join(f"    POP {r}" for r in "FEDCBA")
ASM = f"""
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
irq_entry_noframe:
{SAVE}
    CALL on_tick
{RESTORE}
    IRET
"""

image, sym = build_fixed(C, ASM)


def attempt(mode, k=None, restore_flags=True):
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, PROGRAM_LOAD_ADDR)
    ram.write_word(sym["__g_mode"], mode)
    cpu = new_cpu(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    cpu.restore_flags = restore_flags
    tick = every(k) if k else None
    try:
        n = run(cpu, tick=tick, limit=30_000_000)
    except Exception as e:                                      # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:110]}
    return {"result": cpu.reg.read(0), "n": n, "ticks": ram.read_word(sym["__g_ticks"]),
            "raised": tick.state["raised"] if tick else 0,
            "stack": cpu.sp == STACK_TOP and cpu.reg.read(5) == HEAP_START}


base = attempt(0)
print(f"baseline, no interrupts: result={base['result']:#x}  {base['n']:,} instructions  "
      f"stack+F restored={base['stack']}")
for k in (1, 2, 3, 7, 101):
    r = attempt(1, k)
    ok = r.get("result") == base["result"] and r.get("stack")
    print(f"interrupt every {k:>3} instr: {'SAME' if ok else 'DIFFERENT'}  {r}")
for label, kwargs in (("IRET does not restore flags", {"mode": 1, "k": 1, "restore_flags": False}),
                      ("IRET does not restore flags, every 7", {"mode": 1, "k": 7, "restore_flags": False}),
                      ("handler keeps the interrupted F", {"mode": 2, "k": 1}),
                      ("handler keeps the interrupted F, every 7", {"mode": 2, "k": 7})):
    r = attempt(**kwargs)
    same = r.get("result") == base["result"] and r.get("stack")
    print(f"NEGATIVE CONTROL {label}: {'same (control failed to detect)' if same else 'BROKEN as expected'}  {r}")
