/* PigeonFS. See fs.h, and docs/filesystem.md for the design.
 *
 * The layers, bottom-up, in this order in the file:
 *
 *   block device   the only code that programs the IO header
 *   block cache    8 blocks from the heap, shared by every volume
 *   FAT            links, and next-fit allocation
 *   volumes        format, mount, the superblock
 *   paths          the current directory, normalisation, the walk
 *   entries        finding, adding and clearing directory entries
 *   files          the open-file table, read, write, seek
 *   the rest       directories, rename, lines, whole files
 *
 * tools/pfs.py implements the same format and is the oracle:
 * tests/test_fs.py checks that this library and pfs.py leave
 * BYTE-IDENTICAL images after the same operations. So wherever the two
 * could differ -- which block the allocator picks, which slot an entry
 * takes, what the tail of a last block holds -- they must not.
 *
 * Every private name starts with __fs_ or FS__. All units are compiled
 * into one translation unit with one global scope and one macro table,
 * so `static` hides nothing, and a user function called `lookup` would
 * otherwise be a "defined twice" error in here.
 *
 * All block arithmetic is shifts and masks (a block is 1 << 9 bytes).
 * DIV by zero raises in the emulator and takes the machine down, and
 * there is no divide in this file to guard.
 */
#include <pigeon/fs.h>
#include <pigeon/io.h>
#include <pigeon/mem.h>
#include <pigeon/string.h>

#define FS__BLOCK       512u
#define FS__MAGIC       0x53464750u     /* "PGFS" in byte order */
#define FS__FAT_START   1u
#define FS__FREE        0u
#define FS__EOC         0xFFFFFFFFu
#define FS__RESERVED    0xFFFFFFFEu
#define FS__ENTRY       64u
#define FS__ROOT_OFFSET 64u
#define FS__MIN_BLOCKS  16u
#define FS__NONE        0xFFFFFFFFu

#define FS__VOLUMES     4
#define FS__FILES       8
#define FS__SLOTS       8

/* HDD commands, the same on both disk channels */
#define FS__HDD_GET_SIZE 1u
#define FS__HDD_READ     2u
#define FS__HDD_WRITE    3u
#define FS__HDD_FLUSH    5u

/* What a dirty cache block holds -- which is also the order a flush
 * writes them in. Contents first, then the FAT that links them, then
 * the entries that point at them, then the superblock: a block becomes
 * reachable only after what is in it is on the disk (section 6.3). */
#define FS__CONTENT 0u
#define FS__FAT     1u
#define FS__DIR     2u
#define FS__SUPER   3u

#define FS__DIRHANDLE 0x100u            /* in __fs_file.flags */

/* __fs_scan modes */
#define FS__FIND  0u                    /* the entry with this name  */
#define FS__EMPTY 1u                    /* the first empty slot      */
#define FS__USED  2u                    /* any entry at all          */

#define FS__NO_SLOT ((struct __fs_slot *)0)
#define FS__NO_VOL  ((struct __fs_volume *)0)
#define FS__NO_FILE ((struct __fs_file *)0)

/* A directory entry, exactly as it is on the disk (section 3.3). */
struct __fs_dirent {
    char     name[32];
    unsigned type;
    unsigned first;
    unsigned size;
    unsigned mtime;
    unsigned reserved[4];
};

struct __fs_slot {
    unsigned channel;                   /* 0: the slot is empty */
    unsigned block;
    unsigned stamp;                     /* for least-recently-used */
    unsigned dirty;
    unsigned kind;                      /* FS__CONTENT .. FS__SUPER */
    unsigned char *data;
};

struct __fs_volume {
    unsigned used;
    unsigned channel;
    unsigned total;                     /* blocks on the disk */
    unsigned fat_blocks;
    unsigned data_start;
    unsigned free_blocks;               /* the superblock's hints, kept exact */
    unsigned next_free;
    unsigned hints_dirty;
    unsigned open;                      /* handles open on this volume */
};

struct __fs_file {
    unsigned used;
    unsigned flags;
    unsigned vol;                       /* index into __fs_vols */
    unsigned ent_block;                 /* where its directory entry lives */
    unsigned ent_offset;
    unsigned first;
    unsigned size;
    unsigned pos;                       /* bytes, or slots for a directory */
    unsigned cur_index;                 /* a logical block and where it is: */
    unsigned cur_block;                 /* sequential access walks no chain */
};

/* A resolved path: which volume, the normalised path (in a scratch
 * buffer), where its last '/' is, and where its parent's entry lives. */
struct __fs_where {
    unsigned vol;
    char    *path;
    unsigned length;
    unsigned split;
    unsigned pblock;
    unsigned poffset;
};

static struct __fs_slot   __fs_cache[FS__SLOTS];
static struct __fs_volume __fs_vols[FS__VOLUMES];
static struct __fs_file   __fs_files[FS__FILES];
static unsigned __fs_clock;
static unsigned __fs_ready;
static unsigned __fs_cwd_vol;           /* index + 1; 0 when nothing is mounted */
static char *__fs_cwd;                  /* "/a/b", normalised */
static char *__fs_path_a;               /* scratch for a normalised path */
static char *__fs_path_b;               /* a second one, for rename */

/* The cache is 4 KB and the paths another 768 bytes. They come from the
 * heap, not from static arrays: a static global is part of the program
 * image, and the BIOS copies every byte of that on every boot. */
static int __fs_init(void) {
    unsigned char *heap;
    unsigned i;
    if (__fs_ready) return FS_OK;
    if (sizeof(struct __fs_dirent) != FS__ENTRY) return FS_ECORRUPT;
    heap = (unsigned char *)malloc(FS__SLOTS * FS__BLOCK + 768u);
    if (heap == (unsigned char *)0) return FS_ENOMEM;
    for (i = 0u; i < FS__SLOTS; i++) {
        __fs_cache[i].data = heap + i * FS__BLOCK;
        __fs_cache[i].channel = 0u;
    }
    __fs_cwd = (char *)(heap + FS__SLOTS * FS__BLOCK);
    __fs_path_a = __fs_cwd + 256;
    __fs_path_b = __fs_path_a + 256;
    __fs_cwd[0] = 0;
    __fs_ready = 1u;
    return FS_OK;
}

/* --- the block device ---------------------------------------------------
 * The only code here that touches the IO header. Programming it is the
 * cheap part: moving 512 bytes through the window with memcpy is about
 * 4,350 instructions, and that is what the rest of this file tries to
 * do as rarely as possible. */

static unsigned __fs_io(unsigned channel, unsigned rw, unsigned command,
                        unsigned length, unsigned address) {
    IO_RW = rw;
    IO_CMD = command;
    IO_LEN = length;
    IO_ADDR = address;
    IO_CH = channel;                    /* last: this store fires it */
    return IO_RETLEN;
}

/* A read past the end of the image comes back short -- the BIOS loader
 * relies on that to find the end of a program -- so the rest is zeroed
 * here. Up to 8 blocks: one IO window. */
static void __fs_blk_read(unsigned channel, unsigned block, unsigned count,
                          unsigned char *buf) {
    unsigned want = count << 9;
    unsigned got = __fs_io(channel, 0u, FS__HDD_READ, want, block << 9);
    if (got > want) got = 0u;           /* 0xFFFFFFFF: no device, checked at mount */
    memcpy(buf, (void *)IO_DATA, got);
    if (got < want) memset(buf + got, 0, want - got);
}

