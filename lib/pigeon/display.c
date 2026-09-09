/* Drawing on the memory-mapped framebuffer. See display.h.
 *
 * Two things shape this code:
 *
 * Everything clips. user/checkerboard.asm has no clipping, walks its
 * pixel index past the end of the framebuffer, and eventually overwrites
 * its own code until the CPU faults on a corrupted instruction. Doing the
 * bounds check in one place is the whole reason this library exists.
 *
 * Coordinates are unsigned, so one `>=` catches both ends: a negative
 * value wraps to a huge one. That is also one comparison instead of two,
 * and it avoids the signed-compare sequence entirely.
 */
#include <pigeon/display.h>
#include <pigeon/io.h>
#include <pigeon/mem.h>

/* Commands on CH_DISPLAY. The DISP_ prefix is not decoration: units are
 * compiled together with no linker and the preprocessor's macro table is
 * shared across them, so a bare CMD_FILL here would silently overwrite
 * input.c's -- the preprocessor does not warn on redefinition. */
#define DISP_CMD_INFO      1
#define DISP_CMD_SET_BASE  2
#define DISP_CMD_FILL      4

#define DISP_BYTES (DISP_W * DISP_H * 4)

/* Where drawing goes: the screen itself, or a back buffer once one has
 * been asked for. A global rather than a function so the per-pixel cost
 * is one load, not a call. */
unsigned disp_target = DISP_BASE;

/* The heap buffer, 0 until one is asked for.
 *
 * This exists because `disp_target != DISP_BASE` STOPPED meaning "I have
 * a back buffer" once presenting became a page flip: the two surfaces are
 * the hardware framebuffer and this buffer, so on alternate frames the
 * draw target legitimately IS DISP_BASE. Gating on that would make
 * present a no-op every other frame and halve the update rate while
 * showing stale content. */
static unsigned disp_back = 0u;

/* 0 = not probed, 1 = the device is there, 2 = software only. */
static unsigned disp_hw = 0u;

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

    /* The device's screen must be the size this program was compiled
     * for, or a hardware fill would write the machine's DISPLAY_SIZE
     * bytes into a buffer we sized DISP_BYTES and corrupt the heap.
     * These agree by construction now that the geometry is predefined
     * from the memory map -- but a .bin built before a resolution change
     * still runs, and nothing rebuilds it. */
    if (IO_DATAW[2] != (unsigned)DISP_BYTES) return 2u;

    return 1u;
}

static int disp_have_hw(void) {
    if (disp_hw == 0u) disp_hw = disp_probe();
    return disp_hw == 1u;
}

/* Rows are contiguous, so walking a pointer along one beats recomputing
 * y*DISP_W + x per pixel -- that is a MUL plus address arithmetic every
 * time round. */
static color_t *row_ptr(unsigned x, unsigned y) {
    return ((color_t *)disp_target) + y * DISP_W + x;
}

int disp_use_back_buffer(void) {
    void *buffer;
    if (disp_back != 0u) return 1;                 /* already have one */
    buffer = malloc(DISP_BYTES);
    if (buffer == NULL) return 0;
    disp_back = (unsigned)buffer;
    disp_target = disp_back;
    return 1;
}

void disp_present(void) {
    if (disp_back == 0u) return;                   /* drawing straight to it */

    if (disp_have_hw()) {
        /* Hand the display the buffer just drawn. One store each for the
         * five header fields, instead of a copy of every pixel. */
        disp_call(0u, DISP_CMD_SET_BASE, 4u, disp_target);
        if (IO_DATAW[0] != 0u) {
            /* Draw the next frame into whichever surface just left the
             * screen. It still holds the frame BEFORE the one now
             * showing -- see the note in display.h. */
            disp_target = (disp_target == disp_back) ? DISP_BASE : disp_back;
            return;
        }
        disp_hw = 2u;                              /* refused: stop asking */
    }

    /* No device, or it would not take the base: copy, as before. The
     * screen is DISP_BASE again, so drawing goes back to the heap. */
    {
        color_t *src = (color_t *)disp_target;
        color_t *dst = (color_t *)DISP_BASE;
        unsigned n = DISP_W * DISP_H;
        if (disp_target == DISP_BASE) return;
        while (n > 0u) { *dst = *src; dst++; src++; n--; }
        disp_target = disp_back;
    }
}

void disp_set(unsigned x, unsigned y, color_t c) {
    if (x >= DISP_W) return;
    if (y >= DISP_H) return;
    *row_ptr(x, y) = c;
}

color_t disp_get(unsigned x, unsigned y) {
    if (x >= DISP_W) return 0;
    if (y >= DISP_H) return 0;
    return *row_ptr(x, y);
}

void disp_clear(color_t c) {
    /* disp_target, not DISP_BASE: with a back buffer in play this must
     * clear the buffer being drawn into, or the clear lands on screen
     * while every shape lands in the buffer. */
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
        unsigned n = DISP_W * DISP_H;
        while (n > 0u) { *p = c; p++; n--; }
    }
}

