# 6. `<pigeon/display.h>` — the display library

Plain C over the memory-mapped framebuffer. Every *drawing* primitive is
pointer arithmetic and stores — the screen is just memory. The two whole-screen
operations are not: `disp_clear` and `disp_present` go through the display
device on `CH_DISPLAY`, because as C loops they were 87% of the 3D cube's
executed instructions.

## The framebuffer

`DISPLAY_START` (`0x1418`), `DISPLAY_W` × `DISPLAY_H` (192 × 108, 16:9) pixels,
4 bytes each, 82,944 bytes total. Those three names are predefined by the
compiler from `emulator/memory_map.py`, so this is the only place they are
written down. Pixel `(x, y)` is at

```
DISPLAY_START + (y * DISPLAY_W + x) * 4
```

Nothing needs to be told the screen changed. `DisplayIO.update()` snapshots the
region on the emulator side at 30 FPS; a store is on screen within a frame.

## Colour — verified

Bytes in RAM are **B, G, R, A**, which the emulator swaps to RGBA when serving a
frame. So a 32-bit word literal reads naturally as **`0xAARRGGBB`**:

| Word | RAM bytes | Result |
|---|---|---|
| `0xFF000000` | `00 00 00 ff` | black |
| `0xFFFF0000` | `00 00 ff ff` | red |
| `0xFF00FF00` | `00 ff 00 ff` | green |
| `0xFF0000FF` | `ff 00 00 ff` | blue |
| `0xFFFFFF00` | `00 ff ff ff` | yellow |
| `0xFF00FFFF` | `ff ff 00 ff` | cyan |
| `0xFFFF00FF` | `ff 00 ff ff` | magenta |
| `0xFFFFFFFF` | `ff ff ff ff` | white |

Every row was written to RAM and read back through the serving path.

**Alpha is not blended** — the emulator passes it straight to the canvas, so
anything with `AA = 0x00` is invisible. Always set `0xFF` unless you mean it.
This is a real trap: `user/sincos.asm` defines a "black" constant of
`0x00F0000`, seven hex digits, which is transparent rather than black.

## API

```c
#ifndef PIGEON_DISPLAY_H
#define PIGEON_DISPLAY_H

#define DISP_W  100
#define DISP_H  100

typedef unsigned int color_t;      /* 0xAARRGGBB */

#define RGB(r, g, b)  ((color_t)(0xFF000000u | ((r) << 16) | ((g) << 8) | (b)))
#define RGBA(r,g,b,a) ((color_t)(((a) << 24) | ((r) << 16) | ((g) << 8) | (b)))

#define BLACK   0xFF000000u
#define WHITE   0xFFFFFFFFu
#define RED     0xFFFF0000u
#define GREEN   0xFF00FF00u
#define BLUE    0xFF0000FFu
#define YELLOW  0xFFFFFF00u
#define CYAN    0xFF00FFFFu
#define MAGENTA 0xFFFF00FFu

/* --- pixels ----------------------------------------------------------- */
void    disp_set(unsigned x, unsigned y, color_t c);   /* clipped */
color_t disp_get(unsigned x, unsigned y);              /* 0 when off-screen */
color_t *disp_ptr(unsigned x, unsigned y);             /* unclipped, for loops */

/* --- shapes ----------------------------------------------------------- */
void disp_clear(color_t c);
void disp_hline(unsigned x, unsigned y, unsigned w, color_t c);
void disp_vline(unsigned x, unsigned y, unsigned h, color_t c);
void disp_rect(unsigned x, unsigned y, unsigned w, unsigned h, color_t c);
void disp_frame(unsigned x, unsigned y, unsigned w, unsigned h, color_t c);
void disp_line(int x0, int y0, int x1, int y1, color_t c);   /* Bresenham */
void disp_circle(int cx, int cy, int r, color_t c);

/* --- text: a 4x6 font, 96 printable ASCII glyphs ---------------------- */
#define GLYPH_W 4
#define GLYPH_H 6
void disp_char(unsigned x, unsigned y, char ch, color_t fg);
void disp_text(unsigned x, unsigned y, const char *s, color_t fg);

/* --- blitting --------------------------------------------------------- */
void disp_blit(unsigned x, unsigned y, unsigned w, unsigned h,
               const color_t *pixels);

#endif
```

## Implementation notes

**Everything clips.** `disp_set` returns silently when `x >= DISP_W` or
`y >= DISP_H`. Coordinates are `unsigned`, so a negative value wraps to a huge
one and the single `>=` test catches it — one comparison instead of two, and it
avoids the sign-bit-flip sequence entirely.

This is not a theoretical concern: `user/checkerboard.asm` has no clipping, runs
its pixel index past 10,000, and eventually walks the write pointer out of the
framebuffer and over its own code until the CPU faults on a corrupted
instruction. Clipping in one place is the whole reason to have this library.

```c
static color_t *const FB = (color_t *)0x1418;   /* DISPLAY_START */

color_t *disp_ptr(unsigned x, unsigned y) { return FB + y * DISP_W + x; }

void disp_set(unsigned x, unsigned y, color_t c) {
    if (x >= DISP_W || y >= DISP_H) return;
    FB[y * DISP_W + x] = c;
}
```

**Fill rows, not pixels.** A row is contiguous, so `disp_rect` walks a pointer
along it rather than recomputing `y * DISP_W + x` per pixel — that multiply is a
`MUL` plus address arithmetic every time:

```c
void disp_rect(unsigned x, unsigned y, unsigned w, unsigned h, color_t c) {
    if (x >= DISP_W || y >= DISP_H) return;
    if (x + w > DISP_W) w = DISP_W - x;          /* clip, don't bail */
    if (y + h > DISP_H) h = DISP_H - y;
    for (unsigned row = 0; row < h; row++) {
        color_t *p = disp_ptr(x, y + row);
        for (unsigned col = 0; col < w; col++) *p++ = c;
    }
}
```

`disp_clear` is the degenerate case, and it is not a loop at all: it fires
`CMD_FILL` on `CH_DISPLAY` and the device fills the buffer in one slice
assignment. As C it was 20,736 stores — roughly 660,000 instructions a frame
once the compiler had spilled every local — which is why it and `disp_present`
together dominated every profile.

**`disp_line` and `disp_circle` take signed `int`** because Bresenham needs
negative deltas. They are the only functions here that pay for signed
comparison, which is the right trade.

**The font** is a 96-entry table of 6 bytes, one per glyph row, 4 bits used —
576 bytes of static data. `disp_char` reads a row byte and tests bits high to
low. At 4×6 the 192×108 screen holds 38 columns × 18 rows of text.

## What is deliberately not here

~~No double buffering.~~ There is now: `disp_use_back_buffer()` and
`disp_present()`. It did not stay a library-level change. A `memcpy` per frame
was 20,736 word copies and showed up as 42% of the cube's instructions, so
presenting became a page flip — the guest hands the device the address of the
buffer it just drew (`CMD_SET_BASE`) and gets back the one that was on screen.
The consequence is in `display.h`: the buffer you draw into after a present
holds the frame *before* the one now showing, not a clean slate.

No alpha blending, no clipping rectangles, no sprites.
