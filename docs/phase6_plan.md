# Phase 6: `boot.conf`, a splash screen, and the startup program

> **Status: final plan, 2026-09-15; every question is decided
> ([§8](#8-your-answers), [§9](#9-follow-up-questions)), and built the same
> day ([§10](#10-as-built)).** kernel.md §17's item 6, sketched in
> [phase4_plan.md](phase4_plan.md) §11 as a boot screen and a startup
> *script*. What you asked for is simpler and different: the kernel reads
> `/etc/boot.conf`, runs the splash screen it names, prints `PigeonOS`, then
> runs the startup program it names, which for now is the shell. Facts marked
> *checked* were read in the code, *measured* ones were run; the rest is
> reasoned.

---

## 1. What you asked for

From the chat and your answers, 2026-09-15:

- **The kernel starts a splash program,** which shows something for 2 to 3
  seconds and ends.
- **`boot.conf` says what runs right after the kernel:** the splash screen,
  how long it shows, and the startup program.
- **The startup program is the shell,** for now, and starts again whenever it
  ends.
- **Boot is strict:** an error means the machine doesn't boot.
- **The order on screen:** the splash, then `PigeonOS` on the console, then
  the shell.
- **The splash, for now, is an extremely simple RGB fade:** a placeholder for
  a real splash screen. Any key ends it early.

---

## 2. Where things stand

- **The kernel starts the shell and nothing else.** After mounting its disk,
  `main` runs `SHELL`, `/bin/sh.bin`, as `sh`, and again whenever it ends; if
  it can't start, the kernel prints why and stops *(checked:
  `user/os/kernel.c:37`, `main`)*.
- **`PigeonOS` is printed before the disk is mounted** *(checked: `main`)*.
- **After any program, `k_tidy` puts things back:** it stops timers 0 to 15,
  empties the input queues, points the display at the screen again and
  redraws the console *(checked: `kernel.c`, `k_tidy`)*. A splash that paints
  the whole screen needs to clean up nothing.
- **The kernel can read a whole file:** `fs_load(path, buf, max)` returns the
  bytes read or `FS_E2BIG`, and `k_exec` already uses it *(checked:
  `lib/pigeon/fs.h:135`)*.
- **The shell reads a config file already,** `/etc/shell_header.conf`: at most
  1,024 bytes, `\r` taken off line ends, and a bad file reported in one line
  *(checked: `user/os/bin/sh.c:120–140`)*.
- **A program gets its arguments from `exec`,** as `main(argc, argv)`
  *(checked: `k_exec`)*, which is how the splash will be told its length.
- **Programs time themselves with the timer device,** `START` then `STATUS`
  for the milliseconds left, as `cube.c` does *(checked: `user/cube.c:58–90`)*.
- **The disc's `/etc` holds only the prompt file,** and the project file
  copies anything that isn't `.c` or `.asm` as it is *(checked:
  `user/os/etc/`, `pigeon_compiler_init.txt`)*.
- **Every kernel test boots a disk with no `/etc`** *(checked:
  `tests/test_kernel.py`, `make_disk`)*, and with no `boot.conf` the kernel
  boots the shell as today (F1), so none of them change. The install and project tests count the disc's
  files: 20 now *(checked)*.
- **An RGB fade is cheap** *(measured, with a prototype in scratch, not in the
  repo)*: a fade of 2.5 s ran 2.50 s on the real clock and made 765
  full-screen fills, about 189 instructions each, between 16,648 looks at the
  timer, about 213 each, for 3.7 million instructions. Built as a program
  file, with the display library and its font, it's 25,000 bytes. On the
  tests' stepping clock the same 2.5 s is 50 looks at the timer.

---

## 3. The goal

```ini
# /etc/boot.conf -- what runs right after the kernel
splash    = /bin/splash.bin
splash_ms = 2500
startup   = /bin/sh.bin
```

Booting the installed disk: bios2; the kernel mounts the disk and reads
`boot.conf`; the screen fades through red, green and blue for 2.5 seconds;
`PigeonOS` appears on the console; and the shell's prompt follows it.

---

## 4. The design

### 4.1 `boot.conf`

- **`key = value`, one a line,** as the project file writes them. `#` starts
  a comment, blank lines are skipped, spaces around the key and the value
  are trimmed, and a `\r` before a line break is ignored.
- **Three keys** (Q1, Q5):
  - `splash`, the program shown first;
  - `splash_ms`, how long it shows: a whole number of milliseconds from 1 to
    60,000, and 2,500 when it isn't there;
  - `startup`, the program that runs after it.
- **Strict** (Q3): anything wrong in the file stops boot, with one line on
  the console and the debug port naming the file, the line and what's wrong,
  as a failed mount does today. Wrong is:
  - an unknown key, a key given twice, or a line with no `=` or no value;
  - a `splash_ms` that isn't a number from 1 to 60,000, or one with no
    `splash`;
  - a file over 1,024 bytes;
  - no `startup` (F1).
- **With no `boot.conf` at all,** the kernel starts `/bin/sh.bin`, as before
  phase 6 (F1).
- **Paths are as `exec` takes them;** `boot.conf` is read with the root as
  the current directory.

### 4.2 The kernel

- **After mounting,** `main` reads `boot.conf`. Then, in this order (Q7):
  1. **the splash,** if one is named, as `splash 2500`: its `splash_ms` is its
     first argument;
  2. **`PigeonOS`** on the console, which until now came before mounting;
  3. **the startup program,** again whenever it ends (Q4).
- **`PigeonOS` comes first when boot stops early:** a failed mount, an error
  in `boot.conf` or a failed splash prints the banner, then the error, so the
  console reads as it does today. It's printed once either way.
- **The splash is an ordinary `exec`** with break on. One that can't start,
  or faults, stops boot, with why on the console (F2); Ctrl+C or a key only
  ends it, and boot carries on.
- **The startup program's name** in `argv[0]` is its file name without
  `.bin`: `sh` for `/bin/sh.bin`, as today.
- **A startup program that can't start stops boot** (Q3), with the line the
  kernel prints today for the shell, naming the program. That holds when it
  is started again after ending, too.
- **The debug port:** `[kernel] boot.conf: splash /bin/splash.bin for 2500 ms,
  startup /bin/sh.bin`, and each error before the machine stops. The splash's
  and the startup program's `exec` lines are 5b's.

### 4.3 `splash.c`, the fade

`user/os/bin/splash.c`, on the disc as `/bin/splash.bin` (Q9), where `splash`
at the prompt runs it again:

- **Its length is its first argument,** in milliseconds, which the kernel
  takes from `splash_ms`; with none, as from the shell, 2,500.
- **Timed on the timer device,** not by counting frames, so it lasts as long
  on a fast host as a slow one.
- **Black, then red, green, blue, and black again** (Q8: a placeholder), each
  fading into the next over a quarter of the time. At every look at the timer
  it works out the colour for the milliseconds gone, and fills the screen
  only when the colour changed, with `disp_clear`, the hardware fill.
- **Any key ends it early** (Q6). `k_tidy` empties the queues afterwards, so
  the key never reaches the shell.
- **About 60 lines,** with no library but the display, `io.h` and `string.h`
  for the argument. A real splash screen later replaces this one file.

### 4.4 The disc

- `user/os/etc/boot.conf`, as §3, and two lines in
  `pigeon_compiler_init.txt`: `/etc/boot.conf` and `/bin/splash.bin`.
- The example disc then holds 22 files, and its tests count 22.

---

## 5. Steps

**Step 1. `boot.conf` in the kernel.**
- The reader, the order of splash, `PigeonOS` and startup program, the
  splash's argument, the strict errors, and the lines on the console and
  the debug port.
- **Tests,** in `test_kernel.py`, each on a disk made for it:
  - a splash stand-in that shows it was given `splash_ms`, then `PigeonOS`,
    then the shell, in that order;
  - comments, blank lines, spaces and `\r`; `splash_ms` left out, giving
    2,500; no `splash`, so straight to `PigeonOS` and the startup program;
  - each error in §4.1 stopping boot, with its line on the console and the
    port;
  - a startup program other than the shell, started again when it ends;
  - a startup program that's missing, or isn't a program, stopping boot;
  - a splash that's missing, isn't a program or faults, stopping boot; one
    ended by Ctrl+C, and boot carrying on;
  - a disk with no `boot.conf` starting the shell, with its line on the port.

**Step 2. The fade, and the disc.**
- `splash.c`, `etc/boot.conf`, the project file's two lines.
- **Tests:**
  - the real splash on a Machine: the screen goes through red, green and
    blue, it ends by itself after its argument's milliseconds on the
    stepping clock, and a key ends it early;
  - its cost, counted: instructions per look at the timer, and per fill;
  - the example disc: 22 files, with `boot.conf` and `splash.bin`, and the
    install test's reboot going through the splash and `PigeonOS` to the
    prompt.

**Step 3. Docs.** kernel.md §17's item 6 and the kernel section on booting,
kernel_overview.md, phase4_plan.md §11's note, os_cd.md's installed disk, the
README's booting section, and the disc's `readme.txt`.

| Step | Needs | Size |
|---|---|---|
| 1. `boot.conf` in the kernel | — | medium |
| 2. The fade and the disc | 1 | small |
| 3. Docs | 1, 2 | small |

One commit for the phase, after the whole suite and deliberate breakages for
every piece, as for phase 5.

---

## 6. Risks

- **Strict boot means a typo stops the installed disk.** It can be fixed from
  the host with `tools/pfs.py`, or by installing again from the disc; the
  shell's `edit` isn't reachable once boot stops. The error names the file
  and the line, so the fix is quick.
- **A startup program that ends at once** is started again at once, forever
  (Q4), printing its line each time.
- **Boot gets `splash_ms` slower,** on purpose. The install test's splash
  runs on the stepping clock, so it costs the tests 50 looks at the timer.
- **The screen isn't the console's while the splash runs,** and anything the
  splash prints lands on the console underneath, shown once it ends.

---

## 7. Not in phase 6

- **A real splash screen,** a logo or more: a later change to `splash.c`.
- **The progress bar** from phase 4's sketch, run between boot steps.
- **A startup script:** later, as the shell's own `sh.conf`, not
  `boot.conf`'s (Q2).
- **Phase 7, the launcher.**

---

## 8. Your answers

Answered in this file on 2026-09-15.

1. ~~**The keys' names:** `splash` and `startup`, or phase 4's
   `loading_graphics`, or something else?~~

   Answer: yes, thoose key names are fine.

   **Decided (you):** `splash` and `startup` (§4.1).

2. ~~**The startup script** from phase 4's sketch, `/etc/startup`, run by the
   shell before its first prompt: dropped, or a later phase?~~

   Answer:boot.conf is what runs right after the kernel and what app starts next. so the splash screen -> shell. later we can add sh.conf with a startup, but right now we are focusing on the kernel.

   **Decided (you):** not in phase 6. `boot.conf` names what runs right after
   the kernel; a startup script comes later as the shell's own `sh.conf`
   (§7).

3. ~~**A startup program that can't start:** fall back to `/bin/sh.bin`, or stop
   with the message, as the shell does today?~~

   Answer: no, lets be strict. if there is an error, we wont boot.

   **Decided (you):** no fallback: an error stops boot (§4.1, §4.2).
   **Reply:** strict fits every error in the file itself, and §4.1 lists
   those. Two cases aren't clear from "an error" alone, a disk with no
   `boot.conf` and a splash that fails, so they're follow-ups F1 and F2.

4. ~~**A startup program that ends:** start it again, as the shell is today, or
   start the shell instead?~~

   Answer: start it again

   **Decided (you):** started again, as the shell is today (§4.2).

5. ~~**The splash's length:** 2, 2.5 or 3 seconds, and does it belong in
   `boot.conf` (`splash_ms = 2500`), or in `splash.c`?~~

   Answer: it can be on the boot.conf

   **Decided (you):** `splash_ms` in `boot.conf`, 2,500 when it's left out.
   **Reply:** the kernel hands it to the splash as its first argument, so the
   splash stays an ordinary program that `splash` at the prompt can run too
   (§4.1, §4.3). Decided with it (left to me): a whole number from 1 to
   60,000, and a `splash_ms` with no `splash` is an error, as strict boot
   suggests.

6. ~~**A key ends the splash early?**~~

   Answer: thats good

   **Decided (you):** any key (§4.3).

7. ~~**`PigeonOS` on the console:** printed after the splash, or before it,
   under the fade?~~

   Answer: it should go: splash (whatever the splash renders) -> PigeonOs string on the console -> shell

   **Decided (you):** the splash, then `PigeonOS`, then the startup program
   (§4.2).

8. ~~**The fade itself:** black → red → green → blue → black, or red → green →
   blue and straight to the prompt?~~

   Answer: i dont care. it should be just a placeholder for a more sophisticated splash screen.

   **Decided (left to me):** black → red → green → blue → black, kept as
   simple as it can be, since it's a placeholder (§4.3).

9. ~~**`splash.bin` in `/bin`,** where `splash` at the prompt runs it again, or
   somewhere the shell doesn't look, such as `/boot/splash.bin`?~~

   Answer:it can be in /bin

   **Decided (you):** `/bin/splash.bin` (§4.3).

---

## 9. Follow-up questions

Both come from strict boot (Q3); each has what I'd suggest.

- **F1. A disk with no `boot.conf`,** or a `boot.conf` with no `startup`:
  boot the shell as today, or stop?
  *Suggested:* no `boot.conf` at all boots the shell as today, since nothing
  was asked for, and every disk made before phase 6 keeps booting, the
  kernel tests' among them. A `boot.conf` that's there but has no `startup`
  is an error, and stops.

  Answer: yeah it can start the she;;

  **Decided (you):** no `boot.conf` starts the shell, as today; a `boot.conf`
  with no `startup` is an error, as suggested (§4.1).

- **F2. A splash that fails:** it can't start (missing, not a program), or it
  starts and then faults. Stop boot, or carry on to `PigeonOS` and the
  startup program?
  *Suggested:* a splash that can't start stops boot, as strict boot says. One
  that ran and faulted also stops, with the fault on the console. Ctrl+C or a
  key isn't an error: the splash ended, and boot carries on.

  Answer:i dont care.

  **Decided (left to me):** as suggested. A splash that can't start, or
  faults, stops boot; Ctrl+C or a key only ends it (§4.2).

---

## 10. As built

- **The kernel** reads `/etc/boot.conf` as §4.1 says, runs the splash as
  `splash <splash_ms>`, prints `PigeonOS`, and runs the startup program,
  again whenever it ends. It is 248,392 bytes, from 236,068. The banner is
  printed once, by `k_banner`, before whichever comes first: the startup
  program, or the error that stops boot. So a failed mount still reads under
  `PigeonOS`, as it always has.
- **The shell's messages are unchanged:** with `/bin/sh.bin` as the startup
  program, the console still says `shell ended, starting it again`. Another
  program says its own name there.
- **`/bin/splash.bin`** is 37,792 bytes, with the input and string libraries
  beside the display's. A look at the timer costs 624 instructions on the
  stepping clock, where nearly every look is a new colour and a fill; on the
  real clock the prototype filled on one look in 22 (§2).
- **The disc** has `/etc/boot.conf` and `/bin/splash.bin`: 22 files. Its
  `readme.txt` says what boots.
- **Tests:** 25 new, in `test_kernel.py`, and the install and project tests
  count 22 files and boot through the splash. 30 deliberate breakages, each
  failing a test; `splash_ms` taking letters needed a case of its own first,
  `1s`, since `soon` read as digits is already over a minute. The full
  suite passes: 1,407 tests.
- **Later the same day, the fade gave way to the pigeon.** `splash.bin` draws
  `/etc/bmp/pigeon.bmp` with `<pigeon/bmp.h>` ([bmp_plan.md](bmp_plan.md)),
  stretched to the screen, and holds it for `splash_ms`; any key still ends
  it. An image that won't load is one line on the console, and the splash
  ends with status 1: boot carries on, as after any splash that doesn't
  fault. The disc gains `/etc/bmp/pigeon.bmp`: 23 files. From `exec` to the
  splash's first look at the timer, loading it and the image and drawing,
  is 1,755,788 instructions, and a look while it waits is 179 *(measured)*;
  a test holds them under 2.3 million and 250.
- **Then the eyes.** `/etc/bmp/eye-mask.bmp` lies exactly over the pigeon,
  white at its eyes' 36 pixels. The splash finds them once, and at each look
  at the timer draws just those pixels again: each its own colour blended
  toward yellow by `(isin(angle) + 256) / 2` of 256, the angle following the
  time gone, one flash a second whatever the host's speed. A mask that won't
  load leaves the eyes still, with its line on the console once the splash
  ends, since the console draws on the same screen. The disc holds 24 files.
  Loading the program, the pigeon and the mask and drawing is 3,387,860
  instructions, and a look with the eyes drawn again is 6,757 *(measured)*;
  the splash's default length, run from the prompt, is 4,000 ms.
