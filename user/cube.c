/* A spinning 3D cube you can drag with the mouse.
 *
 *   hold left button + drag   rotate
 *   right button              reset to the starting angles
 *   space                     toggle auto-spin
 *   escape                    quit
 *
 * All the arithmetic lives in <pigeon/math.h>: there is no floating
 * point on this machine, angles are 0..255 around the circle, and the
 * fixed-point helpers are the ones that know SHR is a logical shift and
 * DIV is unsigned. This file is just the cube.
 *
 * The auto-spin is paced off the wall clock, not off the frame counter --
 * see "the clock" below for why.
 *
 * Build:
 *   python3 start_emulator.py cube --run
 */
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/io.h>
#include <pigeon/math.h>

#define HALF    30          /* half the cube's edge, in world units */
#define DIST    150         /* eye distance; larger = flatter perspective */
#define CX      (DISP_W / 2)          /* screen centre */
#define CY      (DISP_H / 2 - 4)      /* a little high, to clear the caption */

/* A full turn is 256 steps, so a drag across the whole screen should be
 * about one revolution. The screen is no longer square, so the two axes
 * do NOT share a gain -- with one, a horizontal drag out-rotates a
 * vertical one by the aspect ratio. */
#define YAW_GAIN   (256 / DISP_W)
#define PITCH_GAIN (256 / DISP_H)

/* Auto-spin rates, in Q8 angle steps per millisecond -- the same Q8 as
 * <pigeon/math.h>, so 256 of these is one whole step and a turn is 256
 * steps. 8 is therefore one revolution every 8.2 seconds. The pitch keeps
 * the 1:4 ratio it had back when it was gated on every fourth frame. */
#define YAW_RATE    8
#define PITCH_RATE  2

#define FACE    0xFF30C0FF
#define EDGE    0xFF60E0FF
#define BACK    0xFF102030
#define DIM     0xFF505868


/* --- the clock ------------------------------------------------------------
 *
 * The auto-spin used to advance one step per frame, which pinned its
 * speed to however fast the emulator happened to run -- so the same
 * binary now whips round tens of times faster than it did before the
 * interpreter and the codegen were optimised. Spinning by elapsed real
 * time instead makes the speed a property of the demo rather than of the
 * host it lands on.
 *
 * CH_TIMER counts down in wall-clock milliseconds; <pigeon/io.h> has the
 * bus rules, and the reply is two words in the data window: the status,
 * then the milliseconds left. Reading that twice and subtracting gives
 * the frame's duration. Both readings come off one absolute countdown, so
 * a frame shorter than a millisecond reads as 0 ms and its remainder
 * turns up in a later frame instead of being rounded away -- which
 * matters here, because frames now are that short.
 */
#define TIMER_ID      0
#define TCMD_START    1
#define TCMD_STATUS   5

#define CLOCK_SPAN    10000u   /* ms per countdown, renewed well before zero */
#define CLOCK_LOW      1000u   /* renew it once the span is down to this */
#define CLOCK_MAX_DT    100u   /* a longer gap is a stall, not motion */

static unsigned clock_left;    /* ms left on the countdown, as last read */

static unsigned clock_read(void) {
    IO_RW = 0;                             /* read */
    IO_CMD = TCMD_STATUS;
    IO_LEN = 0;
    IO_ADDR = TIMER_ID;
    IO_CH = CH_TIMER;                      /* this store fires it -- last */
    return IO_DATAW[1];                    /* word 0 is status, word 1 the ms */
}

static void clock_restart(void) {
    IO_RW = 0;
    IO_CMD = TCMD_START;
    IO_LEN = CLOCK_SPAN;                   /* for START, LEN is a duration */
    IO_ADDR = TIMER_ID;
    IO_CH = CH_TIMER;
    clock_left = CLOCK_SPAN;
}

/* Milliseconds since the previous call; 0 on the first one. */
static unsigned clock_delta(void) {
    unsigned left = clock_read();
    unsigned dt;

    /* 0 means never started or run out; larger than last time means the
     * countdown was renewed underneath us. Neither is a measurable gap. */
    if (left == 0u || left > clock_left) {
        clock_restart();
        return 0u;
    }

    dt = clock_left - left;
    clock_left = left;

    /* Renew after taking the reading, so the span rolls over without
     * costing a frame's worth of time. */
    if (left < CLOCK_LOW) clock_restart();

    if (dt > CLOCK_MAX_DT) dt = CLOCK_MAX_DT;
    return dt;
}


