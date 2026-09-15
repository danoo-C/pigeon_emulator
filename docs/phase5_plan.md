# Phase 5: the serial debug port, and a panel for it

> **Status: final plan, 2026-09-15, checked again against the code the same
> day ([§11](#11-checked-again)). Every question is decided
> ([§10](#10-your-answers)). All three parts are built: 5b as planned in
> [phase5b_plan.md](phase5b_plan.md), and 5c in [phase5c_plan.md](phase5c_plan.md).** Swapped with the boot
> screen and startup script, as you decided: those become phase 6, and the
> launcher phase 7. The idea was sketched in [phase4_plan.md](phase4_plan.md)
> §11; this is the full plan. Facts marked *checked* were read in the code,
> *measured* ones were run; the rest is reasoned.

---

## 1. What you asked for

From the chat, 2026-09-15:

- **A debug port,** with a panel on the left of the screen, closable and
  expandable like VS Code's explorer.
- **bios2 and the kernel say where boot has got to.** Stage 1 of the BIOS
  too, *"if a message fits, then why not."*
- **Faults, "100%", and every `exec`,** *"but it cannot slow the system down
  too much."* Logs only: *"not the whole console."*
- **Timestamps added on the host.**
- **The whole GUI at once:** the browser and the pygame client in the same
  part.
- **A log file on the host** as an option.

---

## 2. Where things stand

- **IO channels 1 to 7 are taken, and 8 is free** *(checked:
  `emulator/memory_map.py:154–160`)*.
- **A device answers through the data window,** 4 KB, and an IO command
  can instead name RAM directly, as the HDD's `READ_DMA` does with
  `[address, count]` in the window *(checked: `emulator/devices/hdd.py`,
  `_dma`)*. The HDD refuses a range below `PROGRAM_LOAD_ADDR`, since it
  writes RAM *(checked: `hdd.py:166`)*.
- **With R/W 1 the controller copies no reply into the window:** RETURN_DATA
  is only the length of what the device returned *(checked:
  `emulator/io_controller.py:82–85`)*. The HDD's WRITE returns nothing.
- **An empty channel answers `0xFFFFFFFF`,** and nothing breaks *(checked:
  `emulator/io_controller.py:15`)*. `display.c` already tells a bare CPU,
  an empty channel and a device apart *(checked: `lib/pigeon/display.c`,
  `disp_probe`)*.
- **Stage 1 of the BIOS is 944 of 1,024 bytes,** so 80 are free *(checked:
  docs/os_cd.md's table, and `build/bios.bin`)*. Just before it jumps to
  bios2, R/W is 0, LENGTH is 8 and A points at the data window, left from
  loading bios2, and the controller writes back only RETURN_DATA and the
  channel *(checked: `firmware/bios.asm:66–101`, `io_controller.py:85–86`)*.
  So a write naming RAM is seven instructions, four of them stores: **with
  its 12 bytes of text, stage 1 assembles to 1,012 bytes** *(measured)*.
- **The boot sector is 376 of 384 bytes,** so 8 are free *(checked:
  docs/os_cd.md; `firmware/boot.asm:3–4`)*: no message fits.
- **bios2's boot is a few functions:** `check_all` looks at the devices,
  `main` counts down, `boot` tries a device and says why it failed,
  `menu` runs the menu, and `hand_over` jumps *(checked:
  `firmware/bios2.c:209`, `:292`, `:302`, `:335`, `:382`)*.
- **The installer's steps** are in its `main`: mount the disc, size the
  disk, count the files, wait for Enter, install, restart *(checked:
  `user/os/installer.c:300`)*.
- **The kernel's moments** are `main` (mount, start the shell), `k_exec`
  and `k_fault` (faults, breaks, panics) *(checked: `user/os/kernel.c:1241`,
  `:1305`, `:1356`)*. Three things shape its logging *(all checked)*:
  - `struct proc` keeps no path (`kernel.c:109`), and `k_fault` is given
    only the vector and the address.
  - `k_exit` hands its code to `exec_abort` as it is, so `exit(3)` and
    `return 3` look the same to `k_exec` (`kernel.c:1301`). Ctrl+C and `q`
    at `-- more --` end through `exec_abort` in the paging code
    (`kernel.c:912–918`), not through `k_fault`. Every end but a panic
    comes back to `k_exec`.
  - `k_fault` runs on `fault_frames`, 1,024 bytes (`kernel.asm:299`), with
    `handle_depth[]` right after it.
- **`printf` with no kernel returns -1** *(checked:
  `lib/pigeon/stdio.c:170–210`)*, as phase 4 decided until this phase. It
  writes in three places: the formatter's 64-byte flush (`stdio.c:30`),
  `puts` through `print()`, and `putchar` through `write()`.
- **stdio costs 15,960 bytes,** because it brings `sys.c` along *(measured:
  a `snprintf` program against a `strlcpy` one)*. The kernel, bios2 and the
  installer include neither today, and no names would clash *(checked)*.
- **The display server** serves `/`, `/frame`, `/clear` and `/info` on the
  display port, 8000 by default and 1234 in your `config.json` *(checked:
  `emulator/devices/display_io.py`, `start_fastapi`)*. It knows nothing of
  the other devices: `Machine.start_servers` hands it `hid_url` and `cd_url`
  *(checked: `emulator/machine.py:125`)*.
- **The browser page is one column:** controls, the CD row, then the canvas
  *(checked: `display/index.html:7`, `:20–39`)*. Its wheel listener is on the
  canvas only, **but two listeners are on the window** *(checked)*:
  `mousemove` posts the position while a button is held or the pointer is
  over the canvas (`:368`), and `mouseup` posts a release for any button,
  wherever it was pressed (`:384`).
- **The pygame client** sizes its window from the screen and the toolbar,
  and draws the screen at `(0, BUTTON_BAR_HEIGHT)` *(checked:
  `display/display.py`, `_resize_window`, `_render`)*. It sends the pointer's
  position every frame, wherever it is (`display.py:743–745`), and a
  `MOUSEWHEEL` event carries no position *(checked)*.
- **The timer device reads a clock the tests can step** *(checked:
  `emulator/devices/timer.py:54–57`, `tests/_runner.py`)*. Each read moves
  it 0.05 s, and the tests swap the module's `clock`, so a reader looks it
  up each time rather than importing it.
- **No test talks to the HTTP servers** *(checked)*. The CD tests call the
  device's methods instead.
- **Include cycles are safe:** `libraries_for` keeps a set of what it has
  seen *(checked: `emulator/programs.py`)*.
- **`build/` is "safe to delete"** in `.gitignore` *(checked)*, so anything
  kept there goes with a clean.

---

## 3. The goal

When phase 5 is done:

- a program, bios2 or the kernel writes a line to the debug port, and it
  never shows on the machine's screen;
- the browser and the pygame client show the lines in a panel on the left,
  with the time since power-on in front of each;
- `--serial` prints them in the launcher's terminal, and `--serial-log PATH`
  writes them to a file;
- booting from the installed disk reads like this *(illustrative)*:

  ```
  [   0.000] bios: bios2
  [   0.031] [bios2] hard disk: PIGEONOS, boots /boot.bin
  [   0.032] [bios2] CD: empty
  [   0.034] [bios2] counting down from 5 s
  [   1.920] [bios2] Enter: booting the hard disk
  [   1.921] [bios2] its boot sector, at 0x15898
  [   2.104] [kernel] started, 209,528 bytes at 0x20000
  [   2.106] [kernel] mounted channel 2, PIGEONOS
  [   2.210] [kernel] exec /bin/sh.bin at 0x01000000, depth 1
  [   9.873] [kernel] exec /bin/ls.bin at 0x0102C000, depth 2
  [   9.951] [kernel] /bin/ls.bin ended: 0
  [  14.002] [kernel] /bin/div0.bin ended: divided by zero at 0x01034A10
  ```

---

## 4. The design

### 4.1 The device: `CH_DEBUG`, channel 8

Write-only, as the sketch said, and named `CH_DEBUG`, the debug port (Q1).
The panels that show it are titled "Serial".

| cmd | name | R/W | LENGTH | does |
|---|---|---|---|---|
| 0 | NOP | 0 | 4 | 4 zero bytes |
| 1 | WRITE | 1 | the text's length | takes the text from the data window, up to 4 KB; RETURN_DATA is the bytes taken |
| 2 | WRITE_DMA | 0 | 8 | takes `count` bytes from RAM at `address`, both in the window; `count` comes back, or `0xFFFFFFFF` refused |

- **WRITE is for C,** which already has text in hand. **WRITE_DMA is for
  assembly:** no copy loop, which is what lets stage 1 say anything.
- **WRITE takes at most the window,** 4 KB, and cuts off the rest of a
  longer LENGTH. With R/W 1, RETURN_DATA is the only way a count comes back.
- **WRITE_DMA refuses** a range past the end of RAM, and more than 64 KB at
  once. **Any lower address is fine,** unlike the HDD's rule: the port only
  reads RAM, and stage 1's text is in the BIOS, at `0x3E8`.
- **On the host:** the last 64 KB, as a ring. Every byte since power-on has
  an offset, so a reader asks for what came after the last offset it saw.
- **Timestamps:** when a byte starts a line, the device notes the time since
  it was made, from the timer device's clock, looked up at each line so
  the tests' stepping clock applies. The guest sends bytes only.
- **`since(offset)`** returns what came after the offset: the text, the
  offset to ask from next, and the line starts with their times. When the
  offset has fallen out of the ring, it starts from the oldest byte kept
  and says how many were lost.

### 4.2 Writing to it: `<pigeon/debug.h>`

```c
int dbg_write(char *text, unsigned n);   /* -> bytes taken, or -1: no port */
int dbg_print(char *text);
int dbg_printf(char *format, ...);       /* up to 256 bytes a call */
```

- **It probes the port the way `display.c` probes the display,** once: on a
  bare CPU, or a machine without the device, every call returns -1 at once.
- **`dbg_printf` formats with `vsnprintf`,** so it brings `stdio.c` and
  `sys.c` along, 15,960 bytes *(measured)*, into the kernel, bios2 and the
  installer. It formats straight into the data window, so a line needs no
  buffer and is never copied, and a call costs the frame stack only the
  formatter's frames: 232 bytes *(measured)*.
- **`printf`, `puts` and `putchar` with no kernel write to the port,** all
  three of their writes, the formatter's flush included, and return -1 only
  when there's no port either. On a bare CPU, as
  `test_stdio.py` runs them, that's still -1 *(checked: `tests/test_stdio.py:119`)*.
- **Each program says who it is:** the library adds no prefix, and bios2
  writes `[bios2] `, the kernel `[kernel] `.

### 4.3 Who says what

- **Stage 1, `firmware/bios.asm`:** `bios: bios2`, just before the jump,
  with WRITE_DMA. That's 64 bytes of code and 12 of text, in the 80 free.
  If it doesn't fit once assembled, stage 1 stays silent, as you said.
- **The boot sector:** nothing; 8 bytes can't hold a message. bios2 says
  what it hands over to, and the kernel says it started.
- **bios2:**
  - each device checked, and what it found: a program and its size, a disk
    or disc and its label and boot record, or nothing;
  - the countdown starting, and what ended it: Enter, Esc or time;
  - each boot tried, where it jumps, and why it failed when it comes back;
  - each menu choice.
- **The installer:** the disc mounted, the disk's size, the files to copy,
  Enter or Esc, each file copied, the boot record written, and every error.
- **The kernel:**
  - started, and the disk it mounted, or why it couldn't;
  - **every `exec`:** the path, where it went and at what depth, then how it
    ended: its status, `exit`, a fault, Ctrl+C or `q`. `struct proc` keeps a
    copy of the path, and `k_exit` sets a flag so an `exit` can be told from
    a return;
  - **every fault,** with the program and the address. `k_fault` notes the
    address, and `k_exec` puts it in the line saying how the program ended,
    so a fault is logged on the kernel's own frame stack, not `fault_frames`;
  - **a panic,** with the address, before the machine halts. Only this one
    is logged from `k_fault`, where `dbg_printf`'s 232 bytes fit;
  - the shell started again.
- **Not the console.** The kernel logs its own events only.

**"It cannot slow the system down too much."** A log line is formatted, then
copied into the window. The estimate is a few thousand instructions a line,
where `exec` already reads a file of tens of kilobytes *(both to be measured
in step 1)*. Step 1 measures an `exec` of `echo` with and without its two
lines, and the tests pin the cost of a line in instructions.

### 4.4 The host side

- **`GET /serial?from=N`** on the display server, the page's own origin:
  `{"start", "next", "text", "stamps", "lost"}`. The route is a few lines
  around `since()`, which the tests call directly. `Machine.start_servers`
  hands the port to the display server, as it hands it `hid_url` and
  `cd_url`.
- **`--serial`,** and `"serial": true` in `config.json`: each finished line
  is printed in the launcher's terminal with its time, headless or not. A
  line without its `\n` is printed after half a second, and whatever is left
  when the machine halts is printed then.
- **One printer serves the terminal and the file.** It runs in the
  emulator's loop at the display's 30 frames a second: it takes what came
  since it last looked, with `since()`, writes it in one go and flushes. A
  flood is 30 writes a second, not one a line.
- **`--serial-log PATH`,** and `"serial_log"` in `config.json`: the same
  lines, written to a file. It's started fresh each run, with the date and
  time the run started as its first line (Q4). One file is one run: the
  installer's restart jumps back to the BIOS in the same run, so both boots
  land in it.

### 4.5 The panel

**In the browser:**
- **The layout** becomes the controls and the CD row, then a row of the
  panel and the canvas.
- **A "Serial" button** in the controls opens and closes it, and a × in its
  header closes it too. It's open the first time the page loads (Q2).
- **Dragging its right edge** changes its width, between 160 px and 70% of
  the window.
- **Remembered in `localStorage`:** its width, whether it's open, and
  whether times show.
- **The text** is a monospace column. It follows new lines unless you've
  scrolled up, and a Clear button empties the view without touching the
  machine's buffer, so the terminal and the log file are unaffected.
- **Times** show by default, dim, before each line, with a checkbox to
  hide them (Q5).
- **It polls `/serial?from=N`** every 250 ms while open. A reload starts
  from the oldest byte the machine still keeps.
- **The wheel over the panel scrolls the panel.** It never reaches HID,
  since the page's wheel listener belongs to the canvas.
- **Nothing done in the panel reaches HID.** The window's `mouseup` releases
  only buttons pressed on the canvas, and its `mousemove` posts nothing while
  the panel's edge is being dragged.

**In the pygame client:**
- **A "Serial" toolbar button** opens and closes it. The window grows by
  its width, and the screen is drawn to its right.
- **Its right edge** can be dragged to resize it, and the wheel over it
  scrolls it, judged by where the pointer is. Presses and the wheel over the
  panel never go to HID, and the guest's mouse position is measured from the
  screen's new left edge: over the panel it reads as x 0, as it reads y 0
  over the toolbar today.
- **The window is resized when a drag ends,** not on every motion, since
  `set_mode` makes the window again each time and flickers on WSLg. While
  dragging, a line shows where the edge will go.
- **The text** wraps to the panel's width, follows new lines unless
  scrolled up, and has a Clear button in the panel's header.
- **A thread polls `/serial`,** as one already fetches frames.
- **Remembered between runs** in `build/display.json` (Q3): whether it's
  open, and its width. A missing or damaged file gives the defaults, and so
  does a clean, which deletes `build/`.

---

## 5. Steps

### Part 5a: the port, and what the host does with it

**Step 1. Measure first.** How many instructions a formatted 40-byte line
costs, copied into the data window, and how many an `exec` of `echo` costs
today; and how deep the formatter goes on the frame stack, for a panic on
`fault_frames`. The numbers set the tests' bounds, and go in this plan as
*measured*.
- ***Measured:***
  - **an `exec` of `echo`,** from `k_exec` to its return: 106,945, 132,678
    and 159,859 instructions, typed three times on one boot. Each costs more
    than the last, so step 8 compares an `exec` with its lines against the
    same `exec` without them, not against a fixed number;
  - **a 41-byte line,** `snprintf` and a copy into the window: 5,772
    instructions, with 240 bytes of frame stack;
  - **`dbg_printf` as built,** formatting straight into the window: 6,179
    instructions and 232 bytes. With a buffer and a copy it was 8,461.

**Step 2. The device.**
- `emulator/devices/debug_port.py`: WRITE, WRITE_DMA and NOP; the ring;
  the offsets and line times; `since()`.
- `memory_map.py`: `CH_DEBUG = 8`, so assembly and C have the name.
- `machine.py`: registered on every machine, as the CD drive is.
- **Tests,** in a new `tests/test_debug_port.py`:
  - each command, and the refusals;
  - WRITE cut at 4 KB with the count in RETURN_DATA, and WRITE_DMA from
    below `PROGRAM_LOAD_ADDR`;
  - the ring wrapping, and `since()` from before its start;
  - a line split across writes getting one time;
  - the times on the stepping clock.

**Step 3. `<pigeon/debug.h>` and `printf` with no kernel.**
- `lib/pigeon/debug.h` and `debug.c`; `stdio.c` writing to the port when
  there's no kernel.
- **Tests:**
  - `dbg_print` and `dbg_printf` on a Machine, read back from the device;
  - on a bare CPU, -1 and no hang;
  - `printf` longer than its 64-byte chunk, `puts` and `putchar`, with no
    kernel on a Machine, all reaching the port;
  - the cost of a line, counted.

**Step 4. The terminal, the log file and `/serial`.**
- `cli.py` and `config.py`: `--serial`, `--serial-log PATH`, `serial` and
  `serial_log`.
- `display_io.py`: the `/serial` route; `machine.py`: the port handed to it.
- **Tests:**
  - the flags and keys, in `test_config.py`;
  - the printer and the log file, with a stepping clock, checked line by
    line, the file started fresh with the run's date and time;
  - a partial line printed after its wait, and at the halt;
  - a flood written in one go a frame.

**Part 5a, as built:**
- **`emulator/devices/debug_port.py`:** NOP, WRITE and WRITE_DMA; 64 KB
  kept, with a time for each line from the timer's clock, read once a write
  and only when a line starts; `since()`; `clean()`, which shows control
  characters and bad bytes as U+FFFD; and `serial_reply()`, which
  `GET /serial` returns. `CH_DEBUG = 8` in `memory_map.py`, and
  `machine.debug` on every machine, handed to the display server by
  `start_servers`.
- **`lib/pigeon/debug.h` and `debug.c`:** `dbg_write`, `dbg_print` and
  `dbg_printf`. `dbg_printf` formats straight into the data window rather
  than a global buffer, as §4.2 now says: 6,179 instructions a line
  instead of 8,461.
- **`stdio.c`:** `printf`'s flush, `puts` and `putchar` with no kernel write
  to the port. Every program with `printf` carries `debug.c` now, 4,320
  bytes: `mkdir.bin` is 30,712.
- **`emulator/serial_printer.py`:** one printer for `--serial` and
  `--serial-log PATH`, which `Console` polls at each frame of a run and at
  its end. `serial` and `serial_log` in `config.json`.
- **Tests:** 54 new: `tests/test_debug_port.py` (28), `tests/test_serial.py`
  (19), 6 in `test_config.py` and 1 in `test_stdio.py`. 43 deliberate
  breakages, each failing a test; four of them needed a test added first.
  The full suite: 1,337 of 1,338 pass. The other, in `test_project.py`,
  counts the disc's files and finds 20, not 19: `/bin/corrupter.bin`,
  added to the project file alongside this part, not by it.

### Part 5b: what the machine says

*Planned in full, with each line's format, in
[phase5b_plan.md](phase5b_plan.md), and built there.*

**Step 5. Stage 1.** The message before the jump to bios2, if it fits.
**Tests:** `test_bios2.py` finds `bios: bios2` first on the port, and the
BIOS still assembles to 1 KB or less. If it doesn't fit, the plan records
that and stage 1 stays as it is.

**Step 6. bios2.** The lines in §4.3. **Tests:** booting the hard disk, the
CD, and channel 1, each with its lines in order; Esc and the menu; a boot
that fails and says why.

**Step 7. The installer.** **Tests:** `test_install.py` checks the lines for
an install, a cancel and a disk too small.

**Step 8. The kernel.**
- The lines in §4.3, with `dbg_printf`. The kernel must never call
  `printf`, which would go through its own system calls.
- **Tests:**
  - boot, mount and the shell, in order;
  - an `exec` and its end, with its status, and `exit` told from a return;
  - a nested `exec` with its depth;
  - a fault with its program and address, Ctrl+C and `q` at `-- more --`,
    each logged;
  - a panic logged before the halt;
  - nothing of the console reaching the port;
  - an `exec` with its lines costing at most the measured bound more than
    without.

### Part 5c: the panel, in both front ends

*Planned in full, with its decisions, in [phase5c_plan.md](phase5c_plan.md),
and built there.*

**Step 9. The browser.** `display/index.html`.
**Tests,** in `test_input.py` or a new `test_front_ends.py`, run under node
like the outbox and wheel tests *(checked: they exist)*:
- the page's text-and-times assembler, lifted out: partial lines, lost
  bytes, times hidden;
- the follow-unless-scrolled rule, and the width clamp;
- the page still valid JavaScript;
- the panel's handlers never posting to HID, and the window's `mouseup` and
  `mousemove` quiet for presses and drags that started in the panel;
- the panel open the first time, then as `localStorage` last had it.

**Step 10. The pygame client.** `display/display.py`.
**Tests,** without a window:
- the layout: where the screen goes with the panel open and closed;
- the mouse position measured from the screen, and presses and the wheel
  over the panel kept from HID;
- the window resized once, when a drag ends;
- wrapping and the follow rule, as plain functions;
- the poller parsing `/serial`'s answer;
- the panel's state written to `build/display.json` and read back, and a
  missing or damaged file giving the defaults.

### Step 11. Docs

At the end of each part:
- the README: channel 8's row in the IO table, the flags and the keys;
- `lib/README.md`: `debug.h`, and `printf`'s new fallback;
- kernel.md §17, renumbered: phase 5 the port, 6 the boot screen and startup
  script, 7 the launcher;
- os_cd.md, for stage 1 and bios2's lines;
- kernel_overview.md's build order, and phase4_plan.md §11's note, whose
  Phase 7 still calls the launcher kernel.md's item 6.

---

## 6. Order and commits

| Part | Steps | What you get |
|---|---|---|
| **5a** | 1–4 | the port, `debug.h`, `printf` with no kernel, `--serial`, `--serial-log`, `/serial` |
| **5b** | 5–8 | stage 1, bios2, the installer and the kernel logging boot, `exec`s and faults |
| **5c** | 9, 10 | the panel in the browser and in pygame |

Each part is committed on its own, with step 11 at its end. 5b can be seen
in the terminal before 5c exists.

| Step | Needs | Size |
|---|---|---|
| 1. Measure | — | small |
| 2. The device | — | small to medium |
| 3. `debug.h`, `printf` | 2 | small |
| 4. Terminal, file, `/serial` | 2 | medium |
| 5. Stage 1 | 2 | small, and tight |
| 6. bios2 | 3 | small to medium |
| 7. The installer | 3 | small |
| 8. The kernel | 1, 3 | medium |
| 9. The browser panel | 4 | medium |
| 10. The pygame panel | 4 | medium to large: the window, the mouse and wrapping |
| 11. Docs | all | small |

---

## 7. How it will be checked

As in phase 4:
- each step's tests, running only the files a step touches while it's built,
  and the whole suite at the end of each part;
- deliberate breakages for every piece, each of which must fail a test, with
  the test filter checked so it can't miss one again;
- costs counted in instructions, never timed.

---

## 8. Risks

- **Stage 1's 80 bytes.** Assembled with the message, it's 1,012 bytes
  *(measured)*, 12 to spare. If step 5 needs more, stage 1 stays silent.
- **Logging `exec` in the kernel** must stay cheap. Its two lines cost about
  12,400 instructions *(measured: 6,179 a line)*, against 107,000 to 160,000
  for an `exec` of `echo` today, so about a tenth, and a test pins it.
- **The kernel must not call `printf`,** which would call itself through the
  system-call table. `dbg_printf` is kernel-safe; a comment and a breakage
  test say so.
- **Bytes that aren't text,** from a program writing garbage, reach the panel
  as replacement characters, and never break the page or the terminal.
- **The terminal printing a flood** of lines runs in the emulator's thread.
  The printer writes at most 30 times a second, whatever came since the last
  time, so megabytes of log cost 30 writes a second (§4.4).
- **The panic's frame stack.** `k_fault` runs on 1,024 bytes. `dbg_printf`
  needs 232 of them *(measured)*, and a test holds it to 300.
- **The pygame panel is the biggest piece:** the window, the mouse mapping
  and the toolbar all move. Its logic is written as plain functions so it
  can be tested without a window.

---

## 9. Not in phase 5

- **Typing into the panel,** sending bytes to the machine. The port stays
  write-only; the sketch's "maybe, much later" still stands.
- **Mirroring the console** to the port, as you decided.
- **Phase 6, the boot screen and the startup script,** and **phase 7, the
  launcher,** as sketched in phase4_plan.md §11.

---

## 10. Your answers

Answered in this file on 2026-09-15; each went as recommended.

1. ~~**The name:** `CH_DEBUG`, "the debug port", or `CH_SERIAL`, "the serial
   port"?~~

   Answer: CH_DEBUG

   **Decided (you):** `CH_DEBUG`, the debug port (§4.1). The panels keep the
   title "Serial", as recommended.

2. ~~**The browser panel the first time:** open or closed?~~

   Answer: okay

   **Decided (you):** open the first time; after that, as you left it
   (§4.5).

3. ~~**The pygame client remembering the panel** between runs, in
   `build/display.json`?~~

   Answer: okay

   **Decided (you):** yes, whether it's open and its width (§4.5).

4. ~~**The log file:** started fresh each run, or added to?~~

   Answer: fresh

   **Decided (you):** fresh each run, with the run's date and time as its
   first line (§4.4).

5. ~~**Times in the panel:** shown by default, with a checkbox to hide
   them?~~

   Answer: show them.

   **Decided (you):** shown, with a checkbox to hide them (§4.5).

---

## 11. Checked again

The same day, every *checked* fact was read again in the code, and they held.
What changed, all folded in above:

1. **The browser's window listeners** would have posted to HID for presses
   and drags in the panel (§2, §4.5).
2. **The kernel keeps no path and can't tell `exit` from a return,** and a
   fault's line moves to `k_exec`, off `fault_frames` (§2, §4.3).
3. **stdio costs 15,960 bytes** with `sys.c`, not about 20 KB (§2, §4.2).
4. **`printf` writes in three places,** and all three go to the port (§4.2).
5. **WRITE's count comes back in RETURN_DATA,** and WRITE stops at 4 KB
   (§4.1).
6. **WRITE_DMA takes any address in RAM,** unlike the HDD, so stage 1 can
   name its own text (§4.1).
7. **`/serial` needs `machine.py`** to hand the port over (§4.4, step 4).
8. **The printer:** 30 writes a second, which settles "flushed after each
   line" against "in batches" (§4.4, §8).
9. **Stage 1, measured:** 1,012 of 1,024 bytes (§2, §8).
10. **The pygame window is resized when a drag ends** (§4.5).
11. **Smaller:** your display port is 1234; `stdio.c`'s range ends at 210;
    one log file is one run; phase4_plan.md's Phase 7 note; a clean deletes
    `build/display.json`.