/* IO_RW = 1 is what hands the window to the device. With 0 the
 * controller passes it a zeroed buffer, and the disk gets zeros. */
static void __fs_blk_write(unsigned channel, unsigned block, unsigned count,
                           unsigned char *buf) {
    memcpy((void *)IO_DATA, buf, count << 9);
    __fs_io(channel, 1u, FS__HDD_WRITE, count << 9, block << 9);
}

/* --- the block cache ----------------------------------------------------
 * Write-back within a call, write-through across calls: every public
 * function flushes before it returns, so nothing is dirty while the
 * program runs its own code, and stopping the emulator then loses
 * nothing. */

static struct __fs_slot *__fs_cached(unsigned channel, unsigned block) {
    unsigned i;
    struct __fs_slot *s;
    for (i = 0u; i < FS__SLOTS; i++) {
        s = &__fs_cache[i];
        if (s->channel == channel && s->block == block) return s;
    }
    return FS__NO_SLOT;
}

/* An empty slot if there is one, else the least recently used clean
 * one, else the least recently used of all. */
static struct __fs_slot *__fs_victim(void) {
    unsigned i;
    struct __fs_slot *s;
    struct __fs_slot *clean = FS__NO_SLOT;
    struct __fs_slot *any = FS__NO_SLOT;
    for (i = 0u; i < FS__SLOTS; i++) {
        s = &__fs_cache[i];
        if (s->channel == 0u) return s;
        if (!s->dirty && (clean == FS__NO_SLOT || s->stamp < clean->stamp)) clean = s;
        if (any == FS__NO_SLOT || s->stamp < any->stamp) any = s;
    }
    if (clean != FS__NO_SLOT) return clean;
    return any;
}

static void __fs_dirty(struct __fs_slot *s, unsigned kind) {
    if (!s->dirty || kind > s->kind) s->kind = kind;
    s->dirty = 1u;
}

static void __fs_put_hints(struct __fs_volume *v, struct __fs_slot *s) {
    unsigned *sb = (unsigned *)s->data;
    sb[7] = v->free_blocks;
    sb[8] = v->next_free;
    __fs_dirty(s, FS__SUPER);
    v->hints_dirty = 0u;
}

static struct __fs_slot *__fs_get(unsigned channel, unsigned block, unsigned load);

/* Write every dirty block, contents first and the superblock last. */
static void __fs_flush(void) {
    unsigned i;
    unsigned kind;
    struct __fs_slot *s;
    struct __fs_volume *v;

    /* Hints go into a superblock that is already cached, so it is
     * written once, in the last pass. */
    for (i = 0u; i < FS__VOLUMES; i++) {
        v = &__fs_vols[i];
        if (v->used && v->hints_dirty) {
            s = __fs_cached(v->channel, 0u);
            if (s != FS__NO_SLOT) __fs_put_hints(v, s);
        }
    }
    for (kind = FS__CONTENT; kind <= FS__SUPER; kind++) {
        for (i = 0u; i < FS__SLOTS; i++) {
            s = &__fs_cache[i];
            if (s->dirty && s->kind == kind) {
                __fs_blk_write(s->channel, s->block, 1u, s->data);
                s->dirty = 0u;
            }
        }
    }
    /* A superblock that was not cached. Every slot is clean now, so
     * fetching it cannot need another flush. */
    for (i = 0u; i < FS__VOLUMES; i++) {
        v = &__fs_vols[i];
        if (v->used && v->hints_dirty) {
            s = __fs_get(v->channel, 0u, 1u);
            __fs_put_hints(v, s);
            __fs_blk_write(s->channel, 0u, 1u, s->data);
            s->dirty = 0u;
        }
    }
}

/* The cached copy of a block, read from the disk if `load`. With load
 * 0 a block that was not cached comes back zeroed; one that was keeps
 * what it holds -- use __fs_fresh() for a block that must be zeros. */
static struct __fs_slot *__fs_get(unsigned channel, unsigned block, unsigned load) {
    struct __fs_slot *s = __fs_cached(channel, block);
    __fs_clock++;
    if (s != FS__NO_SLOT) {
        s->stamp = __fs_clock;
        return s;
    }
    s = __fs_victim();
    if (s->dirty) {
        /* Never write one block out of turn: flush them all, in order. */
        __fs_flush();
        s = __fs_victim();
    }
    s->channel = channel;
    s->block = block;
    s->dirty = 0u;
    s->stamp = __fs_clock;
    if (load) __fs_blk_read(channel, block, 1u, s->data);
    else memset(s->data, 0, FS__BLOCK);
    return s;
}

static struct __fs_slot *__fs_fresh(unsigned channel, unsigned block) {
    struct __fs_slot *s = __fs_get(channel, block, 0u);
    memset(s->data, 0, FS__BLOCK);
    return s;
}

static void __fs_drop(unsigned channel) {
    unsigned i;
    for (i = 0u; i < FS__SLOTS; i++) {
        if (__fs_cache[i].channel == channel) {
            __fs_cache[i].channel = 0u;
            __fs_cache[i].dirty = 0u;
        }
    }
}

/* Whole-block file IO goes straight between the window and the caller's
 * buffer. Before a direct read, a dirty cached copy must reach the disk
 * or the read would miss it; before a direct write, a cached copy must
 * go or a later flush would put the stale one back. */
static void __fs_bypass(unsigned channel, unsigned block, unsigned count,
                        unsigned writing) {
    unsigned i;
    struct __fs_slot *s;
    for (i = 0u; i < FS__SLOTS; i++) {
        s = &__fs_cache[i];
        if (s->channel == channel && s->block >= block && s->block < block + count) {
            if (writing) {
                s->channel = 0u;
                s->dirty = 0u;
            } else if (s->dirty) {
                __fs_blk_write(channel, s->block, 1u, s->data);
                s->dirty = 0u;
            }
        }
    }
}

/* --- the FAT ------------------------------------------------------------ */

static unsigned __fs_in_data(struct __fs_volume *v, unsigned block) {
    return block >= v->data_start && block < v->total;
}

static unsigned __fs_fat(struct __fs_volume *v, unsigned block) {
    struct __fs_slot *s = __fs_get(v->channel, FS__FAT_START + (block >> 7), 1u);
    unsigned *words = (unsigned *)s->data;
    return words[block & 127u];
}

static void __fs_fat_set(struct __fs_volume *v, unsigned block, unsigned value) {
    struct __fs_slot *s = __fs_get(v->channel, FS__FAT_START + (block >> 7), 1u);
    unsigned *words = (unsigned *)s->data;
    words[block & 127u] = value;
    __fs_dirty(s, FS__FAT);
}

/* The next block of a chain: FS__EOC at its end, 0 if the link is
 * broken -- free, reserved, or outside the data region. */
static unsigned __fs_next(struct __fs_volume *v, unsigned block) {
    unsigned next = __fs_fat(v, block);
    if (next == FS__EOC || __fs_in_data(v, next)) return next;
    return 0u;
}

/* Logical block `index` of the chain starting at `first`, or 0. */
static unsigned __fs_nth(struct __fs_volume *v, unsigned first, unsigned index) {
    unsigned block = first;
    while (index > 0u) {
        block = __fs_next(v, block);
        if (block == 0u || block == FS__EOC) return 0u;
        index--;
    }
    return block;
}

