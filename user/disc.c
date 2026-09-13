/* user/disc.c -- what is in the CD drive, and a copy of it on the disk.
 *
 *   r / enter     check the drive now      c    copy the disc to 2:/
 *   up / down     scroll                   e    eject it
 *   pgup / pgdn   page                     t    a raw disc: hex <-> text
 *   home / end    first / last             esc  quit
 *
 * Phase 6 of docs/cd-drive.md: the demo for <pigeon/cd.h>. A disc goes in
 * from the display -- "Load from server" or "Load from PC" -- and this
 * shows what it is:
 *
 *   - a disc that carries PigeonFS is mounted, read-only, and its tree is
 *     listed two levels deep. `c` copies the whole tree to 2:/<its label>.
 *   - anything else is raw bytes: a hex dump, or text when the start of it
 *     looks like text, and `t` switches between the two. `c` copies it as
 *     one file, 2:/<its name>.
 *
 * NOTHING HAS TO BE PRESSED when a disc goes in or comes out. The loop
 * watches cd_generation(), which moves on every insert and eject, and
 * redraws when it does. That is also when a mounted disc is unmounted: a
 * volume left mounted across a swap would still hold cached blocks of the
 * disc that went away.
 *
 * `e` ejects from in here, with cd_eject(), which unmounts first.
 *
 * The screen is laid out exactly as user/files.c lays it out -- the same
 * header, nine body rows, the same status line -- so tests/test_files.py's
 * reader of the screen reads this one too. Like files.c it takes every key
 * from key_read() alone, one ordered stream, and a blank hard disk is
 * formatted on first run and never again.
 *
 * Build and run:
 *   python3 start_emulator.py disc --run
 */
#include <pigeon/cd.h>
#include <pigeon/fs.h>
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/string.h>
#include <pigeon/mem.h>

/* --- layout: user/files.c's, derived the same way --------------------------- */
#define CELL      (GLYPH_W + 1)
#define COLS      (DISP_W / CELL)
#define ROW       (GLYPH_H + 1)
#define HEAD_Y    1
#define HEAD_RULE (HEAD_Y + GLYPH_H + 1)
#define LIST_Y    (HEAD_RULE + 3)
#define STATUS_Y  (DISP_H - GLYPH_H - 1)
#define FOOT_RULE (STATUS_Y - 2)
#define ROWS      ((FOOT_RULE - 1 - LIST_Y) / ROW)
#define BODY_ROWS (ROWS - 1)            /* row 0 says what the disc is */

#define BG      0xFF0A0C10
#define BAR     0xFF232936
#define ACCENT  0xFF30C0FF
#define INK     0xFFD8DEE9
#define DIM     0xFF6A7284
#define WARN    0xFFFF6B5E
#define GOOD    0xFF62D58A

/* --- what is in the drive ------------------------------------------------------ */
#define K_NODRIVE 0
#define K_EMPTY   1
#define K_RAW     2
#define K_FS      3

/* "000000  00 11 22 33 44 55 abcdef" -- exactly the 32 columns. The ASCII
 * column starts at 26: one more space and its sixth character would land
 * in row[COLS], where the terminator lives, and be drawn off the edge with
 * nothing on screen to say it was missing. */
#define HEX_PER_ROW 6u

/* A raw disc is read for its text view up to here, and no further: a disc
 * can be megabytes, and the screen shows 288 characters at a time. */
#define TEXT_MAX  (32u * 1024u)
#define MAX_WRAP  1024
#define MAX_LINES 160
#define COPY_DEPTH 4u                   /* dir handles + 2 stay under fs.c's 8 */

typedef struct { char text[COLS + 1]; } line_t;

static int       kind;
static cd_info_t disc;
static unsigned  seen_gen;
static char      label[16];
static int       top;

static int       text_mode;
static char     *text_buf;
static unsigned  text_len;
static int       text_cut;
static unsigned  wrap_at[MAX_WRAP];
static unsigned  wrap_len[MAX_WRAP];
static int       n_wrap;

static line_t    lines[MAX_LINES];
static int       n_lines;
static int       lines_cut;
static int       copied_files;

static int       hdd_ok;
static int       hdd_err;

static char      message[COLS + 1];
static color_t   msg_ink;
static int       running;
static int       dirty;

/* --- rows of text ----------------------------------------------------------------- */

static void say(char *text, color_t ink) {
    strlcpy(message, text, sizeof(message));
    msg_ink = ink;
}

