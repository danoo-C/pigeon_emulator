# `<pigeon/bmp.h>`: loading a 24-bit bitmap

> **Status: final plan, 2026-09-15; your answers are folded in
> ([§8](#8-your-answers)), and built the same day ([§9](#9-as-built)).** A small library that reads a BMP file and hands back its
> pixels at the size asked for, cropped or stretched, in the screen's own
> format. Facts marked *checked* were read in the code, *measured* ones were
> run; the rest is reasoned.

---

## 1. What you asked for

From the chat, 2026-09-15:

- **`int *bmp_load(char *path, int w, int h, CROP/STRETCH)`,** returning an
  array of raw pixels.
- **For loading `pigeon.bmp`,** a 24-bit bitmap, which is in `etc/bmp/`.

---

## 2. Where things stand

- **The screen is 192 × 108, one word a pixel, `0xAARRGGBB`.** Alpha isn't
  blended; it goes straight to the canvas, so every pixel wants `0xFF`
  *(checked: `lib/pigeon/display.h:8–10`, `emulator/memory_map.py:98–99`)*.
  A 24-bit BMP's colours fit it exactly.
- **The display library has no call that draws a block of pixels:** it sets
  pixels and draws lines, rectangles and text *(checked: `display.h:73–82`)*.
  A full-screen image is one `memcpy` into the framebuffer.
- **The heap:** `malloc` and `free` *(checked: `lib/pigeon/mem.h:20–22`)*. It
  doesn't join freed blocks back together *(checked: `lib/README.md`)*.
- **Reading a file through the kernel:** `stat` gives its size, and `read`
  passes any length straight to `fs_read`, so a whole file is one call
  *(checked: `lib/pigeon/syscall.h:50`, `user/os/kernel.c`, `k_read`)*. There
  is no seek.
- **Without a kernel,** a program reads files with `fs.h`'s `fs_load_alloc`
  *(checked: `fs.h:136`)*; `sys.h`'s calls jump through an empty table there,
  which `stdio.c` checks for before calling *(checked: `sys.h`, `stdio.c`)*.
- **What reading a file costs a program** *(measured)*:
  - through the kernel, `sys.h` and `mem.h`: 12,160 bytes;
  - with its own filesystem, `fs.h` and `mem.h`: 103,512 bytes.
- **A header drags in its library:** a program that includes `display.h`
  gets `display.c` and its font *(checked: `emulator/programs.py`,
  `libraries_for`)*, so `bmp.h` shouldn't include it just for `color_t`.
- **Two images are in `user/os/etc/bmp/`,** not yet committed or on the
  disc: `pigeon.bmp` and `eye-mask.bmp`. Both are 192 × 108, 24 bits a pixel,
  uncompressed, with a 40-byte header and their pixels at byte 54: 62,262
  bytes, the screen's size exactly *(checked)*.
- **Decoding is cheap enough** *(measured, prototypes in scratch, not in the
  repo)*: a 192 × 108 screen from a 24-bit BMP already in memory, cropped
  from a 192 × 108 image or stretched from a 1024 × 576 one, which cost the
  same:

  | How | Instructions | A pixel |
  |---|---|---|
  | three bytes a pixel, its source column worked out each time | 1,978,363 | 95.4 |
  | the columns worked out once, in a table | 1,822,266 | 87.9 |
  | the table, and each pixel read as one word and masked | 868,518 | 41.9 |

  The last was checked pixel for pixel against the same crop and stretch in
  Python. It is about a third of a second of emulator time. The trick: the
  file stores a pixel as B, G, R, and a word read at those bytes is
  `0x..RRGGBB` already, so a pixel is one load, one AND and one OR.

---

## 3. The goal

```c
#include <pigeon/bmp.h>
#include <pigeon/mem.h>

unsigned *pixels = bmp_load("/etc/bmp/pigeon.bmp", DISP_W, DISP_H, BMP_STRETCH);
if (pixels == NULL) {
    print(bmp_strerror(bmp_error()));
    return 1;
}
memcpy((void *)DISP_BASE, pixels, DISP_W * DISP_H * 4u);
free(pixels);
```

---

## 4. The design

### 4.1 The calls

```c
#define BMP_CROP          0      /* the middle of the image, at its own size   */
#define BMP_CROP_TOP_LEFT 1      /* its top-left corner, at its own size       */
#define BMP_STRETCH       2      /* all of it, scaled to w x h                 */

unsigned *bmp_load  (char *path, unsigned w, unsigned h, int mode);  /* through the kernel */
unsigned *bmp_decode(unsigned char *file, unsigned size,
                     unsigned w, unsigned h, int mode);              /* a file in memory   */
int       bmp_info  (char *path, unsigned *w, unsigned *h);          /* its own size (Q9)  */
int       bmp_error (void);                                          /* why the last NULL  */
char     *bmp_strerror(int err);
```

- **What comes back:** `w × h` words, row by row from the top left, each
  `0xFFRRGGBB`, in memory from `malloc`, which the caller frees. It's the
  screen's own format, so a full-screen one copies straight in.
- **`unsigned *`, not `int *`** (Q1): the same bits as `color_t`, which is
  `unsigned int`, without `bmp.h` including `display.h`.
- **`bmp_load` reads through the kernel** (Q2): `stat`, then one `read` of
  the whole file into the heap, `bmp_decode`, and the file's memory freed.
  Without a kernel it returns `NULL` with `BMP_ENOKERNEL` rather than call
  through the empty table; a program with no kernel loads the file with
  `fs_load_alloc` and hands it to `bmp_decode`.

### 4.2 The file

- **24-bit and 32-bit, uncompressed** (Q5): `BM`; the pixels' offset at
  byte 10; a header of at least 40 bytes at 14; the width at 18 and the
  height at 22, negative for rows stored top-down; one plane; 24 or 32 bits a
  pixel at 28; compression 0 at 30.
- **A 32-bit file is brought down to 24-bit colour:** its fourth byte, the
  alpha, is dropped, and every pixel comes back opaque, since the screen
  doesn't blend. One saved with compression 3, bit fields, is taken too when
  its masks are the usual `0x00FF0000`, `0x0000FF00` and `0x000000FF`, which
  is how most editors write a 32-bit BMP.
- **Rows are padded to 4 bytes,** which a 32-bit row never needs, and stored
  bottom-up unless the height is negative.
- **Checked before any pixel is read:** the pixels fit inside the file, the
  width and height aren't 0, and the image is at most 8,192 × 8,192 (Q7).
- **The one-byte read past a pixel,** in a 24-bit file: reading a pixel as a
  word touches the byte after it. That is the row's padding, or the next row, except for the
  last pixel of a row with no padding at the end of the file. `bmp_load`
  allocates one spare byte for it; `bmp_decode`, given someone else's buffer,
  reads that one pixel as three bytes instead.

### 4.3 Crop and stretch

- **`BMP_CROP`** (Q3): the middle `w × h` of the image, at its own size. Where
  the image is smaller than `w × h` it's centred, on black (`0xFF000000`).
- **`BMP_CROP_TOP_LEFT`** (Q3): the same from the image's top-left corner.
  Where the image is smaller, it sits in the top-left, with black to its
  right and below.
- **`BMP_STRETCH`** (Q4): the whole image scaled to `w × h`, each pixel taken
  from the nearest source pixel, `x × W / w`. The aspect isn't kept.
- **The output:** `w` and `h` from 1 to 4,096 (Q7); `w × h × 4` must fit the
  heap, or `BMP_ENOMEM`.
- **How:** a table of `w` source columns, worked out once a call, and a
  pixel read as one masked word (§2).

### 4.4 Errors

`NULL` from `bmp_load` and `bmp_decode`, and why from `bmp_error()` (Q6):

- **a file error,** as the kernel returns it (not found, is a directory, and
  so on), which `bmp_strerror` names as `sys_strerror` does;
- **`BMP_ENOKERNEL`:** `bmp_load` with no kernel, so use `bmp_decode`;
- **`BMP_ENOTBMP`:** no `BM`, or a header too short to be one;
- **`BMP_EFORMAT`:** not 24- or 32-bit, compressed but for a 32-bit file's
  usual bit fields, or not one plane;
- **`BMP_ETRUNCATED`:** the file ends before its pixels do;
- **`BMP_ETOOBIG`:** over the limits in Q7;
- **`BMP_ENOMEM`:** the heap couldn't hold the file or the result;
- **`BMP_EARGS`:** a width or height of 0, or a mode that isn't one.

Its own codes are below any the kernel or `fs.h` use, so one number never
means two things.

### 4.5 Where it goes

`lib/pigeon/bmp.h` and `bmp.c`, `tests/test_bmp.py`, and a section in
`lib/README.md`.

---

## 5. Steps

**Step 1. The decoder.** `bmp_decode`, `bmp_error` and `bmp_strerror`.
- **Tests,** on a bare CPU with the file placed in RAM, against the same crop
  and stretch in Python on BMPs Python writes:
  - 192 × 108 cropped to itself; 191 × 107 and 190 × 106, for each padding;
    1 × 1; top-down;
  - 32-bit files, plain and with bit fields, their alpha dropped; bit fields
    with other masks refused;
  - 1024 × 576 stretched down, and 64 × 36 stretched up;
  - cropping a bigger image to its middle and to its top-left corner, and a
    smaller one centred, or in the corner, on black;
  - `etc/bmp/pigeon.bmp` itself, against Python;
  - every error in §4.4 but the kernel's;
  - the last pixel of a file with no padding, not read past its end;
  - the cost, counted: under 50 instructions a pixel.

**Step 2. `bmp_load` and `bmp_info` through the kernel.**
- **Tests,** under the kernel, as `test_kernel.py` runs programs: a stand-in
  that loads a BMP from the disk and copies it to the screen, read back from
  the framebuffer; a missing file; a file that isn't a BMP; `bmp_info`; and
  `bmp_load` on a bare CPU returning `BMP_ENOKERNEL`.

**Step 3. Docs.** `lib/README.md`: the table's row and a `bmp` section.

| Step | Needs | Size |
|---|---|---|
| 1. The decoder | — | small to medium |
| 2. Through the kernel | 1 | small |
| 3. Docs | 1, 2 | small |

One commit, after the whole suite and deliberate breakages for every piece.

---

## 6. Risks

- **Memory at once:** the file, the result and the column table. A 1024 × 576
  BMP is 1.77 MB and its screen 82,944 bytes, well inside the heap. The heap
  doesn't join freed blocks, so a program loading many images one after
  another fragments it.
- **Speed:** about a third of a second for a screen, plus reading the file.
- **Stretching a big image down** skips pixels rather than averaging them,
  so fine detail can shimmer. Averaging would cost far more; nearest is what
  was asked for.

---

## 7. Not in this plan

- **Showing `pigeon.bmp` in the splash, and putting it on the disc** (Q8):
  done straight after, as `/bin/splash.bin` drawing `/etc/bmp/pigeon.bmp`
  ([phase6_plan.md](phase6_plan.md) §10), and `eye-mask.bmp` after it, for
  the eyes to flash.
- **Other formats:** PNG, 8-bit palettes, and compressed or 16-bit BMPs.
- **Keeping the aspect** with black bars, or smoothing when stretching (Q4).
- **A drawing call** such as `disp_blit`: the result is already in the
  screen's format.

---

## 8. Your answers

Answered in the chat, 2026-09-15.

1. ~~**The return type:** `unsigned *`, `int *` as you wrote it, or
   `color_t *`?~~

   Answer: usnigned

   **Decided (you):** `unsigned *` (§4.1).

2. ~~**Reading the file:** through the kernel, with `bmp_decode` for programs
   that run without one, or with `fs.h`, which works anywhere?~~

   Answer: kernel

   **Decided (you):** through the kernel, with `bmp_decode` for a file already
   in memory (§4.1).

3. ~~**Crop:** the middle of the image, and black around one that's smaller, or
   its top-left corner?~~

   Answer: both

   **Decided (you):** both, a mode each: `BMP_CROP` from the middle and
   `BMP_CROP_TOP_LEFT` from the corner, with black around a smaller image
   in either (§4.3).
   **Reply:** read as both kinds of crop. If you meant yes to both halves of
   the suggestion, the middle and the black, that's `BMP_CROP` alone, and the
   corner mode is a few lines to leave out.

4. ~~**Stretch:** the nearest pixel, with the aspect not kept? And do you want
   a third mode, `BMP_FIT`, keeping the aspect with black bars?~~

   Answer: not keeping aspect

   **Decided (you):** the nearest pixel, the aspect not kept, and no
   `BMP_FIT` (§4.3).

5. ~~**Formats:** 24-bit only, or 32-bit as well?~~

   Answer: we can downscale

   **Decided (you):** 32-bit files too, brought down to 24-bit colour, their
   alpha dropped (§4.2).
   **Reply:** read as the colour depth, since the question was about formats.
   Decided with it (left to me): a 32-bit file saved with bit fields is taken
   when its masks are the usual ones.

6. ~~**Errors:** `NULL`, with `bmp_error()` and `bmp_strerror()` saying why?~~

   Answer: i dont care.

   **Decided (left to me):** as suggested (§4.4).

7. ~~**Limits:** images up to 8,192 × 8,192, and results up to 4,096 × 4,096?~~

   Answer: okay

   **Decided (you):** those limits (§4.2, §4.3).

8. ~~**`pigeon.bmp`:** it isn't in the project. Will you add it, and where
   should it live, `/etc/pigeon.bmp`? And should the splash show it as part of
   this work, or next?~~

   Answer: i doesnt matter now, but its in etc/bmp/

   **Decided (you):** not part of this work. The images are in
   `user/os/etc/bmp/` (§2), which on the disc would be `/etc/bmp/` (§7).

9. ~~**`bmp_info`:** a call giving an image's own width and height without
   decoding it, so a program can load one at its own size?~~

   Answer: okay.

   **Decided (you):** `bmp_info` (§4.1).

---

## 9. As built

- **`lib/pigeon/bmp.h` and `bmp.c`,** as §4: `bmp_load`, `bmp_decode`,
  `bmp_info`, `bmp_error` and `bmp_strerror`, the three modes, 24- and 32-bit
  files, and the errors numbered from -300 to -306.
- **No spare byte, and no special last pixel.** §4.2 planned both, against
  reading the byte just past a file. On this machine that read can't fault:
  every address is RAM, and a read at the top of memory wraps round
  (`emulator/ram.py`, `read_word`). And the OR that makes a pixel opaque
  throws that byte away, so no test could tell either way. So neither was
  built, and `bmp.c` says why.
- **A directory** needs no check of its own: opening one through the kernel
  fails with its "is a directory", which `bmp_strerror` passes on.
- **Tests:** 43, in `tests/test_bmp.py`: the decoder on a bare CPU, checked
  pixel for pixel against Python, `etc/bmp/pigeon.bmp` among the images; a
  screen at under 50 instructions a pixel; `bmp_load` and `bmp_info` under
  the kernel; and `BMP_ENOKERNEL` with no kernel. 33 deliberate breakages:
  30 failed a test, and the other 3 changed nothing a test could see, since
  they broke code that could never matter: the corner crop's check for a
  position past the image, which its callers make again, and a check for a
  directory in `bmp_load` and `bmp_info`, which the kernel makes. That code
  is gone. The full suite passed: 1,450 tests.
