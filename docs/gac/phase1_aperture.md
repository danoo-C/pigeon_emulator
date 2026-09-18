# Phase 1: the aperture and `--ram`

> Part of [the GAC plan](README.md). **Status: built, 2026-09-18
> ([As built](#as-built)).**
> Design: [design.md §5.1](design.md#51-the-address-space-doubles-and-vram-is-mapped-into-the-top-half).
> Decisions: Q2, Q3 and §11 in [decisions.md](decisions.md).

**The goal:** the address space becomes twice the RAM, and its top half is
an aperture onto a buffer of video memory. Nothing is behind it yet but
zeros: no device, no mode, no channel. A guest store into the aperture
lands in VRAM, and a guest load from it reads VRAM back. Also, the machine's
RAM size becomes something you can set.

**Why this is its own phase:** it is the one change on the hot path. Every
`MRW`/`MWW` the machine ever executes goes through `RAM.read_word` and
`RAM.write_word`, so this phase is where a regression would be felt by
everything. Doing it alone, with nothing else changing, means the benchmark
compares like with like.

---

## Steps

### 1.1 `VRAM_SIZE` in the memory map

`emulator/memory_map.py` gains `VRAM_SIZE = 16 MB` (Q12), the default size of
video memory. **No `VRAM_APERTURE` and no `ADDR_SPACE` constant:** both depend
on the machine's RAM size, so they are computed per machine (§5.1).
`CH_VRAM` and `CH_GAC` wait for Phases 2 and 3, when there is a device to
put on them.

### 1.2 The aperture in `RAM`

`RAM(size, vram_size=0)`:

- **No VRAM (`vram_size == 0`), the default: exactly today's behaviour.**
  `mask = size - 1`, and `vram_base = size`, so the new compare can never
  be true: `addr & mask` is always below `size`. `tests/test_smoke.py`'s
  1024-byte RAM, which wraps a word at 1022, is what proves this.
- **With VRAM:** `vram_base = size`, `mask = 2 * size - 1`, and
  `self.vram = bytearray(vram_size)`. `vram_size` must be at most `size`,
  because the aperture is `size` bytes long. `size` must be at most 2 GB, so
  the mask fits in 32 bits.
- Each of the four accessors gets one compare, `if a >= self.vram_base`,
  before anything else. An aperture address past the end of VRAM **reads 0
  and drops writes**. There is nothing there, the same as an unregistered
  channel.
- **A word that straddles a boundary goes byte by byte through the byte
  accessors**, so each byte finds its own buffer. There are two boundaries:
  RAM's top into the aperture, and the aperture's top, which wraps back to
  address 0.
- **Aperture writes set `vram_dirty`, not `io_pending`.** It is one store
  with no range arithmetic, the same shape as `io_pending`. Phase 8 reads it
  (§5.5).
- `load_bytes` already refuses anything past `size`, so nothing can load
  into video memory. `display_slice` and `dump_ram` stay RAM-only for now;
  Phase 2 is where a snapshot can come from VRAM.

### 1.3 Executing video memory is a fault

The CPU fetches with `struct.unpack_from` straight out of `ram.mem`
*(checked: `cpu.py:69`, `machine.py:224`)*. That never masks, so a jump into
the aperture runs off the end of the bytearray and raises `struct.error`,
which is already `VEC_BAD_FETCH`. **That is the right answer, and it costs
nothing:** a program jumping into its framebuffer is a bug. The step is only
a test to pin it down.

The DMA devices (HDD, debug port, `CH_DISPLAY`'s `FILL`/`COPY`) check
addresses against `len(ram.mem)` *(checked)*, so they already refuse the
aperture. Getting bytes in and out of VRAM by DMA is Phase 2's
`UPLOAD`/`DOWNLOAD`.

### 1.4 `Machine(ram_size, vram_size)`

`Machine` passes `vram_size=VRAM_SIZE` by default, so a real machine has
video memory from now on. Tests that build a `RAM` directly keep none.

### 1.5 `--ram` and `--vram`, and `config.json`'s `ram` and `vram` (§11)

- `"ram": "128M"` and `"vram": "16M"` in `DEFAULTS`, parsed with the
  existing `_size()`. `--ram SIZE` and `--vram SIZE` override them once.
- `ram` must be a power of two, **at least 128 MB and at most 2 GB**. Below
  128 MB, the fixed addresses near the top would wrap: the second-stage BIOS
  at `0x07000000` and `STACK_TOP`. Above 2 GB, the aperture would not fit in
  32 bits.
- `vram` may be `null` for no video memory, which is the machine from
  before this plan. It must not be larger than `ram`.

### 1.6 Tests: `tests/test_vram_aperture.py`

- no VRAM: the mask, `vram_base` and wrapping are exactly as before
- a word and a byte round-trip through the aperture and land in `ram.vram`,
  not in `ram.mem`
- past the end of VRAM: reads 0, writes dropped, and neither buffer grows
- straddling RAM's top into the aperture, and the aperture's top back to 0
- `vram_dirty` is set by an aperture write, and `io_pending` is not
- a guest program (`MWW`/`MRW` through the CPU) draws into the aperture
- a jump into the aperture is `VEC_BAD_FETCH`
- the size checks in `RAM`, and `ram`/`vram` in `config.py` and the CLI

### 1.7 Proof and measurement

- **The whole suite, unchanged.** No existing test should need an edit. If
  one does, this phase is wrong, and it is worth stopping (§8).
- **`tools/bench.py` before and after,** and a new line for a word written
  through the aperture. §4.2 measured the extra compare as free, and this is
  where that gets confirmed.
- **A 1 GB machine, once, by hand**: `RAM(1 << 30, vram_size=16 MB)`
  should have its aperture at `0x40000000` and mask `0x7FFFFFFF`, with a
  word round-tripping through both halves. It is not in the suite, because
  `bytearray(1 GB)` is really allocated and the suite runs in parallel
  (§8). The result goes in *As built* below.

**Done when:** all of 1.7 holds.

---

## What `--ram 1G` does and does not give you yet

Found while planning this phase, and not said in the original plan: **a
bigger RAM is not yet bigger for the guest.** The machine allocates it,
wraps at it, and puts the aperture above it. But the guest's layout is
fixed at 128 MB: `STACK_TOP = RAM_SIZE - 4` is a constant baked into every
build, `BIOS2_LOAD_ADDR` is `0x07000000`, and `lib/pigeon/mem.c` stops the
heap at `0x07F00000` *(checked)*. On a 1 GB machine, everything above
128 MB is there and addressable, but nothing uses it.

This does not hurt the GAC plan. The aperture is found through `VRAM_INFO`
from Phase 2 on, never through a constant, which is the rule §5.1 and §8
already set. Letting the guest *use* more RAM is a separate and fairly small
job: a way for the guest to ask how much RAM there is (the BIOS setting the
stack from it, `mem.c` ending the heap from it). It is listed as a follow-up
under [README.md §9](README.md#9-not-in-this-plan) rather than done here.

---

## As built

Built 2026-09-18, as planned, with one change of approach in 1.2.

**The aperture is not a compare in front of RAM. It is the fallback
behind it.** The plan's version, `if a >= self.vram_base:` first in every
accessor, measured **+12–15 ns on every byte access** (`read_byte`
60 → 74 ns), and that is about 20% of every `MR`/`MW` on every machine. §4.2's
"the extra branch is free" was measured on words, where it held. So:

- **bytes:** `self.mem[a]` inside a `try`. A RAM address never raises. An
  aperture address is past the end of `mem`, raises `IndexError`, and only
  then goes to VRAM. A `try` costs nothing until it catches (Python 3.11+).
- **words:** the `a + 4 <= self.size` bounds check RAM always had is the
  branch. Failing it now means "not wholly in RAM", and only then is VRAM
  tried, then the byte-by-byte path.

The cost moved to where it belongs. **RAM is back to what it cost before**,
and the aperture pays instead:

| | before | after |
|---|---|---|
| RAM `write_word` | 165 ns | 164–171 ns |
| RAM `read_word` | 181–187 ns | 144–158 ns |
| RAM `write_byte` | 88–90 ns | 90 ns |
| RAM `read_byte` | 60 ns | 63–66 ns |
| aperture `write_word`, **one pixel** | — | 196–205 ns |
| aperture `read_word` | — | 204 ns |
| aperture `write_byte` / `read_byte` | — | 261 / 230 ns (the exception) |
| real program, `tools/bench.py` | 2.77M IPS | 2.80M IPS |

A pixel through the aperture is about 20% dearer than a RAM word, and it is
**still one instruction**. That is the property the design needed (§4.1: the
bus would be 15–25×). An aperture *byte* costs about 4× a RAM byte. Pixels are
words, and `lib/pigeon/mem.c`'s `memcpy` moves aligned data a word at a time
*(checked)*, so that path should be rare. `tools/bench.py` now prints both
word lines.

**The 1 GB machine, by hand:** `RAM(1 << 30, vram_size=16 MB)` builds in
0.46 s with 1,049 MB peak RSS. The aperture is at `0x40000000` and the mask
is `0x7FFFFFFF`, as §5.1's table says. Words round-trip at the top of RAM, at
the first and last word of VRAM, and through the wrap one whole space up;
past VRAM reads 0.

**Files:** `emulator/ram.py` (the aperture), `emulator/memory_map.py`
(`VRAM_SIZE`), `emulator/machine.py` (`vram_size=VRAM_SIZE`),
`emulator/config.py` (`ram`, `vram`, the checks), `emulator/cli.py`
(`--ram`, `--vram`), `tools/bench.py` (two lines). Tests:
`tests/test_vram_aperture.py` (new, 24 cases) and 14 new cases in
`tests/test_config.py`.

**`--vram 0`** (or `"vram": null`/`0`) gives the machine from before this
plan. That was not in the plan, but it is how to rule video memory in or
out when chasing a bug.

**The suite:** 1,676 passed, 2 failed. **Neither failure is this phase.**
Both are `test_install.py` and `test_project.py` expecting
`"32 files to copy"`, while the OS disc now has 33 files. The 33rd is the
uncommitted `/docs/gui.pgs` line in `user/os/pigeon_compiler_init.txt`. With
that one line reverted, the test passes. Those two assertions need their count
bumped when `gui.pgs` is committed.
