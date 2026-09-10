# The C libraries

Four headers, compiled by `pigeon-cc` and covered by execution tests in
`tests/test_libs.py` — every test compiles the C and *runs* it.

| Header | What it gives you |
|---|---|
| `<pigeon/mem.h>` | `memcpy` `memmove` `memset` `memcmp`, `malloc` `calloc` `free`, `heap_used` |
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
