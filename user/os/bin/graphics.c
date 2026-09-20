/* graphics -- shapes, text and images on the screen, as many to a call as
 * you care to write (docs/graphics_plan.md).
 *
 *     graphics -clear 0xFF101018 -disc 96 54 20 0xFFFF0000 -wait
 *
 * One call, one program load: loading this off the disk costs far more
 * than any drawing it does, which is why the argument list takes as many
 * operations as you like rather than one a call.
 *
 *     -clear C              the whole screen
 *     -px X Y C             one pixel
 *     -rect X Y W H C       filled
 *     -frame X Y W H C      an outline
 *     -line X0 Y0 X1 Y1 C
 *     -circle X Y R C       an outline
 *     -disc X Y R C         filled
 *     -text X Y WORDS C     the 5x7 font
 *     -f FILE X Y W H MODE  a BMP, at that place and size
 *     -wait                 hold the screen until a key, and print its code
 *
 * A colour is 0xAARRGGBB, alpha included: the display hands alpha to the
 * canvas without blending, so 0x00... is invisible. Numbers are decimal
 * or 0x..., and negative where a shape may start off the screen. MODE is
 * STRETCH, CROP or CROP_TOP_LEFT (<pigeon/bmp.h>).
 *
 * The whole list is checked before the first pixel: a mistake in the
 * sixth shape leaves you with none, not five. Shapes off the edge are
 * clipped, which is not a mistake.
 *
 * The kernel draws its console again the moment a program ends, so at the
 * prompt a picture without -wait is gone before you see it. In a script
 * that says `# graphics` it stays, and the script holds it with -wait.
 */
#include <pigeon/bmp.h>
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/mem.h>
#include <pigeon/stdio.h>
#include <pigeon/string.h>
#include <pigeon/sys.h>

/* There is no switch in this compiler, so the operations are an id and an
 * if-chain, the way the explorer's modes are. */
#define OP_NONE   0
#define OP_CLEAR  1
#define OP_PX     2
#define OP_RECT   3
#define OP_FRAME  4
#define OP_LINE   5
#define OP_CIRCLE 6
#define OP_DISC   7
#define OP_TEXT   8
#define OP_IMAGE  9
#define OP_WAIT   10

static int op_id(char *word) {
    if (strcmp(word, "-clear")  == 0) return OP_CLEAR;
    if (strcmp(word, "-px")     == 0) return OP_PX;
    if (strcmp(word, "-rect")   == 0) return OP_RECT;
    if (strcmp(word, "-frame")  == 0) return OP_FRAME;
    if (strcmp(word, "-line")   == 0) return OP_LINE;
    if (strcmp(word, "-circle") == 0) return OP_CIRCLE;
    if (strcmp(word, "-disc")   == 0) return OP_DISC;
    if (strcmp(word, "-text")   == 0) return OP_TEXT;
    if (strcmp(word, "-f")      == 0) return OP_IMAGE;
    if (strcmp(word, "-wait")   == 0) return OP_WAIT;
    return OP_NONE;
}

/* What an operation takes, a letter an argument: n a number, c a colour,
 * s any words, f a file and m a BMP mode. Both passes walk this, so how
 * long an operation is and what its arguments mean are written once --
 * the check and the drawing cannot drift apart. */
static char *op_form(int op) {
    if (op == OP_CLEAR)  return "c";
    if (op == OP_PX)     return "nnc";
    if (op == OP_RECT)   return "nnnnc";
    if (op == OP_FRAME)  return "nnnnc";
    if (op == OP_LINE)   return "nnnnc";
    if (op == OP_CIRCLE) return "nnnc";
    if (op == OP_DISC)   return "nnnc";
    if (op == OP_TEXT)   return "nnsc";
    if (op == OP_IMAGE)  return "fnnnnm";
    return "";                              /* -wait */
}

/* --- reading the arguments ---------------------------------------------- */

/* Decimal or 0x..., with a minus sign: 1 if the WHOLE word is one. A
 * trailing letter is a mistake, not something to stop at, so `-rect 10 1o
 * 5 5 WHITE` is caught rather than drawn ten rows too high. */
static int number_of(char *s, int *out) {
    char *end;
    unsigned v;
    int negative = 0;

    if (*s == '-') { negative = 1; s++; }
    if (*s == 0) return 0;
    v = strtou(s, &end, 0);
    if (end == s || *end != 0) return 0;
    *out = negative ? 0 - (int)v : (int)v;
    return 1;
}

/* A colour is unsigned and has no sign: 0xFF00FF00 does not fit an int. */
static int colour_of(char *s, unsigned *out) {
    char *end;
    unsigned v = strtou(s, &end, 0);
    if (end == s || *end != 0 || *s == '-') return 0;
    *out = v;
    return 1;
}

static int mode_of(char *s) {
    if (strcmp(s, "STRETCH")       == 0) return BMP_STRETCH;
    if (strcmp(s, "CROP")          == 0) return BMP_CROP;
    if (strcmp(s, "CROP_TOP_LEFT") == 0) return BMP_CROP_TOP_LEFT;
    return -1;
}

static int number(char *s) { int v = 0; number_of(s, &v); return v; }
static unsigned colour(char *s) { unsigned v = 0u; colour_of(s, &v); return v; }

/* --- drawing ------------------------------------------------------------- */

/* disp_rect's coordinates are unsigned, so a rectangle starting off the
 * top or the left would wrap to a huge one and vanish. Clip those two
 * edges here, while they are still signed; the library clips the far two. */
static void fill_rect(int x, int y, int w, int h, color_t c) {
    if (w <= 0 || h <= 0) return;
    if (x < 0) { w = w + x; x = 0; }
    if (y < 0) { h = h + y; y = 0; }
    if (w <= 0 || h <= 0) return;
    disp_rect((unsigned)x, (unsigned)y, (unsigned)w, (unsigned)h, c);
}

