/* <pigeon/display.h> -- drawing on the memory-mapped framebuffer.
 *
 * The screen is just memory, so this is pointer arithmetic and stores:
 * no IO channel, no driver. Nothing needs telling that the screen
 * changed -- the emulator snapshots the region at 30 FPS, so a store is
 * visible within a frame.
 *
 * Colour words are 0xAARRGGBB. Alpha is NOT blended: the emulator hands
 * it straight to the canvas, so anything with AA = 0x00 is invisible.
 * Always set 0xFF unless you mean it.
 */
#ifndef PIGEON_DISPLAY_H
#define PIGEON_DISPLAY_H

#define DISP_W 100
#define DISP_H 100
#define DISP_BASE 0x1418

typedef unsigned int color_t;

#define BLACK   0xFF000000
#define WHITE   0xFFFFFFFF
#define RED     0xFFFF0000
#define GREEN   0xFF00FF00
#define BLUE    0xFF0000FF
#define YELLOW  0xFFFFFF00
#define CYAN    0xFF00FFFF
#define MAGENTA 0xFFFF00FF
#define GREY    0xFF808080

/* --- double buffering ---------------------------------------------------
 *
 * The emulator snapshots the framebuffer on its own clock, so it will
 * happily catch a half-drawn screen: clear, then a few shapes, then a
 * snapshot -- which is a black flash. Draw into a back buffer and the
 * screen only ever receives whole frames.
 *
 *     if (!disp_use_back_buffer()) { ... out of memory ... }
 *     for (;;) { draw_everything(); disp_present(); }
 *
 * The buffer comes from the heap, so it costs nothing in the program
 * image -- 40,000 bytes of `.space` would otherwise be copied by the
 * BIOS on every boot.
 */
int  disp_use_back_buffer(void);   /* 0 if the heap could not provide one */
void disp_present(void);           /* copy the back buffer to the screen  */

/* --- pixels ------------------------------------------------------------ */
void    disp_set(unsigned x, unsigned y, color_t c);   /* clipped */
color_t disp_get(unsigned x, unsigned y);              /* 0 when off-screen */

/* --- shapes ------------------------------------------------------------ */
void disp_clear(color_t c);
void disp_hline(unsigned x, unsigned y, unsigned w, color_t c);
void disp_vline(unsigned x, unsigned y, unsigned h, color_t c);
void disp_rect(unsigned x, unsigned y, unsigned w, unsigned h, color_t c);
void disp_frame(unsigned x, unsigned y, unsigned w, unsigned h, color_t c);
void disp_line(int x0, int y0, int x1, int y1, color_t c);
void disp_circle(int cx, int cy, int r, color_t c);

/* --- text: a 4x6 font over printable ASCII ----------------------------- */
#define GLYPH_W 4
#define GLYPH_H 6
void disp_char(unsigned x, unsigned y, int ch, color_t fg);
void disp_text(unsigned x, unsigned y, char *s, color_t fg);

#endif
