/* bios2 -- the second stage of the BIOS (docs/os_cd.md).
 *
 * firmware/bios.asm has 1 KB and no room for a font. So it loads this off
 * the read-only firmware device on CH_BIOS2, in one READ_DMA, to
 * BIOS2_LOAD_ADDR, and jumps here. The launcher builds it for that address
 * (cc.py --org BIOS2_LOAD_ADDR). Only the code and data move: the frame
 * stack and heap stay at HEAP_START, above anything this loads.
 *
 * It checks three devices, in the order it boots them:
 *
 *   Program    channel 1, what the launcher was asked to run. Bootable
 *              with 1 byte to PROGRAM_MAX_SIZE -- which is where this
 *              file's own frame stack begins.
 *   Hard disk  channel 2. Bootable when block 0 carries BOOT_SIGNATURE at
 *   CD         channel 6. BOOT_RECORD. That is the only check; the volume
 *              label is read just to show a name.
 *
 * A two-second countdown boots the first device that can boot. Enter boots
 * it at once. Esc opens the menu instead, where the arrow keys choose and
 * Enter boots. With nothing to boot it waits in the menu, watching the CD
 * drive's generation counter, so a disc put in from a front end shows up
 * without a key.
 *
 * Handing over. A program is copied to PROGRAM_LOAD_ADDR with one
 * READ_DMA. A disk's block 0 is copied to BOOT_LOAD_ADDR, the channel is
 * written to BOOT_CHANNEL, and the boot sector's code at BOOT_ENTRY runs.
 * First, either way, the timer stops, the screen is cleared to 0 and the
 * input queues are emptied, so the program sees none of the keys the menu
 * read. A boot that fails comes back to the menu and says why.
 *
 * C cannot jump, so the hand-over is a CALL: the program starts a few
 * return addresses down the hardware stack. Nothing expects them back;
 * programs end in HALT.
 */
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/io.h>
#include <pigeon/string.h>

/* --- layout: 32 columns by 12 rows, as user/files.c derives them --------- */
#define CELL (GLYPH_W + 1)
#define ROW  (GLYPH_H + 1)
#define COLS (DISP_W / CELL)
#define X(c) ((unsigned)(c) * CELL)
#define Y(r) (1u + (unsigned)(r) * ROW)

#define R_TITLE  0
#define R_RAM    1
#define R_FIRST  3                  /* the devices, one row each */
#define R_STATUS 7
#define R_KEYS   11
#define C_NAME   2
#define C_DETAIL 13

/* user/files.c's palette: every ink bright, every background dark. */
#define BG     0xFF0A0C10
#define PANEL  0xFF181C24
#define BAR    0xFF232936
#define ACCENT 0xFF30C0FF
#define INK    0xFFD8DEE9
#define DIM    0xFF6A7284
#define WARN   0xFFFF6B5E

/* --- devices -------------------------------------------------------------- */
#define HDD_GET_SIZE 1u
#define HDD_READ     2u
#define HDD_READ_DMA 6u
#define CD_MEDIA     8u
#define CD_MEDIA_LEN 48u
#define CD_MAGIC     0x44434750u    /* "PGCD", first in a MEDIA reply */
#define PGFS_MAGIC   0x53464750u    /* "PGFS", first in block 0 */
#define LABEL_AT     36u            /* the volume label, in block 0 */
#define LABEL_MAX    15u

#define TIMER_ID      1u
#define TIMER_START   1u
#define TIMER_STOP    2u
#define TIMER_STATUS  5u
#define TIMER_RUNNING 1u
#define COUNTDOWN_MS  2000u

#define DEVICES   3
#define D_PROGRAM 0
#define D_DISK    1
#define D_CD      2
#define DETAIL    20

struct device {
    char    *name;
    unsigned channel;
    int      bootable;
    unsigned size;                  /* the program's, on channel 1 */
    char     detail[DETAIL];        /* what the screen shows after the name */
};

typedef void (*entry_fn)(void);

struct device devices[DEVICES];
char message[COLS + 1];             /* why the last boot failed; "" if none */

/* --- IO ------------------------------------------------------------------- */

/* Program the header and fire. A reply is left in the window. */
unsigned fire(unsigned channel, unsigned read_write, unsigned command,
              unsigned length, unsigned address) {
    IO_RW = read_write;
    IO_CMD = command;
    IO_LEN = length;
    IO_ADDR = address;
    IO_CH = channel;
    return IO_RETLEN;
}

/* The bytes a disk channel holds, or 0 when it holds no disk: a disk
 * answers GET_SIZE with exactly 8 bytes, an empty channel 0xFFFFFFFF. */
unsigned disk_size(unsigned channel) {
    if (fire(channel, 0u, HDD_GET_SIZE, 8u, 0u) != 8u) return 0u;
    if (IO_DATAW[1] != 0u) return 0xFFFFFFFFu;         /* 4 GB or more */
    return IO_DATAW[0];
}

