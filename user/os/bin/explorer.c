/* explorer -- a file explorer you drive with the mouse (docs/explorer_plan.md).
 *
 *     explorer        the folder you are in
 *
 * The list is that folder: directories first, then files, with ".." on top
 * unless it is the root. Up/Down, PgUp/PgDn, Home/End move; Enter opens;
 * Backspace goes to the parent; Esc quits; F5 reads the folder again; ? is
 * the key list. n makes a file, d a directory, F2 renames, x deletes, and o
 * opens the selected file with a command you type. The wheel scrolls, a
 * click selects, a second click on the row already selected opens it, a
 * click on empty space below the entries opens the folder's menu, and a
 * right-click on a row opens that entry's own.
 *
 * What opens a file is /etc/explorer.conf: one rule a line,
 * `command = pattern`, the first matching pattern winning, and the file's
 * name added to the end of the command --
 *
 *     EXEC            = .bin      run the file itself, as the prompt would
 *     /bin/img.bin -s = .bmp      img -s pigeon.bmp
 *     /bin/edit.bin   = *.*       everything else
 *
 * -- which is also what it uses when there is no conf file. A pattern is
 * ".ext", matched without case, or "*.*", which matches everything and so
 * belongs last. A line the parser can't read is said on the status line and
 * skipped; a rule after a "*.*", which can never fire, is said and kept.
 * Directories are the explorer's own, and no rule applies to them.
 *
 * A program runs through the kernel's exec, so the explorer is still here
 * when it ends. The kernel draws its console over this screen after every
 * program, so what the program printed is what you see, with
 * "[ press any key ]" under it; the next key brings the list back. The
 * folder is read again then, and the explorer goes back to its own
 * directory, since there is one current directory and every program shares
 * it -- a shell started here can leave it anywhere.
 */
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

/* --- layout -------------------------------------------------------------
 * From DISPLAY_W/H and the font, never typed: 32 columns by 12 rows of a
 * 6x9 cell, as user/files.c lays itself out for the same reason. */
#define CELL      (GLYPH_W + 1)                 /* 6 px: glyph plus a gap  */
#define COLS      (DISP_W / CELL)               /* 32                      */
#define ROW       (GLYPH_H + 1)                 /* 9 px per line           */

#define HEAD_Y    1
#define HEAD_RULE (HEAD_Y + GLYPH_H + 1)
#define LIST_Y    (HEAD_RULE + 3)
#define STATUS_Y  (DISP_H - GLYPH_H - 1)
#define FOOT_RULE (STATUS_Y - 2)
#define ROWS      ((FOOT_RULE - 1 - LIST_Y) / ROW)      /* 9 rows          */

#define SIZE_COLS 9                             /* the size, right-aligned */
#define NAME_COLS (COLS - SIZE_COLS - 1)

#define BG      0xFF0A0C10
#define PANEL   0xFF181C24
#define BAR     0xFF232936
#define ACCENT  0xFF30C0FF
#define INK     0xFFD8DEE9
#define DIM     0xFF6A7284
#define WARN    0xFFFF6B5E

/* --- what there is ------------------------------------------------------ */
#define CONF        "/etc/explorer.conf"
#define SHELL       "/bin/sh.bin"
#define CONF_MAX    1024u           /* bytes of it, as the shell's prompt file */
#define MAX_RULES   32
#define CMD_MAX     128u            /* a rule's command, as written        */
#define PAT_MAX     16u
#define WORDS       8               /* words in a command, before the file */
#define PATH        264
#define MAX_ENTRIES 128             /* a longer folder is shown cut short  */

/* There is no switch in this compiler, so the modes are if-chains. */
#define M_LIST    0
#define M_MENU    1
#define M_PROMPT  2
#define M_CONFIRM 3
#define M_HELP    4

#define P_NEWFILE  1
#define P_NEWDIR   2
#define P_RENAME   3
#define P_OPENWITH 4

#define MENU_MAX 4
#define KEY_F2   (KEY_F1 + 1)
#define KEY_F5   (KEY_F1 + 4)
#define MB_RIGHT_BUTTON 1

static int mode;
static int running;
static int dirty;

static sys_stat_t entries[MAX_ENTRIES];
static int n_entries;
static int truncated;
static int sel;                     /* the highlighted entry               */
static int top;                     /* the first entry drawn               */
static char here[PATH];             /* the folder shown, as getcwd says it */

