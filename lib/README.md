# The C libraries

Seven headers, compiled by `pigeon-cc` and covered by execution tests in
`tests/test_libs.py`, `tests/test_fs.py` and `tests/test_cdlib.py`. Every test compiles the C and
*runs* it.

| Header | What it gives you |
|---|---|
| `<pigeon/mem.h>` | `memcpy` `memmove` `memset` `memcmp`, `malloc` `calloc` `free`, `heap_used` |
| `<pigeon/string.h>` | `strlen` `strcmp` `strlcpy` `strlcat` `strchr` …, numbers as text (`utoa` `itoa` `strtou` `atoi`), `isdigit` and friends |
| `<pigeon/fs.h>` | files and directories on the HDD channels: `fs_open`/`read`/`write`/`seek`, `fs_mkdir`/`readdir`/`rename`, `fs_load`/`fs_save`, a current directory |
| `<pigeon/cd.h>` | the CD drive: `cd_info`, `cd_read`, `cd_has_fs`/`cd_label` for a disc that carries a filesystem, `cd_save` to copy a disc onto the current volume, and `cd_eject` |
| `<pigeon/display.h>` | pixels, lines, rects, circles, 4×6 text — all clipped |
| `<pigeon/input.h>` | mouse position/buttons/edges, keyboard characters, key edges, held-key state |
| `<pigeon/math.h>` | fixed point, trig, roots, random, 3D vectors |

There is no linker: units are compiled together, so pass the library
sources on the command line.

```bash
python3 compiler/cc.py game.c lib/pigeon/display.c lib/pigeon/input.c -o build/game.bin
python3 start_emulator.py build/game.bin --run
```

## mem

A bump allocator with a first-fit free list. Blocks carry an 8-byte header,
so `free(p)` finds it at `p - 8` without searching. **No coalescing and no
splitting** — freeing large blocks then allocating small ones fragments.
Adequate here because the other two libraries allocate nothing at runtime.

`memcpy` and `memset` move a word at a time while both pointers are aligned,
then finish byte by byte. That is not a micro-optimisation: the byte loop is
about four instructions per byte and the framebuffer is 82,944 bytes.
(`disp_clear` no longer pays that: it is a hardware fill on CH_DISPLAY.)

## string

Strings, numbers as text, and character classes. The names are the
standard C ones, cut down to what this machine needs. There is no printf, so
to show a number you write it into a buffer first:

```c
char line[32];
unsigned n = strlcpy(line, "score ", sizeof(line));
itoa(score, line + n);
disp_text(2, 2, line, WHITE);
```

**Copies are bounded.** `strlcpy` and `strlcat` always leave the result
terminated. They return the length they *tried* to make, so a result
`>= size` means the copy was cut short. There is no `strcat` and no
`strncpy`. With no memory protection, an overrun doesn't fault; it
overwrites whatever comes next.

**Bytes compare as unsigned.** `char` is signed here, so compared as `char`,
`0xE9` would sort below `'a'`. `strtou` saturates at `0xFFFFFFFF`, as
`strtoul` does, while `atoi` wraps.

**It stands alone.** It uses no heap and doesn't need `mem.c`, so a program
that only formats numbers doesn't pull in an allocator.

## display

The screen is memory, so this is stores and pointer arithmetic — no device,
no driver. Colour words are **`0xAARRGGBB`**, and alpha is **not blended**:
the emulator hands it straight to the canvas, so `AA = 0x00` is invisible.

**Everything clips.** Coordinates are `unsigned`, so one `>=` catches both
ends — a negative value wraps to a huge one. That is also one comparison
instead of two, and it avoids the signed-compare sequence. `user/checkerboard.asm`
is what happens without this: it runs its pixel index past the end of the
framebuffer and overwrites its own code until the CPU faults.

`disp_line` and `disp_circle` take signed `int` because Bresenham needs
negative deltas — the only functions here that pay for signed comparison.

## input

Two buffers, because guest code asks two different questions:

```c
for (;;) {
    int k;                                   /* FIFO: nothing is missed  */
    while ((k = key_read()) >= 0) type_character(k);
    unsigned e;
    while ((e = mouse_event()) != 0)
        click(ME_BUTTON(e), ME_PRESSED(e), mouse_x(), mouse_y());

    if (key_down(KEY_LEFT))  x--;            /* real-time: what is held  */
    if (key_down(KEY_RIGHT)) x++;
}
```

Drain the FIFOs in a `while`, not once per frame: both hold 256 entries and
drop the **oldest** when full, so a slow frame loses the earliest input.

