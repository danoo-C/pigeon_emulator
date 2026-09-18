/* The PigeonOS installer (docs/os_cd.md, section 8).
 *
 * user/os/pigeon_compiler_init.txt names this as the installer, so
 * cc.py --project builds it onto the disc as /install.bin, and the disc's
 * boot sector loads it and jumps here. It puts the disc on the hard disk:
 *
 *   1. Mount the disc -- the channel bios2 left at BOOT_CHANNEL -- show
 *      what will happen, and ask, because everything on the hard disk is
 *      erased. ESC cancels before anything is written.
 *   2. Format the hard disk, with the disc's volume label.
 *   3. Copy /boot.bin first, so its blocks are one run on the fresh disk,
 *      then every other file on the disc but /install.bin.
 *   4. Unmount the hard disk, so fs.c holds no copy of block 0, and write
 *      block 0's boot record and boot sector directly. The sector is the
 *      disc's own. The record points at /boot.bin, once its blocks are
 *      checked to be the one run the boot sector needs -- read straight
 *      from the root directory and the FAT, since fs.h has no call that
 *      says where a file's blocks are (docs/filesystem.md, section 3).
 *   5. ENTER restarts, by calling address 0, where the BIOS still is. The
 *      hard disk comes before the CD, so the hard disk is what boots.
 *
 * Each step is said on the debug port too, each line starting
 * "[installer] " (docs/phase5b_plan.md step 3), every file copied and every
 * failure included.
 */
#include <pigeon/debug.h>
#include <pigeon/display.h>
#include <pigeon/fs.h>
#include <pigeon/input.h>
#include <pigeon/io.h>
#include <pigeon/mem.h>
#include <pigeon/string.h>

#define CELL (GLYPH_W + 1)
#define ROW  (GLYPH_H + 1)
#define COLS (DISP_W / CELL)
/* Arrays are sized for the widest screen there is, DISPLAY_MAX_W: COLS is
 * the screen's own, worked out at run time now, and cannot size one. */
#define MAX_COLS  (DISPLAY_MAX_W / CELL)
#define Y(r) (1u + (unsigned)(r) * ROW)

#define R_TITLE  0
#define R_WHAT   1
#define R_DISK   3
#define R_ERASE  4
#define R_FILES  6
#define R_BOOTS  7
#define R_STATUS 9
#define R_DETAIL 10
#define R_KEYS   11

/* user/files.c's palette. */
#define BG   0xFF0A0C10
#define BAR  0xFF232936
#define INK  0xFFD8DEE9
#define DIM  0xFF6A7284
#define WARN 0xFFFF6B5E
#define GOOD 0xFF62D58A

#define HDD_GET_SIZE 1u
#define HDD_READ     2u
#define HDD_WRITE    3u
#define FAT_START    1u             /* fs.c's FS__FAT_START; mount refuses any other */
#define FAT_EOC      0xFFFFFFFFu
#define ENTRY_WORDS  16u            /* a directory entry is 64 bytes */
#define MAX_DEPTH    6u             /* each level holds one directory handle */

#define INSTALLER_PATH "/install.bin"
#define SYSTEM_PATH    "/boot.bin"
#define NO_BOOT   (-200)            /* /boot.bin could not be made to boot */

unsigned char block[BOOT_BLOCK];    /* a raw block, just read or about to be written */
#define WORD(i) (((unsigned *)block)[i])

unsigned disc;                      /* the channel the disc is in */
unsigned files_total;               /* files on the disc, but the installer */
unsigned files_done;

/* --- the screen ----------------------------------------------------------- */

void show(int row, char *text, color_t ink) {
    disp_rect(0u, Y(row) - 1u, DISP_W, ROW, BG);
    disp_text(0u, Y(row), text, ink);
}

int wait_for(int one, int other) {
    int key;
    for (;;) {
        key = key_read();
        if (key == one || key == other) return key;
    }
}

/* --- raw blocks ----------------------------------------------------------- */

unsigned fire(unsigned channel, unsigned read_write, unsigned command,
              unsigned length, unsigned address) {
    IO_RW = read_write;
    IO_CMD = command;
    IO_LEN = length;
    IO_ADDR = address;
    IO_CH = channel;
    return IO_RETLEN;
}

