# GAC and VRAM: a graphics accelerator, its own video memory, and a screen that resizes

> **Status: Phases 1 and 2 built, 2026-09-18; Phase 3 is next. Every question is decided
> ([decisions.md](decisions.md)).** You answered the design questions on
> 2026-09-17 and the one follow-up (§11, whether `--ram` ships with this) on
> 2026-09-18: it ships with Phase 1, defaulting to 128 MB.

This folder is the plan that used to be one 1,100-line file,
`docs/gac_plan.md`, split so each phase can be read, built and ticked off on
its own:

| file | what is in it |
|---|---|
| **README.md** (this) | what you asked for, where things stood, the goal, the phases, what is not in the plan |
| [design.md](design.md) | the numbers (§4), the design (§5), the file-by-file list (§6), the risks (§8) |
| [decisions.md](decisions.md) | your answers, Q1–Q15 (§10) and the follow-up (§11) |
| `phaseN_*.md` | one phase each: its steps, its tests, what "done" means, and — once built — what was built |

Section numbers (§4, §5.3.1, …) are kept from the original file, so every
cross-reference still means what it did.

---

## Phases

Each phase leaves the machine working and its tests passing. Phases 1–4 are
the emulator and touch no guest code; Phases 5–7 are guest code and touch no
emulator. That split is deliberate: if the second half turns out to be more
than you want to do at once, the first half is still a machine with a
graphics accelerator and separate video memory, running the existing
192 × 108 world.

| phase | what | side | status |
|---|---|---|---|
| [1](phase1_aperture.md) | **The aperture and `--ram`.** VRAM mapped above RAM, nothing behind it yet | emulator | **built** 2026-09-18 |
| [2](phase2_vram.md) | **`CH_VRAM`.** Modes, surfaces, the scanout selector, upload/download | emulator | **built** 2026-09-18 |
| [3](plans/phase3_gac.md) | **`CH_GAC`.** Every primitive, `BATCH`, text, then blending | emulator | **being built** |
| [4](phase4_frontends.md) | **The front ends follow the mode.** Browser and pygame resize | emulator | planned |
| [5](phase5_display_lib.md) | **`<pigeon/display.h>` on runtime geometry** | guest | planned |
| [6](phase6_programs_console.md) | **The five fixed-size programs, then the kernel console** | guest | planned |
| [7](phase7_setmode.md) | **`setmode`, and `# graphics` scripts that pick a mode** | guest | planned |
| [8](phase8_damage.md) | **Damage rectangles and bandwidth** | both | planned |

---

## 1. What you asked for

> could you please design a graphics accelerator and separate vram for my
> emulator? the end goal is to have dynamically resizable display. […] also
> think of things like performance if the vram ends up on the IO controller.
> so what i really want is 2 IO devices GAC (graphics accelerator) and VRAM —
> directly access the video ram and framebuffer.

So: two new devices on the bus, **`CH_VRAM`** and **`CH_GAC`**; the
framebuffer moves out of the 128 MB address map and into video memory the
VRAM device owns; the resolution stops being a constant in
`emulator/memory_map.py` and becomes something the machine is *told* at run
time; and — the part your performance question is really about — the guest
still gets to **write a pixel with one store instruction**, because video
memory is *mapped*, not tunnelled through the bus.

That last point is the whole design. §4 has the numbers behind it.

---

## 2. Where things stand

- **The screen is a fixed slab in the middle of the memory map.**
  `DISPLAY_START = 0x1418`, `192 × 108 × 4 = 82,944` bytes, wedged between
  the IO window and the boot sector's landing pad *(checked:
  `emulator/memory_map.py`)*.
- **It cannot grow.** After the framebuffer come the boot sector's 512 bytes,
  its channel word, and the 128-byte system-call table, and all of it must end
  before `PROGRAM_LOAD_ADDR = 0x20000` *(checked: the three `raise
  RuntimeError` overlap guards in `memory_map.py`)*. That leaves **125,284
  bytes for the framebuffer, ever**:

  | mode | bytes | fits? |
  |---|---|---|
  | 192 × 108 (today) | 82,944 | yes |
  | 224 × 126 | 112,896 | just |
  | 256 × 144 | 147,456 | **no** |
  | 640 × 480 | 1,228,800 | no, by 10× |
  | 1280 × 720 | 3,686,400 | no, by 29× |

  **The screen cannot reach 256 × 144 without moving `PROGRAM_LOAD_ADDR`** —
  and that address is baked into every built `.bin`, into `firmware/bios.asm`,
  and into the boot record's contract *(checked: `bios.asm`, `os_cd.md`)*.
  This is the real reason VRAM has to come off the map, and it is worth
  saying out loud: **separate VRAM is not a nicety here, it is the only way
  the screen gets bigger.**
- **There is already a display device on the bus,** `CH_DISPLAY = 5`, with
  `INFO`, `SET_BASE`, `GET_BASE`, `FILL` and `COPY` *(checked:
  `emulator/devices/display_io.py`)*. It exists because profiling the 3D cube
  found **87% of all guest instructions** inside two library functions doing a
  word at a time what the host can do with a slice *(checked: that module's
  docstring)*. The GAC is that idea, finished.
- **`SET_BASE` is already a page flip,** and the scanout base is already a
  variable rather than `DISPLAY_START` *(checked: `DisplayIO.scanout_base`,
  `disp_present`)*. The guest can already point the screen at heap memory. So
  "where the screen is" is *already* not a constant — only "how big it is" is.
