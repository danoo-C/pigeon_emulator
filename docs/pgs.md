# `pgs`: shell scripts

> **Status: built,** `user/os/bin/pgs.c`, planned in
> [pgs_plan.md](pgs_plan.md) and built to it on 2026-09-16.
> `/bin/pgs.bin` is an ordinary program: `pgs FILE` runs a script, and so
> does a `.pgs` typed at the prompt. Tests: `tests/test_pgs.py`, and the
> kernel's own for [`exec_out`](kernel.md).

---

## 1. A script

```sh
;; /docs/hello.pgs -- run it with `pgs /docs/hello.pgs`, or click it.

$name = world
echo hello $name

$files = $(ls /bin)             ;; what ls printed, in a variable
for $one in $files
    echo "  " $one
end

$i = 0
while $i -lt 3
    echo tick $i
    let $i = $i + 1
end

if -e /etc/explorer.conf
    echo the explorer has its rules
end
```

Three ways to run one:

| | |
|---|---|
| `pgs FILE [ARGS...]` | the plain way |
| `FILE.pgs [ARGS...]` | the shell runs a `.pgs` here as a script |
| a click in the explorer | `/bin/pgs.bin = .pgs` in `/etc/explorer.conf` |

`/etc/startup.pgs`, when it exists, is run by the shell before its first
prompt — **every** shell, the way every shell reads `.bashrc`, since a shell
has no way to know whether it is the first one. So it runs again after
`exit`, and inside the explorer's `Shell here`. Two things follow: a banner in
it greets you every time, and a script that starts a shell will run itself
again inside it, down to the kernel's eight programs.

## 2. Lines

- **One command a line.** No `;`, no `&&`, no line continuation.
- **`;;` starts a comment,** to the end of the line, anywhere outside quotes.
- **A line starting with `#` is a setting,** not a comment: `# stop-on-error`
  is the only one so far. Settings live in the header, before the first
  command, and a word `pgs` doesn't know is a mistake with its line number —
  a silently ignored typo is what the language is built to avoid.
- **A line is a command,** an assignment, a setting, a block word, or blank.

## 3. Words

Split at spaces, with `"` grouping them, and **then each word is expanded on
its own**, so a value with a space in it stays one argument:

```sh
$file = "my notes.txt"
edit $file                  ;; edit gets one argument, not two
```

`\$` is a dollar, `\\` a backslash, `\"` a quote. There are no single quotes.
At most 16 words a command, and the words of one line together at most 16 KB.

## 4. Variables

```sh
$name = world               ;; the rest of the line, expanded
$greeting = hello $name     ;; "hello world"
$out = $(ls /bin)           ;; what a command printed
$empty =                    ;; an empty value, which is not the same as unset
```

- A line whose first word starts with `$` and whose second word is `=` is an
  assignment. The name is not expanded — it is being set.
- Names are letters, digits and `_`, at most 31 characters; 32 variables at a
  time; a value is at most 8 KB, the size of a capture, so a listing fits in
  one. Values live on the heap.
- **A name that was never set stops the script:**
  `hello.pgs:4: no such variable: $nmae`. There is no `set -u` to turn on
  here, and a typo that quietly becomes an empty string is the most expensive
  bug a small language can have.
- **The script's own:** `$0` its path, `$1`…`$9` its arguments, `$#` how many
  there are, `$?` the last command's status — 0, a program's own value, or a
  negative kernel code for a crash or Ctrl+C.
- There is no environment: a program gets what the line gives it. A `cd` in a
  program *does* move the script, since PigeonOS has one current directory.

## 5. `$( )`: what a command printed

```sh
$here = $(pwd)
$names = $(ls /docs)
echo $here has $names
```

The command runs through the kernel's `exec_out`, and what it wrote to
`STDOUT` becomes the value, without the newlines at its end; the newlines
inside it stay, which is what `for` walks. `STDERR` still goes to the screen,
so a program's complaints are not swallowed. A builtin works too — `$(pwd)`,
`$(echo hi)`.

