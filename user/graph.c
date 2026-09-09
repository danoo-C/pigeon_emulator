/* A graphing calculator.
 *
 *   type            edit the expression; it recompiles on every keystroke
 *   left/right      move the caret      home/end   jump to either end
 *   drag            pan the graph       right-click reset the view
 *   up/down         zoom in/out, about the mouse pointer if it is on the graph
 *   pageup/pagedn   the same
 *   enter           fit the y range to the visible curve
 *   f1              reset the view
 *   escape          quit
 *
 * Point at the curve and the status line reads out x and y there.
 *
 *   Build:  python3 start_emulator.py graph --run
 *           ./run-pypy.sh graph --run        (much smoother; see the file)
 *
 * --- why this file carries its own arithmetic --------------------------
 *
 * The machine has no floating point, so <pigeon/math.h> exists -- but its
 * Q8 format is built for 3D world coordinates, and a grapher wants the
 * opposite trade. Q8 resolves 1/256, which is a quarter of a pixel at the
 * default zoom and visibly stair-steps two zoom steps in; and because its
 * fmul() is a bare 32-bit multiply, both operands have to stay under 181
 * or the product silently wraps -- x*x fails at x = 14.
 *
 * So the numbers here are Q16: one unit is 65536. That resolves 1/65536,
 * which stays under a pixel down to a span of 0.003, and qmul() below
 * does the multiply through 16-bit halves, so the 48-bit intermediate is
 * exact and only the RESULT has to fit. What it cannot represent, it
 * saturates to +-Q_MAX rather than wrapping, which is what lets a curve
 * shoot off the top of the screen instead of reappearing at the bottom.
 *
 * Everything signed still funnels through math.h's ishr()/idiv()/imod():
 * SHR is a logical shift on this machine and DIV is unsigned, so `>>` and
 * `/` on a value that might be negative are wrong here, not merely
 * imprecise.
 *
 * --- the shape of the program ------------------------------------------
 *
 * The expression is compiled once per edit into a flat RPN array, not
 * walked as a tree per sample: a redraw evaluates it 192 times, once per
 * column, and a tree walk would pay for the dispatch twice over.
 *
 * The 192 sampled y values are cached. Panning up or down and zooming
 * are then pure remapping -- the arithmetic only reruns when the
 * expression or the x range changes.
 */
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/math.h>
#include <pigeon/mem.h>

/* --- Q16 fixed point ---------------------------------------------------
 *
 * Q_MAX is not arbitrary. qmul() splits both operands into 16-bit halves
 * and sums four partial products in unsigned 32-bit arithmetic; with both
 * inputs at or below 2^27 the largest total that can reach the final
 * clamp is about 2.3e9, which fits under 2^32. Let a value past 2^27 into
 * the multiplier and that sum wraps instead. So every operation below
 * clamps its result into +-Q_MAX, and that invariant is what keeps the
 * multiply honest.
 */
#define Q_BITS    16
#define Q_ONE     65536
#define Q_MAX     0x07FFFFFF        /* 2047.99998 -- see above */

#define Q_PI      205887
#define Q_TWOPI   411775
#define Q_HALFPI  102944
#define Q_E       178145
#define Q_LN2     45426             /* ln 2      */
#define Q_INVLN2  94548             /* 1 / ln 2  */
#define Q_LOG10_2 19728             /* log10 2   */
#define Q_RAD2IDX 10680707          /* 1024 / 2pi -- radians to table index */

/* Set by any operation that is undefined at this sample: a divide by
 * zero, a root or a logarithm out of domain. Saturation does NOT set it --
 * a value too big to represent is still a point on the curve, just one
 * far off the top of the screen. */
static int ev_bad;

static int qclamp(int v) {
    if (v >  Q_MAX) return  Q_MAX;
    if (v < -Q_MAX) return -Q_MAX;
    return v;
}

/* (a * b) >> 16, with the 48-bit intermediate carried exactly.
 *
 * a*b as one 32-bit multiply would overflow for anything past 181.0, so
 * the operands are split at 16 bits and the four partial products are
 * summed at the scale the result wants:
 *
 *     a*b >> 16  =  (ah*bh << 16) + ah*bl + al*bh + (al*bl >> 16)
 *
 * ah*bh is the only term that can overflow on its own -- it lands 16 bits
 * up -- so it is checked before the sum, and the sum itself is bounded by
 * the Q_MAX invariant described above.
 */
static int qmul(int a, int b) {
    int neg = 0;
    unsigned ua, ub, ah, al, bh, bl, hi, r;

    if (a < 0) { a = -a; neg = 1; }
    if (b < 0) { b = -b; neg = !neg; }

    ua = (unsigned)a;
    ub = (unsigned)b;
    ah = ua >> 16;  al = ua & 0xFFFFu;
    bh = ub >> 16;  bl = ub & 0xFFFFu;

    hi = ah * bh;
    if (hi > 0x7FFFu) return neg ? -Q_MAX : Q_MAX;

    r = (hi << 16) + ah * bl + al * bh + ((al * bl + 0x8000u) >> 16);
    if (r > (unsigned)Q_MAX) return neg ? -Q_MAX : Q_MAX;
    return neg ? -(int)r : (int)r;
}

/* (a << 16) / b. The numerator is 48 bits, so this is done as a whole
 * part and a fractional part: one hardware divide each, rather than a
 * 32-iteration long division.
 *
 * The fractional divide needs (r << 16) to fit, so when the divisor is
 * over 15 bits both it and the remainder are normalised down together.
 * That throws away bits below 2^-15 OF THE DIVISOR, which costs about
 * 4 units of Q16 in the result -- a twentieth of a pixel at the tightest
 * zoom this program allows.
 */
static int qdiv(int a, int b) {
    int neg = 0;
    unsigned ua, ub, q, r, frac;

    if (b == 0) { ev_bad = 1; return 0; }
    if (a < 0) { a = -a; neg = 1; }
    if (b < 0) { b = -b; neg = !neg; }

    ua = (unsigned)a;
    ub = (unsigned)b;
    q = ua / ub;
    r = ua - q * ub;
    if (q > (unsigned)(Q_MAX >> 16)) return neg ? -Q_MAX : Q_MAX;

    while (ub > 0x7FFFu) { ub = ub >> 1; r = r >> 1; }
    frac = ((r << 16) + (ub >> 1)) / ub;
    if (frac > 0xFFFFu) frac = 0xFFFFu;

    q = (q << 16) + frac;
    if (q > (unsigned)Q_MAX) return neg ? -Q_MAX : Q_MAX;
    return neg ? -(int)q : (int)q;
}

/* --- tables ------------------------------------------------------------
 *
 * Four tables, all Q16, all read the same way: index the whole part,
 * interpolate linearly toward the next entry with the fraction. Linear
 * interpolation between N samples of a smooth function errs by about
 * (span/N)^2 / 8 times the second derivative, which is why the sine
 * table is the big one -- 256 points across a QUARTER turn puts a full
 * turn at 1024 steps and the error at well under one Q16 unit, so the
 * curve is exact to the format rather than to the table.
 *
 * The quarter turn is all that is stored; the other three are the same
 * numbers read backwards or negated. 257 entries, not 256, so the last
 * interpolation has a right-hand neighbour to reach for.
 */