/* Block 0 of a disk, into the window. 1 when the whole block came back. */
int read_block0(unsigned channel) {
    return fire(channel, 0u, HDD_READ, BOOT_BLOCK, 0u) == BOOT_BLOCK;
}

/* Copy the first `size` bytes of a disk to `address` in one READ_DMA,
 * with [address, count] in the window. 1 when every byte arrived. */
int load(unsigned channel, unsigned address, unsigned size) {
    IO_DATAW[0] = address;
    IO_DATAW[1] = size;
    fire(channel, 0u, HDD_READ_DMA, 8u, 0u);           /* R/W 0: the count comes back */
    return IO_DATAW[0] == size;
}

/* 1 with a disc in the CD drive. *generation gets the drive's counter,
 * which moves on every insert and eject -- 0 when there is no drive. */
int cd_media(unsigned *generation) {
    *generation = 0u;
    if (fire(CH_CD, 0u, CD_MEDIA, CD_MEDIA_LEN, 0u) != CD_MEDIA_LEN) return 0;
    if (IO_DATAW[0] != CD_MAGIC) return 0;
    *generation = IO_DATAW[2];
    return IO_DATAW[1] != 0u;
}

void timer(unsigned command, unsigned ms) {
    fire(CH_TIMER, 0u, command, ms, TIMER_ID);
}

/* Milliseconds left on the countdown; 0 once it has run out. */
unsigned countdown_left(void) {
    fire(CH_TIMER, 0u, TIMER_STATUS, 8u, TIMER_ID);
    if (IO_DATAW[0] != TIMER_RUNNING) return 0u;
    return IO_DATAW[1];
}

/* --- what each device holds ----------------------------------------------- */

void check_program(void) {
    struct device *d = &devices[D_PROGRAM];
    char digits[12];

    d->bootable = 0;
    d->size = disk_size(CH_USERPROG);
    if (d->size == 0u) {
        strlcpy(d->detail, "none", DETAIL);
        return;
    }
    if (d->size > PROGRAM_MAX_SIZE) {
        strlcpy(d->detail, "too big", DETAIL);
        return;
    }
    d->bootable = 1;
    utoa(d->size, digits, 10u);
    strlcpy(d->detail, digits, DETAIL);
    strlcat(d->detail, " bytes", DETAIL);
}

void check_disk(int index) {
    struct device *d = &devices[index];
    unsigned generation;
    unsigned k;

    d->bootable = 0;
    if (d->channel == CH_CD && !cd_media(&generation)) {
        strlcpy(d->detail, "no disc", DETAIL);
        return;
    }
    if (disk_size(d->channel) < BOOT_BLOCK || !read_block0(d->channel)) {
        strlcpy(d->detail, (d->channel == CH_CD) ? "no disc" : "no disk", DETAIL);
        return;
    }
    if (IO_DATAW[BOOT_RECORD / 4] != BOOT_SIGNATURE) {
        strlcpy(d->detail, "no boot sector", DETAIL);
        return;
    }
    d->bootable = 1;
    strlcpy(d->detail, "bootable", DETAIL);
    if (IO_DATAW[0] == PGFS_MAGIC && IO_DATA[LABEL_AT] != 0) {
        for (k = 0u; k < LABEL_MAX && IO_DATA[LABEL_AT + k] != 0; k++) {
            d->detail[k] = (char)IO_DATA[LABEL_AT + k];
        }
        d->detail[k] = 0;
    }
}

void check_all(void) {
    check_program();
    check_disk(D_DISK);
    check_disk(D_CD);
}

int first_bootable(void) {
    int i;
    for (i = 0; i < DEVICES; i++) {
        if (devices[i].bootable) return i;
    }
    return -1;
}

void say(char *name, char *what) {
    strlcpy(message, name, COLS + 1);
    strlcat(message, what, COLS + 1);
}

/* --- the screen ----------------------------------------------------------- */

/* One row of text, on a strip cleared first. */
void draw_line(int row, char *text, color_t ink) {
    disp_rect(0u, Y(row) - 1u, DISP_W, ROW, BG);
    disp_text(X(0), Y(row), text, ink);
}

void draw_device(int index, int selected) {
    struct device *d = &devices[index];
    color_t ink;
    unsigned y;

    y = Y(R_FIRST + index);
    disp_rect(0u, y - 1u, DISP_W, ROW, selected ? PANEL : BG);
    ink = d->bootable ? INK : DIM;
    if (selected) {
        disp_char(X(0), y, '>', ACCENT);
        if (d->bootable) ink = ACCENT;
    }
    disp_text(X(C_NAME), y, d->name, ink);
    disp_text(X(C_DETAIL), y, d->detail, ink);
}

void draw_devices(int selected) {
    int i;
    for (i = 0; i < DEVICES; i++) draw_device(i, i == selected);
}

