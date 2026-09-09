.ORG 0x20000

; ============================================================================
; Constants and Memory Map
; ============================================================================
DISPLAY_START = 0x1218    ; Start of display framebuffer in RAM
DISPLAY_WIDTH = 100
DISPLAY_HEIGHT = 100
PIXEL_SIZE = 4
ALPHA = 255

IO_POINTER = 0x00000200   ; IO Controller base address
IO_CHANNEL = 0
IO_R_W = 4
IO_COMMAND = 8
IO_LENGTH  = 12
IO_ADDRESS = 16

HEAP_ADDRESS = 0x120000
X = 0 ; variable screen pos X at HEAP_ADDRESS+0
Y = 4 ; variable screen pos Y at HEAP_ADDRESS+4

; ============================================================================
; Main program entry point
; ============================================================================
START:
    ; Initialize C with display start address (used across functions)
    MOV C #DISPLAY_START

    ; ========================================================================
    ; FUNC1 - Write red pixel, then wait, then jump to FUNC2
    ; ========================================================================
FUNC1:
    ; Save return address (FUNC1_RET0) by loading into C and pushing
    MOV C #FUNC1_RET0
    PUSH C                  ; Stack: [FUNC1_RET0]
    
    ; Call WRITE_COL(1) - 1 means write red
    MOV A #1
    JMP WRITE_COL
    
FUNC1_RET0:
    ; After WRITE_COL returns, call WAIT
    MOV C #FUNC1_RET1
    PUSH C                  ; Stack: [FUNC1_RET1]
    JMP WAIT
    
FUNC1_RET1:
    ; Loop back to FUNC2
    JMP FUNC2


    ; ========================================================================
    ; FUNC2 - Write blue pixel, then wait, then loop back to FUNC1
    ; ========================================================================
FUNC2:
    ; Save return address (FUNC2_RET0) by loading into C and pushing
    MOV C #FUNC2_RET0
    PUSH C                  ; Stack: [FUNC2_RET0]
    
    ; Call WRITE_COL(0) - 0 means write blue
    MOV A #0
    JMP WRITE_COL
    
FUNC2_RET0:
    ; After WRITE_COL returns, call WAIT
    MOV C #FUNC2_RET1
    PUSH C                  ; Stack: [FUNC2_RET1]
    JMP WAIT
    
FUNC2_RET1:
    ; Loop back to FUNC1
    JMP FUNC1


    ; ========================================================================
    ; WAIT - Busy-wait loop for a fixed number of iterations
    ; Destroys: A
    ; Preserves: stack pointer (expects to pop return address)
    ; ========================================================================
WAIT:
    ; Initialize counter to 10
    MOV A 1

WAIT_LOOP:
    POP A                   ; Pop return address into A
    JMP A                   ; Jump back to caller
    ; Decrement counter
    SUB A A #1

    ; Check if counter reached 0
    CMP A #0
    
    ; Padding (NOP for timing/debugging)
    NOP
    NOP
    NOP
    
    ; If A != 0, keep looping
    JNZ WAIT_LOOP

    ; Counter reached 0, return to caller
    ; Stack has: [return_address]
    POP A                   ; Pop return address into A
    JMP A                   ; Jump back to caller


    ; ========================================================================
    ; WRITE_COL - Write a pixel with color based on A
    ; Input: A = 1 for red, A = 0 for blue
    ; Destroys: E (used as pixel address pointer)
    ; Stack: Expects return address on top of stack
    ; ========================================================================
WRITE_COL:
    ; Save return address (WRITE_COL_RET)
    MOV C #WRITE_COL_RET
    PUSH C                  ; Stack: [..., WRITE_COL_RET]

    ; Dispatch based on A value
    CMP A #1
    JZ WRITE_RED            ; If A == 1, write red
    
    CMP A #0
    JZ WRITE_BLUE           ; If A == 0, write blue
    
    ; If neither 1 nor 0, fall through to return (no pixel written)

WRITE_COL_RET:
    ; Return to caller
    POP A                   ; Pop return address
    JMP A


    ; ========================================================================
    ; WRITE_RED - Write a red pixel (R=0xFF, G=0x00, B=0x00, A=0xFF)
    ; Destroys: E (used as pixel address pointer)
    ; Stack: Expects return address on top
    ; ========================================================================
WRITE_RED:
    ; Point E to the start of the pixel data
    MOV E #DISPLAY_START

    ; -------- Write Red channel (0xFF) --------
    MW E #0xFF
    
    ; -------- Write Green channel (0x00) --------
    ADD E E #1              ; Move E to next byte
    MW E #0x00
    
    ; -------- Write Blue channel (0x00) --------
    ADD E E #1              ; Move E to next byte
    MW E #0x00
    
    ; -------- Write Alpha channel (0xFF) --------
    ADD E E #1              ; Move E to next byte
    MW E #0xFF

    ; Return to caller (WRITE_COL_RET)
    POP A                   ; Pop return address
    JMP A


    ; ========================================================================
    ; WRITE_BLUE - Write a blue pixel (R=0x00, G=0x00, B=0xFF, A=0xFF)
    ; Destroys: E (used as pixel address pointer)
    ; Stack: Expects return address on top
    ; ========================================================================
WRITE_BLUE:
    ; Point E to the start of the pixel data
    MOV E #DISPLAY_START

    ; -------- Write Red channel (0x00) --------
    MW E #0x00
    
    ; -------- Write Green channel (0x00) --------
    ADD E E #1              ; Move E to next byte
    MW E #0x00
    
    ; -------- Write Blue channel (0xFF) --------
    ADD E E #1              ; Move E to next byte
    MW E #0xFF
    
    ; -------- Write Alpha channel (0xFF) --------
    ADD E E #1              ; Move E to next byte
    MW E #0xFF
    
    ; Return to caller (WRITE_COL_RET)
    POP A                   ; Pop return address
    JMP A