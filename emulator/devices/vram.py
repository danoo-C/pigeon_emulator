"""Video memory as a device: the mode, the surfaces in it, and what the
screen shows (docs/gac/phase2_vram.md, design in docs/gac/design.md §5.2).

The bytes are not here. They are RAM's `vram` buffer, which the guest
reaches with ordinary MRW/MWW through the aperture above RAM (ram.py) --
a pixel is one store, never a bus trip. This device is the control side:
it decides how big the screen is, hands out pieces of video memory, and
tells DisplayIO where to read the screen from.

  callback(read_write, command, length, address, data)

  cmd  name         ADDRESS      window in          reply
  0    NOP          --           --                 0
  1    INFO         --           --                 magic, vram_size, aperture, generation
  2    GET_MODE     --           --                 w, h, pitch, format, surface 0's offset
  3    SET_MODE     --           w, h               1 / 0, then w, h, pitch, offset as now
  4    MODE_COUNT   --           --                 how many modes MODE_AT answers for
  5    MODE_AT      index        --                 w, h -- or 0, 0 past the end
  6    PREFERRED    --           --                 w, h, serial: what the host window
                                                    would like; serial counts requests
  7    ALLOC        --           w, h               handle, offset, pitch -- 0s if full
  8    FREE         handle       --                 1 / 0
  9    SCANOUT      handle       --                 1 / 0 -- the page flip
  10   SCANOUT_RAM  RAM address  --                 1 / 0 -- the screen out of RAM
  11   UPLOAD       handle       ram, offset, n     n moved, or 0xFFFFFFFF
  12   DOWNLOAD     handle       ram, offset, n     n moved, or 0xFFFFFFFF

Every reply word is little-endian. Arguments in the window are read with
R/W 0, the way the HDD's DMA commands read theirs, so the reply comes back
in the same window.

INFO answers a magic word first, as the CD drive's MEDIA does: on a machine
without this device the channel answers 0xFFFFFFFF, and a stale window
could say anything, so a program checks the magic before believing the
rest. The aperture base is in INFO because it moves with the machine's RAM
size -- a guest must take it from here, never from a constant.

A surface is a handle, an offset into video memory, a width and a height;
the pitch is always width * 4. Handle 0 is the screen of the current mode.
Allocation is first fit into the gaps between live surfaces, and **a
surface never moves**: a guest may hold a raw aperture pointer into one.
That is also why a mode change puts the new screen wherever it fits rather
than at offset 0 -- another surface may be sitting just past the old one.

The mode starts as the machine's power-on mode and changes only when the
guest asks (Q1, Q6); `generation` counts the changes, so a program can
tell that the screen changed shape under a pointer it holds. A mode
change clears the new screen and points the scanout at it. At 192 x 108 the
machine powers on scanning out of RAM at DISPLAY_START, exactly as it did
before video memory existed; surface 0 exists all the same, ready to be
flipped to.

Only ever called from the emulator thread, like every device callback,
except `set_preferred`, which the display server calls (Phase 4) and which
only replaces one tuple.
"""
import logging
import struct
import weakref
from typing import Dict, List, Optional, Sequence, Tuple

from ..memory_map import DISPLAY_START, DISPLAY_W, DISPLAY_H, IO_START, IOHeader, PROGRAM_LOAD_ADDR

log = logging.getLogger(__name__)

CMD_NOP = 0
CMD_INFO = 1
CMD_GET_MODE = 2
CMD_SET_MODE = 3
CMD_MODE_COUNT = 4
CMD_MODE_AT = 5
CMD_PREFERRED = 6
CMD_ALLOC = 7
CMD_FREE = 8
CMD_SCANOUT = 9
CMD_SCANOUT_RAM = 10
CMD_UPLOAD = 11
CMD_DOWNLOAD = 12

#: "PGVR" in byte order, the convention of the CD drive's MEDIA_MAGIC.
VRAM_MAGIC = 0x52564750
#: Bytes in memory are B, G, R, A: the word 0xAARRGGBB, little-endian.
FORMAT_BGRA = 1
#: UPLOAD/DOWNLOAD's answer when it moved nothing.
DMA_REFUSED = 0xFFFFFFFF

