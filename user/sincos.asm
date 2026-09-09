.ORG 0x20000

; ============================================================================
; Optimized Oscilloscope Simulator
; - Draw crosshairs once at startup
; - Erase old sine pixels and draw new ones (no full clear)
; - Sine value is computed on the fly with an integer approximation
;   (Bhaskara I's formula) instead of read from a hardcoded LUT, so the
;   angle resolution is per-pixel (1 degree granularity via the 0-359
;   range) rather than the old fixed 16-step table.
; ============================================================================

DISPLAY_START = 0x1418
DISPLAY_WIDTH = 100
DISPLAY_HEIGHT = 100
CENTER_X = 50
CENTER_Y = 50

; Bhaskara I sine approximation, valid for 0-180 degrees, mirrored for
; 180-360:
;     sin(x deg) =~ 4*x*(180-x) / (40500 - x*(180-x))
; This returns a value in [0,1]; we scale directly by AMPLITUDE so the
; subroutine below hands back an already-scaled, already-signed offset
; in pixels (peak error is about 1px at this amplitude).
AMPLITUDE = 45          ; wave swings CENTER_Y +/- this many pixels
PHASE_STEP = 24         ; degrees the wave advances per frame
CROSSHAIR_REDRAW_INTERVAL = 10   ; redraw crosshairs every N frames (~150ms/frame)

HEAP_ADDRESS = 0x120000
CURRENT_PHASE = 0       ; Current phase (degrees, 0-359) at HEAP + 0
PREVIOUS_PHASE = 4      ; Previous phase (degrees, 0-359) at HEAP + 4
FRAME_COUNT = 8         ; Frames since the last crosshair redraw, at HEAP + 8

; Colors
COLOR_WHITE = 0xFFFFFFFF
COLOR_RED = 0xFF00FFFF
COLOR_BLACK = 0x00F0000

IO_POINTER = 0x00000400
IO_CHANNEL = 0
IO_R_W = 4
IO_COMMAND = 8
IO_LENGTH  = 12
IO_ADDRESS = 16

; ============================================================================
; Program Entry Point
; ============================================================================
START:
    MOV F #START_CROSSHAIR_RET
    PUSH F
    JMP DRAW_CROSSHAIRS
START_CROSSHAIR_RET:
    JMP INIT_STATE


; ============================================================================
; DRAW_CROSSHAIRS - Draw static crosshairs (only once at startup)
; ============================================================================
DRAW_CROSSHAIRS:
    ; Vertical line at CENTER_X
    MOV B #0
VERT_LINE:
    MOV A #CENTER_X
    MOV C B
    
    ; Inline draw white pixel
    MOV D #DISPLAY_START
    MOV E C
    MUL E E #100
    ADD E E A
    MUL E E #4
    ADD D D E
    MWW D #COLOR_WHITE
    
    ADD B B #1
    CMP B #100
    JNZ VERT_LINE
    
    ; Horizontal line at CENTER_Y
    MOV B #0
HORIZ_LINE:
    MOV A B
    MOV C #CENTER_Y
    
    ; Inline draw white pixel
    MOV D #DISPLAY_START
    MOV E C
    MUL E E #100
    ADD E E A
    MUL E E #4
    ADD D D E
    MWW D #COLOR_WHITE
    
    ADD B B #1
    CMP B #100
    JNZ HORIZ_LINE
    
    POP F
    JMP F


; ============================================================================
; INIT_STATE - Initialize phase counters
; ============================================================================
INIT_STATE:
    MOV D #HEAP_ADDRESS
    MOV A #0
    MWW D A             ; CURRENT_PHASE = 0
    
    ADD D D #4
    MWW D A             ; PREVIOUS_PHASE = 0
    
    ADD D D #4
    MWW D A             ; FRAME_COUNT = 0
    
    JMP MAIN_LOOP

WAIT:
    MOV A #IO_POINTER + #IO_R_W
    MWW A #0                                    ;SET IO MODE TO READ
    
    MOV A #IO_POINTER + #IO_COMMAND
    MWW A #1            ;READ
    
    MOV A #IO_POINTER + #IO_LENGTH
    MWW A  #150   ;wait time ms

    
    MOV A #IO_POINTER + #IO_ADDRESS
    MWW A #1   ;timer 0

    ; TRIGGER IO
    MOV A #IO_POINTER
    MWW A #4            ;TIMER CHANEL

    MOV A #IO_POINTER + #IO_COMMAND
    MWW A #5        ;READ status
WAIT_L:
    ; TRIGGER IO
    MOV A #IO_POINTER
    MWW A #4            ;TIMER CHANEL

    MOV A #IO_POINTER + 24
    MRW C A                     ; NOTE: uses C, not B - B holds our X
                                 ; counter across this call and must
                                 ; not be clobbered by the poll result
    CMP C #2
    JNZ #WAIT_L

    JMP #SINE_UPDATE_LOOP

; ============================================================================
; MAIN_LOOP - Update sine wave efficiently
; ============================================================================
MAIN_LOOP:
    ; For each X pixel (0-99), erase old sine and draw new sine
    MOV B #0            ; B = X coordinate
    JMP WAIT
    
SINE_UPDATE_LOOP:
    ; ====== Calculate old Y using math-based sine (PREVIOUS_PHASE) ======
    MOV D #HEAP_ADDRESS
    ADD D D #4
    MRW C D              ; C = PREVIOUS_PHASE (degrees)

    MOV A B
    MUL A A #360
    DIV A A #100          ; A = base angle for this X (degrees, 0-359)
    ADD A A C             ; A = base angle + phase

    MOV E A
    DIV E E #360
    MUL E E #360
    SUB A A E             ; A = angle mod 360, range [0,359]

    MOV F #OLD_SINE_RET
    PUSH F
    JMP SINE_APPROX
OLD_SINE_RET:
    ; A = signed sine offset, range [-AMPLITUDE, AMPLITUDE]

    MOV F #CENTER_Y
    SUB F F A             ; F = CENTER_Y - offset

    ; Clamp old Y
    CMP F #0
    JL OLD_CLAMP_LOW
    CMP F #100
    JG OLD_CLAMP_HIGH
    JMP OLD_Y_READY
    
OLD_CLAMP_LOW:
    MOV F #0
    JMP OLD_Y_READY
OLD_CLAMP_HIGH:
    MOV F #99
    
OLD_Y_READY:
    ; ====== Erase old pixel (black) at (B, F) ======
    MOV A B
    MOV C F
    
    MOV D #DISPLAY_START
    MOV E C
    MUL E E #100
    ADD E E A
    MUL E E #4
    ADD D D E
    MWW D #COLOR_BLACK
    
    ; ====== Calculate new Y using math-based sine (CURRENT_PHASE) ======
    MOV D #HEAP_ADDRESS
    MRW E D               ; E = CURRENT_PHASE (degrees)

    MOV A B
    MUL A A #360
    DIV A A #100           ; A = base angle for this X (degrees, 0-359)
    ADD A A E              ; A = base angle + phase

    MOV D A
    DIV D D #360
    MUL D D #360
    SUB A A D              ; A = angle mod 360, range [0,359]

    MOV F #NEW_SINE_RET
    PUSH F
    JMP SINE_APPROX
NEW_SINE_RET:
    ; A = signed sine offset, range [-AMPLITUDE, AMPLITUDE]

    MOV C #CENTER_Y
    SUB C C A              ; C = CENTER_Y - offset

    ; Clamp new Y
    CMP C #0
    JL NEW_CLAMP_LOW
    CMP C #100
    JG NEW_CLAMP_HIGH
    JMP NEW_Y_READY
    
NEW_CLAMP_LOW:
    MOV C #0
    JMP NEW_Y_READY
NEW_CLAMP_HIGH:
    MOV C #99
    
NEW_Y_READY:
    ; ====== Draw new pixel (red) at (B, C) ======
    MOV A B
    
    MOV D #DISPLAY_START
    MOV E C
    MUL E E #100
    ADD E E A
    MUL E E #4
    ADD D D E
    MWW D #COLOR_RED
    
    ; Next pixel
    ADD B B #1
    CMP B #100
    JNZ SINE_UPDATE_LOOP
    
    ; ====== Update phases for next frame ======
    MOV D #HEAP_ADDRESS
    MRW A D             ; A = CURRENT_PHASE
    
    ; Store current as previous
    ADD D D #4
    MWW D A
    
    ; Advance current phase for next frame
    MOV D #HEAP_ADDRESS
    MRW A D
    
    ADD A A #PHASE_STEP
    
    ; Wrap phase to 0-359
    MOV E A
    DIV E E #360
    MUL E E #360
    SUB A A E
    
    ; Store updated phase
    MWW D A
    
    ; ====== Periodically redraw crosshairs ======
    ; The moving trace erases through the crosshair pixels over time,
    ; so every CROSSHAIR_REDRAW_INTERVAL frames we redraw them on top
    ; of whatever's currently on screen. Doing this at the END of the
    ; frame (after this frame's trace update) means the crosshair is
    ; fully intact right after a redraw, instead of getting immediately
    ; punched through by that same frame's old-pixel erase step.
    MOV D #HEAP_ADDRESS
    ADD D D #FRAME_COUNT
    MRW A D
    ADD A A #1
    MWW D A                          ; save incremented count back first

    CMP A #CROSSHAIR_REDRAW_INTERVAL
    JL SKIP_CROSSHAIR_REDRAW

    MOV A #0
    MWW D A                          ; due - reset counter to 0

    MOV F #CROSSHAIR_RETURN
    PUSH F
    JMP DRAW_CROSSHAIRS
CROSSHAIR_RETURN:

SKIP_CROSSHAIR_REDRAW:
    JMP MAIN_LOOP


; ============================================================================
; SINE_APPROX - integer sine approximation (Bhaskara I's formula)
; Input:   A = angle in degrees, must already be wrapped to [0,359]
; Output:  A = sin(angle) * AMPLITUDE, signed, range [-AMPLITUDE, AMPLITUDE]
; Clobbers: C, D, E, F
; Preserves: B (and everything else, since it's untouched)
; Call convention: caller pushes its own return label, then JMPs in;
;   this routine POPs it and JMPs back out. No self-push shenanigans.
; ============================================================================
SINE_APPROX:
    MOV D A               ; D = angle (0-359)
    MOV F #0              ; F = 0 -> positive half, F = 1 -> negative half

    CMP D #180
    JG SINE_NEG_HALF
    JMP SINE_FOLD_DONE
SINE_NEG_HALF:
    SUB D D #180           ; fold to 0-180 range using symmetry
    MOV F #1
SINE_FOLD_DONE:
    ; D = pos (0-180), F = half flag

    MOV E #180
    SUB E E D
    MUL E E D              ; E = pos * (180 - pos), range 0-8100

    MOV C E
    MUL C C #4
    MUL C C #AMPLITUDE     ; C = 4 * pos * (180-pos) * AMPLITUDE

    MOV D #40500
    SUB D D E               ; D = 40500 - pos*(180-pos), always >= 32400

    DIV C C D               ; C = magnitude, range [0, AMPLITUDE]

    CMP F #1
    JNZ SINE_APPLY_POS
    MOV A #0
    SUB A A C               ; A = -C (negative half)
    JMP SINE_RETURN
SINE_APPLY_POS:
    MOV A C                 ; A = C (positive half)
SINE_RETURN:
    POP F
    JMP F