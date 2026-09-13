"""File-backed HDD module for the pigeon emulator.

This module exposes an HDD device compatible with the `IOController`'s
channel callback interface:

    callback(read_write, command, length, address, data) -> bytes

Parameters provided by the `IOController`:
- `read_write`: 0 for read (controller expects returned bytes), 1 for write
  (controller provides `data` bytes to be written).
- `command`: operation code (see COMMAND_* constants below)
- `length`: number of bytes to read/write
- `address`: device-specific address. For this HDD we treat `address`
  as a byte offset on the disk image file.
- `data`: a `bytearray` of length `length` for write operations, or an
  empty/zeroed bytearray for reads.

Behavior:
- Reads come back SHORT past EOF -- they are not zero-filled, whatever an
  older version of this line said. Both the BIOS loader and fs.c's
  __fs_blk_read find the end of a program that way, by comparing
  IO_RETURN_DATA against what they asked for.
- Writes extend the file if necessary.

Command list:
  CMD_NOP = 0         - No-op, returns `length` zero bytes.
  CMD_GET_SIZE = 1    - Return 8-byte little-endian disk size (in bytes).
  CMD_READ = 2        - Read `length` bytes from disk at file-offset `address`.
  CMD_WRITE = 3       - Write `length` bytes from `data` to disk at `address`.
  CMD_TRUNCATE = 4    - Truncate/resize disk to `address` bytes (length ignored).
  CMD_FLUSH = 5       - Flush OS buffers to disk; returns zero-length bytes.
  CMD_READ_DMA = 6    - Read straight into guest RAM (see DMA below).
  CMD_WRITE_DMA = 7   - Write straight from guest RAM.

DMA (docs/filesystem.md, section 9):
  R/W       0 -- for BOTH commands, and not by accident: IOController
              copies a device's reply back into the window only when R/W
              is 0, and these commands have a reply worth reading.
  ADDRESS   the byte offset on the disk
  LENGTH    8, the size of the parameter block
  window    [ram_address, byte_count], two LE words. The device reads them
            out of RAM itself -- it holds the RAM for the transfer anyway,
            and with R/W = 0 the controller hands it a zeroed buffer.
  reply     one word: bytes transferred, or 0xFFFFFFFF if the range was
            refused. A read past EOF is SHORT, and the rest of the range
            is zero-filled, so the guest's buffer is always fully defined.

  A range must start at or above PROGRAM_LOAD_ADDR -- below it are the
  BIOS, the IO header and the framebuffer -- and end inside RAM. There is
  no 4 KB limit: a contiguous run of any length is one command.

  An HDD built without RAM does not know commands 6 and 7, and answers
  them like any unknown command, with LENGTH zero bytes. That is older
  hardware, and it is what lets the library DETECT DMA: a reply of 4 bytes
  means yes, 8 means no, and one fs.c runs on either.

The module creates `disks/hdd.img` at the repo root if it does not exist.
That is outside build/ on purpose: it is a disk, and what programs save
on it (docs/filesystem.md) has to survive a clean build. A new image is
DEFAULT_SIZE bytes of zeros -- blank, so a program's fs_format() or
`pfs mkfs` formats it without having to be forced.
"""

import logging
import os
import struct
from pathlib import Path
from typing import Optional

from ..memory_map import IO_START, PROGRAM_LOAD_ADDR, IOHeader

log = logging.getLogger(__name__)

# The repo root: devices/ -> emulator/ -> root
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DISK = REPO_ROOT / "disks" / "hdd.img"
# The size of a newly created image. tools/pfs.py gives a new image the
# same size, and tests/test_pfs.py checks that the two agree.
DEFAULT_SIZE = 4 * 1024 * 1024


CMD_NOP = 0
CMD_GET_SIZE = 1
CMD_READ = 2
CMD_WRITE = 3
CMD_TRUNCATE = 4
CMD_FLUSH = 5
CMD_READ_DMA = 6
CMD_WRITE_DMA = 7

DMA_PARAMS = 8                  # [ram_address, byte_count] in the window
DMA_REFUSED = 0xFFFFFFFF
_WINDOW_BASE = IO_START + IOHeader.USABLE_AFTER


