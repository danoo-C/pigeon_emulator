/* Video memory, CH_VRAM, from a program's side. See vram.h, and
 * emulator/devices/vram.py for the device.
 *
 * Every command is sent with R/W 0: its arguments are words in the data
 * window, and its reply comes back in the same window.
 */
#include <pigeon/io.h>
#include <pigeon/vram.h>

/* Prefixed: units share one macro table, and a bare CMD_INFO would
 * silently replace another library's. */
#define VRAM_CMD_INFO        1u
#define VRAM_CMD_GET_MODE    2u
#define VRAM_CMD_SET_MODE    3u
#define VRAM_CMD_MODE_COUNT  4u
#define VRAM_CMD_MODE_AT     5u
#define VRAM_CMD_PREFERRED   6u
#define VRAM_CMD_ALLOC       7u
#define VRAM_CMD_FREE        8u
#define VRAM_CMD_SCANOUT     9u
#define VRAM_CMD_SCANOUT_RAM 10u
#define VRAM_CMD_OWNER       13u
#define VRAM_CMD_FREE_OWNED  14u
#define VRAM_MAGIC           0x52564750u      /* "PGVR" */

/* 0 = not asked yet, 1 = there, 2 = not. */
static unsigned vram_there = 0u;
static unsigned vram_bytes = 0u;
static unsigned vram_base = 0u;

/* One command: its reply's length, which is also how a missing device
 * shows (0xFFFFFFFF from an empty channel). */
static unsigned vram_call(unsigned command, unsigned address) {
    IO_RW   = 0u;
    IO_CMD  = command;
    IO_LEN  = 4u;
    IO_ADDR = address;
    IO_CH   = CH_VRAM;             /* this store fires it -- must be last */
    return IO_RETLEN;
}

/* display.c's probe, for this channel: no controller leaves the channel
 * where it was put; an empty channel answers 0xFFFFFFFF, which is enormous
 * as a length, so it is tested before anything reads the window; and a
 * stale window could say anything, which is what the magic is for. */
int vram_present(void) {
    if (vram_there == 0u) {
        vram_there = 2u;
        IO_RW   = 0u;
        IO_CMD  = VRAM_CMD_INFO;
        IO_LEN  = 4u;
        IO_ADDR = 0u;
        IO_CH   = CH_VRAM;
        if (IO_CH != 0u) {
            IO_CH = 0u;
        } else if (IO_RETLEN != 0xFFFFFFFFu && IO_RETLEN >= 16u && IO_DATAW[0] == VRAM_MAGIC) {
            vram_bytes = IO_DATAW[1];
            vram_base = IO_DATAW[2];
            vram_there = 1u;
        }
    }
    return vram_there == 1u;
}

unsigned vram_size(void) {
    return vram_present() ? vram_bytes : 0u;
}

unsigned vram_aperture(void) {
    return vram_present() ? vram_base : 0u;
}

unsigned vram_generation(void) {
    if (!vram_present()) return 0u;
    vram_call(VRAM_CMD_INFO, 0u);
    return IO_DATAW[3];
}

int vram_mode(unsigned *w, unsigned *h, unsigned *offset) {
    if (!vram_present()) return 0;
    vram_call(VRAM_CMD_GET_MODE, 0u);
    *w = IO_DATAW[0];
    *h = IO_DATAW[1];
    *offset = IO_DATAW[4];
    return 1;
}

int vram_set_mode(unsigned w, unsigned h) {
    if (!vram_present()) return 0;
    IO_DATAW[0] = w;
    IO_DATAW[1] = h;
    vram_call(VRAM_CMD_SET_MODE, 0u);
    return IO_DATAW[0] == 1u;
}

unsigned vram_mode_count(void) {
    if (!vram_present()) return 0u;
    vram_call(VRAM_CMD_MODE_COUNT, 0u);
    return IO_DATAW[0];
}

int vram_mode_at(unsigned i, unsigned *w, unsigned *h) {
    if (!vram_present()) return 0;
    vram_call(VRAM_CMD_MODE_AT, i);
    *w = IO_DATAW[0];
    *h = IO_DATAW[1];
    return *w != 0u;
}

unsigned vram_preferred(unsigned *w, unsigned *h) {
    *w = 0u;
    *h = 0u;
    if (!vram_present()) return 0u;
    vram_call(VRAM_CMD_PREFERRED, 0u);
    *w = IO_DATAW[0];
    *h = IO_DATAW[1];
    return IO_DATAW[2];
}

unsigned vram_alloc(unsigned w, unsigned h, unsigned *offset) {
    if (!vram_present()) return 0u;
    IO_DATAW[0] = w;
    IO_DATAW[1] = h;
    vram_call(VRAM_CMD_ALLOC, 0u);
    *offset = IO_DATAW[1];
    return IO_DATAW[0];
}

int vram_free(unsigned handle) {
    if (!vram_present()) return 0;
    vram_call(VRAM_CMD_FREE, handle);
    return IO_DATAW[0] == 1u;
}

int vram_scanout(unsigned handle) {
    if (!vram_present()) return 0;
    vram_call(VRAM_CMD_SCANOUT, handle);
    return IO_DATAW[0] == 1u;
}

int vram_scanout_ram(unsigned address) {
    if (!vram_present()) return 0;
    vram_call(VRAM_CMD_SCANOUT_RAM, address);
    return IO_DATAW[0] == 1u;
}

int vram_owner(unsigned owner) {
    if (!vram_present()) return 0;
    vram_call(VRAM_CMD_OWNER, owner);
    return 1;
}

unsigned vram_free_owned(unsigned owner) {
    if (!vram_present()) return 0u;
    vram_call(VRAM_CMD_FREE_OWNED, owner);
    return IO_DATAW[0];
}
