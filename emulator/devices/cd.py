"""CD -- a removable, read-only disc the host puts in and takes out.

The design, with the reasoning for every choice here, is in
docs/cd-drive.md. Phases 0 and 1 of it: the device, and the HTTP surface
the front ends put discs in through.

It is hdd.py with three differences, and they are the whole device:

  - It can be EMPTY. An HDD is bound to one host file for the life of the
    Machine; a disc is swapped while the machine runs, from outside it.
  - It is READ-ONLY. Not by a flag -- by having no write path at all, so
    a file you picked off your own disk cannot be damaged by a guest bug.
  - It knows WHAT IT IS HOLDING. A disc reports a size and a name,
    because the guest's reason for reading one is usually to write a copy
    somewhere with that name.

It serves raw bytes and knows nothing about filesystems. Whether a disc
carries a PigeonFS image is a question for <pigeon/cd.h>, which sits on
<pigeon/fs.h> and can answer it; this file never parses a disc.

  callback(read_write, command, length, address, data)

  cmd  name      R/W  ADDRESS      LENGTH  reply
  0    NOP        0   --           n       n zero bytes
  1    GET_SIZE   0   --           8       the size, LE, 8 bytes; 0 when empty
  2    READ       0   byte offset  n       n bytes, SHORT at the end of the disc
  3    WRITE      1   --           --      refused: 0 bytes, nothing written
  4    TRUNCATE   --  --           --      refused: 0 bytes
  5    FLUSH      0   --           0       0 bytes -- a no-op that succeeds
  8    MEDIA      0   --           48      magic, present, generation, size, name
  9    EJECT      0   --           8       ejected (1, or 0 if already empty), generation

THE NUMBERING IS NOT FREE. lib/pigeon/fs.c drives a disk with GET_SIZE=1,
READ=2, WRITE=3 and FLUSH=5, and its __fs_disk_blocks() refuses only
channels 0, HID, TIMER and DISPLAY before probing -- channel 6 already
passes. So a CD that answers 1 and 2 the way a disk does IS a disk as far
as the filesystem is concerned, and fs_mount(CH_CD) works with no change
to that library. An earlier draft of the design numbered this device's
own commands 1, 2 and 3; fs_mount would have sent READ and been handed
the media status. Everything CD-specific therefore starts at 8, above
every number hdd.py uses.

EJECT is the guest's own hand on the drive. The host's Eject buttons were
first meant to be the only way a disc came out; the owner of the project
wanted full control from inside the machine as well. It is sent with
R/W = 0 because it carries no payload, which keeps the rule below exactly
as it was: anything in the write direction is refused, EJECT included, and
ejecting closes a handle without ever touching the file behind it. The
reply is taken under the same lock as the eject, so the guest learns
whether there WAS a disc without a check-then-eject the host could race.
The device knows nothing about guest mounts -- <pigeon/cd.h>'s cd_eject()
unmounts a mounted disc first; a program that fires the raw command gets
the same hazard a host eject has.

3 and 4 exist only to be refused. A device that silently ignored an
unknown command would leave fs.c unable to tell a refusal from a device
that had never heard of writing -- which is exactly what fs.c needs to
know to report FS_EROFS instead of pretending the write worked.
"""

import logging
import struct
from pathlib import Path
from threading import Lock, Thread
from typing import Optional

from ..memory_map import IO_SIZE, IOHeader

log = logging.getLogger(__name__)

# devices/ -> emulator/ -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

# hdd.py's numbers, and they have to stay hdd.py's numbers. See above.
CMD_NOP = 0
CMD_GET_SIZE = 1
CMD_READ = 2
CMD_WRITE = 3
CMD_TRUNCATE = 4
CMD_FLUSH = 5
# CD-specific, above everything hdd.py uses.
CMD_MEDIA = 8
CMD_EJECT = 9
EJECT_BYTES = 8

#: "PGCD" in byte order, the same convention as fs.c's FS__MAGIC.
#:
#: This makes MEDIA a PROBE as well as a query. hdd.py answers an unknown
#: command with `length` zero bytes, so a plain disk replies to command 8
#: with zeros and the magic does not match; an empty channel is answered
#: by the controller with 0xFFFFFFFF; a CD replies with this. One test,
#: three distinguishable answers, no negotiation.
MEDIA_MAGIC = 0x44434750
MEDIA_BYTES = 48

# FS_NAME_MAX is 31, so a longer name could not become a guest filename
# anyway -- see _disc_name().
NAME_MAX = 31
NAME_FIELD = 32

