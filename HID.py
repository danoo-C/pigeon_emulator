"""
HID -- Human Interface Device (input) support for the emulator.

Works with the IOController to provide mouse and keyboard input to the
CPU. HID does NOT handle display output at all -- that's DisplayIO's job
(display_io.py). This module only tracks input:

  - mouse position and button state (live, overwritten on each update)
  - keyboard events (queued in a FIFO, since discrete keypresses need to
    be polled one at a time rather than overwritten like mouse state)

An external input source (e.g. a pygame window capturing real mouse and
keyboard input) pushes events in over its own FastAPI server. This runs
on a different port than DisplayIO's server -- multiple uvicorn instances
can run in the same process as long as each one runs on its own thread
(giving it its own asyncio event loop) and listens on its own port, which
is exactly how both HID and DisplayIO are set up.
"""

from collections import deque
from threading import Lock, Thread
from typing import Optional

CMD_NOP = 0
CMD_GET_MOUSE_POS = 1       # 32 bits split into 2 16-bit values: x is the high 16 bits, y is the low 16 bits
CMD_GET_MOUSE_BUTTONS = 2   # 8 bits, each bit a button: bit0=left, bit1=right, bit2=middle, bit3=back, bit4=forward
CMD_GET_KEYBOARD = 3        # pops the next queued key code from the FIFO; 0x00 if none waiting
CMD_GET_MOUSE_EVENT = 4     # pops the next queued button press/release edge from the FIFO; 0x00 if none waiting

KEY_QUEUE_MAXLEN = 256      # oldest events are dropped once the queue is full
MOUSE_EVENT_QUEUE_MAXLEN = 256

# Mouse button indices, used both in the live bitmask and in queued edge events
BUTTON_LEFT = 0
BUTTON_RIGHT = 1
BUTTON_MIDDLE = 2
BUTTON_BACK = 3
BUTTON_FORWARD = 4

# Mouse event byte layout (why not just reuse the bitmask): a bitmask only tells
# you what's held *right now*, so a quick press+release between two CPU polls
# is invisible -- the same problem keyboard already solves with a FIFO. This
# gives button clicks the same treatment.
#   bit 7        VALID   always 1 for a real event; 0x00 means "queue empty"
#                        (kept separate from bit semantics below so a real
#                        "button 0 release" event, which would otherwise
#                        encode as plain 0x00, is never confused with "empty")
#   bit 6        PRESS   1 = button went down, 0 = button went up
#   bits 0-4     BUTTON  which button index (see BUTTON_* above)
_EVENT_VALID_BIT = 0x80
_EVENT_PRESS_BIT = 0x40