static char message[COLS + 1];
static color_t msg_ink;

static char rule_cmd[MAX_RULES * CMD_MAX];      /* "EXEC", "/bin/img.bin -s" */
static char rule_pat[MAX_RULES * PAT_MAX];      /* ".bmp", "*.*"             */
static int n_rules;

static char *menu_item[MENU_MAX];
static int menu_count;
static int menu_sel;
static int menu_folder;             /* 1 the folder's menu, 0 an entry's   */
static unsigned menu_x;
static unsigned menu_y;
static unsigned menu_w;
static unsigned menu_h;

static char prompt_label[16];
static char prompt_buf[PATH];
static unsigned prompt_len;
static int prompt_what;

/* Nine lines, a row apart: what the list area holds. */
static char *HELP[] = {
    "up/down enter  move, open",
    "bksp esc       parent, quit",
    "f5             read it again",
    "n d            new file, folder",
    "f2 x           rename, delete",
    "o              open with...",
    "click        select; again: open",
    "click space  folder menu",
    "right-click  entry menu"
};
#define HELP_LINES 9

/* --- the status line ----------------------------------------------------- */

static void say(char *text, color_t ink) {
    strlcpy(message, text, sizeof(message));
    msg_ink = ink;
    dirty = 1;
}

/* "note.txt: no such file or directory", in the kernel's own words, so a
 * refusal is never silent. */
static void say_err(char *what, int status) {
    unsigned n = strlcpy(message, what, sizeof(message));
    if (n + 2u < sizeof(message)) {
        message[n] = ':';
        message[n + 1u] = ' ';
        strlcpy(message + n + 2u, sys_strerror(status), sizeof(message) - n - 2u);
    }
    msg_ink = WARN;
    dirty = 1;
}

/* "explorer.conf:3: expected command = pattern". The first complaint is the
 * one shown; the rest are counted, since the status line is one row. */
static void complain(unsigned line, char *what, char *detail) {
    char text[COLS + 1];
    char number[STR_UTOA_MAX];
    if (message[0] != 0) {
        strlcat(message, " (+)", sizeof(message));
        return;
    }
    strlcpy(text, "conf", sizeof(text));
    if (line > 0u) {
        strlcat(text, ":", sizeof(text));
        utoa(line, number, 10u);
        strlcat(text, number, sizeof(text));
    }
    strlcat(text, ": ", sizeof(text));
    strlcat(text, what, sizeof(text));
    strlcat(text, detail, sizeof(text));
    say(text, WARN);
}

/* --- rows of text -------------------------------------------------------
 * Fixed-width cells drawn in one disp_text call, so columns line up
 * without measuring pixels. */

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

/* "<dir>", "431 B" or "12 K": six columns at the most. */
static void size_text(sys_stat_t *e, char *out) {
    if (e->type == S_DIR) {
        strcpy(out, "<dir>");
        return;
    }
    if (e->size < 1000u) {
        out = out + utoa(e->size, out, 10u);
        strcpy(out, " B");
        return;
    }
    out = out + utoa((e->size + 1023u) / 1024u, out, 10u);
    strcpy(out, " K");
}

static void entry_row(int i, char *row) {
    char size[STR_UTOA_MAX + 4];
    sys_stat_t *e = &entries[i];
    unsigned n;

    blank(row);
    n = strlen(e->name);
    if (n > (unsigned)NAME_COLS - 1u) {
        /* Cut rather than run into the size column; '~' says the name on
         * the disk is longer. */
        memcpy(row + 1, e->name, (unsigned)NAME_COLS - 1u);
        row[NAME_COLS] = '~';
    } else {
        memcpy(row + 1, e->name, n);
        if (e->type == S_DIR && strcmp(e->name, "..") != 0) row[1u + n] = '/';
    }
    size_text(e, size);
    place_right(row, size);
}

/* --- the folder ---------------------------------------------------------- */

static int at_root(void) {
    unsigned n = strlen(here);
    return (n > 0u && here[n - 1u] == '/') ? 1 : 0;     /* "2:/" */
}

static void where_am_i(void) {
    if (getcwd(here, PATH) < 0) strlcpy(here, "?", PATH);
}

static int before(sys_stat_t *a, sys_stat_t *b) {
    if (a->type != b->type) return (a->type == S_DIR) ? 1 : 0;
    return (strcmp(a->name, b->name) < 0) ? 1 : 0;
}