# One READ can move at most the data window, the same ceiling every other
# device works to. Clamped here rather than left to IOController's
# truncate-and-warn, so a guest asking for more gets a short read it can
# act on instead of a log line it cannot see.
WINDOW = IO_SIZE - IOHeader.USABLE_AFTER

#: Where /cd/list looks, relative to the repo root. config.json overrides
#: all four of these; the defaults are what a Machine built without a
#: Config gets.
DEFAULT_DIRS = ("cds", "build")
DEFAULT_UPLOAD_DIR = "cds"
#: The only path here that writes to the host disk, so it is the only one
#: that needs a ceiling.
DEFAULT_MAX_UPLOAD = 64 * 1024 * 1024


def _disc_name(basename: str) -> str:
    """The name a guest sees for a disc: printable ASCII, at most 31 bytes.

    Never a host path -- a guest has no use for /home/you/build, and
    leaking host paths into guest memory buys nothing. 31 bytes because
    FS_NAME_MAX is 31, so anything longer could not become a filename.

    The sanitising is about NUL-safety and disp_text(), NOT about
    PigeonFS: the result can still be something fs_save() rejects with
    FS_EINVAL, and cd_save() has to be ready for that.
    """
    cleaned = "".join(c if 0x20 <= ord(c) <= 0x7E else "_" for c in basename)
    return cleaned[:NAME_MAX]


