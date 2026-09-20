# The canvas: a widget you draw into yourself

> Part of [the GUI plan](README.md). **Status: design, and §7's questions are
> decided (2026-09-20) — you left them blank, so every recommendation
> stands.** Facts marked *(checked)* were read in the code
> on 2026-09-20.

**What you asked for:** a canvas widget, so the graphing calculator has a
perfect place to render, and so does the cube.

**And the constraint that comes with it:** `user/graph.c` and `user/cube.c`
**are not to be modified**. They must keep working with no GUI library at
all. Nothing in this plan touches them ([§6](#6-the-originals-are-not-touched)).

---

## 1. Why a canvas is different from every other widget

Every other widget is drawn *by* the library: it owns the pixels, knows when
they are stale, and repaints them. A canvas is the opposite — **the app owns
the pixels** and the library owns only the rectangle they live in.

That inverts three things, and each needs a decision rather than a default:

| | other widgets | canvas |
|---|---|---|
| who draws | the library | **the app** |
| when | when damaged | when the app says, or every frame |
| what the pixels survive | nothing; redrawn from state | **kept between frames** |

---

## 2. It is a real surface, not a hole in the screen

The obvious implementation is "a rectangle of the screen the app may scribble
in". That is wrong here for one concrete reason: **`disp_*` clips to the
screen, not to a sub-rectangle** *(checked: `display.c`, every primitive
clips against `disp_w`/`disp_h`)*. A canvas that is a bare rectangle has no
way to stop `disp_line` from drawing across the rest of the window.

So a canvas **allocates its own surface**, and the machine already has
everything for that:

- **`vram_alloc(w, h, &offset)`** returns a handle *and* an offset; with
  `vram_aperture()` the pixels are at `aperture + offset` and are written
  with a plain store *(checked: `lib/pigeon/vram.h`)*.
- **`gac_ram_surface(address, w, h)`** does the same for a `malloc`ed buffer
  *(checked: `lib/pigeon/gac.h:43`)*.

Either way the app gets **both** a raw pixel pointer and a GAC handle, which
is exactly the pair the two programs in question need:

- `cube.c` writes pixels one at a time — `disp_set(PX[i], PY[i], WHITE)`
  *(checked: `cube.c:182`)* — so it wants a pointer.
- `graph.c` draws lines, rectangles and text — so it wants a handle the
  accelerator can take.

```c
int      gui_canvas(int x, int y, int w, int h, gui_handler on);
unsigned gui_canvas_handle(int id);        /* the GAC surface handle      */
unsigned *gui_canvas_pixels(int id);       /* the first pixel, or NULL    */
int      gui_canvas_w(int id);             /* in DEVICE pixels (§4)       */
int      gui_canvas_h(int id);
void     gui_canvas_clear(int id, unsigned colour);
void     gui_canvas_animate(int id, int on);   /* redraw every frame      */
```

---

## 3. Drawing into it with the code you already have

The nicest property falls out of something `display.c` already does
internally. It keeps `disp_target` and `disp_target_h` — where drawing goes —
and swaps them for the back buffer *(checked: `display.c:59, 186-187,
203-208`)*. Expose that and **existing drawing code works inside a canvas
unchanged**:

```c
int  disp_push_target(unsigned handle, unsigned address, int w, int h);
void disp_pop_target(void);
```

so a canvas handler is:

```c
void on_plot(int id, gui_event *e) {
    if (e->type != GUI_DRAW) return;
    disp_push_target(gui_canvas_handle(id), (unsigned)gui_canvas_pixels(id),
                     gui_canvas_w(id), gui_canvas_h(id));
    /* ... the same disp_clear / disp_line / disp_text calls as ever,
       with disp_w and disp_h now meaning the canvas ... */
    disp_pop_target();
}
```

`disp_push_target` must swap `disp_w` and `disp_h` too, or clipping would
still be against the screen — that is the whole point of it.

**And a push must not straddle a `disp_present()`.** Present reassigns
`disp_target` itself on the flip *(checked: `display.c:209-219`)*, so a
present inside a push would be undone by the pop. The library draws canvases
inside the frame and presents after, so this never arises in `gui_poll()`;
it is a rule for a program using `disp_push_target` by hand
([build.md §6.1](../fonts/build.md)).

**This is what makes the canvas worth having:** the body of `graph.c`'s
`render()` or `cube.c`'s draw loop can be *copied* into a canvas handler and
compile as-is, because `disp_*` and `DISP_W`/`DISP_H` keep meaning what they
meant. Copied — not moved ([§6](#6-the-originals-are-not-touched)).

---

## 4. The canvas is not scaled, and that is deliberate

[scaling.md](scaling.md) scales every widget's *position and size*. For a
canvas, scaling the **contents** would mean resampling a picture the app
drew — blurring a plot, or a 3D render, to fit. Nobody wants that.

So: **the canvas surface is allocated at its device size.** Scaling changes
how big the canvas *is* — more room, more pixels — never how dense its pixels
are. A plot drawn at 1.5 × scale is a bigger plot with more detail, not the
same plot enlarged.

The consequence the app must handle: **the canvas changes size when the
screen mode does.** On `GUI_RESIZE` the library frees the old surface,
allocates one at the new device size, and raises `GUI_DRAW`. Anything the app
cached against the old width — `graph.c` caches `ys[]` indexed by plot width
*(checked: `graph.c:817-818`)* — has to be rebuilt, which is exactly what
`GUI_RESIZE` is for.

---

## 5. Events, drawing, and the frame

- **`GUI_DRAW`** is a new event type, raised only for canvases: "your pixels
  are needed now". It is raised when the canvas is damaged, when it is first
  shown, after a resize, and — if `gui_canvas_animate(id, 1)` — every frame.
- **Mouse and key events arrive in canvas-local coordinates**, `e->x` and
  `e->y` relative to the canvas's top-left, so a plot can hit-test its own
  contents without knowing where it sits. `graph.c` does pointer tracing and
  drag-to-pan *(checked: `graph.c:1405-1429`)*; both become ordinary
  `GUI_DRAG` and `GUI_MOTION` handlers.
- **The library does not clear the canvas.** `cube.c` and `graph.c` both
  clear as their first act *(checked: `graph.c:1252`)*, and clearing twice is
  a wasted screenful. `gui_canvas_clear()` is there for apps that want it.
- **The pixels are kept between frames.** A canvas that did not change costs
  one blit, or nothing at all if the library is repainting only damage
  ([design.md §5](design.md)).
- **Animation is opt-in.** `cube.c` spins continuously and wants a frame
  every frame; a plot wants one only when something moved. `animate` is the
  difference, and without it a canvas costs nothing when idle.

---

## 6. The originals are not touched

**`user/graph.c` and `user/cube.c` are not modified by any phase of this
plan.** They keep working on a bare machine with no GUI library, no fonts
beyond the built-in 5 × 7, and no accelerator.

There is already precedent for the way to do this: `user/os/bin/graph-corrupt.c`
is a *copy* of `graph.c` with changes, living beside it *(checked)*. So a
GUI-hosted plot would be a new file — `graph-gui.c`, say — that copies the
drawing and maths and replaces only the main loop and the chrome. The
originals stay on the disc, stay in the tests, and stay the thing that proves
the machine works without any of this.

[§7 Q5](#7-questions) asks whether a hosted copy is wanted at all, or whether
the canvas should be demonstrated with something new and small.

---

## 7. Questions

Answer inline under each; the recommendation stands where you leave it blank.

1. **VRAM or RAM for the canvas surface?** VRAM is where the screen lives, so
   blitting to the screen never crosses the aperture, and the GAC draws there
   at full speed. RAM always exists, even on a machine with no video memory.
   **Recommendation: VRAM when `vram_present()`, RAM otherwise**, chosen by
   the library, with `gui_canvas_pixels()` giving the right pointer either
   way.

   **Decided (you):** the recommendation — VRAM when `vram_present()`, RAM otherwise.

2. **Does `disp_push_target()` / `disp_pop_target()` belong in
   `<pigeon/display.h>`?** It is what lets existing `disp_*` code draw into a
   canvas unchanged (§3), and the machinery is already there internally.
   It is also useful with no GUI at all — any program wanting to draw
   off-screen. **Recommendation: yes, in `display.h`,** as a general
   facility this library then uses, rather than something GUI-only.

   **Decided (you):** the recommendation — `disp_push_target()` / `disp_pop_target()` go in `<pigeon/display.h>` as a general facility.

3. **Should a canvas be able to keep a back buffer of its own?** A canvas
   that animates is drawing while the screen is being read. Since the canvas
   is already a separate surface that is blitted once a frame, tearing is
   mostly moot — but a slow canvas (a raytracer, say) might want to draw over
   several frames and present when done. **Recommendation: not in v1**; add
   `gui_canvas_present(id)` later if something needs it.

   **Decided (you):** the recommendation — no canvas back buffer in v1.

4. **What happens to canvas contents on resize?** The surface must be
   reallocated, so the old pixels are gone. Options: drop them and raise
   `GUI_DRAW` *(recommended)*; or scale the old contents into the new surface
   as a stopgap so there is no blank frame. **Recommendation: drop and
   redraw** — a stretched frame of a plot is a lie, and `GUI_DRAW` arrives in
   the same frame anyway.

   **Decided (you):** the recommendation — drop the pixels on resize and raise `GUI_DRAW`.

5. **What demonstrates the canvas?** **Recommendation: a new, small
   program** — a plotter or a spinning shape written for the canvas, a few
   hundred lines — rather than a hosted copy of `graph.c`. A copy of
   `graph.c` is 1,400 lines and would prove the canvas works by dragging a
   whole application through it; Phase 9 already taught that picking the
   first user badly is expensive
   *(checked: [phase9_srcalpha.md §11](../gac/plans/phase9_srcalpha.md#11-as-built))*.

   **Decided (you):** the recommendation — a new small program, not a hosted copy of `graph.c`. `graph.c` and `cube.c` stay untouched either way (§6).

6. **Does the canvas need `gui_canvas_animate`, or should the app just call
   `gui_damage(id)` every frame?** They are equivalent; `animate` is a
   convenience that also tells the library "do not bother coalescing, this
   one is always dirty". **Recommendation: keep `animate`** — it is one
   flag, and it lets the library skip work for the idle case, which is every
   other canvas.

   **Decided (you):** the recommendation — keep `gui_canvas_animate`.
