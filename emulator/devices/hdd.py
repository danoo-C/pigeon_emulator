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
- Reads return exactly `length` bytes (zero-filled if past EOF).
- Writes extend the file if necessary.

Command list:
  CMD_NOP = 0         - No-op, returns `length` zero bytes.
  CMD_GET_SIZE = 1    - Return 8-byte little-endian disk size (in bytes).
  CMD_READ = 2        - Read `length` bytes from disk at file-offset `address`.
  CMD_WRITE = 3       - Write `length` bytes from `data` to disk at `address`.
  CMD_TRUNCATE = 4    - Truncate/resize disk to `address` bytes (length ignored).
  CMD_FLUSH = 5       - Flush OS buffers to disk; returns zero-length bytes.

The module creates `build/pigeon_hard_drive.bin` at the repo root if it
does not exist. Defaults to a small initial size (1 MiB) when creating a new image.
"""

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# The repo root: devices/ -> emulator/ -> root
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DISK = REPO_ROOT / "build" / "pigeon_hard_drive.bin"


CMD_NOP = 0
CMD_GET_SIZE = 1
CMD_READ = 2
CMD_WRITE = 3
CMD_TRUNCATE = 4
CMD_FLUSH = 5


class HDD:
    def __init__(self, path: Optional[str] = None, create_size: int = 1024 * 1024):
        self.path = Path(path) if path is not None else DEFAULT_DISK
        self.create_size = create_size
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
