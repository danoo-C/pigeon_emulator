"""Display bridge for the pigeon emulator.

This module keeps the emulator RAM display region in an in-memory frame
buffer and serves it to external clients via a background FastAPI server.

The emulator should call `DisplayIO.update()` periodically. The update
function snapshots the screen and stores it, as it is in memory -- B, G,
R, A -- together with the mode it was drawn in. /frame serves those bytes
with the mode in an X-Pigeon-Mode header, and the clients swizzle
(docs/gac/plans/phase4_frontends.md): 1.3 ms in JavaScript at 1280 x 720,
nothing at all in pygame, where the server's version took 6.4 ms of the
emulator's own thread.

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
  5    COPY          0    buffer         4       1 moved, 0 refused

COPY reads three words from the data window, as the HDD's DMA commands do
-- the offset to move to, the offset to move from, and how many bytes --
and moves them within the buffer at ADDRESS, overlapping or not. It is a
console's scroll: a screen of text redrawn cost about a million
instructions, and the same pixels moved here cost a handful
(docs/phase4b_plan.md, step 1).

LENGTH is always the size of the PAYLOAD, never a fill length and never a
colour. That is load-bearing: IOController does `bytearray(length)` before
it calls a device, so a LENGTH of 0xFF102030 -- an ordinary 0xAARRGGBB
colour -- would ask for a 4 GB allocation and take the emulator down with
a MemoryError and no PC to blame it on.

The screen's size is the current mode, not a constant
(docs/gac/phase2_vram.md). The VRAM device on CH_VRAM changes it, and
points the scanout at a surface in video memory; INFO, FILL and COPY
follow the mode, so a program built for 192 x 108 that checks INFO -- as
disp_probe does -- falls back to software instead of writing a big mode's
worth of bytes into a buffer sized for a small one. GET_BASE answers the
aperture address of a VRAM surface, and SET_BASE takes it back.

This device is the only one that writes to RAM outside the IO data
window. It can, because it is constructed with the RAM it snapshots; the
bus itself still has no general DMA path -- the HDD gained its own pair of
DMA commands in filesystem phase 6, and does the same -- so every other device
still just returns bytes.
"""
import logging
import struct
from collections import deque
from pathlib import Path
from threading import Lock, Thread
from typing import Optional

from ..memory_map import (
    DISPLAY_START, DISPLAY_SIZE, DISPLAY_W, DISPLAY_H, IO_START, IOHeader, PROGRAM_LOAD_ADDR,
)
from .debug_port import serial_reply

log = logging.getLogger(__name__)

CMD_NOP = 0
CMD_INFO = 1
CMD_SET_BASE = 2
CMD_GET_BASE = 3
CMD_FILL = 4
CMD_COPY = 5