/* One block, marked end-of-chain; 0 when the disk is full. Next-fit
 * from the next_free hint, exactly as tools/pfs.py searches, so both
 * leave files in the same places -- and mostly contiguous, which is what
 * lets whole-block IO move several at once. */
static unsigned __fs_alloc(struct __fs_volume *v) {
    unsigned span = v->total - v->data_start;
    unsigned block = v->next_free;
    unsigned seen = 0u;
    struct __fs_slot *s;
    unsigned *words;

    if (!__fs_in_data(v, block)) block = v->data_start;
    while (seen < span) {
        s = __fs_get(v->channel, FS__FAT_START + (block >> 7), 1u);
        words = (unsigned *)s->data;
        /* the rest of this FAT block, without going back to the cache */
        while (seen < span) {
            if (words[block & 127u] == FS__FREE) {
                words[block & 127u] = FS__EOC;
                __fs_dirty(s, FS__FAT);
                v->free_blocks--;
                v->next_free = (block + 1u == v->total) ? v->data_start : block + 1u;
                v->hints_dirty = 1u;
                return block;
            }
            block++;
            seen++;
            if (block == v->total) {
                block = v->data_start;
                break;
            }
            if ((block & 127u) == 0u) break;
        }
    }
    return 0u;
}

static int __fs_free_chain(struct __fs_volume *v, unsigned block) {
    unsigned next;
    unsigned count = 0u;
    while (block != FS__EOC) {
        if (!__fs_in_data(v, block) || count >= v->total) return FS_ECORRUPT;
        next = __fs_fat(v, block);
        __fs_fat_set(v, block, FS__FREE);
        v->free_blocks++;
        v->hints_dirty = 1u;
        count++;
        block = next;
    }
    return FS_OK;
}

/* --- volumes ------------------------------------------------------------ */

static struct __fs_volume *__fs_volume_of(unsigned channel) {
    unsigned i;
    for (i = 0u; i < FS__VOLUMES; i++) {
        if (__fs_vols[i].used && __fs_vols[i].channel == channel) return &__fs_vols[i];
    }
    return FS__NO_VOL;
}

/* The disk's size in blocks, or 0 when the channel holds no disk.
 *
 * GET_SIZE is command 1 on a disk -- and mouse position on HID, INFO on
 * the display, and START on the timer. So the channels known not to be
 * disks are refused before anything is sent: a probe would start a
 * timer. Anything else must answer with exactly the 8 bytes a disk does;
 * an empty channel answers 0xFFFFFFFF. */
static unsigned __fs_disk_blocks(unsigned channel) {
    if (channel == 0u || channel == CH_HID || channel == CH_TIMER
            || channel == CH_DISPLAY) return 0u;
    if (__fs_io(channel, 0u, FS__HDD_GET_SIZE, 8u, 0u) != 8u) return 0u;
    if (IO_DATAW[1] != 0u || IO_DATAW[0] > 0xFFFFFE00u) {
        return 0x7FFFFFu;               /* all a 32-bit IO_ADDRESS reaches */
    }
    return IO_DATAW[0] >> 9;
}

int fs_format(unsigned channel, char *label, unsigned flags) {
    unsigned total;
    unsigned fat_blocks;
    unsigned data_start;
    unsigned i;
    unsigned j;
    unsigned block;
    unsigned *words;
    struct __fs_slot *s;
    int r = __fs_init();

    if (r < 0) return r;
    if (label == (char *)0) label = "";
    if (strlen(label) > 15u) return FS_EINVAL;
    total = __fs_disk_blocks(channel);
    if (total == 0u) return FS_ENODEV;
    if (__fs_volume_of(channel) != FS__NO_VOL) return FS_EBUSY;
    if (total < FS__MIN_BLOCKS) return FS_EINVAL;
    __fs_drop(channel);

    /* A disk lives until it is deliberately reformatted: without the
     * force flag, only a disk whose first block is all zeros. That also
     * refuses a program image, which is what channel 1 holds. */
    if ((flags & FS_FORMAT_FORCE) == 0u) {
        s = __fs_get(channel, 0u, 1u);
        words = (unsigned *)s->data;
        for (i = 0u; i < 128u; i++) {
            if (words[i] != 0u) return FS_ENOTBLANK;
        }
    }

    fat_blocks = (total + 127u) >> 7;
    data_start = FS__FAT_START + fat_blocks;

    /* The FAT and the root first and the superblock last: a format cut
     * short leaves no magic, so the disk is refused, not trusted. */
    for (i = 0u; i < fat_blocks; i++) {
        s = __fs_fresh(channel, FS__FAT_START + i);
        words = (unsigned *)s->data;
        for (j = 0u; j < 128u; j++) {
            block = (i << 7) + j;
            if (block < data_start) words[j] = FS__RESERVED;
            else if (block == data_start) words[j] = FS__EOC;   /* the root */
        }
        __fs_dirty(s, FS__FAT);
    }
    s = __fs_fresh(channel, data_start);                        /* 8 empty slots */
    __fs_dirty(s, FS__CONTENT);

    s = __fs_fresh(channel, 0u);
    words = (unsigned *)s->data;
    words[0] = FS__MAGIC;
    words[1] = 1u;                      /* version */
    words[2] = FS__BLOCK;
    words[3] = total;
    words[4] = FS__FAT_START;
    words[5] = fat_blocks;
    words[6] = data_start;
    words[7] = total - data_start - 1u; /* free: everything but the root */
    words[8] = data_start + 1u;         /* next free: just past it */
    strlcpy((char *)s->data + 36, label, 16u);
    words[24] = FS_TYPE_DIR;            /* the root's entry, at byte 64 */
    words[25] = data_start;
    words[26] = FS__BLOCK;
    __fs_dirty(s, FS__SUPER);
    __fs_flush();
    return FS_OK;
}

int fs_mount(unsigned channel) {
    struct __fs_volume *v = FS__NO_VOL;
    struct __fs_slot *s;
    unsigned *w;
    unsigned blocks;
    unsigned i;
    unsigned index = 0u;
    int r = __fs_init();

    if (r < 0) return r;
    if (__fs_volume_of(channel) != FS__NO_VOL) return FS_OK;
    blocks = __fs_disk_blocks(channel);
    if (blocks == 0u) return FS_ENODEV;
    for (i = 0u; i < FS__VOLUMES; i++) {
        if (!__fs_vols[i].used) {
            v = &__fs_vols[i];
            index = i;
            break;
        }
    }
    if (v == FS__NO_VOL) return FS_EMFILE;

    __fs_drop(channel);
    s = __fs_get(channel, 0u, 1u);
    w = (unsigned *)s->data;
    if (w[0] != FS__MAGIC) return FS_ENOFS;
    if (w[1] != 1u || w[2] != FS__BLOCK || w[3] < FS__MIN_BLOCKS || w[3] > blocks
            || w[4] != FS__FAT_START || w[5] != ((w[3] + 127u) >> 7)
            || w[6] != FS__FAT_START + w[5]
            || w[24] != FS_TYPE_DIR || w[25] < w[6] || w[25] >= w[3]
            || w[26] == 0u || (w[26] & 511u) != 0u) {
        return FS_ECORRUPT;
    }

    v->used = 1u;
    v->channel = channel;
    v->total = w[3];
    v->fat_blocks = w[5];
    v->data_start = w[6];
    v->free_blocks = w[7];
    v->next_free = w[8];
    v->hints_dirty = 0u;
    v->open = 0u;
    if (__fs_cwd_vol == 0u) {
        __fs_cwd_vol = index + 1u;
        strcpy(__fs_cwd, "/");
    }
    return FS_OK;
}