/* readdir hands entries back in the order the directory holds them, which
 * is the order they were made in with holes reused: fine for the kernel,
 * unreadable here. Directories first, then by name -- from `first`, so ".."
 * keeps the top. */
static void sort_entries(int first) {
    sys_stat_t tmp;
    int i;
    int j;
    int m;
    for (i = first; i < n_entries - 1; i++) {
        m = i;
        for (j = i + 1; j < n_entries; j++) {
            if (before(&entries[j], &entries[m]) != 0) m = j;
        }
        if (m != i) {
            memcpy(&tmp, &entries[i], sizeof(sys_stat_t));
            memcpy(&entries[i], &entries[m], sizeof(sys_stat_t));
            memcpy(&entries[m], &tmp, sizeof(sys_stat_t));
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
    int first;
    int dh;
    int r;

    where_am_i();
    n_entries = 0;
    truncated = 0;
    if (!at_root()) {
        strlcpy(entries[0].name, "..", 32u);
        entries[0].type = S_DIR;
        entries[0].size = 0u;
        n_entries = 1;
    }
    first = n_entries;

    dh = opendir(".");
    if (dh < 0) {
        say_err("opendir", dh);
        return;
    }
    r = readdir(dh, &entries[n_entries]);
    while (r == 1) {
        n_entries++;
        if (n_entries >= MAX_ENTRIES) {
            truncated = 1;
            break;
        }
        r = readdir(dh, &entries[n_entries]);
    }
    closedir(dh);
    if (r < 0) say_err("readdir", r);

    sort_entries(first);
    if (sel >= n_entries) sel = n_entries - 1;
    if (sel < 0) sel = 0;
    scroll_fix();
    dirty = 1;
}

static void go_to(char *path) {
    int status = chdir(path);
    if (status < 0) {
        say_err(path, status);
        return;
    }
    sel = 0;
    top = 0;
    message[0] = 0;
    reload();
}

/* --- the drawing ---------------------------------------------------------
 *
 * Straight to the screen, and only when something changed. No back buffer:
 * the console draws at DISPLAY_START, and a program this one starts must
 * be able to print where you can see it, so the display is left pointing
 * where the kernel left it. */

static void draw_header(void) {
    char row[COLS + 1];
    char count[STR_UTOA_MAX + 8];
    char *path = here;
    unsigned n = strlen(here);

    disp_rect(0, 0, DISP_W, HEAD_RULE, BAR);
    blank(row);
    if (n > (unsigned)COLS - 10u) {         /* a deep path: keep its end */
        path = here + n - ((unsigned)COLS - 11u);
        place(row, 0u, "~");
        place(row, 1u, path);
    } else {
        place(row, 0u, here);
    }
    utoa((unsigned)n_entries, count, 10u);
    strlcat(count, (n_entries == 1) ? " item" : " items", sizeof(count));
    if (truncated != 0) strlcat(count, "+", sizeof(count));
    place_right(row, count);
    disp_text(0, HEAD_Y, row, ACCENT);
    disp_hline(0, HEAD_RULE, DISP_W, DIM);
}

static void draw_status(char *hint) {
    char row[COLS + 1];

    disp_hline(0, FOOT_RULE, DISP_W, DIM);
    blank(row);
    if (message[0] != 0) {
        place(row, 0u, message);
        disp_text(0, STATUS_Y, row, msg_ink);
        return;
    }
    place(row, 0u, (hint != NULL) ? hint : "enter open   ? keys   esc quit");
    disp_text(0, STATUS_Y, row, DIM);
}

static void draw_list(void) {
    char row[COLS + 1];
    unsigned y;
    unsigned h;
    unsigned bar;
    unsigned pos;
    int i;
    int r;

    if (n_entries == 0) disp_text(CELL, LIST_Y + ROW, "(empty folder)", DIM);
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
            disp_text(0, y, row, (entries[i].type == S_DIR) ? INK : DIM);
        }
    }
    /* A scrollbar in the last pixel column, which the 5-px glyphs in
     * column 31 never reach. */
    if (n_entries > ROWS) {
        h = (unsigned)ROWS * ROW;
        bar = h * (unsigned)ROWS / (unsigned)n_entries;
        pos = h * (unsigned)top / (unsigned)n_entries;
        if (bar < 3u) bar = 3u;
        disp_vline(DISP_W - 1, LIST_Y, h, PANEL);
        disp_vline(DISP_W - 1, LIST_Y + pos, bar, ACCENT);
    }
}

