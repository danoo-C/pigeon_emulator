"""P4 (docs/kernel.md §16): the single-tasking system end to end, in guest code.

A kernel (C, fixed address) fills a system-call table and a vector table,
then runs a shell as a relocatable program. The shell runs other programs
through the table; the kernel's exec places each child above its parent's
heap, copies and patches it, and calls it. Children exit normally, call
exit() from deep recursion, divide by zero, jump into data, spin until a
Ctrl-C break, and run grandchildren. A timer interrupt fires throughout."""
from proto import *  # noqa: F403
from emulator.memory_map import HEAP_START

SAVE = "\n".join(f"    PUSH {r}" for r in "ABCDEF")
RESTORE = "\n".join(f"    POP {r}" for r in "FEDCBA")

KERNEL = r'''
#define SYSTAB 0x15818u
#define FILES  0x16000u
#define MAGIC  0x31584750u
#define POOL   0x01000000u
#define TOP    0x07F00000u
#define MAXDEPTH 8

typedef int (*call4_fn)(unsigned, int, char **, unsigned *);
typedef void (*abort_fn)(int, unsigned *);

extern int exec_call;
extern int exec_abort;
extern int kinit_ivt;
extern int kpanic;
extern int w_puts;
extern int w_putint;
extern int w_exec;
extern int w_exit;
extern int irq_timer;
extern int fault_div0;
extern int fault_badop;
extern int fault_fetch;
extern int on_break;

struct proc {
    unsigned base;
    unsigned end;
    unsigned heap_ptr_at;
    unsigned save_sp;
    unsigned save_f;
};
struct proc procs[MAXDEPTH];
int depth;
unsigned deepest;
unsigned vectors[8];
unsigned irqframes[512];
char console[8192];
unsigned con_len;
unsigned ticks;
unsigned tick_work;
unsigned bases[16];
unsigned nexec;
char *shell_argv[2];

void k_puts(char *s) {
    while (*s) {
        if (con_len < 8190u) {
            console[con_len] = *s;
            con_len = con_len + 1u;
        }
        s++;
    }
    console[con_len] = 0;
}

void k_putint(int v) {
    char buf[12];
    char one[2];
    int n;
    unsigned u;
    n = 0;
    one[1] = 0;
    if (v < 0) {
        k_puts("-");
        u = 0u - (unsigned)v;
    } else {
        u = (unsigned)v;
    }
    if (u == 0u) {
        k_puts("0");
        return;
    }
    while (u != 0u) {
        buf[n] = (char)('0' + (u % 10u));
        n++;
        u = u / 10u;
    }
    while (n > 0) {
        n--;
        one[0] = buf[n];
        k_puts(one);
    }
}

int streq(char *a, char *b) {
    while (*a != 0 && *a == *b) {
        a++;
        b++;
    }
    return *a == *b;
}

unsigned *find(char *name) {
    unsigned count;
    unsigned i;
    count = *(unsigned *)FILES;
    for (i = 0u; i < count; i++) {
        if (streq((char *)(FILES + 4u + i * 20u), name))
            return (unsigned *)(*(unsigned *)(FILES + 20u + i * 20u));
    }
    return (unsigned *)0;
}

int k_exec(char *name, int argc, char **argv) {
    unsigned *h;
    unsigned size;
    unsigned nrel;
    unsigned frame;
    unsigned words;
    unsigned base;
    unsigned i;
    unsigned delta;
    unsigned *src;
    unsigned *dst;
    unsigned *rel;
    unsigned *p;
    int status;
    h = find(name);
    if (h == (unsigned *)0) return -1;
    if (h[0] != MAGIC || h[1] != 1u) return -3;
    if (depth + 1 >= MAXDEPTH) return -5;
    size = h[2];
    nrel = h[4];
    frame = h[5];
    if (depth == 0) base = POOL;
    else base = ((*(unsigned *)procs[depth].heap_ptr_at) + 7u) & 0xFFFFFFF8u;
    if (base + size + frame + 0x10000u > TOP) return -4;
    words = size >> 2;
    src = h + 8;
    dst = (unsigned *)base;
    for (i = 0u; i < words; i++) dst[i] = src[i];
    rel = src + words;
    delta = base - h[7];
    for (i = 0u; i < nrel; i++) {
        p = (unsigned *)(base + rel[i]);
        *p = *p + delta;
    }
    depth++;
    if ((unsigned)depth > deepest) deepest = (unsigned)depth;
    if (nexec < 16u) {
        bases[nexec] = base;
        nexec = nexec + 1u;
    }
    procs[depth].base = base;
    procs[depth].end = base + size;
    procs[depth].heap_ptr_at = base + h[6];
    status = ((call4_fn)&exec_call)(base + h[3], argc, argv, &procs[depth].save_sp);
    depth--;
    return status;
}

void k_exit(int code) {
    if (depth == 0) {
        k_puts("panic: fault in the kernel\n");
        ((void (*)(void))&kpanic)();
    }
    ((abort_fn)&exec_abort)(code, &procs[depth].save_sp);
}

void on_tick(void) {
    unsigned i;
    ticks = ticks + 1u;
    for (i = 0u; i < 3u; i++) {
        if (i == 1u) tick_work = tick_work + 1u;
    }
}

int main(void) {
    unsigned *t;
    int s;
    t = (unsigned *)SYSTAB;
    t[0] = (unsigned)&w_puts;
    t[1] = (unsigned)&w_putint;
    t[2] = (unsigned)&w_exec;
    t[3] = (unsigned)&w_exit;
    vectors[0] = (unsigned)&fault_div0;
    vectors[1] = (unsigned)&fault_badop;
    vectors[2] = (unsigned)&fault_fetch;
    vectors[3] = (unsigned)&irq_timer;
    vectors[4] = (unsigned)&on_break;
    ((void (*)(void))&kinit_ivt)();
    k_puts("kernel up\n");
    shell_argv[0] = "sh";
    shell_argv[1] = (char *)0;
    s = k_exec("sh", 1, shell_argv);
    k_puts("shell exited ");
    k_putint(s);
    k_puts("\n");
    return s;
}
'''

