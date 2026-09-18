"""The VRAM device: modes, surfaces and the scanout (docs/gac/phase2_vram.md).

    python3 tests/test_vram.py      (or: python3 -m pytest tests/)
"""
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.devices.display_io import (                             # noqa: E402
    CMD_GET_BASE, CMD_INFO as DISPLAY_INFO, CMD_FILL, CMD_SET_BASE, DisplayIO)
from emulator.devices.vram import (                                   # noqa: E402
    CMD_ALLOC, CMD_DOWNLOAD, CMD_FREE, CMD_GET_MODE, CMD_INFO, CMD_MODE_AT, CMD_MODE_COUNT,
    CMD_NOP, CMD_PREFERRED, CMD_SCANOUT, CMD_SCANOUT_RAM, CMD_SET_MODE, CMD_UPLOAD,
    DMA_REFUSED, FORMAT_BGRA, VRAM, VRAM_MAGIC)
from emulator.io_controller import IOChannel, IOController           # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    CH_VRAM, DISPLAY_H, DISPLAY_MODES, DISPLAY_SIZE, DISPLAY_START, DISPLAY_W, HEAP_START,
    IO_START, IOHeader, RAM_SIZE)
from emulator.ram import RAM                                          # noqa: E402

WINDOW = IO_START + IOHeader.USABLE_AFTER
SCREEN = DISPLAY_W * DISPLAY_H * 4
# Small on purpose. The suite runs a worker per CPU, and a 128 MB RAM
# copied twice per case is how it ran the host out of memory; nothing
# here depends on how big RAM is, only on it holding HEAP_START.
TEST_RAM = 1 << 24
TEST_VRAM = 1 << 23


def device(mode=(DISPLAY_W, DISPLAY_H), vram_size=TEST_VRAM, modes=DISPLAY_MODES):
    ram = RAM(TEST_RAM, vram_size)
    display = DisplayIO(ram)
    return VRAM(ram, display, modes, mode)


def call(vram, command, address=0, window=()):
    """R/W 0, the arguments in the data window: how the guest asks."""
    struct.pack_into(f"<{len(window)}I", vram.ram.mem, WINDOW, *window)
    return vram.callback(0, command, 4, address, bytearray(4))


def words(reply):
    return struct.unpack(f"<{len(reply) // 4}I", reply)


# --- power-on --------------------------------------------------------------------

def test_info_answers_the_magic_first():
    vram = device()
    assert words(call(vram, CMD_INFO)) == (VRAM_MAGIC, TEST_VRAM, TEST_RAM, 0)


def test_the_aperture_in_info_moves_with_ram():
    ram = RAM(1 << 20, 1 << 18)
    vram = VRAM(ram, DisplayIO(ram), DISPLAY_MODES)
    assert words(call(vram, CMD_INFO))[2] == 1 << 20


def test_power_on_at_192x108_is_the_machine_from_before():
    """Scanning out of RAM at DISPLAY_START, byte for byte -- the BIOS and
    every .asm program draw there."""
    vram = device()
    display = vram.display
    assert display.scanout_vram is None and display.scanout_base == DISPLAY_START
    assert (display.width, display.height, display.display_size) == (DISPLAY_W, DISPLAY_H,
                                                                     DISPLAY_SIZE)
    display.ram.mem[DISPLAY_START:DISPLAY_START + 4] = b"\x01\x02\x03\x04"
    assert display.snapshot()[:4] == b"\x01\x02\x03\x04"


def test_the_screen_surface_exists_from_power_on():
    vram = device()
    assert words(call(vram, CMD_GET_MODE)) == (DISPLAY_W, DISPLAY_H, DISPLAY_W * 4,
                                               FORMAT_BGRA, 0)


def test_power_on_in_a_bigger_mode_scans_out_of_video_memory():
    vram = device(mode=(640, 360))
    assert vram.display.scanout_vram == 0
    assert vram.display.display_size == 640 * 360 * 4
    vram.ram.write_word(vram.ram.vram_base, 0xFF112233)     # through the aperture
    assert vram.display.snapshot()[:4] == b"\x33\x22\x11\xff"


@cases(((320, 180), "not one of"), ((1280, 720), "does not fit"))
def test_a_bad_power_on_mode_is_refused(mode, expected):
    try:
        device(mode=mode, vram_size=1 << 20, modes=((192, 108), (1280, 720)))
    except ValueError as e:
        assert expected in str(e), e
        return
    raise AssertionError(f"{mode} should have been refused")


def test_nop_answers():
    assert call(device(), CMD_NOP) == bytes(4)


# --- modes -------------------------------------------------------------------------