_WINDOW_BASE = IO_START + IOHeader.USABLE_AFTER

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
        # The current mode. DISPLAY_W x DISPLAY_H until the VRAM device
        # says otherwise (set_mode).
        self.width, self.height = DISPLAY_W, DISPLAY_H
        self.display_size = DISPLAY_SIZE
        # The other scanout source: an offset into video memory, or None
        # while the screen is read from RAM at scanout_base. Only one of
        # the two is live.
        self.scanout_vram: Optional[int] = None
        # The VRAM device, when the machine has one; Machine wires it. It
        # is how SET_BASE takes an aperture address.
        self.vram = None
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
        # Frames that did not change are not sent again
        # (docs/gac/plans/phase8_bandwidth.md): a picture gets a new number
        # only when it differs from the last, with the band of rows that
        # did, and the last BANDS of those bands answer /frame?since=N.
        self._frame_number = 0
        self._bands: deque = deque(maxlen=BANDS)
        self._server_thread: Optional[Thread] = None
        # Told to the browser front end via /info so it knows where to POST
        # input. Keeps config.json the single source of truth for ports.
        self.hid_url: Optional[str] = None
        # Both are set by Machine.start_servers, so config.json stays the
        # single source of truth for ports and index.html hardcodes none.
        self.cd_url: Optional[str] = None
        # The debug port /serial reads, set there too: the page polls it on
        # its own origin (docs/phase5_plan.md §4.4).
        self.debug_port = None

    # --- the framebuffer ---------------------------------------------------

    def snapshot(self) -> bytes:
        """The bytes currently on screen, wherever the guest put them.

        One definition of "where the screen is", so a caller never has to
        assume DISPLAY_START -- after a page flip, that is only the screen
        every other frame.
        """
        offset = self.scanout_vram
        if offset is not None:
            return bytes(self.ram.vram[offset:offset + self.display_size])
        base = self.scanout_base
        return bytes(self.ram.mem[base:base + self.display_size])

    # --- the scanout selector, driven by the VRAM device ---------------------

    def set_mode(self, width: int, height: int) -> None:
        """A new mode: every size this device uses follows it."""
        self.width, self.height = width, height
        self.display_size = width * height * 4
        self._fill_colour = self._fill_bytes = None     # sized for the old one

    def scan_vram(self, offset: int) -> None:
        """Show the screen out of video memory, from `offset`. The VRAM
        device checks the surface before it gets here."""
        self.scanout_vram = offset

    def scan_ram(self, base: int) -> bool:
        """Show the screen out of RAM at `base`, if the display would take
        it there. Refused, the scanout is left as it was."""
        if not self._valid_base(base):
            log.warning("DISPLAY: refusing scanout base %#x", base)
            return False
        self.scanout_base = base
        self.scanout_vram = None
        return True

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
            # Back to the hardware framebuffer -- but only while a screen
            # fits there. In a bigger mode it runs over the boot sector and
            # into the program, and FILL would write all of it.
            return self.display_size <= DISPLAY_SIZE
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
            return (self.width.to_bytes(4, "little")
                    + self.height.to_bytes(4, "little")
                    + self.display_size.to_bytes(4, "little"))

        if command == CMD_GET_BASE:
            if self.scanout_vram is not None:
                # Where the guest would draw it: the surface in the aperture.
                return ((self.ram.vram_base + self.scanout_vram) & 0xFFFFFFFF
                        ).to_bytes(4, "little")
            return self.scanout_base.to_bytes(4, "little")

        if command == CMD_SET_BASE:
            if self.vram is not None and address >= self.ram.vram_base:
                ok = self.vram.scanout_aperture(address)
            else:
                # Refused, the previous base is kept rather than clamped to
                # DISPLAY_START: a clamp would leave the guest drawing into
                # a buffer nobody is watching while believing it had
                # flipped. Answering 0 lets it fall back to copying.
                ok = self.scan_ram(address)
            return b"\x01\x00\x00\x00" if ok else b"\x00\x00\x00\x00"

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

        if command == CMD_COPY:
            return self._copy(address)

        if command == CMD_NOP:
            return b"\x00\x00\x00\x00"

        log.warning("DISPLAY: unknown command %d", command)
        return b""

    def _copy(self, base: int) -> bytes:
        """COPY: move bytes within the buffer at base, as memmove does.

        Both ranges must lie inside one screen's worth of bytes from a base
        the display would take, so the slices below can neither reach the
        IO header nor run past the end of RAM -- where a slice assignment
        grows the bytearray instead of raising. The right-hand slice is a
        copy, so overlapping ranges come out right either way.
        """
        to, source, count = struct.unpack_from("<III", self.ram.mem, _WINDOW_BASE)
        if (not self._valid_base(base) or to + count > self.display_size
                or source + count > self.display_size):
            log.warning("DISPLAY: refusing copy of %d bytes from +%#x to +%#x at %#x",
                        count, source, to, base)
            return b"\x00\x00\x00\x00"
        mem = self.ram.mem
        mem[base + to:base + to + count] = mem[base + source:base + source + count]
        return b"\x01\x00\x00\x00"

    @property
    def generation(self) -> int:
        """How many times the mode has changed: the VRAM device's count, or
        0 on a machine without video memory, whose mode never does."""
        return self.vram.generation if self.vram is not None else 0

    def update(self) -> bool:
        """Snapshot the screen into the frame /frame serves, with the mode
        it was drawn in. The two are stored together, under one lock, so a
        frame can never be labelled with a mode it was not drawn in -- the
        guest may switch while a request is being served.

        A picture the same as the last is not a new frame, and returns
        False: a byte comparison, about 0.3 ms at 1280 x 720. A different one
        gets the next number and the band of rows that changed, 0.06 to
        0.8 ms to find; a new mode is a new frame, all of it."""
        data = self.snapshot()
        mode = (self.width, self.height, self.generation)
        last = self._frame                 # only this thread ever replaces it
        if last is not None and last[1:] == mode:
            if last[0] == data:
                return False
            first, end = changed_rows(last[0], data, self.width * 4, self.height)
            fresh_mode = False
        else:
            first, end, fresh_mode = 0, self.height - 1, True
        with self._frame_lock:
            self._frame = (data,) + mode
            self._frame_number += 1
            if fresh_mode:
                self._bands.clear()        # a band from another mode means nothing
            self._bands.append((self._frame_number, first, end))
        return True

    def frame_state(self):
        """The frame, its number and the bands of the frames before it, read
        together: ((bytes, w, h, generation), number, [(number, first, last)])."""
        with self._frame_lock:
            frame = self._frame
            if frame is None:
                frame = (bytes(self.display_size), self.width, self.height, self.generation)
            return frame, self._frame_number, list(self._bands)

    def get_frame(self):
        """The latest frame: (bytes as in memory, width, height,
        generation). Black at the current mode until the first update."""
        with self._frame_lock:
            if self._frame is None:
                return bytes(self.display_size), self.width, self.height, self.generation
            return self._frame

    def clear(self) -> None:
        """Blank the frame being served, until the next update: a new frame,
        all of it."""
        with self._frame_lock:
            self._frame = (bytes(self.display_size), self.width, self.height,
                           self.generation)
            self._frame_number += 1
            self._bands.clear()
            self._bands.append((self._frame_number, 0, self.height - 1))

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
            from fastapi import Body, FastAPI, Query, Response
            from fastapi.responses import HTMLResponse, JSONResponse
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
            async def frame(since: Optional[int] = None):
                if since is None:
                    data, headers = frame_reply(self)
                    status = 200
                else:
                    status, data, headers = frame_since(self, since)
                return Response(content=data, status_code=status,
                                media_type="application/octet-stream", headers=headers)

            @app.post("/clear")
            async def clear_endpoint():
                self.clear()
                return {"status": "ok"}

            @app.get("/info")
            async def info():
                return info_reply(self)

            @app.post("/preferred")
            async def preferred(body: dict = Body(...)):
                status, reply = preferred_reply(self, body)
                return JSONResponse(reply, status_code=status)

            @app.get("/serial")
            async def serial(offset: int = Query(0, alias="from")):
                return serial_reply(self.debug_port, offset)

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