/* The bytes on a disk channel; 0 with no disk. */
unsigned disk_bytes(unsigned channel) {
    if (fire(channel, 0u, HDD_GET_SIZE, 8u, 0u) != 8u) return 0u;
    if (IO_DATAW[1] != 0u) return 0xFFFFFFFFu;
    return IO_DATAW[0];
}

/* Block n of a disk into `block`. 1 when the whole block came back. */
int read_block(unsigned channel, unsigned n) {
    unsigned i;
    if (fire(channel, 0u, HDD_READ, BOOT_BLOCK, n * BOOT_BLOCK) != BOOT_BLOCK) return 0;
    for (i = 0u; i < BOOT_BLOCK / 4u; i++) WORD(i) = IO_DATAW[i];
    return 1;
}

/* `block` onto a disk, as block n. */
void write_block(unsigned channel, unsigned n) {
    unsigned i;
    for (i = 0u; i < BOOT_BLOCK / 4u; i++) IO_DATAW[i] = WORD(i);
    fire(channel, 1u, HDD_WRITE, BOOT_BLOCK, n * BOOT_BLOCK);
}

/* --- files ---------------------------------------------------------------- */

/* A path on one volume, the way fs.h names it: "6:/bin/cube.bin". */
void on(char *out, unsigned channel, char *path) {
    unsigned n;
    n = (unsigned)utoa(channel, out, 10u);
    out[n] = ':';
    out[n + 1u] = 0;
    strlcat(out, path, FS_PATH_MAX + 8u);
}

void join(char *out, char *dir, char *name) {
    strlcpy(out, dir, FS_PATH_MAX + 1u);
    if (strcmp(dir, "/") != 0) strlcat(out, "/", FS_PATH_MAX + 1u);
    strlcat(out, name, FS_PATH_MAX + 1u);
}

void progress(char *path) {
    char line[MAX_COLS + 1];
    char digits[12];
    strlcpy(line, "Copying ", sizeof(line));
    utoa(files_done, digits, 10u);
    strlcat(line, digits, sizeof(line));
    strlcat(line, " of ", sizeof(line));
    utoa(files_total, digits, 10u);
    strlcat(line, digits, sizeof(line));
    show(R_STATUS, line, INK);
    show(R_DETAIL, path, DIM);
}

/* One file from the disc to the same path on the hard disk. */
int copy_file(char *path) {
    char from[FS_PATH_MAX + 8];
    char to[FS_PATH_MAX + 8];
    fs_stat_t st;
    char *data;
    int n;
    int r;

    files_done++;
    progress(path);
    dbg_printf("[installer] copying %s\n", path);
    on(from, disc, path);
    on(to, CH_HDD, path);
    r = fs_stat(from, &st);
    if (r < 0) return r;
    data = (char *)malloc(st.size + 1u);
    if (data == NULL) return FS_ENOMEM;
    n = fs_load(from, data, st.size + 1u);
    r = n;
    if (n >= 0) {
        r = fs_save(to, data, (unsigned)n);
        if (r >= 0 && r != n) r = FS_ENOSPC;
    }
    free(data);
    if (r < 0) dbg_printf("[installer] %s: %s\n", path, fs_strerror(r));
    return (r < 0) ? r : 0;
}

/* Every file under `dir` on the disc but the installer: counted, or, with
 * `copying`, copied -- all but /boot.bin, which install() copies first. */
int walk(char *dir, unsigned depth, int copying) {
    fs_stat_t st;
    char from[FS_PATH_MAX + 8];
    char to[FS_PATH_MAX + 8];
    char path[FS_PATH_MAX + 1];
    int dh;
    int r;

    if (depth > MAX_DEPTH) return FS_ENAMETOOLONG;
    on(from, disc, dir);
    dh = fs_opendir(from);
    if (dh < 0) return dh;
    r = 0;
    while (r >= 0 && fs_readdir(dh, &st) == 1) {
        join(path, dir, st.name);
        if (st.type == FS_TYPE_DIR) {
            if (copying) {
                on(to, CH_HDD, path);
                r = fs_mkdir(to);
                if (r == FS_EEXIST) r = 0;
            }
            if (r >= 0) r = walk(path, depth + 1u, copying);
        } else if (strcmp(path, INSTALLER_PATH) != 0) {
            if (!copying) files_total++;
            else if (strcmp(path, SYSTEM_PATH) != 0) r = copy_file(path);
        }
    }
    fs_closedir(dh);
    return r;
}

