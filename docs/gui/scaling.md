# Dynamic scaling: design once, run at any size

> Part of [the GUI plan](README.md). **Status: design, 2026-09-20. Questions
> open ([questions.md](questions.md)).** Facts marked *(checked)* were read
> in the code on 2026-09-20.

**What you asked for:** say you designed the app at 720p, and when the
resolution changes the positions scale automatically — stretch, or keep the
aspect ratio — scaling *positions*, not stretching pixels.

---

## 1. Design coordinates

Every widget stores the coordinates you wrote. The screen's pixels are
derived, never stored:

```c
gui_design(1280, 720);              /* "I laid this out for 720p"        */
gui_scale(GUI_SCALE_ASPECT);        /* NONE | STRETCH | ASPECT | INTEGER */
```

| mode | what it does |
|---|---|
| `GUI_SCALE_NONE` | design pixels are screen pixels; a bigger screen shows more empty space |
| `GUI_SCALE_STRETCH` | x and y scale independently; fills the screen, changes proportions |
| `GUI_SCALE_ASPECT` | one factor, `min(W/Wd, H/Hd)`, centred, with a margin on two sides |
| `GUI_SCALE_INTEGER` | as `ASPECT`, but the factor is rounded **down to a whole number** |

`GUI_SCALE_INTEGER` earns its place on this machine specifically: every glyph
is a bitmap ([the font plan](../fonts/README.md)), and a bitmap font scaled by 1.5 looks
worse than one scaled by 1. At 1920 × 1080 with a 1280 × 720 design, `ASPECT`
gives 1.5 and `INTEGER` gives 1 — smaller, but crisp. Which is right depends
on the app, so it is a mode rather than a rule.

---

## 2. Scale the corners, not the size

The obvious implementation scales `x`, `y`, `w`, `h` independently:

```c
dx = x * W / Wd;   dw = w * W / Wd;      /* NO */
```

and it produces **one-pixel gaps and overlaps between adjacent widgets**,
because `(x*W/Wd) + (w*W/Wd)` is not `((x+w)*W/Wd)` once the division
truncates. Two boxes laid out edge to edge at design size stop touching.

So the library scales the **corners**:

```c
x0 = x * W / Wd;          /* the left edge   */
x1 = (x + w) * W / Wd;    /* the right edge  */
dw = x1 - x0;
```

Now a widget's right edge and its neighbour's left edge are computed from the
same design number, so they land on the same pixel, always. Rows of buttons
and table columns stay flush at every scale.

---

## 3. The arithmetic, and the two traps in it

**There is no floating point** — it is rejected in the lexer *(checked:
`lexer.py:161-165`)*. All of this is integer multiply-then-divide, which is
fine, but two things bite:

1. **Divide is only correct for non-negative operands.** `/` and `%` lower to
   the machine's unsigned `DIV` *(checked: `codegen.py:512-517`,
   `design/02-language.md:71-75`)*. A widget at `x = -20` — deliberately
   off the left edge, which existing programs do — would scale to nonsense.
   The library takes the sign out first:

   ```c
   static int gui_scale_axis(int v, int num, int den) {
       if (v < 0) return -(int)(((unsigned)(-v) * (unsigned)num) / (unsigned)den);
       return (int)(((unsigned)v * (unsigned)num) / (unsigned)den);
   }
   ```

2. **Overflow.** The multiply happens before the divide, so the intermediate
   is `coordinate × screen width`. At the extremes this machine allows —
   a 4096-pixel design coordinate and a 4096-pixel screen — that is
   16,777,216, comfortably inside 32 bits. **No overflow is possible at any
   size the machine can display**, so the library does not carry a slow
   64-bit path. Worth stating because it is the usual reason to.

For `ASPECT` and `INTEGER` there is also a centring offset, computed once per
layout, not per widget:

```
    f   = min(W / Wd, H / Hd)        as a fraction, or floored for INTEGER
    ox  = (W - Wd * f) / 2
    oy  = (H - Hd * f) / 2
```

---

## 4. The part that is easy to miss: the font does not scale

**Positions scale continuously. Bitmap glyphs do not.** Scale a layout by 1.5
and keep drawing 8 × 16 text and every label overflows the box that was
measured for it. This is the single thing that makes "just scale the
coordinates" not work, and it is why [the font plan](../fonts/README.md) comes first.

The library resolves it at layout time, using the fonts that happen to be
loaded:

```c
gui_font_load(1, "/etc/font/mono5x7.pf");
gui_font_load(2, "/etc/font/mono8x16.pf");
gui_font_auto(1);        /* pick a loaded font to suit the scale */
```

`gui_font_auto()` picks, for each layout, the loaded font whose cell height
is **closest to but not over** the design cell height times the scale. At 720p
designing with 8 × 16: scale 1 keeps 8 × 16; scale 0.5 drops to 5 × 7; scale 2
would take a 16 × 32 if one were loaded and keep 8 × 16 if not. Nothing is
stretched, so text stays crisp at every size; it changes *which* font is used,
which is what a bitmap UI has to do.

`gui_font(slot)` turns that off and pins one font.

**A consequence worth stating:** a widget's natural size has to be measured
with the font the layout will actually use, so `gui_text_w(slot, s)` is part
of layout, not of drawing. This is also where every existing UI would have to
change — their layout comes from the compile-time macros `GLYPH_W`/`GLYPH_H`
*(checked: `explorer.c:45-60`, `graph.c:768-787`, and four more)*, which stop
being constants as soon as there is more than one font.

---

## 5. When the screen changes under you

**No program in `user/` handles this today.** `disp_follow()` and
`disp_generation()` exist and are used only by the kernel *(checked:
`kernel.c:1209, 1369, 1567`)*. So a mode change mid-run leaves
width-formatted caches stale — `disc.c`'s `lines[]` are still formatted to
the old column count *(checked: `disc.c:264`)*, `graph.c`'s `ys[]` is still
indexed to the old plot width *(checked: `graph.c:817-818`)*.

`gui_poll()` compares `disp_generation()` every frame *(checked: it changes
whenever the mode does)*. When it moves:

1. re-resolve every widget's device rectangle,
2. re-pick the font if `gui_font_auto()` is on,
3. raise **`GUI_RESIZE`** so the app can rebuild anything of its own,
4. damage the whole screen.

**And treat the pointer as unknown until it next moves.** HID has no idea the
screen resized: it keeps reporting the last position it was given, which may
be off the new screen entirely *(checked: `hid.py:121-124` — no mode
awareness)*. Reporting a hover from that stale position would light up the
wrong widget, so the library reports no hovered widget until a fresh position
arrives.

---

## 6. Hit-testing stays in device pixels

The pointer arrives in **screen pixels** *(checked: both front ends clamp to
`0..disp_w-1`)*. There are two ways to reconcile that with design
coordinates, and only one is exact:

- map the pointer *back* into design space — needs a divide, and truncation
  makes it lossy, so the widget you hit is not always the widget you see;
- **hit-test against the resolved device rectangles** — exact, because they
  are the very pixels that were drawn.

The library does the second. It also means `e->x` and `e->y`, which are
widget-local, are in device pixels; an app that wants design units divides,
and mostly does not care.

**Outside the letterbox** — the margin `ASPECT` and `INTEGER` leave — there is
no widget, and the library reports none rather than clamping to the nearest.
