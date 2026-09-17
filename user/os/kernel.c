/* The PigeonOS kernel (docs/kernel.md §11, docs/kernel_exec.md).
 *
 * Built for PROGRAM_LOAD_ADDR and installed as the project's system,
 * /boot.bin, which the boot sector loads. It mounts the disk it booted
 * from, fills in the system-call and vector tables, and runs what
 * /etc/boot.conf names: a splash screen, then the startup program -- again
 * whenever it ends -- which is /bin/sh.bin when there is no boot.conf
 * (docs/phase6_plan.md).
 *
 * A program is a program file (compiler/program_file.py). exec loads it
 * just above the program that started it, patches it for where it landed,
 * and calls it like a function, through exec_call in kernel.asm. It ends by
 * returning from main, or by exit(), a fault or Ctrl+C, which all come back
 * through exec_abort: the stack is put back as exec_call left it. Then the
 * kernel tidies up after it.
 *
 * The console lives here, so every program's output lands on one 32x12
 * grid of text, which the kernel keeps and redraws after a program in case
 * it drew over the screen. Typing a line -- echo, backspace, Enter -- is
 * the console's too, so read(STDIN) hands a program a finished line.
 *
 * Nothing is protected: a program can overwrite the kernel.
 *
 * What it does is said on the debug port, each line starting "[kernel] "
 * (docs/phase5b_plan.md step 4): starting, the disk it mounted, every exec
 * and how each program ended, and a panic. Never through printf, which
 * would call this kernel's own system calls -- and nothing of the console.
 */
#include <pigeon/debug.h>
#include <pigeon/display.h>
#include <pigeon/fs.h>
#include <pigeon/input.h>
#include <pigeon/io.h>
#include <pigeon/mem.h>
#include <pigeon/string.h>
#include <pigeon/syscall.h>
#asm "kernel.asm"

#define SHELL         "/bin/sh.bin"
#define BOOT_CONF     "/etc/boot.conf"
#define BOOT_CONF_MAX 1024u         /* bytes, as the shell's prompt file      */
#define SPLASH_MS     2500u         /* the splash's length when boot.conf gives none */
#define SPLASH_MS_MAX 60000u
#define PROGRAMS      0x01000000u   /* where the first program goes          */
#define PROGRAMS_TOP  0x07F00000u   /* the hardware stack's megabyte above   */
#define MIN_HEAP      0x10000u      /* room a program must have past its frames */
#define CAPTURE_MAX   0x10000u      /* the most exec_out will write into a buffer */
#define MAX_DEPTH     8             /* the kernel, and 7 programs in a chain */
#define HANDLES       16            /* fs.c has 8; room to spare             */
#define TIMERS        16u           /* timer ids stopped after a program      */

/* How a program ended, for its line on the debug port. */
#define K_HOW_RETURN  0u
#define K_HOW_EXIT    1u
#define K_HOW_FAULT   2u
#define K_HOW_BREAK   3u
#define K_HOW_QUIT    4u

#define K_TIMER_STOP        2u
#define K_DISPLAY_SET_BASE  2u
#define K_HID_SET_BREAK     8u
#define K_KEY_LCTRL         0x8Bu
#define K_KEY_RCTRL         0x8Cu

/* The console: 32 columns by 12 rows, DISP_W / (GLYPH_W + 1) by
 * DISP_H / (GLYPH_H + 1) -- a column and a row of space between cells. */
#define CON_COLS   32u
#define CON_ROWS   12u
#define CON_CELL_W 6u
#define CON_CELL_H 9u
#define CON_INK    0xFFD8D8D8u
#define CON_BG     BLACK
#define CON_DEFAULT  8u             /* con_palette's own ink                    */
#define CON_INVERSE  0x10u          /* a cell's ink as its background           */
#define CON_ESC_NONE 0u
#define CON_ESC_SEEN 1u             /* ESC, waiting for '['                     */
#define CON_ESC_CSI  2u             /* ESC [, reading numbers until a letter    */
#define CON_ESC_OSC  3u             /* ESC ], reading text until BEL or ESC \   */
#define CON_ESC_OSC_END 4u          /* ESC inside ESC ]: the end, with its \    */
#define CON_OSC_MAX  64u            /* an ESC ] that never ends stops here      */

/* Line input (docs/phase4b_plan.md step 2). */
#define LINE_MAX     255u           /* characters in a typed line, as the shell's */
#define HISTORY      16u            /* lines Up and Down step through            */
#define HISTORY_LINE 256u
#define COMPLETE_TEXT 64u           /* setcomplete's directory and built-ins, each */
#define MATCHES      64u            /* names Tab considers, of up to 31 bytes      */
#define SCROLLBACK   100u           /* rows kept after they scroll off the top     */
#define PAGE_ROWS    11u            /* PgUp and PgDn: a screen, less a row to keep */
#define WHEEL_ROWS   3u             /* a wheel notch                               */

typedef int  (*call4_fn)(unsigned, int, char **, unsigned *);
typedef void (*abort_fn)(int, unsigned *);
typedef void (*void_fn)(void);

extern unsigned __heap_limit;       /* where mem.c stops this heap (Q7) */

extern int kinit;
extern int khalt;
extern int exec_call;
extern int exec_abort;
extern int w_write;
extern int w_read;
extern int w_open;
extern int w_close;
extern int w_opendir;
extern int w_readdir;
extern int w_closedir;
extern int w_stat;
extern int w_chdir;
extern int w_getcwd;
extern int w_exec;
extern int w_exec_out;
extern int w_exec_io;
extern int w_exit;
extern int w_getkey;
extern int w_mkdir;
extern int w_rmdir;
extern int w_remove;
extern int w_rename;
extern int w_setcomplete;
extern int w_setbreak;
extern int w_paging;
extern int w_keepscreen;
extern int kswallow;
extern int fault_div;
extern int fault_opcode;
extern int fault_fetch;
extern int on_break;
extern int irq_ignore;

struct proc {
    unsigned base;          /* where its image starts                     */
    unsigned heap_ptr_at;   /* its __heap_ptr: how far its heap has grown */
    unsigned save_sp;       /* exec_call's stack pointer and F, for exec_abort */
    unsigned save_f;
    unsigned breaks;        /* 1: Ctrl+C is its break; setbreak turns it off */
    unsigned how;           /* K_HOW_*: how it ended, set where it did      */
    unsigned fault_pc;      /* the instruction it faulted on                */
    char path[256];         /* FS_PATH_MAX + 1: as exec was given it        */
};

struct proc procs[MAX_DEPTH];
int depth;                      /* programs running; 0 is the kernel alone   */
unsigned in_kernel;             /* 1 while kernel code runs (kernel.asm)     */
unsigned started;               /* the last exec got as far as the program   */
unsigned mounted;               /* the channel of the disk the kernel uses   */
unsigned banner_shown;          /* 1 once PigeonOS is on the console         */
char boot_splash[256];          /* boot.conf's splash, or "" for none        */
char boot_startup[256];         /* boot.conf's startup, or SHELL             */
unsigned boot_splash_ms;
unsigned vectors[VECTOR_COUNT];
unsigned fault_frames[256];     /* the frame stack k_fault runs on           */
int handle_depth[HANDLES];      /* the program an fs handle is open for; 0 none */
unsigned handle_dir[HANDLES];   /* 1 if that handle is a directory's         */

char con_grid[384];             /* CON_ROWS * CON_COLS characters            */
char con_look[384];             /* each cell's ink, and CON_INVERSE          */
unsigned con_row;
unsigned con_col;               /* CON_COLS: the row is full, wrap before the next */
unsigned con_top;               /* the rows that scroll, ESC [ t ; b r: all 12 */
unsigned con_bottom;            /* unless a program such as edit sets fewer   */
unsigned con_attr;              /* the look new characters get               */
unsigned con_esc;               /* CON_ESC_*: partway through a sequence     */
unsigned con_arg[4];            /* its numbers, as far as it has got         */
unsigned con_args;
char con_osc[16];               /* the start of an ESC ] sequence's text     */
unsigned con_osc_len;
unsigned con_marked;            /* 1: con_mark_row is where a prompt starts  */
unsigned con_mark_row;

char history[4096];             /* HISTORY lines of HISTORY_LINE, oldest first */
unsigned history_count;
char line_typed[256];           /* the line being typed, while Up shows another */
unsigned line_row;              /* where the line being typed starts         */
unsigned line_col;

char complete_dir[512];         /* MAX_DEPTH x COMPLETE_TEXT: where each      */
char complete_builtins[512];    /* program's commands are, from setcomplete  */
char match_names[2048];         /* MATCHES x 32: the names Tab found          */
unsigned match_dir[64];         /* 1: that name is a directory's              */
unsigned match_count;
unsigned list_row;              /* Tab's list under the line: its first row   */
unsigned list_rows;             /* and how many; 0 when none is showing       */

char back_grid[3200];           /* SCROLLBACK x CON_COLS: the rows that scrolled off, */
char back_look[3200];           /* a ring with back_next where the next one goes      */
unsigned back_count;
unsigned back_next;
unsigned view_back;             /* rows the view is scrolled back; 0 is the screen    */

/* A capture (exec_out): while out_buf is set, what the program at out_depth
 * or deeper writes to STDOUT goes there instead of the console. Every
 * exec_out puts back the capture it interrupted, so captures nest. */
char *out_buf;                  /* NULL when nothing is captured             */
unsigned out_size;              /* its room, the terminator included         */
unsigned out_len;               /* what is in it                             */
int out_depth;                  /* the shallowest program it covers          */

/* A redirection (exec_to, exec_from): the same idea with a file on the end
 * of it. The innermost of a capture and a redirection is the one that takes
 * the output, which is what `$(cmd > file)` would mean if pgs allowed it. */