/* Eight corners of a cube, and the twelve edges joining them. */
static int VX[8] = { -HALF,  HALF,  HALF, -HALF, -HALF,  HALF,  HALF, -HALF };
static int VY[8] = { -HALF, -HALF,  HALF,  HALF, -HALF, -HALF,  HALF,  HALF };
static int VZ[8] = { -HALF, -HALF, -HALF, -HALF,  HALF,  HALF,  HALF,  HALF };

static int EA[12] = { 0, 1, 2, 3,  4, 5, 6, 7,  0, 1, 2, 3 };
static int EB[12] = { 1, 2, 3, 0,  5, 6, 7, 4,  4, 5, 6, 7 };

/* projected screen coordinates, filled in each frame */
static int PX[8];
static int PY[8];

int angle_x;
int angle_y;
unsigned spin_x_acc;      /* Q8 fractions of a step, waiting to carry */
unsigned spin_y_acc;
int spinning;
int dragging;
int last_mx;
int last_my;
int running;

/* --- the actual 3D --------------------------------------------------------
 *
 * Rotate about Y, then about X, then divide by depth for perspective.
 */
static void project(void) {
    vec3 v;
    int i;

    for (i = 0; i < 8; i++) {
        v3_set(&v, VX[i], VY[i], VZ[i]);
        v3_rotate_y(&v, &v, angle_y);          /* yaw, then pitch -- the */
        v3_rotate_x(&v, &v, angle_x);          /* library tolerates out == in */
        v3_project(&v, DIST, CX, CY, &PX[i], &PY[i]);
    }
}

static void draw_cube(void) {
    int i;
    disp_clear(BACK);

    /* a floor grid, so the rotation reads as rotation */
    for (i = 0; i < 5; i++) {
        disp_hline(DISP_W / 10, DISP_H - 30 + i * 4, (DISP_W * 8) / 10,
                   i == 0 ? DIM : 0xFF202838);
    }

    for (i = 0; i < 12; i++) {
        int a = EA[i];
        int b = EB[i];
        /* the four edges of the far face are drawn dimmer */
        disp_line(PX[a], PY[a], PX[b], PY[b], i < 4 ? EDGE : FACE);
    }

    /* corner dots, so you can see the vertices themselves */
    for (i = 0; i < 8; i++) {
        disp_set((unsigned)PX[i], (unsigned)PY[i], WHITE);
    }

    disp_text(2, 2, dragging ? "DRAG" : (spinning ? "SPIN" : "HOLD"), DIM);
    disp_text(2, DISP_H - GLYPH_H - 2, "drag to rotate", DIM);
    disp_present();
}

/* --- input ---------------------------------------------------------------- */

static void handle_mouse(void) {
    unsigned buttons = mouse_buttons();
    int mx = (int)mouse_x();
    int my = (int)mouse_y();

    if (buttons & MB_RIGHT) {
        angle_x = 24;
        angle_y = 32;
        return;
    }

    if (buttons & MB_LEFT) {
        if (dragging) {
            angle_y = angle_y + (mx - last_mx) * YAW_GAIN;
            angle_x = angle_x + (my - last_my) * PITCH_GAIN;
        }
        dragging = 1;
        last_mx = mx;
        last_my = my;
    } else {
        dragging = 0;
    }
}

static void handle_keys(void) {
    int code = key_read();
    while (code >= 0) {
        if (code == KEY_ESC) running = 0;
        if (code == ' ') spinning = !spinning;
        code = key_read();
    }
}

int main(void) {
    long frame = 0;

    disp_use_back_buffer();

    angle_x = 24;
    angle_y = 32;
    spin_x_acc = 0;
    spin_y_acc = 0;
    spinning = 1;
    dragging = 0;
    running = 1;

    clock_restart();

    while (running) {
        unsigned dt = clock_delta();

        handle_keys();
        handle_mouse();

        /* Auto-spin only when the mouse is not driving it, so a drag
         * feels like it is holding the cube rather than fighting it. The
         * rates are per millisecond and in Q8, so most frames add a
         * fraction of a step; the accumulators keep what has not carried
         * into a whole step yet. */
        if (spinning && !dragging) {
            spin_y_acc = spin_y_acc + YAW_RATE * dt;
            spin_x_acc = spin_x_acc + PITCH_RATE * dt;
            angle_y = angle_y + (int)(spin_y_acc >> FX_BITS);
            angle_x = angle_x + (int)(spin_x_acc >> FX_BITS);
            spin_y_acc = spin_y_acc & (FX_ONE - 1);
            spin_x_acc = spin_x_acc & (FX_ONE - 1);
        }

        angle_x = angle_x & ANGLE_MASK;
        angle_y = angle_y & ANGLE_MASK;

        project();
        draw_cube();

        // frame++;
        // if (frame > 2000000) running = 0;
    }
    return 0;
}
