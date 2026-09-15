# The shell

> **Status: built,** `user/os/bin/sh.c`. §1's loop, splitting with quotes,
> the lookup, and `cd`, `exit` and `help` came in kernel.md's phase 3; §2's
> prompt file and its colors in phase 4a ([phase4_plan.md](phase4_plan.md)
> step 5); §5's line editing, Tab completion and scrollback in phase 4b.1
> ([phase4b_plan.md](phase4b_plan.md)). The box characters wait for a later
> phase. The shell is
> `/bin/sh.bin`, an ordinary program that the kernel starts at boot
> ([kernel_overview.md](kernel_overview.md)). Decided 2026-09-14.

---

## 1. What it does

**It doesn't draw text or read keys.** The kernel's console does both: it
turns keys into a line, with echo and backspace, and draws everything on the
screen. The shell only works out what a typed line means and runs it:

```c
for (;;) {
    print_prompt();                     // from /etc/shell_header.conf, §2
    read(0, line, 255);                 // the console handles the typing
    argc = split(line, argv);           // "ls /docs" -> {"ls", "/docs"}
    if (argc == 0) continue;
    if (builtin(argc, argv)) continue;  // cd, exit, help
    path = find(argv[0]);               // /bin/ls.bin, then ls.bin here
    if (!path) { say "ls: not found"; continue; }
    status = exec(path, argc, argv);    // ls runs; the shell waits
    if (status != 0) say the error;     // "exited with 1", "crashed"
}
```

- **Splitting:** at spaces, with double quotes grouping words; up to 16 words.
- **Built-ins:** `cd` (it changes the kernel's current directory, so it can't
  be a program), `exit`, and `help`.
- **Finding programs:** a name with no `/` is `/bin/<name>.bin`, then
  `<name>.bin` in the current directory. You never type the `.bin`.
- **`exit`:** the kernel starts the shell again, which also re-reads its
  prompt.

---

## 2. The prompt: `/etc/shell_header.conf`

The shell reads this file when it starts. **The first line is the prompt.**
**A second line, if there is one, is shown once in its place:** the first
prompt after the shell starts. The example disc's file:

```
"\n``GREEN``|-(``BLUE``PGS``GREEN``)-[``RESET````CWD````GREEN``]-(``CSTATUS````GREEN``)\n|-``BLUE``>``RESET`` "
"``GREEN``|-(``BLUE``PGS``GREEN``)-[``RESET````CWD````GREEN``]-(``CSTATUS````GREEN``)\n|-``BLUE``>``RESET`` "
```

looks like this, in green and blue with the status in white, after `cd /docs`:

```
PigeonOS
|-(PGS)-[2:/]-(0)
|-> cd /docs

|-(PGS)-[2:/docs]-(0)
|-> 
```

The `\n` at the start of the first line leaves a blank line between one
command and the next. The second line is the same prompt without it, so
there's no blank line under `PigeonOS` at boot.

| Write | Means |
|---|---|
| `\n` | a new line |
| `\\` | a backslash |
| ``` ``CWD`` ``` | the current directory |
| ``` ``STATUS`` ``` | the last program's exit status |
| ``` ``CSTATUS`` ``` | the same, colored by its sign: white for 0, magenta for a program's exit value, red for an error such as a crash or Ctrl+C. The color stays on after it |
| ``` ``BLACK`` ``` ``` ``RED`` ``` ``` ``GREEN`` ``` ``` ``YELLOW`` ``` ``` ``BLUE`` ``` ``` ``MAGENTA`` ``` ``` ``CYAN`` ``` ``` ``WHITE`` ``` | text in that color from here on |
| ``` ``GREY`` ``` | the console's own light grey ink |
| ``` ``RESET`` ``` | back to the normal color |

- **Everything else is printed as written**, including trailing spaces, so
  `|-> ` keeps the space before your cursor. An unknown ``` ``NAME`` ``` is
  printed as it is, so a typo shows up on screen.
- **Forgiving about editors:** line breaks at the end of the file are
  ignored, and so is a `\r` before a line break. Quotes around a line are
  removed. **Quote a line that ends in a space,** as many editors trim
  trailing spaces.
- **At most two lines of 255 bytes each,** and 1024 bytes in all. A file
  with more is reported when the shell starts, and the built-in prompt is
  used.
