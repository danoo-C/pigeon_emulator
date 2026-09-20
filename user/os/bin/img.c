/* img -- show a BMP image until Esc (docs/bmp_plan.md).
 *
 *     img FILE.bmp       the image at its own size, in the middle of the
 *                        screen: on black when smaller, its middle when bigger
 *     img -s FILE.bmp    stretched to fill the screen
 *
 * The file is read through the kernel, so ./image.bmp is from where you are.
 * Esc comes back to the prompt, and so does Ctrl+C, the kernel's break, which
 * the shell reports as "stopped". Nothing is printed while the image shows,
 * so the console doesn't draw over it, and the kernel draws the console
 * again once img ends.
 */
#include <pigeon/bmp.h>
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/mem.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

int main(int argc, char **argv) {
    char *path;
    int mode = BMP_CROP;
    unsigned *pixels;
    unsigned event;

    if (argc == 3 && strcmp(argv[1], "-s") == 0) {
        mode = BMP_STRETCH;
        path = argv[2];
    } else if (argc == 2) {
        path = argv[1];
    } else {
        print("usage: img [-s] FILE.bmp\n");
        return 1;
    }

    disp_init();                /* the screen, as the machine has it (display.h) */
    pixels = bmp_load(path, DISP_W, DISP_H, mode);
    if (pixels == NULL) {
        print("img: ");
        print(path);
        print(": ");
        print(bmp_strerror(bmp_error()));
        print("\n");
        return 1;
    }
    disp_blit(pixels, 0, 0, DISP_W, DISP_H);
    free(pixels);

    for (;;) {
        event = key_event();
        if (event != 0u && KE_PRESSED(event) != 0u && KE_CODE(event) == KEY_ESC) break;
    }
    return 0;
}
