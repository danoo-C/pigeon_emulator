# A CD drive: removable discs the host picks

> **Status: phases 0 to 4 are done** — the device
> (`emulator/devices/cd.py`, `tests/test_cd.py`), read-only volumes in
> `fs.c` (`FS_EROFS`), the HTTP surface with its config, and both front
> ends. What is left is the guest library, `<pigeon/cd.h>`, and the demo. **Every decision is
> settled**; the log is in [§15](#15-decision-log).
>
> Phase 0 **measured the two claims §3 and §5 rest on**, and both held: a
> PigeonFS disc mounts on channel 6 with `fs.c` untouched, and a write to it
> silently reports success. See §3.4. Anything else numeric here is still
> reasoned rather than run, and marked *unverified*.

| Decision | Choice |
|---|---|
| What the **device** serves | **Raw bytes.** A flat, read-only byte range. It knows nothing about filesystems |
| What the **library** does with them | `<pigeon/cd.h>` is **a layer on top of `<pigeon/fs.h>`**: it detects a PigeonFS disc, and `cd_save()` copies a raw one onto the main disk in a single call |
| Command numbering | **Compatible with `hdd.py`** for 0–5, CD-specific commands at 8. This is what makes `fs_mount(CH_CD)` work with no change to `fs.c` |
| Which channel | **6**, always registered, so "no disc" and "no drive" are different answers. The library takes a channel, so a second drive on 7 costs nothing |
| Write access | **None.** There is no write path in the device, and `fs.c` learns to say `FS_EROFS` instead of pretending a write worked |
| Picking a file | **Three buttons in both front ends**: *Load from server*, *Load from PC*, *Eject* |
| Reachable paths | Anything under the **repo root**, by default |
| Demo | `user/disc.c`: check the drive, list the tree if it is a filesystem, hex-dump it if it is not, copy it either way |

---

## 1. What this is

A CD drive is `hdd.py` with three differences, and they are the whole device:

- **It can be empty.** `HDD` is bound to one host file at construction and
  keeps it for the life of the `Machine`. A disc is swapped while the machine
  runs, from outside the machine.
- **It is read-only.** Not by a flag — by having no write path at all.
- **It knows what it is holding.** A disc reports a size and a name, because
  the guest's reason for reading it is usually to write a copy somewhere with
  that name.

**The device serves raw bytes and nothing else.** It does not know what
PigeonFS is, it never parses a disc, and it never wraps a bare file in a
synthetic image. That was decided first and nothing since has changed it.

**The library is where a disc becomes more than bytes.** `<pigeon/cd.h>` sits
on `<pigeon/fs.h>`, so a disc that happens to carry a PigeonFS image is a
volume you can walk, and a disc that is a bare `.bin` is bytes you can copy.
The guest asks `cd_has_fs()` and branches.

Those two facts are not in tension, and keeping them apart is the point: the
device stays the size of `hdd.py`, and everything that has to know about
superblocks lives in guest code that already knows about superblocks.

---

## 2. The channel

`CH_CD = 6` in `emulator/memory_map.py`, next to the five already there. 7
stays free — deliberately, because the library takes a channel argument and a
second drive should be a one-line change.

The device is registered by `Machine.__init__` **unconditionally**, like HID
and the timer, not conditionally the way `CH_USERPROG` is. An unregistered
channel makes `IOController.update()` write `0xFFFFFFFF` to `RETURN_DATA` and
log a warning, which is the right answer for a machine with no drive and the
wrong answer for a drive with no disc. Keeping the device always present lets
the guest tell those apart, and `<pigeon/cd.h>` uses exactly that to keep
running on a machine built before this device existed (§9).

---

## 3. The commands

**The numbering is not free.** `fs.c` talks to a disk with four commands —
`GET_SIZE = 1`, `READ = 2`, `WRITE = 3`, `FLUSH = 5` (`lib/pigeon/fs.c:50`) —
and `__fs_disk_blocks()` refuses only channels 0, `CH_HID`, `CH_TIMER` and
`CH_DISPLAY` before probing. **Channel 6 is already allowed through.** So a
CD that answers 1 and 2 the way a disk does is a disk as far as `fs.c` is
concerned, and `fs_mount(CH_CD)` works with no change to the library at all.

Revision 1 of this document numbered the CD's own commands 1, 2, 3 — which
would have had `fs.c` send `READ` and receive `INFO`, and `GET_SIZE` and
receive a status word. That is why the CD-specific commands now start at 8,
above everything `hdd.py` uses.

Following the house rule that `LENGTH` is always the size of the **payload**
and nothing else — the rule `display_io.py` documents at length, because a
`LENGTH` that means anything else makes `IOController` allocate it.

| cmd | name | R/W | ADDRESS | LENGTH | reply |
|---|---|---|---|---|---|
| 0 | `NOP` | 0 | — | n | n zero bytes |
| 1 | `GET_SIZE` | 0 | — | 8 | the disc size, LE, 8 bytes. **0 when empty** |
| 2 | `READ` | 0 | byte offset | n | n bytes, **short** at the end of the disc |
| 3 | `WRITE` | 1 | — | — | **refused**: 0 bytes, nothing written |
| 4 | `TRUNCATE` | — | — | — | refused: 0 bytes |
| 5 | `FLUSH` | 0 | — | 0 | 0 bytes — a no-op that succeeds |
| 8 | `MEDIA` | 0 | — | 48 | `magic`, `present`, `generation`, `size`, `name[32]` |

0–5 are `hdd.py`'s numbers and mean what they mean there, so anything that can
already drive a disk can drive this. 3 and 4 exist only to be refused: a
device that silently ignored an unknown command would leave `fs.c` unable to
tell a refusal from a device that had never heard of writing.

`FLUSH` succeeding as a no-op is deliberate — `fs_sync()` calls it
unconditionally, and a read-only volume has nothing to flush, so failing it
would make `fs_sync()` fail for no reason.

**`READ` comes back short past the end**, exactly as `hdd.py` does; the guest
reads `IO_RETLEN` for the real count. Worth knowing while implementing:
`hdd.py`'s module docstring still claims "Reads return exactly `length` bytes
(zero-filled if past EOF)", but the zero-fill in `_read_at` is commented out
and the real behaviour is a short read. The BIOS loader and `fs.c`'s
`__fs_blk_read` both depend on the short read, so the code is right and the
docstring is stale. Copy the code, not the docstring, and fix the docstring
while you are there.

With no disc in the drive, `READ` returns **zero bytes** and `GET_SIZE`
returns 0. `fs_mount(CH_CD)` on an empty drive therefore fails with
`FS_ENODEV` through the existing path, with nothing added.

`READ` is capped at the data window (4,096 bytes — `IO_SIZE -
IO_USABLE_AFTER`). The device clamps rather than relying on `IOController`'s
truncate-and-warn, so a guest asking for more gets a short read it can act on
instead of a log line it cannot see.

### 3.1 `MEDIA`, and why it is at 8

Everything a disk cannot answer lives in one command, and its first word is a
magic number, `0x44434750` — "PGCD" in byte order, the same trick `FS__MAGIC`
uses at `fs.c:35`.

That makes it a **probe**. `hdd.py`'s fallback for an unknown command is
`b"\x00" * length`, so asking a plain disk for `MEDIA` returns 48 zero bytes
and the magic does not match. An empty channel answers `0xFFFFFFFF`. A CD
answers the magic. One test, three distinguishable answers, no new
negotiation — the same shape as the DMA detection in §9 of
[filesystem.md](filesystem.md).

`fs.c` uses this to find out that a volume is read-only (§5), and `cd.c` uses
it to find out there is a drive at all.

### 3.2 The generation counter

One 32-bit counter, starting at 0, **incremented on every insert and every
eject**. It is the only way the guest can notice that the disc it was halfway
through reading is not the disc it is reading now.

Not a "changed" flag that clears on read: a flag loses a double-swap, and two
pieces of guest code polling it race to consume it. A counter is idempotent to
read and monotonic, so any number of readers can each remember the value they
last saw.

The idiom, which belongs in the header:

> Sample `generation` before a transfer and again after. If it moved, throw
> the transfer away and start over.

A mounted volume is the sharp case: swapping a disc under a mounted
filesystem leaves `fs.c` holding a cache full of the old disc's blocks.
`cd.c` documents that you unmount before you eject, and the counter is how a
program notices you did not.

### 3.3 The name

`MEDIA` reports the **basename only**, never a host path — truncated to 31
bytes plus a NUL, with any byte outside printable ASCII replaced by `_`.

31 because `FS_NAME_MAX` is 31, so a longer name could not become a filename
anyway. Basename only because a guest has no use for `/home/you/build`, and
leaking host paths into guest memory buys nothing. The sanitising is about
NUL-safety and `disp_text()`, not about PigeonFS: a name can still be
something `fs_save()` rejects with `FS_EINVAL`, and `cd_save()` has to be
ready for that.

### 3.4 Both of these were checked, not assumed

Phase 0 ended by running the claim rather than trusting it: a 256 KiB
PigeonFS image with one file on it, inserted into the drive of a real
`Machine`, and a guest program compiled against **today's unmodified
`fs.c`**.

```
fs_mount(CH_CD)                    ->  FS_OK
fs_statvfs(CH_CD, &info).label     ->  "DISC"
fs_getcwd(...)                     ->  "6:/"      (first mount becomes current)
fs_load("/hello.txt", ...)         ->  the exact bytes written from the host
fs_save("/nope.txt", "x", 1)       ->  1          <-- reports SUCCESS
the host image afterwards          ->  byte-identical
```

The first four lines are §3 confirmed: the `hdd.py`-compatible numbering is
enough, and mounting a disc needs no change to the filesystem library at all.

The fifth is §5 confirmed, and it is the reason §5 is not optional. The disc
is safe — the device refused the write, and the host file did not change by a
byte — but `fs_save()` returned 1 as though it had written one. The guest is
told a lie it has no way to detect. That is what `FS_EROFS` is for, and phase
1 is where it lands.

---

## 4. Inside `emulator/devices/cd.py`

A small class mirroring `hdd.py`'s shape:

```
CD
  .insert(path)   -> opens "rb", reads size and name, bumps generation
  .eject()        -> closes, bumps generation
  .status()       -> for the HTTP layer
  .callback(...)  -> the IOController contract
```

**Threading.** `insert`/`eject` arrive on the uvicorn thread; `callback` runs
on the emulator thread out of `IOController.update()`. A `Lock` guards the
handle and the metadata together, exactly as `hid.py` guards its queues. One
`READ` holds the lock across its `seek` + `read`, so a single command sees one
disc or the other and never a mixture. Across two commands, the generation
counter is the guard.

**A failed insert leaves the drive alone.** If the path is missing, outside
`cd_root`, or unreadable, the endpoint returns 4xx and whatever was in the
drive stays in it. Ejecting the good disc you had because you mistyped the
next one is a surprise nobody wants.

**A disc is a live handle, not a snapshot.** The size is read at insert; the
reads go to the file. If the file shrinks underneath, reads go short, which is
already a case the guest handles.

**Path validation lives on `CD`, not in the route.** `CD.resolve(path)`
returns the resolved path or raises, and the HTTP handler is three lines
around it. That is what makes §12's decision to skip `fastapi.testclient`
honest rather than lazy: the logic worth testing is not in the route.

**Should `CD` reuse `HDD`?** No. The overlap is `seek` + `read`, about six
lines; the rest of `HDD` is the write path, `truncate`, `flush`, and creating
a missing image at `DEFAULT_SIZE` — every one of which is a thing a CD must
not do. Composition would mean holding an `HDD` and refusing most of it.

---

## 5. Read-only volumes: the one change to `fs.c`

This falls out of mounting a disc, and it is the part of this design that
touches a finished, heavily tested module. It is worth being explicit about
why it cannot be skipped.

`__fs_blk_write()` **ignores what the device returns** (`fs.c:195`). So on a
device with no write path, every write silently does nothing and every call
above it reports success: `fs_save("6:/x", …)` returns the byte count,
`fs_mkdir("6:/d")` returns `FS_OK`, and the cache happily serves back what
was never written until it is evicted. The disc is safe — the device refuses
— but the guest is lied to, which is worse than an error.

So:

- `struct __fs_volume` gains `unsigned readonly`.
- `fs_mount()` sends `MEDIA` (§3.1) once. Magic matches → `readonly = 1`.
  Anything else — zeros from a disk, `0xFFFFFFFF` from an empty channel —
  leaves it 0. One extra IO command per mount, on the order of 40
  instructions *(unverified)*.
- A new error, `FS_EROFS (-19)`, and its line in `fs_strerror()`:
  `"read-only disk"`.
- Guards returning it: `fs_format`, `fs_open` with any of
  `FS_WRITE`/`FS_CREATE`/`FS_TRUNC`, `fs_write`, `fs_mkdir`, `fs_rmdir`,
  `fs_remove`, `fs_rename`. Eight call sites.
- `__fs_flush()` skips dirty blocks on a read-only volume. Nothing should be
  able to dirty one, which is exactly why the assertion is cheap to add and
  worth having.
- `fs_sync()` is **not** guarded: `FLUSH` is a no-op that succeeds (§3).

`tests/test_fs.py` gains a read-only section, and its existing 141 tests are
the regression net for everything else.

This also means the feature is not free of risk to the filesystem module.
Phase it accordingly (§13): `fs.c` changes on its own, with its own test run,
before anything depends on it.

---

## 6. The HTTP surface

Its own FastAPI server on its own port, started from `Machine.start_servers`
alongside the other two. That is the established pattern here, and `hid.py`'s
docstring already explains why several uvicorn instances coexist: each on its
own thread with its own event loop and its own port.

```
GET  /cd/status                  -> {present, generation, name, size, path, root}
GET  /cd/list                    -> [{name, path, size}, ...]
POST /cd/insert   {path: "..."}  -> 200 status | 403 outside root | 404 missing
POST /cd/upload   (multipart)    -> saves under cd_upload_dir, then inserts
POST /cd/eject                   -> 200 status
```

**CORS.** The browser page is served from the *display* port, so the CD
server's `allow_origins` must list it — the same trap `machine.py` already
documents for HID, where `http://127.0.0.1` does not match
`http://127.0.0.1:1234` because origins compare with the port.

**Discovery.** `DisplayIO`'s `/info` already hands the page `hid_url`, set by
`Machine.start_servers`. Add `cd_url` the same way, so `config.json` stays the
single source of truth for ports and `index.html` keeps hardcoding nothing.

**`/cd/list`** walks each directory in `cd_dirs`, **non-recursively**, listing
regular non-hidden files with their sizes, sorted by name. Non-recursive keeps
it predictable: the way to expose more is to add a directory to `cd_dirs`, not
to discover that the emulator crawled your home folder. Sizes are shown
because the default `cd_dirs` includes `build/`, which holds a 128 MB
`ram.bin` — you want to see that before you click it.

**What `insert` will open.** Any path under `cd_root`, which **defaults to the
repo root**. So the whole project — `build/`, `disks/`, `cds/`, `user/` — is
reachable and nothing else is. Set `cd_root` to `null` to allow the entire
filesystem; set it to `cds` to allow almost nothing.

The honest cost of that default: *Load from PC* opens a real file dialog, and
a file on your Desktop is outside the repo, so inserting it returns 403. The
dialog therefore **opens at `cd_root` and is restricted to it**, and the 403
message names the config key rather than just failing. If you find yourself
hitting it often, `cd_root: null` is the setting — the servers bind
`127.0.0.1` and there is no write path, so the exposure is read-only, local,
and one file at a time.

---

## 7. The front ends

Both get the same three buttons and a label showing what is in the drive,
since a raw disc gives no feedback anywhere else.

### 7.1 pygame

`display/display.py` already has a `Button` class and a `_build_buttons()`
with *Clear*, *-* and *+*, so this is three more entries:

```
[ Clear ] [ - ] [ + ]   [ Load from server ] [ Load from PC ] [ Eject ]   demo.bin
```

- **Load from server** draws an **overlay list inside the pygame window** —
  the filenames from `/cd/list`, arrow keys and click to choose, Esc to
  cancel. Drawn rather than delegated so this path depends on nothing but
  pygame.
- **Load from PC** opens a real folder browser with `tkinter.filedialog`,
  rooted at `cd_root`, and POSTs the chosen path to `/cd/insert`.
- **Eject** POSTs `/cd/eject`, and is greyed out when the drive is empty.

**tkinter is the one new dependency, and it is not installed here.** `import
tkinter` raises `ModuleNotFoundError` in this `.venv`; on Linux it is a system
package (`python3-tk`), not a pip one, so it cannot go in
`requirements-client.txt`. It is accepted as an **optional** dependency:
import it lazily inside the click handler, and if it is missing, put
"Load from PC needs python3-tk" in the status label **when it is clicked**.
The button is not disabled up front: finding out would mean importing
tkinter at startup, which is exactly the import this is avoiding, and a
button that explains itself on click is clearer than one that is grey for no
visible reason. Losing that one button must not take the client with it, and
*Load from server* keeps working because it never touches tkinter.

**The bar is laid out from the font, not from typed positions.** The first
three buttons ended at 175 px, with the `px:` label at 185 and the status
text at 280, and the window's minimum width was 260 px — which at
`pixel_size = 1`, a 192 px framebuffer, already cut the bar off after `+`.
Measured with `SysFont(None, 22)`: the three CD buttons span **324 px** (this
section had estimated 320), *Eject* ends at 564 px, and the minimum window
width is now **830 px** — every button plus a disc label of ordinary length.
No second row was needed.

### 7.2 The browser

`index.html`'s `#controls` div gains the same three, plus a status span:

```html
<button id="cd-server">Load from server</button>
<button id="cd-pc">Load from PC</button>
<button id="cd-eject">Eject</button>
<span id="cd-status">no disc</span>
```

- **Load from server** fetches `/cd/list` and reveals a `<select>`; picking
  a row inserts it and hides the list again. An empty listing says so
  rather than showing an empty box.
- **Load from PC** is a hidden `<input type="file">`. A browser cannot hand
  over a path: `input.files[0]` is bytes with a name and nothing else. So this
  button **uploads**, and the server writes the bytes down before inserting
  them. That asymmetry with pygame is not a design choice, it is the sandbox,
  and it belongs in a comment in `index.html` so nobody "fixes" it later.

The page **polls `/cd/status` every two seconds**. It is not the only thing
that can work the drive — the pygame client drives the same device — and
without the poll the two front ends disagree about what is in it until you
touch a button. That costs one request every two seconds next to a frame
fetch at 30 FPS, and it pauses while the tab is hidden.

An emulator with no drive serves no `cd_url`; the page then disables the
three buttons and says so, rather than failing on the first click.

Uploads land in `cd_upload_dir` (default `cds/`) under the file's own
basename, overwriting. So an uploaded disc **appears in `/cd/list`
afterwards** — upload once, re-insert forever — and the folder grows until you
clean it out, which is the accepted trade. `cd_max_upload` (default 64 MiB)
caps it, because this is the one path that writes to the host disk -- checked
against `Content-Length` before the body is read, and again against what
actually arrived.

**Keep `cd_upload_dir` inside `cd_dirs`.** "It stays in the picker" is only
true while it is, the two are set independently, and nothing else would
notice -- so `tests/test_cd.py` asserts that the shipped config keeps them
agreeing.

**Not multipart.** `UploadFile` needs `python-multipart`, which is not in
`requirements.txt`. The upload is a raw body with the name in a query
parameter, which is also less work in the browser: `fetch(url, {method:
'POST', body: file})` posts the `File` object directly. The name is reduced
to a basename on **both** separators, because it arrives over HTTP from
whatever the client is running and `\` is an ordinary character to a POSIX
`Path`.

---

## 8. Config

```json
"cd_port": 8002,
"cd_root": ".",
"cd_dirs": ["cds", "build"],
"cd_upload_dir": "cds",
"cd_max_upload": "64M"
```

`cd_root` is relative to the repo root like every other path in
`config.json`, so `"."` *is* the repo root; `null` means anywhere.

`cds/` is created on demand and goes in `.gitignore`, next to `disks/` and for
the same reason: it holds what you put there, not what the build makes. The
repo's own `config.json` uses 1234/1235 rather than the 8000/8001 defaults, so
it gets `"cd_port": 1236`.

---

## 9. The guest library: `<pigeon/cd.h>`

```c
#define CD_OK        0
#define CD_ENODISC (-1)   /* the drive is empty                */
#define CD_ENODEV  (-2)   /* no CD drive on this channel       */
#define CD_EINVAL  (-3)
#define CD_EIO     (-4)   /* a read came back short mid-disc   */

#define CD_NAME_MAX 31

typedef struct {
    unsigned present;
    unsigned generation;
    unsigned size;
    char     name[32];
} cd_info_t;

int      cd_info (unsigned channel, cd_info_t *out);
int      cd_present(unsigned channel);
unsigned cd_generation(unsigned channel);
int      cd_read (unsigned channel, unsigned offset, void *buf, unsigned n);

int      cd_has_fs(unsigned channel);          /* 1 if the disc carries PigeonFS */
int      cd_label (unsigned channel, char *out, unsigned size);
int      cd_save  (unsigned channel, char *path);   /* the whole disc -> a file */
```

**Every call takes a channel**, the way `fs_mount` does, so a second drive on
channel 7 needs no new API. `CH_CD` is the one to pass.

**`int`, not `bool`.** This compiler has no `bool`, no `stdbool.h` and no
`enum` (`filesystem.md` §6.7), so `cd_has_fs()` returns 1 or 0 like
`key_down()` does.

**`cd_read` loops internally**, like `fs_read`, so a caller can ask for more
than the 4 KB window and get it. It returns the byte count, short at the end
of the disc, 0 past it.

**`CD_ENODEV` is detected, not assumed** — the `MEDIA` probe of §3.1. One
`cd.c` runs on a machine with a drive and on one without.

**`cd_has_fs()` does not mount.** It reads four bytes at offset 0 and compares
them to `0x53464750` ("PGFS", `fs.c:35`). Mounting to find out would take a
volume slot, make the disc the current volume if it is the first one mounted,
and have to be undone — for a question answered by one read. `cd_label()` is
the same read, 16 bytes at offset 36.

**`cd_save()` is why `cd.c` includes `<pigeon/fs.h>`.** It opens `path` on the
current volume with `FS_WRITE | FS_CREATE | FS_TRUNC`, copies the disc through
a 512-byte buffer, closes, and returns the byte count. Passing `NULL` uses the
disc's own name.

The cost is real and is accepted: `libraries_for()` follows includes
transitively, so **every program that includes `<pigeon/cd.h>` also compiles
`fs.c`** — about 99 KB, per `filesystem.md` §7. A program whose only job is to
read a disc raw pays for a filesystem it never calls. The trade is that the
thing you actually want to do is one line instead of eight, and that a disc
carrying a filesystem is a volume rather than a puzzle.

For a disc that **is** a filesystem, `cd_save()` is the wrong tool — it would
copy the image, not its contents. That case is `fs_mount(CH_CD)` and an
ordinary file-by-file copy between two volumes, which `fs.c` already supports
and nothing has yet used.

---

## 10. The demo: `user/disc.c`

One screen, one key to check the drive, and it tells you what you have:

```
PIGEON DISC                [PGFS]     <- or [raw]
  /
    doom/
    readme.txt      1.2 K
  c  copy to 2:/      e  eject
```

- **No disc** → say so, and keep polling `cd_generation()` so inserting one
  from the host updates the screen without a keypress.
- **A PigeonFS disc** → `fs_mount(CH_CD)` and list the tree, two levels deep.
  `c` copies it file by file onto `2:/`.
- **A raw disc** → say `raw data`, show the size and name, and `c` calls
  `cd_save()`.

**Named `disc.c`, not `cd.c`.** You asked for "an extremely simple cd.c
program", and this is that program — but `lib/pigeon/cd.c` already exists, and
`compile_units` compiles a program and its libraries as one translation unit.
Two files called `cd.c` in one build is legal and confusing in equal measure:
the launcher would list the program as "cd", and the generated assembly would
carry both. Cosmetic, so overrule it if you disagree.

**On "convert to text?"** — you floated it, and the answer is no, with
evidence from the program next door. `user/files.c` already has
`looks_like_text()` and uses it to *refuse* to display a non-text file,
because a `.bin` rendered through the 5×7 font is a screen of noise; that
check exists precisely because the alternative was tried and was useless. So a
raw disc gets a **hex dump** instead, which is what raw bytes are actually
read with — and it fits: four columns of offset, six bytes as hex pairs, six
as ASCII is 29 of the 32 columns. The one nuance worth keeping: if a raw disc
*does* pass `looks_like_text()`, show it as text, because then it probably is
a text file that simply has no filesystem around it.

---

## 11. Changes outside `cd.py`

| File | Change |
|---|---|
| `emulator/devices/cd.py` | new |
| `emulator/memory_map.py` | `CH_CD = 6` |
| `emulator/machine.py` | construct and register the device; start its server; add `cd_url` |
| `emulator/devices/display_io.py` | `/info` gains `cd_url` |
| `emulator/devices/hdd.py` | fix the stale "zero-filled if past EOF" docstring (§3) |
| `emulator/config.py`, `config.json` | the five keys in §8 |
| `emulator/cli.py` | `--cd-port`, and print the CD URL next to Display and HID |
| `lib/pigeon/fs.c`, `lib/pigeon/fs.h` | `FS_EROFS`, the `readonly` flag, eight guards (§5) |
| `display/display.py` | three buttons, the overlay list, the tkinter dialog, the status label, a minimum window width |
| `display/index.html` | three buttons, the file input, the status span |
| `lib/pigeon/cd.h`, `lib/pigeon/cd.c` | new |
| `user/disc.c` | new |
| `tests/test_cd.py` | new |
| `tests/test_fs.py` | a read-only section |
| `README.md`, `lib/README.md`, `docs/filesystem.md` | the device, the library, the IO bus table, the layout, `cds/`, `FS_EROFS` |
| `.gitignore` | `cds/` |

Nothing in the BIOS, the IO controller, the assembler or the compiler.

---

## 12. Testing

The repo's device tests call `callback()` directly and never start a server —
`tests/test_display.py` builds a bare `DisplayIO(RAM(...))` and pokes it. Same
here.

**The HTTP routes are not tested directly.** That would need
`fastapi.testclient`, and therefore `httpx`, a test-only dependency this repo
does not have. You left the choice to me, so: no. The condition that makes it
defensible is in §4 — path resolution, listing and the insert/eject state
machine all live on `CD`, and the routes are three-line wrappers around them.
If a route ever grows a decision of its own, that is the moment to revisit.

**The device**, with no server and no machine:

| Area | Cases |
|---|---|
| Empty drive | `GET_SIZE` is 0; `READ` returns 0 bytes; `MEDIA` says `present = 0` but still carries the magic |
| Insert | size and basename are reported; `READ` at 0 and at an offset returns the file's bytes |
| Short reads | a read crossing the end returns only what exists; wholly past it, 0 bytes; more than the window is clamped |
| Eject | back to empty, and the handle is really closed |
| Generation | up on every insert **and** every eject, never repeats, moves even when the same path is inserted twice |
| Read-only | `WRITE`, `TRUNCATE` and every command with `read_write = 1` return 0 bytes, and the host file is byte-identical afterwards |
| `MEDIA` as a probe | the magic matches on a CD; `HDD`'s answer to command 8 is 48 zero bytes; an empty channel answers `0xFFFFFFFF` |
| Names | a 40-byte basename truncates to 31; a control byte comes back sanitised; a host path never appears |
| Paths | `insert` outside `cd_root` is refused; a symlink out of it is refused after resolution; a missing path leaves the current disc in place |

**`fs.c`'s read-only volumes**, in `tests/test_fs.py`'s existing harness, with
a CD device on channel 6:

| Area | Cases |
|---|---|
| Mount | a PigeonFS image in the drive mounts, and `fs_statvfs` reports it |
| Reading | open, read, seek, `readdir`, `stat`, `getcwd` all work on `6:/` |
| Refusals | `fs_format`, `fs_open(FS_WRITE)`, `fs_mkdir`, `fs_rmdir`, `fs_remove`, `fs_rename` each return `FS_EROFS`, **and the image is byte-identical afterwards** |
| Two volumes | `2:/x` and `6:/x` are different files; a copy from one to the other lands on the writable one |
| Regression | the existing 141 tests still pass — a writable disk must not have become read-only |

**The guest library**, the way `tests/test_libs.py` does it: compile a C
program with `cd.c`, run it on a `Machine` whose drive holds a temporary file,
check what `main` returns. The one that matters is the round trip — a known
pattern on the disc, read back through `cd_read` in pieces that do not divide
the size evenly, compared byte for byte in the guest. Plus `cd_has_fs()`
answering correctly for a `.bin`, for a PigeonFS image, and for an empty
drive; and `cd_save()` landing a byte-identical copy on `2:/`, verified from
the host with `tools/pfs.py`.

---

## 13. Phases

Each one runs and is testable before the next.

0. **`cd.py` and its tests.** No server, no front end, no library.
   `tests/test_cd.py` drives `callback()` directly. This is where the
   `hdd.py`-compatible numbering gets proved, including the `MEDIA` probe
   against a real `HDD`. ***Done*** — 40 tests, plus the end-to-end check in
   §3.4. The device is registered on every `Machine`, and `hdd.py`'s stale
   docstring is fixed.
1. **`fs.c`: read-only volumes.** On its own, with its own run of
   `tests/test_fs.py`, because it is the only part of this that can break
   something that already works. ***Done*** — `FS_EROFS`, a `readonly` flag
   on the volume set by the `MEDIA` probe at mount, eight guards, and the
   flush skip. 13 tests, and the 51 that were already there still pass.
   Mutating the probe to answer "read-only" for everything fails 40 of the
   64, which is the regression that would matter.
2. **The HTTP layer**: the server, the five endpoints, `cd_url` on `/info`,
   the config keys, the CLI flag. ***Done***, and driven against a live
   uvicorn once to prove the whole path: insert by name, 404 on a missing
   path, 403 outside `cd_root` with the disc that was in still in, an
   upload the guest could read through the bus on the next command, 413
   over the limit, and eject.
3. **The browser front end.** Before pygame, because it needs no new
   dependency and so proves the endpoints end to end before tkinter is in the
   picture. ***Done***, and it did earn that place in the order: the page's
   own logic was run against a live server with a stubbed DOM — 14 checks,
   including that a 403 and a 413 reach the user as the server's own words
   rather than a bare status code. That harness needs node, which is not a
   dependency here, so what is committed is the static agreement check
   `tests/test_input.py` already makes for the keycode table: every `/cd/`
   path the page calls must be one `cd.py` serves, and every `cd-` element
   it looks up must be one the markup declares.
4. **The pygame front end**: the overlay list, then the tkinter dialog with
   its graceful absence, then the button-bar width. ***Done***. The real
   client was driven headlessly (`SDL_VIDEODRIVER=dummy`) against live
   servers — 22 checks: the picker lists, navigates by key and by click, and
   inserts; Esc and a click outside cancel; eject tracks the drive; a 403
   reaches the bar as the server's own words; and with tkinter absent, as it
   is on this machine, *Load from PC* names `python3-tk` and nothing else
   breaks. What is committed is the part with the client's own decisions in
   it, built with `object.__new__` so no server is needed: nothing reaches
   the guest while the picker is open, opening it releases held keys,
   navigation clamps, the bar never overlaps at pixel size 1 or 16, and
   `tkinter` is never imported at module level.
5. **`<pigeon/cd.h>`** and its tests on the emulator.
6. **`user/disc.c`** and the docs.

---

## 14. What this does not do

Stated so it does not have to be rediscovered:

- **It does not run a disc.** Copying a `.bin` onto `2:/` is not executing it,
  and executing it is blocked by things this device does not touch: every
  program is compiled `.ORG PROGRAM_LOAD_ADDR` with absolute addresses, so a
  loader would have to overwrite itself. A CD drive makes that problem
  *reachable*, not solved.
- **It does not notify.** There are no interrupts on this bus, so a guest
  learns a disc arrived by polling. One IO command, but a program that never
  polls never notices.
- **It does not protect a mounted disc from being ejected.** The host can
  eject at any moment; `fs.c` will be holding a cache of blocks that are no
  longer there. The generation counter lets a program detect it. Nothing
  prevents it.

---

## 15. Decision log

1–4 settled 2026-09-12, 5–12 on 2026-09-13.

1. The **device serves raw bytes** to the program running in the emulator — no
   filesystem in the device, no synthetic image wrapping a bare file.
2. **Two buttons** for picking, *Load from server* and *Load from PC*, rather
   than one mechanism.
3. **Read-only**, like a real CD.
4. **A `cd` library**, and `user/files.c` is left alone.
5. **Eject is a third button** in both front ends.
6. **tkinter is accepted** as an optional dependency for *Load from PC* in
   pygame, degrading to a disabled button where it is missing — as it is on
   this machine today.
7. **`cd.c` is a layer on `fs.c`**: `cd_save()` makes copying one call, and
   `cd_has_fs()` tells you whether the disc is a filesystem. Every `cd.h` user
   pays the ~99 KB of `fs.c`, and that is accepted.
8. **A disc may carry a PigeonFS image**, mounted read-only on its channel.
   Which forces 9 and 10.
9. **CD commands are `hdd.py`-compatible** for 0–5, with CD-specific commands
   at 8, so `fs_mount(CH_CD)` needs no change to `fs.c`.
10. **`fs.c` gains `FS_EROFS`** and a per-volume read-only flag, because
    otherwise a write to a disc silently appears to succeed.
11. **Uploads land in `cds/<original name>`**, overwriting, and stay visible in
    the server list.
12. **`cd_root` defaults to the repo root** — anything in the project is
    insertable, nothing outside it is.
13. **The HTTP routes are not unit-tested** (left to me): path and state logic
    lives on `CD`, which is tested, and the routes stay thin enough that this
    holds.

---

## 16. Two things I decided rather than asked

Both are small and both are reversible — say so and they change.

1. **The demo is `user/disc.c`, not `user/cd.c`**, because `lib/pigeon/cd.c`
   already exists and they would be compiled into the same translation unit
   (§10).
2. **A raw disc is shown as a hex dump, not as text** (§10), because
   `user/files.c` already refuses to render non-text files for exactly that
   reason — with the exception that a raw disc which passes its
   `looks_like_text()` check is shown as text after all.
