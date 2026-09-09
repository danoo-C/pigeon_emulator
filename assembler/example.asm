; Example Pigeon Assembly Program
; Counts from 0 to 10 and stores results in memory

.ORG 0x0

; Static definitions
START_VALUE = 0
MAX_VALUE = 10
RESULTS_ADDR = 0x1000   ; Where to store results
COUNTER_ADDR = 0x2000   ; Where to store the counter

START:
    ; Initialize counter
    MOV A #START_VALUE
    MOV B #MAX_VALUE
    MOV C #RESULTS_ADDR

LOOP:
    ; Store current value
    MWW C A              ; Write A (current count) to memory[C]
    
    ; Increment address for next write
    ADD C C #4           ; C += 4 (word size)
    
    ; Increment counter
    ADD A #1             ; A += 1
    
    ; Check if we're done (A < MAX_VALUE)
    CMP A B
    JL LOOP              ; Jump back if A < B
    
    ; Done!
    HALT

; Note: This demonstrates:
;   - Static definitions with arithmetic
;   - Labels and jumps
;   - Memory writes (MWW)
;   - Arithmetic (ADD, CMP)
;   - Conditional jumps (JL)
;   - Comments and structured code
