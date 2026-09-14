# Phase 4: `printf`, a fuller shell, and tools for working with files

> **Status: final plan, 2026-09-15. Nothing built.** The stage after
> [kernel.md](kernel.md)'s phase 3, which built the kernel, its console, a
> simple shell, and `ls`, `cat` and `echo`. It follows kernel.md §17's
> phase 4 and the decisions in [kernel_changes.md](kernel_changes.md) and
> [shell.md](shell.md). Facts marked *checked* were read in the code; the
> rest is reasoned.
>
> **Every question is decided.** §9 and §10 keep your answers, with the
> replies to your counter-questions. The phases after this one, including
> the boot screen, the startup script and the serial debug port, are
> sketched in [§11](#11-later-phases).

---

## 1. Where things stand

**Built in phase 3:** the kernel boots from the hard disk, starts
`/bin/sh.bin`, and runs programs, with `exit`, faults and Ctrl+C all coming
back to the shell. The console is 32×12, with line input. There are thirteen
system calls, and the shell has `cd`, `exit` and `help`. The calculator, the
cube and the file browser run from `/bin` and return on Esc.

**What's missing:**

- **No `printf`.** Programs print a piece at a time with `print()` and
  `itoa`. The compiler refuses `...` *(checked: `compiler/parser.py:288`)*.
- **The prompt is only the current directory,** and the console has one ink
  color and no way to move its cursor.
- **No way to manage files from the prompt.** There's no `mkdir`, `rm`,
  `cp` or `mv`, and no system calls for them, although `fs.c` has every one
  *(checked: `lib/pigeon/fs.h`)*.
- **Line input only knows Backspace and Enter.** There's no history, and
  anything that scrolls off the top is gone.
- **Nothing pages long output,** and nothing on the machine can edit a file.
- **Ctrl+C pressed while the kernel is busy between programs** waits, then
  ends whichever program runs next (kernel.md Q12, reasoned, not run).

---

## 2. The goal

When phase 4 is done:

- programs print with `printf("%s: %d files\n", dir, n)`;
- the prompt comes from `/etc/shell_header.conf`, in color;
- you make, copy, move and delete files at the prompt, and `ls` is sorted,
  with `ls -l` for sizes;
- `edit FILE` edits a file on the machine, the way nano does;
- `more` pages a long file, or a command's output;
- line input has arrow keys and history, and PgUp, PgDn or the mouse wheel
  scroll back through what scrolled off;
- Ctrl+C only ever stops the program you meant.

---

## 3. Steps

Each step can be tested before the next exists. §4 groups them into two
halves, each committed on its own.

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
  they need the kernel.
- **With no kernel, `printf` returns -1 and prints nothing** (F1). It knows
  because the system-call slot is 0, so it never calls through the empty
  table to address 0. A program with no kernel formats with `snprintf` and
  draws the result where it chooses, with `disp_text`. The serial debug
  port will give it somewhere to print later (§11).
- **Formats:** `%d %i %u %x %X %o %c %s %p %%`, the flags `-` and `0`, a
  width, and a precision for `%s`. No floating point: the machine has none.
- **Built on `string.c`'s `utoa`.** Size per program that uses it: a few KB
  *(estimate)*.

**Tests:**
- every conversion, compared with Python's `%` on the same values;
- `snprintf` truncating but returning the full length;
- `printf` through the kernel, read back from the console;
- `printf` with no kernel returning -1.

### Step 3. The console becomes a small terminal

**Colors first**, as shell.md §4 plans. Then **the few codes a full-screen
program needs**, so `edit` and `more` draw through the console like terminal
programs, rather than bundling the display library and fighting the kernel
over the screen.

| Sequence | Does |
|---|---|
| `ESC [ 3n m` | ink n, from 0 to 7: black, red, green, yellow, blue, magenta, cyan, white |
| `ESC [ 7 m` | inverse, for a block cursor, a title bar or a shortcut bar |
| `ESC [ 0 m` | back to normal |
| `ESC [ 2 J` | clear the screen and put the cursor home: all `clear` needs |
| `ESC [ r ; c H` | move the cursor to row r, column c, counting from 1 as terminals do |
| `ESC [ K` | clear from the cursor to the end of the line |

- **A color and an inverse bit for each cell** beside the character grid,
  384 bytes. Redrawing and scrolling keep them.
- **An unknown sequence is dropped, not printed.** **A sequence split across
  two `write` calls still works,** since the parser's state lives in the
  kernel between calls.

**Tests:**
- colored and inverse text, checked in the framebuffer's pixels;
- colors kept through a scroll;
- each code;
- a sequence split across two writes;
- a sequence that isn't one of these, dropped.

### Step 4. Line editing, history and scrollback

All of it lives in the kernel's console, so every program that reads a line
gets it, not just the shell.

- **Editing:** Left and Right, Home and End, Backspace and Delete, and
  typing anywhere in the line.
- **History:** Up and Down step through the last 16 lines typed. That's
  16 × 256 bytes, 4 KB, in the kernel, shared by every program that reads
  lines.
- **Scrollback:** the console keeps the last 100 lines that scrolled off,
  3.2 KB.
  - While it waits for a line, or in `more`, PgUp, PgDn and the mouse wheel
    move the view.
  - Typing, or new output, brings the view back to the bottom.
  - While a program runs, the program owns the keys and the screen.
- **The mouse wheel is new to the machine.**
  - **Today no front end sends it.** The pygame client drops wheel events
    *(checked: `display/display.py:130–131`)*, and the browser page has no
    wheel handler *(checked: `display/index.html`)*.
  - **HID has room for it:** its mouse-event byte holds a button number up to
    31, and 5 and up are unused *(checked: `emulator/devices/hid.py`)*.
  - **The plan:** one notch up is a press and a release of button 5, and one
    notch down the same for button 6. Both front ends send them, and
    `input.h` names them `MB_WHEEL_UP` and `MB_WHEEL_DOWN`.

**Tests:**
- each editing key;
- history up, down and past either end;
- scrollback with PgUp and PgDn, and the view coming back on new output;
- wheel events through HID and through `input.c`;
- the browser page sending them, checked with the JS test in `test_input.py`;
- the pygame client's wheel mapping.

### Step 5. The prompt from `/etc/shell_header.conf`

Exactly as shell.md §2 decides:
- **Escapes:** `\n` and `\\`, ``` ``CWD`` ```, ``` ``STATUS`` ```, the color
  names, and ``` ``RESET`` ```.
- **An unknown ``` ``NAME`` ```** is printed as written.
- **A trailing line break and quotes around the whole text** are ignored.
- **Size:** at most 255 bytes.
- **When the file is missing,** the built-in prompt is used.

The shell turns a color name into step 3's code. It reads the file when it
starts, so after saving it in `edit`, `exit` shows the new prompt. The
example disc gets an `/etc/shell_header.conf` holding shell.md's example.

**Tests:**
- each escape and each color;
- a missing file;
- a file too long;
- ``` ``STATUS`` ``` after a program that failed.

### Step 6. File commands

- **New system calls,** in the free slots after `SYS_GETKEY` (13 of 32 are
  used): `mkdir`, `rmdir`, `remove` and `rename`, each passing straight to
  `fs.c`.
- **New programs in `/bin`:**
  - `mkdir`, `rmdir`, `rm` and `mv`;
  - `cp`, which copies with `open`, `read` and `write`;
  - `clear`, which prints `ESC [ 2 J`.

**Tests:**
- each command, checked against `pfs.py`'s view of the disk, with `fsck`
  clean afterwards;
- a refusal each: a directory that isn't empty, a file that doesn't exist,
  and a copy onto a read-only disc.

### Step 7. A more comfortable `ls`

- **Sorted by name**, whatever order the directory holds them in.
  Directories still end in `/`.
- **`ls -l`** adds a size column, right-aligned, with `<dir>` for a
  directory.
- **Several directories named** are listed one after another, as now.
- **Written with `printf`.** A directory of up to 256 entries is sorted in
  memory; past that, the rest follow unsorted *(a limit to revisit)*.

**Tests:** sorted order; `-l` sizes against `pfs.py`; a file and a directory
in the same listing.

### Step 8. `more`

**Paging lives in the kernel's console, not only in `more`.** There are no
pipes, so a program by itself could only page a file, never another program's
output (F2).

- **`more FILE…`** shows files a screen at a time.
- **`more COMMAND ARGS…`** runs a program with its output paged, as in
  `more ls /bin`.
  - **Which one:** if the first word names an existing file, it's paged as a
    file; otherwise it's looked up and run as a command, as the shell would.
  - **How:** `more` turns paging on with a new system call, `paging(on)`,
    then runs the command with `exec`. The kernel turns paging off when
    `more` ends.
- **When the screen fills,** `-- more --` shows on the last row, and the
  console waits there, inside `write`:
  - Space shows the next screen, and Enter the next line;
  - PgUp and the wheel look back;
  - `q` stops. For a file, `more` ends. For a command, the command ends the
    way `exit()` ends a program, from inside its `write`.

**Tests:**
- a long file paged with Space, Enter and `q`;
- `more ls /bin` with more entries than fit;
- `q` ending the command, and the shell carrying on;
- a word that is a file taken as a file, and one that isn't taken as a
  command.

### Step 9. `edit FILE`: a mini nano

A full-screen editor that looks and behaves like a small nano, drawing
through the console with step 3's codes.

```
row 0    edit  shell_header.conf  Modified     title bar, inverse
rows 1-9 the text                              9 rows
row 10   [ Wrote 3 lines ]                     messages and questions
row 11   ^O Save ^X Exit ^K Cut ^W Find        shortcuts, keys inverse
```

**Keys,** nano's where nano has one:

| Key | Does |
|---|---|
| arrows, Home, End | move |
| PgUp and PgDn, or ^Y and ^V | a screen up or down |
| Backspace, Delete | delete before, or at, the cursor |
| Enter | split the line |
| Tab | spaces to the next multiple of 4 |
| ^O, or ^S | write the file under its name: `[ Wrote N lines ]` |
| ^X | exit. With unsaved changes it asks `Save modified buffer? Y N`: Y saves and exits, N exits, ^C stays |
| ^K | cut the current line; several ^K in a row gather the lines |
| ^U | paste what was cut, above the cursor's line |
| ^W | find: type the text and Enter searches forward, wrapping at the end; ^C cancels |
| ^C | show the line and column in the message row |
| ^G | a screen listing these keys; any key comes back |

- **Opening:** a file that doesn't exist says `[ New File ]` and is created
  on the first save. A file over 64 KB, or holding a zero byte, is refused.
  With no name, it prints how to use it.
- **Long lines** scroll sideways as the cursor moves along them.
- **Ctrl, and Ctrl+C:** `edit` reads key events itself, with `input.h`,
  tracking Ctrl as the console's line input does. It turns break off with a
  new system call, `setbreak(on)`, so Ctrl+C is nano's ^C and can't throw
  work away. The kernel turns break back on when `edit` ends, however it
  ends.
- **Not grown from `files.c`.** Its note editor only appends to a new
  512-byte note, with no cursor movement *(checked: `user/files.c:99`,
  `:845–870`)*.

**Tests:**
- open a file, move, type in the middle, save with ^O, and check the bytes
  with `pfs.py`;
- ^X with unsaved changes: Y, N and ^C;
- ^K several times, then ^U;
- ^W found, not found, and wrapping;
- `[ New File ]` created on save;
- a long line scrolling sideways;
- ^C showing the position without ending `edit`;
- ^G and back.

### Step 10. Ctrl+C only while a program runs

This closes kernel.md Q12's gap:

- **Break is on only while a program runs.** The kernel turns it on just
  before `exec_call`, and off when the program comes back, including through
  `exec_abort`.
- **When a nested program ends, break comes back on,** since its parent is
  still running, unless the parent had turned it off with `setbreak`.
- **While the kernel loads or tidies,** Ctrl+C is an ordinary key, and
  tidying empties the queue.
- **Line input** already turns break off while it waits. It will restore
  break as it found it, instead of turning it on.

**Test:** Ctrl+C pressed while the kernel is loading a large program must
not end that program. Pressing it at the right moment needs care (§7).

### Step 11. Docs and the example disc

- **Docs:**
  - kernel.md §17 and §18, with §17's list numbered to match §11's phases;
  - kernel_overview.md's build order;
  - shell.md and kernel_changes.md, for what's built;
  - `lib/README.md`, for `stdarg`, `stdio` and the new calls;
  - `compiler/README.md` and `compiler/design/03-abi.md`, for the variadic
    frame;
  - README.
- **The example disc:** the new programs and `/etc/shell_header.conf`.
- **The install test:** checks the colored prompt appears.

---

## 4. Order and size

**Two halves, each committed on its own** (F3):

| Half | Steps | What you get |
|---|---|---|
| **4a** | 1, 2, 3, 5, 6, 7, 10 | `printf`, colors, the prompt file, file commands, a better `ls`, Ctrl+C fixed |
| **4b** | 4, 8, 9 | line editing, history, scrollback and the wheel; `more`; `edit` |

Step 11 comes at the end of each half.

| Step | Needs | Size |
|---|---|---|
| 1. Variadic functions | — | medium |
| 2. `stdio` | 1 | medium |
| 3. The console as a terminal | — | medium |
| 4. Line editing, history, scrollback, wheel | 3 | medium; touches both front ends |
| 5. The prompt file | 2, 3 | small |
| 6. File commands | 2 | small to medium |
| 7. `ls` | 2 | small |
| 8. `more` | 3, 4 | medium |
| 9. `edit` | 2, 3 | large: the biggest program yet |
| 10. Ctrl+C timing | — | small |
| 11. Docs, disc | all | small |

---

## 5. How it will be checked

As in phases 1–3:
- each step's tests;
- deliberate breakages for every piece, each of which must fail a test;
- the full suite at the end of each half;
- the example disc installed and booted in `test_install.py`.

---

## 6. Not in phase 4

- **Redirection, `ls > file`:** later, as kernel_changes.md Q6 decided.
- **Pipes:** they wait for multitasking, or never come.
- **Multitasking** (kernel.md §14): still optional.
- **Memory protection:** out of reach on this CPU.
- **Box-drawing characters** (Q1), and **`pwd`, `mem` and `ver`** (Q2).
- **The boot screen and the startup script** (Q9), **the serial debug
  port** (F1) and **the launcher** (Q7): the phases after this one (§11).

---

## 7. Risks, and what to check first

- **Whether `va_arg` compiles as macros** (step 1): `*(ap)++` on a pointer,
  and a cast from a word to any type.
- **`printf` in every program that uses it.** There's no shared library, so
  each program carries its own copy, a few KB each.
- **The console's escape parser** (step 3) keeps state between `write`
  calls, and must never print half a sequence.
- **The wheel touches both front ends** (step 4), each with its own input
  code, and the browser's key table is already checked against Python by a
  test. The wheel mapping needs the same.
- **`more COMMAND` waits inside `write`** (step 8), with the kernel running
  and interrupts off, and `q` ends the command from there. That's the path
  `exit()` already takes, from inside a system call.
- **`edit` is the largest program yet** (step 9). Its cursor, sideways
  scrolling, cut and paste, and saving need the most tests.
- **The kernel image grows** by about 8 KB of history, scrollback and cell
  colors *(estimate)*, still far below the kernel's 1 MB.
- **Testing step 10** needs Ctrl+C to arrive while the kernel is busy,
  without depending on host speed. One way: a program that runs a large
  child, with the test pressing Ctrl+C once the child's load has begun. How
  to detect that moment is still open.

---

## 8. Files

| File | Steps |
|---|---|
| `compiler/parser.py`, `typesys.py`, `analyzer.py`, maybe `codegen.py` | 1 |
| `lib/pigeon/stdarg.h`, `stdio.h`, `stdio.c` (new) | 1, 2 |
| `user/os/kernel.c` | 3, 4, 6, 8, 9, 10 |
| `user/os/kernel.asm` | 6, 8, 9, 10 |
| `lib/pigeon/syscall.h`, `sys.h`, `sys.c` | 6, 8, 9 |
| `emulator/devices/hid.py`, `lib/pigeon/input.h`, `display/display.py`, `display/index.html` | 4 |
| `user/os/bin/sh.c`, `ls.c`, `cat.c`, `echo.c` | 2, 5, 7 |
| `user/os/bin/mkdir.c`, `rmdir.c`, `rm.c`, `mv.c`, `cp.c`, `clear.c`, `more.c`, `edit.c` (new) | 6, 8, 9 |
| `user/os/pigeon_compiler_init.txt`, `user/os/etc/shell_header.conf` (new) | 5, 11 |
| `tests/test_compiler.py`, `test_stdio.py` (new), `test_kernel.py`, `test_edit.py` (new), `test_input.py`, `test_install.py` | all |
| `docs/…`, `lib/README.md`, `compiler/README.md`, README | 11 |

---

## 9. Your answers

1. ~~**Box-drawing characters** for a Kali-style prompt (`┌ └ ─ │ ├`)?~~

   Answer: add that later

   **Reply:** Later, then (§11). The prompt file works with `|-` until
   then.

2. ~~**Which file commands?** `mkdir`, `rmdir`, `rm`, `mv`, `cp` and `clear`
   are in the plan. Also `more`, `edit`, `pwd`, `mem` or `ver`?~~

   Answer:  add the more and the text aditor. add the other ones later

   **Reply:** The six planned commands stay (step 6), `more` and `edit`
   come in (steps 8 and 9), and `pwd`, `mem` and `ver` wait (§11).

3. ~~**A text editor on the machine?**~~

   Answer: now

   **Reply:** Step 9, a mini nano (F4). It's a new program, since `files.c`'s
   note editor can only append to a new 512-byte note.

4. ~~**`printf` without the kernel.**~~

   Answer: imean where would printf print? it doesnt make sense if there is no cursor, tell me ideas how could it work without beeing sketchy

   **Reply:** Right: without the kernel there's no cursor and no screen of
   text, so a `printf` that drew somewhere would have to guess. Three ideas
   that don't guess:

   - **A. `printf` needs the kernel.** Without one it returns -1 and prints
     nothing, rather than calling through the empty table to address 0.
     `snprintf` works everywhere, and a program with no kernel draws the
     string where it chooses, with `disp_text`, as bios2 and the installer
     already do.
   - **B. A debug port.** A new write-only device on IO channel 8, which is
     free, whose bytes appear outside the machine, like QEMU's debug port or
     a serial line. `printf` with no kernel writes there. It isn't on the
     screen, so there's no cursor to fake.
   - **C. The console as a library,** bundled into every program. Every such
     program would carry the console and the display library, and a program
     started by the kernel would have two consoles fighting over the
     screen. Not recommended.

   Decided in F1: A now, B in a later phase.

5. ~~**`ls`:** names only, or also `ls -l` with sizes? And sorted?~~

   Answer: yeah why not, lats make ls more comfortable

   **Reply:** Step 7: sorted by name, `ls -l` with sizes, directories still
   marked with `/`.

6. ~~**Paging:** keep no `-- more --`, or add `more`?~~

   Answer: add more

   **Reply:** Step 8, with paging in the kernel's console as well as in a
   `more` program. There are no pipes, so `more` by itself could page a file
   but never `ls /bin`. Decided in F2: it does both.

7. ~~**The launcher** (kernel.md phase 6): in phase 4, or a phase of its
   own?~~

   Answer: i dont care

   **Reply, decided (left to me):** a phase of its own, after the debug
   port (§11). Phase 4 has grown, and the launcher is host-side work that
   doesn't touch the machine.

8. ~~**Line editing at the prompt:** now, or later?~~

   Answer:now, also could we do history and scrolling with the scrollwheel?

   **Reply:** Yes to both, in step 4.
   - **History:** Up and Down through the last 16 lines, kept by the
     kernel's console, so every program that reads a line gets it.
   - **Scrolling back:** the console keeps the last 100 lines that scrolled
     off, and PgUp, PgDn or the wheel move the view while it waits for a
     line, or in `more`.
   - **The wheel is the one new piece of hardware.** Neither front end sends
     it today, and HID has no code for it, but HID's event byte has room: a
     notch becomes a press of button 5 or 6. Both front ends learn to send
     it.

9. ~~**Anything you missed while using it?**~~

   Answer: i mean, i would like startup script option, and i like ls more than dir, so stick with ls. also i would like an option for a bootscreen, when the kernel finds etc/boot.conf with loading_graphics = /bin/bootscreen.bin it will load that? is that possible? also we should put the bootscreen script into another phaze to not have too much work for this phaze, so just document the bootscreen and startup program into another phaze.

   **Reply:** `ls` it is, with no `dir`. The boot screen and the startup
   script are **both possible**. They're sketched as phase 5 in §11, with
   the one real limit on a boot screen explained there.

---

## 10. Follow-ups, decided

Your answers came in the chat on 2026-09-15; they're quoted here.

1. ~~**`printf` with no kernel:** A, B, or both, B later?~~

   You: *"the debug port is an incredible idea, i could have the serial line
   on the virtual window on the left side, closable and expandable. like the
   explorer panel in vscode but on the left of the display. but yeah leave
   that for later, right now just a -1"*

   **Decided:** A in phase 4: `printf` returns -1 with no kernel (step 2).
   The debug port, with the side panel you describe, is phase 6 (§11).

2. ~~**`more COMMAND`:** also run a command with paging on, or only page
   files?~~

   You: *"yeah more should be able to do that"*

   **Decided:** both (step 8). A word that names an existing file is paged
   as a file; otherwise it's run as a command.

3. ~~**Two halves:** build and commit 4a, then 4b?~~

   No answer. **Kept, as I suggested (left to me):** 4a, then 4b, so there's
   a working, committed stopping point halfway (§4). Say if you'd rather have
   it in one go.

4. ~~**The editor**~~ (new, from your message).

   You: *"and the edit could be like a mini nano"*

   **Decided:** step 9 is a mini nano: its title bar and shortcut bar, and
   ^O, ^X, ^K, ^U, ^W, ^C and ^G, all as nano has them.

---

## 11. Later phases

Documented now so phase 4 stays its size. **Not planned in detail yet;**
each gets a plan like this one when its turn comes. kernel.md §17 numbers
the older items differently (its item 5, booting from disk, is done), and
step 11 renumbers that list to match this one.

### Phase 5: a boot screen and a startup script

**`/etc/boot.conf`**, read by the kernel at boot if it's there:

```ini
# /etc/boot.conf
loading_graphics = /bin/bootscreen.bin
startup          = /etc/startup
```

The same `key = value` form as the project file, with `#` comments. With no
file, the kernel boots as it does today.

**A boot screen is possible, with one limit that shapes it.** The kernel
runs one program at a time, with interrupts off while it works. So nothing
can animate *while* the kernel itself is loading. Two ways that work within
that:

- **Before the shell.** The kernel runs `/bin/bootscreen.bin` once. It draws
  its picture or animation for as long as it likes, or until a key, and
  returns. Then the kernel starts the shell. The boot screen can be any
  program, drawing with the display library as the calculator does.
- **As progress.** The kernel also runs it between boot steps, with the step
  as its argument — `bootscreen.bin progress 40` — and it redraws a bar and
  returns at once. The picture stays up in between, because the kernel
  doesn't redraw its console until boot is finished.

**The honest part:** today boot is only mounting the disk and loading the
shell, well under a second at full speed *(estimate, not measured)*. A
progress bar would flash past. It becomes worth having once boot does more,
such as running the startup script.

**When something is wrong:** a missing `boot.conf`, a missing boot-screen
program, or a file that isn't a program is one line on the console, and boot
carries on. A boot screen that never returns can be ended with Ctrl+C.

**The startup script, `/etc/startup`,** is like DOS's `AUTOEXEC.BAT`:
commands, one per line, run before the first prompt.

```
# /etc/startup
echo Welcome to PigeonOS
cd /docs
```

- **The shell runs it, not the kernel,** since the shell already knows how
  to run a line.
- **Only once per boot.** The kernel starts the shell the first time as
  `sh -startup`, with the script's path from `boot.conf`, and afterwards as
  plain `sh`, so `exit` doesn't run the script again.
- **A line that fails is reported, and the rest still run.** `#` starts a
  comment.

### Phase 6: the serial debug port, and a panel for it

Your idea: the machine's serial line, shown in a panel on the left of the
display, **closable and expandable, like VS Code's explorer panel.**

**The device:**
- **`CH_DEBUG`, IO channel 8**, which is free *(checked:
  `emulator/memory_map.py`)*. It's write-only: a program sends bytes with
  R/W 1, and the machine keeps the last 64 KB of them on the host side.
- **Nothing on the machine's screen changes,** so there's no cursor to fake.
- **`printf` with no kernel** writes there instead of returning -1.
- **The kernel can copy its console output there too,** as a log that
  outlives scrolling. This one is optional.

**The panel in the browser:**
- **Today the page is one column:** the controls, the CD drive, then the
  screen's canvas *(checked: `display/index.html`)*. The panel and the
  canvas become a row, with the panel on the left.
- **A button closes and opens it.** Dragging its edge widens it, and the
  width is remembered.
- **Its text follows the end** unless you've scrolled up to read, and a
  button clears it.
- **Text arrives by polling a new endpoint beside `/frame`,** for example
  `/serial?from=N`, which returns what came after byte N. The page already
  polls `/frame` the same way *(checked)*.

**In the pygame client:** the same panel, to the left of the screen, opened
and closed with a button, with the window growing to fit *(checked: the
client sets its own window size, `display/display.py:464`)*.

**Headless:** `--serial` prints it in the launcher's terminal too, for runs
without a front end and for tests.

**Maybe, much later:** typing into the panel sends bytes the other way, if a
program ever wants to read the serial line.

### Phase 7: the launcher

kernel.md §17's item 6. For example `python3 start_emulator.py pigeonos`:
it rebuilds the disc if it's out of date, installs it onto a disk image if
the disk has no system yet, and boots the installed disk.

### Whenever it fits

- **Box-drawing characters** for a Kali-style prompt: five glyphs in the
  font, and UTF-8 in the console (Q1).
- **`pwd`, `mem` and `ver`** (Q2).
- **Multitasking** stays optional (kernel.md §14).
