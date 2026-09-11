# PigeonFS: a filesystem for the pigeon machine

> **Status: phases 0 to 4 are done**: the string library, the host tool, the
> disk's new home, and the guest library with its tests. What's left is the
> demo program (phase 5) and HDD DMA (phase 6). **Every decision is settled** (the log is in
> [§14](#14-decision-log)). The numbers in §1 and §7 were **measured** on this
> emulator: a probe program was compiled, run and counted. Anything that was
> reasoned but not run is marked *unverified*.

| Decision | Choice |
|---|---|
| Where the logic lives | A guest C library, `<pigeon/fs.h>` (`lib/pigeon/fs.c`), that implements the format over raw HDD blocks, **plus** a host tool, `tools/pfs.py`, that implements the same format in Python |
| Directories | Nested, with `/`-separated paths, **plus a current directory**: relative paths, `.` and `..` |
| Which disk | The IO channel is an **argument** to `fs_mount` / `fs_format`. Several disks can be mounted at once |
| Naming a volume in a path | A channel prefix, as in FatFs: `2:/saves/a.dat`. A path without one uses the current volume |
| Boot-disk protection | A **library guard only**. The emulator is not changed (§6.6) |
| Lifetime | **A disk keeps its contents until someone deliberately reformats it**, like a real drive. Nothing formats it implicitly, and reformatting a PGFS disk needs a force flag |
| Disk image | Moves from `build/pigeon_hard_drive.bin` to `disks/hdd.img` (gitignored). A new image is **4 MiB** |
| API | POSIX-style file descriptors, with `fs_load` / `fs_save` and line helpers (`fs_gets` / `fs_puts`) on top |
| Names | 1 to 31 bytes, case-sensitive |
| Timestamps | None. An `mtime` field is reserved and always zero |
| Block copy path | The IO window copy now; **HDD DMA commands as a later, separate phase** (§9) |
| Prerequisite | A small string library, `<pigeon/string.h>`, is built first (§10) |

---

## 1. The hardware it sits on

Every fact below was checked against the code or the running machine, and
each one constrains the design.

**Two identical disks.** Both channels are `emulator/devices/hdd.py`
instances with the same commands: `0` NOP, `1` GET_SIZE (8-byte LE), `2` READ,
`3` WRITE, `4` TRUNCATE, `5` FLUSH. `IO_ADDRESS` is a byte offset on the image.
Because it is 32 bits wide, no disk can be bigger than 4 GiB.

| Channel | Backed by | Notes |
|---|---|---|
| 1 `CH_USERPROG` | the program's own `.bin`, opened `r+b` | The BIOS reads it until a short chunk comes back, so it loads **the whole file** into RAM. Nothing checks the size |
| 2 `CH_HDD` | the image named by `"disk"` in `config.json` | The HDD device creates it full of zeros, at 4 MiB, if it is missing |

**Everything passes through the 4 KB window.** The device returns bytes and
the CPU copies them. One command moves at most 4096 bytes. On a write the
controller takes `LENGTH` bytes starting at the window base, so a `LENGTH` over
4096 would send part of the framebuffer as payload.

**`IO_RW = 1` is what hands the window to the device.** If `CMD_WRITE` runs with
`IO_RW = 0`, the controller passes a zeroed buffer, and the device writes zeros
to the disk.

**A read past EOF comes back short, not zero-filled.** `IO_RETURN_DATA < LENGTH`.
The BIOS loader relies on this to find the end of the program, so the device
stays as it is. The library zero-fills the rest of the block itself.

**Writes report nothing.** WRITE returns 0 bytes, so a write error has no
channel. A channel with no device on it answers `0xFFFFFFFF`.

**Command numbers mean different things on different devices.** Command 1 is
GET_SIZE on a disk, mouse position on HID, INFO on the display, and **START on
the timer**. So you cannot probe an unknown channel without risking a side
effect (§6.6).

**The window is shared by every device.** Data has to be copied out before
the next IO call of any kind: a `key_read()` would overwrite it. There is one
CPU and no interrupts, so copying it out "before the library function returns"
is enough.

**`DIV` by zero kills the emulator.** It raises a Python exception rather than
faulting the guest. All block arithmetic here uses shifts and masks (`512 = 1 << 9`),
so the block code never divides and has no divide to guard.

**Static globals are part of the program image.** Measured: adding
`static unsigned char big[65536];` made the image exactly 65,536 bytes larger,
and the BIOS copies every byte on every boot. So large buffers come from the
heap, the way `disp_use_back_buffer()` gets its back buffer.

**Copying is the cost, and nothing else comes close.** Measured on a Machine
with a real HDD on channel 2:

| Operation | Instructions | ≈ time at 2.5M IPS |
|---|---|---|
| read one 512 B block (IO header + `memcpy` out of the window) | 4,349 | 1.74 ms |
| write one 512 B block (`memcpy` into the window + IO header) | 4,343 | 1.74 ms |

Almost all of that is the compiled `memcpy`, at about 34 instructions per word.
Programming the IO header costs next to nothing by comparison. That puts a
ceiling of **~290 KB/s** on disk throughput, and the design is built to copy as
few blocks as possible.

---

## 2. Architecture

```
 user program
   │  fs_load / fs_save / fs_load_alloc             convenience layer
   │  fs_gets / fs_puts
   │  fs_open / read / write / seek / tell / close  file layer: open-file table
   │  fs_mkdir / rmdir / remove / rename / stat     namespace layer: current directory,
   │  fs_opendir / readdir / closedir                 path normalisation, path walk
   │  fs_chdir / getcwd
   │  fs_format / mount / unmount / statvfs / sync  volume layer: mount table, superblock
   │
   ├── allocator      FAT: next-fit search, extend a chain, free a chain
   ├── block cache    8 × 512 B from the heap, shared by every volume,
   │                  tagged (channel, block)
   └── block device   blk_read / blk_write / blk_read_run / blk_write_run
                      the ONLY code that touches <pigeon/io.h>
                             │
          IO controller ── channel N ── HDD device ── host image file
                                                            ▲
                          tools/pfs.py  (same format) ──────┘
```

- **One unit, `lib/pigeon/fs.c`.** There is no linker. The file contains the
  layers above in bottom-up order, and `fs.h` is the public interface.
- **Dependencies:** `<pigeon/string.h>` (paths, names, the current directory),
  `<pigeon/mem.h>` (`memcpy`, `memset`, `malloc`) and `<pigeon/io.h>`. The
  launcher adds `fs.c` automatically when a program has `#include <pigeon/fs.h>`
  (see `programs.libraries_for`), and it pulls in `string.c` and `mem.c`
  through `fs.c`'s own includes.
- **The host tool is the reference implementation.** Every guest test is
  checked against it (§12), which is how two implementations of one format
  stay in agreement.

---

## 3. On-disk format, version 1

All integers are little-endian 32-bit words. A **block** is 512 bytes, and
block *N* starts at byte offset `N × 512`.

```
block 0            superblock
blocks 1 .. F      FAT: one word per block on the disk
blocks F+1 ..      data: files and directories; the root directory starts at F+1
```

### 3.1 Superblock (block 0)

| Offset | Field | Value |
|---|---|---|
| 0 | `magic` | `0x53464750`, which reads "PGFS" in byte order |
| 4 | `version` | `1` |
| 8 | `block_size` | `512`. It is stored so a later version can change it; v1 refuses any other value |
| 12 | `total_blocks` | disk size / 512, rounded down |
| 16 | `fat_start` | `1` |
| 20 | `fat_blocks` | `ceil(total_blocks / 128)` |
| 24 | `data_start` | `fat_start + fat_blocks` |
| 28 | `free_blocks` | a hint: updated by every allocate and free, recomputed by `fsck` |
| 32 | `next_free` | a hint: where the next-fit search starts |
| 36 | `label` | up to 15 bytes, NUL-terminated and NUL-padded to 16 |
| 52 | reserved | 0 |
| 64 | `root` | **a 64-byte directory entry that describes the root directory** |
| 128 | reserved | 0 up to 511 |

Because the root's entry lives in the superblock, every directory, root
included, is described by an entry. There is one code path for "where does
this directory start and how big is it", not two.

### 3.2 FAT

| Value | Meaning |
|---|---|
| `0x00000000` | free |
| `0xFFFFFFFF` | end of chain |
| `0xFFFFFFFE` | reserved: the superblock and the FAT's own blocks |
| anything else | the number of the next block in this chain |

A FAT block holds 128 entries. Block 0 is always reserved, so `0` can also
mean "no block" in a directory entry.

| Disk | Blocks | FAT blocks | Overhead |
|---|---|---|---|
| 1 MiB | 2,048 | 16 | 0.8 % |
| **4 MiB (the default)** | 8,192 | 64 | 0.8 % |
| 16 MiB | 32,768 | 256 | 0.8 % |

### 3.3 Directory entry (64 bytes, 8 per block)

| Offset | Size | Field |
|---|---|---|
| 0 | 32 | `name`: 1 to 31 bytes, NUL-terminated, NUL-padded |
| 32 | 4 | `type`: `0` empty slot, `1` file, `2` directory |
| 36 | 4 | `first`: first block, or `0` for an empty file |
| 40 | 4 | `size` in bytes. For a directory this is always a whole number of blocks |
| 44 | 4 | `mtime`: reserved, always `0` in v1 |
| 48 | 16 | reserved, `0` |

A directory is a file whose contents are these entries. A new directory gets
one zeroed block, which is 8 empty slots. Deleting an entry sets its `type` to
0, and free slots are reused before the directory grows by another block. In
v1 directories never shrink.

**There are no `.` or `..` entries on disk.** The library resolves them on the
path string instead (§5). That is only correct because every directory has
exactly one parent: the format has no hard links to directories and no
symlinks. Adding either would mean storing `..` for real.

### 3.4 Why FAT, and why 512-byte blocks

**FAT** puts allocation state and file chaining in a single table. That is the
least code to write in a C subset with no `switch` and no `union`, and there
is no inode table to size at format time. The cost is random access: seeking to
byte *N* means following *N*/512 links. Each open file caches its current
(logical, physical) block pair, so sequential access, the common case, costs
one FAT lookup per block, and that FAT block is nearly always already in the
cache. Seeking at random in a large file is linear. That is acceptable at this
scale, and it can be fixed later with extents without changing the API.

**512 bytes** because a block is the unit of copying, and copying is the cost.
A small file or a partial update copies 512 bytes, not 4 KB. Contiguous runs
can still be moved 8 blocks, or 4 KB, per command (§6.1).

---

## 4. The C API: `<pigeon/fs.h>`

The authoritative version, with a comment on every call, is
`lib/pigeon/fs.h`.

```c
#ifndef PIGEON_FS_H
#define PIGEON_FS_H

/* --- errors -------------------------------------------------------------
 * Every call returns >= 0 on success and one of these on failure, the
 * same convention as key_read() returning -1. */
#define FS_OK             0
#define FS_ENOENT       (-1)    /* no such file or directory            */
#define FS_EEXIST       (-2)
#define FS_ENOTDIR      (-3)    /* a path component is a file           */
#define FS_EISDIR       (-4)
#define FS_ENOTEMPTY    (-5)
#define FS_ENOSPC       (-6)    /* disk full                            */
#define FS_EMFILE       (-7)    /* too many open handles, or volumes    */
#define FS_EBADF        (-8)    /* not an open handle, or wrong mode    */
#define FS_EINVAL       (-9)
#define FS_ENAMETOOLONG (-10)   /* a name over 31 bytes, a path over 255 */
#define FS_EBUSY        (-11)   /* it is open, it is the current directory,
                                   or the volume has open files         */
#define FS_ENODEV       (-12)   /* no disk on that channel              */
#define FS_ENOFS        (-13)   /* the disk has no PGFS superblock      */
#define FS_ENOTBLANK    (-14)   /* format refused: the disk holds data  */
#define FS_EXDEV        (-15)   /* rename across volumes                */
#define FS_E2BIG        (-16)   /* the result is bigger than the buffer */
#define FS_ECORRUPT     (-17)   /* inconsistent on disk: run pfs fsck   */
#define FS_ENOMEM       (-18)   /* the heap could not provide the cache */

char *fs_strerror(int err);     /* for disp_text()                      */

/* --- volumes ---------------------------------------------------------- */
#define FS_FORMAT_FORCE 1       /* format a disk that holds data, PGFS or not */

int fs_format(unsigned channel, char *label, unsigned flags);
int fs_mount(unsigned channel);   /* the first mount sets the current dir to "N:/" */
int fs_unmount(unsigned channel); /* FS_EBUSY while files are open on it */
int fs_sync(unsigned channel);    /* HDD FLUSH: fsync the image on the host */

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
#define FS_CREATE   4
#define FS_TRUNC    8
#define FS_APPEND  16

#define FS_SEEK_SET 0
#define FS_SEEK_CUR 1
#define FS_SEEK_END 2

int fs_open (char *path, unsigned flags);       /* -> fd >= 0              */
int fs_read (int fd, void *buf, unsigned n);    /* -> bytes, 0 at the end  */
int fs_write(int fd, void *buf, unsigned n);    /* -> bytes, short when full */
int fs_seek (int fd, int offset, int whence);   /* -> the new position     */
int fs_tell (int fd);
int fs_close(int fd);

/* --- lines ------------------------------------------------------------ */
int fs_gets(int fd, char *buf, unsigned size);  /* one line, '\n' kept, NUL-terminated;
                                                   -> length, 0 only at the end */
int fs_puts(int fd, char *s);                   /* -> bytes written        */

/* --- the namespace ---------------------------------------------------- */
#define FS_NAME_MAX  31
#define FS_PATH_MAX  255
#define FS_TYPE_FILE 1
#define FS_TYPE_DIR  2

typedef struct { char name[32]; unsigned type; unsigned size; } fs_stat_t;

int fs_chdir (char *path);                      /* also switches volume: "1:/" */
int fs_getcwd(char *buf, unsigned size);        /* "2:/saves" -> length, or FS_E2BIG */

int fs_stat  (char *path, fs_stat_t *out);
int fs_mkdir (char *path);
int fs_rmdir (char *path);                      /* must be empty           */
int fs_remove(char *path);                      /* files only              */
int fs_rename(char *from, char *to);            /* one volume; may change directory */

int fs_opendir (char *path);                    /* -> a handle, from the fd table */
int fs_readdir (int dh, fs_stat_t *out);        /* 1 got one, 0 the end, <0 error */
int fs_closedir(int dh);

/* --- whole files ------------------------------------------------------ */
int   fs_load(char *path, void *buf, unsigned max);  /* -> bytes, or FS_E2BIG */
void *fs_load_alloc(char *path, unsigned *size);     /* malloc'd; NULL on error */
int   fs_save(char *path, void *buf, unsigned n);    /* create or replace       */

#endif
```

A program using it:

```c
#include <pigeon/fs.h>

int main(void) {
    int r = fs_mount(CH_HDD);
    if (r == FS_ENOFS) {                       /* brand new, all-zero disk */
        if (fs_format(CH_HDD, "PIGEON", 0) < 0) return 1;   /* never forced */
        r = fs_mount(CH_HDD);
    }
    if (r < 0) return 1;

    fs_mkdir("/saves");                        /* FS_EEXIST from then on: fine */
    fs_chdir("/saves");
    fs_save("score", &score, 4);               /* relative: /saves/score */

    int fd = fs_open("../log.txt", FS_WRITE | FS_CREATE | FS_APPEND);
    fs_puts(fd, "started\n");
    fs_close(fd);
    return 0;
}
```

The first run formats the disk, and every later run finds the files the
previous one left. A disk holding data is never formatted by this code: without
`FS_FORMAT_FORCE`, `fs_format` refuses anything that isn't blank (§6.6).

### The rules

- **One writer per file.** Opening a file with `FS_WRITE` while any other
  handle has it open returns `FS_EBUSY`. Any number of readers is fine. The
  reason: a file's size and first block live in its open-file slot, so two
  writers would each keep their own copy and the last to write would win. A
  shared per-file node is the way to lift this later.
- **`fs_remove`, `fs_rename`, `fs_rmdir` and `FS_TRUNC` refuse anything that is
  open**, with `FS_EBUSY`. An open file remembers where its directory entry
  is, so moving or freeing that entry under it would send its size updates
  to the wrong place. The current directory has the same protection (§5).
- **No sparse files.** Seeking past the end returns `FS_EINVAL`, because the
  FAT has no way to record a hole.
- `FS_WRITE` without `FS_CREATE` on a missing file returns `FS_ENOENT`. With
  `FS_APPEND`, every write goes to the current end of the file.
- **`fs_gets` works like `fgets`.** It keeps the `'\n'` so that 0 can only mean
  the end of the file; an empty line comes back as `"\n"`. A line longer than
  the buffer comes back in pieces. It scans the cached block directly rather
  than calling `fs_read` one byte at a time, because every public call flushes
  the cache (§6.2).
- A directory handle works only with `fs_readdir`, and a file handle only with
  `fs_read` / `fs_write` / `fs_gets` / `fs_puts`. The wrong pairing returns
  `FS_EBADF`.
- **Aligned buffers are faster.** `memcpy` copies a word at a time only when
  both pointers are word-aligned. The window and `malloc` are aligned, so
  pass `fs_read` / `fs_write` an aligned buffer too.
- **`fs_sync` is about the host, not the emulator.** Every call already leaves
  the image written through to the host file, so it survives the emulator
  shutting down or crashing. `fs_sync` adds an `fsync` (HDD FLUSH), for a host
  that loses power.
- **Fixed limits:** 4 volumes, 8 open handles (files and directories
  together), 8 cache blocks. These are constants in `fs.c`. With no linker, a
  `#define` in the program can't reach the library, so they are not tunable
  per program.

---

## 5. Paths, volumes and the current directory

```
path     :=  [ channel ":" ] [ "/" ] [ segment { "/" segment } ]
channel  :=  decimal digits: the IO channel number
segment  :=  name | "." | ".."
name     :=  1..31 bytes, none of them '/', ':' or NUL
```

**There is one current directory**: a volume plus an absolute path on it.
DOS keeps a separate one per drive; that is extra state for very little, so
PigeonFS keeps a single one. It starts as `N:/` on the first successful mount.

| Written | Means |
|---|---|
| `a/b` | relative to the current directory |
| `/a/b` | from the root of the **current** volume |
| `2:/a/b` or `2:a/b` | from the root of the volume on channel 2. A prefix always means "from that volume's root" |
| `.` / `..` | this directory / its parent. `..` at a root stays at the root, as in POSIX |

- **`fs_chdir` is also how you switch volumes:** `fs_chdir("1:/")`. It
  refuses a file (`FS_ENOTDIR`) and an unmounted channel (`FS_ENODEV`).
  `fs_getcwd` returns the prefixed form, such as `2:/saves`, so its result
  always works as an argument to `fs_chdir`.
- **Every path is resolved by first making it absolute, then normalising it.**
  The library copies `cwd + "/" + path` into a 256-byte scratch buffer and
  rewrites it in place: empty segments and `.` are dropped, and `..` removes
  the segment before it. Then it walks the result from the root. As §3.3
  explains, this is correct only because a directory has exactly one parent.
  A result longer than `FS_PATH_MAX` (255) gives `FS_ENAMETOOLONG`.
- Names are case-sensitive and compared byte for byte, and may contain
  spaces. `.` and `..` can't be used as the name of something new
  (`FS_EINVAL`).
- **Normalising first makes plain string checks reliable.** `/a//b/../c` and
  `/a/c` become the same string before anything compares them:
  - **Moving a directory into its own subtree** is refused with `FS_EINVAL`
    when the normalised `to` starts with the normalised `from` followed by
    `/`.
  - **The current directory is protected.** `fs_rmdir` of the current
    directory returns `FS_EBUSY`. Its ancestors can't be removed anyway,
    because they aren't empty. `fs_rename` of the current directory or any
    ancestor also returns `FS_EBUSY`, since it would leave the saved path
    naming something that no longer exists.
  - **`fs_unmount` of the current volume** moves the current directory to the
    root of another mounted volume. If none is left, relative paths return
    `FS_ENODEV` until something is mounted.
- **The cost of walking a path.** A relative path is walked from the root, so a
  deep current directory costs a block read per level. The cache usually
  already holds those blocks. A later optimisation, if it's needed, is to
  start from the current directory's first block when the path contains no
  `..`. Directories are read 8 entries per block, so a root holding 20 entries
  is 3 blocks. The walk records where the entry it finds lives (block, slot),
  so a later size update can rewrite that entry in place.

---

## 6. Inside `fs.c`

### 6.1 The block device

```c
static int blk_read     (unsigned ch, unsigned block, unsigned char *buf);
static int blk_write    (unsigned ch, unsigned block, unsigned char *buf);
static int blk_read_run (unsigned ch, unsigned block, unsigned count, unsigned char *buf);
static int blk_write_run(unsigned ch, unsigned block, unsigned count, unsigned char *buf);
```

These program the IO header the way `input.c`'s `hid_call` does: `IO_RW`,
`IO_CMD`, `IO_LEN = 512 × count`, `IO_ADDR = block << 9`, and **`IO_CH = ch`
last**. After a read they copy `IO_RETLEN` bytes out of the window and zero the
rest. If `IO_RETLEN` is `0xFFFFFFFF`, the result is `FS_ENODEV`.

The `_run` versions move up to 8 contiguous blocks (one full window) in a
single command, **straight between the window and the caller's buffer**. That
makes one copy instead of two, and it skips the cache. The file layer uses them
when the FAT chain is contiguous and the caller's buffer covers whole blocks.

These four functions are all that changes when the DMA commands arrive (§9).

### 6.2 The block cache and the heap

- **One `malloc` at the first mount or format** holds the 8 cache slots of
  512 bytes with their tags, the 256-byte current-directory string, and the
  256-byte path scratch buffer: about 4.7 KB. The small volume and file tables
  are `static`, a few hundred bytes of image. Everything larger is on the heap,
  for the reason in §1.
- Each slot is tagged (channel, block) and carries an LRU stamp and a dirty
  bit.
- Metadata (superblock, FAT, directories) and partial-block file IO go through
  the cache. Whole-block file IO bypasses it, so a large read doesn't evict
  the FAT blocks the next read will need.
- **Write-back within a call, write-through across calls.** Every public
  function flushes the cache before it returns, so nothing is dirty while
  control is back in the user program. This matters because the emulator can
  be stopped (Ctrl-C) between any two guest instructions, and the guest gets
  no shutdown hook. With the flush, the image on the host is consistent
  whenever the program is running its own code. Inside a single call, a FAT
  block that a large write touches 40 times is still written only once.
- `fs_unmount` drops that channel's slots.

### 6.3 Crash ordering

The emulator can only be killed in the middle of a call if it is stopped while
the library is running. Ordering the writes keeps the damage from that case
repairable:

> A block is made **reachable** only after its contents are on disk: data,
> then FAT, then the directory entry. It is made **unreachable** before it is
> freed: clear the entry, flush, then free the chain.

`fs_remove` and `FS_TRUNC` therefore have an explicit flush point partway
through. After a kill, the worst outcomes are **leaked blocks**, or, for
`fs_rename` only, **one file visible under both names**. The rename writes the
new entry before it clears the old one. `pfs fsck --repair` detects and fixes
both. Nothing ever leaves two files sharing blocks, or an entry pointing into
free space. The `free_blocks` hint can be off after a kill. Mount doesn't
recompute it, because that would mean scanning the whole FAT; `fsck` does.

### 6.4 The allocator

- `alloc_block` searches **next-fit** from `next_free` and wraps around once. It
  returns `FS_ENOSPC` if a full lap finds nothing. Next-fit keeps files
  mostly contiguous, and that is what lets the `_run` transfers take effect.
- `extend(last)` links a new block after `last`, and `free_chain(first)` walks a
  chain and zeroes it.
- New directory blocks are zeroed, because every slot has to read as empty.
  New file blocks are not zeroed, because they are about to be written.

### 6.5 Open files

```c
struct fs_file {
    unsigned used, flags, volume;
    unsigned ent_block, ent_slot;   /* where the directory entry lives */
    unsigned first, size, pos;
    unsigned cur_index, cur_block;  /* cached logical -> physical block */
};
```

An fd is an index into this table. Directory handles use the same table, with
a flag set.

**`fs_read`** clamps *n* to `size - pos`, then loops. For each position,
`logical = pos >> 9` and `offset = pos & 511`. It maps the logical block to a
physical one, walking forward from the cached pair when it can and from
`first` when it can't. If the position is block-aligned and at least a block
remains, it reads a contiguous run with `blk_read_run`. Otherwise it gets the
block from the cache and copies part of it.

**`fs_write`** first moves `pos` to `size` if the file has `FS_APPEND`. It
extends the chain when `pos` reaches its end. A partial-block write
read-modify-writes through the cache, except for a fresh block past `size`,
which has nothing worth reading. Then it updates the size in the entry. When
the disk fills up it returns the bytes written so far, or `FS_ENOSPC` if that
is zero.

### 6.6 Format, mount, and the channel guard

The only protection for the boot disk is in the library, as decided:

1. **Known non-disk channels are refused by constant before any IO:**
   `CH_HID`, `CH_TIMER` and `CH_DISPLAY` all return `FS_ENODEV`. On the
   timer, command 1 is START, so even probing it would have a side effect.
2. **Any other channel must answer GET_SIZE with exactly 8 bytes**, which is
   what an HDD returns. An empty channel answers `0xFFFFFFFF`, which gives
   `FS_ENODEV`.
3. **Mount** checks the magic (`FS_ENOFS` if it's wrong), then the version,
   the block size, that `total_blocks × 512` fits within GET_SIZE, and that
   `fat_blocks`, `data_start` and the root entry are consistent
   (`FS_ECORRUPT` if not). Mounting a channel that is already mounted returns
   `FS_OK` and does nothing.
4. **Format** refuses a mounted channel (`FS_EBUSY`) and a disk under 16 blocks
   (`FS_EINVAL`). **Without `FS_FORMAT_FORCE`, it formats only a disk whose
   block 0 is all zeros.** Anything else returns `FS_ENOTBLANK`: an existing
   PigeonFS disk, and just as much a program image. That is the "a disk
   lives until it is deliberately reformatted" rule. The only ways to wipe a
   disk are to pass the force flag in code or to run `pfs mkfs --force`.

On channel 1, block 0 holds a program's code, so mount fails with `FS_ENOFS`
and format fails with `FS_ENOTBLANK`. **`FS_FORMAT_FORCE` on channel 1
overwrites the program's `.bin` on the host.** That is documented rather than
prevented. If channel 1 does hold a PGFS image (tests do this, §12), it mounts
like any other disk. Just don't boot from one: the BIOS would load the entire
image into RAM with no size check.

**What persistence depends on.** The emulator never formats, truncates or
re-creates an existing image. The HDD device only creates one when the file is
missing, and it creates it full of zeros. The image lives in `disks/`, outside
`build/`, so a clean build doesn't delete it. The guest flushes before every
return, so its data survives the emulator shutting down or crashing.
`fs_sync` covers the host itself going down. Format uses the whole disk, as
reported by GET_SIZE.

### 6.7 Writing it in this compiler's C

- There is no `switch`, `union` or `enum`, so dispatch is `if` chains and
  constants are `#define`s. Function-like macros work.
- Loop counters and block numbers are `unsigned`. Signed comparison costs two
  extra `XOR`s here, and block numbers are never negative anyway.
- **`static` hides nothing here.** `compile_units` compiles every unit as one
  translation unit, with one global scope and one macro table. A name clash
  is a loud compile error ("defined twice"), not a silent override. But the
  only way a user can fix it is by renaming their own function. So internal
  functions are named `__fs_…`, as `string.c` does with `__str_digit`.
  Internal macros keep the `FS_` prefix, the way `display.c` prefixes its
  command macros with `DISP_`. This document leaves the prefix off
  internal names to keep them readable.
- The feature this format depends on was checked by running a probe program on
  the emulator: a struct with a `char[]` member read back through a pointer
  cast into a byte buffer. `sizeof` on a 32-byte entry gave 32, and
  names, `first` and `size` all read back correctly.

---

## 7. Performance

These are measured on the finished library: whole guest programs on a 4 MiB
disk, each starting with a cold cache, with the program's own startup
subtracted.

| Operation | Instructions | ≈ time at 2.5M IPS |
|---|---|---|
| `fs_mount` | 6,269 | 2.5 ms |
| `fs_open("/a/b/c.txt")`, cold cache | 21,687 | 8.7 ms |
| `fs_load`, 4 KB | 50,565 | 20 ms |
| `fs_save`, 4 KB, a new file | 84,609 | 34 ms |
| `fs_load`, 100 KB | 932,437 | 0.37 s |
| `fs_save`, 100 KB, a new file | 1,094,893 | 0.44 s |
| `fs_format`, 4 MiB | 1,148,704 | 0.46 s |

The disk isn't what is slow; the copy loop is, and phase 6 (§9) removes it.

The code isn't small either. A program that includes `<pigeon/fs.h>`
compiles to about 99 KB together with `mem.c` and `string.c`, and the BIOS
copies every byte of that on every boot.

---

## 8. The host tool: `tools/pfs.py`

It uses the Python standard library only, like the assembler and the tests. It
is an importable module (`PgfsImage`) with a CLI on top, and the tests use the
module as their oracle.

```
python3 tools/pfs.py mkfs  [--size 4M] [--label NAME] [--force]
python3 tools/pfs.py info
python3 tools/pfs.py ls    [-l] [/path]
python3 tools/pfs.py tree  [/path]
python3 tools/pfs.py put   [-r] local /dest       (-r for a folder)
python3 tools/pfs.py get   [-r] /src local        (-r for a directory)
python3 tools/pfs.py cat   /path
python3 tools/pfs.py mkdir [-p] /path ...
python3 tools/pfs.py rm    [-r] /path ...
python3 tools/pfs.py mv    /from /to
python3 tools/pfs.py fsck  [--repair]
```

`put`, `get` and `mv` behave like `cp` and `mv`: if the destination is an
existing directory, the item goes inside it and keeps its name. `put`
writes exactly the way `fs_save` does. A new file gets its empty entry
before any data, as `fs_open(FS_CREATE)` does, and an existing file is
truncated before it's written. The order matters beyond crash safety: it
decides which blocks a growing directory and the data end up in, and the
two implementations have to agree on that.

- Every command takes `--image PATH`. The default is `"disk"` from
  `config.json` (read with `emulator.config.load_config`), so `pfs ls` shows the
  same disk the emulator puts on channel 2. On the host, paths are always
  absolute. `.` and `..` are normalised exactly as in the guest. A channel
  prefix such as `2:/` is refused, because the image is itself the volume.
- **Exit status:** 0 on success and 1 on any error. For `fsck`, 1 means
  problems remain: without `--repair` that is any problem, and with it, only
  the problems it can't fix safely.
- **`mkfs` sizing:** a missing image is created at **4 MiB**. An existing image
  keeps its current size unless `--size` is given.
- **`mkfs` follows the same rule as the guest.** It formats an all-zero image
  freely, and it refuses any image holding data, PigeonFS or not, unless
  `--force` is given.
- **Don't write to the image while the emulator is running.** The guest keeps
  the superblock and its cache in RAM between calls and won't see changes made
  from the host. Reading is safe, because the guest flushes before every
  return.
- **`fsck`** checks:
  - that the superblock is sane
  - that every chain stays in range, has no cycles, and has
    `ceil(size / 512)` blocks
  - that no block belongs to two chains
  - that the reserved FAT region is intact
  - that the `free_blocks` hint is right
  - that names are valid and unique within each directory

  `--repair` frees leaked blocks, trims chains that are longer than their size,
  resolves a rename that was interrupted (§6.3), restores the reserved FAT
  region, and rewrites the hints. It only reports anything where a fix would
  have to guess which data to throw away: cross-links, broken chains,
  chains shorter than their size, and bad names.

---

## 9. The block copy path

**Decided:** build everything on the IO window copy (A), and add HDD DMA
commands (C) as phase 6, **after** the correctness tests pass. Then measure
again.

| | Per 512 B block | What it costs to build |
|---|---|---|
| **A. Window copy**: phases 1 to 5 | ~4,350 instructions (measured) | nothing; ~290 KB/s |
| B. Hand-written assembly copy loop: not chosen | ~7 instructions per word, ≈ 900 *(unverified)* | the compiler has no inline assembly, and the launcher only builds `.c` units |
| **C. HDD DMA commands**: phase 6 | ~40 instructions, any length *(unverified)* | two new commands in `hdd.py`, and the HDD gets access to RAM the way `DisplayIO` already has |

**What C looks like:** `CMD_READ_DMA = 6` and `CMD_WRITE_DMA = 7`.
`IO_ADDRESS` is the disk offset. `IO_LENGTH = 8` is the size of the payload,
following `DisplayIO`'s rule that LENGTH is always the payload size. The data
window holds `[ram_address, byte_count]`. The device checks the RAM range the
way `DisplayIO._valid_base` does: it must start at or above
`PROGRAM_LOAD_ADDR` and end inside RAM. Its reply is one word, the number of
bytes transferred; a short count means end of file, and the rest is
zero-filled. A transfer is not limited to 4 KB, so a contiguous run of any
length takes one command.

**Detection:** an HDD that doesn't know command 6 answers with `LENGTH` zero
bytes, so `IO_RETLEN` is 8. One that does answers with a single word, so
`IO_RETLEN` is 4. The library probes once at mount and uses whichever the
device supports, so one `fs.c` runs on either.

This deliberately changes a stated principle: the README says "the bus itself
deliberately has no general DMA path". That sentence gets updated in phase 6.
The block device (§6.1) is the only part of `fs.c` that changes.

---

## 10. The prerequisite: `<pigeon/string.h>`

**Why it comes first.** The machine has no string handling, so every program
writes its own:

- `user/demo.c` builds its status line one digit at a time, with the comment
  "built by hand; there is no printf".
- `user/graph.c` has a private `str_put` (append) and `is_digit`.

The filesystem needs the same pieces, and more of them:
- names: compare, copy with a length limit
- paths: find `/` and `:`, join the current directory and a relative path
- `fs_getcwd`, `fs_gets` and `fs_puts`
- channel numbers: text to number when parsing `2:`, number to text for
  `fs_getcwd`

Writing all of that as private code in `fs.c` would make a third copy. A
shared library means one tested version, and the file browser demo gets
number formatting for file sizes out of it too.

```c
#ifndef PIGEON_STRING_H
#define PIGEON_STRING_H

typedef unsigned int size_t;              /* its own, not mem.h's: see below */
#ifndef NULL
#define NULL ((void *)0)
#endif

size_t strlen (char *s);
int    strcmp (char *a, char *b);
int    strncmp(char *a, char *b, size_t n);
char  *strcpy (char *dst, char *src);
size_t strlcpy(char *dst, char *src, size_t size); /* always NUL-terminates;
                                                      -> strlen(src), so
                                                      >= size means it was cut */
size_t strlcat(char *dst, char *src, size_t size);
char  *strchr (char *s, int c);                     /* NULL if absent */
char  *strrchr(char *s, int c);

/* --- numbers <-> text: there is no printf ------------------------------ */
int      utoa  (unsigned v, char *out, unsigned base); /* base 2..16 -> length */
int      itoa  (int v, char *out);                     /* decimal, '-' if negative */
unsigned strtou(char *s, char **end, unsigned base);   /* end: first unused char */
int      atoi  (char *s);                              /* spaces, sign, digits */

/* --- characters --------------------------------------------------------- */
int isdigit(int c);  int isalpha(int c);  int isspace(int c);
int tolower(int c);  int toupper(int c);

#endif
```

- **It stands alone: no heap and no `mem.c`.** It defines `size_t` and `NULL`
  itself instead of including `mem.h`. `libraries_for` only follows
  `#include` lines in `.c` files, so a header that included `mem.h` would
  declare `memcpy` without `mem.c` ever being compiled in. And a program that
  only formats a number shouldn't have to pay for an allocator.
- **Bounded copies only.** There is `strlcpy` / `strlcat` and no `strcat` or
  `strncpy`. `strncpy` leaves the result unterminated when the source is too
  long, and on a machine with no memory protection an overrun simply corrupts
  whatever comes next.
- **The number conversions need the same care as `math.h`.** `DIV` is
  unsigned, so `itoa` converts the magnitude as an unsigned value (which also
  handles `INT_MIN`). `utoa` rejects any base outside 2..16, so it can never
  divide by zero.
- The names follow C's, so they can't collide with `graph.c`'s private
  `is_digit`.
- Rewriting `demo.c` and `graph.c` to use it is optional and not planned. Both
  work as they are.

---

## 11. Changes outside `lib/pigeon/fs.*`

| File | Change |
|---|---|
| `lib/pigeon/string.h`, `lib/pigeon/string.c` | new (phase 0) |
| `lib/pigeon/fs.h`, `lib/pigeon/fs.c` | new |
| `lib/pigeon/mem.h` | its comment "No strings" points to `string.h` instead |
| `tools/pfs.py` | new |
| `tests/test_libs.py` | a `<pigeon/string.h>` section |
| `tests/test_pfs.py`, `tests/test_fs.py` | new |
| `config.json`, `emulator/config.py` | `"disk": "disks/hdd.img"` |
| `emulator/devices/hdd.py` | `DEFAULT_DISK` and the docstring point to `disks/hdd.img`; `create_size` becomes 4 MiB. **Read behaviour is unchanged**: the BIOS relies on short reads |
| `.gitignore` | add `disks/` |
| `README.md`, `lib/README.md` | string and filesystem sections, the config table, the layout |
| `user/files.c` | the demo program (phase 5) |
| `emulator/devices/hdd.py`, `README.md` | the DMA commands and the IO bus text (phase 6) |

Nothing changes in the BIOS, the IO controller, the memory map or the
compiler. No test mentions the old disk path. `build/pigeon_hard_drive.bin`
has no PGFS superblock, so there is nothing to migrate from it.

---

## 12. Testing

**`tests/test_libs.py`, string section:**
- `strlen` / `strcmp` / `strncmp`, including the empty string and one string
  being a prefix of the other
- `strlcpy` / `strlcat` truncating at the exact edge, always terminated, and
  returning the full source length
- `strchr` / `strrchr` finding the NUL itself
- `utoa` in bases 2, 10 and 16, including 0 and `0xFFFFFFFF`
- `itoa` at `INT_MIN`, -1, 0 and `INT_MAX`
- `strtou` / `atoi` with leading spaces, a sign, and trailing garbage
  (checking where `end` stops)

**`tests/test_pfs.py`** (host side, fast):
- `mkfs` geometry at several sizes, and the 4 MiB default
- `mkfs` refusing a populated image without `--force`
- `put` / `get` round trips at the block edges
- `mkdir` / `rm` / `mv`, with a clean `fsck` after every operation
- `fsck` catching each kind of injected corruption (cycle, cross-link, leak,
  wrong size), and `--repair` fixing the leak

**`tests/test_fs.py`** (the guest: C running on the emulator). The harness
`run_fs(source, image)`:
- builds the program with `fs.c`
- boots a `Machine` with `disk_path` set to a temporary image, and optionally
  `program_path` set to a second image for channel 1
- loads the program directly, as `test_libs.py` does
- runs to `HALT` and returns `A`

**After every test, `PgfsImage(image).fsck()` must come back clean.** That one
assertion catches most allocator bugs.

| Area | Cases |
|---|---|
| Mounting | format and mount on a fresh all-zero image; mount on zeros → `FS_ENOFS` |
| **Lifetime** | format without force on a **populated** PGFS disk → `FS_ENOTBLANK`, and every file is still there; with `FS_FORMAT_FORCE` → empty |
| Boot-disk guard | channel 1 holding a program `.bin`: mount → `FS_ENOFS`, format → `FS_ENOTBLANK`, and the file is **byte-identical** afterwards |
| Channel guard | `CH_HID` / `CH_TIMER` / `CH_DISPLAY` → `FS_ENODEV`, and **timer 0 was not started**; empty channel 7 → `FS_ENODEV` |
| Round trips | 0, 1, 511, 512, 513, 4095, 4096, 4097 bytes and 100 KB: every block and window edge |
| File ops | append; seek with SET / CUR / END; tell; truncate on open; seeking past the end refused |
| Lines | `fs_puts` then `fs_gets` line by line; an empty line comes back as `"\n"`; a line longer than the buffer comes back in pieces; a last line with no `'\n'`; 0 only at the end |
| Namespace | nested `mkdir`; `readdir` lists each entry exactly once; `stat`; removing a file returns **every** block (`statvfs` shows the starting free count again) |
| **Current directory** | `chdir` then `getcwd` round trip; relative open, create and remove; `.`, `..`, `a/../b` and `//`; `..` at the root stays there; `chdir` into a file → `FS_ENOTDIR`; `rmdir` of the current directory → `FS_EBUSY`; renaming an ancestor of it → `FS_EBUSY`; `chdir("1:/")` switches volume; unmounting the current volume |
| Refusals | `rmdir` on a non-empty directory; renaming a directory into its own subtree, written with `..` to defeat a naive string check; `EBUSY` for the one-writer rule and for removing an open file; `EMFILE` at the 9th open; `ENAMETOOLONG` at a 32-byte name and a 256-byte path |
| Full disk | fill it → `FS_ENOSPC` with a short write; delete → the space can be used again |
| Two volumes | PGFS images on channels 1 and 2: `1:/x` and `2:/x` are different files |
| Cross-implementation | the host `put`s a file and the guest reads the same bytes; the guest `fs_save`s and the host `get`s the same bytes |
| **Oracle** | the guest and `pfs.py` run the same operations on two images, which must match **byte for byte**. This runs twice: on a fresh disk, and on a 32-block disk where next-fit wraps around into reused blocks. Only reused blocks expose a directory block left un-zeroed, a last block's tail left un-zeroed, or a write assuming its blocks are contiguous |
| **Persistence** | the guest writes, the `Machine` is closed, a new `Machine` opens the same image, and the guest reads everything back |
| Crash | a program that creates, replaces, renames and removes, with a copy of the disk kept after **every** write it makes. Stopping the emulator can only land between two writes, so these copies are every state a crash can leave. Each must be fully repaired by `fsck --repair`, with every file still readable. Sampling a few moments instead missed a planted "free before unlink" bug, because the write that matters is one of dozens. Being repairable isn't enough on its own, since a file left with no name looks like nothing worse than leaked blocks. So once the file being renamed back and forth exists, it has to exist under some name in every later state |

---

## 13. Phases

0. **`<pigeon/string.h>`** and its tests. *Done.*
1. **The format and the host tool.** `tools/pfs.py` and `test_pfs.py`. The
   Python implementation is the oracle for every guest test after this.
   *Done.*
2. **Move the disk image.** Config, `hdd.py` (path and 4 MiB), `.gitignore`,
   README. *Done.*
3. **`fs.c`, bottom-up**, with each layer's tests passing before the next is
   written:
   1. block device and cache
   2. format / mount / statvfs / sync
   3. allocator
   4. path normalisation, the current directory, the path walk, directory
      entries
   5. open / read / write / seek / close
   6. mkdir / rmdir / remove / rename / readdir / stat / chdir / getcwd
   7. gets / puts / load / save / strerror

   *Done.*
4. **Cross-implementation, persistence and crash tests.** *Done*, in
   `tests/test_fs.py`.
5. **`user/files.c`**, a file browser: the arrow keys move around, Enter goes
   into a directory or opens a text file on screen, Backspace goes to `..`,
   and `n` lets you type a note and save it. Plus the docs.
6. **HDD DMA commands** and the library's fast path (§9), then a new
   measurement.

---

## 14. Decision log

Settled with the project owner on 2026-09-11.

| # | Question | Answer |
|---|---|---|
| 1 | Where the logic lives | C library + host tool |
| 2 | Directory model | Nested directories |
| 3 | Which disk | The channel is an argument; several can be mounted at once |
| 4 | Naming a volume in a path | Channel prefix, `2:/…` |
| 5 | Protecting the boot disk | Library guard only |
| 6 | API | fd API plus whole-file helpers |
| 7 | Disk image location | `disks/`, outside `build/` |
| 8 | Timestamps | None; `mtime` reserved |
| 9 | Block copy path | Window copy first, DMA as a later phase |
| 10 | Default image size | 4 MiB |
| 11 | Formatting | "The HDD should live until reformatting": persistent, never formatted implicitly, and reformatting needs force |
| 12 | Names | 31 bytes, case-sensitive, is enough |
| 13 | Text helpers | Yes. They are built on a string library that comes first |
| 14 | Current directory | Yes: `fs_chdir`, `fs_getcwd`, relative paths, `.` and `..` |

No open questions remain.
