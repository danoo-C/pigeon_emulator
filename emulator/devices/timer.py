"""IO Timer device for the Pigeon IOController.

This module implements a simple countdown-timer peripheral that plugs
into Pigeon's channel-based IOController the same way the HDD/HID/
display devices do. Each timer is identified by an "address" (its
channel id) and is created lazily the first time that address is used.

Commands (passed as `command`):
    CMD_NOP    (0) - no-op, returns `length` zero bytes.
    CMD_START  (1) - start (or restart) the timer at `address`.
                     `length` is the requested duration in milliseconds.
    CMD_STOP   (2) - stop the timer at `address` (freezes it in
                     STATUS_STOPPED). A ticking timer stops ticking.
    CMD_RESET  (4) - restart the timer at `address` using its last
                     duration.
    CMD_STATUS (5) - read back the current status/remaining time for
                     the timer at `address` without changing it.
    CMD_TICK   (6) - start the timer at `address` ticking: every `length`
                     milliseconds, until STOP, it raises the timer
                     interrupt, VEC_TIMER (docs/kernel.md §13). It reads
                     as RUNNING, with the time left to its next tick.
                     START makes it a one-shot timer again.

Status values (returned as the first byte of every response):
    STATUS_NONE    (0) - timer was never started.
    STATUS_RUNNING (1) - timer is counting down.
    STATUS_DONE    (2) - timer reached zero.
    STATUS_STOPPED (3) - timer was explicitly stopped.

Return encoding:
    CMD_START/STOP/RESET/STATUS/TICK all return a fixed RESPONSE_LEN
    bytes (8 by default) as two little-endian 32-bit words, regardless of
    the requested duration:
        word 0 (bytes 0-3) -> status (one of STATUS_*)
        word 1 (bytes 4-7) -> remaining time in milliseconds
                               (0 once the timer is done, stopped, or
                               was never started)
    This is deliberately NOT tied to `length`, since `length` is the
    duration for CMD_START and could be large (e.g. 5000 for a 5s
    timer) - reusing it as the response size would make a long timer
    come back as a mostly-zero, multi-KB response. CMD_NOP still
    echoes back `length` zero bytes, matching the original stub.

Timing note: this is wall-clock based (it reads `clock`, which is
time.time()), so it's a "real seconds" timer, not tied to emulated CPU
cycles. A tick is noticed when the machine polls, every
CLOCK_SAMPLE_INTERVAL instructions (emulator/machine.py), and ticks missed
in between arrive as one: an interrupt is a single pending bit.
"""

import struct
import time

# What every timer reads the time from. The tests swap in a clock that steps
# each time it is read (tests/_runner.py), so a guest waiting on a timer
# costs them no real seconds.
clock = time.time


CMD_NOP = 0
CMD_START = 1
CMD_STOP = 2
CMD_RESET = 4
CMD_STATUS = 5
CMD_TICK = 6


STATUS_NONE = 0
STATUS_RUNNING = 1
STATUS_DONE = 2
STATUS_STOPPED = 3

# Bytes per response: two 32-bit words (status, remaining_ms). Fixed on
# purpose: `length` is the requested duration for CMD_START and can be
# large (a 5s timer is length=5000), so it must NOT also drive the
# response size, or a long timer would come back as a 5000-byte,
# mostly-zero response.
RESPONSE_LEN = 8