class HDD:
    def __init__(self, path: Optional[str] = None, create_size: int = DEFAULT_SIZE,
                 ram=None):
        self.path = Path(path) if path is not None else DEFAULT_DISK
        self.create_size = create_size
        # DMA needs the RAM it copies to and from. Without it commands 6 and
        # 7 are unknown here, exactly as on an HDD from before they existed.
        self.ram = ram
        self._f = None
        self._open()

    def _open(self):
        exists = self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # open in r+b if exists, else create and fill to create_size
        if not exists:
            with open(self.path, "wb") as f:
                f.truncate(self.create_size)
        self._f = open(self.path, "r+b")

    def close(self):
        if self._f:
            try:
                self._f.close()
            finally:
                self._f = None

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    def _read_at(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        self._f.seek(offset)
        data = self._f.read(length)
        # if len(data) < length:
        #     data += b"\x00" * (length - len(data))

        log.debug("HDD: read %d bytes at offset %d, returning %d", length, offset, len(data))
        return data

    def _write_at(self, offset: int, data: bytes):
        if not data:
            return
        self._f.seek(offset)
        self._f.write(data)
        # ensure file length reflects write if we wrote past EOF
        self._f.flush()

    def _dma_range_ok(self, address: int, count: int) -> bool:
        """DisplayIO._valid_base's rule, for a byte range instead of a screen.

        Below PROGRAM_LOAD_ADDR are the BIOS, the IO header and the
        framebuffer; a transfer into the IO header would rewrite the very
        command being run. Past the end of RAM a slice assignment does not
        raise -- it grows the bytearray, and every address mask in the
        machine is wrong from then on.
        """
        return address >= PROGRAM_LOAD_ADDR and address + count <= len(self.ram.mem)

    def _dma(self, cmd: int, offset: int) -> bytes:
        address, count = struct.unpack_from("<II", self.ram.mem, _WINDOW_BASE)
        if not self._dma_range_ok(address, count):
            log.warning("HDD: refusing DMA %s of %d bytes at %#x",
                        "read" if cmd == CMD_READ_DMA else "write", count, address)
            return struct.pack("<I", DMA_REFUSED)

        if cmd == CMD_READ_DMA:
            data = self._read_at(offset, count)
            # EXACTLY count bytes go in. A shorter slice assignment would
            # SHRINK RAM and slide every address above the buffer down.
            self.ram.mem[address:address + count] = data + bytes(count - len(data))
            return struct.pack("<I", len(data))

        self._write_at(offset, bytes(self.ram.mem[address:address + count]))
        return struct.pack("<I", count)

    def _truncate(self, size: int):
        self._f.truncate(size)
        self._f.flush()

    def callback(self, read_write: int, command: int, length: int, address: int, data: bytearray) -> bytes:
        """IOController-compatible callback.

        - `read_write`: 0=read, 1=write
        - `command`: one of CMD_* constants
        - `length`: number of bytes to transfer
        - `address`: byte offset on the disk image (interpreted by this HDD)
        - `data`: bytearray of length `length` containing write payload when
          `read_write==1`, otherwise ignored.
        """
        # normalize
        addr = int(address) & 0xFFFFFFFF
        cmd = int(command)
        length = int(length) if length is not None else 0

        if cmd == CMD_NOP:
            return b"\x00" * length

        if cmd == CMD_GET_SIZE:
            # 8-byte little-endian size
            return int(self.size).to_bytes(8, byteorder="little")

        if cmd == CMD_READ:
            return self._read_at(addr, length)

        if cmd == CMD_WRITE:
            # data holds payload (controller supplies it when read_write==1)
            if not isinstance(data, (bytes, bytearray)):
                raise TypeError("write data must be bytes or bytearray")
            # truncate/expand file as needed by writing
            self._write_at(addr, bytes(data[:length]))
            return b""

        if cmd == CMD_TRUNCATE:
            self._truncate(addr)
            return b""

        if self.ram is not None and cmd in (CMD_READ_DMA, CMD_WRITE_DMA):
            return self._dma(cmd, addr)

        if cmd == CMD_FLUSH:
            self._f.flush()
            os.fsync(self._f.fileno())
            return b""

        # unknown command -> return zeros for reads, ignore for writes
        return b"\x00" * length


if __name__ == "__main__":
    # quick CLI to inspect/modify the image
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, help="print or set size (bytes)")
    ap.add_argument("--read", nargs=2, metavar=("OFFSET", "LENGTH"))
    ap.add_argument("--write", nargs=2, metavar=("OFFSET", "FILE"))
    args = ap.parse_args()
    h = HDD()
    if args.size is not None:
        print(h.size)
    if args.read:
        off = int(args.read[0], 0)
        ln = int(args.read[1], 0)
        print(h._read_at(off, ln))
    if args.write:
        off = int(args.write[0], 0)
        data = Path(args.write[1]).read_bytes()
        h._write_at(off, data)
    h.close()
