/* edit -- a small nano (docs/phase4b_plan.md step 7).
 *
 *     edit FILE
 *
 * The screen is nano's, cut to 32x12: the title bar on row 0 and the
 * shortcuts on row 11, both inverse; the text on rows 1 to 9, which are set
 * to scroll on their own, so moving down a line draws one row instead of
 * nine; messages and questions on row 10. It draws through the console with
 * escape codes, gathered and written once a key, and reads key presses and
 * the mouse itself with input.h, after turning Ctrl+C as the break off, so
 * ^C is nano's ^C.
 *
 *     ^O ^S  save              ^X  exit, asking first if it changed
 *     ^K     cut a line        ^U  paste what was cut, above the line
 *     ^W     find              ^C  where the cursor is
 *     ^Y ^V  a screen up, down, as PgUp and PgDn do
 *     ^G     the keys          Tab spaces to the next multiple of 4
 *     the wheel scrolls the text, and a click puts the cursor there
 *
 * The text is one gap buffer: the file, with a gap where the cursor is, in
 * one block grown by copying, since mem.c never joins freed blocks. Saving
 * writes FILE~, then removes FILE and renames FILE~ to it, so a full disk
 * leaves the old file whole.
 */
#include <pigeon/input.h>
#include <pigeon/mem.h>
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define ED_COLS     32u
#define ED_ROWS     9u              /* of text, on the screen's rows 1 to 9 */
#define ED_MSG_ROW  10u
#define ED_KEYS_ROW 11u
#define ED_CELL_W   6u
#define ED_CELL_H   9u
#define ED_MAX_TEXT 65536u          /* 64 KB */
#define ED_MIN_ROOM 16384u
#define ED_PATH     264
#define ED_OUT_MAX  1024u
#define ED_FIND_MAX 20u
#define ED_TAB      4u
#define ED_SHIFT    24u             /* how far a long line moves sideways at a time */
#define ED_WHEEL    3u              /* lines a wheel notch moves, and the most a scroll does */
#define ED_NONE     0xFFFFFFFFu
#define ED_LCTRL    0x8Bu
#define ED_RCTRL    0x8Cu
#define ED_DISK_FULL (-6)           /* FS_ENOSPC, for a write that stopped short */

static char *buf;                   /* the text before the gap, the gap, the text after */
static unsigned cap;
static unsigned gap;                /* where the gap starts: the cursor */
static unsigned gap_end;
static char path[ED_PATH];
static unsigned modified;
static unsigned shown_modified;     /* what the title bar says */

static unsigned top;                /* where the line on text row 0 starts */
static unsigned top_line;           /* and its number, from 0 */
static unsigned want_col;           /* the column Up and Down aim for */
static unsigned shown_line;         /* the cursor's line as last drawn */
static unsigned shown_shift;        /* and how far it was moved sideways */

static unsigned redraw_all;         /* what this key needs drawn again */
static unsigned dirty_line;         /* from this line to the bottom, or ED_NONE */
static unsigned part_line;          /* this line from part_col, or ED_NONE */
static unsigned part_col;

static char *cut;                   /* the lines ^K cut */
static unsigned cut_len;
static unsigned cut_cap;
static unsigned cutting;            /* this key was ^K */
static unsigned last_cut;           /* and so was the key before */

static unsigned message_up;
static char out[1024];
static unsigned out_len;
static unsigned ctrl;

/* --- output, gathered and written once a key --------------------------------- */

static void out_flush(void) {
    if (out_len > 0u) write(STDOUT, out, out_len);
    out_len = 0u;
}

static void out_char(int c) {
    if (out_len == ED_OUT_MAX) out_flush();
    out[out_len] = (char)c;
    out_len++;
}

static void out_text(char *s) {
    while (*s != 0) {
        out_char((int)*s);
        s++;
    }
}

static void out_number(unsigned n) {
    char digits[12];
    utoa(n, digits, 10u);
    out_text(digits);
}

