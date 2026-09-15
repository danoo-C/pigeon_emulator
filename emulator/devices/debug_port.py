"""The debug port: lines out of the machine that never reach its screen.

docs/phase5_plan.md §4.1. bios2, the kernel and any program say where they
have got to here, and the host shows it -- in the launcher's terminal, a
log file, and the front ends' Serial panel -- while the guest's own screen
stays exactly as the program drew it.

  callback(read_write, command, length, address, data)

  cmd  name       R/W  LENGTH  reply
  0    NOP        0    4       4 zero bytes
  1    WRITE      1    n       the text from the data window, at most the
                               window (4 KB); RETURN_DATA is the bytes taken
  2    WRITE_DMA  0    8       [address, count] in the window: count bytes
                               taken from RAM; count, or 0xFFFFFFFF refused

WRITE is for C, which has the text in hand already. With R/W 1 the
controller copies no reply into the window, so the count comes back as
RETURN_DATA, the length of the reply -- a reply of that many zero bytes.
Sent with R/W 0 it has no text to take, and takes nothing.

WRITE_DMA is for assembly: no copy loop, which is what lets stage 1 of the
BIOS say anything in its last 80 bytes. It is sent with R/W 0 so its count
comes back, as the HDD's DMA commands are. Unlike the HDD's, ANY range
inside RAM is fine, the BIOS's own bytes included: the port only reads
RAM, so nothing it is pointed at can be damaged, and stage 1's text lives
in the BIOS. It refuses a range past the end of RAM and more than DMA_MAX
bytes at once.

The host keeps the last KEEP bytes. Every byte since the port was made has
an offset, so a reader asks for what came after the last offset it saw,
and one that has fallen behind is told how many bytes it lost. The guest
sends bytes only: the time a line started is noted here, from the timer
device's clock -- looked up at each write, not imported, so the tests'
stepping clock applies (tests/_runner.py).
"""

import struct
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import List, Tuple

from . import timer
from ..memory_map import IO_SIZE, IO_START, IOHeader

CMD_NOP = 0
CMD_WRITE = 1
CMD_WRITE_DMA = 2

KEEP = 64 * 1024            # bytes the host keeps
DMA_MAX = 64 * 1024         # the most one WRITE_DMA takes
DMA_REFUSED = 0xFFFFFFFF
WINDOW = IO_SIZE - IOHeader.USABLE_AFTER
_WINDOW_BASE = IO_START + IOHeader.USABLE_AFTER


# Control characters would move a terminal's cursor or change its colours:
# every one but the newline and the tab, and DEL, shows as U+FFFD, as bytes
# that aren't UTF-8 do. One character for one, so offsets into the text stay
# where they were.
_CONTROLS = {c: "\ufffd" for c in [*range(0x20), 0x7F] if c not in (0x09, 0x0A)}


def clean(data: bytes) -> str:
    """Bytes from the guest as text that is safe to show anywhere."""
    return data.decode("utf-8", errors="replace").translate(_CONTROLS)


@dataclass
class Chunk:
    """What came after an offset: `data`, from `start` up to `next`, the
    offset to ask from next time. `stamps` are the lines starting in it,
    as (offset, seconds since the port was made). `lost` counts the bytes
    between the offset asked for and `start` that are no longer kept."""
    start: int
    next: int
    data: bytes
    stamps: List[Tuple[int, float]]
    lost: int


class DebugPort:
    def __init__(self, ram, keep: int = KEEP):
        self.ram = ram
        self.keep = keep
        # Written from the emulator thread, read from the HTTP server's.
        self._lock = Lock()
        self._kept = bytearray()
        self._start = 0                     # the offset of _kept[0]
        self._stamps: deque = deque()       # (offset, seconds), oldest first
        self._line_start = True             # the next byte starts a line
        self._born = timer.clock()

    @property
    def end(self) -> int:
        """The offset the next byte will have: every byte ever written."""
        return self._start + len(self._kept)

    def write(self, data: bytes) -> None:
        """Take bytes, as the guest's commands do."""
        if not data:
            return
        with self._lock:
            offset = self.end
            starts = [offset] if self._line_start else []
            newline = data.find(b"\n")
            while newline != -1 and newline + 1 < len(data):
                starts.append(offset + newline + 1)
                newline = data.find(b"\n", newline + 1)
            self._line_start = data.endswith(b"\n")
            if starts:
                # One look at the clock a write, and only when a line starts:
                # on the stepping clock every look moves the guest's timers.
                now = timer.clock() - self._born
                self._stamps.extend((start, now) for start in starts)

            self._kept += data
            excess = len(self._kept) - self.keep
            if excess > 0:
                del self._kept[:excess]
                self._start += excess
                while self._stamps and self._stamps[0][0] < self._start:
                    self._stamps.popleft()

    def since(self, offset: int) -> Chunk:
        """What came after `offset`. An offset past the end is from a machine
        before this one -- a page left open across a restart -- and reads
        from the oldest byte kept, as 0 does."""
        with self._lock:
            end = self.end
            if offset > end:
                offset = 0
            start = max(offset, self._start)
            data = bytes(self._kept[start - self._start:])
            stamps = [s for s in self._stamps if s[0] >= start]
            return Chunk(start=start, next=end, data=data, stamps=stamps,
                         lost=start - offset)

    def callback(self, read_write: int, command: int, length: int, address: int,
                 data: bytearray) -> bytes:
        cmd = int(command)
        if cmd == CMD_NOP:
            return bytes(4)
        if cmd == CMD_WRITE:
            if read_write != 1:
                return b""
            taken = bytes(data[:min(int(length), WINDOW)])
            self.write(taken)
            return bytes(len(taken))
        if cmd == CMD_WRITE_DMA:
            where, count = struct.unpack_from("<II", self.ram.mem, _WINDOW_BASE)
            if count > DMA_MAX or where + count > len(self.ram.mem):
                return struct.pack("<I", DMA_REFUSED)
            self.write(bytes(self.ram.mem[where:where + count]))
            return struct.pack("<I", count)
        return b""


def serial_reply(port, offset) -> dict:
    """GET /serial?from=N, as the display server answers it: the text after
    byte N, the byte offset to ask from next, how many bytes were lost, and
    each line start as [index into the text, seconds since power-on].

    The text is decoded a line at a time, so an index counts characters in
    the text the page is given, not bytes. With no port, nothing."""
    if port is None:
        return {"start": 0, "next": 0, "text": "", "stamps": [], "lost": 0}
    chunk = port.since(max(0, int(offset)))
    times = dict(chunk.stamps)
    cuts = sorted({0, len(chunk.data), *(s - chunk.start for s in times)})
    pieces, stamps, at = [], [], 0
    for begin, finish in zip(cuts, cuts[1:]):
        if chunk.start + begin in times:
            stamps.append([at, round(times[chunk.start + begin], 3)])
        piece = clean(chunk.data[begin:finish])
        pieces.append(piece)
        at += len(piece)
    return {"start": chunk.start, "next": chunk.next, "text": "".join(pieces),
            "stamps": stamps, "lost": chunk.lost}
