#!/usr/bin/env python3
"""Disassemble a pigeon binary.

    python3 tools/disasm.py build/bios.bin
    python3 tools/disasm.py build/check.bin --org 0x20000 --check

Replaces the old test.py, which had its own third copy of the decode-and-
format logic (main.py had the other two).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emulator.instruction_set import (
    INSTR_SIZE, INSTRUCTIONS_BY_OPCODE, NONE_REG, disassemble)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("binary", type=Path)
    parser.add_argument("--org", type=lambda v: int(v, 0), default=0,
                        help="address the binary is loaded at (default 0)")
    parser.add_argument("--check", action="store_true",
                        help="only report instructions with an invalid opcode")
    args = parser.parse_args(argv)

    data = args.binary.read_bytes()
    if len(data) % INSTR_SIZE:
        print(f"warning: {len(data)} bytes is not a multiple of {INSTR_SIZE}",
              file=sys.stderr)

    bad = 0
    for offset in range(0, len(data) - INSTR_SIZE + 1, INSTR_SIZE):
        chunk = data[offset:offset + INSTR_SIZE]
        invalid = chunk[0] not in INSTRUCTIONS_BY_OPCODE
        bad += invalid
        if invalid or not args.check:
            line = disassemble(chunk, args.org + offset)
            if invalid:
                note = "  <-- 0xFF is NONE_REG, not an opcode" if chunk[0] == NONE_REG else "  <--"
                print(f"{line}{note}   raw: {chunk.hex(' ')}")
            else:
                print(line)

    total = len(data) // INSTR_SIZE
    print(f"\n{total} instructions, {bad} invalid", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
