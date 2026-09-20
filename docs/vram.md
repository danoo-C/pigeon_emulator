# Video memory: modes, surfaces and the screen

> **Status: built,** `emulator/devices/vram.py` on IO channel 9, and
> `lib/pigeon/vram.h`. How it came to be is [gac/](gac/README.md); this is
> how to use it. Its partner, the accelerator that draws into it, is
> [gac.md](gac.md).

The machine has **16 MB of video memory** (`config.json`'s `"vram"`,
`--vram SIZE`; `0` or `null` for none). It holds the screen, in whatever
**mode** the screen is in, and any other **surfaces** a program asks for, such
as back buffers and sprites.

Most programs never touch this directly. `<pigeon/display.h>` asks the machine
how big the screen is, draws through the accelerator, and gets its back buffer
from here. Read on if you want a mode of your own, or pixels in video memory
that `display.h` does not manage.

---

## 1. Modes

| mode | console | bytes |
|---|---|---|
| **192 × 108**, at power-on | 32 × 12 | 82,944 |
| 320 × 180 | 53 × 20 | 230,400 |
| 640 × 360 | 106 × 40 | 921,600 |
| 854 × 480 | 142 × 53 | 1,639,680 |
| 1280 × 720 | 213 × 80 | 3,686,400 |

The list is `config.json`'s `"display_modes"`, and `"display_mode"` (or
`--mode WxH`) is the one the machine powers on in. Nothing larger than
1280 × 720 can be offered, because that is the size programs size their
arrays for (`DISPLAY_MAX_W`, `DISPLAY_MAX_H`).

**Who changes the mode:**

- **You**, at the prompt: `setmode 640 360` ([shell.md](shell.md) §9), or the
  **Mode** list in the browser's toolbar or pygame's, which the console takes
  at the next prompt. That is the console's mode, and it stays.
- **A program**, for itself: `disp_setmode(w, h)`. When it ends, the kernel
  puts back the mode it started with, however it ended. So a game that crashes
  does not leave you in its mode, and a `# graphics 640x360` script keeps its
  mode across the `graphics` lines it runs ([graphics.md](graphics.md) §4).
- **Nobody else.** The window only asks. It never switches a program's screen
  under it.

At **192 × 108 the screen is in RAM** at `DISPLAY_START`, as it always was,
where the BIOS, bios2 and every `.asm` program draw. Every other mode's screen
is in video memory.

## 2. The aperture: a pixel is one store

Video memory is **mapped into the address space, above RAM**. On the 128 MB
machine it starts at `0x08000000`, and on a 1 GB one at `0x40000000`. A pixel
there is an ordinary `MWW`, one instruction, as in RAM. The bus carries
only control: modes, surfaces, flips.

**Never write the aperture's address down.** It moves with the machine's RAM
size, and a program built on a 128 MB machine would draw into the middle of
its heap on a 1 GB one. The store succeeds silently. Ask `vram_aperture()`.
`display.h` does, and it hands you `DISP_BASE`.

Past the end of video memory, the aperture reads 0 and drops writes. Running
code from it is a fetch fault.

## 3. Surfaces

A surface is a piece of video memory: a handle, an offset, a width and a
height. The pitch is always width × 4. **Handle 0 is the screen** of the current
mode.

```c
unsigned offset;
unsigned back = vram_alloc(640, 360, &offset);   /* 0 if video memory is full */
unsigned *pixels = (unsigned *)(vram_aperture() + offset);
...
vram_scanout(back);                              /* the page flip */
vram_free(back);
```

- **A surface never moves.** A pointer into it stays good until you free it.
- **The kernel frees a program's surfaces when it ends,** however it ends, the
  way it closes its files. The screen is nobody's and is never freed.
- **`vram_scanout(handle)` is the page flip.** The surface must be the
  screen's size. `vram_scanout_ram(address)` shows the screen out of RAM
  again.

## 4. The device: channel 9

Arguments are words in the data window, sent with R/W 0, and the reply comes
back in the window.

| cmd | name | ADDRESS | window in | reply |
|---|---|---|---|---|
| 0 | `NOP` | — | — | 0 |
| 1 | `INFO` | — | — | magic `"PGVR"`, size, aperture, generation |
| 2 | `GET_MODE` | — | — | w, h, pitch, format (1 = B,G,R,A), the screen's offset |
| 3 | `SET_MODE` | — | w, h | 1 / 0, then w, h, pitch, offset as they are now |
| 4 | `MODE_COUNT` | — | — | how many modes |
| 5 | `MODE_AT` | index | — | w, h, or 0, 0 past the end |
| 6 | `PREFERRED` | — | — | w, h, a count of requests: what the window asked for |
| 7 | `ALLOC` | — | w, h | handle, offset, pitch, or 0s when full |
| 8 | `FREE` | handle | — | 1 / 0 |
| 9 | `SCANOUT` | handle | — | 1 / 0 |
| 10 | `SCANOUT_RAM` | address | — | 1 / 0 |
| 11 | `UPLOAD` | handle | ram, offset, n | n, or `0xFFFFFFFF` |
| 12 | `DOWNLOAD` | handle | ram, offset, n | n, or `0xFFFFFFFF` |
| 13 | `OWNER` | n | — | 1: surfaces allocated from now on are n's |
| 14 | `FREE_OWNED` | n | — | how many were freed: n's and deeper, here and in the GAC |

`generation` counts mode changes, so a program can tell that the screen
changed shape under a pointer it holds. `INFO` answers its magic first, so a
program on a machine without video memory (the channel answers `0xFFFFFFFF`)
can tell. `OWNER` and `FREE_OWNED` are the kernel's.

`CH_DISPLAY` (channel 5) still works as it did: its `INFO` answers the current
mode, and `SET_BASE`/`GET_BASE` take and give aperture addresses as well as RAM
ones.

## 5. `<pigeon/vram.h>`

`vram_present`, `vram_size`, `vram_aperture`, `vram_generation`, `vram_mode`,
`vram_set_mode`, `vram_mode_count`, `vram_mode_at`, `vram_preferred`,
`vram_alloc`, `vram_free`, `vram_scanout`, `vram_scanout_ram`, and the
kernel's `vram_owner` and `vram_free_owned`. One function a command, in
`lib/pigeon/vram.c`. Every one answers 0, or does nothing, on a machine
without video memory.

## 6. Where things are

| | |
|---|---|
| `emulator/ram.py` | the aperture |
| `emulator/devices/vram.py` | the device |
| `emulator/devices/display_io.py` | what the screen is read from, and `/frame` |
| `lib/pigeon/vram.h`, `vram.c` | the library |
| `user/os/bin/setmode.c` | `setmode` |
| `user/os/kernel.c` | owners, the mode put back, `setmode`'s system call (slot 24) |
| `tests/test_vram.py`, `test_vram_aperture.py` | the device and the aperture |
