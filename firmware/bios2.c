/* bios2 -- the second stage of the BIOS (docs/os_cd.md).
 *
 * firmware/bios.asm has 1 KB and no room for a font. So it loads this off
 * the read-only firmware device on CH_BIOS2, in one READ_DMA, to
 * BIOS2_LOAD_ADDR, and jumps here. The launcher builds it for that address
 * (cc.py --org BIOS2_LOAD_ADDR). Only the code and data move: the frame
 * stack and heap stay at HEAP_START, above anything this loads.
 *
 * Phase 2: no screen yet. It boots the program on channel 1, as stage 1
 * does -- but in one DMA transfer instead of a loop over the 4 KB window,
 * and with no progress bar and no two-second wait. The screen, the
 * countdown and the boot menu are phase 3; a disk's boot sector is phase 4.
 *
 * When nothing boots, main returns and the startup code halts with the
 * reason in A:
 *     1  NOTHING_TO_BOOT   channel 1 is empty, or its program is bigger
 *                          than PROGRAM_MAX_SIZE -- which is where this
 *                          file's own frame stack begins
 *     2  TRANSFER_FAILED   the transfer came back short, or was refused
 *
 * It hands over with a CALL, because C cannot jump. The program starts on
 * top of two return addresses: the startup code's call to main, and main's
 * call into the program. Nothing here expects them back; programs end in
 * HALT.
 */
#include <pigeon/io.h>

#define HDD_GET_SIZE  1u
#define HDD_READ_DMA  6u
#define DISP_CMD_FILL 4u

#define NOTHING_TO_BOOT 1
#define TRANSFER_FAILED 2

typedef void (*entry_fn)(void);

/* How many bytes a disk channel holds, or 0 when it holds no disk. A disk
 * answers GET_SIZE with exactly 8 bytes; an empty channel with 0xFFFFFFFF. */
unsigned disk_size(unsigned channel) {
    IO_RW = 0u;
    IO_CMD = HDD_GET_SIZE;
    IO_LEN = 8u;
    IO_ADDR = 0u;
    IO_CH = channel;
    if (IO_RETLEN != 8u) return 0u;
    if (IO_DATAW[1] != 0u) return 0xFFFFFFFFu;         /* 4 GB or more */
    return IO_DATAW[0];
}

/* Copy the first `size` bytes of a disk to `address` in one READ_DMA, with
 * [address, count] in the window. 1 when every byte arrived. */
int load(unsigned channel, unsigned address, unsigned size) {
    IO_DATAW[0] = address;
    IO_DATAW[1] = size;
    IO_RW = 0u;                     /* 0, so the count of bytes moved comes back */
    IO_CMD = HDD_READ_DMA;
    IO_LEN = 8u;                    /* the size of [address, count] */
    IO_ADDR = 0u;                   /* from the start of the disk */
    IO_CH = channel;
    return IO_DATAW[0] == size;
}

/* A blank screen for the program -- every word 0, as stage 1 leaves it. */
void clear_screen(void) {
    IO_DATAW[0] = 0u;               /* the colour */
    IO_RW = 1u;
    IO_CMD = DISP_CMD_FILL;
    IO_LEN = 4u;                    /* the size of the colour, not of the fill */
    IO_ADDR = DISPLAY_START;
    IO_CH = CH_DISPLAY;
}

int main(void) {
    unsigned size;

    size = disk_size(CH_USERPROG);
    if (size == 0u || size > PROGRAM_MAX_SIZE) return NOTHING_TO_BOOT;
    if (!load(CH_USERPROG, PROGRAM_LOAD_ADDR, size)) return TRANSFER_FAILED;
    clear_screen();
    ((entry_fn)PROGRAM_LOAD_ADDR)();
    return 0;
}