static int SINQ[257] = {
         0,    402,    804,   1206,   1608,   2010,   2412,   2814,
      3216,   3617,   4019,   4420,   4821,   5222,   5623,   6023,
      6424,   6824,   7224,   7623,   8022,   8421,   8820,   9218,
      9616,  10014,  10411,  10808,  11204,  11600,  11996,  12391,
     12785,  13180,  13573,  13966,  14359,  14751,  15143,  15534,
     15924,  16314,  16703,  17091,  17479,  17867,  18253,  18639,
     19024,  19409,  19792,  20175,  20557,  20939,  21320,  21699,
     22078,  22457,  22834,  23210,  23586,  23961,  24335,  24708,
     25080,  25451,  25821,  26190,  26558,  26925,  27291,  27656,
     28020,  28383,  28745,  29106,  29466,  29824,  30182,  30538,
     30893,  31248,  31600,  31952,  32303,  32652,  33000,  33347,
     33692,  34037,  34380,  34721,  35062,  35401,  35738,  36075,
     36410,  36744,  37076,  37407,  37736,  38064,  38391,  38716,
     39040,  39362,  39683,  40002,  40320,  40636,  40951,  41264,
     41576,  41886,  42194,  42501,  42806,  43110,  43412,  43713,
     44011,  44308,  44604,  44898,  45190,  45480,  45769,  46056,
     46341,  46624,  46906,  47186,  47464,  47741,  48015,  48288,
     48559,  48828,  49095,  49361,  49624,  49886,  50146,  50404,
     50660,  50914,  51166,  51417,  51665,  51911,  52156,  52398,
     52639,  52878,  53114,  53349,  53581,  53812,  54040,  54267,
     54491,  54714,  54934,  55152,  55368,  55582,  55794,  56004,
     56212,  56418,  56621,  56823,  57022,  57219,  57414,  57607,
     57798,  57986,  58172,  58356,  58538,  58718,  58896,  59071,
     59244,  59415,  59583,  59750,  59914,  60075,  60235,  60392,
     60547,  60700,  60851,  60999,  61145,  61288,  61429,  61568,
     61705,  61839,  61971,  62101,  62228,  62353,  62476,  62596,
     62714,  62830,  62943,  63054,  63162,  63268,  63372,  63473,
     63572,  63668,  63763,  63854,  63944,  64031,  64115,  64197,
     64277,  64354,  64429,  64501,  64571,  64639,  64704,  64766,
     64827,  64884,  64940,  64993,  65043,  65091,  65137,  65180,
     65220,  65259,  65294,  65328,  65358,  65387,  65413,  65436,
     65457,  65476,  65492,  65505,  65516,  65525,  65531,  65535,
     65536
};

/* atan over [0, 1]; arguments past 1 fold in through atan(1/v). */
static int ATANT[65] = {
         0,   1024,   2047,   3070,   4091,   5110,   6126,   7140,
      8150,   9156,  10158,  11155,  12147,  13133,  14114,  15088,
     16055,  17015,  17968,  18913,  19850,  20779,  21699,  22610,
     23512,  24406,  25289,  26163,  27028,  27882,  28727,  29561,
     30386,  31200,  32003,  32797,  33580,  34353,  35115,  35867,
     36608,  37340,  38060,  38771,  39472,  40162,  40842,  41512,
     42172,  42823,  43464,  44095,  44716,  45328,  45931,  46525,
     47109,  47685,  48251,  48809,  49359,  49899,  50432,  50956,
     51472
};

/* log2 over the mantissa [1, 2); the exponent comes from the bit
 * position, so these two tables cover the whole positive range. */
static int LOG2T[65] = {
         0,   1466,   2909,   4331,   5732,   7112,   8473,   9814,
     11136,  12440,  13727,  14996,  16248,  17484,  18704,  19909,
     21098,  22272,  23433,  24579,  25711,  26830,  27936,  29029,
     30109,  31178,  32234,  33279,  34312,  35334,  36346,  37346,
     38336,  39316,  40286,  41246,  42196,  43137,  44068,  44990,
     45904,  46809,  47705,  48593,  49472,  50344,  51207,  52063,
     52911,  53751,  54584,  55410,  56229,  57040,  57845,  58643,
     59434,  60219,  60997,  61769,  62534,  63294,  64047,  64794,
     65536
};

/* 2^f over [0, 1); the whole part of the exponent becomes a shift. */
static int EXP2T[65] = {
     65536,  66250,  66971,  67700,  68438,  69183,  69936,  70698,
     71468,  72246,  73032,  73828,  74632,  75444,  76266,  77096,
     77936,  78785,  79642,  80510,  81386,  82273,  83169,  84074,
     84990,  85915,  86851,  87796,  88752,  89719,  90696,  91684,
     92682,  93691,  94711,  95743,  96785,  97839,  98905,  99982,
    101070, 102171, 103283, 104408, 105545, 106694, 107856, 109031,
    110218, 111418, 112631, 113858, 115098, 116351, 117618, 118899,
    120194, 121502, 122825, 124163, 125515, 126882, 128263, 129660,
    131072
};

/* --- transcendentals ---------------------------------------------------
 *
 * Angles are radians, as a calculator's are -- not the 0..255 turns that
 * <pigeon/math.h> uses. The conversion is folded into the table index, so
 * sin() costs one range reduction, one multiply and one interpolation.
 */

/* Reduce first, THEN scale. Scaling a large angle straight to a table
 * index saturates -- sin(500) would want an index of 81,000 -- and the
 * remainder is what the table wants anyway. imod() is exact on the raw
 * integers, so the only error is that Q_TWOPI is 2pi rounded to Q16:
 * 2e-6 radians of drift per turn, or a thousandth of a pixel after a
 * hundred turns. */
static int qsin(int rad) {
    int t, i, f, quad, pos, a, b;

    rad = imod(rad, Q_TWOPI);
    if (rad < 0) rad = rad + Q_TWOPI;

    t = qmul(rad, Q_RAD2IDX);           /* Q16 index into a 1024-step turn */
    i = t >> 16;
    f = t & 0xFFFF;
    if (i >= 1024) { i = 1023; f = 0xFFFF; }

    quad = i >> 8;
    pos  = i & 255;
    if (quad == 0)      { a =  SINQ[pos];       b =  SINQ[pos + 1]; }
    else if (quad == 1) { a =  SINQ[256 - pos]; b =  SINQ[255 - pos]; }
    else if (quad == 2) { a = -SINQ[pos];       b = -SINQ[pos + 1]; }
    else                { a = -SINQ[256 - pos]; b = -SINQ[255 - pos]; }

    return a + ishr((b - a) * f, Q_BITS);
}

static int qcos(int rad) { return qsin(rad + Q_HALFPI); }

/* No pole test: at the asymptote the cosine is merely tiny, not zero, so
 * the divide saturates and the curve leaves the top of the screen -- which
 * is what it should do. draw_curve() is what refuses to join the two ends
 * across the gap. */
static int qtan(int rad) { return qdiv(qsin(rad), qcos(rad)); }

/* Newton, seeded from the integer square root.
 *
 * sqrt of a Q16 value is isqrt(v) * 256 exactly, but isqrt() truncates,
 * so that seed is off by up to 256 Q16 units. Newton doubles the correct
 * bits each pass, so three passes take it below one unit from anywhere in
 * range. Cheaper than a 32-iteration bit-restoring root at Q16 width. */
