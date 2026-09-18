# ram.py
import struct

from .memory_map import (
    RAM_SIZE, IO_START, IO_SIZE, DISPLAY_START, DISPLAY_SIZE,
)

_IO_END = IO_START + IO_SIZE

# struct beats int.from_bytes / to_bytes on a bytearray by roughly 2x for
# a 32-bit access, and MRW/MWW are on the hot path. Measured on this
# machine: 4.8M -> 9.0M reads/s, 4.8M -> 11.2M writes/s.
_UNPACK_WORD = struct.Struct("<I").unpack_from
_PACK_WORD = struct.Struct("<I").pack_into


class RAM:
    """The flat address space, backed by one bytearray.

    Addresses wrap: `size` must be a power of two so `addr & self.mask`
    is a correct modulo. Word accesses that straddle the top of memory
    wrap byte by byte -- an earlier version let a slice assignment there
    silently *grow* the bytearray past `size`.

    Writes landing in the IO window set `io_pending`. The main loop
    checks that flag instead of polling the IO controller after every
    instruction; IOController.update() clears it once it has finished
    its own writes.

    Video memory (docs/gac/phase1_aperture.md). Given `vram_size`, the
    address space doubles: RAM keeps the bottom half exactly as it was,
    and the top half, from `vram_base` -- wherever this RAM ends -- is an
    aperture onto a separate `vram` buffer, so a pixel is still one MWW.
    Past the end of video memory the aperture reads 0 and drops writes.
    Writes into it set `vram_dirty` rather than `io_pending`. Without
    video memory, `vram_base` is `size`, which `addr & mask` never
    reaches, so this is the RAM from before the aperture, byte for byte.

    The aperture base moves when RAM does, which is why it is an
    attribute and not a constant in memory_map: a guest learns it from
    the VRAM device, never from a number baked into its build.
    """

    def __init__(self, size=RAM_SIZE, vram_size=0):
        if size <= 0 or size & (size - 1):
            raise ValueError(f"RAM size must be a positive power of two, got {size}")
        if not 0 <= vram_size <= size:
            raise ValueError(f"video memory must fit in the aperture, which is as "
                             f"big as RAM ({size} bytes); got {vram_size}")
        if vram_size and size > 1 << 31:
            raise ValueError("with video memory, RAM can be at most 2 GB: the "
                             "aperture above it has to fit in 32 bits")
        self.size = size
        self.mem = bytearray(size)
        self.vram = bytearray(vram_size)
        self.vram_size = vram_size
        self.vram_base = size
        self.mask = 2 * size - 1 if vram_size else size - 1
        self.io_pending = False
        self.vram_dirty = False

    # --- byte access ------------------------------------------------------
    #
    # RAM takes exactly the path it took before the aperture: an address
    # in video memory is past the end of `mem`, so the bytearray raises
    # and only then does the aperture get a look. A try costs nothing
    # until it catches (Python 3.11+), where a compare in front measured
    # ~12 ns on every byte access -- 20% of a read_byte. An aperture BYTE
    # pays for the exception instead (~220 ns); pixels are words, and
    # memcpy moves them a word at a time.

    def read_byte(self, addr):
        a = addr & self.mask
        try:
            return self.mem[a]
        except IndexError:
            a -= self.vram_base
            return self.vram[a] if a < self.vram_size else 0

    def write_byte(self, addr, value):
        a = addr & self.mask
        try:
            self.mem[a] = value & 0xFF
        except IndexError:
            a -= self.vram_base
            if a < self.vram_size:
                self.vram[a] = value & 0xFF
                self.vram_dirty = True
            return
        # A guest can select an IO channel with a plain byte write (MW),
        # not just MWW, so this path has to arm the flag too.
        if IO_START <= a < _IO_END:
            self.io_pending = True

    # --- word access (32-bit, little-endian) ------------------------------
    #
    # The bounds check RAM always had is the one that sends a word to the
    # aperture: failing it now means "not wholly in RAM", and only then is
    # video memory tried. So a RAM word costs what it did.

    def read_word(self, addr):
        a = addr & self.mask
        if a + 4 <= self.size:
            return _UNPACK_WORD(self.mem, a)[0]
        off = a - self.vram_base
        if 0 <= off and off + 4 <= self.vram_size:
            return _UNPACK_WORD(self.vram, off)[0]
        # Not wholly inside one buffer: it straddles the top of RAM or of
        # the address space, or runs past the end of video memory. Byte
        # by byte, so each byte finds its own buffer and wraps on its own.
        # A slice here would come back short and int.from_bytes would
        # decode it as a smaller number without complaining.
        rb = self.read_byte
        return rb(a) | rb(a + 1) << 8 | rb(a + 2) << 16 | rb(a + 3) << 24

    def write_word(self, addr, value):
        a = addr & self.mask
        value &= 0xFFFFFFFF
        if a + 4 <= self.size:
            _PACK_WORD(self.mem, a, value)
            if IO_START <= a < _IO_END:
                self.io_pending = True
            return
        off = a - self.vram_base
        if 0 <= off and off + 4 <= self.vram_size:
            _PACK_WORD(self.vram, off, value)
            self.vram_dirty = True
            return
        # Byte by byte, as in read_word. A slice assignment here would
        # RESIZE the bytearray rather than wrap, which is the bug this
        # replaced.
        wb = self.write_byte
        for i in range(4):
            wb(a + i, value >> (8 * i))

    # --- bulk ------------------------------------------------------------

    def load_bytes(self, data, start=0):
        end = start + len(data)
        if end > self.size:
            raise ValueError("Data too large to fit in RAM at that address")
        self.mem[start:end] = data

    def display_slice(self):
        return self.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE]

    def dump_ram(self, start=0, end=None):
        """A view, not a copy -- the full space is 128 MB. file.write()
        accepts a memoryview directly."""
        if end is None:
            end = self.size
        return memoryview(self.mem)[start:end]
