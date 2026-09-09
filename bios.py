"""BIOS helper for the pigeon emulator."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from memory_map import BIOS_START, BIOS_MAX


class BIOS:
    def __init__(self, bios_bytes: Optional[bytes] = None):
        self.bios_bytes = bios_bytes or b""

    @classmethod
    def from_file(cls, path: str | Path) -> "BIOS":
        path = Path(path)
        return cls(path.read_bytes())

    def write_bios(self, ram) -> None:
        """Write the BIOS bytes into RAM at address 0x0."""
        if len(self.bios_bytes) > BIOS_MAX:
            raise ValueError(f"BIOS too large ({len(self.bios_bytes)} bytes), max is {BIOS_MAX}")
        ram.load_bytes(self.bios_bytes, start=BIOS_START)
