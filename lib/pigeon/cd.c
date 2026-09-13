/* The CD drive's guest library. See cd.h and docs/cd-drive.md.
 *
 * Everything here is two device commands. MEDIA (8) is the one a disk
 * cannot answer: its first word is a magic number, so it says whether a
 * channel is a CD drive at all as well as what is in it. READ (2) is a
 * disk's own READ -- the drive answers 0-5 exactly as hdd.py does, which
 * is also why fs_mount(CH_CD) works without this file.
 */
#include <pigeon/cd.h>
#include <pigeon/fs.h>
#include <pigeon/io.h>
#include <pigeon/mem.h>
#include <pigeon/string.h>

#define CD__READ      2u
#define CD__MEDIA     8u
#define CD__MEDIA_LEN 48u
#define CD__MAGIC     0x44434750u      /* "PGCD", emulator/devices/cd.py  */
#define CD__PGFS      0x53464750u      /* "PGFS", fs.c's FS__MAGIC        */
#define CD__LABEL_AT  36u              /* docs/filesystem.md section 3.1  */
#define CD__LABEL_LEN 16u

/* One READ moves at most the data window. Derived, not typed: 4096 is
 * what it is today, and display.h has a whole paragraph on what typed
 * copies of machine constants turn into. */
#define CD__WINDOW    (IO_SIZE - IO_USABLE_AFTER)

static unsigned __cd_io(unsigned channel, unsigned command, unsigned length,
                        unsigned address) {
    IO_RW = 0u;
    IO_CMD = command;
    IO_LEN = length;
    IO_ADDR = address;
    IO_CH = channel;                    /* last: this store fires it */
    return IO_RETLEN;
}

/* Ask the channel what it is holding: CD_OK with a disc in, CD_ENODISC
 * with none, CD_ENODEV when it is not a CD drive.
 *
 * The channels known not to be drives are refused BEFORE anything is
 * sent, for the reason fs.c's __fs_disk_blocks() gives: the same command
 * number means something else on another device, and a probe must never
 * start a timer. A disk answers command 8 with zeros and an empty channel
 * with 0xFFFFFFFF, so neither can pass for the magic.
 *
 * On CD_OK and CD_ENODISC the reply is left in the data window, and the
 * caller must copy what it needs before its next IO command of any kind. */
static int __cd_media(unsigned channel) {
    if (channel == 0u || channel == CH_HID || channel == CH_TIMER
            || channel == CH_DISPLAY) {
        return CD_ENODEV;
    }
    if (__cd_io(channel, CD__MEDIA, CD__MEDIA_LEN, 0u) != CD__MEDIA_LEN) return CD_ENODEV;
    if (IO_DATAW[0] != CD__MAGIC) return CD_ENODEV;
    return (IO_DATAW[1] != 0u) ? CD_OK : CD_ENODISC;
}

int cd_info(unsigned channel, cd_info_t *out) {
    unsigned i;
    int r;

    if (out == NULL) return CD_EINVAL;
    memset(out, 0, sizeof(cd_info_t));
    r = __cd_media(channel);
    if (r == CD_ENODEV) return r;
    out->present = IO_DATAW[1];
    out->generation = IO_DATAW[2];
    out->size = IO_DATAW[3];
    for (i = 0u; i < CD_NAME_MAX; i++) out->name[i] = (char)IO_DATA[16u + i];
    out->name[CD_NAME_MAX] = 0;
    return r;
}

int cd_present(unsigned channel) {
    return (__cd_media(channel) == CD_OK) ? 1 : 0;
}

unsigned cd_generation(unsigned channel) {
    if (__cd_media(channel) == CD_ENODEV) return 0u;
    return IO_DATAW[2];
}

int cd_read(unsigned channel, unsigned offset, void *buf, unsigned n) {
    unsigned char *out = (unsigned char *)buf;
    unsigned done = 0u;
    unsigned want;
    unsigned got;
    int r;

    if (n > 0x7FFFFFFFu) return CD_EINVAL;           /* the count must fit an int */
    if (buf == NULL && n != 0u) return CD_EINVAL;
    r = __cd_media(channel);
    if (r < 0) return r;

    while (done < n) {
        want = n - done;
        if (want > CD__WINDOW) want = CD__WINDOW;
        got = __cd_io(channel, CD__READ, want, offset + done);
        if (got > want) return CD_ENODEV;            /* 0xFFFFFFFF: the device went */
        memcpy(out + done, (void *)IO_DATA, got);
        done = done + got;
        if (got < want) break;                       /* the end of the disc */
    }
    return (int)done;
}

/* Four bytes at offset 0 against PigeonFS's magic. Not a mount: mounting
 * to find out would take a volume slot, make the disc the current volume
 * if it were the first one mounted, and then have to be undone -- all for
 * a question one read answers. */
int cd_has_fs(unsigned channel) {
    unsigned magic = 0u;
    if (cd_read(channel, 0u, &magic, 4u) != 4) return 0;
    return (magic == CD__PGFS) ? 1 : 0;
}

int cd_label(unsigned channel, char *out, unsigned size) {
    char label[CD__LABEL_LEN];
    int r;

    if (out == NULL || size == 0u) return CD_EINVAL;
    out[0] = 0;
    r = __cd_media(channel);
    if (r < 0) return r;
    if (!cd_has_fs(channel)) return CD_ENOFS;
    r = cd_read(channel, CD__LABEL_AT, label, CD__LABEL_LEN);
    if (r < 0) return r;
    if ((unsigned)r != CD__LABEL_LEN) return CD_EIO;
    label[CD__LABEL_LEN - 1u] = 0;
    return (int)strlcpy(out, label, size);
}

int cd_save(unsigned channel, char *path) {
    cd_info_t info;
    unsigned char buf[CD__WINDOW];
    unsigned offset = 0u;
    int fd;
    int n;
    int w;
    int r;

    r = cd_info(channel, &info);
    if (r < 0) return r;
    if (path == NULL) path = info.name;
    if (path[0] == 0) return CD_EINVAL;

    fd = fs_open(path, FS_WRITE | FS_CREATE | FS_TRUNC);
    if (fd < 0) return fd;                         /* an FS_* code, passed through */

    for (;;) {
        n = cd_read(channel, offset, buf, CD__WINDOW);
        if (n < 0) {
            fs_close(fd);
            fs_remove(path);
            return n;
        }
        if (n == 0) break;
        w = fs_write(fd, buf, (unsigned)n);
        if (w != n) {
            /* A short write is a full disk. The partial copy goes: a file
             * that is quietly shorter than the disc it came from looks
             * complete to everything that opens it later. */
            fs_close(fd);
            fs_remove(path);
            return (w < 0) ? w : FS_ENOSPC;
        }
        offset = offset + (unsigned)n;
        if ((unsigned)n < CD__WINDOW) break;
    }
    fs_close(fd);

    /* Checked once, at the end, and that is enough: the counter only ever
     * moves forward, so ANY swap during the copy leaves it different. */
    if (cd_generation(channel) != info.generation) {
        fs_remove(path);
        return CD_ECHANGED;
    }
    return (int)offset;
}

char *cd_strerror(int err) {
    if (err >= 0) return "ok";
    if (err == CD_ENODISC) return "no disc in the drive";
    if (err == CD_ENODEV) return "no CD drive there";
    if (err == CD_EINVAL) return "invalid argument";
    if (err == CD_EIO) return "the disc ended early";
    if (err == CD_ENOFS) return "the disc has no filesystem";
    if (err == CD_ECHANGED) return "the disc was changed";
    return fs_strerror(err);
}
