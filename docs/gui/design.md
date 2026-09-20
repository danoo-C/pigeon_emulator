# The design: a pool, a frame, and what gets repainted

> Part of [the GUI plan](README.md). **Status: design, 2026-09-20. Questions
> open ([questions.md](questions.md)).** Read
> [constraints.md](constraints.md) first — several decisions here are forced
> rather than chosen. Facts marked *(checked)* were read in the code on
> 2026-09-20.

---

## 1. Retained, in a codebase that is immediate

You asked for tkinter: objects that persist, with handlers attached. That is
**retained mode**, and it is worth saying plainly that **every UI on this
machine today is immediate mode** — `explorer.c`, `graph.c`, `disc.c`,
`files.c` all do "set a dirty flag, then clear the screen and draw the world
from globals" *(checked: `explorer.c:1073-1076`, `graph.c:1464-1467`,
`disc.c:689-692`)*.

Retained is still right here, for a reason that is specific rather than
fashionable: **the duplication in those files is exactly the duplication a
retained library removes.** The same list widget is written three times, the
same popup menu once, the same single-line text input twice, the same
`blank`/`place`/`place_right` row composer three times byte for byte, and the
same palette five times *(checked)*. Immediate mode is why — with nothing to
attach state to, every program re-implements the state.

What retained must **not** do is take the frame loop away, because several
programs cannot give it up (§4).

---

## 2. Widgets are a pool, and links are indices

The compiler cannot compile `struct gui_widget { struct gui_widget *parent; }`
— a struct may not mention itself ([constraints.md §2](constraints.md)). So:

```c
/* One array, allocated once. A widget is an int index into it. */
static gui_widget *gui_pool;     /* gui_init() mallocs it            */
static int gui_cap;              /* how many                         */
static int gui_used;

typedef struct {
    int kind;                    /* GUI_LABEL, GUI_BUTTON, ...        */
    int parent, first, next;     /* the tree, as indices; -1 for none */
    int x, y, w, h;              /* design coordinates (scaling.md)   */
    int flags;                   /* visible, enabled, focused, ...    */
    unsigned colour[GUI_ROLES];  /* 0 = take the theme's (theme.md)   */
    gui_handler on;              /* a function pointer -- these work  */
    void *user;                  /* whatever the app wants            */
    int i0, i1;                  /* per-kind state: selection, value  */
    char *text;
} gui_widget;
```

This is not a workaround grudgingly accepted. It is **better than pointers on
this machine**, for three reasons:

1. **`lib/pigeon/fs.c` already does it** — it chains blocks by index, not
   pointer, for the same compiler reason *(checked)*. The idiom is house
   style.
2. **The frame stack is 256 KB with no overflow check** *(checked:
   `codegen.py:23`)*. A recursive tree walk is a real hazard; **walking an
   array with an explicit index stack is not**, and the library does that
   everywhere.
3. **An index survives what a pointer would not.** A stale id is detectable
   (`id < 0 || id >= gui_used`); a stale pointer is not.

**Handlers are function pointers stored on the widget** — verified to work
as struct members called through `->` ([constraints.md §3](constraints.md)),
which is the one feature this whole design needs.

---

## 3. The frame

```c
gui_init(256);                         /* the pool                      */
gui_font_load(1, "/etc/font/mono8x16.pf");
gui_design(1280, 720);                 /* scaling.md                    */

ok = gui_button(20, 20, 120, 32, "Run", on_run);

while (gui_poll()) { }                 /* or your own loop, see below   */
```

`gui_poll()` does exactly this, once:

1. **Snapshot the input.** `mouse_x()`, `mouse_y()` **once** into one
   position — they are two separate bus transactions and can disagree
   *(checked: `input.c:35-36`)* — plus `mouse_buttons()`, then drain
   `mouse_event()` and `key_event()` to exhaustion, as every existing UI does
   *(checked: `explorer.c:1063-1071`)*.
2. **Synthesize.** Hit-test the pointer, compare with last frame, and raise
   `GUI_ENTER` / `GUI_LEAVE` / `GUI_MOTION` / `GUI_PRESS` / `GUI_RELEASE` /
   `GUI_CLICK` / `GUI_DRAG` / `GUI_WHEEL` / `GUI_KEY`. None of these exist in
   the hardware ([events.md](events.md)).
3. **Check the mode.** `disp_generation()` changes when the screen does
   *(checked)*. If it moved: re-run layout, raise `GUI_RESIZE`, damage
   everything.
4. **Dispatch**, default behaviour first, then the app's handler.
5. **Repaint what is damaged** (§5), and present if double-buffered.

It returns 0 when the app has called `gui_quit()`.

---

## 4. The library must not own the loop

Three programs in the tree would break if it did *(checked)*:

- **`explorer.c` execs a child** and then blocks while the kernel paints its
  console over the UI (`explorer.c:694-708`).
- **`installer.c` has no loop at all** — it is straight-line with blocking
  waits (`installer.c:376, 405`).
- **`disc.c` calls `render()` re-entrantly** from inside a long copy, to show
  progress (`disc.c:416-417`).

