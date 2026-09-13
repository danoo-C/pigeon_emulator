"""The CD device: a removable, read-only disc the host puts in and takes out.

Phase 0 of docs/cd-drive.md -- the device on its own, with no HTTP server,
no front end and no guest library. Tests drive callback() directly, the
way tests/test_display.py drives DisplayIO.

Two things here are worth more than the rest.

THE COMMAND NUMBERING. lib/pigeon/fs.c drives a disk with GET_SIZE=1,
READ=2, WRITE=3, FLUSH=5, and its channel guard already lets channel 6
through -- so this device's numbers have to be hdd.py's numbers or
fs_mount(CH_CD) sends READ and gets something else back. An earlier draft
of the design had exactly that collision. test_it_answers_a_disk_command_
set_and_reads_like_one holds the line.

THE MEDIA PROBE. Command 8 has to be answerable three ways that no caller
can confuse: a CD returns the magic, a plain HDD returns zeros (its
unknown-command fallback), and an empty channel is answered 0xFFFFFFFF by
the controller. fs.c will use that to decide a volume is read-only, and
<pigeon/cd.h> to decide there is a drive at all, so it is tested against a
real HDD and through the real bus rather than asserted about.

    python3 tests/test_cd.py      (or: python3 -m pytest tests/)
"""
import ast
import os
import re
import struct
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.devices.cd import (                                     # noqa: E402
    CD, CMD_EJECT, CMD_FLUSH, CMD_GET_SIZE, CMD_MEDIA, CMD_NOP, CMD_READ,
    CMD_TRUNCATE, CMD_WRITE, EJECT_BYTES, MEDIA_BYTES, MEDIA_MAGIC, NAME_MAX, WINDOW)
from emulator.devices.hdd import HDD                                  # noqa: E402
from emulator.io_controller import (                                  # noqa: E402
    ERR_NO_SUCH_CHANNEL, IOChannel, IOController)
from emulator.memory_map import (                                     # noqa: E402
    CH_CD, DISPLAY_H, DISPLAY_W, IO_START, IOHeader, RAM_SIZE)
from emulator.ram import RAM                                          # noqa: E402