static int qsqrt(int v) {
    int s;
    if (v < 0) { ev_bad = 1; return 0; }
    if (v == 0) return 0;
    s = isqrt(v) << 8;
    if (s <= 0) s = 1;
    s = (s + qdiv(v, s)) >> 1;
    s = (s + qdiv(v, s)) >> 1;
    s = (s + qdiv(v, s)) >> 1;
    return s;
}

/* Split the value into a power of two and a mantissa in [1, 2): the
 * exponent is the bit position, and the table covers the mantissa. */
static int qlog2(int v) {
    unsigned u;
    int p, m, i, f;

    if (v <= 0) { ev_bad = 1; return 0; }
    u = (unsigned)v;
    p = 0;
    while (u >= 0x20000u) { u = u >> 1; p++; }
    while (u <  0x10000u) { u = u << 1; p--; }

    m = (int)u - Q_ONE;                 /* 0 .. 65535 */
    i = m >> 10;                        /* 0 .. 63    */
    f = (m << 6) & 0xFFFF;
    return p * Q_ONE + LOG2T[i] + ishr((LOG2T[i + 1] - LOG2T[i]) * f, Q_BITS);
}

static int qln(int v)    { return qmul(qlog2(v), Q_LN2); }
static int qlog10(int v) { return qmul(qlog2(v), Q_LOG10_2); }

/* e^v as 2^(v/ln2): the whole part of the exponent is a shift and the
 * fraction is a table lookup. The clamps are the format's, not the
 * function's -- 2^11 is already past Q_MAX and 2^-20 is already zero. */
static int qexp(int v) {
    int t, n, f, i, ff, r;

    t = qmul(v, Q_INVLN2);
    n = ishr(t, Q_BITS);
    f = t - n * Q_ONE;                  /* 0 .. 65535, ishr floors */

    if (n >  11) return Q_MAX;
    if (n < -20) return 0;

    i  = f >> 10;
    ff = (f << 6) & 0xFFFF;
    r  = EXP2T[i] + ishr((EXP2T[i + 1] - EXP2T[i]) * ff, Q_BITS);

    if (n >= 0) r = r << n;
    else        r = r >> (-n);          /* r is positive here */
    return r > Q_MAX ? Q_MAX : r;
}

static int qatan(int v) {
    int neg = 0, inv = 0, i, f, r;

    if (v < 0) { v = -v; neg = 1; }
    if (v > Q_ONE) { v = qdiv(Q_ONE, v); inv = 1; }

    i = v >> 10;
    if (i >= 64) {
        r = ATANT[64];
    } else {
        f = (v << 6) & 0xFFFF;
        r = ATANT[i] + ishr((ATANT[i + 1] - ATANT[i]) * f, Q_BITS);
    }
    if (inv) r = Q_HALFPI - r;
    return neg ? -r : r;
}

/* asin through atan, so there is one arc table rather than three. The
 * identity blows up at +-1, which is exactly where the answer is +-pi/2,
 * so that case is taken directly. */
static int qasin(int v) {
    int d;
    if (v > Q_ONE || v < -Q_ONE) { ev_bad = 1; return 0; }
    if (v ==  Q_ONE) return  Q_HALFPI;
    if (v == -Q_ONE) return -Q_HALFPI;
    d = qsqrt(Q_ONE - qmul(v, v));
    if (d == 0) return v > 0 ? Q_HALFPI : -Q_HALFPI;
    return qatan(qdiv(v, d));
}

static int qacos(int v) { return Q_HALFPI - qasin(v); }

/* An integer exponent goes through binary exponentiation, which is what
 * makes (-2)^3 and x^2 work at all: the exp/ln route below is undefined
 * for a negative base, and squaring is the single most common thing
 * anyone types at a grapher. */
static int qpow(int a, int b) {
    int n, neg, r, base;

    if ((b & 0xFFFF) == 0) {
        n = ishr(b, Q_BITS);
        if (n >= -64 && n <= 64) {
            neg = 0;
            if (n < 0) { n = -n; neg = 1; }
            r = Q_ONE;
            base = a;
            while (n > 0) {
                if (n & 1) r = qmul(r, base);
                n = n >> 1;
                if (n > 0) base = qmul(base, base);
            }
            if (!neg) return r;
            if (r == 0) { ev_bad = 1; return 0; }
            return qdiv(Q_ONE, r);
        }
    }
    if (a <= 0) { ev_bad = 1; return 0; }
    return qexp(qmul(b, qln(a)));
}

static int qfloor(int v) { return ishr(v, Q_BITS) * Q_ONE; }
static int qceil(int v)  { return -qfloor(-v); }
static int qround(int v) { return qfloor(v + (Q_ONE / 2)); }

/* --- decimals ----------------------------------------------------------
 *
 * There is no printf, so axis labels and the readout are built a digit at
 * a time. `dec` is how many decimal places to consider; trailing zeros
 * are dropped, because a 4-pixel font on a 192-pixel screen cannot spare
 * the columns for "2.00".
 */
static int fmt_fx(char *out, int v, int dec) {
    unsigned ip, fp, scale;
    char digits[12];
    int n = 0, i, d;

    if (v < 0) { out[n] = '-'; n++; v = -v; }

    scale = 1u;
    for (i = 0; i < dec; i++) scale = scale * 10u;

    ip = ((unsigned)v) >> 16;
    fp = ((((unsigned)v) & 0xFFFFu) * scale + 0x8000u) >> 16;
    if (fp >= scale) { fp = fp - scale; ip = ip + 1u; }

    i = 0;
    if (ip == 0u) { digits[0] = '0'; i = 1; }
    while (ip > 0u) { digits[i] = (char)(48 + (int)(ip % 10u)); i++; ip = ip / 10u; }
    while (i > 0) { i--; out[n] = digits[i]; n++; }

    if (dec > 0 && fp > 0u) {
        for (i = dec - 1; i >= 0; i--) {
            digits[i] = (char)(48 + (int)(fp % 10u));
            fp = fp / 10u;
        }
        d = dec;
        while (d > 0 && digits[d - 1] == '0') d--;
        if (d > 0) {
            out[n] = '.'; n++;
            for (i = 0; i < d; i++) { out[n] = digits[i]; n++; }
        }
    }

    /* A tick just below zero rounds to "-0", which reads as a mistake. */
    if (n == 2 && out[0] == '-' && out[1] == '0') { out[0] = '0'; n = 1; }

    out[n] = 0;
    return n;
}

/* --- the expression, compiled ------------------------------------------
 *
 * Recursive descent straight into a flat RPN array. There is no tree: a
 * redraw evaluates this 192 times and the only thing a tree would buy is
 * the chance to walk it.
 *
 * Precedence, loosest first:  + -   * / %  and juxtaposition   unary -
 * then ^, which binds tightest and associates RIGHT, so 2^3^2 is 512 and
 * -x^2 is -(x^2). Juxtaposition is a multiply at the same level as `*`,
 * so 2x, 3sin(x) and (x+1)(x-1) all mean what they look like.
 */
