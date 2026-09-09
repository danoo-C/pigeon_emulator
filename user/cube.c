/* A spinning 3D cube you can drag with the mouse.
 *
 *   hold left button + drag   rotate
 *   right button              reset to the starting angles
 *   space                     toggle auto-spin
 *   escape                    quit
 *
 * There is no floating point on this machine, so everything here is
 * fixed point: angles are 0..255 around the circle and the sine table
 * below is scaled by 256 (call it Q8).
 *
 * Two machine facts drive the odd-looking helpers:
 *
 *   SHR is a LOGICAL shift -- zeros come in at the top -- so `x >> 8` on
 *   a negative number is garbage, not a divide by 256.
 *   DIV is UNSIGNED, so `a / b` with either side negative is wrong too.
 *
 * So smul() and sdiv() take the sign off, do the work on positives where
 * the hardware is correct, and put the sign back.
 *
 * Build:
 *   python3 start_emulator.py cube --run
 */
#include <pigeon/display.h>
#include <pigeon/input.h>

#define FP      8           /* fractional bits */
#define ONE     256         /* 1.0 in Q8 */
#define HALF    30          /* half the cube's edge, in world units */
#define DIST    150         /* eye distance; larger = flatter perspective */
#define CX      50          /* screen centre */
#define CY      46

#define FACE    0xFF30C0FF
#define EDGE    0xFF60E0FF
#define BACK    0xFF102030
#define DIM     0xFF505868

static int SIN[256] = {
        0,     6,    13,    19,    25,    31,    38,    44,
       50,    56,    62,    68,    74,    80,    86,    92,
       98,   104,   109,   115,   121,   126,   132,   137,
      142,   147,   152,   157,   162,   167,   172,   177,
      181,   185,   190,   194,   198,   202,   206,   209,
      213,   216,   220,   223,   226,   229,   231,   234,
      237,   239,   241,   243,   245,   247,   248,   250,
      251,   252,   253,   254,   255,   255,   256,   256,
      256,   256,   256,   255,   255,   254,   253,   252,
      251,   250,   248,   247,   245,   243,   241,   239,
      237,   234,   231,   229,   226,   223,   220,   216,
      213,   209,   206,   202,   198,   194,   190,   185,
      181,   177,   172,   167,   162,   157,   152,   147,
      142,   137,   132,   126,   121,   115,   109,   104,
       98,    92,    86,    80,    74,    68,    62,    56,
       50,    44,    38,    31,    25,    19,    13,     6,
        0,    -6,   -13,   -19,   -25,   -31,   -38,   -44,
      -50,   -56,   -62,   -68,   -74,   -80,   -86,   -92,
      -98,  -104,  -109,  -115,  -121,  -126,  -132,  -137,
     -142,  -147,  -152,  -157,  -162,  -167,  -172,  -177,
     -181,  -185,  -190,  -194,  -198,  -202,  -206,  -209,
     -213,  -216,  -220,  -223,  -226,  -229,  -231,  -234,
     -237,  -239,  -241,  -243,  -245,  -247,  -248,  -250,
     -251,  -252,  -253,  -254,  -255,  -255,  -256,  -256,
     -256,  -256,  -256,  -255,  -255,  -254,  -253,  -252,
     -251,  -250,  -248,  -247,  -245,  -243,  -241,  -239,
     -237,  -234,  -231,  -229,  -226,  -223,  -220,  -216,
     -213,  -209,  -206,  -202,  -198,  -194,  -190,  -185,
     -181,  -177,  -172,  -167,  -162,  -157,  -152,  -147,
     -142,  -137,  -132,  -126,  -121,  -115,  -109,  -104,
      -98,   -92,   -86,   -80,   -74,   -68,   -62,   -56,
      -50,   -44,   -38,   -31,   -25,   -19,   -13,    -6
};

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
int spinning;
int dragging;
int last_mx;
int last_my;
int running;

/* --- sign-safe fixed point ---------------------------------------------- */

/* (a * b) >> FP, correct for negative operands. */
static int smul(int a, int b) {
    int negative = 0;
    int result;
    if (a < 0) { a = -a; negative = 1; }
    if (b < 0) { b = -b; negative = !negative; }
    result = (a * b) >> FP;
    return negative ? -result : result;
}

/* a / b, correct for negative operands. */
static int sdiv(int a, int b) {
    int negative = 0;
    int result;
    if (b == 0) return 0;
    if (a < 0) { a = -a; negative = 1; }
    if (b < 0) { b = -b; negative = !negative; }
    result = a / b;
    return negative ? -result : result;
}

static int isin(int angle) { return SIN[angle & 255]; }
static int icos(int angle) { return SIN[(angle + 64) & 255]; }

/* --- the actual 3D --------------------------------------------------------
 *
 * Rotate about Y, then about X, then divide by depth for perspective.
 */
static void project(void) {
    int sy = isin(angle_y);
    int cy = icos(angle_y);
    int sx = isin(angle_x);
    int cx = icos(angle_x);
    int i;

    for (i = 0; i < 8; i++) {
        int x = VX[i];
        int y = VY[i];
        int z = VZ[i];

        int x1 = smul(x, cy) - smul(z, sy);      /* yaw   */
        int z1 = smul(x, sy) + smul(z, cy);

        int y2 = smul(y, cx) - smul(z1, sx);     /* pitch */
        int z2 = smul(y, sx) + smul(z1, cx);

        /* Perspective. The cube's half-diagonal is about 52, and DIST is
         * 150, so the denominator cannot reach zero -- no guard needed. */
        int depth = DIST + z2;
        PX[i] = CX + sdiv(x1 * DIST, depth * 2);
        PY[i] = CY + sdiv(y2 * DIST, depth * 2);
    }
}

static void draw_cube(void) {
    int i;
    disp_clear(BACK);

    /* a floor grid, so the rotation reads as rotation */
    for (i = 0; i < 5; i++) {
        disp_hline(10, 78 + i * 4, 80, i == 0 ? DIM : 0xFF202838);
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
    disp_text(2, 92, "drag to rotate", DIM);
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
            /* Screen is 100 px wide and a full turn is 256 steps, so
             * dragging across the window is a bit over one revolution. */
            angle_y = angle_y + (mx - last_mx) * 2;
            angle_x = angle_x + (my - last_my) * 2;
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
    spinning = 1;
    dragging = 0;
    running = 1;

    while (running) {
        handle_keys();
        handle_mouse();

        /* Auto-spin only when the mouse is not driving it, so a drag
         * feels like it is holding the cube rather than fighting it. */
        if (spinning && !dragging) {
            angle_y = angle_y + 1;
            if ((frame & 3) == 0) angle_x = angle_x + 1;
        }

        angle_x = angle_x & 255;
        angle_y = angle_y & 255;

        project();
        draw_cube();

        frame++;
        if (frame > 2000000) running = 0;
    }
    return 0;
}