# The same bytes tests/test_pfs.py's pattern() makes, so a disc is never
# accidentally self-similar enough to hide an off-by-one.
def pattern(n):
    return bytes((i * 7 + i // 512) & 0xFF for i in range(n))


def read(device, command, length=0, address=0):
    """One read command, as IOController would deliver it."""
    return device.callback(0, command, length, address, bytearray(length))


def write(device, command, payload=b"", address=0):
    """One WRITE-direction command, as IOController would deliver it."""
    return device.callback(1, command, len(payload), address, bytearray(payload))


def media(device):
    """MEDIA, unpacked: magic, present, generation, size, name."""
    raw = read(device, CMD_MEDIA, MEDIA_BYTES)
    assert len(raw) == MEDIA_BYTES, f"MEDIA replied {len(raw)} bytes, want {MEDIA_BYTES}"
    magic, present, generation, size = struct.unpack("<IIII", raw[:16])
    name = raw[16:].split(b"\x00")[0].decode("ascii")
    return magic, present, generation, size, name


class drive:
    """A CD and a directory to put discs in, with the root pinned to it.

    upload_dir is that same directory, which is what the shipped config
    does too: cd_upload_dir is one of cd_dirs, so an uploaded disc shows
    up in the picker afterwards.
    """

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.cd = CD(root=self.dir, dirs=[self.dir], upload_dir=self.dir)
        return self

    def __exit__(self, *exc):
        self.cd.close()
        self._tmp.cleanup()
        return False

    def disc(self, name="demo.bin", data=b""):
        path = self.dir / name
        path.write_bytes(data)
        return path


# --- an empty drive ----------------------------------------------------------

def test_an_empty_drive_says_so_without_pretending_to_be_absent():
    with drive() as d:
        assert read(d.cd, CMD_GET_SIZE, 8) == (0).to_bytes(8, "little")
        assert read(d.cd, CMD_READ, 512, 0) == b""

        magic, present, _, size, name = media(d.cd)
        # The magic is there even with no disc: it says "this is a drive",
        # which is a different question from "there is a disc in it".
        assert magic == MEDIA_MAGIC
        assert present == 0 and size == 0 and name == ""


def test_an_empty_drive_reports_no_disc_in_its_status():
    with drive() as d:
        assert d.cd.status() == {"present": False, "generation": 0,
                                 "name": "", "size": 0, "path": None,
                                 "root": str(d.dir)}


# --- inserting and reading ----------------------------------------------------

def test_a_disc_reports_its_size_and_name():
    with drive() as d:
        d.disc("demo.bin", pattern(1234))
        d.cd.insert(d.dir / "demo.bin")

        assert read(d.cd, CMD_GET_SIZE, 8) == (1234).to_bytes(8, "little")
        magic, present, generation, size, name = media(d.cd)
        assert (magic, present, size, name) == (MEDIA_MAGIC, 1, 1234, "demo.bin")
        assert generation == 1


@cases(
    ("the start",        0,    64),
    ("an offset",        700,  64),
    ("one whole window", 0,    WINDOW),
    ("a single byte",    4095, 1),
)
def test_read_returns_the_discs_bytes(label, offset, length):
    data = pattern(2 * WINDOW)
    with drive() as d:
        d.disc("disc.img", data)
        d.cd.insert(d.dir / "disc.img")
        got = read(d.cd, CMD_READ, length, offset)
        assert got == data[offset:offset + length], label


def test_reads_come_back_short_at_the_end_and_empty_past_it():
    """The BIOS and fs.c both find an end this way. Never zero-filled."""
    with drive() as d:
        d.disc("small.bin", pattern(100))
        d.cd.insert(d.dir / "small.bin")

        crossing = read(d.cd, CMD_READ, 512, 60)
        assert len(crossing) == 40, "a read crossing the end was padded"
        assert crossing == pattern(100)[60:]
        assert read(d.cd, CMD_READ, 512, 100) == b"", "a read past the end was not empty"
        assert read(d.cd, CMD_READ, 512, 1_000_000) == b""


def test_a_read_bigger_than_the_window_is_clamped_not_truncated_later():
    """Clamped here, so the guest gets a short count it can act on rather
    than IOController's truncate-and-warn, which it cannot see."""
    with drive() as d:
        d.disc("big.img", pattern(4 * WINDOW))
        d.cd.insert(d.dir / "big.img")
        assert len(read(d.cd, CMD_READ, 4 * WINDOW, 0)) == WINDOW


def test_ejecting_empties_the_drive_and_closes_the_handle():
    with drive() as d:
        d.disc("demo.bin", pattern(64))
        d.cd.insert(d.dir / "demo.bin")
        handle = d.cd._f
        d.cd.eject()

        assert handle.closed, "the host file handle was left open"
        assert read(d.cd, CMD_READ, 64, 0) == b""
        assert read(d.cd, CMD_GET_SIZE, 8) == (0).to_bytes(8, "little")
        assert media(d.cd)[1] == 0


def test_swapping_discs_closes_the_one_being_replaced():
    with drive() as d:
        d.disc("a.bin", b"aaaa")
        d.disc("b.bin", b"bbbbbbbb")
        d.cd.insert(d.dir / "a.bin")
        first = d.cd._f
        d.cd.insert(d.dir / "b.bin")

        assert first.closed, "the previous disc's handle was leaked"
        assert read(d.cd, CMD_READ, 8, 0) == b"bbbbbbbb"


# --- the generation counter ------------------------------------------------------

def test_generation_moves_on_every_change_and_only_on_a_change():
    with drive() as d:
        d.disc("a.bin", b"aaaa")
        seen = [d.cd.status()["generation"]]

        d.cd.insert(d.dir / "a.bin")
        seen.append(d.cd.status()["generation"])
        d.cd.insert(d.dir / "a.bin")            # the same path: a fresh handle
        seen.append(d.cd.status()["generation"])
        d.cd.eject()
        seen.append(d.cd.status()["generation"])

        assert seen == sorted(seen) and len(set(seen)) == len(seen), seen

        # An eject of an empty drive changes nothing, so it must not move:
        # the counter answers "is what I am reading still what I was
        # reading", and nothing was.
        quiet = d.cd.status()["generation"]
        d.cd.eject()
        d.cd.eject()
        assert d.cd.status()["generation"] == quiet


def test_generation_is_visible_to_the_guest_through_media():
    with drive() as d:
        d.disc("a.bin", b"aaaa")
        d.cd.insert(d.dir / "a.bin")
        first = media(d.cd)[2]
        d.cd.eject()
        assert media(d.cd)[2] != first


# --- read-only ---------------------------------------------------------------------

@cases(CMD_WRITE, CMD_TRUNCATE, CMD_NOP, CMD_READ, CMD_GET_SIZE, CMD_MEDIA, CMD_EJECT, 99)
def test_nothing_the_bus_can_express_as_a_write_is_accepted(command):
    """Read-only is the absence of a write path, not a flag. Every command
    in the WRITE direction is refused before it is even decoded."""
    original = pattern(512)
    with drive() as d:
        path = d.disc("disc.img", original)
        d.cd.insert(path)
        assert write(d.cd, command, b"\xFF" * 64, address=0) == b""
        assert path.read_bytes() == original, "the host file was modified"
        assert d.cd.status()["present"], "a write-direction command took the disc out"


def test_write_and_truncate_are_refused_even_in_the_read_direction():
    original = pattern(512)
    with drive() as d:
        path = d.disc("disc.img", original)
        d.cd.insert(path)
        assert read(d.cd, CMD_WRITE, 64, 0) == b""
        assert read(d.cd, CMD_TRUNCATE, 0, 0) == b""
        assert path.read_bytes() == original


def test_flush_is_a_no_op_that_succeeds():
    """fs_sync() sends FLUSH unconditionally; a read-only volume has
    nothing to flush, so failing it would fail fs_sync() for no reason."""
    with drive() as d:
        d.disc("disc.img", b"x" * 16)
        d.cd.insert(d.dir / "disc.img")
        assert read(d.cd, CMD_FLUSH, 0, 0) == b""


def test_nop_and_unknown_commands_answer_like_a_disk():
    with drive() as d:
        assert read(d.cd, CMD_NOP, 12) == b"\x00" * 12
        assert read(d.cd, 99, 12) == b"\x00" * 12


# --- MEDIA as a probe ---------------------------------------------------------------

def test_a_plain_hdd_does_not_answer_the_media_magic():
    """The other half of the probe. hdd.py's unknown-command fallback is
    `length` zero bytes, so command 8 on a disk is 48 zeros -- which is
    how fs.c will tell a read-only disc from a writable disk."""
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "hdd.img"
        image.write_bytes(bytes(4096))
        disk = HDD(str(image))
        try:
            reply = disk.callback(0, CMD_MEDIA, MEDIA_BYTES, 0, bytearray(MEDIA_BYTES))
        finally:
            disk.close()
    assert reply == b"\x00" * MEDIA_BYTES
    assert struct.unpack("<I", reply[:4])[0] != MEDIA_MAGIC


def test_the_probe_has_three_distinguishable_answers_on_the_real_bus():
    """CD, disk and empty channel, through IOController itself."""
    ram = RAM(RAM_SIZE)
    controller = IOController(ram)

    def fire(channel, command, length):
        ram.write_word(IO_START + IOHeader.IO_R_W, 0)
        ram.write_word(IO_START + IOHeader.COMMAND, command)
        ram.write_word(IO_START + IOHeader.LENGTH, length)
        ram.write_word(IO_START + IOHeader.ADDRESS, 0)
        ram.write_word(IO_START + IOHeader.IO_CHANNEL, channel)
        controller.update()
        retlen = ram.read_word(IO_START + IOHeader.RETURN_DATA)
        base = IO_START + IOHeader.USABLE_AFTER
        return retlen, ram.read_word(base)

    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "hdd.img"
        image.write_bytes(bytes(4096))
        disk = HDD(str(image))
        cd = CD(root=Path(tmp), dirs=[Path(tmp)])
        controller.register_channel(CH_CD, IOChannel(cd.callback, name="CD"))
        controller.register_channel(2, IOChannel(disk.callback, name="HDD"))
        try:
            assert fire(CH_CD, CMD_MEDIA, MEDIA_BYTES) == (MEDIA_BYTES, MEDIA_MAGIC)
            assert fire(2, CMD_MEDIA, MEDIA_BYTES) == (MEDIA_BYTES, 0)
            # Channel 7 has no device: the controller answers, not a device.
            assert fire(7, CMD_MEDIA, MEDIA_BYTES)[0] == ERR_NO_SUCH_CHANNEL
        finally:
            disk.close()
            cd.close()

    assert ERR_NO_SUCH_CHANNEL != MEDIA_BYTES, "an empty channel must not look like a reply"


def test_a_cd_answers_the_disk_command_set_and_reads_like_one():
    """fs_mount(CH_CD) depends on this and on nothing else.

    fs.c sends GET_SIZE=1 expecting exactly 8 bytes, then READ=2 with a
    byte offset. If either number or either shape drifts, mounting a disc
    stops working -- silently, because fs.c would just decide there is no
    disk there.
    """
    from emulator.devices import hdd as hdd_module

    for name in ("CMD_NOP", "CMD_GET_SIZE", "CMD_READ", "CMD_WRITE",
                 "CMD_TRUNCATE", "CMD_FLUSH"):
        from emulator.devices import cd as cd_module
        assert getattr(cd_module, name) == getattr(hdd_module, name), (
            f"{name} differs from hdd.py -- fs.c drives both with the same numbers")

    data = pattern(2048)
    with drive() as d:
        d.disc("vol.img", data)
        d.cd.insert(d.dir / "vol.img")

        size = read(d.cd, CMD_GET_SIZE, 8)
        assert len(size) == 8, "fs.c's __fs_disk_blocks() requires exactly 8 bytes"
        assert int.from_bytes(size, "little") == 2048
        # fs.c reads block n as READ at offset n << 9.
        assert read(d.cd, CMD_READ, 512, 2 << 9) == data[1024:1536]


# --- names ---------------------------------------------------------------------------

@cases(
    ("a plain name",     "demo.bin",                 "demo.bin"),
    ("at the limit",     "a" * NAME_MAX,             "a" * NAME_MAX),
    ("over the limit",   "b" * 40,                   "b" * NAME_MAX),
    ("non-ascii",        "résumé.txt",     "r_sum_.txt"),
    ("a tab in it",      "we\tird.bin",              "we_ird.bin"),
)
def test_the_name_a_guest_sees_is_sanitised_and_bounded(label, filename, expected):
    with drive() as d:
        d.disc(filename, b"x")
        d.cd.insert(d.dir / filename)
        name = media(d.cd)[4]
        assert name == expected, label
        assert len(name.encode("ascii")) <= NAME_MAX


def test_a_host_path_never_reaches_the_guest():
    with drive() as d:
        nested = d.dir / "deep"
        nested.mkdir()
        (nested / "demo.bin").write_bytes(b"x")
        d.cd.insert(nested / "demo.bin")

        raw = read(d.cd, CMD_MEDIA, MEDIA_BYTES)
        assert b"deep" not in raw and b"/" not in raw
        assert media(d.cd)[4] == "demo.bin"


# --- which paths may be inserted --------------------------------------------------------

def test_a_path_outside_the_root_is_refused():
    with drive() as d:
        with tempfile.TemporaryDirectory() as outside:
            stranger = Path(outside) / "elsewhere.bin"
            stranger.write_bytes(b"x")
            try:
                d.cd.insert(stranger)
            except PermissionError as e:
                assert "cd_root" in str(e), "the error should name the setting to change"
            else:
                raise AssertionError("a disc outside the root was accepted")


def test_a_symlink_pointing_out_of_the_root_is_refused():
    """Resolved before the check, so a link inside the root that points
    outside it is outside it."""
    with drive() as d:
        with tempfile.TemporaryDirectory() as outside:
            stranger = Path(outside) / "elsewhere.bin"
            stranger.write_bytes(b"x")
            link = d.dir / "innocent.bin"
            try:
                link.symlink_to(stranger)
            except (OSError, NotImplementedError):
                return                      # no symlinks here; nothing to prove
            try:
                d.cd.insert(link)
            except PermissionError:
                return
            raise AssertionError("a symlink out of the root was accepted")


def test_no_root_means_anywhere():
    with tempfile.TemporaryDirectory() as outside:
        stranger = Path(outside) / "elsewhere.bin"
        stranger.write_bytes(b"hello")
        cd = CD(root=None, dirs=[])
        try:
            cd.insert(stranger)
            assert read(cd, CMD_READ, 5, 0) == b"hello"
        finally:
            cd.close()


def test_a_failed_insert_leaves_the_disc_that_is_in_alone():
    """Ejecting the good disc because you mistyped the next one is a
    surprise nobody wants."""
    with drive() as d:
        d.disc("good.bin", b"good")
        d.cd.insert(d.dir / "good.bin")
        before = d.cd.status()

        for bad in (d.dir / "nope.bin", d.dir):          # missing, and a directory
            try:
                d.cd.insert(bad)
            except (FileNotFoundError, PermissionError, IsADirectoryError):
                pass
            else:
                raise AssertionError(f"{bad} was accepted as a disc")

        assert d.cd.status() == before, "a failed insert disturbed the drive"
        assert read(d.cd, CMD_READ, 4, 0) == b"good"


# --- the listing ----------------------------------------------------------------------

def test_the_listing_shows_files_with_their_sizes_and_skips_the_rest():
    with drive() as d:
        d.disc("b.bin", b"x" * 10)
        d.disc("a.img", b"y" * 20)
        d.disc(".hidden", b"z")
        (d.dir / "subdir").mkdir()

        listed = d.cd.list_discs()
        assert [e["name"] for e in listed] == ["a.img", "b.bin"], listed
        assert [e["size"] for e in listed] == [20, 10]
        assert all(Path(e["path"]).is_absolute() for e in listed)


def test_a_missing_listing_directory_is_not_an_error():
    """A fresh clone has no cds/, and that must not break the picker."""
    cd = CD(root=None, dirs=[Path("/nonexistent-cd-dir")])
    try:
        assert cd.list_discs() == []
    finally:
        cd.close()


def test_the_same_file_reachable_twice_is_listed_once():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "one").mkdir()
        (root / "one" / "disc.bin").write_bytes(b"x")
        cd = CD(root=root, dirs=[root / "one", root / "one"])
        try:
            assert len(cd.list_discs()) == 1
        finally:
            cd.close()


# --- the machine ------------------------------------------------------------------------

def test_the_drive_is_on_the_bus_of_every_machine():
    """Registered unconditionally: "no disc" and "no drive" have to be
    different answers, and <pigeon/cd.h> detects the second one."""
    from emulator.machine import Machine

    bios = REPO_ROOT / "build" / "bios.bin"
    assert bios.exists(), "build/bios.bin is missing; run start_emulator.py once"
    with tempfile.TemporaryDirectory() as tmp:
        machine = Machine(bios_path=str(bios), disk_path=str(Path(tmp) / "hdd.img"))
        try:
            channel = machine.io_controller.channels.get(CH_CD)
            assert channel is not None, "no CD device on channel 6"
            assert channel.name == "CD"
            assert media(machine.cd)[:2] == (MEDIA_MAGIC, 0), "should boot with no disc"
        finally:
            machine.close()

# --- EJECT: the guest's own hand on the drive --------------------------------

def test_the_guest_can_eject_a_disc():
    with drive() as d:
        path = d.disc("demo.bin", pattern(64))
        d.cd.insert(path)
        handle = d.cd._f

        ejected, generation = struct.unpack("<II", read(d.cd, CMD_EJECT, EJECT_BYTES))
        assert (ejected, generation) == (1, 2), (ejected, generation)
        assert handle.closed, "the host file handle was left open"
        assert d.cd.status()["present"] is False
        assert read(d.cd, CMD_READ, 64, 0) == b""
        assert media(d.cd)[1:3] == (0, 2)
        assert path.read_bytes() == pattern(64), "ejecting touched the file"


def test_a_guest_eject_of_an_empty_drive_says_so_and_moves_nothing():
    """The same rule as the host's eject(), because it is the same code:
    nothing came out, so the generation must not move."""
    with drive() as d:
        d.disc("a.bin", b"x")
        d.cd.insert(d.dir / "a.bin")
        d.cd.eject()
        before = d.cd.status()["generation"]
        assert struct.unpack("<II", read(d.cd, CMD_EJECT, EJECT_BYTES)) == (0, before)
        assert d.cd.status()["generation"] == before


def test_eject_is_not_a_disk_command():
    """9 sits above everything hdd.py uses, and a disk answers it the way it
    answers any unknown command -- so a guest aiming EJECT at the HDD
    channel gets zeros and its disk stays exactly where it was."""
    from emulator.devices import hdd as hdd_module

    disk_numbers = {getattr(hdd_module, n) for n in dir(hdd_module) if n.startswith("CMD_")}
    assert CMD_EJECT not in disk_numbers, f"EJECT ({CMD_EJECT}) collides with a disk command"

    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "hdd.img"
        image.write_bytes(pattern(4096))
        disk = HDD(str(image))
        try:
            reply = disk.callback(0, CMD_EJECT, EJECT_BYTES, 0, bytearray(EJECT_BYTES))
        finally:
            disk.close()
        assert reply == b"\x00" * EJECT_BYTES
        assert image.read_bytes() == pattern(4096)


def test_a_guest_eject_through_the_real_bus():
    ram = RAM(RAM_SIZE)
    controller = IOController(ram)
    with drive() as d:
        d.disc("demo.bin", b"y" * 16)
        d.cd.insert(d.dir / "demo.bin")
        controller.register_channel(CH_CD, IOChannel(d.cd.callback, name="CD"))

        ram.write_word(IO_START + IOHeader.IO_R_W, 0)
        ram.write_word(IO_START + IOHeader.COMMAND, CMD_EJECT)
        ram.write_word(IO_START + IOHeader.LENGTH, EJECT_BYTES)
        ram.write_word(IO_START + IOHeader.ADDRESS, 0)
        ram.write_word(IO_START + IOHeader.IO_CHANNEL, CH_CD)
        controller.update()

        base = IO_START + IOHeader.USABLE_AFTER
        assert ram.read_word(IO_START + IOHeader.RETURN_DATA) == EJECT_BYTES
        assert (ram.read_word(base), ram.read_word(base + 4)) == (1, 2)
        assert d.cd.status()["present"] is False


# --- uploads -----------------------------------------------------------------
#
# A browser cannot hand over a path -- input.files[0] is bytes with a name
# -- so this is the only way the browser front end can put a disc in. The
# routes around it are three lines each; everything worth testing is here.

def test_status_says_where_discs_may_come_from():
    """A front end's file dialog opens at cd_root. Without this it would
    guess, and offer the user paths that come back 403."""
    with drive() as d:
        assert d.cd.status()["root"] == str(d.dir)
    cd = CD(root=None, dirs=[])
    try:
        assert cd.status()["root"] is None, "null cd_root should say so, not lie"
    finally:
        cd.close()


def test_an_upload_is_written_down_and_inserted():
    with drive() as d:
        status = d.cd.save_upload("note.txt", b"uploaded bytes")
        assert status["present"] and status["name"] == "note.txt"
        assert (d.dir / "note.txt").read_bytes() == b"uploaded bytes"
        # And the guest can read it immediately, through the bus.
        assert read(d.cd, CMD_READ, 14, 0) == b"uploaded bytes"


def test_an_upload_keeps_its_name_so_it_can_be_re_inserted():
    """cd_upload_dir defaults to one of cd_dirs, so an uploaded disc turns
    up in the picker afterwards: upload once, re-insert forever."""
    with drive() as d:
        d.cd.save_upload("keeper.bin", b"x" * 8)
        assert "keeper.bin" in [e["name"] for e in d.cd.list_discs()]


@cases(
    ("a path, not a name", "../../etc/passwd", "passwd"),
    ("an absolute path",   "/etc/hostname",    "hostname"),
    ("a windows path",     "C:\\Users\\me\\d.bin", "d.bin"),
)
def test_an_upload_name_is_reduced_to_a_basename(label, sent, expected):
    """The name comes from a browser and is never trusted as a path."""
    with drive() as d:
        status = d.cd.save_upload(sent, b"x")
        assert status["name"] == expected, label
        assert (d.dir / expected).is_file()


@cases("", ".", "..", "/")
def test_an_upload_with_no_usable_name_is_refused(sent):
    with drive() as d:
        try:
            d.cd.save_upload(sent, b"x")
        except ValueError:
            return
        raise AssertionError(f"{sent!r} was accepted as a file name")


def test_an_upload_over_the_limit_is_refused_and_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        room = Path(tmp)
        cd = CD(root=room, dirs=[room], upload_dir=room, max_upload=16)
        try:
            cd.save_upload("fits.bin", b"x" * 16)
            try:
                cd.save_upload("toobig.bin", b"x" * 17)
            except ValueError as e:
                assert "cd_max_upload" in str(e), "the error should name the setting"
            else:
                raise AssertionError("an oversize upload was accepted")
            assert not (room / "toobig.bin").exists(), "it was written anyway"
            assert cd.status()["name"] == "fits.bin", "the good disc was disturbed"
        finally:
            cd.close()


def test_an_upload_dir_outside_the_root_is_refused_before_anything_is_written():
    with tempfile.TemporaryDirectory() as inside, tempfile.TemporaryDirectory() as outside:
        cd = CD(root=Path(inside), dirs=[], upload_dir=Path(outside))
        try:
            try:
                cd.save_upload("sneaky.bin", b"x")
            except PermissionError:
                assert not any(Path(outside).iterdir()), "it wrote the file first"
                return
            raise AssertionError("an upload outside cd_root was accepted")
        finally:
            cd.close()


# --- the config the drive is built from ------------------------------------------

def test_the_shipped_config_points_the_picker_at_the_upload_folder():
    """An uploaded disc is only re-insertable if cd_upload_dir is one of
    cd_dirs. Nothing else checks that the two agree, and they are set in
    different places."""
    from emulator.config import load_config

    config = load_config()
    assert config.cd_upload_dir in config.cd_dirs, (
        f"cd_upload_dir ({config.cd_upload_dir}) is not in cd_dirs "
        f"({config.cd_dirs}) -- uploads would vanish from the picker")


def test_a_drive_built_from_the_config_agrees_with_it():
    from emulator.config import load_config

    config = load_config()
    cd = CD(root=config.cd_root, dirs=config.cd_dirs,
            upload_dir=config.cd_upload_dir, max_upload=config.cd_max_upload)
    try:
        assert cd.root == config.cd_root
        assert cd.dirs == list(config.cd_dirs)
        assert cd.upload_dir == config.cd_upload_dir
        assert cd.max_upload == config.cd_max_upload
        # The default root is the repo, so the project is reachable...
        assert cd.resolve("build/bios.bin") == (REPO_ROOT / "build/bios.bin").resolve()
        # ...and nothing above it is.
        try:
            cd.resolve("/etc/hostname")
        except PermissionError:
            pass
        else:
            raise AssertionError("cd_root did not restrict anything")
    finally:
        cd.close()


# --- the browser front end ---------------------------------------------------
#
# The page is JavaScript and this suite is Python, so these are the same
# kind of check tests/test_input.py makes against the keycode table: the
# two files have to agree, and nothing else would notice if they stopped.
# Running the page's logic for real needs a browser (or node, which is not
# a dependency here); what drift actually looks like is a renamed endpoint
# or a mistyped element id, and both are visible from here.

INDEX_HTML = REPO_ROOT / "display" / "index.html"
CD_PY = REPO_ROOT / "emulator" / "devices" / "cd.py"


def test_every_endpoint_the_page_calls_is_one_the_device_serves():
    page = set(re.findall(r"'(/cd/[a-z]+)", INDEX_HTML.read_text()))
    served = set(re.findall(r'@app\.(?:get|post)\("(/cd/[a-z]+)"\)', CD_PY.read_text()))

    assert page, f"{INDEX_HTML} calls no /cd endpoints at all"
    assert served, f"{CD_PY} registers no /cd routes at all"
    assert page <= served, (
        f"{INDEX_HTML} calls {sorted(page - served)}, which "
        f"{CD_PY.name} does not serve (it serves {sorted(served)})")


def test_the_page_wires_the_elements_it_declares():
    html = INDEX_HTML.read_text()
    declared = set(re.findall(r'id="(cd-[a-z]+)"', html))
    looked_up = set(re.findall(r"getElementById\('(cd-[a-z]+)'\)", html))

    assert declared, "the CD controls are gone from index.html"
    assert looked_up == declared, (
        f"getElementById and id= disagree: only in the markup "
        f"{sorted(declared - looked_up)}, only in the script "
        f"{sorted(looked_up - declared)}")


def test_the_page_is_told_where_the_drive_is():
    """index.html hardcodes no ports. It learns the CD server's address
    from /info, the same way it learns HID's -- so if display_io stops
    sending cd_url, every button on the page quietly goes dead."""
    display_io = (REPO_ROOT / "emulator" / "devices" / "display_io.py").read_text()
    assert '"cd_url": self.cd_url' in display_io, "/info no longer carries cd_url"
    assert "info.cd_url" in INDEX_HTML.read_text(), "the page no longer reads cd_url"

    machine = (REPO_ROOT / "emulator" / "machine.py").read_text()
    assert "self.display_io.cd_url" in machine, "start_servers no longer sets cd_url"


# --- the pygame front end ----------------------------------------------------
#
# DisplayClient blocks in __init__ until the display server answers, so
# these build one with object.__new__ and give it only what the code under
# test touches: the picker and the bar layout, which is where the client's
# own decisions live. Talking to the server is covered by the endpoint
# tests above; what is left is what the client does with a keypress.
#
# pygame is an optional client dependency (requirements-client.txt), so
# each of these returns early without it -- the same bargain
# tests/test_input.py makes with _pygame_client().

DISCS = [{"name": f"d{i}.bin", "path": f"/x/d{i}.bin", "size": i} for i in range(5)]


def _client_module():
    from test_input import _pygame_client
    return _pygame_client()


def _bare_client(module):
    c = object.__new__(module.DisplayClient)
    c._picker = None
    c._key_sent = {}
    c.sent = []
    c._send_key = lambda code, pressed: c.sent.append((code, pressed))
    c.inserted = []
    c._insert_path = lambda path: c.inserted.append(path)
    return c


def _key(pygame, key):
    return pygame.event.Event(pygame.KEYDOWN, key=key, unicode="")


def test_nothing_reaches_the_guest_while_the_picker_is_open():
    module = _client_module()
    if module is None:
        return
    import pygame
    c = _bare_client(module)
    c._open_picker(DISCS)
    for key in (pygame.K_a, pygame.K_SPACE, pygame.K_DOWN, pygame.K_F1, pygame.K_BACKSPACE):
        assert c._picker_event(_key(pygame, key)), f"key {key} was not consumed"
        assert c._picker_event(pygame.event.Event(pygame.KEYUP, key=key))
    assert c._picker_event(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(5, 5)))
    assert c.sent == [], f"keys reached the guest behind the overlay: {c.sent}"


