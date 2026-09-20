# Redirection: `ls > out.txt`

> **Status: built, 2026-09-17 ([§9](#9-as-built)); every question is decided
> ([§7](#7-your-answers)).** Written alongside [pgs_plan.md](pgs_plan.md)
> (its Q7) and built after it.
> Sending a
> program's output to a file instead of the console, in the shell and in
> scripts: `>` to write, `>>` to add, and `<` to feed a program a file. One
> more system call, about 20 lines each in the shell and in `pgs`. Facts
> marked *(checked)* were read in the code; the rest is reasoned.

---

## 1. What this is for

```
2:/> ls -l /bin > listing.txt
2:/> echo done >> listing.txt
2:/> more < listing.txt
```

and the same lines inside a `.pgs` script. It is the other half of
[pgs_plan.md](pgs_plan.md) §4.5: `$( )` takes a program's output **into a
variable**, this takes it **into a file**. A variable is 255 bytes and lives
until the script ends; a file is as big as the disk and outlives the machine
being switched off.

Three things it unlocks that nothing else does: keeping a program's output
(`ls -l > /docs/backup-list.txt`), building a file from several programs
(`>>`), and feeding a program a file it would otherwise want typed
(`< answers.txt`, Q4).

---

## 2. Where things stand

- **`k_write` sends `STDOUT` and `STDERR` to the console and nowhere else;
  every other descriptor is a file** *(checked: `user/os/kernel.c`,
  `k_write`)*. `k_read` is the mirror: `STDIN` is a typed line, anything else
  is a file *(checked: `k_read`)*.
- **The kernel already opens, writes and closes files for a program:**
  `fs_open`, `fs_write`, `fs_close`, with the handle table and the depth that
  owns each handle, closed by `k_tidy` when that program ends *(checked:
  `k_open`, `k_tidy`)*.
- **The shell splits a line into at most 16 words, double quotes grouping
  them** *(checked: `user/os/bin/sh.c:39–73`)*; `>` would be found in that
  word list, which is where every shell finds it.
- **`pgs` will split the same way** *([pgs_plan.md](pgs_plan.md) §4.2)*, and
  its `$( )` capture call, `exec_out`, is the model this one copies
  *(pgs_plan.md §4.5)*.
- **The system call table has 32 slots and 20 used** *(checked:
  `emulator/memory_map.py:133`, `lib/pigeon/syscall.h`)*; `exec_out` takes
  one, this takes another, and 10 are left.
- **A file that a program is writing is not visible to `pfs.py` until it is
  closed and the disk flushed** *(checked: `fs.c` writes through
  `__fs_flush`)*, which the kernel does when the program ends.

---

## 3. The design

### 3.1 The system calls

```c
/* <pigeon/sys.h> */
#define R_TRUNC  0      /* >  : written fresh, and put in place at the end */
#define R_APPEND 1      /* >> : added to the end of what is there          */

int exec_to  (char *path, int argc, char **argv, char *file, unsigned how);
int exec_from(char *path, int argc, char **argv, char *file);
```

`exec_to` runs the program exactly as `exec` does and returns the same
status, but everything it — or anything it runs — writes to `STDOUT` goes
into `file`. `exec_from` is the same in the other direction: the program's
`read(STDIN)` takes its lines from `file` instead of the keyboard, and gets
0 at its end (Q1). One of each on a call is allowed; both at once is fine
too, since they are separate slots.

The kernel opens the file before the program starts, closes it when the
program ends, and puts the previous redirection back, so a script that
redirects a script that redirects needs nothing extra. A file that can't be
opened is the call's error and the program never starts, which is what a
shell should report.

Inside, it is `exec_out`'s twin *([pgs_plan.md](pgs_plan.md) §4.5)*: one more
test in `k_write`, an fs handle instead of a buffer —

```c
if (out_file >= 0 && depth >= out_depth) return fs_write(out_file, buf, n);
```

— and the mirror of it in `k_read` for `STDIN`.

**`>` writes beside the file and renames at the end (Q5),** the way `edit`
saves *(checked: `user/os/bin/edit.c`)*: the kernel opens `file~`, the
program writes into that, and when it ends `file` is removed and `file~`
renamed onto it. A program that crashes, is stopped with Ctrl+C, or fills the
disk leaves the old `file` untouched and `file~` is removed — so
`ls -l > list.txt` can never eat the list you already had because `ls`
faulted halfway.

**`>>` appends in place, with no temporary** (F1): it has nothing to lose,
since the bytes already there stay whatever happens, and a copy-then-append
would rewrite a megabyte to add a line.

Two things follow from the rename that are worth knowing: the new `file` does
not exist until the program ends, so nothing can watch it grow, and `file~`
is a name the write needs — `>` onto a file whose `~` name is taken is an
error before the program starts.

The capture of `$( )` and a redirection cannot both be on for one program;
`pgs` refuses that line rather than doing half of it (Q3).

### 3.2 In the shell

`sh.c` looks for `>`, `>>` and `<` in the words it has already split, takes
them and the filename out of the list, and calls `exec_to` or `exec_from`
instead of `exec`. About 20 lines, and the redirection words are
recognised only as **whole words**, so `echo 1>2` is one argument and
`ls > out.txt` is a redirection — one rule, easy to say out loud.

The shell's own built-ins (`cd`, `help`, `exit`) don't redirect: `help > x`
is a mistake, reported as one (Q5).

### 3.3 In `pgs`

The same, on the same word list *([pgs_plan.md](pgs_plan.md) §4.2)*, so a
line in a script reads exactly like a line at the prompt:

```sh
ls /bin > $out                ;; the filename can be a variable
echo done >> $out
$text = $(more $out)          ;; and $( ) still works as it does
```

`echo`, `pwd` and the other `pgs` builtins **do** redirect, unlike the
shell's, because a script writing a file with `echo` is one of the reasons to
have this at all (Q5):

```sh
echo hello > note.txt
echo world >> note.txt
```

---

## 4. Steps

1. **`exec_to` and `exec_from` in the kernel:** `syscall.h`, `kernel.asm`,
   `kernel.c`, `sys.c`, `sys.h`. *Tests* in `tests/test_kernel.py`: output
   lands in the file and not on the console; `>` writes `file~` and renames
   it at the end; a program that faults or is stopped with Ctrl+C leaves the
   old file whole and no `file~` behind; `>>` adds in place; a grandchild's
   output goes there too; `<` feeds a program its lines and gives 0 at the
   end; a file that can't be opened stops the program starting; the handle is
   closed when the program ends; a redirection inside a redirection; a disk
   that fills up mid-write.
2. **The shell:** `>`, `>>` and `<` in `sh.c`. *Tests* in
   `tests/test_kernel.py`: each form, a missing filename, a built-in with a
   redirection, a quoted `">"` staying an argument.
3. **`pgs`:** the same words, with builtins redirecting too. *Tests* in
   `tests/test_pgs.py`.
4. **The docs:** `docs/shell.md`, `docs/pgs.md`, and this plan's "as built".

---

## 5. Risks

- **`file~` in the way.** The rename that makes `>` safe (Q5) needs that
  name, so a folder already holding `list.txt~` refuses `> list.txt` until it
  is gone. The error says which name it wanted.
- **`>>` has no such safety** (F1): bytes appended to a file that then
  crashes stay appended, half a line and all.
- **A full disk** shows up as a short write inside the program, which most
  programs won't notice. The kernel can't do much about that; the shell
  reports the status it gets.
- **`>` in a script that a person reads as text.** `echo $a > $b` writes a
  file; `echo $a >$b` — no space — is the same thing, since the words are
  split first. Anything else would need a real tokeniser.

---

## 6. Not in this plan

- **Pipes** (`a | b`): two programs at once, which the kernel does not do
  *(checked: `k_run` waits for the child)*.
- **`2>`** and merging `STDERR` into `STDOUT` (Q2): it stays on the console.
- **`>` from the explorer**, which runs one command with a file appended and
  has no line to put a `>` on.

---

## 7. Your answers

Answered in this file on 2026-09-16.

1. ~~**`>` and `>>` — and `<`?**~~

   Answer: okay, all 3

   **Decided (you):** all three, so `exec_from` comes with `exec_to` (§3.1).

2. ~~**`STDERR`:** on the console always, or `2>` as well?~~

   Answer: okay

   **Decided (you):** on the console; no `2>` in this plan (§6).

3. ~~**A redirection around a `$( )`,** as in `$x = $(ls) > out.txt`.~~

   Answer: yeah, an error.

   **Decided (you):** `pgs` refuses the line — `hello.pgs:4: $( ) and > on
   one line` (§3.1, §3.3).

4. ~~**The shell's built-ins,** which print almost nothing, against `pgs`'s
   `echo`, which is how a script writes a file.~~

   Answer: okay

   **Decided (you):** the shell's three refuse a redirection and say so;
   `pgs`'s builtins redirect (§3.2, §3.3).

5. ~~**Truncating:** empty the file first, as every shell does, or write
   `file~` and rename, as `edit` does?~~

   Answer: oooh that is a nice feature, yeah, do it like edit

   **Decided (you):** `>` writes `file~` and renames it when the program
   ends, so a crash, a Ctrl+C or a full disk leaves the old file whole
   (§3.1).
   **Decided (left to me):** `>>` appends in place instead (F1) — it has
   nothing to lose, and a copy-then-append would rewrite the whole file to
   add a line. Both are said plainly in `docs/shell.md` when this is built.

6. ~~**When to build it:** before `pgs` or after?~~

   Answer: i think build this after

   **Decided (you):** after [pgs_plan.md](pgs_plan.md), before
   [graphics_plan.md](graphics_plan.md).

---

## 8. Follow-up

- **F1. `>>` in place, `>` through `file~`.** The rename Q5 asked for makes
  `>` safe; appending cannot use it without copying the whole file first, so
  `>>` writes straight to the end. If you would rather `>>` were safe too, it
  costs a copy of the file per append — say so and I will make both of them
  work that way.

  Answer:

---

## 9. As built

Built to this plan on 2026-09-17, in the order §4 gives. Five things it
corrected along the way.

- **One system call, not two.** `exec_to` and `exec_from` cannot do
  `sort < in.txt > out.txt` between them -- each runs the program itself, and
  a program runs once. The kernel has **`exec_io(path, argc, argv, in, out,
  how)`**, either end NULL, and `<pigeon/sys.h>` keeps `exec_to` and
  `exec_from` as one-line wrappers over it, which is how a shell line usually
  reads. One slot instead of two, and both ends work together.
- **`k_remount()`.** `fs_unmount` refuses while any handle is open *(checked:
  `fs.c`)*, so `k_tidy`'s "mount the disk again" is silently skipped while a
  redirected file is open -- and that remount is what keeps the kernel's cache
  honest after a program with its own `fs.c`. `exec_io` closes the file and
  does it again itself; `k_tidy` and it now share the one function.
- **Quoting protects a marker.** §3.2 said "whole words", which covers
  `echo 1>2` but not `echo ">"`. Both splitters now remember which words had
  quotes in them, so a quoted `>` is text. It is the only thing the quotes are
  remembered for.
- **The shell names the file, not the program.** `talker > /nodir/out.txt`
  said `talker: not found`, which sends you looking in the wrong place: the
  shell has already found the program, so an `E_NOENT` from `exec_io` is the
  redirection's file, and that is what is printed.
- **"A value cannot redirect" replaces Q3's message.** The trap is not `$( )`
  and `>` together -- `cat $(pwd) > out.txt` is a perfectly good line. It is
  that an assignment's right-hand side is *all* value, so `$x = $(ls) >
  out.txt` quietly puts `> out.txt` in the variable. Any unquoted `>`, `>>` or
  `<` word in a value is now `a value cannot redirect: quote it if you meant
  the text`.

Also built: `<` on a `pgs` builtin is a mistake (none of them reads input that
way), while `>` on one writes the file with the same `file~` rule a program
gets. F1 was left unanswered and its suggestion stands: `>>` appends in place.

- **Tests:** 16 in `tests/test_kernel.py` (the call, the rename, a fault
  keeping the old file, a program that returns a number keeping its output, a
  grandchild's output, `<` feeding lines, both ends at once, the shell's
  words, the built-ins refusing, a file that will not open) and 11 in
  `tests/test_pgs.py`. `docs/shell.md` §8 and `docs/pgs.md` §10 are the
  manual.
