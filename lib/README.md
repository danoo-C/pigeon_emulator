# The C libraries

Three headers, compiled by `pigeon-cc` and covered by execution tests in
`tests/test_libs.py` — every test compiles the C and *runs* it.

| Header | What it gives you |
|---|---|
| `<pigeon/mem.h>` | `memcpy` `memmove` `memset` `memcmp`, `malloc` `calloc` `free`, `heap_used` |
| `<pigeon/display.h>` | pixels, lines, rects, circles, 4×6 text — all clipped |
| `<pigeon/input.h>` | mouse position/buttons/edges, keyboard characters, key edges, held-key state |

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
about four instructions per byte and the framebuffer is 40,000 bytes.

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
