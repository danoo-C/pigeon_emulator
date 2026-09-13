/* <pigeon/cd.h> -- the CD drive: removable, read-only discs.
 *
 * The design, with the reasoning for every choice here, is in
 * docs/cd-drive.md. The drive is on channel CH_CD, and a disc is put in
 * or taken out from OUTSIDE the machine -- the display front ends'
 * "Load from server", "Load from PC" and "Eject" buttons -- so a program
 * cannot assume the disc it started with is still the one in the drive.
 *
 *     cd_info_t disc;
 *     if (cd_info(CH_CD, &disc) == CD_OK) {
 *         fs_mount(CH_HDD);
 *         cd_save(CH_CD, NULL);            -- the whole disc, as <its name>
 *     }                                       on the current volume
 *
 * A disc is raw bytes. It MAY carry a PigeonFS image, and then it is also
 * a read-only volume: cd_has_fs() says so, and fs_mount(CH_CD) mounts it
 * with the ordinary <pigeon/fs.h>, where every write is FS_EROFS.
 *
 * THIS HEADER BRINGS fs.c WITH IT. cd.c includes <pigeon/fs.h> so that
 * cd_save() can be one call, and the launcher follows includes, so any
 * program that includes this header compiles the filesystem too -- about
 * 99 KB. That was chosen deliberately (docs/cd-drive.md, section 9).
 *
 * ERRORS HAVE THEIR OWN RANGE, -101 and down, so no CD_* code can ever be
 * mistaken for an FS_* one. cd_save() can fail either way -- the disc
 * went away, or the disk filled up -- and hands back whichever it was;
 * cd_strerror() names both kinds.
 *
 * A DISC CAN CHANGE UNDER YOU. Every insert and every eject moves a
 * generation counter. Sample cd_generation() before a long read and
 * again after; if it moved, the bytes came from two different discs.
 * cd_save() does exactly that, and answers CD_ECHANGED.
 */
#ifndef PIGEON_CD_H
#define PIGEON_CD_H

/* Defined here as well, because the example above passes NULL and a
 * program that includes only this header would otherwise not compile:
 * every unit is preprocessed on its own, so the definition that cd.c gets
 * from <pigeon/string.h> never reaches the program. Guarded the way
 * string.h guards it; the compiler accepts the identical repeat. */
#ifndef NULL
#define NULL ((void *)0)
#endif

#define CD_OK            0
#define CD_ENODISC    (-101)   /* the drive is empty                       */
#define CD_ENODEV     (-102)   /* no CD drive on that channel              */
#define CD_EINVAL     (-103)
#define CD_EIO        (-104)   /* the disc ended before it should have     */
#define CD_ENOFS      (-105)   /* the disc carries no PigeonFS             */
#define CD_ECHANGED   (-106)   /* the disc was swapped during the call     */

char *cd_strerror(int err);    /* CD_* and FS_* codes both                 */

#define CD_NAME_MAX 31

typedef struct {
    unsigned present;          /* 1 with a disc in, 0 without              */
    unsigned generation;       /* moves on every insert and every eject    */
    unsigned size;             /* bytes; 0 when empty                      */
    char     name[32];         /* the disc's file name -- never a path     */
} cd_info_t;

/* CD_OK with a disc in. CD_ENODISC with none -- and *out is still filled
 * in, so the generation can be watched while the drive is empty.
 * CD_ENODEV when the channel is not a CD drive; *out is then zeroed. */
int cd_info(unsigned channel, cd_info_t *out);

/* These two answer 1 or 0 and NEVER a negative code, the way key_down()
 * does. `if (cd_present(CH_CD))` has to be safe, and in C a negative
 * error is true. When you need to know WHY the answer is 0 -- no disc,
 * or no drive at all -- ask cd_info(). */
int cd_present(unsigned channel);
int cd_has_fs(unsigned channel);

unsigned cd_generation(unsigned channel);         /* 0 with no drive       */

/* Bytes read, short at the end of the disc and 0 past it. Any count
 * works: the library loops over the 4 KB IO window itself. */
int cd_read(unsigned channel, unsigned offset, void *buf, unsigned n);

/* The PigeonFS volume label, as strlcpy returns it: the length it TRIED
 * to copy, so a result >= size means it was cut. CD_ENOFS on a raw disc. */
int cd_label(unsigned channel, char *out, unsigned size);

/* The whole disc, as a file on the current volume. path NULL means the
 * disc's own name. Returns the bytes written, or a CD_* or FS_* code --
 * FS_EROFS, say, if the current volume is the disc itself. A copy that
 * fails partway, or whose disc was swapped while it ran, is removed
 * rather than left looking complete. */
int cd_save(unsigned channel, char *path);

#endif
