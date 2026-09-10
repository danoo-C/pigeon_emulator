/* A little menu-and-textbox demo for the pigeon machine.
 *
 *   arrow keys / mouse   move the menu selection
 *   type                 goes into the text box
 *   backspace            deletes
 *   enter                stamps the box contents onto the canvas
 *   escape               quit
 *
 * It shows both input buffers doing the job each is for: the character
 * FIFO for typing (a key tapped between two frames must not be lost) and
 * the real-time key state for held arrows (smooth repeat, not the
 * keyboard's auto-repeat rate).
 *
 * Build:
 *   python3 compiler/cc.py user/demo.c lib/pigeon/display.c \
 *           lib/pigeon/input.c lib/pigeon/mem.c -o build/demo.bin
 */
#include <pigeon/display.h>
#include <pigeon/input.h>
#include <pigeon/mem.h>

/* --- layout, with a 4x6 font -------------------------------------------
 *
 * Top-anchored rows are absolute; anything that has to sit near the
 * middle or the bottom is derived from DISP_H, and the text width from
 * DISP_W. These were all literals for a 100x100 screen, so the whole
 * lower half of the UI hung off the bottom the moment the screen
 * changed shape. */
#define TITLE_H     (GLYPH_H + 3)
#define MENU_Y      (TITLE_H + 2)
#define MENU_STEP   (GLYPH_H + 2)
#define MENU_COUNT  3
#define LABEL_Y     (MENU_Y + MENU_COUNT * MENU_STEP + 2)
#define BOX_Y       (LABEL_Y + GLYPH_H + 1)
#define BOX_H       (GLYPH_H + 5)
#define CANVAS_Y    (BOX_Y + BOX_H + 2)
#define STATUS_Y    (DISP_H - GLYPH_H - 1)
#define CANVAS_H    (STATUS_Y - 3 - CANVAS_Y)
#define TEXT_MAX    ((DISP_W - 10) / (GLYPH_W + 1))

#define ACCENT  0xFF30C0FF
#define DIM     0xFF505868
#define PANEL   0xFF181C24

/* --- state -------------------------------------------------------------- */
char text[TEXT_MAX + 1];
int  text_len;
int  selected;
int  stamped;                   /* which shape the canvas is showing */
int  key_count;
int  running;
int  dirty;                     /* something changed; the screen needs redrawing */

char *MENU[] = { "CIRCLE", "BOX", "RAYS" };

/* Menu geometry, so the mouse and the renderer agree on where an item is. */
static int menu_row_at(unsigned my) {
    int i = 0;
    while (i < MENU_COUNT) {
        unsigned top = MENU_Y + i * MENU_STEP;
        if (my >= top && my < top + (unsigned)GLYPH_H) return i;
        i++;
    }
    return -1;
}

/* --- drawing ------------------------------------------------------------ */

static void draw_title(void) {
    disp_rect(0, 0, DISP_W, TITLE_H, ACCENT);
    disp_text(3, 2, "PIGEON CC DEMO", 0xFF000000);
}

static void draw_menu(void) {
    int i = 0;
    while (i < MENU_COUNT) {
        unsigned y = MENU_Y + i * MENU_STEP;
        if (i == selected) {
            disp_rect(2, y - 1, DISP_W - 4, GLYPH_H + 2, ACCENT);
            disp_text(6, y, MENU[i], 0xFF000000);
            disp_text(1, y, ">", 0xFF000000);
        } else {
            disp_text(6, y, MENU[i], DIM);
        }
        i++;
    }
}

static void draw_textbox(void) {
    disp_text(2, LABEL_Y, "TYPE:", DIM);
    disp_frame(2, BOX_Y, DISP_W - 4, BOX_H, DIM);
    disp_text(5, BOX_Y + 3, text, WHITE);
    /* the caret sits just past the last glyph */
    disp_vline(5 + text_len * (GLYPH_W + 1), BOX_Y + 2, GLYPH_H + 2, ACCENT);
}

/* The canvas redraws whatever the last ENTER stamped. Each branch uses a
 * different part of the display library. */
static void draw_canvas(void) {
    int cx = DISP_W / 2;
    int cy = CANVAS_Y + CANVAS_H / 2;
    int i;

    disp_rect(0, CANVAS_Y, DISP_W, CANVAS_H, PANEL);

    if (stamped == 0) {
        disp_circle(cx, cy, 13, ACCENT);
        disp_circle(cx, cy, 8, 0xFF206090);
    } else if (stamped == 1) {
        disp_frame(cx - 16, cy - 11, 32, 22, ACCENT);
        disp_rect(cx - 10, cy - 5, 20, 10, 0xFF206090);
    } else {
        i = 0;
        while (i < 12) {
            disp_line(cx, cy, cx - 30 + i * 5, cy - 12, ACCENT);
            i++;
        }
    }

    /* whatever was typed, echoed across the canvas */
    if (text_len > 0) disp_text(3, CANVAS_Y + CANVAS_H - GLYPH_H - 1, text, WHITE);
}

