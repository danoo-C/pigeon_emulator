/* splash -- the boot screen, for now (docs/phase6_plan.md).
 *
 * The kernel runs this first when /etc/boot.conf names it, as `splash 2500`:
 * how long to show, in milliseconds, from boot.conf's splash_ms. Run from
 * the prompt with no argument, it shows for 2,500.
 *
 * A placeholder for a real splash screen: the screen fades from black to
 * red, green, blue and back to black, a quarter of the time each. It is
 * timed on the timer device, not by frames, so it lasts as long on any
 * host, and the screen is filled only when the colour changes, one
 * hardware fill each. Any key ends it early; the kernel empties the input
 * queues after it, so that key never reaches the shell.
 */
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/io.h>
#include <pigeon/string.h>

#define DEFAULT_MS   2500u
#define LONGEST_MS   60000u
#define TIMER_ID     1u
#define TIMER_START  1u
#define TIMER_STATUS 5u
#define RUNNING      1u

/* Where the fade is at 0, 1/4, 2/4, 3/4 and all of the time. */
color_t stops[5] = {BLACK, RED, GREEN, BLUE, BLACK};

static void timer(unsigned command, unsigned length) {
    IO_RW = 0u;
    IO_CMD = command;
    IO_LEN = length;
    IO_ADDR = TIMER_ID;
    IO_CH = CH_TIMER;               /* this store fires it -- must be last */
}

/* Milliseconds left to show, or 0 once the time is up. */
static unsigned left_ms(void) {
    timer(TIMER_STATUS, 8u);
    if (IO_DATAW[0] != RUNNING) return 0u;
    return IO_DATAW[1];
}

/* One channel of `from` and `to`, `part` 256ths of the way. */
static color_t mix(color_t from, color_t to, unsigned shift, unsigned part) {
    unsigned a = (from >> shift) & 255u;
    unsigned b = (to >> shift) & 255u;
    return ((a * (256u - part) + b * part) >> 8) << shift;
}

/* 1 once any key has gone down. */
static int key_pressed(void) {
    unsigned event;
    for (;;) {
        event = key_event();
        if (event == 0u) return 0;
        if (KE_PRESSED(event) != 0u) return 1;
    }
}

int main(int argc, char **argv) {
    unsigned ms = DEFAULT_MS;
    unsigned left;
    unsigned gone;
    unsigned stop;
    unsigned part;
    color_t colour;
    color_t shown = 0u;             /* no colour is 0: every one has its alpha */
    int given;

    if (argc > 1) {
        given = atoi(argv[1]);
        if (given > 0 && (unsigned)given <= LONGEST_MS) ms = (unsigned)given;
    }
    timer(TIMER_START, ms);
    for (;;) {
        left = left_ms();
        if (left == 0u || key_pressed()) break;
        gone = ms - left;
        stop = gone * 4u / ms;
        part = (gone * 4u - stop * ms) * 256u / ms;
        colour = BLACK | mix(stops[stop], stops[stop + 1u], 16u, part)
                       | mix(stops[stop], stops[stop + 1u], 8u, part)
                       | mix(stops[stop], stops[stop + 1u], 0u, part);
        if (colour != shown) {
            disp_clear(colour);
            shown = colour;
        }
    }
    return 0;
}