void draw_frame(void) {
    char line[COLS + 1];

    disp_clear(BG);
    disp_rect(0u, 0u, DISP_W, ROW + 1u, BAR);
    disp_text(X(0), Y(R_TITLE), "PIGEON BIOS", INK);
    utoa(RAM_SIZE / 1048576u, line, 10u);
    strlcat(line, " MB RAM", COLS + 1);
    disp_text(X(0), Y(R_RAM), line, DIM);
}

void draw_countdown(int first, unsigned seconds) {
    char line[COLS + 1];
    char digits[12];

    draw_devices(-1);
    strlcpy(line, "Booting ", COLS + 1);
    strlcat(line, devices[first].name, COLS + 1);
    strlcat(line, " in ", COLS + 1);
    utoa(seconds, digits, 10u);
    strlcat(line, digits, COLS + 1);
    draw_line(R_STATUS, line, INK);
    draw_line(R_KEYS, "ESC menu    ENTER boot now", DIM);
}

void draw_menu(int selected) {
    draw_devices(selected);
    if (message[0] != 0) draw_line(R_STATUS, message, WARN);
    else if (first_bootable() < 0) draw_line(R_STATUS, "Nothing to boot", WARN);
    else draw_line(R_STATUS, "BOOT MENU", ACCENT);
    draw_line(R_KEYS, "UP DOWN choose   ENTER boot", DIM);
}

/* --- handing over --------------------------------------------------------- */

void hand_over(unsigned entry) {
    timer(TIMER_STOP, 0u);
    disp_clear(0u);
    while (key_read() != -1) { }
    while (key_event() != 0u) { }
    while (mouse_event() != 0u) { }
    ((entry_fn)entry)();
}

/* Boot device `index`. Comes back only if that failed, with why in message. */
void boot(int index) {
    struct device *d = &devices[index];
    unsigned k;

    message[0] = 0;
    if (d->channel == CH_USERPROG) {
        if (!load(CH_USERPROG, PROGRAM_LOAD_ADDR, d->size)) {
            say(d->name, ": load failed");
            return;
        }
        hand_over(PROGRAM_LOAD_ADDR);
        /* A program ends in HALT; one that returns instead lands here. */
        draw_frame();
        say(d->name, ": ended");
        return;
    }
    /* Read it again: the disc may have changed since the screen was drawn. */
    if (!read_block0(d->channel) || IO_DATAW[BOOT_RECORD / 4] != BOOT_SIGNATURE) {
        say(d->name, ": no boot sector");
        return;
    }
    for (k = 0u; k < BOOT_BLOCK / 4u; k++) ((unsigned *)BOOT_LOAD_ADDR)[k] = IO_DATAW[k];
    *(unsigned *)BOOT_CHANNEL = d->channel;
    hand_over(BOOT_ENTRY);
    /* A boot sector returns when it cannot load its file (firmware/boot.asm).
     * The screen was cleared for it, so the frame is drawn again. */
    draw_frame();
    say(d->name, ": boot failed");
}

/* --- the menu --------------------------------------------------------------- */

void menu(void) {
    int selected;
    int key;
    int redraw;
    unsigned seen;
    unsigned now;

    selected = first_bootable();
    if (selected < 0) selected = 0;
    cd_media(&seen);
    redraw = 1;
    for (;;) {
        if (redraw) {
            draw_menu(selected);
            redraw = 0;
        }
        key = key_read();
        if (key == KEY_UP && selected > 0) {
            selected--;
            redraw = 1;
        }
        if (key == KEY_DOWN && selected < DEVICES - 1) {
            selected++;
            redraw = 1;
        }
        if (key == KEY_ENTER) {
            if (devices[selected].bootable) {
                boot(selected);
                check_all();
            } else {
                say(devices[selected].name, ": can't boot");
            }
            redraw = 1;
        }
        cd_media(&now);
        if (now != seen) {
            seen = now;
            check_disk(D_CD);
            message[0] = 0;
            if (!devices[selected].bootable && first_bootable() >= 0) {
                selected = first_bootable();
            }
            redraw = 1;
        }
    }
}

int main(void) {
    int first;
    int key;
    unsigned seconds;
    unsigned shown;

    devices[D_PROGRAM].name = "Program";
    devices[D_PROGRAM].channel = CH_USERPROG;
    devices[D_DISK].name = "Hard disk";
    devices[D_DISK].channel = CH_HDD;
    devices[D_CD].name = "CD";
    devices[D_CD].channel = CH_CD;
    message[0] = 0;

    draw_frame();
    check_all();
    first = first_bootable();
    if (first >= 0) {
        timer(TIMER_START, COUNTDOWN_MS);
        shown = 0xFFFFFFFFu;
        for (;;) {
            seconds = (countdown_left() + 999u) / 1000u;
            if (seconds != shown) {
                draw_countdown(first, seconds);
                shown = seconds;
            }
            key = key_read();
            if (key == KEY_ESC) break;
            if (key == KEY_ENTER || seconds == 0u) {
                boot(first);                    /* back here only if that failed */
                check_all();
                break;
            }
        }
        timer(TIMER_STOP, 0u);
    }
    menu();
    return 0;
}