int out_file;                   /* STDOUT into this handle, or -1            */
int out_file_depth;
int in_file;                    /* STDIN out of this one, a line at a time   */
int in_file_depth;

unsigned page_owner;            /* the program that turned paging on; 0 none */
unsigned screen_owner;          /* the program keeping the screen; 0 none */
unsigned page_rows;             /* rows the output moved down since the last wait */
unsigned page_counting;         /* 1 while a write's rows count              */

/* Inks 0 to 7 are the ANSI colors, in ANSI's order; 8 is the console's own. */
color_t con_palette[9] = {BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE, CON_INK};

/* --- devices ------------------------------------------------------------ */

static void k_io(unsigned channel, unsigned command, unsigned length, unsigned address) {
    IO_RW = 0u;
    IO_CMD = command;
    IO_LEN = length;
    IO_ADDR = address;
    IO_CH = channel;            /* this store fires it -- must be last */
}

/* Ctrl+C as the break, for the program running and only while one runs
 * (docs/phase4_plan.md step 10). Turning it off drops a break HID raised
 * and the machine hasn't taken. Turning it on first takes, and ignores, a
 * break already pending in the CPU: one pressed while the kernel was busy,
 * which no program asked for. */
static void k_break(unsigned on) {
    if (on != 0u) ((void_fn)&kswallow)();
    k_io(CH_HID, K_HID_SET_BREAK, 0u, on);
}

/* --- the console ------------------------------------------------------- */

/* A character in a look, at a cell on a background already cleared: in its
 * ink, or, inverse, the ink as the background and the character cut out of
 * it. */
static void con_paint(unsigned row, unsigned col, int ch, unsigned look) {
    unsigned x = col * CON_CELL_W;
    unsigned y = row * CON_CELL_H;
    color_t ink = con_palette[look & 15u];
    if ((look & CON_INVERSE) != 0u) {
        disp_rect(x, y, CON_CELL_W, CON_CELL_H, ink);
        disp_char(x, y, ch, CON_BG);
    } else {
        disp_char(x, y, ch, ink);
    }
}

/* A cell of the screen, on a background already cleared. */
static void con_draw(unsigned row, unsigned col) {
    con_paint(row, col, (int)con_grid[row * CON_COLS + col], (unsigned)con_look[row * CON_COLS + col]);
}

static void con_cell(unsigned row, unsigned col) {
    disp_rect(col * CON_CELL_W, row * CON_CELL_H, CON_CELL_W, CON_CELL_H, CON_BG);
    con_draw(row, col);
}

static void con_redraw(void) {
    unsigned row;
    unsigned col;
    unsigned i;
    disp_clear(CON_BG);
    for (row = 0u; row < CON_ROWS; row++) {
        for (col = 0u; col < CON_COLS; col++) {
            i = row * CON_COLS + col;
            if (con_grid[i] != ' ' || ((unsigned)con_look[i] & CON_INVERSE) != 0u)
                con_draw(row, col);
        }
    }
}

/* Every cell a space in the console's own ink, and the cursor home. The
 * look new characters get stays as it was. */
static void con_clear(void) {
    unsigned i;
    for (i = 0u; i < CON_ROWS * CON_COLS; i++) {
        con_grid[i] = ' ';
        con_look[i] = (char)CON_DEFAULT;
    }
    con_row = 0u;
    con_col = 0u;
    con_marked = 0u;
    con_redraw();
}

/* The top n rows, about to scroll off the whole screen, kept in the
 * scrollback: the last SCROLLBACK of them (docs/phase4b_plan.md step 4). */
static void back_keep(unsigned n) {
    unsigned r;
    for (r = 0u; r < n; r++) {
        memcpy((void *)(back_grid + back_next * CON_COLS), (void *)(con_grid + r * CON_COLS), CON_COLS);
        memcpy((void *)(back_look + back_next * CON_COLS), (void *)(con_look + r * CON_COLS), CON_COLS);
        back_next = (back_next + 1u) % SCROLLBACK;
        if (back_count < SCROLLBACK) back_count++;
    }
}

/* Rows top to bottom move n rows up, or down, on the grid and on the
 * screen, and the rows they leave are blank. The pixels are moved rather
 * than the text drawn again (docs/phase4b_plan.md step 1), and blank rows
 * need no drawing: disp_scroll leaves them the background. */
static void con_scroll(unsigned top, unsigned bottom, unsigned n, unsigned up) {
    unsigned rows = bottom + 1u - top;
    unsigned kept;
    unsigned blank;
    int dy;
    if (n > rows) n = rows;
    kept = rows - n;
    if (up != 0u && top == 0u && bottom == CON_ROWS - 1u) back_keep(n);
    if (up != 0u) {
        memmove((void *)(con_grid + top * CON_COLS), (void *)(con_grid + (top + n) * CON_COLS), kept * CON_COLS);
        memmove((void *)(con_look + top * CON_COLS), (void *)(con_look + (top + n) * CON_COLS), kept * CON_COLS);
        blank = top + kept;
        dy = 0 - (int)(n * CON_CELL_H);
    } else {
        memmove((void *)(con_grid + (top + n) * CON_COLS), (void *)(con_grid + top * CON_COLS), kept * CON_COLS);
        memmove((void *)(con_look + (top + n) * CON_COLS), (void *)(con_look + top * CON_COLS), kept * CON_COLS);
        blank = top;
        dy = (int)(n * CON_CELL_H);
    }
    memset((void *)(con_grid + blank * CON_COLS), ' ', n * CON_COLS);
    memset((void *)(con_look + blank * CON_COLS), (int)CON_DEFAULT, n * CON_COLS);
    disp_scroll(top * CON_CELL_H, rows * CON_CELL_H, dy, CON_BG);

    /* What points at rows moves with them: the line being typed, and the
     * prompt's mark, which is lost when its row scrolls away. */
    if (up != 0u && line_row >= top && line_row <= bottom)
        line_row = line_row >= top + n ? line_row - n : top;
    if (con_marked != 0u && con_mark_row >= top && con_mark_row <= bottom) {
        if (up != 0u && con_mark_row >= top + n) con_mark_row = con_mark_row - n;
        else if (up == 0u && con_mark_row + n <= bottom) con_mark_row = con_mark_row + n;
        else con_marked = 0u;
    }
}

/* The next row: on the scrolling rows' last one, they scroll; below them,
 * on the screen's last row, the cursor stays, as on a terminal. */
static void con_newline(void) {
    if (page_counting != 0u) page_rows++;
    con_col = 0u;
    if (con_row == con_bottom) {
        con_scroll(con_top, con_bottom, 1u, 1u);
        return;
    }
    if (con_row + 1u < CON_ROWS) con_row++;
}

/* One character of an escape sequence, after the ESC: '[', then numbers
 * separated by ';', then a letter (docs/phase4_plan.md step 3). The console
 * knows m, J, H and K, and r, S and T for scrolling some of the rows
 * (docs/phase4b_plan.md step 1). Anything else ends the sequence and is dropped, never
 * printed, and so are the other parameter bytes, such as the '?' of
 * ESC [ ? 25 l. The state lives here between calls, so a sequence may come
 * in two writes. */
/* One character of an ESC ] sequence's text, which ends at a BEL or at
 * ESC \, or is given up after CON_OSC_MAX characters so a stray ESC ]
 * can't swallow everything after it. The console knows one: ESC ] 133 ; A,
 * the mark a shell prints where its prompt starts, which Ctrl+L goes by
 * (docs/phase4b_plan.md step 2). Any other is dropped. */
static void con_osc_char(int c) {
    if (c == 7 || con_esc == CON_ESC_OSC_END) {
        con_esc = CON_ESC_NONE;
        con_osc[con_osc_len < 15u ? con_osc_len : 15u] = 0;
        if (con_osc_len < 15u && strcmp(con_osc, "133;A") == 0) {
            con_marked = 1u;
            con_mark_row = con_row;
        }
        return;
    }
    if (c == 27) {
        con_esc = CON_ESC_OSC_END;
        return;
    }
    if (con_osc_len < 15u) con_osc[con_osc_len] = (char)c;
    con_osc_len++;
    if (con_osc_len >= CON_OSC_MAX) con_esc = CON_ESC_NONE;
}

