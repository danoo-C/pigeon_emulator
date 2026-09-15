# Phase 4b: line editing, Tab completion, scrollback, `more` and `edit`

> **Status: final plan, 2026-09-15; every question is decided (§9 and §10).
> 4b is built:** 4b.1 (steps 1–4), 4b.2 (steps 5 and 6) and 4b.3 (step
> 7), as kernel.md §17 records, with five corrections to this plan in
> [§11](#11-corrections-while-building). The second half of
> [phase4_plan.md](phase4_plan.md) §4, its steps 4, 8 and 9, planned against
> the code as 4a left it. What they do was decided there; this plan adds how,
> one step phase 4 didn't foresee (step 1), and two things you added: Tab
> completion (step 3) and clicking in `edit` (step 7). Facts marked *checked*
> were read in the code, and *measured* ones were run; the rest is reasoned.

---

## 1. Where things stand

**4a is committed** (`90fdc52`, then `8d60f04` for `CSTATUS`): `printf`, the
console's escape codes, the prompt file, the file commands, a sorted `ls`,
and Ctrl+C only while a program runs. 1,187 tests pass.

What 4b builds on, and what's in its way:

- **Line input knows four keys.** `con_read_line` reads key edges from HID's
  event queue and handles printable keys, Backspace, Enter and Ctrl+C. Tab,
  the arrows, Home, End, Delete, PgUp and PgDn do nothing *(checked:
  `user/os/kernel.c:342–387`)*.
- **`getkey()` returns characters only,** from HID's character queue, so a
  program can't see Ctrl or a release through it *(checked: `kernel.c:461`)*.
- **Scrolling is slow, and 4b scrolls a lot.** `con_newline` moves the grid
  up a row, then `con_redraw` clears the screen and draws every cell that
  isn't a space *(checked: `kernel.c:190–206`)*. Run on the machine, at the
  README's 2.5 million instructions a second *(measured)*:

  | What | Instructions | Time |
  |---|---|---|
  | one cell: its background, then its glyph | 5,067 | 2 ms |
  | a screen full of glyphs, as `con_redraw` draws one | 968,562 | 0.39 s |
  | the same screen cell by cell, as escape codes draw it | 1,884,986 | 0.75 s |
  | the framebuffer moved up a row with `memmove` | 674,955 | 0.27 s |

  So a screen full of text already takes up to 0.4 s for each row it
  scrolls. Scrollback, `more` and `edit` would all feel it. `memmove` is no
  way out: it already moves a word at a time here *(checked:
  `lib/pigeon/mem.c:38–51`, through `memcpy`)*.
- **The display device fills a buffer in one command,** `FILL`, but can't
  move pixels *(checked: `emulator/devices/display_io.py:149–163`)*.
- **Nothing sends the mouse wheel.**
  - HID's mouse-event byte holds a button number up to 31, and 0–4 are used
    *(checked: `emulator/devices/hid.py:61–76`)*.
  - The pygame client drops the legacy wheel buttons, and only its disc
    picker handles `MOUSEWHEEL` *(checked: `display/display.py:130–132`,
    `:634`, `:717–755`)*.
  - The browser page listens for `mousedown` and `mouseup`, and nothing else
    *(checked: `display/index.html:358–370`)*.
- **Where a click lands.** The browser posts the pointer's position just
  before each press *(checked: `index.html:362–363`)*. The pygame client
  sends positions at most 30 times a second, and none with a press
  *(checked: `display.py:100`, `:712–714`, `:736–738`)*, so a quick click
  can be read where the pointer was up to 33 ms before.
- **Holding a key repeats in the browser, but not in pygame:** the pygame
  client never calls `pygame.key.set_repeat` *(checked)*, so holding
  Backspace or an arrow moves one step.
- **The console knows only `ESC [` sequences.** After an ESC, any other
  character ends the sequence, and what follows prints as text *(checked:
  `kernel.c:217–224`)*.
- **Ending a program from inside a system call already works.** `exit()`
  calls `exec_abort`, which puts back the stack pointer and `F` that
  `exec_call` saved *(checked: `kernel.c:560`, `kernel.asm:48–59`)*.
  `more`'s `q` can take the same path.
- **Break is on or off for whoever runs,** with no setting per program.
  `exec` turns it on around a program, and line input turns it off while it
  waits and back on after *(checked: `kernel.c:342–386`, `:551–556`)*.
- **17 of the 32 system-call slots are used** *(checked:
  `lib/pigeon/syscall.h`)*, and the kernel's `fs.c` has 8 handles
  *(checked: `kernel.c:35`)*.

---

## 2. The goal

When 4b is done:

- a screen of text scrolls in well under a tenth of a second;
- line input has Left, Right, Home, End and Delete, typing anywhere, bash's
  Ctrl+A, Ctrl+E, Ctrl+U and Ctrl+L, and Up and Down through the last 16
  lines;
- Tab completes commands and file names;
- PgUp, PgDn and the mouse wheel look back through the last 100 rows while
  a line is being typed;
- holding a key repeats in both front ends;
- `more FILE` and `more COMMAND` page a screen at a time;
- `edit FILE` edits a file the way nano does, and a click moves its cursor.

---

## 3. Steps

### Step 1. Fast scrolling *(new)*

Phase 4 didn't plan this, but every later step scrolls: scrollback's view,
Tab's list, `more`'s pages and `edit`'s text.

- **A `COPY` command on the display device,** command 5 (Q2). ADDRESS names
  a buffer, checked the way `FILL` checks it. The data window holds a
  destination offset, a source offset and a length, all inside that buffer.
  The device moves the bytes with one slice, as `FILL` does, so a scroll
  costs a few hundred instructions *(estimate)* instead of 0.4 s.
- **`disp_scroll(y, h, dy, bg)` in `display.c`** moves the pixel rows from
  `y` to `y + h` by `dy` rows, up when it's negative, and clears the gap it
  leaves. When the device refuses, it falls back to `memmove`, the way
  `disp_clear` falls back when `FILL` isn't there *(checked: `display.c:87`,
  `:150`)*.
