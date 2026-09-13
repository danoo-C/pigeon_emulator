/* The PigeonOS installer -- for now, a placeholder (docs/os_cd.md, section 8).
 *
 * user/os/pigeon_compiler_init.txt names this as the installer, so
 * cc.py --project builds it and writes it to the disc as /install.bin, and
 * the disc's boot sector loads it and jumps here.
 *
 * One day it formats the hard disk, writes the boot sector and copies the
 * disc onto it. Today it proves the disc works. It mounts the channel it
 * was booted from -- bios2 left that at BOOT_CHANNEL -- shows the title
 * cc.py --project wrote to /pigeon.txt, lists the files it would copy, and
 * halts with their number in A.
 */
#include <pigeon/display.h>
#include <pigeon/fs.h>
#include <pigeon/string.h>

#define CELL (GLYPH_W + 1)
#define ROW  (GLYPH_H + 1)
#define COLS (DISP_W / CELL)
#define Y(r) (1u + (unsigned)(r) * ROW)

#define R_TITLE 0
#define R_WHAT  1
#define R_INTRO 3
#define R_FIRST 4               /* the list runs from here to R_LAST */
#define R_LAST  10
#define R_FOOT  11
#define MAX_DEPTH 6u            /* each level holds a handle, of FS__FILES */

/* user/files.c's palette. */
#define BG   0xFF0A0C10
#define BAR  0xFF232936
#define INK  0xFFD8DEE9
#define DIM  0xFF6A7284
#define WARN 0xFFFF6B5E

unsigned files_found;           /* the files it would copy */
unsigned next_row;              /* the next row of the list */

void show_line(unsigned r, char *text, color_t ink) {
    disp_text(0u, Y(r), text, ink);
}

/* Every file under `dir` but the installer itself: listed while there is
 * room, counted either way. */
void list_files(char *dir, unsigned depth) {
    fs_stat_t st;
    char path[FS_PATH_MAX + 1];
    char shown[COLS + 1];
    int dh;

    if (depth > MAX_DEPTH) return;
    dh = fs_opendir(dir);
    if (dh < 0) return;
    while (fs_readdir(dh, &st) == 1) {
        strlcpy(path, dir, sizeof(path));
        if (strcmp(dir, "/") != 0) strlcat(path, "/", sizeof(path));
        strlcat(path, st.name, sizeof(path));
        if (st.type == FS_TYPE_DIR) {
            list_files(path, depth + 1u);
        } else if (strcmp(path, "/install.bin") != 0) {
            files_found++;
            if (next_row <= R_LAST) {
                strlcpy(shown, "  ", sizeof(shown));
                strlcat(shown, path, sizeof(shown));
                show_line(next_row, shown, INK);
                next_row++;
            }
        }
    }
    fs_closedir(dh);
}

int main(void) {
    char text[256];
    char title[COLS + 1];
    char foot[COLS + 1];
    char digits[12];
    unsigned i;
    int r;

    disp_clear(BG);
    disp_rect(0u, 0u, DISP_W, ROW + 1u, BAR);
    r = fs_mount(*(unsigned *)BOOT_CHANNEL);
    if (r < 0) {
        show_line(R_TITLE, "INSTALLER", INK);
        show_line(R_INTRO, "Boot this from its disc.", WARN);
        show_line(R_FIRST, fs_strerror(r), DIM);
        return r;
    }

    /* The title is the first line of /pigeon.txt. */
    strlcpy(title, "(no /pigeon.txt)", sizeof(title));
    r = fs_load("/pigeon.txt", text, sizeof(text) - 1u);
    if (r > 0) {
        text[r] = 0;
        for (i = 0u; i < (unsigned)COLS && text[i] != 0 && text[i] != 10; i++) {
            title[i] = text[i];
        }
        title[i] = 0;
    }
    show_line(R_TITLE, title, INK);
    show_line(R_WHAT, "INSTALLER", DIM);
    show_line(R_INTRO, "Would copy to the hard disk:", DIM);

    files_found = 0u;
    next_row = R_FIRST;
    list_files("/", 0u);

    utoa(files_found, digits, 10u);
    strlcpy(foot, digits, sizeof(foot));
    strlcat(foot, " files. Nothing installed yet.", sizeof(foot));
    show_line(R_FOOT, foot, WARN);
    return (int)files_found;
}