int fs_unmount(unsigned channel) {
    unsigned i;
    unsigned j;
    struct __fs_volume *v;
    for (i = 0u; i < FS__VOLUMES; i++) {
        v = &__fs_vols[i];
        if (v->used && v->channel == channel) {
            if (v->open != 0u) return FS_EBUSY;
            __fs_flush();
            __fs_drop(channel);
            v->used = 0u;
            if (__fs_cwd_vol == i + 1u) {
                /* The current directory was on it: move to another
                 * volume's root, or to nowhere. */
                __fs_cwd_vol = 0u;
                for (j = 0u; j < FS__VOLUMES; j++) {
                    if (__fs_vols[j].used) {
                        __fs_cwd_vol = j + 1u;
                        strcpy(__fs_cwd, "/");
                        break;
                    }
                }
            }
            return FS_OK;
        }
    }
    return FS_ENODEV;
}

int fs_sync(unsigned channel) {
    if (__fs_volume_of(channel) == FS__NO_VOL) return FS_ENODEV;
    __fs_flush();
    __fs_io(channel, 0u, FS__HDD_FLUSH, 0u, 0u);
    return FS_OK;
}

int fs_statvfs(unsigned channel, fs_volinfo *out) {
    struct __fs_volume *v = __fs_volume_of(channel);
    struct __fs_slot *s;
    if (v == FS__NO_VOL) return FS_ENODEV;
    s = __fs_get(channel, 0u, 1u);
    out->total_blocks = v->total;
    out->free_blocks = v->free_blocks;
    out->block_size = FS__BLOCK;
    strlcpy(out->label, (char *)s->data + 36, 16u);
    return FS_OK;
}

/* --- paths ---------------------------------------------------------------
 * Every path is made absolute and normalised before anything looks at
 * it: "2:/a//b/../c" becomes volume 2 and "/a/c". That is correct only
 * because a directory has exactly one parent -- no hard links to
 * directories, no symlinks -- and it is what makes every later check a
 * plain string compare that "/a//b" cannot fool. */

/* Normalise `path` into `out`. Returns the volume's index, or an error. */
static int __fs_resolve(char *path, char *out) {
    unsigned digits = 0u;
    unsigned vol;
    unsigned len;
    unsigned n;
    unsigned i;
    char *seg;
    char *p = path;

    if (!__fs_ready) return FS_ENODEV;
    while (isdigit(p[digits])) digits++;
    if (digits > 0u && p[digits] == ':') {
        /* "2:..." -- always from the root of that volume */
        vol = FS__VOLUMES;
        n = strtou(p, (char **)0, 10u);
        for (i = 0u; i < FS__VOLUMES; i++) {
            if (__fs_vols[i].used && __fs_vols[i].channel == n) vol = i;
        }
        if (vol == FS__VOLUMES) return FS_ENODEV;
        p = p + digits + 1u;
        strcpy(out, "/");
    } else {
        if (__fs_cwd_vol == 0u) return FS_ENODEV;
        vol = __fs_cwd_vol - 1u;
        if (*p == '/') strcpy(out, "/");
        else strcpy(out, __fs_cwd);
    }

    len = strlen(out);
    while (*p != 0) {
        while (*p == '/') p++;
        if (*p == 0) break;
        seg = p;
        n = 0u;
        while (p[n] != 0 && p[n] != '/') n++;
        p = p + n;
        if (n == 1u && seg[0] == '.') continue;
        if (n == 2u && seg[0] == '.' && seg[1] == '.') {
            /* back to the previous '/'; at the root, stay there */
            while (len > 1u && out[len - 1u] != '/') len--;
            if (len > 1u) len--;
            out[len] = 0;
            continue;
        }
        if (n > FS_NAME_MAX) return FS_ENAMETOOLONG;
        if (len + (len > 1u) + n > FS_PATH_MAX) return FS_ENAMETOOLONG;
        if (len > 1u) {
            out[len] = '/';
            len++;
        }
        memcpy(out + len, seg, n);
        len = len + n;
        out[len] = 0;
    }
    return (int)vol;
}

/* --- directory entries --------------------------------------------------- */

static void __fs_read_entry(struct __fs_volume *v, unsigned block, unsigned offset,
                            struct __fs_dirent *e) {
    struct __fs_slot *s = __fs_get(v->channel, block, 1u);
    memcpy(e, s->data + offset, FS__ENTRY);
}

/* The root's entry lives in the superblock, at (0, 64), so this is also
 * how the root's size is updated -- one code path for every directory. */
static void __fs_write_entry(struct __fs_volume *v, unsigned block, unsigned offset,
                             struct __fs_dirent *e) {
    struct __fs_slot *s = __fs_get(v->channel, block, 1u);
    memcpy(s->data + offset, e, FS__ENTRY);
    __fs_dirty(s, block == 0u ? FS__SUPER : FS__DIR);
}

static void __fs_clear_entry(struct __fs_volume *v, unsigned block, unsigned offset) {
    struct __fs_slot *s = __fs_get(v->channel, block, 1u);
    memset(s->data + offset, 0, FS__ENTRY);
    __fs_dirty(s, FS__DIR);
}

static void __fs_new_entry(struct __fs_dirent *e, char *name, unsigned n,
                           unsigned type, unsigned first, unsigned size) {
    memset(e, 0, FS__ENTRY);
    memcpy(e->name, name, n);
    e->type = type;
    e->first = first;
    e->size = size;
}

static int __fs_check_name(char *name, unsigned n) {
    unsigned i;
    if (n == 0u) return FS_EINVAL;
    if (n > FS_NAME_MAX) return FS_ENAMETOOLONG;
    for (i = 0u; i < n; i++) {
        if (name[i] == ':') return FS_EINVAL;
    }
    return FS_OK;
}

/* Look through a directory, 8 entries a block: for the entry named
 * `name` (FS__FIND), the first empty slot (FS__EMPTY), or any entry at
 * all (FS__USED). FS_OK with the entry and where it lives, FS_ENOENT if
 * there is none. `dir` may be the same struct as `out`. */
static int __fs_scan(struct __fs_volume *v, struct __fs_dirent *dir, unsigned mode,
                     char *name, unsigned n, struct __fs_dirent *out,
                     unsigned *block_out, unsigned *offset_out) {
    unsigned block = dir->first;
    unsigned blocks = dir->size >> 9;
    unsigned i;
    unsigned slot;
    struct __fs_slot *s;
    struct __fs_dirent *d;
    unsigned hit;

    for (i = 0u; i < blocks; i++) {
        if (!__fs_in_data(v, block)) return FS_ECORRUPT;
        s = __fs_get(v->channel, block, 1u);
        for (slot = 0u; slot < 8u; slot++) {
            d = (struct __fs_dirent *)(s->data + (slot << 6));
            if (mode == FS__EMPTY) hit = d->type == 0u;
            else if (mode == FS__USED) hit = d->type != 0u;
            else hit = d->type != 0u && n < 32u && memcmp(d->name, name, n) == 0
                       && d->name[n] == 0;
            if (hit) {
                memcpy(out, d, FS__ENTRY);
                *block_out = block;
                *offset_out = slot << 6;
                return FS_OK;
            }
        }
        block = __fs_next(v, block);
    }
    return FS_ENOENT;
}

