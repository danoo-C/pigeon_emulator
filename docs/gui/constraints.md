# What the machine and the compiler allow

> Part of [the GUI plan](README.md). **Status: findings, 2026-09-20.** Every
> row here was **run**, not read: the compiler probes were compiled with
> `compiler.cc.compile_to_asm` and the ones that compile were then *executed
> on the emulator* and their return value checked. Facts marked *(checked)*
> were read in the code.

A GUI library is mostly an API, and an API written against a C its compiler
does not have is worthless. This file is the ground it stands on. Read it
before [api.md](api.md); it is why that header looks the way it does.

---

## 1. The silent miscompiles — **fixed, 2026-09-20**

**These used to compile without a warning and produce wrong values**, which
is why much of this library's shape was a way of avoiding them. Every one was
found by running the program on the emulator and reading `A`, and every one
is now either correct or a diagnostic
([compiler_plan.md](../compiler_plan.md)):

| written | expected | was | now |
|---|---|---|---|
| `struct P y; y = x;` then read `y.c` | 3 | **9** — only the first word was copied | **3**, every word copied |
| `int f(struct P p)` called as `f(q)` | 3 | **0** | *refused:* take a `struct P *` |
| `struct P mk(void)` returning a struct | 3 | **0** | *refused:* fill one through a pointer |
| `int a = 2*3+1;` at file scope | 7 | **0** | **7** |
| `int a = -1;` at file scope | −1 | **0** | **−1** |
| `int a = sizeof(struct P);` at file scope | 8 | **0** | **8** |
| `int b = a;` at file scope | — | **0** | *refused:* not a constant |
| `int v = -20; v / 2` | −10 | **2147483638** | **−10** |
| `int a = -7; a % 3` | −1 | **0** | **−1** |
| `int a = -8; a >> 1` | −4 | **2147483644** | **−4** |

**What this means for the code you write here:**

1. **A struct may be assigned; it still may not cross a call.** `y = x;`
   copies every word, and so does `struct P y = x;`. A by-value parameter or
   return is a diagnostic naming the fix, because a parameter slot is one
   word and a return comes back in `A`. Every function in this library still
   takes `struct T *`, which is house style and what `math.h`, `bmp.c` and
   `fs.c` already do *(checked)*.
2. **A global may be initialised with any constant** — a folded expression,
   `sizeof`, a cast, `?:`, or an address such as `&thing`, an array's name or
   a function's. A **table of function pointers at file scope now works**,
   where before every entry was silently zero. What is still impossible is a
   brace initialiser on a *struct* (§2), so the theme is still filled at run
   time ([theme.md](theme.md)) — for that reason alone, not this one.
3. **Signed `/`, `%` and `>>` are correct**, so [scaling.md](scaling.md)'s
   arithmetic no longer has to keep its coordinates non-negative. It still
   does, because the unsigned path skips the sign-fixing helper and is
   smaller and faster — but it is now an optimisation rather than a
   correctness rule.

Two more went the same way on the same day: **`char` now sign-extends when
it is loaded**, so `char c = -1; (int)c` is −1 rather than 255 and
`(char)200` is −56; and **`char *p = "hi";` at file scope is a pointer**
rather than the 3-byte array it silently became. `unsigned char` is
untouched, and is still the right type for a byte.

---

## 2. What is not there at all

Each one errors cleanly, which is the good case:

| missing | the error | what to write instead |
|---|---|---|
| **Self-referential structs** — `struct W { struct W *parent; }` | `unknown struct tag 'W'` | an `int` index into a pool |
| Forward declaration — `struct W;` | same | — |
| `typedef struct W W;` before the body | same | — |
| `enum` | `expected a type, got 'enum'` | `#define GUI_KIND_BUTTON 2` |
| `switch` / `case` | `switch is not supported yet; use if/else` | an `if`-chain, as `graphics.c` does |
| `union`, bit-fields | refused | separate members |
| **`static` locals** | `static locals are not supported yet` | a file-scope static |
| Brace initialiser on a **local** | `brace initialisers work on globals only` | assign in code |
| Brace initialiser on a **struct** | `a brace initialiser needs an array` | assign in code |
| Designated initialisers `.x = 1` | parse error | assign in code |
| `int (*tbl[4])(int);` inline | `expected ')', got '['` | `typedef int (*cb)(int); cb tbl[4];` |
| `goto` | `goto is not supported` | — |
| `#if` / `#elif`, `#`, `##` | `unsupported directive` | `#ifdef` / `#ifndef` only |
| Floating point | rejected in the lexer | fixed point, as `math.c` does |
| Adjacent string literals `"a" "b"` | parse error | one literal |

**The first row is the one that shapes everything.** A widget tree cannot be
built from pointers, because a struct cannot mention itself. The fix is the
one `lib/pigeon/fs.c` already uses for its block chains: **hold the
relationship as an `int` index into a global array** *(checked)*. That turns
out to suit this machine well — see [design.md §2](design.md).

---

## 3. What works, and is load-bearing

