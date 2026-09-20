# `graphics`: drawing from the shell and from scripts

> **Status: built,** `user/os/bin/graphics.c` and `# graphics` in
> `user/os/bin/pgs.c`, planned in [graphics_plan.md](graphics_plan.md) and
> built to it on 2026-09-17. `/bin/graphics.bin` is an ordinary program, and
> the mode it draws in is one kernel call, `keepscreen` ([kernel.md](kernel.md)
> §10). Tests: `tests/test_graphics.py`, `tests/test_pgs.py` and the kernel's
> own.

---

## 1. One call, many shapes

```sh
2:/> graphics -clear 0xFF101018 -disc 96 54 20 0xFFFF0000 -wait
```

The argument list is read left to right and each operation takes its own
arguments, as many to a call as a line holds:

| | |
|---|---|
| `-clear C` | the whole screen |
| `-px X Y C` | one pixel |
| `-rect X Y W H C` | filled |
| `-frame X Y W H C` | an outline |
| `-line X0 Y0 X1 Y1 C` | |
| `-circle X Y R C` | an outline |
| `-disc X Y R C` | filled |
| `-text X Y WORDS C` | the 5×7 font; quote it if it has spaces in it |
| `-f FILE X Y W H MODE` | a BMP, at that place and size |
| `-wait` | hold the screen until a key, and print its code |

**A colour is `0xAARRGGBB`**, and an alpha from `0x01` to `0xFE` **blends**:
`0x80FF0000` is half red over what is already there, so shapes can be laid
over a picture. `0xFF...` is solid, and `0x00...` is stored as it is, which
means black: the screen shows every pixel opaque whatever its alpha
([gac.md](gac.md) §3). **Numbers are decimal or `0x...`,** and may be negative where a shape
starts off the screen. **`MODE`** is `STRETCH`, `CROP` or `CROP_TOP_LEFT`
(`<pigeon/bmp.h>`): `STRETCH` scales the whole image into `W x H`, `CROP`
takes the middle `W x H` of it at its own size, and `CROP_TOP_LEFT` that
corner, on black where the image is smaller.

The screen is 192 × 108, so `-rect 0 0 192 108 C` is all of it. A shape over
an edge is clipped, which is not a mistake.

**Why one call carries so much:** the program is 92 KB off the disk, and
`exec` reads all of it. Drawing is the cheap part. A shape a call would spend
its whole time loading — and an animation, a frame a call, wants to be a C
program over `<pigeon/display.h>` instead.

## 2. A list with a mistake in it draws nothing

```
2:/> graphics -clear 0xFF000000 -rect 0 0 50 50 0xFF00FF00 -disc 96 54 20 red
graphics: not a colour: red
```

The whole list is checked before the first pixel: an unknown operation, a
missing argument, a number that is not one, a colour that is not one, a mode
that is not one, or an image that cannot be read. So a typo in the sixth
shape leaves you with none, rather than with five and a puzzle. `-wait`
ahead of the mistake does not hold the screen either, since nothing has run
yet.

Only one thing can fail once drawing has begun: an image the heap cannot
hold. That stops the rest of the list.

## 3. `-wait`, and why a picture needs it

The kernel paints its console back over the screen the moment a program ends
([kernel.md](kernel.md) §11, `k_tidy`). So at the prompt:

```sh
2:/> graphics -disc 96 54 20 0xFFFF0000
2:/> graphics -disc 96 54 20 0xFFFF0000 -wait
```

The first is drawn and gone before you see it. The second is there until you
press a key.

`-wait` holds it until a key goes down and prints that key's code — `27` for
Esc — so a script can ask which one it was:

```sh
$key = $(graphics -wait)
```

Ctrl+C ends it the way it ends any program. Events queued before the picture
was drawn are thrown away first, so the Enter that started the command does
not end the wait.

## 4. `# graphics`: a script that owns the screen

A `.pgs` script whose header says `# graphics` gets the screen to itself
([pgs.md](pgs.md) §2):

