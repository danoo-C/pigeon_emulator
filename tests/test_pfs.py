"""PigeonFS images from the host: tools/pfs.py.

tools/pfs.py is the reference implementation the guest library will be
checked against (docs/filesystem.md, section 12). So these tests pin the
FORMAT -- byte offsets, the allocator's choices, write order -- and not
just round trips through the same code that wrote the bytes.

    python3 tests/test_pfs.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import struct
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pfs                                                        # noqa: E402
from _runner import cases, run_module                             # noqa: E402
from pfs import (BLOCK, EOC, FREE, RESERVED, TYPE_DIR, TYPE_FILE,  # noqa: E402
                 Entry, PgfsError, PgfsImage)

MiB = 1 << 20


@contextlib.contextmanager
def image(size=MiB, **kwargs):
    """A freshly formatted image in a temporary directory."""
    with tempfile.TemporaryDirectory() as d:
        with PgfsImage.mkfs(Path(d) / "disk.img", size, **kwargs) as img:
            yield img


@contextlib.contextmanager
def scratch():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def clean(img):
    report = img.fsck()
    assert report.clean, "fsck: " + "; ".join(p.message for p in report.problems)


def raises(code, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except PgfsError as e:
        assert e.code == code, f"expected {code}, got {e.code}: {e}"
        return e
    raise AssertionError(f"expected {code}, but nothing was raised")


def pattern(n):
    """n bytes that are not all the same, so a misplaced block shows."""
    return bytes((i * 7 + i // 512) & 0xFF for i in range(n))


def chain(img, path):
    return img._chain(img.stat(path).first)


# --- the format, byte for byte ----------------------------------------------

@cases(
    ("the minimum, 16 blocks", 16 * BLOCK, 16, 1, 2),
    ("1 MiB", MiB, 2048, 16, 17),
    ("4 MiB, the default", 4 * MiB, 8192, 64, 65),
    ("16 MiB", 16 * MiB, 32768, 256, 257),
    ("128 blocks: one full FAT block", 128 * BLOCK, 128, 1, 2),
    ("129 blocks: the FAT needs a second", 129 * BLOCK, 129, 2, 3),
    ("a size that is not whole blocks rounds down", MiB + 300, 2048, 16, 17),
)
def test_geometry(label, size, total, fat_blocks, data_start):
    with image(size) as img:
        info = img.info()
        assert (info["total_blocks"], info["fat_blocks"], info["data_start"]) == \
            (total, fat_blocks, data_start)
        # everything past the FAT is free except the root directory's block
        assert info["free_blocks"] == info["free_hint"] == total - data_start - 1
        clean(img)


def test_superblock_layout():
    """Section 3.1, offset by offset. The guest will read exactly these."""
    with image(MiB, label="PIGEON") as img:
        raw = img.path.read_bytes()
    assert raw[0:4] == b"PGFS"
    assert struct.unpack_from("<9I", raw, 0) == (
        0x53464750,   # magic
        1,            # version
        512,          # block size
        2048,         # total blocks
        1,            # FAT start
        16,           # FAT blocks
        17,           # data start
        2030,         # free blocks
        18,           # next free: just past the root directory
    )
    assert raw[36:52] == b"PIGEON".ljust(16, b"\0")
    assert raw[52:64] == bytes(12)
    # the root's entry: no name, a directory, one block at the data start
    assert struct.unpack_from("<32s4I16s", raw, 64) == (bytes(32), TYPE_DIR, 17, 512, 0,
                                                        bytes(16))
    assert raw[128:512] == bytes(384)


def test_fat_layout():
    with image(MiB) as img:
        raw = img.path.read_bytes()
    fat = struct.unpack_from("<2048I", raw, BLOCK)
    assert fat[:17] == (RESERVED,) * 17, "the superblock and the FAT reserve themselves"
    assert fat[17] == EOC, "the root directory is one block"
    assert set(fat[18:]) == {FREE}
    assert raw[17 * BLOCK:18 * BLOCK] == bytes(BLOCK), "the root starts as 8 empty slots"


def test_directory_entry_layout():
    """Section 3.3: 64 bytes, and the first file lands in slot 0 of the
    root and in the first free block."""
    with image(MiB) as img:
        img.write_file("/hello.txt", b"hi")
        raw = img.path.read_bytes()
    assert struct.unpack_from("<32s4I16s", raw, 17 * BLOCK) == (
        b"hello.txt".ljust(32, b"\0"), TYPE_FILE, 18, 2, 0, bytes(16))
    assert raw[18 * BLOCK:19 * BLOCK] == b"hi" + bytes(BLOCK - 2)
    assert struct.unpack_from("<I", raw, BLOCK + 18 * 4) == (EOC,)
    assert struct.unpack_from("<2I", raw, 28) == (2029, 19), "free count and next_free"


def test_an_image_caught_half_made_is_refused():
    """mkfs writes the superblock last, so an image whose format was
    interrupted has no magic and is refused rather than trusted."""
    with scratch() as d:
        path = d / "disk.img"
        path.write_bytes(bytes(MiB))
        raises("ENOFS", PgfsImage, path)


# --- the allocator ------------------------------------------------------------

def test_files_are_laid_out_contiguously():
    with image(MiB) as img:
        img.write_file("/a", pattern(3 * BLOCK))
        img.write_file("/b", pattern(BLOCK))
        assert chain(img, "/a") == [18, 19, 20]
        assert chain(img, "/b") == [21]


def test_allocation_is_next_fit_not_first_fit():
    """Freed blocks behind the hint are not reused until the search wraps.
    That keeps new files contiguous, which is what lets the guest move a
    run of blocks in one IO command -- and the guest must match it."""
    with image(MiB) as img:
        img.write_file("/a", pattern(3 * BLOCK))
        img.write_file("/b", pattern(BLOCK))
        img.remove("/a")
        img.write_file("/c", pattern(BLOCK))
        assert chain(img, "/c") == [22], "first fit would have taken block 18"


def test_the_search_wraps_around():
    with image(16 * BLOCK) as img:           # data blocks 3..15 after the root at 2
        img.write_file("/big", pattern(12 * BLOCK))
        assert chain(img, "/big") == list(range(3, 15))
        img.remove("/big")
        img.write_file("/two", pattern(2 * BLOCK))
        assert chain(img, "/two") == [15, 3]
        clean(img)


def test_an_empty_file_has_no_blocks():
    with image(MiB) as img:
        before = img.free_count()
        img.write_file("/empty", b"")
        e = img.stat("/empty")
        assert (e.first, e.size) == (0, 0)
        assert img.free_count() == before
        assert img.read_file("/empty") == b""
        clean(img)


# --- files --------------------------------------------------------------------

@cases(0, 1, 511, 512, 513, 4095, 4096, 4097, 100_000)
def test_round_trip(size):
    """Every block edge and every IO-window edge (4096)."""
    data = pattern(size)
    with image(MiB) as img:
        img.write_file("/f", data)
        assert img.read_file("/f") == data
        assert img.stat("/f").size == size
        assert len(chain(img, "/f")) == -(-size // BLOCK)
        clean(img)
        path = img.path
        img.close()
        with PgfsImage(path) as again:        # and it is still there after reopening
            assert again.read_file("/f") == data


def test_replacing_a_file_frees_its_old_blocks():
    with image(MiB) as img:
        start = img.free_count()
        img.write_file("/f", pattern(10 * BLOCK))
        img.write_file("/f", b"short")
        assert img.read_file("/f") == b"short"
        assert img.free_count() == start - 1
        assert len(img.listdir("/")) == 1, "replaced in place, not added twice"
        clean(img)


def test_a_file_cannot_replace_a_directory():
    with image(MiB) as img:
        img.mkdir("/d")
        raises("EISDIR", img.write_file, "/d", b"x")


# --- directories --------------------------------------------------------------

def test_nested_directories():
    with image(MiB) as img:
        img.mkdir("/a")
        img.mkdir("/a/b")
        img.write_file("/a/b/f", b"deep")
        assert [e.name for e in img.listdir("/a")] == ["b"]
        assert img.read_file("/a/b/f") == b"deep"
        assert img.stat("/a/b").is_dir and img.stat("/a/b").size == BLOCK
        clean(img)


def test_mkdir_refusals():
    with image(MiB) as img:
        img.mkdir("/a")
        img.write_file("/f", b"x")
        raises("EEXIST", img.mkdir, "/a")
        raises("EEXIST", img.mkdir, "/")
        raises("ENOENT", img.mkdir, "/missing/child")
        raises("ENOTDIR", img.mkdir, "/f/child")


def test_mkdir_parents():
    with image(MiB) as img:
        img.mkdir("/x/y/z", parents=True)
        img.mkdir("/x/y", parents=True)          # already there: not an error
        assert img.stat("/x/y/z").is_dir
        img.write_file("/x/file", b"")
        raises("ENOTDIR", img.mkdir, "/x/file/sub", parents=True)
        clean(img)


def test_a_directory_grows_a_block_at_a_time_and_reuses_slots():
    """8 entries per block: 20 entries need 3 blocks. Free slots are used
    before the directory grows again, and it never shrinks (section 3.3)."""
    with image(MiB) as img:
        img.mkdir("/d")
        for i in range(20):
            img.write_file(f"/d/f{i:02}", b"")
        assert img.stat("/d").size == 3 * BLOCK
        assert sorted(e.name for e in img.listdir("/d")) == [f"f{i:02}" for i in range(20)]
        for i in range(5):
            img.remove(f"/d/f{i:02}")
        for i in range(5):
            img.write_file(f"/d/g{i}", b"")
        assert img.stat("/d").size == 3 * BLOCK, "the freed slots were not reused"
        assert len(img.listdir("/d")) == 20
        clean(img)


def test_the_root_grows_too():
    """The root's entry lives in the superblock, so its growth is written
    there -- the one directory whose size is not in another directory."""
    with image(MiB) as img:
        for i in range(9):
            img.write_file(f"/f{i}", b"")
        assert img.root.size == 2 * BLOCK
        path = img.path
        img.close()
        with PgfsImage(path) as again:
            assert again.root.size == 2 * BLOCK
            assert len(again.listdir("/")) == 9
            clean(again)


def test_removing_a_tree_returns_every_block():
    with image(MiB) as img:
        start = img.free_count()
        img.mkdir("/x/y", parents=True)
        for i in range(12):                     # enough to grow /x/y to two blocks
            img.write_file(f"/x/y/f{i}", pattern(i * 300))
        img.write_file("/x/top", pattern(5000))
        img.remove_tree("/x")
        assert img.free_count() == start
        assert img.listdir("/") == []
        clean(img)


def test_remove_and_rmdir_refusals():
    with image(MiB) as img:
        img.mkdir("/d")
        img.write_file("/d/f", b"x")
        raises("ENOTEMPTY", img.rmdir, "/d")
        raises("ENOTDIR", img.rmdir, "/d/f")
        raises("EISDIR", img.remove, "/d")
        raises("EBUSY", img.rmdir, "/")
        raises("EBUSY", img.remove_tree, "/")
        raises("ENOENT", img.remove, "/nope")


# --- rename -------------------------------------------------------------------

def test_rename_in_place_and_across_directories():
    with image(MiB) as img:
        img.mkdir("/a")
        img.mkdir("/b")
        img.write_file("/a/f", b"data")
        img.rename("/a/f", "/a/g")
        assert img.read_file("/a/g") == b"data" and not img.exists("/a/f")
        img.rename("/a/g", "/b/h")
        assert img.read_file("/b/h") == b"data" and img.listdir("/a") == []
        clean(img)


def test_renaming_a_directory_moves_its_contents():
    with image(MiB) as img:
        img.mkdir("/a/b", parents=True)
        img.write_file("/a/b/f", b"inside")
        img.mkdir("/c")
        img.rename("/a", "/c/a2")
        assert img.read_file("/c/a2/b/f") == b"inside"
        assert not img.exists("/a")
        clean(img)


def test_rename_refusals():
    with image(MiB) as img:
        img.mkdir("/a/b", parents=True)
        img.write_file("/f", b"")
        img.write_file("/g", b"")
        raises("EEXIST", img.rename, "/f", "/g")
        raises("EINVAL", img.rename, "/a", "/a/b/inside")
        # the same thing spelt so that a naive string check would miss it
        raises("EINVAL", img.rename, "/a", "/a/./b/../b/inside")
        raises("EBUSY", img.rename, "/", "/x")
        raises("ENOENT", img.rename, "/missing", "/x")
        raises("ENOENT", img.rename, "/f", "/missing/x")
        img.rename("/f", "/./f")                 # to itself: nothing to do
        assert img.exists("/f")
        clean(img)


# --- names and paths ------------------------------------------------------------

@cases(
    ("31 bytes", "n" * 31, None),
    ("32 bytes", "n" * 32, "ENAMETOOLONG"),
    ("15 two-byte characters", "é" * 15, None),
    ("16 two-byte characters: 32 bytes", "é" * 16, "ENAMETOOLONG"),
    ("a colon", "a:b", "EINVAL"),
    ("spaces are fine", "my file.txt", None),
)
def test_names(label, name, error):
    """NAME_MAX counts bytes on disk, not characters."""
    with image(MiB) as img:
        if error:
            raises(error, img.write_file, "/" + name, b"")
        else:
            img.write_file("/" + name, b"ok")
            assert img.read_file("/" + name) == b"ok"
            clean(img)


@cases(".", "..", "")
def test_names_that_are_never_valid(name):
    raises("EINVAL", pfs.check_name, name)


@cases(
    ("/a/./b//../c", ["a", "c"]),
    ("a/b", ["a", "b"]),
    ("/../../x", ["x"]),
    ("/", []),
    ("", []),
    ("/a/b/..", ["a"]),
)
def test_path_normalisation(path, parts):
    """The guest's rules (section 5): '..' at the root stays at the root."""
    assert pfs.split_path(path) == parts


