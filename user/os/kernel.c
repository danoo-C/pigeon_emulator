/* The PigeonOS kernel (docs/kernel.md §11, docs/kernel_exec.md).
 *
 * Built for PROGRAM_LOAD_ADDR and installed as the project's system,
 * /boot.bin, which the boot sector loads. It mounts the disk it booted
 * from, fills in the system-call and vector tables, and starts /bin/sh.bin
 * -- again whenever it ends.
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
 */
#include <pigeon/display.h>
#include <pigeon/fs.h>
#include <pigeon/input.h>
#include <pigeon/io.h>
#include <pigeon/string.h>
#include <pigeon/syscall.h>
#asm "kernel.asm"

#define SHELL         "/bin/sh.bin"
#define PROGRAMS      0x01000000u   /* where the first program goes          */
#define PROGRAMS_TOP  0x07F00000u   /* the hardware stack's megabyte above   */
#define MIN_HEAP      0x10000u      /* room a program must have past its frames */
#define MAX_DEPTH     8             /* the kernel, and 7 programs in a chain */
#define HANDLES       16            /* fs.c has 8; room to spare             */
#define TIMERS        16u           /* timer ids stopped after a program      */

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
extern int w_exit;
extern int w_getkey;
extern int w_mkdir;
extern int w_rmdir;
extern int w_remove;
extern int w_rename;
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
};

struct proc procs[MAX_DEPTH];
int depth;                      /* programs running; 0 is the kernel alone   */
unsigned in_kernel;             /* 1 while kernel code runs (kernel.asm)     */
unsigned started;               /* the last exec got as far as the program   */
unsigned mounted;               /* the channel of the disk the kernel uses   */
unsigned vectors[VECTOR_COUNT];
unsigned fault_frames[256];     /* the frame stack k_fault runs on           */
int handle_depth[HANDLES];      /* the program an fs handle is open for; 0 none */
unsigned handle_dir[HANDLES];   /* 1 if that handle is a directory's         */

char con_grid[384];             /* CON_ROWS * CON_COLS characters            */
char con_look[384];             /* each cell's ink, and CON_INVERSE          */
unsigned con_row;
unsigned con_col;               /* CON_COLS: the row is full, wrap before the next */
unsigned con_attr;              /* the look new characters get               */
unsigned con_esc;               /* CON_ESC_*: partway through a sequence     */
unsigned con_arg[4];            /* its numbers, as far as it has got         */
unsigned con_args;

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

/* A cell on a background already cleared: its character in its ink, or,
 * inverse, the ink as the background and the character cut out of it. */
static void con_draw(unsigned row, unsigned col) {
    unsigned x = col * CON_CELL_W;
    unsigned y = row * CON_CELL_H;
    unsigned look = (unsigned)con_look[row * CON_COLS + col];
    color_t ink = con_palette[look & 15u];
    if ((look & CON_INVERSE) != 0u) {
        disp_rect(x, y, CON_CELL_W, CON_CELL_H, ink);
        disp_char(x, y, (int)con_grid[row * CON_COLS + col], CON_BG);
    } else {
        disp_char(x, y, (int)con_grid[row * CON_COLS + col], ink);
    }
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
    con_redraw();
}

static void con_newline(void) {
    unsigned i;
    con_col = 0u;
    if (con_row + 1u < CON_ROWS) {
        con_row++;
        return;
    }
    for (i = 0u; i < (CON_ROWS - 1u) * CON_COLS; i++) {
        con_grid[i] = con_grid[i + CON_COLS];
        con_look[i] = con_look[i + CON_COLS];
    }
    for (i = (CON_ROWS - 1u) * CON_COLS; i < CON_ROWS * CON_COLS; i++) {
        con_grid[i] = ' ';
        con_look[i] = (char)CON_DEFAULT;
    }
    con_redraw();
}

/* One character of an escape sequence, after the ESC: '[', then numbers
 * separated by ';', then a letter (docs/phase4_plan.md step 3). The console
 * knows m, J, H and K. Anything else ends the sequence and is dropped, never
 * printed, and so are the other parameter bytes, such as the '?' of
 * ESC [ ? 25 l. The state lives here between calls, so a sequence may come
 * in two writes. */