def test_the_offered_modes_are_listed():
    vram = device()
    assert words(call(vram, CMD_MODE_COUNT)) == (len(DISPLAY_MODES),)
    listed = [words(call(vram, CMD_MODE_AT, address=i)) for i in range(len(DISPLAY_MODES))]
    assert listed == [tuple(m) for m in DISPLAY_MODES]
    assert words(call(vram, CMD_MODE_AT, address=99)) == (0, 0)


def test_set_mode_switches_everything_at_once():
    vram = device()
    reply = words(call(vram, CMD_SET_MODE, window=(640, 360)))
    assert reply[:4] == (1, 640, 360, 640 * 4)
    display = vram.display
    assert (display.width, display.height, display.display_size) == (640, 360, 640 * 360 * 4)
    assert display.scanout_vram == reply[4]
    assert words(call(vram, CMD_INFO))[3] == 1, "generation did not move"
    assert words(call(display.vram, CMD_GET_MODE))[:2] == (640, 360)
    assert words(display.callback(0, DISPLAY_INFO, 12, 0, bytearray(12))) == (640, 360,
                                                                              640 * 360 * 4)


def test_a_new_mode_starts_black():
    vram = device()
    vram.ram.vram[:16] = b"\xff" * 16
    call(vram, CMD_SET_MODE, window=(320, 180))
    offset = vram.surfaces[0].offset
    assert vram.ram.vram[offset:offset + 320 * 180 * 4] == bytes(320 * 180 * 4)


@cases((641, 360), (0, 0), (1920, 1080))
def test_a_mode_not_offered_is_refused_and_nothing_changes(w, h):
    vram = device()
    reply = words(call(vram, CMD_SET_MODE, window=(w, h)))
    assert reply == (0, DISPLAY_W, DISPLAY_H, DISPLAY_W * 4, 0)
    assert vram.display.scanout_base == DISPLAY_START and vram.display.scanout_vram is None
    assert vram.generation == 0


def test_a_mode_that_no_longer_fits_is_refused():
    """Another surface is holding the room the new screen would need."""
    vram = device(vram_size=1 << 20, modes=((192, 108), (320, 180), (400, 300)))
    call(vram, CMD_ALLOC, window=(192, 108))           # right after the screen
    before = vram.surfaces[0].offset
    assert words(call(vram, CMD_SET_MODE, window=(400, 300)))[0] == 1, "fits after it"
    assert vram.surfaces[0].offset != before
    small = device(vram_size=400 * 300 * 4, modes=((192, 108), (400, 300)))
    call(small, CMD_ALLOC, window=(192, 108))
    assert words(call(small, CMD_SET_MODE, window=(400, 300)))[0] == 0
    assert small.mode == (192, 108) and small.surfaces[0].offset == 0


def test_the_preferred_size_is_only_stored():
    vram = device()
    assert words(call(vram, CMD_PREFERRED)) == (0, 0, 0)
    vram.set_preferred(640, 360)
    vram.set_preferred(854, 480)
    assert words(call(vram, CMD_PREFERRED)) == (854, 480, 2)
    assert vram.mode == (DISPLAY_W, DISPLAY_H), "the host must not switch the mode"


# --- surfaces ----------------------------------------------------------------------

def test_alloc_hands_out_the_room_after_the_screen():
    vram = device()
    handle, offset, pitch = words(call(vram, CMD_ALLOC, window=(100, 50)))
    assert handle not in (0, 0xFFFFFFFF)
    assert offset == SCREEN and pitch == 400


def test_a_full_alloc_answers_zeros():
    vram = device(vram_size=SCREEN * 2)
    assert words(call(vram, CMD_ALLOC, window=(DISPLAY_W, DISPLAY_H)))[0] != 0
    assert words(call(vram, CMD_ALLOC, window=(1, 1))) == (0, 0, 0)
    assert words(call(vram, CMD_ALLOC, window=(0, 10))) == (0, 0, 0)


def test_surfaces_never_move_and_freed_room_is_reused():
    vram = device()
    a = words(call(vram, CMD_ALLOC, window=(10, 10)))
    b = words(call(vram, CMD_ALLOC, window=(10, 10)))
    assert words(call(vram, CMD_FREE, address=a[0])) == (1,)
    assert vram.surfaces[b[0]].offset == b[1], "a free moved another surface"
    c = words(call(vram, CMD_ALLOC, window=(5, 5)))
    assert c[1] == a[1], "the hole was not reused"
    assert c[0] != a[0], "a handle was handed out twice"
    call(vram, CMD_SET_MODE, window=(320, 180))
    assert vram.surfaces[b[0]].offset == b[1], "a mode change moved a surface"


