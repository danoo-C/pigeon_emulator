"""The host's side of the debug port: docs/phase5_plan.md §4.4, step 4.

--serial prints what the machine writes to its debug port in the launcher's
terminal, --serial-log PATH writes it to a file, and GET /serial hands it to
the front ends. The printer and /serial's reply are plain code around the
port's since(), so they're tested without a terminal or a server.

    python3 tests/test_serial.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run_module                                        # noqa: E402
from emulator.cli import Console, serial_printer                      # noqa: E402
from emulator.config import load_config                               # noqa: E402
from emulator.devices import timer                                    # noqa: E402
from emulator.devices.debug_port import DebugPort, serial_reply      # noqa: E402
from emulator.machine import Machine                                  # noqa: E402
from emulator.memory_map import PROGRAM_LOAD_ADDR                     # noqa: E402
from emulator.serial_printer import PARTIAL_WAIT, SerialPrinter       # noqa: E402
from test_debug_port import build_c                                   # noqa: E402

BIOS = REPO_ROOT / "build" / "bios.bin"


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class Terminal(io.StringIO):
    """A terminal that counts how often it's written to."""

    def __init__(self):
        super().__init__()
        self.writes = 0

    def write(self, text):
        self.writes += 1
        return super().write(text)


def port_at(seconds=0.0):
    """A port whose clock the test moves: (port, clock)."""
    clock = Clock(1000.0)
    timer.clock = clock
    port = DebugPort(None)
    clock.now += seconds
    return port, clock


# --- the printer -------------------------------------------------------------------

def test_each_line_is_printed_with_the_time_it_started():
    port, clock = port_at(0.031)
    terminal = Terminal()
    printer = SerialPrinter(port, terminal=terminal, clock=Clock())
    port.write(b"[bios2] hard disk: PIGEONOS\n[bios2] CD: empty\n")
    clock.now += 2.073
    port.write(b"[kernel] started\n")
    printer.poll()
    assert terminal.getvalue().splitlines() == [
        "[   0.031] [bios2] hard disk: PIGEONOS",
        "[   0.031] [bios2] CD: empty",
        "[   2.104] [kernel] started",
    ]


def test_nothing_new_prints_nothing():
    port, _ = port_at()
    terminal = Terminal()
    printer = SerialPrinter(port, terminal=terminal, clock=Clock())
    port.write(b"once\n")
    printer.poll()
    printer.poll()
    assert terminal.getvalue() == "[   0.000] once\n" and terminal.writes == 1


def test_the_log_file_is_started_fresh_with_the_runs_date_and_time():
    port, clock = port_at(1.5)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "logs" / "serial.log"
        path.parent.mkdir()
        path.write_text("a whole earlier run\n")
        terminal = Terminal()
        printer = SerialPrinter(port, terminal=terminal, log_path=path, clock=Clock(),
                                started=datetime(2026, 9, 15, 19, 30, 5))
        port.write(b"hello\n")
        printer.poll()
        # Flushed each look, so a crash loses nothing already printed.
        assert path.read_text().splitlines() == [
            "pigeon serial log, run started 2026-09-15 19:30:05",
            "[   1.500] hello",
        ]
        printer.close()
        assert path.read_text().splitlines()[1:] == terminal.getvalue().splitlines()


def test_the_log_file_alone_prints_nothing_to_the_terminal():
    port, _ = port_at()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "serial.log"
        printer = SerialPrinter(port, log_path=path, clock=Clock())
        port.write(b"quiet\n")
        printer.close()
        assert path.read_text().splitlines()[1] == "[   0.000] quiet"


def test_a_line_without_its_newline_waits_half_a_second():
    port, _ = port_at(3.0)
    terminal = Terminal()
    host = Clock(50.0)
    printer = SerialPrinter(port, terminal=terminal, clock=host)
    port.write(b"Loading")
    printer.poll()
    host.now += PARTIAL_WAIT - 0.1
    printer.poll()
    assert terminal.getvalue() == ""
    host.now += 0.1
    printer.poll()
    assert terminal.getvalue() == "[   3.000] Loading\n"
    port.write(b" done\n[kernel] next\n")
    printer.poll()
    assert terminal.getvalue().splitlines()[1:] == ["            done", "[   3.000] [kernel] next"]