void disp_hline(unsigned x, unsigned y, unsigned w, color_t c) {
    color_t *p;
    if (y >= DISP_H || x >= DISP_W) return;
    if (x + w > DISP_W) w = DISP_W - x;      /* clip, do not bail */
    p = row_ptr(x, y);
    while (w > 0u) { *p = c; p++; w--; }
}

void disp_vline(unsigned x, unsigned y, unsigned h, color_t c) {
    color_t *p;
    if (x >= DISP_W || y >= DISP_H) return;
    if (y + h > DISP_H) h = DISP_H - y;
    p = row_ptr(x, y);
    while (h > 0u) { *p = c; p = p + DISP_W; h--; }
}

void disp_rect(unsigned x, unsigned y, unsigned w, unsigned h, color_t c) {
    unsigned row = 0u;
    if (x >= DISP_W || y >= DISP_H) return;
    if (x + w > DISP_W) w = DISP_W - x;
    if (y + h > DISP_H) h = DISP_H - y;
    while (row < h) { disp_hline(x, y + row, w, c); row++; }
}

void disp_frame(unsigned x, unsigned y, unsigned w, unsigned h, color_t c) {
    if (w == 0u || h == 0u) return;
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
    if (r < 0) return;
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

/* 4x6 font, printable ASCII 0x20..0x7E. One byte per glyph row; only the
 * top 4 bits are used, tested high to low. 95 glyphs x 6 rows. */
static char FONT[] = {
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x20, 0x20, 0x20, 0x00, 0x00, 0x20,
        0x50, 0x50, 0x00, 0x00, 0x00, 0x00,
        0x50, 0xF0, 0x50, 0xF0, 0x50, 0x00,
        0x60, 0xC0, 0x60, 0xC0, 0x60, 0x00,
        0x90, 0x20, 0x40, 0x80, 0x00, 0x90,
        0x60, 0xA0, 0x40, 0xA0, 0x60, 0x00,
        0x20, 0x20, 0x00, 0x00, 0x00, 0x00,
        0x20, 0x40, 0x40, 0x40, 0x20, 0x00,
        0x40, 0x20, 0x20, 0x20, 0x40, 0x00,
        0x00, 0xA0, 0x40, 0xA0, 0x00, 0x00,
        0x00, 0x40, 0xE0, 0x40, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x40, 0x20,
        0x00, 0x00, 0xE0, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x20, 0x00,
        0x10, 0x20, 0x40, 0x80, 0x00, 0x00,
        0x60, 0xA0, 0xA0, 0xA0, 0x60, 0x00,
        0x20, 0x60, 0x40, 0x40, 0x70, 0x00,
        0xE0, 0x20, 0x40, 0x80, 0x70, 0x00,
        0xE0, 0x20, 0x60, 0x20, 0xE0, 0x00,
        0xA0, 0xA0, 0xE0, 0x20, 0x20, 0x00,
        0xE0, 0x80, 0xE0, 0x20, 0xE0, 0x00,
        0x60, 0x80, 0xE0, 0xA0, 0xE0, 0x00,
        0xE0, 0x20, 0x20, 0x40, 0x40, 0x00,
        0x60, 0xA0, 0x60, 0xA0, 0x60, 0x00,
        0x60, 0xA0, 0xE0, 0x20, 0xE0, 0x00,
        0x00, 0x20, 0x00, 0x20, 0x00, 0x00,
        0x00, 0x20, 0x00, 0x00, 0x40, 0x20,
        0x20, 0x40, 0x80, 0x40, 0x20, 0x00,
        0x00, 0xE0, 0x00, 0xE0, 0x00, 0x00,
        0x80, 0x40, 0x20, 0x40, 0x80, 0x00,
        0xE0, 0x20, 0x20, 0x00, 0x20, 0x00,
        0x60, 0xA0, 0xE0, 0xC0, 0x60, 0x00,
        0x40, 0xA0, 0xE0, 0xA0, 0xA0, 0x00,
        0xC0, 0xA0, 0xC0, 0xA0, 0xC0, 0x00,
        0x60, 0x80, 0x80, 0x80, 0x60, 0x00,
        0xC0, 0xA0, 0xA0, 0xA0, 0xC0, 0x00,
        0xE0, 0x80, 0xE0, 0x80, 0xE0, 0x00,
        0xE0, 0x80, 0xE0, 0x80, 0x80, 0x00,
        0x60, 0x80, 0x80, 0xA0, 0x60, 0x00,
        0xA0, 0xA0, 0xE0, 0xA0, 0xA0, 0x00,
        0xE0, 0x40, 0x40, 0x40, 0xE0, 0x00,
        0x20, 0x20, 0x20, 0xA0, 0x40, 0x00,
        0xA0, 0xA0, 0xC0, 0xA0, 0xA0, 0x00,
        0x80, 0x80, 0x80, 0x80, 0xE0, 0x00,
        0xA0, 0xE0, 0xE0, 0xA0, 0xA0, 0x00,
        0xA0, 0xE0, 0xE0, 0xE0, 0xA0, 0x00,
        0x40, 0xA0, 0xA0, 0xA0, 0x40, 0x00,
        0xC0, 0xA0, 0xC0, 0x80, 0x80, 0x00,
        0x40, 0xA0, 0xA0, 0xE0, 0x60, 0x00,
        0xC0, 0xA0, 0xC0, 0xA0, 0xA0, 0x00,
        0x60, 0x80, 0x60, 0x20, 0x40, 0xC0,
        0xE0, 0x40, 0x40, 0x40, 0x40, 0x00,
        0xA0, 0xA0, 0xA0, 0xA0, 0x60, 0x00,
        0xA0, 0xA0, 0xA0, 0xA0, 0x40, 0x00,
        0xA0, 0xA0, 0xE0, 0xE0, 0xA0, 0x00,
        0xA0, 0xA0, 0x40, 0xA0, 0xA0, 0x00,
        0xA0, 0xA0, 0x40, 0x40, 0x40, 0x00,
        0xE0, 0x20, 0x40, 0x80, 0xE0, 0x00,
        0x60, 0x40, 0x40, 0x40, 0x60, 0x00,
        0x80, 0x80, 0x40, 0x20, 0x10, 0x00,
        0x60, 0x20, 0x20, 0x20, 0x60, 0x00,
        0x40, 0xA0, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0xE0, 0x00,
        0x40, 0x20, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x60, 0xA0, 0xA0, 0x60, 0x00,
        0x80, 0xC0, 0xA0, 0xA0, 0xC0, 0x00,
        0x00, 0x60, 0x80, 0x80, 0x60, 0x00,
        0x20, 0x60, 0xA0, 0xA0, 0x60, 0x00,
        0x00, 0x60, 0xE0, 0xC0, 0x60, 0x00,
        0x20, 0x40, 0xE0, 0x40, 0x40, 0x00,
        0x00, 0x60, 0xA0, 0x60, 0x20, 0xC0,
        0x80, 0xC0, 0xA0, 0xA0, 0xA0, 0x00,
        0x40, 0x00, 0x20, 0x40, 0x40, 0x00,
        0x20, 0x00, 0x20, 0xA0, 0x40, 0x00,
        0x80, 0xA0, 0xC0, 0xA0, 0xA0, 0x00,
        0x40, 0x40, 0x40, 0x40, 0x20, 0x00,
        0x00, 0xE0, 0xE0, 0xA0, 0xA0, 0x00,
        0x00, 0xC0, 0xA0, 0xA0, 0xA0, 0x00,
        0x00, 0x40, 0xA0, 0xA0, 0x40, 0x00,
        0x00, 0xC0, 0xA0, 0xA0, 0xC0, 0x80,
        0x00, 0x60, 0xA0, 0xA0, 0x60, 0x20,
        0x00, 0xA0, 0xC0, 0x80, 0x80, 0x00,
        0x00, 0x60, 0x80, 0x20, 0x60, 0x00,
        0x40, 0xE0, 0x40, 0x40, 0x60, 0x00,
        0x00, 0xA0, 0xA0, 0xA0, 0x60, 0x00,
        0x00, 0xA0, 0xA0, 0xA0, 0x40, 0x00,
        0x00, 0xA0, 0xA0, 0xE0, 0xE0, 0x00,
        0x00, 0xA0, 0x40, 0x40, 0xA0, 0x00,
        0x00, 0xA0, 0xA0, 0x60, 0x20, 0xC0,
        0x00, 0xE0, 0x20, 0x40, 0x80, 0xE0,
        0x20, 0x60, 0x40, 0x20, 0x60, 0x00,
        0x20, 0x20, 0x20, 0x20, 0x20, 0x00,
        0xC0, 0x40, 0x20, 0xC0, 0x40, 0x00,
        0x00, 0x50, 0xA0, 0x00, 0x00, 0x00
};

void disp_char(unsigned x, unsigned y, int ch, color_t fg) {
    unsigned index;
    unsigned row;
    unsigned col;
    unsigned bits;

    if (ch < 0x20 || ch > 0x7E) return;
    index = ((unsigned)ch - 0x20u) * GLYPH_H;
    for (row = 0u; row < GLYPH_H; row++) {
        bits = ((unsigned)FONT[index + row]) & 0xF0u;
        for (col = 0u; col < GLYPH_W; col++) {
            if (bits & (0x80u >> col)) disp_set(x + col, y + row, fg);
        }
    }
}

void disp_text(unsigned x, unsigned y, char *s, color_t fg) {
    while (*s) {
        disp_char(x, y, (int)*s, fg);
        x = x + GLYPH_W + 1u;
        s++;
    }
}