static void con_escape(int c) {
    unsigned i;
    unsigned n;
    if (con_esc == CON_ESC_SEEN) {
        con_esc = CON_ESC_NONE;
        if (c == '[') {
            con_esc = CON_ESC_CSI;
            con_args = 0u;
            con_arg[0] = 0u;
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

/* The cursor: an underscore in the cell where the next character goes. */
static void con_cursor(unsigned on) {
    if (con_col == CON_COLS) {
        if (on == 0u) return;
        con_newline();
    }
    con_cell(con_row, con_col);
    if (on != 0u) disp_char(con_col * CON_CELL_W, con_row * CON_CELL_H, '_', CON_INK);
}

static void con_back(void) {
    if (con_col == 0u) {
        if (con_row == 0u) return;
        con_row--;
        con_col = CON_COLS;
    }
    con_col--;
    con_grid[con_row * CON_COLS + con_col] = ' ';
    con_cell(con_row, con_col);
}

/* A typed line into buf, '\n' included and not NUL-terminated, as read()
 * returns it. Keys other than printable ones, Backspace and Enter do
 * nothing. Waits: this is the kernel, interrupts off, until Enter.
 *
 * Break is off while it waits, or Ctrl+C would sit pending until the line
 * was done and then end the program reading it. Here Ctrl+C is a key that
 * throws the line away. Edges come from the key-event queue, in order, so
 * Ctrl held while C went down is known even when both were typed long
 * before this looks; the character queue holds the same presses, and is
 * emptied at the end so the next program doesn't get them. */
static int con_read_line(char *buf, unsigned size) {
    unsigned n = 0u;
    unsigned event;
    unsigned code;
    unsigned ctrl = 0u;
    if (size < 2u) return FS_EINVAL;
    k_break(0u);
    con_cursor(1u);
    while (1) {
        event = key_event();
        if (event == 0u) continue;
        code = KE_CODE(event);
        if (code == K_KEY_LCTRL || code == K_KEY_RCTRL) {
            ctrl = KE_PRESSED(event) != 0u;
            continue;
        }
        if (KE_PRESSED(event) == 0u) continue;
        if (ctrl != 0u && (code == 'c' || code == 'C')) {
            con_cursor(0u);
            con_puts("^C");
            n = 0u;
            break;
        }
        if (code == KEY_ENTER) break;
        if (code == KEY_BACKSPACE) {
            if (n > 0u) {
                n--;
                con_cursor(0u);
                con_back();
                con_cursor(1u);
            }
            continue;
        }
        if (code < 32u || code > 126u || n + 2u > size) continue;
        buf[n] = (char)code;
        n++;
        con_put((int)code);
        con_cursor(1u);
    }
    con_cursor(0u);
    con_put('\n');
    buf[n] = '\n';
    while (key_read() >= 0) { }
    k_break(1u);                            /* a program is reading this line */
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
    return fs_strerror(status);
}

/* --- the system calls, as kernel.asm's wrappers call them ---------------- */

int k_write(int fd, char *buf, unsigned n) {
    unsigned i;
    if (fd == STDOUT || fd == STDERR) {
        for (i = 0u; i < n; i++) con_put((int)buf[i]);
        return (int)n;
    }
    if (fd < 3) return E_BADF;
    return fs_write(fd - 3, buf, n);
}

int k_read(int fd, char *buf, unsigned n) {
    if (fd == STDIN) return con_read_line(buf, n);
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

/* --- running programs ------------------------------------------------------ */

/* Put back what a program may have left behind: files open, timers
 * running, keys queued, the display pointed at a buffer of its own. The
 * disk is mounted afresh, since a program with its own fs.c may have
 * written what this one's cache doesn't know. */
static void k_tidy(void) {
    int h;
    unsigned t;
    char cwd[FS_PATH_MAX + 8];
    int n;
    for (h = 0; h < HANDLES; h++) {
        if (handle_depth[h] == depth) {
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
    n = fs_getcwd(cwd, FS_PATH_MAX + 8u);
    if (fs_unmount(mounted) >= 0) {
        fs_mount(mounted);
        if (n > 0) fs_chdir(cwd);
    }
    con_attr = CON_DEFAULT;                 /* no color left on for the shell */
    con_esc = CON_ESC_NONE;
    con_redraw();
}

int k_exec(char *path, int argc, char **argv) {
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
    procs[depth].base = base;
    procs[depth].heap_ptr_at = base + header[6];
    started = 1u;
    k_break(1u);
    status = ((call4_fn)&exec_call)(base + header[3], argc, argv, &procs[depth].save_sp);
    k_break(0u);
    k_tidy();
    depth--;
    if (depth > 0) k_break(1u);             /* its parent runs again */
    return status;
}

void k_exit(int code) {
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
        con_puts("\nkernel panic: ");
        con_puts(k_strerror(status));
        con_puts(" at 0x");
        con_number(pc, 16u);
        con_put('\n');
        ((void_fn)&khalt)();
    }
    if (status == ENDED_BREAK) con_puts("^C\n");
    ((abort_fn)&exec_abort)(status, &procs[depth].save_sp);
}

/* --- boot ------------------------------------------------------------------ */

static void k_tables(void) {
    unsigned *table = (unsigned *)SYSCALL_TABLE;
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
    vectors[VEC_DIV_ZERO] = (unsigned)&fault_div;
    vectors[VEC_BAD_OPCODE] = (unsigned)&fault_opcode;
    vectors[VEC_BAD_FETCH] = (unsigned)&fault_fetch;
    vectors[VEC_TIMER] = (unsigned)&irq_ignore;
    vectors[VEC_BREAK] = (unsigned)&on_break;
    ((void_fn)&kinit)();
}

int main(void) {
    char *shell_argv[2];
    unsigned channel;
    int status;

    __heap_limit = PROGRAMS - PROGRAM_FILE_HEADER;  /* the first header lands there */
    in_kernel = 1u;
    k_tables();
    con_attr = CON_DEFAULT;
    con_clear();
    con_puts("PigeonOS\n");

    /* The disk it booted from (Q8): bios2 leaves the channel. */
    channel = *(unsigned *)BOOT_CHANNEL;
    if (channel != CH_HDD && channel != CH_CD) channel = CH_HDD;
    status = fs_mount(channel);
    if (status < 0) {
        con_puts("cannot mount disk ");
        con_number(channel, 10u);
        con_puts(": ");
        con_puts(fs_strerror(status));
        con_put('\n');
        return status;
    }
    mounted = channel;

    shell_argv[0] = "sh";
    shell_argv[1] = (char *)0;
    while (1) {
        status = k_exec(SHELL, 1, shell_argv);
        if (started == 0u) {
            con_puts("cannot start ");
            con_puts(SHELL);
            con_puts(": ");
            con_puts(k_strerror(status));
            con_put('\n');
            return status;
        }
        con_puts("shell ended, starting it again\n");
    }
    return 0;
}