static void con_escape(int c) {
    unsigned i;
    unsigned n;
    if (con_esc == CON_ESC_OSC || con_esc == CON_ESC_OSC_END) {
        con_osc_char(c);
        return;
    }
    if (con_esc == CON_ESC_SEEN) {
        con_esc = CON_ESC_NONE;
        if (c == '[') {
            con_esc = CON_ESC_CSI;
            con_args = 0u;
            con_arg[0] = 0u;
        } else if (c == ']') {
            con_esc = CON_ESC_OSC;
            con_osc_len = 0u;
        }
        return;
    }
    if (c >= '0' && c <= '9') {
        if (con_args == 0u) con_args = 1u;
        if (con_arg[con_args - 1u] < 1000u)
            con_arg[con_args - 1u] = con_arg[con_args - 1u] * 10u + (unsigned)(c - '0');
        return;
    }
    if (c == ';') {
        if (con_args == 0u) con_args = 1u;
        if (con_args < 4u) {
            con_arg[con_args] = 0u;
            con_args++;
        }
        return;
    }
    if (c >= 0x20 && c <= 0x3F) return;
    con_esc = CON_ESC_NONE;

    if (c == 'm') {
        if (con_args == 0u) con_args = 1u;
        for (i = 0u; i < con_args; i++) {
            n = con_arg[i];
            if (n == 0u) con_attr = CON_DEFAULT;
            else if (n == 7u) con_attr = con_attr | CON_INVERSE;
            else if (n == 27u) con_attr = con_attr & 15u;
            else if (n >= 30u && n <= 37u) con_attr = (con_attr & CON_INVERSE) | (n - 30u);
            else if (n == 39u) con_attr = (con_attr & CON_INVERSE) | CON_DEFAULT;
        }
    } else if (c == 'J') {
        if (con_args > 0u && con_arg[0] == 2u) con_clear();
        if (con_args > 0u && con_arg[0] == 3u) {        /* ESC [ 3 J: the scrollback */
            back_count = 0u;
            back_next = 0u;
        }
    } else if (c == 'H') {
        n = (con_args > 0u && con_arg[0] > 0u) ? con_arg[0] : 1u;
        i = (con_args > 1u && con_arg[1] > 0u) ? con_arg[1] : 1u;
        if (n > CON_ROWS) n = CON_ROWS;
        if (i > CON_COLS) i = CON_COLS;
        con_row = n - 1u;
        con_col = i - 1u;
    } else if (c == 'K') {
        for (i = con_col; i < CON_COLS; i++) {
            con_grid[con_row * CON_COLS + i] = ' ';
            con_look[con_row * CON_COLS + i] = (char)con_attr;
            con_cell(con_row, i);
        }
    } else if (c == 'r') {
        /* ESC [ t ; b r: rows t to b scroll, counting from 1; ESC [ r, all
         * of them. The cursor goes home, as on a VT100. */
        n = (con_args > 0u && con_arg[0] > 0u) ? con_arg[0] : 1u;
        i = (con_args > 1u && con_arg[1] > 0u) ? con_arg[1] : CON_ROWS;
        if (i > CON_ROWS) i = CON_ROWS;
        if (n < i) {
            con_top = n - 1u;
            con_bottom = i - 1u;
            con_row = 0u;
            con_col = 0u;
        }
    } else if (c == 'S' || c == 'T') {
        n = (con_args > 0u && con_arg[0] > 0u) ? con_arg[0] : 1u;
        con_scroll(con_top, con_bottom, n, (unsigned)(c == 'S'));
    }
}

static void con_put(int c) {
    if (con_esc != CON_ESC_NONE) {
        con_escape(c);
        return;
    }
    if (c == 27) {
        con_esc = CON_ESC_SEEN;
        return;
    }
    if (c == '\n') {
        con_newline();
        return;
    }
    if (c == '\r') return;
    if (c == '\t') {
        con_put(' ');
        while (con_col % 4u != 0u && con_col < CON_COLS) con_put(' ');
        return;
    }
    if (c < 32 || c > 126) c = '?';
    if (con_col == CON_COLS) con_newline();
    con_grid[con_row * CON_COLS + con_col] = (char)c;
    con_look[con_row * CON_COLS + con_col] = (char)con_attr;
    con_cell(con_row, con_col);
    con_col++;
}

static void con_puts(char *s) {
    while (*s != 0) {
        con_put((int)*s);
        s++;
    }
}

static void con_number(unsigned v, unsigned base) {
    char digits[STR_UTOA_MAX];
    utoa(v, digits, base);
    con_puts(digits);
}

/* --- typing a line ------------------------------------------------------ */

/* The console's position for character i of the line being typed, which
 * starts at line_row, line_col and wraps at CON_COLS. A position below the
 * scrolling rows scrolls them first, and line_row moves up with them. */
static void line_place(unsigned i) {
    unsigned cell = line_col + i;
    unsigned row = line_row + cell / CON_COLS;
    if (row > con_bottom) {
        con_scroll(con_top, con_bottom, row - con_bottom, 1u);
        row = line_row + cell / CON_COLS;
    }
    if (row >= CON_ROWS) row = CON_ROWS - 1u;
    con_row = row;
    con_col = cell % CON_COLS;
}

/* Just past the line's last character, as con_put leaves a row it filled:
 * CON_COLS, so a newline after it doesn't leave a blank row. */
static void line_end(unsigned n) {
    if (n > 0u && (line_col + n) % CON_COLS == 0u) {
        line_place(n - 1u);
        con_col = CON_COLS;
        return;
    }
    line_place(n);
}

/* The cursor, an underscore over character i, on or off. */
static void line_cursor(unsigned i, unsigned on) {
    line_place(i);
    con_cell(con_row, con_col);
    if (on != 0u) disp_char(con_col * CON_CELL_W, con_row * CON_CELL_H, '_', CON_INK);
}

/* The line from character `from` to its end drawn again, and the cells
 * after it blanked up to `old`, where a longer line ended: after a change,
 * only what moved is drawn. */
static void line_draw(char *buf, unsigned from, unsigned n, unsigned old) {
    unsigned i;
    for (i = from; i < n || i < old; i++) {
        line_place(i);
        con_grid[con_row * CON_COLS + con_col] = i < n ? buf[i] : ' ';
        con_look[con_row * CON_COLS + con_col] = (char)con_attr;
        con_cell(con_row, con_col);
    }
}

/* A line from history, or the one being typed, into buf: its length. */
static unsigned line_load(char *buf, char *from, unsigned max) {
    unsigned n = 0u;
    while (from[n] != 0 && n < max) {
        buf[n] = from[n];
        n++;
    }
    return n;
}

/* A line typed kept for Up, unless it repeats the last one. The oldest
 * goes when HISTORY are kept. */
static void history_add(char *line, unsigned n) {
    char *last;
    if (history_count > 0u) {
        last = history + (history_count - 1u) * HISTORY_LINE;
        if (strlen(last) == n && memcmp((void *)last, (void *)line, n) == 0) return;
    }
    if (history_count == HISTORY) {
        memmove((void *)history, (void *)(history + HISTORY_LINE), (HISTORY - 1u) * HISTORY_LINE);
        history_count--;
    }
    memcpy((void *)(history + history_count * HISTORY_LINE), (void *)line, n);
    history[history_count * HISTORY_LINE + n] = 0;
    history_count++;
}

static unsigned con_blank_row(unsigned row) {
    unsigned i;
    for (i = 0u; i < CON_COLS; i++) {
        if (con_grid[row * CON_COLS + i] != ' '
                || ((unsigned)con_look[row * CON_COLS + i] & CON_INVERSE) != 0u) return 0u;
    }
    return 1u;
}

/* --- Tab (docs/phase4b_plan.md step 3) ------------------------------------ */

/* A name Tab could complete to, kept unless it's there already or MATCHES
 * are. */
static void match_add(char *name, unsigned n, unsigned dir) {
    unsigned i;
    char *slot;
    if (n == 0u || n > 31u || match_count == MATCHES) return;
    for (i = 0u; i < match_count; i++) {
        slot = match_names + i * 32u;
        if (strlen(slot) == n && memcmp((void *)slot, (void *)name, n) == 0) return;
    }
    slot = match_names + match_count * 32u;
    memcpy((void *)slot, (void *)name, n);
    slot[n] = 0;
    match_dir[match_count] = dir;
    match_count++;
}

/* The names in a directory that start with prefix: every one, or with
 * commands on, the .bin files, without their .bin. */
static void match_entries(char *path, char *prefix, unsigned plen, unsigned commands) {
    fs_stat_t st;
    unsigned n;
    int dh = fs_opendir(path);
    if (dh < 0) return;                     /* none, or fs.c's handles all open */
    while (fs_readdir(dh, &st) == 1) {
        n = strlen(st.name);
        if (n < plen || memcmp((void *)st.name, (void *)prefix, plen) != 0) continue;
        if (commands == 0u) {
            match_add(st.name, n, st.type == S_DIR ? 1u : 0u);
        } else if (st.type == S_FILE && n > 4u && n - 4u >= plen
                   && strcmp(st.name + n - 4u, ".bin") == 0) {
            match_add(st.name, n - 4u, 0u);
        }
    }
    fs_closedir(dh);
}

/* The matches in name order, for the list. */
static void match_sort(void) {
    char held[32];
    unsigned dir;
    unsigned i;
    unsigned j;
    for (i = 1u; i < match_count; i++) {
        memcpy((void *)held, (void *)(match_names + i * 32u), 32u);
        dir = match_dir[i];
        j = i;
        while (j > 0u && strcmp(match_names + (j - 1u) * 32u, held) > 0) {
            memcpy((void *)(match_names + j * 32u), (void *)(match_names + (j - 1u) * 32u), 32u);
            match_dir[j] = match_dir[j - 1u];
            j--;
        }
        memcpy((void *)(match_names + j * 32u), (void *)held, 32u);
        match_dir[j] = dir;
    }
}

/* Tab: the word before the cursor completed, as far as the names that
 * match it agree. A word is split off at spaces outside double quotes, as
 * the shell splits. The first word, when the program named its commands
 * with setcomplete, matches those; any other word, or one with a '/',
 * matches the names in its directory. One match gets a '/' after a
 * directory and a space after anything else, and a name with a space
 * comes back inside quotes. Returns 1 when several names matched and there
 * was nothing to add, so a second Tab lists them. */
