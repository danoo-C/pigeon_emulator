# Questions

> Part of [the GUI plan](README.md). **Status: Q1–Q16 answered 2026-09-20 and
> folded in, except [Q6](#6-retained-mode-at-all), which is re-asked with the
> explanation you wanted. A third set, Q17–Q22, is raised by your answers.**
> Answer
> inline under each; **the recommendation stands where you leave it blank.**
> Questions about fonts live in [fonts questions](../fonts/questions.md) and about
> the canvas in [canvas.md §7](canvas.md#7-questions) — they are separable,
> and fonts F1 is worth doing whatever you decide here.
>
> **Q1–Q8 are decisions I had to make to write the design at all. Q9–Q16 are
> things your first description did not settle and I filled in without
> asking** — they are listed last but they are the ones most likely to be
> wrong, because I guessed.

---

## 1. Overriding a colour: a setter, or a second constructor?


You asked for `button(x, y, ptr)` taking the theme's colour and
`button(x, y, ptr, colour)` overriding it. C has no overloading and no
default arguments, so ([theme.md §4](theme.md)):

- **(a)** `gui_button(...)` then `gui_set_color(id, GUI_BG, c)`
- **(b)** `gui_button(...)` and a twin `gui_button_c(..., colour)`
- **(c)** an options struct, filled then passed by pointer

**Recommendation: (a).** A button has at least four colours that matter —
background, ink, hover, press — so a single positional `colour` can only be
one of them, and (b) becomes a `_c`/`_c2` family across eleven widgets. (a)
is one function for every role and every widget. A thin `gui_button_c()`
meaning "background, please" can sit on top later if the short form is worth
it.

---

**Decided (you):** blank, so the recommendation — `gui_set_color()`.

## 2. Does a handler get the widget's id, or a pointer to it?


```c
typedef void (*gui_handler)(int id, gui_event *e);        /* recommended */
typedef void (*gui_handler)(gui_widget *w, gui_event *e); /* the alternative */
```

You asked for "the object pointer". The tree is already indices because a
struct cannot refer to itself on this compiler
([constraints.md §2](constraints.md)), so an id keeps one model instead of
two, and a bad id is detectable where a bad pointer is not. The cost is that
you reach fields through `gui_text(id)`, `gui_value(id)`, `gui_set_color(id,
…)` rather than `w->text`.

**Recommendation: the id.** The pool never moves, so pointers *would* be
safe — but public struct fields also let an app write `w->x = 10` and move a
widget without repainting what it left behind, which the setters prevent.


---

**Decided (you):** *"the id, its safer and the setter can let the library know something changed"* — the recommendation, and for the second reason exactly: a setter is the only place damage can be raised.

## 3. Does the app's handler run before or after the default?


[events.md §3](events.md). Either the library applies its default (hover
colour, checkbox toggle) and then calls you, or it calls you first and you
say whether the default should follow.

**Recommendation: default first, then the handler, with `e->cancel` checked
before the default is applied.** It makes the common case — "I only care
about clicks" — need no code at all, and the override case says exactly what
it means.

---

**Decided (you):** blank, so the recommendation — default first, then the handler.

## 4. Does `/etc/gui.conf` ship in v1?


A theme read from a file, in `explorer.conf`'s format
([theme.md §5](theme.md)).

**Recommendation: no, not in v1.** The library is complete without it —
`gui_theme_pigeon()` plus assignment covers re-theming from C. A config file
is a parser plus error reporting, and it is only worth it once you want to
re-theme without rebuilding.


---

**Decided (you):** *"both, a file and a default in the library, so if the file doesn't exist, it can work. also when i use the library, i should be able to either create the theme in code or load it from a file. but if load by file and it doesn't exist, it doesn't default, it doesn't start. same logic as in PGS."*

So **three ways in, and the distinction is who asked**:

- **Nothing said** → the built-in pigeon palette. Always works.
- **Built in code** → `gui_theme_pigeon(&t)` then assignment.
- **`gui_theme_load(&t, "/etc/gui.conf")`** → and if that file is missing or
  bad it **fails**; it does not quietly fall back. Asking for a specific theme
  and silently getting a different one is the bug this avoids.

`/etc/gui.conf` therefore ships in v1 after all, which reverses the
recommendation. **[Q17](#17-how-hard-is-a-failed-gui_theme_load) asks how hard
"it doesn't start" should be.**

## 5. Does the library draw a mouse cursor?


Nothing on the machine draws one today; you see your host pointer over the
canvas *(checked)*. Phase 9 made a soft-edged sprite cheap, so it is now
easy to draw — the work is *un*drawing it before the frame under it is
repainted ([events.md §5](events.md)).

**Recommendation: not in v1.** The host pointer is adequate until
letterboxing exists; once `GUI_SCALE_ASPECT` is real, the host pointer stops
agreeing with the guest's idea of where things are, and that is the moment
to add one.


---

**Decided (you):** *"no it doesnt, maybe in the future"* — the recommendation. Noted for when `GUI_SCALE_ASPECT` letterboxing makes the host pointer disagree with the guest.

## 6. Retained mode at all?

**Re-asked, with the explanation you wanted. This is the one question still
open.**

### The two ways of building a UI, in plain terms

**Immediate mode — what every program on this machine does today.** There are
no widget objects. Every frame you call drawing functions that describe what
the screen should look like *right now*. All the state lives in your own
variables, and you do the hit-testing yourself.

```c
/* explorer.c, roughly. This is immediate mode. */
if (dirty) {
    disp_clear(BG);
    draw_header();
    draw_list();          /* draws rows from YOUR array, every time */
    draw_status();
    dirty = 0;
}
...
/* and to know if something was clicked, you work it out: */
row = (mouse_y() - LIST_Y) / ROW;
if (row >= 0 && row < ROWS) select(row);
```

**Retained mode — tkinter, and what you asked for.** You create objects once;
the library remembers them. You attach a handler. The library works out what
to draw, when to draw it, and which object the pointer is over.

```c
/* The same list, retained. */
list = gui_list(10, 40, 300, 200, on_pick);
gui_list_add(list, "notes.txt");
gui_list_add(list, "pigeon.bmp");
...
void on_pick(int id, gui_event *e) {
    if (e->type == GUI_CHANGE) open_file(gui_value(id));
}
```

You never write the redraw, and you never compute which row was clicked.

### The trade

| | immediate | retained |
|---|---|---|
| where state lives | your variables | the library's objects (plus yours) |
| who redraws | you, all of it, every frame | the library, only what changed |
| who hit-tests | you, by hand | the library |
| code for a simple screen | more | much less |
| control over exactly what is drawn | total | you ask, it draws |
| fits this codebase | it *is* this codebase | a new idea here |
| the cost | every program re-implements list, entry, menu | a pool, ids, and a frame loop to understand |

**The concrete argument from your own code:** the list widget is written
three times (`explorer.c`, `disc.c`, `files.c`), the text entry twice, the
palette five times, the row composer three times byte-for-byte *(checked)*.
That duplication is exactly what having objects to hang state on removes.

**The concrete argument against:** immediate mode is simple and total. There
is no pool, no ids, no "why did it not repaint". `graph.c` draws a curve
however it likes; a retained library is something you ask.

### What I would do

**Recommendation: retained — and port nothing.** New programs use the
library; `explorer.c`, `disc.c`, `graph.c` and the rest keep working
untouched. The canvas widget ([canvas.md](canvas.md)) is the bridge: inside
it you are back in immediate mode and can draw exactly what you want, so you
get retained chrome around immediate content — which is what a plot or a cube
actually wants.

Answer: yeah retained mode. like tkinter

**Decided (you):** *"yeah retained mode. like tkinter"* — retained it is, and
nothing existing is ported. `explorer.c`, `disc.c`, `graph.c` and the rest keep
working untouched; the canvas ([canvas.md](canvas.md)) is where you are back in
immediate mode and can draw exactly what you like.

---

## 7. What is the first program built with it?


Phase 9 taught this lesson the expensive way: the first user was chosen in
the plan, and the choice was wrong because the program picked did not
actually need the feature
*(checked: [phase9_srcalpha.md §11](../gac/plans/phase9_srcalpha.md#11-as-built))*.
So the candidates, honestly:

- **A new `/bin/panel.bin`** — a settings panel: mode picker, theme picker,
  font picker. Uses labels, buttons, a list and a slider, needs nothing
  ported, and is genuinely useful.
- **Porting `explorer.c`** — the real test, and the real risk: it cannot use
  a back buffer, because it must leave the display where the kernel left it
  for an `exec`'d child *(checked: `explorer.c:341-346`)*.
- **A demo** — proves nothing, but costs nothing.

**Recommendation: `panel.bin`.** It exercises five widget kinds, it has an
obvious reason to exist, and being new it cannot break anything that works.


---

**Decided (you):** *"lets do the panel, and also think about porting the shell to use the GUI library or just the fonts, that way i could zoom the fontsize and use different fonts […] but dont do it now, but document it, so i dont forget about it."*

`panel.bin` it is. **The shell idea is written up in
[fonts/shell.md](../fonts/shell.md)** so it is not lost — and it turns out to be
mostly a *font* job rather than a GUI one, which makes it far smaller than it
sounds.

## 8. Is this the right thing to build next at all?


The honest alternative is that **[the font plan](../fonts/README.md) F1 alone** — one 8 × 16
font, no device change, no library — makes 720p readable, which is the
problem you actually described. The GUI library is a much larger piece of
work that this would sit underneath.

**Recommendation: do F1 first, on its own, and see.** It is small, it is
independent, and it fixes the complaint. Then decide whether the GUI library
is what you want on top of it, with the readability problem already gone and
no pressure from it.



---

# Things I assumed without asking

Everything from here down is a gap in the original description that I closed
with a guess so the design could be written. Each one is a real fork.

---

**Decided (you):** *"yeah, implement the font system first."* — the recommendation, and [fonts/sizes.md](../fonts/sizes.md) strengthens it: the font work grew, so it front-loads even more of the value.

## 9. Which widgets do you actually want?


You said "labels, buttons (with a handler function pointer) and other useful
stuff". I filled "other useful stuff" in as: **checkbox, radio, text entry,
slider, progress bar, list, panel, image, rule, and canvas**
([api.md §3](api.md)).

Things I did **not** include, any of which might be what you meant: a menu
bar, a dropdown/combo box, tabs, a tooltip, a standalone scrollbar, a tree
view, a modal dialog helper, a multi-line text area, a table/grid.

**Recommendation: the eleven in api.md, minus radio** — radio is a checkbox
with a group, and can wait. But this is a guess about your app, not a
technical judgement, so it is worth a minute.


---

**Decided (you):** *"add the radio also, it shouldnt be that big."* — so all twelve, radio included. It is a checkbox with a group id and one rule (only one in a group is set), which is a few lines on top of checkbox.

## 10. Do widgets need to nest at all?


I gave them a parent/child tree (`gui_attach`, `gui_panel`) so a panel can
hold a group and hide or move them together. **You never asked for nesting.**
A flat list would be simpler, smaller and quite possibly enough.

**Recommendation: keep the tree, but shallow** — it costs three `int`s per
widget and is the difference between "hide this panel" and "hide these nine
things one at a time". If you only ever want one flat screen of controls, say
so and it goes.


---

**Decided (you):** *"yeah the nesting is good!"* — the tree stays.

## 11. One handler per widget, or one per event kind?


I gave each widget **one** handler that receives every event and switches on
`e->type`:

```c
void on_run(int id, gui_event *e) {
    if (e->type == GUI_CLICK) ...
    else if (e->type == GUI_ENTER) ...
}
```

Your description said `button(x, y, handler_ptr)` — one pointer — which is
what I built. But it also said "on hover i can call either the hover handler
of the gui library or change the color myself", which could mean you pictured
**separate** handlers: `on_click`, `on_hover`, `on_leave`.

**Recommendation: one handler.** Separate ones mean a widget carries five or
six function pointers, most of them null, and every `gui_button()` call grows
arguments. `gui_on(id, GUI_ENTER, fn)` can be added on top if a specific
widget wants one callback per event.


---

**Decided (you):** *"one handler function and it decides what each event does"* — the recommendation, and what [api.md](api.md) is written against.

## 12. Keyboard navigation?


Not mentioned at all. I designed focus (`gui_focus`, `GUI_FOCUS`, `GUI_BLUR`)
because a text entry needs it, but **no Tab-between-widgets and no
Enter-activates-the-focused-button**.

**Recommendation: add Tab/Shift-Tab and Enter/Space in G2.** It is a small
amount of code once focus exists, and a GUI that cannot be driven from the
keyboard is frustrating on a machine whose mouse is polled at 30 Hz
*(checked: `display.py:107`)*.


---

**Decided (you):** *"yeah, i want that."* — Tab / Shift-Tab to move focus, Enter and Space to activate, in G2.

## 13. Does a GUI app own the whole screen?


I assumed **yes** — one app, the whole screen, as every program does now.
That means a GUI program cannot also `printf` to the console, because the
console and the GUI would fight over the same pixels.

The alternative is reserving a strip for console output, the way `explorer.c`
keeps the display where the kernel left it so an `exec`'d child can print
visibly *(checked: `explorer.c:341-346`)*.

**Recommendation: the whole screen**, with `gui_end()` restoring things so the
shell's console comes back cleanly. A GUI app that needs to show output shows
it in a widget.


---

**Decided (you):** *"lets have a console widget. that way i can put a console anywhere. and yeah putting multiple would show the same thing."*

**This is a new widget rather than an answer to the question as asked** — and
a good one: it means a GUI app never has to choose between having a window
and being able to print. It is designed in
[api.md §9](api.md#9-the-console-widget), and *"multiple show the same thing"*
settles its main design point: the text lives in **one buffer per program**
and a console widget is a *view* onto it. **[Q18](#18-what-feeds-the-console-widget)
to [Q20](#20-does-the-console-widget-take-input) are what it raises.**

## 14. Is scaling on or off by default, and which mode?


I made `gui_design()` and `gui_scale()` explicit calls, so **scaling is off
until you ask for it** and a program that never calls them works in raw
device pixels.

**Recommendation: off by default, `GUI_SCALE_ASPECT` when turned on.** Off by
default because a program that has not thought about scaling should not have
its coordinates silently changed; `ASPECT` when on because `STRETCH` distorts
and `INTEGER` can waste half the screen.


---

**Decided (you):** *"i dont care. we can do off by default"* — the recommendation: off until `gui_design()` and `gui_scale()` are called, `GUI_SCALE_ASPECT` when turned on.

## 15. Do you want to author your own fonts?


I assumed yes, and proposed a `.pf` format plus `tools/make_font.py`
([fonts.md §4.3](../fonts/README.md)). That is a real chunk of work — a format, a
generator, and a way to edit glyphs — and it is only worth it if you want
more than the one good font that fixes the readability problem.

**Recommendation: ship one 8 × 16 font and the loader; write the generator
only when you want a second font.** The format is needed either way; the
authoring tooling is not.


---

**Decided (you):** *"yeah, please make a font crator tool please. and also, could i load a real font from my pc and it would translate it into .pf?"*

**Yes to both, and the second is easier than the first.** `pygame.freetype`
is already installed as part of the pygame client's dependencies and
rasterises TrueType and OpenType with full antialiasing — verified here
against `DejaVuSansMono.ttf`, one of **26** faces already on this machine
*(measured)*. So `tools/make_font.py --ttf … --size 20` is a real thing with
no new dependency
([fonts/format.md §2](../fonts/format.md)).
A `--draw` mode for hand-authoring glyphs as text comes with it.

## 16. Does the library run at a frame rate, or only when something happens?


I designed `gui_poll()` to do exactly one frame's work and return, with no
sleeping and no frame pacing — so `gui_run()` spins as fast as the machine
allows, which is what every existing UI does *(checked: `explorer.c:1057`,
`graph.c:1451`, all busy polls)*.

That is fine for a UI that only redraws on damage, and wrong for a canvas
that animates: `cube.c` spinning would run as fast as the CPU goes rather
than at a sensible rate.

**Recommendation: add `gui_fps(n)` in G6, alongside the canvas** — a frame
cap using the timer device, defaulting to off. It only matters once something
animates.

**Decided (you):** *"yeah thats good! and yeah, default to off."* — `gui_fps(n)` in G6, off by default.


---

# Third pass: what your answers raised

From Q4, Q13 and Q15. Same rule: blank means the recommendation.

---

## 17. How hard is a failed `gui_theme_load`?

You said a theme loaded from a file that is missing *"doesn't default, it
doesn't start"*. Two ways to mean that:

- **(a)** `gui_theme_load()` returns 0, and **your program** decides — prints
  a message and exits, or carries on. The library never exits for you.
- **(b)** the library prints and exits the program itself.

**Recommendation: (a).** A library that calls `exit()` is impossible to use
from anything that wants to recover, and "it doesn't start" is then one `if`
in your `main` — which is also exactly how `bmp_load` and `fs_*` already
behave on this machine *(checked)*.

Answer:obviosly A

**Decided (you):** *"obviosly A"* — `gui_theme_load()` returns 0 and your program
decides. The library never calls `exit()`, which matches how `bmp_load` and the
`fs_*` calls already behave *(checked)*. "It doesn't start" is then one `if` in
your `main`.

---

## 18. What feeds the console widget?

Two ways text gets into it:

- **(a) Automatic** — the widget captures the program's own `printf`, so
  existing code that prints just works and its output appears in the widget.
  Needs `stdio` to be redirectable, which today it is not: `printf` goes
  straight to `write(STDOUT)` *(checked: `lib/pigeon/stdio.c`)*.
- **(b) Explicit** — `gui_console_print(id, s)`, and `printf` still goes to
  the real console underneath.
- **(c) Both** — explicit always, plus `gui_console_capture()` to opt in to
  redirecting `printf`.

**Recommendation: (c).** Explicit is trivial and always right; capture is the
thing that makes it pleasant, and being opt-in means nothing changes for
programs that do not ask. Since *"multiple show the same thing"*, capture is
per **program**, not per widget — there is one buffer and the widgets are
views onto it.

Answer:

**Decided (you):** blank, so the recommendation — (c): `gui_console_print()` always, plus an opt-in `gui_console_capture()` that redirects this program's `printf`. Capture is per **program**, since there is one buffer.

---

## 19. How much scrollback, and whose?

The kernel console keeps 100 rows *(checked: `kernel.c:96`)*. A console widget
needs its own buffer, since it is showing your program's output, not the
shell's.

**Recommendation: a fixed ring you choose at creation,
`gui_console(x, y, w, h, rows)`, defaulting to 200 lines**, each line stored
as text rather than pixels so it re-wraps when the widget or the font changes.
Wheel and Page Up scroll it; it follows the bottom unless you have scrolled
away, which is what every terminal does.

Answer:

**Decided (you):** blank, so the recommendation — a ring you size at creation, defaulting to 200 lines, stored as text so it re-wraps when the widget, the screen or the font changes.

---

## 20. Does the console widget take input?

Output-only, or a real terminal you can type into (which is what porting the
shell would eventually need — [fonts/shell.md](../fonts/shell.md))?

**Recommendation: output-only in v1**, with `GUI_CHAR` delivered to it when
focused so an app *can* implement a prompt if it wants. A real line editor —
history, tab completion, the kernel's `read()` semantics — is a much bigger
thing and belongs with the shell work, not with the widget.

Answer: naah, lets do input and output.

**Decided (you):** *"naah, lets do input and output."* — **this reverses the
recommendation**: the console widget takes input as well. So it is a small
terminal, not a log pane, and it needs a caret, echo, and at least some line
editing. [Q23](#23-how-much-line-editing-does-the-console-widget-do) asks how
much, because that is the whole size of the job.

---

## 21. Does the console widget get its own font?

A GUI's chrome might want 12 × 24 while its console output wants something
denser, so more text fits.

**Recommendation: yes — `gui_console_font(id, slot)`**, defaulting to the
UI font. It costs nothing once font slots exist (F2), and "the console is
small, the buttons are big" is the normal arrangement.

Answer: yeah

**Decided (you):** *"yeah"* — `gui_console_font(id, slot)`, defaulting to the UI font. Costs nothing once slots exist (F2), and "small console, big buttons" is the normal arrangement.

---

## 22. Which real font should be rasterised as the default?

`tools/make_font.py` can take any TTF (Q15). The machine already has 26,
including DejaVu Sans Mono.

**Recommendation: DejaVu Sans Mono at size 20 → a 12 × 24 cell**, because it
is already here, it is monospaced, it is licensed for redistribution, and
§8's table says 12 × 24 gives 98 × 27 at 720p — a normal terminal. If you have
a face you would rather look at, name it and that becomes the default
instead.

Answer: 

**Decided (you):** blank, so the recommendation — **DejaVu Sans Mono at size 20**, giving a 12 × 24 cell and 98 × 27 at 720p. It is already on the machine, monospaced, and redistributable.


---

## 23. How much line editing does the console widget do?

Raised by your answer to [Q20](#20-does-the-console-widget-take-input): the
console takes input, so it needs a caret and some editing. How much is the
whole size of the job.

- **(a) Characters, Backspace, Enter.** A `GUI_LINE` event carries the
  finished line. Perhaps fifty lines of code.
- **(b) Plus history on Up/Down, and left/right within the line.** Another
  fifty, and it is what makes a REPL pleasant.
- **(c) The kernel's full line editor** — history, word motion, Tab
  completion, paging *(checked: `kernel.c:1210-1245`)*. Several hundred lines,
  and Tab completion cannot live in the widget anyway, because only the app
  knows what is being completed.

**Recommendation: (b).** It is the point where a console stops being annoying,
and it does not drag the widget into knowing what your program's commands are.
The app gets `GUI_LINE` with the text and decides everything else. If you later
want the shell itself in a widget, that is
[the shell plan](../fonts/shell.md) and it is a different conversation.

Answer:
