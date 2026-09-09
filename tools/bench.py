#!/usr/bin/env python3
"""Measure interpreter throughput and the display conversion.

    python3 tools/bench.py
"""
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from emulator.cpu import CPU
from emulator.devices.display_io import DisplayIO
from emulator.instruction_set import NONE_REG, encode
from emulator.io_controller import IOController
from emulator.memory_map import DISPLAY_SIZE, RAM_SIZE
from emulator.ram import RAM

ITERATIONS = 2_000_000


def bench_cpu():
    ram = RAM(RAM_SIZE)
    ram.load_bytes(encode("ADD", dst=0, src1=0, src2=NONE_REG, imm=1)
                   + encode("CMP", src1=0, src2=NONE_REG, imm=ITERATIONS)
                   + encode("JL", src1=NONE_REG, imm=0)
                   + encode("HALT"), 0)
    cpu = CPU(ram)
    start = time.perf_counter()
    executed = 0
    while cpu.run() == 0:
        executed += 1
    elapsed = time.perf_counter() - start
    return executed / elapsed, cpu.reg.read(0)


def bench_io_idle():
    """The old main loop called this after every single instruction."""
    ram = RAM(RAM_SIZE)
    io = IOController(ram)
    start = time.perf_counter()
    for _ in range(200_000):
        io.update()
    return 200_000 / (time.perf_counter() - start)


def bench_display():
    display = DisplayIO(RAM(RAM_SIZE))
    data = bytes(range(256)) * (DISPLAY_SIZE // 256)
    start = time.perf_counter()
    for _ in range(200):
        display._convert_to_rgba(data)
    return (time.perf_counter() - start) / 200 * 1000


def bench_real_program():
    """The number that actually matters: a compiled C program running
    through Machine.run(), which is the path a user takes.

    The synthetic loop above is ADD/CMP/JL with no memory traffic and no
    IO. Real code is roughly half its speed, and optimising against the
    synthetic figure means tuning the wrong thing -- so both are reported.

    The BIOS is skipped: it waits two REAL seconds on the timer, and that
    spin is IO-bound, so including it measures the wall clock rather than
    the interpreter.
    """
    import contextlib
    import io as _io

    program = REPO_ROOT / "build" / "demo.bin"
    bios = REPO_ROOT / "build" / "bios.bin"
    if not (program.exists() and bios.exists()):
        return None, "build/demo.bin or build/bios.bin missing -- build them first"

    from emulator.machine import Machine
    from emulator.memory_map import PROGRAM_LOAD_ADDR

    best = 0.0
    for _ in range(3):
        machine = Machine(bios_path=str(bios), program_path=str(program))
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                while machine.cpu.pc < PROGRAM_LOAD_ADDR:
                    if machine.step() == 1:
                        break
                # run(), not step(): run() is the path a user actually
                # takes, and it inlines the dispatch. Timing step() here
                # under-reported real throughput by about 25%.
                before = machine.total_instructions
                start = time.perf_counter()
                machine.run(deadline=start + 0.4)
                elapsed = time.perf_counter() - start
                executed = machine.total_instructions - before
                best = max(best, executed / elapsed)
        finally:
            machine.close()
    return best, None


if __name__ == "__main__":
    ips, final = bench_cpu()
    print(f"CPU dispatch      {ips:>12,.0f} IPS   synthetic ADD/CMP/JL loop")
    print(f"                  A={final:,} after {ITERATIONS:,} iterations")

    real, problem = bench_real_program()
    if problem:
        print(f"Real program      {'--':>12}       {problem}")
    else:
        print(f"Real program      {real:>12,.0f} IPS   compiled C, the honest number")

    print(f"IO update, idle   {bench_io_idle():>12,.0f} calls/s "
          f"(no longer on the per-instruction path)")
    ms = bench_display()
    print(f"Frame conversion  {ms:>12.3f} ms      (was 2.22 ms; "
          f"{1000 / ms:,.0f} FPS ceiling)")
