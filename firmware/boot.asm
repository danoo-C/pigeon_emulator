; Boot sector for the pigeon emulator (docs/os_cd.md).
;
; This is the code in bytes BOOT_CODE..511 of a bootable disk's block 0,
; its PigeonFS superblock: 384 bytes, 48 instructions. bios2 copies the
; whole block to BOOT_LOAD_ADDR, writes the channel it read it from to
; BOOT_CHANNEL, and calls BOOT_ENTRY, which is here.
;
; It loads the file named by the boot record -- also in block 0, at
; BOOT_RECORD: [BOOT_SIGNATURE, first block, size in bytes] -- to
; PROGRAM_LOAD_ADDR and jumps to it. The file must be contiguous: 48
; instructions cannot follow a FAT chain, so whatever writes the record
; checks that (tools/pfs.py, PgfsImage.make_bootable).
;
; It reads through the IO window, 4 KB at a time, whatever the channel.
; The CD drive has no DMA, so one path serves the hard disk and the disc.
; Each chunk copies only what is left of the file, so nothing past its
; last word is written.
;
; If it cannot load the file -- a size of 0 or over PROGRAM_MAX_SIZE, or
; the disk ending before the file does -- it returns to bios2, which says
; so in its menu. That is why F is saved first: it is bios2's frame
; pointer, and the loop uses F for the channel.
.ORG BOOT_ENTRY

WINDOW       = IO_SIZE - IO_USABLE_AFTER
WINDOW_BASE  = IO_START + IO_USABLE_AFTER
RECORD_FIRST = BOOT_LOAD_ADDR + BOOT_RECORD + 4
RECORD_SIZE  = BOOT_LOAD_ADDR + BOOT_RECORD + 8
DISK_READ    = 2

BOOT:
    PUSH F                          ; bios2's frame pointer, for a boot that fails
    MOV A #BOOT_CHANNEL
    MRW F A                         ; F = the channel to read
    MOV A #RECORD_SIZE
    MRW C A                         ; C = bytes still to load
    SUB A C #1                      ; unsigned, so 0 wraps and fails too
    CMP A #PROGRAM_MAX_SIZE
    JGE FAIL                        ; empty, or it would run into HEAP_START
    MOV A #RECORD_FIRST
    MRW B A
    MUL B B #BOOT_BLOCK             ; B = the file's byte offset on the disk
    MOV D #PROGRAM_LOAD_ADDR        ; D = where the next word goes

; ---------------------------------------------------------------- chunk
CHUNK:
    MOV A #IO_START + #IO_R_W
    MWW A #0
    MOV A #IO_START + #IO_COMMAND
    MWW A #DISK_READ
    MOV A #IO_START + #IO_LENGTH
    MWW A #WINDOW
    MOV A #IO_START + #IO_ADDRESS
    MWW A B
    MOV A #IO_START
    MWW A F                         ; fire it
    MOV A #IO_START + #IO_RETURN_DATA
    MRW E A                         ; E = bytes that came back
    CMP E #0
    JZ FAIL                         ; the disk ended before the file did
    ADD B B E                       ; the next chunk starts after these
    MOV A E
    CMP C E
    JGE WHOLE
    MOV A C                         ; the file ends inside this chunk
WHOLE:
    SUB C C A                       ; A = bytes of the file in this chunk
    ADD A A #WINDOW_BASE            ; A = where they end in the window
    PUSH B                          ; the copy needs B for the word
    MOV E #WINDOW_BASE

COPY:
    MRW B E
    MWW D B
    ADD E E #4
    ADD D D #4
    CMP E A
    JL COPY                         ; a partial last word is copied whole

    POP B
    CMP C #0
    JNZ CHUNK
    JMP #PROGRAM_LOAD_ADDR

FAIL:
    POP F
    RET
