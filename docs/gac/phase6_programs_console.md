# Phase 6: the fixed-size programs, then the kernel console

> Part of [the GAC plan](README.md). **Status: planned.** Needs Phase 5.
> Design: [design.md §5.7](design.md#57-the-kernel-console) and §6's program
> table. Decisions: Q1, Q6 in [decisions.md](decisions.md).

**The goal:** every program still works at 192 × 108, and the kernel console
fills whatever screen it is given. This is the biggest phase: **93
`CON_COLS`/`CON_ROWS` sites in `kernel.c` alone** *(checked)*. Hence the two
halves.

---

## Phase 6a: the five programs with compile-time sizes

Array sizes must be constant, so the compiler catches every one of these at
build time (§8).

| step | file | what |
|---|---|---|
| 6a.1 | `user/files.c` | `char row[COLS + 1]` → `MAX_COLS`; `COLS`/`ROWS` at run time |
| 6a.2 | `user/disc.c` | the same, plus `line_t.text[COLS + 1]` and `bytes[HEX_PER_ROW * ROWS]` |
| 6a.3 | `user/os/installer.c` | `COLS` at run time |
| 6a.4 | `firmware/bios2.c` | `COLS`, and `detail[DETAIL]` checked against the cap |
| 6a.5 | `user/os/bin/img.c`, `graphics.c` | `memcpy` to `DISPLAY_START` (`img.c:44`, `graphics.c:169`) → the current target |
| 6a.6 | `edit.c`, `explorer.c`, `splash.c`, `demo.c`, `cube.c`, `graph.c` | recompile, and check each for a hidden `DISPLAY_START` |

`user/*.asm` stay as they are, at 192 × 108.

## Phase 6b: the kernel console

### 6b.1 `con_cols` / `con_rows`

These are computed from `disp_w / CON_CELL_W` and `disp_h / CON_CELL_H`.
The four grids are sized from `DISPLAY_MAX_W`/`DISPLAY_MAX_H`, about 76 KB
more static data at the 1280 × 720 cap.

### 6b.2 The 93 sites

`row * CON_COLS + col` → `row * con_cols + col`. This is mechanical, so it
is done as a separate commit that changes nothing at 192 × 108.

### 6b.3 `con_redraw` through `GAC_BATCH`/`GAC_TEXT`

This is not an optimisation. It is what makes a big console possible at all
(§4.3: 34 seconds a redraw otherwise).

### 6b.4 Who owns the mode (Q6)

Only the program that asked may change the mode, and `k_tidy` puts it back
when that program ends, the way it already restores the scanout base
*(checked: `kernel.c:1336`)*.

### 6b.5 Adopt the preferred mode at the prompt

Between programs, the kernel reads `VRAM_PREFERRED` and switches if the
window asked (Q1).

**And the mode picker in both front ends' toolbars**, which posts
`/preferred` (built in Phase 4). It was left out of Phase 4 on purpose
([phase 4 plan](plans/phase4_frontends.md), decision 4), so that no button
ships that does nothing until this step makes the kernel listen.

### 6b.6 Reflow on a mode change

The console keeps its grid and re-wraps it. **Clearing is the acceptable
first cut** (§8) if reflow turns out fiddly.

**Done when:** the full suite passes at 192 × 108, and the console fills a
640 × 360 screen set from a test.

---

## As built

*(filled in when the phase is done)*
