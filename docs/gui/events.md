# Events: what a handler gets, and who synthesizes it

> Part of [the GUI plan](README.md). **Status: design, 2026-09-20. Every
> question is decided ([questions.md](questions.md),
> [fonts/build.md §7](../fonts/build.md#7-questions)).** Facts marked *(checked)* were read
> in the code on 2026-09-20.

**What you asked for:** the handler receives an event, not just "a click" —
`EVENT_HOVER`, `EVENT_CLICK` with `MOUSE_LEFT` / `MOUSE_RIGHT` — and the
object, so on hover you can either let the library do its thing or change the
colour yourself.

---

## 1. The hardware gives almost none of this

| what a GUI wants | what the machine has |
|---|---|
| hover / enter / leave | **nothing** — poll the position and diff it |
| motion, drag | **nothing** — difference successive polls |
| click (press *and* release on the same widget) | press and release **edges** only |
| double click | **nothing**, no timestamps either |
| modifier flags on an event | **nothing** — latch Shift/Ctrl from key edges |
| left / right / middle | yes, as bits and edges |
| wheel | yes — as **press+release of buttons 5 and 6**, one pair a notch |
| a drawn cursor | **nothing, anywhere** |

*(checked: `emulator/devices/hid.py`, `lib/pigeon/input.h`.)* Every existing
UI synthesizes what it needs by hand — `explorer.c`'s `track_pointer` and
`graph.c`'s `update_trace` are both "poll the position each frame and see if
it changed" *(checked: `explorer.c:1030-1036`, `graph.c:1419-1429`)*.

**So synthesizing events is not a convenience the library adds on top. It is
most of what the library is.**

---

## 2. The event

```c
#define GUI_ENTER    1      /* the pointer came onto this widget        */
#define GUI_LEAVE    2
#define GUI_MOTION   3      /* it moved while over this widget          */
#define GUI_PRESS    4
#define GUI_RELEASE  5
#define GUI_CLICK    6      /* press and release, both on this widget   */
#define GUI_DRAG     7      /* moved with a button held                 */
#define GUI_WHEEL    8      /* e->wheel is -1 or +1 a notch             */
#define GUI_KEY      9      /* a key edge, while this widget has focus  */
#define GUI_CHAR    10      /* a character typed into it                */
#define GUI_CHANGE  11      /* its value changed: checkbox, slider, entry */
#define GUI_FOCUS   12
#define GUI_BLUR    13
#define GUI_RESIZE  14      /* the screen mode changed (design.md §3)   */
#define GUI_DRAW    15      /* a canvas needs its pixels (canvas.md)    */
#define GUI_LINE    16      /* a console widget got a line (api.md §9)  */

#define GUI_MOUSE_LEFT   1
#define GUI_MOUSE_RIGHT  2
#define GUI_MOUSE_MIDDLE 3

#define GUI_MOD_SHIFT 1
#define GUI_MOD_CTRL  2
#define GUI_MOD_ALT   4

typedef struct {
    int type;        /* GUI_CLICK, ...                                   */
    int id;          /* the widget it is about                           */
    int button;      /* GUI_MOUSE_LEFT, ... for press/release/click      */
    int x, y;        /* the pointer, in the WIDGET's own coordinates     */
    int wheel;       /* -1 or +1                                         */
    int key;         /* the keycode, for GUI_KEY                         */
    int ch;          /* the character, for GUI_CHAR                      */
    unsigned mods;   /* GUI_MOD_*, latched by the library                */
    int cancel;      /* set it to 1 to stop the default behaviour        */
} gui_event;

typedef void (*gui_handler)(int id, gui_event *e);
```

**Function pointers as struct members, called through `->`, are verified to
work** ([constraints.md §3](constraints.md)) — this is the feature the whole
API rests on.

Three points about the shape, each forced or deliberate:

- **`int id`, not `gui_widget *`.** A struct cannot refer to itself on this
  compiler ([constraints.md §2](constraints.md)), so the tree is indices
  already; passing an id keeps one model rather than two, and a bad id is
  *detectable* where a bad pointer is not. The accessors — `gui_rect(id,…)`,
  `gui_set_color(id,…)`, `gui_text(id)`, `gui_user(id)` — are how you reach
  the object. [questions.md Q2](questions.md) asks whether you would rather
  have the pointer.
- **`gui_event *e`, never `gui_event e`.** Passing a struct by value copies
  only its first four bytes, silently ([constraints.md §1](constraints.md)).
  Everything in this library is a pointer for that reason.
- **`x` and `y` are widget-local**, so a handler does not need to know where
  its widget ended up — which matters once scaling moves it
  ([scaling.md](scaling.md)).

---

## 3. Default behaviour, and how you override it

You asked to be able to *either* let the library handle hover *or* do it
yourself. The order is:

1. The library applies the **default** for the event — on `GUI_ENTER` a
   button takes its `GUI_HOVER` colour, on `GUI_PRESS` its `GUI_PRESS`
   colour, on `GUI_CLICK` a checkbox toggles.
2. Then your handler runs, and can do anything, including overriding what
   step 1 just did.

So the plain case needs no code:

```c
void on_run(int id, gui_event *e) {
    if (e->type == GUI_CLICK && e->button == GUI_MOUSE_LEFT) start();
}
```

and the "I want my own hover" case says so:

```c
void on_run(int id, gui_event *e) {
    if (e->type == GUI_ENTER) {
        e->cancel = 1;                        /* not the theme's hover */
        gui_set_color(id, GUI_BG, 0xFF802020);
    } else if (e->type == GUI_LEAVE) {
        gui_set_color(id, GUI_BG, 0);         /* 0 = back to the theme */
    }
}
```

`e->cancel` is checked **before** the default is applied, so setting it in
the handler works even though the handler runs second — the library applies
the default, calls you, and rolls it back if you cancelled. *(An alternative
is to call the handler first; [questions.md Q3](questions.md) asks which you
prefer, since it is visible in exactly this snippet.)*

---

## 4. How each event is manufactured

**Once a frame**, `gui_poll()`:

- Reads the pointer **once** into a snapshot. `mouse_x()` and `mouse_y()` are
  two separate bus transactions and can straddle a move *(checked:
  `input.c:35-36`)* — reading them per widget would let one widget see a
  different pointer from the next.
- **Hit-tests** the snapshot against the widget rectangles, topmost first,
  skipping hidden and disabled ones. The result is *the* hovered widget.
- **`GUI_ENTER` / `GUI_LEAVE`** come from comparing that with last frame's.
- **`GUI_MOTION`** when it is the same widget and the position moved.
- **Drains `mouse_event()`** — each edge is `ME_VALID`, `ME_PRESSED`,
  `ME_BUTTON` *(checked: `input.h:31-37`)*. Buttons 0/1/2 become
  press/release; **buttons 5 and 6 are the wheel**, and their release is
  discarded so one notch is one `GUI_WHEEL`.
- **`GUI_CLICK`** when a release lands on the widget that took the press. The
  press target is remembered, so dragging off a button and releasing does not
  click it — which is what people expect and what no existing program does.
- **`GUI_DRAG`** from the live button mask, not the edge queue — the same
  choice `graph.c` documents *(checked: `graph.c:1402-1404`)*, because edges
  tell you a drag started and only polling tells you it is still going.
- **Drains `key_event()`**, latching Shift and Ctrl from their own keycodes
  (`LSHIFT 0x89`, `LCTRL 0x8B`, …) into `e->mods`. **There are no modifier
  flags in the hardware** and `input.h` does not even declare those keycodes
  — `kernel.c` and `edit.c` each hardcode them *(checked: `kernel.c:64-65`,
  `edit.c:54-55`)*. The library declares them once and latches them once.
- **`GUI_CHAR`** comes from `key_read()`, the character FIFO, for the focused
  widget. **Do not mix the two streams for text**: `graph.c` carries an
  explicit warning that reading both reorders edits *(checked:
  `graph.c:1335-1342`)*, so a text entry reads characters and everything else
  reads edges.

### 4.1 Things that will bite, and what the library does

- **Key repeat arrives from the front ends** — pygame sets 400 ms/40 ms
  *(checked: `display.py:157`)*, the browser passes the OS's through. A
  repeat is another press with **no intervening release**. Fine for typing,
  wrong for "is this key held": the library trusts the latch, not the count.
- **The queues are 256 deep and drop the oldest silently** *(checked:
  `hid.py:53-55`)*. `gui_poll()` drains to empty every frame, so a slow frame
  loses input rather than reordering it.
- **The pointer goes stale across a mode change** until the next mouse move,
  because HID has no idea the screen resized *(checked)*. On `GUI_RESIZE` the
  library therefore treats the pointer as *unknown* — no hovered widget —
  until a fresh position arrives, rather than reporting a hover at a position
  that means nothing.
- **Nothing drains the queues for you on entry.** The kernel drains on
  program *exit* (`k_tidy`) *(checked: `kernel.c:1547-1549`)*, so the Enter
  that launched your program is gone — but a program started another way may
  still see stale input. `gui_init()` drains, as `bios2.c` does
  *(checked: `bios2.c:317-319`)*.

---

## 5. The cursor

**Nothing draws one, anywhere in the emulator or the guest** *(checked)* —
what you see is your host pointer over the canvas. For a GUI that is
tolerable at first and wrong in the end: the host pointer does not know about
letterboxing ([scaling.md](scaling.md)) and cannot be themed.

Drawing one is now cheap, because Phase 9 landed: a soft-edged cursor is an
RGBA sprite through `gac_blit_alpha(..., GAC_SRC_ALPHA)`
*(checked: `docs/gac/plans/phase9_srcalpha.md`)*. The work is not the
drawing, it is the **un**drawing — the cursor has to be lifted before the
frame under it is repainted, which is one more damage rectangle a frame.

[questions.md Q5](questions.md) asks whether the library draws a cursor at
all in v1.
