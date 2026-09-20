# Phase 3: `CH_GAC`, the accelerator

> Part of [the GAC plan](../README.md). **Status: built, 2026-09-18
> ([§8](#8-as-built)); every question decided ([§7](#7-decisions)).** Needs Phase 2
> (built). Design: [design.md §5.3](../design.md#53-ch_gac--10--the-accelerator)
> and [§5.3.1](../design.md#531-alpha-blending-in-v1-q9); the numbers are §4.3
> and §4.5. Decisions: Q8, Q9, Q10, Q11, Q13 in
> [decisions.md](../decisions.md). Facts marked *(checked)* were read in the
> code, and *(measured)* ones were run on this machine on 2026-09-18.

**The goal:** bulk drawing moves to the host. A full-screen clear at
1280 × 720 becomes one bus command instead of 3.7 million guest instructions,
and a full console redraw becomes about 80 `TEXT` commands instead of
34 seconds (§4.3). **Nothing in the guest uses the GAC yet.** That is
Phase 5. This phase is the device, and tests proving that it draws exactly
what `lib/pigeon/display.c` draws.

---

## 1. What the GAC has to match

`lib/pigeon/display.c` is the reference. Phase 5 routes its calls to the
GAC, and if a single pixel moves when that happens, every screenshot test
in the suite finds out. So each primitive is **the same algorithm**, not
merely a similar one *(checked: `display.c`)*:

| primitive | `display.c` | notes for the GAC |
|---|---|---|
| rect / hline / vline | clip `x >= W` → nothing, then trim `w` to the edge | slice assignment per row |
| frame | two hlines, two vlines | the four corners are drawn twice: **must be once when blending** |
| line | Bresenham, `e2 = 2·err`, `e2 > -dy` then `e2 < dx` | the same loop, pixel by pixel; 0.72 ms for a 1280-pixel line *(measured)* |
| circle | midpoint, `err = 1 - r`, 8 points per step | octant joins repeat a pixel: **once when blending** |
| disc | the circle's loop, 4 spans per step | spans repeat rows: **each row once, at its widest** |
| char / text | 5 × 7 font in a 6-wide cell, top 5 bits of each row byte, ink only, advance `GLYPH_W + 1` | the kernel's inverse cells draw a rect and then a glyph in the background colour *(checked: `kernel.c` `con_paint`)* |
| scroll | full-width band, move `kept` rows, fill the gap | one slice move, one fill |
| clear | device `FILL` of a whole screen | `FILL` of the surface |

**The repeated pixels matter only once blending exists.** Storing a pixel
twice is harmless, but blending it twice at 50% gives 75%. So the GAC
computes each shape's *set* of pixels (rows for a disc, points for a circle
and a frame) and draws each one once. For opaque colours the result is
byte-identical to `display.c`; for blended ones it is right.

---

## 2. The device

`emulator/devices/gac.py`, `CH_GAC = 10` (Q13). Like `CH_VRAM`, **every
argument is a word in the data window, read with R/W 0**, and every command
answers at least one word. Colours are `0xAARRGGBB`. Coordinates are
**signed** 32-bit (Q1 below).

### 2.1 Surfaces

A command names its surfaces by handle:

- **`0`**: the screen of the current mode (VRAM surface 0).
- **a VRAM handle**: anything `CH_VRAM`'s `ALLOC` handed out. The GAC looks
  its geometry up in the VRAM device every time and **never keeps a copy**,
  so a `FREE` or a mode change cannot leave it drawing into memory that was
  handed to someone else (§8, "two devices that must agree").
- **a RAM surface**: `RAM_SURFACE [address, w, h]` registers a rectangle of
  system RAM and answers a handle for it, **`0x80000000` and up** so it can
  never collide with a VRAM handle. **This replaces §5.3's "handle
  `0xFFFFFFFF` plus the geometry in the window" (Q2).** It is checked once
  when registered, with the rule `CH_DISPLAY` and the HDD already use: wholly
  in RAM, and at or above `PROGRAM_LOAD_ADDR`, **or exactly `DISPLAY_START`
  with at most `DISPLAY_SIZE` bytes**. That last case matters: at power-on the
  screen *is* `DISPLAY_START`, and the kernel console must be able to use the
  GAC there (Phase 6). A RAM surface can never reach the IO window, the BIOS
  or past the end of RAM, whatever the guest asks for.

### 2.2 Commands

| cmd | name | window | reply | |
|---|---|---|---|---|
| 0 | `NOP` | — | `0` | |
| 1 | `INFO` | — | `magic, features, window_bytes` | magic `"PGGA"`; features bit 0 text, bit 1 blending |
| 2 | `FILL` | `dst, x, y, w, h, colour` | `1` / `0` | `0` only for a bad handle; clipped to nothing is `1` |
| 3 | `FRAME` | `dst, x, y, w, h, colour` | `1` / `0` | an outline, each pixel once |
| 4 | `BLIT` | `src, sx, sy, dst, dx, dy, w, h` | `1` / `0` | a copy; overlap-safe; src and dst may be the same surface |
| 5 | `BLIT_SCALED` | `src, sx, sy, sw, sh, dst, dx, dy, dw, dh` | `1` / `0` | nearest neighbour, `bmp.c`'s mapping `i * size / n` *(checked: `bmp.c:104`)* |
| 6 | `LINE` | `dst, x0, y0, x1, y1, colour` | `1` / `0` | |
| 7 | `CIRCLE` | `dst, cx, cy, r, colour` | `1` / `0` | an outline |
| 8 | `DISC` | `dst, cx, cy, r, colour` | `1` / `0` | filled |
| 9 | `SET_FONT` | `address, glyph_w, glyph_h, cell_w, cell_h, first, count` | `1` / `0` | copies the font out of RAM; one byte per glyph row, top bit first, `glyph_w <= 8` |
| 10 | `TEXT` | `dst, x, y, fg, bg, length` + the bytes | `1` / `0` | advances `cell_w`; `bg` with alpha 0 means "no background" (Q3) |
| 11 | `SCROLL` | `dst, x, y, w, h, dy, bg` | `1` / `0` | `disp_scroll` for any band, not only a full-width one |
| 12 | `BATCH` | `count` + records | `ran, refused` | §2.3 |
| 13 | `DAMAGE` | — | `0` | reserved; Phase 8 |
| 14 | `BLIT_ALPHA` | `src, sx, sy, dst, dx, dy, w, h, alpha` | `1` / `0` | 3b only |
| 15 | `RAM_SURFACE` | `address, w, h` | `handle` or `0` | §2.1 |
| 16 | `RAM_FREE` | — (handle in ADDRESS) | `1` / `0` | |

**`TEXT` in one call, 80 calls for a 1280 × 720 console:** a line of 213
characters is 213 bytes plus 6 words, far inside the 4 KB window.

### 2.3 `BATCH`

The window holds `count`, then `count` records, each `cmd, nwords,
args…`, where `nwords` counts the argument words (a `TEXT` record's bytes are
padded to whole words). **The whole batch is checked before anything is
drawn** (Q4): an unknown command, `BATCH` inside a `BATCH`, or a record
running off the end of the window refuses the batch, answers `0, count`, and
draws nothing. A well-formed batch then runs in order. A record that fails
at run time (a bad handle) is skipped and counted. The answer is how many ran
and how many were refused.

This is the same rule `graphics.bin` already follows for its argument list,
"a mistake anywhere draws nothing at all" *(checked: `test_graphics.py`)*, one
level down.

---

## 3. How each primitive is drawn, and what it costs

All of it is pure Python on the `bytearray` of the surface: `ram.vram` or
`ram.mem`. The GAC writes those buffers directly, as `CH_DISPLAY`'s `FILL`
already does *(checked)*. **It does not set `vram_dirty`.** That flag means
"written through the aperture, damage unknown". The GAC will report its own
damage in Phase 8.

- **`FILL`, `SCROLL`, `BLIT`:** a slice per row, or one slice when the
  rectangle spans full rows. Measured: a full 1280 × 720 fill 0.288 ms, a
  1280 × 700 blit 0.258 ms (§4.3).
- **`BLIT` overlapping itself:** rows are copied bottom-up when moving down
  and top-down otherwise. The right-hand slice is already a copy, so within a
  row the direction never matters.
- **`LINE`, `CIRCLE`:** the `display.c` loops, a 4-byte slice per pixel,
  clipped per pixel. 0.72 ms for a 1280-pixel line *(measured)*: per-pixel
  work is fine when there are only a line's worth of pixels.
- **`DISC`:** the circle loop collects, for each row, the widest span, then
  draws each row once as one slice.
- **`TEXT`:** `SET_FONT` precomputes, for every glyph and every glyph row,
  that row's mask as bytes (`FF FF FF FF` where there is ink). A string is
  drawn **one pixel row at a time across the whole string**: join the
  glyphs' row masks into one big integer `M`, then
  `row = (dst & ~M) | (fg & M)`, or `(bg & ~M) | (fg & M)` with a background.
  That is 8 big-integer operations per line of text, not 8 per glyph.
  *Measured:* a full 213 × 80 console at 1280 × 720 in **11.8 ms** ink-only and
  **12.6 ms** with a background. It **matches a per-pixel reference exactly**.
  (Shifting glyphs into a growing integer instead took 97.6 ms. The joined
  bytes are what make it fast.)

---

## 4. Phase 3b: blending (Q9)

Every constant-colour command, which is all of them except `BLIT` and
`BLIT_SCALED`, blends on its colour's alpha byte. `BLIT` copies bytes
verbatim, alpha included.

1. **`a == 255` first:** the opaque path above, untouched. `a == 0`:
   nothing, answer `1`. **Every colour in the tree today is `0xFF…`**
   *(checked)*, so nothing that draws today gets slower.
2. **Otherwise, three `bytes.translate` tables**, one per colour channel,
   built once per command: `t[d] = (d·(255−a) + s·a + 127) // 255`. A span
   is blended by translating its B, G and R strided slices. The alpha byte is
   left as it was (§5.3.1). 3.65 ms for a full 720p screen (§4.5).
3. **Shapes with scattered pixels** (`LINE`, `CIRCLE`, `FRAME`'s sides) blend
   pixel by pixel through the same three tables. That is three lookups a
   pixel, on a line's worth of pixels.
4. **`TEXT` with a translucent colour:** translate the row to get it fully
   blended, then merge it under the mask as above. The same 8 operations per
   line, plus the translates.
5. **`BLIT_ALPHA`:** one global alpha, the SWAR big-integer blend from §4.5,
   18.5 ms for a full 720p screen. **Checked against the per-pixel formula
   in the tests, byte for byte.**
6. **Per-pixel source alpha is refused:** there is no command for it, and
   §9 says why.

The rounding in (2) is the one decision the formula needs. `+ 127` rounds to
nearest, so a blend of a colour with itself is that colour exactly, and 50%
of black over white is `0x80`, not `0x7F`.

---

## 5. Steps

Each step ends with its tests passing. Following your note in memory, the
full suite runs only at the end of 3a and of 3b.

**Phase 3a: opaque drawing**

1. **The device and its surfaces.** `CH_GAC` in `memory_map.py`, `gac.py`
   with `NOP`, `INFO`, `RAM_SURFACE` and `RAM_FREE`, and handle resolution:
   screen, VRAM handle, RAM handle. Registered in `Machine` only when there
   is video memory, as `CH_VRAM` is (Q5).
2. **`FILL`, `FRAME`, `SCROLL`**, with clipping on every edge, signed
   coordinates included.
3. **`BLIT`, `BLIT_SCALED`**, overlap both ways, and between RAM and VRAM
   surfaces.
4. **`LINE`, `CIRCLE`, `DISC`.**
5. **`SET_FONT`, `TEXT`.**
6. **`BATCH`.**
7. **The equality test (§6).** `display.c` and the GAC draw the same scene;
   the bytes must match.
8. **`tools/bench.py`** gains a GAC line (a full-screen fill and a full
   console of text at 1280 × 720). **Full suite.**

**Phase 3b: blending**

9. **The fast paths and the tables:** `FILL`, `FRAME`, `LINE`, `CIRCLE`,
   `DISC`, `TEXT` at any alpha.
10. **`BLIT_ALPHA`**, SWAR.
11. **Tests against the per-pixel formula**, and one per shape proving each
    pixel is blended once. `tools/bench.py` gains a blended-fill line.
    **Full suite.**

**Done when:** every command in §2.2 is built and tested; the equality test
passes; the bench is within reach of §4.3 and §4.5; and the full suite
passes.

---

## 6. Tests: `tests/test_gac.py`

- **The equality test, the one that matters most.** One C program draws a
  scene with `display.c` on a bare CPU, the way `test_libs.py` runs it, so it
  takes the software path into `DISPLAY_START` *(checked)*. The scene has
  rects, frames, lines in all eight octants, circles and discs half off
  every edge, text with descenders, and a scroll up and down. The GAC then
  draws the same list into a RAM surface of the same size. **The two
  framebuffers must be equal byte for byte.** It is one compile, so it is
  cheap.
- Every command's refusals: an unknown handle, a freed VRAM handle, a RAM
  surface that would reach the IO window or past RAM, a font past RAM,
  `glyph_w > 8`.
- **Nothing is written outside the target rectangle.** Every drawing test
  compares the whole buffer, not only the pixels it expects.
- Clipping on all four edges, and with negative coordinates.
- `BLIT` overlap in all four directions, and RAM ↔ VRAM.
- `BATCH`: runs in order, a malformed batch draws nothing, and a bad handle
  is skipped and counted.
- 3b: the translate tables and SWAR against the per-pixel formula on
  random data; `a == 255` byte-identical to opaque; `a == 0` writes nothing;
  a translucent disc, circle and frame blend each pixel exactly once.
- **Memory:** every test uses a small RAM, as `test_vram.py` does now. That
  was the lesson of the crash.

---

## 7. Decisions

Answered 2026-09-18: *"lets do all recommendation"*, written into this
section's title as `Questions - ALL RECOMMENDATION`. Every recommendation
stands, and the body above already says what each one means.

1. ~~**Coordinates: signed, with real clipping?**~~ `display.c` takes rect
   coordinates unsigned, so a rect at x = −5 wraps to a huge x and draws
   **nothing** *(checked)*. `graphics.c` works around that by clipping
   before it calls *(checked: `graphics.c:126`)*. **Recommendation:** the GAC
   takes signed coordinates and draws the visible part. When Phase 5 routes
   `disp_rect` here, a rect half off the left edge starts drawing its
   visible part. That is a fix, not a regression, and nothing in the tree
   relies on the old behaviour (`graphics.c` clips first). The equality test
   keeps to on-screen rects for that one case.

   **Decided (you), 2026-09-18:** the recommendation.

2. ~~**RAM surfaces as registered handles, instead of `0xFFFFFFFF` plus the
   geometry in every command?**~~ §5.3 had every command carry a RAM surface's
   address and size inline. **Recommendation:** register once with
   `RAM_SURFACE` and get a handle. Every command then has a fixed shape, the
   range is checked once instead of on every call, and `BATCH` records stay
   short.

   **Decided (you), 2026-09-18:** the recommendation.

3. ~~**`TEXT`'s background: alpha 0 means "none"?**~~ `disp_text` draws ink
   only, and the kernel draws a rect first when it wants a background
   *(checked)*. **Recommendation:** `TEXT` takes `bg` anyway, and `bg` with
   alpha 0 means "leave what is there". That is exactly what blending would
   do with it, so it is not a special case. A console line can then be
   redrawn with its background in one call instead of a rect plus a text.

   **Decided (you), 2026-09-18:** the recommendation.

4. ~~**`BATCH` checks everything before drawing anything?**~~
   **Recommendation:** yes for its *shape* (unknown command, overrun,
   nested batch); no for run-time refusals (a bad handle is skipped and
   counted). The first is a bug in the program that built the batch. The
   second can happen legitimately, for example a surface freed between
   building and sending.

   **Decided (you), 2026-09-18:** the recommendation.

5. ~~**`CH_GAC` only on machines with video memory?**~~ **Recommendation:**
   yes, like `CH_VRAM`. With `--vram 0` there are no VRAM surfaces, so the
   GAC could still draw into RAM surfaces, but a machine without video memory
   is "the machine from before this plan" and should stay exactly that.

   **Decided (you), 2026-09-18:** the recommendation.

6. ~~**One font, or several?**~~ The OS has one font, and `display.c` and the
   kernel share it *(checked)*. **Recommendation:** one font slot now. A
   `font` argument can be added to `SET_FONT` and `TEXT` later without
   renumbering anything, since a new command is cheaper than a slot nobody
   uses.

   **Decided (you), 2026-09-18:** the recommendation.

7. ~~**Commits.**~~ Phases 1 and 2 are still uncommitted, and so are the
   `gui.pgs` demo and its disc entry, which is why two installer tests
   expect 32 files and find 33. **Recommendation:** before starting, commit
   Phases 1 and 2 on `graphics` as one commit. Commit your `gui.pgs` work
   separately, with the two tests' count bumped to 33. Then commit 3a and 3b
   each on their own. Or tell me to leave all committing to you.

   **Decided (you), 2026-09-18:** the recommendation, and done: `1730bdd`
   (`gui.pgs`, with the tests at 33), `eade0f2` (the `ADD` experiment, as you
   left it), `c9a17ef` (Phases 1 and 2), `b7ef940` (this plan).

---

## 8. As built

Built 2026-09-18: 3a in `354dcfc`, 3b in the commit after it.
`emulator/devices/gac.py` is new, and its docstring's command table is the
reference. `CH_GAC = 10` is in `memory_map.py`, and `Machine` registers it
beside `CH_VRAM`. The design is as §2 to §4 above, with these additions:

- **`LINE`, `CIRCLE` and `DISC` refuse anything over 2¹⁷ pixels** (answer
  `0`). Coordinates are signed 32-bit, and Bresenham walks every pixel
  whether or not it is visible, so a guest asking for a line from −2³¹
  would hold the emulator for hours. 131,072 pixels is a hundred times the
  widest screen. Not in the plan; found while writing the loop.
- **`BLIT_SCALED` answers `0` when its source rectangle is not wholly
  inside the source surface.** The destination clips as usual. Clipping the
  source would change which pixels `bmp.c`'s mapping picks, so the request
  is refused instead of drawn wrongly.
- **The window is copied (4 KB) at the start of each command.** Nothing a
  command draws can then change the arguments it is still reading, and a
  `BATCH`'s records are plain bytes.
- **Blending is one object per colour, not a flag.** `ink(colour)` answers
  an opaque `Ink`, a `ClearInk` for alpha 0, or a `BlendInk` with its three
  translate tables. Every primitive calls `span`/`pixel`/`over` on
  whichever it got, so no primitive knows which kind it has, and the opaque
  path is exactly 3a's.
- **The SWAR division is `(x + 1 + (x >> 8)) >> 8`.** It is checked to
  equal `x // 255` for every x a blend can produce (0 to 65,152), so
  `BLIT_ALPHA` is byte-for-byte the per-pixel formula, as the tests confirm.
  Rows wider than 4,096 pixels are blended in pieces.

**Measured** (`tools/bench.py`, 1280 × 720, through the device's callback):

| | here | the plan's figure |
|---|---|---|
| full-screen `FILL`, opaque | 0.64–0.71 ms | 0.288 ms (the bare slice) |
| full-screen `FILL`, alpha 0x80 | 4.7 ms | 3.65 ms (§4.5) |
| full-screen `BLIT_ALPHA` | 25 ms | 18.5 ms (§4.5) |
| a whole 213 × 80 console of `TEXT`, with a background | 17–19 ms | 12.6 ms (§3) |

Each is a little over the bare figure, which timed the inner operation alone
and not the bus call, the window copy and the per-row loop around it. The
opaque fill builds the whole screen's colour bytes each call (3.6 MB). That
could be cached, as `CH_DISPLAY`'s fill does, if Phase 8's measurements say
it matters.

**Tests:** `tests/test_gac.py`, 86 cases. **The equality test passes on
three scenes**: a fixed one covering every primitive, every edge, text with
descenders and scrolls both ways, and two seeded random ones of 60 shapes.
`display.c` and the GAC agree on every byte. Each blend is checked against
the per-pixel formula. Each shape is checked to blend each pixel exactly
once, and that test does catch the fault: swapping in `display.c`'s
overlapping disc makes it fail with pixels blended up to six times. The
largest file's peak is 324 MB, the same as `test_display.py`.

**The suite:** 1,815 passed, none failed.

**For Phase 5:** `display.h`'s comment *"Alpha is NOT blended"* is still
true of the library's software path, and becomes false for anything routed
to the GAC. It changes with `graphics.md` §1 in Phase 7, as §6 of the design
says.