def test_opening_the_picker_releases_every_held_key():
    """The picker swallows every release while it is open, so a key held
    as it opened would stay down in the guest's bitmap forever -- the same
    hazard index.html handles on blur."""
    module = _client_module()
    if module is None:
        return
    import pygame
    c = _bare_client(module)
    c._key_sent = {pygame.K_a: 0x61, pygame.K_LEFT: 0x80}
    c._open_picker(DISCS)
    assert sorted(c.sent) == [(0x61, False), (0x80, False)], c.sent
    assert c._key_sent == {}


def test_picker_navigation_stays_in_range():
    module = _client_module()
    if module is None:
        return
    import pygame
    c = _bare_client(module)
    c._open_picker(DISCS)
    c._picker["rows"] = 2

    def at(key):
        c._picker_event(_key(pygame, key))
        return c._picker["index"]

    assert at(pygame.K_UP) == 0, "moved above the first row"
    assert at(pygame.K_END) == 4
    assert at(pygame.K_DOWN) == 4, "moved past the last row"
    assert at(pygame.K_PAGEUP) == 2
    assert at(pygame.K_HOME) == 0
    assert at(pygame.K_PAGEDOWN) == 2
    assert at(pygame.K_PAGEDOWN) == 4 and at(pygame.K_PAGEDOWN) == 4


