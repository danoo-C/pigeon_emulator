# The explorer

> **Status: built,** `user/os/bin/explorer.c` and `user/os/etc/explorer.conf`,
> planned in [explorer_plan.md](explorer_plan.md) and built to it on
> 2026-09-16. `/bin/explorer.bin` is an ordinary program: `explorer` at the
> prompt runs it, Esc comes back. Tests: `tests/test_explorer.py`.

---

## 1. What it is

A file explorer you drive with the mouse. It draws the folder you are in,
opens what you click with the program `/etc/explorer.conf` names for it, and
is still there when that program ends.

```
+--------------------------------+
|2:/docs              4 items    |   where you are, and how many entries
|--------------------------------|
|>..                             |   the selected row is marked and lit
| bmp/                     <dir> |
| readme.txt              1234 B |
| note.txt                  56 B |
| pigeon.bmp               6.2 K |
|--------------------------------|
|enter open   ? keys   esc quit  |   what it did, or what went wrong
+--------------------------------+
```

Directories come first, then files, both by name, with `..` on top unless
you are at the root. A name too long for its row is cut with a `~`. The
right column is the size, or `<dir>`. With more entries than rows there is a
scrollbar in the last pixel column; past 128 entries the header's count gains
a `+` and the rest are not shown.

## 2. Keys and mouse

| | |
|---|---|
| Up, Down, PgUp, PgDn, Home, End | move |
| Enter | open the selected entry |
| Backspace | the folder above |
| Esc | quit |
| F5 | read the folder again |
| `?` | the keys, on screen |
| `n`, `d` | a new file, a new folder |
| F2, `x` | rename, delete |
| `o` | open the selected file with a command you type |
| wheel | scroll, three rows a notch |
| click | select; a second click on the selected row opens it |
| click on empty space | the folder's menu |
| right-click a row | that entry's menu |

The **folder menu** is `New file...`, `New folder...` and `Shell here`. The
**entry menu** is `Open`, `Open with...`, `Rename...` and `Delete`. Both open
where you clicked, snapped to the rows and columns the list uses, and are
worked with the mouse or with Up/Down/Enter; Esc, or a click outside, closes
one. A menu near an edge is nudged back on screen.

`New file...` asks for a name and opens it with the rule that matches it --
with the conf below, that is `edit`, which makes the file when you save it.
`Delete` asks `delete note.txt? y/n` first. Everything that can go wrong --
a name that exists, a folder that isn't empty, a disk that is full -- is one
line on the status line, in the kernel's own words.

## 3. `/etc/explorer.conf`

One rule a line, `command = pattern`:

```ini
EXEC            = .bin        # run it, as typing its name at the prompt does
/bin/img.bin -s = .bmp        # img -s pigeon.bmp
/bin/edit.bin   = *.*         # everything else
```

- **The command** is what you would type at the prompt, and **the file you
  opened is added to the end of it**. `/bin/img.bin -s` on `pigeon.bmp` runs
  `img -s pigeon.bmp`. Its first word is found the way the shell finds a
  program -- `/bin/<name>.bin` first, then the folder you are in, with `.bin`
  added when it is missing -- so a word means the same here as at the prompt.
  At most 8 words, quotes grouping them as the shell's do.
- **`EXEC`** is the explorer's own, not a program: it runs the file itself,
  exactly as typing its name at the prompt would. It takes no arguments.
- **The pattern** is `.ext`, a name that ends in it with either case, or
  `*.*`, which matches every file. Rules are tried top to bottom and the
  first match wins, so `*.*` belongs last.
- **Directories are the explorer's own.** No rule applies to them; clicking
  one opens it in the list.
- **A file no rule matches** isn't opened: `no rule for note.zzz`.
- **Mistakes are said, not fatal.** A line with no `=`, an empty half, a
  pattern that is neither form, a command over 127 bytes, or the 33rd rule is
  `conf:3: ...` on the status line and is skipped. A rule after a `*.*`, which
  can never fire, is said and kept -- the file is used as you wrote it. Only
  the first complaint is shown, with `(+)` when there were more.
- **With no conf file at all** the three rules above are what it uses, and
  the status line says `no /etc/explorer.conf`.

## 4. Running a program

The explorer calls the kernel's `exec` and waits, as the shell does, so it is
still running underneath and gets control back however the program ends --
returning, `exit()`, a fault, or Ctrl+C, which ends the program and not the
explorer ([kernel_exec.md](kernel_exec.md)).

What you see then is the **console**: the kernel draws it over the explorer's
screen after every program ([kernel.md](kernel.md) §11, `k_tidy`), so what
the program printed is there, with `[ press any key ]` under it. The next key
-- or a click -- brings the list back. That is why `ls` through `EXEC` reads
like the shell running it, and why a program that draws, such as `img`, holds
its picture only while it runs.

Two things are put back before the list is drawn again: the folder is read
once more, since the program may have made or removed files, and the explorer
goes back to its own directory. There is one current directory in PigeonOS
and every program shares it, so a shell started with `Shell here` can leave
it anywhere.

`Shell here` is `EXEC` on `/bin/sh.bin`: the shell runs in the folder you are
looking at, and `exit` comes back to the explorer.

## 5. Where it sits

- `/bin/explorer.bin`, from `user/os/bin/explorer.c`, and
  `/etc/explorer.conf`, from `user/os/etc/explorer.conf`; both are on the
  installation disc (`user/os/pigeon_compiler_init.txt`).
- `explorer` at the prompt starts it in the folder you are in, and
  `explorer /docs` starts it there.
- Nothing stops `startup = /bin/explorer.bin` in
  [`/etc/boot.conf`](phase6_plan.md) for a machine that should boot into it;
  the disc's own `boot.conf` starts the shell.
- `/bin/files.bin` (`user/files.c`) stays what it always was: the pre-kernel
  demo of `<pigeon/fs.h>`, keyboard-only, mounting the disk itself. The
  explorer is the one that runs programs.

## 6. How it draws

Straight to the screen, and only when something has changed -- no back
buffer. The console draws at `DISPLAY_START` and a program the explorer
starts has to be able to print where you can see it, so the display is left
pointing where the kernel left it. The layout comes from `DISPLAY_W/H` and
the font, never typed: 32 columns by 12 rows of a 6x9 cell, the same way
`user/files.c` derives its own, so a change of resolution or font moves
everything together.
