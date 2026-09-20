# `<pigeon/gui.h>`: a GUI library, and fonts you can read

> **Status: design, 2026-09-20. Nothing is built. Every question is now
> answered and folded in** — Q1–Q16, then Q17–Q22 raised by those answers,
> then [Q23](questions.md#23-how-much-line-editing-does-the-console-widget-do),
> settled through [fonts/build.md](../fonts/build.md), which also checked this
> plan against the code it rests on and found nothing here that has to change.
> Facts marked *(checked)* were read in the code on 2026-09-20; the compiler
> findings in [constraints.md](constraints.md) were **run**, not read — and
> **re-run on 2026-09-20**, reproducing byte for byte
> ([build.md §1](../fonts/build.md)). They now have a plan of their own,
> [compiler_plan.md](../compiler_plan.md).

---

## What you asked for

> right now when i use 720p the text is extremely small, barely readable. so
> what i would really like is a gui library that will allow me to do things
> similar to tkinter. there will be the classic objects like labels, buttons
> (with a handler function pointer) […] the handler function pointer will
> receive an event not just the click […] also we could do something like
> enable_dynamic_scaling […] also i want a font system.

Five things, and they turn out to be one thing with four parts:

1. **Text big enough to read** at 720p and up.
2. **Widgets** — labels, buttons, and the rest — with **handler function
   pointers**.
3. **Events**, not just clicks: hover, left, right, and the widget itself.
4. **A colour table** you can take or replace, per widget or wholesale.
5. **Dynamic scaling** — design at 720p, run anywhere, stretch or keep aspect.
6. **A canvas** — a widget the app draws into itself, so a plot or a spinning
   cube has somewhere to render ([canvas.md](canvas.md)).

**And one constraint:** `user/graph.c` and `user/cube.c` are **not modified**.
They keep working with no GUI library at all
([canvas.md §6](canvas.md#6-the-originals-are-not-touched)).

> **Where the questions are.** All of them are folded in as
> **Decided (you)** — [questions.md](questions.md) for this plan,
> [fonts/questions.md](../fonts/questions.md) for the font system, and
> [fonts/build.md §7](../fonts/build.md#7-questions) for the five raised by
> checking both plans against the code. **None is open.**

---

> **The fonts moved out.** You asked for the font documentation in a folder of
> its own and to be built first, so it is **[docs/fonts/](../fonts/README.md)**
> now — its own plan, with its own phases and questions. This plan depends on
> it; it does not depend on this one.

## The files

| file | what is in it |
|---|---|
| **README.md** (this) | what you asked for, what was found, the phases |
| [constraints.md](constraints.md) | **read this first** — what the compiler and the machine actually allow. Every decision elsewhere points back here |
| [design.md](design.md) | the architecture: the widget pool, the frame, what gets repainted |
| [events.md](events.md) | the event model, and how each event is manufactured |
| [theme.md](theme.md) | the pigeon colour table, roles, overriding |
| [scaling.md](scaling.md) | design coordinates, the four modes, and the font problem inside them |
| [canvas.md](canvas.md) | the canvas widget: a surface the app draws into itself |
| [fonts/shell.md](../fonts/shell.md) | **later:** the shell with a font you can change, recorded so it is not lost |
| [api.md](api.md) | the proposed header, and the widget catalogue |
| [questions.md](questions.md) | every question, your answers, and what was decided |
| [fonts/build.md](../fonts/build.md) | **both plans checked against the code** — what holds, what did not, and the amendments |

---

## What the audits found

Four things were checked before any of this was designed, because an API
written against a C the compiler does not have is worthless.

### 1. The readable font needs no new hardware — but a *bigger* one does

`SET_FONT` allows glyphs up to **8 pixels wide** and a cell up to 4096
pixels, so **8 × 16 on a 9 × 18 cell fits the existing device exactly**
*(checked: `gac.py:679-689`)*. That is 1,520 bytes of font data and turns a
720p console from **213 × 80** characters into **142 × 40**.

The cheap trick of scaling the existing 5 × 7 up does **not** work: doubling
a 5-wide glyph is 10 wide, and `glyph_w <= 8` refuses it. So 8 × 16 is
exactly the ceiling of what the device can draw today, and 1080p will want
the storage format widened ([fonts/device.md](../fonts/device.md)).

### 2. There is one font slot, and every program clobbers it

The device holds a single font; any `SET_FONT` replaces it; and `disp_init()`
re-uploads the 5 × 7 on **every program start** — it is the only caller in the
tree *(checked: `display.c:731-733`)*. The kernel console and all 97
`disp_text` call sites assume a 6-pixel cell. **So font slots are not a
luxury: they are what lets a GUI use a big font without breaking the console
around it.** Phase 3 foresaw this and left room *(checked:
`phase3_gac.md:290-299`)*.

### 3. Silent miscompiles, confirmed by running them

Not "unsupported" — **accepted, and wrong, with no diagnostic**
([constraints.md §1](constraints.md)):

| written | expected | actually |
|---|---|---|
| `y = x` on a 12-byte struct | copies 12 bytes | **copies 4** |
| `f(q)` passing a struct by value | passes it | **passes its first word** |
| `int a = 2*3+1;` at file scope | 7 | **0** |
| `int a = -1;` at file scope | −1 | **0** |
| `a / b` on negative operands | −10 | **2147483638** |
| `a >> 1` on a negative value | −4 | **2147483644** |

These shape the whole API: **every function takes a pointer, and every table
is filled at run time.** They are also worth fixing in the compiler
independently of this library — a rejected program is a nuisance, a silently
wrong one is a trap — and they now have a plan of their own,
**[compiler_plan.md](../compiler_plan.md)**, which re-ran every row above and
added the last two.

What *does* work, and is the one thing this design cannot do without:
**function pointers as struct members, called through `->`** — verified.

### 4. A widget tree cannot be built from pointers

`struct gui_widget { struct gui_widget *parent; }` does not compile: a struct
may not mention itself *(verified)*. So widgets live in **one pool and link by
`int` index**, exactly as `lib/pigeon/fs.c` chains its blocks *(checked)*.
That also avoids recursion, which matters because the frame stack is 256 KB
with **no overflow check** *(checked: `codegen.py:23`)*.

---

## The shape of it

```c
gui_init(64);
gui_font_load(1, "/etc/font/mono8x16.pf");
gui_font_auto(1);
gui_design(1280, 720);
gui_scale(GUI_SCALE_ASPECT);

quit = gui_button(20, 60, 140, 34, "Quit", on_quit);
gui_set_color(quit, GUI_BG, 0xFF204080);     /* override just this one */

gui_run();
```

```c
void on_quit(int id, gui_event *e) {
    if (e->type == GUI_CLICK && e->button == GUI_MOUSE_LEFT) gui_stop();
    if (e->type == GUI_ENTER) {              /* my hover, not the theme's */
        e->cancel = 1;
        gui_set_color(id, GUI_BG, 0xFF802020);
    }
}
```

---

## Phases

Each leaves the machine working. **F1 is independent of everything else and
fixes the complaint on its own** — given the one line in `k_tidy()` that puts
the console's font back when a program ends, without which a program that
loads a font leaves the shell drawing on the wrong grid
([fonts/build.md §2](../fonts/build.md)).

**[The font plan](../fonts/README.md) is built first, in full** — F1 to F5 —
because you said so and because measuring agreed: 8 × 16 is not actually
enough ([fonts/sizes.md](../fonts/sizes.md)). Only then:

| | what | side | needs |
|---|---|---|---|
| **G1** | The pool, the frame, damage, label + button + panel | guest | F3 |
| **G2** | The event model in full: hover, drag, wheel, focus, keys, Tab | guest | G1 |
| **G3** | Theme, per-widget colour, `/etc/gui.conf` | guest | G1 |
| **G4** | Scaling: the four modes, `GUI_RESIZE`, `gui_font_auto` | guest | G3 |
| **G5** | The rest of the widgets: entry, list, slider, checkbox, radio, image | guest | G2 |
| **G6** | **The canvas** and `disp_push_target()`; **the console widget**, input and output; `gui_fps()` | guest | G4 |
| **G7** | `panel.bin`, the first program built with it | guest | G5, G6 |

---

## What this is not

- **A window manager.** One app owns the screen, as every program does now.
- **A console back-end.** `edit.c` is ANSI escapes to stdout and does not
  include `display.h` at all *(checked)*; it stays outside.
- **A port of the existing UIs.** `explorer.c`, `disc.c`, `files.c`,
  `graph.c` and `cube.c` keep working untouched — **`graph.c` and `cube.c`
  are not modified by any phase of this plan**, so the machine keeps working
  without any of it ([canvas.md §6](canvas.md#6-the-originals-are-not-touched)).
- **Reflowing layout** — rows, columns, weights. Absolute positions first, as
  you asked; a layout pass can come later over the same machinery.
- **Proportional text, before F6.** Nothing on the machine supports
  variable-width glyphs today, in three independent places *(checked)* — the
  font plan adds it in [F6](../fonts/README.md), after this library's first
  phases, and the GUI's own chrome is what it is for
  ([fonts/vector.md §3](../fonts/vector.md)).
- **Overlapping windows or z-order** beyond "the modal is on top".

---

## The one recommendation

**Do F1 on its own first.** One 8 × 16 font file makes 720p readable, needs no
device change, no library and no decisions — and it is the problem you
actually described. Everything else here is worth doing, and is much larger;
it will be easier to judge with the text already readable
([questions.md Q8](questions.md)).
