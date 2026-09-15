# How the OS fits together

> **Status: the short version. Steps 1 to 3 of §7 are built: relocatable
> programs, the CPU's interrupts and faults, and the kernel with a console
> and a simple shell. So is step 4:** `printf`, colors, the prompt file and
> the file commands, then line editing, Tab completion, scrollback, `more`
> and `edit`. The detail is in
> [kernel.md](kernel.md), [kernel_exec.md](kernel_exec.md) and
> [kernel_changes.md](kernel_changes.md), and every question in them is now
> decided (§6). Booting from disk is already built ([os_cd.md](os_cd.md)).

---

## 1. The pieces

| Piece | What it is | Where it lives |
|---|---|---|
| **Firmware** | BIOS → bios2 → boot sector. Loads `/boot.bin` from the hard disk and jumps to it. | Built ([os_cd.md](os_cd.md)) |
| **Kernel** | `/boot.bin`. Owns the disk (`fs.c`), the console, and running programs (`exec`). | `0x20000`, never moves |
| **System-call table** | Addresses of kernel functions, filled in at boot. The only way into the kernel. | `0x15A1C` |
| **`sys.c`** | A small library in every program: one stub per system call, calling through the table. | Inside each program |
| **Shell** | `/bin/sh.bin`, an ordinary program. Reads a line, finds the program, asks the kernel to run it ([shell.md](shell.md)). | `0x01000000` |
| **Commands** | `/bin/ls.bin`, `cat.bin`, … ordinary programs. | Just above whoever started them |

```
0x00000000  BIOS, IO, screen
0x00015A1C  system-call table
0x00020000  kernel, with its stack and heap below 0x01000000
0x01000000  shell: code, stack, heap
            ls: code, stack, heap            right above the shell's heap
            a program ls starts, if any      right above ls
0x07FFFFFC  hardware stack, shared, grows down
```

---

## 2. Nothing runs in parallel

- **One program runs at a time.** When the shell starts `ls`, the shell
  *waits*: it is paused inside its `exec` call, as in DOS.
- **Waiting costs nothing.** The shell's return address is on the stack, and
  its memory is untouched because `ls` is placed above it.
- **When `ls` returns, so does `exec`**, and the shell carries on from the
  next line.
- **Running programs side by side** is an optional later step (kernel.md §14),
  not part of the first version.

---

## 3. Typing `ls`

The whole machine is one chain of function calls on one stack:

```
kernel_main                                    kernel
 └ exec("/bin/sh.bin")                         kernel starts the shell at boot
    └ main                                     shell
       ├ read(0, line)        ── table ──►     console: keys, echo, Enter → "ls"
       └ exec("/bin/ls.bin")  ── table ──►     kernel: load ls, patch it, call it
          └ main                               ls
             ├ readdir(...)   ── table ──►     kernel's fs.c: the next entry
             └ printf(...)                     ls formats the line itself
                └ write(1, text) ─ table ─►    console: draws it on the screen
          ◄ return 0                           kernel: tidy up after ls
       ◄ exec returns 0                        shell: print the prompt
```

1. **You type `ls` and press Enter.** The shell is inside `read(0, …)`. The
   kernel's console takes the keys, echoes them, handles backspace, and
   returns the line `"ls"`.
2. **The shell splits the line** into `argc` and `argv`, checks it isn't a
   built-in like `cd` or `exit`, and turns `ls` into `/bin/ls.bin`.
3. **The shell calls `exec("/bin/ls.bin", argc, argv)`.** From here on, the
   shell is paused.
4. **The kernel loads `ls`:** reads the file with its `fs.c`, checks the
   header, puts it just above the shell's heap, fixes its addresses for that
   spot, and calls its entry point like a function.
5. **`ls` runs.** It asks the kernel for the directory entries, formats each
   line with its own `printf`, and passes the text to `write(1, …)`.
6. **The kernel's console draws the text** at the cursor, and scrolls when the
   12 rows are full.
7. **`ls` returns 0.** The kernel tidies up after it — open files, the display,
   the key queues, its memory — and `exec` returns 0 to the shell.
8. **The shell prints the next prompt** and waits in `read` again.

---

## 4. What puts `ls`'s output "in the shell"

- **The kernel's console.** It owns the cursor and the 32×12 text grid. Neither
  program draws text itself; both only call `write`.
- **`ls` doesn't send its output to the shell**, and the shell never sees it.
  Both print to the same console, one after the other, so the lines from `ls`
  appear under the command you typed and the prompt appears under them.
- **The kernel decides where `write(1, …)` goes.** For now that's the screen.
  Later, `ls > list.txt` can send it to a file without changing `ls`.
- **If a program drew graphics over the text**, the kernel redraws the
  console's grid when it returns, so the shell's screen comes back.

---

## 5. When `ls` doesn't return normally

These need the CPU changes (kernel.md §13):