static int __fs_find(struct __fs_volume *v, struct __fs_dirent *dir, char *name,
                     unsigned n, struct __fs_dirent *out, unsigned *block, unsigned *offset) {
    return __fs_scan(v, dir, FS__FIND, name, n, out, block, offset);
}

static unsigned __fs_has_empty(struct __fs_volume *v, struct __fs_dirent *dir) {
    struct __fs_dirent scratch;
    unsigned b;
    unsigned o;
    return __fs_scan(v, dir, FS__EMPTY, (char *)0, 0u, &scratch, &b, &o) == FS_OK;
}

/* Walk the first `length` bytes of a normalised path from the root. */
static int __fs_walk(struct __fs_volume *v, char *path, unsigned length,
                     struct __fs_dirent *e, unsigned *block, unsigned *offset) {
    unsigned i = 1u;
    unsigned n;
    int r;
    __fs_read_entry(v, 0u, FS__ROOT_OFFSET, e);
    *block = 0u;
    *offset = FS__ROOT_OFFSET;
    while (i < length) {
        n = 0u;
        while (i + n < length && path[i + n] != '/') n++;
        if (e->type != FS_TYPE_DIR) return FS_ENOTDIR;
        r = __fs_find(v, e, path + i, n, e, block, offset);
        if (r < 0) return r;
        i = i + n + 1u;
    }
    return FS_OK;
}

/* Resolve `path` into `buf` and find its parent directory. The name is
 * what follows the last '/', and is empty for the root. */
static int __fs_locate(char *path, char *buf, struct __fs_where *w,
                       struct __fs_dirent *parent) {
    unsigned pb;
    unsigned po;
    int r = __fs_resolve(path, buf);
    if (r < 0) return r;
    w->vol = (unsigned)r;
    w->path = buf;
    w->length = strlen(buf);
    w->split = w->length - 1u;
    while (buf[w->split] != '/') w->split--;
    r = __fs_walk(&__fs_vols[w->vol], buf, w->split == 0u ? 1u : w->split,
                  parent, &pb, &po);
    if (r < 0) return r;
    if (parent->type != FS_TYPE_DIR) return FS_ENOTDIR;
    w->pblock = pb;
    w->poffset = po;
    return FS_OK;
}

static char *__fs_name(struct __fs_where *w) { return w->path + w->split + 1u; }
static unsigned __fs_name_len(struct __fs_where *w) { return w->length - w->split - 1u; }

/* The entry a located path names: the root itself when the name is empty. */
static int __fs_target(struct __fs_where *w, struct __fs_dirent *parent,
                       struct __fs_dirent *e, unsigned *block, unsigned *offset) {
    if (__fs_name_len(w) == 0u) {
        memcpy(e, parent, FS__ENTRY);
        *block = 0u;
        *offset = FS__ROOT_OFFSET;
        return FS_OK;
    }
    return __fs_find(&__fs_vols[w->vol], parent, __fs_name(w), __fs_name_len(w),
                     e, block, offset);
}

/* Add `e` to a directory whose own entry is `dir`, at (dblock, doffset).
 * A full directory grows by one zeroed block; the caller has checked
 * there is one to spare. */
static int __fs_add_entry(struct __fs_volume *v, struct __fs_dirent *dir,
                          unsigned dblock, unsigned doffset, struct __fs_dirent *e,
                          unsigned *block_out, unsigned *offset_out) {
    struct __fs_dirent scratch;
    struct __fs_slot *s;
    unsigned block;
    unsigned offset;
    unsigned last;

    if (__fs_scan(v, dir, FS__EMPTY, (char *)0, 0u, &scratch, &block, &offset) != FS_OK) {
        last = __fs_nth(v, dir->first, (dir->size >> 9) - 1u);
        if (last == 0u) return FS_ECORRUPT;
        block = __fs_alloc(v);
        if (block == 0u) return FS_ENOSPC;
        s = __fs_fresh(v->channel, block);         /* every slot must read as empty */
        __fs_dirty(s, FS__CONTENT);
        __fs_fat_set(v, last, block);
        __fs_flush();                              /* its zeros and its link first */
        dir->size = dir->size + FS__BLOCK;
        __fs_write_entry(v, dblock, doffset, dir);
        offset = 0u;
    }
    __fs_write_entry(v, block, offset, e);
    *block_out = block;
    *offset_out = offset;
    return FS_OK;
}

/* --- handles --------------------------------------------------------------- */

static int __fs_free_fd(void) {
    int i;
    for (i = 0; i < FS__FILES; i++) {
        if (!__fs_files[i].used) return i;
    }
    return -1;
}

static struct __fs_file *__fs_handle(int fd, unsigned need, unsigned dir) {
    struct __fs_file *f;
    if (fd < 0 || fd >= FS__FILES) return FS__NO_FILE;
    f = &__fs_files[fd];
    if (!f->used) return FS__NO_FILE;
    if (((f->flags & FS__DIRHANDLE) != 0u) != (dir != 0u)) return FS__NO_FILE;
    if ((f->flags & need) != need) return FS__NO_FILE;
    return f;
}

static unsigned __fs_is_open(unsigned vol, unsigned block, unsigned offset) {
    unsigned i;
    struct __fs_file *f;
    for (i = 0u; i < FS__FILES; i++) {
        f = &__fs_files[i];
        if (f->used && f->vol == vol && f->ent_block == block && f->ent_offset == offset) {
            return 1u;
        }
    }
    return 0u;
}

/* Is this path, on this volume, the current directory or above it? */
static unsigned __fs_holds_cwd(unsigned vol, char *path, unsigned length) {
    if (__fs_cwd_vol != vol + 1u) return 0u;
    if (length == 1u) return 1u;
    if (strncmp(__fs_cwd, path, length) != 0) return 0u;
    return __fs_cwd[length] == 0 || __fs_cwd[length] == '/';
}

static void __fs_open_handle(struct __fs_file *f, unsigned flags, unsigned vol,
                             unsigned block, unsigned offset, struct __fs_dirent *e) {
    f->used = 1u;
    f->flags = flags;
    f->vol = vol;
    f->ent_block = block;
    f->ent_offset = offset;
    f->first = e->first;
    f->size = e->size;
    f->pos = (flags & FS_APPEND) != 0u ? e->size : 0u;
    f->cur_index = FS__NONE;
    f->cur_block = 0u;
    __fs_vols[vol].open++;
}

static void __fs_release(struct __fs_file *f) {
    __fs_vols[f->vol].open--;
    f->used = 0u;
}

/* The block holding logical block `index` of an open file, walking on
 * from where the last access left off when it can -- so sequential IO
 * costs one FAT lookup per block, not a walk from the start. */
static unsigned __fs_file_block(struct __fs_volume *v, struct __fs_file *f, unsigned index) {
    unsigned at;
    unsigned block;
    if (f->cur_index != FS__NONE && index >= f->cur_index) {
        at = f->cur_index;
        block = f->cur_block;
    } else {
        at = 0u;
        block = f->first;
    }
    if (block == 0u) return 0u;
    while (at < index) {
        block = __fs_next(v, block);
        if (block == 0u || block == FS__EOC) return 0u;
        at++;
    }
    f->cur_index = index;
    f->cur_block = block;
    return block;
}

/* The block for logical `index` while writing: an existing one, or a new
 * one linked on when index is just past the end of the chain. 0 when the
 * disk is full. */
