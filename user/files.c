/* user/files.c -- a file browser for PigeonFS.
 *
 *   up / down     move             enter   open a directory, view a file
 *   pgup / pgdn   page             bksp    up to the parent directory
 *   home / end    first / last     esc     quit
 *   n             new note         d       new directory
 *   x             delete           ?       the key list, on screen
 *
 * This is phase 5 of docs/filesystem.md: the program that shows
 * <pigeon/fs.h> doing the job it was built for. It walks the whole API --
 * mount, format, statvfs, opendir/readdir, chdir/getcwd, stat, mkdir,
 * remove, rmdir, open/puts/close, load and save -- and every failure it
 * can hit is reported through fs_strerror() on the status line instead
 * of being swallowed. A demo that only shows the happy path would hide
 * exactly the half of the library that took the longest to get right.
 *
 * The disk keeps what you leave on it. On a disk with no filesystem --
 * a fresh disks/hdd.img is 4 MiB of zeros -- the first run formats it
 * and writes a small tree so there is something to walk around in. After
 * that it never formats again: that is the lifetime rule the library
 * enforces with FS_ENOTBLANK, and this program does not carry a force
 * flag, so nothing you make here can be wiped by re-running it.
 *
 * Build and run:
 *   python3 start_emulator.py files --run
 *
 * To put real files on the disk, or to look at what this wrote:
 *   python3 tools/pfs.py put -r docs /docs
 *   python3 tools/pfs.py tree
 */
#include <pigeon/fs.h>
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/string.h>
#include <pigeon/mem.h>

/* --- one input stream ---------------------------------------------------
 *
 * Everything comes from key_read(). The HID device pushes every press
 * into the character FIFO, named keys included -- see push_key() in
 * emulator/devices/hid.py -- so the arrows arrive in the same queue as
 * the letters, and one queue is ordered with itself.
 *
 * user/demo.c could not do that: it wanted key EDGES for hold-to-repeat,
 * so it read arrows from one FIFO and characters from another, and its
 * header documents at length how draining them separately turned
 * DOWN-then-ENTER into ENTER-then-DOWN. Nothing here repeats on hold,
 * and a browser where you type a filename and press ENTER cannot afford
 * that reordering, so it takes the single stream instead.
 */

/* --- layout -------------------------------------------------------------
 *
 * The screen is 192x108 in a 6x8 cell, which is 32 columns by 12 rows --
 * everything below is derived from DISPLAY_W/H and the font, never
 * typed, for the reason display.h gives: the screen has changed shape
 * before and every baked-in number drew off the bottom when it did. */
#define CELL      (GLYPH_W + 1)                 /* 6 px: glyph plus a gap   */
#define COLS      (DISP_W / CELL)               /* 32                       */
#define ROW       (GLYPH_H + 1)                 /* 9 px per line            */

#define HEAD_Y    1
#define HEAD_RULE (HEAD_Y + GLYPH_H + 1)
#define LIST_Y    (HEAD_RULE + 3)
#define STATUS_Y  (DISP_H - GLYPH_H - 1)
#define FOOT_RULE (STATUS_Y - 2)
#define ROWS      ((FOOT_RULE - 1 - LIST_Y) / ROW)      /* 9 rows           */

/* A row is "> name.................. 1234 B". The size is right-aligned
 * in the last SIZE_COLS columns, so the name gets what is left. */
#define SIZE_COLS 9
#define NAME_COLS (COLS - SIZE_COLS - 1)

#define BG      0xFF0A0C10
#define PANEL   0xFF181C24
#define BAR     0xFF232936
#define ACCENT  0xFF30C0FF
#define INK     0xFFD8DEE9
#define DIM     0xFF6A7284
#define WARN    0xFFFF6B5E
#define GOOD    0xFF62D58A

/* --- modes --------------------------------------------------------------
 * There is no switch in this compiler (see docs/filesystem.md 6.7), so
 * dispatch is if-chains over these. */
