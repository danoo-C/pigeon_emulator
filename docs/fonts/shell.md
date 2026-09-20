# Later: the shell with a font you can change

> Part of [the GUI plan](../gui/README.md). **Status: recorded, not planned,
> 2026-09-20.** You asked for this to be written down so it is not
> forgotten ([questions.md Q7](questions.md)), and explicitly **not built
> now**. Facts marked *(checked)* were read in the code on 2026-09-20.

**What you said:**

> think about porting the shell to use the GUI library or just the fonts,
> that way i could zoom the fontsize and use different fonts. and i would be
> able to then use the panel.bin to change the font and ctrl+scroll in the
> shell or just type the fontsize in panel.bin. but dont do it now, but
> document it, so i dont forget about it.

---

## 1. It is a font job, not a GUI job

The instinct to say "port the shell to the GUI library" is natural and, it
turns out, unnecessary. **The kernel console already does the hard part.**

- **It already resizes at run time.** Phase 6 made it so: `con_resize()`
  recomputes `con_cols` and `con_rows` and relays the grid, and the mode
  picker calls it whenever the screen changes *(checked: `kernel.c:1093-1094`,
  `con_resize`)*.
- **Its cell is two constants**, `CON_CELL_W 6u` and `CON_CELL_H 9u`
  *(checked: `kernel.c:73-74`)*, which is the only reason the size is fixed.
- **`con_cols` and `con_rows` are already variables**, computed as
  `DISP_W / CON_CELL_W` and `DISP_H / CON_CELL_H` *(checked:
  `kernel.c:2061-2062`)*.

So changing the shell's font is: make those two constants **variables** taken
from the selected font slot, and call `con_resize()`. The console reflows by
machinery that already exists and is already tested.

**That is much smaller than porting anything.**

---

## 2. The static grids are already big enough — verified

The obvious worry is the console's fixed arrays: `con_grid`, `con_look`,
`back_grid`, `back_look`, sized `CON_MAX_COLS × CON_MAX_ROWS` from
`DISPLAY_MAX_W / CON_CELL_W` *(checked: `kernel.c:77-78, 170-171, 201-202`)*.

**They do not need to grow, because a bigger font needs *fewer* cells.** The
arrays are sized for the *smallest* cell, and the smallest cell is the one
they are already sized for — the built-in 5 × 7 on a 6-wide cell. Every font
worth switching to is wider, so it needs fewer columns and fewer rows:

| font | cell | columns at 1280 | vs today's 216 slots |
|---|---|---|---|
| 5 × 7 *(today)* | 6 × 9 | 213 | the maximum |
| 8 × 16 | 9 × 18 | 142 | fits |
| 12 × 24 | 13 × 26 | 98 | fits easily |
| 16 × 32 | 18 × 36 | 71 | fits easily |

So zooming the shell's font costs **no extra memory at all**. The one rule to
keep is that nothing may select a font whose cell is *narrower* than 6 px,
which is also the smallest font that exists.

---

## 3. What it would take

1. **Font slots** ([the font plan](README.md) F2) — without them, the shell's font
   is the same global slot every program clobbers, so this cannot work first.
2. **`CON_CELL_W`/`CON_CELL_H` become variables**, read from the selected
   slot's cell, plus a `con_resize()` on change.
3. **A way to say which font.** Three, in increasing effort:
   - `/etc/boot.conf` names a font at startup — one line, matches how the
     splash is already configured *(checked)*;
   - a system call so `panel.bin` can set it live;
   - **Ctrl + wheel in the shell**, which is the nice one — and the kernel
     *already* reads mouse wheel events in its line editor and throws away
     everything that is not a wheel *(checked: `kernel.c:1219-1223`)*. Adding
     "if Ctrl is held, change the font size instead of scrolling" lands in
     code that already exists.
4. **`panel.bin`** gets a font picker, which it wants anyway
   ([questions.md Q7](questions.md)).

---

## 4. Why not the GUI library

Because the console is not a GUI. It is a character grid with a line editor,
scrollback, paging and `read()` semantics that the kernel's own callers
depend on *(checked)*. Rebuilding that on widgets would risk all of it to
gain nothing — the thing you actually want is a different font, and §1 says
that is two variables.

**The GUI library's console *widget* ([api.md §9](../gui/api.md#9-the-console-widget))
is a different thing entirely**: a view of one program's output, inside a GUI
app. It is not the shell and does not try to be.

---

## 5. When

After F2 at the earliest, and there is no reason it must come before or after
the GUI library — it is independent of it. It is small enough to be a good
thing to do on a day when the GUI plan feels large.
