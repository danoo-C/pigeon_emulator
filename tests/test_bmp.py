"""<pigeon/bmp.h>: BMP images at the size asked for (docs/bmp_plan.md).

The decoder runs on a bare CPU, with a BMP that Python wrote placed in RAM,
and its pixels are checked one by one against the same crop or stretch done
in Python. bmp_load and bmp_info run under the kernel, reading files off a
test disk through its system calls.

    python3 tests/test_bmp.py      (or: python3 -m pytest tests/)
"""
import functools
import re
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from emulator.cpu import CPU                                          # noqa: E402
from emulator.memory_map import PROGRAM_LOAD_ADDR, RAM_SIZE           # noqa: E402
from emulator.ram import RAM                                          # noqa: E402
from test_debug_port import build_c                                   # noqa: E402

HEADER = REPO_ROOT / "lib" / "pigeon" / "bmp.h"
PIGEON = REPO_ROOT / "user" / "os" / "etc" / "bmp" / "pigeon.bmp"
FILE_AT = 0x04000000
CROP, CROP_TOP_LEFT, STRETCH = 0, 1, 2
ALPHA = 4                       # BMP_ALPHA, added to any of the three
# The error codes, read from bmp.h so the tests follow it.
CODES = {name: int(value) for name, value in
         re.findall(r"#define (BMP_E\w+|BMP_OK)\s+\(?(-?\d+)\)?", HEADER.read_text())}
BGRA_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF)


# --- files, and what they should decode to ----------------------------------------

def colour(x, y):
    """Every pixel of a test image different from its neighbours: (r, g, b)."""
    return ((x * 37 + y * 11) & 255, (x * 3 + y * 29 + 7) & 255, ((x ^ y) * 5 + 1) & 255)


def alpha_at(x, y):
    """A different alpha on every pixel too, for BMP_ALPHA: a byte taken
    from the wrong place cannot then come out right by luck."""
    return (x * 53 + y * 17 + 3) & 255


def write_bmp(w, h, bits=24, top_down=False, compression=0, masks=None, masks_inside=False,
              planes=1, info=40, alpha=0x80):
    """A BMP of `colour`, as an editor would save it."""
    per = bits // 8
    stride = (w * per + 3) & ~3
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            r, g, b = colour(x, y)
            a = alpha_at(x, y) if alpha == "vary" else alpha
            row += bytes((b, g, r)) + (bytes((a,)) if bits == 32 else b"")
        rows.append(bytes(row) + bytes(stride - len(row)))
    pixels = b"".join(rows if top_down else reversed(rows))
    header = struct.pack("<IiiHHIIiiII", info, w, -h if top_down else h, planes, bits, compression,
                         len(pixels), 2835, 2835, 0, 0)
    fields = struct.pack("<III", *masks) if masks else b""
    if masks_inside:
        header = header + fields + bytes(info - 40 - len(fields))
        fields = b""
    else:
        header = header + bytes(info - 40)
    offset = 14 + len(header) + len(fields)
    return struct.pack("<2sIHHI", b"BM", offset + len(pixels), 0, 0, offset) + header + fields + pixels


def reference(data, w, h, mode):
    """The same crop or stretch, in Python: 0xFFRRGGBB words, top row first.
    With BMP_ALPHA on a 32-bit file, the file's own alpha byte instead, and
    what falls outside the image clear rather than black."""
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    per = struct.unpack_from("<H", data, 28)[0] // 8
    top_down, height = height < 0, abs(height)
    stride = (width * per + 3) & ~3
    keep = bool(mode & ALPHA) and per == 4
    outside = 0x00000000 if keep else 0xFF000000
    mode = mode & ~ALPHA

    def source(i, n, size):
        if mode == STRETCH:
            return i * size // n
        if mode == CROP_TOP_LEFT:
            return i if i < size else None
        if size >= n:
            return i + (size - n) // 2
        margin = (n - size) // 2
        return i - margin if margin <= i < margin + size else None

    out = []
    for y in range(h):
        sy = source(y, h, height)
        for x in range(w):
            sx = source(x, w, width)
            if sy is None or sx is None:
                out.append(outside)
                continue
            at = offset + (sy if top_down else height - 1 - sy) * stride + sx * per
            b, g, r = data[at:at + 3]
            top = (data[at + 3] << 24) if keep else 0xFF000000
            out.append(top | r << 16 | g << 8 | b)
    return out