/* cd_strerror() names both ranges, so one helper serves a CD_* code and
 * the FS_* codes cd_save() and the tree copy pass through. */
static void say_err(char *what, int err) {
    unsigned n = strlcpy(message, what, sizeof(message));
    if (n + 2u < sizeof(message)) {
        message[n] = ':';
        message[n + 1u] = ' ';
        strlcpy(message + n + 2u, cd_strerror(err), sizeof(message) - n - 2u);
    }
    msg_ink = WARN;
}

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

static void size_text(unsigned size, char *out) {
    if (size < 10000u) {
        out = out + utoa(size, out, 10u);
        strcpy(out, " B");
        return;
    }
    out = out + utoa((size + 1023u) / 1024u, out, 10u);
    strcpy(out, " K");
}

static void hex_digits(char *row, unsigned col, unsigned value, unsigned width) {
    unsigned i;
    unsigned d;
    for (i = 0u; i < width; i++) {
        d = (value >> ((width - 1u - i) * 4u)) & 15u;
        row[col + i] = (char)(d < 10u ? 48u + d : 87u + d);      /* '0'.., 'a'.. */
    }
}

/* "6:/" + rest, built from the channel number rather than typed, so a
 * drive moved to another channel moves this with it. */
static void vol_path(char *out, unsigned size, unsigned channel, char *rest) {
    char num[STR_UTOA_MAX];
    utoa(channel, num, 10u);
    strlcpy(out, num, size);
    strlcat(out, ":/", size);
    strlcat(out, rest, size);
}

static void join(char *out, unsigned size, char *dir, char *name) {
    unsigned n = strlcpy(out, dir, size);
    if (n > 0u && out[n - 1u] != '/') strlcat(out, "/", size);
    strlcat(out, name, size);
}

/* --- text -------------------------------------------------------------------------- */

static int looks_like_text(char *p, unsigned n) {
    unsigned i;
    unsigned c;
    for (i = 0u; i < n; i++) {
        c = (unsigned)(unsigned char)p[i];
        if (c == 0u) return 0;
        if (c < 32u && c != 9u && c != 10u && c != 13u) return 0;
    }
    return 1;
}

static int wrap_text(char *p, unsigned n) {
    int count = 0;
    unsigned i = 0u;
    unsigned start;
    unsigned len;

    while (count < MAX_WRAP) {
        start = i;
        len = 0u;
        while (i < n && p[i] != 10 && len < (unsigned)COLS) {
            i++;
            len++;
        }
        wrap_at[count] = start;
        wrap_len[count] = len;
        count++;
        if (i < n && p[i] == 10) {
            i++;
            if (i >= n) break;
        } else if (i >= n) {
            break;
        }
    }
    return count;
}

static void close_text(void) {
    if (text_buf != NULL) {
        free(text_buf);
        text_buf = NULL;
    }
    text_len = 0u;
    n_wrap = 0;
}

static int load_text(void) {
    unsigned want = disc.size;
    int n;

    close_text();
    text_cut = 0;
    if (want > TEXT_MAX) {
        want = TEXT_MAX;
        text_cut = 1;
    }
    text_buf = (char *)malloc(want + 1u);
    if (text_buf == NULL) return FS_ENOMEM;
    n = cd_read(CH_CD, 0u, text_buf, want);
    if (n < 0) {
        close_text();
        return n;
    }
    text_len = (unsigned)n;
    n_wrap = wrap_text(text_buf, text_len);
    return 0;
}

/* --- a filesystem disc --------------------------------------------------------------- */

static void add_line(char *text) {
    if (n_lines >= MAX_LINES) {
        lines_cut = 1;
        return;
    }
    strlcpy(lines[n_lines].text, text, COLS + 1);
    n_lines++;
}

/* Two levels: the root, and what is in each directory directly under it.
 * Unsorted -- readdir's order, which is the order things were made in. */
static void list_dir(char *path, unsigned depth) {
    fs_stat_t st;
    char row[COLS + 1];
    char size[16];
    char child[FS_PATH_MAX + 1];
    unsigned indent;
    unsigned n;
    int dh;

    dh = fs_opendir(path);
    if (dh < 0) {
        say_err("list", dh);
        return;
    }
    indent = 1u + depth * 2u;
    while (fs_readdir(dh, &st) == 1) {
        blank(row);
        place(row, indent, st.name);
        n = indent + strlen(st.name);
        if (st.type == FS_TYPE_DIR) {
            if (n < (unsigned)COLS) row[n] = '/';
        } else {
            size_text(st.size, size);
            place_right(row, size);
        }
        add_line(row);
        if (st.type == FS_TYPE_DIR && depth == 0u) {
            join(child, sizeof(child), path, st.name);
            list_dir(child, depth + 1u);
        }
    }
    fs_closedir(dh);
}