/* menu_x and menu_y are the first item's text, on the cells the list uses,
 * so a menu's words line up with the names under it. The box is the three
 * pixels above that and two below the last line. */
static void draw_menu(void) {
    unsigned y;
    int i;

    disp_rect(menu_x, menu_y - 3u, menu_w, menu_h, PANEL);
    disp_frame(menu_x, menu_y - 3u, menu_w, menu_h, ACCENT);
    for (i = 0; i < menu_count; i++) {
        y = menu_y + (unsigned)i * ROW;
        if (i == menu_sel) disp_rect(menu_x + 1u, y - 1u, menu_w - 2u, GLYPH_H + 1, BAR);
        disp_text(menu_x + CELL, y, menu_item[i], (i == menu_sel) ? ACCENT : INK);
    }
}

static void draw_prompt(void) {
    char row[COLS + 1];
    unsigned y = LIST_Y + 3u * ROW;
    unsigned label;
    unsigned room;
    char *shown = prompt_buf;

    disp_rect(0, y - 4u, DISP_W, GLYPH_H + 8, PANEL);
    disp_frame(0, y - 4u, DISP_W, GLYPH_H + 8, ACCENT);
    blank(row);
    label = strlen(prompt_label) + 1u;
    place(row, 1u, prompt_label);
    room = (unsigned)COLS - label - 2u;
    if (prompt_len > room) shown = prompt_buf + prompt_len - room;   /* its end */
    place(row, label + 1u, shown);
    place(row, label + 1u + strlen(shown), "_");
    disp_text(0, y, row, INK);
}

static void draw_help(void) {
    int i;
    disp_rect(0, LIST_Y - 2u, DISP_W, FOOT_RULE - LIST_Y + 2u, PANEL);
    for (i = 0; i < HELP_LINES; i++) {
        disp_text(CELL, LIST_Y + (unsigned)i * ROW, HELP[i], INK);
    }
}

static void render(void) {
    disp_clear(BG);
    draw_header();
    if (mode == M_HELP) {
        draw_help();
        draw_status("any key: back");
        return;
    }
    draw_list();
    if (mode == M_MENU) draw_menu();
    if (mode == M_PROMPT) draw_prompt();
    if (mode == M_PROMPT) draw_status("enter does it, esc doesn't");
    else if (mode == M_CONFIRM) draw_status(message);
    else draw_status(NULL);
}

/* --- /etc/explorer.conf --------------------------------------------------- */

static char *trim(char *s) {
    unsigned n;
    while (*s == ' ' || *s == '\t') s++;
    n = strlen(s);
    while (n > 0u && (s[n - 1u] == ' ' || s[n - 1u] == '\t' || s[n - 1u] == '\r')) n--;
    s[n] = 0;
    return s;
}

static void add_rule(char *cmd, char *pat) {
    strlcpy(rule_cmd + (unsigned)n_rules * CMD_MAX, cmd, CMD_MAX);
    strlcpy(rule_pat + (unsigned)n_rules * PAT_MAX, pat, PAT_MAX);
    n_rules++;
}

/* ".bmp" or "*.*", and nothing else. */
static int good_pattern(char *pat) {
    unsigned i;
    unsigned n = strlen(pat);
    if (strcmp(pat, "*.*") == 0) return 1;
    if (n < 2u || n >= PAT_MAX || pat[0] != '.') return 0;
    for (i = 1u; i < n; i++) {
        if (pat[i] == ' ' || pat[i] == '.' || pat[i] == '/') return 0;
    }
    return 1;
}

static void built_in_rules(void) {
    n_rules = 0;
    add_rule("EXEC", ".bin");
    add_rule("/bin/img.bin -s", ".bmp");
    add_rule("/bin/edit.bin", "*.*");
}

/* The conf into the rules: `command = pattern` a line, # a comment. What
 * can't be read is one line on the status line and is skipped; a rule after
 * a "*.*" is said and kept, since it is the file as you wrote it. */
