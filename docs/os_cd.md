# Installation media: a two-stage BIOS, the boot sector and `cc.py --project`

> **Status: phases 1 to 6 are built** (§9): stage 1 of the BIOS, the
> firmware device, bios2 with its screen and menu, the boot sector,
> `cc.py --project` with the launcher's `--cd`, and the installer. The
> example disc installs the graphing calculator onto a blank hard disk,
> which then boots it. Power-on runs through four stages:
>
> 1. **The BIOS**, 1 KB at address 0, loads **bios2** from a firmware
>    device on IO channel 7, and jumps to it.
> 2. **bios2** is a C program with the font, the screen, the 2-second
>    countdown and the boot menu. It boots a program on channel 1, or copies
>    the hard disk's or the CD's boot sector into memory and jumps to it.
> 3. **The disc's boot sector** loads the installer and runs it.
> 4. **The installer** puts the disc on the hard disk and restarts (§8).
>    `cc.py --project` writes the disc.
>
> All four are built and tested; their sizes are in the table below. Facts
> in §2 were checked in the code on 2026-09-13. Anything reasoned but not
> run is marked *unverified*.
> You left the open questions to me, and the decisions are in
> [§10](#10-decisions).
>
> The boot sector is kernel.md §5 option A, which settles kernel.md Q1.

| Piece | Where it runs | What it does | Size |
|---|---|---|---|
| **BIOS** (`firmware/bios.asm`) | `0x0` | Loads bios2 from channel 7 by DMA and jumps. Without bios2, runs today's channel-1 loader | 944 of 1,024 bytes, built |
| **bios2** (`firmware/bios2.c`) | `0x07000000` | Screen, countdown, menu; boots channel 1, or a boot sector from the hard disk or the CD | 46,068 bytes, built, with the display, input, mem and string libraries |
| **Firmware device** | channel 7 | A read-only HDD holding `build/bios2.bin` | — |
| **Boot sector** (`firmware/boot.asm`) | `0x15898` | Loads the file its boot record names into `0x20000`, and jumps; returns to bios2 if it can't | 376 of 384 bytes, built |
| **`cc.py --project`** | host | Builds the installer and files into a PigeonFS disc, with the boot sector in block 0 | built; the example disc is 473.0 KiB |
| **Installer** (`user/os/installer.c`) | `0x20000` | Formats the hard disk, copies the disc onto it, makes it boot `/boot.bin`, and restarts | 139,120 bytes, built |

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
- **A format erases those bytes:** `fs_format` (`lib/pigeon/fs.c:581`) and
  `pfs.py mkfs` both write block 0 afresh. Before phase 4, `pfs.py`'s
  `_write_super()` also rebuilt block 0 from zeros on every write; it now
  updates the block in place, as the guest's `fs.c` does.

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

As built in phase 3, and as `tests/test_bios2.py` reads it back. Rows 8 to
10 are empty:

```
PIGEON BIOS
128 MB RAM

  Program    32044 bytes
  Hard disk  no boot sector
  CD         INSTALL

Booting Program in 2



ESC menu    ENTER boot now
```

```
PIGEON BIOS
128 MB RAM

> Program    32044 bytes
  Hard disk  no boot sector
  CD         INSTALL

BOOT MENU



UP DOWN choose   ENTER boot
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
5. **A device that can't boot** is shown in grey with its reason: `none` or
   `too big` for a program, and `no disk`, `no disc` or `no boot sector`
   otherwise. Enter on one says it can't boot, and so does a boot that
   fails, such as a short transfer.
6. **With nothing bootable**, the menu waits. It checks the CD again whenever
   the drive's generation counter moves, so a disc put in from a front end
   shows up without a key press.

### 5.3 Handing over

- **Before either jump:**
  - stop its countdown timer;
  - clear the screen to 0, which `test_loader.py` expects. bios2 draws
    straight to the screen, so there is no scanout base to restore;
  - empty the character, key-edge and mouse queues, so the Esc and Enter
    from the menu don't reach the program.
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
| 52–55 | boot signature `0x54424750`, "PGBT" in byte order | `PgfsImage.make_bootable` |
| 56–59 | the first block of the file to boot | `PgfsImage.make_bootable` |
| 60–63 | its size in bytes | `PgfsImage.make_bootable` |
| 64–127 | the root directory's entry | `pfs.py` |
| 128–511 | the boot sector: 384 bytes, 48 instructions | `PgfsImage.make_bootable` |

- **As built: 376 bytes, 47 instructions.**
  - It reads the channel from `BOOT_CHANNEL`, and the first block and size
    from its own copy of the record.
  - It reads 4 KB windows starting at that block, copies only what is left
    of the file from each, and jumps to `0x20000`.
- **It returns to bios2 when it can't load the file,** which bios2 shows as
  "boot failed": a size of 0 or over `PROGRAM_MAX_SIZE`, or the disk ending
  before the file does. So it saves `F` first, which is bios2's frame
  pointer; the loop uses `F` for the channel. Fitting that return in took
  trimming the draft by three instructions: one unsigned comparison checks
  for both 0 and too big, and the copy loop ends on an address instead of a
  count.
- **One code path for the hard disk and the CD:** the IO window only, because
  the CD has no DMA.
- **The disk format doesn't change.** `fs.c` and `pfs.py` never read those
  bytes, and both now update block 0 in place, keeping them. A format erases
  them.
- **`PgfsImage.make_bootable(path, sector)`**, and `pfs.py boot PATH` on top
  of it, writes the record and the sector. It refuses a directory, an empty
  file, one over `PROGRAM_MAX_SIZE`, a sector over 384 bytes, and a file
  whose blocks aren't contiguous.

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
system     = ../graph.c         # optional: what the installed hard disk boots
bootsector = boot.asm           # optional: firmware/boot.asm when left out

[files]                         # what the installer puts on the hard disk
/bin/files.bin   = ../files.c   # a .c or .asm is built; anything else is copied
/bin/cube.bin    = ../cube.c
/docs/readme.txt = readme.txt
```

**`python3 compiler/cc.py --project user/os/pigeon_compiler_init.txt`**
writes `build/pigeonos.img`:

1. **Check the project file.** Every mistake is reported with its line, all
   of them at once, before anything is built.
2. **Build the installer** for `0x20000`, as the launcher builds a program,
   and every `.c` and `.asm` in `[files]`. Builds go to `build/<name>/` and
   happen again only when a source or a library it includes changed. Each
   build is named after its source as well as its path on the disc, so
   pointing a path at another source always builds it afresh.
3. **Assemble the boot sector**, and refuse it if it's over 384 bytes.
4. **Make the image** with `PgfsImage.mkfs`, sized for every file and
   directory plus a little room, with the label.
5. **Write `/install.bin` first.** Then `/pigeon.txt`: the title the
   installer shows, then the `[project]` keys. Then `/boot.bin`, the
   `system`, if the project names one; one over 1 MB is refused, since the
   boot sector couldn't load it. Then the files in order, making
   directories as needed.
6. **Make the image boot `/install.bin`** with `PgfsImage.make_bootable`,
   which refuses a file that isn't contiguous. The first file on a fresh
   image is.
7. **Run `fsck`, then move the image into place.** It is written beside the
   output first, so a failed build leaves no half-written disc. The same
   project builds the same disc, byte for byte.

As run on the example:

```
PigeonOS 0.1, from user/os/pigeon_compiler_init.txt
  /install.bin               139,120 B   user/os/installer.c
  /pigeon.txt                     60 B
  /boot.bin                  114,580 B   user/os/../graph.c
  /bin/files.bin             174,088 B   user/os/../files.c
  /bin/cube.bin               40,048 B   user/os/../cube.c
  /docs/readme.txt               254 B   user/os/readme.txt
build/pigeonos.img: 473.0 KiB, label PIGEONOS, boots /install.bin
```

**The launcher's `--cd PATH`, or `"cd"` in `config.json`**, puts a disc in
the drive before power-on. Otherwise a disc can't be in the drive when
bios2 looks.

---

## 8. The installer

`user/os/installer.c`, built onto the disc as `/install.bin`. Booted from
the disc, it puts the disc on the hard disk:

1. **It mounts the disc**, from the channel bios2 left at `BOOT_CHANNEL`.
   It shows the title from `/pigeon.txt`, the hard disk's size, how many
   files it will copy, and whether the disk will boot. Everything on the
   hard disk is erased, so it asks: Enter installs, and Esc cancels with
   nothing written.
2. **It formats the hard disk** with the disc's volume label.
3. **It copies `/boot.bin` first**, so its blocks are one run on the fresh
   disk. Then it copies every other file but `/install.bin`, showing each.
4. **It makes the hard disk boot `/boot.bin`:**
   - it unmounts the disk first, so `fs.c` holds no copy of block 0;
   - `fs.h` has no call that says where a file's blocks are, so it finds
     `/boot.bin`'s entry in the root directory's first block, and follows
     the FAT to check the blocks are one run that ends the chain;
   - it writes the boot record, and the disc's own boot sector, into block 0
     directly, then reads the block back.
5. **Enter restarts,** by calling address 0, where the BIOS still is. The
   hard disk comes before the CD in bios2's order, so the hard disk is what
   boots.

A failure stops with the reason on screen: no hard disk, a disk too small,
or a `/boot.bin` that would not boot.

**The project names what the installed disk boots.** `system = ../graph.c`
in `[boot]` puts the graphing calculator on the disc as `/boot.bin` (§7).

**Run end to end** in `tests/test_install.py`, on a `Machine` with a blank
4 MiB hard disk and the example disc:
- **The install:** the installer copied five files.
- **The disk, read back from the host:**
  - it held them byte for byte;
  - its boot record pointed at `/boot.bin`, with the disc's boot sector
    beside it;
  - `fsck` was clean.
- **After the restart:** bios2 counted down to the hard disk, whose boot
  sector loaded the calculator, and the calculator drew its curve.

**What still holds for an installed disk:** its boot record points at
`/boot.bin`'s blocks. Anything that rewrites that file has to write the
record again (`pfs.py boot` does, on the host; kernel.md §5).

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
3. **bios2's screen, countdown and menu, and its half of booting a disk.**
   ***Done.*** Booting a disk moved here from phase 4: a menu that lists the
   hard disk and the CD but can't boot them is half a feature, and bios2's
   side is only copying block 0 and calling it.
   - **`firmware/bios2.c` is 46,068 bytes**, with `display.c`, `input.c`,
     `mem.c` and `string.c`. The screen is §5.2's:
     - a title, the RAM, and one row per device, with what it holds or why it
       can't boot;
     - a 2-second countdown to the first device that can boot: Enter boots at
       once, Esc opens the menu;
     - in the menu, the arrow keys choose and Enter boots;
     - with nothing to boot, it waits in the menu. A disc put in changes the
       drive's generation counter, which re-checks the CD and selects it;
     - a boot that fails comes back to the menu and says why.

     Phase 2's halts with 1 and 2 in `A` are gone.
   - **Handing over:**
     - the timer stops, the screen is cleared to 0, and the character,
       key-edge and mouse queues are emptied;
     - a program is loaded with one `READ_DMA` and called;
     - a disk's block 0 is copied to `BOOT_LOAD_ADDR`, its channel written to
       `BOOT_CHANNEL`, and `BOOT_ENTRY` called.
   - **The memory map** gains `BOOT_BLOCK`, `BOOT_RECORD`, `BOOT_CODE`,
     `BOOT_SIGNATURE`, `BOOT_LOAD_ADDR` (`0x15818`), `BOOT_ENTRY` and
     `BOOT_CHANNEL`, for the C, the assembler and Python alike.
   - **Run end to end,** `start_emulator.py screen --headless --run` waited
     out the countdown and booted `screen` to `HALT` in 2.65 s.
   - **Tests:**
     - **`tests/test_bios2.py`, now 45 cases,** reads bios2's screen back as
       text, using `test_files.py`'s font reader, split out as `text_at()`.
       Disks and discs that can boot carry a stand-in boot sector of four
       instructions, which leaves its channel in `A`.
     - **What they cover:** the countdown screen; booting when the countdown
       ends, not before; Enter booting at once; a program up to exactly
       `PROGRAM_MAX_SIZE` arriving in one transfer; Esc, the arrows and Enter
       booting a hard disk, with block 0 and the channel where they belong; a
       disc counting down and booting; each device's reason, six ways; a disc
       put in while the menu waits; Enter on a device that can't boot; a short
       or refused transfer; and the program finding a blank screen, empty
       queues and a stopped timer.
     - **A screen caught mid-redraw doesn't count.** A test waits for two
       matching looks, or for the row bios2 draws last. The first version
       caught "Booting Progr" half written. Afterwards the 13 screen-reading
       tests passed five runs in a row.
     - **`test_loader.py`'s bios2 route** now waits out the countdown too.
   - **Nine deliberate breakages each failed the tests:**
     - not emptying the input queues;
     - not stopping the timer;
     - not clearing the screen at hand-over;
     - ignoring Esc;
     - not waiting for the countdown;
     - not checking the boot signature;
     - never writing the channel;
     - ignoring the CD's generation counter;
     - running a short transfer anyway.
   - **The full suite: 902 of 903 tests passed.** The one failure was the
     countdown-screen test before the fix above, in a run started earlier.
     Since the fix, all 45 cases in its file pass, and nothing else changed.
4. **The boot sector, `firmware/boot.asm`.** ***Done.***
   - **376 bytes, 47 instructions** (§6):
     - it loads the file named by the boot record, through the IO window, to
       `0x20000`, and jumps there;
     - when it can't, it returns to bios2, which draws its screen again and
       says "boot failed".
   - **`tools/pfs.py`:**
     - `PgfsImage.make_bootable(path, sector)` writes the record and the
       sector, after checking the file can boot;
     - `boot_record()` reads the record back, and `boot_sector()` assembles
       `firmware/boot.asm`;
     - `pfs.py boot PATH` is the command, and `pfs.py info` shows the record;
     - `_write_super()` updates block 0 in place instead of rebuilding it
       from zeros.
   - **By hand, end to end:**
     - `pfs.py mkfs`, `put build/screen.bin /boot.bin` and `boot /boot.bin`
       on a temporary image ("272 B from block 66"), and `fsck` clean;
     - then `start_emulator.py --disk` with that image and no program: the
       BIOS, bios2's countdown, the boot sector, and `screen` at `HALT`, in
       2.8 s.
   - **Tests:**
     - **`tests/test_boot.py`, 21 cases:**
       - full boots from the hard disk and the CD: 816 bytes, just over one
         window, an exact multiple of the window, 100 KB, and exactly
         `PROGRAM_MAX_SIZE`, with nothing written past the end;
       - a file further into the disk;
       - three bad records coming back to the menu with the frame redrawn;
       - `make_bootable` refusing a directory, a missing file, an empty file,
         a fragmented file, a file over 1 MB, and an oversized sector;
       - the boot bytes surviving later writes from `pfs.py` and from the
         guest's `fs.c`;
       - the command line.
     - **`test_pfs.py` (92) and `test_fs.py` (66) still pass,** among them
       the tests comparing the guest's images with `pfs.py`'s byte for byte.
   - **Nine deliberate breakages each failed the tests:**
     - in the boot sector: its size check, copying only what's left, its
       end-of-disk check, reading the channel instead of assuming 2, and
       restoring `F`;
     - bios2 not redrawing after a failed boot;
     - `pfs.py` rebuilding block 0 from zeros;
     - `make_bootable` without its contiguity check, or without its size
       check.
   - **The full suite passes: 924 tests**, the 903 from phase 3 and 21 new.
5. **The project file, `cc.py --project`, and `--cd`.** ***Done.***
   - **`compiler/project.py`:**
     - `read_project()` checks a project file and reports every mistake at
       once, each with its line;
     - `build_disc()` builds and writes the disc (§7).
   - **`cc.py --project FILE [-o OUT]`** builds to `build/<name>.img` unless
     `-o` says otherwise, and refuses sources, `-S` or `--org` alongside it.
   - **The launcher:** `--cd PATH` and `"cd"` in `config.json`, through
     `disc_drive()`. A disc outside `cd_root`, or one that isn't there, stops
     the launcher with the reason.
   - **The example:** `user/os/pigeon_compiler_init.txt`, the placeholder
     `user/os/installer.c`, and `user/os/readme.txt`. It builds a 341.5 KiB
     disc that boots through the launcher.
   - **Tests:**
     - **`tests/test_project.py`, 26 cases:**
       - a project reading as written, and a label taken from the name;
       - fourteen mistakes, each on its line, and several at once;
       - the disc holding the launcher's own builds, in order, with the boot
         record on `/install.bin`;
       - the same project building the same bytes;
       - a source swapped for an older one being built again;
       - a named boot sector, and one too big;
       - the disc booting its installer;
       - the example building, booting, and its installer listing four files;
       - the command line;
       - `--cd`.
     - **`test_config.py`** gains the `"cd"` cases.
   - **Nine deliberate breakages each failed the tests:**
     - losing the line numbers;
     - writing the installer last;
     - allowing the disc's own paths;
     - naming builds without their source;
     - dropping the file-versus-directory check;
     - allowing a path twice;
     - `--cd` not inserting the disc;
     - not checking the `"cd"` setting;
     - `--project` accepting sources.
   - **The full suite passes: 953 tests**, the 924 from phase 4 and 29 new.
6. **The installer, and a system for the hard disk.** ***Done.***
   - **`user/os/installer.c`,** 139,120 bytes, does §8.
   - **`[boot] system = ...`** builds a system onto the disc as `/boot.bin`,
     refusing one over 1 MB, and `/boot.bin` is reserved as `/install.bin`
     is. The example's system is `user/graph.c`, the graphing calculator,
     and its disc is now 473.0 KiB.
   - **Tests:**
     - **`tests/test_install.py`, 3 cases:**
       - installing the example disc onto a blank disk, restarting, and the
         calculator drawing from the hard disk;
       - Esc leaving a disk byte-identical;
       - a 256 K disk reported as a failed install.
     - **`test_project.py`** gains the system cases, and its example test now
       checks the installer's question.
   - **Found on the way: pigeon-cc's preprocessor expands macros inside
     string literals.** With `#define INSTALLER "/install.bin"`, the string
     `"INSTALLER"` became `""/install.bin""`, and the installer didn't
     compile. The installer's macros were renamed around it; the
     preprocessor is unchanged.
   - **Eight deliberate breakages each failed the tests:**
     - in the installer: not copying `/boot.bin` first, never writing
       block 0, taking the boot sector from the hard disk instead of the
       disc, ignoring Esc, copying itself, and not restarting;
     - in `cc.py --project`: leaving the system off the disc, and not
       reserving `/boot.bin`.
   - **One check only observed:** without the unmount before block 0 is
     written, all three install tests still pass, so `fs.c` did not write its
     cached block 0 back in this flow. The unmount stays: it is the order that
     can't go wrong.
   - **The full suite passes: 959 tests**, the 953 from phase 5 and 6 new.

| File | Change |
|---|---|
| `firmware/bios.asm` | loads bios2 from channel 7, else today's loader |
| `firmware/bios2.c`, `firmware/boot.asm` | new |
| `emulator/memory_map.py` | `CH_BIOS2`, `BIOS2_LOAD_ADDR`, `BOOT_LOAD_ADDR`, `BOOT_CHANNEL` |
| `emulator/devices/hdd.py` | `readonly` |
| `emulator/machine.py`, `cli.py`, `config.py`, `config.json` | the firmware device, building bios2, `--bios2`, `--cd`, the debugger's threshold |
| `lib/pigeon/fs.c`, `lib/pigeon/cd.c` | refuse channel 7 |
| `compiler/cc.py`, `compiler/project.py` | `--org`, `--project` |
| `user/os/pigeon_compiler_init.txt`, `user/os/installer.c` | new: the example project, and its installer (§8) |
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
