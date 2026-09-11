# Pigeon Emulator

A from-scratch virtual machine: a custom 32-bit CPU with its own instruction
set, a 128 MB flat address space, a DMA-capable IO bus with pluggable devices
(disk, timer, mouse/keyboard), memory-mapped video, a two-pass assembler, and a
browser/pygame front-end that renders the framebuffer over HTTP.

Everything is written by hand — the ISA, the encoder, the assembler, the boot
ROM, the device protocol. Nothing emulates existing hardware; this is an
invented machine.

```
   user/*.asm ──assembler──▶ build/*.bin ──▶ boot disk (channel 1)
                                                  │
 firmware/bios.asm ──▶ build/bios.bin ──▶ RAM @ 0x0
                                │                 │ BIOS DMAs the program in
                                └──▶ CPU ──▶ RAM @ 0x20000 ──▶ JMP
                                         │
                                    IO bus @ 0x400
                                   ┌─────┴──────┬────────┬────────┐
                                USERPROG      HDD      HID     TIMER
                                  (1)         (2)      (3)      (4)

     RAM @ 0x1418 (framebuffer) ──▶ DisplayIO ──▶ :8000/frame ──▶ browser / pygame
                            HID ◀── :8001/key,/mouse_* ◀─────────┘
```

---

## Quick start

