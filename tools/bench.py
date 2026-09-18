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
from emulator.memory_map import HEAP_START, RAM_SIZE, VRAM_SIZE
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


def bench_memory():
    """A word written into the heap, and one through the video-memory
    aperture above RAM (docs/gac/phase1_aperture.md). The first must not
    move when the aperture exists; the second is what one pixel costs."""
    ram = RAM(RAM_SIZE, VRAM_SIZE)
    results = []
    for addr in (HEAP_START, ram.vram_base + 4096):
        write = ram.write_word
        start = time.perf_counter()
        for _ in range(1_000_000):
            write(addr, 0x12345678)
        results.append((time.perf_counter() - start) * 1000)   # ns per write
    return results


def bench_gac():
    """The accelerator at its biggest mode, through its callback as the
    bus calls it: a full-screen FILL, and a full console of TEXT -- the
    redraw that costs about 34 s in guest code (docs/gac/design.md §4.3)."""
    import random
    import re
    import struct
    from emulator.devices.display_io import DisplayIO
    from emulator.devices.gac import CMD_BLIT_ALPHA, CMD_FILL, CMD_SET_FONT, CMD_TEXT, GAC
    from emulator.devices.vram import VRAM
    from emulator.memory_map import DISPLAY_MODES, IO_START, IOHeader

    window = IO_START + IOHeader.USABLE_AFTER
    ram = RAM(1 << 24, 1 << 23)
    vram = VRAM(ram, DisplayIO(ram), DISPLAY_MODES, (1280, 720))
    gac = GAC(ram, vram)
    source = (REPO_ROOT / "lib" / "pigeon" / "display.c").read_text()
    table = source[source.index("FONT[] = {"):source.index("};", source.index("FONT[] = {"))]
    font = bytes(int(h, 16) for h in re.findall(r"0x([0-9A-Fa-f]{2})", table))
    ram.mem[HEAP_START:HEAP_START + len(font)] = font

    def call(command, fmt, *args, tail=b""):
        data = struct.pack(fmt, *args) + tail
        ram.mem[window:window + len(data)] = data
        gac.callback(0, command, 4, 0, bytearray(4))

    call(CMD_SET_FONT, "<7I", HEAP_START, 5, 8, 6, 9, 0x20, 95)
    start = time.perf_counter()
    for _ in range(20):
        call(CMD_FILL, "<IiiiiI", 0, 0, 0, 1280, 720, 0xFF101018)
    fill = (time.perf_counter() - start) / 20 * 1000

    # Blending reads every byte it writes: §4.5's numbers are the ones most
    # likely to rot, so they are measured here, through the device.
    start = time.perf_counter()
    for _ in range(5):
        call(CMD_FILL, "<IiiiiI", 0, 0, 0, 1280, 720, 0x80FF0000)
    blended = (time.perf_counter() - start) / 5 * 1000
    back, _ = vram.alloc(1280, 720)            # a second screen to lay over the first
    start = time.perf_counter()
    for _ in range(3):
        call(CMD_BLIT_ALPHA, "<IiiIiiiiI", back, 0, 0, 0, 0, 0, 1280, 720, 128)
    blit_alpha = (time.perf_counter() - start) / 3 * 1000

    rng = random.Random(1)
    lines = [bytes(rng.randrange(0x21, 0x7F) for _ in range(1280 // 6)) for _ in range(720 // 9)]
    start = time.perf_counter()
    for row, text in enumerate(lines):
        call(CMD_TEXT, "<IiiIII", 0, 0, row * 9, 0xFFC0C0C0, 0xFF101018, len(text), tail=text)
    console = (time.perf_counter() - start) * 1000
    return fill, console, blended, blit_alpha


def bench_display():
    """What serving a frame costs the emulator's own thread, 30 times a
    second: the snapshot, at the power-on mode and the biggest. The clients
    swizzle now (docs/gac/plans/phase4_frontends.md); the server's swizzle
    took 6.4 ms of this thread at 1280 x 720."""
    from emulator.devices.vram import VRAM
    from emulator.memory_map import DISPLAY_MODES
    ram = RAM(1 << 24, 1 << 23)
    display = DisplayIO(ram)
    vram = VRAM(ram, display, DISPLAY_MODES)
    times = []
    for mode in ((192, 108), (1280, 720)):
        vram.set_mode(*mode)
        start = time.perf_counter()
        for _ in range(100):
            display.update()
        times.append((time.perf_counter() - start) / 100 * 1000)
    return times


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
    heap, aperture = bench_memory()
    print(f"Word write, heap  {heap:>12.1f} ns")
    print(f"Word write, VRAM  {aperture:>12.1f} ns      (through the aperture: one pixel)")
    fill, console, blended, blit_alpha = bench_gac()
    print(f"GAC fill, 720p    {fill:>12.3f} ms      a whole 1280x720 screen, one command")
    print(f"  blended         {blended:>12.3f} ms      the same at alpha 0x80 (§4.5: 3.65)")
    print(f"  BLIT_ALPHA      {blit_alpha:>12.1f} ms      a whole screen over another (§4.5: 18.5)")
    print(f"GAC text, 720p    {console:>12.1f} ms      a whole 213x80 console, 80 commands")
    small, big = bench_display()
    print(f"Frame snapshot    {small:>12.3f} ms      192x108; the clients swizzle")
    print(f"  at 720p         {big:>12.3f} ms      1280x720 (the server's swizzle was 6.4)")
