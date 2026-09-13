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
from emulator.memory_map import (DISPLAY_H, DISPLAY_START, DISPLAY_W,  # noqa: E402
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
MATH = "#include <pigeon/math.h>\n"
STRING = "#include <pigeon/string.h>\n"


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


# --- <pigeon/string.h> ------------------------------------------------------

def string_returns(body, expected):
    returns(f"{STRING}int main(void) {{ {body} }}", expected, "string.c")


@cases(
    ("strlen of the empty string", 'return strlen("");', 0),
    ("strlen",                     'return strlen("pigeon");', 6),
    ("strcmp equal",               'return strcmp("abc", "abc");', 0),
    ("strcmp both empty",          'return strcmp("", "");', 0),
    ("strcmp less",                'return strcmp("abc", "abd") < 0;', 1),
    ("strcmp greater",             'return strcmp("abd", "abc") > 0;', 1),
    ("a prefix sorts first",       'return strcmp("ab", "abc") < 0;', 1),
    ("a longer string sorts after", 'return strcmp("abc", "ab") > 0;', 1),
    ("strncmp equal within n",     'return strncmp("abcX", "abcY", 3);', 0),
    ("strncmp differs within n",   'return strncmp("abcX", "abcY", 4) < 0;', 1),
    ("strncmp of nothing",         'return strncmp("a", "b", 0);', 0),
    ("strncmp stops at the NUL",   'return strncmp("ab", "ab", 10);', 0),
)
def test_string_length_and_compare(label, body, expected):
    string_returns(body, expected)


@cases(
    ("strcmp", r'return strcmp("\xE9", "a") > 0;', 1),
    ("strncmp", r'return strncmp("\xE9", "a", 1) > 0;', 1),
)
def test_high_bytes_compare_unsigned(label, body, expected):
    """char is signed in this compiler. Compared as char, 0xE9 is -23 and
    sorts below 'a'; every C library compares bytes as unsigned char."""
    string_returns(body, expected)


@cases(
    ("strcpy copies and terminates",
     'char b[8]; strcpy(b, "hey"); return b[3] == 0 && strcmp(b, "hey") == 0;', 1),
    ("strcpy returns dst", 'char b[4]; return strcpy(b, "x") == b;', 1),
    ("strlcpy that fits",
     'char b[8]; unsigned n = strlcpy(b, "abc", 8);'
     ' return n * 10 + (strcmp(b, "abc") == 0);', 31),
    ("strlcpy filling size exactly",
     'char b[4]; unsigned n = strlcpy(b, "abc", 4);'
     ' return n * 10 + (strcmp(b, "abc") == 0);', 31),
    ("strlcpy one over: cut, terminated, full length back",
     'char b[4]; unsigned n = strlcpy(b, "abcd", 4);'
     ' return n * 100 + (b[3] == 0) * 10 + (strcmp(b, "abc") == 0);', 411),
    ("strlcpy into size 0 writes nothing",
     'char b[2]; b[0] = 88; unsigned n = strlcpy(b, "abc", 0);'
     ' return n * 100 + b[0];', 388),
    ("strlcat that fits",
     'char b[8]; strcpy(b, "ab"); unsigned n = strlcat(b, "cd", 8);'
     ' return n * 10 + (strcmp(b, "abcd") == 0);', 41),
    ("strlcat cut short",
     'char b[5]; strcpy(b, "ab"); unsigned n = strlcat(b, "cdef", 5);'
     ' return n * 10 + (strcmp(b, "abcd") == 0);', 61),
    ("strlcat onto an unterminated buffer writes nothing",
     'char b[4]; b[0] = 65; b[1] = 65; b[2] = 65; b[3] = 65;'
     ' unsigned n = strlcat(b, "xy", 4); return n * 10 + (b[3] == 65);', 61),
)
def test_string_copy(label, body, expected):
    """The bounded copies are the point: on a machine with no memory
    protection an overrun faults nothing, it just rewrites the neighbour.
    Every edge is here -- fits, fills exactly, one over, no room at all."""
    string_returns(body, expected)


@cases(
    ("strchr finds the first",
     'char *s = "hello"; return (unsigned)strchr(s, 108) - (unsigned)s;', 2),
    ("strchr absent", 'return strchr("hello", 122) == NULL;', 1),
    ("strchr finds the terminator",
     'char *s = "abc"; return (unsigned)strchr(s, 0) - (unsigned)s;', 3),
    ("strrchr finds the last",
     'char *s = "hello"; return (unsigned)strrchr(s, 108) - (unsigned)s;', 3),
    ("strrchr absent", 'return strrchr("hello", 122) == NULL;', 1),
    ("strrchr finds the terminator",
     'char *s = "hello"; return (unsigned)strrchr(s, 0) - (unsigned)s;', 5),
    ("strchr of a byte held in a char",
     r'char *s = "a\xE9b"; char c = s[1];'
     r' return (unsigned)strchr(s, c) - (unsigned)s;', 1),
)
def test_string_search(label, body, expected):
    """The last case is the one that goes wrong: a char holding 0xE9 is
    passed as the int -23, and a search that compares it to the unsigned
    byte in the string never finds it."""
    string_returns(body, expected)


def utoa_case(label, value, base, text):
    body = (f'char b[STR_UTOA_MAX]; int n = utoa({value}u, b, {base});'
            f' return n * 10 + (strcmp(b, "{text}") == 0);')
    return (label, body, len(text) * 10 + 1)


@cases(
    utoa_case("zero", 0, 10, "0"),
    utoa_case("decimal", 1234, 10, "1234"),
    utoa_case("largest decimal", 0xFFFFFFFF, 10, "4294967295"),
    utoa_case("hex is lower case", 0xBEEF, 16, "beef"),
    utoa_case("largest hex", 0xFFFFFFFF, 16, "ffffffff"),
    utoa_case("binary", 5, 2, "101"),
    utoa_case("largest binary fills STR_UTOA_MAX", 0xFFFFFFFF, 2, "1" * 32),
    ("base 1 is refused",
     'char b[4]; b[0] = 88; int n = utoa(5, b, 1); return n * 10 + (b[0] == 0);', 1),
    ("base 17 is refused",
     'char b[4]; b[0] = 88; int n = utoa(5, b, 17); return n * 10 + (b[0] == 0);', 1),
)
def test_utoa(label, body, expected):
    """Base 0 or 1 is not just nonsense: DIV by zero raises in the emulator
    and takes the machine down, so the base check is also the guard."""
    string_returns(body, expected)


def itoa_case(label, value, text):
    body = (f'char b[16]; int n = itoa({value}, b);'
            f' return n * 10 + (strcmp(b, "{text}") == 0);')
    return (label, body, len(text) * 10 + 1)


@cases(
    itoa_case("zero", 0, "0"),
    itoa_case("minus one", -1, "-1"),
    itoa_case("largest int", 2147483647, "2147483647"),
    itoa_case("most negative int", "-2147483647 - 1", "-2147483648"),
)
def test_itoa(label, body, expected):
    """-(-2147483648) does not fit in an int. itoa takes the magnitude as
    an unsigned, which is the case a naive `v = -v` gets wrong."""
    string_returns(body, expected)


@cases(
    ("leading spaces", 'return strtou("  42", NULL, 10);', 42),
    ("a plus sign", 'return strtou("+7", NULL, 10);', 7),
    ("base 0 finds hex", 'return strtou("0x1F", NULL, 0);', 31),
    ("base 0 finds binary", 'return strtou("0b101", NULL, 0);', 5),
    ("base 0 defaults to decimal", 'return strtou("019", NULL, 0);', 19),
    ("base 16 accepts 0x", 'return strtou("0XfF", NULL, 16);', 255),
    ("0b is a hex digit in base 16", 'return strtou("0b1", NULL, 16);', 0xB1),
    ("end stops at the first unused character",
     'char *s = "123abc"; char *e; unsigned v = strtou(s, &e, 10);'
     ' return v * 10 + ((unsigned)e - (unsigned)s);', 1233),
    ("no digits leaves end at the start",
     'char *s = "  xyz"; char *e; strtou(s, &e, 10); return e == s;', 1),
    ("0x with no hex digit after it is just 0",
     'char *s = "0xg"; char *e; unsigned v = strtou(s, &e, 0);'
     ' return v * 10 + ((unsigned)e - (unsigned)s);', 1),
    ("an invalid base parses nothing",
     'char *s = "5"; char *e; unsigned v = strtou(s, &e, 1);'
     ' return v * 10 + (e == s);', 1),
    ("the largest value", 'return strtou("4294967295", NULL, 10) == 0xFFFFFFFF;', 1),
    ("one below it", 'return strtou("4294967294", NULL, 10) == 0xFFFFFFFE;', 1),
)
def test_strtou(label, body, expected):
    string_returns(body, expected)


@cases(
    ("one past the top, where the add carries out",
     'return strtou("4294967296", NULL, 10) == 0xFFFFFFFF;'),
    ("far past the top, where the multiply would",
     'return strtou("99999999999", NULL, 10) == 0xFFFFFFFF;'),
    ("in hex", 'return strtou("123456789", NULL, 16) == 0xFFFFFFFF;'),
)
def test_strtou_saturates_rather_than_wrapping(label, body):
    """4294967296 is 429496729 * 10 + 6: the multiply fits and only the
    final add overflows. A wrapped result would be 0, which is a perfectly
    plausible number to get back from a config file."""
    string_returns(body, 1)


@cases(
    ("negative with spaces", 'return atoi("  -42");', -42),
    ("plus sign", 'return atoi("+17");', 17),
    ("stops at garbage", 'return atoi("12abc");', 12),
    ("no digits", 'return atoi("abc");', 0),
    ("most negative int", 'return atoi("-2147483648");', -2147483648),
)
def test_atoi(label, body, expected):
    string_returns(body, expected)


@cases(
    ("ten digits", "int n = 0; for (int c = -1; c < 256; c++) if (isdigit(c)) n++; return n;", 10),
    ("52 letters", "int n = 0; for (int c = -1; c < 256; c++) if (isalpha(c)) n++; return n;", 52),
    ("six spaces", "int n = 0; for (int c = -1; c < 256; c++) if (isspace(c)) n++; return n;", 6),
    ("true is exactly 1", "return isdigit('7') + isalpha('B') + isspace(' ');", 3),
    ("toupper", "return toupper('a') == 'A' && toupper('z') == 'Z' && toupper('A') == 'A'"
                " && toupper('`') == '`' && toupper('{') == '{' && toupper('1') == '1';", 1),
    ("tolower", "return tolower('A') == 'a' && tolower('Z') == 'z' && tolower('a') == 'a'"
                " && tolower('@') == '@' && tolower('[') == '[' && tolower(-1) == -1;", 1),
)
def test_character_classes(label, body, expected):
    """Every class is one unsigned compare, so the neighbours of each range
    are where it would break: '@' and '[' sit either side of A-Z, '`' and
    '{' either side of a-z, and -1 must not wrap into anything."""
    string_returns(body, expected)


def test_the_documented_example():
    """lib/README.md and string.h both show this; it has to work."""
    string_returns('char line[32]; int score = -42;'
                   ' unsigned n = strlcpy(line, "score ", sizeof(line));'
                   ' itoa(score, line + n); return strcmp(line, "score -42") == 0;', 1)


def test_string_does_not_pull_in_the_heap():
    """string.h defines size_t and NULL itself instead of including mem.h,
    so formatting a number does not drag an allocator into the image."""
    assert libraries_for(LIB / "string.c") == [LIB / "string.c"]


def test_string_and_mem_together():
    """Both headers define size_t and NULL; one program may include both."""
    returns(MEM + STRING + "int main(void){ char *p = (char *)malloc(16);"
            " strlcpy(p, \"pigeon\", 16); int n = strlen(p); free(p); return n; }",
            6, "mem.c", "string.c")


# --- <pigeon/display.h> -----------------------------------------------------

def framebuffer(ram):
    """The screen, for a bare CPU. run() builds no IO controller, so the
    display library stays on its software path and DISPLAY_START really is
    the screen -- see screen_of() for the Machine case, where it is not."""
    return bytes(ram.mem[DISPLAY_START:DISPLAY_START + DISPLAY_W * DISPLAY_H * 4])


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
    start, width = DISPLAY_W - 5, 50          # starts on screen, runs off the edge
    returns(DISPLAY + "int main(void){ disp_hline(%d,0,%d,RED); int n=0;"
                      " for(unsigned i=0;i<DISP_W;i++) if(disp_get(i,0)) n++;"
                      " return n; }" % (start, width),
            DISPLAY_W - start, "display.c")


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
    assert lit_pixels(cpu.ram) == DISPLAY_W * DISPLAY_H


def test_line_reaches_both_ends():
    returns(DISPLAY + "int main(void){ disp_line(0,0,DISP_W-1,DISP_H-1,WHITE);"
                      " return disp_get(0,0) && disp_get(DISP_W-1,DISP_H-1)"
                      " && disp_get(DISP_W/2,DISP_H/2) ? 1:0; }",
            1, "display.c")


def test_circle_is_centred():
    """Points on the axes must be r away from the centre, and the centre
    itself untouched -- an outline, not a disc."""
    returns(DISPLAY + "int main(void){ int cx=DISP_W/2, cy=DISP_H/2;"
                      " disp_circle(cx,cy,10,WHITE);"
                      " return disp_get(cx+10,cy) && disp_get(cx-10,cy)"
                      " && disp_get(cx,cy+10) && !disp_get(cx,cy) ? 1 : 0; }",
            1, "display.c")


def test_text_draws_something_legible():
    cpu = run(DISPLAY + 'int main(void){ disp_clear(BLACK); disp_text(1,1,"Hi",WHITE);'
                        ' return 0; }', "display.c")
    drawn = lit_pixels(cpu.ram)
    # at most two full glyph cells; the bound tracks GLYPH_W x GLYPH_H
    assert 6 <= drawn <= 2 * 8 * 5, f"two glyphs drew {drawn} pixels"


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


# --- <pigeon/math.h> --------------------------------------------------------

def math_returns(body, expected):
    returns(f"{MATH}int main(void) {{ {body} }}", expected, "math.c")


@cases(
    ("arithmetic shift right",  "return ishr(-256, 8);", -1),
    ("shift a bigger negative", "return ishr(-1024, 4);", -64),
    ("shift is unchanged for positives", "return ishr(1024, 4);", 64),
    ("signed divide",           "return idiv(-256, 2);", -128),
    ("signed divide, negative divisor", "return idiv(7, -2);", -3),
    ("remainder follows the dividend",  "return imod(-7, 3);", -1),
    ("remainder, negative divisor",     "return imod(7, -3);", 1),
    ("fixed multiply by a negative",    "return fmul(-3, FX_ONE);", -3),
    ("fixed multiply both negative",    "return fmul(-3, -FX_ONE);", 3),
    ("fixed divide",            "return fdiv(FX(3), FX(2));", 384),
)
def test_sign_safety(label, body, expected):
    """Every one of these is WRONG with the bare hardware operation.

    SHR is a logical shift and DIV is unsigned, so `-256 >> 8` gives
    16777215 and `-256 / 256` gives the same. Encapsulating that once is
    the reason this library exists -- user/cube.c had its own copy, and
    the next program would have written a third.
    """
    math_returns(body, expected)


@cases(
    ("divide by zero",    "return idiv(5, 0);", 0),
    ("remainder by zero", "return imod(5, 0);", 0),
    ("fixed divide by zero", "return fdiv(FX(5), 0);", 0),
)
def test_divide_by_zero_is_survivable(label, body, expected):
    """Not tidiness: the emulator raises a Python exception on DIV by
    zero, so an unguarded divide takes the whole machine down rather than
    faulting the guest."""
    math_returns(body, expected)


@cases(
    ("sin 0",     "return isin(0);", 0),
    ("sin 90",    "return isin(64);", 256),
    ("sin 180",   "return isin(128);", 0),
    ("sin 270",   "return isin(192);", -256),
    ("cos 0",     "return icos(0);", 256),
    ("cos 90",    "return icos(64);", 0),
    ("negative angles wrap", "return isin(-64);", -256),
    ("angles past a full turn", "return isin(256 + 64);", 256),
)
def test_trig_quarter_points(label, body, expected):
    math_returns(body, expected)


def test_cos_is_sin_a_quarter_turn_ahead():
    math_returns("int a; for (a = 0; a < 256; a++)"
                 " if (icos(a) != isin(a + 64)) return a;"
                 " return -1;", -1)


@cases(("zero", "return isqrt(0);", 0), ("one", "return isqrt(1);", 1),
       ("rounds down", "return isqrt(2);", 1), ("exact", "return isqrt(100);", 10),
       ("large", "return isqrt(1000000);", 1000),
       ("negative is zero", "return isqrt(-9);", 0),
       ("fixed sqrt of 1.0", "return fsqrt(FX_ONE);", 256),
       ("fixed sqrt of 4.0", "return fsqrt(FX(4));", 512))
def test_roots(label, body, expected):
    math_returns(body, expected)


@cases(("+x", "return iatan2(0, 10);", 0),   ("+x+y", "return iatan2(10, 10);", 32),
       ("+y", "return iatan2(10, 0);", 64),  ("-x+y", "return iatan2(10, -10);", 96),
       ("-x", "return iatan2(0, -10);", 128),("-x-y", "return iatan2(-10, -10);", 160),
       ("-y", "return iatan2(-10, 0);", 192),("+x-y", "return iatan2(-10, 10);", 224))
def test_atan2_directions(label, body, expected):
    """The axes are where a binary search goes off by one: rounding down
    put +y at 63 instead of 64."""
    math_returns(body, expected)


def test_atan2_inverts_the_sine_table_exactly():
    """Round-trip every direction: atan2(sin a, cos a) must give back a."""
    math_returns("int a; int worst = 0; for (a = 0; a < 256; a++) {"
                 " int g = iatan2(isin(a), icos(a)); int d = g - a;"
                 " if (d > 128) d = d - 256; if (d < -128) d = d + 256;"
                 " d = iabs(d); if (d > worst) worst = d; } return worst;", 0)


@cases(("abs", "return iabs(-7);", 7), ("sign negative", "return isign(-9);", -1),
       ("sign zero", "return isign(0);", 0), ("sign positive", "return isign(9);", 1),
       ("min", "return imin(3, 9);", 3), ("max", "return imax(3, 9);", 9),
       ("clamp high", "return iclamp(5, 0, 3);", 3),
       ("clamp low", "return iclamp(-5, 0, 3);", 0),
       ("clamp inside", "return iclamp(2, 0, 3);", 2),
       ("fx_int truncates toward zero", "return fx_int(FX(3) + 128);", 3),
       ("fx_int on a negative", "return fx_int(-FX(3));", -3),
       ("fx_round goes to nearest", "return fx_round(FX(3) + 128);", 4))
def test_integer_helpers(label, body, expected):
    math_returns(body, expected)


def test_random_is_seeded_and_repeatable():
    math_returns("rand_seed(42); unsigned a = irand();"
                 " rand_seed(42); return a == irand() ? 1 : 0;", 1)


def test_random_stays_in_range():
    math_returns("int i; rand_seed(7); for (i = 0; i < 300; i++) {"
                 " int v = irand_range(-5, 5); if (v < -5 || v > 5) return 0; }"
                 " return 1;", 1)


def test_seeding_with_zero_does_not_stick():
    """Zero is a fixed point of xorshift -- it would return 0 forever."""
    math_returns("rand_seed(0); return irand() != 0 ? 1 : 0;", 1)


def test_vec3_dot_and_cross():
    math_returns("vec3 a; vec3 b; v3_set(&a,1,2,3); v3_set(&b,4,5,6);"
                 " return v3_dot(&a,&b);", 32)
    math_returns("vec3 a; vec3 b; vec3 c; v3_set(&a,1,0,0); v3_set(&b,0,1,0);"
                 " v3_cross(&c,&a,&b); return c.x*100 + c.y*10 + c.z;", 1)


def test_vec3_length_of_a_345_triangle():
    math_returns("vec3 v; v3_set(&v, 3, 4, 0); return v3_length(&v);", 5)


def test_vec3_add_sub_scale():
    math_returns("vec3 a; vec3 b; vec3 c; v3_set(&a,1,2,3); v3_set(&b,10,20,30);"
                 " v3_add(&c,&a,&b); return c.x*10000 + c.y*100 + c.z;", 112233)
    math_returns("vec3 a; vec3 b; vec3 c; v3_set(&a,10,20,30); v3_set(&b,1,2,3);"
                 " v3_sub(&c,&a,&b); return c.x*10000 + c.y*100 + c.z;", 91827)
    math_returns("vec3 v; v3_set(&v, 10, 20, 30); v3_scale(&v, &v, FX_HALF);"
                 " return v.x*10000 + v.y*100 + v.z;", 51015)


def test_a_full_turn_returns_to_the_start():
    math_returns("vec3 v; int i; v3_set(&v, 40, 0, 0);"
                 " for (i = 0; i < 256; i++) v3_rotate_y(&v, &v, 1);"
                 " return (iabs(v.x - 40) <= 4 && iabs(v.z) <= 4) ? 1 : 0;", 1)


def test_rotation_by_a_quarter_turn():
    math_returns("vec3 v; v3_set(&v, 100, 0, 0); v3_rotate_y(&v, &v, 64);"
                 " return (iabs(v.x) <= 1 && iabs(v.z - 100) <= 1) ? 1 : 0;", 1)


@cases("v3_rotate_x", "v3_rotate_y", "v3_rotate_z")
def test_rotation_tolerates_aliasing(name):
    """The cube chains rotations as v3_rotate_x(&v, &v, a). A version that
    wrote out->x before reading in->y would corrupt the other two
    components and give a subtly wrong shape rather than an obvious
    failure."""
    math_returns(f"vec3 a; vec3 b; vec3 c;"
                 f" v3_set(&a, 11, 22, 33); v3_set(&b, 11, 22, 33);"
                 f" {name}(&c, &a, 37);"
                 f" {name}(&b, &b, 37);"
                 f" return (b.x == c.x && b.y == c.y && b.z == c.z) ? 1 : 0;", 1)


def test_project_puts_the_origin_at_the_centre():
    math_returns("vec3 v; int sx; int sy; v3_set(&v, 0, 0, 0);"
                 " v3_project(&v, 150, 50, 46, &sx, &sy);"
                 " return sx * 100 + sy;", 5046)


def test_project_survives_a_point_behind_the_eye():
    """Depth is clamped, because a divide by zero would raise a Python
    exception and take the emulator down, not the guest program."""
    math_returns("vec3 v; int sx; int sy; v3_set(&v, 10, 10, -1000);"
                 " v3_project(&v, 150, 50, 46, &sx, &sy); return 1;", 1)


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
    fb = machine.display_io.snapshot()
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
    # These mirror demo.c's layout, which derives from the screen size --
    # keep them in step with the #defines at the top of that file.
    canvas_y = DISPLAY_H // 2 + 6
    canvas_h = DISPLAY_H - canvas_y - 14
    centre = ((canvas_y + canvas_h // 2) * DISPLAY_W + DISPLAY_W // 2) * 4
    pixel = machine.display_io.snapshot()[centre:centre + 3]
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
    assert lit_pixels(cpu.ram) == DISPLAY_W * DISPLAY_H


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
        fb = machine.display_io.snapshot()
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
    """Whatever is actually on screen. Not ram.mem[DISPLAY_START:] -- the
    display library page-flips, so that address is the visible surface
    only every other frame."""
    return machine.display_io.snapshot()


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
