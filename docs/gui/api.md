# `<pigeon/gui.h>`: the proposed header, and the widgets

> Part of [the GUI plan](README.md). **Status: design, 2026-09-20. Every
> question is decided ([questions.md](questions.md),
> [fonts/build.md §7](../fonts/build.md#7-questions)).** Everything here is shaped by
> [constraints.md](constraints.md) — no `enum`, no `switch`, no struct by
> value, no self-referential struct, no brace initialisers. Read that first
> if a choice here looks odd.

---

## 1. A whole program

```c
#include <pigeon/gui.h>

int running = 1;                    /* a global: no static locals here */

void on_quit(int id, gui_event *e) {
    if (e->type == GUI_CLICK) gui_stop();
}

void on_colour(int id, gui_event *e) {
    if (e->type == GUI_ENTER) {              /* my own hover, not the theme's */
        e->cancel = 1;
        gui_set_color(id, GUI_BG, 0xFF802020);
    } else if (e->type == GUI_LEAVE) {
        gui_set_color(id, GUI_BG, 0);        /* 0 = back to the theme */
    }
}

int main(void) {
    int title;
    int quit;

    gui_init(64);                            /* 64 widgets, from the heap */
    gui_font_load(1, "/etc/font/mono8x16.pf");
    gui_font_auto(1);
    gui_design(1280, 720);
    gui_scale(GUI_SCALE_ASPECT);

    title = gui_label(20, 20, "Pigeon control panel");
    quit  = gui_button(20, 60, 140, 34, "Quit", on_quit);
    gui_button(180, 60, 140, 34, "Hover me", on_colour);

    gui_run();                               /* until gui_stop() */
    gui_end();
    return 0;
}
```

Nothing above passes a struct by value, initialises a global with anything
but a literal, or needs a `switch`.

---

## 2. Lifecycle, and the frame

```c
int  gui_init(int max_widgets);   /* 0 on failure: the heap said no       */
void gui_end(void);               /* free the pool, leave the screen tidy */

int  gui_poll(void);              /* ONE frame: input, dispatch, repaint  */
void gui_run(void);               /* while (gui_poll()) {}                */
void gui_stop(void);              /* make the next gui_poll() return 0    */

void gui_draw(void);              /* repaint now, mid-operation           */
void gui_damage(int id);          /* this widget changed                  */
void gui_damage_all(void);        /* something outside the library changed */
```

`gui_poll()` rather than only `gui_run()` because **three programs in the
tree could not use a library that owned the loop** — `explorer.c` execs a
child and blocks, `installer.c` is straight-line, `disc.c` renders
re-entrantly from inside a copy *(checked)*. `gui_damage_all()` is the door
`disc.c` needs for its disc-swap poll ([design.md §4](design.md)).

---

## 3. Making widgets

Every one returns an `int` id, or `-1` if the pool is full.

```c
int gui_label   (int x, int y, char *text);
int gui_button  (int x, int y, int w, int h, char *text, gui_handler on);
int gui_checkbox(int x, int y, char *text, int checked, gui_handler on);
int gui_radio   (int x, int y, char *text, int group,   gui_handler on);
int gui_entry   (int x, int y, int w, char *buf, int cap, gui_handler on);
int gui_slider  (int x, int y, int w, int lo, int hi, int value, gui_handler on);
int gui_progress(int x, int y, int w, int lo, int hi, int value);
int gui_list    (int x, int y, int w, int h, gui_handler on);
int gui_panel   (int x, int y, int w, int h);       /* a frame to parent into */
int gui_image   (int x, int y, int w, int h, char *bmp_path);   /* -sprite's blit */
int gui_rule    (int x, int y, int w);              /* a separator */
int gui_canvas  (int x, int y, int w, int h, gui_handler on);  /* canvas.md */
int gui_console (int x, int y, int w, int h, int rows);        /* §9        */
```

Two notes:

- **`gui_entry` borrows your buffer** — it writes into `buf` up to `cap` and
  never allocates. That suits a machine where the app already owns its
  storage, and it means no ownership question.
- **`gui_canvas` is the one widget the library does not draw.** It owns a
  surface; the app fills it and gets `GUI_DRAW` when it should. It is what
  gives a plot or a spinning cube somewhere to render, and it has a file of
  its own ([canvas.md](canvas.md)).
- **`gui_image` draws through `gac_blit_alpha(..., GAC_SRC_ALPHA)`** when the
  file has an alpha channel, so an icon with a soft edge composites properly
  *(checked: Phase 9 shipped this, `docs/gac/plans/phase9_srcalpha.md`)*.
  That is what makes icons look like icons rather than squares.

### Parenting and the tree

```c
void gui_attach(int child, int parent);   /* both are ids */
int  gui_parent(int id);
int  gui_first_child(int id);
int  gui_next_sibling(int id);
```

Indices, not pointers — a struct cannot name itself here
([constraints.md §2](constraints.md)). Traversal is a loop over the pool, not
recursion, because the frame stack is 256 KB with no overflow check
*(checked: `codegen.py:23`)*.

---

## 4. Reading and changing a widget

```c
void gui_set_text (int id, char *text);
char *gui_text    (int id);
void gui_set_value(int id, int v);      /* checkbox, slider, list selection */
int  gui_value    (int id);
void gui_set_color(int id, int role, unsigned c);   /* 0 = use the theme */
void gui_move     (int id, int x, int y);           /* design coordinates */
void gui_resize   (int id, int w, int h);
void gui_show     (int id, int visible);
void gui_enable   (int id, int enabled);
void gui_focus    (int id);
void gui_rect     (int id, int *x, int *y, int *w, int *h);   /* device pixels */
void gui_set_user (int id, void *p);
void *gui_user    (int id);
```

Every setter damages the widget, which is the reason they are setters and not
public struct fields: writing `w->x = 10` directly would move it without
repainting what it left behind.

`gui_user` is the escape hatch — attach your own record to a widget without
the library knowing anything about it.

---

## 5. Theme, font, scaling

```c
void gui_theme_pigeon(gui_theme *t);        /* theme.md */
void gui_use_theme(gui_theme *t);

int  gui_font_load(int slot, char *path);   /* fonts.md */
void gui_font(int slot);                    /* pin one  */
void gui_font_auto(int on);                 /* pick per scale */
int  gui_text_w(int slot, char *s);
int  gui_cell_h(int slot);

void gui_design(int w, int h);              /* scaling.md */
void gui_scale(int mode);
```

---

## 6. Events

The full model is in [events.md](events.md). In one line:

```c
typedef void (*gui_handler)(int id, gui_event *e);
```

verified to work as a struct member called through `->`
([constraints.md §3](constraints.md)).

---

## 7. Where each widget came from

Not invented — nine of the eleven already exist in the tree, written more
than once *(checked)*:

| widget | already written in |
|---|---|
| list with selection + scrolling | `explorer.c`, `disc.c`, `files.c` — **3 copies** |
| single-line entry with caret | `explorer.c`, `graph.c` — 2 copies |
| popup menu, measured and edge-flipped | `explorer.c:828-875` |
| status line (`say` / `say_err`) | `explorer.c`, `disc.c`, `files.c` — 3 copies |
| "press any key" modal | `explorer.c`, `installer.c`, `graphics.c` — 3 copies |
| label rows / a form | `installer.c:42-50`'s named row constants |
| progress | `disc.c`'s re-entrant "copying…" render |
| image | `img.c`, `splash.c` |
| canvas | `graph.c`'s plot area, `cube.c`'s whole screen |

The row-composition helpers `blank` / `place` / `place_right` are **byte
identical across three files** and become the library's internals.

---

## 8. Naming

Everything is `gui_`, **including internals**. There is one translation unit
and `static` hides nothing: two files with `static int helper(void)` collide
with `'helper' is defined twice` *(checked: `cc.py:115-140`,
`analyzer.py:115-132`)*. This is why `gac_`, `__fs_`, `bmp_` and `__stdio_`
exist, and the library follows the house rule.


---

## 9. The console widget

**Your answer to Q13 asked for this**, and it settles the design in one
sentence: *"putting multiple would show the same thing."*

So there is **one text buffer per program**, and a console widget is a
**view** onto it. Two consoles on screen show the same output, scrolled
independently. That is the right model anyway: the buffer is your program's
output, and where it is shown is a layout question.

```c
int  gui_console(int x, int y, int w, int h, int rows);  /* rows of scrollback */
void gui_console_print(int id, char *s);
void gui_console_clear(int id);
void gui_console_font(int id, int slot);   /* denser than the UI font, usually */
int  gui_console_capture(int id);          /* this program's printf lands here */
void gui_console_prompt(int id, char *s);  /* what the input line starts with  */
```

**Lines are stored as text, not pixels.** A 200-line ring of `char *`, not a
bitmap — so the same buffer re-wraps when the widget is resized, when the
screen mode changes, or when the font does. A pixel scrollback would be wrong
at the first resize.

**It follows the bottom unless you have scrolled away**, which is what every
terminal does; the wheel and Page Up scroll it, and printing while scrolled
back does not yank you to the end.

**`gui_console_capture()` redirects this program's `printf`.** Today `printf`
goes straight to `write(STDOUT)` *(checked: `lib/pigeon/stdio.c`)*, so this
needs `stdio` to gain a redirect hook — small, and useful on its own. It is
**opt-in**, so nothing changes for a program that does not ask, and it is per
*program*, not per widget, because there is one buffer.

**It takes input as well as output** — you reversed the recommendation on
[Q20](questions.md#20-does-the-console-widget-take-input), so it is a small
terminal rather than a log pane. When it has focus it shows a caret, echoes
what you type, and raises **`GUI_LINE`** with the finished line on Enter:

```c
void on_console(int id, gui_event *e) {
    if (e->type == GUI_LINE) run_command(gui_console_line(id));
}
```

How much editing it does is [Q23](questions.md#23-how-much-line-editing-does-the-console-widget-do)
— the recommendation is characters, Backspace, Enter, history on Up/Down and
left/right within the line, which is where a console stops being annoying.
Tab completion stays with the app, because only the app knows what is being
completed. The kernel's own line editor is a much larger thing and belongs
with [the shell plan](../fonts/shell.md).

**Why this is a good answer to "does a GUI app own the whole screen".** The
question was whether a GUI program could still print. With this widget it
never has to choose: it owns the screen *and* has somewhere for output to go.