def test_enter_inserts_the_selection_and_escape_inserts_nothing():
    module = _client_module()
    if module is None:
        return
    import pygame
    c = _bare_client(module)
    c._open_picker(DISCS)
    c._picker_event(_key(pygame, pygame.K_DOWN))
    c._picker_event(_key(pygame, pygame.K_RETURN))
    assert c._picker is None and c.inserted == ["/x/d1.bin"], c.inserted

    c._open_picker(DISCS)
    c._picker_event(_key(pygame, pygame.K_ESCAPE))
    assert c._picker is None and c.inserted == ["/x/d1.bin"], "Esc inserted something"


def test_a_click_picks_a_row_and_a_click_outside_picks_nothing():
    module = _client_module()
    if module is None:
        return
    import pygame
    c = _bare_client(module)

    def opened():
        c._open_picker(DISCS)
        # What _draw_picker would have laid out on the previous frame.
        c._picker["panel"] = pygame.Rect(10, 50, 300, 200)
        c._picker["rects"] = [(pygame.Rect(16, 82 + 24 * i, 280, 22), i) for i in range(3)]

    opened()
    row = c._picker["rects"][2][0]
    c._picker_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=row.center))
    assert c.inserted == ["/x/d2.bin"], c.inserted

    opened()
    c._picker_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(1, 1)))
    assert c._picker is None and c.inserted == ["/x/d2.bin"], "an outside click inserted"


