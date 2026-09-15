/* <pigeon/debug.h> -- lines to the debug port, off the screen.
 *
 *     dbg_printf("[kernel] exec %s at %p, depth %d\n", path, base, depth);
 *
 * Nothing written here reaches the machine's screen. The host shows it in
 * the launcher's terminal (--serial), a log file (--serial-log) and the
 * front ends' Serial panel, with the time each line started
 * (docs/phase5_plan.md). The library sends bytes only: no prefix and no
 * time, so a program says who it is itself.
 *
 * Any program can call these, the kernel included. The first call asks
 * whether there is a port, as display.c asks about the display; on a bare
 * CPU, or a machine without the device, every call returns -1 at once.
 *
 * dbg_printf formats with vsnprintf straight into the IO data window, so
 * a line needs no buffer and is never copied, and a call costs the frame
 * stack only the formatter's frames -- the kernel logs a panic on a stack
 * of 1 KB. A line longer than DBG_LINE_MAX - 1 bytes is cut short.
 */
#ifndef PIGEON_DEBUG_H
#define PIGEON_DEBUG_H

#define DBG_LINE_MAX 256u

int dbg_write(char *text, unsigned n);  /* -> bytes taken, or -1: no port.
                                           An n of 0 only asks.           */
int dbg_print(char *text);              /* a string                       */
int dbg_printf(char *format, ...);      /* up to DBG_LINE_MAX - 1 bytes   */

#endif
