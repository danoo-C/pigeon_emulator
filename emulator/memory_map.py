"""
The full address space layout for the machine -- the one place every
reserved region gets defined. ram.py, cpu.py, io_controller.py,
devices/display_io.py, and the assembler all take their addresses from
here rather than hardcoding numbers.

The assembler injects every UPPERCASE int in this module (plus the
IOHeader fields, prefixed IO_) as a predefined symbol, so assembly
sources can say `DISPLAY_START` instead of retyping 0x1418. That is not
a convenience -- user/ui.asm drifted to a stale 0x1218 by hand-copying,
and silently drew into the IO region instead of the screen.

128 MB (0x00000000 - 0x07FFFFFF) layout, low addresses at the top:

    0x00000000 - 0x000003FF   BIOS              (1 KB)   <- CPU boots here
    0x00000400 - 0x00001417   IO controller     (4 KB + 24 B header)
    0x00001418 - 0x00015817   display           (81 KB, 192x108 x 4 B, 16:9)
    0x00015818 - 0x00015A1B   a boot sector's copy and its channel, while booting
    0x00020000 - 0x0011FFFF   program (static)  (1 MB, fixed load point)
    0x00120000 - ...          HEAP -- grows UP toward higher addresses
                                  ... free space ...
    0x07000000 - 0x07EFFFFF   second-stage BIOS, when there is one (15 MB max)
                              STACK -- grows DOWN toward lower addresses
    ...                - 0x07FFFFFC   (stack starts here, at the very top)

Heap and stack share one uninterrupted block and grow toward each other,
so they only run out of room when every last free byte between them is
gone -- neither has to reserve a guess up front.

Region starts below BIOS are computed off the end of the previous region
(IO_START off BIOS_MAX, DISPLAY_START off IO_START + IO_SIZE) rather than
hardcoded, so resizing any one of them cascades automatically instead of
silently overlapping the next region.
"""

RAM_SIZE = 0x08000000  # 128 MB total address space

# --- CPU ---
# Registers are named A, B, C, ... in assembly. The assembler rejects any
# register past this count, so a program cannot encode a register the CPU
# does not have (which used to assemble fine and fail at runtime).
REGISTER_COUNT = 6   # A-F

# --- BIOS ---
BIOS_START = 0x00000000
BIOS_MAX   = 0x00000400   # 1 KB reserved

# --- Second-stage BIOS (docs/os_cd.md) ---
# 1 KB has no room for a font, so the BIOS above loads a second stage off
# the read-only firmware device on CH_BIOS2 and jumps to it. It runs here,
# high up: one READ_DMA reaches it (the HDD refuses DMA below
# PROGRAM_LOAD_ADDR), and nothing it loads lands on it -- a program goes
# to PROGRAM_LOAD_ADDR, with its frame stack and heap at HEAP_START. It
# ends before the last megabyte, which the hardware stack grows down into;
# lib/pigeon/mem.c stops the heap at 0x07F00000 for the same reason.
BIOS2_LOAD_ADDR = 0x07000000
BIOS2_MAX       = 0x07F00000 - BIOS2_LOAD_ADDR   # 15 MB

# --- IO controller (channel-based, DMA-capable bus: HDD, sound, keyboard, ...) ---
# Header (byte offsets from IO_START), all little-endian 4-byte fields:
#   0-3    CHANNEL   which module is selected
#   4-7    R/W       0=read, 1=write (the low byte is the only one that matters)
#   8-11   COMMAND   the operation to run; writing the low byte (offset 4)
#                    fires the command
#   12-15  LENGTH    how many bytes the command operates on
#   16-19  ADDRESS   the RAM address the controller DMAs to/from directly
#                    (bypassing the CPU, like a real disk controller)
class IOHeader:
    IO_CHANNEL = 0
    IO_R_W = 4
    COMMAND = 8
    LENGTH  = 12
    ADDRESS = 16
    RETURN_DATA = 20  # the controller can write a return value here for the CPU to read
    USABLE_AFTER = 24  # the first byte after the header that the controller can read/write

IO_START = BIOS_MAX  # right after BIOS, wherever it ends
IO_SIZE  = 0x00001000 + IOHeader.USABLE_AFTER  # 4 KB + 24 bytes for the header, so the controller can read/write the header itself

# --- Display (memory-mapped video) ---
DISPLAY_START = IO_START + IO_SIZE  # right after the IO region