static unsigned line_tab(char *buf, unsigned *np, unsigned *curp, unsigned max) {
    char word[256];
    char path[256];
    char add[300];
    unsigned n = *np;
    unsigned cur = *curp;
    unsigned start = 0u;            /* where the word starts in buf   */
    unsigned first = 1u;            /* it is the line's first word    */
    unsigned quoted = 0u;
    unsigned inside = 0u;
    unsigned w = 0u;                /* the word, quotes left out      */
    unsigned slash = 0u;            /* its directory: up to its last / */
    unsigned plen;
    unsigned common;
    unsigned len;
    unsigned quote;
    unsigned i;
    char *dir;
    char *b;

    for (i = 0u; i < cur; i++) {
        if (buf[i] == '"') inside = 1u - inside;
        if (buf[i] == ' ' && inside == 0u) start = i + 1u;
    }
    for (i = 0u; i < start; i++) {
        if (buf[i] != ' ') first = 0u;
    }
    for (i = start; i < cur; i++) {
        if (buf[i] == '"') {
            quoted = 1u;
            continue;
        }
        word[w] = buf[i];
        w++;
        if (buf[i] == '/') slash = w;
    }
    word[w] = 0;
    plen = w - slash;

    match_count = 0u;
    dir = complete_dir + (unsigned)depth * COMPLETE_TEXT;
    if (first != 0u && slash == 0u && dir[0] != 0) {
        b = complete_builtins + (unsigned)depth * COMPLETE_TEXT;
        while (*b != 0) {                   /* the built-ins, between spaces */
            while (*b == ' ') b++;
            len = 0u;
            while (b[len] != 0 && b[len] != ' ') len++;
            if (len >= plen && memcmp((void *)b, (void *)word, plen) == 0) match_add(b, len, 0u);
            b = b + len;
        }
        match_entries(dir, word, plen, 1u);
    } else {
        strlcpy(path, ".", 256u);
        if (slash > 0u) {
            memcpy((void *)path, (void *)word, slash);
            path[slash] = 0;
            if (slash > 1u && path[slash - 2u] != ':') path[slash - 1u] = 0;   /* "/docs/", not "/" or "2:/" */
        }
        match_entries(path, word + slash, plen, 0u);
    }
    if (match_count == 0u) return 0u;
    match_sort();

    common = strlen(match_names);           /* the start every match shares */
    for (i = 1u; i < match_count; i++) {
        len = 0u;
        while (len < common && match_names[i * 32u + len] == match_names[len]) len++;
        common = len;
    }
    if (match_count > 1u && common == plen) return 1u;

    quote = quoted;
    for (i = 0u; i < slash; i++) {
        if (word[i] == ' ') quote = 1u;
    }
    for (i = 0u; i < common; i++) {
        if (match_names[i] == ' ') quote = 1u;
    }
    len = 0u;
    if (quote != 0u) {
        add[len] = '"';
        len++;
    }
    memcpy((void *)(add + len), (void *)word, slash);
    len = len + slash;
    memcpy((void *)(add + len), (void *)match_names, common);
    len = len + common;
    if (match_count == 1u && match_dir[0] != 0u) {
        add[len] = '/';
        len++;
    } else if (match_count == 1u) {
        if (quote != 0u) {
            add[len] = '"';
            len++;
        }
        add[len] = ' ';
        len++;
    }
    if (n - (cur - start) + len > max) return 0u;
    memmove((void *)(buf + start + len), (void *)(buf + cur), n - cur);
    memcpy((void *)(buf + start), (void *)add, len);
    *np = n - (cur - start) + len;
    *curp = start + len;
    return 0u;
}

/* The matches listed under the line, in columns, as zsh does. The screen
 * scrolls up to make room, and a list taller than the room left ends with
 * how many more there are. The next key clears it (line_unlist). */
static void line_list(unsigned n) {
    unsigned width = 0u;
    unsigned cols;
    unsigned rows;
    unsigned room;
    unsigned shown;
    unsigned extra;                         /* the line's rows after its first */
    unsigned i;
    unsigned r;
    unsigned c;
    char *name;
    for (i = 0u; i < match_count; i++) {
        c = strlen(match_names + i * 32u) + match_dir[i];
        if (c > width) width = c;
    }
    width = width + 2u;
    if (width > CON_COLS) width = CON_COLS;
    cols = CON_COLS / width;
    rows = (match_count + cols - 1u) / cols;
    extra = (line_col + n) / CON_COLS;
    if (con_bottom - con_top <= extra) return;
    room = con_bottom - con_top - extra;
    list_rows = rows > room ? room : rows;
    shown = rows > room ? room - 1u : rows;
    if (line_row + extra + list_rows > con_bottom)
        con_scroll(con_top, con_bottom, line_row + extra + list_rows - con_bottom, 1u);
    list_row = line_row + extra + 1u;
    for (r = 0u; r < shown; r++) {
        for (c = 0u; c < cols && r * cols + c < match_count; c++) {
            i = r * cols + c;
            name = match_names + i * 32u;
            con_row = list_row + r;
            con_col = c * width;
            con_puts(name);
            if (match_dir[i] != 0u) con_put('/');
        }
    }
    if (shown < rows) {
        con_row = list_row + shown;
        con_col = 0u;
        con_puts("and ");
        con_number(match_count - shown * cols, 10u);
        con_puts(" more");
    }
}

static void line_unlist(void) {
    unsigned r;
    for (r = 0u; r < list_rows; r++) {
        memset((void *)(con_grid + (list_row + r) * CON_COLS), ' ', CON_COLS);
        memset((void *)(con_look + (list_row + r) * CON_COLS), (int)CON_DEFAULT, CON_COLS);
        disp_rect(0u, (list_row + r) * CON_CELL_H, DISP_W, CON_CELL_H, CON_BG);
    }
    list_rows = 0u;
}

/* --- looking back (docs/phase4b_plan.md step 4) ----------------------------- */

/* Where row v of the view comes from, `back` rows back: the scrollback's
 * rows, oldest first, run on into the screen's. */
static char *view_grid(unsigned back, unsigned v) {
    unsigned i = back_count - back + v;
    if (i < back_count) return back_grid + ((back_next + SCROLLBACK - back_count + i) % SCROLLBACK) * CON_COLS;
    return con_grid + (i - back_count) * CON_COLS;
}

static char *view_look(unsigned back, unsigned v) {
    unsigned i = back_count - back + v;
    if (i < back_count) return back_look + ((back_next + SCROLLBACK - back_count + i) % SCROLLBACK) * CON_COLS;
    return con_look + (i - back_count) * CON_COLS;
}

/* Row v of the view drawn, from column `from` to the end. */
static void view_row(unsigned back, unsigned v, unsigned from) {
    char *grid = view_grid(back, v);
    char *look = view_look(back, v);
    unsigned col;
    disp_rect(from * CON_CELL_W, v * CON_CELL_H, (CON_COLS - from) * CON_CELL_W, CON_CELL_H, CON_BG);
    for (col = from; col < CON_COLS; col++) {
        if (grid[col] != ' ' || ((unsigned)look[col] & CON_INVERSE) != 0u)
            con_paint(v, col, (int)grid[col], (unsigned)look[col]);
    }
}

/* The view moved to `back` rows back. The pixels scroll and only the rows
 * that come into view are drawn, then the marker in the top-right corner
 * that says how far back it is. */
static void view_move(unsigned back) {
    char mark[12];
    unsigned d;
    unsigned v;
    unsigned len;
    unsigned i;
    if (back > back_count) back = back_count;
    if (back == view_back) return;
    d = back > view_back ? back - view_back : view_back - back;
    if (d >= CON_ROWS) {
        for (v = 0u; v < CON_ROWS; v++) view_row(back, v, 0u);
    } else if (back > view_back) {          /* further back: the rows move down */
        disp_scroll(0u, CON_ROWS * CON_CELL_H, (int)(d * CON_CELL_H), CON_BG);
        for (v = 0u; v < d; v++) view_row(back, v, 0u);
        view_row(back, d, CON_COLS - 4u);   /* where the old marker went */
    } else {
        disp_scroll(0u, CON_ROWS * CON_CELL_H, 0 - (int)(d * CON_CELL_H), CON_BG);
        for (v = CON_ROWS - d; v < CON_ROWS; v++) view_row(back, v, 0u);
        view_row(back, 0u, CON_COLS - 4u);
    }
    view_back = back;
    if (back > 0u) {
        mark[0] = '-';
        utoa(back, mark + 1, 10u);
        len = strlen(mark);
        for (i = 0u; i < len; i++) con_paint(0u, CON_COLS - len + i, (int)mark[i], CON_DEFAULT | CON_INVERSE);
    }
}

/* The view moved by delta rows, back when positive. The line's cursor
 * hides while the view is off the screen's bottom row. */
static void view_by(int delta, unsigned cur) {
    unsigned back;
    if (delta > 0) back = view_back + (unsigned)delta;
    else back = (unsigned)(0 - delta) >= view_back ? 0u : view_back - (unsigned)(0 - delta);
    if (back > back_count) back = back_count;
    if (back == view_back) return;
    if (view_back == 0u) line_cursor(cur, 0u);
    view_move(back);
    if (view_back == 0u) line_cursor(cur, 1u);
}

/* --- paging (docs/phase4b_plan.md step 6) ----------------------------------- */

/* -- more --, in inverse at the start of the row the output goes on next,
 * or taken away again. */
static void page_mark(unsigned on) {
    char *text = "-- more --";
    unsigned i;
    for (i = 0u; i < 10u; i++) {
        con_grid[con_row * CON_COLS + i] = on != 0u ? text[i] : ' ';
        con_look[con_row * CON_COLS + i] = (char)(on != 0u ? CON_DEFAULT | CON_INVERSE : CON_DEFAULT);
        con_cell(con_row, i);
    }
}

/* A screen of output shown with paging on: the console waits, inside
 * write. Space lets another screen through and Enter one more row; PgUp,
 * PgDn and the wheel look back. q stops. Output from a program that more
 * ran ends that program, and everything it ran, coming back to more's exec
 * as ENDED_QUIT; for more's own output this returns 1, and write returns
 * E_QUIT. Ctrl+C is the break for the program writing, as it would be
 * without paging; a program with break off takes it as q. */