#define M_LIST    0
#define M_VIEW    1
#define M_PROMPT  2
#define M_EDIT    3
#define M_CONFIRM 4
#define M_HELP    5

#define P_NOTE 1
#define P_DIR  2

#define MAX_ENTRIES 128     /* a listing bigger than this is shown truncated */
#define MAX_WRAP    1024    /* display lines a viewed file may wrap to       */
#define VIEW_MAX    (32u * 1024u)
#define NOTE_MAX    512

static unsigned  vol;                   /* the channel we mounted           */
static int       mode;
static int       running;
static int       dirty;

static fs_stat_t entries[MAX_ENTRIES];
static int       n_entries;
static int       truncated;
static int       sel;                   /* highlighted entry                */
static int       top;                   /* first entry drawn                */

static char      message[COLS + 1];
static color_t   msg_ink;

static char     *view_buf;              /* malloc'd while M_VIEW            */
static unsigned  view_size;
static char      view_name[FS_NAME_MAX + 1];
static int       view_top;

static unsigned  wrap_at[MAX_WRAP];     /* where each display line starts   */
static unsigned  wrap_len[MAX_WRAP];
static int       n_wrap;

static char      note_buf[NOTE_MAX + 1];
static int       note_len;
static char      note_name[FS_NAME_MAX + 1];

static char      prompt_label[12];
static char      prompt_buf[FS_NAME_MAX + 1];
static int       prompt_len;
static int       prompt_what;

static char      confirm_msg[COLS + 1];

static char *HELP[] = {
    "up/down    move",
    "pgup/pgdn  page",
    "home/end   first / last",
    "enter      open dir or file",
    "bksp       parent directory",
    "n          new note",
    "d          new directory",
    "x          delete",
    "esc        quit"
};
#define HELP_LINES 9

/* The tree the first run leaves behind. Written line by line through
 * fs_open/fs_puts rather than as one literal, because this compiler does
 * not join adjacent string literals -- and because fs_puts is part of
 * what the demo is here to show. */
static char *README_TXT[] = {
    "PigeonFS",
    "",
    "This disk is real: what you",
    "write here is still here the",
    "next time the machine boots.",
    "",
    "arrows  move",
    "enter   open",
    "bksp    parent dir",
    "n       new note",
    "d       new directory",
    "x       delete",
    "?       all the keys",
    "esc     quit",
    "",
    "From the host:",
    "  tools/pfs.py tree",
    "  tools/pfs.py put f /f"
};
#define README_LINES 18

static char *HELLO_TXT[] = {
    "The first note.",
    "",
    "Press n to write another one,",
    "then TAB to save it.",
    "",
    "Long lines wrap at the width of",
    "the screen, and the viewer",
    "scrolls with the arrow keys."
};
#define HELLO_LINES 8

static char *TODO_TXT[] = {
    "- phase 6: HDD DMA commands",
    "- measure the copy path again"
};
#define TODO_LINES 2

/* --- status line messages ----------------------------------------------- */

static void say(char *text, color_t ink) {
    strlcpy(message, text, sizeof(message));
    msg_ink = ink;
}

/* "what: the library's own words". Every error the user can provoke
 * reaches the screen this way, so a refusal is never silent. */
static void say_err(char *what, int err) {
    unsigned n = strlcpy(message, what, sizeof(message));
    if (n + 2u < sizeof(message)) {
        message[n] = ':';
        message[n + 1u] = ' ';
        strlcpy(message + n + 2u, fs_strerror(err), sizeof(message) - n - 2u);
    }
    msg_ink = WARN;
}

/* --- building a row of text ---------------------------------------------
 * Rows are laid out as fixed-width character cells and drawn in one
 * disp_text call, so columns line up without measuring pixels. */

static void blank(char *row) {
    memset(row, 32, COLS);
    row[COLS] = 0;
}

static void place(char *row, unsigned col, char *s) {
    while (*s != 0 && col < (unsigned)COLS) {
        row[col] = *s;
        col++;
        s++;
    }
}

