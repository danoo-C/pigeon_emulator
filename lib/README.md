# The C libraries

Ten headers, compiled by `pigeon-cc` and covered by execution tests in
`tests/test_libs.py`, `tests/test_fs.py`, `tests/test_cdlib.py`, `tests/test_stdio.py`,
`tests/test_compiler.py` and `tests/test_kernel.py`. Every test compiles the C and
*runs* it.

| Header | What it gives you |
|---|---|
| `<pigeon/mem.h>` | `memcpy` `memmove` `memset` `memcmp`, `malloc` `calloc` `free`, `heap_used` |
| `<pigeon/string.h>` | `strlen` `strcmp` `strlcpy` `strlcat` `strchr` …, numbers as text (`utoa` `itoa` `strtou` `atoi`), `isdigit` and friends |
| `<pigeon/stdio.h>` | `snprintf` `vsnprintf`, and through the kernel `printf` `vprintf` `puts` `putchar` |
| `<pigeon/stdarg.h>` | `va_list` `va_start` `va_arg` `va_copy` `va_end`, for a function of your own that takes `...` |
| `<pigeon/fs.h>` | files and directories on the HDD channels: `fs_open`/`read`/`write`/`seek`, `fs_mkdir`/`readdir`/`rename`, `fs_load`/`fs_save`, a current directory |
| `<pigeon/cd.h>` | the CD drive: `cd_info`, `cd_read`, `cd_has_fs`/`cd_label` for a disc that carries a filesystem, `cd_save` to copy a disc onto the current volume, and `cd_eject` |
| `<pigeon/display.h>` | pixels, lines, rects, circles, 4×6 text — all clipped |
| `<pigeon/input.h>` | mouse position/buttons/edges, keyboard characters, key edges, held-key state |
| `<pigeon/math.h>` | fixed point, trig, roots, random, 3D vectors |
| `<pigeon/sys.h>` | for a program the kernel runs: `write` `read` `open` `close`, `opendir` `readdir` `stat`, `mkdir` `rmdir` `remove` `rename`, `chdir` `getcwd`, `exec` `exit` `getkey`, `print`, `sys_strerror` |

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

The heap stops at `__heap_limit`, a word the compiler emits after
`__heap_ptr`, and `malloc` returns `NULL` past it. The kernel writes it for
each program it runs, and for itself ([docs/kernel.md](../docs/kernel.md)
Q7). Left at 0, the limit is a megabyte below the top of RAM, where the
hardware stack grows down.

`memcpy` and `memset` move a word at a time while both pointers are aligned,
then finish byte by byte. That is not a micro-optimisation: the byte loop is
about four instructions per byte and the framebuffer is 82,944 bytes.
(`disp_clear` no longer pays that: it is a hardware fill on CH_DISPLAY.)

## string

Strings, numbers as text, and character classes. The names are the
standard C ones, cut down to what this machine needs. `printf` is in
`<pigeon/stdio.h>`, below; without it, to show a number you write it into a
buffer first:

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

## sys

The system calls, for a program the kernel runs (`user/os/kernel.c`,
[docs/kernel.md](../docs/kernel.md) §10). Each function calls through the
kernel's table at `SYSCALL_TABLE`, so a program carries a few lines instead
of a console and `fs.c` of its own, and every program shares the kernel's
one screen of text and one current directory.

```c
char line[256];
print("name? ");
read(STDIN, line, 255);             /* a typed line, '\n' included */
if (exec("/bin/ls.bin", argc, argv) == ENDED_BREAK) print("stopped\n");
```

**File descriptors 0, 1 and 2 are the console;** `open` gives 3 and up, and
`read`, `write` and `close` take either. **Errors are below 0:** -1 to -19
are `fs.h`'s codes passed on, `exec` adds `E_NOTPROG`, `E_NOMEM` and
`E_DEPTH`, and `ENDED_*` for a program that didn't return: a fault, or
Ctrl+C. `sys_strerror` names them all. **The slot numbers and codes are in
`<pigeon/syscall.h>`,** which has no `.c`, so the kernel includes it without
the stubs. **Only a program the kernel started can call them:** without a
kernel the table is empty.

**`mkdir`, `rmdir`, `remove` and `rename` pass straight to `fs.c`,** so they
refuse what it refuses, with its codes: a directory that isn't empty, a path
that doesn't exist, or anything on the read-only CD.

**Text written to the console can carry escape codes,** as on a terminal:
`ESC [ 30 m` to `ESC [ 37 m` for an ink, `ESC [ 7 m` for inverse, `ESC [ 0 m`
back to normal, `ESC [ 2 J` to clear, `ESC [ r ; c H` to move the cursor, and
`ESC [ K` to clear to the end of the line ([docs/kernel.md](../docs/kernel.md)
§12). The kernel puts the ink back to normal after each program.

## stdio

`printf` and its friends, on the compiler's variadic functions
([compiler/design/03-abi.md](../compiler/design/03-abi.md#variadic-functions)).

```c
printf("%s: %d files\n", dir, count);
n = snprintf(line, sizeof(line), "%-12s %5u", name, size);
```

**`snprintf` and `vsnprintf` work in any program.** `printf`, `vprintf`,
`puts` and `putchar` write to `STDOUT` through the kernel, so only a program
the kernel runs prints with them. **Without a kernel they print nothing and
return -1:** they find `write`'s slot in the system-call table holding 0, and
never call through it to address 0. A program with no kernel formats with
`snprintf` and draws the text where it likes, with `disp_text`.

**`snprintf` returns the length it wanted,** as C's does, and always
terminates what it kept, so a result `>= size` means the text was cut short.

**Conversions:** `%d %i %u %x %X %o %c %s %p %%`, the flags `-` and `0`, a
width, and for `%s` a precision, as in `%.5s`. An unknown conversion is
printed as written. There is no floating point. **A call takes at most 8
arguments after the format,** the compiler's limit; a ninth is a compile
error.

**`printf` needs no heap:** it formats into 64 bytes on its frame and writes
them each time they fill.

**Each program carries its own copy,** with `string.c`, since there is no
shared library: `mkdir.bin`, which uses `printf`, is 26,392 bytes, and
`clear.bin`, which only calls `print`, is 5,872.

## stdarg

For a function of your own that takes `...`. Macros, with no `.c`:

```c
int sum(int count, ...) {
    va_list ap;
    int total = 0;
    va_start(ap, count);
    while (count > 0) {
        total = total + va_arg(ap, int);
        count--;
    }
    va_end(ap);
    return total;
}
```

**Every extra argument is one word:** an integer, a `char`, a pointer or a
function. A struct is refused at the call. **Nothing counts them for you:**
as in C, a count or a format says how many came, and reading past them gives
whatever the slot held. **A `va_list` is a plain pointer,** so it can be
passed on, as `printf` passes one to `vprintf`, and `va_copy` is an
assignment.
