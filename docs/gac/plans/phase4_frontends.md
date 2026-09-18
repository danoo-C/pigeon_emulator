# Phase 4: the front ends follow the mode

> Part of [the GAC plan](../README.md). **Status: built, 2026-09-18
> ([§8](#8-as-built)); every question decided ([§7](#7-decisions)).** Needs Phase 2
> (built). Design: [design.md §5.5](../design.md#55-the-host-side-front-ends-and-the-wire);
> the numbers are §4.4. Decisions: Q1, Q7, Q15 in
> [decisions.md](../decisions.md). Facts marked *(checked)* were read in the
> code, and *(measured)* ones were run on this machine on 2026-09-18.

**The goal:** when the machine's mode changes, the browser canvas and the
pygame window change with it, with no restart and no reload. The host can
also *ask* for a mode, but only the guest switches (Q1).

**What you will be able to see at the end of this phase:** run the emulator
with `--mode 640x360` and both front ends open at 640 × 360. A test that
switches the mode through the bus sees the next frame arrive at the new size
and be drawn at it. Nothing in the guest switches modes yet: that is
`setmode`, Phase 7. So in daily use this phase is mostly invisible until
then, and that is expected.

---

## 1. Where things stand

- **Both front ends ask the machine for the geometry once, at startup**
  *(checked)*. `index.html` sets `W`, `H` from `/info` in `init()` and has no
  default geometry on purpose. It drops any frame whose length is not
  `W·H·4`, with a console warning. `display.py` sets `disp_w`, `disp_h` and
  `frame_size` in `__init__` from `_wait_for_server()`, and `_render`
  silently skips a frame of the wrong size.
- **So today, a mode change freezes both front ends on the last frame of
  the old size.** Phase 2 already makes `/info` answer the live size, so a
  reload recovers the browser, but not the pygame client.
- **Both windows are sized from the mode, not the other way round**
  *(checked)*. The browser's canvas is `W × scale` with a whole-number scale
  box (default 4). pygame's window is `disp_w × pixel_size` plus the toolbar
  and the Serial panel, set with `pygame.display.set_mode`, **not
  resizable**. Neither can be dragged to a new size. That matters for how
  "the window asks for a mode" can work (Q1 below).
- **At the big modes, today's scale is far too big.** 1280 × 720 at scale 4
  is a 5,120-pixel-wide canvas.
- **The server swizzles every frame.** `DisplayIO.update()` snapshots the
  screen and converts B,G,R,A to R,G,B,A 30 times a second *(checked)*. The
  design moves that into the clients (Q7): **1.33 ms** in JavaScript at
  1280 × 720 *(measured, node 24)*, and none at all in pygame, whose
  `frombuffer` takes `"BGRA"` directly *(checked: pygame 2.6.1 in `.venv`)*.
  The server's work drops to the snapshot: **0.69 ms** at 720p
  *(measured)*, against 6.4 ms with the swizzle (§4.4).
- **Alpha reaches the screen today.** Both clients draw each frame with its
  alpha: the browser through `putImageData`, pygame through an `"RGBA"`
  surface blitted over the window's background *(checked)*. `display.h` says
  so: *"anything with AA = 0x00 is invisible"*. No colour in the guest uses
  alpha *(checked)*, but **unwritten memory does**: a fresh mode's screen is
  zeros, and shows as the page's background, not black. Since Phase 3b, a
  translucent GAC colour keeps the destination's alpha, so this now matters
  (Q3).
- **Mouse coordinates are already scaled by the clients** from `W`/`H`
  (`toDisplayXY`) and `disp_w`/`disp_h` (`SP.on_screen`) *(checked)*. HID
  stores whatever it is sent, masked to 16 bits. So the mouse follows a mode
  change for free once `W`/`H` do. At worst one frame of clicks lands a
  pixel or two out (Q15).
- **The repo has a pattern for testing front-end code, and this phase uses
  it** *(checked: `test_serial_panel.py`, `test_serial.py`)*. The page's
  logic lives in a marked section of `index.html` and is run under node. The
  pygame client's logic lives in a module that imports no pygame
  (`serial_panel.py`). The server's replies are plain functions
  (`serial_reply`) that tests call without starting a server.

---

## 2. The wire

### 2.1 `/frame`: raw bytes, with the mode in a header

- The body is the screen **as it is in memory, B,G,R,A** (Q7), exactly
  `w·h·4` bytes.
- **`X-Pigeon-Mode: w,h,generation`** says which mode those bytes are in.
- **The bytes and the header are captured together.** `DisplayIO.update()`
  runs on the emulator thread and stores `(bytes, w, h, generation)` under
  the frame lock it already has. A frame can therefore never be labelled
  with a mode it was not drawn in, even if the guest switches while
  `/frame` is being served.
- `generation` is the VRAM device's (Phase 2), or 0 on a machine without
  video memory.
- `_convert_to_rgba` leaves the server. `test_smoke.py`'s swizzle test moves
  to the two clients' tests, where the swizzle now is (Q5).

### 2.2 `/info` gains four fields

`generation`, `format: "bgra"`, `modes: [[192,108], …]` and
`preferred: [w, h]` (`[0, 0]` for none), beside `w`, `h`, `size`,
`scanout`, `hid_url`, `cd_url`. `format` is there so a client that expects
something else can say so instead of showing swapped colours.

### 2.3 `POST /preferred {w, h}`

Stores what the host would like, through `VRAM.set_preferred` (Phase 2).
**Only an offered mode is accepted**; anything else answers 400 with the
list. A machine without video memory answers 404. Nothing switches: the
guest reads it with `PREFERRED` and decides (Q1). The kernel does that at
the prompt from Phase 6 on.

All three are written as plain functions (`frame_reply`, `info_reply`,
`preferred_reply`) that the FastAPI routes call. The tests call them
directly, as `test_serial.py` does with `serial_reply`.

---

## 3. The browser, `display/index.html`

- **A marked logic section, `// ---- the screen's logic`,** with pure
  functions that the node tests run:
  - `toRGBA(arrayBuffer)`: the swizzle over a `Uint32Array`, in place.
    It also **forces alpha to 0xFF** if Q3 says so, at no extra cost.
  - `parseMode(header)`: `"640,360,3"` → `{w, h, generation}`, or `null`
    for a missing or malformed header.
  - `fitScale(w, h, roomW, roomH, wanted)`: the largest whole scale, at
    least 1, at which `w × h` fits the room, capped at `wanted` (Q2).
- **The wiring:** `loop()` reads the header on every frame. When the mode
  differs from `W`/`H`, it resizes the off-screen canvas, recomputes the
  scale, resizes the canvas, and re-fits the Serial panel (`serialFit()`
  already exists). Then it draws the frame. A frame whose length does not
  match its *own* header is still dropped with a warning, as today.
- The same `fitScale` runs when the browser window is resized, so a big mode
  in a small window stays on screen.

## 4. pygame, `display/display.py`

- **A small module beside it, `display/screen.py`, importing no pygame,**
  like `serial_panel.py`: `parse_mode(header)` and
  `fit_pixel_size(w, h, room_w, room_h, wanted)`.
- The fetch thread keeps `(bytes, mode)` together. The render loop, which is
  the main thread and the only one allowed to call `set_mode`, notices the
  change. It then sets `disp_w`/`disp_h`, picks a pixel size that fits the
  desktop (`pygame.display.Info()`), and calls the existing
  `_resize_window()`.
- `frombuffer(frame, (w, h), "BGRA")`, then `.convert()` if Q3 makes the
  screen opaque (that drops the alpha on the way to the window).
- The `+`/`-` buttons keep working. `fit_pixel_size`'s `wanted` is the last
  size you chose, so a mode change back to 192 × 108 returns to your size,
  not to 1.

---

## 5. Steps

1. **The server:** `DisplayIO.update()` stores raw bytes with their mode;
   `frame_reply`, `info_reply`, `preferred_reply`, and the routes;
   `_convert_to_rgba` removed. Tests on the functions: bytes and header
   agree across a `SET_MODE`; the new `/info` fields; `/preferred`'s
   refusals.
2. **The browser's logic section** and its node tests: the swizzle
   (the old `test_smoke.py` reference, moved here), `parseMode` and
   `fitScale`.
3. **The browser's wiring:** resize on a header change and on a window
   resize. Tested as the Serial panel's wiring is: the source is checked for
   the calls, since node has no DOM.
4. **`display/screen.py`** and its tests, then **`display.py`'s wiring**.
5. **End to end, no browser needed:** a `RAM` with video memory, its
   `DisplayIO` and `VRAM`, a `SET_MODE` through the real `IOController`,
   `update()`, then `frame_reply`. Hand that reply to the page's logic under
   node, and check `W`/`H` come out 640 × 360 and the pixels come out right.
   This is the plan's "done when": *a mode change from a Python test changes
   what the browser draws*.
6. **`tools/bench.py`:** the "Frame conversion" line becomes "Frame
   snapshot" at 192 × 108 and at 1280 × 720.
7. **Docs:** `display.h`'s alpha sentence, if Q3 changes it.
   `docs/graphics.md`'s alpha wording waits for Phase 7, when `0x80…` colours
   reach it through the GAC. **Full suite.**

**Done when:** step 5 passes; both front ends open at `--mode 640x360` and at
192 × 108 (checked by you, since I cannot open a window here); and the full
suite passes.

---

## 6. Risks

- **Nothing here can be seen from a test with a real browser or window.**
  node runs the page's logic but has no DOM, and pygame needs a display. The
  logic is tested; the wiring is small and checked by reading it; the last
  check is you opening both at two modes. The same trade was made for the
  Serial panel *(checked)*.
- **An old pygame client against a new emulator** shows swapped red and
  blue, since it still reads `"RGBA"`. Both front ends are in this repo and
  change in the same commit, so this only bites a client started from an
  old checkout.
- **Bandwidth.** 1280 × 720 raw is 3.6 MB a frame. The browser polls at
  about 15 FPS (`setTimeout(loop, 66)` *(checked)*), which is 55 MB/s over
  localhost. That is fine, and it is Phase 8's job to make it smaller.

---

## 7. Decisions

Answered 2026-09-18: *"all recommendation"*, in this section's title and in
the chat. Every recommendation stands.

1. ~~**How does a window ask for a mode?**~~ The design said "a resized window
   posts a preferred size", but neither window can be resized today:
   both are sized *from* the mode (§1).
   - **(a) Recommendation: a mode picker.** A small list of the offered modes
     in both front ends' toolbars. Choosing one posts `/preferred`, and the
     OS switches at its next prompt. It is predictable, has no feedback loop
     (a mode change resizing the window, which asks for another mode…), and
     is easy to test.
   - **(b) Follow the window.** Make the windows resizable, and ask for the
     largest offered mode that fits at the current scale. This is closer to
     "dynamically resizable", but it needs debouncing, and a drag through
     five sizes asks for five modes.
   - (a) now; (b) can be added later as a "follow window" checkbox that
     uses the same `/preferred`.

   **Decided (you), 2026-09-18:** the recommendation.

2. ~~**When the mode changes, what happens to the scale?**~~
   **Recommendation:** the largest whole scale that fits the window (the
   desktop, for pygame), never more than the scale you last chose, and at
   least 1. So 192 × 108 stays at your 4, and 1280 × 720 comes up at 1 or 2
   instead of a canvas five thousand pixels wide. Whole numbers only, so
   pixels stay square and sharp.

   **Decided (you), 2026-09-18:** the recommendation.

3. ~~**Should the screen ignore alpha?**~~ Today a pixel with alpha 0 shows the
   page's background: *"AA = 0x00 is invisible"* (`display.h`).
   **Recommendation:** yes. Both front ends draw every pixel opaque, so
   memory nobody has drawn on is black, and the colour a program sees in
   memory is the colour on the screen. It costs nothing (one OR in the
   swizzle, `.convert()` in pygame). No guest colour uses alpha to be
   invisible *(checked)*, and the design already assumed "the scanout
   ignores alpha" (§5.3.1). `display.h`'s sentence changes to say so.

   **Decided (you), 2026-09-18:** the recommendation.

4. ~~**The mode picker: now, or with Phase 6?**~~ With (1a), choosing a mode
   does nothing visible until the kernel reads `PREFERRED` at its prompt,
   which is Phase 6. **Recommendation:** build `/preferred` now, but add
   the picker to the toolbars in Phase 6, together with the kernel side. That
   way no button ever ships that does nothing when you press it. Phase 6's
   plan then carries a step for it.

   **Decided (you), 2026-09-18:** the recommendation.

5. ~~**Raw BGRA only on `/frame`, or also a `?format=rgba` for other
   readers?**~~ **Recommendation:** raw only. Both clients are ours and
   change together. `format` in `/info` covers the rest, and a second path
   would keep the server swizzle alive just for a reader that does not
   exist. The swizzle's test moves to the two clients.

   **Decided (you), 2026-09-18:** the recommendation.

---

## 8. As built

Built 2026-09-18, as §2 to §5 describe, with every recommendation.

**The server** (`emulator/devices/display_io.py`): `update()` stores
`(bytes, w, h, generation)` under one lock, `_convert_to_rgba` is gone, and
`/frame`, `/info` and `/preferred` are thin routes over `frame_reply`,
`info_reply` and `preferred_reply`. The server's per-frame work is now the
snapshot alone: **0.63 ms at 1280 × 720**, where the swizzle was 6.4 ms
(`tools/bench.py`'s "Frame snapshot" lines replace "Frame conversion").

**The browser** (`display/index.html`): the section
`// ---- the screen's logic` holds `parseMode`, `toRGBA` and `fitScale`.
The wiring is `setMode` (on the frame whose header changed), `sizeCanvas`
(on a mode change, the scale box, a window resize, and the Serial panel
opening, closing or being dragged), and `canvasRoom`.

**pygame** (`display/display.py`): the helpers are in
**`display/screen_mode.py`**, not `screen.py` as planned, because a bare
module called `screen` on the path is an easy collision. `_resize_window`
is where the pixel size is fitted, because every change of layout already
goes through it. `_set_mode` changes the geometry and calls it.

**The tests:** `tests/test_frontends.py`, 22 cases: the three replies, the
page's logic under node, `screen_mode.py`, one table of cases that both
clients are held to, the wiring read from the source, and end to end: a
`SET_MODE` on the bus, `update()`, `frame_reply`, and the page's own logic
turning that into a 640 × 360 picture with the right colours.

Four existing tests changed, each for a reason that is this phase's:

- `test_smoke.py`'s swizzle test is gone. Its reference moved into
  `test_frontends.py`, where the swizzle now is.
- `test_serial_panel.py` builds a client without `__init__`. It gained the
  two new attributes, and its test frame is now in B,G,R,A, as `/frame` sends
  it (in R,G,B,A it drew blue).
- `test_cd.py` checked `/info` for `cd_url` by searching the source. It now
  calls `info_reply` and checks the reply. The bar-layout test sets the
  pixel size you asked for, on a desktop with room for every size.

**Run for real, not only tested:** the emulator's real HTTP servers, with
the real pygame client under SDL's dummy video driver. `/frame` came back as
82,944 bytes with `X-Pigeon-Mode: 192,108,0`. `/info` had the new fields.
`/preferred` accepted 640 × 360 (200) and refused 641 × 360 (400). The client
opened at 192 × 108, at pixel size 3 on the dummy's 1024 × 768 desktop with the
Serial panel open. After a `SET_MODE` it followed to 640 × 360 at pixel size 1,
and the guest's red top-left pixel was red in the window. **The browser page
was not run in a browser here.** Its logic is tested under node, and its
wiring was checked by reading it and by `node --check`. Opening it at two modes
is the last check, and it is yours.

**Also changed:** `display.h`'s alpha sentence, and `docs/graphics.md`'s,
which both said `0x00…` is invisible. That has not been true since this
phase. `graphics.md`'s blending wording still waits for Phase 7. Phase 6's
file carries the mode picker (decision 4).

**The suite:** 1,836 passed, none failed.

