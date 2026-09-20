# 2. The language

A C subset chosen so the display and input libraries can be written naturally.

## Types

```c
char            /* 1 byte,  signed by default            */
unsigned char   /* 1 byte                                 */
int             /* 4 bytes, signed                        */
unsigned int    /* 4 bytes  (also spelled `unsigned`)     */
void            /* return type and `void *` only          */
T *             /* 4 bytes, any depth                     */
T [N]           /* fixed-size array, decays to T *         */
struct { ... }  /* members laid out in declaration order   */
```

`short` and `long` are accepted as aliases for `int`. `float`, `double` and
`long long` are rejected with a clear message — there is no floating-point
hardware and soft-float is out of scope.

`typedef` is supported; `union`, `enum` and bitfields are phase 2.

### Signedness matters more than usual

The machine compares, divides and shifts **unsigned**, and loads a byte
zero-extended. `int` and `char` are signed, so the compiler puts the sign back
every time — two extra `XOR`s on a relational operator
([01-overview.md](01-overview.md) §4), two instructions after a `char` load, a
`__divsi3` call for `/` and `%`, three instructions for `>>`.

All of it is skipped for an unsigned type. So declaring loop counters,
coordinates and bytes `unsigned` produces meaningfully smaller code, and the
libraries in `05`–`07` do so deliberately — but a signed type is now *correct*
rather than merely smaller-if-you-avoid-it.

## Declarations

```c
int   x;                      /* global -> .bss, zero-initialised            */
int   y = 7;                  /* global with an initialiser -> emitted inline */
int   z = 2 * K + 1;          /* any constant expression: folded at compile time */
int  *p = &x;                 /* an address is a constant too                 */
static int z;                 /* same storage; name not exported             */
int   grid[100];              /* array; size must be a constant expression   */
struct point { int x, y; };
struct point origin;
```

`char *p = "hi";` is a **pointer** to the string and `char v[] = "hi";` is a
3-byte array of its own. Both spellings reach the same type in the
declarator, so until 2026-09-20 either became the array *(docs/compiler_plan.md)*.

**A global's initialiser must be a compile-time constant** — a folded
expression (`sizeof`, casts, `?:` and the arithmetic operators all fold), or
an address: `&global`, an array's or a function's name, a string. Anything
else, such as another variable's value, is an error. *(Until 2026-09-20 it
was silently zero, which is why `int a = -1;` gave 0 — docs/compiler_plan.md.)*

Local declarations may appear anywhere in a block (C99 style). Locals are **not**
implicitly zeroed — the frame stack is reused across calls, so an uninitialised
local holds whatever the previous call left there. The compiler warns on a read
that is obviously before any write.

## Statements

`if` / `else`, `while`, `do`/`while`, `for`, `break`, `continue`, `return`,
compound blocks, expression statements, and the null statement.

`switch` is phase 2 — a jump table needs `CALL reg`-style indirect dispatch,
which the ISA supports (`JMP` takes a register), so it is a matter of effort
rather than capability.

`goto` is not supported.

## Operators

| Group | Supported |
|---|---|
| Arithmetic | `+ - * / %` and unary `-` |
| Bitwise | `& \| ^ ~ << >>` |
| Relational | `== != < > <= >=` |
| Logical | `&& \|\|` `!` — short-circuiting |
| Assignment | `=` and all compound forms (`+=`, `<<=`, …) |
| Increment | `++` / `--`, prefix and postfix |
| Pointer | `*` `&` `[]` `.` `->` |
| Other | `sizeof`, casts, `?:`, comma |

**Signed `/`, `%` and `>>` are correct.** The machine's `DIV` and `SHR` are
unsigned and there is no `SAR`, so the compiler puts the sign back: `/` and
`%` call a `__divsi3` / `__modsi3` helper it plants when something uses one,
and `>>` shifts the sign in with three extra instructions. Both are skipped
entirely when the operands are unsigned, which is most of this machine's
arithmetic. *(Until 2026-09-20 these were the raw unsigned instructions with
no diagnostic: `-20 / 2` was 2,147,483,638 and `-8 >> 1` was 2,147,483,644.
An earlier version of this document claimed a diagnostic that did not exist —
docs/compiler_plan.md.)*

`<pigeon/math.h>`'s `idiv`, `imod` and `ishr` do the same thing in C. They
stay, because they are published API and because they also take a zero
divisor without faulting, but plain `/`, `%` and `>>` are now correct on
their own.

## Functions

```c
int  add(int a, int b);              /* prototype  */
int  add(int a, int b) { return a + b; }
void clear(void);
int  apply(int (*fn)(int), int v);   /* function pointers: JMP/CALL take a register */
```

Up to 8 parameters. A variadic function, `int printf(char *format, ...)`,
takes up to 8 more, each a word, walked with `<pigeon/stdarg.h>`. Variadics
came later than this design, with `printf` in the kernel's phase 4: see
[03-abi.md](03-abi.md#variadic-functions).

Recursion works and is verified — see [03-abi.md](03-abi.md).

## Preprocessor

Enough to make headers work, no more:

- `#include "..."` and `#include <...>`, with an include path
- `#define NAME value` — object-like macros only, no parameters
- `#ifdef` / `#ifndef` / `#else` / `#endif`
- `#pragma once`

Function-like macros are phase 2. The display library is designed so it does not
need them.

## Entry point

```c
int main(void);
```

The compiler emits a startup stub before `main` that sets the frame pointer and
initialises the heap, then calls `main`, then `HALT`s with the return value in
`A`. `#pragma origin` is not needed — the compiler always emits
`.ORG PROGRAM_LOAD_ADDR`, using the symbol the assembler already predefines.

## Deliberately absent

| Missing | Why |
|---|---|
| `float`, `double` | no FP hardware, soft-float out of scope |
| `long long` | no 64-bit support in the ISA |
| `goto` | not needed for the libraries; complicates label handling |
| `switch` | phase 2 |
| `union`, `enum`, bitfields | phase 2 |
| standard C library beyond memory ops | scope, per the brief |
