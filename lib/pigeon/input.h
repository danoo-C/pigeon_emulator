/* <pigeon/input.h> -- keyboard and mouse.
 *
 * Unlike the display, input is not memory-mapped: it goes through the IO
 * controller on channel 3. Two buffers, because guest code asks two
 * different questions:
 *
 *   FIFO       "what happened, in order?"   -- typing, clicks
 *   real-time  "what is true right now?"    -- hold-to-move, drag
 *
 * Drain the FIFOs each frame, then sample the state. Neither substitutes
 * for the other: a key tapped between two frames is invisible to the
 * state bitmap, and replaying every edge since boot to learn whether W is
 * held is absurd.
 */
#ifndef PIGEON_INPUT_H
#define PIGEON_INPUT_H

/* --- mouse: real-time -------------------------------------------------- */
unsigned mouse_x(void);
unsigned mouse_y(void);
unsigned mouse_buttons(void);

#define MB_LEFT    0x01
#define MB_RIGHT   0x02
#define MB_MIDDLE  0x04
#define MB_BACK    0x08
#define MB_FORWARD 0x10

/* --- mouse: FIFO ------------------------------------------------------- */
unsigned mouse_event(void);        /* 0 when the queue is empty */
#define ME_VALID(e)   ((e) & 0x80)
#define ME_PRESSED(e) ((e) & 0x40)
#define ME_BUTTON(e)  ((e) & 0x1F)

/* --- keyboard: FIFO ---------------------------------------------------- */
int      key_read(void);           /* next character, or -1 if none */
unsigned key_event(void);          /* (flags << 8) | code; 0 when empty */
#define KE_VALID(e)   ((e) & 0x8000)
#define KE_PRESSED(e) ((e) & 0x4000)
#define KE_CODE(e)    ((e) & 0xFF)

/* --- keyboard: real-time ----------------------------------------------- */
int  key_down(int code);
void key_bitmap(unsigned char *out);      /* 32 bytes, bit N = key N held */

/* Printable ASCII passes through unchanged; named keys live in 0x80-0x9F,
 * which ASCII does not use. Front ends translate into this space. */
#define KEY_BACKSPACE 0x08
#define KEY_TAB       0x09
#define KEY_ENTER     0x0D
#define KEY_ESC       0x1B
#define KEY_DELETE    0x7F
#define KEY_LEFT      0x80
#define KEY_RIGHT     0x81
#define KEY_UP        0x82
#define KEY_DOWN      0x83
#define KEY_HOME      0x84
#define KEY_END       0x85
#define KEY_PGUP      0x86
#define KEY_PGDN      0x87
#define KEY_INSERT    0x88
#define KEY_F1        0x90

#endif