def test_the_bar_lays_out_without_overlap_at_every_pixel_size():
    """The first three buttons used to sit at typed x positions. That held
    for three short labels; "Load from server" alone is wider than all three,
    and at pixel size 1 the old 260 px minimum already cut the bar off."""
    module = _client_module()
    if module is None:
        return
    import pygame
    previous = os.environ.get("SDL_VIDEODRIVER")
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.init()
    try:
        c = object.__new__(module.DisplayClient)
        c.font = pygame.font.SysFont(None, 22)
        c.cd_url = "http://127.0.0.1:1"
        c.disp_w, c.disp_h = DISPLAY_W, DISPLAY_H
        c._build_buttons()

        assert [b.label for b in c.buttons] == [
            "Clear", "-", "+", "Load from server", "Load from PC", "Eject"]
        for left, right in zip(c.buttons, c.buttons[1:]):
            assert right.rect.left > left.rect.right, f"{left.label} overlaps {right.label}"
        assert c._px_x > c.buttons[2].rect.right, "the px label sits on the + button"
        assert c._info_x > c.buttons[-1].rect.right, "the disc label sits on Eject"

        for px in (module.MIN_PIXEL_SIZE, module.MAX_PIXEL_SIZE):
            c.pixel_size = px
            c._resize_window()
            assert c.screen.get_width() >= c.buttons[-1].rect.right, (
                f"at pixel size {px} the window is {c.screen.get_width()} px and "
                f"the bar needs {c.buttons[-1].rect.right}")
    finally:
        pygame.quit()
        if previous is None:
            os.environ.pop("SDL_VIDEODRIVER", None)
        else:
            os.environ["SDL_VIDEODRIVER"] = previous


