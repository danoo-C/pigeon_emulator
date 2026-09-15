"""The debug port on channel 8: docs/phase5_plan.md §4.1, step 2.

A program writes lines here that never reach the machine's screen, and the
host keeps the last 64 KB of them with the time each line started. These
tests drive the device's callback directly and through a real IOController,
since the two commands depend on what the controller copies back and what
it doesn't.

    python3 tests/test_debug_port.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import struct
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _runner                                                        # noqa: E402
from _runner import cases, run_module                                 # noqa: E402
from assembler.assembler import Assembler                             # noqa: E402
from compiler.cc import compile_units                                 # noqa: E402
from emulator import memory_map                                       # noqa: E402
from emulator.cpu import CPU                                          # noqa: E402
from emulator.devices import timer                                    # noqa: E402
from emulator.devices.debug_port import (                             # noqa: E402
    CMD_NOP, CMD_WRITE, CMD_WRITE_DMA, DMA_MAX, DMA_REFUSED, WINDOW, DebugPort)
from emulator.io_controller import (                                  # noqa: E402
    ERR_NO_SUCH_CHANNEL, IOChannel, IOController)
from emulator.machine import Machine                                  # noqa: E402
from emulator.programs import libraries_for                           # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    CH_DEBUG, DISPLAY_START, HEAP_START, IO_START, PROGRAM_LOAD_ADDR, RAM_SIZE, IOHeader)
from emulator.ram import RAM                                          # noqa: E402

WINDOW_BASE = IO_START + IOHeader.USABLE_AFTER


class Bus:
    """A RAM, a controller and the port on CH_DEBUG."""

    def __init__(self, keep=None):
        self.ram = RAM(RAM_SIZE)
        self.port = DebugPort(self.ram) if keep is None else DebugPort(self.ram, keep=keep)
        self.controller = IOController(self.ram)
        self.controller.register_channel(CH_DEBUG, IOChannel(self.port.callback, name="DEBUG"))

    def fire(self, read_write, command, length, channel=CH_DEBUG):
        ram = self.ram
        ram.write_word(IO_START + IOHeader.IO_R_W, read_write)
        ram.write_word(IO_START + IOHeader.COMMAND, command)
        ram.write_word(IO_START + IOHeader.LENGTH, length)
        ram.write_word(IO_START + IOHeader.ADDRESS, 0)
        ram.write_word(IO_START + IOHeader.IO_CHANNEL, channel)
        self.controller.update()
        return ram.read_word(IO_START + IOHeader.RETURN_DATA)

    def write(self, text):
        self.ram.mem[WINDOW_BASE:WINDOW_BASE + len(text)] = text
        return self.fire(1, CMD_WRITE, len(text))

    def dma(self, address, count):
        struct.pack_into("<II", self.ram.mem, WINDOW_BASE, address, count)
        length = self.fire(0, CMD_WRITE_DMA, 8)
        assert length == 4, f"a WRITE_DMA reply is one word, got {length} bytes"
        return self.ram.read_word(WINDOW_BASE)


class Clock:
    """A clock the test sets by hand, counting how often it is read."""

    def __init__(self):
        self.now = 100.0
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.now


# --- the commands ------------------------------------------------------------------

def test_the_port_is_channel_8_on_every_machine():
    assert CH_DEBUG == 8 and memory_map.symbols()["CH_DEBUG"] == 8
    machine = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"))
    try:
        channel = machine.io_controller.channels[CH_DEBUG]
        assert channel.name == "DEBUG" and channel.callback == machine.debug.callback
    finally:
        machine.close()


def test_nop_answers_four_zero_bytes_and_an_empty_channel_does_not():
    """What <pigeon/debug.h> probes with: 4 here, 0xFFFFFFFF with no port."""
    bus = Bus()
    bus.ram.mem[WINDOW_BASE:WINDOW_BASE + 4] = b"\xAA" * 4
    assert bus.fire(0, CMD_NOP, 4) == 4
    assert bytes(bus.ram.mem[WINDOW_BASE:WINDOW_BASE + 4]) == bytes(4)
    assert bus.fire(0, CMD_NOP, 4, channel=9) == ERR_NO_SUCH_CHANNEL
    assert bus.port.end == 0, "a NOP wrote something"


def test_write_takes_the_text_and_says_how_much_in_return_data():
    bus = Bus()
    assert bus.write(b"hello\n") == 6
    assert bus.port.since(0).data == b"hello\n"


def test_write_takes_at_most_the_window():
    """The controller hands over LENGTH bytes whatever LENGTH is -- past the
    window, that's the framebuffer -- so the port cuts it, and says so."""
    bus = Bus()
    text = bytes(i & 0x7F for i in range(WINDOW))
    bus.ram.mem[WINDOW_BASE:WINDOW_BASE + WINDOW] = text
    bus.ram.mem[DISPLAY_START:DISPLAY_START + 100] = b"\xEE" * 100
    assert bus.fire(1, CMD_WRITE, WINDOW + 100) == WINDOW
    assert bus.port.since(0).data == text