```sh
# graphics

graphics -clear 0xFF101018 \
         -disc 150 22 12 0xFFFFCC33 \
         -rect 0 82 192 26 0xFF1E3A1E

$key = $(graphics -f /etc/bmp/pigeon.bmp 60 14 72 62 STRETCH \
                  -text 68 90 PigeonOS 0xFFFFFFFF \
                  -wait)
```

Three things it turns on:

1. **A black screen** before the first command, so the console the shell left
   behind is not under the picture.
2. **Nothing else paints.** Every command the script runs goes through the
   kernel's `exec_out` into a buffer that is thrown away, and a builtin's
   output goes the same nowhere. An `echo` in a graphics script prints
   nothing and is **not** a mistake — a script is run both ways while it is
   being written. A redirection still writes its file.
3. **The console stays off the screen between commands,** which is
   `keepscreen` (§5). Without it the second call above would find the first
   one's picture already wiped.

A mistake turns the mode off before it says so, so the message is on a
console you can read; the console comes back when the script ends, whatever
happened. **`graphics`'s own complaints are the exception:** they are a
program's output, which (2) throws away with everything else, so a script
whose picture is missing a shape is worth running once without the
`# graphics` line to see what it says. `pgs`'s messages are not captured —
they go to STDERR, which nothing takes.

`/docs/logo.pgs` on the installed disc is the worked example.

### A bigger screen: `# graphics 640x360`

The header can name a mode, one the machine offers (`setmode -list`,
[shell.md](shell.md) §9):

```sh
# graphics 640x360

graphics -clear 0xFF101018 \
         -disc 320 180 60 0xFFFF0000 \
         -rect 0 330 640 30 0xFF1E3A1E \
         -text 280 342 "640 x 360" 0xFFFFFFFF
graphics -disc 360 150 40 0x8033CCFF -wait
```

The screen is that size from the first command, and the second disc is laid
half over the first. **The mode is the script's:** every `graphics` line it
runs starts and ends in it, and when the script ends, however it ends, the
screen is back the way it found it. A mode the machine does not offer is a
mistake at the header, with the modes it does offer, and the screen is not
touched. `# graphics` alone is the screen as it already is.

## 5. What the kernel had to grow: `keepscreen`

```c
int keepscreen(int on);     /* -> what it was */
```

While it is on, the kernel does not redraw its console after the programs
that program runs — the console's text is kept, it is simply not painted
back over the picture between one child and the next. It has `paging()`'s
shape exactly: the depth that turned it on owns it, deeper programs inherit
it, and the kernel clears it when that program ends, however it ended, so a
script that crashes cannot leave the console invisible. The owner's own tidy
still redraws, which is what brings the console back.

Two things it does not do: it does not stop a program writing to the console
(a `# graphics` script handles that by capturing), and it does not move the
display, so a program that page-flipped is still pointed back at
`DISPLAY_START` afterwards.

## 6. Limits worth knowing

- **32 words a line** in the shell and in `pgs`, `graphics` itself included.
  That is 5 shapes and a `-wait`, give or take; it was 16 before this, which
  a drawing call outgrew at once.
- **255 characters a typed line.** A `.pgs` line has no such limit, and `\`
  at the end of one carries it on to the next.
- **`-text` is the 5×7 font** and nothing else. Bigger letters want a C
  program.
- **No double buffering.** `graphics` draws straight to the screen, so a long
  list is drawn visibly. Two separate runs cannot share a back buffer — each
  would have to flip one the next knows nothing about.
- **`img` is still `img`:** a quick look at a BMP, full screen, until Esc.
  `graphics -f` is the same picture with a place and a size.

## 7. Where things are

| | |
|---|---|
| `user/os/bin/graphics.c` | the program |
| `lib/pigeon/display.h` | the shapes, `disp_disc` included |
| `lib/pigeon/bmp.h` | `-f`, through `bmp_load` |
| `user/os/kernel.c` | `keepscreen`, slot 22 |
| `user/os/bin/pgs.c` | `# graphics`, `# graphics WxH`, and `\` |
| `user/os/bin/setmode.c` | `setmode` ([vram.md](vram.md)) |
| `user/os/docs_logo.pgs` | `/docs/logo.pgs`, the example |
| `tests/test_graphics.py` | the operations, at the pixel |