static void place_right(char *row, char *s) {
    unsigned n = strlen(s);
    if (n >= (unsigned)COLS) place(row, 0u, s);
    else place(row, (unsigned)COLS - n, s);
}

/* "<dir>", "431 B" or "12 K" -- at most six columns either way. */
static void size_text(fs_stat_t *e, char *out) {
    unsigned kb;
    if (e->type == FS_TYPE_DIR) {
        strcpy(out, "<dir>");
        return;
    }
    if (e->size < 1000u) {
        out = out + utoa(e->size, out, 10u);
        strcpy(out, " B");
        return;
    }
    kb = (e->size + 1023u) / 1024u;
    out = out + utoa(kb, out, 10u);
    strcpy(out, " K");
}

static void entry_row(int i, char *row) {
    char size[STR_UTOA_MAX + 4];
    fs_stat_t *e;
    unsigned n;

    e = &entries[i];
    blank(row);

    n = strlen(e->name);
    if (n > (unsigned)NAME_COLS - 1u) {
        /* Cut long names rather than letting them run into the size
         * column; the '~' says the name on disk is longer. */
        memcpy(row + 1, e->name, (unsigned)NAME_COLS - 1u);
        row[NAME_COLS] = '~';
    } else {
        memcpy(row + 1, e->name, n);
        if (e->type == FS_TYPE_DIR) row[1u + n] = '/';
    }

    size_text(e, size);
    place_right(row, size);
}

/* --- the listing --------------------------------------------------------- */

static int before(fs_stat_t *a, fs_stat_t *b) {
    if (a->type != b->type) return (a->type == FS_TYPE_DIR) ? 1 : 0;
    return (strcmp(a->name, b->name) < 0) ? 1 : 0;
}

/* readdir hands entries back in directory-block order, which is the
 * order they were created in with holes reused -- fine for the library,
 * unreadable on screen. Directories first, then by name. */
static void sort_entries(void) {
    fs_stat_t tmp;
    int i, j, m;

    for (i = 0; i < n_entries - 1; i++) {
        m = i;
        for (j = i + 1; j < n_entries; j++) {
            if (before(&entries[j], &entries[m]) != 0) m = j;
        }
        if (m != i) {
            memcpy(&tmp, &entries[i], sizeof(fs_stat_t));
            memcpy(&entries[i], &entries[m], sizeof(fs_stat_t));
            memcpy(&entries[m], &tmp, sizeof(fs_stat_t));
        }
    }
}

static void scroll_fix(void) {
    if (sel < top) top = sel;
    if (sel > top + ROWS - 1) top = sel - ROWS + 1;
    if (top > n_entries - ROWS) top = n_entries - ROWS;
    if (top < 0) top = 0;
}

static void reload(void) {
    int dh, r;

    n_entries = 0;
    truncated = 0;

    dh = fs_opendir(".");
    if (dh < 0) {
        say_err("opendir", dh);
        return;
    }
    r = fs_readdir(dh, &entries[0]);
    while (r == 1) {
        n_entries++;
        if (n_entries >= MAX_ENTRIES) {
            truncated = 1;
            break;
        }
        r = fs_readdir(dh, &entries[n_entries]);
    }
    fs_closedir(dh);
    if (r < 0) say_err("readdir", r);

    sort_entries();
    if (sel >= n_entries) sel = n_entries - 1;
    if (sel < 0) sel = 0;
    scroll_fix();
    dirty = 1;
}

/* --- wrapping text into display lines ------------------------------------
 *
 * Used by the viewer and by the note editor, which never run at once, so
 * they share the one index. A trailing '\n' produces a final empty line:
 * the viewer should show it, and the editor needs the caret to land
 * there after you press enter. */
