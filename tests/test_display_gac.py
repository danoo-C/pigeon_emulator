"""<pigeon/display.h> on the machine's devices (docs/gac/plans/phase5_display_lib.md).

C programs, compiled with the libraries as tests/test_libs.py compiles them,
run two ways: on a bare CPU, where the library draws in software, and on a
Machine, where it has CH_VRAM and CH_GAC. What they draw must not depend on
which.

    python3 tests/test_display_gac.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import struct
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_libs                                                      # noqa: E402
from _runner import run_module                                        # noqa: E402
from emulator.machine import Machine                                  # noqa: E402
from emulator.memory_map import (                                     # noqa: E402
    DISPLAY_H, DISPLAY_SIZE, DISPLAY_START, DISPLAY_W, PROGRAM_LOAD_ADDR)

BIOS = REPO_ROOT / "build" / "bios.bin"
STEP_LIMIT = 20_000_000
VRAM = "#include <pigeon/vram.h>\n"
GAC = "#include <pigeon/gac.h>\n"


@contextlib.contextmanager
def machine_running(source, *libraries, **machine_args):
    """The program on a Machine, with its devices, run to HALT without the
    BIOS: loaded at PROGRAM_LOAD_ADDR and started there, as test_libs.run()
    does on a bare CPU. Yields the machine, closed afterwards."""
    image = test_libs.build(source, *libraries)
    with tempfile.TemporaryDirectory() as d:
        machine = Machine(bios_path=str(BIOS), disk_path=str(Path(d) / "hdd.img"),
                          **machine_args)
        try:
            machine.ram.load_bytes(image, PROGRAM_LOAD_ADDR)
            machine.cpu.pc = PROGRAM_LOAD_ADDR
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(STEP_LIMIT):
                    if machine.step() == 1:
                        break
                else:
                    raise AssertionError(f"did not halt within {STEP_LIMIT:,} instructions")
            yield machine
        finally:
            machine.close()


def returned(machine):
    return machine.cpu.reg.read(0)


# --- <pigeon/vram.h> ------------------------------------------------------------------------

def test_vram_answers_on_a_machine_with_video_memory():
    with machine_running(VRAM + """
        int main(void){
            unsigned w; unsigned h; unsigned offset;
            if (!vram_present()) return 1;
            if (vram_aperture() != 0x08000000u) return 2;
            if (!vram_mode(&w, &h, &offset)) return 3;
            if (w != DISPLAY_W || h != DISPLAY_H) return 4;
            if (vram_mode_count() != 5u) return 5;
            if (!vram_set_mode(640u, 360u)) return 6;
            if (vram_generation() != 1u) return 7;
            if (vram_set_mode(641u, 360u)) return 8;
            return 100;
        }""", "vram.c") as m:
        assert returned(m) == 100
        assert m.display_io.width == 640


def test_vram_says_not_here_on_a_machine_without_it():
    with machine_running(VRAM + """
        int main(void){
            unsigned w; unsigned h; unsigned offset;
            if (vram_present()) return 1;
            if (vram_mode(&w, &h, &offset)) return 2;
            if (vram_alloc(10u, 10u, &offset) != 0u) return 3;
            return 100;
        }""", "vram.c", vram_size=0) as m:
        assert returned(m) == 100


def test_vram_says_not_here_on_a_bare_cpu():
    cpu = test_libs.run(VRAM + "int main(void){ return vram_present() ? 1 : 100; }", "vram.c")
    assert cpu.reg.read(0) == 100


def test_the_kernels_owner_calls_free_what_a_depth_allocated():
    with machine_running(VRAM + """
        int main(void){
            unsigned offset;
            unsigned a; unsigned b;
            vram_owner(1u);
            a = vram_alloc(16u, 16u, &offset);
            vram_owner(2u);
            b = vram_alloc(16u, 16u, &offset);
            if (a == 0u || b == 0u) return 1;
            if (vram_free_owned(2u) != 1u) return 2;
            if (vram_free(b)) return 3;             /* already gone */
            if (!vram_free(a)) return 4;            /* still there */
            return 100;
        }""", "vram.c") as m:
        assert returned(m) == 100


# --- <pigeon/gac.h> -------------------------------------------------------------------------

def pixel(fb, x, y, w=DISPLAY_W):
    return struct.unpack_from("<I", fb, (y * w + x) * 4)[0]


def test_gac_draws_on_the_power_on_screen_through_a_ram_surface():
    with machine_running(GAC + """
        char A[] = { 0xF8, 0x88, 0x88, 0xF8, 0x88, 0x88, 0x88, 0x00 };
        int main(void){
            unsigned s;
            if (!gac_present()) return 1;
            s = gac_ram_surface(DISPLAY_START, DISPLAY_W, DISPLAY_H);
            if (s == 0u) return 2;
            if (!gac_fill(s, 0, 0, DISPLAY_W, DISPLAY_H, 0xFF000080u)) return 3;
            if (!gac_disc(s, 50, 50, 10, 0xFFFF0000u)) return 4;
            if (!gac_set_font(A, 5u, 8u, 6u, 8u, 0x41u, 1u)) return 5;
            if (!gac_text(s, 100, 10, 0xFFFFFFFFu, 0u, "AA", 2u)) return 6;
            if (gac_fill(12345u, 0, 0, 1, 1, 0xFFFFFFFFu)) return 7;
            return 100;
        }""", "gac.c") as m:
        assert returned(m) == 100
        fb = bytes(m.ram.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE])
        assert pixel(fb, 0, 0) == 0xFF000080 and pixel(fb, 50, 50) == 0xFFFF0000
        assert pixel(fb, 100, 10) == 0xFFFFFFFF and pixel(fb, 106, 10) == 0xFFFFFFFF


def test_gac_text_longer_than_the_window_goes_in_pieces():
    """5,000 characters: two TEXT calls, the second starting where the first
    left off, a cell for each character sent."""
    with machine_running(GAC + """
        char A[] = { 0x80, 0, 0, 0, 0, 0, 0, 0 };
        char s[5001];
        int main(void){
            unsigned i;
            unsigned h;
            for (i = 0u; i < 5000u; i++) s[i] = 'A';
            h = gac_ram_surface(DISPLAY_START, DISPLAY_W, DISPLAY_H);
            gac_set_font(A, 1u, 1u, 1u, 1u, 0x41u, 1u);
            return gac_text(h, -4900, 0, 0xFFFFFFFFu, 0u, s, 5000u) ? 100 : 1;
        }""", "gac.c") as m:
        assert returned(m) == 100
        fb = bytes(m.ram.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE])
        lit = [x for x in range(DISPLAY_W) if pixel(fb, x, 0) == 0xFFFFFFFF]
        assert lit == list(range(100)), lit[:5]


def test_gac_says_not_here_without_video_memory():
    with machine_running(GAC + """
        int main(void){
            if (gac_present()) return 1;
            if (gac_fill(0u, 0, 0, 5, 5, 0xFFFFFFFFu)) return 2;
            return 100;
        }""", "gac.c", vram_size=0) as m:
        assert returned(m) == 100



# --- the library draws the same pictures either way ------------------------------------------

SCENE = test_libs.DISPLAY + """
unsigned image[12 * 7];
int main(void){
    unsigned i;
    for (i = 0u; i < 12u * 7u; i++) image[i] = 0xFF000000u | (i * 0x030507u);
    disp_init();
    disp_clear(0xFF101018u);
    disp_rect(10u, 10u, 40u, 20u, RED);           /* big: the accelerator's */
    disp_rect(60u, 10u, 3u, 2u, GREEN);           /* small: stores (Q3) */
    disp_hline(0u, 40u, DISP_W, BLUE);
    disp_hline(5u, 42u, 4u, YELLOW);
    disp_vline(100u, 0u, DISP_H, CYAN);
    disp_vline(102u, 5u, 3u, MAGENTA);
    disp_rect(180u, 100u, 40u, 40u, WHITE);       /* off the far corner */
    disp_frame(5u, 60u, 50u, 30u, WHITE);
    disp_frame(150u, 80u, 60u, 50u, GREY);        /* off the edges */
    disp_line(0, 0, DISP_W - 1, DISP_H - 1, WHITE);
    disp_line(-30, 20, 250, 90, 0xFFFF8000u);
    disp_circle(96, 54, 30, RED);
    disp_circle(0, 0, 25, GREEN);
    disp_disc(170, 20, 18, 0xFF4080FFu);
    disp_disc(-5, 100, 20, 0xFFFF40FFu);
    disp_text(2u, 2u, "Hello, pigeon! gjpqy_", WHITE);
    disp_char(150u, 50u, 'Q', YELLOW);
    disp_set(1u, 107u, RED);
    disp_blit(image, 120, 60, 12u, 7u);
    disp_blit(image, -4, 100, 12u, 7u);           /* clipped left and bottom */
    disp_scroll(30u, 40u, -7, 0xFF202040u);
    disp_scroll(60u, 30u, 5, 0xFF402020u);
    return 0;
}
"""


def test_the_library_draws_the_same_picture_with_the_accelerator_as_without():
    software = test_libs.framebuffer(test_libs.run(SCENE, "display.c").ram)
    with machine_running(SCENE, "display.c") as m:
        # disp_init uploads the font only when it chose the accelerator.
        assert m.gac is not None and m.gac.font is not None, "drew in software"
        drawn = bytes(m.ram.mem[DISPLAY_START:DISPLAY_START + DISPLAY_SIZE])
    diff = [((i // 4) % DISPLAY_W, (i // 4) // DISPLAY_W)
            for i in range(0, DISPLAY_SIZE, 4) if software[i:i + 4] != drawn[i:i + 4]]
    assert not diff, f"{len(diff)} pixels differ, the first at {diff[:5]}"


def test_a_translucent_colour_blends_through_the_accelerator():
    """And is stored as it is in software (Q5): the one difference."""
    source = test_libs.DISPLAY + """
        int main(void){ disp_init(); disp_clear(0xFF000000u); disp_rect(0u, 0u, 1u, 1u, 0x80FFFFFFu);
                        return 0; }"""
    with machine_running(source, "display.c") as m:
        assert pixel(bytes(m.ram.mem[DISPLAY_START:DISPLAY_START + 4]), 0, 0) == 0xFF808080
    software = test_libs.framebuffer(test_libs.run(source, "display.c").ram)
    assert pixel(software, 0, 0) == 0x80FFFFFF


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "display on the GAC"))
