; BIOS loader for the pigeon emulator.
;
; Reads the user program off IO channel 1 into the controller's data
; window, copies it word by word to PROGRAM_LOAD_ADDR (painting each word
; into the framebuffer on the way, as a boot progress bar), waits two
; seconds on the timer, clears the screen, and jumps to the program.
;
; PROGRAM_LOAD_ADDR, IO_START, DISPLAY_START, HEAP_START and the IO_*
; header offsets are all predefined by the assembler from
; emulator/memory_map.py -- do not retype them here. user/ui.asm hardcoded
; its own copies, drifted two layout generations behind, and spent its life
; drawing into the IO region.
.ORG 0x0
SOURCE_PTR = 0

; Size of the DMA read from the boot disk. Capped by the IO data window
; (IO_SIZE - IO_USABLE_AFTER), NOT by PROGRAM_MAX_SIZE: reading more than
; the window holds would run straight into DISPLAY_START.
PROG_SIZE = 0x00001000

IO_POINTER = IO_START
IO_PROG_CHANEL = CH_USERPROG

HEAP_ADDRESS = HEAP_START
DSZE = 0

START:
    ;set up IO
    MOV A #IO_POINTER + #IO_R_W
    MWW A #0                                    ;SET IO MODE TO READ
    
    MOV A #IO_POINTER + #IO_COMMAND
    MWW A #2            ;HHD READ
    
    MOV A #IO_POINTER + #IO_LENGTH
    MWW A  #PROG_SIZE    ;SET DATA LENGHT (THE HDD HANDLES IF THE LENGS IS SMALLER THAN THE DATA)
    
    MOV A #IO_POINTER + #IO_ADDRESS
    MWW A #SOURCE_PTR   ;OUR PROGRAM LIVES ON 0x0 IN THE HDD    

    ; TRIGGER IO
    MOV A #IO_POINTER
    MWW A #IO_PROG_CHANEL             ;SELECT IO IO_CHANNEL

    ; NOW THE PROGRAM IS IN 0x00000100 + 24
    MOV B #IO_POINTER + #IO_USABLE_AFTER       ; B IS NOW OUR READ HEAD
    
    MOV A #IO_POINTER + #IO_RETURN_DATA        ; how many bytes the controller returned
    MRW C A
    DIV C C 4

    MOV A #HEAP_ADDRESS + #DSZE
    MWW A C

    MOV D #PROGRAM_LOAD_ADDR                    ; D IS WRITE HEAD
    ; DEBUG: before copy loop
COPY_LOOP:
    MRW A B ; READS THE NEXT BYTE FROM IO INTO A
    


    MOV E #DISPLAY_START
    MOV F C ; copy counter
    MUL F F #4 ;MUL counter

    ADD E E F   ; increment E


    MWw E A ; write every pixel at once

    

    ;Alpha 
    ADD E E #3 
    MW E 0xFF ; hardcode alpha to 0xff

    ; MOVE THE DATA AFTER DOING DISPLAY SHIT TO NOT BRICK THE COPY
    MWW D A ; WRITES A INTO THE PROGRAM_LOAD_ADDR 

    ;INCREMENT
    ADD B B #4
    ADD D D #4

    SUB C C #1
    CMP C #0 ; CHECKS IF WE WROTE EVERYTHING
    JNZ COPY_LOOP
    JMP #WAIT

WAIT:
    MOV A #IO_POINTER + #IO_R_W
    MWW A #0                                    ;SET IO MODE TO READ
    
    MOV A #IO_POINTER + #IO_COMMAND
    MWW A #1            ;READ
    
    MOV A #IO_POINTER + #IO_LENGTH
    MWW A  #2000   ;wait time ms

    
    MOV A #IO_POINTER + #IO_ADDRESS
    MWW A #1   ;timer 0

    ; TRIGGER IO
    MOV A #IO_POINTER
    MWW A #CH_TIMER     ;TIMER CHANEL

    MOV A #IO_POINTER + #IO_COMMAND
    MWW A #5        ;READ status
WAIT_L:
    ; TRIGGER IO
    MOV A #IO_POINTER
    MWW A #CH_TIMER     ;TIMER CHANEL

    MOV A #IO_POINTER + #IO_USABLE_AFTER
    MRW B A 
    CMP B #2
    JNZ #WAIT_L

    JMP #CLEAR

CLEAR:
    MOV A #HEAP_ADDRESS + #DSZE
    MRW C A
    ADD C C #1
    MOV B #0 ; counter

CLEAR_L:

    MOV E #DISPLAY_START
    MOV F B ; copy counter
    MUL F F #4 ;MUL counter

    ADD E E F   ; increment E

    ;Red
    ;               ADD E E #1
    MWW E 0x00000000

    ADD B B #1 ;increment couter
    SUB C C #1
    CMP C #0 ; CHECKS IF WE WROTE EVERYTHING
    JNZ CLEAR_L

    JMP #PROGRAM_LOAD_ADDR
HALT:
    HALT
    