- **Once each time the shell starts,** not once each boot: `exit` starts the
  shell again, and it shows the second line again. Once a boot needs the
  kernel to tell the shell it is the first start, as phase 5's `sh -startup`
  will ([phase4_plan.md §11](phase4_plan.md#11-later-phases)).
- **A missing or unreadable file** doesn't stop the shell: it uses its
  built-in prompt, the current directory and `> ` on one line. *Changed while
  building:* this said your first example, `|-PGS ``CWD``\n|-> `, would be
  the built-in prompt. The example disc carries a prompt file instead, so the
  installed system shows it, and a disk with no file gets the one-line
  prompt phase 3 had.
- **The screen is 32×12 characters.** A two-line prompt and a blank line use
  three rows for each command, and a long directory wraps.

### Looking like Kali

Kali's prompt is two lines drawn with box characters, in color:

```
``BLUE``┌──(``GREEN``pigeon``BLUE``)-[``WHITE````CWD````BLUE``]\n``BLUE``└─``GREEN``$``RESET`` 
```

```
┌──(pigeon)-[/docs]
└─$ 
```

The colors work with the design as it is. The box characters `┌ └ ─ │ ├` need
five glyphs added to the console's font, which today has only printable ASCII
(`lib/pigeon/display.c`). The file is written as UTF-8 on the host, and the
console draws those five characters, and `?` for any other non-ASCII one.
Kali's `㉿` doesn't fit a 5×7 glyph, so use `@`. Until the glyphs exist, `|-`
as in your example is the ASCII version.

---

## 3. Putting the file on the hard disk

- **With the installer:** add a line to the project file, rebuild the disc and
  install it. A file that isn't `.c` or `.asm` goes onto the disc as it is, and
  its folders are made for it ([os_cd.md](os_cd.md) §7).

  ```ini
  [files]
  /etc/shell_header.conf = shell_header.conf
  ```

- **Straight into a disk image, from the host:**

  ```bash
  python3 tools/pfs.py mkdir -p --image disks/hdd.img /etc
  python3 tools/pfs.py put --image disks/hdd.img shell_header.conf /etc/shell_header.conf
  ```

- **On the machine itself:** not yet, as there is no text editor.

---

## 4. What this needs from the design

**Nothing structural.** The shell opens and reads the file through the
kernel's `open` and `read` system calls, gets the directory from `getcwd`,
and prints the prompt with `write`, all of which are already planned
(kernel_exec.md §8). Two small additions, both inside the kernel's console:

- **Colors.** `disp_text` already takes a color. The console learns the ANSI
  color codes real terminals use — `ESC [ 3n m` and `ESC [ 0 m` — and the shell
  turns ``` ``BLUE`` ``` into one. Any program can then print in color.
  *Built in phase 4a,* with `ESC [ 7 m` for inverse, `ESC [ 2 J`,
  `ESC [ r ; c H` and `ESC [ K` too ([kernel.md](kernel.md) §12).
- **Five box-drawing glyphs**, only if you want Kali's real `┌──` rather than
  `|-`.

---

## 5. Typing at the prompt

*Built in phase 4b.1* ([phase4b_plan.md](phase4b_plan.md) steps 2–4). The
kernel's console does all of it, so every program that reads a line gets the
same keys, not only the shell.

| Key | Does |
|---|---|
| Left, Right, Home, End | move along the line; Ctrl+A and Ctrl+E are Home and End |
| Backspace, Delete | delete before, or at, the cursor |
| Up, Down | the last 16 lines typed; Down past the newest gives back what you were typing |
| Tab | complete a command or a file name; a second Tab lists the choices under the line |
| Ctrl+U | throw away what's typed |
| Ctrl+L | move the prompt and the line to the top of the screen |
| Ctrl+C | throw the line away |
| PgUp, PgDn, the mouse wheel | look back through the last 100 rows; any other key comes back |

- **Tab completes the word before the cursor.**
  - **In the first word, commands.** When the shell starts, it tells the
    console where they are: the `.bin` files in `/bin`, and the built-ins
    `cd`, `exit` and `help`.
  - **In any other word, or one with a `/`, file and directory names.**
  - One match gets a `/` after a directory and a space after anything else.
    Several grow the word as far as they agree, and a second Tab lists them
    in columns under the line, as zsh does; the next key clears the list.
  - A name with a space comes back in quotes.
- **Ctrl+L finds where the prompt starts** by an invisible mark the shell
  prints first, `ESC ] 133 ; A`, so a prompt of several rows moves up whole,
  and the blank line before it stays behind.
- **While looking back,** a marker in the top-right corner, such as `-24`,
  says how many rows back the view is. `clear` also empties what you can look
  back through.
- **History is kept in memory,** so it starts empty at each boot. An empty
  line, or one repeating the last, isn't kept. A line holds up to 255
  characters.
- **Keys typed while a program runs are thrown away when it ends,** so the
  Esc that closes `graph` never reaches the prompt.

---

## 6. A screen at a time: `more`

*Built in phase 4b.2* ([phase4b_plan.md](phase4b_plan.md) step 6).

```
more /docs/readme.txt       a file, or several, one after another
more ls -l /bin             a command, with its output paged
```

A first word that names a file means files; anything else is found as the
shell finds a program, `/bin` first. After a screen, `-- more --` shows on
the bottom row and waits:

| Key | Does |
|---|---|
| Space | the next screen |
| Enter | one more row |
| PgUp, PgDn, the mouse wheel | look back |
| q | stop: the command, and anything it ran, or the files |
| Ctrl+C | stop the program that is writing, as it would without `more` |

The paging is the console's, not `more`'s, so it works for any command:
`more` turns it on for itself and whatever it runs, and it ends when `more`
ends.
