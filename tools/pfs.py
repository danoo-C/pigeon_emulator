#!/usr/bin/env python3
"""PigeonFS disk images, from the host.

    python3 tools/pfs.py mkfs --size 4M --label PIGEON
    python3 tools/pfs.py put notes.txt /docs/notes.txt
    python3 tools/pfs.py ls -l /docs
    python3 tools/pfs.py fsck --repair

The format is specified in docs/filesystem.md, section 3, and this module
is its reference implementation: the guest library (lib/pigeon/fs.c) is
tested against it. So wherever the two could differ -- which block the
allocator picks, which directory slot a new entry takes, the order writes
reach the disk -- this does what the guest does, not whatever would be
easiest in Python.

Every command works on --image, which defaults to "disk" in config.json:
the image the emulator puts on IO channel 2. Don't WRITE to it while the
emulator is running -- the guest keeps the superblock and its cache in RAM
between calls and will not see the change. Reading is safe: the guest
flushes before every call returns.
"""
import argparse
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# --- the format (docs/filesystem.md, section 3) ------------------------------

BLOCK = 512
MAGIC = 0x53464750                  # "PGFS" in byte order
VERSION = 1
FAT_START = 1
FAT_PER_BLOCK = BLOCK // 4          # 128 entries in one FAT block
ENTRY = 64
ENTRIES_PER_BLOCK = BLOCK // ENTRY  # 8
ROOT_OFFSET = 64                    # the root's directory entry, inside the superblock

FREE = 0x00000000
EOC = 0xFFFFFFFF                    # end of chain
RESERVED = 0xFFFFFFFE               # the superblock and the FAT's own blocks

TYPE_EMPTY, TYPE_FILE, TYPE_DIR = 0, 1, 2

NAME_MAX = 31                       # bytes, not characters: names are UTF-8
PATH_MAX = 255
LABEL_MAX = 15                      # 16 bytes on disk, always NUL-terminated
MIN_BLOCKS = 16
MAX_BYTES = 1 << 32                 # IO_ADDRESS is a 32-bit byte offset
DEFAULT_SIZE = 4 << 20

# magic version block_size total_blocks fat_start fat_blocks data_start
# free_blocks next_free, then the label: bytes 0..51 of the superblock.
_SUPER = struct.Struct("<9I16s")
# name type first size mtime reserved
_ENTRY = struct.Struct("<32s4I16s")

_CHANNEL_PREFIX = re.compile(r"^\d+:")


class PgfsError(Exception):
    """A failure, with `code` naming the guest's FS_E* error it matches."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Entry:
    """One directory entry. `where` is (block, byte offset) of its 64 bytes
    on disk; the root's lives in the superblock, at (0, ROOT_OFFSET)."""
    name: str
    type: int
    first: int
    size: int
    mtime: int = 0
    where: Optional[Tuple[int, int]] = None

    @property
    def is_dir(self) -> bool:
        return self.type == TYPE_DIR


@dataclass
class Problem:
    message: str
    fixed: bool = False


@dataclass
class FsckReport:
    problems: List[Problem] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.problems

    @property
    def unrepaired(self) -> List[Problem]:
        return [p for p in self.problems if not p.fixed]


def _fat_blocks(total: int) -> int:
    return (total + FAT_PER_BLOCK - 1) // FAT_PER_BLOCK


def _encode(name: str) -> bytes:
    # surrogateescape so a name that is not valid UTF-8 still round-trips.
    return name.encode("utf-8", "surrogateescape")


def _pack_entry(e: Entry) -> bytes:
    return _ENTRY.pack(_encode(e.name), e.type, e.first, e.size, e.mtime, b"")


def _unpack_entry(raw: bytes, offset: int, where) -> Entry:
    name, type_, first, size, mtime, _ = _ENTRY.unpack_from(raw, offset)
    return Entry(name.split(b"\0", 1)[0].decode("utf-8", "surrogateescape"),
                 type_, first, size, mtime, where)


def check_name(name: str) -> bytes:
    """The rules for naming something new (section 5). Returns its bytes."""
    raw = _encode(name)
    if name in (".", ".."):
        raise PgfsError("EINVAL", f"{name!r} cannot be used as a name")
    if not raw:
        raise PgfsError("EINVAL", "a name cannot be empty")
    if len(raw) > NAME_MAX:
        raise PgfsError("ENAMETOOLONG",
                        f"{name!r} is {len(raw)} bytes; a name is at most {NAME_MAX}")
    if any(byte in raw for byte in b"/:\0"):
        raise PgfsError("EINVAL", f"{name!r}: a name cannot contain '/', ':' or NUL")
    return raw