def test_path_limits_and_prefixes():
    raises("ENAMETOOLONG", pfs.split_path, "/" + "a/" * 128)
    assert pfs.split_path("/" + "a/" * 127) == ["a"] * 127     # 254 bytes: fits
    raises("EINVAL", pfs.split_path, "2:/saves")


def test_dotted_paths_reach_the_same_file():
    with image(MiB) as img:
        img.mkdir("/a/b", parents=True)
        img.write_file("/a/./b/../f", b"one")
        assert img.read_file("/a/f") == b"one"


# --- running out of space ---------------------------------------------------------

def test_a_full_disk_refuses_and_changes_nothing():
    with image(16 * BLOCK) as img:           # 13 free blocks
        assert img.free_count() == 13
        raises("ENOSPC", img.write_file, "/f", pattern(14 * BLOCK))
        assert img.free_count() == 13 and img.listdir("/") == []
        clean(img)
        img.write_file("/f", pattern(13 * BLOCK))
        assert img.free_count() == 0
        raises("ENOSPC", img.mkdir, "/d")
        img.remove("/f")
        assert img.free_count() == 13
        clean(img)


def test_space_for_directory_growth_is_counted_up_front():
    """A 9th entry in a full directory needs a block for the directory as
    well as the file's own. Checking only the file would write its data,
    then fail to add the entry and leak every block."""
    with image(16 * BLOCK) as img:
        for i in range(8):
            img.write_file(f"/e{i}", b"")        # fills the root's 8 slots
        raises("ENOSPC", img.write_file, "/big", pattern(13 * BLOCK))
        assert img.free_count() == 13
        clean(img)