def test_write_sent_with_rw_zero_takes_nothing():
    """With R/W 0 the controller hands over zeros, not the text."""
    bus = Bus()
    bus.ram.mem[WINDOW_BASE:WINDOW_BASE + 3] = b"abc"
    assert bus.fire(0, CMD_WRITE, 3) == 0
    assert bus.port.end == 0


def test_write_dma_takes_bytes_straight_from_ram():
    bus = Bus()
    bus.ram.mem[HEAP_START:HEAP_START + 9] = b"from ram\n"
    assert bus.dma(HEAP_START, 9) == 9
    assert bus.port.since(0).data == b"from ram\n"


def test_write_dma_reads_below_the_program_as_stage_1_needs():
    """Unlike the HDD, which writes RAM and refuses below PROGRAM_LOAD_ADDR:
    stage 1's text is in the BIOS."""
    bus = Bus()
    bus.ram.mem[0x3E8:0x3E8 + 12] = b"bios: bios2\n"
    assert 0x3E8 < PROGRAM_LOAD_ADDR
    assert bus.dma(0x3E8, 12) == 12
    assert bus.port.since(0).data == b"bios: bios2\n"


@cases(("past the end of RAM", RAM_SIZE - 4, 8),
       ("starting past the end", RAM_SIZE, 1),
       ("wrapping round", 0xFFFFFFF0, 0x20),
       ("more than 64 KB", HEAP_START, DMA_MAX + 1))
def test_write_dma_refuses(label, address, count):
    bus = Bus()
    assert bus.dma(address, count) == DMA_REFUSED, label
    assert bus.port.end == 0, label
    assert len(bus.ram.mem) == RAM_SIZE, label


def test_write_dma_reaches_the_last_bytes_of_ram_and_takes_64_kb():
    bus = Bus()
    bus.ram.mem[RAM_SIZE - 4:] = b"end\n"
    assert bus.dma(RAM_SIZE - 4, 4) == 4
    assert bus.dma(HEAP_START, DMA_MAX) == DMA_MAX
    assert bus.port.end == 4 + DMA_MAX


def test_an_unknown_command_answers_nothing():
    bus = Bus()
    assert bus.fire(1, 7, 16) == 0
    assert bus.port.end == 0


# --- what the host keeps -----------------------------------------------------------

def test_since_gives_what_came_after_an_offset():
    port = DebugPort(None)
    port.write(b"one\n")
    port.write(b"two\n")
    chunk = port.since(4)
    assert (chunk.start, chunk.next, chunk.data, chunk.lost) == (4, 8, b"two\n", 0)
    assert port.since(8).data == b"" and port.since(8).next == 8


def test_the_ring_keeps_the_last_bytes_and_counts_what_a_reader_lost():
    port = DebugPort(None, keep=10)
    port.write(b"0123456789")
    port.write(b"abcdef")
    chunk = port.since(0)
    assert (chunk.start, chunk.next, chunk.data, chunk.lost) == (6, 16, b"6789abcdef", 6)
    chunk = port.since(8)
    assert (chunk.start, chunk.data, chunk.lost) == (8, b"89abcdef", 0)


def test_a_line_that_falls_out_of_the_ring_takes_its_time_with_it():
    port = DebugPort(None, keep=8)
    port.write(b"aaa\nbbb\n")
    port.write(b"cc\n")
    assert [offset for offset, _ in port.since(0).stamps] == [4, 8]