- **The console scrolls with it:** `con_newline` moves the pixels up one
  cell and draws only the new bottom row.
- **Scroll regions, for `edit`:**
  - `ESC [ t ; b r` sets the rows that scroll, and `ESC [ r` puts back the
    whole screen;
  - `ESC [ n S` scrolls the region up n rows, and `ESC [ n T` down;
  - a newline on the region's last row scrolls only the region;
  - tidying after a program puts back the whole screen.

**Tests:**
- `COPY` on the device, and refused for a bad buffer or a range outside it;
- `disp_scroll`, pixel for pixel against what `memmove` gives, both ways;
- the console after scrolling, pixel for pixel the same as a full redraw;
- a region scrolling while the rows outside it stay;
- a console scroll costing under 20,000 instructions, counted rather than
  timed.

### Step 2. Line editing and history

All in `con_read_line`, so every program that reads a line gets them.

- **Keys:**
  - Left and Right, Home and End, Backspace and Delete;
  - typing anywhere, which pushes the rest of the line along;
  - Ctrl+A and Ctrl+E, as Home and End, and Ctrl+U, which clears the line
    (Q10);
  - Ctrl+C, which still throws the line away.
- **Ctrl+L clears the screen but keeps the prompt and the line,** as bash
  does (Q10). The console can't redraw a prompt it didn't write, so it moves
  the prompt to the top instead: the rows above it scroll into scrollback.
  - **Where the prompt starts:** the shell marks it, by printing
    `ESC ] 133 ; A BEL` first, the mark VS Code's and iTerm2's terminals use
    for the same thing. The console learns `ESC ]` sequences, which end at a
    BEL or `ESC \`, or after 64 bytes; the rest are dropped.
  - Blank rows at the mark are skipped, so the blank line before your prompt
    doesn't take the top row.
  - A program that prints no mark gets the row where its input begins moved
    to the top.
- **Wrapping:** a line longer than its row wraps, as it does now. The
  console remembers the row and column where the input began, and moves that
  row up whenever the screen scrolls. After a change it redraws from the
  cursor to the end of the line, about 2 ms a cell *(measured)*.
- **History:** the last 16 lines, 16 × 256 bytes, shared by every program
  that reads lines, and kept only in memory (Q3).
  - Up shows the line before, Down the one after.
  - Down past the newest gives back what you were typing.
  - An empty line, or a repeat of the last one, isn't kept.
- **Typing ahead** still works: keys pressed while a program ran wait in the
  queue, in order, and edit the next line as if typed then.

**Tests:**
- each key, at the start, in the middle and at the end of a line;
- a line wrapping onto a second row and edited there;
- a line typed with a two-line prompt on the bottom row, so the screen
  scrolls under it;
- Ctrl+L with the shell's two-line prompt, with and without a mark;
- an `ESC ]` sequence dropped, and one never ended stopping after 64 bytes;
- history up, down and past both ends, and the typed line coming back;
- `read()` returning exactly what's on the screen.

### Step 3. Tab completion *(yours, Q11)*

In the console, beside line editing, so it works wherever a line is typed.

- **The word before the cursor is completed.** Words are split at spaces, as
  the shell splits them.
- **File and directory names, in any word.** The part after the word's last
  `/` is matched against the names in the directory before it, or in the
  current directory. The kernel lists the directory with its own `fs.c`.
- **Commands in the first word, when the program asks for them.** The shell
  calls a new `setcomplete(dir, builtins)`, slot 17, when it starts:
  `setcomplete("/bin", "cd exit help")`. A first word with no `/` then also
  matches the built-ins and the `.bin` files in `/bin`, without the `.bin`.
  - **This answers Q11's worry.** The shell's rules stay in the shell,
    which tells the console where its commands are, instead of the kernel
    knowing.
  - **Per program:** a program that never calls it gets file names only,
    and the setting ends with the program that set it.
- **One Tab:**
  - one match: the word becomes it, followed by `/` for a directory and a
    space for anything else;
  - several: the word grows to the longest start they share;
  - none: nothing changes.
- **A second Tab in a row, with several matches, lists them under the line,**
  in columns, as zsh does (§10).
  - The prompt and the line stay where they are. The screen scrolls up if
    the list needs the room.
  - A list taller than the room ends with `and 12 more`.
  - The next key clears the list.
- **A name with a space** comes back inside double quotes, which the shell
  already understands *(checked: `sh.c`, `split`)*.
- **Limits:** the first 64 matches are used *(chosen for a 32×12 screen)*.
  When all 8 of `fs.c`'s handles are open, Tab does nothing, rather than
  failing the program.

**Tests:**
- a command from `/bin`, a built-in, a file, and a directory getting its `/`;
- a path in a later word, as in `cat /docs/re` then Tab;
- several matches growing to their shared start, the list on a second Tab,
  and the next key clearing it;
- the list scrolling the screen when the line is on the bottom row, and
  `and N more` when it doesn't fit;
- a name with a space coming back in quotes;
- a program that didn't call `setcomplete` getting file names only;
- no match leaving the line as it was.

### Step 4. Scrollback and the wheel

- **The last 100 rows that scrolled off the top,** characters and looks,
  6.4 KB. Only the whole screen's scroll keeps them, not a region's.
- **While a line is being typed:**
  - PgUp and PgDn move the view 11 rows, and a wheel notch 3 (Q6);
  - the cursor hides, and an inverse marker in the top-right corner says how
    far back you are, such as `-24` (Q5);
  - any other key brings the view back to the bottom, then does what it does.
- **`ESC [ 2 J` keeps the scrollback, and `ESC [ 3 J` empties it,** as xterm
  does. `/bin/clear` prints both, so `clear` empties it (Q4).
- **The wheel:** a notch up is a press and a release of button 5, and down
  of button 6, as phase 4 decided.
  - `hid.py` names them `BUTTON_WHEEL_UP` and `BUTTON_WHEEL_DOWN`.
  - `input.h` names them `ME_WHEEL_UP` and `ME_WHEEL_DOWN`, to compare with
    `ME_BUTTON(e)`. phase4_plan.md called them `MB_`, but the `MB_` names
    are masks for the held-button state.
  - **pygame:** `MOUSEWHEEL`'s `y` becomes that many notches. The legacy
    buttons 4 and 5 stay dropped, since pygame 2 sends them for the same
    notch.
  - **Browser:** a `wheel` listener on the canvas stops the page scrolling
    and adds up `deltaY`, sending a notch every 100 pixels, 3 lines or a
    page. A trackpad's stream of small deltas doesn't become a flood.
- **Key repeat in pygame:** `pygame.key.set_repeat(400, 40)`, close to a
  browser's (Q7).

**Tests:**
- the view moved by PgUp, PgDn and wheel events pushed through HID, and
  brought back by a key;
- the marker;
- 100 rows kept, and a 101st dropping the oldest;
- `ESC [ 3 J`, and `clear`;
- HID carrying buttons 5 and 6 through its queue;
- the browser's wheel adder, run under node like the outbox tests *(checked:
  `tests/test_input.py`)*;
- the pygame wheel mapping as a plain function, and the repeat being set.

### Step 5. `setbreak`, and break per program

What `edit` needs, and what 4a left for 4b.

- **`setbreak(on)`, slot 18,** turns Ctrl+C as the break on or off for the
  program that calls it, and returns what it was.
- **The kernel keeps the setting for each program.** `exec` starts every
  program with break on, and when a program ends, its parent's setting comes
  back. Line input restores the setting it found, instead of turning break
  on.

**Tests:**
- a program with break off gets Ctrl+C as a key and isn't stopped;
- a program it runs starts with break on, and when that ends, break is off
  again;
- once it ends, the next program the shell runs has break on.

### Step 6. Paging in the console, and `more`

As phase4_plan.md step 8 decided: paging lives in the console, so `more` can
page another program's output.

- **`paging(on)`, slot 19,** remembers which program turned paging on. It
  goes off when that program ends.
- **Counting rows:** every row the output moves down counts, and reading a
  line starts the count again. At 11, the console shows `-- more --` in
  inverse on the bottom row and waits inside `write`:
  - Space shows another screen, and Enter one more row;
  - PgUp, PgDn and the wheel look back, as in step 4;
  - `q` stops. Output from the program `more` ran ends that program, through
    `exec_abort`, with a new status, `ENDED_QUIT`, which `more` takes as
    success. For `more`'s own output, from a file, `write` returns a new
    `E_QUIT` and `more` stops.
  - Ctrl+C is the break, as now, and ends the program when `write` returns.
- **Programs nested deeper,** such as `more sh` running `ls`: `q` ends all of
  them above `more`. Tidying then has to close the files of every program it
  ended; today it closes only the current depth's *(checked:
  `kernel.c:482–488`)*.
- **`/bin/more.bin`:** `more FILE…` or `more COMMAND ARGS…`. When the first
  word names a file, the words are paged as files. Otherwise the first word
  is found as the shell finds a program, `/bin` first, and run. With no
  words it prints how to use it.

**Tests:**
- a long file, with Space, Enter and `q`;
- `more ls /bin` with more entries than fit;
- `q` ending the command, with its files closed, and the shell carrying on;
- PgUp while waiting;
- a word that names a file taken as a file, and one that doesn't taken as a
  command.

### Step 7. `edit FILE`: a mini nano

The keys, the screen and the behaviour were decided in phase4_plan.md
step 9. This is how to build it.

- **The screen:** the title bar on row 0 and the shortcuts on row 11, both
  inverse; the text on rows 1–9, set as a scroll region (step 1), so moving
  down a line draws one row, not nine; messages on row 10.
- **Keys:** `edit` turns break off with `setbreak`, then reads key edges with
  `input.h`'s `key_event`, tracking Ctrl as line input does. `getkey` can't
  do this, as it returns characters only.
- **The text is one gap buffer:** the file, with a gap where the cursor is.
  - One `malloc` of twice the file or 16 KB, whichever is more, grown by
    copying when the gap closes.
  - One block, because `mem.c` never joins freed blocks *(checked:
    `lib/README.md`, mem)*, so a block for each line would fragment the heap
    as lines are edited.
  - Files up to 64 KB, as decided.
- **Drawing after a key, only what changed:**
  - typing redraws the rest of the line;
  - moving past the top or bottom scrolls the region and draws one row;
  - only PgUp, PgDn and `^G` redraw all nine rows.
- **The mouse** (Q8):
  - **the wheel** scrolls the text 3 rows;
  - **a left click in the text moves the cursor there.** The cell is
    `mouse_x() / 6` across and `mouse_y() / 9` down *(checked:
    `input.h:19–20`, `kernel.c:48–49`)*, turned into a line and a column
    through the view's scroll, and kept inside the text: past a line's end
    it goes to the end, below the last line to the last line. A click on the
    title, message or shortcut row does nothing.
  - **The pygame client sends the position with each press,** first, as
    the browser does, so a quick click lands where it was made (§1).
- **Saving** writes `FILE~`, removes `FILE`, then renames `FILE~` to `FILE`
  (Q9). A full disk then leaves the old file whole. The rename can't go
  over the old file, since `fs_rename` needs the target gone *(checked:
  `lib/pigeon/fs.h:127`)*.
- **Size:** `stdio`, `string`, `mem`, `input` and `sys`, about 50–70 KB
  *(estimate)*.

**Tests,** in a new `tests/test_edit.py`:
- phase4_plan.md step 9's list;
- the wheel;
- a click in a line, past its end, below the text and on a bar;
- the pygame client's press handler sending the position before the press,
  with its HTTP session replaced by a recorder;
- a save onto a full disk leaving the old file;
- a file with a zero byte, or over 64 KB, refused;
- the drawing for one key, counted in instructions.

### Step 8. Docs and the example disc

- **Docs:**
  - kernel.md §17, with 4b's entry;
  - shell.md: line editing, Tab completion, scrollback and the prompt mark;
  - `lib/README.md`: `disp_scroll`, `setcomplete`, `setbreak` and `paging`;
  - README: the display's `COPY`, and the wheel;
  - phase4_plan.md's status.
- **The example disc:** `/bin/more.bin` and `/bin/edit.bin`, with readme.txt
  mentioning them. `test_project.py` then counts 19 files.

---

## 4. Order and commits

Three parts, each committed on its own (Q1):

| Part | Steps | What you get |
|---|---|---|
| **4b.1** | 1, 2, 3, 4 | fast scrolling, line editing and history, Tab completion, scrollback and the wheel, key repeat in pygame |
| **4b.2** | 5, 6 | break per program, and `more` |
| **4b.3** | 7 | `edit`, with the mouse |

Step 8 comes at the end of each part.

| Step | Needs | Size |
|---|---|---|
| 1. Fast scrolling | — | small to medium |
| 2. Line editing, history | 1 | medium |
| 3. Tab completion | 2 | medium |
| 4. Scrollback, the wheel | 1, 2 | medium; touches both front ends |
| 5. `setbreak` | — | small |
| 6. Paging, `more` | 1, 4 | medium |
| 7. `edit` | 1, 4, 5 | large: the biggest program yet |
| 8. Docs, disc | all | small |

---

## 5. How it will be checked

As in 4a:
- each step's tests, running only the test files a step touches while it's
  built, and the whole suite once at the end of each part;
- deliberate breakages for every piece, each of which must fail a test;
- speed checked by counting instructions, never by the clock, so the tests
  don't depend on how fast the host is.

---

## 6. Risks

- **Line editing on wrapped lines** is where the bugs will be. The row where
  the input starts moves when the screen scrolls, and a two-line prompt on
  the bottom row scrolls before the first key. Step 2's tests start there.
- **Tab's list moves rows the line editor is keeping track of.** Scrolling
  for the list shifts the input's start row, the same as output does, so it
  goes through the same code.
- **A Tab in a large directory reads all of it** through `fs.c`, which may
  take a moment *(not measured)*.
- **`ESC ]` swallows text until it ends.** A program that prints a stray
  `ESC ]` loses up to 64 bytes of what follows, rather than all of it.
- **`q` ends programs from inside `write`.** That's the path `exit()` takes,
  but ending several programs at once is new (step 6).
- **`edit` is the largest program yet, and the most sensitive to drawing
  speed.** A key that redraws a whole row costs about 160,000 instructions,
  65 ms *(from the measured cell)*, so holding a key in the middle of a long
  line will lag a little.
- **The browser's wheel deltas differ by device and browser.** The adder is
  tested under node, but only a real mouse and a trackpad will show whether
  100 pixels feels like one notch.
- **The kernel image grows by about 15 KB:** history and scrollback are
  globals and live in its image, plus the code for editing and completion
  *(estimate)*. That's still far under the 1 MB the boot sector can load.

---

## 7. Files

| File | Steps |
|---|---|
| `emulator/devices/display_io.py`, `lib/pigeon/display.h`, `display.c` | 1 |
| `user/os/kernel.c` | 1–6 |
| `user/os/kernel.asm` | 3, 5, 6 |
| `lib/pigeon/syscall.h`, `sys.h`, `sys.c` | 3, 5, 6 |
| `user/os/bin/sh.c` | 2, 3 |
| `emulator/devices/hid.py`, `lib/pigeon/input.h`, `display/index.html` | 4 |
| `display/display.py` | 4, 7 |
| `user/os/bin/clear.c`; `more.c` and `edit.c` (new) | 4, 6, 7 |
| `user/os/pigeon_compiler_init.txt`, `user/os/readme.txt` | 8 |
| `tests/test_display.py`, `test_kernel.py`, `test_input.py`, `test_project.py`; `test_edit.py` (new) | all |
| docs | 8 |

---

## 8. Not in 4b

- **History saved across boots** (Q3), which can come with phase 5's startup
  script, and **searching in `more`**.
- Everything in phase4_plan.md §6 and §11: redirection, pipes, the boot
  screen and startup script, the serial debug port, and the launcher.

---

## 9. Your answers

Q8 and Q11 were answered here. The rest in the chat on 2026-09-15: *"the
others, recommendation is good!"*

1. ~~**Three commits,** 4b.1 the console, 4b.2 `more` and 4b.3 `edit`, or 4b
   in one?~~

   **Decided (you):** three, as recommended (§4). Each is a working stopping
   point.

2. ~~**Fast scrolling through the display device:** a new `COPY` command,
   like `FILL`, or only faster code in the library?~~

   **Decided (you):** the command, as recommended (step 1). The library's
   `memmove` already moves a word at a time and took 0.27 s.

3. ~~**History:** leave out empty lines and a repeat of the last line? And
   keep it across reboots, in a file such as `/etc/history`?~~

   **Decided (you):** both left out, and memory only for now, as recommended
   (step 2). Saving it can come with phase 5's startup script.

4. ~~**`clear` and scrollback:** should `clear` also empty the scrollback, as
   Linux's `clear` does?~~

   **Decided (you):** yes, as recommended (step 4). `ESC [ 2 J` alone keeps
   it.

5. ~~**A marker while looking back:** an inverse `-24` in the top-right
   corner, or nothing?~~

   **Decided (you):** the marker, as recommended (step 4).

6. ~~**How far the view moves:** 11 rows for PgUp and PgDn, 3 for a wheel
   notch?~~

   **Decided (you):** as recommended (step 4).

7. ~~**Key repeat in pygame:** repeat a held key after 400 ms, then every
   40 ms?~~

   **Decided (you):** yes, as recommended (step 4).

8. ~~**`edit` and the mouse:** the wheel scrolls the text. Should a click
   also move the cursor?~~

   *Recommended then:* the wheel now, and clicks later.

   Answer: add the click also

   **Reply:** Done in step 7: a left click in the text puts the cursor on
   the character clicked, or at the end of that line, or on the last line.
   One thing the click needs outside `edit`: the pygame client sends the
   pointer's position only 30 times a second and never with a press, so it
   will now send the position with each press, as the browser already does.
   Without that, a quick click could land where the pointer was a moment
   earlier.

9. ~~**Saving in `edit`:** through `FILE~` and a rename, or straight over the
   file, as nano does?~~

   **Decided (you):** through `FILE~`, as recommended (step 7).

10. ~~**More keys in line input,** as bash has: Ctrl+A, Ctrl+E, Ctrl+U and
    Ctrl+L?~~

    **Decided (you):** yes, as recommended (step 2). Ctrl+L needed one more
    piece, decided while planning (left to me): the console can't redraw a
    prompt it didn't write, so it moves the prompt to the top instead. It
    finds where the prompt starts from a mark the shell prints,
    `ESC ] 133 ; A`, the same mark VS Code's terminal uses.

11. ~~**Tab completion** of commands and file names?~~

    *Recommended then:* later, since the console would have to list `/bin`
    and the shell's rules for finding programs would live in two places.

    Answer: YEES ADD THE TAB KEY FOR AUTOCOMPLETE

    **Reply:** Added as step 3, in 4b.1, and the worry has a way round it.
    The console completes file names on its own, since the kernel's `fs.c`
    can list any directory. For commands, the shell tells the console where
    they are when it starts, `setcomplete("/bin", "cd exit help")`, so the
    rules stay in the shell. One Tab completes as far as it can; a second
    lists the choices under the line (§10).

---

## 10. Follow-up, decided

Answered in the chat on 2026-09-15.

1. ~~**What a second Tab does** when several names match: A, list them under
   the line, as zsh does, or B, cycle through them, as Windows' `cmd` does?~~

   You: *"A like zsh"*

   **Decided:** A (step 3). The list goes under the line in columns, the
   prompt and the line stay put, and the next key clears it.

---

## 11. Corrections while building

Found while building 4b.1, on 2026-09-15:

1. **Typing ahead doesn't survive a program,** as step 2 said it did. The
   kernel empties the key queues after every program, on purpose, so the Esc
   that closes `graph` never reaches the shell. Step 2 left that alone.
2. **A scroll costs thousands of instructions, not a few hundred** (step 1's
   estimate). Moving the pixels is a handful; drawing the blank row the rest
   are copied from is most of it. `disp_scroll` of a whole screen took 7,121,
   and a console scroll, with its grid of characters and looks, 13,731
   *(measured)*, against up to about a million before.
3. **The console's scroll isn't checked pixel for pixel against a full
   redraw** (step 1's tests): nothing can ask the kernel to redraw in the
   middle of a test. Every scrolled cell is read back as text and in its
   color instead, and `disp_scroll` is checked pixel for pixel against a
   model, with the display device and without.
4. **The kernel grew by 56,392 bytes, not about 15 KB** (§6's estimate).
   About 14 KB is history, scrollback and Tab's names, which live in the
   image as globals; the rest is the code for editing, Tab and looking back.
   At 204,316 bytes it is still far under the 1 MB the boot sector loads.
5. **`edit` is 81,172 bytes, not 50–70 KB** (step 7's estimate). Its
   drawing is within the plan: a key typed in the middle of a line costs
   142,567 instructions *(measured)*, against §6's 160,000.
