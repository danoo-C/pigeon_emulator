# `# graphics`: scripts that own the screen, and `graphics.bin`

> **Status: final plan, 2026-09-16; every question is decided
> ([§7](#7-your-answers)). Written because [pgs_plan.md](pgs_plan.md)'s F4
> asked for it, and built after `pgs` and after
> [redirect_plan.md](redirect_plan.md).** Your idea, from pgs_plan.md's Q13: a
> script that says `# graphics` gets the screen to itself, black and without
> a console on it, and draws on it by running `graphics.bin`. Facts marked
> *(checked)* were read in the code; the rest is reasoned.

---

## 1. What you asked for

From the chat, 2026-09-16:

> also i just had an idea. `#graphics` script wont show a shell. just a black
> screen, and later we can add graphics.c which will allow commands like
> `graphics.bin -rect 0 0 10 10 0xFF00FF00` — `x y w h color` — and the same
> for circles and text. also a `graphics.bin -px x y col`

and, when I asked whether one call should carry more than one shape
*(pgs_plan.md F5)*:

> yeah, why not even combining `graphics -f picture.gfx 10 10 50 50 STRECH
> -rect 0 0 10 10 0xFF00FF00 -circle 96 54 20 0xFFFF0000`

So: a drawing program that takes as many operations as you care to write on
one line, and a script mode that gives it a clean screen to draw on.

---

## 2. Where things stand

- **The screen is memory:** `0xAARRGGBB` a pixel at `DISPLAY_START`, 192×108,
  and `<pigeon/display.h>` has `disp_clear`, `disp_set`, `disp_rect`,
  `disp_frame`, `disp_line`, `disp_circle`, `disp_hline`, `disp_vline` and a
  5×7 font in `disp_char`/`disp_text` *(checked: `lib/pigeon/display.h`)*.
  Everything `graphics.bin` needs to draw already exists.
- **Images too:** `<pigeon/bmp.h>`'s `bmp_load(path, w, h, BMP_CROP |
  BMP_STRETCH)` reads a BMP into pixels at a size you ask for, and
  `/bin/img.bin` is 52 lines of exactly that *(checked: `lib/pigeon/bmp.h`,
  `user/os/bin/img.c`)*.
- **The kernel redraws its console after every program.** `k_tidy` calls
  `con_redraw`, which clears the screen and draws the console's text on it
  *(checked: `user/os/kernel.c`)*. **This is the whole problem:** a script
  that runs `graphics` twice gets its first picture wiped before the second
  call draws. The explorer's `[ press any key ]` pause exists for the same
  reason *(built: [explorer.md](explorer.md) §4)*.
- **A program's own output lands on that console** *(checked: `k_write`)*, so
  a stray `echo` in a graphics script paints text over the picture.
- **`pgs` will run every command through `exec`, and can run one through
  `exec_out` instead** *([pgs_plan.md](pgs_plan.md) §4.5)*, which is a ready
  way to keep a command's output off the screen.
- **A program that loads is tens of kilobytes off the disk:** `img.bin` and
  friends are 50–120 KB *(checked: `build/pigeonos/bin/`)*, and `exec` reads
  the whole file *(checked: `k_run`)*. That is why one call should carry many
  shapes.
- **There are 10 free system-call slots** after `pgs` and redirection take
  theirs *(checked: `syscall.h`, `memory_map.py:133`)*.
- **`img` holds its picture by not returning:** it waits for Esc, and the
  console comes back when it does *(checked: `img.c`)*. Anything that draws
  and returns is wiped at once.

---

## 3. The goal

```sh
# graphics                          ;; this script owns the screen

graphics -clear 0xFF101018 \
         -f logo.bmp 10 10 50 50 STRETCH \
         -rect 0 0 10 10 0xFF00FF00 \
         -disc 96 54 20 0xFFFF0000 \
         -text 4 100 "PigeonOS" 0xFFFFFFFF

$key = $(graphics -wait)            ;; hold it, and say which key ended it
```

The `\` at the end of a line is new: `pgs` has no continuation until this
plan adds one (Q8), and a drawing call is what it was invented for.

One program load, five operations, and a screen that stays until the script
says otherwise. Without `# graphics` the same line still draws — and the
kernel paints its console back over it the moment `graphics` returns, which
is what `-wait` is for at the prompt (Q3).

---

## 4. The design

### 4.1 `graphics.bin`: one call, many operations

`user/os/bin/graphics.c` → `/bin/graphics.bin`, over `<pigeon/display.h>` and
`<pigeon/bmp.h>`. The argument list is read left to right and each operation
takes its own arguments:

| | |
|---|---|
| `-clear COLOUR` | the whole screen |
| `-px X Y COLOUR` | one pixel |
| `-rect X Y W H COLOUR` | filled |
| `-frame X Y W H COLOUR` | outline |
| `-line X0 Y0 X1 Y1 COLOUR` | |
| `-circle X Y R COLOUR` | an outline |
| `-disc X Y R COLOUR` | filled (Q7) |
| `-text X Y "WORDS" COLOUR` | the 5×7 font |
| `-f FILE X Y W H MODE` | a BMP, at that place and size (Q1) |
| `-wait` | hold the screen until a key, and say which |

A colour is `0xAARRGGBB` — alpha included, since the display takes it as
written and `0x00…` is invisible *(checked: `display.h`)*. Numbers are
decimal or `0x…`. A wrong operation, a missing argument or a colour that
isn't a number is one line on the console and status 1, and **nothing at all
is drawn**: the list is checked before the first pixel, so a typo in the
sixth shape doesn't leave you with five (Q2).

Sizes come from the screen, so `-rect 0 0 192 108` is the whole of it; a
shape off the edge is clipped by the library and not an error.

**`-f` draws an image (Q1),** and `MODE` is one of `bmp.h`'s three, which is
the list you half-remembered: `STRETCH` scales all of it to `W x H`, `CROP`
takes the middle `W x H` at the image's own size, and `CROP_TOP_LEFT` takes
that corner, black where the image is smaller *(checked: `lib/pigeon/bmp.h`,
`BMP_STRETCH`, `BMP_CROP`, `BMP_CROP_TOP_LEFT`)*. So `-f` is `img` with a
place and a size, and `bmp_load` does the work. A file of `graphics`
operations, replayed, is a second little format and is not in this plan.

**`-wait` holds the screen until a key and prints its code (Q3),** so
`$key = $(graphics -wait)` lets a script branch on what was pressed — 27 is
Esc *(checked: `lib/pigeon/input.h`, `KEY_ESC`)*. Ctrl+C ends it the way it
ends any program. It is also what makes `graphics` usable at the prompt at
all: without `# graphics`, the kernel paints its console back the moment
`graphics` returns *(§2)*.

**`-rect` is filled and `-frame` is not**, because the library has both
*(checked: `display.c`)*. **`-circle` is an outline and `-disc` is filled
(Q7)**; the filled one goes into `<pigeon/display.h>` as `disp_disc`, not
into `graphics.c`, so `graph.c` and anything else can have it.

### 4.2 `# graphics` in `pgs`

The directive *([pgs_plan.md](pgs_plan.md) §4.2)* in a script's header turns
on three things:

1. **A black screen to start with:** `pgs` clears it once, before the first
   command.
2. **Nothing else may draw on it.** Every command the script runs goes
   through `exec_out` with a buffer `pgs` throws away, so a program that
   prints paints nothing. `echo` in a graphics script writes to that same
   buffer, which is to say nowhere, and is not an error (Q4): a script gets
   run both ways while it is being written, and `docs/pgs.md` says where the
   output went.
3. **The console stays off the screen between commands,** which is the part
   the kernel has to grow (§4.3).

When the script ends, the console comes back the way it does after any
program, and the picture is gone with it. A script that wants its picture
looked at ends with `graphics -wait`.

Errors are the exception to (2): a script that stops on a mistake turns the
mode off first, so `hello.pgs:7: no such variable: $nmae` is on a console you
can read.

### 4.3 What the kernel has to grow

```c
/* <pigeon/sys.h> */
int keepscreen(int on);     /* -> what it was */
```

While it is on for a program, `k_tidy` skips `con_redraw` for the programs
that program runs — the console's text is kept, but it is not painted back
over the screen between one child and the next. It is `paging()`'s shape
exactly *(checked: `kernel.c` `k_paging`, and `page_owner`'s depth rule)*:
the depth that turned it on owns it, deeper programs inherit it, and `k_run`
clears it when that program ends, so a script that crashes cannot leave the
console invisible.

The owner's *own* tidy still redraws, which is what brings the console back
when the script ends.

Two things it deliberately does not do: it does not stop a program writing to
the console — `# graphics` handles that by capturing (§4.2) — and it does not
change where the display reads from, so `k_tidy` still points it at
`DISPLAY_START` after a program that page-flipped *(checked: `k_tidy`)*.

### 4.4 The disc

- `/bin/graphics.bin`, and a `/docs/logo.pgs` drawing something with it: 32
  files.
- `/etc/explorer.conf` needs nothing new — a `.pgs` file already opens with
  `pgs`.

---

## 5. Steps

1. **`disp_disc` in `<pigeon/display.h>`** (Q7), beside `disp_circle`.
   *Tests* in `tests/test_display.py`, where the other shapes are tested.
2. **`graphics.bin`:** the argument list, the shapes, the text, `-wait`, the
   checks in §4.1. *Tests* in `tests/test_graphics.py`: each operation draws
   what it says at the pixel level, several in one call, a bad list drawing
   nothing at all, `-wait` holding until a key and printing its code. It runs
   under the kernel like any program, so the harness is
   `tests/test_kernel.py`'s.
3. **`keepscreen` in the kernel:** `syscall.h`, `kernel.asm`, `kernel.c`,
   `sys.c`, `sys.h`. *Tests* in `tests/test_kernel.py`: the console is not
   redrawn after a child while it is on, it is again when the owner ends, a
   program that faults leaves it off, and deeper programs inherit it.
4. **`# graphics` in `pgs`, and the `\` continuation** *(Q8)*: the directive,
   the black start, every command captured, the mode off before an error, and
   a line that ends in `\` joined to the next. *Tests* in
   `tests/test_pgs.py`: a script that draws twice keeps both, an `echo` paints
   nothing, an error reaches a readable console, a continued line is one
   command, a `\` inside quotes is not a continuation.
5. **`-f` images** *(Q1)*, on `bmp.h`.
6. **The disc and the docs:** `/docs/logo.pgs`, `docs/graphics.md`,
   `docs/pgs.md`'s directive list, and this plan's "as built".

---

## 6. Risks

- **A picture that outlives its script.** `keepscreen` is cleared when the
  owner ends, and the console is redrawn then, so the screen always comes
  back. The failure to watch for is the opposite — a script that draws and
  exits looks like it did nothing, because the console lands on top a
  millisecond later. `-wait` is the answer and the docs have to say so.
- **One program load a call**, tens of kilobytes *(§2)*. Many shapes in one
  call is the fix (F5 in pgs_plan.md), and a script drawing an animation
  frame by frame is still a program load a frame — at which point it wants to
  be a C program, not a script, and the docs should say that too.
- **`-text` is the 5×7 font** and nothing else; a script wanting big letters
  wants `graph.c`'s kind of drawing, not this.
- **No double buffering.** `graphics` draws straight to the screen, so a
  slow list is drawn visibly. `disp_use_back_buffer` across separate program
  runs is not possible — each run would have to flip a buffer the next one
  knows nothing about *(checked: `display.c`, `disp_back` is a program's own
  heap)*.

---

## 7. Your answers

Answered in this file on 2026-09-16.

1. ~~**What `-f FILE X Y W H STRETCH` reads:** a BMP, a file of drawing
   commands, or both by extension?~~

   Answer: the numbers are X Y W H STRECH/CROP/the other crop, i dont
   remember how exactly its called

   **Decided (you):** an image. The modes are `bmp.h`'s three and the one you
   couldn't name is `CROP_TOP_LEFT` *(checked: `lib/pigeon/bmp.h`)*, so the
   operation is `-f FILE X Y W H STRETCH|CROP|CROP_TOP_LEFT` (§4.1). A file
   of drawing commands is not in this plan.

2. ~~**A bad operation part-way through a list:** nothing, or up to the
   mistake?~~

   Answer: yeah, dotn draw anything

   **Decided (you):** nothing at all; the list is checked before the first
   pixel (§4.1).

3. ~~**`-wait`:** return the key, and Esc and Ctrl+C both end it?~~

   Answer: yyyes, thats an amazing idea.

   **Decided (you):** it prints the key's code, so `$key = $(graphics -wait)`
   can be branched on, and Ctrl+C ends it like any program (§4.1).

4. ~~**`echo` inside a `# graphics` script:** silently lost, or an error?~~

   Answer: yes that is correct

   **Decided (you):** silently lost, so a script can be run both ways while
   it is written; the docs say where it went (§4.2).

5. ~~**Anything else `# graphics` has to hide?**~~

   Answer: nothing

   **Decided (you):** nothing — the console is the only thing in the way
   (§4.3).

6. ~~**`graphics.bin` next to `img`.**~~

   Answer: let img be img, its a quick way to see images, and the graphics is
   its big brother

   **Decided (you):** `img` stays exactly as it is (§6).

7. ~~**A filled circle?**~~

   Answer: yes add both

   **Decided (you):** `-circle` outline and `-disc` filled, with `disp_disc`
   added to `<pigeon/display.h>` so everything else can use it too (§4.1,
   step 1).

8. ~~**A `\` line continuation in `pgs`?**~~

   Answer: i dont really care.

   **Decided (left to me):** add it, with this plan (step 4). A drawing call
   is the one line long enough to want it, §3's example uses it, and it is
   about ten lines in `pgs`'s line reader. `pgs` ships without it until then,
   as [pgs_plan.md](pgs_plan.md) §4.2 says.