KASM = f"""
kinit_ivt:
    SETIV #__g_vectors
    RET
kpanic:
    HALT
; int exec_call(entry, argc, argv, save): remember SP and F, call the program
exec_call:
    ADD C, F, #12
    MRW D, C
    GETSP A
    MWW D, A
    ADD D, D, #4
    MWW D, F
    MRW E, F
    ADD C, F, #4
    MRW A, C
    ADD C, F, #8
    MRW B, C
    ADD F, F, #16
    MWW F, A
    ADD C, F, #4
    MWW C, B
    EI
    CALL E
    DI
    SUB F, F, #16
    RET
; void exec_abort(code, save): return from that exec_call with A = code
exec_abort:
    ADD C, F, #4
    MRW D, C
    MRW A, F
    MRW B, D
    ADD D, D, #4
    MRW F, D
    SETSP B
    DI
    RET
w_puts:
    DI
    CALL k_puts
    EI
    RET
w_putint:
    DI
    CALL k_putint
    EI
    RET
w_exec:
    DI
    CALL k_exec
    EI
    RET
w_exit:
    DI
    CALL k_exit
fault_div0:
    MOV A, #0xFFFFFF9C
    JMP fault_common
fault_badop:
    MOV A, #0xFFFFFF9B
    JMP fault_common
fault_fetch:
    MOV A, #0xFFFFFF9A
    JMP fault_common
on_break:
    MOV A, #0xFFFFFFFE
    JMP fault_common
fault_common:
    MOV F, #__g_irqframes
    MWW F, A
    CALL k_exit
irq_timer:
{SAVE}
    MOV F, #__g_irqframes
    CALL on_tick
{RESTORE}
    IRET
"""

SYS_H = r'''
#define SYSTAB 0x15818u
typedef void (*puts_fn)(char *);
typedef void (*putint_fn)(int);
typedef int (*exec_fn)(char *, int, char **);
typedef void (*exit_fn)(int);
void sys_puts(char *s) { ((puts_fn)(*(unsigned *)SYSTAB))(s); }
void sys_putint(int v) { ((putint_fn)(*(unsigned *)(SYSTAB + 4u)))(v); }
int sys_exec(char *name, int argc, char **argv) {
    return ((exec_fn)(*(unsigned *)(SYSTAB + 8u)))(name, argc, argv);
}
void sys_exit(int code) { ((exit_fn)(*(unsigned *)(SYSTAB + 12u)))(code); }
'''