static unsigned page_wait(void) {
    unsigned event;
    unsigned code;
    unsigned wheel;
    unsigned ctrl = 0u;
    page_mark(1u);
    k_break(0u);
    while (1) {
        wheel = mouse_event();
        if (wheel != 0u && ME_PRESSED(wheel) != 0u) {
            if (ME_BUTTON(wheel) == ME_WHEEL_UP) view_move(view_back + WHEEL_ROWS);
            if (ME_BUTTON(wheel) == ME_WHEEL_DOWN) view_move(view_back > WHEEL_ROWS ? view_back - WHEEL_ROWS : 0u);
        }
        event = key_event();
        if (event == 0u) continue;
        code = KE_CODE(event);
        if (code == K_KEY_LCTRL || code == K_KEY_RCTRL) {
            ctrl = KE_PRESSED(event) != 0u;
            continue;
        }
        if (KE_PRESSED(event) == 0u) continue;
        if (code == KEY_PGUP) {
            view_move(view_back + PAGE_ROWS);
            continue;
        }
        if (code == KEY_PGDN) {
            view_move(view_back > PAGE_ROWS ? view_back - PAGE_ROWS : 0u);
            continue;
        }
        if (view_back > 0u) view_move(0u);  /* any other key: back to the bottom */
        if (code == ' ') {
            page_rows = 0u;
            break;
        }
        if (code == KEY_ENTER) {
            page_rows = PAGE_ROWS - 1u;
            break;
        }
        if (code == 'q' || code == 'Q' || (ctrl != 0u && (code == 'c' || code == 'C'))) {
            page_mark(0u);
            while (key_read() >= 0) { }
            page_rows = 0u;
            if (ctrl != 0u && procs[depth].breaks != 0u) {
                con_puts("^C\n");
                procs[depth].how = K_HOW_BREAK;
                ((abort_fn)&exec_abort)(ENDED_BREAK, &procs[depth].save_sp);
            }
            if ((unsigned)depth > page_owner) {
                depth = (int)page_owner + 1;
                procs[depth].how = K_HOW_QUIT;
                ((abort_fn)&exec_abort)(ENDED_QUIT, &procs[depth].save_sp);
            }
            k_break(procs[depth].breaks);
            return 1u;
        }
    }
    page_mark(0u);
    while (key_read() >= 0) { }
    k_break(procs[depth].breaks);
    return 0u;
}

/* A typed line into buf, '\n' included and not NUL-terminated, as read()
 * returns it, of up to LINE_MAX characters. Waits: this is the kernel,
 * interrupts off, until Enter.
 *
 * The keys (docs/phase4b_plan.md step 2): Left, Right, Home and End, or
 * Ctrl+A and Ctrl+E, move; typing inserts at the cursor; Backspace and
 * Delete delete; Ctrl+U throws away what's typed; Up and Down step through
 * the last HISTORY lines, Down past the newest giving back what was being
 * typed; Ctrl+L moves the prompt and the line to the top of the screen,
 * the prompt found by its mark (con_osc_char); Tab completes the word
 * before the cursor, and a second Tab lists the choices (line_tab). PgUp,
 * PgDn and the mouse wheel look back through the scrollback, and any other
 * key brings the view back first (view_by).
 *
 * Break is off while it waits, or Ctrl+C would sit pending until the line
 * was done and then end the program reading it. Here Ctrl+C is a key that
 * throws the line away. Edges come from the key-event queue, in order, so
 * Ctrl held while C went down is known even when both were typed long
 * before this looks; the character queue holds the same presses, and is
 * emptied at the end so the next program doesn't get them. */
static int con_read_line(char *buf, unsigned size) {
    unsigned n = 0u;                /* characters typed */
    unsigned cur = 0u;              /* the cursor, before character cur */
    unsigned old;
    unsigned max;
    unsigned browse = history_count;
    unsigned event;
    unsigned code;
    unsigned ctrl = 0u;
    unsigned top;
    unsigned tabbed = 0u;           /* the last key was a Tab */
    unsigned was_tab;
    unsigned wheel;
    if (size < 2u) return FS_EINVAL;
    max = size - 2u;
    if (max > LINE_MAX) max = LINE_MAX;
    k_break(0u);
    page_rows = 0u;                         /* reading a line starts the count again */
    if (con_col == CON_COLS) con_newline();
    line_row = con_row;
    line_col = con_col;
    line_cursor(0u, 1u);
    while (1) {
        wheel = mouse_event();              /* a notch: a press and a release */
        if (wheel != 0u && ME_PRESSED(wheel) != 0u) {
            if (ME_BUTTON(wheel) == ME_WHEEL_UP) view_by((int)WHEEL_ROWS, cur);
            if (ME_BUTTON(wheel) == ME_WHEEL_DOWN) view_by(0 - (int)WHEEL_ROWS, cur);
        }
        event = key_event();
        if (event == 0u) continue;
        code = KE_CODE(event);
        if (code == K_KEY_LCTRL || code == K_KEY_RCTRL) {
            ctrl = KE_PRESSED(event) != 0u;
            continue;
        }
        if (KE_PRESSED(event) == 0u) continue;
        if (ctrl != 0u && code >= 'A' && code <= 'Z') code = code + 32u;
        if (code == KEY_PGUP || code == KEY_PGDN) {
            view_by(code == KEY_PGUP ? (int)PAGE_ROWS : 0 - (int)PAGE_ROWS, cur);
            continue;
        }
        if (view_back > 0u) view_move(0u);  /* any other key: back to the bottom */
        line_cursor(cur, 0u);
        if (list_rows > 0u) line_unlist();
        was_tab = tabbed;
        tabbed = 0u;
        if (ctrl != 0u && code == 'c') {
            line_end(n);
            con_puts("^C");
            n = 0u;
            break;
        }
        if (code == KEY_ENTER) {
            line_end(n);
            break;
        }
        old = n;
        if (code == KEY_LEFT) {
            if (cur > 0u) cur--;
        } else if (code == KEY_RIGHT) {
            if (cur < n) cur++;
        } else if (code == KEY_HOME || (ctrl != 0u && code == 'a')) {
            cur = 0u;
        } else if (code == KEY_END || (ctrl != 0u && code == 'e')) {
            cur = n;
        } else if (code == KEY_BACKSPACE) {
            if (cur > 0u) {
                memmove((void *)(buf + cur - 1u), (void *)(buf + cur), n - cur);
                cur--;
                n--;
                line_draw(buf, cur, n, old);
            }
        } else if (code == KEY_DELETE) {
            if (cur < n) {
                memmove((void *)(buf + cur), (void *)(buf + cur + 1u), n - cur - 1u);
                n--;
                line_draw(buf, cur, n, old);
            }
        } else if (ctrl != 0u && code == 'u') {
            n = 0u;
            cur = 0u;
            line_draw(buf, 0u, 0u, old);
        } else if (ctrl != 0u && code == 'l') {
            top = line_row;
            if (con_marked != 0u && con_mark_row <= line_row) top = con_mark_row;
            while (top < line_row && con_blank_row(top) != 0u) top++;
            if (top > con_top) con_scroll(con_top, con_bottom, top - con_top, 1u);
        } else if (code == KEY_TAB && ctrl == 0u) {
            if (line_tab(buf, &n, &cur, max) != 0u) {
                if (was_tab != 0u) line_list(n);
            } else {
                line_draw(buf, 0u, n, old);
            }
            tabbed = 1u;
        } else if (code == KEY_UP) {
            if (browse > 0u) {
                if (browse == history_count) {
                    memcpy((void *)line_typed, (void *)buf, n);
                    line_typed[n] = 0;
                }
                browse--;
                n = line_load(buf, history + browse * HISTORY_LINE, max);
                cur = n;
                line_draw(buf, 0u, n, old);
            }
        } else if (code == KEY_DOWN) {
            if (browse < history_count) {
                browse++;
                if (browse == history_count) n = line_load(buf, line_typed, max);
                else n = line_load(buf, history + browse * HISTORY_LINE, max);
                cur = n;
                line_draw(buf, 0u, n, old);
            }
        } else if (ctrl == 0u && code >= 32u && code <= 126u && n < max) {
            memmove((void *)(buf + cur + 1u), (void *)(buf + cur), n - cur);
            buf[cur] = (char)code;
            n++;
            cur++;
            line_draw(buf, cur - 1u, n, old);
        }
        line_cursor(cur, 1u);
    }
    con_put('\n');
    if (n > 0u) history_add(buf, n);
    buf[n] = '\n';
    while (key_read() >= 0) { }
    con_marked = 0u;                        /* a mark counts for one line */
    k_break(procs[depth].breaks);           /* as the program reading had it */
    return (int)(n + 1u);
}

static char *k_strerror(int status) {
    if (status == E_NOTPROG) return "not a program";
    if (status == E_NOMEM) return "no room to run it";
    if (status == E_DEPTH) return "too many programs running";
    if (status == ENDED_DIV_ZERO) return "divided by zero";
    if (status == ENDED_BAD_OPCODE) return "ran a bad instruction";
    if (status == ENDED_BAD_FETCH) return "ran off the end of memory";
    if (status == ENDED_BREAK) return "stopped";
    if (status == E_QUIT || status == ENDED_QUIT) return "stopped";
    return fs_strerror(status);
}

/* --- the system calls, as kernel.asm's wrappers call them ---------------- */