class CD:
    """A CD drive. Empty until something is put in it."""

    def __init__(self, root=REPO_ROOT, dirs=DEFAULT_DIRS,
                 upload_dir=DEFAULT_UPLOAD_DIR, max_upload=DEFAULT_MAX_UPLOAD):
        # insert/eject arrive on the HTTP thread once phase 2 lands;
        # callback() runs on the emulator thread out of
        # IOController.update(). One lock over the handle and the
        # metadata together, the way hid.py guards its queues: a single
        # READ then sees one disc or the other, never a mixture.
        self._lock = Lock()
        self._f = None
        self._path: Optional[Path] = None
        self._name = ""
        self._size = 0
        self._generation = 0

        #: Discs may only be inserted from under here. None means anywhere.
        self.root = None if root is None else Path(root).resolve()
        self.dirs = [Path(d) if Path(d).is_absolute() else REPO_ROOT / d
                     for d in dirs]
        self.upload_dir = (Path(upload_dir) if Path(upload_dir).is_absolute()
                           else REPO_ROOT / upload_dir)
        self.max_upload = int(max_upload)
        self._server_thread = None

    # --- the drive ---------------------------------------------------------

    def resolve(self, path) -> Path:
        """The host path a disc may be inserted from, or an exception.

        Kept on the device rather than in the HTTP route on purpose: it
        is the part worth testing, and tests/test_cd.py can reach it
        without starting a server.

        Symlinks are followed BEFORE the check, so a link inside the root
        pointing outside it is outside it.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        candidate = candidate.resolve()
        if self.root is not None and not candidate.is_relative_to(self.root):
            raise PermissionError(
                f"{candidate} is outside {self.root}; set cd_root to null in "
                f"config.json to allow discs from anywhere")
        return candidate

    def insert(self, path):
        """Put a disc in. Raises rather than ejecting what is already in.

        The handle is opened before the swap so that a missing or
        unreadable file leaves the drive exactly as it was. Ejecting the
        good disc you had because you mistyped the next one is a surprise
        nobody wants.
        """
        target = self.resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"{target}: not a file")
        handle = open(target, "rb")
        size = target.stat().st_size

        with self._lock:
            if self._f is not None:
                self._f.close()
            self._f = handle
            self._path = target
            self._size = size
            self._name = _disc_name(target.name)
            self._bump()
        log.info("CD: inserted %s (%d bytes)", target, size)
        return self.status()

    def save_upload(self, name, data):
        """Write an uploaded disc down, then insert it.

        A browser cannot hand over a path -- input.files[0] is bytes with
        a name and nothing else -- so this is the only way the browser
        front end can put a disc in. It is not an asymmetry anyone chose;
        it is the sandbox.

        The bytes land in upload_dir under their own basename,
        overwriting, which means an uploaded disc turns up in
        list_discs() afterwards: upload once, re-insert forever. The
        folder grows until someone empties it.
        """
        # Both separators, because this name arrives over HTTP from
        # whatever the client happens to be running. A browser only ever
        # sends a basename, but "C:\\Users\\me\\d.bin" is not a basename
        # to a POSIX Path -- the backslash is an ordinary character here,
        # so the whole string would become one very strange file name.
        safe = Path(str(name).replace("\\", "/")).name
        if not safe or safe in (".", ".."):
            raise ValueError(f"{name!r} is not a usable file name")
        if len(data) > self.max_upload:
            raise ValueError(f"{len(data)} bytes is over the {self.max_upload}-byte "
                             f"limit; raise cd_max_upload in config.json")
        # Checked BEFORE anything is written: an upload_dir outside
        # cd_root is refused like any other path, and refusing it after
        # the write would leave the file behind anyway.
        target = self.resolve(self.upload_dir / safe)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return self.insert(target)

    def eject(self):
        """Take the disc out. Empty already is a no-op, generation included."""
        with self._lock:
            ejected = self._eject_locked()
            status = self._status_locked()
        if ejected:
            log.info("CD: ejected")
        return status

    def _eject_locked(self):
        """Call with the lock held. True if there was a disc to take out.

        Shared by the host's eject() and the guest's EJECT, so the two
        cannot drift: an empty drive is left alone and its generation does
        not move, whoever asked."""
        if self._f is None:
            return False
        self._f.close()
        self._f = None
        self._path = None
        self._name = ""
        self._size = 0
        self._bump()
        return True

    def close(self):
        self.eject()

    def _bump(self):
        """Call with the lock held, and only when the drive's contents change.

        The counter answers "is what I am reading still what I was
        reading", so it must not move when nothing moved -- an eject of
        an empty drive changes nothing. Re-inserting the SAME path does
        bump it: that is a fresh handle on a file that may have been
        rewritten underneath.

        Not a "changed" flag that clears on read: a flag loses a
        double-swap, and two pieces of guest code polling it race to
        consume it.
        """
        self._generation = (self._generation + 1) & 0xFFFFFFFF

    # --- what the host asks ------------------------------------------------

    def _status_locked(self):
        return {
            "present": self._f is not None,
            "generation": self._generation,
            "name": self._name,
            "size": self._size,
            "path": str(self._path) if self._path is not None else None,
            # So a front end's file dialog can open where discs are
            # actually accepted from, instead of guessing and offering
            # the user paths that will come back 403.
            "root": str(self.root) if self.root is not None else None,
        }

    def status(self):
        with self._lock:
            return self._status_locked()

    def list_discs(self):
        """What /cd/list will show: the files in `dirs`, non-recursively.

        Non-recursive keeps it predictable -- the way to expose more is to
        add a directory, not to discover that the emulator crawled your
        home folder. Sizes are included because the default dirs include
        build/, which holds a 128 MB ram.bin you want to recognise before
        you click it.
        """
        found = []
        seen = set()
        for directory in self.dirs:
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue                      # a missing cds/ is not an error
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    resolved = entry.resolve()
                    size = entry.stat().st_size
                except OSError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                found.append({"name": entry.name, "path": str(resolved),
                              "size": size})
        return found

    # --- the HTTP surface ---------------------------------------------------

    def start_fastapi(self, host: str = "127.0.0.1", port: int = 8002,
                      allow_origins=None):
        """Serve the endpoints the front ends put discs in through.

        Its own port, on its own daemon thread, exactly like HID: several
        uvicorn instances coexist happily as long as each has its own
        thread (so its own event loop) and its own port.

        The routes are deliberately THIN. Every decision they make --
        which paths are allowed, what a listing contains, what an upload
        is called -- lives on CD above, where tests/test_cd.py can reach
        it without a network. That is what makes it defensible not to
        pull in fastapi.testclient (and httpx) to test the routes
        themselves; if a route ever grows a decision of its own, that is
        the moment to revisit it.
        """
        if self._server_thread is not None and self._server_thread.is_alive():
            return

        # Checked on the caller's thread -- an ImportError inside the
        # daemon thread below is swallowed, leaving the drive silently
        # unreachable with no explanation.
        try:
            from fastapi import FastAPI, HTTPException, Request
            from fastapi.middleware.cors import CORSMiddleware
            from pydantic import BaseModel
            import uvicorn
        except ImportError as e:
            raise RuntimeError(
                "FastAPI/uvicorn/pydantic not installed: pip install -r "
                "requirements.txt") from e

        def _run():
            app = FastAPI()
            app.add_middleware(
                CORSMiddleware,
                # The browser front end is served from the DISPLAY port,
                # so that origin has to be listed here or every POST from
                # the page fails preflight. Origins compare with the
                # port: "http://127.0.0.1" does not match
                # "http://127.0.0.1:1234".
                allow_origins=allow_origins or [f"http://{host}:{port}"],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["*"],
            )

            class Insert(BaseModel):
                path: str

            @app.get("/cd/status")
            async def cd_status():
                return self.status()

            @app.get("/cd/list")
            async def cd_list():
                return self.list_discs()

            @app.post("/cd/insert")
            async def cd_insert(body: Insert):
                try:
                    return self.insert(body.path)
                except PermissionError as e:
                    raise HTTPException(status_code=403, detail=str(e))
                except (FileNotFoundError, IsADirectoryError) as e:
                    raise HTTPException(status_code=404, detail=str(e))
                except OSError as e:
                    raise HTTPException(status_code=400, detail=str(e))

            @app.post("/cd/upload")
            async def cd_upload(request: Request, name: str = "disc.bin"):
                """A raw body, with the file name in a query parameter.

                Not multipart: UploadFile needs python-multipart, which is
                not in requirements.txt, and a raw body is less work at
                both ends -- the browser posts the File object itself.
                """
                declared = request.headers.get("content-length")
                if declared is not None and declared.isdigit():
                    if int(declared) > self.max_upload:      # before reading it
                        raise HTTPException(
                            status_code=413,
                            detail=f"{declared} bytes is over the "
                                   f"{self.max_upload}-byte limit; raise "
                                   f"cd_max_upload in config.json")
                data = await request.body()
                try:
                    return self.save_upload(name, data)
                except ValueError as e:
                    raise HTTPException(status_code=413, detail=str(e))
                except PermissionError as e:
                    raise HTTPException(status_code=403, detail=str(e))
                except OSError as e:
                    raise HTTPException(status_code=400, detail=str(e))

            @app.post("/cd/eject")
            async def cd_eject():
                return self.eject()

            uvicorn.run(app, host=host, port=port, log_level="warning")

        self._server_thread = Thread(target=_run, daemon=True)
        self._server_thread.start()

    # --- what the guest asks -----------------------------------------------

    def _read_at(self, offset: int, length: int) -> bytes:
        """Call with the lock held. SHORT past the end, never zero-filled.

        The same behaviour hdd.py really has -- its docstring's claim that
        reads are zero-filled is stale, and both the BIOS loader and
        fs.c's __fs_blk_read depend on the short read to find an end.
        """
        if length <= 0:
            return b""
        self._f.seek(offset)
        return self._f.read(length)

    def _media(self) -> bytes:
        with self._lock:
            header = struct.pack("<IIII", MEDIA_MAGIC,
                                 1 if self._f is not None else 0,
                                 self._generation, self._size)
            name = self._name.encode("ascii", "replace")[:NAME_MAX]
        return header + name.ljust(NAME_FIELD, b"\x00")

    def callback(self, read_write: int, command: int, length: int,
                 address: int, data: bytearray) -> bytes:
        """IOController-compatible callback. See the table at the top."""
        cmd = int(command)
        length = max(0, int(length) if length is not None else 0)
        addr = int(address) & 0xFFFFFFFF

        # Read-only is the absence of a write path, in one place. Anything
        # the bus can express as a write is refused before it is decoded.
        if read_write == 1:
            return b""

        if cmd == CMD_NOP:
            return b"\x00" * length

        if cmd == CMD_GET_SIZE:
            # 0 when the drive is empty, which is what makes
            # fs_mount(CH_CD) fail with FS_ENODEV through fs.c's existing
            # path, with nothing added there.
            with self._lock:
                return self._size.to_bytes(8, "little")

        if cmd == CMD_READ:
            with self._lock:
                if self._f is None:
                    return b""
                return self._read_at(addr, min(length, WINDOW))

        if cmd == CMD_MEDIA:
            return self._media()

        if cmd == CMD_EJECT:
            with self._lock:
                ejected = self._eject_locked()
                generation = self._generation
            if ejected:
                log.info("CD: ejected by the guest")
            return struct.pack("<II", 1 if ejected else 0, generation)

        if cmd in (CMD_WRITE, CMD_TRUNCATE):
            return b""

        if cmd == CMD_FLUSH:
            # A no-op that SUCCEEDS: fs_sync() calls FLUSH unconditionally,
            # and a read-only volume has nothing to flush, so failing it
            # would fail fs_sync() for no reason.
            return b""

        # Unknown command -> zeros, the same shape as hdd.py, which is what
        # makes MEDIA usable as a probe in the other direction.
        return b"\x00" * length
