/* Fixed point, trig, roots and 3D vectors. See math.h.
 *
 * Two things shape this code.
 *
 * The only broken primitive is the SHIFT. MUL is correct for negative
 * operands already -- the machine multiplies the low 32 bits in two's
 * complement, which is what C wants -- and only `>>` and `/` go wrong.
 * So fmul() is one sign-safe shift, not the two sign-strips an earlier
 * version in user/cube.c used. Everything signed funnels through ishr(),
 * idiv() and imod(); nothing else in the file touches `>>` or `/` on a
 * value that might be negative.
 *
 * Every divide guards against zero. Not for tidiness: the emulator raises
 * a Python exception on DIV by zero, so an unguarded divide takes the
 * whole machine down instead of faulting the guest.
 */
#include <pigeon/math.h>

/* --- integers ---------------------------------------------------------- */

int iabs(int x) { return x < 0 ? -x : x; }

int isign(int x) {
    if (x > 0) return 1;
    if (x < 0) return -1;
    return 0;
}

int imin(int a, int b) { return a < b ? a : b; }
int imax(int a, int b) { return a > b ? a : b; }

int iclamp(int x, int lo, int hi) {
    if (x < lo) return lo;
    if (x > hi) return hi;
    return x;
}

/* Arithmetic shift right, built from the logical one the hardware has.
 * For a negative value, complement it, shift zeros in, and complement
 * back -- which is the same as shifting ones in. */
int ishr(int x, int n) {
    if (x >= 0) return x >> n;
    return ~((~x) >> n);
}

int idiv(int a, int b) {
    int negative = 0;
    int result;
    if (b == 0) return 0;
    if (a < 0) { a = -a; negative = 1; }
    if (b < 0) { b = -b; negative = !negative; }
    result = a / b;
    return negative ? -result : result;
}

/* Truncating remainder, so the sign follows the dividend as C requires:
 * -7 % 3 is -1, not 2. */
int imod(int a, int b) {
    if (b == 0) return 0;
    return a - idiv(a, b) * b;
}

/* --- fixed point ------------------------------------------------------- */

int fx_int(int f) { return ishr(f, FX_BITS); }

int fx_round(int f) {
    return f < 0 ? -ishr(FX_HALF - f, FX_BITS) : ishr(f + FX_HALF, FX_BITS);
}

/* (a * b) / 256. The product must stay under 2^31 or MUL truncates
 * silently: a world coordinate times a sine is 60*256, four orders of
 * magnitude of headroom, but two large fixed-point numbers will overflow. */
int fmul(int a, int b) { return ishr(a * b, FX_BITS); }

int fdiv(int a, int b) {
    if (b == 0) return 0;
    return idiv(a << FX_BITS, b);
}

/* --- trig ---------------------------------------------------------------
 *
 * 256 entries, one full turn, scaled by 256. Costs 1 KB of program image
 * and one masked array read per lookup -- cheaper than any approximation
 * that needs a divide.
 */

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

/* The mask handles negative angles too: AND is bitwise, so -1 becomes
 * 255, which is the step just before a full turn. */
int isin(int angle) { return SIN[angle & ANGLE_MASK]; }
int icos(int angle) { return SIN[(angle + ANGLE_QUARTER) & ANGLE_MASK]; }

/* Angle of the vector (x, y), 0..255, measured the same way as isin/icos:
 * 0 along +x, 64 along +y. Binary search over the quarter turn, so no
 * division and no table beyond the one above. */
int iatan2(int y, int x) {
    int lo = 0;
    int hi = ANGLE_QUARTER;
    int mid;
    int quadrant = 0;
    int i;

    if (x == 0 && y == 0) return 0;

    /* Fold into the first quadrant, remembering where it came from. */
    if (x < 0) { x = -x; quadrant = quadrant + 1; }
    if (y < 0) { y = -y; quadrant = quadrant + 2; }

    /* Find the angle a in [0, 64] whose slope best matches y/x, by
     * comparing y*cos(a) against x*sin(a) -- a cross product, so no
     * divide and no loss of precision. Six halvings narrow [0, 64] to a
     * width of one. */
    for (i = 0; i < 6; i++) {
        mid = (lo + hi) >> 1;
        if (y * icos(mid) > x * isin(mid)) lo = mid; else hi = mid;
    }

    /* lo and hi now straddle the answer; take whichever is closer.
     * Rounding down here instead put the +y axis at 63 rather than 64. */
    if (iabs(y * icos(hi) - x * isin(hi)) < iabs(y * icos(lo) - x * isin(lo)))
        mid = hi;
    else
        mid = lo;

    if (quadrant == 1) return ANGLE_HALF - mid;             /* -x, +y */
    if (quadrant == 2) return (ANGLE_STEPS - mid) & ANGLE_MASK;  /* +x, -y */
    if (quadrant == 3) return ANGLE_HALF + mid;             /* -x, -y */
    return mid;
}