- **The guest writes pixels with plain stores.** `disp_set` is
  `*row_ptr(x, y) = c`, `img.c` does `memcpy((void *)DISPLAY_START, …)`,
  `graphics.c` does the same *(checked: `lib/pigeon/display.c`,
  `user/os/bin/img.c`, `user/os/bin/graphics.c`)*. Any design that takes that
  away is a disaster; §4 says by how much.
- **`DISP_W` and `DISP_H` are compile-time constants** predefined by the
  compiler out of `memory_map.symbols()` *(checked: `compiler/cc.py:40`,
  `assembler/assembler.py:46`)*. **22 source files use them** *(checked:
  `grep` over `user/`, `lib/`, `firmware/`)*, and five of those use them to
  size arrays — `char row[COLS + 1]` where `COLS` is `DISP_W / CELL`
  *(checked: `user/files.c:112`, `user/disc.c:81`, `user/os/installer.c`,
  `firmware/bios2.c:51`, and the kernel's `con_grid[384]`)*. Those five are
  the whole migration cost, and §6 lists them.
- **The kernel's console is 32 × 12, hardcoded** *(checked: `kernel.c:67`)*,
  with `con_grid[384]` and `back_grid[3200]` sized to match.
- **The front ends already ask the machine how big the screen is.**
  `index.html` has *"No default geometry on purpose. /info is the only source
  of it"* and sets `off.width = W` from it; `display.py` does the same in
  `_wait_for_server` *(checked: `display/index.html:371-399`,
  `display/display.py:216-219`)*. They each ask **once**, at startup. Making
  them ask again is a small change to code that is already shaped for it.
- **The CD drive already has the pattern for "the hardware changed under
  you":** a `generation` counter in its `MEDIA` reply, which the guest
  compares to notice a disc swap *(checked: `README.md`'s channel table,
  `emulator/devices/cd.py`)*. A mode change wants exactly that.
- **The bus has 8 channels used, and nothing stops more** *(checked:
  `memory_map.py`'s `CH_*`, `machine.py`'s registration loop)*. Channel 9 and
  channel 10 are free.
- **RAM is one `bytearray` with a power-of-two wrap mask** *(checked:
  `emulator/ram.py`)*, and every word access already pays one range check for
  the IO window. Adding a second check is what §5.1 costs.
- **926 test functions** *(checked: `grep -c "def test_" tests/*.py`)*, 17 of
  them in `test_display.py`, and a good number elsewhere assert against a
  `DISPLAY_SIZE`-shaped frame *(checked: `test_bios2.py:435`,
  `test_edit.py:73`)*. §8 is about not breaking them.

---

## 3. The goal

```sh
2:/> setmode 640 360
2:/> graphics -clear 0xFF101018 -disc 320 180 60 0xFFFF0000 -wait
```

and, in the browser, the canvas becoming 640 × 360 without restarting
anything; and, in `config.json`:

```json
  "vram": "16M",
  "display_mode": [192, 108],
  "display_modes": [[192,108], [320,180], [640,360], [1280,720]]
```

and, in C:

```c
disp_init();                       /* asks the hardware what the screen is */
disp_clear(BLACK);                 /* one bus command, whatever the size    */
disp_rect(0, 0, disp_w, 20, BAR);  /* DISP_W still works — it is now a var  */
*row_ptr(x, y) = c;                /* still ONE store. this is the point.   */
```

Three things have to be true at once, and the design exists to keep all three:

1. **The framebuffer is not in the 128 MB map,** so its size is free.
2. **The framebuffer is still directly addressable,** so a pixel is a store.
3. **Bulk work goes to the host,** so a 1280 × 720 clear costs a bus command
   rather than 3.7 million guest instructions.

---

## 9. Not in this plan

- **A blit honouring per-pixel source alpha** — the one piece of blending
  left out of v1, because neither the `translate` table nor the SWAR multiply
  applies when the alpha differs per pixel and the naive loop is 0.28 s a
  screen (§4.5). The flag is reserved and the command refuses it. If you want
  soft-edged sprites later, that is its own small plan, and the honest
  options are a C-speed trick nobody has found yet, an optional `numpy` fast
  path, or accepting the cost for small sprites only.
- **A command queue, fences, or asynchrony.** The bus is synchronous and
  §4.3 says the host's work is a fraction of a millisecond; a queue would buy
  nothing and cost a lot of correctness.
- **3D, textures, shaders.** `user/cube.c` stays software and gets faster for
  free through `GAC_LINE`.
- **A window manager, or more than one thing on the screen at a time.** The
  surfaces are there for it; nothing uses them that way yet.
- **A second display, or a display on a second machine.** The channel numbers
  are the only thing that would need adding.
- **Fullscreen and aspect-ratio letterboxing in the front ends.**
- **Letting the guest use more than 128 MB of RAM.** `--ram 1G` ships in
  Phase 1, and the machine really has 1 GB, but `STACK_TOP`,
  `BIOS2_LOAD_ADDR` and `mem.c`'s heap limit are fixed at 128 MB, so the
  guest does not use the rest yet
  ([phase1_aperture.md](phase1_aperture.md#what---ram-1g-does-and-does-not-give-you-yet)).
  That is a small plan of its own: a way for the guest to ask the RAM size.
- **Removing `CH_DISPLAY`.** It stays, and stays supported, until nothing
  uses it; then a separate, tiny plan.

