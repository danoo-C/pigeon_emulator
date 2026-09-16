# `pgs`: shell scripts for PigeonOS

> **Status: final plan, 2026-09-16; every question is decided
> ([§8](#8-your-answers), [§9](#9-follow-ups)). Nothing is built yet.** A small scripting language,
> `/bin/pgs.bin`, running `.pgs` files: variables, commands, the output of a
> command captured into a variable, and enough control flow to make those
> worth having. The capture is the one part the kernel has to grow a system
> call for. Redirection (`>`) and the graphics mode have their own plans,
> both built after this one: [redirect_plan.md](redirect_plan.md) and
> [graphics_plan.md](graphics_plan.md). Facts marked *(checked)* were read in
> the code; the rest is reasoned.

---

## 1. What you asked for

From the chat and your answers, 2026-09-16:

- **A simple scripting language,** `.pgs` — pigeon shell — and only that
  extension (Q1).
- **`pgs FILE` runs a script**, and the explorer opens `.pgs` files with it.
  Not `sh FILE` (Q8).
- **Variables**, `$name = value`, because without them there is not much a
  script can do (Q2).
- **A separate program, not more `sh.c`** — and the two stay separate for
  good (Q10).
- **A program's output captured into a variable,** `$out = $(ls /bin)` (Q3).
- **`;;` starts a comment,** and a `#` line is a **directive** that turns a
  setting on (Q13, Q14).
- **All of the control flow** — `if`/`else`/`end`, `while`/`end`, the tests,
  `let`, `break` — plus `for $line in $files` (Q12).
- **An unset variable stops the script** rather than expanding to nothing
  (Q5).
- **`/etc/startup.pgs`, run by the shell** — a shell startup script, where
  `boot.conf` is the kernel's (Q9, F3).
- **`./setup.pgs` typed at the prompt runs it too** (F1).
- **Redirection and the graphics mode planned now, built later** (Q7, F4):
  [redirect_plan.md](redirect_plan.md), [graphics_plan.md](graphics_plan.md).

---

## 2. Where things stand

- **The shell is 276 lines and ignores its arguments.** `main(argc, argv)`
  never looks at them: `sh` prompts, splits a line at spaces with double
  quotes grouping, finds the program and `exec`s it *(checked:
  `user/os/bin/sh.c:227`, `sh.c:39–93`)*. Built, it is 47 KB *(checked:
  `build/pigeonos/bin/`)*, and the kernel loads a copy of it for every nested
  shell.
- **Everything a program prints goes to the console, and nowhere else.**
  `k_write` sends `STDOUT` and `STDERR` to `con_put`, a byte at a time; any
  other descriptor is a file *(checked: `user/os/kernel.c`, `k_write`)*.
  There is no redirection, no pipe, and nothing that hands one program's
  output to another: the system call table is 20 calls and none of them is
  about output *(checked: `lib/pigeon/syscall.h`)*.
- **There is room for more calls.** The table has 32 slots, 20 used, and each
  program reaches a call by its slot number, so a call added at the end moves
  nothing *(checked: `emulator/memory_map.py:133`, `syscall.h:33`)*. The
  shape of a new one is fixed: a `w_` wrapper in `kernel.asm` that sets
  `in_kernel` around a `CALL`, a `k_` function in `kernel.c`, a line in
  `k_tables`, and a one-line function in `lib/pigeon/sys.c` *(checked:
  `kernel.asm:247`, `sys.c:79`)*.
- **`paging()` is the precedent for "this program and everything it runs".**
  `page_owner` is the depth that turned paging on; `k_write` obeys it for
  every deeper program, and `k_run` clears it when that program ends
  *(checked: `kernel.c` `k_paging`, `k_run`)*. A capture works the same way.
- **Programs nest, to a depth of 8.** A child is loaded above its parent's
  heap and `exec` returns its status; Ctrl+C ends the deepest program and
  hands control back to its parent *(checked: `kernel.c:47`, `k_run`)*. So
  `sh` → `pgs` → a program is three of the eight, and a script that runs a
  script is one more each time.
- **The kernel redraws its console after every program** *(checked:
  `kernel.c` `k_tidy`, `con_redraw`)*, which is why the explorer has a
  `[ press any key ]` pause. It is also what a `# graphics` script would have
  to stop (§7, F4).
- **A program can read a whole file** with `open`/`read`, as the shell reads
  its prompt file *(checked: `sh.c:111`)*.
- **The kernel's "what runs at boot" already exists:** `/etc/boot.conf`'s
  `startup`, the program it runs and runs again whenever it ends *(checked:
  `kernel.c` `k_read_boot_conf`, `main`)*. A *shell* startup script is the
  shell's own business, which is what §4.9 makes it.
- **The explorer runs what a conf rule names, with the file added to the end
  of the command** *(built today: [explorer.md](explorer.md))*, so `.pgs`
  files need one line in `/etc/explorer.conf` and no new machinery.
- **A startup script has been sketched twice and deferred twice:** as
  `/etc/startup` *([phase4_plan.md](phase4_plan.md) §11)* and as the shell's
  own `sh.conf` *([phase6_plan.md](phase6_plan.md) §7)*.

---

## 3. The goal

`/docs/hello.pgs`:

```sh
;; A script: run it with `pgs hello.pgs`, or click it in the explorer.

$name = world
echo hello $name                    ;; hello world

$files = $(ls /bin)                 ;; the output of ls, in a variable
if $? == 0
    echo /bin holds:
    echo $files
end

$i = 0
while $i != 3
    echo tick $i
    let $i = $i + 1
end

for $line in $files                 ;; a captured listing, a line at a time
    if $line == sh.bin
        echo found the shell
        break
    end
end

$here = $(pwd)
echo done in $here
```

At the prompt:

```
2:/docs> pgs hello.pgs
hello world
/bin holds:
cat.bin
echo.bin
...
```

One line in `/etc/explorer.conf` makes clicking it do the same:

```ini
/bin/pgs.bin = .pgs
```

---

## 4. The design

### 4.1 Where it lives: `/bin/pgs.bin`, not `sh.c`

Your instinct is right, and for a reason worth writing down: **the shell runs
one line at a time and never needs to look back.** A script needs the whole
file in memory (a `while` has to jump back to a line it has already run), a
variable table, block state, and a capture buffer. That is several hundred
lines and a few kilobytes of state on top of a 47 KB shell the kernel reloads
for every nested shell and every `exit` *(§2)*.

So: **`user/os/bin/pgs.c` → `/bin/pgs.bin`**, an ordinary program. Two ways
to start it:

- **`pgs FILE`**, at the prompt — the only command form (Q8). `sh FILE` was
  my idea, not yours, and you were right to ask: the shell would be guessing
  what an argument means, and `pgs` is a word you already have to know.
- **A click in the explorer**, through the conf rule above.

The shell and the script runner stay separate for good (Q10): the prompt
stays 47 KB and instant, and `pgs` can grow without it paying. They share
ideas but not code — about 40 lines of splitting and program-finding, which
`pgs` needs its own version of anyway, since it expands variables and `$( )`
as it splits, and the compiler has no linker for a shared unit.

### 4.2 The language: lines, comments and directives

- **A script is read whole**, at most 16 KB, and kept in memory; lines are
  found in it, so `while` is a jump back to a line number.
- **One command a line.** No `;`, no `&&`, no line continuation.
- **`;;` starts a comment,** to the end of the line, anywhere outside quotes
  (Q14).
- **A line that starts with `#` is a directive,** not a comment — a setting
  turned on by naming it (Q13):

  ```sh
  # stop-on-error        ;; a program that fails ends the script (§4.8)
  ```

  This is the tidy half of your two answers together: because `;;` took the
  comment job, `#` is free to mean something, and nothing has to guess
  whether `# stop-on-error` was a setting or somebody thinking aloud.
  Directives live in the script's **header** — before the first command — and
  a `#` word `pgs` doesn't know is an error with its line number, not a shrug
  (F2). The only directive in this plan is `# stop-on-error`; `# graphics` is
  §7 and F4.
- **Words are split at spaces, with double quotes grouping them**, exactly as
  the shell splits a line *(§2)* — and then **each word is expanded on its
  own**. A value with spaces in it stays one word, always:

  ```sh
  $file = "my notes.txt"
  edit $file                  ;; edit is given one argument, not two
  ```

  This is the one place where `pgs` deliberately differs from `sh` on a real
  machine, where `$file` would split in two and surprise you. Split first,
  expand second, never split again.
- **`\$` is a dollar and `\\` a backslash;** nothing else is special. No
  single quotes (Q6).

### 4.3 Variables

```sh
$name = world               ;; a literal: the rest of the line, expanded
$greeting = hello $name     ;; "hello world"
$out = $(ls /bin)           ;; a command's output (§4.5)
```

- **The name is `$word` on the left of a `=`.** A line whose first word starts
  with `$` and whose second word is `=` is an assignment; everything else is a
  command. That one rule is the whole grammar of it (Q2).
- **Names** are letters, digits and `_`, at most 31 characters. **Values** are
  text, at most 255 bytes, and there are at most 32 variables (Q11). A value
  that doesn't fit is an error on that line, not a quiet cut.
- **An unset variable stops the script** — `hello.pgs:4: no such variable:
  $nmae` (Q5). There is no `set -u` to turn on here, and a typo that silently
  becomes an empty string is the most expensive bug a small language can
  have. `$name =` with nothing after it sets an empty value, which is how you
  say you meant it.
- **The script's own:** `$0` its path, `$1`…`$9` its arguments, `$#` how many
  there are, `$?` the status of the last command — 0, the program's own
  value, or the kernel's negative code for a crash or Ctrl+C.
- **Variables are the script's alone.** There is no environment: a program
  gets what the line gives it as arguments. A `cd` in a child *does* move the
  script, since PigeonOS has one current directory *(checked: the explorer
  had to handle exactly this)*.

### 4.4 Running a program

A line that isn't an assignment, a directive or a builtin is a command. Its
words are expanded, the first is found the way the shell finds a program —
`/bin/<name>.bin` first, then the folder you are in, `.bin` added when
missing *(§2)* — and it is `exec`ed. The script waits, the program's output
goes to the console like any program's, and `$?` is its status.

### 4.5 Capturing output — what the kernel has to grow

This is the part that does not exist yet. Today every `write(STDOUT)` lands
in the console and nothing can intercept it *(§2)*, so `$out = $(cmd)` needs
the kernel's help:

```c
/* <pigeon/sys.h> */
int exec_out(char *path, int argc, char **argv, char *buf, unsigned size);
```

Runs the program exactly as `exec` does and returns the same status, but
everything it — or anything it runs — writes to `STDOUT` goes into `buf`
instead of the console. The buffer is left NUL-terminated; output longer than
`size - 1` is cut, and `pgs` can tell, because the result is exactly that
long.

In the kernel it is `k_exec_out`: remember the capture that was running,
point the new one at `buf`, call the same `k_run` as `exec`, then put the old
one back. `k_write` gains one test at the top —

```c
if (out_buf != NULL && depth >= out_depth) { append to out_buf; return n; }
```

— and that one test also gives the *grandchildren* case for free, which is
how `paging` already works *(§2)*. Because the previous capture is restored
by the call itself, a script that captures a script that captures needs
nothing extra, and `k_tidy` has nothing to clean up.

**Two other ways, both set aside.** A `capture(buf, size)` *mode* like
paging's is more flexible and leakier: it can outlive what it was meant to
cover, and nesting needs one slot per depth. Sending a child's output to a
*file* is [redirect_plan.md](redirect_plan.md)'s job — the right way to do
`ls > out.txt`, the wrong way to do `$( )`, at a disk round trip and an
invented filename per capture.

**What is captured:** `STDOUT` only. `STDERR` keeps going to the console, so
a program's complaints stay visible while its output is being taken (F6).
**Trailing newlines are stripped** from the value, so `$n = $(echo 5)` is `5`
and not `5\n`; newlines inside it stay, which is what `for ... in` walks
(Q4). **Output that doesn't fit is cut, and the script is told so on the
console** — `hello.pgs:12: $(ls /bin) cut at 8192 bytes` — because a silent
cut is how a script quietly does the wrong thing (Q4). The buffer is `pgs`'s
own, **8 KB** (Q11).

### 4.6 Control flow, and arithmetic

```sh
if <test>
    ...
else                        ;; optional
    ...
end

while <test>
    ...
end

for $line in $words         ;; §4.5's captured value, a line at a time
    ...
end
```

A **test** is one of:

| | |
|---|---|
| `$a == b`, `$a != b` | text, after expansion |
| `$a -lt 5`, `-le`, `-gt`, `-ge` | numbers |
| `-e path`, `-d path`, `-f path` | it exists, is a directory, is a file |
| a command | true when its status is 0 |

`break` leaves the innermost `while` or `for`. Blocks nest to 8 deep. There
is no `continue` until something wants it (Q12).

**`for ... in` is the one place a value is split**, and it splits on newlines,
not spaces — so `for $line in $(ls /bin)` walks the names `ls` printed, and a
name with a space in it survives. An empty value runs the body no times.

**Arithmetic** is `let`, integers only, `+ - * / %` and parentheses:

```sh
let $i = $i + 1
let $half = ( $a + $b ) / 2
```

### 4.7 The builtins

`echo`, `cd`, `pwd`, `exit N`, `read $name` (a typed line from the console,
so a script can ask you something), `let`, and the block words. Everything
else is a program. `pwd` and `read` are builtins because there is no
`/bin/pwd.bin` or `/bin/read.bin` to run, and `cd` has to be one because it
moves the script's own directory.

### 4.8 When something is wrong

- **A mistake in the script** — a line that isn't a command, an unknown
  builtin or directive, a bad variable name, an unset variable, an `end` with
  no `if`, a value too long — stops the script with
  `hello.pgs:7: no end for if` on the console, and `pgs` returns 1.
- **A program that fails does not stop the script.** Its status goes in `$?`
  and the next line runs, which is what `$?` is for — unless the header says
  `# stop-on-error`, and then a non-zero status ends the script with
  `hello.pgs:12: ls exited 1` and that status (Q13).
- **`exit N`** ends the script with that status.
- **Ctrl+C** ends the program the script is waiting for and then the script:
  the kernel gives the break to the deepest program and `pgs` sees the
  `Ctrl+C stopped it` status *(§2)*. That is the way out of a `while 1`.

### 4.9 The disc, the explorer, and `/etc/startup.pgs`

- `user/os/bin/pgs.c` → `/bin/pgs.bin`, and `/docs/hello.pgs`, §3's example,
  so there is something to click: 30 files on the disc.
- `/etc/explorer.conf` gains `/bin/pgs.bin = .pgs`, above the `*.*` line.
- **`./setup.pgs` at the prompt runs it (F1).** When the shell can't find a
  word as a program and it ends in `.pgs`, it runs `pgs` on it instead — two
  lines on `sh.c`'s `find()` path. `pgs setup.pgs` still works and is what
  the docs show.
- **The startup script is the shell's (Q9, F3).** When `/etc/startup.pgs` is
  there, `sh` runs it before its first prompt — `exec /bin/pgs.bin
  /etc/startup.pgs` — and prompts when it ends. Six lines in `sh.c`, and no
  kernel change: the kernel's own "what runs at boot" is `boot.conf`'s
  `startup`, and this is the shell's.

  **It runs for every shell, not once a boot** — which is what `.bashrc` does
  and what "a shell startup script" should mean. So it runs again after
  `exit` restarts the shell, inside the explorer's `Shell here`, and in a
  shell a script starts. Three things follow, and they belong in the docs
  rather than in a flag:
  - A banner in it greets you every time, not once.
  - A `cd` in it puts *every* new shell in that folder.
  - **A startup script that starts a shell recurses**, each shell running the
    script again, until the kernel's depth of 8 says `too many programs
    running` *(§2)*. `pgs` can't tell it is inside one, so this is a line in
    `docs/pgs.md`, not a guard.

---

## 5. Steps

1. **The kernel's `exec_out`** *(§4.5)*: `syscall.h`, `kernel.asm`,
   `kernel.c`, `sys.c`, `sys.h`. *Tests* in `tests/test_kernel.py`: output
   captured instead of printed, a grandchild's too, the status still
   returned, output longer than the buffer cut and terminated, a capture
   inside a capture, `STDERR` still reaching the console, and nothing left
   behind for the next program.
2. **`pgs.c`: lines, comments, directives, words, variables, commands,
   `$?`.** *Tests* in `tests/test_pgs.py`: assignment, expansion, `;;`
   anywhere, an unknown directive, a directive after the first command,
   quotes keeping a value in one word, `$1`, `$#`, a command's status, an
   unset variable stopping the script, a missing program.
3. **`$( )`**, on top of step 1. *Tests:* a captured value, trailing newlines
   gone, newlines inside kept, a command that fails, output cut at 8 KB and
   said so, a capture of a script that captures.
4. **`if`, `else`, `while`, `for ... in`, `let`, `break`.** *Tests:* each test
   kind, nesting, a missing `end`, a loop that counts, `for` over a listing
   and over an empty value, `break`, Ctrl+C out of a loop.
5. **The builtins and the errors:** `echo`, `cd`, `pwd`, `read`, `exit`,
   `# stop-on-error`, and every message in §4.8 with its line number.
6. **The disc and the docs:** the project file, `explorer.conf`,
   `/docs/hello.pgs`, `docs/pgs.md`, and this plan's "as built".
7. **The shell's two small changes** *(§4.9)*: `/etc/startup.pgs` before the
   first prompt, and `./setup.pgs` typed by name. *Tests* in
   `tests/test_kernel.py`, beside the shell's own: the script runs before the
   first prompt and its output is above it, a missing one changes nothing, a
   script that fails still leaves a prompt, a second shell runs it again, and
   a `.pgs` typed at the prompt runs.

A full `python3 -m pytest tests/` at the end, not after each step.

---

## 6. Risks

- **The capture buffer is the script's own memory and the kernel writes into
  it.** Nothing on this machine is protected, so a wrong size is a way to
  overwrite something. The kernel should refuse a `size` of 0 and cap it at
  64 KB; beyond that it is the same trust every other call already has.
- **Depth.** `sh` → `pgs` → `$(pgs inner.pgs)` → a program is four of eight
  *(§2)*. Deep enough to be fine, shallow enough to be worth a message when
  it isn't: `too many programs running` already comes back from `exec`.
- **A script is not a shell.** No pipes, no `&&`, no job control; `>` is
  [its own plan](redirect_plan.md). `$( )` is the one way output moves.
- **Growth.** `pgs.bin` will land somewhere between the shell's 47 KB and the
  explorer's 122 KB *(estimated from those two)*.
- **`for` and `$( )` together are a listing walker,** which is the most
  likely way a script meets the 8 KB buffer: `/bin` today is about 300 bytes
  of names, but a folder of 500 files would be 6 KB. The cut message matters.
- **Two languages on one disc.** `boot.conf`, `explorer.conf` and
  `shell_header.conf` stay their own little formats. That is fine — they are
  configuration, not programs — but `pgs` should not grow into them.

---

## 7. Not in this plan

- **A small Python instead of a shell** — weighed on 2026-09-16 and set
  aside. The kernel call for capturing output *(§4.5)* is needed either way,
  so it cancels out of the comparison; what is left is the value model, and
  that is where the cost is. `pgs` has one type, text, in a table of 32 that
  never grows, and allocates nothing while it runs. A Python-like runtime
  needs typed values, lists, frames and a lifetime scheme -- and
  `lib/pigeon/mem.c` says what that would meet: *"First fit, no coalescing
  and no splitting: a workload that frees large blocks then allocates small
  ones will fragment"* *(checked: `mem.c:88`)*. It would want a new allocator
  under it before its first loop. On top of that, the parser is the most
  expensive kind there is -- indentation and operator precedence -- against
  `pgs`'s "split the line at spaces", and the thing these scripts exist to do
  is run programs, which is `ls /bin` in a shell and `run("ls", "/bin")` in
  anything else. If a real language is ever wanted here, a Forth or a tiny
  Lisp is a tenth of the work of Python-like syntax and would be its own
  program, not a replacement for this one.
- **Redirection,** `>` and `>>`: [redirect_plan.md](redirect_plan.md),
  written now and built after this (Q7).
- **`# graphics`, and `graphics.bin`** (Q13, F4): its own plan,
  [graphics_plan.md](graphics_plan.md), written now and built after this one
  and after redirection. `pgs` refuses `# graphics` with `no such setting`
  until it lands, which is honest.
- **Pipes** (`a | b`), which need two programs at once — the kernel runs one
  *(checked: `k_run` waits)*.
- **`&&`, `||`, `;`**, and more than one command on a line. A `\`
  continuation is not here either, but it arrives with
  [graphics_plan.md](graphics_plan.md) (its Q8), where a long drawing call
  wants one.
- **Functions**, arrays, `case`, `continue`.
- **An environment** passed to children.
- **Signals, background jobs, subshells.**
- **An interactive `pgs`** (Q10).

---

## 8. Your answers

Answered in this file on 2026-09-16.

1. ~~**The name and the extension.** `.pgs` everywhere, or `.sh` as an alias
   too?~~

   Answer: only pgs

   **Decided (you):** `.pgs`, and `/bin/pgs.bin` (§4.1, §4.9).

2. ~~**Assignment.** `$name = value`, or `set name = value`?~~

   Answer: okay the $ syntax is good

   **Decided (you):** `$name = value` (§4.3).

3. ~~**Capturing: `$( )`, backticks, or a word — and is `STDERR` captured
   too?**~~

   Answer: $() is good

   **Decided (you):** `$( )` (§4.5).
   **Decided (left to me):** `STDOUT` only, so a program's complaints stay
   visible while its output is being taken. F6 if you want `STDERR` in the
   value too.

4. ~~**The captured value:** strip trailing newlines, and what happens when
   the output doesn't fit?~~

   Answer: suggestion is fine

   **Decided (you):** trailing newlines stripped, inner ones kept; output
   that doesn't fit is cut and said so on the console (§4.5).

5. ~~**A variable that was never set:** empty, or stop the script?~~

   Answer: stop

   **Decided (you):** stop, with its name and its line (§4.3).

6. ~~**Quoting:** double quotes and `\$`, or single quotes as well?~~

   Answer: okay

   **Decided (you):** double quotes and `\$`, no single quotes (§4.2).

7. ~~**Redirection:** now, later, or never?~~

   Answer: later, but create a plan for it now

   **Decided (you):** [redirect_plan.md](redirect_plan.md), written alongside
   this one and built after it.

8. ~~**Starting a script:** `pgs FILE`, `sh FILE`, and/or the shell running
   `./setup.pgs` by name?~~

   Answer: pgs FILE, i dont know hy you said sh FILE...

   **Decided (you):** `pgs FILE` only; no `sh FILE` (§4.1).
   **Reply:** because your first message said `sh ./script.sh` — that is where
   I got it. Dropping it is better: the shell would be guessing what an
   argument means. F1 asks the other half of that question, the shell running
   `./setup.pgs` typed by name.

9. ~~**`/etc/startup.pgs`:** in this plan or a later one?~~

   Answer: now

   **Decided (you):** in this plan, as step 7 (§4.9).
   **Decided (you, in F3):** the *shell* runs it — it is a shell startup
   script, and the kernel's own is `boot.conf`'s `startup`. So it runs for
   every shell, the way `.bashrc` does, not once a boot (§4.9).

10. ~~**Does `pgs` become the shell later?**~~

    Answer: separate

    **Decided (you):** separate, for good (§4.1).

11. ~~**The sizes.**~~

    Answer: yeah 8 for the capture buffer.

    **Decided (you):** the capture buffer is 8 KB; 32 variables, names 31
    bytes, values 255 bytes, scripts 16 KB (§4.3, §4.5).

12. ~~**How much language — and `for $line in $files`?**~~

    Answer: everything you mentioned

    **Decided (you):** `if`/`else`/`end`, `while`/`end`, the four kinds of
    test, `let`, `break`, and `for ... in` splitting on newlines (§4.6). No
    `continue` yet.

13. ~~**When a program in a script fails: carry on, stop, or a switch?**~~

    Answer: lets use `# config-string` if mentioned it turns the config
    option on. also i just had an idea. #graphics script wont show a shell.
    just a black screen, and later we can add graphics.c wich will allow
    commands like `graphics.bin -rect 0 0 10 10 0xFF00FF00` - `x y w h color`
    and the same for circles and text. also a `graphics.bin -px x y col`

    **Decided (you):** a `#` line is a directive that turns a setting on, and
    `# stop-on-error` is the switch (§4.2, §4.8). Carrying on stays the
    default.
    **Decided (left to me):** directives live in the header, before the first
    command, and an unknown one is an error rather than a silent no-op —
    otherwise a typo in `# stop-on-error` is exactly the failure Q5 rules out
    (F2).
    **Reply on `# graphics`:** a good idea, and bigger than it looks. It needs
    the kernel to stop redrawing its console between programs, every command
    in the script run captured so nothing paints text over the picture, and
    `graphics.bin` itself. That is its own plan — §7 holds the sketch, F4 and
    F5 ask how you want it.

14. ~~**Anything else you want in the language?**~~

    Answer: i want ;; for comments if its possible and i mentioned the `#`
    config system above

    **Decided (you):** `;;` starts a comment (§4.2). It is possible, and it
    is what makes the `#` directives clean: with `;;` doing comments, a `#`
    line can mean a setting with nothing left to guess.

---

## 9. Follow-ups

All six answered on 2026-09-16, in this file.

- **F1. The shell running `./setup.pgs` typed by name.** ~~When a word the
  shell can't run as a program ends in `.pgs`, should the shell run `pgs` on
  it?~~

  Answer: suggestion is good

  **Decided (you):** yes — two lines on `sh.c`'s `find()` path (§4.9). It is
  the one thing the shell knows about `.pgs`.

- **F2. Directives: header only, and an unknown one an error?** ~~`#` lines
  before the first command, and anything `pgs` doesn't know reported with its
  line.~~

  Answer: yes to both

  **Decided (you):** both (§4.2). A setting has to be read before the script
  runs, and a silently ignored typo is the failure Q5 rules out.

- **F3. Who runs `/etc/startup.pgs`** ~~— the kernel from `boot.conf`, or the
  shell before its first prompt?~~

  Answer: the shell runs it. its a shell startup script. the kernel has its
  own in boot.conf

  **Decided (you):** the shell, and the framing settles the objection I had.
  I was thinking "once a boot", which a shell cannot know; you are describing
  `.bashrc` — **every shell runs it**, and that is well defined. No kernel
  change, six lines in `sh.c`, and `boot.conf`'s `startup` stays the kernel's
  own (§4.9, which lists what "every shell" means in practice).

- **F4. `# graphics`:** ~~its own plan, or squeezed in here?~~

  Answer: yeah, create another plan please for the graphics, build it later

  **Decided (you):** [graphics_plan.md](graphics_plan.md), written now and
  built after this one and after redirection. Until then `pgs` refuses
  `# graphics` with `no such setting` (§7).

- **F5. `graphics.bin`'s shape.** ~~One shape a call, many shapes a call, or a
  file of drawing commands?~~

  Answer: yeah, why not even combining `graphics -f picture.gfx 10 10 50 50
  STRECH -rect 0 0 10 10 0xFF00FF00 -circle 96 54 20 0xFFFF0000`

  **Decided (you):** one call, as many operations as you like, images among
  them, each taking its own arguments — so a whole picture is one program
  load instead of a hundred. That example is the graphics plan's §3, and what
  `-f` reads is its first question.

- **F6. `STDERR` while capturing.** ~~On the console, or in the value too?~~

  Answer: keep `STDOUT` only

  **Decided (you):** `STDOUT` only; a program's complaints stay visible
  (§4.5).