/* The whole tree, every level -- the listing stops at two, the copy does
 * not. Each file is loaded and saved whole: the heap is ~126 MB and a disc
 * that fits on this machine's disks fits in it. */
static int copy_tree(char *src, char *dst, unsigned depth) {
    fs_stat_t st;
    char s[FS_PATH_MAX + 1];
    char t[FS_PATH_MAX + 1];
    char *data;
    int dh;
    int n;
    int r;

    if (depth > COPY_DEPTH) return FS_ENAMETOOLONG;
    r = fs_mkdir(dst);
    if (r < 0 && r != FS_EEXIST) return r;
    dh = fs_opendir(src);
    if (dh < 0) return dh;

    r = 0;
    while (r >= 0 && fs_readdir(dh, &st) == 1) {
        join(s, sizeof(s), src, st.name);
        join(t, sizeof(t), dst, st.name);
        if (st.type == FS_TYPE_DIR) {
            r = copy_tree(s, t, depth + 1u);
        } else {
            data = (char *)malloc(st.size + 1u);
            if (data == NULL) {
                r = FS_ENOMEM;
            } else {
                n = fs_load(s, data, st.size + 1u);
                if (n < 0) {
                    r = n;
                } else {
                    r = fs_save(t, data, (unsigned)n);
                    if (r >= 0 && r != n) r = FS_ENOSPC;
                    if (r >= 0) copied_files++;
                }
                free(data);
            }
        }
    }
    fs_closedir(dh);
    return (r < 0) ? r : 0;
}

/* --- reading the drive ----------------------------------------------------------------- */

static void refresh(void) {
    char root[8];
    int r;

    /* The last disc's volume, if it was mounted. Harmless when it was not
     * (FS_ENODEV), and nothing here holds a handle between frames, so it
     * cannot be FS_EBUSY. */
    fs_unmount(CH_CD);
    close_text();
    n_lines = 0;
    lines_cut = 0;
    top = 0;
    label[0] = 0;
    text_mode = 0;

    r = cd_info(CH_CD, &disc);
    seen_gen = disc.generation;
    if (r == CD_ENODEV) {
        kind = K_NODRIVE;
        return;
    }
    if (r < 0) {
        kind = K_EMPTY;
        return;
    }

    if (cd_has_fs(CH_CD)) {
        kind = K_FS;
        cd_label(CH_CD, label, sizeof(label));
        r = fs_mount(CH_CD);
        if (r < 0) {
            say_err("mount", r);
            return;
        }
        vol_path(root, sizeof(root), CH_CD, "");
        list_dir(root, 0u);
        return;
    }

    kind = K_RAW;
    r = load_text();
    if (r < 0) {
        say_err("read", r);
        return;
    }
    text_mode = looks_like_text(text_buf, text_len < 4096u ? text_len : 4096u);
}

static void render(void);

static void copy_disc(void) {
    char dst[FS_PATH_MAX + 1];
    char src[8];
    char done[COLS + 1];
    char num[STR_UTOA_MAX];
    int r;

    if (kind != K_RAW && kind != K_FS) {
        say("nothing to copy", WARN);
        return;
    }
    if (!hdd_ok) {
        say_err("2: disk", hdd_err);
        return;
    }
    say("copying...", DIM);
    render();

    if (kind == K_RAW) {
        vol_path(dst, sizeof(dst), CH_HDD, disc.name);
        r = cd_save(CH_CD, dst);
        if (r < 0) {
            say_err("copy", r);
            return;
        }
        strlcpy(done, "copied ", sizeof(done));
        utoa((unsigned)r, num, 10u);
        strlcat(done, num, sizeof(done));
        strlcat(done, " B to ", sizeof(done));
        strlcat(done, dst, sizeof(done));
        say(done, GOOD);
        return;
    }

    vol_path(src, sizeof(src), CH_CD, "");
    vol_path(dst, sizeof(dst), CH_HDD, label[0] != 0 ? label : "disc");
    copied_files = 0;
    r = copy_tree(src, dst, 0u);
    if (r == FS_EINVAL && label[0] != 0) {
        /* A label need not be a legal file name; fall back rather than fail. */
        vol_path(dst, sizeof(dst), CH_HDD, "disc");
        copied_files = 0;
        r = copy_tree(src, dst, 0u);
    }
    if (r < 0) {
        say_err("copy", r);
        return;
    }
    strlcpy(done, "copied ", sizeof(done));
    utoa((unsigned)copied_files, num, 10u);
    strlcat(done, num, sizeof(done));
    strlcat(done, " files to ", sizeof(done));
    strlcat(done, dst, sizeof(done));
    say(done, GOOD);
}