def split_path(path: str) -> List[str]:
    """'/a/./b//../c' -> ['a', 'c'].

    The guest's normalisation (section 5): empty segments and '.' are
    dropped, and '..' removes the segment before it, staying put at the
    root. The host has no current directory, so every path starts at
    the root and a leading '/' is optional.
    """
    if _CHANNEL_PREFIX.match(path):
        raise PgfsError("EINVAL", f"{path}: the host tool addresses the image "
                                  f"itself -- drop the channel prefix")
    parts: List[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    if len(_encode("/" + "/".join(parts))) > PATH_MAX:
        raise PgfsError("ENAMETOOLONG", f"{path}: longer than {PATH_MAX} bytes")
    return parts


def _show(parts: List[str]) -> str:
    return "/" + "/".join(parts)


# --- the image ---------------------------------------------------------------

class PgfsImage:
    """An open PigeonFS image. Use as a context manager, or call close().

    The FAT is held in memory and written back block by block. Writes are
    ordered as the guest orders them (section 6.3): a block becomes
    reachable only after its contents are on disk, and unreachable before
    it is freed.
    """

    def __init__(self, path):
        self.path = Path(path)
        try:
            self._f = open(self.path, "r+b")
        except FileNotFoundError:
            raise PgfsError("ENOENT", f"{self.path}: no such image "
                                      f"(make one with `pfs mkfs`)") from None
        try:
            self._load()
        except BaseException:
            self._f.close()
            raise

    # --- making one ---------------------------------------------------------

    @classmethod
    def mkfs(cls, path, size: Optional[int] = None, label: str = "",
             force: bool = False) -> "PgfsImage":
        """Format an image, creating it if needed, and open it.

        The same rule as the guest's fs_format(): an image whose first
        block is all zeros is formatted freely, and anything else -- a
        PigeonFS disk just as much as a program -- needs `force`. A disk
        lives until someone deliberately reformats it.

        A missing image is created at DEFAULT_SIZE. An existing one keeps
        its size unless `size` is given.
        """
        path = Path(path)
        raw_label = label.encode("utf-8")
        if len(raw_label) > LABEL_MAX:
            raise PgfsError("EINVAL", f"label {label!r} is {len(raw_label)} bytes; "
                                      f"the most is {LABEL_MAX}")

        exists = path.exists()
        if exists:
            with open(path, "rb") as f:
                head = f.read(BLOCK)
            if any(head) and not force:
                what = ("a PigeonFS disk" if head[:4] == struct.pack("<I", MAGIC)
                        else "data that is not PigeonFS")
                raise PgfsError("ENOTBLANK", f"{path} already holds {what}; "
                                             f"--force erases it")

        target = size if size is not None else (
            path.stat().st_size if exists else DEFAULT_SIZE)
        total = target // BLOCK
        if total < MIN_BLOCKS:
            raise PgfsError("EINVAL", f"{target} bytes is too small; the minimum "
                                      f"is {MIN_BLOCKS * BLOCK}")
        if target > MAX_BYTES:
            raise PgfsError("EINVAL", f"{target} bytes is past the 4 GiB a 32-bit "
                                      f"IO_ADDRESS can reach")

        fat_blocks = _fat_blocks(total)
        data_start = FAT_START + fat_blocks
        root = data_start
        fat = [RESERVED] * data_start + [FREE] * (total - data_start)
        fat[root] = EOC
        fat += [FREE] * (fat_blocks * FAT_PER_BLOCK - total)

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "r+b" if exists else "w+b") as f:
            if size is not None or not exists:
                f.truncate(target)
            for i in range(fat_blocks):
                f.seek((FAT_START + i) * BLOCK)
                f.write(struct.pack(f"<{FAT_PER_BLOCK}I",
                                    *fat[i * FAT_PER_BLOCK:(i + 1) * FAT_PER_BLOCK]))
            f.seek(root * BLOCK)
            f.write(bytes(BLOCK))                 # the root: 8 empty slots
            # The superblock goes last, so an image caught half-made has no
            # magic and is refused rather than trusted.
            super_block = bytearray(BLOCK)
            _SUPER.pack_into(super_block, 0, MAGIC, VERSION, BLOCK, total, FAT_START,
                             fat_blocks, data_start, total - data_start - 1, root + 1,
                             raw_label)
            super_block[ROOT_OFFSET:ROOT_OFFSET + ENTRY] = _pack_entry(
                Entry("", TYPE_DIR, root, BLOCK))
            f.seek(0)
            f.write(super_block)
        return cls(path)

    # --- opening ------------------------------------------------------------

    def _load(self):
        """Read and check the superblock, as the guest's fs_mount() does."""
        raw = self._read_block(0)
        (magic, version, block_size, total, fat_start, fat_blocks, data_start,
         free, next_free, label) = _SUPER.unpack_from(raw, 0)
        if magic != MAGIC:
            raise PgfsError("ENOFS", f"{self.path}: no PigeonFS here "
                                     f"(make one with `pfs mkfs`)")

        root = _unpack_entry(raw, ROOT_OFFSET, (0, ROOT_OFFSET))
        wrong = []
        if version != VERSION:
            wrong.append(f"version {version}, expected {VERSION}")
        if block_size != BLOCK:
            wrong.append(f"block size {block_size}, expected {BLOCK}")
        if total < MIN_BLOCKS:
            wrong.append(f"{total} blocks, fewer than {MIN_BLOCKS}")
        if total * BLOCK > self._size():
            wrong.append(f"{total} blocks do not fit in {self._size()} bytes")
        if fat_start != FAT_START:
            wrong.append(f"FAT starts at {fat_start}, expected {FAT_START}")
        if fat_blocks != _fat_blocks(total):
            wrong.append(f"{fat_blocks} FAT blocks, expected {_fat_blocks(total)}")
        if data_start != FAT_START + fat_blocks:
            wrong.append(f"data starts at {data_start}, expected {FAT_START + fat_blocks}")
        if root.type != TYPE_DIR:
            wrong.append("the root entry is not a directory")
        elif not data_start <= root.first < total:
            wrong.append(f"the root starts at block {root.first}, outside the data region")
        if wrong:
            raise PgfsError("ECORRUPT", f"{self.path}: bad superblock: " + "; ".join(wrong))

        self.total = total
        self.fat_blocks = fat_blocks
        self.data_start = data_start
        self.free_hint = free
        self.next_free = next_free
        self._label = label
        self.root = root
        table = b"".join(self._read_block(FAT_START + i) for i in range(fat_blocks))
        self._fat = list(struct.unpack_from(f"<{total}I", table))
        self._dirty_fat = set()

    @property
    def label(self) -> str:
        return self._label.split(b"\0", 1)[0].decode("utf-8", "replace")

    def close(self):
        if self._f is not None:
            self._f.close()
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # --- blocks -------------------------------------------------------------

    def _size(self) -> int:
        return os.fstat(self._f.fileno()).st_size

    def _read_block(self, n: int) -> bytes:
        self._f.seek(n * BLOCK)
        data = self._f.read(BLOCK)
        return data + bytes(BLOCK - len(data))   # past EOF reads as zeros, as on the guest

    def _write_block(self, n: int, data: bytes):
        assert len(data) == BLOCK
        self._f.seek(n * BLOCK)
        self._f.write(data)
        self._f.flush()          # visible to the emulator, and to anyone else reading

    # --- the FAT ------------------------------------------------------------

    def _set(self, block: int, value: int):
        self._fat[block] = value
        self._dirty_fat.add(block // FAT_PER_BLOCK)

    def _flush_fat(self):
        for i in sorted(self._dirty_fat):
            words = self._fat[i * FAT_PER_BLOCK:(i + 1) * FAT_PER_BLOCK]
            words += [FREE] * (FAT_PER_BLOCK - len(words))
            self._write_block(FAT_START + i, struct.pack(f"<{FAT_PER_BLOCK}I", *words))
        self._dirty_fat.clear()

    def _write_super(self):
        raw = bytearray(BLOCK)
        _SUPER.pack_into(raw, 0, MAGIC, VERSION, BLOCK, self.total, FAT_START,
                         self.fat_blocks, self.data_start, self.free_hint,
                         self.next_free, self._label)
        raw[ROOT_OFFSET:ROOT_OFFSET + ENTRY] = _pack_entry(self.root)
        self._write_block(0, bytes(raw))

    def free_count(self) -> int:
        """Free blocks, counted -- not the superblock's hint."""
        return self._fat[self.data_start:self.total].count(FREE)

    def _walk(self, first: int) -> Tuple[List[int], Optional[str]]:
        """The chain from `first`, stopping at the first bad link rather than
        raising, so fsck can see as much as there is to see."""
        blocks: List[int] = []
        seen = set()
        block = first
        if block == 0:
            return blocks, None
        while True:
            if not self.data_start <= block < self.total:
                return blocks, f"the chain points outside the data region (block {block})"
            if block in seen:
                return blocks, f"the chain loops back to block {block}"
            seen.add(block)
            blocks.append(block)
            following = self._fat[block]
            if following == EOC:
                return blocks, None
            if following in (FREE, RESERVED):
                kind = "a free" if following == FREE else "a reserved"
                return blocks, f"the chain runs into {kind} block after block {block}"
            block = following

    def _chain(self, first: int) -> List[int]:
        blocks, bad = self._walk(first)
        if bad:
            raise PgfsError("ECORRUPT", f"{self.path}: {bad}; run `pfs fsck`")
        return blocks

    def _alloc(self, n: int) -> List[int]:
        """n blocks linked into one chain, next-fit from the next_free hint
        -- the guest's allocator, so both leave files in the same places.
        All or nothing: it fails before changing anything."""
        if n == 0:
            return []
        free = self.free_count()
        if free < n:
            raise PgfsError("ENOSPC", f"{self.path}: {n} blocks needed, {free} free")
        block = self.next_free
        if not self.data_start <= block < self.total:
            block = self.data_start
        got: List[int] = []
        while len(got) < n:
            if self._fat[block] == FREE:
                got.append(block)
            block += 1
            if block == self.total:
                block = self.data_start
        for here, following in zip(got, got[1:]):
            self._set(here, following)
        self._set(got[-1], EOC)
        self.next_free = block
        self.free_hint -= n
        return got

    def _free_chain(self, first: int):
        blocks = self._chain(first)
        for block in blocks:
            self._set(block, FREE)
        self.free_hint += len(blocks)

    # --- directory entries --------------------------------------------------

    def _write_entry(self, e: Entry):
        block, offset = e.where
        if (block, offset) == (0, ROOT_OFFSET):
            self.root = e
            self._write_super()
            return
        raw = bytearray(self._read_block(block))
        raw[offset:offset + ENTRY] = _pack_entry(e)
        self._write_block(block, bytes(raw))

    def _clear_at(self, where: Tuple[int, int]):
        block, offset = where
        raw = bytearray(self._read_block(block))
        raw[offset:offset + ENTRY] = bytes(ENTRY)
        self._write_block(block, bytes(raw))

    def _slots_in(self, blocks: List[int]):
        for block in blocks:
            raw = self._read_block(block)
            for i in range(ENTRIES_PER_BLOCK):
                yield _unpack_entry(raw, i * ENTRY, (block, i * ENTRY))

    def _slots(self, d: Entry):
        """Every slot of a directory, empty ones included."""
        return self._slots_in(self._chain(d.first)[:d.size // BLOCK])

    def _find(self, d: Entry, name: str) -> Optional[Entry]:
        for slot in self._slots(d):
            if slot.type != TYPE_EMPTY and slot.name == name:
                return slot
        return None

    def _has_empty_slot(self, d: Entry) -> bool:
        return any(slot.type == TYPE_EMPTY for slot in self._slots(d))

    def _add_entry(self, parent: Entry, e: Entry):
        """Put `e` in the first empty slot, growing the directory by a block
        when there is none. The caller has already checked for space."""
        for slot in self._slots(parent):
            if slot.type == TYPE_EMPTY:
                e.where = slot.where
                break
        else:
            last = self._chain(parent.first)[-1]
            [new] = self._alloc(1)
            self._write_block(new, bytes(BLOCK))      # every slot must read as empty
            self._set(last, new)
            self._flush_fat()
            parent.size += BLOCK
            self._write_entry(parent)
            e.where = (new, 0)
        self._write_entry(e)

    # --- the namespace ------------------------------------------------------

    def _lookup(self, parts: List[str]) -> Entry:
        e = self.root
        for i, name in enumerate(parts):
            if not e.is_dir:
                raise PgfsError("ENOTDIR", f"{_show(parts[:i])}: not a directory")
            found = self._find(e, name)
            if found is None:
                raise PgfsError("ENOENT", f"{_show(parts[:i + 1])}: no such file or directory")
            e = found
        return e

    def _parent(self, parts: List[str]) -> Tuple[Entry, str]:
        if not parts:
            raise PgfsError("EINVAL", "that is the root")
        parent = self._lookup(parts[:-1])
        if not parent.is_dir:
            raise PgfsError("ENOTDIR", f"{_show(parts[:-1])}: not a directory")
        return parent, parts[-1]

    def stat(self, path: str) -> Entry:
        return self._lookup(split_path(path))

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except PgfsError as e:
            if e.code in ("ENOENT", "ENOTDIR"):
                return False
            raise

    def listdir(self, path: str = "/") -> List[Entry]:
        d = self.stat(path)
        if not d.is_dir:
            raise PgfsError("ENOTDIR", f"{path}: not a directory")
        return [slot for slot in self._slots(d) if slot.type != TYPE_EMPTY]

    def read_file(self, path: str) -> bytes:
        e = self.stat(path)
        if e.is_dir:
            raise PgfsError("EISDIR", f"{path}: is a directory")
        data = b"".join(self._read_block(b) for b in self._chain(e.first))
        if len(data) < e.size:
            raise PgfsError("ECORRUPT", f"{path}: {e.size} bytes, but the chain "
                                        f"holds {len(data)}; run `pfs fsck`")
        return data[:e.size]

    def write_file(self, path: str, data: bytes):
        """Create or replace, as the guest's fs_save() does -- which is
        fs_open(FS_CREATE | FS_TRUNC) and then fs_write(). A new file gets
        its empty entry before any data, and an existing one is truncated
        before it is written, so a crash in between leaves an empty file,
        never half old and half new. The order also decides which blocks
        a growing directory and the data get, and it has to match the
        guest's for the two to leave identical images."""
        parts = split_path(path)
        parent, name = self._parent(parts)
        check_name(name)
        e = self._find(parent, name)
        if e is not None and e.is_dir:
            raise PgfsError("EISDIR", f"{_show(parts)}: is a directory")

        needed = -(-len(data) // BLOCK)
        reclaimed = len(self._chain(e.first)) if e is not None else 0
        grows = e is None and not self._has_empty_slot(parent)
        if self.free_count() + reclaimed < needed + grows:
            raise PgfsError("ENOSPC", f"{_show(parts)}: {needed + grows} blocks needed, "
                                      f"{self.free_count() + reclaimed} free")

        if e is None:
            e = Entry(name, TYPE_FILE, 0, 0)
            self._add_entry(parent, e)           # fs_open(FS_CREATE)
        elif e.first or e.size:
            old = e.first
            e.first, e.size = 0, 0
            self._write_entry(e)                 # unreachable first...
            if old:
                self._free_chain(old)            # ...then freed
                self._flush_fat()

        blocks = self._alloc(needed)
        for i, block in enumerate(blocks):
            self._write_block(block, data[i * BLOCK:(i + 1) * BLOCK].ljust(BLOCK, b"\0"))
        self._flush_fat()                        # data, then the FAT, then the entry
        e.first, e.size = (blocks[0] if blocks else 0), len(data)
        self._write_entry(e)
        self._write_super()

    def mkdir(self, path: str, parents: bool = False):
        parts = split_path(path)
        if parents:
            for i in range(1, len(parts) + 1):
                try:
                    existing = self._lookup(parts[:i])
                except PgfsError as e:
                    if e.code != "ENOENT":
                        raise
                    self._mkdir(parts[:i])
                    continue
                if not existing.is_dir:
                    raise PgfsError("ENOTDIR", f"{_show(parts[:i])}: not a directory")
            return
        if not parts:
            raise PgfsError("EEXIST", "/: already exists")
        self._mkdir(parts)

    def _mkdir(self, parts: List[str]):
        parent, name = self._parent(parts)
        check_name(name)
        if self._find(parent, name) is not None:
            raise PgfsError("EEXIST", f"{_show(parts)}: already exists")
        grows = not self._has_empty_slot(parent)
        if self.free_count() < 1 + grows:
            raise PgfsError("ENOSPC", f"{_show(parts)}: {1 + grows} blocks needed, "
                                      f"{self.free_count()} free")
        [block] = self._alloc(1)
        self._write_block(block, bytes(BLOCK))
        self._flush_fat()
        self._add_entry(parent, Entry(name, TYPE_DIR, block, BLOCK))
        self._write_super()

    def remove(self, path: str):
        """Remove a file."""
        e = self.stat(path)
        if e.is_dir:
            raise PgfsError("EISDIR", f"{path}: is a directory")
        self._clear_at(e.where)
        if e.first:
            self._free_chain(e.first)
            self._flush_fat()
        self._write_super()

    def rmdir(self, path: str):
        """Remove an empty directory."""
        parts = split_path(path)
        if not parts:
            raise PgfsError("EBUSY", "cannot remove the root")
        e = self._lookup(parts)
        if not e.is_dir:
            raise PgfsError("ENOTDIR", f"{_show(parts)}: not a directory")
        if not all(slot.type == TYPE_EMPTY for slot in self._slots(e)):
            raise PgfsError("ENOTEMPTY", f"{_show(parts)}: not empty")
        self._clear_at(e.where)
        self._free_chain(e.first)
        self._flush_fat()
        self._write_super()

    def remove_tree(self, path: str):
        """A file, or a directory and everything under it."""
        parts = split_path(path)
        e = self._lookup(parts)
        if not e.is_dir:
            self.remove(_show(parts))
            return
        if not parts:
            raise PgfsError("EBUSY", "cannot remove the root; `pfs mkfs --force` "
                                     "erases everything")
        for child in self.listdir(_show(parts)):
            self.remove_tree(_show(parts + [child.name]))
        self.rmdir(_show(parts))

    def rename(self, source: str, dest: str):
        """Rename or move, within the image. `dest` must not exist.

        The new entry is written before the old one is cleared, as on the
        guest: a crash in between leaves the file under two names, which
        fsck resolves -- never under none."""
        src, dst = split_path(source), split_path(dest)
        if not src:
            raise PgfsError("EBUSY", "cannot move the root")
        e = self._lookup(src)
        if src == dst:
            return
        if e.is_dir and dst[:len(src)] == src:
            raise PgfsError("EINVAL", f"cannot move {_show(src)} inside itself")
        parent, name = self._parent(dst)
        check_name(name)
        if self._find(parent, name) is not None:
            raise PgfsError("EEXIST", f"{_show(dst)}: already exists")
        if not self._has_empty_slot(parent) and self.free_count() < 1:
            raise PgfsError("ENOSPC", f"{_show(dst)}: the directory is full and "
                                      f"there is no block to grow it")
        old = e.where
        self._add_entry(parent, Entry(name, e.type, e.first, e.size, e.mtime))
        self._clear_at(old)
        self._write_super()

    # --- inspection ---------------------------------------------------------

    def info(self) -> dict:
        return {
            "label": self.label,
            "version": VERSION,
            "block_size": BLOCK,
            "total_blocks": self.total,
            "fat_blocks": self.fat_blocks,
            "data_start": self.data_start,
            "free_blocks": self.free_count(),
            "free_hint": self.free_hint,
            "image_bytes": self._size(),
        }

    def fsck(self, repair: bool = False) -> FsckReport:
        """Check everything section 8 lists; with `repair`, fix what can be
        fixed safely.

        Repaired: leaked blocks, chains longer than their size, a file left
        under two names by an interrupted rename, the reserved FAT region,
        and the superblock's hints. Only reported: cross-links, broken
        chains, chains shorter than their size, bad names -- anything where
        a repair would have to guess which data to throw away.
        """
        report = FsckReport()
        fixes: List[Tuple[Problem, Callable[[], None]]] = []

        def found(message: str, fix: Optional[Callable[[], None]] = None):
            problem = Problem(message)
            report.problems.append(problem)
            if fix is not None:
                fixes.append((problem, fix))

        fat, start, total = self._fat, self.data_start, self.total

        for block in range(start):
            if fat[block] != RESERVED:
                found(f"FAT entry {block} should be reserved, it is {fat[block]:#x}",
                      lambda block=block: self._set(block, RESERVED))

        owner = {}      # block -> the path whose chain holds it
        heads = {}      # first block -> (path, entry): one file under two names
        stack = [("/", self.root)]
        while stack:
            path, e = stack.pop()
            blocks, bad = self._walk(e.first)
            if bad:
                found(f"{path}: {bad}")
            if e.is_dir:
                if e.size == 0 or e.size % BLOCK:
                    found(f"{path}: a directory of {e.size} bytes, not a whole "
                          f"number of blocks")
                needed = e.size // BLOCK
            else:
                needed = -(-e.size // BLOCK)
            if len(blocks) < needed:
                found(f"{path}: {e.size} bytes need {needed} blocks, the chain has "
                      f"{len(blocks)}")

            mine = []
            for block in blocks:
                if block in owner:
                    found(f"{path}: block {block} is also in {owner[block]} "
                          f"(cross-linked; not repaired)")
                else:
                    owner[block] = path
                    mine.append(block)
            if len(blocks) > needed and (needed or not e.is_dir):
                found(f"{path}: the chain is {len(blocks) - needed} block(s) longer "
                      f"than its size",
                      lambda e=e, needed=needed, blocks=blocks, mine=set(mine):
                          self._trim(e, needed, blocks, mine))

            if not e.is_dir:
                continue
            names = set()
            for slot in self._slots_in(blocks[:needed]):
                if slot.type == TYPE_EMPTY:
                    continue
                child = path.rstrip("/") + "/" + slot.name
                if slot.type not in (TYPE_FILE, TYPE_DIR):
                    found(f"{child}: unknown type {slot.type}")
                    continue
                try:
                    check_name(slot.name)
                except PgfsError as err:
                    found(f"{child}: {err}")
                if slot.name in names:
                    found(f"{child}: the name appears twice in {path}")
                names.add(slot.name)
                if slot.first and slot.first in heads:
                    other_path, other = heads[slot.first]
                    if other.type == slot.type and other.size == slot.size:
                        found(f"{other_path} and {child} are one "
                              f"{'directory' if slot.is_dir else 'file'} under two "
                              f"names (an interrupted rename); keeping {other_path}",
                              lambda where=slot.where: self._clear_at(where))
                        continue
                if slot.first:
                    heads[slot.first] = (child, slot)
                stack.append((child, slot))

        leaked = [b for b in range(start, total) if fat[b] != FREE and b not in owner]
        if leaked:
            found(f"{len(leaked)} leaked block(s): allocated, but in no file "
                  f"(the first is block {leaked[0]})",
                  lambda: [self._set(b, FREE) for b in leaked])

        counted = self.free_count()
        if self.free_hint != counted:
            found(f"the free_blocks hint says {self.free_hint}, there are {counted}",
                  lambda: None)            # rewritten below, after every other fix
        if not start <= self.next_free < total:
            found(f"the next_free hint {self.next_free} is outside the data region",
                  lambda: None)

        if repair and fixes:
            for problem, fix in fixes:
                fix()
                problem.fixed = True
            self.free_hint = self.free_count()
            if not start <= self.next_free < total:
                self.next_free = start
            self._flush_fat()
            self._write_super()
        return report

    def _trim(self, e: Entry, needed: int, blocks: List[int], mine: set):
        """Cut a chain back to what its size needs, freeing only blocks no
        other file also claims."""
        if needed == 0:
            e.first = 0
            self._write_entry(e)
        else:
            self._set(blocks[needed - 1], EOC)
        for block in blocks[needed:]:
            if block in mine:
                self._set(block, FREE)


def parse_size(text: str) -> int:
    """'4M', '512K', '4MiB', '1048576' -> bytes. Units are binary."""
    t = text.strip().upper()
    t = t.removesuffix("IB").removesuffix("B")
    scale = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(t[-1:], 1)
    number = t[:-1] if scale != 1 else t
    try:
        return int(number) * scale
    except ValueError:
        raise PgfsError("EINVAL", f"not a size: {text!r} (try 4M, 512K or a "
                                  f"byte count)") from None


# --- the command line --------------------------------------------------------

def _default_image() -> Path:
    from emulator.config import ConfigError, load_config
    try:
        return load_config().disk
    except ConfigError as e:
        raise PgfsError("EINVAL", f"config.json: {e}") from None


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1 << 20:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1 << 20):.1f} MiB"


def _printable(name: str) -> str:
    return _encode(name).decode("utf-8", "backslashreplace")


def _join(directory: str, name: str) -> str:
    return directory.rstrip("/") + "/" + name


def _name_of(path: str) -> str:
    parts = split_path(path)
    return parts[-1] if parts else ""


def cmd_mkfs(args) -> int:
    size = parse_size(args.size) if args.size else None
    with PgfsImage.mkfs(args.image, size, args.label, args.force) as img:
        print(f"{args.image}: {img.total} blocks ({_human(img.total * BLOCK)}), "
              f"{img.free_count()} free, label {img.label!r}")
    return 0


def cmd_info(img, args) -> int:
    i = img.info()
    used = i["total_blocks"] - i["data_start"] - i["free_blocks"]
    print(f"image       {img.path}")
    print(f"label       {i['label']!r}")
    print(f"size        {_human(i['total_blocks'] * BLOCK)}  "
          f"({i['total_blocks']} blocks of {BLOCK} B)")
    print(f"used        {used} blocks ({_human(used * BLOCK)})")
    print(f"free        {i['free_blocks']} blocks ({_human(i['free_blocks'] * BLOCK)})")
    print(f"FAT         {i['fat_blocks']} blocks; data from block {i['data_start']}")
    if i["free_hint"] != i["free_blocks"]:
        print(f"note        the free_blocks hint says {i['free_hint']}; run `pfs fsck`")
    return 0


def cmd_ls(img, args) -> int:
    e = img.stat(args.path)
    entries = img.listdir(args.path) if e.is_dir else [e]
    for entry in sorted(entries, key=lambda x: x.name):
        name = _printable(entry.name) + ("/" if entry.is_dir else "")
        if args.long:
            print(f"{'d' if entry.is_dir else '-'} {entry.size:>10}  {name}")
        else:
            print(name)
    return 0


def cmd_tree(img, args) -> int:
    def walk(path: str, prefix: str):
        children = sorted(img.listdir(path), key=lambda x: x.name)
        for i, child in enumerate(children):
            last = i == len(children) - 1
            label = _printable(child.name) + ("/" if child.is_dir else f"  {_human(child.size)}")
            print(prefix + ("└── " if last else "├── ") + label)
            if child.is_dir:
                walk(_join(path, child.name), prefix + ("    " if last else "│   "))

    root = img.stat(args.path)
    if not root.is_dir:
        raise PgfsError("ENOTDIR", f"{args.path}: not a directory")
    print(_show(split_path(args.path)) + (f"  ({img.label})" if img.label else ""))
    walk(args.path, "")
    return 0


def _put_tree(img, local: Path, dest: str):
    img.mkdir(dest)
    for child in sorted(local.iterdir()):
        target = _join(dest, child.name)
        if child.is_dir():
            _put_tree(img, child, target)
        elif child.is_file():
            img.write_file(target, child.read_bytes())


def cmd_put(img, args) -> int:
    local = Path(args.local)
    dest = args.dest
    if img.exists(dest) and img.stat(dest).is_dir:
        dest = _join(dest, local.name)
    if local.is_dir():
        if not args.recursive:
            raise PgfsError("EISDIR", f"{local}: is a directory (use -r)")
        _put_tree(img, local, dest)
    else:
        img.write_file(dest, local.read_bytes())
    return 0


def _get_tree(img, source: str, local: Path):
    local.mkdir(parents=True, exist_ok=True)
    for child in img.listdir(source):
        path = _join(source, child.name)
        if child.is_dir:
            _get_tree(img, path, local / child.name)
        else:
            (local / child.name).write_bytes(img.read_file(path))


def cmd_get(img, args) -> int:
    e = img.stat(args.source)
    local = Path(args.local)
    if local.is_dir() and _name_of(args.source):
        local = local / _name_of(args.source)
    if e.is_dir:
        if not args.recursive:
            raise PgfsError("EISDIR", f"{args.source}: is a directory (use -r)")
        _get_tree(img, args.source, local)
    else:
        local.write_bytes(img.read_file(args.source))
    return 0


def cmd_cat(img, args) -> int:
    data = img.read_file(args.path)
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


def cmd_mkdir(img, args) -> int:
    for path in args.paths:
        img.mkdir(path, parents=args.parents)
    return 0


def cmd_rm(img, args) -> int:
    for path in args.paths:
        if img.stat(path).is_dir:
            if not args.recursive:
                raise PgfsError("EISDIR", f"{path}: is a directory (use -r)")
            img.remove_tree(path)
        else:
            img.remove(path)
    return 0


def cmd_mv(img, args) -> int:
    dest = args.dest
    if img.exists(dest) and img.stat(dest).is_dir:
        dest = _join(dest, _name_of(args.source))
    img.rename(args.source, dest)
    return 0


def cmd_fsck(img, args) -> int:
    report = img.fsck(repair=args.repair)
    for problem in report.problems:
        print(f"{'fixed' if problem.fixed else 'found'}  {problem.message}")
    if report.clean:
        print(f"{img.path}: clean")
    elif report.unrepaired:
        hint = "" if args.repair else "; --repair fixes what can be fixed safely"
        print(f"{img.path}: {len(report.unrepaired)} problem(s) remain{hint}")
    else:
        print(f"{img.path}: repaired")
    return 1 if report.unrepaired else 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--image", type=Path, metavar="PATH",
                        help='the disk image (default: "disk" in config.json)')

    parser = argparse.ArgumentParser(
        prog="pfs", description=__doc__.split("\n")[0],
        epilog="Exit status is 0 on success and 1 on any error; for fsck, 1 "
               "means problems remain.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("mkfs", parents=[common], help="format an image")
    p.add_argument("--size", help="e.g. 4M or 512K; a new image defaults to 4M, "
                                  "an existing one keeps its size")
    p.add_argument("--label", default="", help=f"up to {LABEL_MAX} bytes")
    p.add_argument("--force", action="store_true",
                   help="format an image that already holds data, PigeonFS or not")
    p.set_defaults(handler=cmd_mkfs, opens=False)

    p = sub.add_parser("info", parents=[common], help="geometry and free space")
    p.set_defaults(handler=cmd_info)

    p = sub.add_parser("ls", parents=[common], help="list a directory")
    p.add_argument("-l", dest="long", action="store_true", help="show types and sizes")
    p.add_argument("path", nargs="?", default="/")
    p.set_defaults(handler=cmd_ls)

    p = sub.add_parser("tree", parents=[common], help="list everything, recursively")
    p.add_argument("path", nargs="?", default="/")
    p.set_defaults(handler=cmd_tree)

    p = sub.add_parser("put", parents=[common], help="copy a host file into the image")
    p.add_argument("-r", dest="recursive", action="store_true", help="copy a folder")
    p.add_argument("local")
    p.add_argument("dest")
    p.set_defaults(handler=cmd_put)

    p = sub.add_parser("get", parents=[common], help="copy a file out of the image")
    p.add_argument("-r", dest="recursive", action="store_true", help="copy a directory")
    p.add_argument("source")
    p.add_argument("local")
    p.set_defaults(handler=cmd_get)

    p = sub.add_parser("cat", parents=[common], help="print a file")
    p.add_argument("path")
    p.set_defaults(handler=cmd_cat)

    p = sub.add_parser("mkdir", parents=[common], help="make directories")
    p.add_argument("-p", dest="parents", action="store_true",
                   help="make parents as needed; no error if it exists")
    p.add_argument("paths", nargs="+")
    p.set_defaults(handler=cmd_mkdir)

    p = sub.add_parser("rm", parents=[common], help="remove files")
    p.add_argument("-r", dest="recursive", action="store_true",
                   help="remove directories and everything in them")
    p.add_argument("paths", nargs="+")
    p.set_defaults(handler=cmd_rm)

    p = sub.add_parser("mv", parents=[common], help="rename or move")
    p.add_argument("source")
    p.add_argument("dest")
    p.set_defaults(handler=cmd_mv)

    p = sub.add_parser("fsck", parents=[common], help="check the image")
    p.add_argument("--repair", action="store_true",
                   help="fix leaks, overlong chains, interrupted renames and hints")
    p.set_defaults(handler=cmd_fsck)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.image is None:
            args.image = _default_image()
        if not getattr(args, "opens", True):
            return args.handler(args)
        with PgfsImage(args.image) as img:
            return args.handler(img, args)
    except PgfsError as e:
        print(f"pfs: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"pfs: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