class TI:
    """A single timer instance (one per address/channel)."""

    def __init__(self, id):
        self.id = id
        self.status = STATUS_NONE
        self.user_time = 0.0    # requested duration, in seconds
        self.start_time = 0.0
        self.period = 0.0       # seconds between ticks; 0 for a one-shot timer

    def start(self, duration_seconds: float) -> None:
        self.user_time = duration_seconds
        self.period = 0.0
        self.start_time = clock()
        self.status = STATUS_RUNNING

    def tick(self, period_seconds: float) -> None:
        """Tick every `period_seconds` until stopped. start_time is the last
        tick, so the remaining time is the time to the next one."""
        self.user_time = self.period = period_seconds
        self.start_time = clock()
        self.status = STATUS_RUNNING

    def stop(self) -> None:
        self.status = STATUS_STOPPED

    def reset(self) -> None:
        """Restart the countdown using the last duration passed to start()."""
        self.start_time = clock()
        self.status = STATUS_RUNNING

    @property
    def ticking(self) -> bool:
        return self.status == STATUS_RUNNING and self.period > 0

    def get(self):
        """Return (status, remaining_seconds).

        Side effect: flips RUNNING -> DONE once time is up, unless the
        timer is ticking, which never finishes.
        """
        if self.status != STATUS_RUNNING:
            return (self.status, 0)

        elapsed = clock() - self.start_time
        remaining = self.user_time - elapsed
        if remaining <= 0:
            if not self.period:
                self.status = STATUS_DONE
            return (self.status, 0)
        return (self.status, remaining)

    def due(self, now: float) -> bool:
        """For a ticking timer: whether a tick has come since the last one,
        moving on to the latest. Several missed ticks count as one."""
        missed = int((now - self.start_time) / self.period)
        if missed <= 0:
            return False
        self.start_time += missed * self.period
        return True


class Timer:
    def __init__(self):
        self.timers: dict[int, TI] = {}

    def _get_timer(self, addr: int) -> TI:
        """Return the TI for this address, creating it on first use."""
        timer = self.timers.get(addr)
        if timer is None:
            timer = TI(addr)
            self.timers[addr] = timer
        return timer

    def poll(self) -> bool:
        """Whether any ticking timer has ticked since the last poll: the
        machine raises the timer interrupt when it has. The clock is read
        only while a timer is ticking."""
        due = False
        now = None
        for timer in self.timers.values():
            if timer.ticking:
                if now is None:
                    now = clock()
                due = timer.due(now) or due
        return due

    @staticmethod
    def _encode(status: int, remaining_seconds: float) -> bytes:
        remaining_ms = int(round(remaining_seconds * 1000))
        if remaining_ms < 0:
            remaining_ms = 0
        remaining_ms &= 0xFFFFFFFF
        # word 0: status, word 1: remaining time in ms - both 32-bit, little-endian
        return struct.pack("<II", status & 0xFFFFFFFF, remaining_ms)

    def callback(self, read_write: int, command: int, length: int, address: int, data: bytearray) -> bytes:
        """IOController-compatible callback.

        - `read_write`: 0=read, always read
        - `command`: one of CMD_* constants
        - `length`: for CMD_START, the requested duration in milliseconds;
          for CMD_TICK, the milliseconds between ticks; for CMD_NOP, the
          number of filler bytes to return. Ignored by
          CMD_STOP/CMD_RESET/CMD_STATUS.
        - `address`: timer id
        - `data`: unused
        - always returns bytes, never None -- the controller writes
          len(result) into RETURN_DATA and used to crash on None
        - returns RESPONSE_LEN bytes (two 32-bit words: status, remaining_ms)
          for every command except CMD_NOP; see module docstring for the encoding
        """
        addr = int(address) & 0xFFFFFFFF
        cmd = int(command)

        if cmd == CMD_NOP:
            return b"\x00" * max(length, 0)
        timer = self._get_timer(addr)

        if cmd == CMD_START:
            timer.start(length / 1000.0)
            status, remaining = timer.get()
            return self._encode(status, remaining)

        if cmd == CMD_TICK:
            # At least a millisecond: a period of 0 would be due at every poll.
            timer.tick(max(length, 1) / 1000.0)
            status, remaining = timer.get()
            return self._encode(status, remaining)

        if cmd == CMD_STOP:
            timer.stop()
            return self._encode(timer.status, 0)

        if cmd == CMD_RESET:
            timer.reset()
            status, remaining = timer.get()
            return self._encode(status, remaining)

        if cmd == CMD_STATUS:
            status, remaining = timer.get()
            return self._encode(status, remaining)

        # unknown command: behave like NOP rather than raising
        return b"\x00" * max(length, 0)