def test_a_line_finished_in_time_prints_whole():
    port, _ = port_at()
    terminal = Terminal()
    host = Clock()
    printer = SerialPrinter(port, terminal=terminal, clock=host)
    port.write(b"exec /bin/ls.bin")
    printer.poll()
    host.now += 0.2
    port.write(b" at 0x01000000\n")
    printer.poll()
    assert terminal.getvalue() == "[   0.000] exec /bin/ls.bin at 0x01000000\n"


def test_what_is_waiting_at_the_end_of_a_run_is_printed():
    port, _ = port_at(0.25)
    terminal = Terminal()
    printer = SerialPrinter(port, terminal=terminal, clock=Clock())
    port.write(b"halted mid-line")
    printer.poll()
    assert terminal.getvalue() == ""
    printer.close()
    assert terminal.getvalue() == "[   0.250] halted mid-line\n"


def test_a_flood_is_written_in_one_go_a_look():
    port, _ = port_at()
    terminal = Terminal()
    printer = SerialPrinter(port, terminal=terminal, clock=Clock())
    for i in range(5000):
        port.write(b"line %04d\n" % i)
    printer.poll()
    assert terminal.writes == 1
    lines = terminal.getvalue().splitlines()
    assert len(lines) == 5000 and lines[-1] == "[   0.000] line 4999"


def test_bytes_that_fell_out_of_the_ring_between_looks_are_counted():
    clock = Clock(1000.0)
    timer.clock = clock
    port = DebugPort(None, keep=16)
    terminal = Terminal()
    printer = SerialPrinter(port, terminal=terminal, clock=Clock())
    port.write(b"one\ntwo\nthree\nfour\nfive\nsix\n")      # 28 bytes
    printer.poll()
    assert terminal.getvalue().splitlines() == [
        "           (12 bytes lost)",
        "           e",                         # the rest of "three", its time gone
        "[   0.000] four",
        "[   0.000] five",
        "[   0.000] six",
    ]


def test_control_characters_and_bad_bytes_never_reach_the_terminal():
    port, _ = port_at()
    terminal = Terminal()
    printer = SerialPrinter(port, terminal=terminal, clock=Clock())
    port.write(b"\x1b[31mred\x07\xff\tok\r\n")
    printer.poll()
    assert terminal.getvalue() == "[   0.000] �[31mred��\tok�\n"


# --- /serial -----------------------------------------------------------------------

def test_serial_answers_the_text_after_an_offset_as_json():
    port, clock = port_at(0.5)
    port.write(b"[bios2] CD: empty\n")
    clock.now += 1.25
    port.write(b"[kernel] started\n")
    reply = serial_reply(port, 0)
    assert json.loads(json.dumps(reply)) == {
        "start": 0, "next": 35, "text": "[bios2] CD: empty\n[kernel] started\n",
        "stamps": [[0, 0.5], [18, 1.75]], "lost": 0}
    assert serial_reply(port, 18)["text"] == "[kernel] started\n"
    assert serial_reply(port, 35) == {"start": 35, "next": 35, "text": "", "stamps": [], "lost": 0}


def test_serial_counts_line_starts_in_characters_not_bytes():
    """The page slices the text it's given, so an é before a line moves the
    next line's start by one character, not two bytes."""
    port, _ = port_at()
    port.write("é\nbad \xff\nok\n".encode("utf-8").replace(b"\xc3\xbf", b"\xff"))
    reply = serial_reply(port, 0)
    assert reply["text"] == "é\nbad �\nok\n"
    assert [index for index, _ in reply["stamps"]] == [0, 2, 8]
    text = reply["text"]
    assert [text[i:].split("\n")[0] for i, _ in reply["stamps"]] == ["é", "bad �", "ok"]


