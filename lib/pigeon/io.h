/* <pigeon/io.h> -- the IO controller's header registers.
 *
 * Every device on the bus is programmed the same way: fill in the command
 * fields, then write the channel LAST. That store is what fires the
 * command, and the command has completed by the time the next C statement
 * runs -- the store arms a flag the main loop services as soon as it
 * retires, so there is no completion flag to poll.
 *
 * `volatile` is load-bearing. These are device registers, not memory, and
 * the ORDER of the stores is the protocol. This compiler never caches a
 * load across a statement, so the sequence would survive without it, but
 * the qualifier documents the requirement.
 *
 * The addresses are not written down here. IO_START and the IO_* header
 * offsets are predefined by the compiler out of emulator/memory_map.py,
 * the same names the assembler injects -- so this header cannot drift
 * from the machine the way a retyped 0x400 would.
 *
 * Note the register names are IO_CH / IO_CMD / IO_LEN / IO_ADDR, not
 * IO_CHANNEL / IO_COMMAND / IO_LENGTH / IO_ADDRESS: those latter names
 * are the injected byte OFFSETS, used below. Reusing them for the
 * registers would shadow the offsets this header is built from.
 */
#ifndef PIGEON_IO_H
#define PIGEON_IO_H

#define IO_REG(offset) (*(volatile unsigned *)(IO_START + (offset)))

#define IO_CH     IO_REG(IO_CHANNEL)      /* write LAST -- fires the command */
#define IO_RW     IO_REG(IO_R_W)          /* 0 = read, 1 = write */
#define IO_CMD    IO_REG(IO_COMMAND)
#define IO_LEN    IO_REG(IO_LENGTH)       /* size of the PAYLOAD, nothing else */
#define IO_ADDR   IO_REG(IO_ADDRESS)      /* a per-command parameter */
#define IO_RETLEN IO_REG(IO_RETURN_DATA)  /* bytes the device replied with */

/* The data window, as bytes or as words. */
#define IO_DATA  ((volatile unsigned char *)(IO_START + IO_USABLE_AFTER))
#define IO_DATAW ((volatile unsigned *)(IO_START + IO_USABLE_AFTER))

#endif