def test_the_times_kept_are_only_those_of_lines_still_kept():
    """A long run writes millions of lines; the times must go with the
    bytes, not pile up beside them."""
    port = DebugPort(None, keep=64)
    for _ in range(2000):
        port.write(b"x\n")
    assert len(port._stamps) <= 32


def test_an_offset_past_the_end_is_a_reader_from_before_and_starts_again():
    """A page left open while the emulator restarted asks for more than
    this machine has written."""
    port = DebugPort(None)
    port.write(b"new machine\n")
    chunk = port.since(5000)
    assert (chunk.start, chunk.data, chunk.lost) == (0, b"new machine\n", 0)


# --- the times ---------------------------------------------------------------------

def test_each_line_gets_the_time_it_started_since_the_port_was_made():
    clock = Clock()
    timer.clock = clock
    port = DebugPort(None)
    clock.now = 101.5
    port.write(b"first\nsecond\n")
    clock.now = 103.0
    port.write(b"third\n")
    assert port.since(0).stamps == [(0, 1.5), (6, 1.5), (13, 3.0)]


def test_a_line_split_across_writes_gets_one_time_and_one_look_at_the_clock():
    clock = Clock()
    timer.clock = clock
    port = DebugPort(None)
    clock.now = 101.0
    port.write(b"exec /bin/ls.bin")
    clock.now = 102.0
    reads = clock.reads
    port.write(b" at 0x01000000")
    assert clock.reads == reads, "a write in the middle of a line read the clock"
    port.write(b"\n")
    port.write(b"next\n")
    assert port.since(0).stamps == [(0, 1.0), (31, 2.0)]


def test_times_follow_the_tests_stepping_clock():
    """The port looks the clock up each time, so the clock the tests swap in
    is the one it reads -- 0.05 s a look."""
    timer.clock = _runner.SteppingClock()
    port = DebugPort(None)
    port.write(b"a\n")
    port.write(b"b\nc\n")
    stamps = port.since(0).stamps
    assert [offset for offset, _ in stamps] == [0, 2, 4]
    assert [round(t, 6) for _, t in stamps] == [0.05, 0.1, 0.1]


# --- <pigeon/debug.h>: step 3 -----------------------------------------------------

F = 5                                   # the frame pointer's register


def build_c(text):
    """C text with the libraries its includes name: (image, symbols)."""
    with tempfile.TemporaryDirectory() as d:
        source = Path(d) / "t.c"
        source.write_text(text)
        asm = Path(d) / "t.asm"
        asm.write_text(compile_units([source, *libraries_for(source)]))
        assembler = Assembler(str(asm))
        image = assembler.assemble()
    return image, {**assembler.static_defs, **assembler.symbols}


class Guest:
    """C run to HALT on a Machine, or on a bare CPU with no bus at all."""

    def __init__(self, text, bare=False, port=True):
        self.image, self.symbols = build_c(text)
        if bare:
            self.machine = None
            self.ram = RAM(RAM_SIZE)
            self.cpu = CPU(self.ram)
            self.port = None
        else:
            self.machine = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"))
            self.ram, self.cpu, self.port = self.machine.ram, self.machine.cpu, self.machine.debug
            if not port:
                del self.machine.io_controller.channels[CH_DEBUG]
        self.ram.load_bytes(self.image, PROGRAM_LOAD_ADDR)
        self.cpu.pc = PROGRAM_LOAD_ADDR
        self.steps = 0

    def step(self):
        self.steps += 1
        if self.machine is None:
            return self.cpu.run()
        return self.machine.step()

    def run(self, limit=5_000_000):
        with contextlib.redirect_stdout(io.StringIO()):
            while self.steps < limit:
                if self.step() == 1:
                    return self
        raise AssertionError("did not halt")

    def run_to(self, label, limit=5_000_000):
        """Step until PC is at a label, and into it."""
        with contextlib.redirect_stdout(io.StringIO()):
            while self.cpu.pc != self.symbols[label]:
                assert self.steps < limit and self.step() != 1, f"never reached {label}"
        return self

    def word(self, name):
        return self.ram.read_word(self.symbols[f"__g_{name}"])

    def signed(self, name):
        value = self.word(name)
        return value - (1 << 32) if value & 0x80000000 else value

    def text(self):
        return self.port.since(0).data

    def close(self):
        if self.machine is not None:
            self.machine.close()