#define OP_NUM    0
#define OP_X      1
/* binary, contiguous so the evaluator can range-test rather than chain */
#define OP_ADD    2
#define OP_SUB    3
#define OP_MUL    4
#define OP_DIV    5
#define OP_MOD    6
#define OP_POW    7
#define OP_MIN    8
#define OP_MAX    9
/* unary, everything from here up */
#define OP_NEG   10
#define OP_SIN   11
#define OP_COS   12
#define OP_TAN   13
#define OP_ASIN  14
#define OP_ACOS  15
#define OP_ATAN  16
#define OP_SQRT  17
#define OP_ABS   18
#define OP_LN    19
#define OP_LOG   20
#define OP_EXP   21
#define OP_FLOOR 22
#define OP_CEIL  23
#define OP_ROUND 24
#define OP_SIGN  25

#define FIRST_BINARY OP_ADD
#define LAST_BINARY  OP_MAX

#define FN_COUNT 17
static char *FN_NAME[FN_COUNT] = {
    "sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "abs", "ln",
    "log", "exp", "floor", "ceil", "round", "sign", "min", "max"
};
static int FN_OP[FN_COUNT] = {
    OP_SIN, OP_COS, OP_TAN, OP_ASIN, OP_ACOS, OP_ATAN, OP_SQRT, OP_ABS, OP_LN,
    OP_LOG, OP_EXP, OP_FLOOR, OP_CEIL, OP_ROUND, OP_SIGN, OP_MIN, OP_MAX
};
static int FN_ARGC[FN_COUNT] = {
    1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 2, 2
};

#define CODE_MAX  160
#define NAME_MAX  8

static int code[CODE_MAX];
static int lit[CODE_MAX];
static int code_len;

static char *p_src;
static int   p_pos;
static char *p_err;                 /* NULL while the parse is going well */
static int   p_errpos;

/* Skipping whitespace here rather than in a token loop means p_pos always
 * points at the offending character when something fails, which is what
 * the caret under the error message needs. */
static int p_peek(void) {
    while (p_src[p_pos] == 32 || p_src[p_pos] == 9) p_pos++;
    return (int)p_src[p_pos];
}

static void p_fail_at(char *msg, int at) {
    if (p_err == NULL) { p_err = msg; p_errpos = at; }
}

static void p_fail(char *msg) { p_fail_at(msg, p_pos); }

static void emit(int op, int value) {
    if (code_len >= CODE_MAX) { p_fail("expression too long"); return; }
    code[code_len] = op;
    lit[code_len] = value;
    code_len++;
}

static int is_digit(int c) { return c >= 48 && c <= 57; }
static int is_alpha(int c) {
    return (c >= 97 && c <= 122) || (c >= 65 && c <= 90);
}

/* Four decimal places are kept and the rest dropped. Not laziness: the
 * fraction is turned into Q16 by (num << 16) / den, and a fifth digit
 * puts that numerator past 2^32. Q16 resolves 0.0000153, so the digits
 * being dropped are ones nobody types. */
static int p_number(void) {
    unsigned ip = 0u, num = 0u, den = 1u;
    int c = (int)p_src[p_pos];

    while (is_digit(c)) {
        if (ip < 40000u) ip = ip * 10u + (unsigned)(c - 48);
        p_pos++;
        c = (int)p_src[p_pos];
    }
    if (c == 46) {                                  /* '.' */
        p_pos++;
        c = (int)p_src[p_pos];
        while (is_digit(c)) {
            if (den <= 1000u) { num = num * 10u + (unsigned)(c - 48); den = den * 10u; }
            p_pos++;
            c = (int)p_src[p_pos];
        }
    }
    if (ip > 2047u) ip = 2047u;                     /* Q_MAX, in whole units */
    return (int)(ip << 16) + (int)((num * 65536u + den / 2u) / den);
}

/* Letters only, folded to lower case. Digits are deliberately NOT part of
 * a name, so x2 parses as x*2 the way it does on a calculator. */
static int p_name(char *buf) {
    int n = 0;
    int c = (int)p_src[p_pos];
    while (is_alpha(c)) {
        if (n < NAME_MAX) {
            if (c >= 65 && c <= 90) c = c + 32;
            buf[n] = (char)c;
            n++;
        }
        p_pos++;
        c = (int)p_src[p_pos];
    }
    buf[n] = 0;
    return n;
}

static int name_eq(char *a, char *b) {
    while (*a != 0 && *b != 0) {
        if (*a != *b) return 0;
        a++; b++;
    }
    return *a == 0 && *b == 0;
}

static void p_expr(void);
static void p_unary(void);

static void p_atom(void) {
    char name[NAME_MAX + 1];
    int c = p_peek();
    int i, found;

    if (c == 40) {                                  /* '(' */
        p_pos++;
        p_expr();
        if (p_err != NULL) return;
        if (p_peek() != 41) { p_fail("missing )"); return; }
        p_pos++;
        return;
    }
    if (is_digit(c) || c == 46) { emit(OP_NUM, p_number()); return; }
    if (is_alpha(c)) {
        int at = p_pos;                             /* the name starts here */
        p_name(name);
        if (name_eq(name, "x"))  { emit(OP_X, 0); return; }
        if (name_eq(name, "pi")) { emit(OP_NUM, Q_PI); return; }
        if (name_eq(name, "e"))  { emit(OP_NUM, Q_E); return; }

        found = -1;
        for (i = 0; i < FN_COUNT; i++) if (name_eq(name, FN_NAME[i])) found = i;
        if (found < 0) { p_fail_at("unknown name", at); return; }

        if (p_peek() != 40) { p_fail_at("missing ( after name", at); return; }
        p_pos++;
        p_expr();
        if (p_err != NULL) return;
        if (FN_ARGC[found] == 2) {
            if (p_peek() != 44) { p_fail("missing , -- takes two"); return; }
            p_pos++;
            p_expr();
            if (p_err != NULL) return;
        }
        if (p_peek() != 41) { p_fail("missing )"); return; }
        p_pos++;
        emit(FN_OP[found], 0);
        return;
    }
    if (c == 0) p_fail("expression ends early");
    else        p_fail("cannot read that");
}

static void p_power(void) {
    p_atom();
    if (p_err != NULL) return;
    if (p_peek() == 94) {                           /* '^' */
        p_pos++;
        p_unary();                                  /* right-associative */
        emit(OP_POW, 0);
    }
}

static void p_unary(void) {
    int c = p_peek();
    if (c == 45) { p_pos++; p_unary(); emit(OP_NEG, 0); return; }
    if (c == 43) { p_pos++; p_unary(); return; }
    p_power();
}

static int starts_atom(int c) {
    return is_digit(c) || is_alpha(c) || c == 46 || c == 40;
}

static void p_term(void) {
    int c;
    p_unary();
    while (p_err == NULL) {
        c = p_peek();
        if      (c == 42) { p_pos++; p_unary(); emit(OP_MUL, 0); }
        else if (c == 47) { p_pos++; p_unary(); emit(OP_DIV, 0); }
        else if (c == 37) { p_pos++; p_unary(); emit(OP_MOD, 0); }
        else if (starts_atom(c)) { p_unary(); emit(OP_MUL, 0); }
        else return;
    }
}

static void p_expr(void) {
    int c;
    p_term();
    while (p_err == NULL) {
        c = p_peek();
        if      (c == 43) { p_pos++; p_term(); emit(OP_ADD, 0); }
        else if (c == 45) { p_pos++; p_term(); emit(OP_SUB, 0); }
        else return;
    }
}