static void eject_disc(void) {
    int r = cd_eject(CH_CD);
    /* Re-read here instead of letting the main loop notice the generation
     * move: the loop announces a change it did not cause, and this one was
     * caused on purpose. */
    refresh();
    if (r == CD_OK) say("ejected", GOOD);
    else say_err("eject", r);
}

/* --- drawing --------------------------------------------------------------------------- */

static void draw_header(void) {
    char row[COLS + 1];

    disp_rect(0, 0, DISP_W, HEAD_RULE, BAR);
    blank(row);
    if (kind == K_RAW || kind == K_FS) place(row, 0u, disc.name);
    else place(row, 0u, "CD DRIVE");
    if (kind == K_FS) place_right(row, "[PGFS]");
    else if (kind == K_RAW) place_right(row, "[raw]");
    else if (kind == K_EMPTY) place_right(row, "[empty]");
    disp_text(0, HEAD_Y, row, ACCENT);
    disp_hline(0, HEAD_RULE, DISP_W, DIM);
}

static void draw_wrapped_line(int line, unsigned y) {
    char row[COLS + 1];
    unsigned j;
    unsigned c;

    for (j = 0u; j < wrap_len[line]; j++) {
        c = (unsigned)(unsigned char)text_buf[wrap_at[line] + j];
        row[j] = (c < 32u || c > 126u) ? '.' : (char)c;
    }
    row[j] = 0;
    disp_text(0, y, row, INK);
}

static void draw_hex(void) {
    unsigned char bytes[HEX_PER_ROW * ROWS];
    char row[COLS + 1];
    unsigned offset;
    unsigned i;
    unsigned j;
    unsigned b;
    int got;
    int r;

    offset = (unsigned)top * HEX_PER_ROW;
    got = cd_read(CH_CD, offset, bytes, HEX_PER_ROW * (unsigned)BODY_ROWS);
    if (got < 0) {
        say_err("read", got);
        return;
    }
    for (r = 0; r < BODY_ROWS; r++) {
        i = (unsigned)r * HEX_PER_ROW;
        if (i >= (unsigned)got) break;
        blank(row);
        hex_digits(row, 0u, offset + i, 6u);
        for (j = 0u; j < HEX_PER_ROW && i + j < (unsigned)got; j++) {
            b = bytes[i + j];
            hex_digits(row, 8u + j * 3u, b, 2u);
            row[26u + j] = (b >= 32u && b <= 126u) ? (char)b : '.';
        }
        disp_text(0, LIST_Y + (unsigned)(r + 1) * ROW, row, INK);
    }
}

static void draw_body(void) {
    char row[COLS + 1];
    char num[STR_UTOA_MAX + 12];
    int r;
    int line;

    if (kind == K_NODRIVE) {
        disp_text(0, LIST_Y + ROW, " no CD drive on this machine", DIM);
        return;
    }
    if (kind == K_EMPTY) {
        disp_text(0, LIST_Y + ROW, " no disc in the drive", INK);
        disp_text(0, LIST_Y + 3u * ROW, " put one in from the display:", DIM);
        disp_text(0, LIST_Y + 4u * ROW, " Load from server / from PC", DIM);
        return;
    }

    blank(row);
    if (kind == K_FS) {
        place(row, 1u, "label ");
        place(row, 7u, label[0] != 0 ? label : "(none)");
        utoa((unsigned)n_lines, num, 10u);
        strlcat(num, lines_cut ? "+ entries" : " entries", sizeof(num));
        place_right(row, num);
    } else {
        size_text(disc.size, num);
        place(row, 1u, num);
        if (!text_mode) place_right(row, "hex");
        else if (text_cut) place_right(row, "text, first 32K");
        else place_right(row, "text");
    }
    disp_text(0, LIST_Y, row, ACCENT);

    if (kind == K_FS) {
        if (n_lines == 0) disp_text(0, LIST_Y + ROW, " (an empty disc)", DIM);
        for (r = 0; r < BODY_ROWS; r++) {
            line = top + r;
            if (line >= n_lines) break;
            disp_text(0, LIST_Y + (unsigned)(r + 1) * ROW, lines[line].text, INK);
        }
        return;
    }
    if (text_mode) {
        for (r = 0; r < BODY_ROWS; r++) {
            line = top + r;
            if (line >= n_wrap) break;
            draw_wrapped_line(line, LIST_Y + (unsigned)(r + 1) * ROW);
        }
        return;
    }
    draw_hex();
}

