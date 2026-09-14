/* printf and friends. See stdio.h.
 *
 * One formatter does all of it, writing a character at a time to an
 * output: a buffer that keeps what fits, for snprintf, or a small chunk
 * flushed to STDOUT whenever it fills, for printf, so a long printf needs
 * no big buffer. Either way it counts every character it produced, which
 * is what snprintf returns when it had to cut.
 *
 * There is no switch in pigeon-cc, so the conversions are an if-chain.
 */
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define STDIO_CHUNK 64u

struct __stdio_out {
    char *buf;
    unsigned size;      /* bytes in buf                                  */
    unsigned len;       /* characters produced, kept or not              */
    unsigned used;      /* printf: characters waiting in buf             */
    int console;        /* 1: flush buf to STDOUT as it fills            */
};

static void __stdio_put(struct __stdio_out *o, int c) {
    if (o->console) {
        o->buf[o->used] = (char)c;
        o->used++;
        if (o->used == o->size) {
            write(STDOUT, o->buf, o->used);
            o->used = 0u;
        }
    } else if (o->len + 1u < o->size) {
        o->buf[o->len] = (char)c;
    }
    o->len++;
}

static void __stdio_pad(struct __stdio_out *o, int c, int n) {
    while (n > 0) {
        __stdio_put(o, c);
        n--;
    }
}

static void __stdio_format(struct __stdio_out *o, char *f, va_list ap) {
    char digits[STR_UTOA_MAX + 2];
    char *s;
    char *prefix;
    unsigned n;
    unsigned u;
    unsigned i;
    int v;
    int width;
    int precision;
    int left;
    int zero;
    int spare;

    while (*f != 0) {
        if (*f != '%') {
            __stdio_put(o, (int)*f);
            f++;
            continue;
        }
        f++;
        left = 0;
        zero = 0;
        width = 0;
        precision = -1;
        while (*f == '-' || *f == '0') {
            if (*f == '-') left = 1;
            else zero = 1;
            f++;
        }
        while (*f >= '0' && *f <= '9') {
            width = width * 10 + (*f - '0');
            f++;
        }
        if (*f == '.') {
            f++;
            precision = 0;
            while (*f >= '0' && *f <= '9') {
                precision = precision * 10 + (*f - '0');
                f++;
            }
        }
        if (*f == 0) break;                 /* a '%' at the very end */

        s = digits;
        prefix = "";
        if (*f == 'd' || *f == 'i') {
            v = va_arg(ap, int);
            if (v < 0) {
                prefix = "-";
                u = 0u - (unsigned)v;
            } else {
                u = (unsigned)v;
            }
            n = (unsigned)utoa(u, digits, 10u);
        } else if (*f == 'u') {
            n = (unsigned)utoa(va_arg(ap, unsigned), digits, 10u);
        } else if (*f == 'x' || *f == 'X') {
            n = (unsigned)utoa(va_arg(ap, unsigned), digits, 16u);
            if (*f == 'X') {
                for (i = 0u; i < n; i++) digits[i] = (char)toupper((int)digits[i]);
            }
        } else if (*f == 'o') {
            n = (unsigned)utoa(va_arg(ap, unsigned), digits, 8u);
        } else if (*f == 'p') {
            prefix = "0x";
            n = (unsigned)utoa(va_arg(ap, unsigned), digits, 16u);
        } else if (*f == 'c') {
            digits[0] = (char)va_arg(ap, int);
            n = 1u;
            zero = 0;
        } else if (*f == 's') {
            s = va_arg(ap, char *);
            if (s == NULL) s = "(null)";
            n = strlen(s);
            if (precision >= 0 && n > (unsigned)precision) n = (unsigned)precision;
            zero = 0;
        } else if (*f == '%') {
            __stdio_put(o, '%');
            f++;
            continue;
        } else {
            __stdio_put(o, '%');            /* unknown: as written */
            __stdio_put(o, (int)*f);
            f++;
            continue;
        }
        f++;

        spare = width - (int)(n + strlen(prefix));
        if (left == 0 && zero == 0) __stdio_pad(o, ' ', spare);
        while (*prefix != 0) {
            __stdio_put(o, (int)*prefix);
            prefix++;
        }
        if (left == 0 && zero != 0) __stdio_pad(o, '0', spare);
        for (i = 0u; i < n; i++) __stdio_put(o, (int)s[i]);
        if (left != 0) __stdio_pad(o, ' ', spare);
    }
}

int vsnprintf(char *buf, unsigned size, char *format, va_list ap) {
    struct __stdio_out o;
    o.buf = buf;
    o.size = size;
    o.len = 0u;
    o.used = 0u;
    o.console = 0;
    __stdio_format(&o, format, ap);
    if (size > 0u) buf[o.len < size ? o.len : size - 1u] = 0;
    return (int)o.len;
}

int snprintf(char *buf, unsigned size, char *format, ...) {
    va_list ap;
    int n;
    va_start(ap, format);
    n = vsnprintf(buf, size, format, ap);
    va_end(ap);
    return n;
}

/* Whether a kernel filled in the system-call table: without one, write's
 * slot is 0, and calling through it would jump to address 0. */
static int __stdio_kernel(void) {
    return *(unsigned *)(SYSCALL_TABLE + SYS_WRITE * 4u) != 0u;
}

int vprintf(char *format, va_list ap) {
    char chunk[STDIO_CHUNK];
    struct __stdio_out o;
    if (!__stdio_kernel()) return -1;
    o.buf = chunk;
    o.size = STDIO_CHUNK;
    o.len = 0u;
    o.used = 0u;
    o.console = 1;
    __stdio_format(&o, format, ap);
    if (o.used > 0u) write(STDOUT, chunk, o.used);
    return (int)o.len;
}

int printf(char *format, ...) {
    va_list ap;
    int n;
    va_start(ap, format);
    n = vprintf(format, ap);
    va_end(ap);
    return n;
}

int puts(char *s) {
    if (!__stdio_kernel()) return -1;
    print(s);
    print("\n");
    return 0;
}

int putchar(int c) {
    char one[1];
    if (!__stdio_kernel()) return -1;
    one[0] = (char)c;
    write(STDOUT, one, 1u);
    return c & 255;
}