- **`exit(1)` from deep inside `ls`:** the kernel puts the stack back to where
  it called `ls`, as if `main` had returned 1.
- **A crash**, such as a divide by zero or a bad instruction: the CPU raises a
  fault, the kernel abandons `ls`, and `exec` returns an error to the shell.
- **Ctrl-C:** the keyboard raises a break interrupt, handled like a crash.
- **Not covered:** memory protection. A program can still overwrite the
  kernel.

---

## 6. What was decided

Decided 2026-09-14, left to me. Each is recorded where the question was asked.

| Question | Decision | Where |
|---|---|---|
| Is the shell part of the kernel? | No, it's a program (decided earlier, by you) | kernel.md Q2 |
| How programs use the kernel | System calls for the disk and the console; libraries that keep no shared state are bundled | kernel.md Q3 |
| Where programs go in memory | Relocated, each stacked above the program that started it | kernel.md Q4 |
| How far with the CPU | Stack pointer, faults, interrupts, timer and break now; multitasking later; interrupts checked before every instruction | kernel.md Q5 |
| When the shell exits | The kernel starts it again; if it can't start at all, the kernel says why and halts | kernel_exec.md Q2 |
| Who edits the typed line | The kernel's console | kernel_exec.md Q3 |
| Finding programs | `/bin/<name>.bin`, then the current directory; `.bin` never needed | kernel_exec.md Q4 |
| `ls` or a linker | `ls` and command-line tools; no linker on the machine | kernel_changes.md Q1 |
| `printf` | Variadic functions in the compiler, up to 8 extra arguments | kernel_changes.md Q2 |
| Arguments | `main(argc, argv)`; split at spaces, double quotes group; 16 words, 255 characters | kernel_changes.md Q4 |
| The screen after a program | The kernel redraws the text; no `-- more --` yet | kernel_changes.md Q5 |
| `ls > file` | Later | kernel_changes.md Q6 |
| How the prompt looks | Read from `/etc/shell_header.conf`, with ``` ``CWD`` ``` and color names; box characters need five more glyphs | [shell.md](shell.md) §2 |
| System-call table address | `0x15A1C`, past the boot sector copy and boot channel bios2 leaves at `0x15818` | kernel.md §10 |
| Which programs are relocatable | Every `.c` in a project's `[files]`, and `cc.py --relocatable`; the installer and the system stay at `0x20000` | kernel.md Q6 |
| Heap limits | A `__heap_limit` word in every program. The kernel's stops at `0x00FFFFE0`; `exec` writes each program's | kernel.md Q7 |
| Which disk the kernel mounts | `BOOT_CHANNEL` when it's the hard disk or the CD, otherwise the hard disk; bios2 writes the word for channel 1 too | kernel.md Q8 |
| Console and shell with the kernel | A console and a simple shell in phase 3; `printf`, the prompt file and colors in phase 4 | kernel.md Q9 |
| Assembly in the kernel's C | `#asm "kernel.asm"`, a directive in the C file | kernel.md Q10 |
| What the example disc boots | The kernel and its shell; the calculator, cube and file browser are programs in `/bin` | kernel.md Q11 |
| File descriptors, clean-up, panics, Ctrl+C at the prompt | 0–2 the console, `open` from 3; the disk mounted again after each program; a fault in kernel code halts; Ctrl+C clears the typed line | kernel.md Q12 |

---

## 7. Build order

1. ***Done.*** **Relocatable programs:** new startup code in the compiler,
   program files with an address list to patch, a heap limit in each
   program, and `cc.py --relocatable` (kernel.md §17).
2. ***Done.*** **The CPU:** `GETSP`, `SETSP` and faults; then interrupts, the
   timer and break (kernel.md §13, §17).
3. ***Done.*** **The kernel:** system calls, `exec`, `exit` and faults,
   installed as the project's `system`. With it, a console and a simple
   shell with `ls`, `cat` and `echo` (kernel.md Q9).
4. ***Done.*** **`printf` and the rest of the shell**
   ([phase4_plan.md](phase4_plan.md)). 4a is built: variadic functions and
   `printf`; colors and cursor codes in the console; the prompt from
   `/etc/shell_header.conf` ([shell.md](shell.md)); `mkdir`, `rmdir`, `rm`,
   `mv`, `cp` and `clear`; a sorted `ls` with `-l`; and Ctrl+C only while a
   program runs. 4b.1 and 4b.2 are built too
   ([phase4b_plan.md](phase4b_plan.md)): fast scrolling, line editing and
   history, Tab completion, scrollback with the mouse wheel, break per
   program, `more`, and `edit`, a mini nano.
5. **A boot screen and a startup script,** from `/etc/boot.conf`.
6. **The serial debug port,** with a panel beside the screen.
7. **The launcher.**

Phases 5 to 7 are sketched in [phase4_plan.md §11](phase4_plan.md#11-later-phases).
Multitasking stays optional.