class HID:
    def __init__(self):
        self._lock = Lock()

        self._mouse_x = 0
        self._mouse_y = 0
        self._mouse_buttons = 0  # bitmask

        self._key_queue: deque = deque(maxlen=KEY_QUEUE_MAXLEN)
        self._mouse_event_queue: deque = deque(maxlen=MOUSE_EVENT_QUEUE_MAXLEN)

        self._server_thread: Optional[Thread] = None

    # --- state updates (called by the FastAPI endpoints below) -----------

    def set_mouse_pos(self, x: int, y: int) -> None:
        with self._lock:
            self._mouse_x = x & 0xFFFF
            self._mouse_y = y & 0xFFFF

    def set_mouse_buttons(self, buttons: int) -> None:
        with self._lock:
            self._mouse_buttons = buttons & 0xFF

    def push_key(self, code: int) -> None:
        with self._lock:
            self._key_queue.append(code & 0xFF)

    def pop_key(self) -> int:
        with self._lock:
            if self._key_queue:
                return self._key_queue.popleft()
            return 0

    def push_mouse_event(self, button: int, pressed: bool) -> None:
        """Queue a discrete button transition (press or release). Call this
        for every real MOUSEBUTTONDOWN/UP your input source observes --
        don't try to derive edges by diffing bitmask snapshots, since that
        can miss the same fast clicks polling was already going to miss."""
        event = _EVENT_VALID_BIT | (button & 0x1F)
        if pressed:
            event |= _EVENT_PRESS_BIT
        with self._lock:
            self._mouse_event_queue.append(event)

    def pop_mouse_event(self) -> int:
        with self._lock:
            if self._mouse_event_queue:
                return self._mouse_event_queue.popleft()
            return 0

    # --- IOController-facing callback -------------------------------------

    def callback(self, read_write: int, command: int, length: int, address: int, data: bytearray) -> bytes:
        """IOController-compatible callback."""
        cmd = int(command)
        length = int(length) if length is not None else 0

        if cmd == CMD_NOP:
            return b"\x00" * length

        if cmd == CMD_GET_MOUSE_POS:
            with self._lock:
                packed = (self._mouse_x << 16) | self._mouse_y
            packed_bytes = packed.to_bytes(4, "little")
            return packed_bytes[:length].ljust(length, b"\x00")

        if cmd == CMD_GET_MOUSE_BUTTONS:
            with self._lock:
                buttons = self._mouse_buttons
            return bytes([buttons])[:length].ljust(length, b"\x00")

        if cmd == CMD_GET_KEYBOARD:
            # Pop up to `length` queued key codes (one byte each), FIFO order.
            # Returns 0x00 for any slot with nothing queued.
            out = bytearray(self.pop_key() for _ in range(max(length, 0)))
            return bytes(out)

        if cmd == CMD_GET_MOUSE_EVENT:
            # Pop up to `length` queued button press/release edges, FIFO order.
            # Returns 0x00 for any slot with nothing queued.
            out = bytearray(self.pop_mouse_event() for _ in range(max(length, 0)))
            return bytes(out)

        # unknown command -> return zeros for reads, ignore for writes
        return b"\x00" * length

    # --- FastAPI server (separate port from DisplayIO) ---------------------

    def start_fastapi(self, host: str = "127.0.0.1", port: int = 8001):
        """Start a background FastAPI server that an external input source
        (e.g. a pygame capture window) can push mouse/keyboard events into.

        Runs on its own daemon thread, so it doesn't block the emulator and
        doesn't conflict with DisplayIO's own FastAPI server as long as the
        ports differ (DisplayIO defaults to 8000, this defaults to 8001).
        """
        if self._server_thread is not None and self._server_thread.is_alive():
            return

        def _run():
            try:
                from fastapi import FastAPI
                from fastapi.middleware.cors import CORSMiddleware
                from pydantic import BaseModel
                import uvicorn
            except Exception as e:
                raise RuntimeError("FastAPI/uvicorn/pydantic not installed: install fastapi, uvicorn, and pydantic") from e

            app = FastAPI()
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["*"],
            )

            class MousePos(BaseModel):
                x: int
                y: int

            class MouseButtons(BaseModel):
                buttons: int

            class MouseEvent(BaseModel):
                button: int
                pressed: bool

            class KeyEvent(BaseModel):
                code: int

            @app.post("/mouse_pos")
            async def mouse_pos(body: MousePos):
                self.set_mouse_pos(body.x, body.y)
                return {"status": "ok"}

            @app.post("/mouse_buttons")
            async def mouse_buttons(body: MouseButtons):
                self.set_mouse_buttons(body.buttons)
                return {"status": "ok"}

            @app.post("/mouse_event")
            async def mouse_event(body: MouseEvent):
                self.push_mouse_event(body.button, body.pressed)
                return {"status": "ok"}

            @app.post("/key")
            async def key(body: KeyEvent):
                print(chr(body.code))
                self.push_key(body.code)
                return {"status": "ok"}

            @app.get("/state")
            async def state():
                with self._lock:
                    return {
                        "mouse_x": self._mouse_x,
                        "mouse_y": self._mouse_y,
                        "mouse_buttons": self._mouse_buttons,
                        "queued_keys": len(self._key_queue),
                        "queued_mouse_events": len(self._mouse_event_queue),
                    }

            uvicorn.run(app, host=host, port=port, log_level="warning")

        self._server_thread = Thread(target=_run, daemon=True)
        self._server_thread.start()