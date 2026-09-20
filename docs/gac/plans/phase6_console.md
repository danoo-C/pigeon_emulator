# Phase 6: the kernel console at any size, and the mode picker

> Part of [the GAC plan](../README.md). **Status: built, 2026-09-19
> ([§9](#9-as-built)); every question decided ([§8](#8-decisions)).** Needs
> Phase 5 (built). Design: [design.md §5.7](../design.md#57-the-kernel-console).
> Decisions: Q1, Q6 in [decisions.md](../decisions.md). Facts marked
> *(checked)* were read in the code, and *(measured)* ones were run.
>
> This file replaces the original sketch, `phase6_programs_console.md`. Its
> first half (the programs with compile-time sizes) and its "who owns the
> mode" step moved into Phase 5 (Phase 5's decisions 1 and 4), and are done.

**The goal:** the kernel's console fills whatever screen it is given: 32 × 12
cells at 192 × 108, 106 × 40 at 640 × 360, 213 × 80 at 1280 × 720. You can
pick a mode from a list in the browser's and the pygame window's toolbars,
and the console switches to it the next time you are at the prompt.

**What you will be able to see at the end of this phase:** pick 640 × 360 in
the toolbar, and at the prompt the window grows and the console has 106
columns and 40 rows. Your text is still there, and `ls`, `edit`, `files` and
the explorer work at that size. Pick 192 × 108 again and it all comes back.
`setmode` from the prompt, and `# graphics` scripts that pick a mode, are
Phase 7.

---

## 1. Where things stand

- **The console is 32 × 12, fixed** *(checked: `kernel.c`)*. `CON_COLS 32`,
  `CON_ROWS 12`, `con_grid[384]` and `con_look[384]`, and the scrollback
  `back_grid[3200]` / `back_look[3200]` (100 rows × 32). `CON_COLS` or
  `CON_ROWS` appears on **68 lines** of `kernel.c` *(checked)*. The design
  counted 93 back when it was written. Most are `row * CON_COLS + col`, and
  the rest are bounds and defaults (the scroll region's bottom row, the
  cursor clamp). `PAGE_ROWS 11` is "a screen less a row" written as a
  number.
- **Every cell is drawn on its own.** `con_redraw` calls `con_draw` per
  non-blank cell, and `con_paint` calls `disp_char` (plus a `disp_rect` for
  an inverse cell). Since Phase 5 each of those is one GAC `TEXT` command.
  **A half-full 32 × 12 screen costs 97,903 guest instructions, 64 ms**
  *(measured, Phase 5)*. At 213 × 80 a full screen is 17,040 cells: about
  **9 million instructions, about 4 seconds** per redraw at that rate. So
  drawing a row at a time is not an optimisation. It is what makes a big
  console usable at all (design §4.3).
- **The kernel can already be in any mode.** Phase 5's `disp_init` sizes the
  kernel's `DISP_W`/`DISP_H` from the machine, `k_tidy` puts the kernel's
  mode back after a program, and a power-on mode other than 192 × 108
  (`--mode`) reaches the kernel as it is. Only the console's own numbers
  are fixed.
- **Programs cannot ask how big the console is.** There is no system call
  for it *(checked: `syscall.h`, 23 of 32 slots used)*. `edit` hardcodes
  `ED_COLS 32`, `ED_ROWS 9` and the rows of its message and key lines
  *(checked)*, so on a bigger console it would work in the top-left
  32 × 12 corner.
- **The line editor waits for keys in a loop** *(checked:
  `con_read_line`)*, polling `mouse_event` and `key_event` on every pass. It
  is where the shell's prompt waits, and so the natural place to notice
  that the window asked for another mode (Q1: the kernel adopts it "at the
  prompt").
- **`/preferred` exists and nobody reads it yet** (Phase 4). The picker was
  held back to this phase so that no button ships that does nothing (Phase
  4, decision 4).

---

## 2. The console's size, at run time

### 2.1 A fixed stride, a variable size (Q1)

The grids are sized for the biggest screen, and **each row keeps the same
place in them whatever the mode**:

```c
#define CON_MAX_COLS (DISPLAY_MAX_W / CON_CELL_W)     /* 213 */
#define CON_MAX_ROWS (DISPLAY_MAX_H / CON_CELL_H)     /*  80 */
char con_grid[CON_MAX_ROWS * CON_MAX_COLS];           /* 17,040 bytes, was 384 */
char con_look[CON_MAX_ROWS * CON_MAX_COLS];
char back_grid[SCROLLBACK * CON_MAX_COLS];            /* 21,300 bytes, was 3,200 */
char back_look[SCROLLBACK * CON_MAX_COLS];
unsigned con_cols, con_rows;                          /* the screen's, from DISP_W/H */
```

`row * CON_COLS + col` becomes `row * CON_MAX_COLS + col`. **That is a
constant**, so the 68 lines change mechanically and cost nothing at run
time. The bounds (`col < CON_COLS`, the scroll region, the cursor clamp,
`PAGE_ROWS`) become `con_cols` / `con_rows`. A mode change then moves no
memory at all: a row is where it was, and only how much of it shows changes.
That is about 77 KB more static data in a kernel of 259 KB, with a 1 MB
program region.

### 2.2 What happens to the text when the mode changes (Q2)

- **Narrower:** each row is cut at the new width. What is past the edge is
  cleared, so it cannot reappear as stale text later.
- **Fewer rows:** the top rows go into the scrollback, as they would have
  by scrolling, and the rest move up. The prompt stays on the bottom row,
  and PgUp still reaches the rows that went.
- **Wider or more rows:** the new cells are blank, and the text stays where
  it was, at the top.
- A line being typed at the prompt is redrawn by the line editor at its new
  place (`line_row` moves with its row).

### 2.3 Drawing a row at a time (Q5)

`con_redraw` clears the screen, then for each row draws **each run of cells
with the same look as one `TEXT`**: a blank run costs nothing, and an
inverse run is one `FILL` of its ink plus one `TEXT` in the background
colour. A typical row is one or two commands. So a full 213 × 80 screen is
about 80 to 160 commands, against 17,040. **Estimated 10 to 15 ms against
about 4 s**, to be measured in step 5.

To draw a run, `display.h` gains **`disp_textn(x, y, s, n, fg)`**: `n`
characters of `s`, with no terminating 0 needed. The console's rows are not
C strings. It is the same `TEXT` command `disp_text` sends, and the same
software loop without a GAC.

Single cells (typing, the cursor, `con_cell`) stay one command each: that
is one command per key, and it is already fast.

### 2.4 Adopting the window's mode (Q3)

In `con_read_line`'s wait loop, **when the program reading the line is the
startup program (the shell, at depth 1)**, the kernel looks at `PREFERRED`.
If its request count moved since it last looked, and the mode asked for is
not the current one, the kernel:

1. `disp_setmode(w, h)`. The screen is now black, and the kernel's own
   `DISP_W`/`DISP_H` are the new ones, so `k_tidy` will keep this mode from
   now on.
2. Recomputes `con_cols` and `con_rows`, and adjusts the text (§2.2).
3. Redraws the console, and the line being typed where it now is.

It does this only for the shell's own line, not a line read by any program
(`pgs`'s `read`, say). A running program must not have the screen change
under it (Q6), and a `# graphics` script reading a line is exactly that.
While you are scrolled back through the scrollback it waits until you
return, so the view you are reading does not jump.

### 2.5 `consize`, and `edit` at any size (Q4)

A new system call, slot 23: **`int consize(unsigned *cols, unsigned *rows)`**,
answering `con_cols` and `con_rows`. `edit` asks at start, and its
`ED_COLS` / `ED_ROWS` / message row / keys row become variables. Its arrays
are sized from the console's maxima, the pattern Phase 5 used. `more` needs
nothing: the kernel pages it, and paging uses `con_rows` now.

---

## 3. The mode picker

**The browser** (`display/index.html`): a `<select>` in the toolbar, filled
from `/info`'s `modes` and showing the current mode. Choosing one posts
`/preferred`. It follows the mode when it changes, whoever changed it. Its
logic is two pure functions in the page's screen-logic section,
`modeLabel(w, h)` and `modeOptions(modes, w, h)`, tested under node as
Phase 4's are.

**pygame** (`display/display.py`): a **"Mode"** button that opens a list of
the modes, the way "Load from server" opens its list of discs *(checked:
`_open_picker`)*. Choosing one posts `/preferred`. The list's contents come
from a pure function in `screen_mode.py` (Q6).

In both, a line under the toolbar says **"switches at the prompt"** until the
mode changes. A mode chosen while a program is running waits for its
prompt, and saying so is better than looking broken.

---

## 4. Programs at other sizes

Phase 5 made every program ask the screen's size, but none has *run* in
another mode yet. This phase is where you can switch, so each program that
draws is started once at 640 × 360 and at 1280 × 720 in a test, to see that
it draws inside the screen and quits cleanly: `files`, `disc`, the explorer,
`graph`, `cube`, `demo`, `graphics`, `img`, `splash`, `edit`, the installer
and `bios2` (Q7). Whatever breaks is fixed here. The likely kind of break is
a layout that assumed "about 32 columns", such as a status line placed at a
fixed column.

---

## 5. Steps

1. **The fixed stride:** `CON_MAX_COLS`/`CON_MAX_ROWS`, the four grids,
   `con_cols`/`con_rows` set from `DISP_W`/`DISP_H` in `main`, and the 68
   lines. At 192 × 108 nothing changes: **the full suite must pass as it
   is**, which is the check that the mechanical edit was mechanical.
2. **`disp_textn`**, and `con_redraw` drawing runs (§2.3), with the redraw
   measured in guest instructions before and after, as Phase 5 did.
3. **`consize`** (syscall slot 23, `sys.h`, `sys.c`), and `edit` on it.
4. **Mode changes in the kernel** (§2.2, §2.4): a `con_resize(w, h)` that
   does §2.2, and the adoption in `con_read_line`.
5. **The console at other sizes:** tests that boot the kernel at 640 × 360
   and 1280 × 720 (a `Console` with `display_mode=`) and check the prompt,
   typing, scrolling, the scrollback, a coloured and an inverse cell, and
   `ls`. Then a switch at the prompt, 192 × 108 → 640 × 360 → 192 × 108,
   through `/preferred`'s function: text kept, prompt on the bottom row,
   nothing stale past the edge. **The redraw is timed at 1280 × 720.**
6. **The picker:** the browser's `<select>` and its node tests, then
   pygame's button and list, with `screen_mode.py` tests.
7. **The programs at other sizes** (§4), fixing what breaks.
8. **The bench and the full suite.**

**Done when:** the console fills 640 × 360 and 1280 × 720; a mode chosen in
the toolbar is taken at the next prompt and the text survives it; a full
720p redraw is in the tens of milliseconds; every program that draws works
at the three sizes tested; and the full suite passes.

---

## 6. Tests

- **Unchanged at 192 × 108:** `test_kernel.py`'s console tests (colours,
  inverse, scroll regions, the scroll cost, line editing, history, Tab,
  scrollback, paging) must pass without an edit after step 1. They are
  the proof that the stride change moved nothing.
- **At other sizes:** the same helpers, with `Console` taking a mode. The
  test helpers' `ROWS` and `COLS` become the console's own.
- **Adoption:** `/preferred` then Enter at the prompt switches the mode.
  The same while `pgs` is reading a line with `read` does not switch it
  until the shell's prompt. A second request before the prompt: the last
  one wins.
- **The picker:** `modeOptions` and `modeLabel` under node; the source of
  the wiring read, as Phase 4 did; `screen_mode.py`'s list.
- **`consize`:** a program prints it at 192 × 108 and at 640 × 360.
- **Memory:** the tests at 1280 × 720 use a small RAM where they can, and
  are measured, the lesson from Phase 2.

---

## 7. Risks

- **The 68 lines.** Mechanical, but a missed `CON_COLS` in a
  `row * CON_COLS` gives a stride of the old width at 192 × 108. It changes
  nothing at 32 columns (32 is 32), so **the default-mode tests cannot catch
  it**; only the tests at other sizes can. That is why step 5 tests
  scrolling, the scrollback and inverse cells at 640 × 360, not only the
  prompt.
- **The line editor across a resize.** `con_read_line` keeps its line's
  start (`line_row`, `line_col`) and draws the line wrapped at the width.
  A narrower screen re-wraps it. That is the fiddly part, and the reason
  adoption waits for the shell's prompt rather than happening anywhere.
- **Programs at big modes** (§4) have never run there. The sweep in step 7
  is the mitigation, and the list of what it fixes goes in "As built".
- **Bigger static data** in the kernel (about 77 KB) makes the boot disc's
  kernel and the installer's copy a little larger, well inside the 1 MB
  program region.

---

## 8. Decisions

Answered 2026-09-19: *"okay, all recommendations. please build it!"*. Every
recommendation stands.

1. ~~**The grids: a fixed stride of the widest screen?**~~ **Recommendation:**
   yes (§2.1). `row * CON_MAX_COLS` is a constant, so the 68 lines stay as
   cheap as they are, and a mode change moves no memory. The alternative,
   the stride following the mode, means re-laying every row on every
   change, and a stride that is a variable in 68 places.

   **Decided (you), 2026-09-19:** the recommendation.

2. ~~**What happens to the text on a mode change?**~~ **Recommendation:** keep
   it (§2.2). Each row stays a row: cut when narrower, top rows into the
   scrollback when there are fewer, blank space when bigger. No re-wrapping.
   The alternatives are clearing the screen (simplest, loses your work) and
   re-wrapping long lines to the new width. Re-wrapping would need the
   console to remember which rows were one line that wrapped, and it does
   not today.

   **Decided (you), 2026-09-19:** the recommendation.

3. ~~**When does the kernel adopt a mode chosen in the window?**~~
   **Recommendation:** only while the shell (the startup program, depth 1)
   waits for a line, and not while you are scrolled back (§2.4). Any line
   read would include a `# graphics` script's `read`, which would change the
   screen under a running program (Q6).

   **Decided (you), 2026-09-19:** the recommendation.

4. ~~**A `consize` system call, and `edit` using it?**~~ **Recommendation:**
   yes (§2.5). `edit` is the one full-screen console program, and without it
   `edit` works in a 32 × 12 corner of a big console. The call is also
   what any later full-screen console program would need.

   **Decided (you), 2026-09-19:** the recommendation.

5. ~~**Redraw a row at a time with a `TEXT` per run, and drop the `BATCH`
   builder Phase 5 postponed?**~~ **Recommendation:** yes (§2.3). About 80 to
   160 commands for a full 720p screen is already in the tens of
   milliseconds. Batching them would save the bus overhead of those
   commands, but it would cost a buffer and a builder in the kernel, for
   something the measurement in step 2 should show it does not need. If it
   does, batching goes in then.

   **Decided (you), 2026-09-19:** the recommendation.

6. ~~**pygame's picker: a "Mode" button opening a list, like "Load from
   server"?**~~ **Recommendation:** yes. It reuses the list pygame already
   has, and shows every mode at once. The alternative is a button that
   steps to the next mode on each click, which is simpler but makes you
   click through 854 × 480 to get from 640 × 360 to 1280 × 720.

   **Decided (you), 2026-09-19:** the recommendation.

7. ~~**Run every drawing program at the bigger modes in this phase, and fix
   what breaks?**~~ **Recommendation:** yes (§4). With the picker, this is the
   phase where you can first switch modes and then start them. Leaving the
   sweep for Phase 7 would ship a picker that can take you to a mode where
   `files` draws off the screen.

   **Decided (you), 2026-09-19:** the recommendation.

---

## 9. As built

Built 2026-09-19, as §2 to §5 describe, with every recommendation. What
building it changed or found:

**The stride is 216, not 213.** `CON_MAX_COLS` is rounded up to a multiple
of 4 so every row starts on a word, and `memcpy` moves it a word at a time.
At 213, most rows started unaligned and were copied a byte at a time.

**Rows are moved `con_cols` cells at a time, never a whole stride.** The
first version of the fixed stride made a console scroll cost 180,530
instructions, against the test's 20,000: every scroll moved whole 213-cell
rows, most of them blank at 32 columns wide. `con_copy_row` and
`con_blank_rows` touch only the visible part. That is right because of one
rule, kept everywhere: **a cell past `con_cols`, or a row past `con_rows`,
is always blank**, on the screen and in the scrollback. Nothing writes
there, and `con_resize` blanks what falls outside when the console shrinks.
`con_clear` blanks only the visible rows too; blanking the whole grid would
have been 34,000 stores.

**The redraw, measured** (`con_redraw`, from its first instruction to its
return):

| screen | cell at a time | run at a time |
|---|---|---|
| 32 × 12, half full | 97,903 instructions, 64 ms | **32,958 instructions, 18 ms** |
| 213 × 80, 80 rows of text | ~9 million (estimated), ~17,000 commands | **307,704 instructions, 81 commands** |

The 720p redraw took two further changes. A run used to extend through the
trailing blank cells of its row, a cell at a time, to column 213; now **four
blank cells end a run**. Blank cells are also **skipped four at a time**, a
word of spaces at once. What remains is the kernel's scan of 17,040 cells,
about 0.12 s of guest time for a full 720p redraw. That is not the "tens of
milliseconds" §2.3 estimated, because the scan dominates, not the commands.
Keeping each row's used width would cut the scan, but it would touch every
place that writes a cell. It is noted here rather than done.

**Programs at the bigger modes (§4), screenshotted and read at 640 × 360
and 1280 × 720:** `files`, `disc`, `demo`, `ls`, the explorer, `graphics`,
`img`, `splash`, `pgs`, `edit` and bios2 all lay themselves out correctly.
Two did not:

- **`graph` drew a wall of curve** wherever the curve left the top of the
  plot. **The cause was Phase 5's, not `graph`'s: `disp_w` and `disp_h` were
  `unsigned`.** `DISP_H` had been the literal 108, a signed `int`. As a
  variable it went unsigned, and took `graph`'s `PLOT_BOT` with it, so a row
  above the plot, a negative number, compared as a huge one. **They are
  `int` now**, as the literals were. That restores the old meaning in every
  program that does signed arithmetic with the screen's size, not only
  `graph`. `test_graph.py` has a test at 640 × 360 that fails with the old
  types (a column of 326 curve pixels) and passes with the new ones.
- **`cube` would not turn when dragged on a wide screen.** Its gain was
  `256 / DISP_W` as a whole number, which is 0 on any screen wider than 256.
  It now works out `dx × 256 / DISP_W` with the remainder kept, so a drag
  across the screen is about one turn at any width. That is slightly
  faster at 192 × 108 than the old truncated gain of 1. The cube also
  **scales with the screen's height**, exactly 1 at 108, so the default
  picture is unchanged. `test_libs.py`'s drag test runs at 640 × 360 too.

**`disp_setmode` to the power-on mode shows it out of RAM** at
`DISPLAY_START`, as at power-on and after a reboot. Otherwise a round trip
192 × 108 → 640 × 360 → 192 × 108 left the console drawing in video memory.
That works, but it is not what the BIOS, bios2 or `k_tidy` expect of that
mode.

**`edit`'s scroll region** was a fixed `ESC [2;10r`. It follows `ED_ROWS`
now, like the rest of its layout.

**The picker, run for real:** pygame's "Mode" button, against the emulator's
real servers under SDL's dummy driver, listed the five modes with 192 × 108
marked "(now)". Choosing 640 × 360 posted `/preferred` and said "switches
at the prompt", and the machine recorded the request. The list is the "Load
from server" overlay, given a title and a "choose" action of its own. **The
browser's picker was not run in a browser**: its logic is tested under node
and its wiring read.

**Tests.** In `test_kernel.py`: the console at 640 × 360 (size, a line wider
than 32, `consize`, scrolling and the scrollback, a coloured and an inverse
cell past column 32); a switch at the prompt and back, with the text kept
and nothing past the edge; fewer rows sending the top ones to the
scrollback; no switch while a program reads a line; `edit` using all 40
rows; and the 720p redraw, under 500,000 instructions and 400 commands. The
test helpers learned the screen's size: `text_at` takes a width and a
height, `Console` takes a mode and returns `Rows` that know the console's
width, a blank band is read as a blank row without reading its cells, and
`settled_at` wants the same screen on two looks in a row. With one look it
failed once under the full suite's load, having looked mid-redraw. In
`test_frontends.py`: the pickers' lists, under node and in `screen_mode.py`,
and their wiring. `test_cd.py`'s list of toolbar buttons gained "Mode".

**The suite:** 1,868 passed, none failed.

