/* The system calls, from a program's side. See sys.h.
 *
 * Each slot in the kernel's table holds the address of a small wrapper in
 * user/os/kernel.asm, which turns interrupts off, runs the kernel's C and
 * turns them back on. The arguments travel in the program's own frame, as
 * for any C call through a function pointer, so the kernel needs to know
 * nothing about where a program keeps its frames (docs/kernel.md §10).
 */
#include <pigeon/sys.h>

#define SYS_SLOT(n) (*(unsigned *)(SYSCALL_TABLE + (n) * 4u))

typedef int  (*sys_int_ptr_n)(int, void *, unsigned);
typedef int  (*sys_path_flags)(char *, unsigned);
typedef int  (*sys_int)(int);
typedef int  (*sys_path)(char *);
typedef int  (*sys_int_stat)(int, sys_stat_t *);
typedef int  (*sys_path_stat)(char *, sys_stat_t *);
typedef int  (*sys_buf_size)(char *, unsigned);
typedef int  (*sys_exec_fn)(char *, int, char **);
typedef void (*sys_exit_fn)(int);
typedef int  (*sys_none)(void);
typedef int  (*sys_path_path)(char *, char *);

int write(int fd, void *buf, unsigned n) {
    return ((sys_int_ptr_n)SYS_SLOT(SYS_WRITE))(fd, buf, n);
}

int read(int fd, void *buf, unsigned n) {
    return ((sys_int_ptr_n)SYS_SLOT(SYS_READ))(fd, buf, n);
}

int open(char *path, unsigned flags) {
    return ((sys_path_flags)SYS_SLOT(SYS_OPEN))(path, flags);
}

int close(int fd) { return ((sys_int)SYS_SLOT(SYS_CLOSE))(fd); }

int opendir(char *path) { return ((sys_path)SYS_SLOT(SYS_OPENDIR))(path); }

int readdir(int dh, sys_stat_t *out) {
    return ((sys_int_stat)SYS_SLOT(SYS_READDIR))(dh, out);
}

int closedir(int dh) { return ((sys_int)SYS_SLOT(SYS_CLOSEDIR))(dh); }

int stat(char *path, sys_stat_t *out) {
    return ((sys_path_stat)SYS_SLOT(SYS_STAT))(path, out);
}

int chdir(char *path) { return ((sys_path)SYS_SLOT(SYS_CHDIR))(path); }

int getcwd(char *buf, unsigned size) {
    return ((sys_buf_size)SYS_SLOT(SYS_GETCWD))(buf, size);
}

int mkdir(char *path) { return ((sys_path)SYS_SLOT(SYS_MKDIR))(path); }

int rmdir(char *path) { return ((sys_path)SYS_SLOT(SYS_RMDIR))(path); }

int remove(char *path) { return ((sys_path)SYS_SLOT(SYS_REMOVE))(path); }

int rename(char *from, char *to) { return ((sys_path_path)SYS_SLOT(SYS_RENAME))(from, to); }

int exec(char *path, int argc, char **argv) {
    return ((sys_exec_fn)SYS_SLOT(SYS_EXEC))(path, argc, argv);
}

void exit(int code) { ((sys_exit_fn)SYS_SLOT(SYS_EXIT))(code); }

int getkey(void) { return ((sys_none)SYS_SLOT(SYS_GETKEY))(); }

int setcomplete(char *dir, char *builtins) {
    return ((sys_path_path)SYS_SLOT(SYS_SETCOMPLETE))(dir, builtins);
}

void print(char *s) {
    unsigned n = 0u;
    while (s[n] != 0) n++;
    write(STDOUT, s, n);
}

char *sys_strerror(int status) {
    if (status == E_NOENT) return "not found";
    if (status == -2) return "already exists";
    if (status == -3) return "not a directory";
    if (status == -4) return "is a directory";
    if (status == -5) return "not empty";
    if (status == -6) return "disk full";
    if (status == -7) return "too many open files";
    if (status == E_BADF) return "not an open file";
    if (status == -10) return "name too long";
    if (status == -11) return "busy";
    if (status == -19) return "read-only disk";
    if (status == E_NOTPROG) return "not a program";
    if (status == E_NOMEM) return "no room to run it";
    if (status == E_DEPTH) return "too many programs running";
    if (status == ENDED_DIV_ZERO) return "divided by zero";
    if (status == ENDED_BAD_OPCODE) return "ran a bad instruction";
    if (status == ENDED_BAD_FETCH) return "ran off the end of memory";
    if (status == ENDED_BREAK) return "stopped";
    return "failed";
}
