# Phase 5: the serial debug port, and a panel for it

> **Status: final plan, 2026-09-15. Every question is decided
> ([§10](#10-your-answers)). Nothing built.** Swapped with the boot screen and startup script,
> as you decided: those become phase 6, and the launcher phase 7. The idea
> was sketched in [phase4_plan.md](phase4_plan.md) §11; this is the full
> plan. Facts marked *checked* were read in the code, *measured* ones were
> run; the rest is reasoned. It starts once 4b.3 is committed.

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
  `_dma`)*.
- **An empty channel answers `0xFFFFFFFF`,** and nothing breaks *(checked:
  `emulator/io_controller.py:15`)*. `display.c` already tells a bare CPU,
  an empty channel and a device apart *(checked: `lib/pigeon/display.c`,
  `disp_probe`)*.
- **Stage 1 of the BIOS is 944 of 1,024 bytes,** so 80 are free *(checked:
  docs/os_cd.md's table)*. Just before it jumps to bios2, R/W is 0 and
  LENGTH is 8, left from loading bios2 *(checked: `firmware/bios.asm:66–101`)*,
  so a write naming RAM needs only four stores: 64 bytes, plus the text.
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
  `:1305`, `:1356`)*.
- **`printf` with no kernel returns -1** *(checked:
  `lib/pigeon/stdio.c:170–206`)*, as phase 4 decided until this phase.
- **The display server** serves `/frame`, `/clear` and `/info` on port 8000
  *(checked: `emulator/devices/display_io.py`, `start_fastapi`)*.
- **The browser page is one column:** controls, the CD row, then the canvas
  *(checked: `display/index.html:7`, `:20–39`)*. Its wheel listener is on the
  canvas only *(checked)*.
- **The pygame client** sizes its window from the screen and the toolbar,
  and draws the screen at `(0, BUTTON_BAR_HEIGHT)` *(checked:
  `display/display.py`, `_resize_window`, `_render`)*.
- **The timer device reads a clock the tests can step** *(checked:
  `emulator/devices/timer.py:54–57`, `tests/_runner.py`)*.
- **No test talks to the HTTP servers** *(checked)*. The CD tests call the
  device's methods instead.
- **Include cycles are safe:** `libraries_for` keeps a set of what it has
  seen *(checked: `emulator/programs.py`)*.

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
  [  14.002] [kernel] /bin/div0.bin: divided by zero at 0x01034A10
  ```

---

## 4. The design

### 4.1 The device: `CH_DEBUG`, channel 8

Write-only, as the sketch said, and named `CH_DEBUG`, the debug port (Q1).
The panels that show it are titled "Serial".

| cmd | name | R/W | LENGTH | does |
|---|---|---|---|---|
| 0 | NOP | 0 | 4 | 4 zero bytes |
| 1 | WRITE | 1 | the text's length, up to 4 KB | takes the text from the data window |
| 2 | WRITE_DMA | 0 | 8 | takes `count` bytes from RAM at `address`, both in the window; `count` comes back, or `0xFFFFFFFF` refused |

- **WRITE is for C,** which already has text in hand. **WRITE_DMA is for
  assembly:** no copy loop, which is what lets stage 1 say anything.
- **WRITE_DMA refuses** a range past the end of RAM, and more than 64 KB at
  once.
- **On the host:** the last 64 KB, as a ring. Every byte since power-on has
  an offset, so a reader asks for what came after the last offset it saw.
- **Timestamps:** when a byte starts a line, the device notes the time since
  it was made, from the timer device's clock, so tests can step it. The
  guest sends bytes only.
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
- **`dbg_printf` formats with `vsnprintf`,** so it brings `stdio.c` along,
  about 20 KB *(the size phase 4a measured)*.
- **`printf`, `puts` and `putchar` with no kernel write to the port,** and
  return -1 only when there's no port either. On a bare CPU, as
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
    ended: its status, `exit`, a fault, Ctrl+C or `q`;
  - **every fault,** with the program and the address;
  - **a panic,** with the address, before the machine halts;
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
  around `since()`, which the tests call directly.
- **`--serial`,** and `"serial": true` in `config.json`: each finished line
  is printed in the launcher's terminal with its time, headless or not. A
  line without its `\n` is printed after half a second.
- **`--serial-log PATH`,** and `"serial_log"` in `config.json`: the same
  lines, written to a file. It's started fresh each run, with the date and
  time the run started as its first line, so one file is one boot (Q4), and
  flushed after each line.

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

**In the pygame client:**
- **A "Serial" toolbar button** opens and closes it. The window grows by
  its width, and the screen is drawn to its right.
- **Its right edge** can be dragged to resize it, and the wheel over it
  scrolls it. Presses and the wheel over the panel never go to HID, and the
  guest's mouse position is measured from the screen's new left edge.
- **The text** wraps to the panel's width, follows new lines unless
  scrolled up, and has a Clear button in the panel's header.
- **A thread polls `/serial`,** as one already fetches frames.
- **Remembered between runs** in `build/display.json` (Q3): whether it's
  open, and its width. A missing or damaged file gives the defaults.

---

## 5. Steps

### Part 5a: the port, and what the host does with it

**Step 1. Measure first.** How many instructions a formatted 40-byte line
costs, copied into the data window, and how many an `exec` of `echo` costs
today. The numbers set the tests' bounds, and go in this plan as *measured*.

**Step 2. The device.**
- `emulator/devices/debug_port.py`: WRITE, WRITE_DMA and NOP; the ring;
  the offsets and line times; `since()`.
- `memory_map.py`: `CH_DEBUG = 8`, so assembly and C have the name.
- `machine.py`: registered on every machine, as the CD drive is.
- **Tests,** in a new `tests/test_debug_port.py`:
  - each command, and the refusals;
  - the ring wrapping, and `since()` from before its start;
  - a line split across writes getting one time;
  - the times on the stepping clock.

**Step 3. `<pigeon/debug.h>` and `printf` with no kernel.**
- `lib/pigeon/debug.h` and `debug.c`; `stdio.c` writing to the port when
  there's no kernel.
- **Tests:**
  - `dbg_print` and `dbg_printf` on a Machine, read back from the device;
  - on a bare CPU, -1 and no hang;
  - `printf` with no kernel on a Machine reaching the port;
  - the cost of a line, counted.

**Step 4. The terminal, the log file and `/serial`.**
- `cli.py` and `config.py`: `--serial`, `--serial-log PATH`, `serial` and
  `serial_log`.
- `display_io.py`: the `/serial` route.
- **Tests:**
  - the flags and keys, in `test_config.py`;
  - the printer and the log file, with a stepping clock, checked line by
    line, the file started fresh with the run's date and time;
  - a partial line printed after its wait.

### Part 5b: what the machine says

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
  - an `exec` and its end, with its status;
  - a nested `exec` with its depth;
  - a fault, Ctrl+C and `q` at `-- more --`, each logged;
  - a panic logged before the halt;
  - nothing of the console reaching the port;
  - an `exec` with its lines costing at most the measured bound more than
    without.

### Part 5c: the panel, in both front ends

**Step 9. The browser.** `display/index.html`.
**Tests,** in `test_input.py` or a new `test_front_ends.py`, run under node
like the outbox and wheel tests *(checked: they exist)*:
- the page's text-and-times assembler, lifted out: partial lines, lost
  bytes, times hidden;
- the follow-unless-scrolled rule, and the width clamp;
- the page still valid JavaScript;
- the panel's handlers never posting to HID;
- the panel open the first time, then as `localStorage` last had it.

**Step 10. The pygame client.** `display/display.py`.
**Tests,** without a window:
- the layout: where the screen goes with the panel open and closed;
- the mouse position measured from the screen, and presses over the panel
  kept from HID;
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
- kernel_overview.md's build order, and phase4_plan.md §11's note.

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

- **Stage 1's 80 bytes.** The count in §2 is reasoned from the assembly; if
  the message and its text don't fit, stage 1 stays silent.
- **Logging `exec` in the kernel** must stay cheap. Step 1 measures it
  before anything else, and a test pins it.
- **The kernel must not call `printf`,** which would call itself through the
  system-call table. `dbg_printf` is kernel-safe; a comment and a breakage
  test say so.
- **Bytes that aren't text,** from a program writing garbage, reach the panel
  as replacement characters, and never break the page or the terminal.
- **The terminal printing a flood** of lines runs in the emulator's thread.
  If a program writes megabytes, the printer gathers lines and writes them
  in batches rather than slowing the machine.
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