static int compile_expr(char *text) {
    p_src = text;
    p_pos = 0;
    p_err = NULL;
    p_errpos = 0;
    code_len = 0;

    if (p_peek() == 0) { p_err = "type an expression in x"; p_errpos = 0; return 0; }
    p_expr();
    if (p_err == NULL && p_peek() != 0) p_fail("leftover text");
    return p_err == NULL;
}

/* --- evaluation --------------------------------------------------------
 *
 * A stack machine over the RPN array. There are no bounds checks in the
 * loop: compile_expr() walks the finished code once and proves the depth
 * stays within EVAL_STACK and lands on exactly one value, so paying for
 * the check here -- 192 times a redraw, once per opcode -- would buy
 * nothing.
 */
#define EVAL_STACK 32
static int st[EVAL_STACK];

static int eval(int x) {
    int sp = 0;
    int i, op, a, b;

    ev_bad = 0;
    for (i = 0; i < code_len; i++) {
        op = code[i];

        if (op == OP_NUM) { st[sp] = lit[i]; sp++; continue; }
        if (op == OP_X)   { st[sp] = x;      sp++; continue; }

        if (op <= LAST_BINARY) {
            sp--;
            b = st[sp];
            a = st[sp - 1];
            if      (op == OP_ADD) a = qclamp(a + b);
            else if (op == OP_SUB) a = qclamp(a - b);
            else if (op == OP_MUL) a = qmul(a, b);
            else if (op == OP_DIV) a = qdiv(a, b);
            else if (op == OP_MOD) { if (b == 0) { ev_bad = 1; a = 0; } else a = imod(a, b); }
            else if (op == OP_POW) a = qpow(a, b);
            else if (op == OP_MIN) a = a < b ? a : b;
            else                   a = a > b ? a : b;
            st[sp - 1] = a;
            continue;
        }

        a = st[sp - 1];
        if      (op == OP_NEG)   a = -a;
        else if (op == OP_SIN)   a = qsin(a);
        else if (op == OP_COS)   a = qcos(a);
        else if (op == OP_TAN)   a = qtan(a);
        else if (op == OP_ASIN)  a = qasin(a);
        else if (op == OP_ACOS)  a = qacos(a);
        else if (op == OP_ATAN)  a = qatan(a);
        else if (op == OP_SQRT)  a = qsqrt(a);
        else if (op == OP_ABS)   a = iabs(a);
        else if (op == OP_LN)    a = qln(a);
        else if (op == OP_LOG)   a = qlog10(a);
        else if (op == OP_EXP)   a = qexp(a);
        else if (op == OP_FLOOR) a = qfloor(a);
        else if (op == OP_CEIL)  a = qceil(a);
        else if (op == OP_ROUND) a = qround(a);
        else                     a = isign(a) * Q_ONE;
        st[sp - 1] = a;
    }
    return st[0];
}

/* --- layout ------------------------------------------------------------ */

#define BOX_H     9                             /* the expression bar   */
#define BOX_TX    13                            /* text starts here     */
#define BOX_COLS  ((DISP_W - BOX_TX - 2) / (GLYPH_W + 1))
#define PLOT_Y    10
#define PLOT_H    (DISP_H - PLOT_Y - 9)
#define PLOT_W    DISP_W
#define PLOT_BOT  (PLOT_Y + PLOT_H - 1)
#define PLOT_CX   (PLOT_W / 2)
#define PLOT_CY   (PLOT_Y + PLOT_H / 2)
#define STATUS_Y  (DISP_H - 7)

#define BG      0xFF0B0E14
#define PANEL   0xFF161B26
#define GRID    0xFF232B3A
#define AXIS    0xFF4C596E
#define CURVE   0xFF3FD0FF
#define TRACE   0xFFFFC94D
#define TEXTC   0xFFD8E0EA
#define DIM     0xFF6E7A8C
#define ERRC    0xFFFF6B6B

/* Zoom stops. The floor is where a screen column stops being worth a
 * whole Q16 unit -- past it, neighbouring columns sample the same x and
 * the curve goes to stairs. The ceiling keeps x_at()'s and row_at()'s
 * intermediate products inside 32 bits. */
#define SPAN_MIN  96
#define SPAN_MAX  (1 << 25)

/* --- the view ---------------------------------------------------------- */

static int view_cx, view_cy;        /* centre, Q16 */
static int view_sx, view_sy;        /* half-span, Q16 */

/* Per-pixel steps, split into a whole part and a remainder so that
 * x_at(col) is exact rather than accumulating the truncation of a
 * per-column step 192 times over. */
static int map_xstep, map_xrem, map_xmin;
static int map_ystep, map_yrem;

static int ys[PLOT_W];              /* the sampled curve, Q16 */
static char yok[PLOT_W];            /* 0 where the sample is undefined */
static int cache_valid;
static int dirty;

static void map_update(void) {
    int span;
    span = view_sx + view_sx;
    map_xstep = span / PLOT_W;
    map_xrem  = span - map_xstep * PLOT_W;
    map_xmin  = view_cx - view_sx;

    span = view_sy + view_sy;
    map_ystep = span / PLOT_H;
    map_yrem  = span - map_ystep * PLOT_H;
}

static int x_at(int col) {
    return map_xmin + col * map_xstep + (col * map_xrem) / PLOT_W;
}

static int y_at(int row) {
    int d = PLOT_CY - row;                      /* pixels above the centre */
    if (d >= 0) return view_cy + d * map_ystep + (d * map_yrem) / PLOT_H;
    d = -d;
    return view_cy - d * map_ystep - (d * map_yrem) / PLOT_H;
}

/* The clamp is not just tidiness: it bounds d so d*PLOT_H stays inside 32
 * bits, and it is what leaves a blown-up sample two screens off the edge
 * rather than at some arbitrary wrapped row -- which is the signal
 * draw_curve() reads to tell an asymptote from a steep slope. */
static int row_at(int y) {
    int d = y - view_cy;
    int limit = view_sy << 2;
    if (d >  limit) d =  limit;
    if (d < -limit) d = -limit;
    if (view_sy <= (1 << 22)) return PLOT_CY - idiv(d * PLOT_H, view_sy + view_sy);
    return PLOT_CY - idiv(d, map_ystep > 0 ? map_ystep : 1);
}

static int col_at(int x) {
    int d = x - view_cx;
    int limit = view_sx + view_sx;
    if (d >  limit) d =  limit;
    if (d < -limit) d = -limit;
    if (view_sx <= (1 << 22)) return PLOT_CX + idiv(d * PLOT_W, view_sx + view_sx);
    return PLOT_CX + idiv(d, map_xstep > 0 ? map_xstep : 1);
}

static void view_changed(int x_moved) {
    map_update();
    if (x_moved) cache_valid = 0;
    dirty = 1;
}

static void view_reset(void) {
    view_cx = 0;
    view_cy = 0;
    view_sx = 10 * Q_ONE;
    view_sy = (int)(((unsigned)view_sx * PLOT_H) / PLOT_W);   /* square pixels */
    view_changed(1);
}

static void resample(void) {
    int i, y;
    if (cache_valid) return;
    if (code_len == 0) {            /* nothing has ever compiled */
        for (i = 0; i < PLOT_W; i++) yok[i] = 0;
        cache_valid = 1;
        return;
    }
    for (i = 0; i < PLOT_W; i++) {
        y = eval(x_at(i));
        ys[i] = y;
        yok[i] = (char)(ev_bad ? 0 : 1);
    }
    cache_valid = 1;
}