int k_write(int fd, char *buf, unsigned n) {
    unsigned i;
    int to_file;                /* the depth a redirection covers from, or -1 */
    int to_buf;                 /* and a capture's                            */

    if (fd == STDOUT) {
        /* A capture and a redirection can both be on, one outside the other:
         * the innermost -- the deeper owner -- takes the output. */
        to_file = (out_file >= 0 && depth >= out_file_depth) ? out_file_depth : -1;
        to_buf = (out_buf != NULL && depth >= out_depth) ? out_depth : -1;
        if (to_file >= 0 && to_file >= to_buf) return fs_write(out_file, buf, n);
        if (to_buf >= 0) {
            /* Into the buffer: what doesn't fit is dropped, and the caller
             * knows by the length it gets back. */
            for (i = 0u; i < n; i++) {
                if (out_len + 1u < out_size) {
                    out_buf[out_len] = buf[i];
                    out_len++;
                }
            }
            out_buf[out_len] = 0;
            return (int)n;
        }
    }
    if (fd == STDOUT || fd == STDERR) {
        page_counting = page_owner != 0u ? 1u : 0u;
        for (i = 0u; i < n; i++) {
            if (page_counting != 0u && page_rows >= PAGE_ROWS && con_esc == CON_ESC_NONE) {
                page_counting = 0u;
                if (page_wait() != 0u) return E_QUIT;
                page_counting = 1u;
            }
            con_put((int)buf[i]);
        }
        page_counting = 0u;
        return (int)n;
    }
    if (fd < 3) return E_BADF;
    return fs_write(fd - 3, buf, n);
}

int k_read(int fd, char *buf, unsigned n) {
    if (fd == STDIN) {
        /* < : a line at a time with its '\n', and 0 at the end, which is
         * what a program reading the console already expects. */
        if (in_file >= 0 && depth >= in_file_depth && n >= 2u) return fs_gets(in_file, buf, n);
        return con_read_line(buf, n);
    }
    if (fd < 3) return E_BADF;
    return fs_read(fd - 3, buf, n);
}

static void k_track(int handle, unsigned dir) {
    if (handle >= 0 && handle < HANDLES) {
        handle_depth[handle] = depth;
        handle_dir[handle] = dir;
    }
}

static void k_untrack(int handle) {
    if (handle >= 0 && handle < HANDLES) handle_depth[handle] = 0;
}

int k_open(char *path, unsigned flags) {
    int handle = fs_open(path, flags);
    if (handle < 0) return handle;
    k_track(handle, 0u);
    return handle + 3;
}

int k_close(int fd) {
    if (fd < 3) return E_BADF;
    k_untrack(fd - 3);
    return fs_close(fd - 3);
}

int k_opendir(char *path) {
    int handle = fs_opendir(path);
    k_track(handle, 1u);
    return handle;
}

int k_readdir(int dh, sys_stat_t *out) { return fs_readdir(dh, (fs_stat_t *)out); }

int k_closedir(int dh) {
    k_untrack(dh);
    return fs_closedir(dh);
}

int k_stat(char *path, sys_stat_t *out) { return fs_stat(path, (fs_stat_t *)out); }

int k_chdir(char *path) { return fs_chdir(path); }

int k_getcwd(char *buf, unsigned size) { return fs_getcwd(buf, size); }

int k_getkey(void) { return key_read(); }

int k_mkdir(char *path) { return fs_mkdir(path); }

int k_rmdir(char *path) { return fs_rmdir(path); }

int k_remove(char *path) { return fs_remove(path); }

int k_rename(char *from, char *to) { return fs_rename(from, to); }

/* Where the calling program's commands are, for Tab in its first word:
 * a directory of .bin files, and its built-ins between spaces. It lasts
 * until the program ends; a program it runs starts without. */
int k_setcomplete(char *dir, char *builtins) {
    if (strlen(dir) >= COMPLETE_TEXT || strlen(builtins) >= COMPLETE_TEXT) return FS_EINVAL;
    strlcpy(complete_dir + (unsigned)depth * COMPLETE_TEXT, dir, COMPLETE_TEXT);
    strlcpy(complete_builtins + (unsigned)depth * COMPLETE_TEXT, builtins, COMPLETE_TEXT);
    return 0;
}

/* Ctrl+C as the break, on or off, for the program calling: what it was.
 * It lasts until the program ends, and a program it runs starts with break
 * on (docs/phase4b_plan.md step 5). */
int k_setbreak(int on) {
    unsigned was = procs[depth].breaks;
    procs[depth].breaks = on != 0 ? 1u : 0u;
    k_break(procs[depth].breaks);
    return (int)was;
}

/* Paging on or off for the calling program's output and that of the
 * programs it runs, until it ends (page_wait). */
int k_paging(int on) {
    if (on != 0) {
        page_owner = (unsigned)depth;
        page_rows = 0u;
    } else if (page_owner == (unsigned)depth) {
        page_owner = 0u;
    }
    return 0;
}

/* The console is not painted back over the screen between the programs this
 * one runs, so a script that draws in several calls keeps its picture
 * (docs/graphics_plan.md 4.3). Paging's shape exactly: the depth that turned
 * it on owns it, the programs it runs inherit it, and k_run clears it when
 * that program ends -- so a script that crashes cannot leave the console
 * invisible. Its own tidy still redraws, which is what brings the console
 * back when it ends.
 *
 * It does not stop a program writing to the console, which still paints
 * where it writes: a script that wants the screen to itself captures what
 * it runs. Nor does it move the display, so k_tidy still points that at
 * DISPLAY_START after a program that flipped pages. */
int k_keepscreen(int on) {
    int was = screen_owner != 0u ? 1 : 0;
    if (on != 0) {
        screen_owner = (unsigned)depth;
    } else if (screen_owner == (unsigned)depth) {
        screen_owner = 0u;
    }
    return was;
}

/* --- running programs ------------------------------------------------------ */

/* Put back what a program may have left behind: files open, timers
 * running, keys queued, the display pointed at a buffer of its own. The
 * disk is mounted afresh, since a program with its own fs.c may have
 * written what this one's cache doesn't know. */
/* The disk mounted again, with the current directory kept. A program with
 * its own fs.c may have written what this one's cache doesn't know, so this
 * happens after every program -- and again when a redirection closes, since
 * the attempt inside k_tidy is refused while the file is open. */
static void k_remount(void) {
    char cwd[FS_PATH_MAX + 8];
    int n = fs_getcwd(cwd, FS_PATH_MAX + 8u);
    if (fs_unmount(mounted) >= 0) {
        fs_mount(mounted);
        if (n > 0) fs_chdir(cwd);
    }
}

static void k_tidy(void) {
    int h;
    unsigned t;
    for (h = 0; h < HANDLES; h++) {
        if (handle_depth[h] >= depth) {     /* and programs it ran that q ended */
            if (handle_dir[h] != 0u) fs_closedir(h);
            else fs_close(h);
            handle_depth[h] = 0;
        }
    }
    for (t = 0u; t < TIMERS; t++) k_io(CH_TIMER, K_TIMER_STOP, 0u, t);
    while (key_read() >= 0) { }
    while (key_event() != 0u) { }
    while (mouse_event() != 0u) { }
    k_io(CH_DISPLAY, K_DISPLAY_SET_BASE, 4u, DISPLAY_START);
    k_remount();
    con_attr = CON_DEFAULT;                 /* no color left on for the shell */
    con_esc = CON_ESC_NONE;
    con_marked = 0u;
    con_top = 0u;                           /* nor a scroll region */
    con_bottom = CON_ROWS - 1u;
    /* Not while a program is keeping the screen: its children end without
     * the console landing on top of the picture. k_run has cleared the
     * owner by the time the owner's own tidy runs, so the console always
     * comes back. */
    if (screen_owner == 0u) con_redraw();
}

/* How the program at `depth` ended, on the debug port. */
static void k_log_end(int status) {
    struct proc *p = &procs[depth];
    if (p->how == K_HOW_EXIT) {
        dbg_printf("[kernel] %s ended: exit %d\n", p->path, status);
    } else if (p->how == K_HOW_FAULT) {
        dbg_printf("[kernel] %s ended: %s at 0x%08X\n", p->path, k_strerror(status), p->fault_pc);
    } else if (p->how == K_HOW_BREAK) {
        dbg_printf("[kernel] %s ended: Ctrl+C\n", p->path);
    } else if (p->how == K_HOW_QUIT) {
        dbg_printf("[kernel] %s ended: q at -- more --\n", p->path);
    } else {
        dbg_printf("[kernel] %s ended: %d\n", p->path, status);
    }
}

/* exec itself. *ran becomes 1 once the program is called. */
static int k_run(char *path, int argc, char **argv, unsigned *ran) {
    unsigned base;
    unsigned *header;
    unsigned size;
    unsigned patches;
    unsigned *list;
    unsigned delta;
    unsigned i;
    unsigned *word;
    int got;
    int status;

    started = 0u;
    if (depth + 1 >= MAX_DEPTH) return E_DEPTH;
    if (depth == 0) base = PROGRAMS;
    else base = ((*(unsigned *)procs[depth].heap_ptr_at + 7u) & 0xFFFFFFF8u) + PROGRAM_FILE_HEADER;
    if (base >= PROGRAMS_TOP) return E_NOMEM;

    /* Straight into place: the header lands in the 32 bytes below base. */
    header = (unsigned *)(base - PROGRAM_FILE_HEADER);
    got = fs_load(path, (void *)header, PROGRAMS_TOP - base + PROGRAM_FILE_HEADER);
    if (got == FS_E2BIG) return E_NOMEM;
    if (got < 0) return got;
    if ((unsigned)got < PROGRAM_FILE_HEADER || header[0] != PROGRAM_FILE_MAGIC
            || header[1] != PROGRAM_FILE_VERSION) return E_NOTPROG;
    size = header[2];
    patches = header[4];
    if (size > (unsigned)got || patches > size
            || (unsigned)got != PROGRAM_FILE_HEADER + size + patches * 4u
            || header[3] >= size || header[6] + 8u > size) return E_NOTPROG;
    if (base + size + header[5] + MIN_HEAP > PROGRAMS_TOP) return E_NOMEM;

    list = (unsigned *)(base + size);
    for (i = 0u; i < patches; i++) {
        if (list[i] + 4u > size || (list[i] & 3u) != 0u) return E_NOTPROG;
    }
    delta = base - header[7];
    for (i = 0u; i < patches; i++) {
        word = (unsigned *)(base + list[i]);
        *word = *word + delta;
    }
    *(unsigned *)(base + header[6] + 4u) = PROGRAMS_TOP;       /* its __heap_limit */

    depth++;
    complete_dir[(unsigned)depth * COMPLETE_TEXT] = 0;         /* no commands for Tab yet */
    complete_builtins[(unsigned)depth * COMPLETE_TEXT] = 0;
    procs[depth].base = base;
    procs[depth].heap_ptr_at = base + header[6];
    procs[depth].breaks = 1u;
    procs[depth].how = K_HOW_RETURN;
    strlcpy(procs[depth].path, path, FS_PATH_MAX + 1u);
    dbg_printf("[kernel] exec %s at 0x%08X, depth %d\n", path, base, depth);
    started = 1u;
    *ran = 1u;
    k_break(1u);
    status = ((call4_fn)&exec_call)(base + header[3], argc, argv, &procs[depth].save_sp);
    k_break(0u);
    started = 1u;                           /* a program it ran may have failed to start */
    k_log_end(status);
    if (page_owner >= (unsigned)depth) page_owner = 0u;     /* paging ends with its program */
    if (screen_owner >= (unsigned)depth) screen_owner = 0u; /* and so does the screen */
    k_tidy();
    depth--;
    if (depth > 0) k_break(procs[depth].breaks);            /* its parent runs again, as it had it */
    return status;
}

