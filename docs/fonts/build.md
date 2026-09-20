# What to build, and what the code says about the plan

> **Status: verification, 2026-09-20.** The font plan
> ([README.md](README.md)) and [the GUI plan](../gui/README.md) were checked
> against the code they claim to rest on. Facts marked *(checked)* were read
> in the code; *(run)* ones were compiled and **executed on the emulator**
> today. **The plans hold.** Eight things are wrong or missing, none of them
> fatal, all of them cheaper to fix now than after F2 ships. §7's questions
> are **decided (2026-09-20): you took every recommendation**, and §8 is the
> build order with each amendment in its phase. Every doc they touch has been
> amended, and the compiler bugs §1 re-ran have a plan of their own in
> [compiler_plan.md](../compiler_plan.md).

---

## 1. What was verified, and holds

Everything the two plans lean on was re-checked. Nothing in this section
needs action.

**The compiler findings are exact.** All five miscompiles in
[constraints.md §1](../gui/constraints.md) reproduce, byte for byte, *(run)*:

| probe | expected | got |
|---|---|---|
| 12-byte struct assignment, read `.c` | 3 | **9** |
| struct by value, read `p.c` | 3 | **0** |
| `int a = 2*3+1;` at file scope | 7 | **0** |
| `int a = -1;` at file scope | −1 | **0** |
| `int v = -20; v / 2` | −10 | **2147483638** |

and `struct W { struct W *parent; }`, `static` locals and `switch` all error
exactly as [constraints.md §2](../gui/constraints.md) says *(run)*.

**The GUI's whole shape compiles and runs.** Not only the function-pointer
member — the actual pool: an array of widget structs with a `gui_handler`
member, `pool[i].on(i, &e)` dispatch, an event struct passed by pointer, a
parent chain walked as indices with no recursion. It compiles and returns the
right answer *(run)*. So does `gui_scale_axis(-20, 1920, 1280)` → −30, and
the corner-scaling of [scaling.md §2](../gui/scaling.md), which keeps
neighbouring widgets flush *(run)*.

**The device facts are right.** `SET_FONT`'s rules are exactly as
[device.md §1](device.md) states, it reads `count * glyph_h` bytes straight
out of RAM with no window chunking, and `_text` really is width-agnostic — it
touches nothing but `font.cell_w` and `row_mask` *(checked:
`gac.py:646-689`)*. Commands 17 and 18 are free (16 is the highest), and
feature **value 8** is free (1, 2 and 4 are taken) *(checked:
`gac.py:105-128`)*.

**The one-slot problem is real and as described.** `self.font` is a single
object, `disp_init()` is the only `gac_set_font` caller in the tree, and
`gac_cell_w` is one file-scope static *(checked)*.

**The console arithmetic holds.** `CON_CELL_W`/`CON_CELL_H` are constants,
`con_cols` is already a variable, and the grids are sized from
`DISPLAY_MAX_W / CON_CELL_W`, so a bigger font needs fewer cells and no more
memory *(checked: `kernel.c:73-78, 170-172`)*.

**Every API the GUI plan assumes exists**: `disp_generation`,
`disp_use_back_buffer`, `disp_present`'s page-flip semantics, `vram_alloc`,
`gac_ram_surface`, `fs_load_alloc`, and `display.c`'s internal
`disp_target`/`disp_target_h` pair that `disp_push_target` would expose
*(checked)*.

---

## 2. F1 on its own breaks the shell

> [README.md](README.md): *"F1 on its own already helps, and needs nothing
> from the device."*

It needs one thing from the kernel, and without it F1 is worse than doing
nothing.

**`k_tidy()` does not put the font back.** It restores the mode, the screen
base, open handles, timers and the input queues when a program ends — and
never touches the accelerator's font *(checked: `kernel.c:1533-1572`)*. The
kernel calls `disp_init()` once, at startup *(checked: `kernel.c:2060`)*.

So the moment any program calls `gac_set_font` with an 8 × 16 font and exits,
**the shell goes on drawing its 6 × 9 character grid with 8 × 16 glyphs**.
Every cell overlaps its neighbour. Nothing puts it right until the machine
reboots.