@cases(0, 12345)
def test_the_screen_and_strangers_cannot_be_freed(handle):
    vram = device()
    assert words(call(vram, CMD_FREE, address=handle)) == (0,)
    assert 0 in vram.surfaces


# --- the scanout ------------------------------------------------------------------

def test_scanout_is_a_page_flip_between_surfaces():
    vram = device(mode=(320, 180))
    back = words(call(vram, CMD_ALLOC, window=(320, 180)))
    assert words(call(vram, CMD_SCANOUT, address=back[0])) == (1,)
    assert vram.display.scanout_vram == back[1]
    assert words(call(vram, CMD_SCANOUT, address=0)) == (1,)
    assert vram.display.scanout_vram == vram.surfaces[0].offset


def test_a_surface_of_the_wrong_shape_is_not_shown():
    vram = device(mode=(320, 180))
    small = words(call(vram, CMD_ALLOC, window=(100, 100)))
    assert words(call(vram, CMD_SCANOUT, address=small[0])) == (0,)
    assert words(call(vram, CMD_SCANOUT, address=999)) == (0,)
    assert vram.display.scanout_vram == vram.surfaces[0].offset


def test_freeing_the_surface_on_screen_puts_the_screen_back():
    vram = device(mode=(320, 180))
    back = words(call(vram, CMD_ALLOC, window=(320, 180)))
    call(vram, CMD_SCANOUT, address=back[0])
    call(vram, CMD_FREE, address=back[0])
    assert vram.display.scanout_vram == vram.surfaces[0].offset


def test_scanout_ram_goes_back_to_system_memory():
    vram = device(mode=(320, 180))
    assert words(call(vram, CMD_SCANOUT_RAM, address=HEAP_START)) == (1,)
    assert vram.display.scanout_vram is None and vram.display.scanout_base == HEAP_START


def test_display_start_is_refused_in_a_mode_that_does_not_fit_there():
    """A 320 x 180 screen at DISPLAY_START runs over the boot sector and into
    the program -- and a FILL would write all of it."""
    vram = device(mode=(320, 180))
    display = vram.display
    assert words(call(vram, CMD_SCANOUT_RAM, address=DISPLAY_START)) == (0,)
    before = bytes(display.ram.mem[DISPLAY_START:DISPLAY_START + 400_000])
    display.callback(1, CMD_FILL, 4, DISPLAY_START, bytearray(b"\xff\xff\xff\xff"))
    assert bytes(display.ram.mem[DISPLAY_START:DISPLAY_START + 400_000]) == before


def test_display_get_base_and_set_base_round_trip_through_the_aperture():
    """CH_DISPLAY answers where a VRAM surface is as the guest sees it, and
    takes that answer back -- what k_tidy does with the scanout base."""
    vram = device(mode=(320, 180))
    display = vram.display
    on_screen = words(display.callback(0, CMD_GET_BASE, 4, 0, bytearray(4)))[0]
    assert on_screen == vram.ram.vram_base + vram.surfaces[0].offset
    back = words(call(vram, CMD_ALLOC, window=(320, 180)))
    assert words(display.callback(0, CMD_SET_BASE, 4, vram.ram.vram_base + back[1],
                                  bytearray(4))) == (1,)
    assert display.scanout_vram == back[1]
    assert words(display.callback(0, CMD_SET_BASE, 4, on_screen, bytearray(4))) == (1,)
    assert words(display.callback(0, CMD_SET_BASE, 4, on_screen + 4, bytearray(4))) == (0,)


# --- upload and download ------------------------------------------------------------

def test_upload_and_download_move_bytes_between_ram_and_a_surface():
    vram = device()
    handle, offset, _ = words(call(vram, CMD_ALLOC, window=(16, 16)))
    vram.ram.mem[HEAP_START:HEAP_START + 8] = b"ABCDEFGH"
    assert words(call(vram, CMD_UPLOAD, address=handle, window=(HEAP_START, 4, 8))) == (8,)
    assert vram.ram.vram[offset + 4:offset + 12] == b"ABCDEFGH"
    assert vram.ram.vram_dirty
    assert words(call(vram, CMD_DOWNLOAD, address=handle,
                      window=(HEAP_START + 100, 4, 8))) == (8,)
    assert vram.ram.mem[HEAP_START + 100:HEAP_START + 108] == b"ABCDEFGH"