static void load_conf(void) {
    char text[CONF_MAX + 1u];
    char more[1];
    char *line;
    char *next;
    char *cut;
    char *cmd;
    char *pat;
    unsigned number = 0u;
    int all = 0;                    /* a "*.*" has been taken */
    int fd;
    int n;

    n_rules = 0;
    fd = open(CONF, O_READ);
    if (fd < 0) {
        built_in_rules();
        say("no /etc/explorer.conf", DIM);
        return;
    }
    n = read(fd, text, CONF_MAX);
    if (n >= 0 && read(fd, more, 1u) > 0) {
        close(fd);
        built_in_rules();
        complain(0u, "over 1024 bytes", "");
        return;
    }
    close(fd);
    if (n < 0) {
        built_in_rules();
        complain(0u, sys_strerror(n), "");
        return;
    }
    text[n] = 0;

    line = text;
    while (line != NULL) {
        number++;
        next = strchr(line, '\n');
        if (next != NULL) {
            *next = 0;
            next++;
        }
        cut = strchr(line, '#');
        if (cut != NULL) *cut = 0;
        line = trim(line);
        if (*line != 0) {
            cut = strchr(line, '=');
            if (cut == NULL) {
                complain(number, "expected command = pattern", "");
            } else {
                *cut = 0;
                cmd = trim(line);
                pat = trim(cut + 1);
                if (*cmd == 0) complain(number, "no command", "");
                else if (*pat == 0) complain(number, "no pattern", "");
                else if (strlen(cmd) >= CMD_MAX) complain(number, "command too long", "");
                else if (!good_pattern(pat)) complain(number, "not a pattern: ", pat);
                else if (n_rules == MAX_RULES) complain(number, "over 32 rules", "");
                else {
                    if (all != 0) complain(number, "after *.*, never used", "");
                    if (strcmp(pat, "*.*") == 0) all = 1;
                    add_rule(cmd, pat);
                }
            }
        }
        line = next;
    }
    if (n_rules == 0) {
        built_in_rules();
        if (message[0] == 0) say("no rules: built-in ones", DIM);
    }
}

/* The name ends in the pattern, letters either case -- or the pattern is
 * "*.*", which every file matches. */
static int matches(char *pat, char *name) {
    unsigned p = strlen(pat);
    unsigned n = strlen(name);
    unsigned i;
    if (strcmp(pat, "*.*") == 0) return 1;
    if (n < p) return 0;
    for (i = 0u; i < p; i++) {
        if (tolower((int)name[n - p + i]) != tolower((int)pat[i])) return 0;
    }
    return 1;
}

static int rule_for(char *name) {
    int i;
    for (i = 0; i < n_rules; i++) {
        if (matches(rule_pat + (unsigned)i * PAT_MAX, name) != 0) return i;
    }
    return -1;
}

/* --- running a program ---------------------------------------------------- */

/* Split a line in place at spaces, double quotes keeping the spaces in a
 * word: the shell's own rule (sh.c). The count, or -1 for too many words. */
static int split(char *line, char **argv, int max) {
    int argc = 0;
    char *p = line;
    char *out;
    char c;
    while (1) {
        while (*p == ' ') p++;
        if (*p == 0) break;
        if (argc == max) return -1;
        argv[argc] = p;
        argc++;
        out = p;
        while (*p != 0 && *p != ' ') {
            if (*p == '"') {
                p++;
                while (*p != 0 && *p != '"') {
                    *out = *p;
                    out++;
                    p++;
                }
                if (*p == '"') p++;
            } else {
                *out = *p;
                out++;
                p++;
            }
        }
        c = *p;
        *out = 0;               /* out never passes p, so this ends it in place */
        if (c == 0) break;
        p++;
    }
    argv[argc] = NULL;
    return argc;
}

static int is_file(char *path) {
    sys_stat_t st;
    return (stat(path, &st) >= 0 && st.type == S_FILE) ? 1 : 0;
}

/* Where the program `name` is, into path: 1 if found. The shell's rule
 * (sh.c find()): /bin first, then here, with .bin added when it is
 * missing, so a word means the same here as at the prompt. */
static int find_program(char *name, char *path) {
    unsigned n = strlen(name);
    int bare = (n < 4u || strcmp(name + n - 4u, ".bin") != 0) ? 1 : 0;
    if (strchr(name, '/') == NULL) {
        strlcpy(path, "/bin/", PATH);
        strlcat(path, name, PATH);
        if (bare) strlcat(path, ".bin", PATH);
        if (is_file(path)) return 1;
    }
    strlcpy(path, name, PATH);
    if (bare) strlcat(path, ".bin", PATH);
    return is_file(path);
}

