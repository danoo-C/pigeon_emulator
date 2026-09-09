#!/usr/bin/env python3
"""Launcher for the pigeon emulator.

    python start_emulator.py --program build/check.bin

Assembles firmware/bios.asm into build/bios.bin when that is missing or
stale, then hands off to emulator.cli. Run --help for the full options.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from emulator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