/* An outline as four lines rather than disp_frame, for the same reason,
 * and because disp_line takes its coordinates signed and clips each of
 * them: half a frame off the left edge draws the half that is on. */
static void frame_rect(int x, int y, int w, int h, color_t c) {
    int x1;
    int y1;
    if (w <= 0 || h <= 0) return;
    x1 = x + w - 1;
    y1 = y + h - 1;
    disp_line(x, y, x1, y, c);
    disp_line(x, y1, x1, y1, c);
    disp_line(x, y, x, y1, c);
    disp_line(x1, y, x1, y1, c);
}

/* The FIFO holds the key that started this program -- the shell reads
 * characters, which is a different queue, so the Enter that ran the
 * command is still sitting here as an event. Throw away what happened
 * before the picture, then wait for what happens after it. */
static unsigned wait_for_a_key(void) {
    unsigned event;

    while (key_event() != 0u) { }
    for (;;) {
        event = key_event();
        if (event != 0u && KE_PRESSED(event) != 0u) return KE_CODE(event);
    }
}

/* --- the two passes ------------------------------------------------------ */

static int oops(char *what, char *word) {
    printf("graphics: %s: %s\n", what, word);
    return 1;
}

/* Every operation and every argument, before anything is drawn: 0 if the
 * list is good. */
static int check(int argc, char **argv) {
    int i = 1;
    int op;
    char *form;
    char *kind;
    char **a;
    int n;
    int value;
    unsigned c;
    unsigned w;
    unsigned h;

    while (i < argc) {
        op = op_id(argv[i]);
        if (op == OP_NONE) return oops("no such operation", argv[i]);
        form = op_form(op);
        n = (int)strlen(form);
        if (i + n >= argc) {
            printf("graphics: %s wants %d argument%s\n", argv[i], n, n == 1 ? "" : "s");
            return 1;
        }
        a = argv + i + 1;
        kind = form;
        while (*kind != 0) {
            if (*kind == 'n' && !number_of(*a, &value)) return oops("not a number", *a);
            if (*kind == 'c' && !colour_of(*a, &c)) return oops("not a colour", *a);
            if (*kind == 'm' && mode_of(*a) < 0) return oops("not a mode", *a);
            if (*kind == 'f' && bmp_info(*a, &w, &h) != BMP_OK)
                return oops(*a, bmp_strerror(bmp_error()));
            kind++;
            a++;
        }
        /* bmp_load refuses a size of nothing. Six numbers in a row is
         * easy to miscount, so the message names the one that is wrong. */
        if (op == OP_IMAGE && number(argv[i + 4]) <= 0)
            return oops("not a width", argv[i + 4]);
        if (op == OP_IMAGE && number(argv[i + 5]) <= 0)
            return oops("not a height", argv[i + 5]);
        i = i + 1 + n;
    }
    return 0;
}

/* The list again, drawing it. Only an image can fail this late -- the heap
 * may not hold it -- and that stops the rest. */
static int draw(int argc, char **argv) {
    int i = 1;
    int op;
    char **a;
    unsigned *pixels;

    while (i < argc) {
        op = op_id(argv[i]);
        a = argv + i + 1;
        if (op == OP_CLEAR) {
            disp_clear(colour(a[0]));
        } else if (op == OP_PX) {
            disp_set((unsigned)number(a[0]), (unsigned)number(a[1]), colour(a[2]));
        } else if (op == OP_RECT) {
            fill_rect(number(a[0]), number(a[1]), number(a[2]), number(a[3]), colour(a[4]));
        } else if (op == OP_FRAME) {
            frame_rect(number(a[0]), number(a[1]), number(a[2]), number(a[3]), colour(a[4]));
        } else if (op == OP_LINE) {
            disp_line(number(a[0]), number(a[1]), number(a[2]), number(a[3]), colour(a[4]));
        } else if (op == OP_CIRCLE) {
            disp_circle(number(a[0]), number(a[1]), number(a[2]), colour(a[3]));
        } else if (op == OP_DISC) {
            disp_disc(number(a[0]), number(a[1]), number(a[2]), colour(a[3]));
        } else if (op == OP_TEXT) {
            disp_text((unsigned)number(a[0]), (unsigned)number(a[1]), a[2], colour(a[3]));
        } else if (op == OP_IMAGE) {
            pixels = bmp_load(a[0], (unsigned)number(a[3]), (unsigned)number(a[4]),
                              mode_of(a[5]));
            if (pixels == NULL) return oops(a[0], bmp_strerror(bmp_error()));
            disp_blit(pixels, number(a[1]), number(a[2]),
                      (unsigned)number(a[3]), (unsigned)number(a[4]));
            free(pixels);
        } else if (op == OP_WAIT) {
            printf("%u\n", wait_for_a_key());
        }
        i = i + 1 + (int)strlen(op_form(op));
    }
    return 0;
}

static void usage(void) {
    print("usage: graphics OPERATION...\n");
    print("-clear C   -px X Y C\n");
    print("-rect X Y W H C   -frame ...\n");
    print("-line X0 Y0 X1 Y1 C\n");
    print("-circle X Y R C   -disc ...\n");
    print("-text X Y WORDS C\n");
    print("-f FILE X Y W H MODE\n");
    print("-wait     C is 0xAARRGGBB\n");
}

int main(int argc, char **argv) {
    disp_init();  /* the screen, as the machine has it (display.h) */
    if (argc < 2) {
        usage();
        return 1;
    }
    if (check(argc, argv) != 0) return 1;
    return draw(argc, argv);
}
