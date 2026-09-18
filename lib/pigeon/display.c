/* Drawing on the screen. See display.h.
 *
 * Three things shape this code:
 *
 * Everything clips. user/checkerboard.asm has no clipping, walks its
 * pixel index past the end of the framebuffer, and eventually overwrites
 * its own code until the CPU faults on a corrupted instruction. Doing the
 * bounds check in one place is the whole reason this library exists.
 *
 * Coordinates are unsigned, so one `>=` catches both ends: a negative
 * value wraps to a huge one. That is also one comparison instead of two,
 * and it avoids the signed-compare sequence entirely. The accelerator's
 * coordinates are signed, so a shape is only handed to it where the two
 * mean the same thing, and the picture does not depend on which drew it.
 *
 * The screen's size is the machine's, asked at run time (disp_init), and
 * drawing goes to the graphics accelerator when there is one: one bus
 * command a shape instead of a store a pixel (docs/gac/). A pixel is still
 * one store, through row_ptr, whichever it is. The software below is what a
 * bare CPU (tests/test_libs.py) and a machine without video memory draw
 * with, and what the accelerator was proved against, byte for byte
 * (tests/test_gac.py).
 */
#include <pigeon/display.h>
#include <pigeon/gac.h>
#include <pigeon/io.h>
#include <pigeon/mem.h>
#include <pigeon/vram.h>

/* Commands on CH_DISPLAY. The DISP_ prefix is not decoration: units are
 * compiled together with no linker and the preprocessor's macro table is
 * shared across them, so a bare CMD_FILL here would silently overwrite
 * input.c's -- the preprocessor does not warn on redefinition. */
#define DISP_CMD_INFO      1
#define DISP_CMD_SET_BASE  2
#define DISP_CMD_GET_BASE  3
#define DISP_CMD_FILL      4
#define DISP_CMD_COPY      5

/* An opaque fill smaller than this is cheaper as stores than as a bus
 * command (docs/gac/plans/phase5_display_lib.md, Q3). Measured: a command
 * costs ~84 us whatever its size -- ~240 guest instructions and the host's
 * work -- and a stored pixel ~10 us, so they meet at 8 pixels. A colour
 * that blends always goes to the accelerator, so it blends the same at
 * every size. */
#define DISP_GAC_MIN 8u
#define DISP_BLENDS(c) (((c) >> 24) != 0xFFu)

/* The screen, as the machine has it; see display.h. Until disp_init() has
 * asked, the power-on screen -- which is right on every machine until
 * something changes the mode. */
int disp_w = DISPLAY_W;
int disp_h = DISPLAY_H;
unsigned disp_base = DISPLAY_START;

/* Where drawing goes: the screen itself, or a back buffer once one has
 * been asked for. A global rather than a function so the per-pixel cost
 * is one load, not a call. */
unsigned disp_target = DISPLAY_START;

/* The back buffer, 0 until one is asked for.
 *
 * This exists because `disp_target != DISP_BASE` STOPPED meaning "I have
 * a back buffer" once presenting became a page flip: the two surfaces are
 * the screen and this buffer, so on alternate frames the draw target
 * legitimately IS DISP_BASE. Gating on that would make present a no-op
 * every other frame and halve the update rate while showing stale
 * content. */
static unsigned disp_back = 0u;
static unsigned disp_back_vram = 0u;   /* its VRAM handle; 0 when it is heap */

/* 0 = not probed, 1 = CH_DISPLAY is there, 2 = software only. */
static unsigned disp_hw = 0u;

/* 0 until disp_init() has asked the machine. */
static unsigned disp_ready = 0u;

/* 1 when drawing goes to the accelerator, and the accelerator's handles for
 * the screen, the back buffer, and whichever of them is being drawn on. */
static unsigned disp_gac = 0u;
static unsigned disp_base_h = 0u;
static unsigned disp_back_h = 0u;
static unsigned disp_target_h = 0u;


static void disp_call(unsigned rw, unsigned command,
                      unsigned length, unsigned address) {
    IO_RW   = rw;
    IO_CMD  = command;
    IO_LEN  = length;
    IO_ADDR = address;
    IO_CH   = CH_DISPLAY;          /* this store fires it -- must be last */
}

/* Is there a display device on the bus, and does it agree with us about
 * how big the screen is?
 *
 * Three environments have to be told apart, and the check differs for
 * each. tests/test_libs.py runs a bare CPU with no IOController at all,
 * so the software fallbacks below are not defensive padding -- they are
 * what keeps those tests meaningful. */
