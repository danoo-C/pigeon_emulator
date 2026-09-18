# GAC and VRAM: the design

> Part of [the GAC plan](README.md). This is the design the phases build:
> the measurements behind it, the two devices, the host and guest sides,
> what changes file by file, and the risks. **Section numbers are kept from
> the original single-file plan** (§4, §5, §6, §8), so every "§5.3.1" in
> these files still means the same thing. Where the text says **"step N"**,
> it means **[Phase N](README.md#phases)** — the original plan's eight steps
> became the eight phases one for one. Facts marked *(checked)* were read in
> the code; numbers marked *(measured)* came from benchmark runs on this
> machine on 2026-09-17; the rest is reasoned.

---

## 4. What the numbers say

You asked what happens if VRAM ends up on the IO controller. I measured it
rather than guessing. All figures CPython 3.13 on this machine, 2026-09-17;
`.pypy/bin/pypy3` is roughly 12× faster on the interpreter but the same
ratios hold.

### 4.1 A pixel through the bus is 15× a pixel through a store — and that is the optimistic number

One pixel written by selecting a channel (five header stores, the controller
dispatch, the device callback) against one pixel written by `MWW`:

| | per pixel | rate |
|---|---|---|
| plain store into RAM | 0.25 µs | 3,963,000 px/s |
| **through the IO bus** | **3.72 µs** | **269,000 px/s** |

**15× slower**, host-side — and that measurement *charges the bus path
nothing for the extra guest instructions*. The guest executes **six** stores
instead of one, so it also fetches and decodes six instructions instead of
one. The honest figure is nearer **20–25×**.

What that means for a screen, one pixel at a time through the bus:

| mode | time to write every pixel |
|---|---|
| 192 × 108 | 0.08 s |
| 640 × 480 | **1.14 s** |
| 1280 × 720 | **3.42 s** |

**So: VRAM must not be reached a pixel at a time through the IO controller.**
Not "should not" — a single screen clear would take longer than a second.
This is the load-bearing conclusion of the whole plan, and §5.1 is the answer
to it.

### 4.2 An aperture costs nothing measurable

The alternative is to *map* VRAM into the address space and let the existing
`MWW` reach it. That means `RAM.write_word` has to decide which buffer an
address belongs to. I measured three shapes of that decision at 1.5M writes
each:

| | write_word/s | ns |
|---|---|---|
| today, RAM only | 4,454,000 | 224.5 |
| separate VRAM buffer, one extra compare — RAM address | 4,698,000 | 212.9 |
| separate VRAM buffer, one extra compare — VRAM address | 4,757,000 | 210.2 |
| VRAM appended to the same bytearray — RAM address | 4,607,000 | 217.1 |
| VRAM appended to the same bytearray — VRAM address | 4,738,000 | 211.1 |

**The extra branch is free** — it is inside the run-to-run noise, and the
VRAM path is actually *faster* because it returns before the IO-window check.
There is no reason to prefer the shared-bytearray trick over a clean separate
buffer, so §5.1 takes the separate buffer.

### 4.3 The GAC's own work is cheap, and it is what makes big screens possible

Host-side cost of the accelerated primitives, as row-wise slice assignments
on a `bytearray`:

| operation | time |
|---|---|
| fill 32 × 16 (a glyph cell run) | 0.002 ms |
| fill 192 × 108 (a whole old screen) | 0.020 ms |
| fill 640 × 360 | 0.078 ms |
| **fill 1280 × 720 (a whole screen)** | **0.288 ms** |
| blit 1280 × 700 (a console scroll) | 0.258 ms |

Against that, in guest instructions: a 640 × 480 clear written as a store
loop is about 1.2M instructions ≈ **0.9 s** at the interpreter's honest ~1.3M
IPS. The GAC does it in 0.08 ms. **That is the ratio the accelerator is for.**

The worst case is text. `con_redraw` costs about a million instructions at
192 × 108 *(checked: the note in `display_io.py`)*, because the console is
32 × 12 = 384 cells of 40 pixels each. At 1280 × 720 the console is 213 × 80
= **17,040 cells, 44× more** — call it 44M instructions, **34 seconds** per
redraw. A resizable display without a hardware glyph blitter is not a
resizable display. Hence `GAC_TEXT` in §5.3.

### 4.4 The real ceiling is getting the frame out of the process

Two costs scale with pixels no matter how clever the guest is.

**The RGBA swizzle** in `DisplayIO._convert_to_rgba` (memory is B,G,R,A; the
canvas wants R,G,B,A):

| mode | frame | swizzle | FPS ceiling | of one core at 30 FPS |
|---|---|---|---|---|
| 192 × 108 | 81 KB | 0.056 ms | 17,856 | 0.2% |
| 320 × 180 | 225 KB | 0.207 ms | 4,839 | 0.6% |
| 640 × 360 | 900 KB | 1.138 ms | 879 | 3.4% |
| 640 × 480 | 1.2 MB | 1.388 ms | 720 | 4.2% |
| 1280 × 720 | 3.6 MB | 6.449 ms | 155 | **19.3%** |
| 1920 × 1080 | 8.1 MB | 13.478 ms | 74 | **40.4%** |

**And the HTTP bandwidth,** which nobody has had to think about at 81 KB a
frame: 1280 × 720 at 30 FPS is **105 MiB/s** over localhost, and 1080p is
**237 MiB/s**. That, not the CPU, is what will decide the top mode.

Both have answers (move the swizzle into the front ends, track damage
rectangles in the GAC) and both are in §5.5 and §7 phase 5. Neither is a
reason not to build this — they are the reason **1280 × 720 is the top mode,
not 1920 × 1080** (Q12).

### 4.5 Blending: you are right that it is easy, and that is the trap

You said of alpha blending *"i think now, it shouldnt be that hard"* (Q9), and
that is true of **writing** it — `dst = dst·(1−a) + src·a` is three lines. It
is not true of **running** it. A blend cannot be a slice assignment, because
every destination byte has to be read before it is written, so the obvious
implementation is a Python loop over every pixel. Measured:

| | 1280 × 720 | vs the opaque version |
|---|---|---|
| opaque fill (slice assignment) | 0.329 ms | — |
| blended fill, **naive per-pixel loop** | **≈ 0.21 s** | 640× |
| blended fill, **per-channel `bytes.translate`** | **3.651 ms** | 11× |
| constant-alpha blit, **naive per-pixel loop** | **≈ 0.28 s** | — |
| constant-alpha blit, **SWAR over one big integer** | **18.5 ms** | 15× faster than naive |

So blending goes in v1 — **with the implementation named in the plan**,
because the obvious one is 60× too slow and would make the accelerator slower
than the software it replaces.

**The two tricks, both measured, both pure Python with no new dependency:**

1. **Constant colour, any alpha → three `bytes.translate` calls.** For a fixed
   source colour, `dst' = dst·(255−a)/255 + src·a/255` is an *affine map of one
   byte to one byte* — so it is a 256-entry lookup table, one per channel,
   built once per operation. Take the row's strided slice for a channel,
   `.translate()` it, put it back. That is C-speed, and it covers `FILL`,
   `FRAME`, `LINE`, `CIRCLE`, `DISC` and `TEXT` — every operation whose colour
   is a single word, which is nearly all of them.
2. **Blit at one global alpha → SWAR over a big integer.** `int.from_bytes` a
   whole row, split the bytes into even and odd lanes with a mask so each
   product has 16 bits of room, multiply both lanes by the constant alpha in
   one big-integer multiply, recombine, `to_bytes` it back. **Verified against
   a per-pixel reference implementation — the colour bytes match exactly.**

**And two fast paths that cost nothing:** `a == 255` is the existing opaque
slice assignment and `a == 0` is a no-op, both tested before any table is
built. **So nothing that draws today gets one instruction slower** — `0xFF…`
colours, which is everything in the codebase, take exactly the path they take
now.

**What does not fit,** and is the one piece of blending left out: a blit that
honours **per-pixel source alpha** (an RGBA sprite with soft edges). Neither
trick applies — the alpha differs every pixel, so no table and no single
multiply — and the naive loop is **0.28 s a screen**, 15× the constant-alpha
blit and 76× a blended fill.
The flag is reserved, the command refuses it, and it waits for a plan of its
own (§9).

---

## 5. The design

### 5.1 The address space doubles, and VRAM is mapped into the top half

The map below the top of RAM **does not change by one byte.**
`DISPLAY_START` stays `0x1418`, `PROGRAM_LOAD_ADDR` stays `0x20000`, the
heap still starts at `0x120000`, the stack still starts four bytes below the
top. Every `.bin` ever built keeps running.

What changes is that the *address space* becomes twice the RAM, and the top
half is an **aperture** onto video memory. At today's 128 MB:

```
    0x00000000 - 0x07FFFFFF   RAM, exactly as it is today      (128 MB)
    0x08000000 - 0x0FFFFFFF   VRAM APERTURE                    (128 MB of room)
        0x08000000 + 0                 first byte of video memory
        0x08000000 + VRAM_SIZE - 1     last byte of it
        ... above that: reads 0, writes dropped (nothing is there)
```

**The aperture is not the literal `0x08000000`. It is "wherever this
machine's RAM ends" (Q2).** That distinction is the whole of your 1 GB
question and it is load-bearing, because **RAM size is already a
per-instance thing**: `Machine(ram_size=…)` and `RAM(size)` take it as an
argument, `tests/test_smoke.py` runs on `TEST_RAM = 0x10000` and on a
1024-byte RAM, and `tests/test_bios2.py` on `SMALL_RAM = 1 << 18` *(checked)*.
A module-level constant would be wrong for every one of those machines. So it
is computed in `RAM.__init__`:

```python
def __init__(self, size=RAM_SIZE, vram=None):
    self.size = size
    self.vram = vram
    if vram is None:
        self.mask = size - 1             # EXACTLY as today -- see below
    else:
        self.vram_base = size            # the aperture starts where RAM ends
        self.mask = (size * 2) - 1       # the space is twice the RAM
```

and `write_word` gains one compare:

```python
def write_word(self, addr, value):
    a = addr & self.mask
    if a >= self.vram_base:                # measured free (§4.2)
        self.vram.write_word(a - self.vram_base, value)
        return
    ... exactly as now ...
```

**A `RAM` with no VRAM keeps today's mask and today's behaviour, byte for
byte.** That is not tidiness, it is required: `tests/test_smoke.py:117` builds
`RAM(1024)`, writes a word at 1022, and asserts both that it wraps and that
`len(ram.mem)` is still 1024 *(checked)*. Doubling that machine's space would
put those two bytes in an aperture instead of wrapping them, and the test —
which exists to pin a real bug from `REFACTORING.md` — would fail for a
reason that has nothing to do with graphics.

**How far RAM can grow.** The aperture rides on top of RAM, so it moves up
with it and never collides:

| RAM | aperture at | address space | mask | fits in 32 bits? |
|---|---|---|---|---|
| 128 MB (today) | `0x08000000` | 256 MB | `0x0FFFFFFF` | yes |
| **1 GB (what you asked about)** | **`0x40000000`** | **2 GB** | **`0x7FFFFFFF`** | **yes** |
| 2 GB (the ceiling) | `0x80000000` | 4 GB | `0xFFFFFFFF` | yes, exactly |

So **yes — it still lives above, and 1 GB is a one-line change**, because the
only thing that has to move is a number `RAM.__init__` already computes. 2 GB
of RAM is the ceiling, and that is the ceiling of a 32-bit machine anyway, not
one this design imposes.

**The rule that makes growable RAM safe: the guest must never hardcode the
aperture base.** `VRAM_INFO` reports it (§5.2) and `disp_init()` takes it from
there. `memory_map.symbols()` will export a `VRAM_APERTURE` symbol because it
exports every uppercase int *(checked)*, and **C code must not use it** — a
`.bin` built against a 128 MB machine and run on a 1 GB one would draw at
`0x08000000`, which is now the middle of the heap. Only the assembler sources
and the BIOS may use the symbol, and both are rebuilt with the machine. This
is the same class of trap as `PROGRAM_LOAD_ADDR` moving, and it deserves the
same kind of comment in `display.c` that `disp_probe` already carries.

**This is the answer to your performance question.** VRAM is a device — the
VRAM device owns the buffer, sizes it, allocates surfaces in it and decides
what is scanned out — but its *bytes* are reached by ordinary `MRW`/`MWW`,
not by bus transactions. The bus carries **control**: modes, allocations,
flips, and accelerated operations. It never carries a pixel unless you ask it
to.

`display_slice()` and `dump_ram()` need to learn about the second buffer;
`load_bytes` refuses aperture addresses, since nothing should be booting into
video memory.

> **Why not carve VRAM out of the existing 128 MB?** Because the heap grows up
> from `0x120000` and the stack grows down from the top, they share one
> uninterrupted block on purpose, and a 16 MB hole punched in the middle of it
> is a hole in the thing the memory map's docstring is proudest of. It would
> also get *worse* as RAM grew, not better.

> **Why not append VRAM to the same `bytearray`?** Measured no faster (§4.2),
> and it would make `RAM_SIZE`, `self.size` and `self.mask` three different
> numbers that must not be confused. A separate buffer keeps "this is not
> RAM" true in the code as well as in the design — and with a 1 GB RAM it is
> the difference between one 1 GB allocation and one 1,016 MB allocation that
> has to be reallocated whenever the mode changes.

### 5.2 `CH_VRAM = 9` — video memory

It owns `bytearray(VRAM_SIZE)`, the **mode**, and a small table of
**surfaces**. A surface is a handle, an offset in VRAM, a width, a height and
a pitch. Handle `0` is always the scanout surface, created by the current
mode.

| cmd | name | R/W | ADDRESS | window | reply |
|---|---|---|---|---|---|
| 0 | `NOP` | 0 | — | — | 4 zero bytes |
| 1 | `INFO` | 0 | — | — | `magic, vram_size, aperture_base, generation` |
| 2 | `GET_MODE` | 0 | — | — | `w, h, pitch, format, surface0_offset` |
| 3 | `SET_MODE` | 0 | — | `[w, h]` | `1` accepted / `0` refused, then the new `w, h, pitch, offset` |
| 4 | `MODE_COUNT` | 0 | — | — | how many modes `MODE_AT` will answer for |
| 5 | `MODE_AT` | 0 | index | — | `w, h` — the machine's offered modes |
| 6 | `PREFERRED` | 0 | — | — | `w, h, generation` — what the host window would like (§5.5) |
| 7 | `ALLOC` | 0 | bytes | `[w, h]` | `handle, offset, pitch`, or `0` if VRAM is full |
| 8 | `FREE` | 0 | handle | — | `1` freed / `0` no such handle |
| 9 | `SCANOUT` | 0 | handle | — | `1` accepted / `0` refused — **the page flip** |
| 10 | `SCANOUT_RAM` | 0 | RAM address | — | `1` / `0` — scan out of system RAM instead (§5.4) |
| 11 | `UPLOAD` | 0 | handle | `[ram_addr, vram_off, count]` | bytes moved, or `0xFFFFFFFF` |
| 12 | `DOWNLOAD` | 0 | handle | `[ram_addr, vram_off, count]` | bytes moved, or `0xFFFFFFFF` |

**`INFO` answers a magic word first**, the way the CD drive's `MEDIA` does, so
a program built before this device existed can tell "no VRAM here" from a
stale data window *(the trap `disp_probe` already documents: `0xFFFFFFFF` as
a length is enormous, not short)*.

**`generation` bumps on every mode change.** Guest code that cares compares it
the way `<pigeon/cd.h>` compares the disc's.

**Allocation is a bump allocator with a free list**, and it never moves a
surface — a guest holding a raw pointer into the aperture must not have it
move under them. `FREE` of the last surface rewinds the bump pointer;
otherwise the block joins a free list, exactly as `lib/pigeon/mem.c` does and
with the same fragmentation caveat.

**`UPLOAD`/`DOWNLOAD` are DMA between system RAM and VRAM,** with `[address,
count]` in the window and R/W 0 so the count comes back — deliberately the
same shape as the HDD's `READ_DMA`/`WRITE_DMA` and the debug port's
`WRITE_DMA`, so there is one DMA idiom on this bus and not three. They exist
for `bmp_load`, which decodes into the heap and then wants the result on
screen; a guest with an aperture pointer can also just `memcpy`, and for
anything under a few kilobytes that is faster than programming a transfer.

### 5.3 `CH_GAC = 10` — the accelerator

Every operation names its surfaces by handle and takes its arguments as words
in the 4 KB data window. A surface handle of `0xFFFFFFFF` means *system RAM*,
with the address and geometry given in the window — that is what lets the GAC
blit a decoded BMP straight out of the heap.

| cmd | name | window | what it does |
|---|---|---|---|
| 0 | `NOP` | — | 4 zero bytes |
| 1 | `INFO` | — | `magic, features, max_batch` |
| 2 | `FILL` | `dst, x, y, w, h, colour` | a filled rectangle, clipped |
| 3 | `FRAME` | `dst, x, y, w, h, colour` | an outline |
| 4 | `BLIT` | `src, sx, sy, dst, dx, dy, w, h` | a copy, overlap-safe |
| 5 | `BLIT_SCALED` | `…, sw, sh, dw, dh` | nearest-neighbour — `bmp.h`'s `STRETCH`, in hardware |
| 6 | `LINE` | `dst, x0, y0, x1, y1, colour` | |
| 7 | `CIRCLE` / 8 `DISC` | `dst, cx, cy, r, colour` | outline / filled |
| 9 | `SET_FONT` | `src, glyph_w, glyph_h, first, count, stride` | 1 bit per pixel, the format `display.c`'s `FONT[]` already uses |
| 10 | `TEXT` | `dst, x, y, fg, bg, len` + the bytes | a whole string, one bus trip |
| 11 | `SCROLL` | `dst, y, h, dy, bg` | what `disp_scroll` means, in one call |
| 12 | `BATCH` | a list of the above | up to ~127 operations in one trip |
| 13 | `DAMAGE` | — | the rectangle touched since the last read, and clears it (§5.5) |
| 14 | `BLIT_ALPHA` | `…, alpha` | a blit at one global alpha (§5.3.1) |

Three notes that matter:

- **`SET_FONT` takes the guest's font, it does not carry its own.** The 5×7
  font is 95 glyphs × 8 rows = 760 bytes of `char FONT[]` in `display.c`
  *(checked)*. The OS uploads it once and keeps owning it, so a different font
  is a guest decision and the host has no second copy to drift.
- **`BATCH` is the answer to "a bus trip costs 3.7 µs".** At 32 bytes an
  operation, 4 KB holds about 127 of them, so a console redraw or a whole
  `graphics -clear -rect -disc -text` list is **one** trip.
- **Everything clips, in the device.** That is the same argument
  `lib/pigeon/display.c`'s docstring makes about why the library exists:
  `user/checkerboard.asm` walked off the end of the framebuffer and
  overwrote its own code. A device that clips cannot be talked into
  scribbling outside a surface, whatever the guest asks for.

#### 5.3.1 Alpha blending, in v1 (Q9)

> **Changed in Phase 5** ([plans/phase5_display_lib.md](plans/phase5_display_lib.md)
> §9): alpha 0 is stored like 0xFF, not "nothing". Only 1 to 254 blends.

**Every constant-colour operation above blends on the colour's alpha byte.**
`FILL` with `0x80FF0000` puts half-strength red over what is there;
`0xFFFF0000` is the opaque fill it is today. That is the whole interface — no
mode to set, no state to leave behind, the alpha you already write in the
colour word finally means something.

**How it has to be implemented is part of the design, not an implementation
detail,** because §4.5 measures the obvious version at **0.21 s for a screen**
— slower than the guest code the accelerator exists to replace.

| operation | how | 1280 × 720 |
|---|---|---|
| `a == 255` | the existing slice assignment, untouched | 0.329 ms |
| `a == 0` | return immediately | 0 |
| `FILL`, `FRAME`, `LINE`, `CIRCLE`, `DISC`, `TEXT` at any other `a` | three `bytes.translate` tables, one per channel | 3.651 ms |
| `BLIT_ALPHA` at a global `a` | SWAR: one big-integer multiply per row, even/odd byte lanes | 18.5 ms |
| `BLIT` honouring per-pixel source alpha | **refused in v1** — no table, no single multiply; 0.28 s a screen, 15× the row above | §9 |

Both tricks are pure Python with no new dependency, and the SWAR one is
**verified against a per-pixel reference implementation — the colour bytes
match exactly** *(measured)*. The `a == 255` and `a == 0` tests come first, so
**every colour in the codebase today takes exactly the path it takes now** and
nothing gets slower.

Two consequences worth writing into the device's docstring:

- **Blending reads the destination,** which every other GAC operation does
  not. That makes it the one operation whose cost depends on what is already
  on the surface being a real read, and the one that cannot be reordered
  against another operation touching the same pixels. With a synchronous bus
  (Q11) that is free; it is the reason a queue would stop being free.
- **The alpha byte of the result is the destination's,** not blended. The
  scanout ignores alpha *(checked: `display.h`)*, so there is no meaning to
  give it, and leaving it alone keeps the `translate` trick to three channels
  rather than four.

### 5.4 Scanout: one selector, two sources

The scanout is `(source, target)` where source is `VRAM` (a handle) or `RAM`
(an address). That single change is what keeps the whole existing world alive:

- **A machine boots in `RAM` source at `DISPLAY_START`, 192 × 108** — byte for
  byte what happens today. The BIOS's word-loop clear costs exactly what it
  costs now, `user/screen.asm` runs, `test_bios2.py`'s
  `snapshot() == bytes(DISPLAY_SIZE)` still passes.
- **`CH_DISPLAY = 5` stays exactly as it is** (Q4) and becomes a thin front
  for the same selector: `SET_BASE` is `SCANOUT_RAM`, `GET_BASE` reads it
  back, `FILL` and `COPY` keep working on system RAM. Its `INFO` keeps
  answering the *current mode's* `w, h, size`, which is what `disp_probe`
  already checks against its own `DISP_BYTES` *(checked:
  `lib/pigeon/display.c`)* — so **a `.bin` built for 192 × 108 and run on a
  machine in 640 × 360 falls back to software drawing instead of corrupting
  the heap.** That check was written for exactly this and it already does the
  right thing.
- **The new world is opt-in.** A program asks `CH_VRAM` for a mode and gets
  the aperture; a program that never asks never notices any of this happened.

### 5.5 The host side: front ends and the wire

**`/info` gains `generation`, `format` and `modes`.** Both front ends already
read `w` and `h` from it and have no default geometry *(checked)*, so the
change is: re-read on a generation change and resize the canvas / call
`set_mode` again.

**How they notice.** Rather than polling `/info`, `/frame` answers with the
mode in a header — `X-Pigeon-Mode: 640,360,7` — so the client learns about a
resize in the round trip it was already making, and a client that reads a
frame sized for the old mode has the header to tell it why. (Alternative: a
4-word header on the front of the frame body. The HTTP header is easier to
debug with `curl -I`.)

**The window asks, the guest decides.** A browser or pygame window that is
resized `POST`s `/preferred {w, h}`; the VRAM device stores it and bumps
nothing. The guest reads it with `VRAM_PREFERRED` and may switch, or may
ignore it — a full-screen program in the middle of a frame must not have the
screen change shape under it (Q6). The kernel is the natural place to adopt a
preferred mode: at the prompt, between programs, where it already redraws the
console anyway.

**Serve raw BGRA, swizzle in the client** (Q7). §4.4 says the swizzle is 19%
of a core at 720p and 40% at 1080p, and both front ends are ours: JavaScript
does it in a `Uint32Array` loop in well under a millisecond, and pygame has
`pygame.image.frombuffer` with a format argument. `/info`'s new `format` field
says which order the bytes are in, so an old client and a new server still
agree.

**Damage tracking** (Q10) is the bandwidth answer, and the GAC is the natural
place for it because *it knows what it drew*. Raw aperture stores do not, so
`RAM.write_word` sets a coarse `vram_dirty` flag on any aperture write — one
store, no range arithmetic, exactly the shape of the existing `io_pending`
flag. The rule: **if anything was written through the aperture, send the whole
frame; otherwise send the union of the GAC's rectangles.** Console text,
window furniture and `graphics` calls all go through the GAC, so the common
case gets small frames, and a program doing its own per-pixel work pays full
bandwidth — which is the right way round.

### 5.6 The guest side: `<pigeon/display.h>` on runtime geometry

This is where the work is, and there is a trick that makes most of it
disappear.

```c
extern unsigned disp_w, disp_h, disp_pitch;   /* filled by disp_init() */
extern unsigned disp_base;                    /* the aperture pointer  */

#define DISP_W    disp_w        /* was DISPLAY_W, a compiler predefine */
#define DISP_H    disp_h
#define DISP_BASE disp_base
```

**Every one of the 22 files that says `DISP_W` in an expression keeps
compiling unchanged** — `disp_rect(0, 0, DISP_W, BOX_H, PANEL)` is now a load
instead of an immediate, and that is all. `row_ptr()` becomes

```c
static color_t *row_ptr(unsigned x, unsigned y) {
    return (color_t *)(disp_target + y * disp_pitch + x * 4u);
}
```

which is **still one multiply and one store per pixel**. No bus, no call.

What genuinely breaks is the handful of places that need a *compile-time*
constant — array sizes, `#if`, static initialisers. For those,
`memory_map.py` gains `DISPLAY_MAX_W` and `DISPLAY_MAX_H` (the largest
offered mode), the compiler predefines them as it already predefines
`DISPLAY_W`, and a buffer is sized from the cap and clipped at run time:

```c
#define MAX_COLS (DISPLAY_MAX_W / CELL)
static char row[MAX_COLS + 1];          /* was char row[COLS + 1] */
...
unsigned cols = disp_w / CELL;          /* the real number, this frame */
```

§6 lists every file that needs this. It is five of them.

`disp_init()` probes `CH_VRAM`, takes the mode, and sets the four globals; on
a machine with no VRAM device it sets them from `CH_DISPLAY`'s `INFO`, and on
a bare CPU with no controller at all — which is what `tests/test_libs.py`
builds *(checked)* — it falls back to the compile-time `DISPLAY_W`/`DISPLAY_H`
and software drawing, exactly as `disp_probe` does today.

New calls, all thin covers over the two devices:

```c
int  disp_init(void);                       /* geometry + the aperture      */
int  disp_setmode(unsigned w, unsigned h);  /* 0 if refused                 */
int  disp_modes(disp_mode_t *out, int max); /* what the machine offers      */
int  disp_preferred(unsigned *w, unsigned *h);
unsigned disp_generation(void);             /* changed since you last looked? */
```

`disp_use_back_buffer()` stops calling `malloc` and calls `VRAM_ALLOC`, which
is better in every way: the back buffer leaves the heap, it is the right size
by construction, and `disp_present()` is `VRAM_SCANOUT` — a flip of a handle
rather than of an address that has to be bounds-checked.

### 5.7 The kernel console

`CON_COLS` and `CON_ROWS` become `con_cols` and `con_rows`, computed from
`disp_w / CON_CELL_W` and `disp_h / CON_CELL_H`. `con_grid` and `back_grid`
are sized from the caps:

```c
#define CON_MAX_COLS (DISPLAY_MAX_W / CON_CELL_W)    /* 213 at 1280 wide */
#define CON_MAX_ROWS (DISPLAY_MAX_H / CON_CELL_H)    /*  80 at  720 high */
char con_grid[CON_MAX_COLS * CON_MAX_ROWS];          /* 17,040 B, was 384 */
char back_grid[SCROLLBACK * CON_MAX_COLS];           /* 21,300 B, was 3,200 */
```

That is about 76 KB of extra static data across the four grids at a 1280 × 720
cap, in a kernel that has a 1 MB program region — affordable, and the cap is
`config.json`'s business (Q12), so a machine that only ever runs 320 × 180
pays 320 × 180 prices.

Every `row * CON_COLS + col` becomes `row * con_cols + col`. That is
mechanical, and there are **93 occurrences of `CON_COLS`/`CON_ROWS`** in
`kernel.c` *(checked)* — the largest single mechanical edit in the plan, and
the reason step 6 may want splitting in two.

**`con_redraw` goes through `GAC_BATCH`/`GAC_TEXT`**, which is not an
optimisation but the thing that makes a big console possible at all (§4.3:
34 seconds per redraw otherwise).

**A mode change reflows the console** rather than clearing it: keep the grid,
re-wrap the rows that are now too wide or too narrow, redraw. If that turns
out fiddly, clearing is acceptable and is what most terminals did for years —
but reflow is worth trying first, because the console is where you *see* the
resize work.

### 5.8 What deliberately does not change

- The memory map below the top of RAM, in any respect — and it stays where it
  is when RAM grows, since `PROGRAM_LOAD_ADDR`, `HEAP_START` and
  `DISPLAY_START` are fixed low addresses and only `STACK_TOP` is derived from
  `RAM_SIZE` *(checked)*.
- `CH_DISPLAY`, its five commands, and its tests.
- The boot path: BIOS, boot record, bios2, `PROGRAM_LOAD_ADDR`.
- `0xAARRGGBB` as the colour word — but **`AA` now means something** (§5.3.1,
  Q9). `0xFF…` is byte-for-byte the path it takes today; it is `0x80…` that
  changed, from "a dim colour on black" to "half of one, over what is there".
- The default mode, 192 × 108, unless something asks for another.

---

## 6. What has to change, file by file

**The emulator**

| file | change |
|---|---|
| `emulator/memory_map.py` | `VRAM_SIZE`, `DISPLAY_MAX_W/H`, `CH_VRAM = 9`, `CH_GAC = 10`. **No `ADDR_SPACE` or `VRAM_APERTURE` constant** — both are derived per machine from its RAM size (§5.1, Q2). `DISPLAY_*` stay for the legacy mode |
| `emulator/ram.py` | `vram_base`/`mask` computed in `__init__` from `size`, and **only when there is VRAM**; aperture branch in the four accessors; `vram_dirty`; `display_slice`/`dump_ram` learn about the second buffer |
| `emulator/devices/vram.py` | **new** — §5.2 |
| `emulator/devices/gac.py` | **new** — §5.3, blending included (§5.3.1): the `translate` tables, the SWAR blit, and the `a == 255` / `a == 0` fast paths |
| `emulator/devices/display_io.py` | scanout becomes `(source, target)`; `INFO` answers the live mode; `/info` gains `generation`, `format`, `modes`; `/frame` gains the mode header; `/preferred` endpoint; snapshot reads from VRAM when that is the source |
| `emulator/machine.py` | construct and register the two devices |
| `emulator/config.py` | `ram` and `vram` keys (Phase 1), `display_mode` and `display_modes` (Phase 2) |
| `emulator/cli.py` | `--ram` and `--vram` (Phase 1, §11), `--mode WxH` (Phase 2) |
| `display/index.html` | re-read geometry on a generation change; client-side swizzle; post a preferred size on window resize |
| `display/display.py` | the same three |
| `tools/bench.py` | a GAC line, a per-mode swizzle line, and **a blended-fill line** — §4.5's numbers are the ones most likely to rot |

**The toolchains** — nothing. `memory_map.symbols()` already exports every
uppercase int, so `DISPLAY_MAX_W`, `CH_VRAM` and `CH_GAC` reach both the
assembler and the C compiler the moment they are defined *(checked:
`cc.py:40`, `assembler.py:46`)*. The aperture base deliberately is **not**
among them as a number C may use — §5.1 says why, and `disp_init()` reads it
from `VRAM_INFO` instead.

**The libraries**

| file | change |
|---|---|
| `lib/pigeon/display.h` | `disp_w`/`disp_h`/`disp_pitch`/`disp_base` + the `DISP_W` macro; `disp_init`, `disp_setmode`, `disp_modes`, `disp_preferred`, `disp_generation` |
| `lib/pigeon/display.c` | runtime geometry in `row_ptr` and every clip; GAC calls for clear/fill/blit/scroll/text; back buffer from `VRAM_ALLOC` |
| `lib/pigeon/vram.h` / `.c` | **new** — modes, surfaces, upload/download |
| `lib/pigeon/gac.h` / `.c` | **new** — the primitives, `BATCH`, and `gac_blit_alpha` |
| `lib/pigeon/bmp.c` | load straight into a VRAM surface when one is given |

**The programs that need a real edit** — the five with compile-time array
sizes, plus the kernel:

| file | what |
|---|---|
| `user/os/kernel.c` | `con_cols`/`con_rows`; four grids sized from the caps; **93 `CON_COLS`/`CON_ROWS` sites** *(checked)*; `con_redraw` via the GAC; adopt a preferred mode at the prompt; a `keepscreen`-shaped rule for who owns the mode |
| `user/files.c` | `char row[COLS + 1]` → `MAX_COLS`; `COLS`/`ROWS` runtime |
| `user/disc.c` | the same, plus `line_t.text[COLS + 1]` and `bytes[HEX_PER_ROW * ROWS]` |
| `user/os/installer.c` | `COLS` runtime |
| `firmware/bios2.c` | `COLS`, and `detail[DETAIL]` checked against the cap |
| `user/os/bin/edit.c`, `explorer.c`, `graphics.c`, `img.c`, `splash.c` | expression-only uses — recompile, no edit, **but check each for `memcpy` to `DISPLAY_START`** (`img.c:44` and `graphics.c:169` both do this) |
| `user/demo.c`, `cube.c`, `graph.c` | expression-only — recompile |
| `user/*.asm` | **unchanged and still 192 × 108.** Assembly has no runtime geometry and does not want any; they run in the legacy mode |

**New programs**

- `/bin/setmode.bin` — `setmode`, `setmode 640 360`, `setmode -list`.
- `docs/gac.md` and `docs/vram.md`, the manuals, when it is built.
- A line in `graphics.md` §1 and in `README.md`'s channel table: **`0xAARRGGBB`
  now blends**, and `0x80…` is no longer "half invisible" but "half there".


---

## 8. Risks

- **926 test functions** *(checked)*, and the ones that assert a
  `DISPLAY_SIZE`-shaped frame or index it as 192 wide are the exposure:
  `test_bios2.py:435`, `test_edit.py:73`, `test_display.py` throughout.
  Mitigation: the default mode stays 192 × 108 and the legacy path stays
  byte-identical, so these should pass untouched. If they do not, step 1 is
  wrong and it is worth stopping there.
- **`tests/golden/screen.bin` is a free alarm, and worth knowing about.** It
  is the assembled bytes of `user/screen.asm` *(checked: `test_golden.py:37`)*,
  and that source uses the injected `DISPLAY_START` and `DISPLAY_W` as
  immediates — so **any change to those two constants breaks the golden
  loudly**, which is exactly what should happen. It passing is the evidence
  that the legacy map really did not move.
- **`char row[COLS + 1]` is a real, unavoidable edit** in five files, and the
  compiler will catch every one of them — array sizes must be constant, so
  this fails loudly at build time rather than quietly at run time. That is the
  good kind of breakage.
- **A stale `.bin`.** A program built for 192 × 108 and run at 640 × 360 gets
  `disp_probe`'s size check and drops to software drawing *(checked: the
  `IO_DATAW[2] != DISP_BYTES` test in `display.c`)* — but that check only
  exists in C programs over `display.h`. `user/*.asm` write `DISPLAY_START`
  directly with no check at all, so they must stay in the legacy mode
  (§6) and the mode change must refuse to strand them. The kernel setting the
  mode back to the default when a program ends — `k_tidy` already does this
  for the scanout base *(checked: `kernel.c:1336`)* — is the natural rule.