APPS = {
    "sh": SYS_H + r'''
void report(char *name, int s) {
    sys_puts(name);
    sys_puts(" -> ");
    sys_putint(s);
    sys_puts("\n");
}
int main(int argc, char **argv) {
    char *a[3];
    sys_puts("sh: start, argv[0]=");
    sys_puts(argv[0]);
    sys_puts("\n");
    a[0] = "hello"; a[1] = "world"; a[2] = (char *)0;
    report("hello", sys_exec("hello", 2, a));
    a[0] = "deep";
    report("deep", sys_exec("deep", 1, a));
    a[0] = "div0";
    report("div0", sys_exec("div0", 1, a));
    a[0] = "badop";
    report("badop", sys_exec("badop", 1, a));
    a[0] = "spin";
    report("spin", sys_exec("spin", 1, a));
    a[0] = "nested";
    report("nested", sys_exec("nested", 1, a));
    report("missing", sys_exec("missing", 1, a));
    a[0] = "hello"; a[1] = "again";
    report("hello", sys_exec("hello", 2, a));
    return 0;
}
''',
    "hello": "#include <pigeon/mem.h>\n" + SYS_H + r'''
int main(int argc, char **argv) {
    char *p;
    p = (char *)malloc(64u);
    p[0] = 'x';
    sys_puts("hello ");
    sys_puts(argv[argc - 1]);
    sys_puts(" argc=");
    sys_putint(argc);
    sys_puts("\n");
    return 7;
}
''',
    "deep": SYS_H + r'''
int down(int n) {
    if (n == 0) sys_exit(42);
    return down(n - 1) + 1;
}
int main(void) { return down(50); }
''',
    "div0": SYS_H + r'''
int zero;
int crash(int n) {
    if (n == 0) return 10 / zero;
    return crash(n - 1) + 1;
}
int main(void) { return crash(20); }
''',
    "badop": SYS_H + r'''
unsigned junk[4];
int main(void) {
    void (*f)(void);
    junk[0] = 0xFFFFFFFFu;
    f = (void (*)(void))(unsigned)junk;
    f();
    return 0;
}
''',
    "spin": SYS_H + r'''
int main(void) {
    unsigned n;
    n = 0u;
    sys_puts("spin: looping\n");
    while (1) { n = n + 1u; }
    return 0;
}
''',
    "nested": "#include <pigeon/mem.h>\n" + SYS_H + r'''
int main(void) {
    unsigned *block;
    unsigned i;
    int s;
    char *a[3];
    block = (unsigned *)malloc(4000u);
    for (i = 0u; i < 1000u; i++) block[i] = i * 3u + 1u;
    a[0] = "hello"; a[1] = "from-nested"; a[2] = (char *)0;
    s = sys_exec("hello", 2, a);
    for (i = 0u; i < 1000u; i++) {
        if (block[i] != i * 3u + 1u) {
            sys_puts("nested: heap damaged\n");
            return -1;
        }
    }
    sys_puts("nested: heap intact, child returned ");
    sys_putint(s);
    sys_puts("\n");
    s = sys_exec("div0", 1, a);
    sys_puts("nested: child crashed with ");
    sys_putint(s);
    sys_puts("\n");
    return 5;
}
''',
}

EXPECTED = """kernel up
sh: start, argv[0]=sh
hello world argc=2
hello -> 7
deep -> 42
div0 -> -100
badop -> -101
spin: looping
spin -> -2
hello from-nested argc=2
nested: heap intact, child returned 7
nested: child crashed with -100
nested -> 5
missing -> -1
hello again argc=2
hello -> 7
shell exited 0
"""

kimg, ksym = build_fixed(KERNEL, KASM)
blobs = {name: build_app(src) for name, src in APPS.items()}


def attempt(k):
    ram = RAM(RAM_SIZE)
    ram.load_bytes(kimg, PROGRAM_LOAD_ADDR)
    names = list(blobs)
    ram.write_word(0x16000, len(names))
    for j, name in enumerate(names):
        at = 0x03000000 + j * 0x00100000
        ram.load_bytes(blobs[name], at)
        entry = 0x16004 + j * 20
        ram.load_bytes(name.encode().ljust(16, b"\0"), entry)
        ram.write_word(entry + 16, at)
    cpu = new_cpu(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    timer = every(k) if k else None
    con = ksym["__g_console"]
    st = {"n": 0, "break_at": None, "sent": False}

    def tick(c, opcode):
        if timer:
            timer(c, opcode)
        st["n"] += 1
        if st["sent"]:
            return
        if st["break_at"] is None:
            if st["n"] % 500 == 0 and b"spin: looping" in ram.mem[con:con + 4096]:
                st["break_at"] = st["n"] + 20000
        elif st["n"] >= st["break_at"]:
            c.pending |= 1 << BREAK
            st["sent"] = True

    n = run(cpu, tick=tick, limit=300_000_000)
    text = c_string(ram, con)
    nexec = ram.read_word(ksym["__g_nexec"])
    bases = [hex(ram.read_word(ksym["__g_bases"] + 4 * i)) for i in range(nexec)]
    return {
        "console_ok": text == EXPECTED,
        "text": text,
        "a": cpu.reg.read(0),
        "stack_ok": cpu.sp == STACK_TOP and cpu.reg.read(5) == HEAP_START,
        "ticks": ram.read_word(ksym["__g_ticks"]),
        "deepest": ram.read_word(ksym["__g_deepest"]),
        "bases": bases,
        "instructions": n,
    }


print("program files:", {name: len(b) for name, b in blobs.items()})
for k in (None, 997, 13, 1):
    r = attempt(k)
    text = r.pop("text")
    print(f"timer every {k}: {r}")
    if not r["console_ok"]:
        print(text)