static void zoom_by(int in) {
    int ax, ay, nsx, nsy, anchored;
    unsigned my = mouse_y();

    anchored = (my >= PLOT_Y && my <= PLOT_BOT);
    ax = anchored ? x_at((int)mouse_x()) : view_cx;
    ay = anchored ? y_at((int)my)        : view_cy;

    /* 5:4 a step -- small enough that holding the key feels continuous. */
    if (in) { nsx = idiv(view_sx * 4, 5); nsy = idiv(view_sy * 4, 5); }
    else    { nsx = idiv(view_sx * 5, 4); nsy = idiv(view_sy * 5, 4); }
    if (nsx < SPAN_MIN || nsy < SPAN_MIN) return;
    if (nsx > SPAN_MAX || nsy > SPAN_MAX) return;

    /* Keep whatever is under the pointer under the pointer. */
    if (in) {
        view_cx = ax - idiv((ax - view_cx) * 4, 5);
        view_cy = ay - idiv((ay - view_cy) * 4, 5);
    } else {
        view_cx = ax - idiv((ax - view_cx) * 5, 4);
        view_cy = ay - idiv((ay - view_cy) * 5, 4);
    }
    view_cx = qclamp(view_cx);
    view_cy = qclamp(view_cy);
    view_sx = nsx;
    view_sy = nsy;
    view_changed(1);
}

static void pan_by(int dx, int dy) {
    if (dx != 0) {
        view_cx = qclamp(view_cx - (dx * map_xstep + idiv(dx * map_xrem, PLOT_W)));
    }
    if (dy != 0) {
        view_cy = qclamp(view_cy + (dy * map_ystep + idiv(dy * map_yrem, PLOT_H)));
    }
    view_changed(dx != 0);
}

/* Frame the curve vertically, with a little air so it does not touch the
 * edges. A flat line has no extent to fit, hence the floor. */
static void fit_y(void) {
    int i, lo = 0, hi = 0, any = 0, half;

    resample();
    for (i = 0; i < PLOT_W; i++) {
        if (yok[i] == 0) continue;
        if (any == 0) { lo = ys[i]; hi = ys[i]; any = 1; continue; }
        if (ys[i] < lo) lo = ys[i];
        if (ys[i] > hi) hi = ys[i];
    }
    if (any == 0) return;

    half = ishr(hi - lo, 1);
    half = half + ishr(half, 3) + Q_ONE / 8;
    if (half < SPAN_MIN) half = SPAN_MIN;
    if (half > SPAN_MAX) half = SPAN_MAX;

    view_cy = qclamp(lo + ishr(hi - lo, 1));
    view_sy = half;
    view_changed(0);
}

/* --- grid ---------------------------------------------------------------
 *
 * Ticks land on 1, 2 or 5 times a power of ten, the way a ruler does, so
 * that a label always reads as a round number however far the view has
 * been zoomed. The loops walk the mantissa up or down that sequence until
 * between three and eight ticks fit across the span.
 */
static int nice_step(int span) {
    int m = 1;
    int dec = Q_ONE;
    int step = Q_ONE;
    int guard = 0;

    while (idiv(span, step) > 8 && guard < 60) {
        if      (m == 1) m = 2;
        else if (m == 2) m = 5;
        else { m = 1; dec = dec * 10; if (dec > Q_MAX / 5) dec = Q_MAX / 5; }
        step = dec * m;
        guard++;
    }
    while (idiv(span, step) < 3 && step > 8 && guard < 120) {
        if      (m == 5) m = 2;
        else if (m == 2) m = 1;
        else { m = 5; dec = idiv(dec, 10); if (dec < 1) dec = 1; }
        step = dec * m;
        guard++;
    }
    return step < 1 ? 1 : step;
}

/* Just enough places to tell one tick from the next. */
static int label_decimals(int step) {
    if (step >= Q_ONE) return 0;
    if (step >= 6554)  return 1;
    if (step >= 655)   return 2;
    return 3;
}

static int first_tick(int lo, int step) {
    int k = idiv(lo, step);
    if (k * step > lo) k--;             /* idiv truncates; ticks need floor */
    return k * step;
}

static int text_px(int chars) { return chars * (GLYPH_W + 1) - 1; }

static void draw_grid(void) {
    int xmin, xmax, ymin, ymax, xstep, ystep, ax_row, ax_col, t, c, r;

    xmin = view_cx - view_sx;  xmax = view_cx + view_sx;
    ymin = view_cy - view_sy;  ymax = view_cy + view_sy;
    xstep = nice_step(view_sx + view_sx);
    ystep = nice_step(view_sy + view_sy);
    ax_row = row_at(0);
    ax_col = col_at(0);

    for (t = first_tick(xmin, xstep); t <= xmax; t = t + xstep) {
        c = col_at(t);
        if (c >= 0 && c < PLOT_W) disp_vline((unsigned)c, PLOT_Y, PLOT_H, GRID);
    }
    for (t = first_tick(ymin, ystep); t <= ymax; t = t + ystep) {
        r = row_at(t);
        if (r >= PLOT_Y && r <= PLOT_BOT) disp_hline(0, (unsigned)r, PLOT_W, GRID);
    }

    if (ax_row >= PLOT_Y && ax_row <= PLOT_BOT) disp_hline(0, (unsigned)ax_row, PLOT_W, AXIS);
    if (ax_col >= 0 && ax_col < PLOT_W)         disp_vline((unsigned)ax_col, PLOT_Y, PLOT_H, AXIS);
}

/* A tick label with a one-pixel hole punched around it, so it stays
 * readable where the curve runs behind it. Drawing the glyphs four times
 * in the background colour and once in ink costs five 4x6 blits per
 * label -- nothing against the 192 evaluations a redraw already pays --
 * and unlike a filled box behind the text it takes out only the glyph
 * outline rather than a rectangle of the curve.
 *
 * Every caller keeps x >= 1 and PLOT_Y < y < PLOT_BOT - GLYPH_H so the
 * x-1 and y-1 passes cannot go negative: disp_char takes unsigned, and a
 * negative x would wrap to the far side of the address space and land
 * back at column 0 rather than being clipped away.
 */
static void label_text(int x, int y, char *s, color_t fg) {
    disp_text((unsigned)(x - 1), (unsigned)y,       s, BG);
    disp_text((unsigned)(x + 1), (unsigned)y,       s, BG);
    disp_text((unsigned)x,       (unsigned)(y - 1), s, BG);
    disp_text((unsigned)x,       (unsigned)(y + 1), s, BG);
    disp_text((unsigned)x,       (unsigned)y,       s, fg);
}

/* Drawn AFTER the curve, so a peak passing through a tick cannot eat half
 * a digit and leave a number that reads as a different one. The curve is
 * continuous enough to follow through a four-pixel gap; a mangled label
 * is not something the reader can repair.
 *
 * The tick geometry is recomputed rather than shared with draw_grid():
 * two nice_step() loops are nothing against the 192 evaluations of the
 * expression that a redraw already pays for, and it keeps each function
 * readable on its own. */
