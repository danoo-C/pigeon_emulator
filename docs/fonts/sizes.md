# How small the text is, and what each size would give

> Part of [the font plan](README.md). Facts marked *(checked)* were read in the code on 2026-09-20, and *(measured)* ones were run.

## 1. Why it is small

The only font on the machine is `FONT[]` in `lib/pigeon/display.c` *(checked:
`display.c:533-629`)*. Its glyphs are **5 × 7 ink in a 5 × 8 glyph box**, drawn
on a **6 × 8 cell**, covering printable ASCII `0x20`–`0x7E`, 95 glyphs, **760
bytes** *(checked)*. The kernel console puts it on a 6 × 9 cell — the ninth
row is the line gap *(checked: `kernel.c:73-74`)*.

That cell is a constant, so **the bigger the screen, the smaller the text
looks**:

| screen | console, 6 × 9 cell |
|---|---|
| 192 × 108 | 32 × 12 |
| 640 × 360 | 106 × 40 |
| **1280 × 720** | **213 × 80** |
| 1920 × 1080 | 320 × 120 |

213 columns is about three times what a terminal is normally asked to be.
Nothing is wrong; there is simply one font, sized for a 192 × 108 screen, and
every mode since has kept it.

---

---

## 2. Verified: what each size actually gives you

You asked me to check the sizes, because you want a font that looks genuinely
clean. Measured rather than guessed — the console dimensions are
`DISP_W / cell_w` by `DISP_H / cell_h`, and the host cost is `Font`'s
precomputed masks ([§4.2](#42-what-a-font-costs-the-host)):

| glyph | cell | 720p | 1080p | host KB | over 8 px? |
|---|---|---|---|---|---|
| 5 × 7 *(today)* | 6 × 9 | **213 × 80** | 320 × 120 | 50 | |
| 8 × 8 | 9 × 10 | 142 × 72 | 213 × 108 | 65 | |
| **8 × 16** | 9 × 18 | **142 × 40** | 213 × 60 | 122 | |
| 8 × 16 | 8 × 16 | 160 × 45 | 240 × 67 | 114 | |
| 10 × 20 | 11 × 22 | 116 × 32 | 174 × 49 | 165 | **F4** |
| **12 × 24** | 13 × 26 | **98 × 27** | 147 × 41 | 214 | **F4** |
| 14 × 28 | 15 × 30 | 85 × 24 | 128 × 36 | 270 | **F4** |
| 16 × 32 | 18 × 36 | 71 × 20 | 106 × 30 | 344 | **F4** |

**And the finding that changes the plan.** A comfortable terminal is 80 to
100 columns. At 1280 pixels wide that needs:

| columns wanted | cell width | glyph width | |
|---|---|---|---|
| 80 | 16 px | ~15 px | **needs F4** |
| 100 | 12 px | ~11 px | **needs F4** |
| 120 | 10 px | ~9 px | **needs F4** |
| 142 | 9 px | 8 px | fits today |

> **8 × 16 is not actually enough.** It takes 720p from 213 columns to 142 —
> a real improvement, and still about 40% denser than a normal terminal.
> **Everything genuinely comfortable is more than 8 pixels wide**, so the
> `bpr` stride fix ([device.md §1.2](device.md))
> is not a later nicety for 1080p — it is what "clean" requires at 720p.

So **F4 moves up**, and the recommended default becomes **12 × 24 on a 13 × 26
cell: 98 × 27 at 720p**, which is a normal terminal. 8 × 16 stays as the
stopgap that needs no device change, and 5 × 7 stays for dense lists.

The `cell_w * cell_h <= 4096` cap is not in the way — 18 × 36 is 648, and even
32 × 64 is 2048. The **total** across eight slots is what needs the cap from
[questions.md §1 Q3](questions.md): eight 16 × 32 fonts would be 2.7 MB on the host.

---
