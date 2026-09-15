/* <pigeon/stdio.h> -- printf and friends.
 *
 *     printf("%s: %d files, %u KB free\n", dir, count, free_kb);
 *     n = snprintf(line, 32u, "%-12s %5u", name, size);
 *
 * snprintf and vsnprintf format into a buffer, and work in any program.
 * printf, vprintf, puts and putchar write to STDOUT through the kernel
 * (<pigeon/sys.h>). With no kernel they write to the debug port instead
 * (<pigeon/debug.h>), never calling through the empty system-call table,
 * and return -1 only when there is no port either (docs/phase5_plan.md).
 *
 * Conversions: %d %i %u %x %X %o %c %s %p %%, the flags '-' (to the left)
 * and '0' (zeros), a width, and for %s a precision: %.5s. No floating
 * point, since the machine has none. An unknown conversion is printed as
 * written. A call takes at most 8 arguments after its named ones, the
 * compiler's limit for a variadic call.
 */
#ifndef PIGEON_STDIO_H
#define PIGEON_STDIO_H

#include <pigeon/stdarg.h>

int snprintf (char *buf, unsigned size, char *format, ...);    /* -> the length it wanted */
int vsnprintf(char *buf, unsigned size, char *format, va_list ap);

int printf (char *format, ...);                                 /* -> characters, or -1 */
int vprintf(char *format, va_list ap);
int puts   (char *s);                                           /* s, then a newline    */
int putchar(int c);

#endif
