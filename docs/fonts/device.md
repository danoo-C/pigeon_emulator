# What the accelerator can draw, and the font slots it needs

> Part of [the font plan](README.md). Facts marked *(checked)* were read in the code on 2026-09-20, and *(measured)* ones were run.

## 1. What the device allows today, exactly

`SET_FONT` takes `address, glyph_w, glyph_h, cell_w, cell_h, first, count`
and refuses anything outside these *(checked: `gac.py:679-689`)*:

| rule | value |
|---|---|
| `0 < glyph_w <= 8` | **8 pixels wide, at most** |
| `cell_w >= glyph_w`, `cell_h >= glyph_h` | the cell contains the glyph |
| `cell_w * cell_h <= 4096` | so a cell up to 64 × 64 |
| `0 < count`, `first + count <= 256` | any single-byte character range |
| font blob | `count * glyph_h` bytes, **one byte a glyph row** |

**The font does not go through the 4 KB data window.** `SET_FONT` passes an
*address* and the device reads the bytes straight out of RAM *(checked:
`gac.py:687`)*, so a font's size is not a bus problem and needs no chunking.
`TEXT` is what chunks, at 4,072 characters a command *(checked:
`gac.c:26-27`)*.

### 1.1 The good news: a readable font needs no device change

**8 × 16 glyphs on a 9 × 18 cell fit the existing rules exactly** — `glyph_w`
is 8, which is the limit but is *within* it, and `9 * 18 = 162`, far under
4096. 95 glyphs × 16 rows is **1,520 bytes** of font data.

| screen | 6 × 9 cell (today) | **9 × 18 cell (8 × 16 font)** |
|---|---|---|
| 640 × 360 | 106 × 40 | 71 × 20 |
| **1280 × 720** | **213 × 80** | **142 × 40** |
| 1920 × 1080 | 320 × 120 | 213 × 60 |

So the thing you actually feel can be fixed **with a font file and nothing
else**. That is Phase 1.

### 1.2 The bad news: 8 is a hard ceiling, and scaling up cannot dodge it

The obvious cheap trick — take the 5 × 7 and double every pixel — **does not
work**. A 2× glyph is 10 pixels wide and `glyph_w <= 8` refuses it
*(checked)*. There is no scale factor above 1 that keeps a 5-wide glyph under
8. So:

> **8 × 16 is the largest font this device can draw today, full stop.**
> Anything bigger — and 1080p will want bigger — needs the storage format
> changed.

The limit is *only* the storage format, not the drawing. `Font.__init__`
reads `data[g * glyph_h + r]`, one byte a row, and tests `bits & (0x80 >> c)`,
which yields 0 for `c >= 8` *(checked: `gac.py:358-360`)*. The fix is a
stride: `bpr = (glyph_w + 7) // 8`, read `data[(g * glyph_h + r) * bpr + c // 8]`
and test `0x80 >> (c & 7)`.

**`_text` needs no change at all.** It builds a mask over whole 4-byte pixels
and ORs it in with one big integer a pixel row *(checked: `gac.py:647-677`)* —
that is already width-agnostic. This is worth saying plainly because it makes
wide glyphs a small change rather than a rewrite.

---

---

## 2. The real blocker: there is one font slot, and it is everyone's

`GAC` holds a single `self.font`, and any `SET_FONT` replaces it *(checked:
`gac.py:381, 687-688`)*. That alone would be survivable. These three facts
together are not:

1. **Every program uploads the 5 × 7 font at startup.** `disp_init()` calls
   `gac_set_font` whenever the GAC is there — it is the *only* caller in the
   tree *(checked: `display.c:731-733`)*. So a GUI that installs a big font
   loses it the moment anything calls `disp_init()` again.
2. **The guest library caches the cell width in one static.** `gac_cell_w` is
   a single file-scope variable, set by `gac_set_font` and used to advance
   between chunks of a long string *(checked: `gac.c:32, 191, 213)`*.
3. **The kernel console assumes a 6-pixel cell**, as do all 97 call sites of
   `disp_text`/`disp_textn`/`disp_char` *(checked)*.

So a GUI app that uploads a 9 × 18 cell silently changes the geometry the
console and every other drawing program depend on. **Font slots are not a
convenience — they are what lets a GUI use a big font without breaking the
machine around it.**

This was foreseen. Phase 3's Q6 asked "One font, or several?" and answered
"one font slot now. A `font` argument can be added to `SET_FONT` and `TEXT`
later without renumbering anything" *(checked:
`docs/gac/plans/phase3_gac.md:290-299`)*. This is that later.