int system_on_disc(void) {
    char from[FS_PATH_MAX + 8];
    fs_stat_t st;
    on(from, disc, SYSTEM_PATH);
    return fs_stat(from, &st) == FS_OK && st.type == FS_TYPE_FILE;
}

/* --- making the hard disk boot -------------------------------------------- */

/* Where /boot.bin starts on the hard disk and how big it is -- 1 when its
 * blocks are one run, each linked to the next and the last ending the chain. */
int system_extent(unsigned *first, unsigned *size) {
    unsigned slot;
    unsigned blocks;
    unsigned k;
    unsigned fat;
    unsigned loaded;
    unsigned want;

    if (!read_block(CH_HDD, 0u)) return 0;
    if (!read_block(CH_HDD, WORD(25))) return 0;       /* the root's first block, byte 100 */
    *first = 0u;
    *size = 0u;
    for (slot = 0u; slot < BOOT_BLOCK / 64u; slot++) {
        if (WORD(slot * ENTRY_WORDS + 8u) == FS_TYPE_FILE
                && strcmp((char *)block + slot * 64u, "boot.bin") == 0) {
            *first = WORD(slot * ENTRY_WORDS + 9u);
            *size = WORD(slot * ENTRY_WORDS + 10u);
        }
    }
    if (*first == 0u || *size == 0u || *size > PROGRAM_MAX_SIZE) return 0;
    blocks = (*size + BOOT_BLOCK - 1u) / BOOT_BLOCK;
    loaded = 0xFFFFFFFFu;
    for (k = 0u; k < blocks; k++) {
        fat = FAT_START + (*first + k) / 128u;
        if (fat != loaded) {
            if (!read_block(CH_HDD, fat)) return 0;
            loaded = fat;
        }
        want = (k + 1u == blocks) ? FAT_EOC : *first + k + 1u;
        if (WORD((*first + k) % 128u) != want) return 0;
    }
    return 1;
}

/* Block 0 of the hard disk gets the disc's boot sector, and a boot record
 * for /boot.bin. Read back to be sure it is there. */
int make_bootable(void) {
    unsigned sector[(BOOT_BLOCK - BOOT_CODE) / 4];
    unsigned first;
    unsigned size;
    unsigned i;

    if (!system_extent(&first, &size)) return 0;
    if (!read_block(disc, 0u)) return 0;
    for (i = 0u; i < (BOOT_BLOCK - BOOT_CODE) / 4u; i++) sector[i] = WORD(BOOT_CODE / 4u + i);
    if (!read_block(CH_HDD, 0u)) return 0;
    WORD(BOOT_RECORD / 4u) = BOOT_SIGNATURE;
    WORD(BOOT_RECORD / 4u + 1u) = first;
    WORD(BOOT_RECORD / 4u + 2u) = size;
    for (i = 0u; i < (BOOT_BLOCK - BOOT_CODE) / 4u; i++) WORD(BOOT_CODE / 4u + i) = sector[i];
    write_block(CH_HDD, 0u);
    if (!read_block(CH_HDD, 0u)) return 0;
    if (WORD(BOOT_RECORD / 4u) != BOOT_SIGNATURE || WORD(BOOT_RECORD / 4u + 1u) != first) return 0;
    dbg_printf("[installer] boot record: %s, block %u, %u bytes\n", SYSTEM_PATH, first, size);
    return 1;
}

int install(int system) {
    fs_volinfo vi;
    int r;

    show(R_STATUS, "Formatting the hard disk", INK);
    r = fs_statvfs(disc, &vi);
    if (r < 0) return r;
    dbg_printf("[installer] formatting the hard disk as %s\n", vi.label);
    r = fs_format(CH_HDD, vi.label, FS_FORMAT_FORCE);
    if (r < 0) return r;
    r = fs_mount(CH_HDD);
    if (r < 0) return r;
    files_done = 0u;
    if (system) {
        r = copy_file(SYSTEM_PATH);
        if (r < 0) return r;
    }
    r = walk("/", 0u, 1);
    if (r < 0) return r;
    r = fs_unmount(CH_HDD);
    if (r < 0) return r;
    if (system && !make_bootable()) return NO_BOOT;
    return FS_OK;
}

