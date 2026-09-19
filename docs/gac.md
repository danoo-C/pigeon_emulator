# The GAC: drawing done by the machine

> **Status: built,** `emulator/devices/gac.py` on IO channel 10, and
> `lib/pigeon/gac.h`. How it came to be is [gac/](gac/README.md); this is how
> to use it. It draws into [video memory](vram.md) and into RAM.

The graphics accelerator draws a shape in **one bus command**. Clearing a
1280 × 720 screen a store at a time is 3.7 million guest instructions; here it
is one `FILL`, 0.7 ms of the host's time. A whole screen of text is a few dozen
commands.

**You probably already use it.** `<pigeon/display.h>` sends its clears,
rects, lines, circles, discs, text, scrolls and images here whenever the
machine has one, and draws the same pictures in software when it does not.
Include `<pigeon/gac.h>` yourself only for what `display.h` does not have:
signed coordinates, blits between surfaces, scaled blits, blending a whole
image, and your own font.

---

## 1. Surfaces

Every command names the surface it draws on:

- **`GAC_SCREEN`**, handle 0: the screen of the current mode, in video
  memory;
- **a VRAM handle** from `vram_alloc()`;
- **a rectangle of RAM,** registered once with
  `gac_ram_surface(address, w, h)`. The handle it answers starts at
  `0x80000000`. It must be wholly in RAM and at or above the program's load
  address, or be the power-on screen at `DISPLAY_START`. That is how the GAC
  draws on the 192 × 108 screen, which lives in RAM.

A surface that is not there, such as one freed or never registered, makes the
command answer 0 and draw nothing. The kernel frees what a program registered
when it ends.

## 2. Everything clips

Coordinates are **signed**. Whatever the numbers, **nothing is written outside
the surface**: a rect half off the left edge draws its visible half. `LINE`,
`CIRCLE` and `DISC` refuse anything more than 131,072 pixels across, which is a
hundred times the widest screen. Bresenham walks every pixel whether or not it
is visible, and a line 2³¹ long would hold the machine for hours.

(`display.h`'s own functions take unsigned coordinates and clip before calling
here. So through them a rect at x = −5 still draws nothing, and the picture
never depends on whether the accelerator drew it.)

## 3. Colours, and blending

A colour is `0xAARRGGBB`. **An alpha of 1 to 254 blends:** `0x80FF0000` is half
red over what is there, per channel,
`(dst × (255 − a) + src × a + 127) / 255`, rounded so a colour over itself stays
itself. **`0xFF…` and `0x00…` are stored as they are.** The screen ignores
alpha, so `0` is black, as code has always meant by it. The destination keeps
its own alpha byte.

A shape blends **each pixel once**, even where the software draws one twice
(a disc's rows, a circle's octants, a frame's corners), so a translucent disc
is one even shade. `BLIT` copies bytes exactly, alpha and all.
`BLIT_ALPHA` lays a whole rectangle over another at one alpha you give it. A
copy that honours each source pixel's own alpha is not there.

## 4. Text

`gac_set_font(glyphs, glyph_w, glyph_h, cell_w, cell_h, first, count)`
uploads a font. It is one byte a glyph row, top bit first, glyphs up to 8
wide, and the device keeps a copy. `gac_text(dst, x, y, fg, bg, s, n)` draws
`n` characters a cell apart. **A `bg` with alpha 0 means no background.** A
string longer than the window goes in pieces by itself. `display.h` uploads
its 5 × 7 font when it starts, so after `disp_init()` the font is there.

## 5. The device: channel 10

Arguments are words in the data window, sent with R/W 0. Each command answers
1, or 0 for a surface that is not there.

| cmd | name | window |
|---|---|---|
| 0 | `NOP` | — |
| 1 | `INFO` | → magic `"PGGA"`, features (1 text, 2 blending), window bytes |
| 2 | `FILL` | dst, x, y, w, h, colour |
| 3 | `FRAME` | dst, x, y, w, h, colour |
| 4 | `BLIT` | src, sx, sy, dst, dx, dy, w, h |
| 5 | `BLIT_SCALED` | src, sx, sy, sw, sh, dst, dx, dy, dw, dh |
| 6 | `LINE` | dst, x0, y0, x1, y1, colour |
| 7 | `CIRCLE` | dst, cx, cy, r, colour |
| 8 | `DISC` | dst, cx, cy, r, colour |
| 9 | `SET_FONT` | address, glyph_w, glyph_h, cell_w, cell_h, first, count |
| 10 | `TEXT` | dst, x, y, fg, bg, length, then the characters |
| 11 | `SCROLL` | dst, x, y, w, h, dy, bg |
| 12 | `BATCH` | count, then records of cmd, nwords, words → ran, refused |
| 13 | `DAMAGE` | reserved |
| 14 | `BLIT_ALPHA` | src, sx, sy, dst, dx, dy, w, h, alpha |
| 15 | `RAM_SURFACE` | address, w, h → a handle, or 0 |
| 16 | `RAM_FREE` | — (the handle in ADDRESS) |

`BLIT` is safe when source and destination overlap. `BLIT_SCALED` is nearest
neighbour, `bmp.h`'s `STRETCH`, and needs its whole source inside its surface.
**`BATCH`** checks every record before drawing any: an unknown command, a
batch inside it, or a record running off the window, and nothing is drawn.
Then it runs them in order, skipping and counting any whose surface is not
there.

## 6. What it costs

Measured at 1280 × 720 (`tools/bench.py`): a full-screen `FILL` 0.7 ms,
blended 4.7 ms; a full-screen `BLIT_ALPHA` 25 ms; a whole 213 × 80 console of
`TEXT` about 18 ms. A command costs the guest about 240 instructions whatever
its size, which is why `display.h` fills anything under 8 pixels with stores
instead.

## 7. Where things are

| | |
|---|---|
| `emulator/devices/gac.py` | the device |
| `lib/pigeon/gac.h`, `gac.c` | the library |
| `lib/pigeon/display.c` | what `display.h` sends here |
| `tests/test_gac.py` | every command, and `display.c`'s pictures byte for byte |
| `tests/test_display_gac.py` | the library, with and without the accelerator |