def test_replacing_counts_the_space_it_frees():
    with image(16 * BLOCK) as img:
        img.write_file("/f", pattern(13 * BLOCK))
        img.write_file("/f", pattern(12 * BLOCK))    # only fits because the old goes
        assert img.free_count() == 1
        clean(img)


# --- mkfs ---------------------------------------------------------------------

def test_a_new_image_is_4_mib():
    with scratch() as d:
        with PgfsImage.mkfs(d / "sub" / "new.img") as img:
            assert img.path.stat().st_size == 4 * MiB
            assert img.total == 8192


def test_an_existing_image_keeps_its_size():
    with scratch() as d:
        path = d / "disk.img"
        path.write_bytes(bytes(2 * MiB))
        with PgfsImage.mkfs(path) as img:
            assert img.total == 4096 and path.stat().st_size == 2 * MiB
        with PgfsImage.mkfs(path, 3 * MiB, force=True) as img:
            assert img.total == 6144 and path.stat().st_size == 3 * MiB


def test_mkfs_refuses_a_disk_with_files_on_it():
    """A disk lives until someone deliberately reformats it."""
    with scratch() as d:
        path = d / "disk.img"
        with PgfsImage.mkfs(path, MiB) as img:
            img.write_file("/keep", b"precious")
        raises("ENOTBLANK", PgfsImage.mkfs, path)
        with PgfsImage(path) as img:
            assert img.read_file("/keep") == b"precious"
        with PgfsImage.mkfs(path, force=True) as img:
            assert img.listdir("/") == []