int main(void) {
    char from[FS_PATH_MAX + 8];
    char text[256];
    char line[MAX_COLS + 1];
    char digits[12];
    unsigned bytes;
    unsigned i;
    int system;
    int r;

    disp_init();  /* the screen, as the machine has it (display.h) */
    disp_clear(BG);
    disp_rect(0u, 0u, DISP_W, ROW + 1u, BAR);
    disc = *(unsigned *)BOOT_CHANNEL;
    r = fs_mount(disc);
    if (r < 0) {
        dbg_printf("[installer] cannot mount the disc on channel %u: %s\n", disc, fs_strerror(r));
        disp_text(0u, Y(R_TITLE), "INSTALLER", INK);
        show(R_STATUS, "Boot this from its disc.", WARN);
        show(R_DETAIL, fs_strerror(r), DIM);
        return r;
    }

    /* The title is the first line of /pigeon.txt. */
    strlcpy(line, "(no /pigeon.txt)", sizeof(line));
    on(from, disc, "/pigeon.txt");
    r = fs_load(from, text, sizeof(text) - 1u);
    if (r > 0) {
        text[r] = 0;
        for (i = 0u; i < (unsigned)COLS && text[i] != 0 && text[i] != 10; i++) line[i] = text[i];
        line[i] = 0;
    }
    disp_text(0u, Y(R_TITLE), line, INK);
    show(R_WHAT, "INSTALLER", DIM);
    dbg_printf("[installer] disc on channel %u: %s\n", disc, line);

    bytes = disk_bytes(CH_HDD);
    if (bytes == 0u) {
        dbg_print("[installer] no hard disk\n");
        show(R_STATUS, "There is no hard disk.", WARN);
        return FS_ENODEV;
    }
    strlcpy(line, "Hard disk: ", sizeof(line));
    utoa(bytes / 1024u, digits, 10u);
    strlcat(line, digits, sizeof(line));
    strlcat(line, " K", sizeof(line));
    show(R_DISK, line, INK);
    show(R_ERASE, "Everything on it is erased.", WARN);
    dbg_printf("[installer] hard disk: %u K\n", bytes / 1024u);

    files_total = 0u;
    walk("/", 0u, 0);
    utoa(files_total, digits, 10u);
    strlcpy(line, digits, sizeof(line));
    strlcat(line, " files to copy", sizeof(line));
    show(R_FILES, line, INK);
    system = system_on_disc();
    if (system) show(R_BOOTS, "It will boot /boot.bin", INK);
    else show(R_BOOTS, "Nothing on it will boot", WARN);
    if (system) dbg_printf("[installer] %u files to copy; %s will boot\n", files_total, SYSTEM_PATH);
    else dbg_printf("[installer] %u files to copy; nothing on it will boot\n", files_total);
    show(R_KEYS, "ENTER install   ESC cancel", DIM);

    if (wait_for(KEY_ENTER, KEY_ESC) == KEY_ESC) {
        dbg_print("[installer] Esc: cancelled, nothing written\n");
        show(R_STATUS, "Cancelled: nothing was changed.", INK);
        show(R_KEYS, "", DIM);
        return 0;
    }
    show(R_KEYS, "", DIM);
    dbg_print("[installer] Enter: installing\n");
    r = install(system);
    if (r < 0) {
        show(R_STATUS, "The install failed:", WARN);
        if (r == NO_BOOT) {
            dbg_print("[installer] failed: the hard disk would not boot\n");
            show(R_DETAIL, "the hard disk would not boot", DIM);
        } else {
            dbg_printf("[installer] failed: %s\n", fs_strerror(r));
            show(R_DETAIL, fs_strerror(r), DIM);
        }
        return r;
    }
    strlcpy(line, "Installed ", sizeof(line));
    utoa(files_done, digits, 10u);
    strlcat(line, digits, sizeof(line));
    strlcat(line, " files.", sizeof(line));
    show(R_STATUS, line, GOOD);
    if (system) show(R_DETAIL, "The hard disk boots /boot.bin.", DIM);
    else show(R_DETAIL, "", DIM);
    dbg_printf("[installer] installed %u files; Enter restarts\n", files_done);
    show(R_KEYS, "ENTER restart", DIM);
    wait_for(KEY_ENTER, KEY_ENTER);
    dbg_print("[installer] restarting\n");
    ((void (*)(void))0)();                  /* the BIOS, still at address 0 */
    return 0;
}
