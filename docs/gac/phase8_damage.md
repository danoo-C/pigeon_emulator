# Phase 8: damage rectangles and bandwidth

> Part of [the GAC plan](README.md). **Status: planned.** Needs Phase 7.
> Design: [design.md §5.5](design.md#55-the-host-side-front-ends-and-the-wire)
> ("Damage tracking"); the numbers are §4.4. Decisions: Q10 in
> [decisions.md](decisions.md).

**The goal:** at 1280 × 720, bandwidth, not the CPU, is the ceiling:
105 MiB/s at 30 FPS over localhost (§4.4). This phase makes a frame that
changed a little cost a little. **Its exact shape is decided by what
`tools/bench.py` says after Phase 7, not before it.**

---

## Steps

### 8.1 Measure first

`tools/bench.py` gets a frame-serving line per mode. The measurement is
bytes per second and host CPU at 720p, with the console idle and with it
scrolling.

### 8.2 `GAC_DAMAGE`

The GAC keeps the union of the rectangles it drew since the last read.
`DAMAGE` answers it and clears it. The command number was reserved in
Phase 3.

### 8.3 The rule, using Phase 1's `vram_dirty`

**If anything was written through the aperture, send the whole frame.
Otherwise, send the union of the GAC's rectangles.** Console text, window
furniture and `graphics` calls all go through the GAC, so they get small
frames. A program doing its own per-pixel work pays full bandwidth, which
is the right way round.

### 8.4 `/frame` serves a rectangle

This needs a header saying which rectangle it is. Both front ends blit it
into the canvas they already have.

**Done when:** the bench shows the idle and console-scrolling cases well
below full-frame bandwidth at 720p, and the full suite passes.

---

## As built

*(filled in when the phase is done)*