/* The console's cursor to a cell, counting from 0. */
static void out_go(unsigned row, unsigned col) {
    out_text("\x1b[");
    out_number(row + 1u);
    out_char(';');
    out_number(col + 1u);
    out_char('H');
}

/* A message, centred on its row, until the next key. */
static void say(char *text) {
    unsigned n = strlen(text);
    out_go(ED_MSG_ROW, 0u);
    out_text("\x1b[K");
    out_go(ED_MSG_ROW, n < ED_COLS ? (ED_COLS - n) / 2u : 0u);
    out_text(text);
    message_up = 1u;
}

/* --- the text ------------------------------------------------------------------ */

static unsigned text_length(void) {
    return cap - (gap_end - gap);
}

static int byte_at(unsigned i) {
    if (i < gap) return (int)(unsigned char)buf[i];
    return (int)(unsigned char)buf[i + (gap_end - gap)];
}

static unsigned line_start(unsigned pos) {
    while (pos > 0u && byte_at(pos - 1u) != '\n') pos--;
    return pos;
}

static unsigned line_end(unsigned pos) {
    unsigned n = text_length();
    while (pos < n && byte_at(pos) != '\n') pos++;
    return pos;
}

/* Line breaks from `from` up to `to`. */
static unsigned breaks_between(unsigned from, unsigned to) {
    unsigned n = 0u;
    while (from < to) {
        if (byte_at(from) == '\n') n++;
        from++;
    }
    return n;
}

/* Lines as a person counts them: a last line with no break counts too. */
static unsigned line_total(void) {
    unsigned n = text_length();
    unsigned lines = breaks_between(0u, n);
    if (n > 0u && byte_at(n - 1u) != '\n') lines++;
    return lines;
}

/* The number of the line that starts at, or holds, pos. */
static unsigned line_of(unsigned pos) {
    if (pos >= top) return top_line + breaks_between(top, pos);
    return top_line - breaks_between(pos, top);
}

static void gap_to(unsigned pos) {
    unsigned n;
    if (pos < gap) {
        n = gap - pos;
        memmove((void *)(buf + gap_end - n), (void *)(buf + pos), n);
        gap = pos;
        gap_end = gap_end - n;
    } else if (pos > gap) {
        n = pos - gap;
        memmove((void *)(buf + gap), (void *)(buf + gap_end), n);
        gap = gap + n;
        gap_end = gap_end + n;
    }
}

/* Room for n more bytes in the gap: the block doubled, by copying, as
 * often as that takes. 0 past 64 KB, or when the heap is out. */
static unsigned make_room(unsigned n) {
    unsigned bigger;
    unsigned after;
    char *grown;
    if (text_length() + n > ED_MAX_TEXT) {
        say("[ 64 KB is all edit holds ]");
        return 0u;
    }
    if (gap_end - gap >= n) return 1u;
    bigger = cap * 2u;
    while (bigger - text_length() < n) bigger = bigger * 2u;
    grown = (char *)malloc(bigger);
    if (grown == NULL) {
        say("[ Out of memory ]");
        return 0u;
    }
    after = cap - gap_end;
    memcpy((void *)grown, (void *)buf, gap);
    memcpy((void *)(grown + bigger - after), (void *)(buf + gap_end), after);
    free((void *)buf);
    buf = grown;
    gap_end = bigger - after;
    cap = bigger;
    return 1u;
}

static unsigned insert_text(char *text, unsigned n) {
    if (!make_room(n)) return 0u;
    memcpy((void *)(buf + gap), (void *)text, n);
    gap = gap + n;
    modified = 1u;
    return 1u;
}

/* --- drawing ------------------------------------------------------------------- */

/* How far a line is drawn moved left with the cursor at column col. */
static unsigned shift_for(unsigned col) {
    if (col < ED_COLS - 1u) return 0u;
    return ((col - (ED_COLS - 1u)) / ED_SHIFT + 1u) * ED_SHIFT;
}