# --- the replies, as plain functions -----------------------------------------
#
# The routes above only wrap these, so the tests can call them without a
# server, as tests/test_serial.py does with serial_reply.

#: The byte order /frame's body is in, so a client expecting another can say
#: so instead of showing red and blue swapped.
FRAME_FORMAT = "bgra"


#: How many frames back /frame?since=N can answer with a band of rows. A
#: client further behind than that gets the whole frame.
BANDS = 64


def changed_rows(old: bytes, new: bytes, pitch: int, rows: int):
    """The first and the last row where two frames of one size differ. The
    frames must differ somewhere."""
    first = 0
    while old[first * pitch:(first + 1) * pitch] == new[first * pitch:(first + 1) * pitch]:
        first += 1
    last = rows - 1
    while old[last * pitch:(last + 1) * pitch] == new[last * pitch:(last + 1) * pitch]:
        last -= 1
    return first, last


def frame_reply(display: "DisplayIO"):
    """/frame: the bytes, and the mode they were drawn in as a header,
    X-Pigeon-Mode: w,h,generation, and the frame's number, X-Pigeon-Frame."""
    (data, w, h, generation), number, _ = display.frame_state()
    return data, {"X-Pigeon-Mode": f"{w},{h},{generation}", "X-Pigeon-Frame": str(number)}


def frame_since(display: "DisplayIO", since: int):
    """/frame?since=N, for a client holding frame N (docs/gac/plans/
    phase8_bandwidth.md §2.2): (status, bytes, headers).

    204 and nothing if N is the frame; the rows that changed since N if N is
    one of the last BANDS frames in this mode -- X-Pigeon-Rows: first,last,
    the union of every band after it; otherwise the whole frame, which says
    X-Pigeon-Rows: 0,h-1 all the same."""
    (data, w, h, generation), number, bands = display.frame_state()
    headers = {"X-Pigeon-Mode": f"{w},{h},{generation}", "X-Pigeon-Frame": str(number)}
    if since == number:
        return 204, b"", headers
    after = [band for band in bands if band[0] > since]
    if 0 <= since < number and after and after[0][0] == since + 1:
        first = min(band[1] for band in after)
        last = max(band[2] for band in after)
    else:
        first, last = 0, h - 1                       # too old, another mode, or not ours
    headers["X-Pigeon-Rows"] = f"{first},{last}"
    pitch = w * 4
    return 200, data[first * pitch:(last + 1) * pitch], headers


def info_reply(display: "DisplayIO") -> dict:
    """/info: the live geometry, and what the front ends need to follow it."""
    vram = display.vram
    return {
        "w": display.width, "h": display.height, "size": display.display_size,
        "scanout": display.scanout_base, "hid_url": display.hid_url,
        "cd_url": display.cd_url,
        "generation": display.generation,
        "format": FRAME_FORMAT,
        "modes": [list(m) for m in vram.modes] if vram is not None
                 else [[display.width, display.height]],
        "preferred": list(vram.preferred[:2]) if vram is not None else [0, 0],
    }


def preferred_reply(display: "DisplayIO", body) -> tuple:
    """/preferred {w, h}: what the host window would like. Stored, never
    acted on here -- only the guest switches (docs/gac/decisions.md Q1).
    (status, reply): 404 without video memory, 400 for anything but an
    offered mode."""
    vram = display.vram
    if vram is None:
        return 404, {"error": "this machine has no video memory, so its mode never changes"}
    try:
        mode = (int(body["w"]), int(body["h"]))
    except (KeyError, TypeError, ValueError):
        return 400, {"error": "expected {\"w\": width, \"h\": height}"}
    if mode not in vram.modes:
        return 400, {"error": f"{mode[0]}x{mode[1]} is not an offered mode",
                     "modes": [list(m) for m in vram.modes]}
    vram.set_preferred(*mode)
    return 200, {"preferred": list(mode)}

