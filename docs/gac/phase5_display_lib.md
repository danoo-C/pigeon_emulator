# Phase 5: `<pigeon/display.h>` on runtime geometry

> Part of [the GAC plan](README.md). **Status: planned.** Needs Phases 2
> and 3. Design:
> [design.md §5.6](design.md#56-the-guest-side-pigeondisplayh-on-runtime-geometry)
> and §6's library table. Decisions: Q2, Q5 in [decisions.md](decisions.md).

**The goal:** the C library asks the hardware how big the screen is,
instead of being told at compile time, and hands bulk work to the GAC.
**A pixel is still one store:** `*row_ptr(x, y) = c`.

---

## Steps

### 5.1 `lib/pigeon/vram.h` / `vram.c`

Thin covers over `CH_VRAM`: probe with its magic, modes, surfaces, scanout,
upload/download, generation.

### 5.2 `lib/pigeon/gac.h` / `gac.c`

Thin covers over `CH_GAC`: every primitive, a `BATCH` builder, and
`gac_blit_alpha`.

### 5.3 Runtime geometry in `display.h`

`disp_w`, `disp_h`, `disp_pitch`, `disp_base`, and
`#define DISP_W disp_w` (Q5). The 22 files that use `DISP_W` in an
expression keep compiling unchanged.

### 5.4 `disp_init()`, and the rule about the aperture

It tries `CH_VRAM` first, then `CH_DISPLAY`'s `INFO`, then the compile-time
`DISPLAY_W`/`DISPLAY_H` on a bare CPU with no controller, which is what
`test_libs.py` builds. **`disp_init()` is the only code that learns the
aperture base, and it learns it from `VRAM_INFO`,** with a comment saying
why. That is the one hazard growable RAM created (§5.1, §8).

### 5.5 `display.c` onto the GAC

`row_ptr` and every clip use the runtime geometry. Clear, fill, blit,
scroll and text become GAC calls when there is a GAC, and keep their
software versions when there is not. `disp_use_back_buffer()` gets its
buffer from `VRAM_ALLOC`, and `disp_present()` becomes a `SCANOUT` flip.
Also: `disp_setmode`, `disp_modes`, `disp_preferred`, `disp_generation`.

### 5.6 `bmp.c` loads into a VRAM surface when given one

**Done when:** `test_libs.py` and `test_graphics.py` pass, `user/demo.c`
looks the same at 192 × 108, and the full suite passes.

---

## As built

*(filled in when the phase is done)*
