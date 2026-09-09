/* Memory operations. See mem.h.
 *
 * Loop counters are `unsigned` on purpose: this machine's CMP is
 * unsigned, so a signed comparison costs two extra XOR instructions
 * every time round the loop.
 */

typedef unsigned int size_t;

/* The linker script is the assembler: __heap_ptr and __free_list are
 * emitted by the compiler, and __heap_base sits past the end of the
 * frame stack. */
extern unsigned __heap_ptr;
extern unsigned __free_list;
extern unsigned __heap_base;

/* --- block operations -------------------------------------------------- */

void *memcpy(void *dst, void *src, size_t n) {
    unsigned char *d = (unsigned char *)dst;
    unsigned char *s = (unsigned char *)src;

    /* Word at a time while both are aligned. The byte loop is about four
     * instructions per byte, and a full-screen clear is 40,000 bytes --
     * the difference between ~10,000 and ~40,000 instructions. */
    if ((((unsigned)d | (unsigned)s) & 3u) == 0u) {
        unsigned *dw = (unsigned *)d;
        unsigned *sw = (unsigned *)s;
        while (n >= 4u) { *dw = *sw; dw++; sw++; n -= 4u; }
        d = (unsigned char *)dw;
        s = (unsigned char *)sw;
    }
    while (n > 0u) { *d = *s; d++; s++; n--; }
    return dst;
}

void *memmove(void *dst, void *src, size_t n) {
    unsigned char *d = (unsigned char *)dst;
    unsigned char *s = (unsigned char *)src;

    if (d == s || n == 0u) return dst;
    if (d < s) return memcpy(dst, src, n);

    /* Overlapping the wrong way: copy backwards so a byte is read before
     * it is overwritten. */
    d = d + n;
    s = s + n;
    while (n > 0u) { d--; s--; *d = *s; n--; }
    return dst;
}

void *memset(void *dst, int value, size_t n) {
    unsigned char *d = (unsigned char *)dst;
    unsigned byte = ((unsigned)value) & 0xFFu;

    if (((unsigned)d & 3u) == 0u) {
        unsigned word = byte | (byte << 8) | (byte << 16) | (byte << 24);
        unsigned *dw = (unsigned *)d;
        while (n >= 4u) { *dw = word; dw++; n -= 4u; }
        d = (unsigned char *)dw;
    }
    while (n > 0u) { *d = (unsigned char)byte; d++; n--; }
    return dst;
}

int memcmp(void *a, void *b, size_t n) {
    unsigned char *p = (unsigned char *)a;
    unsigned char *q = (unsigned char *)b;
    while (n > 0u) {
        if (*p != *q) return (int)*p - (int)*q;
        p++; q++; n--;
    }
    return 0;
}

/* --- allocation --------------------------------------------------------
 *
 * Every block carries an 8-byte header immediately before its payload:
 *
 *     -8   size   payload bytes, always a multiple of 4
 *     -4   next   next free block, or 0
 *
 * size is a multiple of 4, so its low bits are free; bit 0 is the in-use
 * flag. malloc returns the payload, so free(p) finds the header at p-8
 * without searching.
 *
 * First fit, no coalescing and no splitting: a workload that frees large
 * blocks then allocates small ones will fragment. The display and input
 * libraries allocate nothing at runtime, so this is adequate; coalescing
 * adjacent free blocks is the obvious next step.
 */

static unsigned heap_limit(void) {
    /* Leave a megabyte of headroom below the hardware stack, which grows
     * down from the top of RAM. */
    return 0x08000000u - 0x00100000u;
}

void *malloc(size_t n) {
    unsigned *previous;
    unsigned *block;
    unsigned *header;
    unsigned next;

    n = (n + 3u) & 0xFFFFFFFCu;
    if (n == 0u) n = 4u;

    if (__heap_ptr == 0u) __heap_ptr = (unsigned)&__heap_base;

    previous = (unsigned *)0;
    block = (unsigned *)__free_list;
    while (block != (unsigned *)0) {
        if (block[0] >= n) {
            next = block[1];
            if (previous != (unsigned *)0) previous[1] = next;
            else __free_list = next;
            block[0] = block[0] | 1u;          /* mark in use */
            return (void *)(block + 2);
        }
        previous = block;
        block = (unsigned *)block[1];
    }

    if (__heap_ptr + n + 8u > heap_limit()) return (void *)0;
    header = (unsigned *)__heap_ptr;
    __heap_ptr = __heap_ptr + n + 8u;
    header[0] = n | 1u;
    header[1] = 0u;
    return (void *)(header + 2);
}

void free(void *p) {
    unsigned *header;
    if (p == (void *)0) return;
    header = ((unsigned *)p) - 2;
    header[0] = header[0] & 0xFFFFFFFEu;       /* clear in use */
    header[1] = __free_list;                   /* push onto the free list */
    __free_list = (unsigned)header;
}

void *calloc(size_t count, size_t size) {
    size_t total = count * size;
    void *p = malloc(total);
    if (p != (void *)0) memset(p, 0, total);
    return p;
}

size_t heap_used(void) {
    if (__heap_ptr == 0u) return 0u;
    return __heap_ptr - (unsigned)&__heap_base;
}