DISPLAY_W, DISPLAY_H = 192, 108   # 16:9
DISPLAY_SIZE  = DISPLAY_W * DISPLAY_H * 4   # 4 bytes per pixel




# --- User program (static code/data -- fixed load point, fixed max size) ---
PROGRAM_LOAD_ADDR = 0x00020000   # moved up from 0x10000 for comfortable headroom above display memory
if DISPLAY_SIZE + DISPLAY_START > PROGRAM_LOAD_ADDR: #check for overlap with display memory
    raise RuntimeError("Display memory overlaps program load address")
PROGRAM_MAX_SIZE  = 0x00100000   # 1 MB reserved for code + static data

# --- Boot sector (docs/os_cd.md) ---
# A bootable disk keeps a boot record and a boot sector in the unused bytes
# of block 0, its PigeonFS superblock: the record -- signature, first block,
# size in bytes -- at BOOT_RECORD, and the code from BOOT_CODE to the end
# of the block. bios2 copies the whole block to BOOT_LOAD_ADDR, the first
# address after the framebuffer, writes the channel it came from to
# BOOT_CHANNEL, and calls BOOT_ENTRY. It sits below PROGRAM_LOAD_ADDR so
# the boot sector can load a program there without overwriting itself.
BOOT_BLOCK     = 512
BOOT_RECORD    = 52
BOOT_CODE      = 128                  # 384 bytes: 48 instructions
BOOT_SIGNATURE = 0x54424750           # "PGBT" in byte order
BOOT_LOAD_ADDR = DISPLAY_START + DISPLAY_SIZE
BOOT_ENTRY     = BOOT_LOAD_ADDR + BOOT_CODE
BOOT_CHANNEL   = BOOT_LOAD_ADDR + BOOT_BLOCK
if BOOT_CHANNEL + 4 > PROGRAM_LOAD_ADDR:
    raise RuntimeError("A boot sector's copy overlaps the program load address")

# --- Program files (docs/kernel.md §8) ---
# A C program the kernel can load at any address, as compiler/program_file.py
# writes it: a header of PROGRAM_FILE_HEADER bytes -- eight words, the magic
# first -- then the image, then the offset of every word in it that holds an
# address. The magic's first byte, read as an opcode, is 80: a program file
# jumped to by mistake stops at once.
PROGRAM_FILE_MAGIC   = 0x58454750         # "PGEX" in byte order
PROGRAM_FILE_VERSION = 1
PROGRAM_FILE_HEADER  = 32

# --- Heap: dynamic allocations, grows UP from just above the program ---
HEAP_START = PROGRAM_LOAD_ADDR + PROGRAM_MAX_SIZE

# --- Stack: grows DOWN from the very top of RAM ---
STACK_TOP = RAM_SIZE - 4   # start 4 bytes in so the first PUSH doesn't touch RAM_SIZE itself

# --- IO bus channels (must match the devices registered in machine.py) ---
CH_USERPROG = 1   # disk holding the user program; the BIOS boots from here
CH_HDD      = 2   # general-purpose file-backed disk
CH_HID      = 3   # mouse + keyboard
CH_TIMER    = 4   # wall-clock countdown timers
CH_DISPLAY  = 5   # framebuffer: scanout base, block fill
CH_CD       = 6   # removable read-only disc, swapped from the host
CH_BIOS2    = 7   # read-only firmware: the second-stage BIOS (docs/os_cd.md)
# 8 is free. <pigeon/cd.h> takes a channel, so a second drive is a
# one-line change here and nowhere else.


def symbols():
    """This module's constants, as the toolchains see them.

    The assembler injects these as predefined symbols and the C compiler
    as predefined macros, so a source can say DISPLAY_W instead of
    retyping 100 -- which is how user/ui.asm ended up drawing into the IO
    region after someone hand-copied a stale address.

    One rule, defined here rather than in either toolchain, because the
    two must not disagree about what a name means. lib/pigeon/display.h
    used to carry its own copy of the display geometry with nothing
    checking it against this file.
    """
    found = {n: v for n, v in globals().items()
             if n.isupper() and isinstance(v, int) and not isinstance(v, bool)}
    for field, value in vars(IOHeader).items():
        if field.isupper() and isinstance(value, int):
            # IOHeader.COMMAND -> IO_COMMAND; IOHeader.IO_R_W stays IO_R_W
            found[field if field.startswith("IO_") else f"IO_{field}"] = value
    return found