/* Any key -- or a click, for a hand that is already on the mouse. */
static void wait_for_a_key(void) {
    unsigned event;
    for (;;) {
        if (key_read() >= 0) return;
        event = mouse_event();
        while (event != 0u) {
            if (ME_PRESSED(event) != 0u && ME_BUTTON(event) < 5u) return;
            event = mouse_event();
        }
    }
}

/* exec, and what has to happen when it comes back: the kernel has drawn
 * its console over this screen, so the program's output is what's there.
 * It stays until a key, and then the folder is read again -- the program
 * may have made or removed files, and may have left the current directory
 * somewhere else. */
static void run(char *path, int argc, char **argv) {
    int status;

    status = exec(path, argc, argv);
    if (status < 0) printf("%s: %s\n", argv[0], sys_strerror(status));
    else if (status > 0) printf("%s: exit %d\n", argv[0], status);
    print("[ press any key ]");
    wait_for_a_key();
    print("\n");
    if (chdir(here) < 0) go_to("/");
    reload();
    message[0] = 0;
    dirty = 1;
}

/* A rule's command with `file` on the end of it -- or, for EXEC, the file
 * itself, run as typing its name at the prompt would run it. */
static void run_command(char *cmd, char *file) {
    char line[CMD_MAX];
    char path[PATH];
    char *argv[WORDS + 2];
    int argc;

    if (strcmp(cmd, "EXEC") == 0) {
        argv[0] = file;
        argv[1] = NULL;
        run(file, 1, argv);
        return;
    }
    strlcpy(line, cmd, CMD_MAX);
    argc = split(line, argv, WORDS);
    if (argc < 0) {
        say("too many words", WARN);
        return;
    }
    if (argc == 0) {
        say("nothing to run", WARN);
        return;
    }
    if (!find_program(argv[0], path)) {
        say_err(argv[0], E_NOENT);
        return;
    }
    if (file != NULL) {
        argv[argc] = file;
        argc++;
        argv[argc] = NULL;
    }
    run(path, argc, argv);
}

static void open_file(char *name) {
    int rule = rule_for(name);
    if (rule < 0) {
        say_err(name, E_NOENT);
        strlcpy(message, "no rule for ", sizeof(message));
        strlcat(message, name, sizeof(message));
        msg_ink = WARN;
        return;
    }
    run_command(rule_cmd + (unsigned)rule * CMD_MAX, name);
}

static void open_entry(void) {
    if (n_entries == 0) return;
    if (entries[sel].type == S_DIR) go_to(entries[sel].name);
    else open_file(entries[sel].name);
}

/* --- the prompt: a name, or a command --------------------------------------- */

static void ask(int what, char *label, char *start) {
    prompt_what = what;
    strlcpy(prompt_label, label, sizeof(prompt_label));
    strlcpy(prompt_buf, (start != NULL) ? start : "", PATH);
    prompt_len = strlen(prompt_buf);
    message[0] = 0;
    mode = M_PROMPT;
    dirty = 1;
}

static void do_prompt(void) {
    char name[PATH];
    int status;

    mode = M_LIST;
    if (prompt_len == 0u) return;
    strlcpy(name, prompt_buf, PATH);

    if (prompt_what == P_NEWDIR) {
        status = mkdir(name);
        if (status < 0) say_err(name, status);
        reload();
    } else if (prompt_what == P_NEWFILE) {
        open_file(name);                    /* edit makes it when you save */
    } else if (prompt_what == P_RENAME) {
        if (n_entries == 0) return;
        status = rename(entries[sel].name, name);
        if (status < 0) say_err(name, status);
        reload();
    } else if (prompt_what == P_OPENWITH) {
        if (n_entries == 0) return;
        run_command(name, entries[sel].name);
    }
}

/* --- deleting ---------------------------------------------------------------- */

static void ask_delete(void) {
    if (n_entries == 0 || strcmp(entries[sel].name, "..") == 0) return;
    strlcpy(message, "delete ", sizeof(message));
    strlcat(message, entries[sel].name, sizeof(message));
    strlcat(message, "? y/n", sizeof(message));
    msg_ink = WARN;
    mode = M_CONFIRM;
    dirty = 1;
}

