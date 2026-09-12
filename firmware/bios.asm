; BIOS loader for the pigeon emulator.
;
; Reads the user program off IO channel 1 and copies it to
; PROGRAM_LOAD_ADDR, painting each word into the framebuffer as it goes
; (a boot progress bar made of program bytes), then waits two seconds,
; clears the screen and jumps to the program.
;
; The DMA window is only IO_SIZE - IO_USABLE_AFTER bytes (4 KB), so the
; load runs as a LOOP over chunks. It used to be a single read, which
; silently truncated any program over 4 KB: the tail never arrived, the
; CPU ran into whatever followed, and there was no diagnostic at all.
;
; The progress bar is CLAMPED to the framebuffer, and the clear that
; follows wipes exactly the framebuffer. Both used to walk one word per
; word copied, from DISPLAY_START, with no upper bound -- which is only
; safe while the program is smaller than the distance from DISPLAY_START
; to PROGRAM_LOAD_ADDR. Past that the bar runs off the bottom of the
; screen and into the load area, and the clear then zeroes the front of
; the program it just loaded. The ceiling was 125,928 bytes and nothing
; announced it: user/files.c compiled to 169,076, booted into 43 KB of
; zeros, and ran until a RET found a return address that was never
; pushed. It is a clamp and not a bigger gap on purpose -- the gap is
; whatever the display happens to need, so the next resolution change
; would move the ceiling again.
;
; PROGRAM_LOAD_ADDR, IO_START, DISPLAY_START, HEAP_START and the IO_*
; header offsets are all predefined by the assembler from
; emulator/memory_map.py -- do not retype them here. user/ui.asm hardcoded
; its own copies, drifted two layout generations behind, and spent its
; life drawing into the IO region.
.ORG 0x0

; One DMA transfer can move at most the data window. Derived, not
; hardcoded: the old PROG_SIZE = 0x1000 happened to end exactly at
; DISPLAY_START, so a one-byte overrun landed in the framebuffer.
WINDOW = IO_SIZE - IO_USABLE_AFTER

; How many words the screen holds -- the bound on both loops below.
DISPLAY_WORDS = DISPLAY_SIZE / 4

IO_POINTER = IO_START
IO_PROG_CHANEL = CH_USERPROG
HDD_READ = 2

; BIOS scratch, in the heap region. The CPU has six registers and the
; copy loop needs five, so the loop counters that must survive a chunk
; boundary live in memory.
HEAP_ADDRESS = HEAP_START
DSZE     = 0      ; total words copied, read later by the screen clear
DISK_OFF = 4      ; byte offset of the next chunk on the boot disk

START:
    MOV A #HEAP_ADDRESS + #DSZE
    MWW A #0                        ; words copied so far
    MOV A #HEAP_ADDRESS + #DISK_OFF
    MWW A #0                        ; start at offset 0 on the disk

    MOV D #PROGRAM_LOAD_ADDR        ; D is the write head, across all chunks

; ---------------------------------------------------------------- chunk
CHUNK:
    MOV A #IO_POINTER + #IO_R_W
    MWW A #0                        ; read
    MOV A #IO_POINTER + #IO_COMMAND
    MWW A #HDD_READ
    MOV A #IO_POINTER + #IO_LENGTH
    MWW A #WINDOW

    MOV A #HEAP_ADDRESS + #DISK_OFF
    MRW B A                         ; B = disk offset
    MOV A #IO_POINTER + #IO_ADDRESS
    MWW A B

    MOV A #IO_POINTER
    MWW A #IO_PROG_CHANEL           ; fire it

    MOV A #IO_POINTER + #IO_RETURN_DATA
    MRW C A                         ; C = bytes this chunk returned
    CMP C #0
    JZ DONE                         ; nothing came back: end of program

    ; advance the disk offset by what we actually got
    MOV A #HEAP_ADDRESS + #DISK_OFF
    MRW E A
    ADD E E C
    MWW A E

    ; remember whether this was a full window, before C is consumed
    MOV A #HEAP_ADDRESS + #DSZE + 8
    MWW A C                         ; scratch: this chunk's byte count

    DIV C C #4                      ; bytes -> words
    MOV B #IO_POINTER + #IO_USABLE_AFTER   ; B = read head into the window

COPY_LOOP:
    MRW A B                         ; next word out of the IO window

    ; progress bar: paint this word at the next pixel, ascending, until
    ; the screen is full -- past that the pixel would land outside the
    ; framebuffer, and on a big enough program inside the program.
    MOV E #HEAP_ADDRESS + #DSZE
    MRW F E                         ; F = words copied so far
    CMP F #DISPLAY_WORDS
    JGE STORE_WORD
    MUL F F #4
    MOV E #DISPLAY_START
    ADD E E F
    MWW E A
    ADD E E #3
    MW E #0xFF                      ; force alpha opaque

STORE_WORD:
    MWW D A                         ; write the word to the load address

    ; bump the copied-words counter
    MOV E #HEAP_ADDRESS + #DSZE
    MRW F E
    ADD F F #1
    MWW E F

    ADD B B #4
    ADD D D #4
    SUB C C #1
    CMP C #0
    JNZ COPY_LOOP

    ; a short chunk means we reached the end of the program
    MOV A #HEAP_ADDRESS + #DSZE + 8
    MRW C A
    CMP C #WINDOW
    JZ CHUNK                        ; full window: there may be more
    JMP DONE

DONE:
    JMP WAIT

; ----------------------------------------------------------------- wait
WAIT:
    MOV A #IO_POINTER + #IO_R_W
    MWW A #0
    MOV A #IO_POINTER + #IO_COMMAND
    MWW A #1                        ; timer START
    MOV A #IO_POINTER + #IO_LENGTH
    MWW A #2000                     ; milliseconds
    MOV A #IO_POINTER + #IO_ADDRESS
    MWW A #1                        ; timer id
    MOV A #IO_POINTER
    MWW A #CH_TIMER

    MOV A #IO_POINTER + #IO_COMMAND
    MWW A #5                        ; timer STATUS
WAIT_L:
    MOV A #IO_POINTER
    MWW A #CH_TIMER
    MOV A #IO_POINTER + #IO_USABLE_AFTER
    MRW B A
    CMP B #2                        ; STATUS_DONE
    JNZ WAIT_L

; ---------------------------------------------------------------- clear
; The whole screen, not "as many words as the bar painted". The bar's
; count is the program's size, which is not a screen and is not bounded
; by one.
CLEAR:
    MOV C #DISPLAY_WORDS
    MOV B #0
CLEAR_L:
    MOV E #DISPLAY_START
    MOV F B
    MUL F F #4
    ADD E E F
    MWW E #0x00000000
    ADD B B #1
    SUB C C #1
    CMP C #0
    JNZ CLEAR_L

    JMP #PROGRAM_LOAD_ADDR
