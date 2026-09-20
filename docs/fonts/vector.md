# "Why cells and pixels? Are we doing SVG style?"

> Part of [the font plan](README.md). **This file is the answer to your
> counter-question** ([questions.md §2 Q5](questions.md)). Facts marked
> *(checked)* were read in the code on 2026-09-20, and *(measured)* ones were
> run.

You asked, against the recommendation of a 12 × 24 cell:

> but why are we thinking of cells and pixels? are we doing svg style?

It is the right question, and **three different things are tangled inside
it.** One of them is not a choice, one of them is a choice we had already made
without discussing it, and one was still open when this was written — §3, now
decided both ways at once.

---

## 1. Pixels are not a choice

The screen is 1,280 × 720 **discrete pixels**. Whatever a font is made of, the
last step is always "which pixels, and how bright". That is true of this
machine, of your laptop, and of every phone — a vector font does not put
curves on a screen, it puts pixels there.

So the question is never *whether* to rasterise. It is **when**:

| | when it rasterises | what it keeps |
|---|---|---|
| your desktop OS | at run time, the first time a glyph is used at a size | a **cache of pixel bitmaps**, keyed by glyph and size |
| this machine, as planned | ahead of time, on the host, into a `.pf` | the same bitmaps, in a file |

**They are the same thing at different moments.** FreeType — which is what
Linux, Android and this plan's own tooling all use — rasterises outlines into
a bitmap cache and then blits bitmaps. If the GAC rendered outlines at run
time it would have to cache the result per size, and a cache of rasterised
glyphs at one size **is a bitmap font**. We would have built `.pf` again, more
slowly, in Python.

So: **yes, we are using real vector fonts** — you point the tool at a
TrueType or OpenType file and it rasterises from the outlines, with the
hinting and the shapes a type designer drew. Verified on this machine against
DejaVu Sans Mono *(measured)*. What we are *not* doing is carrying the
outlines onto the guest and rasterising there, because that buys nothing and
costs a rasteriser.

*(An aside on the literal words: SVG is not really a font format. There is an
`SVG` table in OpenType, used almost only for colour emoji. The vector format
that matters is the outline — TrueType quadratics, OpenType cubics — and that
is what we read.)*

---

## 2. "Pixels" in the sense you probably meant: 1-bit vs coverage

If the objection is *"a pixel font looks blocky and old"* — that is right,
and it is not about vectors. It is about whether a pixel can be **partly**
inked.

**Today every pixel is ink or nothing.** `Font` stores `FF FF FF FF` per
inked pixel and four zero bytes otherwise, and `TEXT` draws with
`(under & ~mask) | (over & mask)` *(checked: `gac.py:341-362, 647-677`)*.
There is no grey. **That, not the absence of vectors, is what makes the text
look like 1985.**

The fix is **antialiasing** — storing 8-bit coverage per pixel and blending
by it — and it is cheap, because for a known foreground and background the
map from coverage to output byte is affine, so it is a 256-entry
`bytes.translate` table, the same shape as the one `blend_table()` builds for
`BlendInk` *(checked: `gac.py:258-262`)*. **Not the same table**: that one is
indexed by the destination byte for a fixed source and alpha, and F5's is
indexed by the coverage for a fixed foreground and background. Three of them,
one per channel, per (fg, bg) pair — and since a console uses about two such
pairs, caching the rendered glyph rows per pair makes antialiased text cost
what 1-bit text costs from the second line onwards.

**And where there is no background**, which is every text call on the machine
today — `disp_text` and friends all pass `bg = 0` *(checked:
`display.c:632-684`)* — the glyph's coverage **is** per-pixel alpha, so
Phase 9's `src_alpha_row`/`blend_span` draws it against whatever is under it,
with no table at all *(checked: `gac.py:287-318`)*. Both paths, or F5 ships
and nothing on the machine looks any different ([build.md §3](build.md)).

That is phase **F5** and it is the single biggest visual win available.

So the honest ranking of what makes text look modern:

1. **Antialiasing** (F5) — grey edges. The largest difference by far.
2. **Size** (F4) — 12 × 24 rather than 5 × 7.
3. **A real typeface** (F1) — DejaVu rather than a hand-drawn 5 × 7.
4. Vector rendering at run time — **no visible difference at all**, once 1–3
   are done, because the output is identical pixels.

---

## 3. "Cells" is the real choice, and the answer is "both"

This is the part of your question that is genuinely undecided, and I should
have raised it rather than assuming.

**A "cell" means every character advances the same number of pixels** — a
monospaced font. `TEXT` advances exactly `cell_w` per character, with no
per-glyph advance stored anywhere *(checked: `gac.py:651`, and `Font` stores
only `cell_w, cell_h, first, count, rows`)*. Modern UI text is **proportional**:
`i` is narrow, `W` is wide, and the difference is most of why a proportional
font looks like a document and a monospaced one looks like a terminal.

I wrote "proportional: not now" into the plan without asking you. Here is the
actual trade:

|  | monospaced (cells) | proportional |
|---|---|---|
| looks like | a terminal | a modern UI |
| a column of numbers lines up | yes, free | no, you must measure |
| "how wide is this string?" | `len * cell_w` | a per-glyph sum |
| the device needs | what it has | a per-glyph advance table |
| `explorer.c`'s row idiom | works | **breaks** |

That last row matters more than it looks. Three programs compose a row as
`char row[COLS+1]` with `blank`/`place`/`place_right` and draw it with **one**
`disp_text`, precisely so columns line up without measuring pixels
*(checked: `explorer.c:186-207`, and two byte-identical copies)*. Proportional
text would break every one of them.

**But they would keep using the monospaced font.** Nothing forces one font on
the whole machine once slots exist — which is the whole point of F2.

### What proportional would actually cost

Smaller than it sounds, because the drawing already works:

- **`Font`** gains a `advance[count]` byte array — 95 bytes.
- **`_text`** computes a prefix sum of advances instead of `i * cell_w`. The
  big-integer mask join is unaffected; the masks simply stop being equal
  width *(checked: the join at `gac.py:363-367` does not care)*.
- **`_text`'s clipping** is the same prefix sum again. It takes the visible
  span as `len(text) * cell_w` and then slices the row mask at `left * 4`
  *(checked: `gac.py:648-663`)*, which assumes a uniform advance just as
  plainly as the drawing does.
- **`gac_text()`'s chunking.** A string over 4,072 characters goes in pieces
  and the guest advances `x` by `chunk * gac_cell_w` between them *(checked:
  `gac.c:207-213`)*. With per-glyph advances that is wrong — and wrong only
  for long strings, which is the worst kind of bug to leave lying about.
- **The guest** needs `disp_text_w(slot, s)`, which [the GUI](../gui/api.md)
  wants anyway for laying out buttons.
- **`.pf`** gains an optional advance table; absent means monospaced, and the
  device is told which by `SET_FONT2`'s `flags` and `advance` words
  ([device.md §3.1](device.md)).

### The recommendation

**Both, and pick per font.** The console, `explorer.c`, `disc.c` and anything
that lines up columns keeps a monospaced font; the GUI's buttons and labels
get a proportional one, where it will look markedly better. A `.pf` says
which it is, and `TEXT` follows the font rather than a global setting.

That becomes phase **F6**, after antialiasing — and
[questions.md §3 Q1](questions.md) **decided it**: both, chosen per font. The
console and anything with columns keeps a monospaced font; the GUI's own
chrome gets a proportional one. A `.pf` says which it is, through
`SET_FONT2`'s `flags` and `advance` words ([device.md §3.1](device.md)), and
`TEXT` follows the font rather than a global setting.

---

## 4. So, to answer the question directly

- **Are we doing SVG style?** We are using real vector typefaces as the
  source, rasterised on the host. Nobody renders outlines on a screen; they
  render them into pixels, and the only question is when. Doing it ahead of
  time is strictly cheaper here and looks identical.
- **Why cells?** Because that is what the device does today, not because it
  is better. It is right for a terminal and wrong for a GUI, and §3 says we
  should have both.
- **Why pixels?** Because the screen is pixels. The thing that makes them
  look bad is that they are currently 1-bit, and F5 fixes exactly that.