def test_the_old_framebuffer_can_be_uploaded():
    vram = device()
    vram.ram.mem[DISPLAY_START:DISPLAY_START + 4] = b"\x09\x08\x07\x06"
    assert words(call(vram, CMD_UPLOAD, address=0,
                      window=(DISPLAY_START, 0, SCREEN))) == (SCREEN,)
    assert vram.ram.vram[:4] == b"\x09\x08\x07\x06"


@cases(
    ("no such surface", CMD_UPLOAD, 77, (HEAP_START, 0, 4)),
    ("past the surface", CMD_UPLOAD, 0, (HEAP_START, SCREEN - 2, 4)),
    ("past the end of RAM", CMD_UPLOAD, 0, (TEST_RAM - 2, 0, 4)),
    ("download into the IO window", CMD_DOWNLOAD, 0, (IO_START, 0, 4)),
    ("download past the end of RAM", CMD_DOWNLOAD, 0, (TEST_RAM - 2, 0, 4)),
)
def test_a_bad_transfer_moves_nothing(label, command, handle, window):
    vram = device()
    ram_before, vram_before = bytes(vram.ram.mem), bytes(vram.ram.vram)
    assert words(call(vram, command, address=handle, window=window)) == (DMA_REFUSED,), label
    ram_after = bytearray(vram.ram.mem)
    ram_after[WINDOW:WINDOW + 12] = ram_before[WINDOW:WINDOW + 12]
    assert ram_after == ram_before and vram.ram.vram == vram_before, label
    assert len(vram.ram.mem) == TEST_RAM, label


def test_an_unknown_command_is_survivable():
    assert call(device(), 99) == b""


# --- on the bus ----------------------------------------------------------------------

def test_a_guest_sets_the_mode_through_the_io_controller():
    vram = device()
    ram = vram.ram
    controller = IOController(ram)
    controller.register_channel(CH_VRAM, IOChannel(vram.callback, name="VRAM"))
    struct.pack_into("<II", ram.mem, WINDOW, 640, 360)
    for field, value in ((IOHeader.IO_R_W, 0), (IOHeader.COMMAND, CMD_SET_MODE),
                         (IOHeader.LENGTH, 20), (IOHeader.ADDRESS, 0),
                         (IOHeader.IO_CHANNEL, CH_VRAM)):
        ram.write_word(IO_START + field, value)
    controller.update()
    assert ram.read_word(IO_START + IOHeader.RETURN_DATA) == 20
    assert struct.unpack_from("<III", ram.mem, WINDOW) == (1, 640, 360)
    assert vram.display.width == 640


# --- in the machine ----------------------------------------------------------------------

BIOS = REPO_ROOT / "build" / "bios.bin"


def test_a_machine_has_video_memory_and_the_device_by_default():
    import tempfile
    from emulator.machine import Machine
    with tempfile.TemporaryDirectory() as d:
        machine = Machine(bios_path=str(BIOS), disk_path=str(Path(d) / "hdd.img"))
        try:
            assert machine.vram is not None and CH_VRAM in machine.io_controller.channels
            assert machine.display_io.scanout_base == DISPLAY_START
            machine.ram.vram[:4] = b"VRAM"
            ram_dump, vram_dump = machine.dump_ram_to(Path(d) / "ram.bin")
            assert vram_dump.name == "ram_vram.bin"
            assert vram_dump.read_bytes()[:4] == b"VRAM"
            assert ram_dump.stat().st_size == RAM_SIZE
        finally:
            machine.close()


def test_a_machine_without_video_memory_has_no_device():
    import tempfile
    from emulator.machine import Machine
    with tempfile.TemporaryDirectory() as d:
        machine = Machine(bios_path=str(BIOS), disk_path=str(Path(d) / "hdd.img"),
                          vram_size=0)
        try:
            assert machine.vram is None and CH_VRAM not in machine.io_controller.channels
            assert machine.dump_ram_to(Path(d) / "ram.bin") == [Path(d) / "ram.bin"]
        finally:
            machine.close()
        try:
            Machine(bios_path=str(BIOS), disk_path=str(Path(d) / "hdd.img"),
                    vram_size=0, display_mode=(640, 360))
        except ValueError as e:
            assert "needs video memory" in str(e), e
            return
    raise AssertionError("a big screen without video memory should be refused")


def test_a_machine_can_power_on_in_a_bigger_mode():
    import tempfile
    from emulator.machine import Machine
    with tempfile.TemporaryDirectory() as d:
        machine = Machine(bios_path=str(BIOS), disk_path=str(Path(d) / "hdd.img"),
                          display_mode=(640, 360))
        try:
            assert machine.display_io.width == 640
            assert machine.display_io.scanout_vram == machine.vram.surfaces[0].offset
        finally:
            machine.close()


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "VRAM device"))