def test_the_cd_buttons_are_disabled_when_there_is_no_drive():
    module = _client_module()
    if module is None:
        return
    import pygame
    previous = os.environ.get("SDL_VIDEODRIVER")
    os.environ["SDL_VIDEODRIVER"] = "dummy"
    pygame.init()
    try:
        c = object.__new__(module.DisplayClient)
        c.font = pygame.font.SysFont(None, 22)
        c.cd_url = None
        c._build_buttons()
        assert not any(b.enabled for b in c.buttons[3:]), "CD buttons live with no drive"
        assert all(b.enabled for b in c.buttons[:3]), "the other buttons went dead too"

        # A disabled button still owns its click, so it cannot fall through.
        eject = c.buttons[5]
        called = []
        eject.callback = lambda: called.append(1)
        assert eject.handle_click(eject.rect.center) is True
        assert called == []
    finally:
        pygame.quit()
        if previous is None:
            os.environ.pop("SDL_VIDEODRIVER", None)
        else:
            os.environ["SDL_VIDEODRIVER"] = previous


def test_tkinter_is_never_imported_at_module_level():
    """python3-tk is a system package, not a pip one, and it is not installed
    on the machine this was written on. A top-level import would take the
    whole client down with it instead of costing one button."""
    tree = ast.parse((REPO_ROOT / "display" / "display.py").read_text())

    def top_level_imports(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue                       # runs only when called
            if isinstance(node, ast.Import):
                yield from (a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                yield node.module or ""
            for field in ("body", "orelse", "finalbody", "handlers"):
                yield from top_level_imports(getattr(node, field, []) or [])

    at_import = [n for n in top_level_imports(tree.body) if n.split(".")[0] == "tkinter"]
    assert at_import == [], f"tkinter is imported at module level: {at_import}"

    loader = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_load_from_pc")
    inside = [a.name for n in ast.walk(loader) if isinstance(n, ast.Import) for a in n.names]
    assert "tkinter" in inside, "Load from PC no longer uses tkinter -- update this test"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "the CD device"))