static unsigned cursor_shift(void) {
    return shift_for(gap - line_start(gap));
}

/* The line starting at `start` on text row r, from screen column `from`,
 * moved left by shift; a moved line shows '$' first. */
static void draw_line(unsigned r, unsigned start, unsigned from, unsigned shift) {
    unsigned end = line_end(start);
    unsigned col = from;
    unsigned i = start + shift + from;
    int c;
    out_go(r + 1u, from);
    if (shift > 0u && from == 0u) {
        out_char('$');
        col = 1u;
        i++;
    }
    while (col < ED_COLS && i < end) {
        c = byte_at(i);
        out_char(c >= 32 && c <= 126 ? c : '?');
        col++;
        i++;
    }
    if (col < ED_COLS) out_text("\x1b[K");
}

/* The start of the line on text row r, or ED_NONE below the last line. */
static unsigned row_start(unsigned r) {
    unsigned start = top;
    unsigned end;
    while (r > 0u) {
        end = line_end(start);
        if (end >= text_length()) return ED_NONE;
        start = end + 1u;
        r--;
    }
    return start;
}

/* Text rows from `from` up to `to`, walking down from the top line. */
static void draw_rows(unsigned from, unsigned to) {
    unsigned r;
    unsigned start = top;
    unsigned there = 1u;
    unsigned end;
    unsigned cur = line_start(gap);
    for (r = 0u; r < to; r++) {
        if (r >= from) {
            if (there != 0u) {
                draw_line(r, start, 0u, start == cur ? cursor_shift() : 0u);
            } else {
                out_go(r + 1u, 0u);
                out_text("\x1b[K");
            }
        }
        if (there != 0u) {
            end = line_end(start);
            if (end < text_length()) start = end + 1u;
            else there = 0u;
        }
    }
}

/* Line number n drawn again, if it is on the screen. */
static void draw_line_number(unsigned n) {
    unsigned start;
    if (n < top_line || n >= top_line + ED_ROWS) return;
    start = row_start(n - top_line);
    if (start == ED_NONE) return;
    draw_line(n - top_line, start, 0u, start == line_start(gap) ? cursor_shift() : 0u);
}

/* The line a typed key changed, from where it changed. */
static void draw_part(void) {
    unsigned start;
    unsigned shift;
    unsigned from;
    if (part_line < top_line || part_line >= top_line + ED_ROWS) return;
    start = row_start(part_line - top_line);
    if (start == ED_NONE) return;
    shift = start == line_start(gap) ? cursor_shift() : 0u;
    from = part_col > shift ? part_col - shift : 0u;
    if (from >= ED_COLS) return;
    draw_line(part_line - top_line, start, from, shift);
}

static void draw_title(void) {
    char *shown = path;
    unsigned n = strlen(path);
    unsigned col = 6u;
    out_go(0u, 0u);
    out_text("\x1b[7m edit ");
    if (n > 17u) {
        out_char('<');
        shown = path + n - 16u;
        col++;
    }
    while (*shown != 0) {
        out_char((int)*shown);
        shown++;
        col++;
    }
    while (col < ED_COLS - 9u) {
        out_char(' ');
        col++;
    }
    out_text(modified != 0u ? "Modified " : "         ");
    out_text("\x1b[0m");
    shown_modified = modified;
}

static void draw_keys(void) {
    out_go(ED_KEYS_ROW, 0u);
    out_text("\x1b[7m^O\x1b[0m Save \x1b[7m^X\x1b[0m Exit \x1b[7m^K\x1b[0m Cut \x1b[7m^W\x1b[0m Find");
}

static void draw_screen(void) {
    out_text("\x1b[0m\x1b[2J\x1b[2;10r");
    draw_title();
    draw_rows(0u, ED_ROWS);
    draw_keys();
    shown_line = line_of(line_start(gap));
    shown_shift = cursor_shift();
}