static void draw_status(void) {
    char row[COLS + 1];
    color_t ink = DIM;

    disp_hline(0, FOOT_RULE, DISP_W, DIM);
    blank(row);
    if (message[0] != 0) {
        place(row, 0u, message);
        ink = msg_ink;
    } else if (kind == K_RAW) {
        place(row, 0u, text_mode ? "c copy  e eject  t hex" : "c copy  e eject  t text");
    } else if (kind == K_FS) {
        place(row, 0u, "c copy  e eject  esc quit");
    } else {
        place(row, 0u, "r check  esc quit");
    }
    disp_text(0, STATUS_Y, row, ink);
}

/* Whole frames only; see display.h. */
static void render(void) {
    disp_clear(BG);
    draw_header();
    draw_body();
    draw_status();
    disp_present();
}

/* --- keys ------------------------------------------------------------------------------- */

static int max_top(void) {
    int total = 0;
    if (kind == K_FS) total = n_lines;
    else if (kind == K_RAW && text_mode) total = n_wrap;
    else if (kind == K_RAW) total = (int)((disc.size + HEX_PER_ROW - 1u) / HEX_PER_ROW);
    total = total - BODY_ROWS;
    return (total < 0) ? 0 : total;
}

static void handle_key(int code) {
    message[0] = 0;                     /* a message lasts until the next key */

    if (code == KEY_ESC) running = 0;
    else if (code == KEY_UP) top--;
    else if (code == KEY_DOWN) top++;
    else if (code == KEY_PGUP) top = top - BODY_ROWS;
    else if (code == KEY_PGDN) top = top + BODY_ROWS;
    else if (code == KEY_HOME) top = 0;
    else if (code == KEY_END) top = max_top();
    else if (code == 'r' || code == KEY_ENTER) {
        refresh();
        if (message[0] == 0) say("checked the drive", DIM);
    }
    else if (code == 'c') copy_disc();
    else if (code == 'e') eject_disc();
    else if (code == 't' && kind == K_RAW) {
        text_mode = !text_mode;
        top = 0;
    }

    if (top > max_top()) top = max_top();
    if (top < 0) top = 0;
    dirty = 1;
}

int main(void) {
    unsigned gen;
    int code;
    int r;

    disp_use_back_buffer();
    message[0] = 0;
    text_buf = NULL;

    /* A blank disk is formatted, once, with no force flag -- the rule
     * user/files.c follows. A disk holding anything else is left alone,
     * and copies then say why they cannot run. */
    r = fs_mount(CH_HDD);
    if (r == FS_ENOFS) {
        r = fs_format(CH_HDD, "PIGEON", 0u);
        if (r >= 0) {
            r = fs_mount(CH_HDD);
            if (r >= 0) say("formatted the blank 2: disk", GOOD);
        }
    }
    hdd_ok = (r >= 0) ? 1 : 0;
    hdd_err = r;

    refresh();
    running = 1;
    dirty = 1;

    while (running) {
        code = key_read();
        while (code >= 0) {
            handle_key(code);
            code = key_read();
        }
        /* One IO command a pass. A disc the display put in or took out
         * shows up here, with no key pressed. */
        gen = cd_generation(CH_CD);
        if (gen != seen_gen) {
            /* Newer than whatever the status line was holding -- except an
             * error from reading the new disc, which is newer still. */
            message[0] = 0;
            refresh();
            if (message[0] == 0) say("the disc changed", DIM);
            dirty = 1;
        }
        if (dirty) {
            render();
            dirty = 0;
        }
    }

    close_text();
    fs_unmount(CH_CD);
    disp_clear(BG);
    disp_present();
    return 0;
}