static void draw_labels(void) {
    char buf[16];
    int xmin, xmax, ymin, ymax, xstep, ystep, ax_row, ax_col;
    int t, c, r, n, lx, ly, dec;

    xmin = view_cx - view_sx;  xmax = view_cx + view_sx;
    ymin = view_cy - view_sy;  ymax = view_cy + view_sy;
    xstep = nice_step(view_sx + view_sx);
    ystep = nice_step(view_sy + view_sy);
    ax_row = row_at(0);
    ax_col = col_at(0);

    /* Labels hang off the axis where it is visible and off the edge where
     * it is not, so a panned-away view still says where it is. */
    ly = ax_row + 2;
    if (ly < PLOT_Y + 1) ly = PLOT_Y + 1;
    if (ly > PLOT_BOT - GLYPH_H - 1) ly = PLOT_BOT - GLYPH_H - 1;
    dec = label_decimals(xstep);
    for (t = first_tick(xmin, xstep); t <= xmax; t = t + xstep) {
        if (t == 0) continue;
        c = col_at(t);
        if (c < 0 || c >= PLOT_W) continue;
        n = fmt_fx(buf, t, dec);
        lx = c - text_px(n) / 2;
        if (lx < 1) lx = 1;
        if (lx + text_px(n) + 1 >= PLOT_W) lx = PLOT_W - text_px(n) - 2;
        label_text(lx, ly, buf, DIM);
    }

    lx = ax_col + 2;
    if (lx < 1) lx = 1;
    dec = label_decimals(ystep);
    for (t = first_tick(ymin, ystep); t <= ymax; t = t + ystep) {
        if (t == 0) continue;
        r = row_at(t) + 1;                          /* below its own line */
        if (r + GLYPH_H + 1 > PLOT_BOT) r = row_at(t) - GLYPH_H;   /* or above it */
        if (r < PLOT_Y + 1 || r + GLYPH_H + 1 > PLOT_BOT) continue;
        n = fmt_fx(buf, t, dec);
        c = lx;
        if (c + text_px(n) + 1 >= PLOT_W) c = PLOT_W - text_px(n) - 2;
        label_text(c, r, buf, DIM);
    }

    if (ax_row >= PLOT_Y && ax_row + 3 + GLYPH_H <= PLOT_BOT &&
        ax_col >= 0 && ax_col + 3 + text_px(1) < PLOT_W)
        label_text(ax_col + 2, ax_row + 2, "0", DIM);
}

/* --- the curve ----------------------------------------------------------
 *
 * One vertical run per column, from the previous sample's row to this
 * one's. Because x advances exactly one pixel per sample, that run IS the
 * segment between them, so clamping it to the plot rectangle clips the
 * curve exactly -- no line clipper, and no cost for a sample a thousand
 * screens off the top.
 *
 * The gap test is what stops tan(x) and 1/x from being drawn with a
 * vertical bar through the asymptote. Two consecutive samples that land
 * more than a screen off the plot on OPPOSITE sides did not travel
 * between those points; there is a pole in between and the pen lifts. A
 * genuinely steep curve moves far less than that in one column, so it is
 * never caught by this.
 */
static void draw_curve(void) {
    int px, row, prev_row = 0, prev_ok = 0, a, b, t;
    int far_up = PLOT_Y - PLOT_H;
    int far_down = PLOT_BOT + PLOT_H;

    for (px = 0; px < PLOT_W; px++) {
        if (yok[px] == 0) { prev_ok = 0; continue; }
        row = row_at(ys[px]);

        if (prev_ok &&
            !((prev_row < far_up && row > far_down) ||
              (prev_row > far_down && row < far_up))) {
            a = prev_row;
            b = row;
            if (a > b) { t = a; a = b; b = t; }
            if (a < PLOT_Y)   a = PLOT_Y;
            if (b > PLOT_BOT) b = PLOT_BOT;
            if (a <= b) disp_vline((unsigned)px, (unsigned)a, (unsigned)(b - a + 1), CURVE);
        } else if (row >= PLOT_Y && row <= PLOT_BOT) {
            disp_set((unsigned)px, (unsigned)row, CURVE);
        }

        prev_row = row;
        prev_ok = 1;
    }
}

/* --- the expression bar ------------------------------------------------- */

#define TEXT_MAX 63

static char text[TEXT_MAX + 1];
static int  text_len;
static int  caret;
static int  scroll;                 /* first character shown in the bar */
static int  trace_on;
static int  trace_col;

static void scroll_fix(void) {
    if (caret < scroll) scroll = caret;
    if (caret > scroll + BOX_COLS - 1) scroll = caret - BOX_COLS + 1;
    if (scroll < 0) scroll = 0;
}

static void draw_box(void) {
    char win[BOX_COLS + 1];
    int i, cx;
    color_t ink = (p_err == NULL) ? TEXTC : ERRC;

    disp_rect(0, 0, DISP_W, BOX_H, PANEL);
    disp_text(2, 1, "y=", DIM);

    for (i = 0; i < BOX_COLS && scroll + i < text_len; i++) win[i] = text[scroll + i];
    win[i] = 0;
    disp_text(BOX_TX, 1, win, ink);

    cx = BOX_TX + (caret - scroll) * (GLYPH_W + 1);
    if (cx >= BOX_TX && cx < DISP_W) disp_vline((unsigned)cx, 0, 8, TRACE);

    /* A rule under the character the parser stopped at -- more use than
     * a message that only says what went wrong and not where. */
    if (p_err != NULL && p_errpos >= scroll && p_errpos < scroll + BOX_COLS) {
        disp_hline((unsigned)(BOX_TX + (p_errpos - scroll) * (GLYPH_W + 1)),
                   BOX_H - 1, GLYPH_W, ERRC);
    }
    disp_hline(0, BOX_H, DISP_W, GRID);
}

/* --- the status line ---------------------------------------------------- */

static int str_put(char *dst, int at, char *src) {
    while (*src != 0) { dst[at] = *src; at++; src++; }
    dst[at] = 0;
    return at;
}

static void draw_status(void) {
    char line[48];
    int n = 0;

    disp_hline(0, DISP_H - 9, DISP_W, GRID);

    if (p_err != NULL) {
        disp_text(2, STATUS_Y, p_err, ERRC);
        return;
    }
    if (trace_on) {
        n = str_put(line, n, "x ");
        n = n + fmt_fx(line + n, x_at(trace_col), 3);
        n = str_put(line, n, "   y ");
        if (yok[trace_col]) n = n + fmt_fx(line + n, ys[trace_col], 3);
        else                n = str_put(line, n, "undefined");
        disp_text(2, STATUS_Y, line, TRACE);
        return;
    }
    n = str_put(line, n, "x ");
    n = n + fmt_fx(line + n, view_cx - view_sx, 2);
    n = str_put(line, n, "..");
    n = n + fmt_fx(line + n, view_cx + view_sx, 2);
    n = str_put(line, n, "  y ");
    n = n + fmt_fx(line + n, view_cy - view_sy, 2);
    n = str_put(line, n, "..");
    n = n + fmt_fx(line + n, view_cy + view_sy, 2);
    disp_text(2, STATUS_Y, line, DIM);
}