/* The cursor: the character under it, in inverse or not. */
static void draw_cursor(unsigned on) {
    unsigned start = line_start(gap);
    int c = ' ';
    if (gap < text_length() && byte_at(gap) != '\n') c = byte_at(gap);
    if (c < 32 || c > 126) c = '?';
    out_go(line_of(start) - top_line + 1u, gap - start - cursor_shift());
    if (on != 0u) out_text("\x1b[7m");
    out_char(c);
    if (on != 0u) out_text("\x1b[0m");
}

/* --- the view ------------------------------------------------------------------ */

/* The text moved up n lines on the screen, as far as there are lines. */
static void view_down(unsigned n) {
    unsigned moved = 0u;
    unsigned end;
    while (moved < n) {
        end = line_end(top);
        if (end >= text_length()) break;
        top = end + 1u;
        top_line++;
        moved++;
    }
    if (moved == 0u) return;
    out_text("\x1b[");
    out_number(moved);
    out_char('S');
    draw_rows(ED_ROWS - moved, ED_ROWS);
}

static void view_up(unsigned n) {
    unsigned moved = 0u;
    while (moved < n && top > 0u) {
        top = line_start(top - 1u);
        top_line--;
        moved++;
    }
    if (moved == 0u) return;
    out_text("\x1b[");
    out_number(moved);
    out_char('T');
    draw_rows(0u, moved);
}

/* The view moved so the cursor's line is on the screen: a few lines by
 * scrolling the text rows, further by drawing them all. */
static void follow_cursor(void) {
    unsigned start = line_start(gap);
    unsigned n;
    if (start < top) {
        n = breaks_between(start, top);
        if (n <= ED_WHEEL) {
            view_up(n);
        } else {
            top = start;
            top_line = top_line - n;
            redraw_all = 1u;
        }
        return;
    }
    n = breaks_between(top, start);
    if (n < ED_ROWS) return;
    n = n - (ED_ROWS - 1u);
    if (n <= ED_WHEEL) {
        view_down(n);
        return;
    }
    while (n > 0u) {
        top = line_end(top) + 1u;
        top_line++;
        n--;
    }
    redraw_all = 1u;
}

/* --- keys ---------------------------------------------------------------------- */

/* A key event as a key pressed, with Ctrl tracked and Ctrl's letters in
 * lower case: 0 for anything else. */
static unsigned press_of(unsigned event) {
    unsigned code;
    if (event == 0u) return 0u;
    code = KE_CODE(event);
    if (code == ED_LCTRL || code == ED_RCTRL) {
        ctrl = KE_PRESSED(event) != 0u;
        return 0u;
    }
    if (KE_PRESSED(event) == 0u) return 0u;
    if (ctrl != 0u && code >= 'A' && code <= 'Z') code = code + 32u;
    return code;
}

static unsigned wait_key(void) {
    unsigned code;
    while (1) {
        code = press_of(key_event());
        if (code != 0u) return code;
    }
    return 0u;
}

/* The cursor's line needs drawing from the cursor on. */
static void mark_part(void) {
    part_line = line_of(line_start(gap));
    part_col = gap - line_start(gap);
}

static void move_up(void) {
    unsigned start = line_start(gap);
    unsigned prev;
    unsigned len;
    if (start == 0u) return;
    prev = line_start(start - 1u);
    len = start - 1u - prev;
    gap_to(prev + (want_col < len ? want_col : len));
}

static void move_down(void) {
    unsigned end = line_end(gap);
    unsigned next;
    unsigned len;
    if (end >= text_length()) return;
    next = end + 1u;
    len = line_end(next) - next;
    gap_to(next + (want_col < len ? want_col : len));
}

