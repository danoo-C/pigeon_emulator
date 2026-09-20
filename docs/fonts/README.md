# Fonts: text you can actually read

> **Status: design, 2026-09-20. Nothing is built. This is the plan that gets
> built first** — you said *"first, get the font system working"*, and
> measuring the sizes agreed with you. [questions.md](questions.md) records
> every answer, and your
> counter-question's answer in [vector.md](vector.md). **Every question is now
> decided.** Facts marked *(checked)* were read in the
> code on 2026-09-20, and *(measured)* ones were run.

---

## What this is for

> right now when i use 720p the text is extremely small, barely readable.

At 1280 × 720 the kernel console is **213 × 80 characters**, because the only
font on the machine is a 5 × 7 drawn on a 6 × 9 cell, sized for a 192 × 108
screen *(checked: `display.c:533-629`, `kernel.c:73-74`)*. Every mode since
has kept it, so the bigger the screen, the smaller the text looks.

This plan makes text **big**, **clean** and **changeable**, and it is
deliberately separate from [the GUI plan](../gui/README.md), which depends on
it rather than the other way round.

---

## The files

| file | what is in it |
|---|---|
| **README.md** (this) | why, the phases, what is not in it |
| [sizes.md](sizes.md) | why it is small now, and **what every size would actually give you** — measured |
| [device.md](device.md) | what the accelerator allows, the one-slot problem, and the slot design |
| [format.md](format.md) | the `.pf` file, and `tools/make_font.py` — including importing a real TTF |
| [vector.md](vector.md) | **your counter-question answered:** vectors, pixels, cells, and what actually makes text look modern |
| [questions.md](questions.md) | every question, your answers, and what was decided |
| [build.md](build.md) | **the plan checked against the code** — what holds, eight things that do not, and the questions they raise |
| [shell.md](shell.md) | **later:** the shell with a font you can change, recorded so it is not lost |

---

## What was found

**1. A readable font needs no device change — but a *comfortable* one does.**
`SET_FONT` allows glyphs up to 8 pixels wide, so **8 × 16 fits today**
*(checked: `gac.py:679-689`)* and takes 720p from 213 columns to 142. But a
normal terminal is 80–100 columns, which needs glyphs 11–15 px wide — **over
the limit**. So the stride fix is not a later nicety; it is what "clean"
requires ([sizes.md](sizes.md)).

**2. There is one font slot, and every program clobbers it.** The device
holds a single font, any `SET_FONT` replaces it, and `disp_init()` re-uploads
the 5 × 7 on *every program start* — the only caller in the tree *(checked:
`display.c:731-733`)*. Slots are what let a big font coexist with the console
([device.md](device.md)).

**3. What makes text look old is not the lack of vectors — it is 1-bit
pixels.** Every glyph pixel is ink or nothing, drawn with a mask OR
*(checked: `gac.py:341-362, 647-677`)*. Antialiasing is the biggest visual
win available, and it is cheap: for a known foreground and background,
coverage → colour is a 256-entry `translate` table, the same shape as the one
the device builds for `BlendInk` but indexed by coverage rather than by the
destination. Where there is no known background — which is **every text call
on the machine today** — the glyph's coverage is per-pixel alpha and Phase
9's blend draws it ([vector.md](vector.md), [build.md §3](build.md)).

**4. A real font off your PC works today.** `pygame.freetype` is already
installed with the pygame client, found **26** TrueType faces on this
machine, and rasterises them with full antialiasing — 79 to 156 coverage
levels *(measured)*. DejaVu Sans Mono at size 20 gives a 12-pixel advance,
exactly the 98-column cell ([format.md](format.md)).

---

## The sizes, measured

| glyph | cell | 720p | 1080p | host KB | over 8 px? |
|---|---|---|---|---|---|
| 5 × 7 *(today)* | 6 × 9 | **213 × 80** | 320 × 120 | 50 | |
| 8 × 16 | 9 × 18 | **142 × 40** | 213 × 60 | 122 | |
| **12 × 24** | 13 × 26 | **98 × 27** | 147 × 41 | 214 | **yes** |
| 16 × 32 | 18 × 36 | 71 × 20 | 106 × 30 | 344 | **yes** |

---

## Phases

**All of these come before any GUI phase.**

| | what | side | needs |
|---|---|---|---|
| **F1** | The `.pf` format, **`font.c`**, `tools/make_font.py` (hand-drawn and `--ttf`), an 8 × 16 font in `/etc/font/`, **the console on it**, and **`k_tidy()` putting the font back** | guest | nothing |
| **F2** | **Font slots**: commands 17/18 *with `flags` and `advance` words*, `FEATURE_FONTS`, per-slot cell in the library | both | F1 |
| **F3** | `disp_cell_w()`/`disp_cell_h()`, layout from run-time metrics | guest | F2 |
| **F4** | **Wide glyphs** (over 8 px) — the `bpr` stride. What 98 columns needs | device | F2 |
| **F5** | **Antialiased text**: 8-bit coverage, the `translate` path where the background is known and Phase 9's blend where it is not | device | F4 |
| **F6** | **Proportional fonts**, chosen per font — monospaced for columns, proportional for GUI chrome | both | F5 |

**F1 on its own already helps**, and needs nothing from the *device* — but it
does need one line from the kernel: `k_tidy()` does not put the accelerator's
font back when a program ends, so a program that loads one leaves the shell
drawing its 6 × 9 grid with 8 × 16 glyphs ([build.md §2](build.md)). F4 and F5
are what make it genuinely good.

> **Every phase above carries an amendment from [build.md](build.md)**, which
> checked this plan against the code and found eight things wrong or missing.
> The amendments are folded in here and in the files they belong to; the
> reasoning stays there.

Recorded but not scheduled: **[shell.md](shell.md)**, the shell with a font
you can change — which turns out to be two variables and a `con_resize()`.

---

## What this is not

- **A rasteriser on the guest.** Outlines are rasterised on the host, ahead
  of time, which is what every system does in effect ([vector.md](vector.md)).
- **Text shaping** — ligatures, kerning pairs, bidirectional text, combining
  marks. Single-byte character sets only, 256 glyphs at most *(checked)*.
- **Colour fonts or emoji.**
- **Hinting at small sizes.** FreeType's hinting is applied when the tool
  rasterises; nothing re-hints on the guest.
- **A font cache on the device.** A `.pf` is already the cache.
