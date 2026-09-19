/* setmode -- the screen's mode, from the prompt (docs/gac/plans/phase7_setmode.md).
 *
 *     setmode              the mode now:            640 x 360
 *     setmode -list        every mode the machine offers, the one now marked
 *     setmode 640 360      switch to it; 640x360 is the same. Silent when it works
 *
 * The console follows: its text is kept, and it fills the new screen. The
 * mode is the console's, not this program's -- a program's own mode goes
 * back when it ends, so the switch is the kernel's, through the setmode
 * system call. It stays until the next setmode, the window's Mode picker,
 * or a reboot.
 */
#include <pigeon/display.h>
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define MAX_MODES 16

static unsigned ws[MAX_MODES];
static unsigned hs[MAX_MODES];

/* What went wrong, on stderr: where a message goes, redirected or not. */
static void say(char *text) {
    write(STDERR, text, strlen(text));
}

static void list(void) {
    int n = disp_modes(ws, hs, MAX_MODES);
    int i;
    if (n > MAX_MODES) n = MAX_MODES;
    for (i = 0; i < n; i++) {
        printf("%u x %u%s\n", ws[i], hs[i],
               ws[i] == (unsigned)DISP_W && hs[i] == (unsigned)DISP_H ? " (now)" : "");
    }
}

/* "640", or the "640" of "640x360" with *rest at its "360". 0 if it is
 * not a number. */
static unsigned number(char *s, char **rest) {
    unsigned n = 0u;
    int any = 0;
    while (*s >= '0' && *s <= '9') {
        n = n * 10u + (unsigned)(*s - '0');
        s++;
        any = 1;
    }
    *rest = s;
    return any ? n : 0u;
}

int main(int argc, char **argv) {
    char *rest;
    char line[80];
    unsigned w;
    unsigned h = 0u;
    int r;
    disp_init();  /* the screen, as the machine has it (display.h) */
    if (argc == 1) {
        printf("%u x %u\n", (unsigned)DISP_W, (unsigned)DISP_H);
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "-list") == 0) {
        list();
        return 0;
    }
    w = number(argv[1], &rest);
    if (argc == 2 && (*rest == 'x' || *rest == 'X')) h = number(rest + 1, &rest);
    else if (argc == 3 && *rest == 0) h = number(argv[2], &rest);
    if (w == 0u || h == 0u || *rest != 0 || argc > 3) {
        say("usage: setmode [-list | WIDTH HEIGHT | WIDTHxHEIGHT]\n");
        return 1;
    }
    r = setmode(w, h);
    if (r < 0) {
        snprintf(line, sizeof(line), "setmode: %u x %u: %s\n", w, h, sys_strerror(r));
        say(line);
        list();
        return 1;
    }
    return 0;
}
