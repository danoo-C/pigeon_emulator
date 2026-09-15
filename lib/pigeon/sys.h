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