**The fix is one line** — `k_tidy()` re-uploads the console's font, beside
the `disp_follow()` it already calls there. It is needed whatever else
happens, because it is what makes the single font slot survivable at all, and
it stops being needed once F2's slot 0 exists. [§7 Q2](#7-questions) asks
whether F1 also puts the new font *on* the console, or only ships the file
and the loader.

---

## 3. Antialiasing, as decided, would never draw a single grey pixel

This is the one that matters. [vector.md §2](vector.md) ranks antialiasing as
**the biggest visual win available**, and [questions §2 Q3](questions.md)
decided that text over an unknown background falls back to 1-bit.

**Every text call on this machine draws over an unknown background.**
`disp_text`, `disp_textn` and `disp_char` all pass `bg = 0u`, and a
background with alpha 0 is the device's "no background" *(checked:
`display.c:632-684`, `gac.py:656-658`)*. The kernel console is no exception:
it fills the cell with `disp_rect` and then calls `disp_textn` with no
background *(checked: `kernel.c:285-293`)*. So do all 97 call sites.

Taken together, the two decisions cancel: **F5 ships, and every glyph on the
machine stays 1-bit.** The only text that would be antialiased is text drawn
by a GUI widget that happens to pass its own background — which is the GUI
plan, phase G1 and later, long after F5.

Two ways out, and they are not exclusive:

| | how | cost |
|---|---|---|
| **(a)** | coverage **is** per-pixel alpha: blend the glyph against whatever is under it, with Phase 9's `src_alpha_row` / `blend_span`, which already exist *(checked: `gac.py:287-318`)* | the slow rim loop, which Phase 9 measured at 0.39 ms for a 128 × 128 sprite |
| **(b)** | give `disp_text` a background, so the fast table path applies; the console passes `CON_BG`, which it already knows | every caller that wants it changes |