/* A screen up or down: the cursor and the view move together. */
static void page(unsigned down) {
    unsigned i;
    unsigned start;
    for (i = 0u; i < ED_ROWS; i++) {
        start = line_start(gap);
        if (down != 0u) move_down();
        else move_up();
        if (line_start(gap) == start) break;
        if (down != 0u) {
            top = line_end(top) + 1u;
            top_line++;
        } else if (top > 0u) {
            top = line_start(top - 1u);
            top_line--;
        }
    }
    redraw_all = 1u;
}

static void backspace(void) {
    if (gap == 0u) return;
    if (gap == top) {                       /* joining the top line to the one above */
        top = line_start(top - 1u);
        top_line--;
        redraw_all = 1u;
    }
    if (byte_at(gap - 1u) == '\n') {
        gap--;
        dirty_line = line_of(line_start(gap));
    } else {
        gap--;
        mark_part();
    }
    modified = 1u;
    want_col = gap - line_start(gap);
}

static void delete_here(void) {
    if (gap >= text_length()) return;
    if (byte_at(gap) == '\n') dirty_line = line_of(line_start(gap));
    else mark_part();
    gap_end++;
    modified = 1u;
}

/* ^K: the cursor's line, with its break, onto the end of what was cut --
 * a fresh cut unless the key before was ^K too. */
static void cut_line(void) {
    unsigned start = line_start(gap);
    unsigned end = line_end(gap);
    unsigned n;
    unsigned i;
    char *grown;
    if (end < text_length()) end++;
    n = end - start;
    if (n == 0u) return;
    if (last_cut == 0u) cut_len = 0u;
    if (cut_len + n + 1u > cut_cap) {
        grown = (char *)malloc((cut_len + n + 1u) * 2u);
        if (grown == NULL) {
            say("[ Out of memory ]");
            return;
        }
        if (cut != NULL) {
            memcpy((void *)grown, (void *)cut, cut_len);
            free((void *)cut);
        }
        cut = grown;
        cut_cap = (cut_len + n + 1u) * 2u;
    }
    for (i = 0u; i < n; i++) cut[cut_len + i] = (char)byte_at(start + i);
    cut_len = cut_len + n;
    if (cut[cut_len - 1u] != '\n') {
        cut[cut_len] = '\n';
        cut_len++;
    }
    dirty_line = line_of(start);
    gap_to(start);
    gap_end = gap_end + n;
    modified = 1u;
    cutting = 1u;
    want_col = 0u;
}

/* ^U: what was cut, above the cursor's line. */
static void paste_lines(void) {
    unsigned start;
    if (cut_len == 0u) return;
    start = line_start(gap);
    dirty_line = line_of(start);
    gap_to(start);
    insert_text(cut, cut_len);
    want_col = 0u;
}

static unsigned save_failed(int status) {
    char said[40];
    snprintf(said, 40u, "[ Not saved: %s ]", sys_strerror(status));
    say(said);
    return 0u;
}

/* ^O: FILE~ written, FILE removed, FILE~ renamed to FILE: 1 if saved. */
static unsigned save_file(void) {
    char temp[ED_PATH + 2];
    char said[40];
    unsigned after = cap - gap_end;
    unsigned lines;
    int fd;
    int first = 0;
    int second = 0;
    strlcpy(temp, path, ED_PATH + 2u);
    strlcat(temp, "~", ED_PATH + 2u);
    fd = open(temp, O_WRITE | O_CREATE | O_TRUNC);
    if (fd < 0) return save_failed(fd);
    if (gap > 0u) first = write(fd, buf, gap);
    if (first >= 0 && after > 0u) second = write(fd, buf + gap_end, after);
    close(fd);
    if (first != (int)gap || second != (int)after) {
        remove(temp);
        if (first < 0) return save_failed(first);
        if (second < 0) return save_failed(second);
        return save_failed(ED_DISK_FULL);
    }
    remove(path);                           /* not there yet, for a new file */
    fd = rename(temp, path);
    if (fd < 0) return save_failed(fd);
    modified = 0u;
    lines = line_total();
    snprintf(said, 40u, "[ Wrote %u line%s ]", lines, lines == 1u ? "" : "s");
    say(said);
    return 1u;
}

