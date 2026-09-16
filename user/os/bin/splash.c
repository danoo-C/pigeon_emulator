/* splash -- the boot screen (docs/phase6_plan.md, docs/bmp_plan.md).
 *
 * The kernel runs this first when /etc/boot.conf names it, as `splash 2500`:
 * how long to show, in milliseconds, from boot.conf's splash_ms. With no
 * argument -- run from the prompt, say -- there is no countdown at all: it
 * shows until a key goes down.
 *
 * It draws /etc/bmp/pigeon.bmp, stretched to the screen, and holds it for
 * that long, timed on the timer device rather than by frames. Any key ends
 * it early; the kernel empties the input queues after it, so that key never
 * reaches the shell.
 *
 * The eyes flash yellow. /etc/bmp/eye-mask.bmp lies exactly over the pigeon:
 * white where the eyes are, black everywhere else. Its white pixels are
 * found once, and at each look at the timer only those pixels are drawn
 * again: each its own colour in the pigeon blended toward yellow, by an
 * amount that follows isin() of the time gone, so the eyes brighten and dim
 * smoothly once a second whatever the speed of the host. The one timer does
 * both jobs: it is started for the whole splash, or, with no countdown, for
 * one flash at a time, started again as each flash ends.
 *
 * An image that won't load is one line on the console. Without the pigeon
 * the splash ends with status 1, which the kernel logs; without the mask
 * the pigeon still shows, only with its eyes still, and the line comes once
 * it's gone -- the console draws on the same screen. Boot carries on either
 * way, as it does after any splash that doesn't fault.
 */
#include <pigeon/bmp.h>
#include <pigeon/input.h>
#include <pigeon/io.h>
#include <pigeon/math.h>
#include <pigeon/mem.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

#define IMAGE        "/etc/bmp/pigeon.bmp"
#define EYES         "/etc/bmp/eye-mask.bmp"
#define LONGEST_MS   60000u
#define FLASH_MS     1000u          /* one flash: dim, bright, and dim again */
#define YELLOW       0xFFFFFF00u
#define MAX_EYES     1024u          /* mask pixels drawn at most             */
#define TIMER_ID     1u
#define TIMER_START  1u
#define TIMER_STATUS 5u
#define RUNNING      1u

unsigned eyes[1024];                /* MAX_EYES: the mask's white pixels, as screen indices */
unsigned eye_count;

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

/* 1 once any key has gone down. */
static int key_pressed(void) {
    unsigned event;
    for (;;) {
        event = key_event();
        if (event == 0u) return 0;
        if (KE_PRESSED(event) != 0u) return 1;
    }
}

static void say_failed(char *path) {
    print("splash: ");
    print(path);
    print(": ");
    print(bmp_strerror(bmp_error()));
    print("\n");
}

/* `from` moved toward `to` by t/256, one channel at a time. */
static unsigned blend(unsigned from, unsigned to, unsigned t) {
    unsigned r = (((from >> 16) & 255u) * (256u - t) + ((to >> 16) & 255u) * t) >> 8;
    unsigned g = (((from >> 8) & 255u) * (256u - t) + ((to >> 8) & 255u) * t) >> 8;
    unsigned b = ((from & 255u) * (256u - t) + (to & 255u) * t) >> 8;
    return 0xFF000000u | (r << 16) | (g << 8) | b;
}

/* The mask's white pixels into eyes[]; 0 when it won't load. */
static int find_eyes(void) {
    unsigned *mask = bmp_load(EYES, DISPLAY_W, DISPLAY_H, BMP_STRETCH);
    unsigned i;
    if (mask == NULL) return 0;
    eye_count = 0u;
    for (i = 0u; i < DISPLAY_W * DISPLAY_H; i++) {
        if (((mask[i] >> 8) & 255u) >= 128u && eye_count < MAX_EYES) {
            eyes[eye_count] = i;
            eye_count++;
        }
    }
    free(mask);
    return 1;
}

int main(int argc, char **argv) {
    unsigned ms = 0u;               /* 0: no countdown -- it waits for a key */
    unsigned period;                /* what the timer is started for         */
    unsigned *pixels;
    unsigned *screen = (unsigned *)DISPLAY_START;
    unsigned left;
    unsigned gone;
    unsigned t;
    unsigned shown = 0xFFFFFFFFu;   /* no amount yet: the first always draws */
    unsigned e;
    int given;
    int eyes_found;

    if (argc > 1) {
        given = atoi(argv[1]);
        if (given > 0 && (unsigned)given <= LONGEST_MS) ms = (unsigned)given;
    }
    pixels = bmp_load(IMAGE, DISPLAY_W, DISPLAY_H, BMP_STRETCH);
    if (pixels == NULL) {
        say_failed(IMAGE);
        return 1;
    }
    memcpy(screen, pixels, DISPLAY_W * DISPLAY_H * 4u);
    eyes_found = find_eyes();

    period = ms;
    if (period == 0u) period = FLASH_MS;
    timer(TIMER_START, period);
    for (;;) {
        left = left_ms();
        if (left == 0u) {
            if (ms != 0u) break;    /* the countdown is up */
            timer(TIMER_START, period);     /* another flash: the clock round again */
            left = period;
        }
        if (key_pressed()) break;
        gone = period - left;
        /* 0 to 256, following the sine: half-bright at the start of each flash */
        t = (unsigned)(isin((int)(gone * ANGLE_STEPS / FLASH_MS)) + FX_ONE) / 2u;
        if (t != shown) {
            for (e = 0u; e < eye_count; e++) screen[eyes[e]] = blend(pixels[eyes[e]], YELLOW, t);
            shown = t;
        }
    }
    free(pixels);
    /* Said now, not when it failed: the console would have written it over
     * the pigeon. Nothing since has touched bmp_error(). */
    if (!eyes_found) say_failed(EYES);
    return 0;
}
