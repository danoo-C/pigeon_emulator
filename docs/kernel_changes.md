# Kernel changes: apps that print to the shell

> **Status: decided, nothing built.** This checks [kernel.md](kernel.md)
> against one requirement: a command-line app — `ls`, `ld`, anything that
> prints — starts from the shell, prints into the shell's console, and returns
> to it. Every fact in §2 was checked on 2026-09-13; the compiler facts were
> compiled and run. Anything reasoned but not run is marked *unverified*. Its
> questions are decided, in [§4](#4-questions); two moved to kernel.md. The
> short version is [kernel_overview.md](kernel_overview.md).

## 1. The answer

**The design can run such apps, but not as written.** The mechanism works:
calling the kernel through a table of function addresses compiles and runs,
and `main(int argc, char **argv)` compiles. The gaps are things kernel.md
doesn't decide, and one it doesn't mention: **there can be no `printf` today,
because the compiler rejects variadic functions.**

| Need | kernel.md | Today |
|---|---|---|
| An app calls the kernel's print | §10, system-call table | **Works** — compiled and ran |
| `printf` | not mentioned | **Blocked** — `...` is a compile error |
| Output lands in the shell's console | §12, console in the kernel | Only if apps reach the console through the kernel; kernel.md Q3 allows otherwise |
| `ls` with no argument, relative paths | not mentioned | The current directory is kept separately in each program's copy of `fs.c` |
| Arguments | §9, "not yet" | `argc`/`argv` compile; how the shell passes them is undecided |
| The shell's screen after the app | §11 resets the display address | The console's text isn't mentioned |
| Stopping on an error | §9 and Q4 | No `exit()` from inside a nested function |

---

## 2. What was checked

**Compiled and run**, with `returns()` from `tests/test_compiler.py`: compile,
assemble, run on the CPU, and check that the hardware stack ends balanced.

| Probe | Result |
|---|---|
| `int printf(char *fmt, ...)` | **Compile error:** "variadic functions are not supported: the frame layout has no way to walk an unknown argument count" (`compiler/parser.py:288`) |
| `int main(int argc, char **argv)` | Compiles and runs |
| Store a function's address at `0x15818` as `unsigned`, cast it back to `int (*)(char *)`, call it with a string literal | Returns 5 for `"hello"` |
| The same through a `typedef`'d function-pointer type | Works |
| A formatter that takes its arguments as an `int` array | Works |

**Read from the code:**

- **Calls between the kernel and an app follow the ABI as it is**
  (`compiler/design/03-abi.md`). The caller writes the arguments past its own
  frame, moves `F`, and calls. The callee uses only `[F + n]` and absolute
  addresses. A kernel function called from an app therefore runs on the app's
  frame stack while using the kernel's globals, and neither side needs the
  other's layout *(unverified: there is no second layout yet to run it
  across)*.
- **Casts are not checked.** `_x_Cast` (`compiler/analyzer.py:448`) takes the
  target type as given, which is why the table casts compile.
- **Pointers cross freely.** There is one address space, so the kernel can read
  a string in the app's data. App memory is above `PROGRAM_LOAD_ADDR`, so the
  HDD accepts DMA transfers into it too.
- **`fs.c` keeps the current directory inside each program.** `__fs_vols`,
  `__fs_cwd_vol` and `__fs_cwd` are `static` (`lib/pigeon/fs.c:157–162`).
  `fs_chdir` and `fs_getcwd` exist (`fs.h:120–121`). An app that bundles
  `fs.c` starts with nothing mounted and no current directory.
- **`display.c` keeps its state inside each program** — `disp_target`,
  `disp_back` and `disp_hw` (`display.c:31–44`). A console library would add a
  cursor and a grid of characters.
- **The console is 32×12.** Each character cell is `GLYPH_W + 1` by
  `GLYPH_H + 1`, 6×9 pixels, on a 192×108 screen.
- **`03-abi.md` documents `-fstack-check` and `-fframe-size`, but `cc.py` has
  neither.** If stack checking is ever added, a kernel function running on the
  app's frame stack would compare `F` against the *kernel's* `__frame_limit`
  and report an overflow that isn't one.

---

## 3. The gaps

### 3.1 No `printf`

A frame puts the parameters at `[F + 0]`, `[F + 4]` … and the locals straight
after them, at offsets fixed when the callee is compiled. Passing more
arguments than there are parameters would overwrite the callee's locals.

Three ways round it:

