"""P7 (docs/kernel.md §16): what checking for a pending interrupt costs in Machine.run's loop.

Three copies of the loop's core, run on the same CPU-only program:
  stock      today's loop
  each       + `if cpu.pending:` before every instruction
  sampled    the check folded into the every-10,000-instructions slow path
"""
import statistics
import sys
import time
from pathlib import Path

from proto import *  # noqa: F403
from proto import _UNPACK

C = (Path(__file__).resolve().parent / "p2_interrupts.py").read_text()
C = C[C.index("r'''") + 4:C.index("'''", C.index("r'''") + 4)]
C = C.replace("r = workload();", "for (r = 0; r < 30; r++) workload();")
SAVE = "\n".join(f"    PUSH {r}" for r in "ABCDEF")
ASM = """
k_enable:
    RET
k_disable:
    RET
irq_entry:
    RET
irq_entry_noframe:
    RET
"""
image, sym = build_fixed(C, ASM)
INTERVAL = 10_000


def fresh():
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, PROGRAM_LOAD_ADDR)
    cpu = new_cpu(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    return cpu


def stock(cpu):
    ram, memory, handlers, unpack = cpu.ram, cpu.ram.mem, cpu_mod._HANDLERS, _UNPACK
    countdown, executed = INTERVAL, 0
    while True:
        if cpu.halted:
            break
        pc = cpu.pc
        opcode, dst, src1, src2, imm = unpack(memory, pc)
        cpu.pc = pc + 8
        handler = handlers[opcode]
        if handler is None:
            raise RuntimeError("bad opcode")
        handler(cpu, dst, src1, src2, imm)
        if ram.io_pending:
            pass
        countdown -= 1
        if countdown:
            continue
        executed += INTERVAL
        countdown = INTERVAL
        time.time()
    return executed + INTERVAL - countdown


def each(cpu):
    ram, memory, handlers, unpack = cpu.ram, cpu.ram.mem, cpu_mod._HANDLERS, _UNPACK
    countdown, executed = INTERVAL, 0
    while True:
        if cpu.halted:
            break
        if cpu.pending:
            pass
        pc = cpu.pc
        opcode, dst, src1, src2, imm = unpack(memory, pc)
        cpu.pc = pc + 8
        handler = handlers[opcode]
        if handler is None:
            raise RuntimeError("bad opcode")
        handler(cpu, dst, src1, src2, imm)
        if ram.io_pending:
            pass
        countdown -= 1
        if countdown:
            continue
        executed += INTERVAL
        countdown = INTERVAL
        time.time()
    return executed + INTERVAL - countdown


def sampled(cpu):
    ram, memory, handlers, unpack = cpu.ram, cpu.ram.mem, cpu_mod._HANDLERS, _UNPACK
    countdown, executed = INTERVAL, 0
    while True:
        if cpu.halted:
            break
        pc = cpu.pc
        opcode, dst, src1, src2, imm = unpack(memory, pc)
        cpu.pc = pc + 8
        handler = handlers[opcode]
        if handler is None:
            raise RuntimeError("bad opcode")
        handler(cpu, dst, src1, src2, imm)
        if ram.io_pending:
            pass
        countdown -= 1
        if countdown:
            continue
        executed += INTERVAL
        countdown = INTERVAL
        time.time()
        if cpu.pending and cpu.ie:
            pass
    return executed + INTERVAL - countdown


loops = {"stock": stock, "each": each, "sampled": sampled}
rates = {name: [] for name in loops}
for _ in range(2):                     # warm-up, mostly for PyPy's JIT
    for fn in loops.values():
        fn(fresh())
for _ in range(5):
    for name, fn in loops.items():
        cpu = fresh()
        t0 = time.perf_counter()
        n = fn(cpu)
        rates[name].append(n / (time.perf_counter() - t0))
impl = "PyPy" if "__pypy__" in sys.builtin_module_names else "CPython"
base = statistics.median(rates["stock"])
print(f"{impl}: {n:,} instructions per run")
for name, r in rates.items():
    m = statistics.median(r)
    print(f"  {name:8} {m:>12,.0f} IPS  {100 * (m / base - 1):+5.1f}%")