/* --- roots --------------------------------------------------------------
 *
 * Bit-restoring square root: two bits of the operand per iteration, no
 * division anywhere.
 */

int isqrt(int n) {
    unsigned remainder = 0u;
    unsigned root = 0u;
    unsigned value;
    unsigned i;

    if (n <= 0) return 0;
    value = (unsigned)n;

    for (i = 0u; i < 16u; i++) {
        root = root << 1;
        remainder = (remainder << 2) | ((value >> 30) & 3u);
        value = value << 2;
        if (remainder > root) {
            remainder = remainder - root - 1u;
            root = root + 2u;
        }
    }
    return (int)(root >> 1);
}

/* sqrt of a Q8 value, in Q8: sqrt(f/256)*256 == sqrt(f*256). */
int fsqrt(int f) {
    if (f <= 0) return 0;
    return isqrt(f << FX_BITS);
}

/* --- random -------------------------------------------------------------
 *
 * xorshift32. Small, fast, and good enough to scatter things on screen;
 * not good enough for anything that matters.
 */

static unsigned rng_state = 2463534242u;

void rand_seed(unsigned seed) {
    /* Zero is a fixed point of xorshift -- it would return 0 forever. */
    rng_state = seed == 0u ? 2463534242u : seed;
}

unsigned irand(void) {
    unsigned x = rng_state;
    x = x ^ (x << 13);
    x = x ^ (x >> 17);
    x = x ^ (x << 5);
    rng_state = x;
    return x;
}

int irand_range(int lo, int hi) {
    unsigned span;
    if (hi <= lo) return lo;
    span = (unsigned)(hi - lo) + 1u;
    return lo + (int)(irand() % span);
}

/* --- 3D vectors ---------------------------------------------------------
 *
 * Each function reads every component it needs into locals BEFORE writing
 * any output, so out == in is safe. Writing out->x first would corrupt
 * the y and z that are still to be read, and the result would be a subtly
 * wrong shape rather than an obvious failure.
 */

void v3_set(vec3 *v, int x, int y, int z) {
    v->x = x;
    v->y = y;
    v->z = z;
}

void v3_add(vec3 *out, vec3 *a, vec3 *b) {
    int x = a->x + b->x;
    int y = a->y + b->y;
    int z = a->z + b->z;
    out->x = x; out->y = y; out->z = z;
}

void v3_sub(vec3 *out, vec3 *a, vec3 *b) {
    int x = a->x - b->x;
    int y = a->y - b->y;
    int z = a->z - b->z;
    out->x = x; out->y = y; out->z = z;
}

void v3_scale(vec3 *out, vec3 *v, int fx_scale) {
    int x = fmul(v->x, fx_scale);
    int y = fmul(v->y, fx_scale);
    int z = fmul(v->z, fx_scale);
    out->x = x; out->y = y; out->z = z;
}

int v3_dot(vec3 *a, vec3 *b) {
    return a->x * b->x + a->y * b->y + a->z * b->z;
}

void v3_cross(vec3 *out, vec3 *a, vec3 *b) {
    int x = a->y * b->z - a->z * b->y;
    int y = a->z * b->x - a->x * b->z;
    int z = a->x * b->y - a->y * b->x;
    out->x = x; out->y = y; out->z = z;
}

int v3_length(vec3 *v) { return isqrt(v3_dot(v, v)); }

void v3_rotate_x(vec3 *out, vec3 *in, int angle) {
    int s = isin(angle);
    int c = icos(angle);
    int y = in->y;
    int z = in->z;
    out->x = in->x;
    out->y = fmul(y, c) - fmul(z, s);
    out->z = fmul(y, s) + fmul(z, c);
}

void v3_rotate_y(vec3 *out, vec3 *in, int angle) {
    int s = isin(angle);
    int c = icos(angle);
    int x = in->x;
    int z = in->z;
    out->x = fmul(x, c) - fmul(z, s);
    out->y = in->y;
    out->z = fmul(x, s) + fmul(z, c);
}

void v3_rotate_z(vec3 *out, vec3 *in, int angle) {
    int s = isin(angle);
    int c = icos(angle);
    int x = in->x;
    int y = in->y;
    out->x = fmul(x, c) - fmul(y, s);
    out->y = fmul(x, s) + fmul(y, c);
    out->z = in->z;
}

void v3_project(vec3 *v, int dist, int cx, int cy, int *sx, int *sy) {
    /* Depth is dist + z. A caller whose model reaches back as far as the
     * eye would divide by zero, so clamp it to something small and
     * positive rather than crashing the emulator. */
    int depth = dist + v->z;
    if (depth < 1) depth = 1;
    *sx = cx + idiv(v->x * dist, depth * 2);
    *sy = cy + idiv(v->y * dist, depth * 2);
}