/* exec, as the system call and main call it: a line on the debug port when
 * the program couldn't even start. */
int k_exec(char *path, int argc, char **argv) {
    unsigned ran = 0u;
    int status;
    status = k_run(path, argc, argv, &ran);
    if (ran == 0u) dbg_printf("[kernel] exec %s: %s\n", path, k_strerror(status));
    return status;
}

/* exec, with the program's STDOUT -- and that of everything it runs -- going
 * into buf instead of the console (docs/pgs_plan.md 4.5). The capture that
 * was running is remembered and put back, so a program that captures a
 * program that captures needs nothing extra, and a fault or Ctrl+C in the
 * child comes back through here like any other status. */
int k_exec_out(char *path, int argc, char **argv, char *buf, unsigned size) {
    char *was_buf = out_buf;
    unsigned was_size = out_size;
    unsigned was_len = out_len;
    int was_depth = out_depth;
    unsigned ran = 0u;
    int status;

    if (buf == NULL || size < 2u) return FS_EINVAL;
    if (size > CAPTURE_MAX) size = CAPTURE_MAX;     /* a wrong size can only reach so far */
    out_buf = buf;
    out_size = size;
    out_len = 0u;
    out_depth = depth + 1;
    buf[0] = 0;
    status = k_run(path, argc, argv, &ran);
    if (ran == 0u) dbg_printf("[kernel] exec %s: %s\n", path, k_strerror(status));
    out_buf = was_buf;
    out_size = was_size;
    out_len = was_len;
    out_depth = was_depth;
    return status;
}

/* exec, with a file on either end of it (docs/redirect_plan.md 3.1).
 *
 * `out` takes STDOUT: R_APPEND writes to the end of it, and R_TRUNC writes
 * `out~` and renames it over `out` once the program has ended by itself, so
 * a fault, a Ctrl+C or a full disk leaves the old file whole and takes the
 * half-written one away with it. `in` feeds STDIN, a line at a time. Either
 * may be NULL. The redirections that were running are put back afterwards,
 * so these nest the way exec_out does.
 */
int k_exec_io(char *path, int argc, char **argv, char *in, char *out, unsigned how) {
    char temp[FS_PATH_MAX + 2];
    int was_out = out_file;
    int was_out_depth = out_file_depth;
    int was_in = in_file;
    int was_in_depth = in_file_depth;
    unsigned ran = 0u;
    int ofd = -1;
    int ifd = -1;
    int status;
    int r;

    temp[0] = 0;
    if (out != NULL && *out != 0) {
        if (how == R_APPEND) {
            ofd = fs_open(out, FS_WRITE | FS_CREATE | FS_APPEND);
        } else {
            if (strlen(out) + 1u > FS_PATH_MAX) return FS_ENAMETOOLONG;
            strlcpy(temp, out, sizeof(temp));
            strlcat(temp, "~", sizeof(temp));
            ofd = fs_open(temp, FS_WRITE | FS_CREATE | FS_TRUNC);
        }
        if (ofd < 0) return ofd;
    }
    if (in != NULL && *in != 0) {
        ifd = fs_open(in, FS_READ);
        if (ifd < 0) {
            if (ofd >= 0) {
                fs_close(ofd);
                if (temp[0] != 0) fs_remove(temp);
            }
            return ifd;
        }
    }
    if (ofd >= 0) {
        out_file = ofd;
        out_file_depth = depth + 1;
    }
    if (ifd >= 0) {
        in_file = ifd;
        in_file_depth = depth + 1;
    }

    status = k_run(path, argc, argv, &ran);
    if (ran == 0u) dbg_printf("[kernel] exec %s: %s\n", path, k_strerror(status));

    if (ofd >= 0) fs_close(ofd);
    if (ifd >= 0) fs_close(ifd);
    out_file = was_out;
    out_file_depth = was_out_depth;
    in_file = was_in;
    in_file_depth = was_in_depth;
    k_remount();                        /* k_tidy could not, with a file open */

    if (temp[0] != 0) {
        if (ran != 0u && status > ENDED_DIV_ZERO) {
            fs_remove(out);             /* it may not be there at all */
            r = fs_rename(temp, out);
            if (r < 0) {
                fs_remove(temp);
                dbg_printf("[kernel] %s: %s\n", out, k_strerror(r));
                return r;
            }
        } else {
            fs_remove(temp);            /* what was there stays there */
        }
    }
    return status;
}

void k_exit(int code) {
    procs[depth].how = K_HOW_EXIT;
    ((abort_fn)&exec_abort)(code, &procs[depth].save_sp);
}

void k_fault(unsigned vector, unsigned pc) {
    int status = ENDED_BREAK;
    unsigned kernel = in_kernel;
    in_kernel = 1u;
    if (vector == VEC_DIV_ZERO) status = ENDED_DIV_ZERO;
    if (vector == VEC_BAD_OPCODE) status = ENDED_BAD_OPCODE;
    if (vector == VEC_BAD_FETCH) status = ENDED_BAD_FETCH;
    if (depth == 0 || kernel != 0u) {
        dbg_printf("[kernel] panic: %s at 0x%08X\n", k_strerror(status), pc);
        con_puts("\nkernel panic: ");
        con_puts(k_strerror(status));
        con_puts(" at 0x");
        con_number(pc, 16u);
        con_put('\n');
        ((void_fn)&khalt)();
    }
    procs[depth].how = K_HOW_FAULT;
    procs[depth].fault_pc = pc;
    if (status == ENDED_BREAK) {
        con_puts("^C\n");
        procs[depth].how = K_HOW_BREAK;
    }
    ((abort_fn)&exec_abort)(status, &procs[depth].save_sp);
}

/* --- boot ------------------------------------------------------------------ */

static void k_tables(void) {
    unsigned *table = (unsigned *)SYSCALL_TABLE;
    out_file = -1;              /* nothing redirected yet. Set here and not  */
    in_file = -1;               /* where they are declared: every global of  */
                                /* this kernel starts as the image's zeros.  */
    table[SYS_WRITE] = (unsigned)&w_write;
    table[SYS_READ] = (unsigned)&w_read;
    table[SYS_OPEN] = (unsigned)&w_open;
    table[SYS_CLOSE] = (unsigned)&w_close;
    table[SYS_OPENDIR] = (unsigned)&w_opendir;
    table[SYS_READDIR] = (unsigned)&w_readdir;
    table[SYS_CLOSEDIR] = (unsigned)&w_closedir;
    table[SYS_STAT] = (unsigned)&w_stat;
    table[SYS_CHDIR] = (unsigned)&w_chdir;
    table[SYS_GETCWD] = (unsigned)&w_getcwd;
    table[SYS_EXEC] = (unsigned)&w_exec;
    table[SYS_EXIT] = (unsigned)&w_exit;
    table[SYS_GETKEY] = (unsigned)&w_getkey;
    table[SYS_MKDIR] = (unsigned)&w_mkdir;
    table[SYS_RMDIR] = (unsigned)&w_rmdir;
    table[SYS_REMOVE] = (unsigned)&w_remove;
    table[SYS_RENAME] = (unsigned)&w_rename;
    table[SYS_SETCOMPLETE] = (unsigned)&w_setcomplete;
    table[SYS_SETBREAK] = (unsigned)&w_setbreak;
    table[SYS_PAGING] = (unsigned)&w_paging;
    table[SYS_EXEC_OUT] = (unsigned)&w_exec_out;
    table[SYS_EXEC_IO] = (unsigned)&w_exec_io;
    table[SYS_KEEPSCREEN] = (unsigned)&w_keepscreen;
    vectors[VEC_DIV_ZERO] = (unsigned)&fault_div;
    vectors[VEC_BAD_OPCODE] = (unsigned)&fault_opcode;
    vectors[VEC_BAD_FETCH] = (unsigned)&fault_fetch;
    vectors[VEC_TIMER] = (unsigned)&irq_ignore;
    vectors[VEC_BREAK] = (unsigned)&on_break;
    ((void_fn)&kinit)();
}

/* The first line on the debug port. Booted from a disk, bios2 left that
 * disk's block 0 at BOOT_LOAD_ADDR, and its boot record holds the size of
 * /boot.bin, which is this kernel. Put in RAM any other way, it can't know. */
