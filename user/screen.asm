; RGB Fade Display Program
; 
; Display formula: R = X + Y, G = X, B = Y
; Creates a smooth gradient fade across the 64x64 display
;
; Pixel format: [B, G, R, A] at DISPLAY_START + (Y*64 + X)*4
 
.ORG 0x20000
 
; Constants
DISPLAY_START = 0x1418
DISPLAY_WIDTH = 100
DISPLAY_HEIGHT = 100
PIXEL_SIZE = 4
ALPHA = 255
 
; Entry point
START:
    MOV E #0              ; E = Y counter (rows)
    
LOOP_Y:
    ; Check if Y >= DISPLAY_HEIGHT
    CMP E #DISPLAY_HEIGHT
    JGE DONE
    
    MOV D #0              ; D = X counter (columns)
    
LOOP_X:
    ; Check if X >= DISPLAY_WIDTH
    CMP D #DISPLAY_WIDTH
    JGE NEXT_Y
    
    ; Calculate pixel offset: (Y * 64 + X) * 4
    ; offset = (Y * WIDTH + X) * PIXEL_SIZE
    
    MOV A E               ; A = Y
    MUL A A #DISPLAY_WIDTH  ; A = Y * WIDTH
    ADD A A D             ; A = Y * WIDTH + X
    MUL A A #PIXEL_SIZE   ; A = (Y * WIDTH + X) * 4
    
    ; Calculate pixel address: DISPLAY_START + offset
    ADD A A #DISPLAY_START
    
    ; Now write the pixel
    ; B = Y, G = X, R = X + Y, A = 255
    
    ; Write B (Blue = Y) at offset 0
    MOV B D               ; B = X
    AND B B E             ; B = X + Y
    MOV C A            ; C = A + 2 (R offset)
    MW C B                ; memory[A+2] = X + Y
    
    ; Write G (Green = X) at offset 1
    MOV B D               ; B = X
    OR B B E 
    MOV C A
    ADD C C #1            ; C = A + 1 (G offset)
    MW C B                ; memory[A+1] = X
    
    ; Write R (Red = X + Y) at offset 2
    MOV B D               ; B = X
    XOR B B E             ; B = X + Y
    MOV C A
    ADD C C #2            ; C = A + 2 (R offset)
    MW C B                ; memory[A+2] = X + Y
    
    ; Write A (Alpha = 255) at offset 3
    MOV B #ALPHA          ; B = 255
    MOV C A
    ADD C C #3            ; C = A + 3 (A offset)
    MW C B                ; memory[A+3] = 255
    
    ; Increment X and loop
    ADD D D #1
    JMP LOOP_X
    
NEXT_Y:
    ; Increment Y and loop
    ADD E E #1
    JMP LOOP_Y
    
DONE:
    HALT