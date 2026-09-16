# The explorer: `/bin/explorer.bin` and `/etc/explorer.conf`

> **Status: final plan, 2026-09-16; every question is decided
> ([§8](#8-your-answers), [§9](#9-follow-ups)). Nothing is built yet.** A file
> explorer you drive with the mouse, which reads `/etc/explorer.conf` to learn
> what opens what, runs that command through the kernel's `exec`, and is there
> again when the program ends. Facts marked *(checked)* were read in the code;
> the rest is reasoned.

---

## 1. What you asked for

From the chat and your answers, 2026-09-16:

- **A file explorer,** which loads `explorer.conf`, and the conf says what to
  do with each kind of file.
- **The conf's lines look like** `EXEC = .bin`, `/bin/img.bin = .bmp`,
  `/bin/edit = *.*`: the action on the left, what it matches on the right.
- **The action is a command,** and the clicked file's name is appended to it:
  `/bin/img.bin -s = .bmp` runs `img -s pigeon.bmp` (Q2).
- **`EXEC` is the explorer's own function, not a program:** it does what
  typing the `.bin` file's name at the shell's prompt does.
- **`*.*` goes last,** and catches every file no earlier line named — in the
  example, everything else opens in `edit`. A rule after it is a warning (Q3).
- **Left-clicking in a folder pops a menu** with `New file...`, `New
  folder...` and `Shell here` on it (Q1).
- **`Open with...`** on a file is a box you type a program name or path into,
  found the way the shell finds one (Q10).
- **A program the explorer started comes back to the explorer when it ends,**
  not to the shell — with its output still on the screen and
  `[ press any key ]` under it (Q4).
- **A mistake in the conf is reported, not fatal** (Q7); the explorer lives in
  `/bin`, and `files.bin` stays where it is (Q8, Q9).

---

## 2. Where things stand

- **A program can run another program and carry on:** `exec(path, argc, argv)`
  loads it, waits, and returns its status *(checked: `lib/pigeon/sys.h:42`,
  `kernel.c` `k_run`)*. That is all "back to the explorer, not the shell"
  needs — the shell does the same thing, and its prompt comes back after `ls`
  *(checked: `user/os/bin/sh.c:270`)*.
- **A child is loaded above its parent's heap,** and at most 8 programs run at
  once, the kernel counted *(checked: `kernel.c:47`, `kernel.c:1299`)*. The
  chain here is kernel → `sh` → explorer → `edit`, four of the eight, and
  programs live in `0x01000000`–`0x07F00000`, about 111 MB, so the explorer
  holding a screen-sized buffer costs a child nothing *(checked:
  `kernel.c:44`)*.
- **Ctrl+C ends the child, not its parent:** every program starts with the
  break on, and the kernel gives the parent its own break setting back when
  the child ends *(checked: `kernel.c:1330`, `kernel.c:1348`)*. So Ctrl+C in a
  program the explorer started lands back in the explorer, like Esc.
- **After every program the kernel tidies up:** it closes the handles the
  program left, stops all 16 timers, empties the key and mouse queues, points
  the display back at the screen, mounts the disk again and puts the current
  directory back, and redraws its console *(checked: `kernel.c` `k_tidy`)*.
  Two consequences for the explorer: **its screen is gone** when a child ends
  — the console is drawn over it — and **a child's `cd` can't move the
  explorer**.
- **The console and the explorer share one screen.** Anything a program
  prints lands on the same 32×12 grid of text the explorer draws its list on,
  so a child that prints (`ls`, `cat`) leaves its output on top of the
  explorer's pixels until the explorer draws again. There is no syscall that
  says whether a program printed anything *(checked: `syscall.h`)*.
- **The shell's rule for a name** is `/bin/<name>.bin` first, then `<name>.bin`
  where you are, with `.bin` added when it's missing *(checked: `sh.c:81–93`)* —
  so `/bin/edit` in your example resolves the same way the prompt does.
- **The shell splits a line at spaces,** double quotes keeping the spaces in a
  word, into at most 16 words *(checked: `sh.c:39–73`)*. A rule's command is
  split the same way (§4.1).
- **`argv[0]` is the word you typed:** the shell passes the first word as it
  was written, and the kernel passes the splash `splash`, not
  `/bin/splash.bin` *(checked: `sh.c:270`, `kernel.c:1569`)*.
- **A config file of this shape is already parsed twice:** `/etc/boot.conf` in
  the kernel (`key = value`, `#` comments, `\r` and spaces trimmed, at most
  1,024 bytes, every mistake reported with its line number) *(checked:
  `kernel.c` `k_read_boot_conf`)*, and `/etc/shell_header.conf` in the shell
  *(checked: `sh.c:111`)*. The explorer's parser is the same one again, with
  the key and value swapped in meaning.
- **A program can read the mouse itself:** `mouse_event()` for clicks and the
  wheel, `mouse_x()`/`mouse_y()` for where the pointer is, straight from the
  HID device on channel 3 — no kernel call *(checked: `lib/pigeon/input.h`)*.
  `edit` already does exactly this, with `setbreak(0)` so Ctrl+C is its own
  *(checked: `user/os/bin/edit.c:819–831`, `edit.c:957`)*.
- **The screen is 192×108,** which is 32 columns by 12 rows of the 5×7 font in
  its 6×9 cell *(checked: `display.h:103`, `user/files.c:58–67`)*.
- **There is a file browser already:** `user/files.c`, on the disc as
  `/bin/files.bin`. It is keyboard-only, and it talks to `<pigeon/fs.h>`
  directly — it mounts the disk itself, from before the kernel existed
  *(checked: `user/files.c:1–30`)*. Its layout, its sorted listing, its
  name-prompt and its confirm box are all worth lifting; its plumbing is not.
- **The programs the explorer will start take a path and end on Esc:**
  `img [-s] FILE.bmp` *(checked: `user/os/bin/img.c:25–33`)*, `edit FILE`,
  which creates the file when you save *(checked: `edit.c:609`)*.
- **The tests can click:** `hid.set_mouse_pos(x, y)` then
  `push_mouse_event(0, True/False)`, as `tests/test_edit.py` does *(checked:
  `tests/test_edit.py:266`)*, and `tests/test_kernel.py` exports the harness
  other test files import.
- **Adding a program to the disc is three lines:** `pigeon_compiler_init.txt`,
  the `INSTALLED` list in `tests/test_install.py:37`, and the one count
  written out in `tests/test_project.py:304`, `26 files to copy` *(checked)*.

---

## 3. The goal

`/etc/explorer.conf`:

```ini
# /etc/explorer.conf -- what the explorer opens each file with.
# Each line is a command; the file you clicked is added to the end of it.
# The first line whose pattern matches the name wins, so *.* goes last.
EXEC            = .bin        # run it, as typing its name at the prompt does
/bin/img.bin -s = .bmp        # img -s pigeon.bmp
/bin/edit.bin   = *.*         # everything else
```

The screen, at `2:/docs`:

```
+--------------------------------+
|2:/docs              4 items    |   path, and how many entries
|--------------------------------|
|..                              |
|bmp/                      <dir> |
|readme.txt               1234 B |
|note.txt                   56 B |   the selected row is inverse
|pigeon.bmp               6.2 KB |
|                                |
|         +--------------+       |   the menu, where you clicked
|         | New file...  |       |
|         | New folder...|       |
|         | Shell here   |       |
|--------------------------------|
|edit note.txt                   |   what it did, or what went wrong
+--------------------------------+
```

Clicking `pigeon.bmp` twice runs `img -s pigeon.bmp`; Esc in `img` leaves the
screen as `img` left it, `[ press any key ]` under it, and the next key brings
this list back. Clicking `note.txt` twice runs `edit note.txt`. Clicking a
`.bin` runs it as the prompt would. Clicking empty space opens the menu above.

---

## 4. The design

### 4.1 `/etc/explorer.conf`

One rule a line, `command = pattern`, which is `boot.conf`'s parser with the
halves meaning something new:

- `#` starts a comment, to the end of the line; blank lines are skipped;
  spaces, tabs and a `\r` an editor left are trimmed off both halves.
- At most **1,024 bytes** and **32 rules**, as `boot.conf` is capped *(§2)*.
- **The command** is either `EXEC` — the explorer's own, §4.2 — or a program
  and its arguments, written as you'd type them at the prompt:
  `/bin/img.bin -s`, `/bin/edit`, `edit`. It is split at spaces the way the
  shell splits a line, quotes included, into at most **8 words** *(§2)*; the
  first is the program, and the shell's rule finds it *(§2)*. The program is
  only looked for when the rule fires, so a conf naming something that isn't
  installed costs nothing until you click one of its files.
- **The pattern** (F3) is one of
  - `.bin`, `.bmp`, `.txt` — a name that ends in it, letters compared without
    case, so `PIGEON.BMP` matches `.bmp`;
  - `*.*` — every file. It goes last: see the warning below.
- **Rules are tried top to bottom and the first match wins.** Nothing is
  sorted or reordered: the file's order is the rule's priority, which is what
  makes "`*.*` goes to the end" mean what you'd expect.
- **A rule after `*.*` can never fire,** so the explorer says so —
  `explorer.conf:5: after *.*, never used` — and keeps it, and the rest of the
  file, exactly as written. A warning, not an error (Q3).
- **A line the parser can't read at all** — no `=`, an empty half, a pattern
  that is neither `.ext` nor `*.*`, a command of more than 8 words, or the
  33rd rule — is reported the same way, with its line number, and skipped.
  Every other line still counts (Q7).
- **Directories are the explorer's own** — clicking one opens it in the list —
  and no rule applies to them (Q6).
- **A file no rule matches** isn't opened; the status line says
  `no rule for note.zzz`.

### 4.2 `EXEC`, and what a rule runs

**The clicked file's name is appended to the command, as its last argument**
(Q2), and the explorer keeps the kernel's current directory on the folder it
is showing, so that name is the plain one — `pigeon.bmp`, not
`2:/docs/pigeon.bmp` (Q5).

- **`/bin/img.bin -s = .bmp`** on `pigeon.bmp` runs
  `exec("/bin/img.bin", 3, {"/bin/img.bin", "-s", "pigeon.bmp"})`. `argv[0]`
  is the rule's first word as written, because that is what typing it would
  have given *(§2)*.
- **`EXEC = .bin`** on `hello.bin` runs `exec("hello.bin", 1, {"hello.bin"})`:
  the file is the program, and there is nothing to append. `EXEC` takes no
  arguments of its own — `EXEC -x` is a warning line (§4.1), decided with the
  rest and easy to relax later.
- **`EXEC` on something that isn't a program file** ends with the kernel's own
  `not a program`, shown on the status line, the same words the shell shows.
- **The status** `exec` returns is shown: `0` is silent, a positive number is
  `edit: exit 2`, a negative one is `sys_strerror`'s words —
  `no such file or directory`, `not a program`, `Ctrl+C stopped it`.

### 4.3 The screen

`user/files.c`'s layout, in the same 32×12 cells, derived from `DISPLAY_W/H`
and `GLYPH_W/H` rather than typed *(§2)*:

- **Row 0:** the current directory, and how many entries it has.
- **Rows 1–9:** the entries, sorted directories first then by name, `..` on
  top unless we're at the root; each row is the name, and the size or `<dir>`
  right-aligned. The selected row is inverse. A name too long for the row is
  cut with a `~`.
- **Row 11:** the status line — what it just did, or the error.
- **Drawing goes straight to the screen, with no back buffer,** as `files.c`
  does, and only when something changed. A back buffer would be a page flip
  the kernel undoes after every child *(§2, `k_tidy`)*, for a list that is
  redrawn a few times a second at most.

Keys, `files.c`'s where they exist: Up/Down, PgUp/PgDn, Home/End move; Enter
opens; Backspace goes to the parent; Esc quits; `F5` reloads; `?` shows the
keys; `n` new file, `d` new directory, `x` delete, `F2` rename, `o` open
with — each of them the same code the menu items call.

### 4.4 The mouse and the menu

- **The wheel** scrolls the list; **a click on a row** selects it; **a second
  click on the row already selected** opens it, as does Enter.
- **A click on empty space** in the list — below the last entry — opens the
  **folder menu** where you clicked (Q1):

      New file...      a name, then the rule that matches it: edit
      New folder...    a name, then mkdir
      Shell here       /bin/sh.bin, in this folder

  Reloading is `F5` and leaving is Esc, so neither takes a row in a
  three-item menu (F2).
- **A right-click on a row** opens the **entry menu** for it:
  `Open`, `Open with...`, `Rename...`, `Delete` — and for a directory `Open`
  is "go into it".
- **`Open with...`** (Q10) asks for a command on the status line, the way
  `Rename...` asks for a name, and runs it as a rule that matched just this
  once: what you type is split at spaces, the file's name is appended, and the
  first word is found the way the shell finds a program — `/bin` first, then
  this folder *(§2, F1)*. `img -s` typed there does what
  `/bin/img.bin -s = .bmp` would have.
- **The menu** is a framed box, its items one per row, the item under the
  pointer highlighted; a click runs it, Esc or a click outside closes it, and
  Up/Down/Enter work it from the keyboard. It is drawn over the list, and the
  list is drawn again when it closes — nothing underneath is saved, since the
  explorer can redraw itself whenever it likes. A box near the bottom or the
  right edge is nudged back on screen.
- **`New file...`** asks for a name, then opens it with the rule that matches
  that name — a plain name goes to `*.*`, which in your conf is `edit`, and
  `edit` creates the file when you save it *(§2)*. **`New folder...`** asks
  for a name and calls `mkdir`. **`Rename...`** asks and calls `rename`.
  **`Delete`** asks `delete note.txt? y/n` first, then `remove`, or `rmdir`
  for an empty directory. Every one of them says what went wrong on the status
  line instead of swallowing it, the way `files.c` reports `fs_strerror`
  *(§2)*.

### 4.5 Running a program, and coming back

The whole of "it comes back to the explorer" is that the explorer calls
`exec` and keeps running *(§2)*. What it has to do around the call:

1. **Before:** `chdir` to the folder being shown, if it isn't there already.
2. **The call itself:** `exec(program, argc, argv)`.
3. **After, always (Q4):** whatever the child left on the screen stays — its
   own drawing, or its console output with the kernel's console drawn over
   the explorer's list *(§2)* — and the explorer prints `[ press any key ]`
   on the console under it. The next key reloads the folder, since the child
   may have made or removed files, and draws the list again. So `ls` through
   `EXEC` reads like the shell running it, and `img` holds its picture one key
   longer.
4. **`setbreak(0)`** while the explorer itself is running, as `edit` does, so
   Ctrl+C doesn't kill the explorer; children get the break back on from the
   kernel anyway *(§2)*.

### 4.6 The disc

- `user/os/bin/explorer.c` → `/bin/explorer.bin`, and
  `user/os/etc/explorer.conf` → `/etc/explorer.conf`, both added to
  `user/os/pigeon_compiler_init.txt`. It is an ordinary program in `/bin`
  (Q9): `explorer` at the prompt runs it, and nothing stops
  `startup = /bin/explorer.bin` in `boot.conf` for anyone who wants to boot
  into it — the disc's own `boot.conf` keeps `startup = /bin/sh.bin`.
- **`/bin/files.bin` stays** where it is (Q8).
- `tests/test_install.py`'s `INSTALLED` gains both, which is where that test
  gets its count; `tests/test_project.py:304` has the count written out, and
  goes from `26 files to copy` to 28 *(checked)*.
- `/docs/readme.txt` gains a line: `explorer` at the prompt.
- **Without `/etc/explorer.conf`** the explorer uses the three built-in rules
  of §3 — `.bin` to `EXEC`, `.bmp` to `img -s`, everything else to `edit` —
  and says so on the status line (Q7).

---

## 5. Steps

Each step ends with its tests passing, in `tests/test_explorer.py`, which
imports `test_kernel.py`'s harness the way `test_edit.py` does *(§2)*.

1. **The list.** `explorer.c` draws the folder and walks it with the keyboard:
   sorted entries, `..`, scrolling, the header and the status line. No conf,
   no `exec`. *Tests:* the rows for a made-up tree, scrolling, entering and
   leaving directories, an empty directory, a directory that won't open.
2. **The mouse.** Wheel, click-to-select, second-click-to-open.
   *Tests:* clicks land on the row under them, the wheel scrolls, a click
   past the last entry selects nothing.
3. **`explorer.conf`.** The parser, the rules, `EXEC`, commands with
   arguments, the appended file name, the `[ press any key ]` pause, the
   reload and the redraw after a child.
   *Tests:* each rule kind fires, with the file last on its command line; the
   first match wins; `*.*` catches the rest; a name no rule matches is
   refused; a rule after `*.*` warns and the rest of the file still works; a
   bad line is reported and skipped; no conf means the built-in rules; `EXEC`
   on a text file says `not a program`; `ls` through `EXEC` leaves its output
   on screen until a key; after `edit` writes a file the list shows it.
4. **The menu.** The box, the items, the name prompt, the confirm,
   `Open with...`, and the things they do.
   *Tests:* a click on empty space opens the folder menu; `New folder...`
   makes one; `New file...` opens `edit` for a typed name; `Shell here` runs
   the shell and `exit` comes back; `Open with...` runs what you type with the
   file appended; `Delete` asks first, and `n` leaves the file alone; Esc
   closes the menu; a menu near an edge stays on screen.
5. **The disc.** The project file, `INSTALLED`, the counts, the readme.
   *Tests:* the install and project tests, updated.
6. **The docs.** `docs/explorer.md`, as `docs/shell.md` documents the shell,
   and this plan's §10 "as built".

A plain `python3 -m pytest tests/` at the end, not after each step.

---

## 6. Risks

- **The console draws on the explorer's screen** *(§2)*. The pause in §4.5 is
  the answer, and every test that runs a child has to expect it.
- **`EXEC` on a big program** could run out of room above the explorer —
  `E_NOMEM`, "no room to run it". Not at today's sizes: 111 MB of program
  space against programs of tens of kilobytes *(§2)*.
- **Explorer → shell → explorer → ...** is four deep already; the kernel stops
  at eight with `too many programs running`, which the status line will show
  like any other error *(§2)*.
- **Two file browsers.** `/bin/files.bin` stays what it is, a pre-kernel demo
  of `fs.h`; the explorer is the one that runs programs (Q8).
- **32 columns is narrow.** Long names are cut, the menu is at most ~16
  columns wide, and `Open with...` shares the status line with everything else
  the explorer has to say.
- **Names with spaces** work everywhere here — the appended name is one `argv`
  entry, never split. A *command* in the conf with a space in its path needs
  the quotes the shell's splitter already understands *(§2)*.

---

## 7. Not in this plan

- **Anything that copies or moves files** — no cut/copy/paste, no
  drag-and-drop. `cp` and `mv` exist at the prompt.
- **Several patterns on one line**, such as `/bin/img.bin = .bmp, .img`, and
  whole-name patterns such as `= readme.txt` (F3).
- **A per-folder conf** (`.explorer.conf` in the directory you're looking at).
- **Icons, columns, or a second pane.**
- **Watching the disk:** the list reloads when you ask it to, when you come
  back from a program, and when the explorer changes something itself.

---

## 8. Your answers

Answered in this file on 2026-09-16.

1. ~~**The left click.** You said left-clicking in a folder pops the menu.
   Which did you mean? (a) Click empty space in the list → the folder menu
   (New file..., New folder..., Shell here); click a row → select it, a second
   click on it opens it; right-click a row → that entry's menu. (b) Click
   *any* row that is a directory → a menu whose first item is `Open`. (c)
   Something else.~~

   Answer: i want new folder, new file (edit), and yeah open shell here

   **Decided (you):** the folder menu is `New file...`, `New folder...` and
   `Shell here`, and nothing else (§4.4).
   **Decided (left to me):** (a) for the clicks themselves, since your answer
   names the menu that empty space opens — a click selects, a second click on
   the selected row opens, and right-click is where a file's own actions are
   (§4.4). `Reload` and `Exit` are `F5` and Esc rather than menu items (F2).

2. ~~**The pattern side.** Is `.ext` and `*.*` enough, or do you also want
   several patterns on a line, a whole name, a bare `*`, or arguments in the
   action (`/bin/img.bin -s = .bmp`)?~~

   Answer: the file is basically command + the filepath appended at the end.

   **Decided (you):** the left half is a **command**, not just a program, and
   the clicked file's name is appended to it as the last argument (§4.2). So
   `/bin/img.bin -s = .bmp` runs `img -s pigeon.bmp`, and the disc's conf uses
   it.
   **Reply:** that settles the action side; the pattern side stays `.ext` and
   `*.*` only, matched without case (F3). Your example's `.img` is
   `.bmp` on the disc — that is the extension `img` reads *(checked)*.

3. ~~**A rule after `*.*`,** which can never fire: ignore it silently, report
   it and keep going, or refuse the whole conf?~~

   Answer: just a warining

   **Decided (you):** a warning with its line number, and the file is used as
   written (§4.1).

4. ~~**After a program ends,** always pause with `[ any key ]`, never pause, or
   let a rule say?~~

   Answer: well, its fine it can show an optput shell like thing and press any
   key to continue after it ends.

   **Decided (you):** always. What the program left — its own screen, or its
   console output — stays up with `[ press any key ]` under it, and the next
   key brings the list back (§4.5).

5. ~~**The argument a program is given:** the plain name, or the full path?~~

   Answer: yeah, the suggestion

   **Decided (you):** the plain name, with the explorer's folder as the
   current directory (§4.2).

6. ~~**Directories:** always the explorer's own, or can the conf say what
   opens one?~~

   Answer: yeha, explorer itself has its own

   **Decided (you):** the explorer's own, with no rule for them (§4.1).

7. ~~**A broken `explorer.conf`:** report the bad line and carry on, or refuse
   to run? And with no conf at all: built-in rules, or nothing?~~

   Answer: report

   **Decided (you):** report the line and carry on with the rest (§4.1); with
   no file at all, the three built-in rules (§4.6).

8. ~~**`/bin/files.bin`:** leave it on the disc beside the explorer, or drop
   it?~~

   Answer:leave it there.

   **Decided (you):** it stays (§4.6).

9. ~~**Where the explorer sits.** A program you run from the prompt, or also a
   `startup` for `boot.conf`?~~

   Answer: explorer will be in the bin directory

   **Decided (you):** `/bin/explorer.bin`, an ordinary program (§4.6). Nothing
   extra is needed for `startup = /bin/explorer.bin` to work for anyone who
   wants it; the disc's `boot.conf` still starts the shell.

10. ~~**The menu's items.** Anything to add or drop? `Open with...` needs a
    list to choose from.~~

    Answer: lets do open with, just a textbox where you type in the program
    name or path, same rule as in the shell, look in the CWD and then /bin

    **Decided (you):** `Open with...` is a typed command, not a list, found
    the way the shell finds a program (§4.4). What you type is a command like
    a rule's, so `img -s` works there too.
    **Reply:** the shell's own rule looks in `/bin` **first** and then where
    you are *(checked: `sh.c:81–93`)*, the other way round from what you
    wrote; F1 settled it as the shell's order.

---

## 9. Follow-ups

All three answered on 2026-09-16, in the chat.

- **F1. Where `Open with...` looks for a program.** ~~You said "same rule as in
  the shell, look in the CWD and then /bin", but the shell looks in `/bin`
  first and then the folder you're in *(checked: `sh.c:81–93`)*. Which?~~

  Answer: first look in the /bin directory.

  **Decided (you):** the shell's own order — `/bin/<name>.bin` first, then
  `<name>.bin` in the folder you're looking at, `.bin` added when it's
  missing. So `Open with...` and the prompt can never run different programs
  from the same word (§4.4), and a rule's command in `explorer.conf` is found
  the same way (§4.1).

- **F2. `Reload` and `Exit` on the folder menu.** ~~You named three items, so
  the menu has three; `F5` reloads and Esc leaves. On the menu too?~~

  Answer: i dont care.

  **Decided (left to me):** left off. The folder menu stays the three you
  named; `F5` reloads and Esc leaves (§4.4). Easy to add later — each is one
  line in the menu's table.

- **F3. The pattern side, still open from Q2.** ~~`.ext` and `*.*` only, or
  also several on a line (`= .bmp, .img`) and whole names
  (`= readme.txt`)?~~

  Answer: i dont care.

  **Decided (left to me):** `.ext` and `*.*` only, matched without case
  (§4.1). Nothing on the disc needs more, and the parser has one place to
  grow when something does (§7).