static unsigned disp_probe(void) {
    disp_call(0u, DISP_CMD_INFO, 12u, 0u);

    /* No controller: the channel store armed a flag nobody services, so
     * the channel is still set. A real controller always clears it. */
    if (IO_CH != 0u) { IO_CH = 0u; return 2u; }

    /* A controller, but nothing on this channel. It answers with
     * ERR_NO_SUCH_CHANNEL, which is 0xFFFFFFFF -- so test that FIRST: as
     * a length it is enormous, not short, and a `< 12` check alone sails
     * straight past it into a stale data window. */
    if (IO_RETLEN == 0xFFFFFFFFu) return 2u;
    if (IO_RETLEN < 12u) return 2u;

    /* The device's screen must be the size this program believes, or a
     * hardware fill would write the machine's screen of bytes into a
     * buffer we sized from ours and corrupt the heap. disp_init() took the
     * size from the machine, so they agree -- unless the mode changed
     * under a program that has not asked again. */
    if (IO_DATAW[2] != disp_w * disp_h * 4u) return 2u;

    return 1u;
}

static int disp_have_hw(void) {
    if (disp_hw == 0u) disp_hw = disp_probe();
    return disp_hw == 1u;
}

/* Every drawing call but a pixel's asks the machine the first time. A pixel
 * does not need to: until disp_init() the globals are the power-on screen,
 * which is where a pixel then goes. */
#define DISP_READY() if (disp_ready == 0u) disp_init()

/* Rows are contiguous, so walking a pointer along one beats recomputing
 * y*DISP_W + x per pixel -- that is a MUL plus address arithmetic every
 * time round. */
static color_t *row_ptr(unsigned x, unsigned y) {
    return ((color_t *)disp_target) + y * disp_w + x;
}

/* The back buffer goes: its video memory, or its heap and its handle. */
static void disp_drop_back(void) {
    if (disp_back == 0u) return;
    if (disp_back_vram != 0u) {
        vram_free(disp_back_vram);
    } else {
        if (disp_gac) gac_ram_free(disp_back_h);
        free((void *)disp_back);
    }
    disp_back = 0u;
    disp_back_vram = 0u;
    disp_target = disp_base;
    disp_target_h = disp_base_h;
}

int disp_use_back_buffer(void) {
    void *buffer;
    unsigned offset;
    unsigned handle;
    DISP_READY();
    if (disp_back != 0u) return 1;                 /* already have one */
    /* Video memory first: the screen's size by construction, out of the
     * heap, and something the display flips to with a handle. */
    handle = vram_alloc(disp_w, disp_h, &offset);
    if (handle != 0u) {
        disp_back = vram_aperture() + offset;
        disp_back_vram = handle;
        disp_back_h = handle;
    } else {
        buffer = malloc(disp_w * disp_h * 4u);
        if (buffer == NULL) return 0;
        disp_back = (unsigned)buffer;
        if (disp_gac) {
            disp_back_h = gac_ram_surface(disp_back, disp_w, disp_h);
            if (disp_back_h == 0u) disp_gac = 0u;  /* it will not draw there: nor anywhere */
        }
    }
    disp_target = disp_back;
    disp_target_h = disp_back_h;
    return 1;
}

void disp_present(void) {
    if (disp_back == 0u) return;                   /* drawing straight to it */

    if (disp_have_hw()) {
        /* Hand the display the buffer just drawn. One store each for the
         * five header fields, instead of a copy of every pixel. CH_DISPLAY
         * takes RAM and video memory alike. */
        disp_call(0u, DISP_CMD_SET_BASE, 4u, disp_target);
        if (IO_DATAW[0] != 0u) {
            /* Draw the next frame into whichever surface just left the
             * screen. It still holds the frame BEFORE the one now
             * showing -- see the note in display.h. */
            if (disp_target == disp_back) {
                disp_target = disp_base;
                disp_target_h = disp_base_h;
            } else {
                disp_target = disp_back;
                disp_target_h = disp_back_h;
            }
            return;
        }
        disp_hw = 2u;                              /* refused: stop asking */
    }

    /* No device, or it would not take the base: copy, as before. The
     * screen is DISP_BASE again, so drawing goes back to the buffer. */
    {
        color_t *src = (color_t *)disp_target;
        color_t *dst = (color_t *)disp_base;
        unsigned n = disp_w * disp_h;
        if (disp_target == disp_base) return;
        while (n > 0u) { *dst = *src; dst++; src++; n--; }
        disp_target = disp_back;
        disp_target_h = disp_back_h;
    }
}

void disp_set(unsigned x, unsigned y, color_t c) {
    if (x >= disp_w) return;
    if (y >= disp_h) return;
    *row_ptr(x, y) = c;
}