static unsigned __fs_write_block(struct __fs_volume *v, struct __fs_file *f,
                                 unsigned index, unsigned *chain) {
    unsigned block;
    unsigned last;
    if (index < *chain) return __fs_file_block(v, f, index);
    block = __fs_alloc(v);
    if (block == 0u) return 0u;
    if (*chain == 0u) {
        f->first = block;
    } else {
        last = __fs_file_block(v, f, *chain - 1u);
        if (last == 0u) return 0u;
        __fs_fat_set(v, last, block);
    }
    *chain = *chain + 1u;
    f->cur_index = index;
    f->cur_block = block;
    return block;
}

/* --- files ----------------------------------------------------------------- */

int fs_open(char *path, unsigned flags) {
    struct __fs_where w;
    struct __fs_dirent parent;
    struct __fs_dirent e;
    struct __fs_volume *v;
    unsigned b;
    unsigned o;
    unsigned old;
    int fd;
    int r;

    if ((flags & (FS_READ | FS_WRITE)) == 0u) return FS_EINVAL;
    if ((flags & (FS_CREATE | FS_TRUNC | FS_APPEND)) != 0u && (flags & FS_WRITE) == 0u) {
        return FS_EINVAL;
    }
    fd = __fs_free_fd();
    if (fd < 0) return FS_EMFILE;
    r = __fs_locate(path, __fs_path_a, &w, &parent);
    if (r < 0) return r;
    v = &__fs_vols[w.vol];
    if (__fs_name_len(&w) == 0u) return FS_EISDIR;

    r = __fs_find(v, &parent, __fs_name(&w), __fs_name_len(&w), &e, &b, &o);
    if (r == FS_ENOENT) {
        if ((flags & FS_CREATE) == 0u) return FS_ENOENT;
        r = __fs_check_name(__fs_name(&w), __fs_name_len(&w));
        if (r < 0) return r;
        if (!__fs_has_empty(v, &parent) && v->free_blocks < 1u) return FS_ENOSPC;
        __fs_new_entry(&e, __fs_name(&w), __fs_name_len(&w), FS_TYPE_FILE, 0u, 0u);
        r = __fs_add_entry(v, &parent, w.pblock, w.poffset, &e, &b, &o);
        if (r < 0) {
            __fs_flush();
            return r;
        }
    } else if (r < 0) {
        return r;
    } else {
        if (e.type == FS_TYPE_DIR) return FS_EISDIR;
        if ((flags & FS_WRITE) != 0u && __fs_is_open(w.vol, b, o)) return FS_EBUSY;
        if ((flags & FS_TRUNC) != 0u && (e.first != 0u || e.size != 0u)) {
            old = e.first;
            e.first = 0u;
            e.size = 0u;
            __fs_write_entry(v, b, o, &e);
            __fs_flush();                          /* unreachable first... */
            if (old != 0u) {
                r = __fs_free_chain(v, old);       /* ...then freed */
                __fs_flush();
                if (r < 0) return r;
            }
        }
    }
    __fs_open_handle(&__fs_files[fd], flags, w.vol, b, o, &e);
    __fs_flush();
    return fd;
}

int fs_close(int fd) {
    struct __fs_file *f = __fs_handle(fd, 0u, 0u);
    if (f == FS__NO_FILE) return FS_EBADF;
    __fs_release(f);
    return FS_OK;
}

int fs_read(int fd, void *buf, unsigned n) {
    struct __fs_file *f = __fs_handle(fd, FS_READ, 0u);
    struct __fs_volume *v;
    struct __fs_slot *s;
    unsigned char *out = (unsigned char *)buf;
    unsigned done = 0u;
    unsigned index;
    unsigned offset;
    unsigned chunk;
    unsigned block;
    unsigned run;

    if (f == FS__NO_FILE) return FS_EBADF;
    v = &__fs_vols[f->vol];
    if (f->pos >= f->size) return 0;
    if (n > f->size - f->pos) n = f->size - f->pos;

    while (done < n) {
        index = f->pos >> 9;
        offset = f->pos & 511u;
        block = __fs_file_block(v, f, index);
        if (block == 0u) return done > 0u ? (int)done : FS_ECORRUPT;
        if (offset == 0u && n - done >= FS__BLOCK) {
            /* Whole blocks go straight from the window into the caller's
             * buffer -- one copy, not two -- as many contiguous ones as
             * fit in the window. */
            run = 1u;
            while (run < 8u && run < ((n - done) >> 9)
                   && __fs_fat(v, block + run - 1u) == block + run) {
                run++;
            }
            __fs_bypass(v->channel, block, run, 0u);
            __fs_blk_read(v->channel, block, run, out + done);
            f->cur_index = index + run - 1u;
            f->cur_block = block + run - 1u;
            chunk = run << 9;
        } else {
            s = __fs_get(v->channel, block, 1u);
            chunk = FS__BLOCK - offset;
            if (chunk > n - done) chunk = n - done;
            memcpy(out + done, s->data + offset, chunk);
        }
        done = done + chunk;
        f->pos = f->pos + chunk;
    }
    return (int)done;
}

int fs_write(int fd, void *buf, unsigned n) {
    struct __fs_file *f = __fs_handle(fd, FS_WRITE, 0u);
    struct __fs_volume *v;
    struct __fs_slot *s;
    struct __fs_dirent e;
    unsigned char *in = (unsigned char *)buf;
    unsigned done = 0u;
    unsigned index;
    unsigned offset;
    unsigned chunk;
    unsigned block;
    unsigned run;
    unsigned chain;
    unsigned old_first;
    unsigned old_size;
    unsigned full = 0u;

    if (f == FS__NO_FILE) return FS_EBADF;
    v = &__fs_vols[f->vol];
    if ((f->flags & FS_APPEND) != 0u) f->pos = f->size;
    old_first = f->first;
    old_size = f->size;
    chain = (f->size + 511u) >> 9;

    while (done < n) {
        index = f->pos >> 9;
        offset = f->pos & 511u;
        block = __fs_write_block(v, f, index, &chain);
        if (block == 0u) {
            full = 1u;
            break;
        }
        if (offset == 0u && n - done >= FS__BLOCK) {
            /* A run of whole blocks, straight from the caller's buffer.
             * A new block that is not contiguous ends the run; it is
             * already linked on, and the next pass starts with it. */
            run = 1u;
            while (run < 8u && run < ((n - done) >> 9)) {
                if (__fs_write_block(v, f, index + run, &chain) != block + run) break;
                run++;
            }
            __fs_bypass(v->channel, block, run, 1u);
            __fs_blk_write(v->channel, block, run, in + done);
            f->cur_index = index + run - 1u;
            f->cur_block = block + run - 1u;
            chunk = run << 9;
        } else {
            chunk = FS__BLOCK - offset;
            if (chunk > n - done) chunk = n - done;
            /* A block past the end holds nothing worth reading, so it
             * starts as zeros -- which is also what pfs.py puts after the
             * last byte of a file. */
            if (index >= ((f->size + 511u) >> 9)) s = __fs_fresh(v->channel, block);
            else s = __fs_get(v->channel, block, 1u);
            memcpy(s->data + offset, in + done, chunk);
            __fs_dirty(s, FS__CONTENT);
        }
        done = done + chunk;
        f->pos = f->pos + chunk;
        if (f->pos > f->size) f->size = f->pos;
    }

    /* The size is the commit: written after the data and the FAT, so a
     * crash leaves the file as it was, plus blocks fsck can reclaim. */
    if (f->first != old_first || f->size != old_size) {
        __fs_read_entry(v, f->ent_block, f->ent_offset, &e);
        e.first = f->first;
        e.size = f->size;
        __fs_write_entry(v, f->ent_block, f->ent_offset, &e);
    }
    __fs_flush();
    if (full && done == 0u) return FS_ENOSPC;
    return (int)done;
}

