# Pigeon Emulator — Audit and Refactor Record

The original audit found, by running code rather than reading it, 7 crash bugs,
~2× of untapped interpreter throughput, four near-duplicate assemblers of which
one worked, and ~5 MB of accidentally-zeroed files committed to git.

This document is now the record of that work: every finding, what was done
about it, and the measurements. Findings are marked **✅ Fixed**, **📝 Recorded**
(a real bug, deliberately not fixed — with the reason), or **⏭️ Deferred**.

**Headline results**

| | Before | After |
|---|---|---|
| Interpreter throughput | 1,220,000 IPS | **2,526,000 IPS** (2.07×) |
| Frame conversion | 2.22 ms | **0.017 ms** (130×) |
| Assembler files | 4 (1,711 lines, 1 working) | **1** |
| Assembler docs | 4 files, 1,146 lines, wrong | **1**, accurate |
| Zeroed junk in git | ~5 MB | **0** |
| Tests | none | **25**, all passing |
| BIOS / screen / check binaries | — | **byte-identical** to pre-refactor |

The last row is the important one: the entire restructure, the assembler
rewrite, and four new opcodes all landed without changing a single byte of
assembled output.

**Contents** — [1 Bugs](#1-correctness-bugs) · [2 Performance](#2-performance) ·
[3 Duplication](#3-duplication--dead-code) · [4 Hygiene](#4-repository-hygiene) ·
[5 Design](#5-design-improvements) · [6 Found during the refactor](#6-found-during-the-refactor) ·
[7 Still open](#7-still-open)

---

## 1. Correctness bugs

Each has a regression test in `tests/test_smoke.py`, named `test_bug_*`.

### 1.1 ✅ `RAM.write_word` near the top of memory *grew* the address space

Slice assignment on a `bytearray` with a mismatched length **resizes it**, so a
word write in the last 3 bytes silently made RAM bigger than `RAM_SIZE` — and
`STACK_TOP = RAM_SIZE - 4` puts the stack exactly there.

```python
>>> r = RAM(1024); r.write_word(1022, 0xDEADBEEF); len(r.mem)
1026            # ← RAM grew
```

`read_word` had the mirror problem: a short slice decoded as a "word" with no
error. **Fixed** in `emulator/ram.py` — word accesses that straddle the top now
wrap byte by byte, matching the semantics `read_byte`/`write_byte` always had.
`RAM.__init__` also rejects a non-power-of-two size, which `self.mask` silently
depended on.

### 1.2 ✅ A device returning `None` crashed the IO controller

`len(io_data)` was called unconditionally, while the timer returned `None` for
`CMD_NOP` *and* for every unrecognized command:

```python
>>> ram.write_word(IO_START + IOHeader.COMMAND, 0)   # CMD_NOP on the timer
>>> io.update()
TypeError: object of type 'NoneType' has no len()
```

**Fixed** at both ends: `emulator/io_controller.py` normalizes `None` to `b""`,
and `emulator/devices/timer.py` now returns bytes for every command, matching
`hdd.py` and `hid.py`.

### 1.3 ✅ An unregistered IO channel raised a bare `KeyError`

A guest typo became an unhandled Python traceback with no PC and no context.
**Fixed** — the controller writes `ERR_NO_SUCH_CHANNEL` to `RETURN_DATA`, logs a
warning, clears the channel and continues. Real hardware doesn't halt because
you addressed an empty slot.

### 1.4 ✅ "Leave blank to skip" didn't skip — it crashed

The prompt offered a blank path and then did `HDD("")`; `Path("")` resolves to
`.`, giving `IsADirectoryError: Is a directory: '.'`. **Gone by construction**:
the prompt is now `--program`, and omitting it leaves channel 1 unregistered.

### 1.5 ✅ The assembler accepted registers the CPU doesn't have

The assembler mapped **A–Z** while the CPU has **A–F**, so `MOV G, #1`
assembled cleanly and died at runtime with `Register index out of range`.
`_find_available_register` made it worse by handing out registers 5–25 as
scratch — almost exclusively registers that don't exist.

**Fixed**: `REGISTER_COUNT` lives in `emulator/memory_map.py` and drives both
the CPU's default and the assembler's register table. Bad registers are now an
assembly-time error that names the limit:

```
Register G does not exist: this CPU has 6 registers (A-F)
```

`_find_available_register` is deleted. Related: `CPU.__init__` defaulted to
**5** registers while the caller passed 6 — any other caller silently lost
register F, which `bios.asm`, `checkerboard.asm` and `sincos.asm` all use.

### 1.6 ✅ `run_debug` used a stale load address

It free-ran until `pc >= 0x10000`, left behind when `PROGRAM_LOAD_ADDR` moved to
`0x20000`, so debug mode silently free-ran through the whole gap instead of
stepping. **Fixed** — it uses `PROGRAM_LOAD_ADDR`.

`bios2.asm` showed the same drift frozen solid (`IO_POINTER = 0x100`,
`PROGRAM_LOAD_ADDR = 0x10000`) and assembled under no surviving assembler. Deleted.

### 1.7 ✅ `BIOS_MAX` disagreed with itself three ways

The value was `0x400` (1024), the inline comment said "512 bytes reserved", and
the module docstring said `0x00000000 - 0x000001FF (512 B)`. **Fixed** — one
number, and the docstring now shows the real computed layout. The 73-line
commented-out copy of the previous layout is gone.

### Also fixed

- **`eval()` in the assembler.** It ran on text straight from the `.asm` file.
  A symbol-substitution regex incidentally blocked `__import__`, but that was an
  accident of the regex, not a defence, and arithmetic still got through —
  `X = 9**9**9` hung the assembler indefinitely. Replaced with an AST walk
  allowing only integer constants, symbols, and `+ - * / % << >> | & ^ ~`.
- **HTTP servers failed silently.** Both raised `RuntimeError` for a missing
  FastAPI *from inside a daemon thread*, where the exception is swallowed — the
  emulator ran on with no display and no explanation. The import is now checked
  on the calling thread.
- **`ram.attach_io` was dead** (marked `#unused`) while still costing a `None`
  check on every byte access. Deleted.
- **`Instruction.operands`** was computed at import by parsing every handler's
  source with `ast`, and read only by the assembler that didn't work. Deleted.

---

## 2. Performance

Reproduce with `python3 tools/bench.py`.

### 2.1 ✅ CPU dispatch — 2.07× measured

Profiling 3M instructions showed the work split across `cpu.step` (slice copy +
dict lookup), a `decode()` call, a `run()` wrapper that only re-checked
`halted`, and `Registers._resolve`. Three Python calls per instruction was about
half the runtime.

`emulator/cpu.py` now folds `run`/`step`, reads via `struct.unpack_from`
straight off the RAM bytearray (no per-instruction slice allocation), and
dispatches through a 256-entry list instead of a dict:

```
before   1,220,000 IPS
after    2,526,000 IPS      ← 2.07x, identical results
```

`decode()` is still exported — the disassembler uses it.

### 2.2 ✅ Frame conversion — 130× measured

`_convert_to_rgba` looped over 40,000 bytes in Python, 30 times a second, to
swap R and B. Strided slice assignment does it in C:

```python
out = bytearray(data)
out[0::4] = data[2::4]      # R <- B
out[2::4] = data[0::4]      # B <- R
```

```
before   2.22  ms/frame     (450 FPS ceiling)
after    0.017 ms/frame     ← 130x, byte-identical output (asserted in tests)
```

At 100×100 this saves 2.2 ms/frame; at 640×480 the old version alone would have
cost 68 ms/frame and capped the display under 15 FPS.

### 2.3 ✅ IO is no longer polled every instruction

The main loop called `io_controller.update()` after *every* instruction. Even
the idle path was a `read_word` — a slice, an allocation and an
`int.from_bytes` — capping the machine at ~3.1M IPS on the fastest possible path
while doing nothing.

Writes landing in the IO window now set `ram.io_pending`, and the main loop
checks that flag. Both `write_byte` and `write_word` arm it, because a guest can
select a channel with `MW`, not just `MWW`.

> The subtle part: `IOController.update()` writes to RAM itself (clearing the
> channel, storing the return length), which re-arms the flag. It clears the
> flag **last**, after its own writes — otherwise every instruction re-runs the
> command forever. `test_io_controller_clears_pending_after_its_own_writes`
> guards this.

### 2.4 ✅ Clock sampling

`time.time()` was called twice per instruction to decide whether a second had
elapsed. Now read once per `CLOCK_SAMPLE_INTERVAL` (10,000) instructions via a
countdown — small enough that a 30 FPS frame deadline is never overshot (~4 ms
at current speeds), large enough to cost nothing.

### 2.5 ✅ Debug printing left the hot path

`io_controller` printed on every channel 1/2 command, `hdd` on every read, `HID`
on every keypress, and `main.py` dumped a full BIOS disassembly on every start.
During boot the copy loop *is* channels 1–2, so this was thousands of `print()`
calls on the slowest path. All now `logging`, behind `--verbose`; the BIOS dump
is behind `--disasm-bios`.

### 2.6 ✅ `dump_ram()` no longer copies 128 MB

Returns a `memoryview`; `file.write()` accepts it directly.

---

## 3. Duplication & dead code

### 3.1 ✅ Four assemblers → one

1,711 lines, 62–83% pairwise identical, and they did not agree:

| | `bios.asm` | `user/*.asm` |
|---|---|---|
| `ass2.py` | ✅ byte-exact | ✅ all four |
| `assemblercopy.py` | ✅ byte-exact | ❌ all four fail |
| `ass.py` | ⚠️ **544 bytes vs 528 — silently different** | ❌ 2 of 4 |
| `assembler.py` | ❌ crashes (`case 3`) | ❌ |

`ass.py` was the dangerous one: it printed ✓ and emitted a BIOS two
instructions longer. Nothing in the filenames said which to trust.

`ass2.py` survives as `assembler/assembler.py`. ~1,280 lines removed.

### 3.2 ✅ The docs described an assembler that didn't exist

1,146 lines across four `.md` files documented an `ADD C #1` shorthand that the
working assembler rejected — and the shipped `assembler/example.asm` used it, so
**the documented example program did not assemble**.

Fixed by implementing the shorthand rather than deleting the example: `ADD C #1`
now expands to `ADD C C #1` for all nine arithmetic/bitwise instructions, with a
test asserting both forms encode identically. The four docs are one accurate
`assembler/README.md`.

### 3.3 ✅ The disassembler was written three times

Identical decode-and-format blocks in `main.py` (twice) and `test.py`, each with
its own drifting format string. Now one `disassemble()` in
`emulator/instruction_set.py`, used by the debugger, the BIOS dump and
`tools/disasm.py`. Its output is **byte-identical to the pre-refactor baseline**.

### 3.4 ✅ Memory-map constants were copy-pasted into assembly

`bios.asm` and every `user/*.asm` hand-copied `IO_POINTER`, `DISPLAY_START`,
`PROGRAM_LOAD_ADDR`, `HEAP_ADDRESS` and the IO header offsets — exactly what
`memory_map.py`'s own docstring asked them not to do.

The assembler now predefines every uppercase int in `memory_map.py` plus the
`IOHeader` fields. Sources were stripped of their hand-copied copies, and
`bios.asm`'s magic `+24`/`+20` are now `IO_USABLE_AFTER` and `IO_RETURN_DATA`.
Redefining a built-in to a *different* value warns; `tests/test_golden.py` fails
if any source drifts. See [6.1](#61-userui-asm-had-been-drawing-into-the-io-region) for why this matters.

### 3.5 ✅ `memory_map.py` was half commented-out duplicate

73 of 152 lines were the previous version of the file, commented out. Git has it.

### 3.6 ✅ Dead imports and vestigial names

`main.py` imported `argparse`, `IO`, `encode`, `PROGRAM_LOAD_ADDR` unused, plus
`import cpu` alongside `from cpu import CPU`, and `INSTRUCTIONS_BY_OPCODE` three
times (once inside a method). `io_controller.py` had `from ram import RAM` *and*
`import ram`, neither used. Also removed: `hdd.default_hdd()` (never called),
`Timer.current_timer` (written, never read), the timer's `print("no support")`
`__main__` stub, three no-op `from __future__ import annotations`, a stray
`pass`, and a menu that said "choose 1-4" while offering 0-4.

Disk file handles are now closed — `Machine` is a context manager.

### 3.7 ✅ Tests

25, all passing, runnable two ways:

```bash
python3 tests/test_golden.py && python3 tests/test_smoke.py   # no dependencies
python3 -m pytest tests/                                       # if you have pytest
```

`tests/_runner.py` exists because this environment is PEP 668-managed and has no
pytest; the suite works either way.

- **`test_golden.py`** — `firmware/bios.asm`, `user/screen.asm` and
  `user/checkerboard.asm` must assemble byte-for-byte to `tests/golden/`. This
  is the gate that proved the assembler rewrite and the four new opcodes changed
  nothing. Plus a drift check across all of `firmware/` and `user/`.
- **`test_smoke.py`** — per-instruction CPU behaviour, and a `test_bug_*`
  regression for each finding in §1.

---

## 4. Repository hygiene

### 4.1 ✅ 5 MB of files that were entirely zeros

| File | Size | Non-zero |
|---|---|---|
| `sincos.py` | 1,048,576 | **0** |
| `screen.asm` | 1,048,576 | **0** |
| `sreen_test.bin` (typo) | 1,048,576 | **0** |
| `display/screen.asm` | 1,048,576 | **0** |
| `display/screen.bin` | 1,048,576 | **0** |

`sincos.py` was a *Python file* of 1 MB of nulls — unparseable
(`SyntaxError: source code string cannot contain null bytes`). Each is exactly
`PROGRAM_MAX_SIZE`, which is what assembler output redirected onto its own input
looks like. The real sources survived as `user/screen.asm` and `user/sincos.asm`.

All deleted, and **the assembler now refuses to write to `.py`/`.asm`/`.c`/`.h`/`.md`.**

The four 16 KB `user_io.bin*` files were runtime scratch (an IO-window dump and
20 bytes of `0xFF`), not source — deleted too.

### 4.2 ✅ No `.gitignore`; 15 `.pyc` files committed

Added, and `__pycache__` untracked in all three directories. `build/` — the
assembled binaries and the disk image — is gitignored.

### 4.3 ✅ `requirements.txt` was missing three dependencies

`pydantic`, `pygame` and `requests` were imported but unlisted, so following the
README got you an `ImportError`. Unused `aiofiles` removed.

Split in two, because they are genuinely different dependency sets:

- **`requirements.txt`** — `fastapi`, `uvicorn`, `pydantic`: only the display
  and input HTTP servers. `--headless` imports none of them, and the CPU,
  assembler, tools and tests need nothing at all.
- **`requirements-client.txt`** — `pygame`, `requests`: only
  `display/display.py`, a separate process that talks HTTP. The browser
  front-end at `:8000` needs neither.

The README's `pip install -r requirements.txt` also fails outright on any
PEP 668-managed system Python (`externally-managed-environment`), which is most
current distros. Both the README and the client now show the venv form, and
`display/display.py` catches the `ImportError` and prints the fix instead of a
bare `ModuleNotFoundError`.

---

## 5. Design improvements

### 5.1 ✅ `main.py` split

`PigeonEmulator.__init__` did five jobs in 69 lines: construct devices, prompt
for input, bind two ports, and print two debug dumps.

- **`emulator/machine.py`** — `Machine`: wiring and state. Prompts for nothing,
  prints nothing, binds no ports. Importable from tests. A context manager.
- **`emulator/cli.py`** — argparse, menu, debugger.
- **`start_emulator.py`** — the launcher, at the repo root.

`start_emulator.py` also **auto-assembles the BIOS when it's missing or older
than `firmware/bios.asm`**. `build/` is gitignored, so without this a fresh
clone would refuse to start until you knew to run the assembler by hand.

### 5.2 ✅ Device registration is data

Channel numbers were magic literals in `main.py` and in every `.asm`. Now
`CH_USERPROG`/`CH_HDD`/`CH_HID`/`CH_TIMER` in `memory_map.py`, which — via
§3.4 — makes the same names available in assembly.

### 5.3 ✅ `CALL` / `RET` / `SHL` / `SHR`

The ISA had a stack but no way to call a function; every subroutine hand-rolled
a return address, which is what `docs/ideas/FFS.asm` was working around. Added
as opcodes 25–28, **appended** so 0–24 keep their numbers — asserted by
`test_opcodes_0_to_24_are_unchanged` and proved by the golden binaries.

### 5.4 ✅ `negative_flag` → `less_flag`

It is an unsigned borrow, not a sign — registers are 32-bit unsigned and never
go negative. The old name actively misled: `sincos.asm` contains two dead
`CMP F, #0` / `JL` clamp branches that can never fire, written by someone who
believed the flag meant what its name said.

### 5.5 ✅ Batched IO copies

The controller moved its data window word by word — 1,024 round trips for a 4 KB
transfer. Now one slice, which also drops the `length // 4` truncation that
silently discarded the last 1–3 bytes of any non-word-multiple transfer.

### 5.6 ✅ CORS narrowed

Both servers used `allow_origins=["*"]`, so any page in any open browser tab
could POST synthetic keystrokes into the running machine. Now the local origin.

---

## 6. Found during the refactor

New findings, not in the original audit.

### 6.1 ✅ `user/ui.asm` had been drawing into the IO region

It declared `DISPLAY_START = 0x1218` and `IO_POINTER = 0x200` — internally
consistent, but a **third** generation of the layout, matching neither the
current map nor the commented-out old one. Every pixel it wrote landed inside
the IO controller's memory instead of the framebuffer, so the program displayed
nothing at all and quietly scribbled on device registers.

**Fixed** by deriving its addresses from the memory map (§3.4). Verified: the
assembled binary no longer contains `0x1218` anywhere and now targets `0x1418`.
This is the concrete reason the built-in symbols and the drift warning exist.

### 6.2 ✅ `CPU.__init__` defaulted to 5 registers

The signature said `register_count=5` while the only caller passed `6`. Any
other construction silently lost register F, which three of the five assembly
programs use. Now defaults to `REGISTER_COUNT`.

### 6.3 📝 `user/checkerboard.asm` overruns the framebuffer and overwrites itself

`checkerboard.asm:93`'s `JMP DRAW_PIXEL_RET` lands on `ADD B B #1`, pushing the
pixel index past the 10,000-pixel loop bound; `CMP B #10000` / `JNZ` is then
never equal, so the loop runs forever with a growing index. At B = 31,487 the
write address reaches `DISPLAY_START + B*4 = 0x20014` — **the program itself** —
and the CPU faults on the corrupted instruction at `0x20010`.

**Not fixed, deliberately.** Two reasons: guest-program logic is outside this
refactor, and `user/checkerboard.asm` is one of the three golden fixtures whose
byte-exact output is what proves the assembler rewrite was faithful. Changing it
would destroy that evidence.

**Confirmed pre-existing.** The original pre-refactor code, run on the original
committed binaries, faults at the identical PC with the identical register
state (B = 31,487). Say the word and I'll fix the program and re-bless the
fixture.

Use `user/screen.asm` for a clean end-to-end demo — it runs to `HALT` and fills
9,999 of 10,000 pixels.

### 6.4 📝 Other guest-program bugs

Real, but guest logic rather than emulator defects:

- **`user/ui.asm:83-105`** — the `WAIT` routine is entirely dead. `WAIT_LOOP:`
  begins with `POP A` / `JMP A`, so it returns immediately; the delay code below
  is unreachable and the two draw routines alternate at full CPU speed.
- **`user/sincos.asm:37`** — `COLOR_BLACK = 0x00F0000` has seven hex digits,
  almost certainly meant to be `0x00000000`. Alpha ends up `0x00`, so it is
  transparent rather than black. `COLOR_RED = 0xFF00FFFF` is **cyan**: the word
  format is `0xAARRGGBB`, so that is R=0 G=255 B=255. (An earlier draft of this
  document called it yellow; I have since written each colour to RAM and read it
  back through the serving path — the verified table is in
  [compiler/design/06-display.md](compiler/design/06-display.md).)
- **`user/screen.asm:50-51`** — writes the blue channel at byte offset 0 (the
  red byte) while the comment says `A+2`; one channel is written twice and
  another never.
- **`firmware/bios.asm`** — `PROG_SIZE = 0x1000` makes the DMA read end at
  `IO_START + 24 + 0x1000 = 0x1418`, which is *exactly* `DISPLAY_START`. A
  one-byte overrun writes into the framebuffer. Now documented at the
  definition; the real fix is to clamp against `IO_SIZE - IO_USABLE_AFTER`.

### 6.5 ✅ The display client could crash the HID server

`display/display.py` forwards raw pygame keycodes, and the `/key` endpoint did
`print(chr(body.code))`. Arrow and function keys are `1073741903`+, well above
`chr()`'s maximum, so **pressing an arrow key raised `ValueError` in the
endpoint**. Now logged as an integer. Verified: posting `1073741903` returns
`{"status":"ok"}` and queues the key.

### 6.6 ⏭️ `display/display.py` has further issues

Untouched — it's a standalone HTTP client, outside the emulator boundary. The
notable ones: HID sends are synchronous inside the render loop (a hanging server
stalls the UI up to 1.5 s per event), UI button clicks also inject a click at
(0,0) into the guest, `_fetch_loop` busy-spins on success regardless of `--fps`,
there are no key-release events, and its docstring names a file
(`display_client.py`) that doesn't exist.

---

## 6b. Second round — input and the compiler blockers

Work done after the design in `compiler/design/` exposed what the toolchain
could not do. All verified by running code; see `tests/test_input.py`,
`tests/test_directives.py`, `tests/test_loader.py` (89 new tests).

### ✅ The assembler could not emit a single byte of static data

No `.byte`, `.word`, `.ascii`, `.asciz`, `.space`, `.align` — and the size model
was a hardcoded `+= 8` per line in *both* passes. A compiler cannot emit an
initialised global, a string literal, or a font table without this.

Added, along with a structural fix for the risk they introduce: a line is now
parsed **once** into an `Item` that knows both its size and its bytes, and
`_emit` asserts the two agree. Pass drift would silently relocate every later
label; now it cannot happen, and the assertion catches it anyway.

Also fixed, each a silent-wrong-answer bug:

| Bug | Was |
|---|---|
| `.ORG PROGRAM_LOAD_ADDR` | literal-only regex → silently **org = 0**, and the compiler design told the compiler to emit exactly that |
| Label named `A`–`F` | shadowed by the register; `JMP A` became register-indirect |
| Duplicate label | accepted, last one won |
| `.L3:` local label | defined but unreferenceable (`ast.parse` chokes on the dot) |
| Error line numbers | indexed the preprocessed list — a bad line 9 reported as "Line 2" |

### ✅ Programs were silently capped at 4 KB

The BIOS issued one DMA read of one IO window and stopped. A 5,616-byte program
was truncated at ~4 KB; the CPU then ran into whatever followed and halted with
a garbage value, **with no error**. The largest hand-written program was already
at 35% of that ceiling.

The loader now reads in chunks until a short read signals EOF. Verified up to
12 KB, including the exact-multiple case that asks for one chunk too many. The
window length is derived from `IO_SIZE - IO_USABLE_AFTER` rather than hardcoded —
the old `PROG_SIZE = 0x1000` ended exactly at `DISPLAY_START`, so a one-byte
overrun landed in the framebuffer.

`tests/golden/bios_v1.{asm,bin}` is a frozen copy of the old BIOS, kept purely
to pin the *assembler*; `firmware/bios.asm` is now free to evolve.

### ✅ Input: four gaps, and a second buffer

Three were documented in §6.5 and `compiler/design/07-input.md`; the fourth was
found while implementing them.

| | Was |
|---|---|
| Non-ASCII keys | `code & 0xFF` turned the Right arrow into `O`, Up into `R`, F1 into `:` |
| Browser front end | sent no input at all — two listeners, both for its own toolbar |
| Key release | never forwarded, so hold-to-move was impossible |
| **Mouse button mask** | **nothing ever posted `/mouse_buttons` and edges did not update it, so `GET_MOUSE_BUTTONS` always returned 0** |

Plus a **real-time buffer** alongside the FIFO: commands 6 and 7 expose held-key
state as a single-key query or a 32-byte bitmap. The FIFO answers "what
happened, in order"; the real-time state answers "what is true right now".
Neither substitutes for the other.

Also: a device reply is now clamped to the 4096-byte IO window (it ended exactly
at `DISPLAY_START`, so an over-long reply scribbled into video memory); FIFO
commands no longer drain on a *write*, where the controller discards the result;
and replies are a fixed size per command rather than `length` bytes, which is
how a word-sized read of the key FIFO used to pop four keys and look at one.

---

## 7. Still open

- **§6.3** — `checkerboard.asm` self-corrupts. Deliberate; see above.
- **§6.4** — four guest-program bugs, recorded not fixed.
- **§6.6** — the pygame client's own defects.
- **`DIV` by zero** raises `ZeroDivisionError` instead of setting a flag the
  guest can test. Needs a status-register design, not a patch.
- **No signed comparison.** `CMP` is unsigned only. If signed comparison is
  wanted, add `CMPS` rather than overloading `CMP`.
- **The `compiler/` sketches** (`docs/ideas/`) reference an `MMW` instruction
  that never existed and a calling convention that predates `CALL`/`RET`.

---

## Appendix — verifying the claims

```bash
python3 tools/bench.py                      # 2.07x CPU, 130x frame conversion
python3 tests/test_golden.py                # byte-exact assembler output
python3 tests/test_smoke.py                 # CPU behaviour + every bug in §1

# the assembler still reproduces the pre-refactor binaries exactly
python3 assembler/assembler.py firmware/bios.asm build/bios.bin
cmp build/bios.bin tests/golden/bios.bin && echo "byte-exact"

# end to end
python3 assembler/assembler.py user/screen.asm build/screen.bin
python3 start_emulator.py --program build/screen.bin --run
curl -s localhost:8000/info                 # {"w":100,"h":100,"size":40000}

# nothing is pure padding any more
python3 -c "
import pathlib
for p in sorted(pathlib.Path('.').rglob('*')):
    if p.is_file() and '.git' not in p.parts and p.stat().st_size > 1000:
        if not any(p.read_bytes()): print(f'{p}: all zero')
"
```