color_t disp_get(unsigned x, unsigned y) {
    if (x >= disp_w) return 0;
    if (y >= disp_h) return 0;
    return *row_ptr(x, y);
}

/* A fill of `pixels` pixels in colour c: the accelerator's, or stores? */
static int disp_by_gac(unsigned pixels, color_t c) {
    return disp_gac != 0u && (pixels >= DISP_GAC_MIN || DISP_BLENDS(c));
}

void disp_clear(color_t c) {
    DISP_READY();
    /* disp_target, not DISP_BASE: with a back buffer in play this must
     * clear the buffer being drawn into, or the clear lands on screen
     * while every shape lands in the buffer. */
    if (disp_gac) {
        gac_fill(disp_target_h, 0, 0, (int)disp_w, (int)disp_h, c);
        return;
    }
    if (disp_have_hw()) {
        IO_DATAW[0] = c;                           /* the colour, in the window */
        /* 4 is the size of that payload, NOT the size of the fill. The
         * controller allocates LENGTH bytes before the device is even
         * called, so putting a fill size -- or a colour -- there is how
         * you ask for a multi-gigabyte allocation by accident. */
        disp_call(1u, DISP_CMD_FILL, 4u, disp_target);
        return;
    }
    {
        color_t *p = (color_t *)disp_target;
        unsigned n = disp_w * disp_h;
        while (n > 0u) { *p = c; p++; n--; }
    }
}

void disp_hline(unsigned x, unsigned y, unsigned w, color_t c) {
    color_t *p;
    DISP_READY();
    if (y >= disp_h || x >= disp_w) return;
    if (x + w > disp_w) w = disp_w - x;      /* clip, do not bail */
    if (disp_by_gac(w, c)) {
        gac_fill(disp_target_h, (int)x, (int)y, (int)w, 1, c);
        return;
    }
    p = row_ptr(x, y);
    while (w > 0u) { *p = c; p++; w--; }
}

void disp_vline(unsigned x, unsigned y, unsigned h, color_t c) {
    color_t *p;
    DISP_READY();
    if (x >= disp_w || y >= disp_h) return;
    if (y + h > disp_h) h = disp_h - y;
    if (disp_by_gac(h, c)) {
        gac_fill(disp_target_h, (int)x, (int)y, 1, (int)h, c);
        return;
    }
    p = row_ptr(x, y);
    while (h > 0u) { *p = c; p = p + disp_w; h--; }
}

void disp_rect(unsigned x, unsigned y, unsigned w, unsigned h, color_t c) {
    unsigned row = 0u;
    DISP_READY();
    if (x >= disp_w || y >= disp_h) return;
    if (x + w > disp_w) w = disp_w - x;
    if (y + h > disp_h) h = disp_h - y;
    if (disp_by_gac(w * h, c)) {
        gac_fill(disp_target_h, (int)x, (int)y, (int)w, (int)h, c);
        return;
    }
    while (row < h) { disp_hline(x, y + row, w, c); row++; }
}

/* Bytes moved within the draw target by the device: 1 if it moved them.
 * The reply's length is checked first, because a device without COPY
 * answers nothing and leaves the arguments sitting in the window. */
static int disp_copy(unsigned to, unsigned from, unsigned bytes) {
    if (!disp_have_hw()) return 0;
    IO_DATAW[0] = to;
    IO_DATAW[1] = from;
    IO_DATAW[2] = bytes;
    disp_call(0u, DISP_CMD_COPY, 4u, disp_target);
    return IO_RETLEN == 4u && IO_DATAW[0] == 1u;
}

void disp_scroll(unsigned y, unsigned h, int dy, color_t bg) {
    unsigned row_bytes;
    unsigned n;
    unsigned kept;
    unsigned gap;
    unsigned i;
    DISP_READY();
    row_bytes = disp_w * 4u;
    if (y >= disp_h || h == 0u || dy == 0) return;
    if (y + h > disp_h) h = disp_h - y;
    if (disp_gac) {
        gac_scroll(disp_target_h, 0, (int)y, (int)disp_w, (int)h, dy, bg);
        return;
    }
    n = dy < 0 ? (unsigned)(0 - dy) : (unsigned)dy;
    if (n >= h) {
        disp_rect(0u, y, disp_w, h, bg);
        return;
    }
    kept = h - n;
    if (dy < 0) {
        if (!disp_copy(y * row_bytes, (y + n) * row_bytes, kept * row_bytes))
            memmove((void *)row_ptr(0u, y), (void *)row_ptr(0u, y + n), kept * row_bytes);
        gap = y + kept;
    } else {
        if (!disp_copy((y + n) * row_bytes, y * row_bytes, kept * row_bytes))
            memmove((void *)row_ptr(0u, y + n), (void *)row_ptr(0u, y), kept * row_bytes);
        gap = y;
    }
    /* The rows left behind: the first drawn, and copied down to the rest,
     * which is cheaper than drawing them when the device will. */
    disp_hline(0u, gap, disp_w, bg);
    for (i = 1u; i < n; i++) {
        if (!disp_copy((gap + i) * row_bytes, gap * row_bytes, row_bytes))
            disp_hline(0u, gap + i, disp_w, bg);
    }
}

