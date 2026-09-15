# A kernel and a shell

> **Status: phases 1 to 3 of §17 are built: relocatable programs, the CPU's
> interrupts and faults (§13), and the kernel, with a console and a simple
> shell (§10–§12). The rest is a proposal, but most of it has been run.** A prototype added the proposed CPU instructions to the emulator
> at runtime, and ran C kernels and programs compiled by `pigeon-cc` through
> them (§16). The console (§12) was not prototyped; boot (§4–7) is built
> instead ([os_cd.md](os_cd.md)). Facts in §2 were checked in the code again
> on 2026-09-14, when §16's scripts, all but P7, were also run again. Anything
> reasoned but not run is marked *unverified*. Details live in
> [kernel_exec.md](kernel_exec.md)
> (running a program, relocation) and [kernel_changes.md](kernel_changes.md)
> (printing from programs). Every question is now decided, in
> [§18](#18-open-questions). Booting from disk is built, as
> [os_cd.md](os_cd.md) describes. For a short picture of how the pieces fit,
> start with [kernel_overview.md](kernel_overview.md).

| Area | Today | Proposed | Run in the prototype |
|---|---|---|---|
| Boot | Built: the BIOS loads bios2 from channel 7; bios2 boots a program on channel 1, or a disk's boot sector, which loads `/boot.bin` ([os_cd.md](os_cd.md)) | The kernel is that `/boot.bin` | No; built and tested instead |
| Programs in memory | A C program is built for `0x20000`, or since phase 1 as a program file that loads anywhere | The kernel stays at `0x20000`; each program is relocated to wherever there's room | Yes; relocation is built |
| Starting a program | Since phase 3, the kernel's `exec` | The kernel loads `/bin/<name>.bin`, patches it and calls it | Yes, from a PigeonFS disk |
| Ending one | `HALT` stops the machine; since phase 2, `GETSP` and `SETSP` make `exit()` possible | Return from `main`, or `exit()` from anywhere | Yes |
| Services | Each program bundles its libraries; since phase 3, one the kernel runs calls it through `<pigeon/sys.h>` | A system-call table the kernel fills in | Yes |
| Crashes | End the emulator, unless a vector table has a handler for the fault (phase 2) | A fault reaches the kernel, which abandons the program | Yes |
| Ctrl-C | A break interrupt once HID's `SET_BREAK` is on (phase 2) | The break abandons the program | Yes; the device side is built in phase 2 |
| Timer | Polled, or ticking with an interrupt since phase 2 | A periodic interrupt | Yes; the device side is built in phase 2 |
| Multitasking | Impossible | Optional: the timer interrupt switches stacks | Yes, three programs |
| Console, shell | Since phase 3, a console in the kernel and a simple shell, `/bin/sh.bin` | A console in the kernel; the shell is a program | No; built instead |

---

## 1. What is possible

- **With the CPU as it is:** one program at a time. The shell runs a program
  and gets control back when it returns from `main`. A crash ends the
  emulator, and nothing can stop a program that never returns.
- **With six new instructions (§13):** `exit()` from anywhere, crashes that
  return to the shell, Ctrl-C, a timer tick — and preemptive multitasking, if
  the kernel wants it (§14). All of these ran in the prototype. Phase 2 built
  the instructions, faults and interrupts; the kernel that uses them is next.
- **Still not possible:** memory protection. Any program can overwrite the
  kernel or another program.

---

## 2. The facts that decide it

Checked in the code, not assumed:

**The CPU**
- There were 29 instructions and six registers, `A` to `F`
  (`emulator/instruction_set.py`, `REGISTER_COUNT` in `emulator/memory_map.py`).
  Phase 2 appended six instructions (§13).
- There were no interrupts, traps or privilege levels. Nothing in `cpu.py`,
  `machine.py` or `io_controller.py` mentioned any. Since phase 2 there are
  interrupts and faults; there are still no privilege levels.
- The stack pointer moved only through `PUSH`, `POP`, `CALL` and `RET`. Since
  phase 2, `GETSP` reads it into a register and `SETSP` loads it.
- **The zero and less flags live in the Python `CPU` object.** Only `CMP` sets
  them, and no instruction reads or writes them. Since phase 2, entering a
  handler pushes them in the flags word, and `IRET` puts them back.
- `JMP` and `CALL` take a register as well as an address (`reg_or_imm`).
- Opcodes are numbered in declaration order, and the file says new instructions
  can safely be **appended**.
- Dividing by zero raised a Python exception, and an unknown opcode raised
  `RuntimeError`. Either one ended the emulator, not just the program. Since
  phase 2 both are faults; with no handler, they raise exactly as before.
- **Instructions run in three places:** `CPU.run` (tests, the benchmark),
  `Machine.step` (the debugger, tests) and a copy inlined in `Machine.run`
  (what users run). `test_run_and_step_execute_identically` keeps the last two
  in step.
- `Machine.run` does its slow work — the clock, the display — once every
  10,000 instructions (`CLOCK_SAMPLE_INTERVAL`). It didn't catch a fetch past
  the end of memory: that `try` was commented out. Since phase 2 it catches it
  as a fault, and the slow path asks the devices for interrupts.

**IO and devices**
- **A device is programmed one field at a time** — `IO_RW`, `IO_CMD`,
  `IO_LEN`, `IO_ADDR` — and the write to `IO_CH` fires the command, which
  finishes before the next instruction. The reply is then read from the data
  window (`lib/pigeon/io.h`, `io_controller.py`). There is one header and one
  window for the whole machine.
- The timer is wall-clock based and polled — `START`, `STOP`, `RESET`,
  `STATUS` — and raised nothing. Since phase 2, `TICK` raises the timer
  interrupt.
- The keyboard and mouse are fed by an HTTP server thread, into queues guarded
  by a lock (`devices/hid.py`). Ctrl has keycodes, `KEY_LCTRL` and `KEY_RCTRL`.

**The memory map** (`emulator/memory_map.py`)

| Range | Size | What |
|---|---|---|
| `0x00000000`–`0x000003FF` | 1 KB | BIOS |
| `0x00000400`–`0x00001417` | 4 KB + 24 B | IO header and data window |
| `0x00001418`–`0x00015817` | 81 KB | framebuffer, 192×108 |
| `0x00015818`–`0x0001FFFF` | ~42 KB | since os_cd.md: the boot sector copy and boot channel bios2 leaves (`BOOT_LOAD_ADDR`, `BOOT_CHANNEL`); the rest unused |
| `0x00020000`–`0x0011FFFF` | 1 MB | program code and data (`PROGRAM_LOAD_ADDR`) |
| `0x00120000`–`0x0015FFFF` | 256 KB | frame stack (`HEAP_START`) |
| `0x00160000` → `0x07F00000` | ~126 MB | heap |
| → `0x07FFFFFC` | | hardware stack, growing down (`STACK_TOP`) |

**The compiler and assembler** (`compiler/`, `assembler/assembler.py`)
- Every C program is emitted with `.ORG PROGRAM_LOAD_ADDR`, or the address
  `cc.py --org` names (`codegen.py:88`). Since phase 1, `cc.py --relocatable`
  emits one for `LINK_ADDR` instead, and builds it twice (§8).
- The startup code `__start` sets the frame pointer `F` to `__frame_base`,
  sets up the heap, calls `main` with no arguments, then **`HALT`s**
  (`codegen.py:102–108`). A relocatable program's returns instead
  (`codegen.py:121`, §9).
- `__frame_base = HEAP_START`, or `__image_end` in a relocatable program; the
  frame stack is 262,144 bytes, and `__heap_base` follows it
  (`codegen.py:210–214`). `__heap_ptr` is a word the compiler emits in every
  program, and since phase 1 `__heap_limit` is the next one
  (`codegen.py:192–197`). `mem.c` reaches both as `extern unsigned`.
- `cc.py` takes the sources, `-o`, `-S`, `-I`, `--org ADDR`,
  `--project FILE` and, since phase 1, `--relocatable`.
- `mem.c`'s heap limit is `__heap_limit`, or when that is 0 a typed
  `0x08000000 - 0x00100000` (`mem.c:94–100`).
- Function pointers compile to `CALL E` and have tests.
- **Variadic functions are rejected** (`parser.py:288`), so there is no
  `printf` (kernel_changes.md §3.1).
- **A function declared but not defined can't be called** — "not declared".
  `extern int name;` plus a cast to a function pointer can, which is how C
  reaches hand-written assembly.
- **Every instruction needs a row in the assembler's `SYNTAX` table**, checked
  at import (`assembler.py:240`). The disassembler reads the instruction table
  itself.
- **`NAME = expression` was worked out before labels had addresses**, so it
  couldn't use a label. Since phase 1 such a definition is settled after
  layout. Code placed after data must start at a multiple of 8.

**The BIOS** (`firmware/bios.asm`, `emulator/bios.py`)
- It must fit in 1 KB, which is 128 instructions; `bios.py` refuses anything
  larger. Today's BIOS is 944 bytes, 118 instructions: it loads bios2 from
  channel 7, and runs the old loader below only when there is no bios2
  ([os_cd.md](os_cd.md) §4).
- The old loader reads channel 1 in 4 KB chunks, copies to
  `PROGRAM_LOAD_ADDR`, paints a progress bar, waits two seconds on the timer,
  clears the screen and jumps.
- It stops when a chunk returns 0 bytes. It does not treat `0xFFFFFFFF`, the
  answer from a channel with no device, as an error. Read from the code: with
  channel 1 empty it would try to copy 0x3FFFFFFF words *(unverified, not run)*.
- `Machine` registers channel 1 only when it is given a program, and the CLI
  allows running with none.
- **bios2 writes `BOOT_CHANNEL` only when it boots a disk or a disc**
  (`firmware/bios2.c:322`). Booting a program on channel 1 leaves the word as
  it was (`:305–310`).

**The filesystem** (`lib/pigeon/fs.c`, `tools/pfs.py`, `docs/filesystem.md` §3.1)
- Block 0 is the superblock. Its first word is the magic `0x53464750`, stored
  as the bytes `50 47 46 53`, so its first byte read as an opcode is 80 —
  past the 29 that exist. Jumping to it would stop the emulator.
- `FS__FAT_START` is fixed at 1, and `fs_mount` rejects any other value.
- The superblock's bytes 52–63 and 128–511 were unused. A bootable disk now
  keeps its boot record and boot sector there ([os_cd.md](os_cd.md) §6).
  `fs_mount` checks only words 0–6 and 24–26, and `__fs_put_hints` updates the
  block in place, so the guest keeps those bytes.
- **So does `pfs.py`, since os_cd.md:** `_write_super()` updates block 0 in
  place instead of rebuilding it from zeros. `fsck` doesn't check the boot
  record; `pfs.py info` shows it.
- The HDD refuses a DMA transfer into or out of memory below
  `PROGRAM_LOAD_ADDR` (`emulator/devices/hdd.py`).
- `fs.c` keeps its cache, open files and current directory in `static`
  globals, so it can't be entered twice at once.

**Everything else**
- `display.c` keeps its state in each program: `disp_target`, `disp_back` and
  `disp_hw`. A program that calls `disp_use_back_buffer()` points the display
  at a buffer on its own heap.
- The screen holds 32×12 characters. `display.h` has `disp_text` and
  `disp_char`, and nothing that scrolls or tracks a cursor.
- A program that includes `<pigeon/fs.h>` is about 101 KB. `user/files.c` and
  `user/disc.c` are about 170 KB and 168 KB.

---

## 3. What already works

- **Jumping into loaded code**, through `JMP reg` or `CALL reg`, or a C
  function pointer.
- **Finding and loading a program from disk**, with `fs.c`.
- **Loading it fast.** One `READ_DMA` is 84 instructions whatever it moves, so
  even a boot sector of a few dozen instructions can load a whole kernel.
- **Room.** 128 MB of RAM, a 1 MB program region, and a 126 MB heap.
- **Devices.** Display, keyboard and mouse, timer, and the CD drive — a natural
  way to install programs onto the disk.

---

## 4. Boot: the BIOS loads a sector

> **Replaced by [os_cd.md](os_cd.md).** The 1 KB BIOS loads a second stage,
> bios2, from a firmware device on channel 7, and bios2 copies the boot sector
> into memory. Block 0's layout is option A of §5.

**Files:** `firmware/bios.asm`, `emulator/machine.py`, `emulator/cli.py`, `config.json`

- **Read one 512-byte block** from the boot disk — channel 2 — into a boot
  address, check a boot signature, and jump. If the disk is missing, the read
  is short, or the signature is wrong, draw an error and `HALT`.
- **Treat `0xFFFFFFFF` as "no disk"**, alongside a short read. Today's BIOS
  only checks for 0.
- **Size is not a problem.** A sector loader has no chunk loop, so it is
  smaller than today's BIOS and fits the 128-instruction limit. The progress
  bar and the two-second wait can go.
- **The boot address can be in the unused gap below `0x20000`.** The BIOS reads
  through the IO window, which works anywhere. What stage 1 loads next — the
  kernel — must go at or above `PROGRAM_LOAD_ADDR`, because that is the lowest
  address the HDD accepts for DMA.
- **The machine needs a "boot the disk" mode** with nothing on channel 1. The
  CLI already allows no program; the BIOS is what has to handle it.
- **Keep the old path** for `start_emulator.py <program>` — every program in
  `user/` depends on it, and it can start the kernel before boot exists.

---

## 5. Where the boot code and the kernel live on disk

> **Built as option A** ([os_cd.md](os_cd.md) §6). The kernel is the ordinary
> contiguous file `/boot.bin`, not `/kernel.bin`, and `fs.c` and `pfs.py`
> both keep bytes 52–63 and 128–511. `fsck` doesn't check the record yet.

**Files:** `tools/pfs.py`, possibly `lib/pigeon/fs.c`, `docs/filesystem.md` §3

The BIOS cannot jump to the start of block 0 as it is (§2). Two ways round it:

**Option A — no format change.** The BIOS loads block 0, checks the PigeonFS
magic, and jumps to **offset 128**. The boot code lives in bytes 128–511:
384 bytes, 48 instructions.

- A **boot record** fits in the unused bytes 52–63: three words — a boot
  signature, the kernel's first block, and its length in blocks.
- `fs.c` already leaves those bytes alone.
- **`pfs.py`'s `_write_super()` must be changed to keep bytes 52–63 and
  128–511** instead of rebuilding the block from zeros.

**Option B — reserved boot blocks.** The boot sector takes block 0, the kernel
takes blocks 1 to K, and the filesystem starts after them.

- `fs.c` and `pfs.py` both assume the superblock is block 0 and
  `FS__FAT_START` is 1. Both block devices would need a volume base offset.
- It is a new version of the on-disk format.

**Finding the kernel.** Forty-eight instructions cannot walk the file
allocation table. So either:

- the kernel lives in reserved contiguous blocks (option B), or
- the kernel is an ordinary file, `/kernel.bin`, **guaranteed contiguous**,
  whose position the install tool writes into the boot record (option A).

The second needs care. If anything rewrites `/kernel.bin` and its blocks move,
the record points at the wrong place and the disk no longer boots. `fsck`
should check that the record still matches a contiguous file of the right
size.

---

## 6. Installing it

> **Built differently** ([os_cd.md](os_cd.md) §6–8). There is no
> `install-boot`: `pfs.py boot PATH` writes the record and the sector, and
> `cc.py --project` and the installer put the kernel on the disk as the
> project's `system`.

**Files:** `tools/pfs.py`

- **`pfs.py install-boot boot.bin kernel.bin`**: write the stage-1 code,
  place the kernel contiguously, and write the boot record.
- **`fsck`** checks the record (option A) or the reserved blocks (option B).
- **`mkfs`** leaves room for it, if option B is chosen.
- **Programs are ordinary files in `/bin`**, copied in with the existing
  `pfs.py put`. P5 did exactly that.

---

## 7. Stage 1 has to be hand-written assembly

> **Built** as `firmware/boot.asm`: 376 bytes, 47 instructions
> ([os_cd.md](os_cd.md) §6). It reads through the IO window rather than by
> DMA, because the CD has no DMA.

**Files:** new `firmware/boot.asm`

No C program can be 48 instructions: each one carries its startup code, its
frame-stack and heap layout, and its libraries. With DMA, stage 1 is about a
dozen instructions:

1. write `[KERNEL_LOAD_ADDR, byte count]` into the IO data window
2. send `READ_DMA` for the kernel's blocks
3. check the reply
4. `JMP #KERNEL_LOAD_ADDR`

The assembler already supports `.ORG` at any address.

---

## 8. Memory: the kernel stays put, programs are relocated

**Files:** `compiler/codegen.py`, `compiler/cc.py`, a new build step,
`lib/pigeon/mem.c`

Every C program is built for `0x20000`, so a kernel and a program — or a shell
and the program it runs — would land on top of each other. An earlier version
of this section proposed separate compile-time layouts. **Relocation is
simpler and more general**, and it has been run
([kernel_exec.md §4–7](kernel_exec.md#4-the-hard-part-where-does-ls-go)).

**How a program becomes relocatable**
- **Assemble it twice, at two addresses, and compare.** A word that differs
  holds an address; every other byte is identical. The list of differing words
  is what the loader patches. Across every C program in `user/`, every
  differing word differed by exactly the distance between the two addresses,
  and no other byte differed at all. The largest program has 1,940 words to
  patch, 99% of them in an instruction's address field.
- **The frame stack and heap move to just after the image.** The compiler
  emits `__frame_base = __image_end`, which the assembler settles once labels
  have addresses. The prototype wrote `__image_end + frame size` into the
  instructions instead, from before the assembler could.
- **A program file** is a 32-byte header — magic, version, image size,
  entry, patch count, frame-stack size, where `__heap_ptr` is, and the address
  it was built for — then the image, then the offsets. The magic is `PGEX`,
  `PROGRAM_FILE_MAGIC` in the memory map; the prototype's was `PGX1`.
- **What is built this way** (Q6): every `.c` in a project's `[files]`, and
  `cc.py --relocatable`. The installer and the `system` stay built for
  `0x20000`, where the boot sector puts them.
- **Each program has its own heap limit** (Q7): `__heap_limit`, the word the
  compiler emits right after `__heap_ptr`, so the header needs no new field.

**Where the kernel puts it**

```
0x00000000  BIOS, IO window, framebuffer
0x00015818  boot sector copy, boot channel      left by bios2 (os_cd.md §5.3)
0x00015A1C  system-call table                   just past the boot channel
0x00020000  kernel image                        fixed; vector table and interrupt frame stack are kernel globals
0x00120000  kernel frame stack and heap         its heap stops at 0x00FFFFE0 (Q7)
0x01000000  shell │ image │ frame stack │ heap →
            ls    │ image │ frame stack │ heap →     loaded just above the shell's heap top
            ...
0x07F00000  1 MB of headroom                    mem.c's limit today
0x07FFFFFC  hardware stack, shared, grows down
```

- **Programs stack like plates.** A parent always waits for its child, so the
  child goes just above the parent's current heap top, and memory never
  fragments. P4 ran three levels deep: a 1,000-word block on the middle
  program's heap was intact after its child finished, and each place was
  reused by the next program.
- **The file loads straight into place.** `fs_load` reads the whole file to 32
  bytes below the program's address, so the header lands in the gap and the
  image lands where it runs. P5 loaded every program this way by DMA.
- **The kernel's heap stops at `0x00FFFFE0`**, where the shell's header lands,
  and each program's at `0x07F00000`. `exec` writes a program's
  `__heap_limit` before calling it (Q7). Today `mem.c` returns a typed
  `0x07F00000` for every program.
- **Only multitasking breaks the plates** (§14).

---

## 9. Starting and ending a program

**Files:** `compiler/codegen.py`, the kernel

**Startup code** replaces today's, which ends in `HALT`. The kernel calls the
entry point as `entry(argc, argv)`. The startup code saves the caller's `F`,
points `F` at its own frame stack, writes `argc` and `argv` into `main`'s
frame, sets up the heap, and calls `main`. It then restores `F` and returns
`main`'s value in `A` (kernel_exec.md §8). This startup code ran in P4 and P5.

**`exit(code)` from anywhere** needs `GETSP` and `SETSP` (§13). The kernel
calls every program through one assembly routine, which remembers where the
stack was. `exit` puts it back, so the kernel's call returns as if the
program had returned. As run in P4 and P5:

```asm
; int exec_call(entry, argc, argv, save)  -- save points at two words
exec_call:
    ADD C, F, #12
    MRW D, C            ; D = save
    GETSP A
    MWW D, A            ; save[0] = SP: the return address into the kernel
    ADD D, D, #4
    MWW D, F            ; save[1] = F
    MRW E, F            ; E = entry
    ADD C, F, #4
    MRW A, C            ; argc
    ADD C, F, #8
    MRW B, C            ; argv
    ADD F, F, #16       ; the program's caller frame: its argc and argv
    MWW F, A
    ADD C, F, #4
    MWW C, B
    EI
    CALL E
    DI
    SUB F, F, #16
    RET                 ; A = main's return value

; void exec_abort(code, save)  -- never returns to its caller
exec_abort:
    ADD C, F, #4
    MRW D, C            ; D = save
    MRW A, F            ; A = code
    MRW B, D
    ADD D, D, #4
    MRW F, D            ; F as it was inside exec_call
    SETSP B             ; SP as it was inside exec_call
    DI
    RET                 ; returns from exec_call with A = code
```

**The same `exec_abort` abandons a program** after a fault or a Ctrl-C (§13).
In P4, a program called `exit(42)` 50 calls deep, and another divided by zero
20 calls deep. The kernel carried on both times, and the hardware stack and
`F` were exactly restored when it finished.

**Without the CPU changes**, only returning from `main` gets back to the
kernel, and a crash still ends the emulator.

---

## 10. How programs call the kernel

**Files:** new `lib/pigeon/sys.h` and `sys.c`; the kernel

> **Built in phase 3:** `lib/pigeon/sys.h` and `sys.c` for programs,
> `lib/pigeon/syscall.h` for the slot numbers and codes both sides share,
> and the wrappers in `user/os/kernel.asm`, which also mark the kernel as
> running (Q12). The table is `SYSCALL_SLOTS` words at `SYSCALL_TABLE`, both
> in `memory_map.py`.

- **A system call is a call through a table** at a fixed address, `0x15A1C`,
  that the kernel fills in at boot. The prototype used `0x15818`, where bios2
  now leaves the boot sector copy and the boot channel, so the table moved
  past them. Programs call through a `typedef`'d
  function pointer:
  `((puts_fn)(*(unsigned *)SYSTAB))(s)`. This ran in P4–P6.
- **Each entry is a small wrapper:** `DI`, call the C function, `EI`, `RET`.
  So kernel code — `fs.c` above all — is never interrupted halfway through
  (§13.7). Kernel code calls the C function directly, because the wrapper's
  `EI` assumes interrupts were on.
- **The filesystem goes through the kernel.** In P5, `ls` listed `/bin`
  through a kernel call into the kernel's `fs.c`. Two copies of `fs.c` on one
  disk would disagree about the free blocks and the current directory
  (kernel_changes.md §3.3).
- **So does the console.** Its cursor and grid live in the kernel
  (kernel_changes.md §3.2).
- **Kernel C reaches its assembly routines** as `extern int name;` plus a
  cast (§2).

A first set of calls is in kernel_exec.md §8. Phase 4a added `mkdir`,
`rmdir`, `remove` and `rename`, slots 13 to 16, each passing straight to
`fs.c` ([phase4_plan.md](phase4_plan.md) step 6). Phase 4b added
`setcomplete`, `setbreak` and `paging`, slots 17 to 19
([phase4b_plan.md](phase4b_plan.md) steps 3, 5 and 6).

---

## 11. The kernel

**Files:** new, built at `0x20000`

> **Built in phase 3** as `user/os/kernel.c` and `kernel.asm` (§17). As
> built, cleaning up after a program also stops timers 0–15 and mounts the
> disk again (Q12), and a fault while kernel code runs is a panic.

**At boot:** set its own heap limit (Q7); mount the disk it was booted from —
the channel bios2 left at `BOOT_CHANNEL` when that is the hard disk or the
CD, and the hard disk otherwise (Q8); fill in the system-call and vector
tables; set up the console;
and start `/bin/sh.bin`. When the shell exits, the kernel starts it again. If
it can't be started at all, the kernel prints why and halts (kernel_exec.md
Q2). The prototype's kernel stopped.

**Running a program** (P4, P5):

1. choose its address: `0x01000000` for the shell, otherwise just above the
   parent's heap top
2. `fs_load` the file to 32 bytes below that address
3. check the header: magic, version, and that the file is as long as the
   header says
4. patch every listed address, and write its `__heap_limit` (Q7)
5. record it — address, heap-top pointer, saved stack — and `exec_call` it
6. take the status: from `main`, from `exit`, or from a fault or break

**Cleaning up after it** *(not in the prototype; built in phase 3)*: point the
display back at the screen, stop timers it started, empty the input queues,
and close files it left open.

**Size.** P5's kernel, with `fs.c`, was 123,300 bytes when run again after
phase 1, on 2026-09-14.

---

## 12. The console and the shell

- **The shell is a program** the kernel starts (decided, §18). A simple one
  is built in phase 3 (Q9).
- **A console in the kernel**, built in phase 3: a 32×12 grid, scrolling, a
  cursor, and line input with echo and backspace. The prototype's console
  was a text buffer.
- **Escape codes, built in phase 4a:** `ESC [ 30 m` to `ESC [ 37 m` for an
  ink, `ESC [ 39 m` for the console's own, `ESC [ 7 m` and `ESC [ 27 m` for
  inverse on and off, `ESC [ 0 m`, `ESC [ 2 J`, `ESC [ r ; c H` and
  `ESC [ K` ([phase4_plan.md](phase4_plan.md) step 3). Each cell keeps its
  look beside its character, so scrolling and redrawing keep the colors.
  Anything else is dropped rather than printed, and a sequence split across
  two `write` calls still works. Tidying after a program puts the ink back.
- **Programs print through it** with a system call, and with `printf` since
  phase 4a, when the compiler gained variadic functions (kernel_changes.md
  §3.1).
- **Typing a line, built in phase 4b.1** ([phase4b_plan.md](phase4b_plan.md)
  steps 1–4): editing anywhere in the line, 16 lines of history, Tab
  completion, Ctrl+L by the prompt's `ESC ] 133 ; A` mark, and 100 rows of
  scrollback with PgUp, PgDn and the mouse wheel (shell.md §5). Scrolling
  moves pixels with the display's `COPY`, 13,731 instructions a line where
  redrawing the screen cost up to a million, and `ESC [ t ; b r`, `S` and `T`
  scroll only some rows.
- **Paging, built in phase 4b.2** ([phase4b_plan.md](phase4b_plan.md) step
  6): after `paging(1)`, a screen of the program's output, or of the
  programs it runs, ends in `-- more --`, and the console waits inside
  `write`. `/bin/more` turns it on for a file or a command.
- Commands, argument splitting and program lookup are in kernel_exec.md §3.

---

## 13. CPU changes

> **Built in phase 2** (§17), as this section describes. Where it left a
> detail open, what was built is written in.

### 13.1 Six instructions

Appended after `SHR`, so every existing opcode, and every binary already
built, stays the same.

| Instruction | Opcode | Does |
|---|---|---|
| `GETSP r` | 29 | `r = SP` |
| `SETSP r` / `#n` | 30 | `SP = value` |
| `EI` | 31 | allow interrupts |
| `DI` | 32 | hold interrupts until `EI` |
| `IRET` | 33 | pop `PC`, then pop the flags word: zero, less, and whether interrupts were on |
| `SETIV r` / `#n` | 34 | set the address of the vector table |

### 13.2 Entering a handler

To deliver interrupt or fault *n*, the CPU:

1. pushes a flags word — bit 0 zero, bit 1 less, bit 2 interrupts on
2. pushes the address to return to
3. turns interrupts off
4. jumps to the address stored in `vector_table[n]`

`IRET` undoes all four. **The CPU saves the flags because no instruction
can.** Otherwise an interrupt between a `CMP` and its jump sends the jump the
wrong way (P2). It does not save registers: the handler pushes the ones it
uses.

### 13.3 Vectors

| n | Kind | Raised by |
|---|---|---|
| 0 `VEC_DIV_ZERO` | fault | `DIV` by zero |
| 1 `VEC_BAD_OPCODE` | fault | an unknown opcode |
| 2 `VEC_BAD_FETCH` | fault | fetching past the end of memory |
| 3 `VEC_TIMER` | interrupt | the timer |
| 4 `VEC_BREAK` | interrupt | break: Ctrl-C |

The table is `VECTOR_COUNT` words, and a vector holding 0 has no handler.
`FLAG_ZERO`, `FLAG_LESS` and `FLAG_IE` name the flags word's bits. All of
these are in `memory_map.py`, so assembly and C have them as names too.

### 13.4 Faults

- **Delivered even with interrupts off.** The pushed address is the faulting
  instruction's own, so a handler could fix the cause and retry it. The kernel
  abandons the program instead (§9).
- **With no handler for it, a fault stops the emulator as it always did**, so
  every existing program behaves the same. No handler means no vector table,
  or a 0 in its vector. The fault raises what it always raised:
  `ZeroDivisionError`, or `RuntimeError` for an unknown opcode or a fetch past
  the end, with the faulting instruction's address. An interrupt with no
  handler stops the emulator with a `RuntimeError` too.
- **A fault inside kernel code** halts with "panic" in the prototype. **A
  fault on a fault handler's first instruction stops the emulator**, with
  "Double fault". That is a vector pointing at no code, which would otherwise
  be entered again and again. A fault later in a handler is delivered as
  usual.

### 13.5 When interrupts are taken

- **An interrupt waits in a pending bit** until interrupts are on, then is
  taken before the next instruction, lowest number first.
- **Checking before every instruction** cost 4.1% on CPython and nothing
  measurable on PyPy (P7).
- **Checking in the every-10,000-instructions slow path** cost nothing
  measurable on either, but an interrupt can then wait up to 10,000
  instructions — about 4 ms at `machine.py`'s own figure of 2.4 million a
  second.
- **`Machine.run` and `Machine.step` must take interrupts at the same
  instructions**, or a program with a timer behaves differently in the
  debugger. Checking every instruction in both is the simple way (Q5).
  `CPU.run` gets the same check.
- **As built:** `CPU.run`, `Machine.step` and `Machine.run` check the pending
  bits before every instruction. The devices are asked every
  `CLOCK_SAMPLE_INTERVAL` instructions, by `step()` and `run()` through one
  countdown. So a program takes its timer's interrupts at the same
  instructions whether it is stepped, run, or stepped part of the way first.
  An opcode with no instruction now has the fault in the handler table,
  which took a check out of both loops.
- **Measured, as built:** `user/demo.c` through `Machine.run`, against the
  commit before phase 2, the two alternating. CPython ran 2.63 million
  instructions a second before and 2.60 million after, medians of three; the
  runs before varied by 4.5% among themselves. PyPy ran 38.2 million before and
  38.4 million after. The cost P7 predicted is lost in the noise.

### 13.6 Devices

The prototype raised these itself. Phase 2 built them on the devices.

- **Timer:** `TICK`, command 6, starts the timer at `address` ticking every
  `length` milliseconds, and `STOP` ends it. `START` makes it a one-shot timer
  again. A ticking timer reads as `RUNNING`, with the time to its next tick.
  The machine asks the timer where it samples the clock, so a tick waits at
  most 10,000 instructions, and ticks missed in between arrive as one. The
  timer reads the clock only while one is ticking.
- **Keyboard:** `SET_BREAK`, command 8, turns break on with `address` 1 and
  off with 0, and answers one byte: whether it is on. With break on, `c` or
  `C` pressed while either Ctrl is held raises vector 4 instead of queuing a
  key, and its release is dropped too. Keys arrive on a server thread, so HID
  sets a flag of its own, which the machine turns into a pending bit on its
  own thread. Changing the CPU's pending bits from two threads could lose
  one. Turning break off drops a break not yet taken.
- **No interrupt for ordinary keys.** The input queues already hold 256
  events.

### 13.7 Rules for code that handles interrupts

Each rule was run with a negative control that failed without it:

1. **Push the registers you use, and point `F` at a frame stack of your own
   before calling C.** `F` still points into the interrupted function's
   frame, whose size the handler can't know. Without this, the P2 workload
   never finished.
2. **Do no IO, or save and restore the IO header and the reply.** With a
   handler doing IO, a program asking the timer for its status got the wrong
   answer 2,000 times in 2,000 when interrupted every instruction, and 33
   times in 2,000 when interrupted every 1,009th (P3). Disabling interrupts
   around the program's IO, or saving the header in the handler, gave zero.
3. **Kernel code that can't be entered twice runs with interrupts off** — the
   system-call wrappers of §10. A print without them lost a character under
   preemption. With them, 35 lines stayed whole across 1.6 million switches
   (P6).

Two more instructions, to read and write the flags word, would let code
restore the interrupt state it found instead of assuming it *(optional, not
prototyped)*.

### 13.8 What changes in the emulator

| File | Change |
|---|---|
| `emulator/instruction_set.py` | six appended instructions |
| `assembler/assembler.py` | six `SYNTAX` rows |
| `emulator/cpu.py` | interrupt state, entering a handler, faults instead of exceptions; `dump()` shows the new state |
| `emulator/machine.py` | taking interrupts in `step()` and in the inlined `run()` |
| `emulator/devices/timer.py`, `hid.py` | the periodic interrupt and break |
| `emulator/memory_map.py` | `VEC_*`, `VECTOR_COUNT` and `FLAG_*`, for assembly and C |
| `tests/` | `test_interrupts.py`: each instruction and handler entry, the faults, compiled C under interrupts like P2, and the devices. Kernel execution tests, like P4–P6, come with the kernel |

The prototype's CPU part was about 120 lines of Python.

---

## 14. Optional: preemptive multitasking

**How a switch works** (P6):

1. The timer interrupt pushes the flags and `PC` onto the running program's
   own hardware stack.
2. The handler pushes `A`–`F`, and stores `GETSP` in that program's slot.
3. A scheduler written in C picks the next program, running on the kernel's
   interrupt frame stack.
4. `SETSP` loads the next program's saved stack pointer. The handler pops
   `F`–`A`, and `IRET` resumes that program.

**A new program's first turn.** The kernel builds its stack as if it had been
interrupted just before its first instruction:

```
[task_exit]           startup code's RET lands here when main returns
[flags: interrupts on]
[PC: entry]
[A] [B] [C] [D] [E]
[F: its argc, argv]   <- the saved SP
```

**Run in P6:** three relocated programs — a prime sieve using `malloc`,
recursion, and a sort — each with its own image, frame stack, heap and 1 MB
hardware stack. All three got the right results with a switch every 1, 5,
37, 500 and 20,000 instructions. At every instruction that was 1.6 million
switches.

**Still needed:**
- **A memory allocator.** Programs no longer finish in reverse order of
  starting, so the plates of §8 don't work. P6 used fixed 16 MB slots.
- **Smaller heap limits.** Each program already gets its own `__heap_limit`
  (Q7). Multitasking writes the end of the program's slot there instead of
  `0x07F00000`.
- **A rule for IO.** A switch in the middle of one program's IO lets the next
  program overwrite the header or the reply — P3's problem, at every switch.
  Either all IO goes through system calls with interrupts off, or every switch
  saves the header and the data window *(reasoned from P3; P6's programs did
  no IO of their own)*. Today the display library does its own IO.
- **Waiting.** A program waiting for a key should give up its turn, through a
  `yield` or a blocking read, instead of spinning.

---

## 15. Build, launcher and tests

**Build and launch** (`emulator/programs.py`, `emulator/cli.py`, `config.json`)

- **Built:** stage 1, bios2, the boot sector, `cc.py --project` and the
  installer ([os_cd.md](os_cd.md)). The kernel goes on the disc as the
  project's `system`, and its programs as `[files]` (Q6).
- **The launcher only builds today's programs**, for channel 1. It never
  builds a program file, which needs a kernel to run, so it has no two kinds
  to tell apart.

**Tests** (`tests/`)

- **Boot and install:** built, in `test_bios2.py`, `test_boot.py`,
  `test_project.py` and `test_install.py`. Still missing: `fsck` catching a
  boot record that no longer matches `/boot.bin`.
- **Relocation:** a program built at one address and patched to another runs
  there, with its frame stack and heap after its image, and `malloc` stops at
  its `__heap_limit`.
- **CPU:** built, in `test_interrupts.py`: each new instruction; flags and
  interrupt state restored by `IRET`; faults with and without a vector table;
  `run` and `step` taking interrupts at the same instructions.
- **The kernel:** built, in `test_kernel.py` and `test_install.py`: load, run
  and return; `exit` from deep recursion; faults and
  break; system calls; programs running programs; the display and timers reset
  after a program; a file with a bad header refused; the right disk mounted
  after a restart (Q8).

---

## 16. What the prototype ran

**How.** A Python harness added the six instructions to the emulator's
instruction and syntax tables at runtime; the repository wasn't changed. It
ran C compiled by `pigeon-cc`: kernels built at `0x20000` with hand-written
assembly routines, and programs built as relocatable files (§8). The harness
raised the device interrupts itself. The scripts are in
[prototypes/kernel/](../prototypes/kernel/README.md), named after the rows
below. Since phase 2 the emulator has the instructions itself; the harness
still adds its own copies, which take opcodes 35–40, so the scripts run
unchanged.

| | What ran | Result |
|---|---|---|
| **P1** | The repository's tests, with the six instructions and syntax rows loaded | 970 passed on 2026-09-14; 844 when first run |
| **P2** | A compiled workload — a sort, a sieve, recursion, function pointers; 119,032 instructions — interrupted after every instruction, and every 2, 3, 7 and 101 | The same result every time; 119,006 interrupts at every instruction; stack and `F` restored. **Controls:** `IRET` not restoring the flags never halted, or gave a wrong result at every 7th; a handler using the interrupted `F` never halted |
| **P3** | A program asking the timer for its status 2,000 times, while a handler does IO | Unprotected: 2,000 wrong when interrupted every 1, 3 or 7 instructions; 923 at 31; 394 at 101; 33 at 1,009. `DI`/`EI` around the program's IO: 0. The handler saving the header: 0 |
| **P4** | A kernel with the system-call and vector tables and relocation. It runs a shell as a relocated program, which runs: `hello` with arguments and `malloc`; `exit(42)` from 50 calls deep; a divide by zero 20 calls deep; a jump into data; a loop stopped by break; and a program that runs two more | All 17 lines of console output exactly right — with no timer, and with a timer every 997, 13 and 1 instructions (61,839 interrupts). Three levels deep; the parent's heap intact; memory reused; stack and `F` restored at the end |
| **P5** | P4 with `fs.c` in the kernel, the programs as files in `/bin` on a PigeonFS image made by `pfs.py`, the shell turning a name into `/bin/<name>.bin` for the kernel's `exec`, which takes a path, and an `ls` program listing `/bin` through a kernel call | The same output plus a correct listing, with a timer every instruction too. Programs were loaded by 28 `READ_DMA` transfers, with no block reads through the IO window |
| **P6** | Three relocated programs switched by a timer interrupt | §14. **Control:** a print without `DI`/`EI` lost one character of 280 |
| **P7** | The cost of checking for interrupts, on copies of `Machine.run`'s loop; medians of 5 runs of 3.57 million instructions | CPython, 2.74 million IPS: every instruction −4.1%, in the slow path +1.4%. PyPy, 34.8 million IPS: +1.9% and +0.7%. Only CPython's every-instruction check cost more than the noise |

**Not run:** booting the kernel from disk (the boot chain itself is built,
[os_cd.md](os_cd.md)); the console and the shell's interface; cleanup after
a program; IO from programs that are preempted; `printf`. The timer and
keyboard device changes, and a double fault, were not prototyped; phase 2
built and tested them.

---

## 17. Suggested phases

Each can be tested before the next exists. Phases 1–3 were run in prototype
form.

1. **Relocatable programs:** the startup code, the frame stack and heap after
   the image, `__heap_limit`, and the build step that compares two builds,
   as `cc.py --relocatable` and for `[files]` (Q6, Q7). Testable with no
   kernel: patch a program to another address and call it. ***Done.***
   - **What was built:**
     - `compiler/codegen.py`, a relocatable mode. `__start` is called as
       `entry(argc, argv)`, moves `F` to `__frame_base = __image_end`, writes
       the arguments into `main`'s frame, and returns `main`'s value with the
       caller's `F` given back. Every program, fixed or not, gets
       `__heap_limit` after `__heap_ptr`; only a relocatable one gets the
       `__image_end` label, so the prototype's scripts, which add their own,
       still run.
     - `lib/pigeon/mem.c`: `heap_limit()` returns `__heap_limit`, or the old
       `0x07F00000` when it is 0.
     - `assembler/assembler.py`: a definition that uses a label is settled
       after layout, and `Assembler(path, origin=…)` builds for another
       address than the `.ORG` names.
     - `compiler/program_file.py` builds at `LINK_ADDR`, `0x01000000`, and
       `0x01021238` above it; refuses a word that differs by anything but
       that distance; and writes §8's header, with the magic `PGEX`.
       `relocate()` patches a program file on the host.
     - `emulator/memory_map.py`: `PROGRAM_FILE_MAGIC`, `PROGRAM_FILE_VERSION`
       and `PROGRAM_FILE_HEADER`, for the kernel's C to use.
     - `cc.py --relocatable`, which refuses `-S`, `--org` and `--project`;
       `Program(relocatable=True)`; and `--project` building each `.c` in
       `[files]` as a program file, under a build name of its own.
   - **Sizes:** `user/files.c` is a 182,004-byte program file, a 174,196-byte
     image with 1,944 addresses. A program with `mem.c` grew by 44 bytes:
     the `__heap_limit` word and `heap_limit()`'s check. The example disc is
     482.0 KiB.
   - **`tests/test_relocatable.py`, 41 cases:**
     - a program with a function pointer, a string table, recursion,
       `malloc` and arguments, loaded at four addresses, returns the right
       value, gives back `F` and a balanced stack, and writes nothing to the
       fixed program region or the frame stack at `HEAP_START`;
     - the patched image is byte for byte the build made for that address;
     - its frames and heap are after its image, at exactly the addresses
       expected;
     - C built for `0x20000` calls it through a function pointer, as the
       kernel will, and keeps its own local;
     - `malloc` stops at `__heap_limit` to the byte, five ways, and a program
       built for `0x20000` sets its own limit;
     - all six C programs in `user/` become program files that patch
       exactly;
     - refused: a word shifted from an address, a byte cut from one, an
       address off a word, a fixed build, five damaged files, and an address
       that isn't a multiple of 8;
     - the assembler's new definitions, and `cc.py --relocatable` on the
       command line.
   - **`test_project.py`** checks that a `.c` in `[files]` is a program file
     while the installer, the system and an `.asm` are not, and that the
     example's `/bin` holds program files.
   - **Seventeen deliberate breakages each failed the tests:** the startup
     not giving back `F`, or not passing `argv`; the frame stack at
     `HEAP_START`, with and without `build()` checking; `__heap_limit`
     moved; `mem.c` ignoring it; the comparison accepting any difference;
     `relocate()` ignoring the link address, or patching nothing; the header
     not checking its length; the assembler ignoring its origin, or refusing
     a label in a definition; `Program` building an image; `--project`
     building `[files]` as images, or the system as a program file;
     `--relocatable` accepting `--org`; and assembly built relocatable.
   - **The full suite passes: 1,012 tests**, the 970 from before and 42 new.
     The prototype's P0 and P2–P6 still run; their figures moved with the
     44 bytes, and §11, §16 and kernel_exec.md §5 carry the new ones.
2. **The CPU:** `GETSP`, `SETSP` and faults first; then `EI`, `DI`, `IRET`,
   `SETIV`, the timer interrupt and break. ***Done.***
   - **What was built:**
     - `emulator/instruction_set.py`: the six instructions, opcodes 29–34.
       `DIV` by zero calls the fault instead of raising.
     - `emulator/cpu.py`: `ie`, `ivt` and `pending`; `interrupt()`,
       `take_interrupt()` and `fault()`, entering a handler as §13.2 says.
       Every opcode with no instruction holds the bad-opcode fault in the
       handler table, and a fetch past the end of memory is caught. `dump()`
       shows the new state.
     - `emulator/machine.py`: both loops take a pending interrupt before each
       instruction. `poll_devices()` turns a tick come due, or a break, into
       a pending bit every `CLOCK_SAMPLE_INTERVAL` instructions, counted by one
       countdown for `step()` and `run()`. `run()` now catches a fetch past
       the end.
     - `emulator/devices/timer.py`: `TICK`, command 6, and `Timer.poll()`.
       `emulator/devices/hid.py`: `SET_BREAK`, command 8, and `take_break()`.
     - `emulator/memory_map.py`: `VEC_*`, `VECTOR_COUNT` and `FLAG_*`, so
       assembly and C have the names.
     - `assembler/assembler.py`: six `SYNTAX` rows.
   - **Decided while building** (§13.4, §13.6): a vector holding 0 is no
     handler. With none, a fault raises what it always raised, and an
     interrupt raises `RuntimeError`. A fault on a fault handler's first
     instruction is a double fault, which stops the emulator. The timer's
     `TICK` is command 6 and HID's `SET_BREAK` command 8. Break takes either
     Ctrl with `c` or `C`, and drops the release too.
   - **Cost:** lost in the noise (§13.5).
   - **`tests/test_interrupts.py`, 95 cases:**
     - each instruction assembled, disassembled and run, and `SETSP` backing
       out of fifty calls;
     - entering a handler: the return address and the flags word, for each
       combination of flags; interrupts off inside; `IRET` restoring the
       flags, interrupts and the stack; an interrupt waiting through `DI`;
       the lowest vector first, with no nesting; an interrupt with no handler;
     - each fault through `CPU.run`, `Machine.step` and `Machine.run`: its
       handler entered with interrupts off and the faulting address. Without
       a handler, or with a table of zeros, the same exception and message as
       before phase 2. A handler fixing the cause and retrying; a double
       fault three ways; a fault later in a handler, delivered;
     - compiled C, like P2, interrupted after every 1, 2, 7 and 101
       instructions, with the same answer, stack and `F`. C abandoning a
       divide by zero twenty calls deep, with `GETSP`, `SETSP` and a fault
       handler, then returning normally through the same routine;
     - the timer: due once a period, missed ticks arriving as one, `STOP`,
       `START` making a one-shot again, and the clock read only while one
       ticks. On a Machine, `step()`, `run()` and a mix of the two taking the
       ticks at the same instructions, and a stopped timer interrupting no
       more;
     - break: Ctrl+C is a key until break is on; then either Ctrl with `c` or
       `C` is the break, and neither edge is a key; `c` alone is still a key;
       turning break off drops a raised one. On a Machine, Ctrl+C stops a
       spinning program within one poll, and with break off it doesn't.
   - **Twenty-eight deliberate breakages each failed the tests:** `IRET` not
     restoring the zero flag, or interrupts; `GETSP` reading nothing; `SETIV`
     ignoring a register; entry pushing in the wrong order, or leaving
     interrupts on; a `DIV` fault returning past the `DIV`, or dividing
     anyway; the highest vector first; `CPU.run` or `Machine.run` taking no
     interrupts; no double-fault check; a 0 vector taken as a handler; the
     bad-opcode fault naming the next instruction; `Machine.run` letting a
     fetch past the end escape; `step()` or `run()` never polling the
     devices; `run()` starting its countdown afresh; the machine never taking
     a break; the timer reading the clock with nothing ticking, interrupting
     for each missed tick, ticking after `STOP`, or finishing; break letting
     the release through, ignoring right Ctrl, needing no Ctrl, or keeping a
     raised break once off; and `SETSP` taking only a register. `SETIV`
     ignoring a register was missed at first, as every program gave the
     table as `#`; a test now loads it from a register.
   - **The full suite passes: 1,107 tests**, the 1,012 from before and 95
     new. The prototype's P0 and P2–P6 still run, with the same figures.
3. **The kernel:** system calls, `exec` from `/bin`, `exit`, faults and break.
   Built at `0x20000`, so the old BIOS path can start it before boot exists.
   ***Done,*** with a console and a simple shell, as you asked (Q9–Q12).
   - **What was built:**
     - `user/os/kernel.c`, 141,636 bytes with `fs.c`, and `kernel.asm`, which
       it names with `#asm`. It mounts the disk it booted from (Q8), fills
       the system-call and vector tables, and starts `/bin/sh.bin`, again
       whenever it ends. With no shell, or no filesystem, it says why and
       halts.
     - `exec` loads a program file 32 bytes below where it goes, checks the
       header, the length and every patch offset, patches it, writes its
       `__heap_limit`, and calls it through `exec_call`. `exit`, a fault or
       Ctrl+C come back through `exec_abort`. A fault while kernel code runs
       is a panic.
     - After a program, the kernel closes its files, stops timers 0–15,
       empties the input queues, points the display back at the screen,
       mounts the disk again and redraws the console.
     - The console: a 32×12 grid with scrolling and a cursor, and line input
       with echo, backspace and Enter. Ctrl+C there throws the line away.
     - Thirteen system calls (§10): `write`, `read`, `open`, `close`,
       `opendir`, `readdir`, `closedir`, `stat`, `chdir`, `getcwd`, `exec`,
       `exit` and `getkey`, through `lib/pigeon/sys.h` and `sys.c`, with the
       slot numbers in `syscall.h` and `SYSCALL_TABLE` in `memory_map.py`.
     - `user/os/bin/`: `sh.c`, the simple shell of shell.md §1, and `ls.c`,
       `cat.c` and `echo.c`.
     - `#asm "file.asm"` in the compiler (Q10): placed among the compiled
       functions, found relative to the file naming it, placed once, and
       watched when deciding whether a build is stale.
     - bios2 writes `CH_USERPROG` to `BOOT_CHANNEL` for a program on
       channel 1 (Q8).
     - The example disc boots the kernel (Q11). `/bin` holds the shell, `ls`,
       `cat` and `echo`, and the calculator, the cube and the file browser as
       program files. The disc is 670.0 KiB.
   - **`tests/test_kernel.py`, 26 cases,** on a Machine booted from a test
     disk, typing through HID and reading the console back as text:
     - the shell started, and a missing shell or filesystem reported;
     - the disk mounted by the boot channel, four ways;
     - arguments with quotes; `ls` and `cat` through the kernel; `cd` seen by
       every program; lookup in `/bin`, then where you are, with `.bin`
       added;
     - a program ending by `exit` fifty calls deep, a divide by zero, a bad
       instruction and a jump past memory, each reported while the shell
       carries on; Ctrl+C ending a program that never ends; Ctrl+C at the
       prompt clearing the line;
     - a program running programs above its intact heap; each program's
       heap limit; the screen back after a program page-flips; a file left
       open closed; a program with its own `fs.c` leaving the kernel's view
       of the disk true, with `fsck` clean;
     - files that aren't programs refused; `exit` starting the shell again;
       and a program overwriting a kernel wrapper, causing a panic that
       halts.
   - **`test_install.py`** now installs the example disc and restarts, and
     the shell runs `graph`, which draws its curve and comes back on Esc.
     **`test_compiler.py`** has five `#asm` cases, and **`test_bios2.py`**
     the boot-channel case.
   - **Twenty-five deliberate breakages each failed the tests:**
     - `exec` writing no heap limit, patching nothing, putting every program
       where the first goes, or skipping the length check;
     - tidying that closes no files, leaves the display where the program
       put it, keeps the stale mount, or doesn't redraw;
     - Ctrl+C ignored; line input leaving break off, or on, or taking Ctrl+C
       as a plain `c`;
     - a fault in kernel code that isn't a panic; the hard disk mounted
       whatever booted; the shell not started again;
     - `exec_call` leaving the kernel marked as running; `exec_abort` not
       restoring `F`; `exec` calling through the exit slot;
     - the shell not looking in `/bin`, or ignoring quotes;
     - `#asm` placing a file twice, resolving from the current directory,
       not making a build stale, or codegen dropping it;
     - bios2 leaving `BOOT_CHANNEL` alone for channel 1.
   - **The full suite passes: 1,140 tests.**
4. **`printf`, a fuller shell, and tools for working with files,** planned in
   [phase4_plan.md](phase4_plan.md), in two halves. ***Done:*** 4a, then 4b.1,
   4b.2 and 4b.3 ([phase4b_plan.md](phase4b_plan.md)).
   - **What 4a built:**
     - **Variadic functions** (step 1). The parser records `...` on a
       function and on a function-pointer type, and refuses it with no named
       parameter before it. The analyzer reserves `VA_SLOTS`, eight words,
       after the named parameters, and refuses a ninth extra argument or a
       struct as one. Codegen needed nothing: it already wrote argument *i* at
       `F + frame + 4i`. The frame is in `compiler/design/03-abi.md`.
     - **The preprocessor** puts a macro argument of only words and stars,
       such as `char *`, in as written, unless one of its words is a macro.
       `va_arg(ap, char *)` needs it: `((char *))` isn't a cast.
     - **`lib/pigeon/stdarg.h`**, macros only, and **`stdio.h` with
       `stdio.c`** (step 2): `snprintf` and `vsnprintf` anywhere; `printf`,
       `vprintf`, `puts` and `putchar` through the kernel, returning -1
       without one.
     - **The console as a small terminal** (step 3): the escape codes in §12,
       a look byte for each of the 384 cells, the parser's state kept between
       writes, and the ink put back when a program ends.
     - **The shell's prompt** from `/etc/shell_header.conf` (step 5, shell.md
       §2), with nine color names, ``` ``CSTATUS`` ```, which colors the
       status by its sign (yours), and a second line shown once as the first
       prompt. Its messages are written with `printf`.
     - **Four system calls,** `mkdir`, `rmdir`, `remove` and `rename`, in
       slots 13–16, and **six commands** in `user/os/bin/` (step 6): `mkdir`,
       `rmdir`, `rm`, `mv`, `cp` and `clear`. `mv` and `cp` into a directory
       keep the name; `cp` copies 512 bytes at a time and refuses a
       directory.
     - **`ls`** (step 7): sorted by name, `-l` with a right-aligned size or
       `<dir>`, and several directories each under its name. It sorts the
       first 256 entries; any after them follow unsorted.
     - **Ctrl+C only while a program runs** (step 10). `exec` turns break on
       just before `exec_call` and off when the program comes back, then on
       again while a parent still runs. Line input turns it off while it
       waits. Turning it on first swallows a break raised while the kernel
       was busy: `kswallow` points the break vector at a routine that only
       returns, opens interrupts for one instruction, and puts the vector
       back.
     - **The example disc** carries the six commands, and your prompt as
       `/etc/shell_header.conf`: `|-(PGS)-[2:/]-(0)` over `|-> `, in green
       and blue with the status colored, and a blank line between commands.
   - **Decided while building:**
     - With no prompt file, or one it refuses, the built-in prompt is the
       current directory and `> `, as in phase 3, not shell.md's example; the
       example disc carries a prompt file instead. A refused file is also
       reported.
     - **Two lines in the prompt file** (your idea, 2026-09-15): the first is
       the prompt, and the second, when there is one, is shown once in its
       place when the shell starts, so after `exit` too. Each line is at most
       255 bytes, and the file 1024. The blank line between commands is a
       `\n` at the start of the first line, not the shell's doing, so it
       stays a choice.
     - `%p` is `0x` and the hex digits; `%s` of a null pointer prints
       `(null)`; an unknown conversion is printed as written.
     - `stdarg.h` also has `va_copy`, and `va_end` sets the pointer to 0.
     - Break stays on while a program reads a line only through line input;
       restoring it as a program left it comes with 4b's `setbreak`.
   - **Sizes:** the kernel is 147,924 bytes, up from 141,636; the shell
     42,676, up from 22,264; `ls` 37,664. A command that uses `printf` is
     about 26 KB, as `stdio.c` brings `string.c`, while `clear`, with only
     `print`, is 5,872 bytes. The example disc is 870.5 KiB, up from 670.0.
   - **Tests, 47 new:**
     - **`test_compiler.py`, 15:** none, one and eight extra arguments, and
       negative ones; chars and pointers; a `va_list` passed on; a call
       through a pointer; extra arguments that call functions themselves;
       recursion; locals and calls not overwriting the slots; four refusals,
       for a ninth extra argument, a struct, too few named ones and `...`
       alone; and a type as a macro argument.
     - **`test_stdio.py`, 3:** every conversion against Python's `%`;
       truncation returning the length wanted; and `printf`, `puts` and
       `putchar` returning -1 with no kernel.
     - **`test_kernel.py`, 28:** colors and inverse checked in the
       framebuffer's pixels, and kept through a scroll; clearing, moving the
       cursor and clearing a line; a sequence split across two writes, and
       unknown ones dropped; the ink reset after a program; `printf` through
       the kernel; the prompt file five ways, with colors and the last status
       after a failure; its second line first and its first line after, with
       quotes, `\r\n` and blank lines at the end; four files refused; the file commands checked against
       `pfs.py` with `fsck` clean, four refusals and a copy onto a read-only
       disc; `clear`; `ls` sorted
       and `ls -l`; and a break raised while interrupts were off ending no
       program.
     - **`test_relocatable.py`, 1:** a program with a variadic function,
       patched to other addresses.
     - **`test_install.py`** checks the disc's two-line prompt before and
       after `graph`, and **`test_project.py`** the new commands.
   - **Forty deliberate breakages each failed the tests:**
     - the compiler reserving no slots, allowing a ninth extra argument, a
       struct as one, or `...` alone; the preprocessor wrapping a type again,
       or leaving a macro's name bare; `va_start` a word off;
     - `stdio.c` ignoring `-`, padding zeros with spaces, ignoring the
       precision, writing `%X` in lower case, returning what it kept, calling
       through the empty table, or never writing its last piece;
     - the console ignoring inks or inverse, leaving colors behind on a
       scroll, `ESC [ K` clearing nothing, `ESC [ H` counting from 0, a `?`
       ending a sequence, a color left on after a program, or each write
       starting a fresh sequence;
     - the shell keeping the quotes, never updating the status, looking for
       another file, or taking a file of any length;
     - `rename` swapping its paths, `rmdir` calling through `remove`'s slot,
       and `mv` into a directory replacing it;
     - `ls` unsorted, or `-l` without sizes;
     - a break left pending, not swallowed;
     - the prompt file's second line shown every time, or never; a third
       line accepted; a line of any length; a `\r`, or the line breaks at the
       end, kept; a file over 1024 bytes cut short without a word; and quotes
       kept on a line.
   - **The full suite passes: 1,187 tests**, the 1,140 from before and 47
     new.
   - **4b.1, built** (phase4b_plan.md steps 1–4):
     - **Fast scrolling.** The display device's `COPY`, command 5, moves
       bytes within a buffer. `disp_scroll` in `display.c` moves rows of
       pixels with it, or with `memmove` when there's no device, and the
       console scrolls by moving pixels instead of redrawing.
       `ESC [ t ; b r`, `ESC [ n S` and `ESC [ n T` scroll only some rows,
       and tidying after a program gives the whole screen back.
     - **Line editing** in `con_read_line`: Left, Right, Home, End, Ctrl+A
       and Ctrl+E; Backspace and Delete anywhere in the line; Ctrl+U; 16
       lines of history on Up and Down; and Ctrl+L. The console learns
       `ESC ]` sequences, and the shell prints `ESC ] 133 ; A` where its
       prompt starts.
     - **Tab completion.** A new system call, `setcomplete`, slot 17, which
       the shell calls with `/bin` and its built-ins. File and directory
       names in any other word. A second Tab lists the choices in columns
       under the line.
     - **Scrollback:** the last 100 rows, looked through with PgUp, PgDn and
       the mouse wheel, with a marker saying how far back. `ESC [ 3 J`
       empties it, and `clear` prints that too.
     - **The wheel:** HID's buttons 5 and 6, named in `hid.py` and
       `input.h`. The pygame client sends `MOUSEWHEEL` as notches, and
       repeats a held key after 400 ms, every 40 ms. The browser adds up
       wheel distance into notches: 100 pixels, 3 lines or a page each.
   - **Decided while building** (phase4b_plan.md §11 has the corrections to
     the plan):
     - a typed line holds up to 255 characters, the shell's limit, so a
       program asking `read()` for more still gets 255 at most;
     - a Tab straight after any Tab lists the matches, whether the first one
       added something or not;
     - Ctrl with a letter no longer types the letter;
     - mouse events other than the wheel are dropped while a line is typed.
   - **Measured:** a whole-screen `disp_scroll` with the device, 7,121
     instructions; one console scroll, 13,731. Before, up to about a
     million.
   - **Sizes:** the kernel is 204,316 bytes, up from 147,924, where the plan
     guessed about 15 KB more. About 14 KB of it is history, scrollback and
     Tab's names, which are globals and so live in the image; the rest is
     code. A program with the display library grew by 4,188 bytes for
     `disp_scroll`, one with `sys.c` by 184 for `setcomplete`, and the shell
     is 43,052. The example disc is 943.5 KiB, up from 870.5.
   - **Tests, 63 new:**
     - **`test_kernel.py`, 36:** scroll regions, `S` and `T`, and a region
       left set undone after its program; a console scroll counted in
       instructions; each editing key at both ends and in the middle; a line
       wrapping, and one typed on the bottom row under a two-line prompt;
       history; Ctrl+L with and without a mark; `ESC ]` dropped, and given
       up after 64 characters; Tab for a command, a built-in, a file, a
       directory, the middle of a line and no match; the list on a second
       Tab, in order, cleared by the next key, and scrolled into view with
       `and N more`; a name with a space in quotes; a program that names no
       commands; PgUp and PgDn with the marker, the wheel, 100 rows kept, and
       `ESC [ 2 J` keeping them while `clear` empties them.
     - **`test_display.py`, 8:** `COPY` both ways and to the screen's last
       byte, and six refusals.
     - **`test_libs.py`, 14:** `disp_scroll` six ways with the device and six
       without, its cost, and a wheel notch through `input.c`.
     - **`test_input.py`, 5:** the wheel buttons through HID, the browser's
       adder under node and its listener, and the pygame client's notches and
       key repeat.
   - **Forty-one deliberate breakages each failed the tests,** two of them
     only after a test was fixed:
     - `COPY` refusing nothing, or ignoring its source; `disp_scroll`'s
       fallback moving rows the wrong way, or clearing only the first row
       left behind; the console redrawing after a scroll; a newline ignoring
       the region; `ESC [ S` scrolling down; a region kept after its program;
     - typing over instead of inserting; Backspace taking the wrong
       character; a shorter line leaving its old end on screen; history
       keeping repeats; Down losing what was typed; the line's row not moving
       with a scroll; Ctrl+L ignoring the mark, or keeping blank rows;
       `ESC ]` never given up; a mark outliving its line; Ctrl with a letter
       typing it; the shell printing no mark;
     - Tab offering no commands, or leaving out the built-ins; a directory
       completed with a space; a name with a space left unquoted; the list
       unsorted, never shown, never cleared, or not scrolled into view; the
       shell naming no commands;
     - nothing kept in the scrollback, or it read from the wrong end;
       `ESC [ 3 J` ignored; `clear` keeping the scrollback; no marker; a key
       leaving the view back; the wheel's up going down;
     - the browser's notches the wrong way, or never starting the count
       again; pygame sending its legacy wheel buttons too, turning the wheel
       the wrong way, or not repeating keys.
     - **Missed at first:** `ESC [ 3 J` ignored, and `clear` keeping the
       scrollback. The test pressed PgUp after `clear` and then typed a key,
       and a key brings the view back whether PgUp moved it or not. It now
       reads how many rows the scrollback holds from the kernel's memory.
   - **The full suite passes: 1,250 tests**, the 1,187 from before and 63
     new.
   - **4b.2, built** (phase4b_plan.md steps 5 and 6):
     - **Break per program.** A new system call, `setbreak`, slot 18, turns
       Ctrl+C as the break off or on for the program calling, and returns
       what it was. The kernel keeps the setting with each program: `exec`
       starts a program with break on, its parent's setting comes back when
       it ends, and line input puts back the setting it found instead of
       turning break on.
     - **Paging:** `paging`, slot 19. After 11 rows of the program's output,
       or of the programs it runs, the console shows `-- more --` in inverse
       on the bottom row and waits inside `write`. Space shows another
       screen, Enter one more row, and PgUp, PgDn and the wheel look back.
       `q` ends the programs `more` ran, coming back to its `exec` as
       `ENDED_QUIT`, or makes `write` return `E_QUIT` for `more`'s own
       output. Tidying closes the files of every program `q` ended, and
       paging ends with the program that turned it on.
     - **`/bin/more`:** `more FILE…` or `more COMMAND ARGS…`; a first word
       that names a file means files.
   - **Decided while building:**
     - Ctrl+C at `-- more --` stops the program that is writing, as it would
       without paging, and a program with break off takes it as `q`;
     - `-- more --` takes the first ten cells of the row the output goes on
       next;
     - several files are paged one after another, with nothing between them;
     - found while testing, and left as it is: `getkey` reads HID's
       character queue and line input its event queue, so keys a program
       took with `getkey` are still there for its next `read`, which takes a
       Ctrl+C among them as Ctrl+C at the line. `lib/README.md` warns about
       it.
   - **Sizes:** the kernel is 209,528 bytes, up from 204,316; `more` is
     31,068; a program with `sys.c` grew by 516 bytes for the two new calls.
     The example disc is 985.0 KiB, up from 943.5.
   - **Tests, 12 new, in `test_kernel.py`:** a program with break off getting
     Ctrl+C as a key, at once and after reading a line; break on for its
     child, off again for it, and on for the shell's next program; a file
     paged with Space, Enter and `q`; `more ls /bin` with more entries than
     fit; PgUp while waiting, and paging ending with `more`; `q` ending a
     command and the program it ran, with their files closed; Ctrl+C at
     `-- more --`; and `more` given a file, a command, neither, and nothing.
     `test_install.py` and `test_project.py` count `more` on the disc.
   - **Nineteen deliberate breakages each failed the tests:**
     - a program starting without break on; the parent getting break back
       on; line input turning break on; `setbreak` not acting at once, or
       returning the new setting;
     - never waiting, or rows not counted; Enter letting a whole screen
       through; no `-- more --`, or one left behind; `q` unwinding `more`'s
       own output, or ending only the program writing; Ctrl+C ignored at
       `-- more --`; tidying only the last program's files; paging outliving
       `more`;
     - `more` reporting `q` as a failure, taking every word as a file,
       reading on after `q`, or turning no paging on.
     - The first run missed three of them because its test filter left out
       the test that covers them; run with it, each failed.
   - **The full suite passes: 1,262 tests**, the 1,250 from before and 12
     new.
   - **4b.3, built** (phase4b_plan.md step 7): **`/bin/edit`, a mini nano.**
     - **The screen:** the title bar on row 0, saying `Modified` once the
       text changes, and the shortcuts on row 11, both inverse; the text on
       rows 1–9, set as a scroll region; messages and questions on row 10.
       It draws through the console's escape codes, gathered and written
       once a key, and draws only what a key changed.
     - **The keys,** nano's: ^O or ^S save, ^X exit (asking `Save modified
       buffer? Y N` when the text changed), ^K cut a line (several in a row
       gather), ^U paste above the cursor's line, ^W find (forward, wrapping,
       ^C cancels), ^C where the cursor is, ^G the keys, ^Y and ^V or PgUp and
       PgDn a screen, Tab spaces to the next multiple of 4, and the arrows,
       Home, End, Backspace, Delete and Enter.
     - **The mouse:** a wheel notch scrolls the text 3 rows, taking the
       cursor along when it would leave the screen; a left click puts the
       cursor on the character it lands on, at the end of that line, or on
       the last line below it. The pygame client now sends where a press
       is before the press, as the browser does.
     - **The text** is one gap buffer, grown by copying. Saving writes
       `FILE~`, removes `FILE` and renames `FILE~` to it, so a full disk
       leaves the old file whole. A file over 64 KB, or holding a zero byte,
       is refused. `edit` turns break off with `setbreak`, so ^C is its own.
   - **Decided while building:**
     - a line longer than the screen moves sideways 24 columns at a time,
       with `$` in its first cell, while the cursor is on it; other lines
       are cut at the edge;
     - leaving clears the screen, so the prompt starts at the top: the
       console keeps no second screen to put the shell's back;
     - messages are centred on row 10 and last until the next key; ^W takes
       up to 20 characters, and "not found" shows 14 of them;
     - a character outside printable ASCII shows as `?`, one cell; a tab in
       a file is one cell too;
     - the text stays under 64 KB while editing, not only when opened; a
       line cut from the end of a file without a line break gets one.
   - **Measured:** a key typed in the middle of a line draws in 142,567
     instructions, where drawing all nine rows would cost about 1.4 million.
   - **Sizes:** `edit` is 81,172 bytes, where the plan guessed 50–70 KB; the
     example disc is 1.0 MiB.
   - **Tests, 22 new:**
     - **`test_edit.py`, 21:** opening, moving, typing in the middle of a
       line, saving and leaving, checked against the disk image; ^X with
       changes answered Y, N and ^C; ^K twice and ^U; a new file made on its
       first save; a save onto a full disk leaving the old file; ^W forward,
       again, wrapping, not found and cancelled; a long line moving sideways
       and back; ^C's position and ^G's keys; the wheel both ways; a click in
       a line, past its end, below the text and on both bars; a directory, a
       zero byte, over 64 KB and no name refused; and one key's drawing
       counted in instructions.
     - **`test_input.py`, 1:** the pygame client sending a press's position
       before the press.
   - **Twenty-six deliberate breakages each failed the tests:**
     - saving without the text before the cursor, or with the gap in place
       of the text after it; `FILE~` left behind by a failed save; a short
       write taken as saved; still `Modified` after a save;
     - ^X not asking, or N saving anyway; ^K's cuts not gathering; Enter
       typing no line break;
     - find never wrapping, or finding what's under the cursor;
     - a long line never moving sideways, or left moved when the cursor goes;
       every key redrawing every row;
     - the wheel the wrong way, or leaving the cursor off the screen; a click
       ignoring the column, ignored below the text, or taken on the bars;
     - ^C a line off; ^G not waiting for a key; a zero byte, or a file over
       64 KB, opened; Ctrl+C left as the break; the screen left behind when
       `edit` ends;
     - the pygame client sending a press without where it was.
   - **The full suite passes: 1,284 tests**, the 1,262 from before and 22
     new.
5. **The serial debug port,** with a panel beside the screen, and boot and
   `exec` logged to it ([phase5_plan.md](phase5_plan.md)). Swapped with the
   boot screen. ***Part 5a is built:*** the port on IO channel 8,
   `<pigeon/debug.h>`, `printf` with no kernel writing to it, `--serial`,
   `--serial-log` and `/serial`. ***So is 5b*** ([phase5b_plan.md](phase5b_plan.md)):
   stage 1, bios2 and the installer say where they've got to, and the kernel
   logs starting, its disk, every `exec` and how each program ended, and a
   panic. `k_exec` also sets `started` again after a program, so `exit`
   after a command that couldn't start restarts the shell. ***And 5c,***
   which finishes phase 5: the Serial panel beside the screen, in the browser
   and the pygame client ([phase5c_plan.md](phase5c_plan.md)).
6. **A boot screen and a startup script,** from `/etc/boot.conf`
   ([phase4_plan.md §11](phase4_plan.md#11-later-phases)).
7. **The launcher, `config.json`, and the docs.**

Also done, out of order: ~~**the sector-booting BIOS, stage 1, and `pfs.py
install-boot`**~~, which this list called phase 5 until phase 4 renumbered it,
as bios2, `firmware/boot.asm`, `pfs.py boot` and `cc.py --project`
([os_cd.md](os_cd.md)). The kernel is installed as the project's `system`.
**Multitasking** stays optional (§14).

---

## 18. Open questions

All decided. The smaller shell questions are decided in kernel_exec.md §10 and
kernel_changes.md §4, and [kernel_overview.md](kernel_overview.md) sums them up.

1. ~~**The boot record.**~~ **Decided 2026-09-13 (left to me):** option A. The
   record goes in bytes 52–63 of block 0 and the boot sector in bytes
   128–511, so the disk format doesn't change. See os_cd.md §6 and §10.

2. ~~**The shell.**~~ **Decided 2026-09-13 (you):** the shell is a separate
   program that the kernel starts, not part of the kernel.

3. ~~**How programs use the kernel.**~~ **Decided 2026-09-14 (left to me):**
   system calls. The filesystem and the console go through the kernel, so there
   is one `fs.c`, one current directory and one cursor; P5 ran this. Programs
   bundle only what keeps no shared state: `string.c`, `math.c`, and `mem.c`
   for their own heap. While one program runs at a time, `display.c` and
   `input.c` stay bundled too, for games and editors. With multitasking (§14)
   they move behind system calls.

4. ~~**Where programs go.**~~ **Decided 2026-09-14 (left to me):** relocation
   (§8). P4 and P5 ran it end to end, and it needs no compile-time layouts.

5. ~~**How far with the CPU, and how precisely.**~~ **Decided 2026-09-14 (left
   to me):** a and b now, c later.
   - **a.** `GETSP`, `SETSP` and faults: `exit()`, and crashes that return to
     the shell.
   - **b.** Plus `EI`, `DI`, `IRET`, `SETIV`, the timer interrupt and break.
   - **c.** Multitasking waits: it also needs an allocator, per-program heap
     limits and an IO rule (§14).

   Interrupts are checked before every instruction, in `CPU.run`,
   `Machine.step` and `Machine.run` alike. That costs 4.1% on CPython and
   nothing on PyPy, and `run` and `step` then agree without any extra work.

6. ~~**Which programs are built relocatable.**~~ **Decided 2026-09-14 (left
   to me):** every `.c` in a project's `[files]`, built as a program file,
   and one source at a time with `cc.py --relocatable`, which refuses `-S`,
   `--org` and `--project` alongside it. The installer and the `system` stay
   built for `0x20000`, because the boot sector loads them there. An `.asm`
   in `[files]` is assembled as today: hand-written assembly has no startup
   code that returns to the kernel, and `test_project.py` already builds one
   that way. The kernel refuses such a file, as it has no magic. The rule
   isn't by path, because the shell also runs programs from the current
   directory, not only from `/bin`.

7. ~~**Heap limits.**~~ **Decided 2026-09-14 (left to me):** the compiler
   emits `__heap_limit`, 0 in the image, as the word right after
   `__heap_ptr`. `mem.c`'s `heap_limit()` returns it, or `0x07F00000` when
   it is 0, so every program built today behaves the same. The kernel sets
   its own to `0x00FFFFE0` first thing: 32 bytes below the shell, where the
   shell's header lands. `exec` writes each program's before calling it —
   `0x07F00000` in the first version, and the end of the program's slot once
   there is multitasking. The kernel needed a limit anyway, and this way
   multitasking changes no program.

8. ~~**Which disk the kernel mounts.**~~ **Decided 2026-09-14 (left to
   me):** the channel in `BOOT_CHANNEL` when it is the hard disk or the CD,
   and the hard disk otherwise. If the mount fails, the kernel says why and
   halts. bios2 also writes `CH_USERPROG` to `BOOT_CHANNEL` before it calls
   a program on channel 1. Today it writes the word only for a disk
   (`firmware/bios2.c:322`), so after the installer restarts from the CD, a
   kernel booted from channel 1 would find 6 there and mount the disc.
   Without bios2 the word is 0, because RAM starts zeroed, and the hard disk
   is mounted. *Phase 3 made bios2 write it for channel 1.*

9. ~~**How much of the console and the shell comes with the kernel.**~~
   **Decided 2026-09-14 (you):** a simple output, and a simple shell program
   with it, so phase 3 already boots to a prompt. The console also reads a
   typed line. `printf`, the shell's prompt file and colors stay in phase 4.

10. ~~**How hand-written assembly gets into the kernel's C.**~~ **Decided
    2026-09-14 (you):** a directive in the C file, `#asm "kernel.asm"`. The
    compiler places the file among the compiled functions, so the kernel
    stays one source path for `cc.py`, the launcher and the project file.

11. ~~**What the example disc boots.**~~ **Decided 2026-09-14 (you):** the
    kernel with the simple shell. The calculator, the cube and the file
    browser become programs in `/bin`; each comes back to the shell on Esc.

12. ~~**The details phase 3 left open.**~~ **Decided 2026-09-14 (left to
    me):**
    - **File descriptors:** 0 reads a line from the console, 1 and 2 write
      to it, and `open` returns `fs.c`'s handle plus 3. A directory handle is
      `fs.c`'s own.
    - **After a program, the kernel mounts its disk again,** keeping the
      current directory. A program may bundle `fs.c` and write behind the
      kernel's cache, which would then hide the new file or hand its blocks
      out again.
    - **A fault while kernel code runs is a panic**, which prints where and
      halts. The system-call wrappers mark the kernel as running, and
      `exec_call` marks it as not, around the program.
    - **Ctrl+C at the prompt throws the line away.** The console turns break
      off while it reads a line and takes Ctrl+C as a key, since a break
      pending until Enter would end the shell instead. Pressed while the
      kernel is busy anywhere else, a break waits for interrupts to come
      back on, and so ends the program that runs next *(reasoned, not run)*.
    - **The kernel's timer vector only returns**, so a program that starts a
      tick doesn't stop the emulator.
    - **A program that ends badly is reported by the shell**, as its
      `sys_strerror` text, and one that returns a non-zero value as
      `name: exit N`.
