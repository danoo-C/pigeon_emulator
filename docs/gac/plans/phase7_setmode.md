# Phase 7: `setmode`, and `# graphics` scripts that pick a mode

> Part of [the GAC plan](../README.md). **Status: built, 2026-09-19
> ([§9](#9-as-built)); every question decided ([§8](#8-decisions)).** Needs
> Phase 6 (built). Design: [design.md §6](../design.md#6-what-has-to-change-file-by-file)'s
> "New programs". Decisions: Q6, Q14 in [decisions.md](../decisions.md).
> Facts marked *(checked)* were read in the code.
>
> This file replaces the original sketch, which named the goal and five
> steps.

**The goal:** the demo the whole plan was written towards
([README.md §3](../README.md#3-the-goal)):

```sh
2:/> setmode 640 360
2:/> graphics -clear 0xFF101018 -disc 320 180 60 0xFFFF0000 -wait
```

A script that says `# graphics 640x360` gets a 640 × 360 screen to itself,
and the screen is back the way it was when the script ends. The manuals are
written: `docs/gac.md`, `docs/vram.md`, and the README's channel and settings
tables.

**What you will be able to see at the end of this phase:** everything the
toolbar's picker does, from the prompt: `setmode` says what the screen is,
`setmode -list` what it could be, and `setmode 640 360` switches it at once.
Graphics scripts can draw on a bigger screen than 192 × 108.

---

## 1. What planning found

Two things the sketch did not see, both *(checked)*, and each needs the
kernel:

1. **`setmode` cannot be an ordinary program.** Since Phase 5 the kernel
   puts the mode back when a program ends (Q6, `k_tidy`). That rule is what
   stops a crashed game leaving you in its mode. It also means a
   `setmode.bin` that calls `disp_setmode` switches the screen, ends, and
   has it switched straight back. A shell built-in would be no better:
   `sh` is a program too, and the kernel's console would not know the
   screen had changed size. **The console's mode can only be changed by the
   kernel,** the way it takes the picker's mode at the prompt (`k_adopt`).
   So `setmode` needs a system call (Q1).
2. **`k_tidy` puts back the wrong mode for scripts.** It compares the screen
   with the *kernel's* mode and sets that one back *(checked: Phase 5's
   `k_tidy`)*. A `# graphics` script is `pgs` running `graphics.bin` once per
   drawing line, and each `graphics.bin` is a program of its own. So after
   the first line of a script that switched to 640 × 360, `k_tidy` would put
   192 × 108 back under the script. **The rule has to be "the mode the
   program started in"** (Q2). That is what Q6 meant all along ("the kernel
   puts it back when that program ends"), and it only differs when programs
   run programs.

And one that is only a small change: **`pgs` reads a setting as a single
word** *(checked: `pgs.c`, `strcmp(word, "graphics")`)*, so
`# graphics 640x360` is a new form for it to parse (Q3).

---

## 2. The kernel

### 2.1 The mode a program started in

`k_run` records, in `procs[depth]`, the mode and the scanout base the
program started with. `k_tidy` puts **those** back, where today it puts the
kernel's own back. So:

- a program that changes the mode gets its parent's mode back when it ends,
  whether that parent is the shell, a script or another program;
- a `graphics.bin` run by a `# graphics 640x360` script ends in 640 × 360,
  since that is what it started in, and the script keeps its screen;
- nothing changes for anything that exists today: every program starts in
  the kernel's mode, so "the mode it started in" is the kernel's.

### 2.2 `setmode`, the system call (Q1)

Slot 24: **`int setmode(unsigned w, unsigned h)`**. The kernel switches the
**console's** mode: what `k_adopt` does at the prompt (`disp_setmode`,
`con_resize`, `con_redraw`), with the console's text kept (Phase 6, §2.2).
It also rewrites the started-in mode of the caller and of every program
below it, so that no `k_tidy` on the way back to the prompt undoes it.
It answers 0, or `E_NOMODE` for a mode the machine does not offer or a
machine without video memory.

Unlike the picker, it switches at once rather than at the next prompt. You
typed it, at a prompt, so the screen you are looking at is the one that
should change.

### 2.3 Console output in a program's own mode

While a program has the screen in a mode of its own (`pgs` in a
`# graphics 640x360` script, a game), the kernel's console is not on
screen. What the program prints still goes into the console's grid, and it
appears when the program ends and the console is drawn back. That is what
`# graphics` scripts already do with `keepscreen` *(checked)*. The
difference is that now even a `print` in the middle is not seen until the
end. `pgs` turns graphics off before it reports a mistake, so the message
it prints is the first thing on the console afterwards.

---

## 3. The programs

### 3.1 `/bin/setmode.bin`

```sh
2:/> setmode              →  192 x 108
2:/> setmode -list        →  192 x 108 (now)
                             320 x 180
                             640 x 360
                             854 x 480
                             1280 x 720
2:/> setmode 640 360      →  (nothing: the screen changes)
2:/> setmode 641 360      →  setmode: 641 x 360 is not a mode this machine offers
                             (and the list, as -list prints it)
```

It is silent when it succeeds, as `cd` is (Q4). The lists come from
`disp_modes`, and `640x360` is accepted as well as `640 360`. It goes on the
OS disc through `pigeon_compiler_init.txt`.

### 3.2 `# graphics WxH` in `pgs` (Q3)

`# graphics` stays exactly as it is. `# graphics 640x360` is the same
thing in that mode: `pgs` calls `disp_setmode` at `graphics_start`, the
point where it takes the screen, so a header with a mistake in it still
leaves the screen alone. A mode the machine does not offer is a mistake at
the header, with the modes listed. The mode is `pgs`'s own, so it goes back
by §2.1 when the script ends, however it ends.

### 3.3 `graphics.bin`

Nothing to do: it draws through the GAC at whatever mode it starts in
(Phase 5). What this phase adds is a test that it does so at 640 × 360,
under a script.

### 3.4 `/docs/gui.pgs` (Q5)

Your demo stays at 192 × 108 (Q5). The new `/docs/logo.pgs`-style demo for
a bigger screen goes in `docs/graphics.md`, and nothing on the disc changes.

---

## 4. The manuals

- **`docs/vram.md`** (new): the aperture and where it is, modes, surfaces,
  the page flip, upload and download, owners; the command table, and
  `<pigeon/vram.h>`.
- **`docs/gac.md`** (new): every command, surfaces and RAM surfaces,
  clipping, blending (alpha 1 to 254 blends, 0 and 255 are stored), `BATCH`,
  text; `<pigeon/gac.h>`; and when a program wants it directly rather than
  through `display.h`.
- **`docs/graphics.md`:** `# graphics WxH`, `setmode`, and the colour
  paragraph. It now says "write 0xFF…", and it gains what `0x80…` does
  through the accelerator.
- **`docs/pgs.md`** and **`docs/shell.md`:** `# graphics WxH`, and `setmode`
  among the commands.
- **`README.md`:** channels 9 and 10 in the channel table; `ram`, `vram`,
  `display_mode` and `display_modes` in the settings table; the aperture in
  the memory map; and a line on the picker.
- **`lib/pigeon/display.h`:** a pointer to `docs/gac.md`.
- **`docs/gac/README.md`:** Phases 1 to 7 built. Phase 8 is the only one
  left, and it is optional (bandwidth).

---

## 5. Steps

1. **The started-in mode** (§2.1): `procs[depth]` records it, and `k_tidy`
   restores it. Tests: a program that switches and runs a child keeps its
   mode when the child ends; a program that switches gets its parent's mode
   back; Phase 5's ownership tests still pass.
2. **The system call** (§2.2): slot 24, `sys.h`/`sys.c`, `kernel.asm`,
   `k_setmode`.
3. **`setmode.bin`** (§3.1), on the disc.
4. **`# graphics WxH`** (§3.2).
5. **The demo, end to end:** `setmode 640 360`, then the `graphics` line,
   through the shell, with a check that the disc is drawn at (320, 180) on a
   640 × 360 screen.
6. **The manuals** (§4).
7. **Full suite.**

**Done when:** the demo runs, from the shell, in a test; a
`# graphics 640x360` script draws at 640 × 360 through several
`graphics.bin` calls and leaves the screen as it found it; the manuals are
written; and the full suite passes.

---

## 6. Tests

- **`setmode`:** the current mode, `-list`, a switch that sticks across the
  commands after it (the console at 106 × 40, `consize` agreeing), a mode
  not offered, `640x360` as one word, and a machine with `--vram 0`.
- **`# graphics 640x360`:** a script of three `graphics` lines draws all
  three at 640 × 360. Afterwards the screen is 192 × 108 again and the
  console is back. A mode not offered is reported at the header, and the
  screen is left alone.
- **The started-in mode:** a stand-in that switches to 640 × 360 and runs a
  child that ends. The parent is still at 640 × 360 after that, and 192 × 108
  is back when the parent ends. The same with the child crashing.
- **The demo** (step 5).

---

## 7. Risks

- **`k_tidy`'s rule changes.** It is the tidy-up every program ending goes
  through. Mitigation: for every program that exists today, "the mode it
  started in" is the kernel's, so the rule is the old one. Phase 5's
  ownership and reboot tests, and Phase 6's console tests, all run it.
- **Two ways to switch the console:** `setmode` at once, and the picker at
  the next prompt. They share the code that does it (`k_adopt`'s body
  becomes `k_console_mode(w, h)`), so they cannot drift apart. The last
  request wins: a `setmode` followed by an older picker request still
  waiting would take the picker's. That seems right, since the picker's
  request is newer by the time the prompt comes round, but it is written
  down here so nobody is surprised.

---

## 8. Decisions

Answered 2026-09-19: *"i choose the recommendations. please start
building!"*. Every recommendation stands.

1. ~~**`setmode` through a new system call that switches the console's own
   mode?**~~ **Recommendation:** yes (§2.2). The alternatives are a
   `setmode.bin` that asks the way the picker does, by leaving a request the
   kernel takes at the next prompt (no new call, but the switch happens only
   after `setmode` has ended, and it cannot say whether it worked), or a
   shell built-in, which has the same problem as a program (§1.1).

   **Decided (you), 2026-09-19:** the recommendation.

2. ~~**`k_tidy` puts back the mode the program started in, not the
   kernel's?**~~ **Recommendation:** yes (§2.1). Without it a `# graphics`
   script cannot keep a mode past its first `graphics` line, and a game that
   runs a helper program loses its screen when the helper ends. For
   everything that exists today it is the same rule.

   **Decided (you), 2026-09-19:** the recommendation.

3. ~~**How does a script ask for a mode: `# graphics 640x360`?**~~
   **Recommendation:** yes, `WxH` after `graphics`, as `--mode` spells it on
   the command line. The alternatives are `# graphics 640 360`, which reads
   like two settings, or a separate `# mode 640x360`, which would mean a
   mode without graphics, and so a console nobody sees.

   **Decided (you), 2026-09-19:** the recommendation.

4. ~~**`setmode W H` silent when it works?**~~ **Recommendation:** yes, as
   `cd` and `mkdir` are. The screen changing is the answer. `setmode` alone
   prints the mode, and a failure prints why on stderr.

   **Decided (you), 2026-09-19:** the recommendation.

5. ~~**Leave `/docs/gui.pgs` at 192 × 108?**~~ **Recommendation:** yes. It is
   your demo, and its layout is written for that size. `docs/graphics.md`
   gets a short example at 640 × 360 instead. Or say so, and I will move it
   up to 640 × 360 and lay it out again.

   **Decided (you), 2026-09-19:** the recommendation.

6. ~~**A power-on mode for the OS, in `/etc/boot.conf`?**~~ Q14 decided the
   mode does not survive a reboot, with "a later one-liner in `boot.conf`"
   for a mode the OS starts in. **Recommendation:** still later, not in
   this phase. `config.json`'s `display_mode` already gives the machine a
   power-on mode, and the kernel takes whatever mode it boots in.

   **Decided (you), 2026-09-19:** the recommendation.

---

## 9. As built

Built 2026-09-19, as §2 to §5 describe, with every recommendation. **The demo
runs**, from the shell, in a test (`test_the_demo`): `setmode 640 360`, then
the `graphics` line, and the disc is at (320, 180) on a 640 × 360 screen.

**Found while building, and not in the plan: the console must not draw
while the screen is in a program's own mode.** Since Phase 6's picker, the
console can be in a mode whose screen lives in video memory. A program that
then switches to a mode of its own gets its screen placed where the
console's was, usually the same memory. Anything the program printed made
the kernel paint console text into the program's picture. So `display.h`
gains **`disp_hidden`**, which makes every drawing call a no-op, and
**`disp_follow()`**, which sets it when the machine's mode is not this
program's and, when it is, finds where the mode's screen now is. The kernel
calls `disp_follow()` once per console write and line read, not per
character, and after every `k_tidy`. What a program prints still goes into
the console's grid, and it appears when the console's mode is back. A test
has a program at 1280 × 720 paint the screen red and print. With the check,
no pixel of the red changes and the text is in the console afterwards.
Without it, 1,188 pixels are painted over.

**The rest is as planned:**

- **The started-in mode** (§2.1): `procs[depth]` keeps the mode and the
  screen's address each program started with (`k_started_in`), and `k_tidy`
  puts those back. A screen that was in video memory is put back as the mode's
  surface 0 rather than its old address, since a mode change can place it
  somewhere else. Every test from Phases 5 and 6 passed without a change,
  which is the check that for existing programs the rule is the old one.
- **`setmode`, slot 24** (§2.2): `k_setmode`, with the picker's switch
  (`k_adopt`) and it now sharing `k_console_mode`. That function also rewrites
  the started-in mode of every program running, so nothing puts it back on
  the way to the prompt. `E_NOMODE` (−24) is new, with
  "not a mode this machine offers" in `sys_strerror`.
- **`/bin/setmode.bin`** (§3.1), on the disc. The disc is 34 files now, and
  the two installer tests count it.
- **`# graphics WxH`** (§3.2): checked at the header, where a mistake lists
  the modes offered; switched at `graphics_start`.
- **The manuals** (§4): `docs/vram.md` and `docs/gac.md` are new;
  `graphics.md` has `# graphics 640x360` and the blending rule; `pgs.md` has
  the setting; `shell.md` has §9, `setmode`; the README has channels 9 and 10,
  `ram`, `vram`, `display_mode` and `display_modes`, the aperture in the
  memory map, and the Mode list.

**Tests:** 11 new cases in `test_kernel.py`: `setmode` in each form, sticking,
refused, and on a machine without video memory; the demo; a
`# graphics 640x360` script of three `graphics` lines, all at 640 × 360 and the
screen given back; a script asking for a mode that is not offered; a program
keeping its mode when its child ends or crashes; and the console not drawing
over a program's picture.

**The suite:** 1,879 passed, none failed.

