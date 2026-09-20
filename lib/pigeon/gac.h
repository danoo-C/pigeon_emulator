/* <pigeon/gac.h> -- the graphics accelerator: drawing done by the machine,
 * one bus command a shape (CH_GAC, docs/gac/).
 *
 *     if (gac_present()) gac_fill(GAC_SCREEN, 0, 0, 640, 360, 0xFF101018);
 *
 * Most programs never include this: <pigeon/display.h> sends its drawing
 * here when the machine has a GAC, and draws in software when it does not.
 *
 * Every shape is clipped by the device, so nothing is ever written outside
 * the surface named, whatever the numbers. Coordinates are signed. A colour
 * is 0xAARRGGBB, and blends on its alpha: 0xFF... is stored as it is,
 * 0x80... is half of it over what is there.
 *
 * gac_blit_alpha's alpha is 0 to 255 for one alpha over the whole rectangle,
 * or GAC_SRC_ALPHA for each source pixel's own -- an RGBA sprite with a soft
 * edge, drawn over what is there, its fully transparent parts not drawn at
 * all. gac_features() & GAC_FEATURE_SRC_ALPHA says whether the machine has
 * it; where it has not, the call returns 0 and draws nothing.
 *
 * A surface is a handle: GAC_SCREEN, one from vram_alloc(), or a rectangle
 * of RAM registered with gac_ram_surface(). Every call returns 1, or 0 when
 * the surface is not there. On a machine without a GAC, gac_present() is 0
 * and every call returns 0 and draws nothing.
 */
#ifndef PIGEON_GAC_H
#define PIGEON_GAC_H

#define GAC_SCREEN 0u                 /* the screen of the current mode, in video memory */

/* gac_blit_alpha: not one alpha for the rectangle, but each source pixel's. */
#define GAC_SRC_ALPHA 256u

/* gac_features() */
#define GAC_FEATURE_TEXT      1u
#define GAC_FEATURE_BLEND     2u
#define GAC_FEATURE_SRC_ALPHA 4u

int gac_present(void);

/* What this GAC can do: the GAC_FEATURE_* bits, or 0 with no GAC. */
unsigned gac_features(void);

unsigned gac_ram_surface(unsigned address, unsigned w, unsigned h);  /* a handle, or 0 */
int gac_ram_free(unsigned handle);

int gac_fill  (unsigned dst, int x, int y, int w, int h, unsigned colour);
int gac_frame (unsigned dst, int x, int y, int w, int h, unsigned colour);
int gac_line  (unsigned dst, int x0, int y0, int x1, int y1, unsigned colour);
int gac_circle(unsigned dst, int cx, int cy, int r, unsigned colour);
int gac_disc  (unsigned dst, int cx, int cy, int r, unsigned colour);
int gac_scroll(unsigned dst, int x, int y, int w, int h, int dy, unsigned bg);
int gac_blit  (unsigned src, int sx, int sy, unsigned dst, int dx, int dy, int w, int h);
int gac_blit_alpha(unsigned src, int sx, int sy, unsigned dst, int dx, int dy,
                   int w, int h, unsigned alpha);
int gac_blit_scaled(unsigned src, int sx, int sy, int sw, int sh,
                    unsigned dst, int dx, int dy, int dw, int dh);

/* A font of `count` glyphs from character `first`, one byte a glyph row,
 * top bit first, glyph_w <= 8, drawn in cells of cell_w x cell_h. The device
 * copies it: `glyphs` may be reused afterwards. */
int gac_set_font(char *glyphs, unsigned glyph_w, unsigned glyph_h,
                 unsigned cell_w, unsigned cell_h, unsigned first, unsigned count);
/* n characters of s, a cell each from x. A bg with alpha 0 draws no
 * background. Any length: a long string goes in pieces. */
int gac_text(unsigned dst, int x, int y, unsigned fg, unsigned bg, char *s, unsigned n);

#endif