So the API is **`gui_poll()` first, `gui_run()` second**:

```c
int gui_poll(void);    /* one frame: input, dispatch, repaint. 0 to quit  */
void gui_run(void);    /* while (gui_poll()) {} -- for apps that want it  */
void gui_draw(void);   /* repaint now, for a program mid-operation        */
void gui_damage_all(void);  /* "something changed that I did not tell you about" */
```

`gui_damage_all()` is the hook `disc.c` needs: it polls `cd_generation()` for
a disc swap and redraws with no key pressed *(checked: `disc.c:680-688`)*.
A retained library with no such door would make that impossible.

---

## 5. Repainting, and the back-buffer trap

This is the sharpest problem in the design, and it is not obvious.

**`disp_present()` is a page flip, not a copy.** `display.h` says so
outright: *"the buffer you draw into after a present still holds the frame
BEFORE the one now showing"*, and *"the one pattern this breaks is redrawing
only what changed"* *(checked: `display.h:86-97`)*. So the two things a GUI
wants most — no flicker, and repainting only what moved — are in direct
conflict as the library stands.

The three ways out, and what each costs:

| | how | flicker | cost a frame |
|---|---|---|---|
| **(a)** | back buffer + full redraw every frame | none | a whole screen, always |
| **(b)** | no back buffer, repaint only damaged rects, **never clear** | none | tiny |
| **(c)** | back buffer + **two damage sets** | none | the last two frames' damage |

**(b) is not a compromise — it is how `installer.c` already works.** It
draws straight to the screen and does not flicker, because it never clears
wholesale: each `show()` repaints its own band *(checked:
`installer.c:81-84`)*. Flicker comes from clearing, not from the absence of a
buffer.

**(c) is the textbook fix for the page flip**, and it is small: keep the
damage list for this frame and the previous one, and when drawing into a
buffer repaint their **union**, because that buffer is one frame stale. Two
lists and a union.

**Recommendation: (c) when a back buffer is available, (b) when it is not**,
chosen automatically — `disp_use_back_buffer()` returns 0 if the heap could
not provide one *(checked: `display.h:111`, and note `graph.c:1436` ignores
that return value)*. `explorer.c`'s case, where the program must leave the
display where the kernel left it, is then just "(b), because there is no back
buffer", with no special case in the library.

Damage is **rectangles in device coordinates**, coalesced: a list of at most
N, and once it overflows, one rectangle covering the screen. The same shape
Phase 8 uses on the host for frames *(checked:
`docs/gac/plans/phase8_bandwidth.md`)*.

---

## 6. Layout is resolved, not macro-expanded

Every existing UI computes layout by macro expansion — `COLS`, `ROWS`,
`LIST_Y`, `PLOT_H` are expressions over `DISP_W`/`DISP_H` re-evaluated at
every use *(checked: `explorer.c:45-60`, `graph.c:768-787`)*. That is
elegant and it cannot be handed to a library as data.

So the library resolves layout **once**, into each widget's device rectangle,
and re-resolves when the design size, the scale mode, the font or the screen
changes. A widget stores **design** coordinates; `gui_device_rect(id, ...)`
gives the pixels ([scaling.md](scaling.md)).

**This is also where the font ripple lands.** With more than one font,
`GLYPH_W`/`GLYPH_H` stop being constants ([fonts/device.md §4](../fonts/device.md)), so a
widget's natural size has to come from `gui_text_w(font, s)` and
`gui_cell_h(font)` at layout time, not from a macro.

---

## 7. What it absorbs

The duplication a GUI library removes, all of it verified *(checked)*:

| thing | copies today |
|---|---|
| `blank` / `place` / `place_right` row composer | 3, byte-identical |
| The palette block | **5** |
| The layout macro block | 4, plus `STATUS_Y` in 3 more |
| `say` / `say_err` status line | 3 |
| The main-loop skeleton | 4 |
| A list with selection and scrolling | 3 |
| A single-line text input with caret | 2 |
| Scroll clamping | 3 |
| A popup menu, measured and edge-flipped | 1 |
| "Press any key" modal | 3 |

Hit-testing is the biggest prize and the biggest risk: today it is written by
hand against the same macros used to draw, with deliberate off-by-N
compensations — `explorer.c`'s `menu_at` offsets by exactly 3 pixels to match
`draw_menu`'s box *(checked: `explorer.c:868-875` against `:427`)*.
Centralising that means one place decides both, which is the point; it also
means those constants stop being the source of truth, which is the risk.

---

## 8. Not in the design

- **A console back-end.** `edit.c` is ANSI escapes to stdout and does not
  include `display.h` at all *(checked)*. It stays outside.
- **`docs_gui.pgs`.** One process launch per draw call; its own header
  already concedes the model does not scale *(checked: `:10-13`)*.
- **Reflowing layout (rows, columns, weights).** Absolute positions first, as
  you asked; a layout pass can come later over the same resolved-rect
  machinery.
- **Overlapping windows, z-order beyond "modal on top".**
- **Proportional text** ([fonts/vector.md §3](../fonts/vector.md)).
