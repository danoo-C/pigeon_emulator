/* The graphics accelerator, CH_GAC, from a program's side. See gac.h, and
 * emulator/devices/gac.py for the device.
 *
 * Every command is sent with R/W 0: its arguments are words in the data
 * window, and its answer comes back as the window's first word.
 */
#include <pigeon/gac.h>
#include <pigeon/io.h>

/* Prefixed: units share one macro table. */
#define GAC_CMD_INFO        1u
#define GAC_CMD_FILL        2u
#define GAC_CMD_FRAME       3u
#define GAC_CMD_BLIT        4u
#define GAC_CMD_BLIT_SCALED 5u
#define GAC_CMD_LINE        6u
#define GAC_CMD_CIRCLE      7u
#define GAC_CMD_DISC        8u
#define GAC_CMD_SET_FONT    9u
#define GAC_CMD_TEXT        10u
#define GAC_CMD_SCROLL      11u
#define GAC_CMD_BLIT_ALPHA  14u
#define GAC_CMD_RAM_SURFACE 15u
#define GAC_CMD_RAM_FREE    16u
#define GAC_MAGIC           0x41474750u     /* "PGGA" */
#define GAC_WINDOW          (IO_SIZE - IO_USABLE_AFTER)
#define GAC_TEXT_MAX        (GAC_WINDOW - 24u)   /* after TEXT's six words */

/* 0 = not asked yet, 1 = there, 2 = not. */
static unsigned gac_there = 0u;
static unsigned gac_feat = 0u;             /* INFO's feature bits, once probed */
static unsigned gac_cell_w = 0u;           /* the font's cell, for a long text's pieces */

/* One command whose arguments are already in the window: its answer. */
static unsigned gac_call(unsigned command, unsigned address) {
    IO_RW   = 0u;
    IO_CMD  = command;
    IO_LEN  = 4u;
    IO_ADDR = address;
    IO_CH   = CH_GAC;              /* this store fires it -- must be last */
    return IO_DATAW[0];
}

/* vram.c's probe, for this channel: see there for why it is in this order. */
int gac_present(void) {
    if (gac_there == 0u) {
        gac_there = 2u;
        IO_RW   = 0u;
        IO_CMD  = GAC_CMD_INFO;
        IO_LEN  = 4u;
        IO_ADDR = 0u;
        IO_CH   = CH_GAC;
        if (IO_CH != 0u) {
            IO_CH = 0u;
        } else if (IO_RETLEN != 0xFFFFFFFFu && IO_RETLEN >= 12u && IO_DATAW[0] == GAC_MAGIC) {
            gac_there = 1u;
            gac_feat = IO_DATAW[1];    /* the same reply: no second command */
        }
    }
    return gac_there == 1u;
}

unsigned gac_features(void) {
    if (!gac_present()) return 0u;
    return gac_feat;
}

unsigned gac_ram_surface(unsigned address, unsigned w, unsigned h) {
    if (!gac_present()) return 0u;
    IO_DATAW[0] = address;
    IO_DATAW[1] = w;
    IO_DATAW[2] = h;
    return gac_call(GAC_CMD_RAM_SURFACE, 0u);
}

int gac_ram_free(unsigned handle) {
    if (!gac_present()) return 0;
    return gac_call(GAC_CMD_RAM_FREE, handle) == 1u;
}

/* The shapes: a destination, four numbers, a colour. */
static int gac_shape(unsigned command, unsigned dst, int a, int b, int c, int d,
                     unsigned colour) {
    if (!gac_present()) return 0;
    IO_DATAW[0] = dst;
    IO_DATAW[1] = (unsigned)a;
    IO_DATAW[2] = (unsigned)b;
    IO_DATAW[3] = (unsigned)c;
    IO_DATAW[4] = (unsigned)d;
    IO_DATAW[5] = colour;
    return gac_call(command, 0u) == 1u;
}

int gac_fill(unsigned dst, int x, int y, int w, int h, unsigned colour) {
    return gac_shape(GAC_CMD_FILL, dst, x, y, w, h, colour);
}

int gac_frame(unsigned dst, int x, int y, int w, int h, unsigned colour) {
    return gac_shape(GAC_CMD_FRAME, dst, x, y, w, h, colour);
}

int gac_line(unsigned dst, int x0, int y0, int x1, int y1, unsigned colour) {
    return gac_shape(GAC_CMD_LINE, dst, x0, y0, x1, y1, colour);
}

/* CIRCLE and DISC take one number fewer: the colour moves up a word. */
static int gac_round(unsigned command, unsigned dst, int cx, int cy, int r, unsigned colour) {
    if (!gac_present()) return 0;
    IO_DATAW[0] = dst;
    IO_DATAW[1] = (unsigned)cx;
    IO_DATAW[2] = (unsigned)cy;
    IO_DATAW[3] = (unsigned)r;
    IO_DATAW[4] = colour;
    return gac_call(command, 0u) == 1u;
}