void disp_frame(unsigned x, unsigned y, unsigned w, unsigned h, color_t c) {
    DISP_READY();
    if (w == 0u || h == 0u) return;
    /* The accelerator's frame is the same four lines -- where its signed
     * numbers mean what these unsigned ones do: a corner on the screen and
     * a size that is not a wrapped negative. */
    if (disp_gac && x < disp_w && y < disp_h && w < 0x80000000u && h < 0x80000000u) {
        gac_frame(disp_target_h, (int)x, (int)y, (int)w, (int)h, c);
        return;
    }
    disp_hline(x, y, w, c);
    disp_hline(x, y + h - 1u, w, c);
    disp_vline(x, y, h, c);
    disp_vline(x + w - 1u, y, h, c);
}

/* Bresenham. These take signed ints because the deltas are genuinely
 * negative -- the only functions here that pay for signed comparison. */
void disp_line(int x0, int y0, int x1, int y1, color_t c) {
    int dx = x1 - x0; int dy = y1 - y0;
    int sx = 1; int sy = 1;
    int err; int e2;

    DISP_READY();
    if (disp_gac) { gac_line(disp_target_h, x0, y0, x1, y1, c); return; }
    if (dx < 0) { dx = -dx; sx = -1; }
    if (dy < 0) { dy = -dy; sy = -1; }
    err = dx - dy;

    while (1) {
        disp_set((unsigned)x0, (unsigned)y0, c);
        if (x0 == x1 && y0 == y1) return;
        e2 = err + err;
        if (e2 > 0 - dy) { err = err - dy; x0 = x0 + sx; }
        if (e2 < dx)     { err = err + dx; y0 = y0 + sy; }
    }
}

/* Midpoint circle: eight-way symmetry, no division, no multiply. */
void disp_circle(int cx, int cy, int r, color_t c) {
    int x = r; int y = 0; int err = 1 - r;
    DISP_READY();
    if (r < 0) return;
    if (disp_gac) { gac_circle(disp_target_h, cx, cy, r, c); return; }
    while (x >= y) {
        disp_set((unsigned)(cx + x), (unsigned)(cy + y), c);
        disp_set((unsigned)(cx + y), (unsigned)(cy + x), c);
        disp_set((unsigned)(cx - y), (unsigned)(cy + x), c);
        disp_set((unsigned)(cx - x), (unsigned)(cy + y), c);
        disp_set((unsigned)(cx - x), (unsigned)(cy - y), c);
        disp_set((unsigned)(cx - y), (unsigned)(cy - x), c);
        disp_set((unsigned)(cx + y), (unsigned)(cy - x), c);
        disp_set((unsigned)(cx + x), (unsigned)(cy - y), c);
        y++;
        if (err < 0) { err = err + 2 * y + 1; }
        else         { x--; err = err + 2 * (y - x) + 1; }
    }
}

/* One row of a disc. Both ends are clipped here, while they are still
 * signed: disp_hline's coordinates are unsigned, so a span starting left
 * of the screen would arrive as a huge x and draw nothing at all. */
static void disp_span(int x0, int x1, int y, color_t c) {
    if (y < 0 || x1 < 0) return;
    if (x0 < 0) x0 = 0;
    disp_hline((unsigned)x0, (unsigned)y, (unsigned)(x1 - x0 + 1), c);
}

/* A filled circle: disp_circle's algorithm with spans instead of points,
 * so the eight octant pixels become four rows between their two x's.
 * The rows overlap where the octants meet, which costs a few writes and
 * nothing else here -- a pixel is stored, not blended. The accelerator
 * draws each row once, which is what makes a translucent disc right. */
