# What is left after Phase 9, and what each piece would cost

> Part of [the GAC plan](README.md). **Status: notes, 2026-09-20. Nothing
> here is decided or planned in detail** — this is the shortlist and its
> prices, so the next choice can be made on numbers rather than on a guess.
> Facts marked *(checked)* were read in the code, and *(measured)* ones were
> run on this machine on 2026-09-20.

Phases 1–9 are built. The list below is [README.md §9](README.md#9-not-in-this-plan)
with the one item that has since been done struck out, plus **1920 × 1080**,
which was not on it.

| | what | size | visible? |
|---|---|---|---|
| [1](#1-1920--1080) | **1920 × 1080** as a mode | small | yes, immediately |
| [2](#2-the-splashc-cleanup) | the `splash.c` eye-flash cleanup | very small | no |
| [3](#3-letting-the-guest-use-more-than-128-mb) | the guest using more than 128 MB | small | only to big programs |
| [4](#4-rows-rather-than-bands) | rows rather than bands for damage | medium | no, bandwidth only |
| [5](#5-retiring-ch_display) | retiring `CH_DISPLAY` | medium | no |

---

## 1. 1920 × 1080

`DISPLAY_MODES` stops at 1280 × 720 *(checked: `emulator/memory_map.py:114`)*.
Adding 1920 × 1080 is **smaller than it looks**, and three things that were
expected to block it do not:

- **Two 1080p surfaces fit the default video memory.** A surface is
  1920 × 1080 × 4 = 8,294,400 bytes, so a screen *and* a back buffer come to
  16,588,800 against `VRAM_SIZE`'s 16 MiB = 16,777,216 *(checked)* — they fit,
  **with 188,416 bytes to spare.** A *third* surface does not, and that is
  worth saying in `vram.md` rather than discovering.
- **The front ends need nothing.** Phase 4 made them follow the mode and
  Phase 8's damage bands scale with it.
- **The kernel's console grows by about 63 KB, and has the room.**
  `CON_MAX_COLS` and `CON_MAX_ROWS` are derived from `DISPLAY_MAX_W/H`
  *(checked: `user/os/kernel.c:77-78`)*:

  | | 1280 × 720 | 1920 × 1080 |
  |---|---|---|
  | `CON_MAX_COLS` × `CON_MAX_ROWS` | 216 × 80 | 320 × 120 |
  | `con_grid` + `con_look` | 34,560 B | 76,800 B |
  | scrollback (`SCROLLBACK` = 100) | 43,200 B | 64,000 B |
  | **total console statics** | **77,760 B** | **140,800 B** |

  `PROGRAM_MAX_SIZE` is 1 MB for code *and* static data *(checked:
  `memory_map.py:125`)*, so +63,040 bytes is affordable.

**The real cost, and the thing to decide:** `DISPLAY_MAX_W`/`DISPLAY_MAX_H`
are the cap **eight guest programs size static arrays from** — `graph.c` and
`graph-corrupt.c` (`int ys[DISPLAY_MAX_W]`, `char yok[DISPLAY_MAX_W]`),
`disc.c`, `explorer.c`, `installer.c`, `demo.c`, `firmware/bios2.c`, and the
kernel *(checked)*. **Every one of those binaries grows whether or not anyone
ever runs at 1080p,** because the cap is compile-time. That is the trade-off:
a mode nobody selects still costs every program on the disc.

The way out, if that cost is unwanted, is to stop sizing arrays from the cap
and allocate them from the live mode at `disp_init()` — which is a bigger
change than adding the mode, and a plan of its own.

> **It makes the text problem worse, not better.** At 1280 × 720 the console
> is 213 × 80 characters in a 6 × 9 cell, which is already too small to read
> comfortably; at 1920 × 1080 it is 320 × 120. **Raising the resolution
> without a bigger font makes every character smaller.** So 1080p wants the
> font system in [docs/fonts/](../fonts/README.md) ahead of it, or alongside it — that is
> the same finding that started the GUI library.

---

## 2. The `splash.c` cleanup

Found while building Phase 9, and written up in
[phase9_srcalpha.md §11](plans/phase9_srcalpha.md#11-as-built). `splash.c`
makes the pigeon's eyes flash by loading a second BMP, scanning it for white
pixels into `unsigned eyes[1024]`, and storing to the screen a pixel at a
time, capped at `MAX_EYES` = 1,024 *(checked: `splash.c:93-104,150`)*.

It can be **two bus commands and no guest loop at all:** composite the yellow
eyes onto a copy of the pigeon's eye region once, then `BLIT_ALPHA` that copy
over the screen at strength `t` each frame — outside the eyes the copy equals
the pigeon, so the blend does nothing there. That deletes `find_eyes`,
`eyes[1024]`, `MAX_EYES` and `blend`, and lifts the cap.

**It needs nothing from Phase 9** — it is `BLIT_ALPHA` as shipped in Phase 3.
Smallest real win on this list.

---

## 3. Letting the guest use more than 128 MB

`--ram 1G` allocates the RAM, wraps at it, and puts the VRAM aperture above
it. But the guest's layout is fixed at 128 MB: `STACK_TOP = RAM_SIZE - 4` is
baked into every build, `BIOS2_LOAD_ADDR` is `0x07000000`, and
`lib/pigeon/mem.c` stops the heap at `0x07F00000` *(checked, and written up
in [phase1_aperture.md](phase1_aperture.md#what---ram-1g-does-and-does-not-give-you-yet))*.
On a 1 GB machine everything above 128 MB is there and addressable, and
nothing uses it.

What it needs: a way for the guest to **ask** how much RAM there is, the BIOS
setting the stack from that, and `mem.c` ending the heap from it. Phase 1
calls it "fairly small". The hazard is the same one the aperture has — a
`.bin` that bakes in a limit runs fine on the machine it was built for and
wrongly on another — so the answer must come from the machine, never from a
predefined symbol.

---

## 4. Rows rather than bands

Phase 8 sends the **band** of rows between the first and last that changed.
For a picture that redraws across the whole screen — `cube` spinning, `demo`
— the band is most of the screen, so they still send 22–27 MiB/s
*(measured, [phase8_bandwidth.md §8](plans/phase8_bandwidth.md#8-as-built))*.
Sending the rows that actually changed, or rectangles, would cut that.

Phase 8 §6 already considered it and left it out. Nothing visible changes;
this is bandwidth only, and the two programs it helps are the two that are
meant to be redrawing everything anyway. **Lowest value on this list.**

---

## 5. Retiring `CH_DISPLAY`

Still live: `kernel.c:1183,1566` page-flips through it, and `display.c` falls
back to it where there is no GAC *(checked)*. README §9 says it stays
supported until nothing uses it, then goes in a separate tiny plan. Both
those users would have to move first, so this is **blocked, not chosen** —
and the `display.c` fallback is what makes a machine with no video memory
work at all, so "nothing uses it" may never be true.