int gac_circle(unsigned dst, int cx, int cy, int r, unsigned colour) {
    return gac_round(GAC_CMD_CIRCLE, dst, cx, cy, r, colour);
}

int gac_disc(unsigned dst, int cx, int cy, int r, unsigned colour) {
    return gac_round(GAC_CMD_DISC, dst, cx, cy, r, colour);
}

int gac_scroll(unsigned dst, int x, int y, int w, int h, int dy, unsigned bg) {
    if (!gac_present()) return 0;
    IO_DATAW[0] = dst;
    IO_DATAW[1] = (unsigned)x;
    IO_DATAW[2] = (unsigned)y;
    IO_DATAW[3] = (unsigned)w;
    IO_DATAW[4] = (unsigned)h;
    IO_DATAW[5] = (unsigned)dy;
    IO_DATAW[6] = bg;
    return gac_call(GAC_CMD_SCROLL, 0u) == 1u;
}

/* BLIT and BLIT_ALPHA: the same eight words, and an alpha after them. */
static void gac_copy_args(unsigned src, int sx, int sy, unsigned dst, int dx, int dy,
                          int w, int h) {
    IO_DATAW[0] = src;
    IO_DATAW[1] = (unsigned)sx;
    IO_DATAW[2] = (unsigned)sy;
    IO_DATAW[3] = dst;
    IO_DATAW[4] = (unsigned)dx;
    IO_DATAW[5] = (unsigned)dy;
    IO_DATAW[6] = (unsigned)w;
    IO_DATAW[7] = (unsigned)h;
}

int gac_blit(unsigned src, int sx, int sy, unsigned dst, int dx, int dy, int w, int h) {
    if (!gac_present()) return 0;
    gac_copy_args(src, sx, sy, dst, dx, dy, w, h);
    return gac_call(GAC_CMD_BLIT, 0u) == 1u;
}

int gac_blit_alpha(unsigned src, int sx, int sy, unsigned dst, int dx, int dy,
                   int w, int h, unsigned alpha) {
    if (!gac_present()) return 0;
    gac_copy_args(src, sx, sy, dst, dx, dy, w, h);
    IO_DATAW[8] = alpha;
    return gac_call(GAC_CMD_BLIT_ALPHA, 0u) == 1u;
}

int gac_blit_scaled(unsigned src, int sx, int sy, int sw, int sh,
                    unsigned dst, int dx, int dy, int dw, int dh) {
    if (!gac_present()) return 0;
    IO_DATAW[0] = src;
    IO_DATAW[1] = (unsigned)sx;
    IO_DATAW[2] = (unsigned)sy;
    IO_DATAW[3] = (unsigned)sw;
    IO_DATAW[4] = (unsigned)sh;
    IO_DATAW[5] = dst;
    IO_DATAW[6] = (unsigned)dx;
    IO_DATAW[7] = (unsigned)dy;
    IO_DATAW[8] = (unsigned)dw;
    IO_DATAW[9] = (unsigned)dh;
    return gac_call(GAC_CMD_BLIT_SCALED, 0u) == 1u;
}

int gac_set_font(char *glyphs, unsigned glyph_w, unsigned glyph_h,
                 unsigned cell_w, unsigned cell_h, unsigned first, unsigned count) {
    if (!gac_present()) return 0;
    IO_DATAW[0] = (unsigned)glyphs;
    IO_DATAW[1] = glyph_w;
    IO_DATAW[2] = glyph_h;
    IO_DATAW[3] = cell_w;
    IO_DATAW[4] = cell_h;
    IO_DATAW[5] = first;
    IO_DATAW[6] = count;
    if (gac_call(GAC_CMD_SET_FONT, 0u) != 1u) return 0;
    gac_cell_w = cell_w;
    return 1;
}

int gac_text(unsigned dst, int x, int y, unsigned fg, unsigned bg, char *s, unsigned n) {
    unsigned chunk;
    unsigned i;
    volatile unsigned char *to;
    if (!gac_present()) return 0;
    while (n > 0u) {
        chunk = n > GAC_TEXT_MAX ? GAC_TEXT_MAX : n;
        IO_DATAW[0] = dst;
        IO_DATAW[1] = (unsigned)x;
        IO_DATAW[2] = (unsigned)y;
        IO_DATAW[3] = fg;
        IO_DATAW[4] = bg;
        IO_DATAW[5] = chunk;
        to = IO_DATA + 24u;
        for (i = 0u; i < chunk; i++) to[i] = (unsigned char)s[i];
        if (gac_call(GAC_CMD_TEXT, 0u) != 1u) return 0;
        s = s + chunk;
        n = n - chunk;
        x = x + (int)(chunk * gac_cell_w);
    }
    return 1;
}