**Recommendation: both.** (a) is the correctness path — it is why F5 works
anywhere, including over a picture — and (b) is the fast path for the two
cases that draw the most text. [§7 Q1](#7-questions).

### 3.1 And `blend_table()` is not the table F5 needs

[vector.md §2](vector.md) says the coverage → colour map is *"a 256-entry
`bytes.translate` table, which `blend_table()` already builds for
`BlendInk`"*. It is the same **shape** and the wrong **index**:
`blend_table(source, alpha)` is indexed by the *destination* byte for a fixed
source and alpha *(checked: `gac.py:258-262`)*. F5 needs the opposite — the
index is the coverage, and the fixed things are the foreground and the
background.

Three new tables per (fg, bg) pair, one per channel, built the same way. The
technique is sound and it is C-speed; the reuse is not. And since a console
uses about two (fg, bg) pairs, **caching the rendered glyph rows per pair**
makes antialiased text cost the same as 1-bit text from the second line
onwards — worth having in the plan, because it is the difference between F5
being free and F5 being a decision.

---

## 4. `SET_FONT2`'s arguments, as drafted, have no room for F5 or F6

[device.md §3.1](device.md) fixes the new command as
`slot, address, glyph_w, glyph_h, cell_w, cell_h, first, count`. But the font
plan already decided two things that change what a font *is*:

- **8-bit coverage with a 1-bit flag in the header** ([questions §2
  Q1](questions.md)) — the device must be told which, because it changes how
  many bytes it reads and how it draws them;
- **proportional fonts, chosen per font** ([questions §3 Q1](questions.md)) —
  which needs a per-glyph advance table.

Neither fits in those eight words, and the shape cannot be changed later
without a third command: `BATCH` checks each record's word count against the
command's struct and **refuses the whole batch** if one record is the wrong
length or names a command it does not know *(checked: `gac.py:744-766`)*.

**Recommendation: two more words now, at F2** — `flags` (bit 0: 8-bit
coverage; bit 1: proportional) and `advance`, the address of the advance
table or 0. Both zero means exactly today's font, so F2 ships unchanged in
behaviour and F5 and F6 become device-side work with no new command.
[§7 Q3](#7-questions).

---

## 5. F6 touches two places `vector.md` does not mention

[vector.md §3](vector.md) prices proportional fonts at "a 95-byte advance
table, a prefix sum in `_text` instead of `i * cell_w`, and a
`disp_text_w()`". Two more, both small, both silent if missed:

1. **`_text`'s clipping.** It computes the visible span as
   `len(text) * cell_w` and then slices the row mask at `left * 4`
   *(checked: `gac.py:648-663`)*. Every one of those assumes a uniform
   advance and has to become the same prefix sum.
2. **`gac_text()`'s chunking.** A string longer than 4,072 characters is sent
   in pieces, and the guest advances `x` by `chunk * gac_cell_w` between them
   *(checked: `gac.c:207-213`)*. With per-glyph advances that is wrong, and
   wrong only for long strings — the worst kind of bug to find later.

---

## 6. `disp_font_load(slot, path)` drags the filesystem into the BIOS

[device.md §3.3](device.md) proposes `int disp_font_load(unsigned slot, char *path)`
in `<pigeon/display.h>`.

**There is no linker.** Units are compiled together and every program names
its libraries on the command line *(checked: `cc.py:115-140`,
`lib/README.md`)*. So the moment `display.c` calls `fs_load_alloc`, every
program that draws must also compile `fs.c` — including `firmware/bios2.c`,
which includes `display.h` and `input.h` and no filesystem at all *(checked:
`bios2.c:42-46`)*, and which has a size cap.

**Recommendation: the file reading goes in its own unit** — a small
`<pigeon/font.h>` whose `font_load(path, ...)` returns the blob and its
metrics, and `display.c` keeps taking an address, as `gac_set_font` already
does. A program that wants a font off the disc then names `font.c` and
`fs.c`; one that only draws names neither. [§7 Q4](#7-questions).

---

## 6.1 Three smaller things

- **[shell.md §2](shell.md)** gives one rule — no font narrower than 6 px —
  but `CON_MAX_ROWS` is `DISPLAY_MAX_H / CON_CELL_H` with `CON_CELL_H` 9
  *(checked: `kernel.c:78`)*, so **the cell may not be shorter than 9 px
  either**. Same reasoning, missing half.
- **`disp_push_target()` must not straddle a `disp_present()`.** Present
  reassigns `disp_target` on the flip *(checked: `display.c:209-219`)*, so a
  present inside a push would be undone by the pop. One sentence in
  [canvas.md §3](../gui/canvas.md), not a design change.
- **[device.md §3.1](device.md)** says *"`FEATURE_FONTS = 8` … bit 8 is
  free"*. The **value** 8 is free; it is bit 3. Wording only.

---

## 6.2 The GUI plan's status blocks are stale

[gui/README.md](../gui/README.md) says *"7 are open — Q6, Q17–Q22, and fonts
questions §2"*. Every one of those now carries a **Decided (you)** line, and
so does all of [fonts/questions.md](questions.md). **The only unanswered
question in either plan is [gui/questions.md Q23](../gui/questions.md#23-how-much-line-editing-does-the-console-widget-do)** —
how much line editing the console widget does. [§7 Q5](#7-questions) re-asks
it here.

And [gui/README.md](../gui/README.md)'s *"What this is not"* still lists
**"Proportional text — nothing on the machine supports variable-width
glyphs"**, which F6 now contradicts. It should say "not before F6".

---

## 7. Questions

Answer inline under each; **the recommendation stands where you leave it
blank.**

### Q1. Antialiased text over an unknown background

§3: the decided 1-bit fallback would make F5 invisible, because every text
call on the machine passes no background.

**Recommendation: both paths.** The device blends coverage against the
destination with Phase 9's existing per-pixel machinery when there is no
background, and uses the table when there is one; `disp_text_bg()` is added
so the console and the GUI take the fast path deliberately.

**Decided (you):** the recommendation — **both paths**. Coverage is per-pixel
alpha, so text over no background blends against what is there with Phase 9's
`src_alpha_row`/`blend_span`; text with a background takes the table. The
console and the GUI pass a background through `disp_text_bg()` and get the
fast one. [questions.md §2 Q3](questions.md) is amended to match.

### Q2. What does F1 actually ship?

§2: a program that loads a font and exits corrupts the shell until `k_tidy()`
puts the console's font back.

- **(a)** F1 ships the `.pf` format, the tool, the file, the loader **and**
  the `k_tidy()` line — but the console keeps the 5 × 7 until F2.
- **(b)** as (a), and the console switches to 8 × 16 straight away: 720p goes
  from 213 × 80 to 142 × 40 the day F1 lands.

**Recommendation: (b).** The complaint was that the text is too small to
read, and (b) is the day it stops being. `con_resize()` already exists and
the grids are already big enough ([shell.md §2](shell.md)), so the cost over
(a) is two constants becoming variables.

**Decided (you):** the recommendation — **(b)**. F1 ships the format, the
tool, the file, the loader, the `k_tidy()` restore **and** the console on
8 × 16, so 720p goes from 213 × 80 to 142 × 40 the day it lands.
`CON_CELL_W`/`CON_CELL_H` become variables and `con_resize()` does the rest —
[shell.md](shell.md) is that work, arriving earlier than it expected.

### Q3. Reserve `flags` and `advance` in `SET_FONT2` now?

§4: two extra words at F2, both zero meaning "exactly today's font", so F5
and F6 need no third command.

**Recommendation: yes.** Two words, spent once, against a `SET_FONT3` later.

**Decided (you):** the recommendation — **`SET_FONT2` carries `flags` and
`advance` from the start**, both zero meaning exactly today's font.
[device.md §3.1](device.md) is amended.

### Q4. Does `font_load()` get its own unit?

§6: keeping the disc read out of `display.c` so a program that only draws
does not compile the filesystem.

**Recommendation: yes** — `<pigeon/font.h>` and `font.c`, with `display.c`
taking an address as it does today.

**Decided (you):** the recommendation — **`font.c` is its own unit**, so a
program that only draws never compiles the filesystem and `bios2.c` is
unaffected. [device.md §3.3](device.md) is amended.

### Q5. How much line editing does the console widget do?

The last open question in either plan, restated from
[gui/questions.md Q23](../gui/questions.md#23-how-much-line-editing-does-the-console-widget-do):
**(a)** characters, Backspace, Enter; **(b)** plus history on Up/Down and
left/right within the line; **(c)** the kernel's full editor.

**Recommendation: (b)** — the point where a console stops being annoying,
without the widget needing to know your program's commands.

**Decided (you):** the recommendation — **(b)**: characters, Backspace,
Enter, history on Up/Down, and left/right within the line. `GUI_LINE` carries
the finished line and the app does the rest. Folded into
[gui/questions.md Q23](../gui/questions.md#23-how-much-line-editing-does-the-console-widget-do).

**That was the last open question in either plan.**

---

## 8. So: what to build

Unchanged from [README.md](README.md) in order — the fixes above are
amendments inside the phases, not a re-plan.

| | what | the amendment from this file |
|---|---|---|
| **F1** | `.pf`, `font.c`, `tools/make_font.py`, `mono8x16.pf` on the disc | **+ the `k_tidy()` font restore (§2)**, **+ the console on the new font**, which is `CON_CELL_W`/`CON_CELL_H` becoming variables and a `con_resize()` ([Q2](#q2-what-does-f1-actually-ship), [shell.md](shell.md)) |
| **F2** | slots: commands 17/18, `FEATURE_FONTS`, per-slot cell | **+ the `flags` and `advance` words (§4)**; **+ `font.c` as its own unit (§6)** |
| **F3** | `disp_cell_w()`/`disp_cell_h()`, layout from run-time metrics | — |
| **F4** | wide glyphs: the `bpr` stride | — |
| **F5** | antialiased text | **the unknown-background path (§3)**, and its own coverage tables, not `blend_table` (§3.1) |
| **F6** | proportional fonts | **+ `_text`'s clipping and `gac_text`'s chunking (§5)** |
| **G1–G7** | the GUI library | starts on a machine whose text already looks right |

**F1 and F2 are where every amendment lands.** F3 to F6 are as written.
Nothing found here changes the GUI plan's design — [constraints.md](../gui/constraints.md)
was re-run in full and the pool, the handlers and the scaling arithmetic all
do what it says they do (§1). The compiler bugs it re-ran, plus two more the
re-run found, are **[compiler_plan.md](../compiler_plan.md)** — independent of
every phase above, and recommended between the fonts and G1.