def test_mkfs_refuses_a_program_image():
    """The boot disk, channel 1, is the program's own .bin."""
    with scratch() as d:
        path = d / "program.bin"
        program = pattern(20 * BLOCK)
        path.write_bytes(program)
        raises("ENOTBLANK", PgfsImage.mkfs, path)
        raises("ENOFS", PgfsImage, path)
        assert path.read_bytes() == program, "the program was touched"


@cases(
    ("too small", 15 * BLOCK),
    ("past 4 GiB", (4 << 30) + BLOCK),
)
def test_mkfs_size_limits(label, size):
    with scratch() as d:
        raises("EINVAL", PgfsImage.mkfs, d / "disk.img", size)
        assert not (d / "disk.img").exists(), "refused, but made a file anyway"


def test_label_limit():
    with scratch() as d:
        with PgfsImage.mkfs(d / "a.img", MiB, label="L" * 15) as img:
            assert img.label == "L" * 15
        raises("EINVAL", PgfsImage.mkfs, d / "b.img", MiB, label="L" * 16)


@cases(("4M", 4 * MiB), ("512K", 512 << 10), ("4MiB", 4 * MiB), ("4mb", 4 * MiB),
       ("1048576", MiB), ("1G", 1 << 30))
def test_parse_size(text, size):
    assert pfs.parse_size(text) == size


