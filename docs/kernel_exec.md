# Running a program from the shell

> **Status: proposal; relocation (§5, §7, §8's startup code) is built,** as
> phase 1 of kernel.md §17. What happens, step by step, when you
> type `ls` at the shell. The kernel boots, starts the shell as its first
> program, and the shell asks the kernel to run others. §5 was compiled and
> run on 2026-09-13, and again on 2026-09-14; anything else reasoned but not
> run is marked *unverified*. Its questions are decided, in
> [§10](#10-questions). The short version is
> [kernel_overview.md](kernel_overview.md).
>
> This answers kernel.md Q2 — the shell is a program, not part of the kernel —
> and changes kernel.md §8: relocation is now needed, and it turned out cheap.
> kernel_changes.md's questions are decided too.

---

## 1. The one idea: everything is a function call

Nothing switches between programs behind their backs. The CPU has had
interrupts since kernel.md's phase 2 (§13), but in the first version they
only end a program or tick the timer. That makes the whole system simpler
than it sounds:

- **The kernel runs a program** by calling a function that happens to live in
  a file it just loaded.
- **A program calls the kernel** by calling a function whose address the kernel
  put in a table at a fixed address — a *system call*.
- **A program finishes** by returning from `main`, which returns to whoever
  called it.

So the entire machine is one chain of calls on one hardware stack. While `ls`
is printing a line, the stack looks like this:

```
return addresses, oldest first         running
────────────────────────────────       ───────────────────────────
kernel_main                            kernel
 └ exec("/bin/sh.bin")                 kernel
    └ __start → main                   shell
       └ run_line("ls /docs")          shell
          └ exec(...)       ── table ─►kernel
             └ __start → main          ls
                └ printf(...)          ls (its own copy of the formatter)
                   └ write(1, buf, n) ─ table ─► kernel
                      └ console_write  kernel
                         └ disp_char   kernel
```

Everything after that is returns. `disp_char` returns to `console_write`,
which returns to `ls`. `ls` finishes and returns to the kernel, which cleans
up and returns to the shell, which prints the next prompt.

---

## 2. From power-on to the prompt

1. **The BIOS** loads bios2, which copies the disk's boot sector into memory
   ([os_cd.md](os_cd.md), built).
2. **The boot sector** loads `/boot.bin`, the kernel, into `0x20000`.
3. **The kernel** mounts the disk it booted from (kernel.md Q8), fills in the
   system-call table, sets up the console, and prints a banner.
4. **The kernel starts the shell**, as Unix starts `init` and DOS starts
   `COMMAND.COM`:

   ```c
   for (;;) {
       int status = exec("/bin/sh.bin", 1, shell_argv);
       console_puts("shell exited, restarting\n");
   }
   ```

   The loop restarts the shell when it exits (Q2, decided). If `exec` can't
   start it at all — no `/bin/sh.bin`, or a bad header — the kernel prints why
   and halts instead of looping. The prototype's kernel stopped (kernel.md §16,
   P4).

---

## 3. Typing `ls /docs`

**The shell**

1. **Reads a line** with `read(0, line, size)`. The kernel's console handles
   the echo, backspace and Enter, and returns the finished line (Q3).
2. **Splits it into words**: `argc = 2`, `argv = {"ls", "/docs"}`.
3. **Handles its own commands** — `cd`, `exit`, `help` — without starting
   anything. `cd` is a `chdir` system call.
4. **Finds the file.** A name without a `/` becomes `/bin/ls.bin` (Q4).
5. **Asks the kernel to run it, and waits:**
   `status = exec("/bin/ls.bin", argc, argv)`.

**The kernel, inside `exec`**

6. **Opens the file and checks its header** (§7): the magic number, the format
   version, and the sizes.
7. **Chooses where the program goes in memory.** This is the hard part — §4.
8. **Loads the image** with `fs_load`, which uses DMA, and **patches its
   addresses** for where it landed (§5).
9. **Records the running program**: its parent, its memory, its open files
   and its arguments.
10. **Calls its entry point** like a C function pointer:
    `status = entry(argc, argv)`.

**`ls`**

11. **Its startup code** saves the caller's frame pointer, switches `F` to its
    own frame stack, sets up its heap, and calls `main(argc, argv)` (§8).
12. **`main` does the work.** `opendir` and `readdir` are system calls into
    the kernel's `fs.c`. Each line is formatted by `printf` in `ls`'s own code
    and handed to `write(1, ...)`. The kernel draws the text on the console and
    scrolls it.
13. **`main` returns 0.** The startup code restores `F` and returns into the
    kernel's `exec`.

**The kernel, again**

14. **Cleans up after `ls`**: closes any files it left open, stops its timers,
    points the display back at the screen, empties the input queues, and frees
    its memory (kernel.md §11).
15. **Returns the status to the shell**, which prints the next prompt.

---

## 4. The hard part: where does `ls` go?

Every program today is built to run at `0x20000`. If the shell is at
`0x20000`, `ls` can't go there too. Real systems have solved this four ways:

| | How | Used by | A program can run a program | Needs |
|---|---|---|---|---|
| **A. Replace the shell** | The shell tells the kernel "run `ls` next" and returns. `ls` runs at the same address, and the kernel then loads a fresh shell | PDP-7 Unix; CP/M reloading its command processor | No: one at a time | Shell state such as history kept in the kernel |
| **B. Two fixed places** | The shell is built for one address, every other program for another | — | Only the shell | A third compiler layout |
| **C. Swap the parent out** | Every program at one address. The kernel writes the parent's memory to disk, runs the child, then reads the parent back | Early Unix on the PDP-11/20, which had no memory management | Yes, to any depth | `exec` switching to the kernel's own frame stack, which C can't do; a disk round trip on every run |
| **D. Relocate** | The kernel loads each program wherever memory is free, and patches the addresses in it | MS-DOS `.EXE`; uClinux flat binaries; MP/M `.PRL` | Yes, to any depth | A list of addresses to patch in each file; frame stack and heap placed after the image |

**I recommend D.** It's the only option that is both general and simple, and
§5 shows it works on this machine today. A and B stop at one level: no
`make`-style program that runs other programs, and no script runner. C gets
to the same place as D with more moving parts.

An earlier kernel.md §8 set relocation aside as "much more work, and nothing
here needs it". The shell as a program needs it, and §5 shows it doesn't need
an assembler change. kernel.md §8 now proposes it.

---

## 5. Relocation, tested

**The trick** (MP/M's `GENMOD` did the same): assemble the program twice, at
two different addresses, and compare the two images. A word that differs
holds an address, and every other word is the same in both. The list of words
that differ is the list the kernel patches.

**Checked on every C program in `user/`**
(`prototypes/kernel/p0_relocation.py`), built with its libraries at
`0x20000` and at `0x01021238`, as run again after phase 1 on 2026-09-14:

| Program | Size | Words to patch | In an instruction's address field | Other bytes that differ |
|---|---|---|---|---|
| `files` | 174,132 | 1,940 | 1,921 | 0 |
| `disc` | 172,436 | 1,789 | 1,789 | 0 |
| `graph` | 114,624 | 1,464 | 1,455 | 0 |
| `cube` | 40,092 | 395 | 395 | 0 |
| `demo` | 32,088 | 347 | 346 | 0 |

- **Every differing word differed by exactly the distance between the two
  addresses**, and no other byte differed at all, so the trick misses
  nothing. The few words outside instructions are `.word` data, such as
  tables of string pointers.
- **A patched program runs.** A test program with a function pointer,
  recursion, a table of strings and `malloc`, built at `0x20000` and patched
  to run at `0x01021238`, returned the right answer with the stack balanced.
  The patched image was byte-for-byte the same as a build made at that
  address.
- **Each program can carry its own frame stack and heap.** Today they sit at
  the fixed `HEAP_START`, where two programs would share them. With both moved
  to just after the program's own image, the test program ran at `0x01021238`:
  `F` started at the end of its image, `malloc` returned memory above that, and
  it wrote **nothing** to `0x20000`–`0x5FFFF` or to
  `0x120000`–`0x17FFFF`.
- **One snag.** The assembler works out `NAME = expression` definitions
  before any label has an address. `__frame_base = __image_end` fails with
  "Undefined symbol: __image_end". The test instead wrote
  `__image_end + 262144` directly into the instructions, which the assembler
  resolves after labels are placed. The compiler can emit it that way, or the
  assembler can learn to resolve definitions later. **Phase 1 took the
  second way:** a definition that uses a label is settled after layout, so
  the compiler emits `__frame_base = __image_end`.

**Cost.** The largest program has about 2,000 words to patch. At a few
instructions each, that's tens of thousands of instructions, well under a
tenth of a second *(unverified estimate)*. Stored as 4-byte offsets, the list
adds about 8 KB to `files`.

---

## 6. Memory, with relocation

A proposal, not measured against anything:

```
0x00000000  BIOS, IO window, framebuffer
0x00015818  boot sector copy and boot channel; system-call table at 0x15A1C
0x00020000  kernel image                      fixed address, built as today
0x00120000  kernel frame stack and heap       heap stops at 0x00FFFFE0
0x01000000  shell │ image │ frame stack │ heap →
            ls    │ image │ frame stack │ heap →      just above the shell's heap
            ...
0x07F00000  one megabyte of headroom          mem.c's limit today
0x07FFFFFC  hardware stack, shared, grows down
```

**Programs stack like plates.** A parent always waits for its child, so the
child goes just above the parent's current heap top. The child can use
everything up to `0x07F00000`. When it exits, the parent's heap can grow
into that space again. Programs finish in reverse order of starting, so
memory never fragments and the kernel needs no allocator for programs.

**The kernel finds the parent's heap top** in `__heap_ptr`. The compiler
emits that word next to the end of every image, and the header records where
it is (§7).

**Only the kernel keeps a fixed address.** Its heap stops at `0x00FFFFE0`,
32 bytes below the shell. Today `mem.c` returns a typed `0x07F00000` for every
program; instead, each program's limit becomes a word that `exec` writes
(kernel.md Q7). Programs need no layout constants at all, which replaces the
separate kernel and app layouts an earlier kernel.md §8 proposed.

---

## 7. The program file

Today a `.bin` is raw instructions. A program file adds a header in front and
the patch list at the end:

| Field | What |
|---|---|
| magic | tells a program from any other file: `PGEX`, `PROGRAM_FILE_MAGIC`; the prototype's was `PGX1` |
| version | the format version |
| image size | bytes of code and data |
| entry | offset of `__start` in the image |
| patch count | number of 4-byte offsets after the image |
| frame stack size | so the kernel can check the program fits |
| heap-top offset | where `__heap_ptr` is in the image; `__heap_limit` is the next word (kernel.md Q7) |
| link address | the address it was built for, so the kernel knows how far each address moves |

Eight words, so the header is 32 bytes, as the prototype built it.

**Built by** a small step after the compiler: assemble twice, compare, and
write the header, the image and the offsets. `cc.py --relocatable` runs it for
one source, and `cc.py --project` for every `.c` in `[files]` (kernel.md Q6).
The BIOS path keeps running raw `.bin` files as it does today.

**Put on the disk** with `pfs.py put`, which already exists — for example
`/bin/ls.bin`.

---

## 8. What each piece is

**The kernel** (`kernel.c`, built at its fixed address)
- `main`: mount, fill the table, start the console, start the shell (§2)
- `exec`: header, placement, load, patch, call, clean up (§3, steps 6–14)
- a record for each running program: parent, memory, open files, arguments
- the console: text grid, scrolling, line input
- the file system: the one copy of `fs.c`, and the current directory

**Program startup code**, emitted by `cc.py --relocatable` instead of the
`HALT` version. Built in phase 1; the prototype ran the same instructions
(kernel.md §16), with the two addresses written out:

```asm
__start:                          ; the kernel called entry(argc, argv)
    MRW  A, F                     ; argc, from the caller's frame
    ADD  C, F, #4
    MRW  B, C                     ; argv
    PUSH F                        ; the kernel's frame pointer
    MOV  F, #__frame_base         ; my frame stack: __image_end, patched at load
    MWW  F, A                     ; main's argc
    ADD  C, F, #4
    MWW  C, B                     ; main's argv
    MOV  C, #__heap_ptr
    MOV  A, #__heap_base          ; my heap, after the frame stack
    MWW  C, A
    CALL main
    POP  F
    RET                           ; main's return value is still in A
```

**A system-call library** (`lib/pigeon/sys.h`, `sys.c`): one short function
per call, each calling through the table. The table cast was compiled and run
in kernel_changes.md §2.

**A first set of system calls**, shaped like Unix's so they're familiar:

| Call | Does |
|---|---|
| `write(fd, buf, n)` | `fd` 1 and 2 are the console; a later version can point them at a file for `ls > out.txt` |
| `read(fd, buf, n)` | `fd` 0 returns a line from the console |
| `open`, `close`, `opendir`, `readdir`, `closedir`, `stat` | the kernel's `fs.c` |
| `chdir`, `getcwd` | the kernel's current directory |
| `exec(path, argc, argv)` | runs a program, waits, and returns its status |
| `getkey` | a raw key without waiting, for games and editors |

**The shell** (`sh.c`, a program): prompt, split, the commands it handles
itself, lookup in `/bin`, and `exec`. Its prompt comes from
`/etc/shell_header.conf` ([shell.md](shell.md)).

**First programs**: `ls`, `cat`, `echo` — small enough to test the whole
chain.

---

## 9. What still can't be done without CPU changes

kernel.md §13 proposes CPU changes for the first three, and its prototype ran
them. Protection is still out of reach.

- **`exit()` from deep inside a program.** Only returning from `main` gets
  back to the kernel (kernel.md Q5).
- **Ctrl-C.** Nothing can take control from a program that is busy. Even when
  it makes a system call, the kernel can't unwind it back to the shell.
- **Crashes.** A bad opcode or a divide by zero ends the emulator, not just
  the program.
- **Protection.** Any program can overwrite the kernel or its parent.

---

## 10. Questions

Decided 2026-09-14, left to me.

1. **Where programs go.** Moved to kernel.md §18, Q4. The prototype there
   ran relocation end to end.

2. ~~**When the shell exits.**~~ **Decided:** the kernel starts it again, as
   Unix restarts a login prompt. If it can't be started at all, the kernel
   prints why and halts rather than looping.

3. ~~**Who edits the typed line.**~~ **Decided:** the kernel's console.
   `read(0, …)` returns a finished line with the echo and backspace already
   done, so every program that reads a line gets them for free.

4. ~~**Finding programs.**~~ **Decided:** a name with no `/` is looked up as
   `/bin/<name>.bin`, then as `<name>.bin` in the current directory. A name
   with a `/` is used as a path. You never type the `.bin`: it's added when
   missing. The shell does the lookup, and the kernel's `exec` takes a path,
   as in P5.