void disp_disc(int cx, int cy, int r, color_t c) {
    int x = r; int y = 0; int err = 1 - r;
    DISP_READY();
    if (r < 0) return;
    if (disp_gac) { gac_disc(disp_target_h, cx, cy, r, c); return; }
    while (x >= y) {
        disp_span(cx - x, cx + x, cy + y, c);
        disp_span(cx - x, cx + x, cy - y, c);
        disp_span(cx - y, cx + y, cy + x, c);
        disp_span(cx - y, cx + y, cy - x, c);
        y++;
        if (err < 0) { err = err + 2 * y + 1; }
        else         { x--; err = err + 2 * (y - x) + 1; }
    }
}

/* An image onto the screen, clipped on all four sides: one BLIT from the
 * pixels registered as a surface for the moment, or a row at a time with
 * memcpy -- not a pixel at a time with disp_set, which for a screenful
 * would be most of a second. */
void disp_blit(unsigned *pixels, int x, int y, unsigned w, unsigned h) {
    unsigned surface;
    int row = 0;
    int at;
    int from;
    int n;

    DISP_READY();
    if (w == 0u || h == 0u) return;
    if (disp_gac) {
        surface = gac_ram_surface((unsigned)pixels, w, h);
        if (surface != 0u) {
            gac_blit(surface, 0, 0, disp_target_h, x, y, (int)w, (int)h);
            gac_ram_free(surface);
            return;
        }
    }
    while (row < (int)h) {
        if (y + row >= 0 && y + row < (int)disp_h) {
            at = x;
            from = 0;
            n = (int)w;
            if (at < 0) { from = 0 - at; n = n + at; at = 0; }
            if (at + n > (int)disp_w) n = (int)disp_w - at;
            if (n > 0)
                memcpy((void *)row_ptr((unsigned)at, (unsigned)(y + row)),
                       pixels + row * (int)w + from, (unsigned)n * 4u);
        }
        row++;
    }
}

int disp_setmode(unsigned w, unsigned h) {
    DISP_READY();
    if (!vram_set_mode(w, h)) return 0;
    /* Everything sized for the old screen goes: the back buffer, and the
     * accelerator's handle for the old screen if it was RAM. */
    disp_drop_back();
    if (disp_gac && disp_base_h != GAC_SCREEN) gac_ram_free(disp_base_h);
    /* The power-on mode is shown out of RAM at DISPLAY_START, as it is at
     * power-on and after a reboot: what the BIOS, bios2 and k_tidy expect
     * of it. Black, as every new mode is. */
    if (w == DISPLAY_W && h == DISPLAY_H && vram_scanout_ram(DISPLAY_START))
        memset((void *)DISPLAY_START, 0, DISPLAY_W * DISPLAY_H * 4u);
    disp_init();
    return 1;
}

int disp_modes(unsigned *ws, unsigned *hs, int max) {
    unsigned count = vram_mode_count();
    unsigned i;
    if (count == 0u) {                             /* no video memory: the one there is */
        if (max > 0) { ws[0] = DISPLAY_W; hs[0] = DISPLAY_H; }
        return 1;
    }
    for (i = 0u; i < count && (int)i < max; i++) vram_mode_at(i, ws + i, hs + i);
    return (int)count;
}

unsigned disp_generation(void) {
    return vram_generation();
}

/* 5x7 font, printable ASCII 0x20..0x7E, in a 6x8 cell. One byte per
 * glyph row; only the top 5 bits are used, tested high to low. Rows 0..6
 * are the body and row 6 is the baseline, so uppercase and digits are a
 * full seven rows and lowercase sits on rows 2..6. Row 7 is the descender
 * row -- blank for everything except g j p q y, the comma and semicolon
 * tails, and the underscore. 95 glyphs x 8 rows. */