# --- opening --------------------------------------------------------------------

def patched(offset, fmt, value):
    """A formatted 1 MiB image with one superblock field overwritten."""
    raw = bytearray(bytes(MiB))
    with scratch() as d:
        with PgfsImage.mkfs(d / "disk.img", MiB) as img:
            raw = bytearray(img.path.read_bytes())
    struct.pack_into(fmt, raw, offset, value)
    return bytes(raw)


@cases(
    ("version", 4, 2),
    ("block size", 8, 1024),
    ("more blocks than the image holds", 12, 4096),
    ("FAT size", 20, 3),
    ("data start", 24, 99),
)
def test_a_bad_superblock_is_refused(label, offset, value):
    with scratch() as d:
        path = d / "disk.img"
        path.write_bytes(patched(offset, "<I", value))
        raises("ECORRUPT", PgfsImage, path)


def test_opening_what_is_not_there():
    with scratch() as d:
        raises("ENOENT", PgfsImage, d / "missing.img")
        (d / "zeros.img").write_bytes(bytes(MiB))
        raises("ENOFS", PgfsImage, d / "zeros.img")


# --- fsck -----------------------------------------------------------------------

def problems(img, repair=False):
    return img.fsck(repair=repair)


def test_fsck_finds_and_frees_a_leaked_block():
    """A kill after the FAT is written but before the entry: the block is
    allocated and nothing points at it."""
    with image(MiB) as img:
        start = img.free_count()
        img._set(100, EOC)
        img._flush_fat()
        report = problems(img)
        assert not report.clean and "leaked" in report.problems[0].message
        assert not problems(img, repair=True).unrepaired
        assert img.free_count() == start
        clean(img)