def test_serial_says_what_a_slow_reader_lost():
    clock = Clock(1000.0)
    timer.clock = clock
    port = DebugPort(None, keep=8)
    port.write(b"0123456789\nabc\n")
    reply = serial_reply(port, 0)
    assert (reply["start"], reply["text"], reply["lost"]) == (7, "789\nabc\n", 7)
    assert reply["stamps"] == [[4, 0.0]]


def test_serial_with_no_port_or_a_bad_offset():
    assert serial_reply(None, 10) == {"start": 0, "next": 0, "text": "", "stamps": [], "lost": 0}
    port, _ = port_at()
    port.write(b"x\n")
    assert serial_reply(port, -5)["text"] == "x\n"


def test_the_display_server_is_handed_the_port():
    """Without binding anything: the three servers' starts are replaced."""
    machine = Machine(bios_path=str(BIOS))
    try:
        for device in (machine.display_io, machine.hid, machine.cd):
            device.start_fastapi = lambda **kw: None
        machine.start_servers()
        assert machine.display_io.debug_port is machine.debug
        source = (REPO_ROOT / "emulator" / "devices" / "display_io.py").read_text()
        assert re.search(r'@app\.get\("/serial"\)\s+async def serial\(offset: int = '
                         r'Query\(0, alias="from"\)\):\s+return serial_reply\(self\.debug_port, '
                         r'offset\)', source), "the /serial route is not serial_reply"
    finally:
        machine.close()


# --- the launcher ------------------------------------------------------------------

def test_no_printer_without_serial_or_a_log():
    machine = Machine(bios_path=str(BIOS))
    try:
        config = load_config().override(serial=False)
        assert serial_printer(machine, config) is None
    finally:
        machine.close()


def test_the_terminal_only_with_serial_and_the_file_only_with_a_log():
    machine = Machine(bios_path=str(BIOS))
    try:
        with tempfile.TemporaryDirectory() as d:
            config = load_config().override(serial=False, serial_log=str(Path(d) / "a.log"))
            log_only = serial_printer(machine, config)
            assert log_only.terminal is None and log_only.log is not None
            log_only.close()
        both = serial_printer(machine, load_config().override(serial=True))
        assert both.terminal is sys.stdout and both.log is None
    finally:
        machine.close()


def test_a_run_looks_for_new_lines_at_every_frame():
    """Not only at the end: Machine.run calls on_frame 30 times a second."""
    machine = Machine(bios_path=str(BIOS))
    try:
        seen = {}
        machine.run = lambda **kw: seen.update(kw)
        printer = SerialPrinter(machine.debug, terminal=io.StringIO(), clock=Clock())
        with contextlib.redirect_stdout(io.StringIO()):
            Console(machine, load_config(), printer).run()
        assert seen["on_frame"] == printer.poll
    finally:
        machine.close()


def test_a_run_with_serial_prints_what_the_guest_logs():
    """The whole path: C writes with printf and no kernel, Console.run runs
    the machine to HALT, and the lines land in the terminal and the file."""
    image, _ = build_c(r"""#include <pigeon/stdio.h>
#include <pigeon/debug.h>
int main(void) {
    printf("hello from the guest\n");
    dbg_print("no newline at the halt");
    return 0;
}
""")
    machine = Machine(bios_path=str(BIOS))
    with tempfile.TemporaryDirectory() as d:
        log = Path(d) / "serial.log"
        try:
            config = load_config().override(serial=True, serial_log=str(log))
            machine.ram.load_bytes(image, PROGRAM_LOAD_ADDR)
            machine.cpu.pc = PROGRAM_LOAD_ADDR
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                printer = serial_printer(machine, config)
                Console(machine, config, printer).run()
                printed = out.getvalue()        # before close(): the run prints it all
                printer.close()
        finally:
            machine.close()
        lines = [line for line in printed.splitlines() if line.startswith("[ ")]
        assert [re.sub(r"^\[\s*\d+\.\d{3}\] ", "", line) for line in lines] == [
            "hello from the guest", "no newline at the halt"], printed
        assert log.read_text().splitlines()[1:] == lines


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the serial printer and /serial"))
