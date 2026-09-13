# A kernel and a shell: what the machine needs first

> **Status: analysis only, nothing built.** This lists what would have to
> change for a simple kernel to boot from the hard disk the way a PC boots from
> a boot sector, and run a shell that starts other programs and gets control
> back when they finish. Every fact in §2 was checked against the code on
> 2026-09-13. Anything reasoned but not run is marked *unverified*. Four
> decisions are open, in [§16](#16-open-questions), for you to answer inline.

| Area | Today | Needed |
|---|---|---|
| Boot | The BIOS copies a whole program file from channel 1 and jumps to it | The BIOS loads one sector from the boot disk, checks a signature, and jumps |
| Disk layout | Block 0 is the PigeonFS superblock; its first word decodes as an invalid instruction | A place for boot code and a way for it to find the kernel (§5) |
| Memory | Every C program is compiled for the same code, frame-stack and heap addresses | A kernel layout and an app layout that don't overlap (§8) |
| Exiting | A program's startup code ends in `HALT`, which stops the machine | App startup code that returns to whoever called it (§9) |
| Services | Each program bundles its own libraries, `fs.c` included | A system-call table the kernel fills in (§10) |
| Text | No console; `display.h` draws characters but doesn't scroll | A console library, and a shell built on it (§12) |

---

## 1. What is possible at all

**A DOS-style system is possible with the CPU as it is**: one program runs at a
time, the shell calls it like a function, and the program returns to the
shell. `JMP reg` and `CALL reg` exist, and calling through a function pointer
compiles and has tests.

**Preemptive multitasking is not.** The machine has no interrupts, so nothing
can take control away from a running program, and no instruction can read or
set the stack pointer, so nothing could switch between stacks. Both would be
CPU changes (§14).

---

## 2. The facts that decide it

Checked in the code, not assumed:

**The CPU**
- There are 29 instructions and six registers, `A` to `F`
  (`emulator/instruction_set.py`, `REGISTER_COUNT` in `emulator/memory_map.py`).
- There are no interrupts, traps or privilege levels. Nothing in `cpu.py`,
  `machine.py` or `io_controller.py` mentions any.
- The stack pointer moves only through `PUSH`, `POP`, `CALL` and `RET`. No
  instruction reads it into a register or loads it from one.
- `JMP` and `CALL` take a register as well as an address (`reg_or_imm`).
- Opcodes are numbered in declaration order, and the file says new instructions
  can safely be **appended**.
- Dividing by zero raises a Python exception, and an unknown opcode raises
  `RuntimeError`. Either one ends the emulator, not just the program.

**The memory map** (`emulator/memory_map.py`)

| Range | Size | What |
|---|---|---|
| `0x00000000`–`0x000003FF` | 1 KB | BIOS |
| `0x00000400`–`0x00001417` | 4 KB + 24 B | IO header and data window |
| `0x00001418`–`0x00015817` | 81 KB | framebuffer, 192×108 |
| `0x00015818`–`0x0001FFFF` | ~42 KB | **unused** — no constant in `memory_map.py` claims it |
| `0x00020000`–`0x0011FFFF` | 1 MB | program code and data (`PROGRAM_LOAD_ADDR`) |
| `0x00120000`–`0x0015FFFF` | 256 KB | frame stack (`HEAP_START`) |
| `0x00160000` → `0x07F00000` | ~126 MB | heap |
| → `0x07FFFFFC` | | hardware stack, growing down (`STACK_TOP`) |

**The compiler** (`compiler/codegen.py`, `compiler/cc.py`)
- Every C program is emitted with `.ORG PROGRAM_LOAD_ADDR` (`codegen.py:80`).
- The startup code `__start` sets the frame pointer `F` to `__frame_base`,
  sets up the heap, calls `main` with no arguments, then **`HALT`s**
  (`codegen.py:91–97`).
- `__frame_base = HEAP_START`, the frame stack is 262,144 bytes, and
  `__heap_base` follows it (`codegen.py:164–166`).
- `cc.py` has no option to choose any of these: its only options are the
  sources, `-o`, `-S` and `-I`.
- `mem.c`'s heap limit is a typed literal, `0x08000000 - 0x00100000`, not
  derived from the memory map.
- Function pointers compile to `CALL E` and have tests in
  `tests/test_compiler.py`.

**The BIOS** (`firmware/bios.asm`, `emulator/bios.py`)
- It must fit in 1 KB, which is 128 instructions; `bios.py` refuses anything
  larger. Today's BIOS is 688 bytes, 86 instructions.
- It reads channel 1 in 4 KB chunks, copies to `PROGRAM_LOAD_ADDR`, paints a
  progress bar, waits two seconds on the timer, clears the screen and jumps.
- It stops when a chunk returns 0 bytes. It does not treat `0xFFFFFFFF`, the
  answer from a channel with no device, as an error. Read from the code: with
  channel 1 empty it would try to copy 0x3FFFFFFF words *(unverified, not run)*.
- `Machine` registers channel 1 only when it is given a program, and the CLI
  allows running with none.

**The filesystem** (`lib/pigeon/fs.c`, `tools/pfs.py`, `docs/filesystem.md` §3.1)
- Block 0 is the superblock. Its first word is the magic `0x53464750`, stored
  as the bytes `50 47 46 53`, so its first byte read as an opcode is 80 —
  past the 29 that exist. Jumping to it would stop the emulator.
- `FS__FAT_START` is fixed at 1, and `fs_mount` rejects any other value.
- The superblock's bytes 52–63 and 128–511 are unused. `fs_mount` checks only
  words 0–6 and 24–26, and `__fs_put_hints` updates the block in place, so the
  guest keeps whatever is in the unused bytes.
- **`pfs.py` does not.** `_write_super()` rebuilds block 0 from a fresh zeroed
  buffer, so a host-side write would erase anything stored there. `fsck` does
  not check those bytes either way.
- The HDD refuses a DMA transfer into or out of memory below
  `PROGRAM_LOAD_ADDR` (`emulator/devices/hdd.py`).

**Everything else**
- `display.c` keeps its state in each program: `disp_target`, `disp_back` and
  `disp_hw`. A program that calls `disp_use_back_buffer()` points the display
  at a buffer on its own heap.
- The timer device is polled — `START`, `STOP`, `RESET`, `STATUS` — and raises
  nothing.
- The screen holds 32×12 characters. `display.h` has `disp_text` and
  `disp_char`, and nothing that scrolls or tracks a cursor.
- A program that includes `<pigeon/fs.h>` is about 99 KB. `user/files.c` and
  `user/disc.c` are both about 169 KB.

---

## 3. What already works

- **Jumping into loaded code**, through `JMP reg` or `CALL reg`, or a C
  function pointer.
- **Finding and loading a program from disk**, with `fs.c`.
- **Loading it fast.** One `READ_DMA` is 84 instructions whatever it moves, so
  even a boot sector of a few dozen instructions can load a whole kernel.
- **Room.** 128 MB of RAM, a 1 MB program region, and a 126 MB heap.
- **Devices.** Display, keyboard and mouse, timer, and the CD drive — a natural
  way to install apps onto the disk.

---

## 4. Boot: the BIOS loads a sector

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
  `user/` depends on it — or give the BIOS a way to choose between the two.

---

## 5. Where the boot code and the kernel live on disk

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

**Files:** `tools/pfs.py`

- **`pfs.py install-boot boot.bin kernel.bin`**: write the stage-1 code,
  place the kernel contiguously, and write the boot record.
- **`fsck`** checks the record (option A) or the reserved blocks (option B).
- **`mkfs`** leaves room for it, if option B is chosen.

---

## 7. Stage 1 has to be hand-written assembly

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

## 8. Memory layouts: the biggest blocker

**Files:** `compiler/codegen.py`, `compiler/cc.py`, `lib/pigeon/mem.c`,
`emulator/memory_map.py`, `emulator/programs.py`

Every C program is built for the same three regions: its code at
`PROGRAM_LOAD_ADDR`, its frame stack at `HEAP_START`, and its heap straight
after. **A kernel and an app built this way sit on top of each other.**
Loading the app overwrites the kernel's code, its local variables and its
heap.

**Needed:**

- **A layout chosen at compile time** — load address, frame-stack base and
  size, heap base and limit. Either as options on `cc.py`, or as a
  `--kind kernel|app` setting that picks named constants.
- **Those constants in `memory_map.py`**, where both toolchains already get
  their predefined symbols: `KERNEL_LOAD_ADDR`, `APP_LOAD_ADDR`, and a frame
  stack and heap for each.
- **`mem.c`'s heap limit from a symbol** the compiler emits, not a literal.
- **The launcher building each kind with its own layout** (`programs.py`).

A possible layout *(a proposal, not measured against anything)*:

| Range | What |
|---|---|
| `0x00015818`–`0x0001FFFF` | boot sector, and the system-call table (§10) |
| `0x00020000`–`0x0011FFFF` | kernel code and data (1 MB) |
| `0x00120000`–`0x00FFFFFF` | kernel frame stack and heap (~15 MB) |
| `0x01000000`–`0x010FFFFF` | app code and data (1 MB) |
| `0x01100000`–`0x07EFFFFF` | app frame stack and heap |
| → `0x07FFFFFC` | the hardware stack, shared |

Keeping the kernel at `PROGRAM_LOAD_ADDR` means the old BIOS path could still
run it for testing.

**The alternative, relocation** — loading any program anywhere and patching
its addresses — needs the assembler to emit a relocation table and a loader to
apply it. Much more work, and nothing here needs it.

---

## 9. Apps return to the shell

**Files:** `compiler/codegen.py`

Today `__start` calls `main` and then `HALT`s, which stops the machine,
shell and all.

**App startup code** instead:

1. save the caller's `F` — the kernel's frame pointer — with `PUSH F`
2. set `F` to the app's own frame stack, and set up the app's heap
3. `CALL main`
4. restore `F` with `POP F`
5. `RET`, with `main`'s return value still in `A` as the exit code

The hardware stack is shared, and `CALL`/`RET` keep it balanced.

**Limits that come with the CPU as it is:**

- **No `exit()` from inside a nested function.** Unwinding to the kernel
  means resetting the stack pointer, and no instruction can. An app exits by
  returning from `main`.
- **No arguments to `main` yet.** `__start` calls it with none. Either the
  startup code writes `argc` and `argv` into `main`'s frame before calling it,
  or apps fetch them with a system call.
- **A crashing app takes the emulator down** (§2), the kernel with it.

---

## 10. How apps call the kernel

**Files:** new `lib/pigeon/sys.h` and `lib/pigeon/sys.c`, and a table in the kernel

**A system call is a call through a table.** There is no trap instruction, so
the kernel writes the addresses of its service functions into a table at a
fixed address when it boots, and apps call through it. Function pointers
already work, and both sides are built by the same compiler with the same
calling convention.

**This is better than each app bundling its own libraries**, for two reasons:

- **Size.** `fs.c` is about 99 KB per app.
- **Two copies of `fs.c` on one disk disagree.** Each keeps its own block
  cache, and its own free-block count and next-free position, in RAM. After an
  app writes the disk, the kernel's copy is stale, and its next allocation
  could hand out blocks the app just used — cross-linking files *(reasoned
  from `fs.c`'s design, not run)*.

If apps do bundle their own `fs.c`, the kernel must at least **unmount before
starting an app and mount again after**, which drops its stale cache.

A first set of calls might be: print a string, read a key, open, read, write
and close a file, list a directory, get the program's arguments, and run a
program.

---

## 11. The kernel

**Files:** new, built with the kernel layout

**At boot:** mount the disk, fill in the system-call table, reset the display
and timers.

**Running an app:**

1. load the file at `APP_LOAD_ADDR` — at or above `PROGRAM_LOAD_ADDR`, so
   `fs_load` uses DMA
2. check its header (below) and size
3. `CALL` its entry point, and take its exit code from `A`

**Cleaning up after it**, because the app shared the devices and changed
their state:

- **the display**: point it back at `DISPLAY_START`. An app that called
  `disp_use_back_buffer()` leaves it showing a buffer on the app's heap.
- **the kernel's own display variables**, `disp_target` and `disp_back`
- **timers** the app started: stop them
- **input**: empty the keyboard and mouse queues

**An executable header.** Today a `.bin` is raw instructions with nothing in
front. Without a header the kernel cannot tell an app from a kernel-layout
binary or a random file, and would jump into anything. At least: a magic
number, a format version, the load address, the entry point, and the size.
The compiler or assembler writes it; the old BIOS path keeps raw binaries.

**Size.** A kernel with `fs.c`, display, input and string would be about
170 KB, well inside 1 MB.

---

## 12. The console and the shell

**Files:** new `lib/pigeon/console.h` and `console.c`; the shell in the kernel

**A console library** — there is no console device and nothing that scrolls:

- a 32×12 grid of characters
- scrolling, a cursor, and printing with newlines
- line editing: typing, backspace, enter

`user/files.c` and `user/disc.c` each lay out their own screens today; neither
is a console.

**The shell, inside the kernel for a first version.** If the shell were itself
an app, it could not run another app: both would need `APP_LOAD_ADDR`.

**Commands:** `ls`, `cd`, `cat`, `run <name>` — or looking bare names up in
`/bin` — and copying from the CD drive.

---

## 13. Build, launcher and tests

**Build and launch** (`emulator/programs.py`, `emulator/cli.py`, `config.json`)

- Build stage 1 from assembly, the kernel with the kernel layout, and apps
  with the app layout and a header.
- Install them onto `disks/hdd.img` and boot with nothing on channel 1.
- Keep today's programs in `user/` running the old way. The launcher has to
  tell the two kinds apart.

**Tests** (`tests/`)

- **Boot:** a disk with a valid signature boots; a bad signature, a missing
  disk and a short read each stop with an error.
- **Install:** `install-boot` places everything, and `fsck` catches a boot
  record that no longer matches the kernel file.
- **Layouts:** programs built at other addresses actually run, with their
  frame stacks and heaps where they were put.
- **App startup:** returning from `main` gets back to the caller with the
  stack balanced and `F` restored.
- **The kernel:** load, run and return; a call through the system-call table;
  the display and timers reset after an app exits; a binary built for the wrong
  layout refused.

---

## 14. Optional: CPU changes

None of these is needed for a simple single-tasking system.

- **Instructions to read and set the stack pointer**, appended to the
  instruction set. They make `exit()` from anywhere possible, and a separate
  stack for each program.
- **CPU faults instead of Python exceptions** for divide by zero, a bad opcode
  and a fetch past RAM, so a crashing app returns to the shell instead of
  ending the emulator.
- **Interrupts**, starting with a timer interrupt, for preemptive
  multitasking. A large change to the CPU and the IO controller.
- **Memory protection.** Today any app can overwrite the kernel.

If the BIOS ever needs more than 128 instructions, `BIOS_MAX` can grow, but
the IO region and the framebuffer are placed right after it and would move.
C programs pick up the new addresses from the predefined symbols; anything
with a typed address would not.

---

## 15. Suggested phases

Each can be tested before the next exists.

1. **Compiler layouts, app startup code, and the executable header.**
   Testable with no kernel: build a program at another address and run it.
2. **The sector-booting BIOS, stage 1, and `pfs.py install-boot`**, including
   `pfs.py` keeping the superblock's unused bytes.
3. **The kernel:** loader, device cleanup, console, and the built-in shell.
4. **The system-call table**, so apps stop carrying their own `fs.c`.
5. **The launcher, `config.json`, and the docs.**

---

## 16. Open questions

Answers inline, please — then I'll fold them into the sections above and turn
this into a decision log.

1. **The boot record.** Option A keeps the disk format as it is, and puts the
   boot code and record in the superblock's unused bytes — which means changing
   `pfs.py` to stop erasing them. Option B reserves boot blocks before the
   filesystem, which is a new format version. Which?

2. **The shell.** Inside the kernel, which is simplest, or a separate app —
   which needs a second app address so the shell and the program it runs
   don't collide?

3. **How apps use the kernel.** A system-call table, so apps are small and
   only the kernel touches the filesystem? Or apps bundle their own libraries,
   and the kernel unmounts before each app and mounts again after?

4. **The CPU.** Leave it as it is for now — apps exit only by returning from
   `main`, and a crashing app ends the emulator — or add stack-pointer
   instructions and CPU faults as part of this work?