def test_fsck_trims_a_chain_longer_than_its_size():
    """A kill after the chain grew but before the size was updated."""
    with image(MiB) as img:
        img.write_file("/f", pattern(BLOCK))
        start = img.free_count()
        [first] = chain(img, "/f")
        img._set(first, 200)
        img._set(200, 201)
        img._set(201, EOC)
        img._flush_fat()
        assert "longer" in problems(img).problems[0].message
        assert not problems(img, repair=True).unrepaired
        assert chain(img, "/f") == [first]
        assert img.free_count() == start + 0 and img.read_file("/f") == pattern(BLOCK)
        clean(img)


def test_fsck_resolves_an_interrupted_rename():
    """rename() writes the new entry before clearing the old one, so a kill
    in between leaves one file under two names."""
    with image(MiB) as img:
        img.write_file("/old", b"content")
        e = img.stat("/old")
        img._add_entry(img.root, Entry("new", e.type, e.first, e.size))
        report = problems(img)
        assert len(report.problems) == 1 and "two names" in report.problems[0].message
        assert not problems(img, repair=True).unrepaired
        names = [x.name for x in img.listdir("/")]
        assert len(names) == 1 and img.read_file("/" + names[0]) == b"content"
        clean(img)


def test_fsck_rewrites_a_wrong_hint():
    with image(MiB) as img:
        img.free_hint = 5
        img.next_free = 0
        img._write_super()
        assert len(problems(img).problems) == 2
        assert not problems(img, repair=True).unrepaired
        clean(img)


def test_fsck_restores_the_reserved_region():
    with image(MiB) as img:
        img._set(3, FREE)
        img._flush_fat()
        assert "reserved" in problems(img).problems[0].message
        assert not problems(img, repair=True).unrepaired
        clean(img)


def test_fsck_reports_but_does_not_repair_a_cross_link():
    """Two files claiming one block: freeing it would corrupt one and
    keeping it corrupts the other, so this is a human's call."""
    with image(MiB) as img:
        img.write_file("/a", pattern(2 * BLOCK))
        img.write_file("/b", pattern(BLOCK))
        [b_block] = chain(img, "/b")
        a_blocks = chain(img, "/a")
        img._set(b_block, a_blocks[1])           # b's chain runs into a's tail
        img._flush_fat()
        report = problems(img, repair=True)
        assert any("cross-linked" in p.message for p in report.unrepaired)


@cases(
    ("a loop", lambda img, blocks: img._set(blocks[-1], blocks[0]), "loops"),
    ("a link out of range", lambda img, blocks: img._set(blocks[-1], 999_999), "outside"),
    ("a link into a free block", lambda img, blocks: img._set(blocks[0], 500), "free"),
)
def test_fsck_reports_a_broken_chain(label, damage, words):
    with image(MiB) as img:
        img.write_file("/f", pattern(3 * BLOCK))
        damage(img, chain(img, "/f"))
        img._flush_fat()
        report = problems(img, repair=True)
        assert any(words in p.message for p in report.unrepaired), \
            [p.message for p in report.problems]


def test_fsck_reports_a_size_bigger_than_the_chain():
    with image(MiB) as img:
        img.write_file("/f", b"x")
        e = img.stat("/f")
        e.size = 5000
        img._write_entry(e)
        assert any("need 10 blocks" in p.message for p in problems(img, repair=True).unrepaired)


# --- the command line -----------------------------------------------------------

def cli(*argv):
    out, err = io.BytesIO(), io.StringIO()
    text = io.TextIOWrapper(out, encoding="utf-8", write_through=True)
    with contextlib.redirect_stdout(text), contextlib.redirect_stderr(err):
        code = pfs.main([str(a) for a in argv])
    text.flush()
    return code, out.getvalue(), err.getvalue()


