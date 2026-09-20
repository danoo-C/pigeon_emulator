# The colour table: the pigeon palette, and overriding it

> Part of [the GUI plan](README.md). **Status: design, 2026-09-20. Questions
> open ([questions.md](questions.md)).** Facts marked *(checked)* were read
> in the code on 2026-09-20.

**What you asked for:** a config where you either take the default pigeon
colour table or supply your own; `button(x, y, handler)` uses the table, and
`button(x, y, handler, colour)` overrides it.

---

## 1. The pigeon palette already exists

It is not something to invent. The same seven colours are copied into **five
files**, and `installer.c` names its copy *"user/files.c's palette"*
*(checked: `explorer.c:62-68`, `disc.c:58-64`, `files.c:75-83`,
`installer.c:52-58`, `demo.c`)*:

| role | value | what it is |
|---|---|---|
| `BG` | `0xFF0A0C10` | the page |
| `PANEL` | `0xFF181C24` | a raised surface |
| `BAR` | `0xFF232936` | title and status bars |
| `ACCENT` | `0xFF30C0FF` | selection, focus, the live thing |
| `INK` | `0xFFD8DEE9` | text |
| `DIM` | `0xFF6A7284` | secondary text, rules |
| `WARN` | `0xFFFF6B5E` | errors, destructive actions |
| `GOOD` | `0xFF6BCB77` | success *(only `disc.c` and `installer.c` carry it)* |

`graph.c` has a second, deliberately different set — `BG 0xFF0B0E14`,
`CURVE 0xFF3FD0FF`, `TRACE 0xFFFFC94D`, `ERRC 0xFFFF6B6B` *(checked:
`graph.c:789-797)`* — which is a good argument for themes being values rather
than constants: one program legitimately wants its own.

**So the default theme is the files.c palette, promoted from a comment in five
files to one named thing.**

---

## 2. Roles, not colours

A widget asks for a *role*; the theme says what colour that is. This is what
makes one override work everywhere.

```c
#define GUI_BG        0      /* the page                        */
#define GUI_PANEL     1
#define GUI_BAR       2
#define GUI_ACCENT    3
#define GUI_INK       4
#define GUI_DIM       5
#define GUI_WARN      6
#define GUI_GOOD      7
#define GUI_BORDER    8
#define GUI_HOVER     9      /* the pointer is over it          */
#define GUI_PRESS    10      /* it is held down                 */
#define GUI_FOCUS    11      /* it has the keyboard             */
#define GUI_DISABLED 12
#define GUI_ROLES    13
```

`#define`, not `enum` — there is no `enum` on this compiler
([constraints.md §2](constraints.md)).

---

## 3. The theme is filled at run time, and it has to be

```c
typedef struct { unsigned c[GUI_ROLES]; } gui_theme;

void gui_theme_pigeon(gui_theme *t);   /* the palette above          */
void gui_theme_graph(gui_theme *t);    /* graph.c's, as a second one */
void gui_use_theme(gui_theme *t);      /* the library keeps the pointer */
```

**This shape is forced, not chosen.** The obvious C —

```c
static gui_theme pigeon = { 0xFF0A0C10, 0xFF181C24, ... };   /* NO */
```

— fails twice over on this compiler: a brace initialiser on a struct is
rejected outright (`a brace initialiser needs an array`), and even a scalar
global initialised with anything but a bare non-negative literal is
**silently dropped to zero** ([constraints.md §1](constraints.md), verified
by running it). So the palette is assigned field by field inside
`gui_theme_pigeon()`, and any theme you write must be too. The header says
so, because the failure is silent.

A custom theme is then just:

```c
gui_theme mine;
gui_theme_pigeon(&mine);              /* start from the default      */
mine.c[GUI_ACCENT] = 0xFFFF9900;      /* change what you care about  */
gui_use_theme(&mine);
```

Note `gui_theme *`, never `gui_theme` — passing a struct by value copies four
bytes and says nothing ([constraints.md §1](constraints.md)).

---

## 4. Overriding one widget

```c
ok = gui_button(20, 20, 120, 32, "Run", on_run);   /* the theme's colours */
gui_set_color(ok, GUI_BG, 0xFF204080);             /* this one, overridden */
gui_set_color(ok, GUI_BG, 0);                      /* 0 = back to the theme */
```

Each widget carries `unsigned colour[GUI_ROLES]`, all zero meaning "ask the
theme". **Zero is a safe sentinel**: a colour of `0x00000000` is fully
transparent black, which nothing means to draw ([gac.md §3](../gac.md)).

### Why a setter rather than a second `gui_button` with a colour argument

You asked for `button(x, y, ptr, colour)` overriding `button(x, y, ptr)`.
There is no overloading and no default argument in C, so the honest options
are:

| | |
|---|---|
| **(a)** `gui_button(...)` then `gui_set_color(id, role, c)` | one function covers **every role × every widget**; the common case stays short |
| **(b)** `gui_button()` and `gui_button_c(..., colour)` | matches what you asked for, but which role does the one colour mean? and it needs a `_c` twin for every widget |
| **(c)** an options struct filled then passed | verbose, and a struct has to be passed by pointer here anyway |

**Recommendation: (a)**, because a button has at least four colours that
matter — background, ink, hover, press — and a single positional `colour`
argument can only be one of them, which makes (b) the start of an
`_c`/`_c2`/`_c3` family. [questions.md Q1](questions.md) asks, and a thin
`gui_button_c()` for "background, please" is easy to add on top of (a) if you
want the short form.

---

## 5. A theme from a file

`explorer.conf` is the template: `value = key` lines, `#` comments, first
match wins, unreadable lines reported on the status line and skipped, and
built-in defaults when the file is missing *(checked:
`explorer.c:508-590`)*. It carries file associations today and **no theme
keys** — so a `/etc/gui.conf` would follow the same parser and the same
manners:

```
# /etc/gui.conf -- the GUI's colours. Anything missing keeps the default.
0xFF0A0C10 = bg
0xFF30C0FF = accent
mono8x16   = font
```

[questions.md Q4](questions.md) asks whether this ships in v1 or waits — the
library works without it, and a config file is a parser plus error handling
that is only worth it once someone wants to re-theme without rebuilding.