static int wrap_text(char *p, unsigned n) {
    int count = 0;
    unsigned i = 0;
    unsigned start, len;

    while (count < MAX_WRAP) {
        start = i;
        len = 0;
        while (i < n && p[i] != 10 && len < (unsigned)COLS) {
            i++;
            len++;
        }
        wrap_at[count] = start;
        wrap_len[count] = len;
        count++;

        if (i < n && p[i] == 10) {
            i++;
            if (i >= n) {
                if (count < MAX_WRAP) {
                    wrap_at[count] = i;
                    wrap_len[count] = 0;
                    count++;
                }
                break;
            }
        } else if (i >= n) {
            break;
        }
    }
    return count;
}

/* One wrapped line onto the screen, with anything unprintable shown as
 * '.' -- a tab or a stray byte must not be handed to disp_char. */
static void draw_wrapped_line(int line, unsigned y, char *p, color_t ink) {
    char row[COLS + 1];
    unsigned j, c;

    for (j = 0u; j < wrap_len[line]; j++) {
        c = (unsigned)(unsigned char)p[wrap_at[line] + j];
        row[j] = (c < 32u || c > 126u) ? '.' : (char)c;
    }
    row[j] = 0;
    disp_text(0, y, row, ink);
}

/* --- chrome --------------------------------------------------------------- */

static void draw_header(char *left, char *right, color_t ink) {
    char row[COLS + 1];

    disp_rect(0, 0, DISP_W, HEAD_RULE, BAR);
    blank(row);
    place(row, 0u, left);
    if (right != NULL) place_right(row, right);
    disp_text(0, HEAD_Y, row, ink);
    disp_hline(0, HEAD_RULE, DISP_W, DIM);
}

static void draw_list_header(void) {
    char cwd[FS_PATH_MAX + 1];
    char tag[20];
    fs_volinfo info;

    if (fs_getcwd(cwd, sizeof(cwd)) < 0) strcpy(cwd, "?");
    if (fs_statvfs(vol, &info) >= 0) {
        strcpy(tag, "[");
        strlcat(tag, info.label, sizeof(tag));
        strlcat(tag, "]", sizeof(tag));
        draw_header(cwd, tag, ACCENT);
    } else {
        draw_header(cwd, NULL, ACCENT);
    }
}

static void draw_status(char *hint) {
    char row[COLS + 1];
    char num[STR_UTOA_MAX + 12];
    fs_volinfo info;
    color_t ink;

    disp_hline(0, FOOT_RULE, DISP_W, DIM);
    blank(row);
    ink = DIM;

    if (message[0] != 0) {
        place(row, 0u, message);
        ink = msg_ink;
    } else if (hint != NULL) {
        place(row, 0u, hint);
    } else {
        utoa((unsigned)n_entries, num, 10u);
        place(row, 0u, num);
        place(row, strlen(num) + 1u, (n_entries == 1) ? "item" : "items");
        if (truncated != 0) place(row, strlen(num) + 7u, "(+)");
        /* Blocks are 512 bytes, so two to the kilobyte. */
        if (fs_statvfs(vol, &info) >= 0) {
            utoa(info.free_blocks / 2u, num, 10u);
            strlcat(num, " KB free", sizeof(num));
            place_right(row, num);
        }
    }
    disp_text(0, STATUS_Y, row, ink);
}

/* --- the screens ---------------------------------------------------------- */

static void draw_list(void) {
    char row[COLS + 1];
    unsigned y, h, bar, pos;
    int i, r;

    draw_list_header();

    if (n_entries == 0) {
        disp_text(CELL, LIST_Y + ROW, "(empty directory)", DIM);
    }
    for (r = 0; r < ROWS; r++) {
        i = top + r;
        if (i >= n_entries) break;
        y = LIST_Y + (unsigned)r * ROW;
        entry_row(i, row);
        if (i == sel) {
            row[0] = '>';
            disp_rect(0, y - 1u, DISP_W, GLYPH_H + 1, PANEL);
            disp_text(0, y, row, ACCENT);
        } else {
            disp_text(0, y, row, (entries[i].type == FS_TYPE_DIR) ? INK : DIM);
        }
    }

    /* A scrollbar in the last pixel column, which the 5-px glyphs in
     * column 31 do not reach. */
    if (n_entries > ROWS) {
        h = (unsigned)ROWS * ROW;
        bar = h * (unsigned)ROWS / (unsigned)n_entries;
        pos = h * (unsigned)top / (unsigned)n_entries;
        if (bar < 3u) bar = 3u;
        disp_vline(DISP_W - 1, LIST_Y, h, PANEL);
        disp_vline(DISP_W - 1, LIST_Y + pos, bar, ACCENT);
    }
    draw_status(NULL);
}

static void draw_view(void) {
    char pos[STR_UTOA_MAX * 2 + 8];
    char num[STR_UTOA_MAX];
    int r, line;

    draw_header(view_name, NULL, INK);
    for (r = 0; r < ROWS; r++) {
        line = view_top + r;
        if (line >= n_wrap) break;
        draw_wrapped_line(line, LIST_Y + (unsigned)r * ROW, view_buf, INK);
    }

    strcpy(pos, "line ");
    utoa((unsigned)(view_top + 1), num, 10u);
    strlcat(pos, num, sizeof(pos));
    strlcat(pos, "/", sizeof(pos));
    utoa((unsigned)n_wrap, num, 10u);
    strlcat(pos, num, sizeof(pos));
    strlcat(pos, "   esc back", sizeof(pos));
    draw_status(pos);
}

static void draw_prompt(void) {
    char row[COLS + 1];
    unsigned label, shown, start;
    unsigned y;

    draw_list_header();

    y = LIST_Y + 3u * ROW;
    disp_rect(0, y - 3u, DISP_W, GLYPH_H + 6, PANEL);
    disp_frame(0, y - 3u, DISP_W, GLYPH_H + 6, DIM);

    /* Column 0 is left empty: the panel's frame runs down it, and a
     * glyph drawn there loses its first ink column to the border -- the
     * 'n' of "note:" read as an 'h'. */
    blank(row);
    place(row, 1u, prompt_label);
    label = strlen(prompt_label) + 2u;

    /* The field scrolls once the name outgrows what is left of the row,
     * so the end you are typing is always the end you can see. */
    shown = (unsigned)COLS - label;
    start = 0u;
    if ((unsigned)prompt_len >= shown) start = (unsigned)prompt_len - shown + 1u;
    place(row, label, prompt_buf + start);

    disp_text(0, y, row, INK);
    disp_vline((label + (unsigned)prompt_len - start) * CELL, y - 1u, GLYPH_H + 2, ACCENT);
    draw_status("enter ok   esc cancel");
}

static void draw_edit(void) {
    unsigned y, caret_col;
    int r, first, line;

    draw_header(note_name, "note", INK);

    n_wrap = wrap_text(note_buf, (unsigned)note_len);
    first = n_wrap - ROWS;
    if (first < 0) first = 0;

    for (r = 0; r < ROWS; r++) {
        line = first + r;
        if (line >= n_wrap) break;
        draw_wrapped_line(line, LIST_Y + (unsigned)r * ROW, note_buf, INK);
    }

    /* The caret sits past the last character, which is always on the
     * last wrapped line -- there is no cursor movement in here. */
    line = n_wrap - 1;
    caret_col = wrap_len[line];
    y = LIST_Y + (unsigned)(line - first) * ROW;
    disp_vline(caret_col * CELL, y - 1u, GLYPH_H + 2, ACCENT);

    draw_status("tab save   esc cancel");
}

static void draw_confirm(void) {
    unsigned y;

    draw_list_header();
    y = LIST_Y + 3u * ROW;
    disp_rect(0, y - 3u, DISP_W, GLYPH_H + 6, PANEL);
    disp_frame(0, y - 3u, DISP_W, GLYPH_H + 6, WARN);
    disp_text(CELL, y, confirm_msg, INK);
    draw_status("y delete   any other key no");
}

static void draw_help(void) {
    int i;

    draw_header("keys", NULL, ACCENT);
    for (i = 0; i < HELP_LINES; i++) {
        disp_text(CELL, LIST_Y + (unsigned)i * ROW, HELP[i], INK);
    }
    draw_status("any key back");
}

