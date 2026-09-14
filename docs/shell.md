# The shell

> **Status: a simple shell is built,** `user/os/bin/sh.c`, in kernel.md's
> phase 3: §1's loop, splitting with quotes, the lookup, and `cd`, `exit`
> and `help`, with the current directory as its prompt. §2's prompt file and
> the colors are not built yet. The shell is `/bin/sh.bin`, an ordinary
> program that the kernel starts at boot ([kernel_overview.md](kernel_overview.md)).
> Decided 2026-09-14.

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

The shell reads this file when it starts, and the whole file is the prompt.
Your example:

```
|-PGS ``CWD``\n|-> 
```

looks like this, after `cd /docs`:

```
|-PGS /docs
|-> ls
```

| Write | Means |
|---|---|
| `\n` | a new line |
| `\\` | a backslash |
| ``` ``CWD`` ``` | the current directory |
| ``` ``STATUS`` ``` | the last program's exit status |
| ``` ``BLUE`` ``` ``` ``RED`` ``` ``` ``GREEN`` ``` ``` ``YELLOW`` ``` ``` ``WHITE`` ``` ``` ``GREY`` ``` | text in that color from here on |
| ``` ``RESET`` ``` | back to the normal color |

- **Everything else is printed as written**, including trailing spaces, so
  `|-> ` keeps the space before your cursor. An unknown ``` ``NAME`` ``` is
  printed as it is, so a typo shows up on screen.
- **Forgiving about editors:** a line break at the very end of the file is
  ignored, and quotes around the whole text are removed.
- **At most 255 bytes.**
- **A missing or unreadable file** doesn't stop the shell: it uses your
  example above as its built-in prompt.
- **The screen is 32×12 characters.** A two-line prompt uses two rows, and a
  long directory wraps.

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
- **Five box-drawing glyphs**, only if you want Kali's real `┌──` rather than
  `|-`.