static void draw_trace(void) {
    int row, a, b;
    if (!trace_on || yok[trace_col] == 0) return;
    row = row_at(ys[trace_col]);
    if (row < PLOT_Y || row > PLOT_BOT) return;
    a = trace_col - 2;  b = trace_col + 2;
    if (a < 0) a = 0;
    if (b > PLOT_W - 1) b = PLOT_W - 1;
    disp_hline((unsigned)a, (unsigned)row, (unsigned)(b - a + 1), TRACE);
    a = row - 2;  b = row + 2;
    if (a < PLOT_Y)   a = PLOT_Y;
    if (b > PLOT_BOT) b = PLOT_BOT;
    disp_vline((unsigned)trace_col, (unsigned)a, (unsigned)(b - a + 1), TRACE);
}

static void render(void) {
    resample();
    disp_clear(BG);
    draw_grid();
    draw_curve();
    draw_labels();
    draw_trace();
    draw_box();
    draw_status();
    disp_present();
}

/* --- editing ------------------------------------------------------------
 *
 * The text recompiles on every keystroke, and a parse that fails leaves
 * the LAST good program in place: half-typed input is the normal state of
 * an expression bar, and blanking the graph on every intermediate
 * keystroke would make it unreadable. The bar turns red and marks the
 * offending character instead.
 */
static int good_code[CODE_MAX];
static int good_lit[CODE_MAX];
static int good_len;
static int running;

static void recompile(void) {
    int i, op, depth = 0, maxd = 0;

    if (compile_expr(text)) {
        /* Prove the stack discipline once here so eval() -- which runs
         * this code 192 times a redraw -- can skip the checks. */
        for (i = 0; i < code_len; i++) {
            op = code[i];
            if      (op <= OP_X)        depth++;
            else if (op <= LAST_BINARY) depth--;
            if (depth > maxd) maxd = depth;
        }
        if (maxd > EVAL_STACK || depth != 1) {
            p_err = "too deeply nested";
            p_errpos = 0;
        }
    }

    if (p_err == NULL) {
        for (i = 0; i < code_len; i++) { good_code[i] = code[i]; good_lit[i] = lit[i]; }
        good_len = code_len;
    } else {
        for (i = 0; i < good_len; i++) { code[i] = good_code[i]; lit[i] = good_lit[i]; }
        code_len = good_len;
    }
    cache_valid = 0;
    dirty = 1;
}

static void set_text(char *s) {
    int n = 0;
    while (s[n] != 0 && n < TEXT_MAX) { text[n] = s[n]; n++; }
    text[n] = 0;
    text_len = n;
    caret = n;
    scroll_fix();
}

static void insert_char(int c) {
    int i;
    if (text_len >= TEXT_MAX) return;
    for (i = text_len; i > caret; i--) text[i] = text[i - 1];
    text[caret] = (char)c;
    text_len++;
    caret++;
    text[text_len] = 0;
    scroll_fix();
    recompile();
}

static void delete_at(int at) {
    int i;
    if (at < 0 || at >= text_len) return;
    for (i = at; i < text_len - 1; i++) text[i] = text[i + 1];
    text_len--;
    text[text_len] = 0;
    scroll_fix();
    recompile();
}

/* --- input --------------------------------------------------------------
 *
 * Everything comes off the key EDGE stream, printable characters
 * included -- the HID controller queues each press into both the
 * character FIFO and the edge FIFO, so the edge FIFO alone carries the
 * whole keyboard IN ORDER. Splitting typing across the two buffers is
 * what makes "type, press left, type" apply the edits out of order; the
 * fix is not to split them.
 */
static void on_key(int c) {
    if (c == KEY_ESC)   { running = 0; return; }
    if (c == KEY_LEFT)  { if (caret > 0) caret--; scroll_fix(); dirty = 1; return; }
    if (c == KEY_RIGHT) { if (caret < text_len) caret++; scroll_fix(); dirty = 1; return; }
    if (c == KEY_HOME)  { caret = 0; scroll_fix(); dirty = 1; return; }
    if (c == KEY_END)   { caret = text_len; scroll_fix(); dirty = 1; return; }

    if (c == KEY_UP   || c == KEY_PGUP) { zoom_by(1); return; }
    if (c == KEY_DOWN || c == KEY_PGDN) { zoom_by(0); return; }
    if (c == KEY_F1)    { view_reset(); return; }
    if (c == KEY_ENTER) { fit_y(); return; }

    if (c == KEY_BACKSPACE) { if (caret > 0) { caret--; delete_at(caret); } return; }
    if (c == KEY_DELETE)    { delete_at(caret); return; }
    if (c >= 32 && c <= 126) insert_char(c);
}

static int drag_on, drag_x, drag_y;

static void handle_mouse(void) {
    unsigned ev = mouse_event();
    int mx, my, at;

    while (ev != 0) {
        mx = (int)mouse_x();
        my = (int)mouse_y();
        if (ME_PRESSED(ev)) {
            if (ME_BUTTON(ev) == 0) {
                if (my >= PLOT_Y && my <= PLOT_BOT) {
                    drag_on = 1;
                    drag_x = mx;
                    drag_y = my;
                } else if (my < BOX_H) {
                    at = mx - BOX_TX + 2;
                    if (at < 0) at = 0;
                    caret = scroll + at / (GLYPH_W + 1);
                    if (caret > text_len) caret = text_len;
                    scroll_fix();
                    dirty = 1;
                }
            } else if (ME_BUTTON(ev) == 1) {
                view_reset();
            }
        } else if (ME_BUTTON(ev) == 0) {
            drag_on = 0;
        }
        ev = mouse_event();
    }
}

/* Dragging reads the live button state, not the edge queue: a drag is a
 * question about what is true now, and the release that ends it may not
 * have been queued yet. */
static void update_drag(void) {
    int mx, my, dx, dy;
    if (drag_on == 0) return;
    if ((mouse_buttons() & MB_LEFT) == 0) { drag_on = 0; return; }
    mx = (int)mouse_x();
    my = (int)mouse_y();
    dx = mx - drag_x;
    dy = my - drag_y;
    if (dx == 0 && dy == 0) return;
    drag_x = mx;
    drag_y = my;
    pan_by(dx, dy);
}

static void update_trace(void) {
    int mx = (int)mouse_x();
    int my = (int)mouse_y();
    int on = (my >= PLOT_Y && my <= PLOT_BOT && mx >= 0 && mx < PLOT_W && drag_on == 0);

    if (on != trace_on || (on && mx != trace_col)) {
        trace_on = on;
        trace_col = mx;
        dirty = 1;
    }
}

int main(void) {
    unsigned ev;
    int i;

    disp_use_back_buffer();     /* whole frames only -- see <pigeon/display.h> */

    for (i = 0; i < PLOT_W; i++) { ys[i] = 0; yok[i] = 0; }
    good_len = 0;
    code_len = 0;
    scroll = 0;
    drag_on = 0;
    trace_on = 0;
    trace_col = 0;
    running = 1;

    set_text("sin(x*2)*2+(x^2*1.1-x^2)");
    recompile();
    view_reset();

    while (running) {
        ev = key_event();
        while (ev != 0) {
            if (KE_PRESSED(ev)) on_key((int)KE_CODE(ev));
            ev = key_event();
        }
        handle_mouse();
        update_drag();
        update_trace();

        /* A redraw is ~20,000 stores plus 192 evaluations of the
         * expression. The screen is static whenever nobody is touching
         * it, so it is only paid for when something actually moved. */
        if (dirty) {
            render();
            dirty = 0;
        }
    }

    disp_clear(BLACK);
    disp_present();
    return 0;
}
