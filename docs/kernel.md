# A kernel and a shell

> **Status: proposal, nothing in the repository changed — but most of it has
> been run.** A prototype added the proposed CPU instructions to the emulator
> at runtime, and ran C kernels and programs compiled by `pigeon-cc` through
> them (§16). Boot (§4–7) and the console (§12) were not prototyped. Facts in
> §2 were checked in the code on 2026-09-13. Anything reasoned but not run is
> marked *unverified*. Details live in [kernel_exec.md](kernel_exec.md)
> (running a program, relocation) and [kernel_changes.md](kernel_changes.md)
> (printing from programs). Five questions are open, in
> [§18](#18-open-questions), for you to answer inline.

| Area | Today | Proposed | Run in the prototype |
|---|---|---|---|
| Boot | The BIOS copies a whole program from channel 1 | The BIOS loads a boot sector, which loads the kernel | No |
| Programs in memory | Every C program is built for `0x20000` | The kernel stays there; each program is relocated to wherever there's room | Yes |
| Starting a program | — | The kernel loads `/bin/<name>.bin`, patches it and calls it | Yes, from a PigeonFS disk |
| Ending one | `HALT` stops the machine | Return from `main`, or `exit()` from anywhere | Yes |
| Services | Each program bundles its libraries | A system-call table the kernel fills in | Yes |
| Crashes | End the emulator | A fault reaches the kernel, which abandons the program | Yes |
| Ctrl-C | Impossible | A break interrupt | Yes; the device side was simulated |
| Timer | Polled | A periodic interrupt | Yes; the device side was simulated |
| Multitasking | Impossible | Optional: the timer interrupt switches stacks | Yes, three programs |
| Console, shell | None | A console in the kernel; the shell is a program | No |

---

## 1. What is possible

- **With the CPU as it is:** one program at a time. The shell runs a program
  and gets control back when it returns from `main`. A crash ends the
  emulator, and nothing can stop a program that never returns.
- **With six new instructions (§13):** `exit()` from anywhere, crashes that
  return to the shell, Ctrl-C, a timer tick — and preemptive multitasking, if
  the kernel wants it (§14). All of these ran in the prototype.
- **Still not possible:** memory protection. Any program can overwrite the
  kernel or another program.

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
- **The zero and less flags live in the Python `CPU` object.** Only `CMP` sets
  them, and no instruction reads or writes them.
- `JMP` and `CALL` take a register as well as an address (`reg_or_imm`).
- Opcodes are numbered in declaration order, and the file says new instructions
  can safely be **appended**.
- Dividing by zero raises a Python exception, and an unknown opcode raises
  `RuntimeError`. Either one ends the emulator, not just the program.
- **Instructions run in three places:** `CPU.run` (tests, the benchmark),
  `Machine.step` (the debugger, tests) and a copy inlined in `Machine.run`
  (what users run). `test_run_and_step_execute_identically` keeps the last two
  in step.
- `Machine.run` does its slow work — the clock, the display — once every
  10,000 instructions (`CLOCK_SAMPLE_INTERVAL`). It doesn't catch a fetch past
  the end of memory: that `try` is commented out (`machine.py:185`).

**IO and devices**
- **A device is programmed one field at a time** — `IO_RW`, `IO_CMD`,
  `IO_LEN`, `IO_ADDR` — and the write to `IO_CH` fires the command, which
  finishes before the next instruction. The reply is then read from the data
  window (`lib/pigeon/io.h`, `io_controller.py`). There is one header and one
  window for the whole machine.
- The timer is wall-clock based and polled — `START`, `STOP`, `RESET`,
  `STATUS` — and raises nothing.
- The keyboard and mouse are fed by an HTTP server thread, into queues guarded
  by a lock (`devices/hid.py`). Ctrl has keycodes, `KEY_LCTRL` and `KEY_RCTRL`.

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

**The compiler and assembler** (`compiler/`, `assembler/assembler.py`)
- Every C program is emitted with `.ORG PROGRAM_LOAD_ADDR` (`codegen.py:80`).
- The startup code `__start` sets the frame pointer `F` to `__frame_base`,
  sets up the heap, calls `main` with no arguments, then **`HALT`s**
  (`codegen.py:91–97`).
- `__frame_base = HEAP_START`, the frame stack is 262,144 bytes, and
  `__heap_base` follows it (`codegen.py:164–166`).
- `cc.py`'s only options are the sources, `-o`, `-S` and `-I`.
- `mem.c`'s heap limit is a typed literal, `0x08000000 - 0x00100000`.
- Function pointers compile to `CALL E` and have tests.
- **Variadic functions are rejected** (`parser.py:288`), so there is no
  `printf` (kernel_changes.md §3.1).
- **A function declared but not defined can't be called** — "not declared".
  `extern int name;` plus a cast to a function pointer can, which is how C
  reaches hand-written assembly.
- **Every instruction needs a row in the assembler's `SYNTAX` table**, checked
  at import (`assembler.py:240`). The disassembler reads the instruction table
  itself.
- **`NAME = expression` is worked out before labels have addresses**, so it
  can't use a label. Code placed after data must start at a multiple of 8.

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
- `fs.c` keeps its cache, open files and current directory in `static`
  globals, so it can't be entered twice at once.

**Everything else**
- `display.c` keeps its state in each program: `disp_target`, `disp_back` and
  `disp_hw`. A program that calls `disp_use_back_buffer()` points the display
  at a buffer on its own heap.
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
  way to install programs onto the disk.

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
  `user/` depends on it, and it can start the kernel before boot exists.

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
- **Programs are ordinary files in `/bin`**, copied in with the existing
  `pfs.py put`. P5 did exactly that.

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
  and no other byte differed at all. The largest program has 1,934 words to
  patch, 99% of them in an instruction's address field.
- **The frame stack and heap move to just after the image.** Startup code
  uses `__image_end` and `__image_end + frame size`, written into the
  instructions, because `NAME = expression` can't use a label (§2).
- **A program file** is a 32-byte header — magic, version, image size,
  entry, patch count, frame-stack size, where `__heap_ptr` is, and the address
  it was built for — then the image, then the offsets.

**Where the kernel puts it**

```
0x00000000  BIOS, IO window, framebuffer
0x00015818  boot sector; system-call table
0x00020000  kernel image                        fixed; vector table and interrupt frame stack are kernel globals
0x00120000  kernel frame stack and heap         the heap must stop below 0x01000000
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
- **The kernel's heap needs a limit below `0x01000000`.** Today `mem.c` returns
  a typed `0x07F00000` for every program.
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

- **A system call is a call through a table** at a fixed address, `0x15818`,
  that the kernel fills in at boot. Programs call through a `typedef`'d
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

A first set of calls is in kernel_exec.md §8.

---

## 11. The kernel

**Files:** new, built at `0x20000`

**At boot:** mount the disk, fill in the system-call and vector tables, set up
the console, and start `/bin/sh.bin`. Whether the kernel starts the shell again
when it exits, or stops, is still open (kernel_exec.md Q2). The prototype's
kernel stops.

**Running a program** (P4, P5):

1. choose its address: `0x01000000` for the shell, otherwise just above the
   parent's heap top
2. `fs_load` the file to 32 bytes below that address
3. check the header: magic, version, and that the file is as long as the
   header says
4. patch every listed address
5. record it — address, heap-top pointer, saved stack — and `exec_call` it
6. take the status: from `main`, from `exit`, or from a fault or break

**Cleaning up after it** *(unverified — not in the prototype)*: point the
display back at the screen, stop timers it started, empty the input queues,
and close files it left open.

**Size.** P5's kernel, with `fs.c`, was 123,192 bytes.

---

## 12. The console and the shell

- **The shell is a program** the kernel starts (decided, §18).
- **A console library in the kernel** *(unverified — the prototype's console
  was a text buffer)*: a 32×12 grid, scrolling, a cursor, and line input
  with echo and backspace.
- **Programs print through it** with a system call. `printf` needs variadic
  functions first (kernel_changes.md §3.1).
- Commands, argument splitting and program lookup are in kernel_exec.md §3.

---

## 13. CPU changes

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
| 0 | fault | `DIV` by zero |
| 1 | fault | an unknown opcode |
| 2 | fault | fetching past the end of memory |
| 3 | interrupt | the timer |
| 4 | interrupt | break: Ctrl-C |

### 13.4 Faults

- **Delivered even with interrupts off.** The pushed address is the faulting
  instruction's own, so a handler could fix the cause and retry it. The kernel
  abandons the program instead (§9).
- **With no vector table set, a fault stops the emulator as it does today**,
  so every existing program behaves the same.
- **A fault inside kernel code** halts with "panic" in the prototype. A fault
  while entering a fault handler should stop the emulator *(unverified)*.

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

### 13.6 Devices *(unverified — the prototype raised these itself)*

- **Timer:** a new command starts a periodic interrupt every so many
  milliseconds, and `STOP` ends it. The check fits where `Machine.run` already
  reads the clock.
- **Keyboard:** a new command turns on break, so Ctrl+C raises vector 4
  instead of queuing a `c`. Keys arrive on a server thread, so HID sets a flag
  of its own, which the loop turns into a pending bit. Changing the CPU's
  pending bits from two threads could lose one.
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
| `tests/` | each instruction and handler entry; compiled C under interrupts, like P2; kernel execution tests, like P4–P6 |

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
- **A heap limit for each program.** `mem.c`'s typed `0x07F00000` is shared
  by every program.
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

- Build stage 1 from assembly, the kernel at `0x20000` as today, and programs
  as relocatable files.
- Install them onto `disks/hdd.img` and boot with nothing on channel 1.
- Keep today's programs in `user/` running the old way. The launcher has to
  tell the two kinds apart.

**Tests** (`tests/`)

- **Boot:** a disk with a valid signature boots; a bad signature, a missing
  disk and a short read each stop with an error.
- **Install:** `install-boot` places everything, and `fsck` catches a boot
  record that no longer matches the kernel file.
- **Relocation:** a program built at one address and patched to another runs
  there, with its frame stack and heap after its image.
- **CPU:** each new instruction; flags and interrupt state restored by
  `IRET`; faults with and without a vector table; `run` and `step` taking
  interrupts at the same instructions.
- **The kernel:** load, run and return; `exit` from deep recursion; faults and
  break; system calls; programs running programs; the display and timers reset
  after a program; a file with a bad header refused.

---

## 16. What the prototype ran

**How.** A Python harness added the six instructions to the emulator's
instruction and syntax tables at runtime; the repository wasn't changed. It
ran C compiled by `pigeon-cc`: kernels built at `0x20000` with hand-written
assembly routines, and programs built as relocatable files (§8). The harness
raised the device interrupts itself. The scripts are in
[prototypes/kernel/](../prototypes/kernel/README.md), named after the rows
below.

| | What ran | Result |
|---|---|---|
| **P1** | The repository's tests, with the six instructions and syntax rows loaded | 844 passed |
| **P2** | A compiled workload — a sort, a sieve, recursion, function pointers; 119,032 instructions — interrupted after every instruction, and every 2, 3, 7 and 101 | The same result every time; 119,006 interrupts at every instruction; stack and `F` restored. **Controls:** `IRET` not restoring the flags never halted, or gave a wrong result at every 7th; a handler using the interrupted `F` never halted |
| **P3** | A program asking the timer for its status 2,000 times, while a handler does IO | Unprotected: 2,000 wrong when interrupted every 1, 3 or 7 instructions; 923 at 31; 394 at 101; 33 at 1,009. `DI`/`EI` around the program's IO: 0. The handler saving the header: 0 |
| **P4** | A kernel with the system-call and vector tables and relocation. It runs a shell as a relocated program, which runs: `hello` with arguments and `malloc`; `exit(42)` from 50 calls deep; a divide by zero 20 calls deep; a jump into data; a loop stopped by break; and a program that runs two more | All 17 lines of console output exactly right — with no timer, and with a timer every 997, 13 and 1 instructions (61,827 interrupts). Three levels deep; the parent's heap intact; memory reused; stack and `F` restored at the end |
| **P5** | P4 with `fs.c` in the kernel, the programs as files in `/bin` on a PigeonFS image made by `pfs.py`, the shell turning a name into `/bin/<name>.bin` for the kernel's `exec`, which takes a path, and an `ls` program listing `/bin` through a kernel call | The same output plus a correct listing, with a timer every instruction too. Programs were loaded by 28 `READ_DMA` transfers, with no block reads through the IO window |
| **P6** | Three relocated programs switched by a timer interrupt | §14. **Control:** a print without `DI`/`EI` lost one character of 280 |
| **P7** | The cost of checking for interrupts, on copies of `Machine.run`'s loop; medians of 5 runs of 3.57 million instructions | CPython, 2.74 million IPS: every instruction −4.1%, in the slow path +1.4%. PyPy, 34.8 million IPS: +1.9% and +0.7%. Only CPython's every-instruction check cost more than the noise |

**Not run:** boot (§4–7); the console and the shell's interface; the timer
and keyboard device changes; cleanup after a program; a fault while entering
a fault handler; IO from programs that are preempted; `printf`.

---

## 17. Suggested phases

Each can be tested before the next exists. Phases 1–3 were run in prototype
form.

1. **Relocatable programs:** the startup code, the frame stack and heap after
   the image, and the build step that compares two builds. Testable with no
   kernel: patch a program to another address and call it.
2. **The CPU:** `GETSP`, `SETSP` and faults first; then `EI`, `DI`, `IRET`,
   `SETIV`, the timer interrupt and break.
3. **The kernel:** system calls, `exec` from `/bin`, `exit`, faults and break.
   Built at `0x20000`, so the old BIOS path can start it before boot exists.
4. **The console and the shell.**
5. **The sector-booting BIOS, stage 1, and `pfs.py install-boot`**, including
   `pfs.py` keeping the superblock's unused bytes.
6. **The launcher, `config.json`, and the docs.**
7. **Optional: multitasking** (§14).

---

## 18. Open questions

Answers inline, please — then I'll fold them into the sections above and turn
this into a decision log. Smaller shell questions are still open in
kernel_exec.md §10 and kernel_changes.md §4.

1. **The boot record.** Option A keeps the disk format as it is, and puts the
   boot code and record in the superblock's unused bytes — which means changing
   `pfs.py` to stop erasing them. Option B reserves boot blocks before the
   filesystem, which is a new format version. Which?

2. ~~**The shell.**~~ **Decided 2026-09-13 (you):** the shell is a separate
   program that the kernel starts, not part of the kernel.

3. **How programs use the kernel.** The filesystem and console through
   system calls, with only libraries that keep no state — `string.c`,
   `math.c` — bundled into programs? Or programs bundle everything, and the
   kernel unmounts before each program and mounts again after? P5 ran the
   first, and the console has to go through the kernel either way. I'd choose
   system calls.

4. **Where programs go.** Relocation (§8), or one of the other options in
   kernel_exec.md §4? P4 and P5 ran relocation end to end. I'd choose
   relocation.

5. **How far with the CPU, and how precisely.**
   - **a.** `GETSP`, `SETSP` and faults: `exit()`, and crashes that return to
     the shell.
   - **b.** Plus `EI`, `DI`, `IRET`, `SETIV`, the timer interrupt and break.
   - **c.** Plus multitasking in the kernel.

   I'd do a and b now, and c later: it also needs an allocator, per-program
   heap limits and an IO rule (§14). And should interrupts be checked before
   every instruction — 4.1% slower on CPython, no cost on PyPy, and `run` and
   `step` agree trivially — or every 10,000? I'd check every instruction.