int fs_seek(int fd, int offset, int whence) {
    struct __fs_file *f = __fs_handle(fd, 0u, 0u);
    int base;
    int target;
    if (f == FS__NO_FILE) return FS_EBADF;
    if (whence == FS_SEEK_SET) base = 0;
    else if (whence == FS_SEEK_CUR) base = (int)f->pos;
    else if (whence == FS_SEEK_END) base = (int)f->size;
    else return FS_EINVAL;
    target = base + offset;
    /* No sparse files: the FAT has no way to record a hole. */
    if (target < 0 || target > (int)f->size) return FS_EINVAL;
    f->pos = (unsigned)target;
    return target;
}

int fs_tell(int fd) {
    struct __fs_file *f = __fs_handle(fd, 0u, 0u);
    if (f == FS__NO_FILE) return FS_EBADF;
    return (int)f->pos;
}

/* --- lines ------------------------------------------------------------------- */

int fs_gets(int fd, char *buf, unsigned size) {
    struct __fs_file *f = __fs_handle(fd, FS_READ, 0u);
    struct __fs_volume *v;
    struct __fs_slot *s;
    unsigned count = 0u;
    unsigned block;
    unsigned offset;
    unsigned char c;

    if (f == FS__NO_FILE) return FS_EBADF;
    if (size < 2u) return FS_EINVAL;
    v = &__fs_vols[f->vol];
    while (count < size - 1u && f->pos < f->size) {
        block = __fs_file_block(v, f, f->pos >> 9);
        if (block == 0u) break;
        s = __fs_get(v->channel, block, 1u);
        offset = f->pos & 511u;
        /* the rest of this block, without a cache lookup per byte */
        while (offset < FS__BLOCK && count < size - 1u && f->pos < f->size) {
            c = s->data[offset];
            buf[count] = (char)c;
            count++;
            offset++;
            f->pos++;
            if (c == '\n') {
                buf[count] = 0;
                return (int)count;
            }
        }
    }
    buf[count] = 0;
    return (int)count;
}

int fs_puts(int fd, char *s) {
    return fs_write(fd, s, strlen(s));
}

/* --- the namespace ------------------------------------------------------------ */

int fs_chdir(char *path) {
    struct __fs_where w;
    struct __fs_dirent parent;
    struct __fs_dirent e;
    unsigned b;
    unsigned o;
    int r = __fs_locate(path, __fs_path_a, &w, &parent);
    if (r < 0) return r;
    r = __fs_target(&w, &parent, &e, &b, &o);
    if (r < 0) return r;
    if (e.type != FS_TYPE_DIR) return FS_ENOTDIR;
    __fs_cwd_vol = w.vol + 1u;
    strcpy(__fs_cwd, w.path);
    return FS_OK;
}

int fs_getcwd(char *buf, unsigned size) {
    char number[STR_UTOA_MAX];
    unsigned n;
    unsigned length;
    if (__fs_cwd_vol == 0u) return FS_ENODEV;
    n = (unsigned)utoa(__fs_vols[__fs_cwd_vol - 1u].channel, number, 10u);
    length = n + 1u + strlen(__fs_cwd);
    if (length + 1u > size) return FS_E2BIG;
    strcpy(buf, number);
    buf[n] = ':';
    strcpy(buf + n + 1u, __fs_cwd);
    return (int)length;
}

int fs_stat(char *path, fs_stat_t *out) {
    struct __fs_where w;
    struct __fs_dirent parent;
    struct __fs_dirent e;
    unsigned b;
    unsigned o;
    int r = __fs_locate(path, __fs_path_a, &w, &parent);
    if (r < 0) return r;
    r = __fs_target(&w, &parent, &e, &b, &o);
    if (r < 0) return r;
    memcpy(out->name, e.name, 32u);
    out->name[31] = 0;
    out->type = e.type;
    out->size = e.size;
    return FS_OK;
}

int fs_mkdir(char *path) {
    struct __fs_where w;
    struct __fs_dirent parent;
    struct __fs_dirent e;
    struct __fs_volume *v;
    struct __fs_slot *s;
    unsigned b;
    unsigned o;
    unsigned block;
    unsigned need;
    int r = __fs_locate(path, __fs_path_a, &w, &parent);

    if (r < 0) return r;
    v = &__fs_vols[w.vol];
    if (__fs_name_len(&w) == 0u) return FS_EEXIST;
    r = __fs_find(v, &parent, __fs_name(&w), __fs_name_len(&w), &e, &b, &o);
    if (r == FS_OK) return FS_EEXIST;
    if (r != FS_ENOENT) return r;
    r = __fs_check_name(__fs_name(&w), __fs_name_len(&w));
    if (r < 0) return r;
    need = __fs_has_empty(v, &parent) ? 1u : 2u;
    if (v->free_blocks < need) return FS_ENOSPC;

    block = __fs_alloc(v);
    if (block == 0u) return FS_ENOSPC;
    s = __fs_fresh(v->channel, block);             /* 8 empty slots */
    __fs_dirty(s, FS__CONTENT);
    __fs_new_entry(&e, __fs_name(&w), __fs_name_len(&w), FS_TYPE_DIR, block, FS__BLOCK);
    r = __fs_add_entry(v, &parent, w.pblock, w.poffset, &e, &b, &o);
    __fs_flush();
    return r;
}

/* Clear an entry, then free its chain -- never the other way round, or a
 * crash in between would leave an entry pointing into free space. */
static int __fs_unlink(struct __fs_volume *v, unsigned block, unsigned offset,
                       unsigned first) {
    int r = FS_OK;
    __fs_clear_entry(v, block, offset);
    __fs_flush();
    if (first != 0u) {
        r = __fs_free_chain(v, first);
        __fs_flush();
    }
    return r;
}

int fs_remove(char *path) {
    struct __fs_where w;
    struct __fs_dirent parent;
    struct __fs_dirent e;
    unsigned b;
    unsigned o;
    int r = __fs_locate(path, __fs_path_a, &w, &parent);
    if (r < 0) return r;
    r = __fs_target(&w, &parent, &e, &b, &o);
    if (r < 0) return r;
    if (e.type == FS_TYPE_DIR) return FS_EISDIR;
    if (__fs_is_open(w.vol, b, o)) return FS_EBUSY;
    return __fs_unlink(&__fs_vols[w.vol], b, o, e.first);
}

int fs_rmdir(char *path) {
    struct __fs_where w;
    struct __fs_dirent parent;
    struct __fs_dirent e;
    struct __fs_dirent scratch;
    unsigned b;
    unsigned o;
    unsigned sb;
    unsigned so;
    int r = __fs_locate(path, __fs_path_a, &w, &parent);
    if (r < 0) return r;
    if (__fs_name_len(&w) == 0u) return FS_EBUSY;           /* the root */
    r = __fs_target(&w, &parent, &e, &b, &o);
    if (r < 0) return r;
    if (e.type != FS_TYPE_DIR) return FS_ENOTDIR;
    if (__fs_holds_cwd(w.vol, w.path, w.length)) return FS_EBUSY;
    if (__fs_is_open(w.vol, b, o)) return FS_EBUSY;
    r = __fs_scan(&__fs_vols[w.vol], &e, FS__USED, (char *)0, 0u, &scratch, &sb, &so);
    if (r == FS_OK) return FS_ENOTEMPTY;
    if (r != FS_ENOENT) return r;
    return __fs_unlink(&__fs_vols[w.vol], b, o, e.first);
}

