"""P3 (docs/kernel.md §16): the IO header is shared between a program and an interrupt handler.

The program asks the timer device for timer 1's status (running) 2,000
times; the handler asks for timer 2's (never started). Mode 0 does nothing
about it, mode 1 disables interrupts around the program's IO, mode 2 has
the handler save and restore the IO header and the first data words."""
from proto import *  # noqa: F403
from emulator.devices.timer import Timer
from emulator.io_controller import IOChannel, IOController
from emulator.memory_map import CH_TIMER

C = r'''
#include <pigeon/io.h>
extern int irq_entry;
extern int k_enable;
extern int k_disable;
extern int k_di;
extern int k_ei;
unsigned vectors[8];
unsigned irqframes[256];
unsigned mode;
unsigned ticks;
unsigned handler_ok;

unsigned timer_status(unsigned id) {
    IO_RW = 0u;
    IO_CMD = 5u;
    IO_LEN = 8u;
    IO_ADDR = id;
    IO_CH = CH_TIMER;
    return IO_DATAW[0];
}

void on_tick(void) {
    unsigned rw;
    unsigned cmd;
    unsigned len;
    unsigned addr;
    unsigned ret;
    unsigned d0;
    unsigned d1;
    ticks = ticks + 1u;
    if (mode == 2u) {
        rw = IO_RW; cmd = IO_CMD; len = IO_LEN; addr = IO_ADDR; ret = IO_RETLEN;
        d0 = IO_DATAW[0]; d1 = IO_DATAW[1];
    }
    if (timer_status(2u) == 0u) handler_ok = handler_ok + 1u;
    if (mode == 2u) {
        IO_RW = rw; IO_CMD = cmd; IO_LEN = len; IO_ADDR = addr; IO_RETLEN = ret;
        IO_DATAW[0] = d0; IO_DATAW[1] = d1;
    }
}

int main(void) {
    unsigned i;
    unsigned bad;
    unsigned s;
    bad = 0u;
    IO_RW = 0u; IO_CMD = 1u; IO_LEN = 100000u; IO_ADDR = 1u; IO_CH = CH_TIMER;
    vectors[3] = (unsigned)&irq_entry;
    ((void (*)(void))&k_enable)();
    for (i = 0u; i < 2000u; i++) {
        if (mode == 1u) ((void (*)(void))&k_di)();
        s = timer_status(1u);
        if (mode == 1u) ((void (*)(void))&k_ei)();
        if (s != 1u) bad = bad + 1u;
    }
    ((void (*)(void))&k_disable)();
    return (int)bad;
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
k_di:
    DI
    RET
k_ei:
    EI
    RET
irq_entry:
{SAVE}
    MOV F, #__g_irqframes
    CALL on_tick
{RESTORE}
    IRET
"""

image, sym = build_fixed(C, ASM)
names = {0: "unprotected", 1: "DI/EI around program IO", 2: "handler saves IO header"}
for mode in (0, 1, 2):
    for k in (1, 3, 7, 31, 101, 1009):
        ram = RAM(RAM_SIZE)
        ram.load_bytes(image, PROGRAM_LOAD_ADDR)
        ram.write_word(sym["__g_mode"], mode)
        io = IOController(ram)
        io.register_channel(CH_TIMER, IOChannel(Timer().callback, name="TIMER"))
        cpu = new_cpu(ram)
        cpu.pc = PROGRAM_LOAD_ADDR
        n = run(cpu, io=io, tick=every(k))
        print(f"{names[mode]:26} interrupt every {k}: wrong answers {cpu.reg.read(0):>4} / 2000   "
              f"handler IO right {ram.read_word(sym['__g_handler_ok'])}/{ram.read_word(sym['__g_ticks'])}  "
              f"stack ok={cpu.sp == STACK_TOP}")
