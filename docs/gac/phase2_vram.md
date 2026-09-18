# Phase 2: `CH_VRAM`, video memory as a device

> Part of [the GAC plan](README.md). **Status: built, 2026-09-18
> ([As built](#as-built)).** Needs Phase 1.
> Design: [design.md §5.2](design.md#52-ch_vram--9--video-memory) and
> [§5.4](design.md#54-scanout-one-selector-two-sources).
> Decisions: Q3, Q4, Q6, Q12, Q13, Q14 in [decisions.md](decisions.md).

**The goal:** the buffer Phase 1 mapped gets an owner. The VRAM device on
channel 9 knows the mode, hands out surfaces in video memory, and decides
what the screen shows: a VRAM surface, or system RAM as today. **The machine
still powers on at 192 × 108, scanning out of `DISPLAY_START`, byte for
byte as it does now.**

---

## Steps

### 2.1 Constants

`CH_VRAM = 9` (Q13). `DISPLAY_MAX_W = 1280` and `DISPLAY_MAX_H = 720`, the
largest offered mode (Q12). The C compiler and the assembler pick them up
automatically, because `memory_map.symbols()` exports every uppercase int.
Nothing uses them until Phase 5.

### 2.2 The device, `emulator/devices/vram.py`: `NOP` and `INFO`

`INFO` answers `magic, vram_size, aperture_base, generation`, **magic
first**, so a program can tell "no VRAM here" (`0xFFFFFFFF`) from a stale
data window (§5.2). The device holds the `RAM`'s `vram` buffer. It does not
own a second copy.

### 2.3 Modes: `GET_MODE`, `SET_MODE`, `MODE_COUNT`, `MODE_AT`, `PREFERRED`

- The offered modes come from the config (2.7). `SET_MODE` refuses anything
  not on the list, or anything that does not fit in VRAM.
- A mode change allocates surface 0 at offset 0, clears it to black, bumps
  `generation`, and points the scanout at it.
- `PREFERRED` answers whatever the host last posted, which is nothing until
  Phase 4. It stores; it never switches (Q1).

### 2.4 Surfaces: `ALLOC` and `FREE`

A bump allocator with a free list that **never moves a surface**, because a
guest may be holding a raw aperture pointer into one. `FREE` of the last
surface rewinds the bump pointer. Handle 0 is the scanout surface of the
current mode, and it cannot be freed.

### 2.5 The scanout selector, with `CH_DISPLAY` rewired onto it

- The scanout becomes `(source, target)`: a VRAM handle, or a RAM address.
  `SCANOUT` is the page flip, and `SCANOUT_RAM` points the screen back at
  system RAM.
- **`CH_DISPLAY` keeps its five commands exactly (Q4).** `SET_BASE` is
  `SCANOUT_RAM`, `GET_BASE` reads it back, and `FILL`/`COPY` stay on system
  RAM. Its `INFO` answers the **current mode's** `w, h, size`, so
  `disp_probe`'s size check makes an old `.bin` fall back to software
  drawing instead of writing past the screen (§5.4).
- `DisplayIO`'s snapshot, what `/frame` serves, reads from VRAM when VRAM is
  the source. `/info` still answers 192 × 108, because the front ends learn
  to follow a mode change in Phase 4.

### 2.6 `UPLOAD` and `DOWNLOAD`

DMA between system RAM and a surface, with `[ram_addr, vram_off, count]`
in the window. It is the same shape as the HDD's `READ_DMA`/`WRITE_DMA`, so
there is one DMA idiom on the bus. Out-of-range requests answer
`0xFFFFFFFF` and move nothing.

### 2.7 Config, CLI, machine

`"display_mode": [192, 108]` and
`"display_modes": [[192,108],[320,180],[640,360],[854,480],[1280,720]]`
(Q12), plus `--mode WxH`. `Machine` constructs and registers the device.
`dump_ram_to` writes VRAM beside the RAM dump. **The mode does not persist
across a reboot (Q14).**

### 2.8 Tests: `tests/test_vram.py`

Every command, including the refusals. Allocation that never moves a
surface. Generation bumps. The scanout flips between VRAM and RAM.
`CH_DISPLAY` reports the live mode.

**Done when:** `test_display.py`'s 17 tests pass **as they are**, the new
`test_vram.py` passes, and the full suite passes.

---

## As built

Built 2026-09-18. `emulator/devices/vram.py` is new; `display_io.py`,
`machine.py`, `memory_map.py`, `config.py` and `cli.py` changed. The command
table in `vram.py`'s docstring is the reference. It differs from §5.2's
table in the places listed below.

**Where it differs from the plan, and why:**

- **The screen goes wherever it fits, not always at offset 0.** A mode
  change frees the old screen and places the new one first-fit. At offset 0,
  a bigger screen would run over a surface allocated after the old one, and
  surfaces never move. `GET_MODE` and `SET_MODE` report the offset, as §5.2
  already had them do. If no gap is big enough, `SET_MODE` is refused and
  nothing changes, including where the old screen is.
- **`ALLOC` takes only `[w, h]` in the window.** §5.2 also had a byte count
  in ADDRESS, which just repeated `w * h * 4`. Pitch is always `w * 4`.
- **`PREFERRED`'s third word is a request counter**, bumped each time the
  host posts a size. It is not the mode's `generation`. The kernel compares
  it to notice a *new* request (Phase 6).
- **In a mode bigger than 192 × 108, `DISPLAY_START` is refused as a
  base.** Not in the plan, and needed: a 320 × 180 screen at `DISPLAY_START`
  runs over the boot sector and into the program, and `CH_DISPLAY`'s `FILL`
  would write all of it. A test pins this.
- **`CH_DISPLAY`'s `GET_BASE` answers the aperture address** of the VRAM
  surface on screen, and `SET_BASE` takes aperture addresses back. So a
  save-and-restore of the base, which is what `k_tidy` does, still
  round-trips when a surface is showing.
- **`/info` answers the live `w` and `h` already.** The front ends read it
  once at startup, so after a mode change they need a reload until
  Phase 4.
- **A power-on mode other than 192 × 108 is allowed** (`--mode 640x360`,
  `"display_mode"`), and scans out of VRAM from power-on. Until Phase 6 that
  is a curiosity: the BIOS, the kernel and every program still draw at
  `DISPLAY_START`, where nobody is looking. `config.json` refuses a mode
  that is not offered, one larger than `DISPLAY_MAX_W` × `DISPLAY_MAX_H`, one
  that does not fit in `vram`, and anything but 192 × 108 with `vram` off.
- **`dump_ram_to` writes `ram_vram.bin` beside `ram.bin`**, and now returns
  both paths. The menu's dump prints both.

**Found while building:**

- **`DisplayIO` holds the VRAM device through a weak reference.** The two
  point at each other, and as a cycle every machine's 144 MB waited for
  Python's cycle collector instead of being freed. In `test_vram.py`
  alone that was an 868 MB peak, against 294 MB with the weak link.
  The first full run, with 12 workers, ran the host out of memory. That run
  is what crashed WSL (exit 137). `test_vram.py` also uses a 16 MB RAM for
  the device tests, since nothing there depends on RAM's size.
- **Known gap for Phase 6:** `/bin/reboot.bin` jumps back to the BIOS
  without resetting the mode. After a `SET_MODE`, a software reboot would
  have the BIOS drawing at `DISPLAY_START` while the screen shows VRAM.
  Q14 says the mode does not survive a reboot, so reboot must put
  192 × 108 and the RAM scanout back. That belongs with 6b.4, where
  `k_tidy` learns to restore the mode.

**Tests:** `tests/test_vram.py`, 39 cases: every command and its refusals,
surfaces that never move, the scanout flip and the refusal of a wrong-shaped
surface, the round-trip through `CH_DISPLAY`, one `SET_MODE` over the real
`IOController`, and three `Machine`-level cases. Twelve more cases in
`tests/test_config.py`. `test_display.py`'s 29 cases pass **unchanged**.

**The suite:** 1,727 passed, 2 failed. Those are the same two as in Phase 1:
the installer expecting 32 files where the uncommitted `/docs/gui.pgs`
makes 33.
