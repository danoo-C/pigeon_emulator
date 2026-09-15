/* The debug port, CH_DEBUG, from a program's side. See debug.h.
 *
 * WRITE is sent with R/W 1, so the controller copies no reply into the
 * window: how many bytes the port took comes back as RETURN_DATA
 * (emulator/devices/debug_port.py). A text longer than the window goes in
 * windows. dbg_printf formats straight into the window, so a line is
 * never copied at all.
 */
#include <pigeon/debug.h>
#include <pigeon/io.h>
#include <pigeon/stdio.h>

/* Prefixed: units share one macro table, and a bare CMD_WRITE would
 * silently replace another library's. */
#define DBG_CMD_NOP    0u
#define DBG_CMD_WRITE  1u
#define DBG_WINDOW     (IO_SIZE - IO_USABLE_AFTER)
#define DBG_TO         ((char *)(IO_START + IO_USABLE_AFTER))

/* 0 = not asked yet, 1 = the port is there, 2 = no port. */
static unsigned dbg_port = 0u;

/* display.c's probe, for this channel. With no controller at all the
 * channel store stays where it was put; an empty channel answers
 * 0xFFFFFFFF; the port answers a NOP with 4 bytes. */
static unsigned dbg_probe(void) {
    IO_RW   = 0u;
    IO_CMD  = DBG_CMD_NOP;
    IO_LEN  = 4u;
    IO_ADDR = 0u;
    IO_CH   = CH_DEBUG;            /* this store fires it -- must be last */
    if (IO_CH != 0u) { IO_CH = 0u; return 2u; }
    if (IO_RETLEN != 4u) return 2u;
    return 1u;
}

/* The n bytes already at the start of the window: how many were taken. */
static unsigned dbg_send(unsigned n) {
    IO_RW   = 1u;
    IO_CMD  = DBG_CMD_WRITE;
    IO_LEN  = n;
    IO_ADDR = 0u;
    IO_CH   = CH_DEBUG;
    return IO_RETLEN;
}

int dbg_write(char *text, unsigned n) {
    unsigned done = 0u;
    unsigned chunk;
    char *from;
    char *to;
    char *end;

    if (dbg_port == 0u) dbg_port = dbg_probe();
    if (dbg_port != 1u) return -1;
    while (done < n) {
        chunk = n - done;
        if (chunk > DBG_WINDOW) chunk = DBG_WINDOW;
        from = text + done;
        end = from + chunk;
        to = DBG_TO;
        while (from != end) {
            *to = *from;
            to++;
            from++;
        }
        if (dbg_send(chunk) != chunk) return (int)done;
        done += chunk;
    }
    return (int)done;
}

int dbg_print(char *text) {
    unsigned n = 0u;
    while (text[n] != 0) n++;
    return dbg_write(text, n);
}

int dbg_printf(char *format, ...) {
    va_list ap;
    int n;

    if (dbg_write(format, 0u) < 0) return -1;       /* no port: don't format */
    va_start(ap, format);
    n = vsnprintf(DBG_TO, DBG_LINE_MAX, format, ap);
    va_end(ap);
    if (n > (int)DBG_LINE_MAX - 1) n = (int)DBG_LINE_MAX - 1;
    return (int)dbg_send((unsigned)n);
}