- **A. Fixed print calls, no format string.** `print_str`, `print_int`,
  `print_hex`, `print_char`. No compiler change. `"%d files, %d KB free"`
  takes five calls — the same pattern `user/files.c` uses with `utoa` today.
- **B. A formatter that takes an argument array.** `printa("%d files", args)`
  with `args` an `int` array. No compiler change, and the probe passed.
  Awkward to call: every `printa` call has to fill an array first.
- **C. Variadic functions in the compiler, with a cap.** A variadic function
  reserves a fixed number of slots — say 8 — between its named parameters and
  its locals. The caller writes up to that many extra arguments; more is a
  compile error. `va_start` and `va_arg` walk the slots. Frame sizes stay
  compile-time constants, which is what the ABI depends on, and calls through
  the system-call table still work, because the cap is also a constant.
  Arguments must be word-sized. The work: parser, analyzer, codegen, a
  `<stdarg.h>`, and tests.

### 3.2 An app's output reaches the shell only through the kernel

The console's cursor, its 32×12 grid and its scrolling all live in the kernel.
An app with its own copy of `console.c` starts at row 0 with an empty grid, so
it draws over the shell's lines. The shell's grid never saw that text, so its
next scroll wipes it *(unverified: reasoned from how a console keeps its
grid)*.

So **the console must be a system call even if apps bundle their other
libraries.** kernel.md's Q3 is framed as all or nothing.

### 3.3 The current directory

The shell's `cd` changes the kernel's `__fs_cwd`. An app with its own `fs.c`
never sees that change: `ls` with no argument, or `ld main.o`, resolves the path against
no directory at all. Either:

- the filesystem is a system call, and apps use the kernel's `fs.c` and its
  current directory, or
- the kernel passes the current directory to each app, which mounts the disk
  and calls `fs_chdir` itself. That comes on top of the unmount and remount
  kernel.md §10 already needs.

### 3.4 Arguments

`main(int argc, char **argv)` compiles. Under the ABI, app startup code would
write `argc` to `[F + 0]` and `argv` to `[F + 4]` before `CALL main`. A
`main(void)` would ignore them, because that is where its uninitialised locals
begin *(unverified)*. Not yet decided:

- how the shell splits a line: at spaces only, or with quotes too
- where the strings live: in kernel memory the app reads, or copied onto the
  app's heap
- a limit on the number of arguments and on the length of a line

### 3.5 The screen after the app returns

§11 points the display back at `DISPLAY_START`, but says nothing about the text.
An app that drew graphics or cleared the screen leaves the framebuffer out of
step with the console's grid. And with only 12 rows, `ls` on a large
directory scrolls most of its output out of sight: there is no scrollback and
no paging.

### 3.6 Stopping on an error

Command-line tools often stop deep inside the code — `exit(1)` on the first
bad input. Without instructions that set the stack pointer (kernel.md §14),
an error has to be returned through every caller up to `main`. That is
kernel.md's Q4, and it matters more for a tool like `ld` than for a game.

---

## 4. Questions

Decided 2026-09-14, left to me.

1. ~~**`ld` or `ls`?**~~ **Decided:** `ls`, and command-line tools in general.
   A linker on the machine is out of scope: it would need an object-file
   format that nothing else needs.

2. ~~**`printf`.**~~ **Decided:** C, variadic functions with a cap of 8 extra
   word-sized arguments (§3.1). It's the only real `printf`, and frame sizes
   stay constant, so the ABI and the system-call table keep working. It is
   built before the shell.

3. **What goes through the kernel.** Moved to kernel.md §18, Q3.

4. ~~**Arguments.**~~ **Decided:** startup code writes `argc` and `argv` into
   `main`'s frame. The shell splits at spaces, with double quotes grouping
   words, up to 16 words on a 255-character line. The strings aren't copied:
   they stay in the caller's memory, which can't move or be freed while its
   child runs.

5. ~~**The screen after an app.**~~ **Decided:** the kernel redraws the
   console's grid, which it keeps as text, so the shell's lines come back even
   after a game drew over them. No `-- more --` for now: output that fills the
   12 rows scrolls.

6. ~~**Redirection.**~~ **Decided:** later, not in the first version. Every
   print already goes through the kernel's `write(1, …)`, so `ls > list.txt`
   can be added in the kernel without changing any program. Pipes wait for
   multitasking, or never come.

7. **`exit()`.** Moved to kernel.md §18, Q5. The prototype there ran
   `exit()` from 50 calls deep.