static char FONT[] = {
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,   /* space */
    0x20, 0x20, 0x20, 0x20, 0x20, 0x00, 0x20, 0x00,   /* ! */
    0x50, 0x50, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,   /* " */
    0x50, 0x50, 0xF8, 0x50, 0xF8, 0x50, 0x50, 0x00,   /* # */
    0x20, 0x78, 0xA0, 0x70, 0x28, 0xF0, 0x20, 0x00,   /* $ */
    0xC0, 0xC8, 0x10, 0x20, 0x40, 0x98, 0x18, 0x00,   /* % */
    0x60, 0x90, 0xA0, 0x40, 0xA8, 0x90, 0x68, 0x00,   /* & */
    0x20, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,   /* ' */
    0x10, 0x20, 0x40, 0x40, 0x40, 0x20, 0x10, 0x00,   /* ( */
    0x40, 0x20, 0x10, 0x10, 0x10, 0x20, 0x40, 0x00,   /* ) */
    0x00, 0x20, 0xA8, 0x70, 0xA8, 0x20, 0x00, 0x00,   /* * */
    0x00, 0x20, 0x20, 0xF8, 0x20, 0x20, 0x00, 0x00,   /* + */
    0x00, 0x00, 0x00, 0x00, 0x00, 0x60, 0x60, 0x40,   /* , */
    0x00, 0x00, 0x00, 0xF8, 0x00, 0x00, 0x00, 0x00,   /* - */
    0x00, 0x00, 0x00, 0x00, 0x00, 0x60, 0x60, 0x00,   /* . */
    0x08, 0x08, 0x10, 0x20, 0x40, 0x80, 0x80, 0x00,   /* / */
    0x70, 0x88, 0x98, 0xA8, 0xC8, 0x88, 0x70, 0x00,   /* 0 */
    0x20, 0x60, 0x20, 0x20, 0x20, 0x20, 0x70, 0x00,   /* 1 */
    0x70, 0x88, 0x08, 0x10, 0x20, 0x40, 0xF8, 0x00,   /* 2 */
    0xF8, 0x10, 0x20, 0x10, 0x08, 0x88, 0x70, 0x00,   /* 3 */
    0x10, 0x30, 0x50, 0x90, 0xF8, 0x10, 0x10, 0x00,   /* 4 */
    0xF8, 0x80, 0xF0, 0x08, 0x08, 0x88, 0x70, 0x00,   /* 5 */
    0x30, 0x40, 0x80, 0xF0, 0x88, 0x88, 0x70, 0x00,   /* 6 */
    0xF8, 0x08, 0x10, 0x20, 0x40, 0x40, 0x40, 0x00,   /* 7 */
    0x70, 0x88, 0x88, 0x70, 0x88, 0x88, 0x70, 0x00,   /* 8 */
    0x70, 0x88, 0x88, 0x78, 0x08, 0x10, 0x60, 0x00,   /* 9 */
    0x00, 0x00, 0x60, 0x60, 0x00, 0x60, 0x60, 0x00,   /* : */
    0x00, 0x00, 0x60, 0x60, 0x00, 0x60, 0x60, 0x40,   /* ; */
    0x00, 0x10, 0x20, 0x40, 0x20, 0x10, 0x00, 0x00,   /* < */
    0x00, 0x00, 0xF8, 0x00, 0xF8, 0x00, 0x00, 0x00,   /* = */
    0x00, 0x40, 0x20, 0x10, 0x20, 0x40, 0x00, 0x00,   /* > */
    0x70, 0x88, 0x08, 0x10, 0x20, 0x00, 0x20, 0x00,   /* ? */
    0x70, 0x88, 0xB8, 0xA8, 0xB8, 0x80, 0x70, 0x00,   /* @ */
    0x70, 0x88, 0x88, 0xF8, 0x88, 0x88, 0x88, 0x00,   /* A */
    0xF0, 0x88, 0x88, 0xF0, 0x88, 0x88, 0xF0, 0x00,   /* B */
    0x70, 0x88, 0x80, 0x80, 0x80, 0x88, 0x70, 0x00,   /* C */
    0xF0, 0x88, 0x88, 0x88, 0x88, 0x88, 0xF0, 0x00,   /* D */
    0xF8, 0x80, 0x80, 0xF0, 0x80, 0x80, 0xF8, 0x00,   /* E */
    0xF8, 0x80, 0x80, 0xF0, 0x80, 0x80, 0x80, 0x00,   /* F */
    0x70, 0x88, 0x80, 0xB8, 0x88, 0x88, 0x78, 0x00,   /* G */
    0x88, 0x88, 0x88, 0xF8, 0x88, 0x88, 0x88, 0x00,   /* H */
    0x70, 0x20, 0x20, 0x20, 0x20, 0x20, 0x70, 0x00,   /* I */
    0x38, 0x10, 0x10, 0x10, 0x10, 0x90, 0x60, 0x00,   /* J */
    0x88, 0x90, 0xA0, 0xC0, 0xA0, 0x90, 0x88, 0x00,   /* K */
    0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0xF8, 0x00,   /* L */
    0x88, 0xD8, 0xA8, 0xA8, 0x88, 0x88, 0x88, 0x00,   /* M */
    0x88, 0xC8, 0xA8, 0xA8, 0xA8, 0x98, 0x88, 0x00,   /* N */
    0x70, 0x88, 0x88, 0x88, 0x88, 0x88, 0x70, 0x00,   /* O */
    0xF0, 0x88, 0x88, 0xF0, 0x80, 0x80, 0x80, 0x00,   /* P */
    0x70, 0x88, 0x88, 0x88, 0xA8, 0x90, 0x68, 0x00,   /* Q */
    0xF0, 0x88, 0x88, 0xF0, 0xA0, 0x90, 0x88, 0x00,   /* R */
    0x78, 0x80, 0x80, 0x70, 0x08, 0x08, 0xF0, 0x00,   /* S */
    0xF8, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20, 0x00,   /* T */
    0x88, 0x88, 0x88, 0x88, 0x88, 0x88, 0x70, 0x00,   /* U */
    0x88, 0x88, 0x88, 0x88, 0x88, 0x50, 0x20, 0x00,   /* V */
    0x88, 0x88, 0x88, 0xA8, 0xA8, 0xD8, 0x88, 0x00,   /* W */
    0x88, 0x88, 0x50, 0x20, 0x50, 0x88, 0x88, 0x00,   /* X */
    0x88, 0x88, 0x50, 0x20, 0x20, 0x20, 0x20, 0x00,   /* Y */
    0xF8, 0x08, 0x10, 0x20, 0x40, 0x80, 0xF8, 0x00,   /* Z */
    0x70, 0x40, 0x40, 0x40, 0x40, 0x40, 0x70, 0x00,   /* [ */
    0x80, 0x80, 0x40, 0x20, 0x10, 0x08, 0x08, 0x00,   /* \ */
    0x70, 0x10, 0x10, 0x10, 0x10, 0x10, 0x70, 0x00,   /* ] */
    0x20, 0x50, 0x88, 0x00, 0x00, 0x00, 0x00, 0x00,   /* ^ */
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF8,   /* _ */
    0x40, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,   /* ` */
    0x00, 0x00, 0x70, 0x08, 0x78, 0x88, 0x78, 0x00,   /* a */
    0x80, 0x80, 0xF0, 0x88, 0x88, 0x88, 0xF0, 0x00,   /* b */
    0x00, 0x00, 0x78, 0x80, 0x80, 0x80, 0x78, 0x00,   /* c */
    0x08, 0x08, 0x78, 0x88, 0x88, 0x88, 0x78, 0x00,   /* d */
    0x00, 0x00, 0x70, 0x88, 0xF8, 0x80, 0x70, 0x00,   /* e */
    0x38, 0x40, 0xF0, 0x40, 0x40, 0x40, 0x40, 0x00,   /* f */
    0x00, 0x00, 0x78, 0x88, 0x88, 0x78, 0x08, 0x70,   /* g */
    0x80, 0x80, 0xF0, 0x88, 0x88, 0x88, 0x88, 0x00,   /* h */
    0x20, 0x00, 0x60, 0x20, 0x20, 0x20, 0x70, 0x00,   /* i */
    0x10, 0x00, 0x30, 0x10, 0x10, 0x10, 0x90, 0x60,   /* j */
    0x80, 0x80, 0x90, 0xA0, 0xC0, 0xA0, 0x90, 0x00,   /* k */
    0x60, 0x20, 0x20, 0x20, 0x20, 0x20, 0x38, 0x00,   /* l */
    0x00, 0x00, 0xF8, 0xA8, 0xA8, 0xA8, 0xA8, 0x00,   /* m */
    0x00, 0x00, 0xF0, 0x88, 0x88, 0x88, 0x88, 0x00,   /* n */
    0x00, 0x00, 0x70, 0x88, 0x88, 0x88, 0x70, 0x00,   /* o */
    0x00, 0x00, 0xF0, 0x88, 0x88, 0xF0, 0x80, 0x80,   /* p */
    0x00, 0x00, 0x78, 0x88, 0x88, 0x78, 0x08, 0x08,   /* q */
    0x00, 0x00, 0xB0, 0xC8, 0x80, 0x80, 0x80, 0x00,   /* r */
    0x00, 0x00, 0x78, 0x80, 0x70, 0x08, 0xF0, 0x00,   /* s */
    0x40, 0x40, 0xF0, 0x40, 0x40, 0x48, 0x30, 0x00,   /* t */
    0x00, 0x00, 0x88, 0x88, 0x88, 0x88, 0x78, 0x00,   /* u */
    0x00, 0x00, 0x88, 0x88, 0x88, 0x50, 0x20, 0x00,   /* v */
    0x00, 0x00, 0x88, 0x88, 0xA8, 0xA8, 0x50, 0x00,   /* w */
    0x00, 0x00, 0x88, 0x50, 0x20, 0x50, 0x88, 0x00,   /* x */
    0x00, 0x00, 0x88, 0x88, 0x88, 0x78, 0x08, 0x70,   /* y */
    0x00, 0x00, 0xF8, 0x10, 0x20, 0x40, 0xF8, 0x00,   /* z */
    0x30, 0x40, 0x40, 0xC0, 0x40, 0x40, 0x30, 0x00,   /* { */
    0x20, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20, 0x00,   /* | */
    0x60, 0x10, 0x10, 0x18, 0x10, 0x10, 0x60, 0x00,   /* } */
    0x00, 0x00, 0x40, 0xA8, 0x10, 0x00, 0x00, 0x00   /* ~ */
};

