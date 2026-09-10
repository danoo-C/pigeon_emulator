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

/* The geometry comes from the machine, not from here. DISPLAY_W,
 * DISPLAY_H and DISPLAY_START are predefined by the compiler out of
 * emulator/memory_map.py -- the same names the assembler injects. These
 * three were literals until the screen changed shape, and nothing
 * checked them against the machine; a resolution change updated the
 * emulator and left every C program drawing at the old size. */
#define DISP_W    DISPLAY_W
#define DISP_H    DISPLAY_H
#define DISP_BASE DISPLAY_START

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
 * image -- a screen of `.space` would otherwise be copied by the BIOS on
 * every boot.
 *
 * disp_present() is a PAGE FLIP, not a copy. It hands the display the
 * address of the buffer you just drew and hands you back the one that
 * was on screen. Both surfaces are real and both keep their contents, so
 *
 *     the buffer you draw into after a present still holds the frame
 *     BEFORE the one now showing -- not a clean slate.
 *
 * Clear it, or write every pixel. disp_clear() is a hardware fill and
 * costs about six instructions, so there is no reason not to. The one
 * pattern this breaks is redrawing only what changed: the parts you skip
 * are two frames old, not one.
 *
 * disp_get() reads through the draw target, so after a present it reads
 * that same two-frames-ago image, not what is on screen.
 *
 * With no display device on the bus -- a bare CPU, as tests/test_libs.py
 * builds -- present falls back to copying and none of the above applies.
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

/* --- text: a 5x7 font over printable ASCII -----------------------------
 *
 * GLYPH_W x GLYPH_H is the CELL, not the ink. The glyph body is 5x7 on
 * rows 0..6 with the baseline on row 6; row 7 carries the descenders of
 * g j p q y and the tails of , ; _. Leave a column between cells --
 * disp_text() advances by GLYPH_W + 1 -- and a row between lines.
 *
 * Lay text out from these two names rather than from 5 and 8. The font
 * was 4x6 until it became unreadable, and every caller that had baked
 * the old numbers into a y-offset drew its next line through the
 * descenders. */
#define GLYPH_W 5
#define GLYPH_H 8
void disp_char(unsigned x, unsigned y, int ch, color_t fg);
void disp_text(unsigned x, unsigned y, char *s, color_t fg);

#endif
