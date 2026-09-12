/* <pigeon/fs.h> -- PigeonFS: files and directories on the HDD channels.
 *
 * The design, with the reasoning for every choice here, is in
 * docs/filesystem.md. tools/pfs.py implements the same format on the
 * host: use it to put files on a disk, look at what a program wrote, and
 * check a disk with fsck.
 *
 *     if (fs_mount(CH_HDD) == FS_ENOFS) {       -- a brand-new, blank disk
 *         fs_format(CH_HDD, "PIGEON", 0);
 *         fs_mount(CH_HDD);
 *     }
 *     fs_mkdir("/saves");
 *     fs_save("/saves/score", &score, 4);
 *
 * Disks are named by their IO channel. A path may start with one --
 * "2:/saves/a" -- and otherwise means the current volume; a path without
 * a leading '/' is relative to the current directory, and "." and ".."
 * work as usual. The first volume mounted becomes current, at its root.
 *
 * A disk keeps its contents until it is deliberately reformatted:
 * fs_format() formats only a blank disk unless given FS_FORMAT_FORCE --
 * and on CH_USERPROG, forcing overwrites the program's own .bin on the
 * host.
 *
 * Every call leaves the disk written through to the host file before it
 * returns, so stopping the emulator between two calls loses nothing.
 * fs_sync() adds an fsync on the host, for the host losing power.
 *
 * A disk may be READ-ONLY -- a disc in the CD drive is (docs/cd-drive.md).
 * Mounting, reading and walking one all work; everything that would write
 * returns FS_EROFS. That is worth stating because the alternative was
 * worse: the device simply refuses the write, so before this existed
 * fs_save() on a disc returned the byte count for bytes that went
 * nowhere.
 */
#ifndef PIGEON_FS_H
#define PIGEON_FS_H

/* --- errors -------------------------------------------------------------
 * Every call returns >= 0 on success and one of these on failure, the
 * same convention as key_read() returning -1. */
#define FS_OK             0
#define FS_ENOENT       (-1)    /* no such file or directory             */
#define FS_EEXIST       (-2)
#define FS_ENOTDIR      (-3)    /* a path component is a file            */
#define FS_EISDIR       (-4)
#define FS_ENOTEMPTY    (-5)
#define FS_ENOSPC       (-6)    /* disk full                             */
#define FS_EMFILE       (-7)    /* too many open handles, or volumes     */
#define FS_EBADF        (-8)    /* not an open handle, or wrong mode     */
#define FS_EINVAL       (-9)
#define FS_ENAMETOOLONG (-10)   /* a name over 31 bytes, a path over 255 */
#define FS_EBUSY        (-11)   /* it is open, it is the current
                                   directory, or the volume has open files */
#define FS_ENODEV       (-12)   /* no disk on that channel, or not mounted */
#define FS_ENOFS        (-13)   /* the disk has no PigeonFS on it        */
#define FS_ENOTBLANK    (-14)   /* format refused: the disk holds data   */
#define FS_EXDEV        (-15)   /* rename across volumes                 */
#define FS_E2BIG        (-16)   /* the result is bigger than the buffer  */
#define FS_ECORRUPT     (-17)   /* inconsistent on disk: run pfs fsck    */
#define FS_ENOMEM       (-18)   /* the heap could not provide the cache  */
#define FS_EROFS        (-19)   /* a read-only disk: a disc in the CD
                                   drive, not a disk on an HDD channel  */

char *fs_strerror(int err);     /* for disp_text()                       */

/* --- volumes ---------------------------------------------------------- */
#define FS_FORMAT_FORCE 1       /* format a disk that holds data, PigeonFS or not */

int fs_format(unsigned channel, char *label, unsigned flags);  /* label: <= 15 bytes */
int fs_mount(unsigned channel);   /* mounting a mounted channel is fine      */
int fs_unmount(unsigned channel); /* FS_EBUSY while handles are open on it   */
int fs_sync(unsigned channel);    /* fsync the image on the host             */

typedef struct {
    unsigned total_blocks;
    unsigned free_blocks;
    unsigned block_size;
    char     label[16];
} fs_volinfo;
int fs_statvfs(unsigned channel, fs_volinfo *out);

/* --- files ------------------------------------------------------------ */
#define FS_READ     1
#define FS_WRITE    2
#define FS_CREATE   4           /* these three need FS_WRITE too */
#define FS_TRUNC    8
#define FS_APPEND  16

#define FS_SEEK_SET 0
#define FS_SEEK_CUR 1
#define FS_SEEK_END 2

/* One writer per file: opening for FS_WRITE while any other handle has
 * the file open is FS_EBUSY. Readers may share it, and a reader sees the
 * file as it was when it was opened. */
int fs_open (char *path, unsigned flags);       /* -> a handle >= 0        */
int fs_read (int fd, void *buf, unsigned n);    /* -> bytes, 0 at the end  */
int fs_write(int fd, void *buf, unsigned n);    /* -> bytes, short when full */
int fs_seek (int fd, int offset, int whence);   /* -> the new position; no
                                                   seeking past the end    */
int fs_tell (int fd);
int fs_close(int fd);

/* --- lines ------------------------------------------------------------
 * fs_gets reads one line into buf, keeping the '\n' as fgets does, so 0
 * means the end of the file and an empty line is "\n". A line longer
 * than the buffer comes back in pieces. size must be at least 2. */
int fs_gets(int fd, char *buf, unsigned size);
int fs_puts(int fd, char *s);                   /* -> bytes written        */

/* --- the namespace ---------------------------------------------------- */
#define FS_NAME_MAX  31
#define FS_PATH_MAX  255
#define FS_TYPE_FILE 1
#define FS_TYPE_DIR  2

typedef struct { char name[32]; unsigned type; unsigned size; } fs_stat_t;

int fs_chdir (char *path);                      /* also switches volume: "1:/" */
int fs_getcwd(char *buf, unsigned size);        /* "2:/saves" -> its length */

int fs_stat  (char *path, fs_stat_t *out);
int fs_mkdir (char *path);
int fs_rmdir (char *path);                      /* must be empty           */
int fs_remove(char *path);                      /* files only              */
int fs_rename(char *from, char *to);            /* one volume; `to` must not
                                                   exist                   */

int fs_opendir (char *path);                    /* -> a handle             */
int fs_readdir (int dh, fs_stat_t *out);        /* 1 got one, 0 the end    */
int fs_closedir(int dh);

/* --- whole files ------------------------------------------------------ */
int   fs_load(char *path, void *buf, unsigned max);  /* -> bytes, or FS_E2BIG */
void *fs_load_alloc(char *path, unsigned *size);     /* malloc'd; NULL on error */
int   fs_save(char *path, void *buf, unsigned n);    /* create or replace       */

#endif
