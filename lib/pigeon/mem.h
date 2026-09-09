/* <pigeon/mem.h> -- memory operations.
 *
 * The whole standard library, for now: block moves and a heap. No
 * strings, no formatted output, no math.
 */
#ifndef PIGEON_MEM_H
#define PIGEON_MEM_H

typedef unsigned int size_t;

#define NULL ((void *)0)

/* --- block operations -------------------------------------------------- */
void *memcpy (void *dst, void *src, size_t n);   /* must not overlap */
void *memmove(void *dst, void *src, size_t n);   /* overlap is fine   */
void *memset (void *dst, int value, size_t n);   /* low byte of value */
int   memcmp (void *a, void *b, size_t n);       /* <0, 0, >0         */

/* --- allocation -------------------------------------------------------- */
void  *malloc(size_t n);            /* NULL when the heap is exhausted */
void  *calloc(size_t count, size_t size);
void   free(void *p);
size_t heap_used(void);             /* bytes handed out, for debugging */

#endif