static void do_delete(void) {
    char name[32];
    int status;

    mode = M_LIST;
    message[0] = 0;
    if (n_entries == 0) return;
    strlcpy(name, entries[sel].name, sizeof(name));
    status = (entries[sel].type == S_DIR) ? rmdir(name) : remove(name);
    if (status < 0) say_err(name, status);
    reload();
}

/* --- the menu ----------------------------------------------------------------- */

static void open_menu(int folder) {
    unsigned longest = 0u;
    unsigned n;
    unsigned y;
    int i;

    menu_folder = folder;
    if (folder != 0) {
        menu_item[0] = "New file...";
        menu_item[1] = "New folder...";
        menu_item[2] = "Shell here";
        menu_count = 3;
    } else {
        menu_item[0] = "Open";
        menu_item[1] = "Open with...";
        menu_item[2] = "Rename...";
        menu_item[3] = "Delete";
        menu_count = 4;
    }
    for (i = 0; i < menu_count; i++) {
        n = strlen(menu_item[i]);
        if (n > longest) longest = n;
    }
    menu_w = (longest + 2u) * CELL;
    menu_h = (unsigned)menu_count * ROW + 5u;
    menu_x = (mouse_x() / CELL) * CELL;             /* the list's columns */
    y = mouse_y();
    if (y < (unsigned)LIST_Y) y = (unsigned)LIST_Y;
    menu_y = (unsigned)LIST_Y + ((y - (unsigned)LIST_Y) / ROW) * ROW;    /* and its rows */
    if (menu_x + menu_w > (unsigned)DISP_W) menu_x = (((unsigned)DISP_W - menu_w) / CELL) * CELL;
    while (menu_y - 3u + menu_h > (unsigned)FOOT_RULE && menu_y >= (unsigned)LIST_Y + ROW) {
        menu_y = menu_y - ROW;                      /* a whole row at a time */
    }
    menu_sel = 0;
    message[0] = 0;
    mode = M_MENU;
    dirty = 1;
}

/* Which item is under (x, y), or -1 when the pointer is off the box. */
static int menu_at(unsigned x, unsigned y) {
    int i;
    if (x < menu_x || x >= menu_x + menu_w) return -1;
    if (y + 3u < menu_y || y >= menu_y + (unsigned)menu_count * ROW) return -1;
    i = (y + 3u < menu_y + ROW) ? 0 : (int)((y + 3u - menu_y) / ROW);
    if (i >= menu_count) return -1;
    return i;
}

static void menu_choose(void) {
    int item = menu_sel;

    mode = M_LIST;
    dirty = 1;
    if (menu_folder != 0) {
        if (item == 0) ask(P_NEWFILE, "new file", "");
        else if (item == 1) ask(P_NEWDIR, "new folder", "");
        else run_command("EXEC", SHELL);
        return;
    }
    if (n_entries == 0) return;
    if (item == 0) open_entry();
    else if (item == 1) ask(P_OPENWITH, "open with", "");
    else if (item == 2) ask(P_RENAME, "rename to", entries[sel].name);
    else ask_delete();
}

/* --- keys --------------------------------------------------------------------- */

static void key_list(int code) {
    if (code == KEY_UP) {
        if (sel > 0) sel--;
    } else if (code == KEY_DOWN) {
        if (sel < n_entries - 1) sel++;
    } else if (code == KEY_PGUP) {
        sel = sel - ROWS;
        if (sel < 0) sel = 0;
    } else if (code == KEY_PGDN) {
        sel = sel + ROWS;
        if (sel > n_entries - 1) sel = n_entries - 1;
    } else if (code == KEY_HOME) {
        sel = 0;
    } else if (code == KEY_END) {
        sel = n_entries - 1;
    } else if (code == KEY_ENTER) {
        open_entry();
    } else if (code == KEY_BACKSPACE) {
        if (!at_root()) go_to("..");
    } else if (code == KEY_ESC) {
        running = 0;
    } else if (code == KEY_F5) {
        reload();
    } else if (code == KEY_F2) {
        if (n_entries > 0 && strcmp(entries[sel].name, "..") != 0) {
            ask(P_RENAME, "rename to", entries[sel].name);
        }
    } else if (code == '?') {
        mode = M_HELP;
    } else if (code == 'n') {
        ask(P_NEWFILE, "new file", "");
    } else if (code == 'd') {
        ask(P_NEWDIR, "new folder", "");
    } else if (code == 'o') {
        if (n_entries > 0 && entries[sel].type == S_FILE) ask(P_OPENWITH, "open with", "");
    } else if (code == 'x') {
        ask_delete();
    }
    if (sel < 0) sel = 0;
    scroll_fix();
}