/* Whole frames only: the emulator snapshots the framebuffer on its own
 * 30 Hz clock and would otherwise catch the gap between the clear and
 * the first row. See display.h. */
static void render(void) {
    disp_clear(BG);
    if (mode == M_VIEW) draw_view();
    else if (mode == M_PROMPT) draw_prompt();
    else if (mode == M_EDIT) draw_edit();
    else if (mode == M_CONFIRM) draw_confirm();
    else if (mode == M_HELP) draw_help();
    else draw_list();
    disp_present();
}

/* --- actions --------------------------------------------------------------- */

static void close_view(void) {
    if (view_buf != NULL) {
        free(view_buf);
        view_buf = NULL;
    }
    view_size = 0u;
    n_wrap = 0;
    mode = M_LIST;
}

/* Anything with a NUL or a control byte in it is not something the 5x7
 * font can show, so say so rather than painting a screen of dots. */
static int looks_like_text(char *p, unsigned n) {
    unsigned i, c;
    for (i = 0u; i < n; i++) {
        c = (unsigned)(unsigned char)p[i];
        if (c == 0u) return 0;
        if (c < 32u && c != 9u && c != 10u && c != 13u) return 0;
    }
    return 1;
}

static void view_file(fs_stat_t *e) {
    int r;

    if (e->size > VIEW_MAX) {
        say("too big to view", WARN);
        return;
    }
    /* malloc size + 1 rather than fs_load_alloc, so an empty file is
     * still a valid allocation and the caller has one path out. */
    view_buf = (char *)malloc(e->size + 1u);
    if (view_buf == NULL) {
        say("out of memory", WARN);
        return;
    }
    r = fs_load(e->name, view_buf, e->size + 1u);
    if (r < 0) {
        free(view_buf);
        view_buf = NULL;
        say_err("read", r);
        return;
    }
    view_size = (unsigned)r;
    if (looks_like_text(view_buf, view_size) == 0) {
        free(view_buf);
        view_buf = NULL;
        say("not a text file", WARN);
        return;
    }
    strlcpy(view_name, e->name, sizeof(view_name));
    n_wrap = wrap_text(view_buf, view_size);
    view_top = 0;
    mode = M_VIEW;
}

static void activate(void) {
    fs_stat_t *e;
    int r;

    if (n_entries == 0) return;
    e = &entries[sel];
    if (e->type == FS_TYPE_DIR) {
        r = fs_chdir(e->name);
        if (r < 0) {
            say_err("chdir", r);
            return;
        }
        sel = 0;
        top = 0;
        reload();
        return;
    }
    view_file(e);
}

static void go_up(void) {
    int r = fs_chdir("..");
    if (r < 0) {
        say_err("chdir", r);
        return;
    }
    sel = 0;
    top = 0;
    reload();
}

static void start_prompt(char *label, int what) {
    strlcpy(prompt_label, label, sizeof(prompt_label));
    prompt_buf[0] = 0;
    prompt_len = 0;
    prompt_what = what;
    mode = M_PROMPT;
}

static void start_delete(void) {
    unsigned n;

    if (n_entries == 0) return;
    n = strlcpy(confirm_msg, "delete ", sizeof(confirm_msg));
    strlcpy(confirm_msg + n, entries[sel].name, sizeof(confirm_msg) - n);
    strlcat(confirm_msg, "?", sizeof(confirm_msg));
    mode = M_CONFIRM;
}

/* rmdir and remove are deliberately not interchangeable in the library,
 * so the refusals they give back -- ENOTEMPTY, EISDIR, EBUSY -- are what
 * reaches the status line. */
static void do_delete(void) {
    fs_stat_t *e;
    int r;

    mode = M_LIST;
    if (n_entries == 0) return;
    e = &entries[sel];
    if (e->type == FS_TYPE_DIR) r = fs_rmdir(e->name);
    else r = fs_remove(e->name);

    if (r < 0) say_err("delete", r);
    else say("deleted", GOOD);
    reload();
}