void disp_char(unsigned x, unsigned y, int ch, color_t fg) {
    unsigned index;
    unsigned row;
    unsigned col;
    unsigned bits;
    char one;

    DISP_READY();
    if (ch < 0x20 || ch > 0x7E) return;
    if (disp_gac) {
        one = (char)ch;
        gac_text(disp_target_h, (int)x, (int)y, fg, 0u, &one, 1u);
        return;
    }
    index = ((unsigned)ch - 0x20u) * GLYPH_H;
    for (row = 0u; row < GLYPH_H; row++) {
        bits = ((unsigned)FONT[index + row]) & 0xF8u;
        for (col = 0u; col < GLYPH_W; col++) {
            if (bits & (0x80u >> col)) disp_set(x + col, y + row, fg);
        }
    }
}

void disp_textn(unsigned x, unsigned y, char *s, unsigned n, color_t fg) {
    unsigned i;
    DISP_READY();
    if (disp_gac) {
        gac_text(disp_target_h, (int)x, (int)y, fg, 0u, s, n);
        return;
    }
    for (i = 0u; i < n; i++) {
        disp_char(x, y, (int)s[i], fg);
        x = x + GLYPH_W + 1u;
    }
}

void disp_text(unsigned x, unsigned y, char *s, color_t fg) {
    unsigned n = 0u;
    DISP_READY();
    if (disp_gac) {
        /* The whole string in one command: the device steps a cell a
         * character, GLYPH_W + 1, as the loop below does, and draws no
         * background -- alpha 0 in bg. */
        while (s[n]) n++;
        gac_text(disp_target_h, (int)x, (int)y, fg, 0u, s, n);
        return;
    }
    while (*s) {
        disp_char(x, y, (int)*s, fg);
        x = x + GLYPH_W + 1u;
        s++;
    }
}

