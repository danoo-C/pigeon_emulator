"""Display bridge for the pigeon emulator.

This module keeps the emulator RAM display region in an in-memory frame
buffer and serves it to external clients via a background FastAPI server.

The emulator should call `DisplayIO.update()` periodically. The update
function creates a snapshot from RAM and stores it in memory as the
current frame (converted to RGBA byte order for web clients).

It is also a device on the IO bus (channel CH_DISPLAY), which is what
makes a page flip and a screen clear cost a handful of instructions
instead of a loop over every pixel. Profiling the 3D cube found 87% of
all executed guest instructions inside two library functions that did
exactly that -- one word at a time, every frame:

  callback(read_write, command, length, address, data)

  cmd  name          R/W  ADDRESS        LENGTH  reply
  0    NOP           0    --             4       4 zero bytes
  1    INFO          0    --             12      W, H, DISPLAY_SIZE (LE words)
  2    SET_BASE      0    new base       4       1 accepted, 0 rejected
  3    GET_BASE      0    --             4       the current base
  4    FILL          1    destination    4       -- (data window holds the colour)

LENGTH is always the size of the PAYLOAD, never a fill length and never a
colour. That is load-bearing: IOController does `bytearray(length)` before
it calls a device, so a LENGTH of 0xFF102030 -- an ordinary 0xAARRGGBB
colour -- would ask for a 4 GB allocation and take the emulator down with
a MemoryError and no PC to blame it on.

This device is the only one that writes to RAM outside the IO data
window. It can, because it is constructed with the RAM it snapshots; the
bus itself deliberately has no general DMA path, so every other device
still just returns bytes.
"""
import logging
from pathlib import Path
from threading import Lock, Thread
from typing import Optional

from ..memory_map import (
    DISPLAY_START, DISPLAY_SIZE, DISPLAY_W, DISPLAY_H, PROGRAM_LOAD_ADDR,
)

log = logging.getLogger(__name__)

CMD_NOP = 0
CMD_INFO = 1
CMD_SET_BASE = 2
CMD_GET_BASE = 3
CMD_FILL = 4

# devices/ -> emulator/ -> repo root, where display/index.html lives.
# This used to be resolved relative to this file's own directory; once the
# module moved into emulator/devices/ that silently pointed at nothing, and
# the miss is swallowed by the except below (you get "Frontend not found",
# not an error).
REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_HTML = REPO_ROOT / "display" / "index.html"


