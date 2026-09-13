# Installation media: a two-stage BIOS, the boot sector and `cc.py --project`

> **Status: phases 1 and 2 are built** (§9): stage 1 of the BIOS, the
> firmware device, and a bios2 with no screen yet. Everything after them is
> still a sketch. Power-on runs through four stages:
>
> 1. **The BIOS**, 1 KB at address 0, loads **bios2** from a firmware
>    device on IO channel 7, and jumps to it.
> 2. **bios2** is a C program with the font, the screen, the 2-second
>    countdown and the boot menu. It boots a program on channel 1, or copies
>    the hard disk's or the CD's boot sector into memory and jumps to it.
> 3. **The disc's boot sector** loads the installer and runs it.
> 4. **The installer.** `cc.py --project` writes the disc. What the installer
>    does comes later (§8).
>
> Stage 1 is built and tested. The boot sector was only drafted and
> assembled, and bios2's size comes from a compiled probe; neither was run.
> Their sizes are in the table below, and facts in §2 were checked in the
> code on 2026-09-13. Anything reasoned but not run is marked *unverified*.
> You left the open questions to me, and the decisions are in
> [§10](#10-decisions).
>
> The boot sector is kernel.md §5 option A, which settles kernel.md Q1.

| Piece | Where it runs | What it does | Size |
|---|---|---|---|
| **BIOS** (`firmware/bios.asm`) | `0x0` | Loads bios2 from channel 7 by DMA and jumps. Without bios2, runs today's channel-1 loader | 944 of 1,024 bytes, built |
| **bios2** (`firmware/bios2.c`) | `0x07000000` | Screen, countdown, menu; boots channel 1, or a boot sector from the hard disk or the CD | Phase 2, no screen: 2,024 bytes. The probe with a screen: 28,496 |
| **Firmware device** | channel 7 | A read-only HDD holding `build/bios2.bin` | — |
| **Boot sector** (`firmware/boot.asm`) | `0x15898` | Loads the file its boot record names into `0x20000`, and jumps | 320 of 384 bytes, drafted |
| **`cc.py --project`** | host | Builds the installer and files into a PigeonFS disc, with the boot sector in block 0 | — |
| **Installer** | `0x20000` | Later: writes the hard disk's boot sector, and mirrors the disc onto it | — |

---

## 1. Power-on, step by step

```
BIOS, at 0x0
  channel 7 holds bios2?     DMA it to 0x07000000 and jump
  otherwise                  today's loader: channel 1 into 0x20000

bios2, at 0x07000000
  check channel 1, the hard disk (channel 2) and the CD (channel 6)
  draw the screen and count down 2 seconds; Esc opens the menu
  a program on channel 1:    DMA it to 0x20000, and call it
  a disk or a disc:          copy its block 0 to 0x15818, store the channel
                             at 0x15A18, and call 0x15898

boot sector, at 0x15898
  read the file its boot record names into 0x20000, and jump there

installer, at 0x20000
  an ordinary compiled program
```

---

## 2. The facts that decide it

**The BIOS, the compiler and the debugger**
- **The BIOS region is 1 KB**, 128 instructions, and `bios.py` refuses more
  (`emulator/bios.py:20`). Today's BIOS is 688 bytes, 86 instructions.
- **The CPU starts with PC at 0 and SP at `STACK_TOP`**
  (`emulator/cpu.py:24`).
- **Every C program is built for `PROGRAM_LOAD_ADDR`**, with its frame stack
  and heap at `HEAP_START` (`compiler/codegen.py:80`, `:164`). The kernel
  prototype builds C for another address by replacing that one line
  (`prototypes/kernel/proto.py:196`).
- **C passes arguments on the frame stack, not in registers**
  (`compiler/design/03-abi.md`). So bios2, written in C, can't hand the boot
  sector a value in `A` without assembly.
- **Calling a function pointer in C is a `CALL`**, which leaves a return
  address on the hardware stack. Today's BIOS jumps, so a program starts with
  SP at `STACK_TOP`. Through bios2 it starts 8 bytes lower, which phase 2
  measured; programs end in `HALT`, and `screen.asm` ran to it that way.
- **The debugger runs freely until PC reaches `PROGRAM_LOAD_ADDR`**, then
  steps (`emulator/cli.py:168`). A bios2 at `0x07000000` would count as the
  program.
- **The display library and its font compile to 17,856 bytes.** A bios2
  probe with a screen, countdown, menu, device checks and a loader compiled
  to 28,496 bytes. It was not run.

**Devices**
- **An `HDD` opens its file writable, and creates a missing file** as 4 MiB
  of zeros (`emulator/devices/hdd.py:97`). It has no read-only mode.
- **One `READ_DMA` moves a whole file**, but only into RAM at or above
  `0x20000` (`hdd.py`, the DMA notes).
- **Channel 7 is free** (`emulator/memory_map.py:95`). An unregistered
  channel answers `0xFFFFFFFF` (`emulator/io_controller.py:15`).
- **`fs.c` and `cd.c` refuse channels 0, HID, timer and display by number**
  before sending any command (`lib/pigeon/fs.c:554`, `lib/pigeon/cd.c:52`).
  Any other channel gets probed as a disk.
- **The CD returns at most 4 KB per `READ` and has no DMA**
  (`emulator/devices/cd.py:485`). A disc can only be put in once the machine
  is running.

**Disks**
- **The superblock's bytes 52–63 and 128–511 are unused**, and nothing can
  jump to the start of block 0 (kernel.md §2).
- **Anything that rewrites the superblock erases those bytes:** `fs_format`
  (`lib/pigeon/fs.c:581`), and `pfs.py`'s `_write_super()`
  (`tools/pfs.py:357`).

**Tests**
- **15 `Machine(bios_path=…)` calls in `tests/`** boot channel-1 programs
  through `build/bios.bin`, and expect it to be built already.
- **`tests/test_loader.py`** checks that big images load whole and that the
  screen is blank when the program starts (`:102`, `:124`).

---

## 3. The firmware device: channel 7

- **`CH_BIOS2 = 7`** in `memory_map.py`. A second CD drive would then take
  8.
- **An `HDD` opened read-only on `build/bios2.bin`**. A new `readonly` option
  opens the file `rb` and never creates it. `WRITE`, `TRUNCATE` and
  `WRITE_DMA` are refused. It is given the RAM, so DMA works.
- **`Machine` registers it only when given a bios2 path**, as it does for
  channel 1. `Machine(bios2_path=None)` is today's machine exactly.
- **`fs.c` and `cd.c` refuse channel 7** the way they refuse the display: one
  line each. So no program can mount or format the firmware.
- **The launcher:**
  - `bios2_binary` in `config.json`, and `--bios2 PATH`. The configured
    file is optional; one named with `--bios2` must exist. *(Phase 1.)*
  - `bios2_source`, rebuilding bios2 when it or its libraries change, and
    `cc.py --org ADDR`, which replaces the one `.ORG` line. *(Phase 2,
    with the first bios2 source.)*

---

## 4. Stage 1: `firmware/bios.asm`

1. **Ask channel 7 for its size.** If it answers with 8 bytes and a size
   above 0, write `[0x07000000, size]` into the IO window and send
   `READ_DMA`. If the bytes moved equal the size, jump to `0x07000000`.
2. **Otherwise run today's loader** for channel 1, unchanged.

Assembled, not run:

| Version | Bytes | Instructions |
|---|---|---|
| Today's BIOS | 688 | 86 |
| Loads bios2, and nothing else | 248 | 31 |
| Loads bios2, or falls back to today's loader | 928 | 116 |
| **As built:** the same, plus a check of the size against `BIOS2_MAX` | 944 | 118 |

**With the fallback, nothing that exists changes.** Every test, and every
`Machine` built without bios2, boots exactly as today. The progress bar
stays: the boot-sector check moved into bios2, so stage 1 no longer has to
lose anything to fit.

---

## 5. Stage 2: bios2

### 5.1 Where it runs

- **Built for `BIOS2_LOAD_ADDR = 0x07000000`**, a new constant in the memory
  map.
  - It's above anything a booted program uses at the start: the image ends
    by `0x11FFFF`, and the frame stack and heap begin at `0x120000`.
  - It's below the last megabyte, which the hardware stack grows into
    (`mem.c`'s heap limit, `0x07F00000`).
- **Why high:**
  - DMA needs an address at or above `0x20000`, so stage 1 loads bios2 in one
    command.
  - Nothing bios2 loads lands on it: programs go to `0x20000`, and boot
    sectors to `0x15818`.
- **Its frame stack and heap stay at `HEAP_START`**, as for any C program. A
  channel-1 program of up to 1 MB ends below them, and bios2 refuses a bigger
  one.
- **The debugger's threshold becomes the program region**,
  `PROGRAM_LOAD_ADDR` up to `+ PROGRAM_MAX_SIZE`. Otherwise it would
  single-step through bios2's countdown.
- **The unused gap below `0x20000` was the other choice.** It would leave
  the debugger alone, but it costs a 42,984-byte ceiling, a window loop
  instead of DMA, and a different address for the boot sector copy.

### 5.2 What it shows

The screen is 32 characters by 12 rows.

```
PIGEON BIOS
128 MB RAM

  Program    32044 bytes
  Hard disk  PIGEONOS
  CD         no disc

Booting Program in 2
ESC  boot menu
```

```
BOOT MENU

> Program    32044 bytes
  Hard disk  PIGEONOS
  CD         PIGEONOS

UP DOWN  choose
ENTER    boot
```

1. **Check each device.** Channel 1 is bootable when it holds a program.
   The hard disk and the CD are bootable when block 0 has the boot signature
   at byte 52. **bios2 checks only that.** It reads the volume label at byte
   36 just to show a name.
2. **Draw the screen, and count down 2 seconds** on the timer.
3. **Esc opens the menu, and Enter boots straight away.** In the menu, the
   arrow keys choose and Enter boots the choice.
4. **With no key pressed, boot the first bootable device**, in this order:
   the program on channel 1, the hard disk, the CD.
5. **A device that can't boot** is shown in grey with its reason: `no disc`,
   `no disk` or `no boot sector`.
6. **With nothing bootable**, the menu waits. It checks the CD again whenever
   the drive's generation counter moves, so a disc put in from a front end
   shows up without a key press.

### 5.3 Handing over

- **Before either jump:**
  - clear the screen, which `test_loader.py` expects;
  - point the display back at the framebuffer, if bios2 drew into a back
    buffer;
  - empty the key queue, so the Esc and Enter from the menu don't reach the
    program.
- **A program on channel 1:** `READ_DMA` it into `0x20000`, and call it.
- **A boot sector:**
  - read block 0 through the IO window, and copy it to `0x15818`. It's the
    window because DMA refuses anything below `0x20000`;
  - write the channel number to `BOOT_CHANNEL`, `0x15A18`, the word just after
    the copy;
  - call `0x15898`.
- **Both are calls**, so the program starts on top of two of bios2's return
  addresses: SP is `STACK_TOP - 8`. `tests/test_bios2.py` pins it.

---

## 6. The boot sector: `firmware/boot.asm`

Block 0 of a bootable disk:

| Bytes | What | Written by |
|---|---|---|
| 0–51 | the superblock | `pfs.py` |
| 52–55 | boot signature `0x54424750`, "PGBT" in byte order | `cc.py --project` |
| 56–59 | the first block of the file to boot | `cc.py --project` |
| 60–63 | its size in bytes | `cc.py --project` |
| 64–127 | the root directory's entry | `pfs.py` |
| 128–511 | the boot sector: 384 bytes, 48 instructions | `cc.py --project` |

- **The draft is 320 bytes, 40 instructions**, assembled and not run. It
  reads the channel from `0x15A18`, and the first block and size from
  `0x15818 + 56` and `+ 60`. It then reads 4 KB windows into `0x20000`
  onward and jumps there. A read that returns 0 bytes stops with `HALT`.
- **One code path for the hard disk and the CD:** the IO window only, because
  the CD has no DMA.
- **The disk format doesn't change.** `fs.c` and `pfs.py` never read those
  bytes, but both erase them when they rewrite block 0. So they are written
  last.

---

## 7. The project file and `cc.py --project`

```ini
# user/os/pigeon_compiler_init.txt

[project]
name    = PigeonOS
version = 0.1
label   = PIGEONOS              # the disc's volume label, 15 bytes at most

[boot]
installer  = installer.c        # the disc's boot sector loads and runs this
bootsector = boot.asm           # optional: firmware/boot.asm when left out

[files]                         # what the installer will mirror onto the hard disk
/bin/files.bin   = ../files.c   # a .c or .asm is built; anything else is copied
/docs/readme.txt = readme.txt
```

**`python3 compiler/cc.py --project user/os/pigeon_compiler_init.txt`**
writes `build/pigeonos.img`:

1. **Check the project file.** Every mistake is reported with its line,
   before anything is built.
2. **Build the installer** for `0x20000`, as the launcher builds a program,
   and every `.c` and `.asm` in `[files]`.
3. **Assemble the boot sector**, and refuse it if it's over 384 bytes.
4. **Make the image** with `PgfsImage.mkfs`, big enough, with the label.
5. **Write `/install.bin` first**, then the files. Check that `/install.bin`
   is contiguous, which the first file on a fresh image should be. Refuse the
   image if it isn't.
6. **Close the image, then write the boot record and the boot sector** into
   block 0.
7. **Run `fsck`**, and print each file with its size.

**The launcher gets `--cd PATH`, and a `"cd"` key in `config.json`** that
defaults to `null`. Otherwise a disc can't be in the drive when bios2 looks.

---

## 8. Later: the installer

What this step has to leave room for:

- **Same block 0 layout on the hard disk.** The installer can copy the boot
  sector out of the disc's own block 0.
- **Format first, then write block 0.** Unmount the disk before writing
  block 0 directly, or `fs.c` may write its cached copy back over it
  *(unverified)*.
- **The hard disk's record points at the system file**, which has to stay
  contiguous. Anything that rewrites that file has to rewrite the record too
  (kernel.md §5).
- **`pfs.py`'s `_write_super()` must keep bytes 52–63 and 128–511** before
  it is used on an installed hard disk.

---

## 9. What changes, in phases

1. **The firmware device and stage 1.** ***Done.***
   - **What was built:**
     - `CH_BIOS2 = 7`, `BIOS2_LOAD_ADDR` and `BIOS2_MAX` (15 MB) in the
       memory map.
     - `HDD(readonly=True)`.
     - `Machine(bios2_path=…)`, which refuses a missing or oversized file.
     - `bios2_binary` in `config.json`, and `--bios2`.
     - The guards in `fs.c` and `cd.c`.
     - The debugger steps only inside the program region.
   - **Stage 1 is 944 bytes, 118 instructions:** the draft, plus a check of
     the size against `BIOS2_MAX`, so a bios2 can never be copied into the
     hardware stack.
   - **`tests/test_bios2.py`, 25 cases:**
     - a bios2 of anything from 816 bytes to 1 MB arrives with one `GET_SIZE`
       and one `READ_DMA`;
     - it beats a program on channel 1;
     - with no device or an empty file, channel 1 boots as before;
     - a size over the limit, a size with the top bit set, a short transfer
       and a refused one all fall back;
     - every write to the device is refused;
     - the launcher's rule, and the debugger's threshold.
   - **The guard tests in `test_fs.py` and `test_cdlib.py`** now check that
     nothing at all reaches channel 7. Their "empty channel" cases moved from
     7 to 8: channel 7 is now refused by number before the probe those cases
     exist to test.
   - **Nine deliberate breakages each failed the tests:**
     - either check in stage 1;
     - either refusal in the device, or its rule never to create the file;
     - `Machine`'s size limit;
     - the debugger's upper bound;
     - either library guard.
   - **The full suite passes: 874 tests**, the 844 from before and 30 new.
2. **bios2, with no screen yet.** ***Done.***
   - **`firmware/bios2.c`, 2,024 bytes.** It asks channel 1 for its size,
     copies the program to `0x20000` with one `READ_DMA`, clears the screen
     with the display's `FILL`, and calls it. There's no progress bar and no
     wait. When it can't boot, it halts with the reason in `A`: 1 when there's
     nothing to boot, including a program over `PROGRAM_MAX_SIZE`, where its
     own frame stack starts; 2 when the transfer was short or refused.
   - **Emptying the key queue and resetting the scanout base wait for
     phase 3.** This bios2 reads no keys and draws nothing.
   - **Boot cost, measured on `build/files.bin` (169 KB):**

     | Route | Instructions from power-on to `0x20000` | Time |
     |---|---|---|
     | Stage 1 alone | 2,887,051 | 2.44 s, 2 s of it the wait |
     | Through bios2 | 267 | under 0.01 s |

   - **`cc.py --org ADDR`** takes a number or a memory-map name, which it
     keeps as a name, and refuses anything that isn't a multiple of 8 inside
     RAM. `generate()` and `compile_units()` take the origin too, and
     `Program` has an `origin`.
   - **The launcher** gains `bios2_source` and rebuilds bios2 when it or its
     libraries are newer than the build, as it does the BIOS. A file named
     with `--bios2` is used as it is, and never built over. Run end to end,
     `start_emulator.py screen --headless --run` built `build/bios2.bin` and
     booted `screen` through it to `HALT`.
   - **Tests:**
     - **`test_bios2.py` covers the real bios2:** programs up to exactly
       `PROGRAM_MAX_SIZE` boot on a screen painted beforehand that ends up
       blank; the three ways there is nothing to boot halt with 1; a short or
       refused transfer halts with 2; the launcher builds bios2 and doesn't
       rebuild an up-to-date build.
     - **Every check in `test_loader.py` now runs twice**, through stage 1
       and through bios2.
     - **`test_compiler.py`** has a program with a global table, a string and
       a function-pointer call, built for `BIOS2_LOAD_ADDR` and run there. It
       also checks what `--org` accepts and refuses.
   - **Eight deliberate breakages each failed the tests:**
     - in bios2, removing the screen clear, the size limit or the transfer
       check;
     - `.ORG` ignoring the origin;
     - `--org` accepting any alignment;
     - `Program` ignoring its origin;
     - the launcher building over `--bios2`, or rebuilding an up-to-date
       bios2.
   - **The full suite passes: 892 tests**, the 874 from phase 1 and 18 new.
3. **bios2's screen, countdown and menu.** Tests queue keys through HID before
   the machine runs.
4. **The boot sector, and bios2 booting it.** A test builds an image with
   `pfs.py`, writes block 0 by hand, and boots a program from it on channel 2
   and on channel 6. Include a program bigger than 4 KB and one that is an
   exact multiple of 4 KB.
5. **The project file, `cc.py --project`, and `--cd`.** The disc boots its
   installer on a `Machine`.

| File | Change |
|---|---|
| `firmware/bios.asm` | loads bios2 from channel 7, else today's loader |
| `firmware/bios2.c`, `firmware/boot.asm` | new |
| `emulator/memory_map.py` | `CH_BIOS2`, `BIOS2_LOAD_ADDR`, `BOOT_LOAD_ADDR`, `BOOT_CHANNEL` |
| `emulator/devices/hdd.py` | `readonly` |
| `emulator/machine.py`, `cli.py`, `config.py`, `config.json` | the firmware device, building bios2, `--bios2`, `--cd`, the debugger's threshold |
| `lib/pigeon/fs.c`, `lib/pigeon/cd.c` | refuse channel 7 |
| `compiler/cc.py`, `compiler/project.py` | `--org`, `--project` |
| `user/os/pigeon_compiler_init.txt`, `user/os/installer.c` | new; the installer is a placeholder until §8 |
| `README.md` | the boot sequence, the memory map, channel 7 |

---

## 10. Decisions

Settled 2026-09-13. You left all nine to me, so each one says why, and any of
them can change.

1. **The firmware device is read-only.** No program can erase the BIOS, by a
   bug or on purpose. Updating bios2 means rebuilding `build/bios2.bin` on the
   host.
2. **Stage 1 falls back to today's loader when there is no bios2.** At 928
   bytes it fits, and nothing that exists changes while bios2 is new. The
   fallback can go once every `Machine` has a bios2.
3. **bios2 runs at `0x07000000`.** One DMA command loads it, it has no size
   ceiling, and nothing it loads lands on it. The debugger's threshold
   changes to the program region.
4. **With no key pressed, the order is channel 1, then the hard disk, then
   the CD.** A program is on channel 1 only because you asked the launcher for
   it, and the install disc can always be picked from the menu.
5. **Esc opens the menu, and Enter boots straight away.** Esc reaches the
   machine from both front ends, and a browser keeps some F-keys for itself.
6. **The boot sector goes in block 0:** the record at bytes 52–63 and the
   code at 128–511, as kernel.md §5 option A. The disk format doesn't change,
   and the draft fits with 64 bytes spare. kernel.md Q1 is marked decided.
7. **`firmware/boot.asm` is shared by every project**, and `bootsector =`
   overrides it.
8. **The project file stays as in §7.** `[files]` builds `.c` and `.asm` by
   extension, so there's no separate `[apps]` section.
9. **The launcher gets `--cd PATH` and a `"cd"` key in `config.json`**, in
   phase 5.