/* ^X: 1 to end edit, asking first when the text changed. */
static unsigned ask_leave(void) {
    unsigned code;
    if (modified == 0u) return 1u;
    say("Save modified buffer? Y N");
    out_flush();
    while (1) {
        code = wait_key();
        if (code == 'y' || code == 'Y') return save_file();
        if (code == 'n' || code == 'N') return 1u;
        if (ctrl != 0u && code == 'c') {
            say("[ Cancelled ]");
            return 0u;
        }
    }
    return 0u;
}

static unsigned matches(unsigned pos, char *text, unsigned n) {
    unsigned i;
    if (pos + n > text_length()) return 0u;
    for (i = 0u; i < n; i++) {
        if (byte_at(pos + i) != (int)(unsigned char)text[i]) return 0u;
    }
    return 1u;
}

/* ^W: text typed on the message row, then looked for from after the
 * cursor, round from the start again when the end comes first. */
static void find_text(void) {
    char text[ED_FIND_MAX + 1u];
    char said[40];
    unsigned n = 0u;
    unsigned code;
    unsigned len = text_length();
    unsigned pos;
    unsigned tries;
    unsigned wrapped = 0u;
    out_go(ED_MSG_ROW, 0u);
    out_text("\x1b[K\x1b[7mFind:\x1b[0m ");
    out_flush();
    while (1) {
        code = wait_key();
        if (ctrl != 0u && code == 'c') {
            say("[ Cancelled ]");
            return;
        }
        if (code == KEY_ENTER) break;
        if (code == KEY_BACKSPACE && n > 0u) {
            n--;
            out_go(ED_MSG_ROW, 6u + n);
            out_text("\x1b[K");
            out_flush();
        } else if (ctrl == 0u && code >= 32u && code <= 126u && n < ED_FIND_MAX) {
            text[n] = (char)code;
            n++;
            out_char((int)code);
            out_flush();
        }
    }
    text[n] = 0;
    out_go(ED_MSG_ROW, 0u);
    out_text("\x1b[K");
    if (n == 0u) return;
    pos = gap + 1u;
    for (tries = 0u; tries < len; tries++) {
        if (pos >= len) {
            pos = 0u;
            wrapped = 1u;
        }
        if (matches(pos, text, n)) break;
        pos++;
    }
    if (tries == len) {
        snprintf(said, 40u, "[ \"%.14s\" not found ]", text);
        say(said);
        return;
    }
    gap_to(pos);
    want_col = gap - line_start(gap);
    if (wrapped != 0u) say("[ Search Wrapped ]");
}

/* ^C: where the cursor is. */
static void show_where(void) {
    char said[40];
    snprintf(said, 40u, "[ line %u of %u, col %u ]", breaks_between(0u, gap) + 1u,
             breaks_between(0u, text_length()) + 1u, gap - line_start(gap) + 1u);
    say(said);
}

/* ^G: the keys, until any key. */
static void show_help(void) {
    out_text("\x1b[2J");
    out_go(0u, 0u);
    out_text("\x1b[7m edit: the keys                 \x1b[0m");
    out_go(2u, 0u);
    out_text(" ^O ^S  save     ^X  exit");
    out_go(3u, 0u);
    out_text(" ^K  cut a line  ^U  paste");
    out_go(4u, 0u);
    out_text(" ^W  find        ^C  where");
    out_go(5u, 0u);
    out_text(" ^Y PgUp ^V PgDn  a screen");
    out_go(6u, 0u);
    out_text(" arrows Home End  move");
    out_go(7u, 0u);
    out_text(" Tab  spaces to the next 4");
    out_go(8u, 0u);
    out_text(" the wheel scrolls the text");
    out_go(9u, 0u);
    out_text(" a click puts the cursor there");
    out_go(10u, 0u);
    out_text(" any key comes back");
    out_flush();
    wait_key();
    draw_screen();
}

