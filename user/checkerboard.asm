.ORG 0x20000

; ============================================================================
; Constants and Memory Map
; ============================================================================
DISPLAY_START = 0x1418    ; Start of display framebuffer in RAM
DISPLAY_WIDTH = DISPLAY_W
DISPLAY_HEIGHT = DISPLAY_H
PIXEL_SIZE = 4
ALPHA = 255

; Heap memory layout
HEAP_ADDRESS = HEAP_START
X_OFFSET = 0              ; X coordinate stored at HEAP_ADDRESS + 0
Y_OFFSET = 4              ; Y coordinate stored at HEAP_ADDRESS + 4

IO_POINTER = 0x00000400   ; IO Controller base address


; ============================================================================
; Main program entry point
; ============================================================================
START:
    ; Initialize D as heap pointer (used to access X and Y variables)
    MOV D #HEAP_ADDRESS

    ; Start the main loop: iterate through all pixels (0-9999 for 100x100 grid)
    MOV B #0                ; B will track current pixel index

PIXEL_LOOP:
    ; ========================================================================
    ; Calculate X and Y from linear pixel index
    ; pixel_index = Y * DISPLAY_WIDTH + X
    ; So: Y = pixel_index / 100, X = pixel_index % 100
    ; ========================================================================
    
    ; Calculate Y = B / DISPLAY_WIDTH (B / 100)
    MOV A B                 ; A = pixel_index
    DIV A A #100            ; A = pixel_index / 100 (integer division)
    
    ; Store Y at HEAP_ADDRESS + Y_OFFSET
    MOV D #HEAP_ADDRESS
    ADD D D #Y_OFFSET       ; D = HEAP_ADDRESS + 4
    MWW D A                 ; Write Y to [D]
    
    ; Calculate X = pixel_index % 100
    ; X = pixel_index - (Y * 100)
    MOV E A                 ; E = Y (from calculation above)
    MUL E E #100            ; E = Y * 100
    MOV A B                 ; A = pixel_index
    SUB A A E               ; A = pixel_index - (Y * 100) = X
    
    ; Store X at HEAP_ADDRESS + X_OFFSET
    MOV D #HEAP_ADDRESS
    MWW D A                 ; Write X to [D] (at offset 0)
    
    ; ========================================================================
    ; Determine pixel color based on checkerboard pattern
    ; Pattern: if (X + Y) is even -> RED, if (X + Y) is odd -> BLUE
    ; We use bitwise AND with 1 to get the least significant bit
    ; ========================================================================
    
    ; Load X and Y from heap
    MOV D #HEAP_ADDRESS
    MRW A D                 ; A = X (read from HEAP_ADDRESS + 0)
    ADD D D #Y_OFFSET
    MRW E D                 ; E = Y (read from HEAP_ADDRESS + 4)
    
    ; Calculate (X + Y) mod 2 using bitwise AND
    ADD A A E               ; A = X + Y
    AND A A #1              ; A = (X + Y) & 1 = least significant bit (0 or 1)
    ; Now A is 0 if (X + Y) is even, or 1 if (X + Y) is odd
    
    ; Call DRAW_PIXEL with color in A (0 = RED for even, 1 = BLUE for odd)
    MOV F #DRAW_PIXEL_RET
    PUSH F
    JMP DRAW_PIXEL
    
DRAW_PIXEL_RET:
    ; Increment pixel index and check if we've drawn all pixels
    ADD B B #1
    
    ; Total pixels = DISPLAY_WIDTH * DISPLAY_HEIGHT = 100 * 100 = 10000
    CMP B #10000
    JNZ PIXEL_LOOP
    
    ; All pixels drawn, infinite loop
    JMP DRAW_PIXEL_RET      ; Just spin here


; ============================================================================
; DRAW_PIXEL - Draw a single pixel at (X, Y) with given color
; Input: A = color (0 = RED, 1 = BLUE)
; Stack: Expects return address on top
; Destroys: E, F (used as address pointers)
; ============================================================================
DRAW_PIXEL:
    MOV F #DRAW_PIXEL_RET_LABEL
    PUSH F
    
    ; Load X and Y from heap
    MOV D #HEAP_ADDRESS
    MRW E D                 ; E = X (at offset 0)
    
    MOV D #HEAP_ADDRESS
    ADD D D #Y_OFFSET
    MRW F D                 ; F = Y (at offset 4)
    
    ; ========================================================================
    ; Calculate pixel byte address in RAM
    ; Address = DISPLAY_START + (Y * DISPLAY_WIDTH + X) * 4
    ; Each pixel is 4 bytes (RGBA)
    ; ========================================================================
    
    ; Calculate linear position = Y * DISPLAY_WIDTH + X
    MOV D F                 ; D = Y
    MUL D D #DISPLAY_WIDTH  ; D = Y * DISPLAY_WIDTH
    ADD D D E               ; D = Y * DISPLAY_WIDTH + X (linear pixel index)
    
    ; Multiply by 4 to get byte offset (each pixel is 4 bytes: RGBA)
    MUL D D #4              ; D = (Y * DISPLAY_WIDTH + X) * 4
    
    ; Add display start address to get final RAM address
    ADD D D #DISPLAY_START  ; D = DISPLAY_START + byte_offset (final pixel address)
    
    ; ========================================================================
    ; Draw pixel based on color in A
    ; ========================================================================
    CMP A #0
    JZ DRAW_RED
    
    CMP A #1
    JZ DRAW_BLUE
    
    ; Invalid color, skip
    JMP DRAW_PIXEL_RET_LABEL

DRAW_RED:
    ; Write RGBA: FF 00 00 FF (Red, Green, Blue, Alpha)
    MW D #0xFF              ; Red channel
    ADD D D #1
    MW D #0x00              ; Green channel
    ADD D D #1
    MW D #0x00              ; Blue channel
    ADD D D #1
    MW D #0xFF              ; Alpha channel
    JMP DRAW_PIXEL_RET_LABEL

DRAW_BLUE:
    ; Write RGBA: 00 00 FF FF (Red, Green, Blue, Alpha)
    MW D #0x00              ; Red channel
    ADD D D #1
    MW D #0x00              ; Green channel
    ADD D D #1
    MW D #0xFF              ; Blue channel
    ADD D D #1
    MW D #0xFF              ; Alpha channel
    JMP DRAW_PIXEL_RET_LABEL

DRAW_PIXEL_RET_LABEL:
    POP A
    JMP A
