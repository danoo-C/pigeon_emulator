#include <pigeon/sys.h>
#include <pigeon/vram.h>

int main(int argc, char **argv) {
    /* The machine as it powers on, since the BIOS draws there and knows
     * nothing of modes: 192 x 108, shown out of RAM, and no video memory
     * held by anyone (docs/gac/plans/phase5_display_lib.md §1). On a
     * machine without video memory these do nothing. */
    vram_free_owned(0u);
    vram_set_mode(DISPLAY_W, DISPLAY_H);
    vram_scanout_ram(DISPLAY_START);
    ((void (*)(void))0)();  
    return 0;
}