@contextlib.contextmanager
def guest(text, **kw):
    g = Guest(text, **kw)
    try:
        yield g
    finally:
        g.close()


CALLS = r"""#include <pigeon/debug.h>
int a; int b; int c;
int main(void) {
    a = dbg_print("[t] hello\n");
    b = dbg_printf("[t] %d files in %s, %x\n", 12, "/bin", 0xBEEFu);
    c = dbg_write("abc", 3u);
    return 0;
}
"""


def test_dbg_print_and_dbg_printf_reach_the_port_and_say_how_much():
    with guest(CALLS) as g:
        g.run()
        assert g.text() == b"[t] hello\n[t] 12 files in /bin, beef\nabc"
        assert (g.signed("a"), g.signed("b"), g.signed("c")) == (10, 27, 3)


def test_on_a_bare_cpu_every_call_returns_minus_one_at_once():
    """No controller: the probe's channel store stays put. dbg_printf gives
    up before formatting, so the whole program is a few hundred
    instructions."""
    with guest(CALLS, bare=True) as g:
        g.run()
        assert (g.signed("a"), g.signed("b"), g.signed("c")) == (-1, -1, -1)
        assert g.ram.read_word(IO_START + IOHeader.IO_CHANNEL) == 0, "the probe left the channel set"
        assert g.steps < 1_000, f"{g.steps:,} instructions with no port"


def test_a_machine_without_the_port_gets_minus_one():
    """An empty channel answers 0xFFFFFFFF, which is not NOP's 4."""
    with guest(CALLS, port=False) as g:
        g.run()
        assert (g.signed("a"), g.signed("b"), g.signed("c")) == (-1, -1, -1)
        assert g.text() == b""


def test_a_text_longer_than_the_window_goes_in_windows():
    with guest(r"""#include <pigeon/debug.h>
char big[10000]; int n;
int main(void) {
    unsigned i;
    for (i = 0u; i < 10000u; i++) big[i] = (char)('a' + i % 26u);
    n = dbg_write(big, 10000u);
    return 0;
}
""") as g:
        g.run(limit=20_000_000)
        assert g.signed("n") == 10000
        assert g.text() == bytes(ord("a") + i % 26 for i in range(10000))


def test_dbg_printf_cuts_a_line_at_255_bytes():
    with guest(r"""#include <pigeon/debug.h>
char long_text[300]; int n;
int main(void) {
    unsigned i;
    for (i = 0u; i < 299u; i++) long_text[i] = 'x';
    n = dbg_printf("%s!", long_text);
    return 0;
}
""") as g:
        g.run()
        assert g.signed("n") == 255
        assert g.text() == b"x" * 255


LINE = r"""#include <pigeon/debug.h>
void mark(void) { }
int main(void) {
    dbg_print("");
    mark();
    dbg_printf("[kernel] exec %s at %p, depth %d\n", "/bin/echo.bin", 0x01000000u, 2);
    mark();
    return 0;
}
"""


def test_a_line_costs_a_few_thousand_instructions():
    """Counted, not timed: a 41-byte line, formatted and sent, once the
    port has been asked about. It measured 6,179: formatted straight into
    the window, where snprintf and a copy there measured 5,772
    (docs/phase5_plan.md step 1)."""
    with guest(LINE) as g:
        g.run_to("mark")
        g.step()
        start = g.steps
        g.run_to("mark")
        steps = g.steps - start
        assert g.text() == b"[kernel] exec /bin/echo.bin at 0x1000000, depth 2\n"
        assert steps < 7_000, f"a line cost {steps:,} instructions"


def test_dbg_printf_needs_only_a_few_hundred_bytes_of_frame_stack():
    """The kernel logs a panic on fault_frames, 1,024 bytes. Everything
    above the caller's frames is filled first, and the highest byte
    changed is how deep the call went."""
    with guest(LINE) as g:
        g.run_to("mark")
        base = g.cpu.reg.read(F)
        g.ram.mem[base:base + 4096] = b"\xA5" * 4096
        g.step()
        g.run_to("mark")
        used = bytes(g.ram.mem[base:base + 4096])
        depth = max(i for i in range(4096) if used[i] != 0xA5) + 1
        assert g.text().startswith(b"[kernel] exec")
        assert depth <= 300, f"dbg_printf used {depth} bytes of frame stack"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the debug port"))