static void draw_status(void) {
    char line[21];
    unsigned mx = mouse_x();
    unsigned my = mouse_y();
    int i;

    /* "M 00,00  K 000" -- built by hand; there is no printf */
    memset(line, 32, 20);
    line[20] = 0;
    line[0] = 'M';
    line[2] = 48 + (mx / 10) % 10;
    line[3] = 48 + mx % 10;
    line[4] = ',';
    line[5] = 48 + (my / 10) % 10;
    line[6] = 48 + my % 10;
    line[9] = 'K';
    line[11] = 48 + (key_count / 100) % 10;
    line[12] = 48 + (key_count / 10) % 10;
    line[13] = 48 + key_count % 10;
    if (mouse_buttons() & MB_LEFT) line[16] = '*';

    disp_hline(0, STATUS_Y - 2, DISP_W, DIM);
    disp_text(1, STATUS_Y, line, DIM);
    for (i = 0; i < 20; i++) line[i] = 0;
}

/* Draw the whole screen into the back buffer, then hand it over in one
 * piece. Redrawing straight onto the framebuffer meant the emulator's
 * 30 Hz snapshot regularly caught the moment between disp_clear() and
 * the first shape -- a black flash, several times a second. */
static void render(void) {
    disp_clear(0xFF0A0C10);
    draw_title();
    draw_menu();
    draw_textbox();
    draw_canvas();
    draw_status();
    disp_present();
}

/* --- input -------------------------------------------------------------- */

/* Only printable text and editing keys come through here. ENTER and the
 * arrows are handled from the EDGE stream instead -- see below. */
static void type_character(int code) {
    if (code == KEY_ESC) { running = 0; return; }
    if (code == KEY_ENTER) return;
    dirty = 1;
    if (code == KEY_BACKSPACE) {
        if (text_len > 0) { text_len--; text[text_len] = 0; }
        return;
    }
    if (code < 32 || code > 126) return;          /* not printable */
    if (text_len >= TEXT_MAX) return;
    text[text_len] = (char)code;
    text_len++;
    text[text_len] = 0;
}

/* Navigation AND activation both come from the key EDGE stream.
 *
 * They have to share a stream to stay in order. An earlier version read
 * ENTER from the character FIFO and the arrows from the edge FIFO, and
 * drained them in separate passes -- so pressing DOWN then ENTER stamped
 * the OLD selection, because every character was consumed before the
 * first edge was looked at. Two FIFOs are ordered within themselves and
 * not against each other; anything whose relative order matters belongs
 * in one of them.
 */
static void handle_key_edges(void) {
    unsigned event = key_event();
    while (event != 0) {
        if (KE_PRESSED(event)) {
            int code = KE_CODE(event);
            if (code == KEY_UP && selected > 0) { selected--; dirty = 1; }
            if (code == KEY_DOWN && selected < MENU_COUNT - 1) { selected++; dirty = 1; }
            if (code == KEY_ENTER) { stamped = selected; dirty = 1; }
        }
        event = key_event();
    }
}

static void handle_mouse(void) {
    unsigned event = mouse_event();
    while (event != 0) {
        if (ME_PRESSED(event) && ME_BUTTON(event) == 0) {
            int row = menu_row_at(mouse_y());
            if (row >= 0) { selected = row; stamped = row; dirty = 1; }
        }
        event = mouse_event();
    }
}

int main(void) {
    int code;
    unsigned last_mx = 9999;
    unsigned last_my = 9999;
    int frame = 0;

    disp_use_back_buffer();     /* whole frames only; see render() */

    text_len = 0;
    text[0] = 0;
    selected = 0;
    stamped = 0;
    key_count = 0;
    running = 1;
    dirty = 1;                  /* draw once at startup */

    while (running) {
        /* 1. drain the FIFOs -- discrete things that must not be missed */
        code = key_read();
        while (code >= 0) {
            key_count++;
            type_character(code);
            code = key_read();
        }
        handle_key_edges();
        handle_mouse();

        /* 2. sample the real-time state -- what is held right now */
        if (key_down(KEY_LEFT)  && stamped != 2) { stamped = 2; dirty = 1; }
        if (key_down(KEY_RIGHT) && stamped != 1) { stamped = 1; dirty = 1; }

        /* the status line shows the cursor, so a moved mouse is a change */
        if (mouse_x() != last_mx || mouse_y() != last_my) {
            last_mx = mouse_x();
            last_my = mouse_y();
            dirty = 1;
        }

        /* 3. redraw only when something actually changed. The UI is static
         * most of the time, and a redraw is ~40,000 stores -- repeating it
         * every pass burned the CPU and gave the snapshot far more chances
         * to catch a half-drawn screen. */
        if (dirty) {
            render();
            dirty = 0;
        }

        frame++;
        if (frame > 2000000) running = 0;     /* don't spin forever headless */
    }

    disp_clear(0xFF0A0C10);
    disp_text(DISP_W / 2 - 6, DISP_H / 2 - 3, "BYE", ACCENT);
    return key_count;
}
