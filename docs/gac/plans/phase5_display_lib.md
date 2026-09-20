# Phase 5: `<pigeon/display.h>` on runtime geometry, drawing through the GAC

> Part of [the GAC plan](../README.md). **Status: built, 2026-09-18
> ([§9](#9-as-built)); every question decided ([§8](#8-decisions)).** Needs
> Phases 2 and 3 (built). Design:
> [design.md §5.6](../design.md#56-the-guest-side-pigeondisplayh-on-runtime-geometry)
> and §6's library table. Decisions: Q2, Q5, Q6 in
> [decisions.md](../decisions.md). Facts marked *(checked)* were read in the
> code.

**The goal:** the C library asks the hardware how big the screen is instead
of being told at compile time, and hands its drawing to the GAC. **A pixel is
still one store**, `*row_ptr(x, y) = c`. Everything else (clears, rects,
lines, circles, text, scrolls, images) becomes one bus command each.

**What you will be able to see at the end of this phase:** the same pictures
as today, pixel for pixel, drawn much faster. The kernel console is drawn by
the library, so its redraw goes from about a million guest instructions to a
few hundred bus commands. `cube`, `graph`, `files`, the explorer and every
`graphics` script get faster without a change to their drawing code.
Translucent colours start working in programs that use them. Big modes are
still Phase 6 and 7: nothing *switches* the mode yet, except a program
calling the new `disp_setmode`, which none does.

---

## 1. What planning found that the original sketch missed

Three things, all *(checked)*, and each changes the phase's shape.

1. **`DISP_W` cannot become a variable on its own.** Seven programs size
   arrays with it, through `COLS` or `BOX_COLS`: `user/files.c`,
   `user/disc.c`, `user/os/bin/explorer.c`, `user/os/installer.c`,
   `firmware/bios2.c`, `user/graph.c` and `user/os/bin/graph-corrupt.c`.
   pigeon-cc folds array sizes at parse time and rejects anything else
   (`parser.py`'s `_constant_expression`). So the moment `#define DISP_W
   disp_w` lands (Q5), those seven stop compiling. The design listed five of
   them, and scheduled them for Phase 6a. **They have to come into this
   phase** (Q1 below).
2. **Nothing frees what a program allocates on the devices.** A back buffer
   from `VRAM_ALLOC`, or a RAM surface registered with the GAC, outlives the
   program that asked for it. Programs end by `exit`, by a fault, or by
   Ctrl+C, and only the kernel sees all three. The kernel already does
   exactly this for files: `handle_depth[]`, and `k_tidy` closes every
   handle opened at the ending program's depth or deeper *(checked:
   `kernel.c:1322`)*. Without the same for surfaces, a few runs of `cube`
   would fill 16 MB of video memory (Q4).
3. **Once a program can call `disp_setmode`, the kernel must put the mode
   back** when it ends (Q6: "the kernel puts it back when that program
   ends"), and **a reboot must too**. `/bin/reboot.bin` just jumps to
   address 0 *(checked)*, so after a mode change the BIOS would draw at
   `DISPLAY_START` while the screen shows video memory. The original plan had
   both in Phase 6 (6b.4, and the gap noted in Phase 2's "As built"). They
   belong where `disp_setmode` appears, which is here.

---

## 2. The library

### 2.1 `lib/pigeon/vram.h` / `vram.c` and `lib/pigeon/gac.h` / `gac.c`

Thin covers over the two devices, one function per command, in the style of
`<pigeon/cd.h>`: probe with the magic word, and answer "not here" on a
machine without the device or on a bare CPU. `gac.c` also has a small
`BATCH` builder: records appended into a buffer, sent with one call.

### 2.2 Runtime geometry in `display.h`

```c
extern unsigned disp_w, disp_h;          /* the screen, from disp_init() */
extern unsigned disp_base;               /* where it is: RAM, or the aperture */
#define DISP_W    disp_w                 /* Q5: every expression keeps compiling */
#define DISP_H    disp_h
#define DISP_BASE disp_base
```

pigeon-cc supports `extern` across units compiled together *(checked:
`analyzer.py:_global`)*. The globals **start as `DISPLAY_W`, `DISPLAY_H`
and `DISPLAY_START`**, so a program is right at the power-on mode even
before it asks. The pitch is always `disp_w * 4`, so there is no
`disp_pitch`.

### 2.3 `disp_init()`

It asks, in order:

1. **`CH_VRAM`**: the mode, the aperture base from `INFO` (never from a
   constant, Q2), and what is on screen. At the power-on mode the screen is
   RAM at `DISPLAY_START`. That is registered as a GAC RAM surface, so the
   GAC draws where the BIOS and the kernel draw. Otherwise it is VRAM
   surface 0 through the aperture.
2. **`CH_DISPLAY`** only, on a machine with `--vram 0`: the geometry from its
   `INFO`, and software drawing with its fill and copy, exactly as today.
3. **Nothing**, on a bare CPU (`tests/test_libs.py`): the compile-time
   `DISPLAY_W` × `DISPLAY_H` at `DISPLAY_START`, and software drawing.

With the GAC it also uploads the library's font with `SET_FONT`. That is 760
bytes, once per program (Q3 of Phase 3: the guest's font, not the host's).
Every `disp_*` function calls `disp_init()` lazily the first time, and
programs call it explicitly at the start (Q2 below).

### 2.4 What goes to the GAC

| call | with the GAC | without |
|---|---|---|
| `disp_set`, `disp_get` | **a store / a load, as now** | the same |
| `disp_clear` | `FILL` of the screen | `CH_DISPLAY` `FILL`, or a loop, as now |
| `disp_rect`, `disp_hline`, `disp_vline` | `FILL` (above a size threshold, Q3) | the loops, as now |
| `disp_frame`, `disp_line`, `disp_circle`, `disp_disc` | `FRAME`, `LINE`, `CIRCLE`, `DISC` | as now |
| `disp_char`, `disp_text` | `TEXT`: a whole string in one call | as now |
| `disp_scroll` | `SCROLL` | `CH_DISPLAY` `COPY`, as now |
| **`disp_blit(pixels, x, y, w, h)`**, new | `BLIT` from a registered RAM surface | a `memcpy` a row at a time |

**`disp_blit` replaces the two hand-written copies to `DISPLAY_START`**
(`graphics.c:169` and `img.c:44` *(checked)*), which would be wrong in any
other mode. `bmp.c` does not change: it decodes into the heap as now, and
`disp_blit` puts the result on screen (Q6).

**Colours with alpha below `0xFF` always go to the GAC, whatever the size,
so a translucent colour blends the same at every size.** Without a GAC they
are stored as they are and do not blend (Q5).

### 2.5 Back buffers and modes

- `disp_use_back_buffer()` asks `VRAM_ALLOC` for a surface of the screen's
  size. `disp_present()` stays the page flip it is. `CH_DISPLAY`'s
  `SET_BASE` already takes both `DISPLAY_START` and aperture addresses
  (Phase 2), so the existing flip code needs only the GAC handle of the
  surface being drawn to follow it. Without video memory, the heap buffer as
  now.
- New: `disp_setmode(w, h)`, `disp_modes(...)`, `disp_generation()`.
  `disp_setmode` re-reads everything `disp_init` read, and drops a back
  buffer of the old size.

---

## 3. The devices and the kernel: who owns a surface

Kept as small as the kernel's file handles, and shaped the same (Q4):

- **`CH_VRAM` gains `OWNER n` and `FREE_OWNED n`.** Every surface
  allocated, and every RAM surface the GAC registers, is tagged with the
  current owner. `FREE_OWNED n` frees every surface tagged `n` or deeper, in
  both devices. The screen (surface 0) is never tagged and never freed.
- **The kernel's `k_run` sets `OWNER depth`** before it starts a program.
  **`k_tidy` calls `FREE_OWNED depth`,** beside the file handles, and then
  puts the mode back if it changed (Q6) *before* its existing
  `SET_BASE DISPLAY_START`. The order matters: freeing a back buffer that
  is on screen puts the scanout on VRAM's screen, and the `SET_BASE` after it
  brings it back to `DISPLAY_START`.
- **`reboot.bin` sets 192 × 108 and the RAM scanout** before it jumps to
  address 0.

That is the only emulator change in this phase: two commands and a tag.

---

## 4. The programs

**The seven with compile-time sizes** (§1.1): every `char row[COLS + 1]`
becomes `char row[MAX_COLS + 1]`, with `MAX_COLS` from `DISPLAY_MAX_W`, the
cap Phase 2 put in the memory map and the compilers already predefine. `COLS`
stays as it is, `(DISP_W / CELL)`, and is now worked out at run time. That is
the design's §5.6 pattern, applied mechanically.

**`disp_init()` at the top of every program that draws** (Q2). Twelve
include `display.h` *(checked)*: `cube`, `demo`, `disc`, `files`, `graph`,
`graph-corrupt`, `explorer`, `graphics`, `pgs`, the installer, `bios2` and
the kernel.

**Two more reach the screen without it** *(checked)*. They use
`DISPLAY_START` and `DISPLAY_W` directly, which is right only at the
power-on mode, so they move onto `display.h`:

- `img.c` copies a decoded BMP to `DISPLAY_START` with one `memcpy`, and
  becomes a `disp_blit`.
- `splash.c` copies its picture there, then flashes the eyes by writing
  pixels through `(unsigned *)DISPLAY_START`. It becomes a `disp_blit` and
  writes through `DISP_BASE`, which is still one store a pixel.

**`graphics.c`** calls `disp_blit`. **`files.c`, `explorer.c` and the
kernel** each use a raw `DISPLAY_START` or `DISPLAY_W` in one or two places
*(checked)*. Each is looked at and moved to `DISP_BASE`/`DISP_W` where it
means "the screen".

Nothing else in them changes. They still lay themselves out for whatever
`DISP_W` says, which is 192 × 108 until Phase 7 lets something switch.

---

## 5. Steps

1. **`vram.h`/`.c` and `gac.h`/`.c`**, with tests in `test_libs.py`'s style:
   a program compiled with them, run on a machine with the devices, and on a
   bare CPU where each says "not here".
2. **The ownership tags** in `vram.py` and `gac.py`, with `test_vram.py` /
   `test_gac.py` cases.
3. **`display.h`/`display.c`: the globals and `disp_init`.** Still drawing in
   software. This step alone moves `DISP_W` to a variable, **so it lands
   together with step 4**, or the tree does not compile.
4. **The seven programs' array sizes**, and `disp_init()` in every program.
   **Full suite**: pictures unchanged, still software.
5. **Routing to the GAC** (§2.4), one family at a time: fills, then shapes,
   then text, then scroll. The threshold for small opaque fills is measured
   here (Q3), and a guest instruction count goes in "As built".
6. **`disp_blit`, and `graphics.c` / `img.c` on it.**
7. **Back buffers from VRAM**, `disp_setmode`, `disp_modes`,
   `disp_generation`.
8. **The kernel:** `OWNER`/`FREE_OWNED` in `k_run`/`k_tidy`, the mode put
   back, and `reboot.c`.
9. **The equality test at the machine level (§6)**, the bench, and the
   **full suite**.

**Done when:** every test that compares a screen passes unchanged (the
pictures did not move); the equality test passes; a program that allocates
and crashes leaves no surface behind; and the full suite passes.

---

## 6. Tests

- **The pictures must not move.** Every existing screenshot-style test
  (`test_graphics.py`, `test_files.py`, `test_explorer.py`, `test_disc.py`,
  `test_edit.py`, `test_pgs.py`, `test_bios2.py`, the cube and graph tests)
  now runs through the GAC. **If they pass unchanged, the GAC draws what the
  software drew.** That is the strongest evidence there is, and it costs
  nothing. Phase 3's equality test already proved the primitives one by one.
- **A new equality test at the machine level:** one C program drawing a
  scene with the library, run on a bare CPU (software) and on a machine with
  the GAC. The two screens must be equal byte for byte.
- `test_libs.py`'s display tests run on a bare CPU, so they cover the
  software path, which must stay exactly as it was.
- **Ownership:** a program that allocates a back buffer and registers a
  surface, then exits; the same with a fault; the same with Ctrl+C. After
  each, the devices hold nothing of it. A program run by a program: the
  inner one's surfaces go when it ends, and the outer one's stay.
- The mode put back by `k_tidy`, and by `reboot.bin`.
- Speed: the kernel's `con_redraw` counted in guest instructions before and
  after, and `tools/bench.py`'s real-program line.

---

## 7. Risks

- **Every screen in the suite goes through new code at once.** That is the
  point, and also the risk. The mitigation is that Phase 3's equality test
  already proved each primitive against `display.c`, byte for byte, so a
  failure here is most likely in the routing, which is small.
- **Rects with negative coordinates change** (Phase 3, Q1): a rect half off
  the left edge now draws its visible part instead of nothing. `graphics.c`
  already clips before calling *(checked)*, so no picture in the tree should
  change. If one does, the test says where.
- **The font is one slot shared by every program** (Phase 3, Q6). Every
  program re-uploads the same library font, so they agree. A program that
  set a different font would leave it for the kernel's console after it
  ends. None does; `k_tidy` can re-upload it if one ever does.
- **The lazy `disp_init` is a safety net, not the rule.** A program that
  reads `DISP_W` before drawing anything, without calling `disp_init()`,
  sees the power-on size. That is right today, and wrong only after Phase 7
  starts other programs in other modes, which is why every program gets the
  explicit call now.

---

## 8. Decisions

Answered 2026-09-18: *"all recommendations, start building phase 5"*. Every
recommendation stands.

1. ~~**Bring Phase 6a (the programs with compile-time sizes) into this
   phase?**~~ **Recommendation:** yes, because it cannot be otherwise. With
   `DISP_W` a variable, the seven programs in §1.1 do not compile, and the
   tree would be broken between phases. Phase 6 becomes the kernel console
   alone, plus the mode picker.

   **Decided (you), 2026-09-18:** the recommendation.

2. ~~**`disp_init()` explicitly at the top of every program, with a lazy
   call as a safety net?**~~ **Recommendation:** yes. Lazy alone works for
   drawing, but a program that reads `DISP_W` to lay itself out before
   drawing anything would see the power-on size. That is harmless now and
   silently wrong once programs start in other modes. It is one line in
   fourteen files.

   **Decided (you), 2026-09-18:** the recommendation.

3. ~~**Small opaque fills in software, big ones on the GAC?**~~ A bus call costs
   the guest roughly 25 instructions plus the host's work, while a software
   fill costs about 4 instructions a pixel. So a 3-pixel `hline` is cheaper
   in software. **Recommendation:** opaque fills under a threshold stay in
   software. The threshold is measured in step 5 (around 16 pixels is my
   guess). Everything translucent, and every other shape, goes to the GAC.
   The alternative, the GAC for everything, is simpler and slightly slower
   for tiny shapes.

   **Decided (you), 2026-09-18:** the recommendation.

4. ~~**Surfaces owned by depth, freed by the kernel, like file handles?**~~
   **Recommendation:** yes, with the two commands on `CH_VRAM` in §3. The
   alternatives are worse: the library freeing on exit misses faults and
   Ctrl+C, and never freeing fills video memory in a few runs. This also
   brings the "put the mode back" rule (Q6) and the reboot fix into this
   phase, because they are the same tidy-up.

   **Decided (you), 2026-09-18:** the recommendation.

5. ~~**Without a GAC, translucent colours are stored, not blended?**~~
   **Recommendation:** yes, and say so in `display.h`. The software path
   exists for bare-CPU tests and `--vram 0` machines. A software blend
   would cost dozens of instructions a pixel, to serve two cases that never
   use translucency.

   **Decided (you), 2026-09-18:** the recommendation.

6. ~~**Images through `disp_blit`, leaving `bmp.c` alone?**~~ The design had
   `bmp.c` load straight into a VRAM surface. **Recommendation:** no. A
   `disp_blit(pixels, x, y, w, h)` covers the two programs that copy images
   today, works in any mode and on every machine, and `bmp.c`'s tests stay
   as they are. Loading into VRAM saves one copy of an image, which nothing
   here needs yet.

   **Decided (you), 2026-09-18:** the recommendation.

---

## 9. As built

Built 2026-09-18, as §2 to §5 describe, with every recommendation. What
differs from the plan, and what building it found:

**Alpha 0 is stored, not invisible.** This changes Phase 3's device. Once
`display.c` drew through the GAC, bios2's `disp_clear(0)` stopped clearing
the screen, because under §5.3.1 a colour with alpha 0 drew nothing. Code
has always used `0` to mean black. The screen has ignored alpha since
Phase 4, so "alpha 0 is invisible" helped nobody and broke existing code.
**Now only alpha 1 to 254 blends; 0x00… and 0xFF… are stored as they are.**
The one "nothing" left is `TEXT`'s background, where alpha 0 still means
"no background" (Phase 3's Q3). `BLIT_ALPHA`'s own alpha argument keeps 0 as
"nothing", since there it is an amount, not a colour. `gac.py`'s docstring
and `test_gac.py` changed with it.

**The threshold for small fills is 8 pixels, not 16** (decision 3),
**measured**: on a `Machine`, a GAC `FILL` costs about 84 µs whatever its
size, about 240 guest instructions plus the host's work. A stored pixel
costs about 10 µs. They meet at 8 pixels:

| width | GAC | stores |
|---|---|---|
| 1 | 99 µs | 22 µs |
| 4 | 83 µs | 44 µs |
| 8 | 84 µs | 79 µs |
| 16 | 86 µs | 150 µs |
| 64 | 83 µs | 555 µs |

**The console redraws about 4.5 times faster**: a half-full screen (190
cells of text) went from **470,983 guest instructions and 274 ms** to
**97,903 and 64 ms**. That was measured on `con_redraw` itself, from its
first instruction to its return, with the kernel from before this phase
built from its own tree. The rest is one `TEXT` command per character,
because the kernel draws cell by cell. Phase 6 draws a line at a time.

**`disp_rect` and friends keep their unsigned coordinates.** The library
clips first, as it always has, and only then hands the GAC what is left. So
a rect at x = −5 still wraps and draws nothing, **whichever path draws it**.
The picture never depends on which path drew it, which is what the equality
test holds the library to. Phase 3's signed clipping is there for programs
that use `<pigeon/gac.h>` directly. `disp_frame` goes to the GAC only when
its corner is on the screen and its size is not a wrapped negative.

**Not built: the `BATCH` builder in `gac.c`** (§2.1). Nothing here needed
it. It belongs with Phase 6's console, which is its first real user.

**`demo.c` is an eighth program with a compile-time size** (`TEXT_MAX`),
which §1 missed and the compiler caught. **The programs that draw number
14**, and not quite the ones §4's first draft named. `edit.c` does not draw,
`pgs.c` does, and `img.c` and `splash.c` wrote the screen through raw
`DISPLAY_START`. All four are handled as §4 now says.

**Tests that changed, each for a reason that is this phase's:**

- `test_graph.py`'s zoom pressed `KEY_UP`, which `graph` does nothing with.
  Zooming has been Page Up and the wheel since the commit that added the
  wheel. The test passed only because its first snapshot caught a frame
  still being drawn in software. The GAC finishes the frame first, so the
  two snapshots matched. It now presses Page Up.
- `test_install.py` looks at bios2's countdown every 2,000 instructions
  instead of every 20,000. Under the suite's stepping clock, the 5-second
  countdown is about 100 timer reads. With bios2 drawing through the GAC
  those reads come so close together that the whole countdown fitted
  between two looks.
- `test_display.py`'s cube test records where the screen really is
  (`on_screen`). A back buffer in video memory leaves `scanout_base` where
  it was, so that alone no longer shows the flip.
- `test_config.py`: `display.c`'s dependencies now include `gac.c` and
  `vram.c`.

**New tests:** `tests/test_display_gac.py`, 9 cases: the two covers on a
machine with the devices, without them, and on a bare CPU; the owner calls;
a translucent colour through the GAC and in software; and **the equality
test**: one C scene, drawn by `display.c` on a bare CPU and on a machine,
**equal byte for byte**, with a check that the machine run really used the
GAC. In `test_kernel.py`: a program that grabs a mode, a back buffer, a
surface and a heap surface leaves none of them behind, whether it ends by
exit, by a fault or by Ctrl+C. A program's surfaces outlive a child that
grabs its own. `reboot.bin` brings back the power-on screen. In
`test_vram.py`: `FREE_OWNED` frees that depth and deeper, in both devices,
never the screen, and puts the screen back if it was showing what went.

**The rest of the suite passed without a change,** including every test
that compares a screen: `test_files`, `test_explorer`, `test_disc`,
`test_edit`, `test_pgs`, `test_graphics`, `test_bios2` and `test_kernel`'s
colour tests. Those pictures are now drawn through the GAC and did not move.

**`tools/bench.py`'s real-program figure** is 2.2M guest instructions a
second now, against 2.8M before. That is not a slowdown: `demo` draws
through the GAC, so it runs far fewer instructions for the same frames, and
the accelerator's host time is part of the wall clock the rate is divided
by.

**The suite:** 1,854 passed, none failed.

