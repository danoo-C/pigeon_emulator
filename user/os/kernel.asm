; kernel.asm -- the kernel's routines that C can't write (docs/kernel.md §9,
; §10, §13). kernel.c names this file with #asm, so its labels and these are
; one program: C reaches a routine as `extern int name;` cast to a function,
; and a routine reaches a C global as __g_name and a C function by name.

; void kinit(void): point the CPU at the vector table kernel.c filled in.
kinit:
    SETIV #__g_vectors
    RET

; void khalt(void): stop the machine, after a panic.
khalt:
    DI
    HALT

; int exec_call(entry, argc, argv, save): call a program as entry(argc, argv).
; save[0] and save[1] remember the stack pointer and F, so exec_abort can
; come back here however deep the program has gone. The program runs with
; interrupts on, and with the kernel marked as not running.
exec_call:
    ADD C, F, #12
    MRW D, C            ; D = save
    GETSP A
    MWW D, A            ; save[0] = SP: the return address into the kernel
    ADD D, D, #4
    MWW D, F            ; save[1] = F
    MRW E, F            ; E = entry
    ADD C, F, #4
    MRW A, C            ; argc
    ADD C, F, #8
    MRW B, C            ; argv
    ADD F, F, #16       ; the program's caller frame: its argc and argv
    MWW F, A
    ADD C, F, #4
    MWW C, B
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    CALL E
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    SUB F, F, #16
    RET                 ; A = what the program returned

; void exec_abort(code, save): return from the exec_call that filled save,
; with code as its value. Never returns to its own caller.
exec_abort:
    ADD C, F, #4
    MRW D, C            ; D = save
    MRW A, F            ; A = code
    MRW B, D            ; B = SP as it was inside exec_call
    ADD D, D, #4
    MRW F, D            ; F as it was inside exec_call
    SETSP B
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    RET

; --- the system calls ---------------------------------------------------------
; A program calls through SYSCALL_TABLE into one of these, with its arguments
; in its own frame. Each runs the kernel's C with interrupts off, so neither a
; break nor a tick lands halfway through fs.c, and with the kernel marked as
; running, so a fault in it is a panic rather than the program's end.

w_write:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_write
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_read:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_read
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_open:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_open
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_close:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_close
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_opendir:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_opendir
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_readdir:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_readdir
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_closedir:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_closedir
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_stat:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_stat
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_chdir:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_chdir
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_getcwd:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_getcwd
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_exec:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_exec
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_exec_out:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_exec_out
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_exec_io:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_exec_io
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_getkey:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_getkey
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_mkdir:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_mkdir
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_rmdir:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_rmdir
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_remove:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_remove
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_rename:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_rename
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_setcomplete:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_setcomplete
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_setbreak:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_setbreak
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_paging:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_paging
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_keepscreen:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_keepscreen
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_consize:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_consize
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_setmode:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_setmode
    MOV C, #__g_in_kernel
    MWW C, #0
    EI
    RET

w_exit:
    DI
    MOV C, #__g_in_kernel
    MWW C, #1
    CALL k_exit         ; never returns

; void kswallow(void): take any interrupt already pending, with break's
; vector pointing at irq_ignore and interrupts on for exactly one
; instruction, then put the vector back. A Ctrl+C pressed while the kernel
; was busy isn't for the program about to run (docs/phase4_plan.md step 10).
kswallow:
    MOV C, #__g_vectors
    ADD C, C, #16       ; vector 4, VEC_BREAK
    MRW D, C
    MOV E, #irq_ignore
    MWW C, E
    EI
    NOP
    DI
    MWW C, D
    RET

; --- faults and Ctrl+C ------------------------------------------------------------
; Each ends the program running, through k_fault(vector, pc), on a frame stack
; of the kernel's own: F still points into whatever was interrupted. k_fault
; panics instead when the kernel itself was running.

fault_div:
    MOV A, #VEC_DIV_ZERO
    JMP fault_common
fault_opcode:
    MOV A, #VEC_BAD_OPCODE
    JMP fault_common
fault_fetch:
    MOV A, #VEC_BAD_FETCH
    JMP fault_common
on_break:
    MOV A, #VEC_BREAK
    JMP fault_common
fault_common:
    GETSP D
    MRW B, D            ; where it happened: the address the CPU pushed
    MOV F, #__g_fault_frames
    MWW F, A
    ADD C, F, #4
    MWW C, B
    CALL k_fault        ; never returns

; A timer tick: the kernel starts no timer, but a program may.
irq_ignore:
    IRET