static void save_note(void) {
    fs_stat_t before_save;
    int existed;
    int r;

    existed = (fs_stat(note_name, &before_save) >= 0) ? 1 : 0;
    r = fs_save(note_name, note_buf, (unsigned)note_len);
    mode = M_LIST;
    if (r < 0) {
        say_err("save", r);
        return;
    }
    say(existed != 0 ? "replaced" : "saved", GOOD);
    reload();
    /* Leave the cursor on what was just written. */
    for (r = 0; r < n_entries; r++) {
        if (strcmp(entries[r].name, note_name) == 0) {
            sel = r;
            scroll_fix();
            break;
        }
    }
}

static void commit_prompt(void) {
    int r;

    if (prompt_len == 0) {
        say("a name is needed", WARN);
        mode = M_LIST;
        return;
    }
    if (prompt_what == P_DIR) {
        r = fs_mkdir(prompt_buf);
        mode = M_LIST;
        if (r < 0) say_err("mkdir", r);
        else say("created", GOOD);
        reload();
        return;
    }
    strlcpy(note_name, prompt_buf, sizeof(note_name));
    note_buf[0] = 0;
    note_len = 0;
    mode = M_EDIT;
}

/* --- keys ------------------------------------------------------------------ */

static void key_list(int code) {
    if (code == KEY_UP && sel > 0) sel--;
    else if (code == KEY_DOWN && sel < n_entries - 1) sel++;
    else if (code == KEY_PGUP) sel = sel - ROWS;
    else if (code == KEY_PGDN) sel = sel + ROWS;
    else if (code == KEY_HOME) sel = 0;
    else if (code == KEY_END) sel = n_entries - 1;
    else if (code == KEY_ENTER) activate();
    else if (code == KEY_BACKSPACE) go_up();
    else if (code == 'n') start_prompt("note:", P_NOTE);
    else if (code == 'd') start_prompt("dir:", P_DIR);
    else if (code == 'x') start_delete();
    else if (code == '?' || code == 'h') mode = M_HELP;
    else if (code == KEY_ESC) running = 0;

    if (sel >= n_entries) sel = n_entries - 1;
    if (sel < 0) sel = 0;
    scroll_fix();
}

static void key_view(int code) {
    if (code == KEY_UP) view_top--;
    else if (code == KEY_DOWN) view_top++;
    else if (code == KEY_PGUP) view_top = view_top - ROWS;
    else if (code == KEY_PGDN) view_top = view_top + ROWS;
    else if (code == KEY_HOME) view_top = 0;
    else if (code == KEY_END) view_top = n_wrap - ROWS;
    else if (code == KEY_ESC || code == KEY_BACKSPACE || code == KEY_ENTER) {
        close_view();
        return;
    }
    if (view_top > n_wrap - ROWS) view_top = n_wrap - ROWS;
    if (view_top < 0) view_top = 0;
}

static void key_prompt(int code) {
    if (code == KEY_ESC) {
        mode = M_LIST;
        return;
    }
    if (code == KEY_ENTER) {
        commit_prompt();
        return;
    }
    if (code == KEY_BACKSPACE) {
        if (prompt_len > 0) {
            prompt_len--;
            prompt_buf[prompt_len] = 0;
        }
        return;
    }
    if (code < 32 || code > 126) return;
    /* '/' would make this a path, and the point of the prompt is a name
     * in the directory you are looking at. */
    if (code == '/' || code == ':') {
        say("no / or : in a name", WARN);
        return;
    }
    if (prompt_len >= FS_NAME_MAX) {
        say("name is full", WARN);
        return;
    }
    prompt_buf[prompt_len] = (char)code;
    prompt_len++;
    prompt_buf[prompt_len] = 0;
}

/* TAB saves, not F1: ESC is cancel everywhere else in here, and the
 * browser front end preventDefault()s every key it translates
 * (display/index.html), so TAB never moves focus out of the canvas. */
