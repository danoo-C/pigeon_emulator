FFS_HEADER = 0x12000 ; random address
FRAME_PTR = 0x0
FRAME_RETURN = 0x4
FRAME_ARGS = 0x8

FOO_ARGC = 1 ; 1 arg
FOO_B = 0x4 ; argc * 4 + 0
FOO_TEMP1 = 0x8

FFS_CALL:
    ;adds 1024 to the FF pointer
    MOV A #FFS_HEADER
    ADD A A #FRAME_PTR
    MRW B A ; get the current frame ptr
    ADD B #1024

    MOV A #FFS_HEADER
    ADD A A #FRAME_PTR

    MMW A B

    POP A
    JMP A

FFS_RETURN:
    ;adds 1024 to the FF pointer
    MOV A #FFS_HEADER
    ADD A A #FRAME_PTR
    MRW B A ; get the current frame ptr
    SUB B #1024

    MOV A #FFS_HEADER
    ADD A A #FRAME_PTR

    MMW A B

    POP A
    JMP A



FOO:
    MOV A #FOO_INIT_RET
    PUSH A
FOO_INIT_RET:

    ;===== int b = 3 =======
    MOV A #FFS_HEADER ; get the FFS header
    ADD A #FRAME_PTR ; get the frame poiner
    MRW B A ; read the frame pointer
    ;B is our frame
    
    ADD B B #FOO_B;now get the address of int b
    MOV A #3 ; =3
    MWW B A

    ;===== return x*3 =======
    MOV A #FFS_HEADER ; get the FFS header
    ADD A #FRAME_PTR ; get the frame poiner
    MRW B A
    ADD B B #0      ;first arg
    ;get x
    MRW F b


    ADD A #FRAME_PTR ; get the frame poiner
    MRW B A ; read the frame pointer
    ;B is our frame
    
    ADD B B #FOO_B;now get the address of int b
    MWW B A
    
   


    