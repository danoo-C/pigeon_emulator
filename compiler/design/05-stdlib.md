# 5. `<pigeon/mem.h>` — the standard library

Memory operations only, per the brief. No strings, no I/O formatting, no math.

```c
#ifndef PIGEON_MEM_H
#define PIGEON_MEM_H

typedef unsigned int size_t;

/* --- block operations ------------------------------------------------- */
void *memcpy (void *dst, const void *src, size_t n);   /* must not overlap */
void *memmove(void *dst, const void *src, size_t n);   /* overlap is fine  */
void *memset (void *dst, int value, size_t n);         /* low byte of value */
int   memcmp (const void *a, const void *b, size_t n); /* <0, 0, >0        */

/* --- allocation ------------------------------------------------------- */
void *malloc (size_t n);          /* NULL when the heap is exhausted */
void *calloc (size_t count, size_t size);
void  free   (void *p);
size_t heap_used(void);           /* bytes handed out, for debugging */
size_t heap_free(void);

#define NULL ((void *)0)

#endif
```

## The heap

One region, growing up, between the frame stack and the hardware stack
([03-abi.md](03-abi.md)):

```
__heap_base   ┌──────────────────────────────┐
              │ [hdr][ payload ][hdr][ ... ] │  allocated and freed blocks
__heap_ptr    ├──────────────────────────────┤  <- bump pointer
              │ never touched                │
              └──────────────────────────────┘
              ... free space up to the hardware stack ...
```

Every block carries an 8-byte header immediately before its payload:

| Offset | Field |
|---|---|
| −8 | `size` — payload bytes, always a multiple of 4 |
| −4 | `next_free` — next block on the free list, or 0 |

`size` is a multiple of 4, so its low two bits are always zero; bit 0 is
borrowed as the in-use flag. `malloc` returns a pointer to the payload, so
`free(p)` finds the header at `p - 8` without a search.

**Allocation** — first fit on the free list, then bump:

```c
void *malloc(size_t n) {
    n = (n + 3) & ~3u;                    /* round up to a word */
    if (n == 0) n = 4;

    unsigned *prev = 0;
    unsigned *b = __free_list;
    while (b) {                           /* first fit */
        if (b[0] >= n) {
            if (prev) prev[1] = b[1]; else __free_list = (unsigned *)b[1];
            b[0] |= 1u;                   /* mark in use */
            return (void *)(b + 2);
        }
        prev = b;
        b = (unsigned *)b[1];
    }

    if (__heap_ptr + n + 8 > __heap_limit) return NULL;   /* out of room */
    unsigned *h = (unsigned *)__heap_ptr;
    __heap_ptr += n + 8;
    h[0] = n | 1u;
    h[1] = 0;
    return (void *)(h + 2);
}

void free(void *p) {
    if (!p) return;
    unsigned *h = (unsigned *)p - 2;
    h[0] &= ~1u;                          /* clear in use */
    h[1] = (unsigned)__free_list;         /* push onto the free list */
    __free_list = h;
}
```

**Deliberately simple.** No coalescing, no splitting, so a workload that frees
many large blocks and then allocates many small ones fragments badly. The
display and input libraries allocate nothing at runtime, so this is adequate;
coalescing adjacent free blocks is the obvious next step if a real program needs
it. First fit was chosen over best fit because it is shorter and the failure
mode is easier to reason about.

## Block operations

`memcpy` copies whole words while it can, then the tail byte by byte. This
matters: the byte loop is roughly four instructions per byte, and the framebuffer
is 40,000 bytes, so a full-screen `memset` is the difference between ~10,000 and
~40,000 instructions.

```c
void *memcpy(void *dst, const void *src, size_t n) {
    unsigned char *d = dst;
    const unsigned char *s = src;

    /* word-at-a-time while both are word-aligned and 4+ bytes remain */
    if ((((unsigned)d | (unsigned)s) & 3u) == 0) {
        unsigned *dw = (unsigned *)d;
        const unsigned *sw = (const unsigned *)s;
        while (n >= 4) { *dw++ = *sw++; n -= 4; }
        d = (unsigned char *)dw;
        s = (const unsigned char *)sw;
    }
    while (n--) *d++ = *s++;
    return dst;
}
```

`memset` is the same shape, broadcasting the byte to a word first
(`w = c | c<<8 | c<<16 | c<<24`) — which the compiler lowers to `SHL`/`OR`, no
multiply needed.

`memmove` compares the pointers and copies backwards when they overlap the wrong
way. `memcmp` is a plain byte loop; it returns the difference of the first
differing pair, so the result is a sign, not a magnitude.

## Notes for the implementer

- `size_t` is `unsigned int`, so all the loop counters here compare unsigned and
  avoid the sign-bit-flip sequence ([04-codegen.md](04-codegen.md)).
- `while (n--)` is the right idiom: it compares against zero, and `CMP A, #0`
  followed by `JZ` is two instructions.
- These are ordinary C, compiled by the same compiler. Nothing here needs
  inline assembly — only the display and input libraries do, and only for the
  IO header.
- **Not thread-safe, and it does not need to be.** There is one CPU and no
  interrupts; the only concurrency in the whole machine is on the Python side,
  in the HTTP servers, which never touch guest memory.
