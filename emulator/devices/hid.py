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

import logging
from collections import deque
from threading import Lock, Thread
from typing import Optional

from .keycodes import describe

# --- FIFO commands: ordered, destructive, nothing is lost ---------------
CMD_GET_KEYBOARD = 3        # pops one character code; 0x00 if none waiting
CMD_GET_MOUSE_EVENT = 4     # pops one button press/release edge; 0x00 if none waiting
CMD_GET_KEY_EVENT = 5       # pops one key press/release edge as 2 bytes [flags][code]

# --- real-time commands: current state, non-destructive -----------------
CMD_GET_MOUSE_POS = 1       # one word: x in bits 31-16, y in bits 15-0
CMD_GET_MOUSE_BUTTONS = 2   # one byte bitmask: bit0=left, bit1=right, bit2=middle, ...
CMD_GET_KEY_STATE = 6       # one byte 0/1 -- which key is passed in `address`
CMD_GET_KEY_BITMAP = 7      # 32 bytes; bit N set means key N is held

CMD_NOP = 0

# Two buffers, because they answer different questions. The FIFO answers
# "what happened, in order" -- a press and release between two polls is
# still delivered. The real-time state answers "what is true right now" --
# which is what hold-to-move needs, and which a FIFO cannot give you
# without the guest replaying every edge since boot.

KEY_QUEUE_MAXLEN = 256      # oldest events are dropped once the queue is full
MOUSE_EVENT_QUEUE_MAXLEN = 256
KEY_EVENT_QUEUE_MAXLEN = 256

KEY_BITMAP_BYTES = 32       # 256 keys, one bit each
KEY_EVENT_BYTES = 2         # [flags][code]

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

# Key events need the same VALID bit for the same reason, and a whole
# second byte for the code: the character FIFO (command 3) returns 0x00
# for "queue empty", which is indistinguishable from a real key whose
# code is 0. Splitting flags from the code removes the ambiguity and
# leaves the full 0x00-0xFF keycode space usable.
_KEY_EVENT_VALID_BIT = 0x80
_KEY_EVENT_PRESS_BIT = 0x40

log = logging.getLogger(__name__)