static void key_prompt(int code) {
    if (code == KEY_ENTER) {
        do_prompt();
    } else if (code == KEY_ESC) {
        mode = M_LIST;
    } else if (code == KEY_BACKSPACE) {
        if (prompt_len > 0u) {
            prompt_len--;
            prompt_buf[prompt_len] = 0;
        }
    } else if (code >= 32 && code < 127 && prompt_len + 1u < (unsigned)PATH) {
        prompt_buf[prompt_len] = (char)code;
        prompt_len++;
        prompt_buf[prompt_len] = 0;
    }
}

static void key_menu(int code) {
    if (code == KEY_UP) {
        if (menu_sel > 0) menu_sel--;
    } else if (code == KEY_DOWN) {
        if (menu_sel < menu_count - 1) menu_sel++;
    } else if (code == KEY_ENTER) {
        menu_choose();
    } else if (code == KEY_ESC) {
        mode = M_LIST;
    }
}

static void handle_key(int code) {
    if (mode == M_LIST) message[0] = 0;     /* a message lasts until a key */
    if (mode == M_PROMPT) key_prompt(code);
    else if (mode == M_MENU) key_menu(code);
    else if (mode == M_HELP) mode = M_LIST;
    else if (mode == M_CONFIRM) {
        if (code == 'y' || code == 'Y') do_delete();
        else {
            mode = M_LIST;
            message[0] = 0;
        }
    } else {
        key_list(code);
    }
    dirty = 1;
}

/* --- the mouse ------------------------------------------------------------------ */

static void wheel(int down) {
    sel = sel + (down ? 3 : -3);
    if (sel > n_entries - 1) sel = n_entries - 1;
    if (sel < 0) sel = 0;
    scroll_fix();
    dirty = 1;
}

static void click(unsigned button) {
    unsigned x = mouse_x();
    unsigned y = mouse_y();
    int r;
    int i;

    if (mode == M_MENU) {
        i = menu_at(x, y);
        if (i < 0) {
            mode = M_LIST;                  /* a click outside closes it */
        } else {
            menu_sel = i;
            menu_choose();
        }
        dirty = 1;
        return;
    }
    if (mode != M_LIST) return;
    message[0] = 0;
    if (y < (unsigned)LIST_Y || y >= (unsigned)FOOT_RULE) return;
    r = (int)((y - (unsigned)LIST_Y) / ROW);
    i = top + r;
    if (i >= n_entries || r >= ROWS) {
        if (button != MB_RIGHT_BUTTON) open_menu(1);    /* empty space: the folder */
        return;
    }
    if (button == MB_RIGHT_BUTTON) {
        sel = i;
        open_menu(0);
        return;
    }
    if (i == sel) open_entry();
    else sel = i;
    dirty = 1;
}

/* The item under the pointer lights up as it moves over the menu. */
static void track_pointer(void) {
    int i = menu_at(mouse_x(), mouse_y());
    if (i >= 0 && i != menu_sel) {
        menu_sel = i;
        dirty = 1;
    }
}

/* --- the program ------------------------------------------------------------------ */

int main(int argc, char **argv) {
    int code;
    unsigned event;

    setbreak(0);                /* ^C is a key here, not the end of it */
    mode = M_LIST;
    message[0] = 0;
    if (argc > 1 && chdir(argv[1]) < 0) say_err(argv[1], E_NOENT);
    where_am_i();
    load_conf();
    reload();
    running = 1;
    dirty = 1;

    while (running != 0) {
        code = key_read();
        while (code >= 0) {
            handle_key(code);
            code = key_read();
        }
        event = mouse_event();
        while (event != 0u) {
            if (ME_PRESSED(event) != 0u) {
                if (ME_BUTTON(event) == ME_WHEEL_UP) wheel(0);
                else if (ME_BUTTON(event) == ME_WHEEL_DOWN) wheel(1);
                else click(ME_BUTTON(event));
            }
            event = mouse_event();
        }
        if (mode == M_MENU) track_pointer();
        if (dirty != 0) {
            render();
            dirty = 0;
        }
    }
    return 0;
}
