"""The C libraries, compiled by pigeon-cc and run on the emulator.

Every test compiles the library source together with a small program,
assembles it, and executes it. A pass means the C compiles AND behaves.

    python3 tests/test_libs.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                             # noqa: E402
from assembler.assembler import Assembler                         # noqa: E402
from compiler.cc import compile_units                             # noqa: E402
from emulator.cpu import CPU                                      # noqa: E402
from emulator.devices import keycodes as K                        # noqa: E402
from emulator.machine import Machine                              # noqa: E402
from emulator.memory_map import (DISPLAY_START, DISPLAY_W,        # noqa: E402
                                 PROGRAM_LOAD_ADDR, RAM_SIZE, STACK_TOP)
from emulator.programs import libraries_for                       # noqa: E402
from emulator.ram import RAM                                      # noqa: E402

LIB = REPO_ROOT / "lib" / "pigeon"
STEP_LIMIT = 20_000_000


def build(source: str, *libraries) -> bytes:
    """Compile a program with its libraries, as one unit.

    Transitive dependencies are resolved the same way the launcher does
    it -- from the #include lines -- so a test naming only "display.c"
    still gets mem.c when display.c starts calling malloc.
    """
    with tempfile.TemporaryDirectory() as d:
        main = Path(d) / "main.c"
        main.write_text(source)

        units = [main] + [LIB / name for name in libraries]
        for unit in list(units):
            for dependency in libraries_for(unit):
                if dependency not in units:
                    units.append(dependency)

        asm = Path(d) / "main.asm"
        asm.write_text(compile_units(units))
        return Assembler(str(asm)).assemble()


def run(source: str, *libraries):
    """Run standalone (no BIOS), returning the CPU at HALT."""
    ram = RAM(RAM_SIZE)
    ram.load_bytes(build(source, *libraries), PROGRAM_LOAD_ADDR)
    cpu = CPU(ram)
    cpu.pc = PROGRAM_LOAD_ADDR
    for _ in range(STEP_LIMIT):
        if cpu.run() == 1:
            return cpu
    raise AssertionError(f"did not halt within {STEP_LIMIT:,} instructions")


def returns(source: str, expected: int, *libraries):
    cpu = run(source, *libraries)
    got = cpu.reg.read(0)
    assert got == (expected & 0xFFFFFFFF), (
        f"returned {got} ({got:#x}), expected {expected} ({expected & 0xFFFFFFFF:#x})")
    assert cpu.sp == STACK_TOP, f"hardware stack unbalanced: SP={cpu.sp:#x}"


MEM = "#include <pigeon/mem.h>\n"
DISPLAY = "#include <pigeon/display.h>\n"
INPUT = "#include <pigeon/input.h>\n"


# --- <pigeon/mem.h> ---------------------------------------------------------

@cases(
    ("memset then memcmp equal",
     "char a[16]; char b[16]; memset(a,120,16); memset(b,120,16);"
     " return memcmp(a,b,16)==0 ? 1 : 0;", 1),
    ("memcmp finds a difference",
     "char a[4]; char b[4]; memset(a,1,4); memset(b,1,4); b[2]=9;"
     " return memcmp(a,b,4)!=0 ? 1 : 0;", 1),
    ("memcpy word path",
     "int a[8]; int b[8]; for(int i=0;i<8;i++) a[i]=i*3; memcpy(b,a,32);"
     " int s=0; for(int i=0;i<8;i++) s+=b[i]; return s;", 84),
    ("memcpy byte tail",
     "char a[7]; char b[7]; for(int i=0;i<7;i++) a[i]=i+65; memcpy(b,a,7);"
     " return b[0]+b[6];", 65 + 71),
    ("memset broadcasts the byte",
     "int v[4]; memset(v,0xAB,16); return v[0]==0xABABABAB ? 1 : 0;", 1),
    ("memset partial length",
     "char b[8]; memset(b,0,8); memset(b,7,3); return b[0]+b[2]+b[3];", 14),
)
def test_mem_blocks(label, body, expected):
    returns(f"{MEM}int main(void) {{ {body} }}", expected, "mem.c")


def test_memmove_handles_overlap_both_ways():
    """memcpy may not overlap; memmove must. Copying forwards over
    yourself needs a backwards loop or every byte becomes the first."""
    returns(MEM + "int main(void){ char b[8]; for(int i=0;i<8;i++) b[i]=i+1;"
                  " memmove(b+2,b,4); return b[2]*10+b[5]; }", 1 * 10 + 4, "mem.c")
    returns(MEM + "int main(void){ char b[8]; for(int i=0;i<8;i++) b[i]=i+1;"
                  " memmove(b,b+2,4); return b[0]*10+b[3]; }", 3 * 10 + 6, "mem.c")


def test_malloc_and_free():
    returns(MEM + "int main(void){ int *p=(int*)malloc(16); if(p==NULL) return 99;"
                  " for(int i=0;i<4;i++) p[i]=i*11; int s=0;"
                  " for(int i=0;i<4;i++) s+=p[i]; free(p); return s; }", 66, "mem.c")


def test_free_list_reuses_a_block():
    returns(MEM + "int main(void){ int *a=(int*)malloc(64); free(a);"
                  " int *b=(int*)malloc(64); return a==b ? 1 : 0; }", 1, "mem.c")


def test_separate_allocations_do_not_overlap():
    returns(MEM + "int main(void){ int *a=(int*)malloc(16); int *b=(int*)malloc(16);"
                  " a[0]=111; b[0]=222; return a[0]+b[0]; }", 333, "mem.c")


def test_calloc_zeroes():
    returns(MEM + "int main(void){ int *p=(int*)calloc(8,4); int s=0;"
                  " for(int i=0;i<8;i++) s+=p[i]; return s; }", 0, "mem.c")


def test_heap_used_grows():
    returns(MEM + "int main(void){ unsigned before=heap_used(); malloc(32);"
                  " return heap_used()>before ? 1 : 0; }", 1, "mem.c")


# --- <pigeon/display.h> -----------------------------------------------------

def framebuffer(ram):
    size = DISPLAY_W * DISPLAY_W * 4
    return bytes(ram.mem[DISPLAY_START:DISPLAY_START + size])


def lit_pixels(ram):
    fb = framebuffer(ram)
    return sum(1 for i in range(0, len(fb), 4) if any(fb[i:i + 3]))


def test_set_and_get_round_trip():
    returns(DISPLAY + "int main(void){ disp_set(5,5,RED); return disp_get(5,5)==RED ? 1:0; }",
            1, "display.c")


def test_clipping_is_silent_not_fatal():
    """user/checkerboard.asm has no clipping: it walks past the end of the
    framebuffer and eventually overwrites its own code. One bounds check
    in one place is the reason this library exists."""
    returns(DISPLAY + "int main(void){ disp_set(500,500,RED); disp_set(0,999,RED);"
                      " return disp_get(500,500)==0 ? 1 : 0; }", 1, "display.c")


def test_hline_and_vline_lengths():
    returns(DISPLAY + "int main(void){ disp_hline(0,0,5,RED); int n=0;"
                      " for(unsigned i=0;i<10;i++) if(disp_get(i,0)) n++; return n; }",
            5, "display.c")
    returns(DISPLAY + "int main(void){ disp_vline(0,0,5,RED); int n=0;"
                      " for(unsigned i=0;i<10;i++) if(disp_get(0,i)) n++; return n; }",
            5, "display.c")


def test_lines_clip_rather_than_bail():
    """A run that starts on-screen and leaves it draws the visible part."""
    returns(DISPLAY + "int main(void){ disp_hline(95,0,50,RED); int n=0;"
                      " for(unsigned i=0;i<100;i++) if(disp_get(i,0)) n++; return n; }",
            5, "display.c")


def test_rect_area():
    cpu = run(DISPLAY + "int main(void){ disp_rect(10,10,20,15,RED); return 0; }",
              "display.c")
    assert lit_pixels(cpu.ram) == 20 * 15


def test_frame_is_a_perimeter():
    returns(DISPLAY + "int main(void){ disp_frame(0,0,4,4,RED); int n=0;"
                      " for(unsigned y=0;y<4;y++) for(unsigned x=0;x<4;x++)"
                      " if(disp_get(x,y)) n++; return n; }", 12, "display.c")


def test_clear_fills_every_pixel():
    cpu = run(DISPLAY + "int main(void){ disp_clear(BLUE); return 0; }", "display.c")
    assert lit_pixels(cpu.ram) == DISPLAY_W * DISPLAY_W


def test_line_reaches_both_ends():
    returns(DISPLAY + "int main(void){ disp_line(0,0,99,99,WHITE);"
                      " return disp_get(0,0) && disp_get(99,99) && disp_get(50,50) ? 1:0; }",
            1, "display.c")


def test_circle_is_centred():
    """Points on the axes must be r away from the centre, and the centre
    itself untouched -- an outline, not a disc."""
    returns(DISPLAY + "int main(void){ disp_circle(50,50,10,WHITE);"
                      " return disp_get(60,50) && disp_get(40,50) && disp_get(50,60)"
                      " && !disp_get(50,50) ? 1 : 0; }", 1, "display.c")


def test_text_draws_something_legible():
    cpu = run(DISPLAY + 'int main(void){ disp_clear(BLACK); disp_text(1,1,"Hi",WHITE);'
                        ' return 0; }', "display.c")
    drawn = lit_pixels(cpu.ram)
    assert 6 <= drawn <= 2 * 6 * 4, f"two glyphs drew {drawn} pixels"


def test_alpha_zero_is_invisible():
    """The emulator does not blend; it hands alpha to the canvas. A colour
    with AA=0 is transparent, which is how sincos.asm's 'black' constant
    ended up invisible."""
    cpu = run(DISPLAY + "int main(void){ disp_set(1,1,0x00FFFFFF); return 0; }",
              "display.c")
    fb = framebuffer(cpu.ram)
    index = (1 * DISPLAY_W + 1) * 4
    assert fb[index + 3] == 0, "alpha byte should be zero"


# --- <pigeon/input.h> -------------------------------------------------------

def run_with_hid(source, setup):
    """Input needs the IO bus, so this one boots a real Machine."""
    image = build(source, "input.c")
    machine = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"))
    try:
        machine.ram.load_bytes(image, PROGRAM_LOAD_ADDR)
        machine.cpu.pc = PROGRAM_LOAD_ADDR
        setup(machine.hid)
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(STEP_LIMIT):
                if machine.step() == 1:
                    break
        return machine.cpu.reg.read(0)
    finally:
        machine.close()


def test_mouse_position():
    source = INPUT + "int main(void){ return mouse_x()*1000 + mouse_y(); }"
    assert run_with_hid(source, lambda hid: hid.set_mouse_pos(37, 42)) == 37042


def test_mouse_button_mask():
    source = INPUT + "int main(void){ return mouse_buttons(); }"
    assert run_with_hid(source, lambda hid: (hid.push_mouse_event(0, True),
                                             hid.push_mouse_event(1, True))) == 0b11


def test_mouse_event_decoding():
    source = (INPUT + "int main(void){ unsigned e = mouse_event();"
                      " return ME_VALID(e) && ME_PRESSED(e) ? ME_BUTTON(e)+10 : 0; }")
    assert run_with_hid(source, lambda hid: hid.push_mouse_event(2, True)) == 12


def test_key_read_and_empty():
    source = INPUT + "int main(void){ return key_read(); }"
    assert run_with_hid(source, lambda hid: hid.push_key(ord('Q'))) == ord('Q')
    empty = INPUT + "int main(void){ return key_read()==-1 ? 7 : 0; }"
    assert run_with_hid(empty, lambda hid: None) == 7


def test_key_event_carries_press_and_release():
    press = (INPUT + "int main(void){ unsigned e=key_event();"
                     " return KE_PRESSED(e) ? KE_CODE(e) : 0; }")
    assert run_with_hid(press, lambda hid: hid.push_key(K.KEY_LEFT, True)) == K.KEY_LEFT

    release = (INPUT + "int main(void){ key_event(); unsigned e=key_event();"
                       " return KE_PRESSED(e) ? 0 : KE_CODE(e); }")
    assert run_with_hid(release, lambda hid: (hid.push_key(K.KEY_UP, True),
                                              hid.push_key(K.KEY_UP, False))) == K.KEY_UP


def test_key_down_tracks_the_real_time_state():
    source = INPUT + "int main(void){ return key_down(KEY_RIGHT); }"
    assert run_with_hid(source, lambda hid: hid.push_key(K.KEY_RIGHT, True)) == 1
    assert run_with_hid(source, lambda hid: (hid.push_key(K.KEY_RIGHT, True),
                                             hid.push_key(K.KEY_RIGHT, False))) == 0


def test_key_bitmap_reports_every_held_key():
    source = (INPUT + "int main(void){ unsigned char b[32]; key_bitmap(b); int n=0;"
                      " for(int i=0;i<256;i++) if(b[i>>3] & (1<<(i&7))) n++; return n; }")
    held = (ord('w'), ord('a'), K.KEY_UP)
    assert run_with_hid(source, lambda hid: [hid.push_key(c, True) for c in held]) == 3


def test_hold_to_move_is_what_the_real_time_buffer_is_for():
    """Ten frames with the key held gives ten steps. A FIFO could not
    answer this without replaying every edge since boot."""
    source = (INPUT + "int main(void){ int x=50; for(int f=0;f<10;f++){"
                      " if(key_down(KEY_LEFT)) x--; if(key_down(KEY_RIGHT)) x++; }"
                      " return x; }")
    assert run_with_hid(source, lambda hid: hid.push_key(K.KEY_RIGHT, True)) == 60


# --- the libraries together -------------------------------------------------

def test_two_libraries_in_one_program():
    """There is no linker -- units are compiled together -- so two
    libraries in one program must not collide."""
    returns(MEM + DISPLAY + "int main(void){ int *p=(int*)malloc(16);"
            " for(int i=0;i<4;i++) p[i]=i; disp_clear(BLACK);"
            " disp_rect(0,0,10,10,RED); free(p);"
            " return disp_get(5,5)==RED ? 1 : 0; }", 1, "mem.c", "display.c")




# --- the demo program -------------------------------------------------------

def run_demo(setup, budget=25_000_000):
    """Boot user/demo.c through the real BIOS with injected input."""
    machine = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"),
                      program_path=str(REPO_ROOT / "build" / "demo.bin"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(3_000_000):          # BIOS load, then into the program
                if machine.step() == 1 or machine.cpu.pc >= PROGRAM_LOAD_ADDR:
                    break
            setup(machine.hid)
            for _ in range(budget):
                if machine.step() == 1:
                    break
        return machine
    finally:
        machine.close()


def demo_pixels(machine):
    size = DISPLAY_W * DISPLAY_W * 4
    fb = bytes(machine.ram.mem[DISPLAY_START:DISPLAY_START + size])
    return sum(1 for i in range(0, len(fb), 4) if fb[i:i + 3] != b"\x0a\x0c\x10")


def test_demo_binary_exists_and_is_large():
    """Built by:
       python3 compiler/cc.py user/demo.c lib/pigeon/display.c \\
               lib/pigeon/input.c lib/pigeon/mem.c -o build/demo.bin
    It is ~31 KB, which only loads because the BIOS reads in chunks -- the
    old single-window loader capped programs at 4 KB."""
    image = (REPO_ROOT / "build" / "demo.bin")
    assert image.exists(), "build/demo.bin is missing; compile it first"
    assert len(image.read_bytes()) > 4096, "the demo should exceed one DMA window"


def test_demo_draws_a_full_screen():
    machine = run_demo(lambda hid: [hid.push_key(c) for c in b"hi"])
    assert demo_pixels(machine) > 500, "the demo drew almost nothing"


def test_demo_counts_typed_characters():
    """key_count is returned from main(), so it survives to the register."""
    machine = run_demo(lambda hid: [hid.push_key(c) for c in b"abcde"] +
                       [hid.push_key(K.KEY_ESC)], budget=40_000_000)
    assert machine.cpu.halted, "ESC should have ended the loop"
    assert machine.cpu.reg.read(0) == 6, "five characters plus the ESC"


def test_demo_navigation_and_activation_stay_in_order():
    """DOWN then ENTER must stamp the NEW selection.

    An earlier version read ENTER from the character FIFO and the arrows
    from the edge FIFO and drained them in separate passes, so every
    character was consumed before the first edge -- ENTER stamped the old
    selection. Two FIFOs are ordered within themselves, not against each
    other.
    """
    def press(hid):
        hid.push_key(K.KEY_DOWN, True)
        hid.push_key(K.KEY_DOWN, False)
        hid.push_key(K.KEY_ENTER, True)
        hid.push_key(K.KEY_ENTER, False)

    machine = run_demo(press)
    # BOX draws a filled inner rect at the canvas centre; CIRCLE leaves it clear.
    centre = DISPLAY_START + ((60 + 16) * DISPLAY_W + 50) * 4
    pixel = bytes(machine.ram.mem[centre:centre + 3])
    assert pixel == b"\x90\x60\x20", (
        f"canvas centre is {pixel.hex()}, expected the BOX fill -- "
        f"ENTER stamped the wrong selection")


# --- double buffering -------------------------------------------------------

def test_back_buffer_allocates():
    returns(DISPLAY + "int main(void){ return disp_use_back_buffer(); }", 1,
            "display.c", "mem.c")


def test_drawing_stays_off_screen_until_present():
    """The whole point: the framebuffer must never hold a half-drawn
    frame, because the emulator snapshots it on its own clock."""
    returns(DISPLAY + """int main(void){
        disp_use_back_buffer();
        disp_set(5,5,RED);
        unsigned before = *(volatile unsigned *)(DISP_BASE + (5*DISP_W+5)*4);
        disp_present();
        unsigned after = *(volatile unsigned *)(DISP_BASE + (5*DISP_W+5)*4);
        return (before == 0 && after == RED) ? 1 : 0; }""", 1, "display.c", "mem.c")


def test_clear_honours_the_draw_target():
    """disp_clear wrote to DISP_BASE directly, so with a back buffer in
    play the clear landed on screen while every shape landed in the
    buffer -- the screen ended up showing uninitialised heap."""
    returns(DISPLAY + """int main(void){
        disp_use_back_buffer();
        disp_clear(BLUE);
        unsigned screen = *(volatile unsigned *)(DISP_BASE + 40);
        disp_present();
        unsigned after = *(volatile unsigned *)(DISP_BASE + 40);
        return (screen != BLUE && after == BLUE) ? 1 : 0; }""", 1,
            "display.c", "mem.c")


def test_present_copies_the_whole_screen():
    cpu = run(DISPLAY + "int main(void){ disp_use_back_buffer();"
                        " disp_clear(BLUE); disp_present(); return 0; }",
              "display.c", "mem.c")
    assert lit_pixels(cpu.ram) == DISPLAY_W * DISPLAY_W


def test_without_a_back_buffer_drawing_is_immediate():
    """Double buffering is opt-in; the default stays direct."""
    returns(DISPLAY + "int main(void){ disp_set(5,5,RED);"
                      " return *(volatile unsigned *)(DISP_BASE + (5*DISP_W+5)*4)"
                      " == RED ? 1 : 0; }", 1, "display.c", "mem.c")


def test_demo_screen_is_stable_when_idle():
    """The demo used to redraw unconditionally, straight onto the
    framebuffer, so the display's 30 Hz snapshot regularly caught the gap
    between disp_clear() and the first shape -- a black flash. It now
    redraws only on a change, into a back buffer."""
    machine = run_demo(lambda hid: [hid.push_key(c) for c in b"hi"],
                       budget=14_000_000)
    def lit():
        size = DISPLAY_W * DISPLAY_W * 4
        fb = bytes(machine.ram.mem[DISPLAY_START:DISPLAY_START + size])
        return sum(1 for i in range(0, len(fb), 4) if fb[i:i + 3] != b"\x0a\x0c\x10")

    with contextlib.redirect_stdout(io.StringIO()):
        counts = []
        for _ in range(6):
            for _ in range(2_500_000 // 30):
                if machine.step() == 1:
                    break
            counts.append(lit())
    machine.close()
    assert len(set(counts)) == 1, f"screen still changing while idle: {counts}"
    assert counts[0] > 500, f"screen is blank: {counts[0]}"


# --- the 3D cube demo -------------------------------------------------------

CUBE = REPO_ROOT / "build" / "cube.bin"


def run_cube(setup, budget=25_000_000):
    machine = Machine(bios_path=str(REPO_ROOT / "build" / "bios.bin"),
                      program_path=str(CUBE))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            while machine.cpu.pc < PROGRAM_LOAD_ADDR:
                if machine.step() == 1:
                    break
            setup(machine)
            for _ in range(budget):
                if machine.step() == 1:
                    break
        return machine
    finally:
        machine.close()


def screen_of(machine):
    size = DISPLAY_W * DISPLAY_W * 4
    return bytes(machine.ram.mem[DISPLAY_START:DISPLAY_START + size])


def test_cube_binary_exists():
    assert CUBE.exists(), "build/cube.bin is missing; run start_emulator.py cube"


def test_cube_draws_a_wireframe():
    machine = run_cube(lambda m: None)
    fb = screen_of(machine)
    edge = sum(1 for i in range(0, len(fb), 4) if fb[i:i + 3] in
               (b"\xff\xc0\x30", b"\xff\xe0\x60"))
    assert edge > 150, f"only {edge} cube-coloured pixels -- is it drawing?"


def test_cube_rotates_when_dragged():
    """The whole point: hold the left button and move, and the cube turns.

    Auto-spin is switched off first, so any change is the drag and not the
    clock.
    """
    def drag(m):
        m.hid.push_key(ord(' '))            # stop the auto-spin
        m.hid.push_mouse_event(0, True)     # button down
        m.hid.set_mouse_pos(40, 50)

    machine = run_cube(drag, budget=14_000_000)
    before = screen_of(machine)
    with contextlib.redirect_stdout(io.StringIO()):
        machine.hid.set_mouse_pos(64, 50)   # drag right -> yaw
        for _ in range(14_000_000):
            if machine.step() == 1:
                break
    after = screen_of(machine)
    machine.close()

    changed = sum(1 for i in range(0, len(before), 4)
                  if before[i:i + 3] != after[i:i + 3])
    assert changed > 100, f"drag moved only {changed} pixels -- rotation is stuck"


def test_cube_is_still_when_not_dragged_and_not_spinning():
    """Releasing the button must stop the rotation, not coast."""
    def settle(m):
        m.hid.push_key(ord(' '))            # auto-spin off
        m.hid.set_mouse_pos(50, 50)

    machine = run_cube(settle, budget=14_000_000)
    before = screen_of(machine)
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(14_000_000):
            if machine.step() == 1:
                break
    after = screen_of(machine)
    machine.close()
    assert before == after, "the cube moved with no input and no auto-spin"


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "C libraries"))
