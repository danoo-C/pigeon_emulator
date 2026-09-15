"""What the machine writes to its debug port, printed on the host.

docs/phase5_plan.md §4.4. One printer serves --serial, the launcher's
terminal, and --serial-log PATH, a file: each line with the time since
power-on that the port noted when the line started.

It runs in the emulator's own loop at the display's 30 frames a second
(Machine.run's on_frame), not once a line: each time it takes whatever came
since it last looked, writes it in one go and flushes. A program writing
megabytes costs 30 writes a second, not one a line.

A line without its newline waits, in case the rest is coming. It's printed
once it has waited PARTIAL_WAIT, or when the run ends; whatever arrives
after that goes on a line of its own, under a blank time.
"""
import time
from datetime import datetime
from pathlib import Path

from .devices.debug_port import clean

PARTIAL_WAIT = 0.5          # seconds a line without its newline waits
NO_TIME = " " * 11          # as wide as "[   0.000] "


def stamp(seconds):
    return NO_TIME if seconds is None else f"[{seconds:8.3f}] "


class SerialPrinter:
    def __init__(self, port, terminal=None, log_path=None, clock=time.monotonic,
                 started=None):
        self.port = port
        self.terminal = terminal
        self.clock = clock
        self.offset = 0
        self._partial = b""            # the line still waiting for its newline
        self._partial_time = None      # the port's time for that line
        self._partial_since = None     # when it began waiting, on self.clock
        self._printed_part = False     # some of this line is printed already
        self.log = None
        if log_path is not None:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Fresh each run, so one file is one run (docs/phase5_plan.md Q4).
            self.log = open(path, "w", encoding="utf-8")
            started = datetime.now() if started is None else started
            self.log.write(f"pigeon serial log, run started {started:%Y-%m-%d %H:%M:%S}\n")
            self.log.flush()

    def _line(self, data):
        time_of = None if self._printed_part else self._partial_time
        return stamp(time_of) + clean(data)

    def poll(self, final=False):
        """Print what came since the last look. `final` prints a line still
        waiting for its newline too, as the end of a run does."""
        chunk = self.port.since(self.offset)
        self.offset = chunk.next
        out = []
        if chunk.lost:
            if self._partial:
                out.append(self._line(self._partial))
            out.append(f"{NO_TIME}({chunk.lost:,} bytes lost)")
            self._partial, self._printed_part = b"", False

        times = dict(chunk.stamps)
        data, pos = chunk.data, 0
        while pos < len(data):
            if not self._partial and not self._printed_part:
                self._partial_time = times.get(chunk.start + pos)
            newline = data.find(b"\n", pos)
            if newline == -1:
                if not self._partial:
                    self._partial_since = self.clock()
                self._partial += data[pos:]
                break
            out.append(self._line(self._partial + data[pos:newline]))
            self._partial, self._printed_part = b"", False
            pos = newline + 1

        if self._partial and (final or self.clock() - self._partial_since >= PARTIAL_WAIT):
            out.append(self._line(self._partial))
            self._partial, self._printed_part = b"", True

        if out:
            text = "\n".join(out) + "\n"
            for sink in (self.terminal, self.log):
                if sink is not None:
                    sink.write(text)
                    sink.flush()

    def close(self):
        self.poll(final=True)
        if self.log is not None:
            self.log.close()
            self.log = None
