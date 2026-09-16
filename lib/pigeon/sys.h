/* <pigeon/sys.h> -- asking the kernel (docs/kernel_exec.md §8).
 *
 * A program the kernel runs reaches the console, the disk and other
 * programs through these calls. Each one calls through the kernel's table
 * at SYSCALL_TABLE, so a program carries these few lines instead of a
 * console and fs.c of its own -- and every program shares the kernel's one
 * screen of text and one current directory.
 *
 *     print("hello\n");
 *     status = exec("/bin/ls.bin", argc, argv);
 *     if (status < 0) print(sys_strerror(status));
 *
 * Only a program the kernel started can call them. Without a kernel the
 * table is empty, and the first call jumps to address 0.
 */
#ifndef PIGEON_SYS_H
#define PIGEON_SYS_H

#include <pigeon/syscall.h>

/* --- the console and files --------------------------------------------- */
int write(int fd, void *buf, unsigned n);   /* -> bytes written            */
int read(int fd, void *buf, unsigned n);    /* STDIN waits for a typed line,
                                               '\n' included; a file gives
                                               0 at its end               */
int open(char *path, unsigned flags);       /* -> a file descriptor >= 3   */
int close(int fd);

/* --- directories --------------------------------------------------------- */
int opendir(char *path);                    /* -> a handle                 */
int readdir(int dh, sys_stat_t *out);       /* 1 got one, 0 the end        */
int closedir(int dh);
int stat(char *path, sys_stat_t *out);
int chdir(char *path);
int getcwd(char *buf, unsigned size);       /* "2:/bin" -> its length      */
int mkdir(char *path);
int rmdir(char *path);                      /* it must be empty            */
int remove(char *path);                     /* a file                      */
int rename(char *from, char *to);           /* on one disk; to must not exist */

/* --- programs ------------------------------------------------------------ */
int  exec(char *path, int argc, char **argv);  /* runs it, waits: its status */

/* exec, with what the program writes to STDOUT -- and what the programs it
 * runs write -- going into buf instead of the console. STDERR still reaches
 * the screen. buf is left NUL-terminated and holds at most size - 1 bytes;
 * output past that is dropped, and a result exactly that long is how you
 * know. Its status is exec's. Buffers over 64 KB are used to 64 KB. */
int  exec_out(char *path, int argc, char **argv, char *buf, unsigned size);

/* exec, with files on either end of it, and NULL for the end you don't mean.
 *
 *   `out`  what the program writes to STDOUT -- and what the programs it
 *          runs write -- goes there. R_APPEND adds to the end of it; R_TRUNC
 *          writes `out~` and renames it over `out` when the program has
 *          ended by itself, so a crash, a Ctrl+C or a full disk leaves the
 *          old file whole. STDERR still reaches the console.
 *   `in`   the program's read(STDIN) takes its lines from there -- one at a
 *          time, the '\n' included, 0 at the end -- instead of the keyboard.
 *
 * A file that will not open is the answer, and the program never starts. */
int  exec_io(char *path, int argc, char **argv, char *in, char *out, unsigned how);

/* The two on their own, which is how a shell line usually reads. */
int  exec_to(char *path, int argc, char **argv, char *file, unsigned how);
int  exec_from(char *path, int argc, char **argv, char *file);
void exit(int code);                        /* ends this program, now      */
int  getkey(void);                          /* a key, or -1; never waits   */
int  setcomplete(char *dir, char *builtins);  /* Tab's commands: a directory of
                                               .bin files, and built-ins
                                               between spaces; 63 bytes each */
int  setbreak(int on);                      /* Ctrl+C as the break for this
                                               program, or a key: what it was */
int  paging(int on);                        /* -- more -- after each screen of
                                               this program's output and its
                                               children's, until it ends     */

/* --- for people ---------------------------------------------------------- */
void  print(char *s);                       /* a string to STDOUT          */
char *sys_strerror(int status);             /* "not found", "divided by zero" */

#endif
