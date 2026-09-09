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
    """

    def __init__(self, size=RAM_SIZE):
        if size <= 0 or size & (size - 1):
            raise ValueError(f"RAM size must be a positive power of two, got {size}")
        self.size = size
        self.mask = size - 1
        self.mem = bytearray(size)
        self.io_pending = False

    # --- byte access ------------------------------------------------------

    def read_byte(self, addr):
        return self.mem[addr & self.mask]

    def write_byte(self, addr, value):
        addr &= self.mask
        self.mem[addr] = value & 0xFF
        # A guest can select an IO channel with a plain byte write (MW),
        # not just MWW, so this path has to arm the flag too.
        if IO_START <= addr < _IO_END:
            self.io_pending = True

    # --- word access (32-bit, little-endian) ------------------------------

    def read_word(self, addr):
        a = addr & self.mask
        if a + 4 <= self.size:
            return _UNPACK_WORD(self.mem, a)[0]
        # Straddles the top of memory: wrap byte by byte. A slice here
        # would come back short and int.from_bytes would decode it as a
        # smaller number without complaining.
        mem, mask = self.mem, self.mask
        return int.from_bytes(bytes(mem[(a + i) & mask] for i in range(4)), "little")

    def write_word(self, addr, value):
        a = addr & self.mask
        value &= 0xFFFFFFFF
        if a + 4 <= self.size:
            _PACK_WORD(self.mem, a, value)
        else:
            # Wrapping write: a slice assignment here would RESIZE the
            # bytearray rather than wrap, which is the bug this replaced.
            mem, mask = self.mem, self.mask
            for i, b in enumerate(value.to_bytes(4, "little")):
                mem[(a + i) & mask] = b
        if IO_START <= a < _IO_END:
            self.io_pending = True

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