class HID:
    def __init__(self):
        self._lock = Lock()

        self._mouse_x = 0
        self._mouse_y = 0
        self._mouse_buttons = 0  # bitmask

        self._key_queue: deque = deque(maxlen=KEY_QUEUE_MAXLEN)
        self._mouse_event_queue: deque = deque(maxlen=MOUSE_EVENT_QUEUE_MAXLEN)
        self._key_event_queue: deque = deque(maxlen=KEY_EVENT_QUEUE_MAXLEN)

        # Real-time key state: one bit per code, so 256 keys in 32 bytes.
        self._key_state = bytearray(KEY_BITMAP_BYTES)

        self._server_thread: Optional[Thread] = None

    # --- state updates (called by the FastAPI endpoints below) -----------

    def set_mouse_pos(self, x: int, y: int) -> None:
        with self._lock:
            self._mouse_x = x & 0xFFFF
            self._mouse_y = y & 0xFFFF

    def set_mouse_buttons(self, buttons: int) -> None:
        with self._lock:
            self._mouse_buttons = buttons & 0xFF

    def push_key(self, code: int, pressed: bool = True) -> None:
        """Record a key transition into both buffers.

        `pressed` defaults to True so an older client that posts only key
        presses keeps working, though it will never release a key in the
        state bitmap.

        Codes above 0xFF are REJECTED, not masked. Masking is what turned
        the Right arrow (pygame 1073741903) into 79 -- the letter 'O' --
        so a front end that forgets to translate now fails loudly instead
        of feeding plausible garbage to the guest.
        """
        if not 0 <= code <= 0xFF:
            log.warning("HID: dropping out-of-range key code %d; front ends must "
                        "translate to the pigeon keycode space (see keycodes.py)",
                        code)
            return

        with self._lock:
            if pressed:
                # The character stream only carries presses -- a release is
                # not a character.
                self._key_queue.append(code)
                self._key_state[code >> 3] |= 1 << (code & 7)
            else:
                self._key_state[code >> 3] &= ~(1 << (code & 7)) & 0xFF

            event = _KEY_EVENT_VALID_BIT | (_KEY_EVENT_PRESS_BIT if pressed else 0)
            self._key_event_queue.append((event, code))

    def pop_key_event(self) -> bytes:
        """Next key edge as [flags][code], or two zero bytes when empty."""
        with self._lock:
            if self._key_event_queue:
                flags, code = self._key_event_queue.popleft()
                return bytes((flags, code))
            return b"\x00\x00"

    def key_is_down(self, code: int) -> bool:
        if not 0 <= code <= 0xFF:
            return False
        with self._lock:
            return bool(self._key_state[code >> 3] & (1 << (code & 7)))

    def key_bitmap(self) -> bytes:
        """Snapshot of every held key, taken atomically."""
        with self._lock:
            return bytes(self._key_state)

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
        if not 0 <= button <= 0x1F:
            log.warning("HID: dropping out-of-range mouse button %d", button)
            return

        event = _EVENT_VALID_BIT | button
        if pressed:
            event |= _EVENT_PRESS_BIT
        with self._lock:
            self._mouse_event_queue.append(event)
            # Keep the real-time mask in step with the edges. Without this
            # the mask is dead: no front end has ever posted /mouse_buttons,
            # so CMD_GET_MOUSE_BUTTONS always returned 0.
            if pressed:
                self._mouse_buttons |= 1 << button
            else:
                self._mouse_buttons &= ~(1 << button) & 0xFF

    def pop_mouse_event(self) -> int:
        with self._lock:
            if self._mouse_event_queue:
                return self._mouse_event_queue.popleft()
            return 0

    # --- IOController-facing callback -------------------------------------

    def callback(self, read_write: int, command: int, length: int, address: int,
                 data: bytearray) -> bytes:
        """IOController-compatible callback.

        Replies are a FIXED size per command, not `length` bytes -- the
        controller reports the true size in IO_RETURN_DATA, and tying the
        reply to `length` is how the FIFO commands became a trap (a guest
        doing a word-sized read of command 3 pops FOUR keys and looks at
        one). devices/timer.py set this precedent first.
        """
        cmd = int(command)
        length = int(length) if length is not None else 0

        if cmd == CMD_NOP:
            return b"\x00" * max(length, 0)

        # Draining a FIFO is destructive, so it must not happen on a write:
        # io_controller discards a write's return value, so the popped input
        # would vanish silently.
        if read_write == 1 and cmd in (CMD_GET_KEYBOARD, CMD_GET_MOUSE_EVENT,
                                       CMD_GET_KEY_EVENT):
            log.warning("HID: command %d is a read; ignoring it on a write", cmd)
            return b""

        # --- real-time state ------------------------------------------------
        if cmd == CMD_GET_MOUSE_POS:
            with self._lock:
                packed = (self._mouse_x << 16) | self._mouse_y
            return packed.to_bytes(4, "little")

        if cmd == CMD_GET_MOUSE_BUTTONS:
            with self._lock:
                return bytes([self._mouse_buttons])

        if cmd == CMD_GET_KEY_STATE:
            return b"\x01" if self.key_is_down(int(address)) else b"\x00"

        if cmd == CMD_GET_KEY_BITMAP:
            return self.key_bitmap()

        # --- FIFOs ----------------------------------------------------------
        if cmd == CMD_GET_KEYBOARD:
            return bytes([self.pop_key()])

        if cmd == CMD_GET_MOUSE_EVENT:
            return bytes([self.pop_mouse_event()])

        if cmd == CMD_GET_KEY_EVENT:
            return self.pop_key_event()

        log.warning("HID: unknown command %d", cmd)
        return b"\x00" * max(length, 0)

    # --- FastAPI server (separate port from DisplayIO) ---------------------

    def start_fastapi(self, host: str = "127.0.0.1", port: int = 8001,
                      allow_origins=None):
        """Start a background FastAPI server that an external input source
        (e.g. a pygame capture window) can push mouse/keyboard events into.

        Runs on its own daemon thread, so it doesn't block the emulator and
        doesn't conflict with DisplayIO's own FastAPI server as long as the
        ports differ (DisplayIO defaults to 8000, this defaults to 8001).
        """
        if self._server_thread is not None and self._server_thread.is_alive():
            return

        # Checked on the caller's thread -- an ImportError inside the daemon
        # thread below would be swallowed, leaving input silently dead.
        try:
            from fastapi import FastAPI
            from fastapi.middleware.cors import CORSMiddleware
            from pydantic import BaseModel
            import uvicorn
        except ImportError as e:
            raise RuntimeError(
                "FastAPI/uvicorn/pydantic not installed: pip install -r requirements.txt") from e

        def _run():
            app = FastAPI()
            app.add_middleware(
                CORSMiddleware,
                # The browser front end is served from the DISPLAY port, so
                # its origin must be allowed here or every POST fails
                # preflight. A bare "http://127.0.0.1" does not match
                # "http://127.0.0.1:1234" -- origins compare with the port.
                allow_origins=allow_origins or [f"http://{host}:{port}"],
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
                pressed: bool = True   # default keeps older clients working

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
                # Never chr() this: the client forwards raw pygame keycodes,
                # and arrow/function keys sit above chr()'s maximum, which
                # used to raise ValueError right here on an ordinary keypress.
                log.debug("HID: key %s %s", describe(body.code),
                          "down" if body.pressed else "up")
                self.push_key(body.code, body.pressed)
                return {"status": "ok"}

            @app.get("/state")
            async def state():
                with self._lock:
                    return {
                        "mouse_x": self._mouse_x,
                        "mouse_y": self._mouse_y,
                        "mouse_buttons": self._mouse_buttons,
                        "queued_keys": len(self._key_queue),
                        "queued_key_events": len(self._key_event_queue),
                        "queued_mouse_events": len(self._mouse_event_queue),
                        "keys_held": sum(bin(b).count("1") for b in self._key_state),
                    }

            uvicorn.run(app, host=host, port=port, log_level="warning")

        self._server_thread = Thread(target=_run, daemon=True)
        self._server_thread.start()