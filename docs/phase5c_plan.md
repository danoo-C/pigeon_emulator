# Phase 5c: the Serial panel, in the browser and in pygame

> **Status: plan, 2026-09-15, and built the same day ([§7](#7-as-built)).** Part 5c of
> [phase5_plan.md](phase5_plan.md), its steps 9 and 10, and step 11's docs
> for this part; the last of phase 5. 5a gave the host `GET /serial`, and 5b
> gave the machine plenty to say on it. This part shows it beside the screen,
> in both front ends. As for 5b, you asked for the building to carry on, so
> the questions are decided in [§5](#5-decisions). Facts marked *checked*
> were read in the code, *measured* ones were run; the rest is reasoned.

---

## 1. Where things stand

- **5a and 5b are committed** (`21ae68c`, `be0b3db`). `GET /serial?from=N`
  answers `{start, next, text, stamps, lost}`, with each stamp an index into
  `text` and the seconds since power-on *(checked:
  `emulator/devices/debug_port.py`, `serial_reply`)*. An offset past the end
  reads from the start, which is how a page left open across a restart finds
  out.
- **The browser page** *(checked: `display/index.html`)*:
  - one column: the controls, the CD row, the canvas and a hint;
  - `mousemove` and `mouseup` are on the window, and `mouseup` posts a release
    for any button, wherever it went down (`:368`, `:384`);
  - its wheel listener is the canvas's (`:393`);
  - the scale box resizes the canvas (`:301`);
  - `tests/test_input.py` lifts the outbox and the wheel adder out of the
    page by their first lines and runs them under node, and checks listeners
    by their source.
- **The pygame client** *(checked: `display/display.py`)*:
  - `_resize_window` sets the window from the screen and the toolbar's width
    (`:493`), and `_render` draws the screen at `(0, BUTTON_BAR_HEIGHT)`;
  - `_window_to_virtual_coords` takes off the toolbar's height only (`:355`);
  - `run` sends the pointer's position every frame, forwards presses below
    the toolbar, remembers a press that began on it with `_toolbar_drag`, and
    turns `MOUSEWHEEL`, which has no position, into HID notches;
  - each thread has its own `requests.Session`, since one isn't safe to share.
- **node 24 and pygame 2.6 are installed** *(checked)*, so the tests that
  need them run here rather than skip.

---

## 2. The browser

- **The layout:** the controls and the CD row, then a row of the panel and
  the screen, with the hint under the screen.
- **The panel:** a header with its title, a *times* checkbox, *Clear* and ×;
  the text in a monospace column; a handle on its right edge.
- **A *Serial* button** in the controls opens and closes it, and × closes it.
- **Its width** is dragged by the handle, between 160 px and 70% of the
  window. Its height follows the screen's.
- **`localStorage`** keeps whether it's open, its width, and whether times
  show. A missing or damaged entry gives the defaults: open, 320 px, times.
- **The text follows new lines** unless you've scrolled up. *Clear* empties
  the view only; the machine's log, the terminal and the file don't change.
- **Times** are dim, `[   1.234] ` before each line, and blank for a line
  whose start was lost.
- **Polling:** `/serial?from=N` every 250 ms while the panel is open. A
  reply that starts before what the page had seen is a new machine, and the
  view starts again.
- **Nothing in the panel reaches HID.** Its handlers post nothing; the
  window's `mouseup` releases only buttons pressed on the screen, and its
  `mousemove` is quiet while the edge is dragged. The wheel over the panel
  scrolls it, since the wheel listener is the canvas's.
- **Tests,** in a new `tests/test_serial_panel.py`, with the panel's logic
  lifted out of the page and run under node: lines put together from
  replies, lost bytes, a restart, the line limit, times, the follow rule, the
  width clamp, and the stored settings; and from the page's source, the
  layout, the listeners that must not post, and the polling.

## 3. The pygame client

- **A *Serial* toolbar button** opens and closes the panel. The window grows
  by its width, and the screen is drawn to its right.
- **The panel:** a header with *Times* and *Clear*, and the text below,
  wrapped to its width in a monospace font, following new lines unless
  scrolled up.
- **Its right edge** is dragged to resize it. A line shows where it will go,
  and the window is resized once, when the drag ends.
- **The wheel over the panel scrolls it,** judged by where the pointer is.
  Presses and the wheel over it never go to HID, and the guest's pointer is
  measured from the screen's new left edge.
- **A thread polls `/serial`** every 250 ms while the panel is open, with a
  session of its own.
- **`build/display.json`** keeps whether it's open, its width and times. A
  missing or damaged file gives the defaults, as in the browser.
- **The logic is plain Python** in `display/serial_panel.py`, which imports
  no pygame: the layout, which region a point is in, the pointer's position
  on the screen, the transcript, wrapping, scrolling, the width clamp, the
  settings file, and checking a reply.
- **Tests,** in the same file: those functions without a window; and
  `DisplayClient`'s mouse handlers on a client made without `__init__`, as
  `test_input.py` makes one, for presses over the panel, the drag, and the
  wheel.

## 4. Order, checks and commit

1. The browser. 2. The pygame client. 3. The docs: the README's front ends,
   phase5_plan.md, kernel.md §17 and kernel_overview.md.

Each step's tests run as it's built; then deliberate breakages for each
piece, the full suite, and one commit for 5c.

---

## 5. Decisions

Decided 2026-09-15 (left to me), since you asked to carry on:

1. ~~**pygame's panel the first time**~~: **open, as the browser's** (Q2 of
   phase 5).
2. ~~**pygame's widest panel**~~: **7/3 of the screen,** which is 70% of the
   window, the browser's rule.
3. ~~**Times in pygame**~~: **shown, with a *Times* button** in the panel's
   header, as the browser has its checkbox (Q5).
4. ~~**How much the panels keep**~~: **the last 2,000 lines.** The machine
   keeps 64 KB, and a reload or reopening fetches what it still has.
5. ~~**A restart of the emulator**~~: **the panel starts again,** known by a
   reply that begins before the last offset seen.
6. ~~**Where the tests go**~~: **a new `tests/test_serial_panel.py`,** for
   both front ends, rather than growing `test_input.py`.

---

## 6. Not in 5c

- **Typing into the panel,** as phase 5 decided.
- **Colors or escape codes** in the panel: the port carries plain text, and
  control characters arrive as U+FFFD.

---

## 7. As built

- **The browser:** `display/index.html` has the panel, its logic between two
  marked comments, and the window's `mouseup` and `mousemove` changed as §2
  says. Shown in headless Chromium, on a first visit, against a running
  emulator: open, left of the screen, the times dim and long lines wrapped.
- **The pygame client:** the logic in `display/serial_panel.py`, and
  `display.py` drawing it. The mouse handling moved out of `run` into
  `_mouse_down`, `_mouse_up`, `_mouse_motion` and `_wheel`, and a press is
  now kept from the guest one button at a time, where a single flag used to
  stand for every button. Drawn with SDL's dummy driver and looked at: the
  toolbar gains *Serial*, the panel its *Times* and *Clear*, and the screen
  starts at the panel's width.
- **`GET /serial`** was fetched from a running display server for the first
  time, `?from=` included.
- **Tests:** 27 new, in `tests/test_serial_panel.py`: 9 for the page, run under
  node, and 18 for the pygame client, one of them drawing it. 53 deliberate
  breakages, each failing a test; the screen drawn at x 0 needed the drawing
  test to look past the panel's width first.
- **The full suite:** 1,378 of 1,382 on the first run. Two were
  `test_cd.py`'s toolbar tests, which counted the buttons by position; they
  now find the CD buttons by name, and pass. The other two were the example
  disc's file count, from `corrupter.bin`, committed next with the counts
  raised to 20.
