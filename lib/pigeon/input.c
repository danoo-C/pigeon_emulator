/* Keyboard and mouse over IO channel CH_HID. See input.h.
 *
 * Every call programs the IO header and then fires the channel; the reply
 * is in the data window by the time the next C statement runs. The bus
 * rules, and why the channel store must come last, are in <pigeon/io.h>.
 */
#include <pigeon/input.h>
#include <pigeon/io.h>

#define CMD_MOUSE_POS     1
#define CMD_MOUSE_BUTTONS 2
#define CMD_KEYBOARD      3
#define CMD_MOUSE_EVENT   4
#define CMD_KEY_EVENT     5
#define CMD_KEY_STATE     6
#define CMD_KEY_BITMAP    7

/* Fire one HID command. `address` carries a parameter for the commands
 * that take one (only CMD_KEY_STATE, so far). */
static void hid_call(unsigned command, unsigned length, unsigned address) {
    IO_RW = 0;                     /* read */
    IO_CMD = command;
    IO_LEN = length;
    IO_ADDR = address;
    IO_CH = CH_HID;                /* this store fires it -- must be last */
}

static unsigned hid_word(unsigned command, unsigned length, unsigned address) {
    hid_call(command, length, address);
    return *(volatile unsigned *)IO_DATA;
}

/* --- mouse -------------------------------------------------------------- */

unsigned mouse_x(void)       { return hid_word(CMD_MOUSE_POS, 4, 0) >> 16; }
unsigned mouse_y(void)       { return hid_word(CMD_MOUSE_POS, 4, 0) & 0xFFFF; }
unsigned mouse_buttons(void) { return hid_word(CMD_MOUSE_BUTTONS, 1, 0) & 0xFF; }
unsigned mouse_event(void)   { return hid_word(CMD_MOUSE_EVENT, 1, 0) & 0xFF; }

/* --- keyboard ----------------------------------------------------------- */

int key_read(void) {
    unsigned code = hid_word(CMD_KEYBOARD, 1, 0) & 0xFF;
    if (code == 0u) return -1;
    return (int)code;
}

/* Two bytes: [flags][code]. The flags byte carries a VALID bit because
 * the character FIFO returns 0x00 for "empty", which a real key whose
 * code is 0 is indistinguishable from. */
unsigned key_event(void) {
    unsigned raw = hid_word(CMD_KEY_EVENT, 2, 0);
    unsigned flags = raw & 0xFF;
    unsigned code = (raw >> 8) & 0xFF;
    if ((flags & 0x80u) == 0u) return 0u;
    return (flags << 8) | code;
}

int key_down(int code) {
    if (code < 0 || code > 0xFF) return 0;
    return (int)(hid_word(CMD_KEY_STATE, 1, (unsigned)code) & 1u);
}

void key_bitmap(unsigned char *out) {
    unsigned i;
    hid_call(CMD_KEY_BITMAP, 32, 0);
    for (i = 0u; i < 32u; i++) out[i] = IO_DATA[i];
}
