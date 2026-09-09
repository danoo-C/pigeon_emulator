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

The machine compares unsigned. `int` is signed, so every relational operator on
signed operands costs two extra `XOR`s ([01-overview.md](01-overview.md) §4).
Declaring loop counters and coordinates `unsigned` produces meaningfully
smaller code, and the libraries in `05`–`07` do so deliberately.

## Declarations

```c
int   x;                      /* global -> .bss, zero-initialised            */
int   y = 7;                  /* global with an initialiser -> emitted inline */
static int z;                 /* same storage; name not exported             */
int   grid[100];              /* array; size must be a constant expression   */
struct point { int x, y; };
struct point origin;
```

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

`/` and `%` on signed operands use the machine's unsigned `DIV`, so results are
only correct for non-negative values. The compiler emits a diagnostic when it
can see a signed division whose operands may be negative; getting this fully
right needs a `__divsi3`-style helper, which is listed in phase 2. *(This
limitation is reasoned, not measured.)*

## Functions

```c
int  add(int a, int b);              /* prototype  */
int  add(int a, int b) { return a + b; }
void clear(void);
int  apply(int (*fn)(int), int v);   /* function pointers: JMP/CALL take a register */
```

Up to 8 parameters. No variadics — `printf` is not in scope, and the display
library takes explicit arguments instead.

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
| varargs | needs a stack-walking convention the frame design does not offer |
| `union`, `enum`, bitfields | phase 2 |
| standard C library beyond memory ops | scope, per the brief |
