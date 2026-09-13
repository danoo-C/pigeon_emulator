/* Strings, numbers as text, and character classes. See string.h.
 *
 * Bytes are compared and searched as UNSIGNED char. `char` is signed in
 * this compiler, so compared as char a byte like 0xE9 would sort below
 * 'a', and a strchr() for one read out of a char variable would never
 * match.
 *
 * The private helper is __str_digit, not a `static` with a plain name:
 * every unit is compiled into ONE translation unit with one global scope
 * (compiler/cc.py, compile_units), so `static` hides nothing, and a user
 * function called digit_value would be a "defined twice" error inside
 * this library.
 *
 * Range checks are a single unsigned compare: `c - '0' < 10u` is false
 * below '0' as well, because the subtraction wraps to a huge value.
 * display.c clips its coordinates the same way.
 */
#include <pigeon/string.h>

/* --- length and comparison --------------------------------------------- */

size_t strlen(char *s) {
    char *p = s;
    while (*p != 0) p++;
    return (unsigned)p - (unsigned)s;
}

int strcmp(char *a, char *b) {
    unsigned char *p = (unsigned char *)a;
    unsigned char *q = (unsigned char *)b;
    while (*p != 0u && *p == *q) { p++; q++; }
    return (int)*p - (int)*q;
}

int strncmp(char *a, char *b, size_t n) {
    unsigned char *p = (unsigned char *)a;
    unsigned char *q = (unsigned char *)b;
    while (n > 0u) {
        if (*p != *q) return (int)*p - (int)*q;
        if (*p == 0u) return 0;
        p++; q++; n--;
    }
    return 0;
}

/* --- copying ----------------------------------------------------------- */

char *strcpy(char *dst, char *src) {
    char *d = dst;
    while (*src != 0) { *d = *src; d++; src++; }
    *d = 0;
    return dst;
}

size_t strlcpy(char *dst, char *src, size_t size) {
    size_t length = strlen(src);
    size_t copy;
    size_t i;
    if (size == 0u) return length;
    copy = length < size ? length : size - 1u;
    for (i = 0u; i < copy; i++) dst[i] = src[i];
    dst[copy] = 0;
    return length;
}

size_t strlcat(char *dst, char *src, size_t size) {
    size_t used = 0u;
    while (used < size && dst[used] != 0) used++;
    /* No terminator inside `size`: dst is not a string we may extend. */
    if (used == size) return size + strlen(src);
    return used + strlcpy(dst + used, src, size - used);
}

/* --- searching --------------------------------------------------------- */

char *strchr(char *s, int c) {
    unsigned want = (unsigned)c & 0xFFu;    /* a sign-extended char still matches */
    unsigned char *p = (unsigned char *)s;
    while (*p != 0u) {
        if (*p == want) return (char *)p;
        p++;
    }
    if (want == 0u) return (char *)p;       /* the terminator is part of the string */
    return (char *)0;
}

char *strrchr(char *s, int c) {
    unsigned want = (unsigned)c & 0xFFu;
    unsigned char *p = (unsigned char *)s;
    char *found = (char *)0;
    while (*p != 0u) {
        if (*p == want) found = (char *)p;
        p++;
    }
    if (want == 0u) return (char *)p;
    return found;
}

/* --- numbers <-> text -------------------------------------------------- */

/* The value of one digit character, or 99 -- more than any base -- for
 * anything that is not one. `| 32` folds upper case onto lower. */
static unsigned __str_digit(unsigned c) {
    if (c - '0' < 10u) return c - '0';
    c = c | 32u;
    if (c - 'a' < 6u) return c - 'a' + 10u;
    return 99u;
}

int utoa(unsigned v, char *out, unsigned base) {
    char digits[32];
    unsigned n = 0u;
    unsigned i;
    unsigned q;
    unsigned d;

    /* Also what keeps the DIV below off zero: a divide by zero raises in
     * the emulator and takes the whole machine down, not the guest. */
    if (base < 2u || base > 16u) { out[0] = 0; return 0; }

    /* Least significant digit first, then reversed. One DIV per digit:
     * the remainder comes back through a multiply, which is all `%`
     * would compile to anyway -- after a second divide. */
    do {
        q = v / base;
        d = v - q * base;
        digits[n] = d < 10u ? '0' + d : 'a' + d - 10u;
        n++;
        v = q;
    } while (v != 0u);

    for (i = 0u; i < n; i++) out[i] = digits[n - 1u - i];
    out[n] = 0;
    return (int)n;
}

int itoa(int v, char *out) {
    if (v < 0) {
        out[0] = '-';
        /* 0u - v, not -v: the magnitude of the most negative int does not
         * fit in an int, but it does in an unsigned. */
        return 1 + utoa(0u - (unsigned)v, out + 1, 10u);
    }
    return utoa((unsigned)v, out, 10u);
}

unsigned strtou(char *s, char **end, unsigned base) {
    unsigned char *p = (unsigned char *)s;
    unsigned value = 0u;
    unsigned over = 0u;
    unsigned any = 0u;
    unsigned limit;
    unsigned scaled;
    unsigned d;

    while (isspace(*p)) p++;
    if (*p == '+') p++;

    /* A prefix counts only when a digit follows it: "0xg" is the number
     * 0 followed by "xg", as in C. */
    if ((base == 0u || base == 16u) && p[0] == '0' && (p[1] | 32u) == 'x'
            && __str_digit(p[2]) < 16u) {
        base = 16u;
        p = p + 2;
    } else if (base == 0u && p[0] == '0' && (p[1] | 32u) == 'b'
            && __str_digit(p[2]) < 2u) {
        base = 2u;
        p = p + 2;
    } else if (base == 0u) {
        base = 10u;
    }

    if (base < 2u || base > 16u) {
        if (end != (char **)0) *end = s;
        return 0u;
    }

    /* Saturate rather than wrap. `limit` is the largest value that can be
     * multiplied by the base without overflowing; the add can still carry
     * out of the top, and then the sum comes out smaller than `scaled`. */
    limit = 0xFFFFFFFFu / base;
    while ((d = __str_digit(*p)) < base) {
        if (value > limit) {
            over = 1u;
        } else {
            scaled = value * base;
            value = scaled + d;
            if (value < scaled) over = 1u;
        }
        any = 1u;
        p++;
    }

    if (over) value = 0xFFFFFFFFu;
    if (end != (char **)0) *end = any ? (char *)p : s;
    return value;
}

int atoi(char *s) {
    unsigned char *p = (unsigned char *)s;
    unsigned value = 0u;
    unsigned negative = 0u;

    while (isspace(*p)) p++;
    if (*p == '-') { negative = 1u; p++; }
    else if (*p == '+') p++;

    while (*p - '0' < 10u) {
        value = value * 10u + (*p - '0');
        p++;
    }
    if (negative) return -(int)value;
    return (int)value;
}

/* --- characters -------------------------------------------------------- */

int isdigit(int c) { return (unsigned)c - '0' < 10u; }
int isalpha(int c) { return ((unsigned)c | 32u) - 'a' < 26u; }
int isspace(int c) { return c == ' ' || (unsigned)c - '\t' < 5u; }

int tolower(int c) {
    if ((unsigned)c - 'A' < 26u) return c + 32;
    return c;
}

int toupper(int c) {
    if ((unsigned)c - 'a' < 26u) return c - 32;
    return c;
}