static void k_log_start(unsigned channel) {
    unsigned *record = (unsigned *)(BOOT_LOAD_ADDR + BOOT_RECORD);
    if ((channel == CH_HDD || channel == CH_CD) && record[0] == BOOT_SIGNATURE) {
        dbg_printf("[kernel] started, %u bytes at 0x%08X\n", record[2], PROGRAM_LOAD_ADDR);
    } else {
        dbg_printf("[kernel] started at 0x%08X\n", PROGRAM_LOAD_ADDR);
    }
}

static void k_log_mount(unsigned channel) {
    fs_volinfo vi;
    if (fs_statvfs(channel, &vi) >= 0 && vi.label[0] != 0) {
        dbg_printf("[kernel] mounted channel %u, %s\n", channel, vi.label);
    } else {
        dbg_printf("[kernel] mounted channel %u\n", channel);
    }
}

/* --- boot.conf (docs/phase6_plan.md) -------------------------------------- */

/* PigeonOS on the console, once: after the splash, or before whatever stops
 * boot first, so an error reads under it as it always has. */
static void k_banner(void) {
    if (banner_shown != 0u) return;
    con_puts("PigeonOS\n");
    banner_shown = 1u;
}

/* What's wrong with boot.conf, on the console and the debug port; line 0 is
 * the file as a whole. Boot is strict, so it stops after this. */
static int k_conf_error(unsigned line, char *first, char *second) {
    char message[96];
    strlcpy(message, first, 96u);
    strlcat(message, second, 96u);
    k_banner();
    con_puts(BOOT_CONF);
    if (line != 0u) {
        con_put(':');
        con_number(line, 10u);
        dbg_printf("[kernel] %s:%u: %s\n", BOOT_CONF, line, message);
    } else {
        dbg_printf("[kernel] %s: %s\n", BOOT_CONF, message);
    }
    con_puts(": ");
    con_puts(message);
    con_put('\n');
    return -1;
}

/* Spaces, tabs and a '\r' off both ends, in place. */
static char *k_trim(char *s) {
    unsigned n;
    while (*s == ' ' || *s == '\t') s++;
    n = strlen(s);
    while (n > 0u && (s[n - 1u] == ' ' || s[n - 1u] == '\t' || s[n - 1u] == '\r')) n--;
    s[n] = 0;
    return s;
}

/* A splash_ms: digits only, from 1 to SPLASH_MS_MAX. 0 when it isn't one. */
static unsigned k_ms(char *s) {
    unsigned v = 0u;
    if (*s == 0) return 0u;
    while (*s != 0) {
        if (*s < '0' || *s > '9' || v > SPLASH_MS_MAX) return 0u;
        v = v * 10u + (unsigned)(*s - '0');
        s++;
    }
    if (v > SPLASH_MS_MAX) return 0u;
    return v;
}

/* /etc/boot.conf into boot_splash, boot_splash_ms and boot_startup: 1 when
 * it was read, 0 when there is none -- the shell, as before phase 6 -- or
 * -1 after saying what's wrong with it. key = value a line; # starts a
 * comment. */
static int k_read_boot_conf(void) {
    char text[1025];                /* BOOT_CONF_MAX and its terminator */
    char *line;
    char *next;
    char *cut;
    char *key;
    char *value;
    unsigned number = 0u;
    unsigned seen = 0u;             /* 1 splash, 2 splash_ms, 4 startup */
    unsigned bit;
    int n;

    boot_splash[0] = 0;
    boot_splash_ms = SPLASH_MS;
    strlcpy(boot_startup, SHELL, FS_PATH_MAX + 1u);
    n = fs_load(BOOT_CONF, text, BOOT_CONF_MAX);
    if (n == FS_ENOENT) return 0;
    if (n == FS_E2BIG) return k_conf_error(0u, "is over 1024 bytes", "");
    if (n < 0) return k_conf_error(0u, fs_strerror(n), "");
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
        line = k_trim(line);
        if (*line != 0) {
            cut = strchr(line, '=');
            if (cut == NULL) return k_conf_error(number, "expected key = value", "");
            *cut = 0;
            key = k_trim(line);
            value = k_trim(cut + 1);
            bit = 0u;
            if (strcmp(key, "splash") == 0) bit = 1u;
            if (strcmp(key, "splash_ms") == 0) bit = 2u;
            if (strcmp(key, "startup") == 0) bit = 4u;
            if (bit == 0u) return k_conf_error(number, "unknown key ", key);
            if ((seen & bit) != 0u) return k_conf_error(number, key, " given twice");
            if (*value == 0) return k_conf_error(number, "no value for ", key);
            seen = seen | bit;
            if (bit == 2u) {
                boot_splash_ms = k_ms(value);
                if (boot_splash_ms == 0u) {
                    return k_conf_error(number, "splash_ms must be 1 to 60000, not ", value);
                }
            } else if (strlen(value) > FS_PATH_MAX) {
                return k_conf_error(number, "path too long for ", key);
            } else if (bit == 1u) {
                strlcpy(boot_splash, value, FS_PATH_MAX + 1u);
            } else {
                strlcpy(boot_startup, value, FS_PATH_MAX + 1u);
            }
        }
        line = next;
    }
    if ((seen & 2u) != 0u && (seen & 1u) == 0u) return k_conf_error(0u, "splash_ms without splash", "");
    if ((seen & 4u) == 0u) return k_conf_error(0u, "no startup", "");
    return 1;
}

/* A program's name for its argv[0]: "/bin/sh.bin" is "sh". */
static void k_program_name(char *path, char *out) {
    char *base = path;
    char *p;
    unsigned n;
    for (p = path; *p != 0; p++) {
        if (*p == '/') base = p + 1;
    }
    strlcpy(out, base, 32u);
    n = strlen(out);
    if (n > 4u && strcmp(out + n - 4u, ".bin") == 0) out[n - 4u] = 0;
}

/* The splash, as `splash 2500`, with its splash_ms. 0 when boot carries on:
 * it ended by itself, by a key or by Ctrl+C. -1 when it couldn't start, or
 * faulted, which stops boot. */
static int k_splash(void) {
    char name[32];
    char digits[12];
    char *argv[3];
    int status;

    k_program_name(boot_splash, name);
    utoa(boot_splash_ms, digits, 10u);
    argv[0] = name;
    argv[1] = digits;
    argv[2] = NULL;
    status = k_exec(boot_splash, 2, argv);
    if (started == 0u) {
        k_banner();
        dbg_printf("[kernel] cannot start the splash, %s: %s\n", boot_splash, k_strerror(status));
        con_puts("cannot start ");
        con_puts(boot_splash);
        con_puts(": ");
        con_puts(k_strerror(status));
        con_put('\n');
        return -1;
    }
    if (procs[1].how == K_HOW_FAULT) {
        k_banner();
        dbg_print("[kernel] the splash faulted: not booting\n");
        con_puts(boot_splash);
        con_puts(": ");
        con_puts(k_strerror(status));
        con_puts(" at 0x");
        con_number(procs[1].fault_pc, 16u);
        con_put('\n');
        return -1;
    }
    return 0;
}

int main(void) {
    char *startup_argv[2];
    char startup_name[32];
    unsigned channel;
    int status;

    __heap_limit = PROGRAMS - PROGRAM_FILE_HEADER;  /* the first header lands there */
    in_kernel = 1u;
    k_tables();
    con_attr = CON_DEFAULT;
    con_bottom = CON_ROWS - 1u;
    con_clear();

    /* The disk it booted from (Q8): bios2 leaves the channel. */
    channel = *(unsigned *)BOOT_CHANNEL;
    k_log_start(channel);
    if (channel != CH_HDD && channel != CH_CD) channel = CH_HDD;
    status = fs_mount(channel);
    if (status < 0) {
        dbg_printf("[kernel] cannot mount channel %u: %s\n", channel, fs_strerror(status));
        k_banner();
        con_puts("cannot mount disk ");
        con_number(channel, 10u);
        con_puts(": ");
        con_puts(fs_strerror(status));
        con_put('\n');
        return status;
    }
    mounted = channel;
    k_log_mount(channel);

    /* What runs after the kernel: boot.conf's splash, then PigeonOS, then its
     * startup program, again whenever it ends. Boot is strict: an error in
     * any of it stops the machine. */
    status = k_read_boot_conf();
    if (status < 0) return status;
    if (status == 0) {
        dbg_printf("[kernel] no %s: starting %s\n", BOOT_CONF, SHELL);
    } else if (boot_splash[0] != 0) {
        dbg_printf("[kernel] boot.conf: splash %s for %u ms, startup %s\n",
                   boot_splash, boot_splash_ms, boot_startup);
    } else {
        dbg_printf("[kernel] boot.conf: startup %s\n", boot_startup);
    }
    if (boot_splash[0] != 0) {
        if (k_splash() < 0) return -1;
    }
    k_banner();

    k_program_name(boot_startup, startup_name);
    startup_argv[0] = startup_name;
    startup_argv[1] = NULL;
    
    while (1) {
        status = k_exec(boot_startup, 1, startup_argv);
        if (started == 0u) {
            dbg_printf("[kernel] cannot start %s: %s\n", boot_startup, k_strerror(status));
            con_puts("cannot start ");
            con_puts(boot_startup);
            con_puts(": ");
            con_puts(k_strerror(status));
            con_put('\n');
            return status;
        }
        if (strcmp(boot_startup, SHELL) == 0) {
            dbg_print("[kernel] the shell ended; starting it again\n");
            con_puts("shell ended, starting it again\n");
        } else {
            dbg_printf("[kernel] %s ended; starting it again\n", boot_startup);
            con_puts(startup_name);
            con_puts(" ended, starting it again\n");
        }
    }
    return 0;
}