# --- the decoder, on a bare CPU ----------------------------------------------------

DECODE = r"""#include <pigeon/bmp.h>
unsigned file; unsigned size; unsigned w; unsigned h; unsigned mode;
unsigned result; int error;
void mark(void) { }
int main(void) {
    mark();
    result = (unsigned)bmp_decode((unsigned char *)file, size, w, h, (int)mode);
    mark();
    error = bmp_error();
    return 0;
}
"""


@functools.lru_cache(maxsize=None)
def program(text):
    return build_c(text)


def signed(word):
    return word - (1 << 32) if word & 0x80000000 else word


def decode(data, w, h, mode):
    """bmp_decode on a bare CPU: (pixels or None, bmp_error(), instructions it took)."""
    image, symbols = program(DECODE)
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, PROGRAM_LOAD_ADDR)
    ram.mem[FILE_AT:FILE_AT + len(data)] = data
    for name, value in (("file", FILE_AT), ("size", len(data)), ("w", w), ("h", h), ("mode", mode)):
        ram.write_word(symbols[f"__g_{name}"], value & 0xFFFFFFFF)
    cpu = CPU(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    mark, steps, marks = symbols["mark"], 0, []
    while cpu.run() != 1:
        steps += 1
        assert steps < 60_000_000, "did not halt"
        if cpu.pc == mark:
            marks.append(steps)
    result = ram.read_word(symbols["__g_result"])
    pixels = list(struct.unpack_from(f"<{w * h}I", ram.mem, result)) if result else None
    return pixels, signed(ram.read_word(symbols["__g_error"])), marks[1] - marks[0]


def first_difference(got, want, w):
    for i, (a, b) in enumerate(zip(got, want)):
        if a != b:
            return f"pixel ({i % w}, {i // w}): {a:#010x}, want {b:#010x}"
    return "same"


def check(data, w, h, mode):
    pixels, error, _ = decode(data, w, h, mode)
    assert error == CODES["BMP_OK"] and pixels is not None, f"error {error}"
    want = reference(data, w, h, mode)
    assert pixels == want, first_difference(pixels, want, w)


@cases((192, 108), (191, 107), (190, 106), (189, 3), (1, 1))
def test_an_image_cropped_to_its_own_size_comes_back_pixel_for_pixel(w, h):
    """Each width a different amount of row padding: 0, 3, 2 and 1 bytes."""
    check(write_bmp(w, h), w, h, CROP)


def test_rows_stored_top_down():
    check(write_bmp(40, 21, top_down=True), 40, 21, CROP)


@cases(("plain", {}),
       ("bit fields after the header", {"compression": 3, "masks": BGRA_MASKS}),
       ("bit fields in a V4 header", {"compression": 3, "masks": BGRA_MASKS, "masks_inside": True,
                                      "info": 108}))
def test_a_32_bit_image_loses_its_alpha(label, extra):
    data = write_bmp(33, 17, bits=32, alpha=0x3C, **extra)
    pixels, error, _ = decode(data, 33, 17, CROP)
    assert error == 0 and pixels == reference(data, 33, 17, CROP), label
    assert all(pixel >> 24 == 0xFF for pixel in pixels), f"{label}: not opaque"


@cases(("down, 640x360 to 96x54", (640, 360), (96, 54)),
       ("up, 32x18 to 96x54", (32, 18), (96, 54)),
       ("by odd amounts, 100x70 to 33x17", (100, 70), (33, 17)))
def test_stretch_takes_the_nearest_pixel(label, source, target):
    check(write_bmp(*source), *target, STRETCH)


@cases(("the middle of a bigger image", (300, 200), CROP),
       ("a smaller image, centred on black", (41, 19), CROP),
       ("the corner of a bigger image", (300, 200), CROP_TOP_LEFT),
       ("a smaller image in the corner, on black", (41, 19), CROP_TOP_LEFT),
       ("wider but shorter", (150, 20), CROP))
def test_crop(label, source, mode):
    check(write_bmp(*source), 96, 54, mode)


def test_a_smaller_image_is_centred_on_black_to_the_pixel():
    pixels, _, _ = decode(write_bmp(2, 2), 4, 4, CROP)
    black = 0xFF000000
    inside = [0xFF000000 | colour(x, y)[0] << 16 | colour(x, y)[1] << 8 | colour(x, y)[2]
              for y in range(2) for x in range(2)]
    assert pixels == [black] * 5 + inside[:2] + [black] * 2 + inside[2:] + [black] * 5, pixels


def header(data, at, fmt, value):
    raw = bytearray(data)
    struct.pack_into(fmt, raw, at, value)
    return bytes(raw)


BASE = write_bmp(8, 6)


@cases(("not BM", b"XX" + BASE[2:], "BMP_ENOTBMP"),
       ("a file too short for a header", BASE[:50], "BMP_ENOTBMP"),
       ("a 12-byte header", header(BASE, 14, "<I", 12), "BMP_ENOTBMP"),
       ("pixels starting inside the header", header(BASE, 10, "<I", 20), "BMP_ENOTBMP"),
       ("16 bits a pixel", header(BASE, 28, "<H", 16), "BMP_EFORMAT"),
       ("compressed", header(BASE, 30, "<I", 1), "BMP_EFORMAT"),
       ("24-bit with bit fields", header(BASE, 30, "<I", 3), "BMP_EFORMAT"),
       ("32-bit with other masks", write_bmp(8, 6, bits=32, compression=3,
                                             masks=(0xFF000000, 0x00FF0000, 0x0000FF00)), "BMP_EFORMAT"),
       ("two planes", header(BASE, 26, "<H", 2), "BMP_EFORMAT"),
       ("no width", header(BASE, 18, "<i", 0), "BMP_EFORMAT"),
       ("wider than 8,192", header(BASE, 18, "<i", 8193), "BMP_ETOOBIG"),
       ("taller than 8,192, top-down", header(BASE, 22, "<i", -8193), "BMP_ETOOBIG"),
       ("a byte short", BASE[:-1], "BMP_ETRUNCATED"),
       ("pixels past the end", header(BASE, 10, "<I", len(BASE) + 1), "BMP_ETRUNCATED"))
def test_a_file_that_cannot_be_decoded_says_why(label, data, code):
    pixels, error, _ = decode(data, 8, 6, CROP)
    assert pixels is None and error == CODES[code], f"{label}: {error}, want {code} {CODES[code]}"


@cases(("no width", (0, 6, CROP), "BMP_EARGS"),
       ("no height", (8, 0, CROP), "BMP_EARGS"),
       ("a mode that isn't one", (8, 6, 3), "BMP_EARGS"),
       ("a negative mode", (8, 6, -1), "BMP_EARGS"),
       ("wider than 4,096", (4097, 6, CROP), "BMP_ETOOBIG"))
def test_a_size_or_mode_that_cannot_be_given_says_why(label, args, code):
    pixels, error, _ = decode(BASE, *args)
    assert pixels is None and error == CODES[code], f"{label}: {error}"


def test_a_success_clears_the_last_error():
    image, symbols = program(r"""#include <pigeon/bmp.h>
unsigned file; unsigned size; int before; int after; unsigned got;
int main(void) {
    bmp_decode((unsigned char *)file, size, 0u, 1u, BMP_CROP);
    before = bmp_error();
    got = (unsigned)bmp_decode((unsigned char *)file, size, 8u, 6u, BMP_CROP);
    after = bmp_error();
    return 0;
}
""")
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, PROGRAM_LOAD_ADDR)
    ram.mem[FILE_AT:FILE_AT + len(BASE)] = BASE
    ram.write_word(symbols["__g_file"], FILE_AT)
    ram.write_word(symbols["__g_size"], len(BASE))
    cpu = CPU(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    for _ in range(5_000_000):
        if cpu.run() == 1:
            break
    word = lambda name: signed(ram.read_word(symbols[f"__g_{name}"]))
    assert word("got") != 0
    assert (word("before"), word("after")) == (CODES["BMP_EARGS"], CODES["BMP_OK"])


def test_the_error_codes_are_their_own():
    """Below every code the kernel, fs.h, cd.h and the installer use."""
    assert CODES["BMP_OK"] == 0
    assert sorted(v for k, v in CODES.items() if k != "BMP_OK") == list(range(-306, -299))


def test_pigeon_bmp_decodes_as_python_reads_it():
    data = PIGEON.read_bytes()
    assert struct.unpack_from("<iiHH", data, 18) == (192, 108, 1, 24), "etc/bmp/pigeon.bmp has changed"
    check(data, 192, 108, CROP)
    check(data, 64, 36, STRETCH)


def test_a_screen_costs_under_50_instructions_a_pixel():
    """Counted, not timed: 868,518 for a screen in the prototype, 41.9 a pixel."""
    _, _, steps = decode(write_bmp(192, 108), 192, 108, CROP)
    assert steps / (192 * 108) < 50, f"{steps:,} instructions, {steps / (192 * 108):.1f} a pixel"


def test_with_no_kernel_load_and_info_say_so_and_do_not_jump_to_zero():
    image, symbols = program(r"""#include <pigeon/bmp.h>
int a; int b; int c; unsigned w; unsigned h; unsigned got;
int main(void) {
    got = (unsigned)bmp_load("/etc/bmp/pigeon.bmp", 10u, 10u, BMP_CROP);
    a = bmp_error();
    b = bmp_info("/etc/bmp/pigeon.bmp", &w, &h);
    c = bmp_error();
    return 0;
}
""")
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, PROGRAM_LOAD_ADDR)
    cpu = CPU(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    for _ in range(2_000_000):
        if cpu.run() == 1:
            break
    assert cpu.halted, "never halted: it called through the empty system-call table"
    word = lambda name: signed(ram.read_word(symbols[f"__g_{name}"]))
    assert word("got") == 0
    assert (word("a"), word("b"), word("c")) == (CODES["BMP_ENOKERNEL"],) * 3


# --- through the kernel ---------------------------------------------------------------

CHECK = r"""#include <pigeon/bmp.h>
#include <pigeon/mem.h>
#include <pigeon/stdio.h>
#include <pigeon/string.h>
int main(int argc, char **argv) {
    unsigned w;
    unsigned h;
    unsigned tw;
    unsigned th;
    unsigned *pixels;
    unsigned i;
    unsigned sum = 0u;
    if (argc < 5) return 9;
    if (bmp_info(argv[1], &w, &h) != BMP_OK) {
        printf("info: %s\n", bmp_strerror(bmp_error()));
        return 1;
    }
    tw = (unsigned)atoi(argv[2]);
    th = (unsigned)atoi(argv[3]);
    pixels = bmp_load(argv[1], tw, th, atoi(argv[4]));
    if (pixels == NULL) {
        printf("load: %s\n", bmp_strerror(bmp_error()));
        return 2;
    }
    for (i = 0u; i < tw * th; i++) sum = sum * 31u + pixels[i];
    printf("%ux%u %x\n", w, h, sum);
    free(pixels);
    return 0;
}
"""


def checksum(pixels):
    total = 0
    for pixel in pixels:
        total = (total * 31 + pixel) & 0xFFFFFFFF
    return total


def test_bmp_load_and_bmp_info_read_through_the_kernel():
    """Short names, so no line fills a console row of 32: the console reader
    takes a full row for one that wrapped."""
    import test_kernel as tk
    tk.STANDINS["bmpc"] = CHECK
    small = write_bmp(50, 30)
    wide = write_bmp(70, 20, bits=32, compression=3, masks=BGRA_MASKS)
    pigeon = PIGEON.read_bytes()
    extra = [("/bin/bmpc.bin", tk.standin("bmpc")), ("/s.bmp", small), ("/w.bmp", wide),
             ("/p.bmp", pigeon), ("/t.txt", b"not a picture\n")]
    with tk.booted(extra=extra) as c:
        assert c.ready(), c.rows()
        for line, data, size, mode in (("bmpc /s.bmp 50 30 0", small, (50, 30), CROP),
                                       ("bmpc /p.bmp 64 36 2", pigeon, (64, 36), STRETCH),
                                       ("bmpc /w.bmp 40 25 1", wide, (40, 25), CROP_TOP_LEFT)):
            width, height = struct.unpack_from("<ii", data, 18)
            want = f"{width}x{abs(height)} {checksum(reference(data, *size, mode)):x}"
            assert c.command(line) == [want], line
        assert c.command("bmpc /none.bmp 8 8 0") == ["info: not found", "bmpc: exit 1"]
        assert c.command("bmpc /t.txt 8 8 0") == ["info: not a BMP", "bmpc: exit 1"]
        assert c.command("bmpc /docs 8 8 0") == ["info: is a directory", "bmpc: exit 1"]
        assert c.command("bmpc /s.bmp 0 8 0") == ["load: no such size or mode", "bmpc: exit 2"]


# --- img: the viewer ------------------------------------------------------------------

ESC = 0x1B


def screen_of(data, mode):
    """The screen's bytes showing `data` as img would."""
    return struct.pack(f"<{192 * 108}I", *reference(data, 192, 108, mode))


def viewer(extra=()):
    """The kernel, with img in /bin, pigeon.bmp in /docs, and `extra`."""
    import test_kernel as tk
    files = [("/bin/img.bin", tk.shell_program("img")), ("/docs/p.bmp", PIGEON.read_bytes())]
    return tk, tk.booted(extra=files + list(extra))


def test_img_shows_an_image_from_where_you_are_until_esc():
    tk, boot = viewer()
    want = screen_of(PIGEON.read_bytes(), CROP)
    with boot as c:
        assert c.ready(), c.rows()
        assert c.command("cd /docs") == []
        c.type("img ./p.bmp\n")
        assert c.run_until(lambda rows: c.machine.display_io.snapshot() == want, seconds=60), "no image"
        assert not c.run_until(lambda rows: c.machine.display_io.snapshot() != want, seconds=1), \
            "the image didn't stay"
        c.press(ord("x"))
        assert not c.run_until(lambda rows: c.machine.display_io.snapshot() != want, seconds=1), \
            "a key that isn't Esc ended it"
        c.press(ESC)
        assert c.ready(), c.rows()
        assert c.output("img ./p.bmp") == [] and tk.last_row(c.rows()) == "2:/docs> _", c.rows()


def test_ctrl_c_ends_img_too():
    tk, boot = viewer()
    want = screen_of(PIGEON.read_bytes(), CROP)
    with boot as c:
        assert c.ready(), c.rows()
        c.type("img /docs/p.bmp\n")
        assert c.run_until(lambda rows: c.machine.display_io.snapshot() == want, seconds=60), "no image"
        c.press(tk.KEY_LCTRL, ord("c"))
        assert c.ready(), c.rows()
        assert c.output("img /docs/p.bmp") == ["^C", "img: stopped"], c.rows()


@cases(("a small image, in the middle", "img /s.bmp", CROP),
       ("stretched to the screen", "img -s /s.bmp", STRETCH))
def test_img_centres_a_small_image_or_stretches_it(label, line, mode):
    small = write_bmp(50, 30)
    tk, boot = viewer([("/s.bmp", small)])
    want = screen_of(small, mode)
    with boot as c:
        assert c.ready(), c.rows()
        c.type(line + "\n")
        assert c.run_until(lambda rows: c.machine.display_io.snapshot() == want, seconds=60), label
        c.press(ESC)
        assert c.ready(), f"{label}: {c.rows()}"


def test_img_says_what_is_wrong_in_one_line():
    tk, boot = viewer([("/t.txt", b"not a picture\n")])
    with boot as c:
        assert c.ready(), c.rows()
        assert c.command("img") == ["usage: img [-s] FILE.bmp", "img: exit 1"]
        assert c.command("img -x /t.txt") == ["usage: img [-s] FILE.bmp", "img: exit 1"]
        assert c.command("img /nope.bmp") == ["img: /nope.bmp: not found", "img: exit 1"]
        assert c.command("img /t.txt") == ["img: /t.txt: not a BMP", "img: exit 1"]


# --- BMP_ALPHA: keeping a 32-bit file's alpha -------------------------------------
#
# docs/gac/plans/phase9_srcalpha.md §4. Without it every pixel comes back
# opaque, which is what gac_blit_alpha(..., GAC_SRC_ALPHA) has nothing to
# work with.


@cases(CROP, CROP_TOP_LEFT, STRETCH)
def test_bmp_alpha_keeps_a_32_bit_files_alpha(mode):
    data = write_bmp(12, 9, bits=32, alpha="vary")
    pixels, err, _ = decode(data, 12, 9, mode | ALPHA)
    assert err == CODES["BMP_OK"]
    want = reference(data, 12, 9, mode | ALPHA)
    assert pixels == want, first_difference(pixels, want, 12)
    assert len({p >> 24 for p in pixels}) > 1, "every pixel came back at one alpha"


def test_without_bmp_alpha_the_same_file_is_opaque():
    data = write_bmp(12, 9, bits=32, alpha="vary")
    pixels, err, _ = decode(data, 12, 9, CROP)
    assert err == CODES["BMP_OK"]
    assert {p >> 24 for p in pixels} == {0xFF}


def test_bmp_alpha_on_a_24_bit_file_is_still_opaque():
    """A 24-bit file has the next pixel's blue where alpha would be, so
    there is nothing to keep. Asking is not an error."""
    data = write_bmp(12, 9, bits=24)
    pixels, err, _ = decode(data, 12, 9, CROP | ALPHA)
    assert err == CODES["BMP_OK"]
    assert {p >> 24 for p in pixels} == {0xFF}
    assert pixels == reference(data, 12, 9, CROP)


def test_bmp_alpha_leaves_the_margin_clear_rather_than_black():
    """A result bigger than the image: what is not the image should not be
    drawn at all, so it is 0x00000000, not opaque black."""
    data = write_bmp(4, 3, bits=32, alpha="vary")
    pixels, err, _ = decode(data, 10, 7, CROP | ALPHA)
    assert err == CODES["BMP_OK"]
    assert pixels[0] == 0x00000000, f"{pixels[0]:#010x}"
    want = reference(data, 10, 7, CROP | ALPHA)
    assert pixels == want, first_difference(pixels, want, 10)
    # ... and without it, that same margin is still black.
    plain, _, _ = decode(data, 10, 7, CROP)
    assert plain[0] == 0xFF000000


def test_bmp_alpha_with_bit_fields():
    data = write_bmp(8, 6, bits=32, compression=3, masks=BGRA_MASKS, alpha="vary")
    pixels, err, _ = decode(data, 8, 6, CROP | ALPHA)
    assert err == CODES["BMP_OK"]
    want = reference(data, 8, 6, CROP | ALPHA)
    assert pixels == want, first_difference(pixels, want, 8)


def test_bmp_alpha_does_not_make_a_bad_mode_good():
    data = write_bmp(8, 6, bits=32, alpha="vary")
    pixels, err, _ = decode(data, 8, 6, 3 | ALPHA)
    assert pixels is None and err == CODES["BMP_EARGS"]


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "BMP images"))