The emulator core needs no third-party packages — only the display and input
servers do. On most modern distros the system Python is
[PEP 668](https://peps.python.org/pep-0668/)-managed and `pip install` into it
fails with `externally-managed-environment`, so use a virtualenv:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # fastapi, uvicorn, pydantic
.venv/bin/pip install -r requirements-client.txt   # pygame, requests

.venv/bin/python start_emulator.py
```

That lists what's in `user/` and lets you pick:

```
Programs in user:

   1.   checkerboard   user/checkerboard.asm      not built
   2.   demo           user/demo.c                not built
   3. * screen         user/screen.asm            built
   4.   sincos         user/sincos.asm            not built
   5.   ui             user/ui.asm                not built

  * built   ~ source is newer than the build   + binary only

Program (number or name, Enter to skip, q to quit): 2
Compiling demo.c with display.c, input.c, mem.c
Assembled build/demo.asm -> build/demo.bin (31180 bytes, 3897 instructions)
Program: demo  (build/demo.bin)
```

**Nothing is built by hand.** Assembly and C are treated the same: pick a
program and anything not built — or whose source is newer than its build —
is rebuilt first. A `.c` program's libraries are worked out from its
`#include <pigeon/…>` lines, so you never name them yourself. Editing a
library marks every program that uses it stale. Same for the BIOS.

You can also name a program up front, by name, number, or path:

```bash
.venv/bin/python start_emulator.py screen --run
.venv/bin/python start_emulator.py 2
.venv/bin/python start_emulator.py user/sincos.asm
.venv/bin/python start_emulator.py --list        # just show the table
```

### Seeing the screen

Two front-ends, and **the browser one needs no extra packages beyond the
emulator's own**:

```bash
# 1. browser -- just open it, no pygame required
xdg-open http://127.0.0.1:8000

# 2. pygame client -- display *and* mouse/keyboard input
.venv/bin/python display/display.py --fps 30
```

The pygame client is a separate process that talks to the emulator only over
HTTP, so it needs its own dependencies (`requirements-client.txt`) and a
working display. On WSL2 that means WSLg — check `echo $DISPLAY` prints
something like `:0`. Harmless ALSA warnings on startup are pygame looking for a
sound card it doesn't need.

### Running it

Without `--run` you get a menu (run / single-step debugger / dump RAM / CPU
state). `--help` lists everything; the useful ones are `--verbose` (log every IO
transaction, disk read and key event), `--headless` (bind no ports) and
`--disasm-bios`.

### config.json

Settings live in `config.json` at the repo root. Command-line flags override it
for a single run; anything you leave out falls back to a built-in default, so a
partial or missing file is fine.

```json
{
  "host": "127.0.0.1",
  "display_port": 8000,
  "hid_port": 8001,

  "program_dirs": ["user"],

  "build_dir": "build",
  "disk": "build/pigeon_hard_drive.bin",

  "bios_source": "firmware/bios.asm",
  "bios_binary": "build/bios.bin",
  "auto_build": true
}
```

| Key | Meaning |
|---|---|
| `host`, `display_port`, `hid_port` | where the two HTTP servers listen. **The pygame client reads these too**, so changing a port here moves both ends. |
| `program_dirs` | folders the launcher scans. Add your own; they're all listed together. |
| `build_dir` | where assembled output and RAM dumps go |
| `disk` | image for IO channel 2 |
| `bios_source` / `bios_binary` | the BIOS and where it builds to |
| `auto_build` | reassemble stale sources automatically (`--no-autobuild` to skip) |

Relative paths are resolved against the repo root, so the file means the same
thing whichever directory you run from. A malformed value is reported with the
key that caused it rather than surfacing later as a stack trace, and an
unrecognised key is a warning, not an error.

> Everything below can be run with plain `python3` instead of `.venv/bin/python`
> — the assembler, the tests, the tools and `--headless` have no third-party
> dependencies at all.

### Demo programs

| | |
|---|---|
| `user/demo.c` | ⭐ **start here** — a menu, a textbox and a canvas, in C |
| `user/cube.c` | a draggable 3D wireframe cube, built on `<pigeon/math.h>` |
| `user/screen.asm` | ✅ runs to `HALT`, fills the screen with a bitwise pattern |
| `user/sincos.asm` | animated plot; loops forever by design |
| `user/ui.asm` | two alternating draw routines; loops forever by design |
| `user/checkerboard.asm` | ⚠️ overruns the framebuffer and eventually overwrites itself — a pre-existing program bug, see [REFACTORING.md §6.3](REFACTORING.md#63-userycheckerboardasm-overruns-the-framebuffer-and-overwrites-itself) |

---

## Layout

```
config.json           host, ports, program folders, paths
start_emulator.py     launcher
emulator/             the machine (importable, no side effects on import)
  cli.py              argparse, program picker, menu, single-step debugger
  config.py           defaults + config.json + flag precedence
  programs.py         finds programs in program_dirs, builds them on demand
  machine.py          Machine: wiring, devices, the run loop
  cpu.py              fetch/decode/execute, PC, SP, flags
  registers.py        the register file, A-F
  ram.py              flat address space, byte/word access, IO write detection
  memory_map.py       single source of truth for every address
  instruction_set.py  the ISA: one decorated handler per opcode
  io_controller.py    channel-based DMA bus
  bios.py             loads bios.bin into RAM at 0x0
  devices/            hdd.py  timer.py  hid.py  display_io.py
assembler/            assembler.py + README.md
firmware/bios.asm     boot ROM source (loads programs in 4 KB chunks)
user/                 example programs (.asm and .c alike)
lib/pigeon/           the C libraries: mem, string, display, input, math
compiler/             pigeon-cc: C -> assembly
display/              pygame client + browser front-end (talks HTTP only)
tools/                disasm.py, bench.py
tests/                test_golden.py, test_smoke.py, test_config.py, golden/
compiler/design/      design for a C compiler + stdlib/display/input libraries
docs/ideas/           older sketches toward a C compiler; neither builds
build/                assembled output + disk image (gitignored)
```

### Boot sequence

1. `bios.py` copies `build/bios.bin` into RAM at `0x0`; the CPU starts at PC = 0.
2. The BIOS programs the IO header and selects **channel 1**, the disk holding
   the user program.
3. The controller DMAs it into the IO data window; the BIOS copies it word by
   word to `0x20000`, painting each word into the framebuffer as it goes — a
   boot progress bar made of program bytes.
4. It waits 2 s on **channel 4**, clears the screen, and jumps to `0x20000`.

---

## Memory map

128 MB flat, defined in `emulator/memory_map.py`. Regions after the BIOS are
*computed* from the previous region's end, so resizing one cascades instead of
silently overlapping.

| Range | Size | Region |
|---|---|---|
| `0x00000000`–`0x000003FF` | 1 KB | BIOS — CPU boots here |
| `0x00000400`–`0x00001417` | 4 KB + 24 B | IO controller (header + data window) |
| `0x00001418`–`0x00015817` | 81 KB | Display framebuffer (192×108 × 4 B, 16:9) |
| `0x00020000`–`0x0011FFFF` | 1 MB | User program (fixed load point) |
| `0x00120000` → | | Heap, grows **up** |
| ← `0x07FFFFFC` | | Stack, grows **down** |

Heap and stack share one uninterrupted block and grow toward each other, so
neither reserves a guess up front.

Assembly sources never retype these — the assembler predefines every constant
in `memory_map.py` as a symbol. See [assembler/README.md](assembler/README.md).

---

## Instruction set

Every instruction is exactly **8 bytes**:

```
byte:  0        1      2       3       4  5  6  7
     [opcode] [dst] [src1] [src2] [  imm (LE u32)  ]
```

An unused operand slot holds `0xFF` (`NONE_REG`). Most instructions accept
*either* a register *or* an immediate in their last slot.

| Op | | Op | | Op | | Op | |
|---|---|---|---|---|---|---|---|
| 0 | `NOP` | 8 | `XOR` | 16 | `JZ` | 24 | `POP` |
| 1 | `MOV` | 9 | `NOT` | 17 | `JNZ` | 25 | `CALL` |
| 2 | `ADD` | 10 | `JMP` | 18 | `JL` | 26 | `RET` |
| 3 | `SUB` | 11 | `MR` | 19 | `JG` | 27 | `SHL` |
| 4 | `MUL` | 12 | `MW` | 20 | `JLE` | 28 | `SHR` |
| 5 | `DIV` | 13 | `MRW` | 21 | `JGE` | | |
| 6 | `OR` | 14 | `MWW` | 22 | `HALT` | | |
| 7 | `AND` | 15 | `CMP` | 23 | `PUSH` | | |

- `MR`/`MW` move **one byte**; `MRW`/`MWW` a **32-bit word**.
- `MW`/`MWW` take their *address* from a register: `MWW D, A` writes A to the
  address in D.
- `CMP` sets `zero_flag` (equal) and `less_flag` (less-than, **unsigned** — it's
  a borrow, not a sign). Pair it with a `J**`.
- Registers are unsigned 32-bit and wrap; `SUB` below zero yields a large value.
- `CALL` pushes the return address, `RET` pops it. Keep the stack balanced.

Adding an instruction is one decorated function in `instruction_set.py`:

```python
@instruction("ROL")
def op_rol(cpu, dst, src1, src2, imm):
    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm) & 31
    cpu.reg.write(dst, ((a << b) | (a >> (32 - b))) & 0xFFFFFFFF)
```

> ⚠️ Opcodes are assigned in declaration order. **Appending is safe; inserting
> renumbers every later opcode and invalidates every existing `.bin`.**
> `tests/test_golden.py` catches it if you do.

---

## IO bus

Write a header at `IO_START` (`0x400`), then write a non-zero channel number —
that fires the command.

| Offset | Field | Meaning |
|---|---|---|
| 0 | `IO_CHANNEL` | Device to select. **Writing this fires the command.** Cleared when done. |
| 4 | `IO_R_W` | 0 = read, 1 = write |
| 8 | `IO_COMMAND` | Device-specific opcode |
| 12 | `IO_LENGTH` | Bytes to transfer (for TIMER: duration in ms) |
| 16 | `IO_ADDRESS` | Device-specific address (disk offset, timer id) |
| 20 | `IO_RETURN_DATA` | Bytes the device returned |
| 24+ | data window | 4 KB payload |

| Channel | Device | Commands |
|---|---|---|
| 1 `CH_USERPROG` | boot disk | as HDD |
| 2 `CH_HDD` | disk | `0` NOP `1` GET_SIZE `2` READ `3` WRITE `4` TRUNCATE `5` FLUSH |
| 3 `CH_HID` | input | **real-time:** `1` mouse pos (x≪16\|y) `2` button mask `6` one key's state `7` 32-byte held-key bitmap · **FIFO:** `3` pop character `4` pop mouse edge `5` pop key edge |
| 4 `CH_TIMER` | timers | `1` START `2` STOP `4` RESET `5` STATUS → `(status, remaining_ms)` |
| 5 `CH_DISPLAY` | framebuffer | `1` INFO → `(w, h, size)` `2` SET_BASE (page flip, ADDRESS = the buffer to scan out) `3` GET_BASE `4` FILL (ADDRESS = destination, colour in the data window) |

Input comes in **two buffers**, because guest code asks two different questions.
The FIFOs answer *"what happened, in order"* — a key pressed and released
between two polls still registers, which is what typing needs. The real-time
state answers *"what is true right now"* — which is what hold-to-move needs, and
what replaying edges cannot give you. Use both: drain the FIFOs each frame, then
sample the state.

Keycodes are a single byte: printable ASCII passes through, named keys take
`0x80`–`0x9F`. Front ends translate into that space (`emulator/devices/keycodes.py`
defines it); the device rejects anything wider rather than truncating it.

Selecting a channel with no device on it writes `0xFFFFFFFF` to
`IO_RETURN_DATA` and keeps running, rather than taking the machine down.

---

## Development

```bash
python3 tests/test_golden.py      # assembler output is byte-exact
python3 tests/test_smoke.py       # CPU behaviour + bug regressions
python3 tests/test_config.py      # config.json + program discovery
python3 tests/test_input.py       # HID: both buffers, keycode translation
python3 tests/test_directives.py  # data directives + the anti-drift guard
python3 tests/test_loader.py      # programs larger than one DMA window
python3 -m pytest tests/          # all 149, if you have pytest

python3 tools/bench.py            # interpreter throughput
python3 tools/disasm.py build/bios.bin
python3 tools/disasm.py build/check.bin --org 0x20000 --check
```

The suite runs without pytest — `tests/_runner.py` provides a minimal runner,
since this project's environment is PEP 668-managed.

`tests/golden/` holds three binaries assembled by the *original* toolchain.
They are the regression gate: if the assembler's output ever changes, read the
diff before re-blessing a fixture.

---

## Status

An old project, revisited and overhauled. It boots, runs programs, draws to the
screen, and now has tests. The interpreter runs about **2.5M instructions per
second**.

The full audit — every bug with its reproduction, the measurements, and what is
still open — is in **[REFACTORING.md](REFACTORING.md)**.

A design for a C compiler targeting this machine, with a memory-operations
standard library and display/input libraries, is in
**[compiler/design/](compiler/design/README.md)**. Not implemented — but the
calling convention, the signed-comparison and modulo lowerings, the framebuffer
colour format and the HID read sequence were each assembled and run against the
emulator before being written down.
