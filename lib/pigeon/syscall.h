/* <pigeon/syscall.h> -- the kernel's system calls: slots, flags, errors.
 *
 * Shared by the kernel (user/os/kernel.c), which fills the table, and by
 * <pigeon/sys.h>, whose functions call through it. It has no .c of its
 * own, so including it adds no code to a program. The table is
 * SYSCALL_SLOTS words at SYSCALL_TABLE, both predefined from
 * emulator/memory_map.py (docs/kernel.md §10).
 */
#ifndef PIGEON_SYSCALL_H
#define PIGEON_SYSCALL_H

/* --- which slot is which ---------------------------------------------- */
#define SYS_WRITE    0
#define SYS_READ     1
#define SYS_OPEN     2
#define SYS_CLOSE    3
#define SYS_OPENDIR  4
#define SYS_READDIR  5
#define SYS_CLOSEDIR 6
#define SYS_STAT     7
#define SYS_CHDIR    8
#define SYS_GETCWD   9
#define SYS_EXEC     10
#define SYS_EXIT     11
#define SYS_GETKEY   12
#define SYS_MKDIR    13
#define SYS_RMDIR    14
#define SYS_REMOVE   15
#define SYS_RENAME   16
#define SYS_SETCOMPLETE 17
#define SYS_SETBREAK 18
#define SYS_PAGING   19
#define SYS_COUNT    20

/* --- the console: every program starts with these; open() gives 3 up -- */
#define STDIN  0
#define STDOUT 1
#define STDERR 2

/* --- open() flags: the same bits as <pigeon/fs.h>'s FS_READ... ---------- */
#define O_READ   1
#define O_WRITE  2
#define O_CREATE 4          /* these three need O_WRITE too */
#define O_TRUNC  8
#define O_APPEND 16

/* --- what stat() and readdir() fill in: fs_stat_t's layout -------------- */
#define S_FILE 1
#define S_DIR  2
typedef struct { char name[32]; unsigned type; unsigned size; } sys_stat_t;

/* --- errors: below 0 ---------------------------------------------------
 * -1 to -19 are <pigeon/fs.h>'s FS_E* codes, passed on as they are. These
 * are the kernel's own, from exec(). */
#define E_NOENT   (-1)      /* FS_ENOENT: no such file or directory */
#define E_BADF    (-8)      /* FS_EBADF: not an open file           */
#define E_NOTPROG (-20)     /* not a program file, or a damaged one */
#define E_NOMEM   (-21)     /* no room above the programs running   */
#define E_DEPTH   (-22)     /* programs running programs too deep   */
#define E_QUIT    (-23)     /* write: q at -- more -- (paging)      */

/* --- how exec() says a program did not return -------------------------- */
#define ENDED_DIV_ZERO   (-100)     /* it divided by zero        */
#define ENDED_BAD_OPCODE (-101)     /* it ran a bad instruction  */
#define ENDED_BAD_FETCH  (-102)     /* it ran off the end of memory */
#define ENDED_BREAK      (-103)     /* Ctrl+C stopped it         */
#define ENDED_QUIT       (-104)     /* q at -- more -- stopped it */

#endif
