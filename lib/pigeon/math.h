/* <pigeon/math.h> -- fixed point, trig, roots and 3D vectors.
 *
 * There is no floating-point hardware, so this is all integers. Numbers
 * with fractions are Q8: one unit is 256, so 1.5 is 384. That format is
 * not a preference -- MUL truncates to 32 bits, so a Qn multiply needs
 * both raw operands under about 46,340. Q8 leaves +-181 units of range;
 * Q16 would leave +-0.7, which is useless.
 *
 * The reason this library exists is that three operations are WRONG on
 * negative numbers, and every one of them turns up in 3D maths:
 *
 *     -256 >> 8    gives 16777215     SHR is a LOGICAL shift
 *     -256 / 256   gives 16777215     DIV is unsigned
 *     -256 * 2     gives -512         MUL is fine
 *
 * ishr(), idiv() and imod() do the work where the hardware is correct and
 * put the sign back. Everything else is built on those, so a caller never
 * has to think about it again.
 *
 * Angles are 0..255 for a full turn, not degrees or radians, so wrapping
 * is one AND rather than the DIV+MUL+SUB that `% 360` compiles to.
 */
#ifndef PIGEON_MATH_H
#define PIGEON_MATH_H

/* --- fixed point: Q8 --------------------------------------------------- */

#define FX_BITS 8
#define FX_ONE  256
#define FX_HALF 128
#define FX(n)   ((n) << FX_BITS)        /* whole number -> fixed */

int fx_int(int f);                      /* fixed -> int, toward zero  */
int fx_round(int f);                    /* fixed -> int, to nearest   */
int fmul(int a, int b);                 /* (a * b) / 256              */
int fdiv(int a, int b);                 /* (a * 256) / b, 0 if b == 0 */

/* --- integers ---------------------------------------------------------- */

int iabs(int x);
int isign(int x);                       /* -1, 0 or 1                 */
int imin(int a, int b);
int imax(int a, int b);
int iclamp(int x, int lo, int hi);
int ishr(int x, int n);                 /* ARITHMETIC shift right     */
int idiv(int a, int b);                 /* signed divide, 0 if b == 0 */
int imod(int a, int b);                 /* signed remainder           */

/* --- trig: 256 steps per turn, results are Q8 in [-256, 256] ----------- */

#define ANGLE_STEPS   256
#define ANGLE_MASK    255
#define ANGLE_QUARTER 64
#define ANGLE_HALF    128

int isin(int angle);
int icos(int angle);
int iatan2(int y, int x);               /* -> 0..255, 0 is +x, 64 is +y */

/* --- roots ------------------------------------------------------------- */

int isqrt(int n);                       /* integer square root */
int fsqrt(int f);                       /* Q8 square root      */

/* --- random ------------------------------------------------------------ */

void     rand_seed(unsigned seed);      /* 0 is remapped; it would stick */
unsigned irand(void);
int      irand_range(int lo, int hi);   /* inclusive both ends */

/* --- 3D vectors --------------------------------------------------------
 *
 * Passed by pointer, never by value: this machine has six registers and
 * puts arguments in memory, so copying twelve bytes per call is pure
 * waste. Components are plain integers in world units; only the scale
 * factor of v3_scale is Q8.
 *
 * Every function here tolerates out == in, so chaining works:
 *
 *     v3_rotate_y(&v, &v, yaw);
 *     v3_rotate_x(&v, &v, pitch);
 */

typedef struct { int x; int y; int z; } vec3;

void v3_set(vec3 *v, int x, int y, int z);
void v3_add(vec3 *out, vec3 *a, vec3 *b);
void v3_sub(vec3 *out, vec3 *a, vec3 *b);
void v3_scale(vec3 *out, vec3 *v, int fx_scale);
int  v3_dot(vec3 *a, vec3 *b);
void v3_cross(vec3 *out, vec3 *a, vec3 *b);
int  v3_length(vec3 *v);
void v3_rotate_x(vec3 *out, vec3 *in, int angle);
void v3_rotate_y(vec3 *out, vec3 *in, int angle);
void v3_rotate_z(vec3 *out, vec3 *in, int angle);

/* Perspective-divide onto the screen. `dist` is the eye distance; larger
 * is flatter. Writes screen coordinates through sx and sy. */
void v3_project(vec3 *v, int dist, int cx, int cy, int *sx, int *sy);

#endif