_WINDOW_BASE = IO_START + IOHeader.USABLE_AFTER
_OK, _NO = struct.pack("<I", 1), struct.pack("<I", 0)


class Surface:
    __slots__ = ("offset", "width", "height")

    def __init__(self, offset: int, width: int, height: int):
        self.offset, self.width, self.height = offset, width, height

    @property
    def size(self) -> int:
        return self.width * self.height * 4

    @property
    def end(self) -> int:
        return self.offset + self.size


class VRAM:
    def __init__(self, ram, display, modes: Sequence[Tuple[int, int]],
                 mode: Tuple[int, int] = (DISPLAY_W, DISPLAY_H)):
        if not ram.vram_size:
            raise ValueError("a VRAM device needs a RAM with video memory")
        self.ram = ram
        self.display = display
        # Weak: the two point at each other, and a cycle would keep a
        # machine's RAM alive until the cycle collector happened by -- in
        # the tests, hundreds of megabytes of it.
        display.vram = weakref.proxy(self)
        self.modes: List[Tuple[int, int]] = [tuple(m) for m in modes]
        if tuple(mode) not in self.modes:
            raise ValueError(f"the power-on mode {mode} is not one of {self.modes}")
        self.surfaces: Dict[int, Surface] = {}
        self._next_handle = 1
        self.generation = 0
        self.preferred = (0, 0, 0)            # w, h, serial
        self.mode = tuple(mode)
        screen = self._place(*self.mode)
        if screen is None:
            raise ValueError(f"the power-on mode {mode} does not fit in "
                             f"{ram.vram_size} bytes of video memory")
        self.surfaces[0] = screen
        display.set_mode(*self.mode)
        if self.mode == (DISPLAY_W, DISPLAY_H):
            display.scan_ram(DISPLAY_START)   # the machine from before VRAM
        else:
            display.scan_vram(screen.offset)

    # --- surfaces -----------------------------------------------------------

    def _place(self, width: int, height: int) -> Optional[Surface]:
        """First fit into the gaps between live surfaces; None if full."""
        if width <= 0 or height <= 0:
            return None
        size = width * height * 4
        at = 0
        for s in sorted(self.surfaces.values(), key=lambda s: s.offset):
            if s.offset - at >= size:
                break
            at = max(at, s.end)
        if at + size > self.ram.vram_size:
            return None
        return Surface(at, width, height)

    def alloc(self, width: int, height: int) -> Optional[Tuple[int, Surface]]:
        surface = self._place(width, height)
        if surface is None:
            return None
        handle = self._next_handle
        # 0 is the screen and 0xFFFFFFFF means "system RAM" to the GAC
        # (design.md §5.3); neither is ever handed out.
        self._next_handle = handle + 1 if handle + 1 < 0xFFFFFFFF else 1
        self.surfaces[handle] = surface
        return handle, surface

    def free(self, handle: int) -> bool:
        if handle == 0 or handle not in self.surfaces:
            return False
        surface = self.surfaces.pop(handle)
        if self.display.scanout_vram == surface.offset:
            # The screen was showing it: the flip goes back to the screen
            # rather than on showing memory the next ALLOC will hand out.
            self.display.scan_vram(self.surfaces[0].offset)
        return True

    # --- the mode ------------------------------------------------------------

    def set_mode(self, width: int, height: int) -> bool:
        """Switch, if the machine offers the mode and it fits. Refused,
        nothing changes -- not even the screen's place in video memory."""
        if (width, height) not in self.modes:
            return False
        old = self.surfaces.pop(0)
        screen = self._place(width, height)
        if screen is None:
            self.surfaces[0] = old
            return False
        self.surfaces[0] = screen
        self.ram.vram[screen.offset:screen.end] = bytes(screen.size)
        self.mode = (width, height)
        self.generation = (self.generation + 1) & 0xFFFFFFFF
        self.display.set_mode(width, height)
        self.display.scan_vram(screen.offset)
        return True

    def set_preferred(self, width: int, height: int) -> None:
        """What the host window would like (Phase 4). Stored, never acted
        on here: only the guest switches (Q1)."""
        self.preferred = (width, height, (self.preferred[2] + 1) & 0xFFFFFFFF)

    # --- the scanout ----------------------------------------------------------

    def scanout(self, handle: int) -> bool:
        """The page flip. The surface must be the screen's shape."""
        surface = self.surfaces.get(handle)
        if surface is None or (surface.width, surface.height) != self.mode:
            return False
        self.display.scan_vram(surface.offset)
        return True

    def scanout_aperture(self, address: int) -> bool:
        """CH_DISPLAY's SET_BASE with an aperture address: what its GET_BASE
        answers while a surface is on screen, so the two round-trip."""
        offset = address - self.ram.vram_base
        for handle, surface in self.surfaces.items():
            if surface.offset == offset:
                return self.scanout(handle)
        return False

    # --- DMA --------------------------------------------------------------------

    def _dma(self, command: int, handle: int) -> int:
        ram_addr, offset, count = struct.unpack_from("<III", self.ram.mem, _WINDOW_BASE)
        surface = self.surfaces.get(handle)
        if surface is None or offset + count > surface.size:
            return DMA_REFUSED
        # RAM must be wholly RAM: past its end a slice assignment grows the
        # bytearray (ram.py). Reading may come from anywhere below that --
        # the old framebuffer at DISPLAY_START included; writing obeys the
        # HDD's rule and stays above the program's load address.
        if ram_addr + count > self.ram.size:
            return DMA_REFUSED
        start = surface.offset + offset
        if command == CMD_UPLOAD:
            self.ram.vram[start:start + count] = self.ram.mem[ram_addr:ram_addr + count]
            self.ram.vram_dirty = True
        else:
            if ram_addr < PROGRAM_LOAD_ADDR:
                return DMA_REFUSED
            self.ram.mem[ram_addr:ram_addr + count] = self.ram.vram[start:start + count]
        return count

    # --- the IO bus device --------------------------------------------------------

    def _window(self, words: int) -> Tuple[int, ...]:
        return struct.unpack_from(f"<{words}I", self.ram.mem, _WINDOW_BASE)

    def _mode_reply(self) -> bytes:
        w, h = self.mode
        return struct.pack("<IIII", w, h, w * 4, self.surfaces[0].offset)

    def callback(self, read_write: int, command: int, length: int, address: int,
                 data: bytearray) -> bytes:
        """IOController-compatible callback. See the module docstring."""
        if command == CMD_NOP:
            return _NO
        if command == CMD_INFO:
            return struct.pack("<IIII", VRAM_MAGIC, self.ram.vram_size,
                               self.ram.vram_base & 0xFFFFFFFF, self.generation)
        if command == CMD_GET_MODE:
            w, h = self.mode
            return struct.pack("<IIIII", w, h, w * 4, FORMAT_BGRA, self.surfaces[0].offset)
        if command == CMD_SET_MODE:
            ok = self.set_mode(*self._window(2))
            return (_OK if ok else _NO) + self._mode_reply()
        if command == CMD_MODE_COUNT:
            return struct.pack("<I", len(self.modes))
        if command == CMD_MODE_AT:
            w, h = self.modes[address] if address < len(self.modes) else (0, 0)
            return struct.pack("<II", w, h)
        if command == CMD_PREFERRED:
            return struct.pack("<III", *self.preferred)
        if command == CMD_ALLOC:
            got = self.alloc(*self._window(2))
            if got is None:
                return struct.pack("<III", 0, 0, 0)
            handle, surface = got
            return struct.pack("<III", handle, surface.offset, surface.width * 4)
        if command == CMD_FREE:
            return _OK if self.free(address) else _NO
        if command == CMD_SCANOUT:
            return _OK if self.scanout(address) else _NO
        if command == CMD_SCANOUT_RAM:
            return _OK if self.display.scan_ram(address) else _NO
        if command in (CMD_UPLOAD, CMD_DOWNLOAD):
            return struct.pack("<I", self._dma(command, address))
        log.warning("VRAM: unknown command %d", command)
        return b""
