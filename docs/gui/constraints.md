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

## 1. Three silent miscompiles

**These compile without a warning and produce wrong values.** They are not
"unsupported" — the compiler accepts them and emits code that does the wrong
thing, so nothing tells you. Every one was confirmed by running the program
on the emulator and reading `A`:

| written | expected | actually returns |
|---|---|---|
| `struct P y; y = x;` then read `y.c` | 3 | **9** — the old value; only the first word is copied |
| `int f(struct P p)` called as `f(q)`, read `p.c` | 3 | **0** |
| `struct P mk(void)` returning a struct, read `.c` | 3 | **0** |
| `int a = 2*3+1;` at file scope | 7 | **0** |
| `int a = -1;` at file scope | −1 | **0** |

The struct cases are one bug: there is no struct copy anywhere in codegen, so
assignment, by-value arguments and by-value returns all move exactly four
bytes *(checked: `codegen.py:697-712`, `607-612`)*. The initialiser case is
another: only a bare non-negative integer literal survives; everything else —
a constant expression, a negative number, `&something`, `sizeof` — is
silently dropped to zero *(checked: `codegen.py:194-200`)*.

**The rules that follow, and they are absolute:**

1. **Never pass, assign or return a struct by value.** Every function takes
   `struct T *`. Every library in the tree already does this — `math.h`'s
   `vec3 *`, `bmp.c`'s `bmp_header *out`, `fs.c` throughout *(checked)* — and
   this is why.
2. **Never initialise a global with anything but a plain literal.** Fill it
   in an `init()` function. For the GUI this decides the whole theme design:
   a `static gui_theme pigeon = {...}` table is impossible twice over, so the
   palette is **filled at run time** ([theme.md](theme.md)).

> These are worth fixing in the compiler regardless of this library — a
> rejected program is a nuisance, a silently wrong one is a trap. It is not
> part of the GUI plan, but it belongs on the list.

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
3. **`/` and `%` are the machine's unsigned divide**, correct only for
   non-negative operands *(checked: `codegen.py:512-517`)*. **This lands
   directly on scaling**, which is all multiply-then-divide — so the scaling
   path keeps coordinates non-negative and does the sign separately
   ([scaling.md](scaling.md)).
4. **`char` is signed but never sign-extended on load** *(checked:
   `codegen.py:414-415`)*. Use `unsigned char` for bytes, `int` for anything
   that can go negative.
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
