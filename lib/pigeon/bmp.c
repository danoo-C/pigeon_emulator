/* BMP images, decoded to the screen's own pixels. See bmp.h.
 *
 * A pixel is read as one word. The file stores it as B, G, R -- and A, in a
 * 32-bit file -- so the word at its first byte is 0x..RRGGBB already, and
 * ORing in 0xFF000000 makes it an opaque screen pixel: one load, one OR and
 * one store, about 42 instructions a pixel with the loop, where three byte
 * loads and their shifts were 95 (docs/bmp_plan.md §2). In a 24-bit file
 * the word's top byte is the next pixel's, or the row's padding, and the OR
 * throws it away. For the last pixel of a file with no padding it is the
 * byte after the file: harmless here, where every address is RAM and a read
 * at the top of memory wraps round (emulator/ram.py, read_word).
 *
 * Where the pixels come from is worked out once a call: a table of source
 * columns, which are always one run, and black beside and above and below
 * an image smaller than what was asked for.
 */
#include <pigeon/bmp.h>
#include <pigeon/mem.h>
#include <pigeon/sys.h>

#define BMP_FILE_HEADER 14u
#define BMP_INFO_MIN    40u
#define BMP_MASKS_END   66u         /* three bit-field masks after a 40-byte header */
#define BMP_MAX_IMAGE   8192u
#define BMP_MAX_RESULT  4096u
#define BMP_BLACK       0xFF000000u

typedef struct {
    unsigned offset;                /* where the pixels start in the file   */
    unsigned width;
    unsigned height;
    unsigned top_down;              /* 1: the first row stored is the top   */
    unsigned bytes;                 /* 3 or 4 a pixel                       */
    unsigned stride;                /* bytes a stored row, padding included */
} bmp_header;

static int bmp_last = BMP_OK;

static unsigned bmp_word(unsigned char *p) {
    return (unsigned)p[0] | ((unsigned)p[1] << 8) | ((unsigned)p[2] << 16) | ((unsigned)p[3] << 24);
}

static unsigned *bmp_fail(int err) {
    bmp_last = err;
    return (unsigned *)0;
}

static int bmp_set(int err) {
    bmp_last = err;
    return err;
}

/* Whether a kernel filled in the system-call table, as stdio.c asks it:
 * without one, open's slot is 0, and calling through it jumps to 0. */
static int bmp_kernel(void) {
    return *(unsigned *)(SYSCALL_TABLE + SYS_OPEN * 4u) != 0u;
}

/* The header of a file `size` bytes long, whose first `have` bytes are at
 * `file`: BMP_OK with *out filled in, or why not. */
static int bmp_parse(unsigned char *file, unsigned have, unsigned size, bmp_header *out) {
    unsigned info;
    unsigned bits;
    unsigned compression;
    unsigned height;

    if (file == (unsigned char *)0 || have < BMP_FILE_HEADER + BMP_INFO_MIN
            || file[0] != 'B' || file[1] != 'M') return BMP_ENOTBMP;
    info = bmp_word(file + 14u);
    out->offset = bmp_word(file + 10u);
    if (info < BMP_INFO_MIN || out->offset < BMP_FILE_HEADER + BMP_INFO_MIN) return BMP_ENOTBMP;
    if (((unsigned)file[26] | ((unsigned)file[27] << 8)) != 1u) return BMP_EFORMAT;
    bits = (unsigned)file[28] | ((unsigned)file[29] << 8);
    if (bits != 24u && bits != 32u) return BMP_EFORMAT;
    compression = bmp_word(file + 30u);
    if (compression == 3u && bits == 32u) {
        if (have < BMP_MASKS_END) return BMP_ETRUNCATED;
        if (bmp_word(file + 54u) != 0x00FF0000u || bmp_word(file + 58u) != 0x0000FF00u
                || bmp_word(file + 62u) != 0x000000FFu) return BMP_EFORMAT;
    } else if (compression != 0u) {
        return BMP_EFORMAT;
    }
    out->width = bmp_word(file + 18u);
    height = bmp_word(file + 22u);
    out->top_down = 0u;
    if ((height & 0x80000000u) != 0u) {
        height = 0u - height;
        out->top_down = 1u;
    }
    out->height = height;
    if (out->width == 0u || out->height == 0u) return BMP_EFORMAT;
    if (out->width > BMP_MAX_IMAGE || out->height > BMP_MAX_IMAGE) return BMP_ETOOBIG;
    out->bytes = bits / 8u;
    out->stride = (out->width * out->bytes + 3u) & 0xFFFFFFFCu;
    if (out->offset > size || size - out->offset < out->stride * out->height) return BMP_ETRUNCATED;
    return BMP_OK;
}

/* Where output position i, of n, comes from along an axis the image has
 * `size` of: its position in the image. Anything from `size` up is past the
 * image, and black -- which is what a position before a centred image comes
 * to as well, wrapping round below 0. */
