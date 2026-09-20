/* <pigeon/display.h> -- drawing on the screen. The machine's side of it is
 * docs/gac.md (the accelerator) and docs/vram.md (modes, video memory).
 *
 * The screen is memory, so a pixel is one store: disp_set is pointer
 * arithmetic, no IO channel, no driver. Nothing needs telling that the
 * screen changed -- the emulator snapshots it at 30 FPS, so a store is
 * visible within a frame.
 *
 * Everything bigger than a pixel -- a clear, a rect, a line, a circle, a
 * string, a scroll, an image -- is one command to the machine's graphics
 * accelerator when it has one (docs/gac/), and drawn in software when it
 * does not. The pictures are the same either way; the accelerator is
 * faster, and blends.
 *
 * Colour words are 0xAARRGGBB. The screen ignores alpha: every pixel is
 * shown opaque, and memory nothing has drawn on is black. With the
 * accelerator, every shape BLENDS on its colour's alpha -- 0x80FF0000 is
 * half red over what is there -- and without it, or through disp_set,
 * the colour is stored as it is. Set 0xFF unless you mean it, as every
 * colour below does.
 */
#ifndef PIGEON_DISPLAY_H
#define PIGEON_DISPLAY_H

/* The geometry comes from the machine, at run time: the screen is
 * whatever size its mode is (docs/gac/plans/phase5_display_lib.md).
 * disp_init() asks, and every drawing call asks the first time if the
 * program has not. Until then these are the power-on screen, DISPLAY_W x
 * DISPLAY_H at DISPLAY_START -- predefined by the compiler out of
 * emulator/memory_map.py, as the assembler injects them.
 *
 * Call disp_init() before laying anything out by DISP_W: a program started
 * in another mode would otherwise see the power-on size.
 *
 * DISP_W and DISP_H are variables now, so they cannot size an array. Size
 * it from DISPLAY_MAX_W / DISPLAY_MAX_H, the largest mode there is, and use
 * DISP_W for how much of it this screen needs. DISP_BASE is where the
 * screen is: RAM at the power-on mode, video memory otherwise. */
/* int, as the DISPLAY_W and DISPLAY_H literals they replaced are: a program
 * that does signed arithmetic with the screen's size -- graph.c's plot area,
 * a row above the top compared with the bottom -- must not have it turn
 * unsigned under it (docs/gac/plans/phase6_console.md, As built). */
extern int disp_w;
extern int disp_h;
extern unsigned disp_base;
#define DISP_W    disp_w
#define DISP_H    disp_h
#define DISP_BASE disp_base

int disp_init(void);        /* asks the machine; returns 1. Safe to call again. */

/* While disp_hidden is set every drawing call does nothing. It is for a
 * program whose picture is not on the screen because another has put the
 * screen in a mode of its own -- the kernel's console, while a program runs
 * in a mode it chose (docs/gac/plans/phase7_setmode.md). disp_follow()
 * looks: it sets disp_hidden when the machine's mode is not this program's,
 * and, when it is, finds where this mode's screen now is. 1 if it is shown. */
extern int disp_hidden;
int disp_follow(void);

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
 * The buffer comes from video memory, or the heap on a machine without
 * it, so it costs nothing in the program image. The kernel gives it back
 * when the program ends.
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

/* --- modes ---------------------------------------------------------------
 *
 * A program may change the mode it runs in, and the kernel puts it back
 * when the program ends. After a disp_setmode, DISP_W, DISP_H and DISP_BASE
 * are the new screen's, it is black, and a back buffer has to be asked for
 * again. 0 when the machine does not offer that mode, or has no video
 * memory. */
int      disp_setmode(unsigned w, unsigned h);
int      disp_modes(unsigned *ws, unsigned *hs, int max);   /* -> how many there are */
unsigned disp_generation(void);    /* changes whenever the mode does */

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
void disp_circle(int cx, int cy, int r, color_t c);   /* an outline */
void disp_disc(int cx, int cy, int r, color_t c);     /* filled     */

/* Move the pixel rows from y to y + h by dy rows, up when dy is negative,
 * and fill the rows left behind with bg; what moves out of the band is
 * gone. A console's scroll. The display device moves the pixels when there
 * is one, so a whole screen costs a few thousand instructions, where
 * redrawing its text cost about a million. */
void disp_scroll(unsigned y, unsigned h, int dy, color_t bg);

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
/* n characters of s, which need not end in a 0 -- a row of a grid, say. One
 * accelerator command for all of them, as disp_text is. */
void disp_textn(unsigned x, unsigned y, char *s, unsigned n, color_t fg);

/* --- images -------------------------------------------------------------
 *
 * w x h pixels, a row after another, drawn with their top left at x, y and
 * clipped to the screen -- what bmp_load() returns, say. */
void disp_blit(unsigned *pixels, int x, int y, unsigned w, unsigned h);

#endif