/* disp_init is here, after the font it hands the accelerator. */
/* The accelerator's handle for a surface at `address`: the screen of the
 * current mode, or a rectangle of RAM registered for it. 0 when it will
 * not take it -- the power-on screen and the heap it will. */
static unsigned disp_surface(unsigned address, unsigned screen) {
    unsigned handle;
    if (address == screen && address >= vram_aperture()) return GAC_SCREEN + 1u;
    if (address >= vram_aperture()) return 0u;     /* video memory, but not the screen */
    handle = gac_ram_surface(address, disp_w, disp_h);
    return handle == 0u ? 0u : handle + 1u;
}

int disp_init(void) {
    unsigned w;
    unsigned h;
    unsigned offset = 0u;
    unsigned handle;

    disp_ready = 1u;
    disp_hw = 0u;
    disp_gac = 0u;
    if (vram_mode(&w, &h, &offset)) {
        /* What is on the screen now: RAM at the power-on mode, the mode's
         * surface in video memory otherwise. CH_DISPLAY says which, as an
         * address the program can draw through either way. */
        disp_w = w;
        disp_h = h;
        disp_call(0u, DISP_CMD_GET_BASE, 4u, 0u);
        disp_base = IO_DATAW[0];
    } else {
        /* No video memory: CH_DISPLAY's size, or the power-on one on a
         * bare CPU, where nothing answers. */
        disp_call(0u, DISP_CMD_INFO, 12u, 0u);
        if (IO_CH != 0u) {
            IO_CH = 0u;
        } else if (IO_RETLEN != 0xFFFFFFFFu && IO_RETLEN >= 12u) {
            disp_w = IO_DATAW[0];
            disp_h = IO_DATAW[1];
        }
        disp_base = DISPLAY_START;
    }
    disp_target = disp_base;

    if (gac_present()) {
        /* The accelerator draws on handles. disp_surface answers the
         * handle plus 1, so that the screen's handle, 0, is not "none". */
        handle = disp_surface(disp_base, vram_aperture() + offset);
        if (handle != 0u && gac_set_font(FONT, GLYPH_W, GLYPH_H, GLYPH_W + 1u, GLYPH_H,
                                         0x20u, 95u)) {
            disp_base_h = handle - 1u;
            disp_target_h = disp_base_h;
            disp_gac = 1u;
        }
    }
    return 1;
}