The buffer is 8 KB. Past that the output is cut and the script is told:
`hello.pgs:12: $( ) cut at 8191 bytes: ls`. `$( )` inside `$( )` is refused,
and so is one with no closing bracket. The status is `$?`, as for any
command.

## 6. Blocks

```sh
if <test>
    ...
else                    ;; optional
    ...
end

while <test>
    ...
end

for $name in VALUE      ;; a line of VALUE at a time
    ...
end
```

A **test** is one of:

| | |
|---|---|
| `$a == b`, `$a != b` | text, after expansion |
| `$a -lt 5`, `-le`, `-gt`, `-ge` | whole numbers |
| `-e path`, `-d path`, `-f path` | it exists, is a directory, is a file |
| anything else | a command: true when its status is 0 |

`break` leaves the innermost `while` or `for`. Blocks nest 8 deep. A block
that isn't running is skipped whole, before anything in it is expanded, so an
unset name in a branch that never runs is not a mistake.

**`for` splits its value on newlines**, not spaces, and takes it **once** —
`for $line in $(ls /bin)` runs `ls` one time and walks the names. An empty
value runs the body no times.

## 7. Sums: `let`

```sh
let $i = $i + 1
let $half = ( $a + $b ) / 2
```

Whole numbers, `+ - * / %`, and `( )` written as their own words. A divide by
zero is a mistake with its line, not a crash. Anything that isn't a number is
`not a number: abc`.

## 8. The builtins

| | |
|---|---|
| `echo WORDS...` | with a newline; it is what a capture takes |
| `cd DIR` | the current directory, which every program shares |
| `pwd` | where you are |
| `read $name` | a typed line into a variable |
| `let`, `break`, `if`, `else`, `while`, `for`, `end` | the words above |
| `exit [N]` | end the script with that status |

Everything else is a program, found the way the shell finds one:
`/bin/<name>.bin` first, then `<name>.bin` where you are, with `.bin` added
when it is missing. So a word means the same in a script as at the prompt.

## 9. When something is wrong

- **A mistake in the script** — an unset variable, an unknown setting, a
  block that doesn't add up, a value too long — stops it with
  `hello.pgs:7: no end for this if`, and `pgs` returns 1.
- **A program that fails does not stop the script.** Its status is `$?` and
  the next line runs — unless the header says `# stop-on-error`, and then it
  stops with `hello.pgs:12: stopped: 2` and that status.
- **Complaints go to `STDERR`,** which a capture never takes, so a script
  whose output is being captured can still say what went wrong.
- **Ctrl+C** ends the program the script is waiting for, and then the script.
  That is the way out of a `while 1`.

## 10. `>`, `>>` and `<`

```sh
args one > /out.txt         ;; a program's output into a file
echo hello > note.txt       ;; and a builtin's, which is most of the point
echo world >> note.txt
eater < note.txt            ;; a file as a program's typed input
sort < in.txt > out.txt     ;; both ends at once
```

They work as they do at the prompt ([shell.md](shell.md) §8): whole words
only, `>` writing `file~` and renaming it when the command has ended by
itself, `>>` adding to the end, `STDERR` still reaching the screen. Two
things are the script's own:

- **A builtin redirects too.** `echo hello > note.txt` writes the file, with
  the same `file~` rule. `<` on a builtin is a mistake — none of them reads
  input that way.
- **A value cannot redirect.** `$x = $(ls) > out.txt` looks like a
  redirection and is not one: the whole right-hand side is the value. Rather
  than quietly making the value `... > out.txt`, `pgs` says
  `a value cannot redirect: quote it if you meant the text`. `$x = "a > b"`
  is that text.

## 11. What it is not

No pipes (`a | b`), no `&&`, no `;`, no functions, no arrays, no environment,
no background jobs. A `# graphics` script that owns the screen is
[graphics_plan.md](graphics_plan.md), planned and not built.
