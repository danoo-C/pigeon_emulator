# Phase 9: a blit that honours each source pixel's own alpha

> Part of [the GAC plan](../README.md). **Status: planned and every question
> decided ([§10](#10-questions)), 2026-09-20 — you took every recommendation;
> built the same day ([§11](#11-as-built)).** Needs Phase 3 (built). Design:
> [design.md §4.5](../design.md#45-blending-you-are-right-that-it-is-easy-and-that-is-the-trap),
> which measured the obvious version, called it 0.28 s a screen and left it
> out of v1. This is the plan of its own that [README.md §9](../README.md#9-not-in-this-plan)
> promised. Facts marked *(checked)* were read in the code, and *(measured)*
> ones were run on this machine on 2026-09-20.

**The goal:** `gac_blit_alpha(sprite, 0, 0, GAC_SCREEN, x, y, w, h, GAC_SRC_ALPHA)`
draws an RGBA sprite over the screen using each source pixel's own alpha, so
a soft edge is soft and a transparent margin is not drawn at all.

**What you will be able to see at the end of this phase:** sprites with soft
edges and real transparency — a cursor over the console, an icon in
`explorer`, the pigeon on the splash screen composited instead of masked.

---

## 1. §4.5 was right about the naive loop, and wrong about what follows from it

§4.5 measured a per-pixel-alpha blit at **0.28 s a screen** and concluded that
neither trick applies, so the command waits. The first half is confirmed. The
second half is wrong, and the reason is worth stating plainly:

> **§4.5 measured the wrong picture.** It measured a blit where *every* pixel
> has a partial alpha. Real sprites are not like that. An antialiased disc is
> **97.6% opaque-or-transparent** and 2.4% soft edge *(measured)*; a full
> screen with one sprite on it is **99.8%** *(measured)*. The expensive
> arithmetic is needed for a thin rim, and the rest is a slice copy or
> nothing at all.

So the operation is not "a blend of every pixel". It is **a classification
followed by three cheap things**, and the classification itself is a handful
of C-speed string searches.

### 1.1 The four candidates, measured

Four implementations, each verified row by row against a reference that
applies `blend_rows`' exact formula per pixel — **all four agree with it byte
for byte** *(measured)*. Warmed (5 runs) and best of 7, so PyPy's JIT is not
being blamed for its warm-up:

**CPython 3.13.12** *(measured)*

| picture | % partial | `BLIT` opaque | `BLIT_ALPHA` one alpha | naive | **split** |
|---|---|---|---|---|---|
| sprite 128 × 128, antialiased | 2.4% | 0.03 ms | 1.03 ms | 5.65 ms | **0.39 ms** |
| sprite 256 × 256, antialiased | 1.1% | 0.10 ms | 2.73 ms | 24.24 ms | **0.84 ms** |
| panel 256 × 128, alpha every pixel | 99.2% | 0.02 ms | 1.32 ms | 14.74 ms | 15.30 ms |
| screen 1280 × 720, antialiased | 0.2% | 0.33 ms | 30.29 ms | 213.35 ms | **4.90 ms** |
| screen 1280 × 720, alpha every pixel | 99.5% | 0.38 ms | 33.04 ms | 463.46 ms | 469.31 ms |

**PyPy 3.11.15** *(measured)* — the interpreter `run-pypy.sh` exists for

| picture | % partial | `BLIT` opaque | `BLIT_ALPHA` one alpha | naive | **split** |
|---|---|---|---|---|---|
| sprite 128 × 128, antialiased | 2.4% | 0.07 ms | 0.82 ms | 0.31 ms | **0.16 ms** |
| sprite 256 × 256, antialiased | 1.1% | 0.01 ms | 3.18 ms | 1.86 ms | **0.44 ms** |
| panel 256 × 128, alpha every pixel | 99.2% | 0.01 ms | 1.65 ms | 0.74 ms | **0.67 ms** |
| screen 1280 × 720, antialiased | 0.2% | 0.02 ms | 47.41 ms | 14.49 ms | **6.60 ms** |
| screen 1280 × 720, alpha every pixel | 99.5% | 0.02 ms | 48.76 ms | 31.44 ms | **19.47 ms** |

**Three things follow, and the third is the surprise:**

1. **`split` never loses.** On PyPy it wins every case. On CPython it wins by
   14× to 44× on every realistic sprite, and ties the naive loop (within 1.3%)
   on the pathological one. There is no case where choosing it costs anything
   measurable, so **there is no per-interpreter choice to make** — which is
   what the first, un-warmed run of this benchmark wrongly suggested.
2. **PyPy's JIT does most of the work by itself,** and that is worth knowing:
   the naive loop is 0.31 ms there against CPython's 5.65 ms. Had the
   emulator only ever run on PyPy, this phase would be one simple loop. It
   does not, so it is not.
3. **The "hard" operation is cheaper than the one already shipped.** A
   per-pixel-alpha blit of a real sprite costs **0.39 ms** where the
   constant-alpha `BLIT_ALPHA` of the same rectangle costs **1.03 ms**, and at
   full screen 4.90 ms against 30.29 ms *(measured)*. Per-pixel alpha lets
   the device *skip* work that a uniform alpha forces it to do. §4.5 ranked
   this operation as the expensive one; measured on real data, it is the
   cheap one.

### 1.2 Why there is no SWAR trick, now measured rather than reasoned

§4.5 said "no table and no single multiply fits it". That is right, and the
plan should say *why* so nobody retries it. A big-integer multiply scales
every lane by **one** factor; per-pixel alpha needs a different factor per
lane, which a multiply cannot express. The one decomposition that can —
`a·x = Σ x << k` over the set bits of `a`, eight masked adds with the masks
built from the alpha bytes by `bytes.translate` — was written and measured:

| | 128 × 128 sprite | 1280 × 720 |
|---|---|---|
| `bitswar`, eight masked adds | **60.29 ms** | **2,235 ms** |
| `split` | 0.39 ms | 4.90 ms |

*(measured, CPython; PyPy 21.15 ms and 951 ms.)* It is **10× slower than the
naive loop it was meant to beat.** Eight big-integer AND-shift-adds per lane
pass cost far more than one multiply, and the mask building costs more again.
Recorded here so the idea is closed rather than rediscovered.

A fifth candidate, `runs` — group each row into maximal runs of one alpha and
hand each run to the existing `blend_rows` — was also measured: 1.61 ms on the
128 × 128 sprite and 52.39 ms at full screen *(measured, CPython)*. Better
than naive, four times worse than `split`, and much more code. Not proposed.

---

## 2. The design: classify, then do the cheap thing

One row at a time, as `_blit_alpha` already works *(checked:
`gac.py:481`)*. The alpha bytes of a row come out at C speed as
`src[3::4]`, and everything below is a string method on that:

```
    row:   . . . . ░ ▒ █ █ █ █ █ █ ▒ ░ . . . .
           └───────┘ └─┘ └─────────┘ └─┘ └────┘
            skipped  soft   copied   soft skipped
```

1. **The ink span.** `lstrip(b"\x00")` and `rstrip(b"\x00")` give the first
   and last pixel with any alpha. Everything outside is alpha 0 and is left
   exactly as it is — **not read, not written, not blended.**
2. **The solid core.** `find(b"\xff")` and `rfind(b"\xff")` bracket the opaque
   pixels; if `count(b"\xff", …)` over that bracket equals its length, the
   core is one unbroken run of 255 and becomes **one slice assignment**, the
   same copy `BLIT` does.
3. **The soft edge.** Only what is left — the rim between the span and the
   core — goes through the per-pixel loop, with `blend_rows`' exact formula
   so the arithmetic matches the rest of the device byte for byte.
4. **No core, or a broken one:** the whole ink span goes through the loop.
   That is the 99%-partial case, and it is the naive loop, no worse.

The destination keeps its own alpha byte, exactly as `blend_rows` does
*(checked: its `_ALPHA` mask)* — the scanout ignores alpha, and this keeps
every operation on the device consistent.

**Overlap.** `_blit_alpha` already reverses the row order when source and
destination are the same buffer and the destination is below *(checked)*. The
same guard applies unchanged.

**Clipping.** `_clip_copy` already does it *(checked)*, and runs before any of
the above, so the classification only ever sees pixels that will be drawn.

---

## 3. The wire: how the guest asks for it

`_blit_alpha` refuses `alpha > 255` today *(checked: `gac.py:484`)* — the
reserved hole README §9 mentions. Two ways to use it, and §10 Q1 asks which:

**(a) A sentinel alpha on command 14, `GAC_SRC_ALPHA = 256`.** No new command,
no new argument shape, no change to `_STRUCTS` or to `BATCH`'s size table.
**An emulator built before this phase refuses it cleanly** — `return False`,
which surfaces as `gac_blit_alpha` returning 0 — rather than logging an
unknown command. This is the recommendation.

**(b) A new command 17, `BLIT_SRC_ALPHA`,** with the same arguments minus the
alpha word. Tidier to read in the command table; costs a new struct entry, a
new branch, and an old emulator answers with `log.warning("GAC: unknown
command")` and an empty reply *(checked: `gac.py`'s `callback`)*.

Either way **`INFO` grows a feature bit,** `FEATURE_SRC_ALPHA = 4`, beside
`FEATURE_TEXT = 1` and `FEATURE_BLEND = 2` *(checked)*, so the guest can ask
rather than try. `docs/gac.md`'s command table and the `DAMAGE`-style
"reserved" note get updated with it.

---

## 4. The other half: where do pixels with alpha come from?

**This is the part that is easy to miss, and it is half the work.** The
command is useless without data to feed it, and today there is none:

- **`bmp.c` reads 32-bit BMPs and throws the alpha away** — `bmp.h` says so
  in as many words, *"a 32-bit file's alpha is dropped"*, and every pixel it
  returns is `0xFFRRGGBB` *(checked)*.
- **`splash.c` is the workaround, in the flesh.** The pigeon's eyes flash by
  loading a *second* BMP, `/etc/bmp/eye-mask.bmp`, white where the eyes are,
  scanning it for white pixels into `unsigned eyes[1024]`, and storing to the
  screen a pixel at a time *(checked: `splash.c:93-104,150`)*. `MAX_EYES` is
  1,024 because that loop is the guest's own. **That whole mechanism is this
  command, done by hand and capped.**

So the phase needs a `BMP_ALPHA` flag for `bmp_load`: with it, a 32-bit file's
alpha byte is kept instead of forced to `0xFF`. That is a small, contained
change to a library that already parses the format *(checked: `bmp.c:74-76`
handles `bits == 32` and the bit-field variant)*. §10 Q2 asks whether a
24-bit file should get a colour-key alpha too, or whether 32-bit files are
enough.

---

## 5. The guest library

- **`lib/pigeon/gac.h`:** `#define GAC_SRC_ALPHA 256u`, and a line in the
  comment above `gac_blit_alpha`.
- **`lib/pigeon/gac.c`:** nothing, if (a) is chosen — the alpha word already
  goes through untouched.
- **`lib/pigeon/bmp.h` / `bmp.c`:** `BMP_ALPHA`, and the sentence about
  dropping alpha replaced.
- **`lib/pigeon/display.h`:** §10 Q3 — whether this gets a `disp_` wrapper
  (`disp_sprite`?) or stays a GAC call the way `gac_blit_scaled` does
  *(checked: `display.h` wraps some GAC operations and not others)*.

---

## 6. Steps

Each leaves the suite passing.

1. **`blend_span`,** the per-pixel loop over a span, with `blend_rows`'
   formula. Tested against a reference before anything calls it.
2. **`_blit_src_alpha`** in `gac.py`: the classification of §2, the three
   cases, the overlap guard, the existing clip.
3. **The wire:** the sentinel (or command 17), `FEATURE_SRC_ALPHA`, and
   `INFO`.
4. **`gac.h`**, and `docs/gac.md`'s tables.
5. **`BMP_ALPHA`** in `bmp.c` / `bmp.h`.
6. **`tools/bench.py`:** a per-pixel-alpha line beside the blended-fill one,
   because §1's numbers are the ones most likely to rot — the same reason
   design.md §6 gives for the lines already there *(checked)*.
7. **Something that uses it.** §10 Q4: `splash.c` losing its eye-mask loop is
   the honest demonstration, since it is the workaround this replaces.

---

## 7. Tests

`tests/test_gac.py`, beside the `BLIT_ALPHA` cases already there:

- **Against a reference,** per-pixel, on every picture §1 measured: the
  antialiased disc, the all-partial panel, pure noise, fully opaque, fully
  transparent. This is the test that matters — the classification has four
  branches and they must all produce identical bytes.
- **The branches individually:** a row with no ink at all (untouched, and the
  destination not even read); a row all opaque (a pure copy); a row whose
  opaque pixels are in *two* runs with a transparent gap between them, so the
  "broken core" fallback is exercised; a one-pixel sprite; a one-pixel-wide
  soft edge with no core.
- **The destination's alpha byte is kept,** as `blend_rows` keeps it.
- **Clipping:** off each of the four edges, and wholly outside.
- **Overlap:** source and destination the same surface, both directions.
- **Refusals:** the sentinel on an emulator without the feature bit; a
  surface handle that is not there.
- **`BATCH`:** the command inside a batch, counted in `ran`/`refused`.
- **`BMP_ALPHA`:** a 32-bit BMP's alpha kept, the same file without the flag
  still opaque, a 24-bit file unaffected.

**A deliberate-breakage pass,** as Phase 1 and the relocatable work did
*(checked: `kernel.md` §17.1 lists seventeen)*: the core copied one pixel too
wide, the span off by one, the rounding term dropped, the destination alpha
overwritten, the overlap guard removed — each must fail a test.

---

## 8. Risks

1. **The all-partial case is slow and now reachable.** A full-screen gradient
   is **469 ms on CPython** *(measured)* — a visible freeze, and 14 frames
   missed. Today nothing can ask for it because the command is refused.
   §10 Q5 asks what to do: accept and document, refuse above some pixel
   count, or leave it and let `MAX_EXTENT` be the only limit.
2. **`split`'s scan is pure overhead when it finds nothing to skip** — 1.3%
   on the pathological picture *(measured)*. Small, and paid only in the case
   that is already slow.
3. **A wrong classification is a silent wrong picture, not a crash.** Hence
   §7's reference test on five pictures rather than spot checks.
4. **`bytes.lstrip`/`rstrip`/`find`/`count` on a `memoryview`.** The targets
   hold `bytearray` buffers and `src.at()` returns offsets *(checked)*; the
   row must be materialised as `bytes` for these methods, which is one copy
   per row. Already true of `_blit_alpha` today *(checked: it slices
   `sbuf[ps:ps + n]`)*, so no new cost — but worth checking it stays one copy
   and not three.

---

## 9. Not in this phase

- **Per-pixel alpha in `BLIT_SCALED`.** Scaling picks source pixels by
  nearest neighbour; combining that with the classification means
  classifying *after* the pick, which is a different loop. Separate, and only
  if something wants it.
- **A blend mode other than "over".** Add, multiply, screen. Nothing asks.
- **Premultiplied alpha.** It would make the arithmetic one multiply shorter
  and every other operation on the device inconsistent with it.
- **Alpha in `TEXT`'s foreground.** `TEXT` already takes a blended `fg`
  *(checked)*; per-glyph coverage (antialiased text) is a font problem, not
  a blending one.

---

## 10. Questions

**Answered 2026-09-20: "all recommendations." Every answer below is the
recommendation, and the build followed them.**

1. **The wire: a sentinel alpha of 256 on command 14, or a new command 17?**
   §3 has both. **Recommendation: the sentinel** — no new argument shape, and
   an emulator built before this phase refuses it cleanly instead of logging
   an unknown command.

   **Decided (you):** the recommendation — the sentinel, `GAC_SRC_ALPHA = 256`
   on command 14, plus `FEATURE_SRC_ALPHA = 4` in `INFO`.

2. **`BMP_ALPHA` for 32-bit files only, or a colour key for 24-bit ones too?**
   A colour key ("this exact colour means transparent") would let the
   existing 24-bit art in `/etc/bmp` gain transparency without being
   re-saved, at the cost of a flag, a colour argument, and a hard edge with
   no soft rim. **Recommendation: 32-bit only.** The whole point of the phase
   is the soft rim, and a colour key cannot give one.

   **Decided (you):** the recommendation — 32-bit files only. No colour key.

3. **Does `<pigeon/display.h>` get a wrapper,** say `disp_sprite(...)`, or
   does this stay a `gac_` call like `gac_blit_scaled`?
   **Recommendation: no wrapper for now** — `display.h` wraps what it needs
   for its own drawing, and a sprite is not part of that.

   **Decided (you):** the recommendation — no `display.h` wrapper; it stays
   `gac_blit_alpha`, as `gac_blit_scaled` does.

4. **What uses it first?** **Recommendation: `splash.c`** — deleting
   `find_eyes`, `eyes[1024]`, `MAX_EYES` and the per-pixel store loop in
   favour of one blit is the clearest possible demonstration, and it removes
   a capped workaround. A cursor for the console, or icons in `explorer`,
   are the other candidates.

   **Decided (you):** the recommendation — `splash.c`, losing `find_eyes`,
   `eyes[1024]` and `MAX_EYES`.

5. **The pathological case: accept, or refuse?** A full-screen picture with a
   partial alpha on nearly every pixel costs 469 ms on CPython. Options:
   **(a)** accept it and say so in `gac.md`, as `BLIT_SCALED`'s cost is
   accepted; **(b)** refuse above some area when the soft pixels exceed some
   share of it, which means the command sometimes fails for reasons the
   guest cannot predict; **(c)** leave `MAX_EXTENT` as the only limit and let
   it be slow. **Recommendation: (a).** A guest that asks for it wants it,
   the failure mode of (b) is worse than slowness, and PyPy — the way this
   emulator is run when speed matters — does it in 19 ms.

   **Decided (you):** the recommendation — (a), accept it and say so in
   `gac.md`.

6. **Is this the phase you want next at all?** The alternative on
   [README.md §9](../README.md#9-not-in-this-plan)'s list is letting the
   guest use more than 128 MB of RAM. This one is worth it if sprites with
   soft edges are something you want to draw; §1 says it is cheaper than
   expected, which was the reason it was deferred.

   **Decided (you):** yes. Built 2026-09-20 ([§11](#11-as-built)).

---

## 11. As built

Built 2026-09-20, to the plan, every recommendation taken. All seven steps
are done — step 7 after a second question, because Q4's answer turned out to
be the wrong program (see below).

- **`emulator/devices/gac.py`:** `src_alpha_row` cuts a row into the three
  kinds of pixel with `lstrip`/`rstrip`/`find`/`rfind`/`count` over
  `src[3::4]`, all at C speed; `blend_span` is the per-pixel loop it hands
  the rim. `_blit_src_alpha` clips with the existing `_clip_copy`, keeps
  `_blit_alpha`'s overlap guard, and copies each source row before writing
  it so an overlapping blit cannot read what it has already drawn.
  `SRC_ALPHA = 256` is the alpha word that selects it, and
  `FEATURE_SRC_ALPHA = 4` joins `INFO`'s features.
- **`lib/pigeon/gac.h` / `gac.c`:** `GAC_SRC_ALPHA`, the `GAC_FEATURE_*`
  bits, and `gac_features()` — the probe already read the features word out
  of `INFO` and threw it away, so this costs no extra bus command.
- **`lib/pigeon/bmp.h` / `bmp.c`:** `BMP_ALPHA`, added to any placement mode,
  keeps a 32-bit file's alpha byte instead of forcing it opaque. A 24-bit
  file has none to keep and stays opaque, which is not an error. Where the
  alpha is kept, what falls outside the image is clear rather than black.
- **`docs/gac.md`:** §3 and both tables.
- **`tools/bench.py`:** two `SRC_ALPHA` lines, the sprite and the
  every-pixel case, measured through the device as §6 asked.
- **`user/os/bin/graphics.c`:** `-sprite FILE X Y W H MODE`, the same form
  as `-f` but loading with `BMP_ALPHA` and drawing through
  `gac_blit_alpha(..., GAC_SRC_ALPHA)` — **the first thing on the machine
  that draws with it.** Where there is no accelerator it blends the pixels
  itself, with the same formula and the same rounding.
- **`lib/pigeon/display.c` / `display.h`:** `disp_gac_surface(&handle)` —
  which surface drawing is going to now, so a program can send the GAC
  something this library has no call for and still land on the back buffer
  when there is one. Not the wrapper Q3 turned down: it draws nothing.
- **`tools/make_badge.py` and `/etc/bmp/badge.bmp`:** a 64 × 64 disc with a
  one-pixel soft rim, 4.5% of it partial alpha. Nothing already on the disc
  had an alpha channel — the two existing pictures are 24-bit — so there was
  no picture to show the feature with. A generator rather than a hand-drawn
  file, so the shape stays readable.
- **`docs/graphics.md`:** `-sprite`, in both tables and a section of its own.

### What it costs *(measured through the real device, `tools/bench.py`)*

| | measured | the plan predicted |
|---|---|---|
| a screen-sized sprite with a soft rim | **4.43 ms** | 4.9 ms |
| a 720p picture with alpha on nearly every pixel | 523.7 ms | 469 ms |
| `BLIT_ALPHA`, a whole screen at one alpha | 25.7 ms | 30.3 ms |

The two that moved, moved together and in the same direction as the
constant-alpha line, so it is the machine on the day, not the code.

### Tests

`tests/test_gac.py`, 27 new: the reference test on six pictures (disc,
gradient, noise, opaque, clear, nearly opaque); a clear sprite and its
margin left alone; an opaque one copied with the destination's alpha kept;
the broken core; six awkward one-row sprites; clipping off all four edges
and wholly outside, with a guard after the framebuffer; overlap both ways;
refusals; inside a `BATCH`; and `src_alpha_row` itself against the formula
over 600 random rows built to hit each branch.

`tests/test_bmp.py`, 8 new, with a different alpha on every pixel so a byte
taken from the wrong place cannot come out right by luck: the alpha kept
under all three placements, dropped without the flag, absent on a 24-bit
file, the margin clear rather than black, bit-field files, and `BMP_ALPHA`
not making a bad mode good.

**The deliberate-breakage pass, all caught:** the core copied one pixel too
wide; the span starting one late; the rounding term dropped; the
destination's alpha overwritten; the broken-core check removed; the overlap
guard removed; the margin drawn anyway; clear pixels blended instead of
skipped. And on `bmp.c`: the alpha dropped, the margin black, a 24-bit
file's padding taken as alpha, `BMP_ALPHA` not stripped before the mode
check.

**Two of those breakages are invisible in the pixels,** and that is worth
recording: blending a pixel whose alpha is 0 comes to the destination
exactly unchanged, so drawing the transparent margin is a pure waste of time
and not a wrong picture. They are caught by `Recorder`, a destination that
remembers which bytes were written — the only way to test the claim the
whole phase rests on.

`tests/test_graphics.py`, 8 new: a sprite blended on each pixel's own
alpha against the formula; its clear pixels left alone where `-f` would
paint them; the same file under `-f` opaque everywhere; a 24-bit file drawn
like `-f`; clipping off the top left; the three error messages; the badge's
soft rim; and the same command on a machine with `vram_size=0`, which has no
GAC, giving a framebuffer identical to the accelerated one.

`tests/test_install.py` and `test_project.py` carry `badge.bmp`: the disc is
35 files, not 34.

**The full suite: 1,935 passed** (1,890 before).

### Step 7, and the question that had to be asked twice

Q4 chose `splash.c`, on the recommendation in §10 — and building it showed
that recommendation was **wrong**, for a reason worth writing down:

> **`splash.c` does not need per-pixel alpha.** Its eye-mask is *binary* —
> white where the eyes are, black elsewhere *(checked: `find_eyes` tests
> `>= 128`)* — and the flashing is one global strength `t` that varies with
> time. So what it wants is a **binary mask plus one alpha for the whole
> rectangle**, which is `BLIT_ALPHA` as it already shipped in Phase 3, not
> this command. Per-pixel alpha would let the eyes have a *soft* rim, but
> the asset has no soft rim to carry, and giving it one means a new 32-bit
> `.bmp` in the example disc.

There is a neat way to do the flash with two bus commands and no guest loop
at all — composite the yellow eyes onto a copy of the pigeon's eye region
once, then `BLIT_ALPHA` that copy over the screen at `t` each frame, since
outside the eyes the copy equals the pigeon and the blend does nothing. It
is a real improvement and it removes `eyes[1024]`, `MAX_EYES`, `find_eyes`
and `blend`. **But it demonstrates Phase 3, not Phase 9,** so it belongs to
its own small change rather than to this one.

The first user of `SRC_ALPHA` therefore wants a picture that genuinely has a
soft edge, and that means an asset to draw. **Asked again, and answered:
`graphics -sprite`,** with `badge.bmp` for it to draw. `graphics` is already
what `# graphics` scripts drive ([graphics.md](../../graphics.md)), so a
sprite is usable from a script the moment it exists.

**Both paths were then checked against each other**, by building
`graphics.c` twice — once with the fallback removed, once with the
accelerator branch disabled — and running the same tests. Both pass, so the
device and the pixel loop draw the same picture. That is now pinned for good
by a test that runs one command on the normal machine and again on one built
with `vram_size=0`, which has no GAC, and compares the framebuffers.

### The splash cleanup, still worth doing

Separately from this phase: the two-command eye flash above deletes
`find_eyes`, `eyes[1024]`, `MAX_EYES` and `blend` from `splash.c` and lifts
the 1,024-pixel cap. It needs nothing from Phase 9.