| feature | verified |
|---|---|
| **Function pointers as struct members, called via `.` and `->`** | ✅ returns 3 |
| **`typedef`'d function-pointer arrays, assigned at run time** | ✅ returns 7 |
| Function pointers as locals, parameters, globals, return values; cast from an integer address | ✅ *(the whole syscall layer is this — `sys.c:13-88`)* |
| `struct` with scalar and fixed-array members; arrays of structs; `t[i].m` | ✅ |
| `struct *`, `->`, chained `a->b->c`, pointer arithmetic | ✅ |
| File-scope `static int t[] = {1,2,3};` and `char *names[] = {"a","bb"};` | ✅ *(`math.c:88` does it)* |
| Variadic functions | ✅ **max 8 extra arguments**, each a word *(checked: `typesys.py:128`)* |
| `for`/`while`/`do`/`break`/`continue`/`?:`/`++`/compound assignment/`&&`/`||` | ✅ |
| Function-like macros, `#ifdef`, `#pragma once` | ✅ |

**Function pointers surviving in full is what makes a tkinter-shaped API
possible at all.** Handlers stored on a widget and called through `->` are
exactly the thing, and they work.

---

## 4. Sharp edges that change the code you write

1. **One translation unit — `static` does not hide anything.** Every source
   file is preprocessed and concatenated before a single parse *(checked:
   `cc.py:115-140`)*, so two files with `static int helper(void)` collide
   with `'helper' is defined twice`. **Every name in this library is prefixed
   `gui_`**, internal ones too. That is why `gac_`, `__fs_`, `bmp_` and
   `__stdio_` exist.
2. **Locals are never zero-initialised** — the frame stack is reused
   *(checked)*. `gui_init()` clears the pool explicitly; a local `gui_event`
   is filled field by field.
3. **`/`, `%` and `>>` are correct on signed operands**, as of 2026-09-20:
   the compiler calls a `__divsi3`/`__modsi3` helper and shifts the sign in
   *(checked: `codegen.py`, `_arithmetic_shift`)*. **Unsigned operands skip
   all of that**, which is why [scaling.md](scaling.md) still casts to
   `unsigned` before it multiplies and divides — smaller and faster, not
   safer. `<pigeon/math.h>`'s `idiv`, `imod` and `ishr` are still there and
   still answer 0 for a zero divisor, where a bare `/` faults.
4. **`char` is signed and sign-extends on load**, as of 2026-09-20 — two
   instructions after the byte load, and none at all for `unsigned char`
   *(checked: `codegen.py`, `_load`)*. `unsigned char` is still the right
   type for a byte, because it says what it means and costs less.
5. **`short`, `long` and `long long` are all silently 32-bit `int`**
   *(checked: `parser.py:169-170`)*. They promise something that is not
   delivered, so they do not appear in this library's header.
6. **The frame stack is 256 KB with no overflow check** *(checked:
   `codegen.py:23`)* — an overflow walks into the malloc heap in silence.
   A recursive widget-tree walk is therefore a genuine hazard, and the pool
   design avoids it: **traversal is a loop over an array, never recursion**.
7. **Array sizes must be compile-time constants** — no `sizeof` inside them,
   and no run-time-sized arrays. Pools are fixed capacity or `malloc`ed.
8. `const` and `volatile` parse and are discarded — fine as documentation,
   they enforce nothing.

---

## 5. Memory

- **`malloc` / `calloc` / `free` exist** *(checked: `lib/pigeon/mem.h`)*, with
  a per-program ceiling the kernel writes into `__heap_limit`
  *(checked: `mem.c:94-99`)*. So a widget pool can be heap-allocated and does
  not have to be a static array.
- **Static data shares 1 MB with code** — `PROGRAM_MAX_SIZE` *(checked:
  `memory_map.py:125`)*. A 256-widget static pool at ~80 bytes each is 20 KB,
  which is affordable, but it is charged to the same budget as the program.
- Therefore: **the pool is `malloc`ed in `gui_init()`**, keeping it off the
  static budget and letting its size be an argument rather than a `#define`
  that a one-translation-unit build makes awkward to override.

---

## 6. What the machine gives the GUI

From the input audit *(checked, `emulator/devices/hid.py`,
`lib/pigeon/input.h`)*:

| need | exists? |
|---|---|
| Mouse position, buttons, wheel (as buttons 5/6) | **yes** — `mouse_x()`, `mouse_y()`, `mouse_buttons()`, `mouse_event()` |
| Hover / enter / leave / motion **events** | **no** — every UI polls the position and diffs it |
| Modifier flags on an event | **no** — latch Shift/Ctrl from key edges, as `edit.c` does |
| A drawn mouse cursor, anywhere | **no** — you are seeing the host pointer |
| Double-click, timestamps | **no** |
| A mouse system call | **no** — programs talk to `CH_HID` directly |

Three consequences the library must own:

- **`mouse_x()` and `mouse_y()` are two separate bus transactions**
  *(checked: `input.c:35-36`)*, so the position can change between them.
  The library reads them **once a frame into a snapshot** and everything
  else uses that.
- **Hover, enter, leave and drag are synthesized** by the library from the
  polled position. That is a large part of what it is for.
- **The pointer position goes stale across a mode change** until the next
  mouse move *(checked: HID has no notion of the screen)*, which matters
  precisely when [scaling.md](scaling.md) is doing its work.