/* One key: 1 when edit should end. */
static unsigned on_key(unsigned code) {
    char typed[4];
    unsigned n;
    if (ctrl != 0u && code >= 'a' && code <= 'z') {
        if (code == 'o' || code == 's') save_file();
        else if (code == 'x') return ask_leave();
        else if (code == 'k') cut_line();
        else if (code == 'u') paste_lines();
        else if (code == 'w') find_text();
        else if (code == 'c') show_where();
        else if (code == 'g') show_help();
        else if (code == 'y') page(0u);
        else if (code == 'v') page(1u);
        else if (code == 'a') code = KEY_HOME;
        else if (code == 'e') code = KEY_END;
        if (code != KEY_HOME && code != KEY_END) return 0u;
    }
    if (code == KEY_LEFT) {
        if (gap > 0u) gap_to(gap - 1u);
        want_col = gap - line_start(gap);
    } else if (code == KEY_RIGHT) {
        if (gap < text_length()) gap_to(gap + 1u);
        want_col = gap - line_start(gap);
    } else if (code == KEY_UP) {
        move_up();
    } else if (code == KEY_DOWN) {
        move_down();
    } else if (code == KEY_HOME) {
        gap_to(line_start(gap));
        want_col = 0u;
    } else if (code == KEY_END) {
        gap_to(line_end(gap));
        want_col = gap - line_start(gap);
    } else if (code == KEY_PGUP) {
        page(0u);
    } else if (code == KEY_PGDN) {
        page(1u);
    } else if (code == KEY_BACKSPACE) {
        backspace();
    } else if (code == KEY_DELETE) {
        delete_here();
    } else if (code == KEY_ENTER) {
        dirty_line = line_of(line_start(gap));
        if (insert_text("\n", 1u)) want_col = 0u;
    } else if (code == KEY_TAB) {
        n = ED_TAB - (gap - line_start(gap)) % ED_TAB;
        memset((void *)typed, ' ', 4u);
        mark_part();
        if (insert_text(typed, n)) want_col = gap - line_start(gap);
    } else if (code >= 32u && code <= 126u) {
        typed[0] = (char)code;
        mark_part();
        if (insert_text(typed, 1u)) want_col = gap - line_start(gap);
    }
    return 0u;
}

/* --- the mouse ----------------------------------------------------------------- */

/* A wheel notch: the text moves three lines, and the cursor with it when
 * it would leave the screen. */
static void scroll_wheel(unsigned down) {
    unsigned start;
    unsigned len;
    unsigned last;
    if (down != 0u) view_down(ED_WHEEL);
    else view_up(ED_WHEEL);
    start = line_start(gap);
    if (start < top) {
        len = line_end(top) - top;
        gap_to(top + (want_col < len ? want_col : len));
    } else if (breaks_between(top, start) >= ED_ROWS) {
        last = row_start(ED_ROWS - 1u);
        len = line_end(last) - last;
        gap_to(last + (want_col < len ? want_col : len));
    }
}

/* A press: a left click in the text puts the cursor on the character it
 * lands on, or at the end of that line, or on the last line below it. */
static void on_mouse(unsigned button) {
    unsigned x;
    unsigned y;
    unsigned start;
    unsigned len;
    unsigned col;
    if (button == ME_WHEEL_UP || button == ME_WHEEL_DOWN) {
        scroll_wheel(button == ME_WHEEL_DOWN);
        return;
    }
    if (button != 0u) return;
    x = mouse_x() / ED_CELL_W;
    y = mouse_y() / ED_CELL_H;
    if (y < 1u || y > ED_ROWS) return;
    start = row_start(y - 1u);
    if (start == ED_NONE) start = line_start(text_length());
    len = line_end(start) - start;
    col = x;
    if (start == line_start(gap)) col = x + cursor_shift();
    gap_to(start + (col < len ? col : len));
    want_col = gap - start;
}