- **Host memory.** 128 MB RAM + 16 MB VRAM per `Machine`, and the suite
  constructs one in 29 places *(checked)*, several of them per test and in
  parallel under `-n logical`. A `bytearray(16 MB)` is allocated eagerly. If
  the suite slows down or the box runs out of memory, VRAM can be allocated
  lazily on the first `SET_MODE`, at the cost of a branch on the aperture
  path.
- **Bandwidth, not CPU, is the ceiling** (§4.4): 105 MiB/s at 720p, 237 at
  1080p. Damage tracking is the answer and it is step 8, so between steps 4
  and 8 there is a window where 720p works but costs more than it should.
- **Two devices that must agree.** The GAC clips against a surface's geometry
  and VRAM owns that geometry; a mode change between a GAC command and the
  frame it was drawing is a torn frame at worst, because both run on the
  emulator thread and the bus is synchronous. Worth writing down in the
  device docstrings so nobody later makes the GAC asynchronous without
  thinking about it.
- **Console reflow** (§5.7) is the one part of this with no obvious right
  answer. Budget for it being clear-on-resize in the first cut.
- **The aperture base is a number that moves when RAM does** (§5.1, Q2). A C
  program that takes it from the predefined symbol instead of from
  `VRAM_INFO` works perfectly on a 128 MB machine and writes into the middle
  of the heap on a 1 GB one — silently, because the heap is real memory and
  the store succeeds. Mitigation: `disp_init()` is the only thing that reads
  it, and a comment in `display.c` saying so. This is the single nastiest
  failure mode in the plan, and it only exists because RAM can now grow.
- **Blending is the one operation that reads the destination** (§5.3.1), so
  it is the one whose cost the §4.3 table does not describe: 3.65 ms for a
  full-screen blended fill against 0.33 ms opaque. A UI that blends its whole
  background every frame at 720p spends 11% of a 30 FPS frame budget on it.
  That is affordable and worth knowing before someone is surprised by it.
- **`0x80…` changes meaning.** Today a half-alpha colour reaches the canvas
  as-is and looks like a dimmer colour on black; from v1 it blends with what
  is under it. Nothing in the tree uses a non-`0xFF` alpha *(checked: the
  colour constants in `display.h` and the `graphics` docs)*, so this should be
  invisible — but it is a change to a documented contract and `graphics.md`
  §1 says the old rule out loud, so both need updating together (§6).