class DisplayIO:
    def __init__(self, ram):
        self.ram = ram
        self.display_start = DISPLAY_START
        self.display_size = DISPLAY_SIZE
        # Where the screen is read from. The guest moves this with
        # CMD_SET_BASE to flip pages; it is not necessarily DISPLAY_START.
        # Only ever touched from the emulator thread (the callback runs
        # from IOController.update(), which runs from Machine.run(), which
        # also calls update()) -- so no lock. Only _frame crosses to the
        # uvicorn thread, and that has one already.
        self.scanout_base = DISPLAY_START
        # A whole screen of one colour, kept between calls: disp_clear()
        # passes the same colour every frame, and rebuilding the pattern
        # would allocate a screen's worth of bytes each time.
        self._fill_colour: Optional[int] = None
        self._fill_bytes: Optional[bytes] = None
        # In-memory frame buffer (RGBA byte order) for FastAPI serving
        self._frame_lock = Lock()
        self._frame: Optional[bytes] = None
        self._server_thread: Optional[Thread] = None
        # Told to the browser front end via /info so it knows where to POST
        # input. Keeps config.json the single source of truth for ports.
        self.hid_url: Optional[str] = None

    # --- the framebuffer ---------------------------------------------------

    def snapshot(self) -> bytes:
        """The bytes currently on screen, wherever the guest put them.

        One definition of "where the screen is", so a caller never has to
        assume DISPLAY_START -- after a page flip, that is only the screen
        every other frame.
        """
        base = self.scanout_base
        return bytes(self.ram.mem[base:base + self.display_size])

    def _valid_base(self, base: int) -> bool:
        """Can the guest point the display (or a fill) at this address?

        Checked when the guest sets it, not when the frame is read: the
        snapshot runs 30 times a second and should stay a bare slice.
        """
        if base & 3:
            return False            # pixels are words; unaligned shears every one
        if base + self.display_size > self.ram.size:
            # The important one. A slice assignment past the end of a
            # bytearray does not raise and does not clamp -- it RESIZES,
            # growing the address space past RAM_SIZE and breaking every
            # `addr & mask` in the machine. See ram.py's write_word.
            return False
        if base == DISPLAY_START:
            return True             # back to the hardware framebuffer
        # Anything below the program is the BIOS, the IO header, or a
        # framebuffer that has half slid off its own start. The realistic
        # way to get here is malloc() returning NULL and the guest
        # scanning out address 0.
        return base >= PROGRAM_LOAD_ADDR

    # --- the IO bus device -------------------------------------------------

    def callback(self, read_write: int, command: int, length: int, address: int,
                 data: bytearray) -> bytes:
        """IOController-compatible callback. See the module docstring."""
        if command == CMD_INFO:
            return (DISPLAY_W.to_bytes(4, "little")
                    + DISPLAY_H.to_bytes(4, "little")
                    + self.display_size.to_bytes(4, "little"))

        if command == CMD_GET_BASE:
            return self.scanout_base.to_bytes(4, "little")

        if command == CMD_SET_BASE:
            if not self._valid_base(address):
                # Keep the previous base rather than clamping to
                # DISPLAY_START: a clamp would leave the guest drawing
                # into a buffer nobody is watching while believing it had
                # flipped. Answering 0 lets it fall back to copying.
                log.warning("DISPLAY: refusing scanout base %#x", address)
                return b"\x00\x00\x00\x00"
            self.scanout_base = address
            return b"\x01\x00\x00\x00"

        if command == CMD_FILL:
            if not self._valid_base(address):
                log.warning("DISPLAY: refusing fill at %#x", address)
                return b""
            colour = int.from_bytes(data[:4], "little") if len(data) >= 4 else 0
            if colour != self._fill_colour:
                self._fill_colour = colour
                self._fill_bytes = (colour.to_bytes(4, "little")
                                    * (self.display_size // 4))
            # Slice assignment, not a write_word loop: at 20,736 pixels
            # the loop would be slower than the guest code it replaces.
            # Safe to bypass RAM's IO-window check because _valid_base
            # admits nothing below PROGRAM_LOAD_ADDR except DISPLAY_START,
            # and both are above the IO window.
            self.ram.mem[address:address + self.display_size] = self._fill_bytes
            return b""

        if command == CMD_NOP:
            return b"\x00\x00\x00\x00"

        log.warning("DISPLAY: unknown command %d", command)
        return b""

    def update(self) -> bool:
        """Snapshot the current display region from RAM into the in-memory frame buffer.

        Returns True if a new frame was captured.
        """
        data = self.snapshot()
        try:
            rgba = self._convert_to_rgba(data)
        except Exception:
            # Keep updates tolerant: don't crash the emulator if conversion fails
            return False
        with self._frame_lock:
            self._frame = rgba
        return True

    def _convert_to_rgba(self, data: bytes) -> bytes:
        # Incoming memory layout is B,G,R,A per pixel; convert to R,G,B,A
        if not data:
            return b"\x00" * self.display_size
        # Swap the R and B channels with strided slice assignment. The
        # equivalent per-byte Python loop cost 2.2 ms/frame at 100x100 --
        # a 450 FPS ceiling that would drop under 15 FPS at 640x480.
        out = bytearray(data)
        out[0::4] = data[2::4]   # R <- B
        out[2::4] = data[0::4]   # B <- R
        return bytes(out)

    def get_frame(self) -> bytes:
        """Return the latest frame in RGBA byte order. If no frame exists, return zeros."""
        with self._frame_lock:
            if self._frame is None:
                return b"\x00" * self.display_size
            return self._frame

    def clear(self) -> None:
        """Clear the in-memory frame."""
        with self._frame_lock:
            self._frame = b"\x00" * self.display_size

    def start_fastapi(self, host: str = "127.0.0.1", port: int = 8000, serve_frontend: bool = True):
        """Start a background FastAPI server that exposes the current frame and control endpoints.

        This is optional and runs in a daemon thread. FastAPI endpoints are async so they do not
        block the emulator; frames are served from an in-memory buffer.
        """
        if self._server_thread is not None and self._server_thread.is_alive():
            return

        # Checked here, on the caller's thread: an ImportError raised inside
        # the daemon thread below is swallowed, leaving the emulator running
        # with no display and no explanation.
        try:
            from fastapi import FastAPI, Response
            from fastapi.responses import HTMLResponse
            from fastapi.middleware.cors import CORSMiddleware
            import uvicorn
        except ImportError as e:
            raise RuntimeError(
                "FastAPI/uvicorn not installed: pip install -r requirements.txt") from e

        def _run():
            app = FastAPI()
            app.add_middleware(
                CORSMiddleware,
                allow_origins=[f"http://{host}:{port}", "http://127.0.0.1", "http://localhost"],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["*"],
            )

            @app.get("/frame")
            async def frame():
                data = self.get_frame()
                return Response(content=data, media_type="application/octet-stream")

            @app.post("/clear")
            async def clear_endpoint():
                self.clear()
                return {"status": "ok"}

            @app.get("/info")
            async def info():
                return {"w": DISPLAY_W, "h": DISPLAY_H, "size": self.display_size,
                        "scanout": self.scanout_base, "hid_url": self.hid_url}

            if serve_frontend:
                @app.get("/", response_class=HTMLResponse)
                async def index():
                    try:
                        return HTMLResponse(content=FRONTEND_HTML.read_text(encoding="utf-8"))
                    except OSError:
                        return HTMLResponse(
                            content=f"<html><body>Frontend not found at "
                                    f"{FRONTEND_HTML}</body></html>")

            uvicorn.run(app, host=host, port=port, log_level="warning")

        self._server_thread = Thread(target=_run, daemon=True)
        self._server_thread.start()