static unsigned bmp_source(unsigned i, unsigned n, unsigned size, int mode) {
    if (mode == BMP_STRETCH) return i * size / n;
    if (mode == BMP_CROP_TOP_LEFT) return i;
    if (size >= n) return i + (size - n) / 2u;
    return i - (n - size) / 2u;
}

unsigned *bmp_decode(unsigned char *file, unsigned size, unsigned w, unsigned h, int mode) {
    bmp_header hd;
    unsigned *pixels;
    unsigned *cols;
    unsigned *o;
    unsigned *c;
    unsigned *end;
    unsigned char *row;
    unsigned x;
    unsigned y;
    unsigned sx;
    unsigned sy;
    unsigned first;
    unsigned last;
    int err;

    if (w == 0u || h == 0u
            || (mode != BMP_CROP && mode != BMP_CROP_TOP_LEFT && mode != BMP_STRETCH)) {
        return bmp_fail(BMP_EARGS);
    }
    if (w > BMP_MAX_RESULT || h > BMP_MAX_RESULT) return bmp_fail(BMP_ETOOBIG);
    err = bmp_parse(file, size, size, &hd);
    if (err != BMP_OK) return bmp_fail(err);
    pixels = (unsigned *)malloc(w * h * 4u);
    cols = (unsigned *)malloc(w * 4u);
    if (pixels == (unsigned *)0 || cols == (unsigned *)0) {
        free(cols);
        free(pixels);
        return bmp_fail(BMP_ENOMEM);
    }

    first = w;                      /* the run of columns that come from the image */
    last = 0u;
    for (x = 0u; x < w; x++) {
        sx = bmp_source(x, w, hd.width, mode);
        if (sx < hd.width) {
            cols[x] = sx * hd.bytes;
            if (first == w) first = x;
            last = x + 1u;
        }
    }

    o = pixels;
    for (y = 0u; y < h; y++) {
        sy = bmp_source(y, h, hd.height, mode);
        end = o + w;
        if (sy >= hd.height || first >= last) {
            while (o != end) {
                *o = BMP_BLACK;
                o++;
            }
        } else {
            if (hd.top_down == 0u) sy = hd.height - 1u - sy;
            row = file + hd.offset + sy * hd.stride;
            end = o + first;
            while (o != end) {
                *o = BMP_BLACK;
                o++;
            }
            c = cols + first;
            end = o + (last - first);
            while (o != end) {
                *o = *(unsigned *)(row + *c) | BMP_BLACK;
                o++;
                c++;
            }
            end = pixels + (y + 1u) * w;
            while (o != end) {
                *o = BMP_BLACK;
                o++;
            }
        }
    }
    free(cols);
    bmp_last = BMP_OK;
    return pixels;
}

unsigned *bmp_load(char *path, unsigned w, unsigned h, int mode) {
    sys_stat_t st;
    unsigned char *file;
    unsigned *pixels;
    int fd;
    int n;

    if (!bmp_kernel()) return bmp_fail(BMP_ENOKERNEL);
    n = stat(path, &st);
    if (n < 0) return bmp_fail(n);
    file = (unsigned char *)malloc(st.size + 1u);
    if (file == (unsigned char *)0) return bmp_fail(BMP_ENOMEM);
    fd = open(path, O_READ);
    if (fd < 0) {
        free(file);
        return bmp_fail(fd);
    }
    n = read(fd, file, st.size);
    close(fd);
    if (n < 0) {
        free(file);
        return bmp_fail(n);
    }
    pixels = bmp_decode(file, (unsigned)n, w, h, mode);
    free(file);
    return pixels;
}

int bmp_info(char *path, unsigned *w, unsigned *h) {
    unsigned char head[66];         /* BMP_MASKS_END */
    sys_stat_t st;
    bmp_header hd;
    int fd;
    int n;

    if (!bmp_kernel()) return bmp_set(BMP_ENOKERNEL);
    n = stat(path, &st);
    if (n < 0) return bmp_set(n);
    fd = open(path, O_READ);
    if (fd < 0) return bmp_set(fd);
    n = read(fd, head, BMP_MASKS_END);
    close(fd);
    if (n < 0) return bmp_set(n);
    n = bmp_parse(head, (unsigned)n, st.size, &hd);
    if (n != BMP_OK) return bmp_set(n);
    *w = hd.width;
    *h = hd.height;
    return bmp_set(BMP_OK);
}

int bmp_error(void) {
    return bmp_last;
}

char *bmp_strerror(int err) {
    if (err == BMP_OK) return "ok";
    if (err == BMP_ENOKERNEL) return "needs the kernel: use bmp_decode";
    if (err == BMP_ENOTBMP) return "not a BMP";
    if (err == BMP_EFORMAT) return "not a 24- or 32-bit BMP";
    if (err == BMP_ETRUNCATED) return "cut short";
    if (err == BMP_ETOOBIG) return "too big";
    if (err == BMP_ENOMEM) return "out of memory";
    if (err == BMP_EARGS) return "no such size or mode";
    return sys_strerror(err);
}