int fs_rename(char *from, char *to) {
    struct __fs_where src;
    struct __fs_where dst;
    struct __fs_dirent sparent;
    struct __fs_dirent dparent;
    struct __fs_dirent e;
    struct __fs_dirent moved;
    struct __fs_volume *v;
    unsigned sb;
    unsigned so;
    unsigned b;
    unsigned o;
    int r;

    r = __fs_locate(from, __fs_path_a, &src, &sparent);
    if (r < 0) return r;
    if (__fs_name_len(&src) == 0u) return FS_EBUSY;         /* the root */
    v = &__fs_vols[src.vol];
    r = __fs_find(v, &sparent, __fs_name(&src), __fs_name_len(&src), &e, &sb, &so);
    if (r < 0) return r;
    r = __fs_locate(to, __fs_path_b, &dst, &dparent);
    if (r < 0) return r;
    if (dst.vol != src.vol) return FS_EXDEV;
    if (strcmp(src.path, dst.path) == 0) return FS_OK;
    /* Into its own subtree. The paths are normalised, so a plain prefix
     * compare is enough -- "/a/./b/../b/x" has already become "/a/b/x". */
    if (e.type == FS_TYPE_DIR && strncmp(dst.path, src.path, src.length) == 0
            && dst.path[src.length] == '/') {
        return FS_EINVAL;
    }
    if (__fs_is_open(src.vol, sb, so)) return FS_EBUSY;
    if (__fs_holds_cwd(src.vol, src.path, src.length)) return FS_EBUSY;
    if (__fs_name_len(&dst) == 0u) return FS_EEXIST;
    r = __fs_find(v, &dparent, __fs_name(&dst), __fs_name_len(&dst), &moved, &b, &o);
    if (r == FS_OK) return FS_EEXIST;
    if (r != FS_ENOENT) return r;
    r = __fs_check_name(__fs_name(&dst), __fs_name_len(&dst));
    if (r < 0) return r;
    if (!__fs_has_empty(v, &dparent) && v->free_blocks < 1u) return FS_ENOSPC;

    memcpy(&moved, &e, FS__ENTRY);
    memset(moved.name, 0, 32u);
    memcpy(moved.name, __fs_name(&dst), __fs_name_len(&dst));
    r = __fs_add_entry(v, &dparent, dst.pblock, dst.poffset, &moved, &b, &o);
    if (r < 0) {
        __fs_flush();
        return r;
    }
    /* The new name reaches the disk before the old one goes: a crash in
     * between leaves the file under two names, which fsck resolves --
     * never under none. */
    __fs_flush();
    __fs_clear_entry(v, sb, so);
    __fs_flush();
    return FS_OK;
}

int fs_opendir(char *path) {
    struct __fs_where w;
    struct __fs_dirent parent;
    struct __fs_dirent e;
    unsigned b;
    unsigned o;
    int fd = __fs_free_fd();
    int r;
    if (fd < 0) return FS_EMFILE;
    r = __fs_locate(path, __fs_path_a, &w, &parent);
    if (r < 0) return r;
    r = __fs_target(&w, &parent, &e, &b, &o);
    if (r < 0) return r;
    if (e.type != FS_TYPE_DIR) return FS_ENOTDIR;
    __fs_open_handle(&__fs_files[fd], FS__DIRHANDLE, w.vol, b, o, &e);
    return fd;
}

int fs_readdir(int dh, fs_stat_t *out) {
    struct __fs_file *f = __fs_handle(dh, 0u, 1u);
    struct __fs_volume *v;
    struct __fs_slot *s;
    struct __fs_dirent *d;
    unsigned block;
    if (f == FS__NO_FILE) return FS_EBADF;
    v = &__fs_vols[f->vol];
    /* pos counts slots: 8 to a block */
    while (f->pos < (f->size >> 6)) {
        block = __fs_file_block(v, f, f->pos >> 3);
        if (block == 0u) return FS_ECORRUPT;
        s = __fs_get(v->channel, block, 1u);
        d = (struct __fs_dirent *)(s->data + ((f->pos & 7u) << 6));
        f->pos++;
        if (d->type != 0u) {
            memcpy(out->name, d->name, 32u);
            out->name[31] = 0;
            out->type = d->type;
            out->size = d->size;
            return 1;
        }
    }
    return 0;
}

int fs_closedir(int dh) {
    struct __fs_file *f = __fs_handle(dh, 0u, 1u);
    if (f == FS__NO_FILE) return FS_EBADF;
    __fs_release(f);
    return FS_OK;
}

/* --- whole files --------------------------------------------------------------- */

int fs_load(char *path, void *buf, unsigned max) {
    int fd = fs_open(path, FS_READ);
    int n;
    if (fd < 0) return fd;
    if (__fs_files[fd].size > max) {
        fs_close(fd);
        return FS_E2BIG;
    }
    n = fs_read(fd, buf, max);
    fs_close(fd);
    return n;
}

void *fs_load_alloc(char *path, unsigned *size) {
    int fd = fs_open(path, FS_READ);
    unsigned n;
    int got;
    void *p;
    if (fd < 0) return (void *)0;
    n = __fs_files[fd].size;
    p = malloc(n > 0u ? n : 1u);
    if (p == (void *)0) {
        fs_close(fd);
        return (void *)0;
    }
    got = fs_read(fd, p, n);
    fs_close(fd);
    if (got < 0 || (unsigned)got != n) {
        free(p);
        return (void *)0;
    }
    if (size != (unsigned *)0) *size = n;
    return p;
}

int fs_save(char *path, void *buf, unsigned n) {
    int fd = fs_open(path, FS_WRITE | FS_CREATE | FS_TRUNC);
    int w;
    if (fd < 0) return fd;
    w = fs_write(fd, buf, n);
    fs_close(fd);
    if (w < 0) return w;
    if ((unsigned)w != n) return FS_ENOSPC;
    return w;
}

char *fs_strerror(int err) {
    if (err >= 0) return "ok";
    if (err == FS_ENOENT) return "no such file or directory";
    if (err == FS_EEXIST) return "already exists";
    if (err == FS_ENOTDIR) return "not a directory";
    if (err == FS_EISDIR) return "is a directory";
    if (err == FS_ENOTEMPTY) return "directory not empty";
    if (err == FS_ENOSPC) return "disk full";
    if (err == FS_EMFILE) return "too many open";
    if (err == FS_EBADF) return "bad handle";
    if (err == FS_EINVAL) return "invalid argument";
    if (err == FS_ENAMETOOLONG) return "name too long";
    if (err == FS_EBUSY) return "in use";
    if (err == FS_ENODEV) return "no such disk";
    if (err == FS_ENOFS) return "not formatted";
    if (err == FS_ENOTBLANK) return "disk holds data";
    if (err == FS_EXDEV) return "across disks";
    if (err == FS_E2BIG) return "too big for the buffer";
    if (err == FS_ECORRUPT) return "disk is corrupt";
    if (err == FS_ENOMEM) return "out of memory";
    return "unknown error";
}
