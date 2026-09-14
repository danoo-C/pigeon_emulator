/* <pigeon/stdarg.h> -- walking a variadic function's extra arguments.
 *
 *     int sum(int count, ...) {
 *         va_list ap;
 *         int total = 0;
 *         va_start(ap, count);
 *         while (count > 0) { total = total + va_arg(ap, int); count--; }
 *         va_end(ap);
 *         return total;
 *     }
 *
 * A variadic function takes up to 8 extra arguments, each one word: an
 * int, a char or a pointer. Its frame holds them right after its last named
 * parameter (compiler/design/03-abi.md), so a va_list is a pointer walking
 * those words. Nothing records how many were passed: the function has to
 * know, from its own arguments -- printf knows from its format.
 *
 * Only macros, and no .c, so including it adds no code.
 */
#ifndef PIGEON_STDARG_H
#define PIGEON_STDARG_H

typedef unsigned *va_list;

#define va_start(ap, last) ((ap) = (unsigned *)&(last) + 1)
#define va_arg(ap, type)   ((type)*(ap)++)
#define va_copy(dst, src)  ((dst) = (src))
#define va_end(ap)         ((ap) = (va_list)0)

#endif
