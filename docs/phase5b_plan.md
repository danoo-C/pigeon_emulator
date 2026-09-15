# Phase 5b: what the machine says on the debug port

> **Status: plan, 2026-09-15, and built the same day
> ([§8](#8-as-built)).** Part 5b of
> [phase5_plan.md](phase5_plan.md): its steps 5 to 8, and step 11's docs for
> this part. 5a gave the machine a debug port and the host a way to show it;
> this part makes stage 1 of the BIOS, bios2, the installer and the kernel
> say where they've got to. You asked for this file and for the building to
> carry on, so the questions it raised are decided in [§6](#6-decisions),
> each with its reason. Facts marked *checked* were read in the code,
> *measured* ones were run; the rest is reasoned.

---

## 1. Where things stand

- **5a is built and not committed.** The port is `CH_DEBUG`, channel 8, and
  `<pigeon/debug.h>` has `dbg_write`, `dbg_print` and `dbg_printf`. A line
  costs 6,179 instructions and 232 bytes of frame stack *(measured)*.
- **Stage 1 has room.** Just before it jumps to bios2, R/W is 0, LENGTH is 8
  and A points at the data window *(checked: `firmware/bios.asm:86–101`)*.
  With 12 bytes of text it assembled to 1,012 of 1,024 bytes *(measured)*.
- **bios2** *(checked: `firmware/bios2.c`)*:
  - `check_program` and `check_disk` fill each device's detail: `none`,
    `too big`, `N bytes`, `no disk`, `no disc`, `no boot sector`, `bootable`
    or the volume label (`:161`, `:181`);
  - `say` builds every reason a boot failed or can't start (`:223`);
  - `main` counts down, and Esc breaks to the menu while Enter, or the time
    running out, boots the first bootable device (`:382`);
  - `boot` hands over to `0x20000`, or to a boot sector at `BOOT_ENTRY`,
    `0x15898` (`:302`);
  - `menu` boots on Enter or says it can't, and checks the CD again when the
    drive's generation counter moves (`:335`);
  - it includes no `stdio.h`, and is 50,488 bytes built.
- **The installer's `main`** mounts the disc, reads its title from
  `/pigeon.txt`, sizes the hard disk, counts the files, waits for Enter or
  Esc, then `install` formats, `copy_file` copies each file and
  `make_bootable` writes the boot record. Enter restarts, by calling address
  0 *(checked: `user/os/installer.c:300`, `:137`, `:236`, `:277`)*.
- **The kernel:**
  - `k_exec` returns early when a program can't be loaded, then calls it
    through `exec_call`. `k_exit`, a fault or Ctrl+C in `k_fault`, and `q` or
    Ctrl+C at `-- more --` in `page_wait` all come back through `exec_abort`
    *(checked: `user/os/kernel.c:1241`, `:1301`, `:1305`, `:912–918`)*;
  - `struct proc` keeps no path (`:109`), and a panic halts inside `k_fault`;
  - `main` mounts the channel bios2 left, or the hard disk, and starts the
    shell again whenever it ends (`:1356`).
- **The kernel can know its own size.** bios2 copies the booted disk's
  block 0 to `BOOT_LOAD_ADDR`, and the boot record in it holds `/boot.bin`'s
  size, which nothing overwrites before the kernel runs *(checked:
  `bios2.c:318`, `emulator/memory_map.py`)*. Loaded straight into RAM, as the
  kernel tests do, it has no record.
- **`started` may be wrong after a program.** `k_exec` clears it and sets it
  just before calling the program, never after, and `main` reads it once
  the shell ends. A shell whose last command failed to start, and which then
  exits, would look to `main` as if it never started *(reasoned, then run:
  after `junk: not a program` and `exit`, the kernel printed
  `cannot start /bin/sh.bin: ok` and halted)*.
- **The shell looks a name up before `exec`,** so a name that isn't there
  never reaches the kernel *(checked: `user/os/bin/sh.c:266`)*.
- **The tests to build on** *(checked)*: `test_bios2.py`'s `boot()` for
  stage 1 and `Power` for bios2; `test_install.py`'s three runs; and
  `test_kernel.py`'s `Console` with its stand-ins `deep`, `div0`, `spin`,
  `panic`, `nested` and `lines`, and `more`.

---

## 2. What the port shows

Installing from the disc, restarting, and running two programs, one of which
divides by zero *(illustrative; the formats are §6's)*:

```
[   0.000] [bios] bios2
[   0.040] [bios2] Program: none
[   0.041] [bios2] Hard disk: no boot sector
[   0.043] [bios2] CD: PIGEONOS, bootable
[   0.044] [bios2] counting down 5 s to CD
[   5.050] [bios2] time: booting CD
[   5.051] [bios2] CD: its boot sector, at 0x00015898
[   5.402] [installer] disc on channel 6: PigeonOS 0.1
[   5.410] [installer] hard disk: 4096 K
[   5.600] [installer] 19 files to copy; /boot.bin will boot
[   7.200] [installer] Enter: installing
[   7.210] [installer] formatting the hard disk as PIGEONOS
[   7.300] [installer] copying /boot.bin
  ...
[   9.900] [installer] boot record: /boot.bin, block 3, 229528 bytes
[   9.901] [installer] installed 19 files; Enter restarts
[  11.000] [installer] restarting
[  11.001] [bios] bios2
  ...
[  16.200] [kernel] started, 229528 bytes at 0x00020000
[  16.210] [kernel] mounted channel 2, PIGEONOS
[  16.300] [kernel] exec /bin/sh.bin at 0x01000000, depth 1
[  20.100] [kernel] exec /bin/ls.bin at 0x0103C000, depth 2
[  20.180] [kernel] /bin/ls.bin ended: 0
[  25.000] [kernel] exec /bin/div0.bin at 0x0103C000, depth 2
[  25.010] [kernel] /bin/div0.bin ended: divided by zero at 0x0103D1A8
```

---

## 3. Steps

### Step 1. Stage 1

- **`firmware/bios.asm`:** after bios2 has arrived whole, a `WRITE_DMA` of
  `[bios] bios2\n` from the BIOS's own bytes, then the jump. Seven
  instructions, 56 bytes, and 13 of text after the last instruction.
- **Nothing on the fallback path:** with no bios2, or one that can't be
  trusted, stage 1 boots channel 1 as before and says nothing.
- **Tests,** in `test_bios2.py`: the line on the port before bios2 runs; no
  line without a bios2, or with a bios2 refused; the BIOS still 1 KB or less.

### Step 2. bios2

- **Each device checked,** once at power-on and again after a boot that
  came back, and the CD again when a disc goes in or out:
  `[bios2] Hard disk: PIGEONOS, bootable`, `[bios2] CD: no disc`.
- **The countdown:** `counting down 5 s to CD`, then what ended it:
  `Enter: booting CD`, `time: booting CD` or `Esc: the menu`. With nothing
  bootable, `nothing to boot: the menu`.
- **Each hand-over:** `Program: 816 bytes, at 0x00020000`, or
  `Hard disk: its boot sector, at 0x00015898`.
- **Every reason `say` gives,** as a line too: `Program: load failed`,
  `Hard disk: boot failed`, `CD: can't boot`.
- **Each menu choice:** `menu: Hard disk`.
- **Tests,** in `test_bios2.py`: a program booted by Enter, and by the time
  running out; the hard disk from the menu after Esc; the CD counted down;
  a load that fails; a device that can't boot; a disc going in.

### Step 3. The installer

- **`[installer] `** before: the disc and its title, or why it can't mount
  it; the hard disk's size, or no disk; the files to copy and whether it
  will boot; Enter or Esc; formatting; each file copied; the boot record's
  block and size; every failure, with its reason; the count installed; and
  the restart.
- **Tests,** in `test_install.py`: the lines of the whole install, in order,
  with every file named; a cancel, with nothing after Esc; a disk too small,
  ending with its failure. After the restart, the kernel's size line names
  exactly `/boot.bin`'s size.

### Step 4. The kernel

- **`[kernel] started, N bytes at 0x00020000`,** from the boot record when
  bios2 left one for a disk, and `started at 0x00020000` when not; then
  `mounted channel 2, PIGEONOS`, or `cannot mount channel 2: ` and why.
- **Every `exec`:** `exec PATH at 0x01000000, depth 1` just before the call,
  or `exec PATH: ` and why it couldn't start.
- **How each ended,** logged by `k_exec` when the program comes back:
  - a return: `PATH ended: 0`;
  - `exit`: `PATH ended: exit 42`;
  - a fault: `PATH ended: divided by zero at 0x0103D1A8`;
  - Ctrl+C: `PATH ended: Ctrl+C`;
  - `q` at `-- more --`: `PATH ended: q at -- more --`.
  `struct proc` gains the path, how the program ended and the fault's
  address; `k_exit`, `k_fault` and `page_wait` fill in how.
- **A panic:** `[kernel] panic: ran a bad instruction at 0x...`, before the
  console's message and the halt. It is logged on `fault_frames`, 1,024
  bytes, where `dbg_printf` needs 232.
- **The shell ending:** `the shell ended; starting it again`, or
  `cannot start /bin/sh.bin: ` and why.
- **`started`,** if step 4 finds §1's case real: set again after the program
  returns, with a test that `exit` after a failed command restarts the shell.
- **The kernel never calls `printf`,** which would go through its own
  system calls. Nothing of the console reaches the port.
- **Tests,** in `test_kernel.py`:
  - boot, mount and the shell, in order;
  - an `exec` and its end with its status, and `exit` told from a return;
  - a nested `exec` at depth 3;
  - a fault with its address inside the program; Ctrl+C; `q` and Ctrl+C at
    `-- more --`;
  - a program that can't start;
  - a panic as the last line;
  - no console text on the port;
  - the cost: the same first `exec` with and without the port.

### Step 5. Docs, breakages and the suite

- **Docs:** os_cd.md (stage 1's size, bios2's and the installer's lines),
  kernel.md §17, kernel_overview.md, the README's `--serial` example,
  phase5_plan.md's status, and this file's as-built notes.
- **Deliberate breakages** for each line and each end, every one failing a
  test; then the full suite.

---

## 4. Order and commit

Steps 1 to 3 are firmware and the installer, and step 4 the kernel; each
step's tests run as it's built, and the full suite once at the end. 5b is one
commit, as phase5_plan.md §6 has it.

---

## 5. Risks

- **Step budgets in the tests.** bios2, the installer and the kernel each
  gain `stdio.c`, `sys.c` and `debug.c`, about 20 KB, and a few thousand
  instructions a line. The tests that wait a number of instructions may
  need more.
- **Names clashing in the kernel.** `sys.c` comes along with `stdio.c`, and
  units share one namespace; the build says so at once if two names meet.
- **The stepping clock.** The port reads it once for each write that starts
  a line, which moves the guest's timers 0.05 s in the tests. bios2's
  countdown tests already allow its first second either way.
- **`q` ending more than one program.** `page_wait` can end the program
  `more` ran and everything under it at once; only the one `k_exec` returns
  to is logged (§6, 5).

---

## 6. Decisions

Decided 2026-09-15 (left to me), since you asked to carry on building:

1. ~~**Stage 1's text**~~: **`[bios] bios2`,** not phase5_plan's
   `bios: bios2`. Every other line has its source in brackets; it's one byte
   longer, and 1,013 bytes still fit.
2. ~~**The kernel's size**~~: **from the boot record bios2 copied,** when it
   booted a disk, since a program built for a fixed address has no label
   for its end. Without a record the line leaves the size out rather than
   guess.
3. ~~**Telling the ends apart**~~: **a field in `struct proc`,** set where the
   end happens, not guessed from the status: a program may return -100,
   which is also `ENDED_DIV_ZERO`.
4. ~~**An `exec` that can't start**~~: **logged, one line,** since a
   `not a program` is what you'd look for there.
5. ~~**Programs that `q` or Ctrl+C at `-- more --` end together**~~: **only
   the one `k_exec` comes back to is logged.** The others never return
   anywhere a line could be written, and the shell prints nothing for them
   either.
6. ~~**bios2's failures**~~: **logged inside `say`,** so every reason the
   screen gives is on the port too, with no second list to keep.
7. ~~**The installer and each file**~~: **one line a file,** about twenty on
   the example disc, as the screen shows them one by one.
8. ~~**Addresses**~~: **`0x` and eight hex digits,** upper case, as the
   phase 5 plan shows them.

---

## 7. Not in 5b

- **The Serial panel,** in the browser and pygame: part 5c.
- **Mirroring the console** to the port, as phase 5 decided.
- **Boot sector lines:** it has 8 bytes free, as phase 5 found.

---

## 8. As built

- **Stage 1:** 1,013 of 1,024 bytes, 125 instructions.
- **bios2:** 72,212 bytes, with `stdio.c`, `sys.c` and `debug.c` along for
  `dbg_printf`.
- **The installer:** 165,060 bytes.
- **The kernel:** 236,068 bytes, from 209,528. The first `exec` of `echo`
  costs 118,130 instructions with the port and 107,904 without it, so
  10,226 for its two lines *(measured)*; a test holds it under 12,000.
- **Found and fixed:** `started`, as §1 says. `k_exec` sets it again when a
  program returns, and a test runs `exit` after a command that couldn't
  start.
- **The install test and your `corrupter.bin`.** The project file now puts
  a 20th file on the disc, so `test_install.py`'s whole install stops at
  `Installed 20 files.` before its new checks, and `test_project.py`'s count
  fails the same way. With that one file left out of the disc in memory,
  the whole install passes, the installer's and the kernel's lines included.
- **Tests:** 17 new cases: 8 in `test_bios2.py`, 9 in `test_kernel.py`, and the
  debug port's lines checked in `test_install.py`'s three runs. 39 deliberate
  breakages, each failing a test; the kernel's size line needed a test of
  its own first. The full suite: 1,353 of 1,355 pass, and the other two are
  the file counts above.
