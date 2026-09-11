/* <pigeon/string.h> -- strings, numbers as text, and character classes.
 *
 * Strings are NUL-terminated char arrays, as in C, and the functions are
 * the standard ones of the same names, cut down to what this machine
 * needs. utoa()/itoa() are here because there is no printf: a number
 * reaches the screen by being written into a buffer and handed to
 * disp_text().
 *
 *     char line[32];
 *     unsigned n = strlcpy(line, "score ", sizeof(line));
 *     itoa(score, line + n);
 *     disp_text(2, 2, line, WHITE);
 *
 * Copies are BOUNDED. There is strlcpy/strlcat and no strcat or strncpy:
 * this machine has no memory protection, so an overrun does not fault --
 * it quietly rewrites whatever comes next, which is how
 * user/checkerboard.asm ends up overwriting its own code. strncpy is left
 * out because it does not terminate a string it had to cut.
 *
 * This library stands alone: no heap, no mem.c. It defines size_t and
 * NULL itself rather than including mem.h, for two reasons. The launcher
 * only follows #include lines in .c files, so a header that included
 * mem.h would declare memcpy without mem.c ever being compiled in. And a
 * program that only formats a number should not pay for an allocator.
 * mem.h defines the same two names; the compiler accepts both.
 */
#ifndef PIGEON_STRING_H
#define PIGEON_STRING_H

typedef unsigned int size_t;
#ifndef NULL
#define NULL ((void *)0)
#endif

/* --- length and comparison: bytes compare as UNSIGNED char ------------- */
size_t strlen (char *s);
int    strcmp (char *a, char *b);                 /* <0, 0, >0         */
int    strncmp(char *a, char *b, size_t n);

/* --- copying ------------------------------------------------------------
 * strlcpy and strlcat always leave a terminated string when size > 0,
 * and return the length they TRIED to make -- so a result >= size means
 * it was cut short. */
char  *strcpy (char *dst, char *src);             /* you guarantee the room */
size_t strlcpy(char *dst, char *src, size_t size);
size_t strlcat(char *dst, char *src, size_t size);

/* --- searching: NULL when absent; searching for 0 finds the terminator -- */
char *strchr (char *s, int c);
char *strrchr(char *s, int c);

/* --- numbers <-> text ---------------------------------------------------
 * utoa writes lower-case digits in base 2..16 and returns the length; an
 * invalid base writes "" and returns 0. The longest result is 32 binary
 * digits, so STR_UTOA_MAX bytes is always room enough, terminator
 * included. itoa is decimal, with a '-' when negative. */
#define STR_UTOA_MAX 33
int utoa(unsigned v, char *out, unsigned base);
int itoa(int v, char *out);

/* strtou skips leading spaces and a '+'. Base 0 works the base out: 0x
 * is hex, 0b binary, anything else decimal; base 16 also accepts 0x. A
 * value too big saturates to 0xFFFFFFFF, as strtoul does. If end is not
 * NULL, *end is set to the first character not used -- or to s when
 * there were no digits at all.
 *
 * atoi is the quick decimal version: spaces, a sign, digits, and no
 * overflow check -- a value too big wraps. */
unsigned strtou(char *s, char **end, unsigned base);
int      atoi  (char *s);

/* --- characters: plain ASCII; anything else, -1 included, is none ------ */
int isdigit(int c);
int isalpha(int c);
int isspace(int c);                               /* space \t \n \v \f \r */
int tolower(int c);
int toupper(int c);

#endif