def test_cli_round_trip():
    with scratch() as d:
        disk = d / "disk.img"
        (d / "hello.txt").write_bytes(b"hello, pigeon\n")
        assert cli("mkfs", "--image", disk, "--label", "TEST")[0] == 0
        assert disk.stat().st_size == 4 * MiB
        assert cli("mkdir", "--image", disk, "/docs")[0] == 0
        assert cli("put", "--image", disk, d / "hello.txt", "/docs")[0] == 0
        code, out, _ = cli("ls", "--image", disk, "-l", "/docs")
        assert code == 0 and b"hello.txt" in out and b"14" in out
        assert cli("cat", "--image", disk, "/docs/hello.txt")[1] == b"hello, pigeon\n"
        assert cli("get", "--image", disk, "/docs/hello.txt", d / "back.txt")[0] == 0
        assert (d / "back.txt").read_bytes() == b"hello, pigeon\n"
        code, out, _ = cli("fsck", "--image", disk)
        assert code == 0 and b"clean" in out


def test_cli_copies_folders_both_ways():
    with scratch() as d:
        disk = d / "disk.img"
        src = d / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_bytes(b"A")
        (src / "sub" / "b.bin").write_bytes(pattern(3000))
        cli("mkfs", "--image", disk)
        assert cli("put", "--image", disk, src, "/")[0] == 1, "a folder needs -r"
        assert cli("put", "--image", disk, "-r", src, "/")[0] == 0
        code, out, _ = cli("tree", "--image", disk)
        assert code == 0 and b"sub/" in out and b"b.bin" in out
        assert cli("get", "--image", disk, "-r", "/src", d / "out")[0] == 0
        assert (d / "out" / "a.txt").read_bytes() == b"A"
        assert (d / "out" / "sub" / "b.bin").read_bytes() == pattern(3000)


def test_cli_rm_mv_and_errors():
    with scratch() as d:
        disk = d / "disk.img"
        cli("mkfs", "--image", disk)
        cli("mkdir", "--image", disk, "-p", "/a/b")
        code, _, err = cli("rm", "--image", disk, "/a")
        assert code == 1 and "use -r" in err
        cli("mkdir", "--image", disk, "/dest")
        assert cli("mv", "--image", disk, "/a", "/dest")[0] == 0     # into the directory
        assert b"b/" in cli("ls", "--image", disk, "/dest/a")[1]
        assert cli("rm", "--image", disk, "-r", "/dest")[0] == 0
        assert cli("ls", "--image", disk)[1] == b""
        code, _, err = cli("cat", "--image", disk, "/nope")
        assert code == 1 and "no such file" in err


def test_cli_fsck_exit_status():
    with scratch() as d:
        disk = d / "disk.img"
        cli("mkfs", "--image", disk)
        with PgfsImage(disk) as img:
            img._set(300, EOC)
            img._flush_fat()
        code, out, _ = cli("fsck", "--image", disk)
        assert code == 1 and b"found" in out
        code, out, _ = cli("fsck", "--image", disk, "--repair")
        assert code == 0 and b"fixed" in out
        assert cli("fsck", "--image", disk)[0] == 0


def test_cli_explains_an_unformatted_image():
    with scratch() as d:
        disk = d / "zeros.img"
        disk.write_bytes(bytes(MiB))
        code, _, err = cli("ls", "--image", disk)
        assert code == 1 and "pfs mkfs" in err
        code, _, err = cli("mkfs", "--image", disk, "--size", "banana")
        assert code == 1 and "not a size" in err


def test_the_emulator_makes_a_blank_disk_the_size_pfs_would():
    """A missing channel-2 image is created by the HDD device, and it has
    to come out blank -- so fs_format() and `pfs mkfs` take it without
    being forced -- and the same size pfs gives a new image, so it does
    not matter which of the two made it."""
    from emulator.devices.hdd import DEFAULT_SIZE, HDD
    assert DEFAULT_SIZE == pfs.DEFAULT_SIZE
    with scratch() as d:
        path = d / "disks" / "hdd.img"          # the folder does not exist yet
        HDD(str(path)).close()
        assert path.stat().st_size == 4 * MiB
        with PgfsImage.mkfs(path) as img:       # no force needed: it is blank
            assert img.total == 8192


def test_the_default_image_is_the_emulators_disk():
    """No --image means the disk config.json puts on channel 2."""
    from emulator.config import load_config
    assert pfs._default_image() == load_config().disk


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "PigeonFS images (tools/pfs.py)"))