Polling `key_read()` to steer something gives you the keyboard's auto-repeat
rate, not smooth motion. Sampling the bitmap for typing drops any key pressed
and released inside one frame. Use the right one.

Keycodes are one byte: printable ASCII passes through, named keys live in
`0x80`–`0x9F`. Front ends translate into that space; the device rejects
anything wider rather than truncating it.

## math

Fixed point is **Q8** — one unit is 256 — and that is forced, not chosen.
`MUL` truncates to 32 bits, so a Qn multiply needs both raw operands under
about 46,340: Q8 leaves ±181 units of range, Q16 leaves ±0.7.

The library exists because **three operations are wrong on negatives**, and
all three appear throughout 3D maths:

```
-256 >> 8    gives 16777215     SHR is a LOGICAL shift
-256 / 256   gives 16777215     DIV is unsigned
-256 * 2     gives -512         MUL is fine
```

Only the shift is genuinely broken — the machine multiplies the low 32 bits
in two's complement, which is what C wants. So `fmul` is one sign-safe
shift, not the two sign-strips `user/cube.c` used to carry.

Angles are **0..255 for a full turn**, so wrapping is one `AND` rather than
the DIV+MUL+SUB that `% 360` compiles to. `isin`/`icos` return Q8 in
[-256, 256]; `iatan2` inverts them exactly, with zero error across all 256
directions.

**Every divide guards against zero** — not for tidiness, but because the
emulator raises a Python exception on `DIV` by zero, so an unguarded divide
takes the whole machine down rather than faulting the guest.

Vectors are passed by pointer, never by value: six registers and
memory-passed arguments make copying twelve bytes per call pure waste. Every
`v3_*` tolerates `out == in`, so chaining works:

```c
v3_rotate_y(&v, &v, yaw);
v3_rotate_x(&v, &v, pitch);
v3_project(&v, DIST, CX, CY, &sx, &sy);
```

That is exactly what `user/cube.c` does, and why the aliasing rule is a
documented guarantee rather than an accident.

## fs

PigeonFS keeps files and directories on the HDD channels, in a FAT-style
format that `tools/pfs.py` can also read and write from the host. A disk is
named by its IO channel. You mount `CH_HDD`, and a path can pick a disk
explicitly with a prefix, as in `2:/saves/a`.

```c
if (fs_mount(CH_HDD) == FS_ENOFS) {      /* a blank disk: format it once */
    fs_format(CH_HDD, "PIGEON", 0);
    fs_mount(CH_HDD);
}
fs_save("/saves/score", &score, 4);
```

**Every call writes through before it returns**, so stopping the emulator
between two calls loses nothing. **A disk is only formatted on purpose:**
`fs_format` refuses anything that isn't blank unless you force it. The
design, the limits and every error code are in
[docs/filesystem.md](../docs/filesystem.md).

It's the biggest library here: a program that includes it is about 99 KB.
Blocks move by DMA, straight between the disk and RAM, at 84 instructions a
transfer of any length, so a 100 KB file loads in about 30 ms. On a disk
without DMA, or into a buffer the disk will not reach, it falls back to the IO
window, where one block costs about 4,350 instructions.

`user/files.c` is the worked example — a file browser that walks
directories, reads text files and writes notes, and reports every refusal
through `fs_strerror()`. Run it with `python3 start_emulator.py files --run`.

## cd

The CD drive on channel 6: a removable, read-only disc that the display front
ends put in and take out while the machine runs. A disc is raw bytes. If it
happens to carry a PigeonFS image it is also a read-only volume, and
`fs_mount(CH_CD)` mounts it with the ordinary `<pigeon/fs.h>`.

```c
cd_info_t disc;
if (cd_info(CH_CD, &disc) == CD_OK) {
    fs_mount(CH_HDD);
    cd_save(CH_CD, NULL);            /* the whole disc, under its own name */
}
```

**Including it compiles `fs.c` too** — about 99 KB — because `cd_save` writes
through the filesystem. **Its errors are -101 and down**, so one can never be
mistaken for an `FS_*` code, and `cd_strerror` names both kinds.
**`cd_present` and `cd_has_fs` answer 1 or 0, never an error**, so
`if (cd_present(CH_CD))` is safe; `cd_info` says why an answer is 0.
**A disc can be swapped while you are reading it**: compare `cd_generation`
before and after. `cd_save` does, removes the mixed copy, and returns
`CD_ECHANGED`. **`cd_eject` takes the disc out from inside the machine**,
unmounting it first if it was mounted, and refuses with `FS_EBUSY` while
files on it are still open. The design is in
[docs/cd-drive.md](../docs/cd-drive.md).