---

---

## 3. The design

### 3.1 Slots on the device

`SET_FONT` gains a **slot** and `TEXT` gains a **font id**. Two ways, and
[questions.md §1 Q1](questions.md) asks which:

**(a) A slot word added to both commands.** `SET_FONT` becomes
`slot, address, glyph_w, glyph_h, cell_w, cell_h, first, count`; `TEXT`
becomes `dst, x, y, fg, bg, font, length`. Both grow by one word, which
changes their `_STRUCTS` entry and so the word count `BATCH` checks — **old
guest code would be refused**, so this must be gated on a feature bit and
`display.c` must send the new shape only when the bit is there.

**(b) New commands 17 `SET_FONT2` and 18 `TEXT2`,** leaving 9 and 10 exactly
as they are. Nothing old breaks, at the cost of two command numbers and a
little duplication in the device.

**Recommendation: (b).** Commands 17+ are free *(checked)*, `BATCH`'s shape
table stays honest for the old commands, and the failure mode for a guest
built before the feature is "the command is not there" rather than "my
arguments are the wrong length". `FEATURE_FONTS = 8` in `INFO` says which
machine you are on — bit 8 is free and `gac_features()` already hands the
word over with no extra bus command *(checked: `gac.c:63-66`)*.

**How many slots?** [questions.md §1 Q2](questions.md). A slot costs host memory only when filled. Eight
is the recommendation: enough for a GUI's small/normal/large/bold plus the
console's, with room, and small enough that `Dict[int, Font]` stays trivial.

**Slot 0 is the console's**, by convention, so `disp_init()` keeps writing
where it always did and nothing existing changes meaning.

### 3.2 What a font costs the host

`Font` precomputes every glyph row as a ready-to-OR mask of `cell_w * 4`
bytes *(checked: `gac.py:341-367`)*. Per font, roughly:

```
    count * glyph_h * (4 * cell_w + 33)  +  count * (8 * cell_h + 56)
```

| font | cell | payload | with CPython overhead |
|---|---|---|---|
| 5 × 7 (today) | 6 × 8 | 18,240 B | **≈ 55 KB** |
| 8 × 16 | 9 × 18 | ~54 KB | **≈ 124 KB** |
| worst the rules allow | 64 × 64, 256 glyphs | — | **≈ 4 MB** |

Eight slots of 8 × 16 is about 1 MB on the host, which is nothing. Eight of
the worst case is 32 MB, which is not — so [questions.md §1 Q3](questions.md) asks whether the device
should cap the *total* across slots rather than only `cell_w * cell_h <= 4096`
per font.

---

### 3.3 The guest side

`gac_cell_w` becomes per-slot, and the library grows:

```c
int      gac_font_load(unsigned slot, char *glyphs, unsigned glyph_w, unsigned glyph_h,
                       unsigned cell_w, unsigned cell_h, unsigned first, unsigned count);
int      gac_text_font(unsigned dst, int x, int y, unsigned fg, unsigned bg,
                       unsigned slot, char *s, unsigned n);
unsigned gac_font_cell_w(unsigned slot);   /* 0 if the slot is empty */
unsigned gac_font_cell_h(unsigned slot);
```

and `<pigeon/display.h>` grows a selected font, so the 97 existing call sites
keep working unchanged:

```c
int  disp_font_load(unsigned slot, char *path);   /* a .pf off the disc */
void disp_font(unsigned slot);                    /* what disp_text draws with now */
unsigned disp_cell_w(void);                       /* of the selected font */
unsigned disp_cell_h(void);
```

**`disp_cell_w()`/`disp_cell_h()` are the important pair.** Every UI in
`user/` derives its layout from the macros `GLYPH_W`/`GLYPH_H` *(checked:
`explorer.c:45-60`, `disc.c:43-56`, `files.c:58-73`, `installer.c:34-40`,
`graph.c:768-787`)*, which are compile-time constants. With more than one
font those have to become **run-time values**, or every layout is wrong for
every font but one. That is the single largest ripple this change causes, and
[the GUI's scaling](../gui/scaling.md) is where it lands.

### 3.4 Software fallback

`display.c` draws text itself on a machine with no GAC *(checked)*, reading
`FONT[]` directly and advancing `GLYPH_W + 1`. A loaded font has to be
drawable there too, or `-sprite`-style divergence appears: the same program
looks different on two machines. The software path needs the same
`bpr`-stride reader. It is slow and that is acceptable — it always was.

---
