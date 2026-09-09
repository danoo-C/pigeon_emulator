; BIOS loader for pigeon emulator - FIXED VERSION
; Reads actual program size from IO buffer, doesn't overwrite if size is 0

.ORG 0x0
SOURCE_PTR = 0
PROGRAM_LOAD_ADDR = 0x10000
IO_POINTER = 0x00000100
IO_CHANNEL = 0
IO_R_W = 4
IO_COMMAND = 8
IO_LENGTH  = 12
IO_ADDRESS = 16
IO_PROG_CHANEL = 1
MAX_PROG_SIZE = 0x00001000

DISPLAY_OFF = 0x00001100

START:
    ; Set up IO to read program from HDD
    MWW #IO_POINTER + #IO_R_W #0                ; SET IO MODE TO READ
    MWW #IO_POINTER + #IO_COMMAND #2            ; HDD READ
    MWW #IO_POINTER + #IO_LENGTH #MAX_PROG_SIZE ; Read max size
    MWW #IO_POINTER + #IO_ADDRESS #SOURCE_PTR   ; Read from HDD offset 0

    ; TRIGGER IO
    MWW #IO_POINTER #IO_PROG_CHANEL             ; SELECT IO CHANNEL

    ; Read actual program size from IO buffer at 0x0100
    MRW C #IO_POINTER                           ; Read 4-byte size from 0x0100
    
    ; Check if size is 0 (no program loaded)
    ;CMP C #0
    ;JZ SKIP_COPY                                ; If size is 0, skip copy and halt
    
    ; Setup copy: B = source, C = size, D = dest
    MOV B #IO_POINTER + 20                      ; B = read head (data starts at 0x0114)
    MOV D #PROGRAM_LOAD_ADDR                    ; D = write head (0x10000)

COPY_LOOP:
    MR A B                                      ; Read byte from IO
    MW D A                                      ; Write to program memory
    
    ; Increment pointers
    ADD B B #1
    ADD D D #1
    
    ; Decrement counter
    SUB C #1
    CMP C #0
    JNZ COPY_LOOP
    
    ; Jump to loaded program
    MOV B #DISPLAY_OFF
    ADD B #0xFF
    MWW B #0x00FF00FF
    JMP #PROGRAM_LOAD_ADDR
    


SKIP_COPY:
    ; No program was loaded, halt
    MWW #DISPLAY_OFF 0xFF0000FF

    HALT

