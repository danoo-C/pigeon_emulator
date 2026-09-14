# Phase 4: `printf`, a fuller shell, and commands for files

> **Status: plan, nothing built.** The stage after [kernel.md](kernel.md)'s
> phase 3, which built the kernel, its console, a simple shell, and `ls`,
> `cat` and `echo`. It follows kernel.md §17's phase 4 and the decisions in
> [kernel_changes.md](kernel_changes.md) and [shell.md](shell.md), and adds
> what using phase 3 showed is missing. Facts marked *checked* were read in
> the code on 2026-09-14; everything else is reasoned.
>
> **Your questions are in [§9](#9-questions-for-you).** Answer them inline,
> under each one, or write "you decide".

---

## 1. Where things stand

**Built in phase 3:** the kernel boots from the hard disk, starts
`/bin/sh.bin`, and runs programs, with `exit`, faults and Ctrl+C all coming
back to the shell. The console is 32×12, with line input. There are thirteen
system calls, and the shell has `cd`, `exit` and `help`. The calculator, the
cube and the file browser run from `/bin` and return on Esc.

**What's missing:**

- **No `printf`.** Programs print a piece at a time with `print()` and
  `itoa`. The shell reports a status as `say(words[0], ": exit ", number)`.
  The compiler refuses `...` *(checked: `compiler/parser.py:288`)*.
- **The prompt is only the current directory.** shell.md §2's prompt file
  and colors aren't built, and the console has one ink color.
- **No way to manage files from the prompt.** There's `ls` and `cat`, but
  no `mkdir`, `rm`, `cp` or `mv`. There are no system calls for those yet,
  although `fs.c` has every one: `fs_mkdir`, `fs_rmdir`, `fs_remove`,
  `fs_rename` *(checked: `lib/pigeon/fs.h`)*.
- **Ctrl+C pressed while the kernel is busy between programs** waits, then
  ends whichever program runs next (kernel.md Q12, reasoned, not run).
- **Nothing on the machine can edit a file**, so `/etc/shell_header.conf`
  can only be put on the disk from the host.

---

## 2. The goal

When phase 4 is done:

- programs print with `printf("%s: %d files\n", dir, n)`;
- the prompt comes from `/etc/shell_header.conf`, in color;
- you can make, copy, move and delete files and directories at the prompt;
- Ctrl+C only ever stops the program you meant.

---

## 3. Steps

Each step can be tested before the next exists.

### Step 1. Variadic functions in the compiler

Decided in kernel_changes.md Q2: **up to 8 extra word-sized arguments.**

**The frame today** *(checked: `analyzer.py` `_function`, `codegen.py`
`_e_Call`)*. Parameters are at `[F + 0]`, `[F + 4]` and so on, locals right
after them, and `frame_size` is a constant for each function. A caller writes
argument *i* at its own `F + frame_size + 4i`, moves `F` up by `frame_size`,
and calls.

**The change:** a variadic function reserves eight more slots after its
named parameters.

```
F + 0 … F + 4*(n-1)        the n named parameters
F + 4*n … F + 4*(n+7)      eight slots for extra arguments      <- new
F + 4*(n+8) …              locals
```

- **The caller writes an extra argument like any other,** into the slot
  after the last one it wrote. Slots it doesn't use keep whatever was there.
- **A variadic function's `frame_size` counts all eight slots,** so its own
  calls never write over them. Frame sizes stay constants, which the ABI
  depends on.
- **More than 8 extra arguments is a compile error** at the call.
- **An extra argument must be word-sized:** `int`, `unsigned`, a `char`
  (widened), or a pointer. A struct is refused.
- **Calls through a function pointer** to a variadic type work the same way,
  since the cap is a constant.

| File | Change |
|---|---|
| `compiler/parser.py` | stop refusing `...`; record it on the function and its prototype |
| `compiler/typesys.py` | `FunctionType` gains `variadic` |
| `compiler/analyzer.py` | reserve the eight slots; `_x_Call` allows up to 8 more arguments than parameters, each word-sized |
| `compiler/codegen.py` | probably nothing: `_e_Call` already writes argument *i* at `F + frame + 4i` *(to confirm)* |
| `lib/pigeon/stdarg.h` | new, no `.c`: `va_list`, `va_start`, `va_arg`, `va_end` |

**`<pigeon/stdarg.h>` can be plain macros.** A parameter is an lvalue
*(checked: `analyzer.py` `_is_lvalue`)*, so its address can be taken, and
the extra slots follow it:

```c
typedef unsigned *va_list;
#define va_start(ap, last) ((ap) = (unsigned *)&(last) + 1)
#define va_arg(ap, type)   ((type)*(ap)++)
#define va_end(ap)
```

If `*(ap)++` or the cast doesn't compile as intended, `va_arg` becomes a
small function instead.

**Tests,** in `test_compiler.py`:
- a sum over a varying number of arguments;
- `int`, `char` and pointers mixed;
- 0 extras and 8 extras work; 9 is refused, and so is a struct;
- a `va_list` passed on to another function;
- a call through a function pointer;
- recursion through a variadic function;
- the hardware stack balanced;
- a relocatable program that uses one.

### Step 2. `<pigeon/stdio.h>`: `printf` and friends

- **`vsnprintf` and `snprintf`** format into a buffer. They work in any
  program, kernel or not.
- **`printf`, `puts` and `putchar`** write through `write(STDOUT, …)`, so
  they need the kernel (Q4).
- **Formats:** `%d %i %u %x %X %o %c %s %p %%`, the flags `-` and `0`, a
  width, and a precision for `%s`. No floating point: the machine has none.
- **Built on `string.c`'s `utoa`.** Size per program that uses it: a few KB
  *(estimate)*.
- **Then** `sh`, `ls`, `cat` and `echo` use it where it reads better.

**Tests:**
- every conversion, compared with Python's `%` on the same values;
- `snprintf` truncating but returning the full length;
- `printf` through the kernel, read back from the console.

### Step 3. Colors on the console

As shell.md §4 plans: **the console learns the ANSI codes real terminals
use.**

- `ESC [ 3n m` (n from 0 to 7) sets the ink; `ESC [ 0 m` resets it.
  `ESC [ 2 J` clears the screen, which is all `clear` needs.
- **A color for each cell** beside the character grid, 384 bytes. Redrawing
  and scrolling keep the colors.
- **Palette:** black, red, green, yellow, blue, magenta, cyan and white,
  from `display.h`. The default ink stays as it is.
- **An unknown sequence is dropped, not printed.** **A sequence split across
  two `write` calls still works,** since the console keeps its parser state
  between calls.

**Tests:**
- colored text, checked in the framebuffer's pixels;
- colors kept through a scroll;
- `ESC [ 2 J` clears the screen;
- a sequence split across two writes.

### Step 4. The prompt from `/etc/shell_header.conf`

Exactly as shell.md §2 decides:
- **Escapes:** `\n` and `\\`, ``` ``CWD`` ```, ``` ``STATUS`` ```, the color
  names, and ``` ``RESET`` ```.
- **An unknown ``` ``NAME`` ```** is printed as written.
- **A trailing line break and quotes around the whole text** are ignored.
- **Size:** at most 255 bytes.
- **When the file is missing,** the built-in prompt is used.

The shell reads the file when it starts, so `exit` re-reads it. The example
disc gets an `/etc/shell_header.conf` holding shell.md's example.

**Tests:**
- each escape and each color;
- a missing file;
- a file too long;
- ``` ``STATUS`` ``` after a program that failed.

### Step 5. File commands

- **New system calls** in the free slots after `SYS_GETKEY`, since 13 of 32
  are used: `mkdir`, `rmdir`, `remove` and `rename`, each passing straight to
  `fs.c`.
- **New programs in `/bin`:** `mkdir`, `rmdir`, `rm`, `mv`, `cp` (copying
  with `open`, `read` and `write`) and `clear`. Which ones exactly is Q2.

**Tests:** each command checked against `pfs.py`'s view of the disk, with
`fsck` clean afterwards. Also a refusal each: a directory that isn't empty,
a file that doesn't exist, and a copy onto a read-only disc.

### Step 6. Ctrl+C only while a program runs

This closes kernel.md Q12's gap:

- **Break is on only while a program runs.** The kernel turns it on just
  before `exec_call` and off when the program comes back, including through
  `exec_abort`.
- **When a nested program ends, break comes back on**, since its parent is
  still running.
- **While the kernel loads or tidies,** Ctrl+C is an ordinary key, and
  tidying empties the queue.
- **Line input** already turns break off while it waits. It will restore
  break as it found it, instead of turning it on.

**Test:** Ctrl+C pressed while the kernel loads a large program must not end
that program. Pressing it at exactly the right moment needs care (§7).

### Step 7. Docs and the example disc

- **Docs:**
  - kernel.md §17 and §18;
  - shell.md and kernel_changes.md, for what's built;
  - `lib/README.md`, for `stdarg` and `stdio`;
  - `compiler/README.md` and `compiler/design/03-abi.md`, for the variadic
    frame;
  - README.
- **The example disc:** the new programs and `/etc/shell_header.conf`.
- **The install test:** checks the colored prompt appears.

---

## 4. Order and size

| Step | Needs | Size | Mostly in |
|---|---|---|---|
| 1. Variadic functions | — | medium | the compiler |
| 2. `stdio` | 1 | medium | `lib/pigeon/` |
| 3. Console colors | — | small | `kernel.c` |
| 4. The prompt file | 2, 3 | small | `sh.c` |
| 5. File commands | 2 | small to medium | `kernel.c`, `sys.c`, `user/os/bin/` |
| 6. Ctrl+C timing | — | small | `kernel.c`, `kernel.asm` |
| 7. Docs, disc | all | small | docs |

Steps 3 and 6 don't depend on the compiler, so they could go first if you
want something visible sooner.

---

## 5. How it will be checked

As in phases 1–3:
- each step's tests;
- deliberate breakages for every piece, each of which must fail a test;
- the full suite at the end;
- the example disc installed and booted in `test_install.py`.

---

## 6. Not in phase 4

- **`ls > file`:** redirection waits, as kernel_changes.md Q6 decided.
- **Pipes:** they wait for multitasking, or never come.
- **Multitasking** (kernel.md §14): still optional, and still needs a memory
  allocator, an IO rule and waiting.
- **Memory protection:** out of reach on this CPU.
- **Scrollback** (Q6), **line editing** (Q8) and **the launcher step**
  (Q7): unless you want them in.

---

## 7. Risks, and what to check first

- **Whether `va_arg` compiles as macros** (step 1): `*(ap)++` on a pointer,
  and a cast from a word to any type.
- **`printf` in every program that uses it.** There is no shared library, so
  each program carries its own copy, a few KB each.
- **An escape sequence split between two `write` calls** (step 3): the
  console has to keep its parser state between calls.
- **Testing step 6** needs Ctrl+C to arrive while the kernel is busy,
  without depending on host speed. One way: a program that runs a large
  child, with the test pressing Ctrl+C once the child's load has started. How
  to detect that moment is still open.
- **A variadic frame is 32 bytes bigger.** Deep recursion through a variadic
  function uses the frame stack faster *(reasoned; 256 KB is about 8,000
  frames)*.

---

## 8. Files

| File | Steps |
|---|---|
| `compiler/parser.py`, `typesys.py`, `analyzer.py`, maybe `codegen.py` | 1 |
| `lib/pigeon/stdarg.h` (new) | 1 |
| `lib/pigeon/stdio.h`, `stdio.c` (new) | 2 |
| `user/os/kernel.c` | 3, 5, 6 |
| `user/os/kernel.asm` | 5, 6 |
| `lib/pigeon/syscall.h`, `sys.h`, `sys.c` | 5 |
| `user/os/bin/sh.c`, `ls.c`, `cat.c`, `echo.c` | 2, 4 |
| `user/os/bin/mkdir.c`, `rmdir.c`, `rm.c`, `mv.c`, `cp.c`, `clear.c` (new) | 5 |
| `user/os/pigeon_compiler_init.txt`, `user/os/etc/shell_header.conf` (new) | 4, 5 |
| `tests/test_compiler.py`, `test_stdio.py` (new), `test_kernel.py`, `test_install.py` | all |
| `docs/…`, `lib/README.md`, `compiler/README.md`, README | 7 |

---

## 9. Questions for you

Answer under each one, or write "you decide".

1. **Box-drawing characters** for a Kali-style prompt (`┌ └ ─ │ ├`)? That's
   five glyphs added to the font, and UTF-8 in the console. *My suggestion:*
   later; `|-` works today.

   Answer:

2. **Which file commands?** `mkdir`, `rmdir`, `rm`, `mv`, `cp` and `clear`
   are in the plan. Also `more` (page long output), `edit` (a text editor),
   `pwd`, `mem` (free memory) or `ver`? *My suggestion:* the six planned
   ones now.

   Answer:

3. **A text editor on the machine?** `user/files.c` already has a small note
   editor. A standalone `edit FILE` would let you change
   `/etc/shell_header.conf` without the host. Now, or later?

   Answer:

4. **`printf` without the kernel.** A program started from channel 1 has no
   console. Should `printf` there draw text on the screen itself, or are
   `snprintf` and `disp_text` enough? *My suggestion:* `snprintf` only;
   `printf` needs the kernel.

   Answer:

5. **`ls`:** names only, as now, or also `ls -l` with sizes? And sorted by
   name?

   Answer:

6. **Paging:** kernel_changes.md Q5 decided no `-- more --` for now, so long
   output scrolls off the 12 rows. Keep it that way, or add `more`?

   Answer:

7. **The launcher** (kernel.md phase 6): for example
   `python3 start_emulator.py pigeonos`, which rebuilds the disc if it's out
   of date and boots the installed disk, installing it first if needed. In
   phase 4, or a phase of its own?

   Answer:

8. **Line editing at the prompt:** Up for the previous command, Left and
   Right to move within the line. Now, or later?

   Answer:

9. **Anything you missed while using it?** Tab completion, `dir` as another
   name for `ls`, a startup script run at boot…

   Answer:
