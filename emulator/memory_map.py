"""
The full address space layout for the machine -- the one place every
reserved region gets defined. ram.py, cpu.py, instruction_set.py,
display.py, io_controller.py, and the assembler should all import their
addresses from here rather than hardcoding numbers.

128 MB (0x00000000 - 0x07FFFFFF) layout, low addresses at the top:

    0x00000000 - 0x000001FF   BIOS               (512 B)   <- CPU boots here
    0x00000200 - ...          IO controller       (4 KB + header)
    ...                       display             (4 KB)
    0x00020000 - 0x0011FFFF   program (static)     (1 MB, fixed load point)
    0x00120000 - ...          HEAP -- grows UP toward higher addresses
                                  ... free space ...
                              STACK -- grows DOWN toward lower addresses
    ...                - 0x07FFFFFF   (stack starts here, at the very top)

Heap and stack share one uninterrupted block and grow toward each other,
so they only run out of room when every last free byte between them is
gone -- neither has to reserve a guess up front.

Region starts below BIOS are computed off the end of the previous region
(IO_START off BIOS_MAX, DISPLAY_START off IO_START + IO_SIZE) rather than
hardcoded, so resizing any one of them cascades automatically instead of
silently overlapping the next region.

NOTE: DISK_START/DISK_SIZE are the old memory-mapped "disk window" the BIOS
reads from directly. Once boot loading moves through IOController/HDDModule
instead, this region can be reclaimed as ordinary free space.
"""

RAM_SIZE = 0x08000000  # 128 MB total address space

# --- BIOS ---
BIOS_START = 0x00000000
BIOS_MAX   = 0x00000400   # 512 bytes reserved (doubled from 256)

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

DISPLAY_W, DISPLAY_H = 100, 100
DISPLAY_SIZE  = DISPLAY_W * DISPLAY_H * 4   # 4 bytes per pixel




# --- User program (static code/data -- fixed load point, fixed max size) ---
PROGRAM_LOAD_ADDR = 0x00020000   # moved up from 0x10000 for comfortable headroom above display memory
if DISPLAY_SIZE + DISPLAY_START > PROGRAM_LOAD_ADDR: #check for overlap with display memory
    raise RuntimeError("Display memory overlaps program load address")
PROGRAM_MAX_SIZE  = 0x00100000   # 1 MB reserved for code + static data

# --- Heap: dynamic allocations, grows UP from just above the program ---
HEAP_START = PROGRAM_LOAD_ADDR + PROGRAM_MAX_SIZE

# --- Stack: grows DOWN from the very top of RAM ---
STACK_TOP = RAM_SIZE - 4   # start 4 bytes in so the first PUSH doesn't touch RAM_SIZE itself

# # memory_map.py
# """
# The full address space layout for the machine -- the one place every
# reserved region gets defined. ram.py, cpu.py, instruction_set.py,
# display.py, io_controller.py, and the assembler should all import their
# addresses from here rather than hardcoding numbers.

# 128 MB (0x00000000 - 0x07FFFFFF) layout, low addresses at the top:

#     0x00000000 - 0x000000FF   BIOS               (256 B)   <- CPU boots here
#     0x00000100 - 0x000010FF   IO controller       (4 KB)
#     0x00001100 - 0x000020FF   display             (4 KB)
#     0x00010000 - 0x0010FFFF   program (static)     (1 MB, fixed load point)
#     0x00110000 - ...          HEAP -- grows UP toward higher addresses
#                                   ... free space ...
#                               STACK -- grows DOWN toward lower addresses
#     ...                - 0x07FFFFFF   (stack starts here, at the very top)

# Heap and stack share one uninterrupted block and grow toward each other,
# so they only run out of room when every last free byte between them is
# gone -- neither has to reserve a guess up front.

# NOTE: DISK_START/DISK_SIZE are the old memory-mapped "disk window" the BIOS
# reads from directly. Once boot loading moves through IOController/HDDModule
# instead, this region can be reclaimed as ordinary free space.
# """

# RAM_SIZE = 0x08000000  # 128 MB total address space

# # --- BIOS ---
# BIOS_START = 0x00000000
# BIOS_MAX   = 0x00000100   # 256 bytes reserved

# # --- IO controller (channel-based, DMA-capable bus: HDD, sound, keyboard, ...) ---
# # Header (byte offsets from IO_START), all little-endian 4-byte fields:
# #   0-3    CHANNEL   which module is selected
# #   4-7    R/W       0=read, 1=write (the low byte is the only one that matters)
# #   8-11   COMMAND   the operation to run; writing the low byte (offset 4)
# #                    fires the command
# #   12-15  LENGTH    how many bytes the command operates on
# #   16-19  ADDRESS   the RAM address the controller DMAs to/from directly
# #                    (bypassing the CPU, like a real disk controller)
# class IOHeader:
#     IO_CHANNEL = 0
#     IO_R_W = 4
#     COMMAND = 8
#     LENGTH  = 12
#     ADDRESS = 16
#     RETURN_DATA = 20  # the controller can write a return value here for the CPU to read
#     USABLE_AFTER = 24  # the first byte after the header that the controller can read/write
# IO_START = 0x00000100
# IO_SIZE  = 0x00001000 + IOHeader.USABLE_AFTER  # 4 KB + 24 bytes for the header, so the controller can read/write the header itself

# # --- Display (memory-mapped video) ---
# DISPLAY_START = 0x00001100

# DISPLAY_W, DISPLAY_H = 100, 100
# DISPLAY_SIZE  = DISPLAY_W * DISPLAY_H * 4   # 4 bytes per pixel




# # --- User program (static code/data -- fixed load point, fixed max size) ---
# PROGRAM_LOAD_ADDR = 0x00010000
# if DISPLAY_SIZE + DISPLAY_START > PROGRAM_LOAD_ADDR: #check for overlap with display memory
#     raise RuntimeError("Display memory overlaps program load address")
# PROGRAM_MAX_SIZE  = 0x00100000   # 1 MB reserved for code + static data

# # --- Heap: dynamic allocations, grows UP from just above the program ---
# HEAP_START = PROGRAM_LOAD_ADDR + PROGRAM_MAX_SIZE

# # --- Stack: grows DOWN from the very top of RAM ---
# STACK_TOP = RAM_SIZE - 4   # start 4 bytes in so the first PUSH doesn't touch RAM_SIZE itself