/* --- a key from start to finish ------------------------------------------------ */

static void key_begin(void) {
    draw_cursor(0u);
    if (message_up != 0u) {
        out_go(ED_MSG_ROW, 0u);
        out_text("\x1b[K");
        message_up = 0u;
    }
    redraw_all = 0u;
    dirty_line = ED_NONE;
    part_line = ED_NONE;
    cutting = 0u;
}

static void key_finish(void) {
    unsigned line;
    unsigned shift;
    follow_cursor();
    if (redraw_all != 0u) {
        draw_rows(0u, ED_ROWS);
    } else {
        if (dirty_line != ED_NONE && dirty_line < top_line + ED_ROWS)
            draw_rows(dirty_line > top_line ? dirty_line - top_line : 0u, ED_ROWS);
        if (part_line != ED_NONE && (dirty_line == ED_NONE || part_line < dirty_line)) draw_part();
    }
    line = line_of(line_start(gap));
    shift = cursor_shift();
    if (shift != shown_shift || line != shown_line) {
        if (shown_shift != 0u && line != shown_line) draw_line_number(shown_line);
        if (shift != shown_shift || shift != 0u) draw_line_number(line);
        shown_shift = shift;
        shown_line = line;
    }
    if (modified != shown_modified) draw_title();
    draw_cursor(1u);
    out_flush();
    last_cut = cutting;
}

int main(int argc, char **argv) {
    sys_stat_t st;
    char said[40];
    unsigned size = 0u;
    unsigned got = 0u;
    unsigned fresh = 0u;
    unsigned i;
    unsigned code;
    unsigned mouse;
    int fd;
    int n;
    if (argc < 2) {
        print("usage: edit FILE\n");
        return 1;
    }
    strlcpy(path, argv[1], ED_PATH);
    if (stat(path, &st) >= 0) {
        if (st.type != S_FILE) {
            printf("edit: %s: is a directory\n", path);
            return 1;
        }
        if (st.size > ED_MAX_TEXT) {
            printf("edit: %s: over 64 KB\n", path);
            return 1;
        }
        size = st.size;
    } else {
        fresh = 1u;
    }
    cap = size * 2u;
    if (cap < ED_MIN_ROOM) cap = ED_MIN_ROOM;
    buf = (char *)malloc(cap);
    if (buf == NULL) {
        print("edit: out of memory\n");
        return 1;
    }
    gap = 0u;
    gap_end = cap - size;
    if (size > 0u) {
        fd = open(path, O_READ);
        if (fd < 0) {
            printf("edit: %s: %s\n", path, sys_strerror(fd));
            return 1;
        }
        while (got < size) {
            n = read(fd, buf + gap_end + got, size - got);
            if (n <= 0) break;
            got = got + (unsigned)n;
        }
        close(fd);
        if (got != size) {
            printf("edit: %s: could not be read\n", path);
            return 1;
        }
        for (i = 0u; i < size; i++) {
            if (buf[gap_end + i] == 0) {
                printf("edit: %s: holds a zero byte, so isn't text\n", path);
                return 1;
            }
        }
    }

    setbreak(0);
    dirty_line = ED_NONE;
    part_line = ED_NONE;
    draw_screen();
    if (fresh != 0u) {
        say("[ New File ]");
    } else {
        snprintf(said, 40u, "[ Read %u line%s ]", line_total(), line_total() == 1u ? "" : "s");
        say(said);
    }
    draw_cursor(1u);
    out_flush();
    while (1) {
        mouse = mouse_event();
        if (mouse != 0u && ME_PRESSED(mouse) != 0u) {
            key_begin();
            on_mouse(ME_BUTTON(mouse));
            key_finish();
        }
        code = press_of(key_event());
        if (code == 0u) continue;
        key_begin();
        if (on_key(code) != 0u) break;
        key_finish();
    }
    out_text("\x1b[r\x1b[0m\x1b[2J");
    out_flush();
    return 0;
}