static void key_edit(int code) {
    if (code == KEY_ESC) {
        mode = M_LIST;
        say("discarded", DIM);
        return;
    }
    if (code == KEY_TAB) {
        save_note();
        return;
    }
    if (code == KEY_BACKSPACE) {
        if (note_len > 0) {
            note_len--;
            note_buf[note_len] = 0;
        }
        return;
    }
    if (code != KEY_ENTER && (code < 32 || code > 126)) return;
    if (note_len >= NOTE_MAX) {
        say("the note is full", WARN);
        return;
    }
    note_buf[note_len] = (code == KEY_ENTER) ? 10 : (char)code;
    note_len++;
    note_buf[note_len] = 0;
}

static void handle_key(int code) {
    message[0] = 0;             /* a message lasts until the next keypress */

    if (mode == M_VIEW) key_view(code);
    else if (mode == M_PROMPT) key_prompt(code);
    else if (mode == M_EDIT) key_edit(code);
    else if (mode == M_CONFIRM) {
        if (code == 'y' || code == 'Y') do_delete();
        else mode = M_LIST;
    } else if (mode == M_HELP) {
        mode = M_LIST;
    } else {
        key_list(code);
    }
    dirty = 1;
}

/* --- first run -------------------------------------------------------------- */

static void write_lines(char *path, char **lines, int count) {
    int fd, i;

    fd = fs_open(path, FS_WRITE | FS_CREATE | FS_TRUNC);
    if (fd < 0) return;
    for (i = 0; i < count; i++) {
        fs_puts(fd, lines[i]);
        fs_puts(fd, "\n");
    }
    fs_close(fd);
}

static void seed_disk(void) {
    fs_mkdir("/notes");
    fs_mkdir("/logs");
    write_lines("/readme.txt", README_TXT, README_LINES);
    write_lines("/notes/hello.txt", HELLO_TXT, HELLO_LINES);
    write_lines("/notes/todo.txt", TODO_TXT, TODO_LINES);
}

/* A disk that cannot be mounted is the end of the program, but not the
 * end of the machine -- there is no console to print to, so the error
 * goes on the screen and stays there until ESC. */
static void die(char *what, int err) {
    int code;

    disp_clear(BG);
    draw_header("PigeonFS", NULL, WARN);
    disp_text(CELL, LIST_Y + ROW, what, INK);
    disp_text(CELL, LIST_Y + 2u * ROW, fs_strerror(err), WARN);
    disp_text(CELL, LIST_Y + 4u * ROW, "run: tools/pfs.py fsck", DIM);
    draw_status("esc quit");
    disp_present();

    for (;;) {
        code = key_read();
        while (code >= 0) {
            if (code == KEY_ESC) return;
            code = key_read();
        }
    }
}

int main(void) {
    int code;
    int r;

    disp_use_back_buffer();
    vol = CH_HDD;
    mode = M_LIST;
    message[0] = 0;
    view_buf = NULL;

    r = fs_mount(vol);
    if (r == FS_ENOFS) {
        /* No force flag: this only ever reaches a disk of zeros. A disk
         * that already holds anything comes back FS_ENOTBLANK and the
         * program stops rather than taking it. */
        r = fs_format(vol, "PIGEON", 0u);
        if (r >= 0) {
            r = fs_mount(vol);
            if (r >= 0) {
                seed_disk();
                say("formatted a blank disk", GOOD);
            }
        }
    }
    if (r < 0) {
        die("cannot mount channel 2", r);
        return r;
    }

    reload();
    running = 1;
    dirty = 1;

    while (running != 0) {
        code = key_read();
        while (code >= 0) {
            handle_key(code);
            code = key_read();
        }
        if (dirty != 0) {
            render();
            dirty = 0;
        }
    }

    close_view();
    fs_unmount(vol);
    disp_clear(BG);
    disp_text(DISP_W / 2 - 9 * CELL / 2, DISP_H / 2 - 4, "disk is safe", DIM);
    disp_present();
    return 0;
}
