#!/usr/bin/env python3
"""Measure interpreter throughput and the display conversion.

    python3 tools/bench.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


if __name__ == "__main__":
    ips, final = bench_cpu()
    print(f"CPU dispatch      {ips:>12,.0f} IPS   (was ~1,220,000 before the rewrite)")
    print(f"                  A={final:,} after {ITERATIONS:,} iterations")
    print(f"IO update, idle   {bench_io_idle():>12,.0f} calls/s "
          f"(no longer on the per-instruction path)")
    ms = bench_display()
    print(f"Frame conversion  {ms:>12.3f} ms      (was 2.22 ms; "
          f"{1000 / ms:,.0f} FPS ceiling)")
