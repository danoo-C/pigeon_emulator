"""pgs, the shell's scripts: docs/pgs_plan.md.

/bin/pgs.bin runs a script under the kernel on a test disk, started at the
shell's prompt the way a person would start it. What the script printed comes
back as the console's lines, so a test reads what you would see.

The script is always /s.pgs, and its output lines are kept under the
console's 32 columns: a row exactly that wide reads as a wrapped one and
Console.output() cannot tell the difference.

    python3 tests/test_pgs.py      (or: python3 -m pytest tests/)
"""
import contextlib
import io
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                   # noqa: E402
from pfs import PgfsImage                                               # noqa: E402
from test_kernel import (                                              # noqa: E402
    COLS, PROMPT, STANDINS, Console, last_row, make_disk, shell_program, standin)
from test_graphics import (                                            # noqa: E402
    GREEN, GREEN_RGB, MAGENTA, MAGENTA_RGB, MARK, held, press_until, rgb, type_in)
from test_kernel import PIGEON_BMP                                     # noqa: E402
from emulator.devices.keycodes import KEY_ESC                          # noqa: E402
from emulator.memory_map import DISPLAY_H, DISPLAY_W                   # noqa: E402

# A program with something to say and a status to go with it.
STANDINS["moan"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) { print("moaning\n"); return 2; }
"""

# More than a capture can hold: 9 KB of it.
STANDINS["flood"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) {
    int i;
    for (i = 0; i < 900; i++) print("0123456789\n");
    return 0;
}
"""


# --- driving it -------------------------------------------------------------------

@contextlib.contextmanager
def booted_with(source, files=(), programs=()):
    """The kernel on a disk holding pgs, the standins named, and /s.pgs."""
    extra = [("/bin/pgs.bin", shell_program("pgs")), ("/s.pgs", source.encode())]
    for name in programs:
        extra.append((f"/bin/{name}.bin", standin(name)))
    extra.extend(files)
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "hdd.img"
        console = Console(make_disk(path, extra))
        console.disk = path
        try:
            assert console.ready(), console.rows()
            yield console
        finally:
            console.close()


def run(source, files=(), programs=(), args=""):
    """A script's output, line by line, as the console shows it."""
    with booted_with(source, files, programs) as c:
        return c.command("pgs /s.pgs" + (" " + args if args else ""))


def wrote(source, path, files=(), programs=(), args=""):
    """What a script left on the disk at `path`, the machine stopped first;
    None for nothing."""
    with booted_with(source, files, programs) as c:
        c.command("pgs /s.pgs" + (" " + args if args else ""))
        c.close()
        with PgfsImage(c.disk) as img:
            return img.read_file(path) if img.exists(path) else None


def screen(source, files=(), programs=(), args=""):
    """The console as one run of text, every row padded to the full width, so
    a line that wrapped at a space reads whole."""
    with booted_with(source, files, programs) as c:
        c.command("pgs /s.pgs" + (" " + args if args else ""))
        return "".join(row.ljust(COLS) for row in c.rows())


# --- variables and words ------------------------------------------------------------

def test_a_value_and_a_word_that_uses_it():
    assert run("$name = world\necho hello $name\n") == ["hello world"]


def test_a_value_is_the_rest_of_the_line_with_its_own_values_in_it():
    assert run("$who = world\n$greeting = hello $who\necho $greeting\n") == ["hello world"]


def test_a_value_with_spaces_stays_one_argument():
    source = '$file = "my notes.txt"\nargs $file\n'
    assert run(source, programs=["args"]) == ["[args][my notes.txt]"]


def test_quotes_and_escapes():
    assert run('echo "a  b" \\$name \\\\ end\n') == ["a  b $name \\ end"]


def test_the_scripts_own_arguments():
    source = "echo $# $1 $2\n"
    assert run(source, args="one two") == ["2 one two"]


def test_an_argument_that_is_not_there_is_empty():
    assert run("echo [$3]\n") == ["[]"]


def test_the_status_of_the_last_command():
    source = "moan\necho status $?\n"
    assert run(source, programs=["moan"]) == ["moaning", "status 2"]


def test_a_variable_that_was_never_set_stops_the_script():
    text = screen("echo one\necho $nmae\necho three\n")
    assert "/s.pgs:2: no such variable: $nmae" in text, text
    assert "three" not in text, "the script carried on"
    assert "pgs: exit 1" in text, text


def test_an_empty_value_is_allowed_and_is_not_an_unset_one():
    assert run("$empty =\necho [$empty]\n") == ["[]"]


# --- comments and settings -----------------------------------------------------------

def test_comments_start_at_two_semicolons():
    source = ";; a whole line\necho hello ;; and the rest of this one\n;;\n"
    assert run(source) == ["hello"]


def test_a_comment_marker_inside_quotes_is_not_one():
    assert run('echo "a ;; b"\n') == ["a ;; b"]


def test_stop_on_error_ends_the_script_at_a_program_that_fails():
    source = "# stop-on-error\necho one\nmoan\necho three\n"
    text = screen(source, programs=["moan"])
    assert "one" in text and "moaning" in text, text
    assert "three" not in text, "it carried on after a failure"
    assert "/s.pgs:3: stopped: 2" in text, text


def test_without_the_setting_a_failure_is_only_a_status():
    source = "moan\necho after $?\n"
    assert run(source, programs=["moan"]) == ["moaning", "after 2"]


def test_a_setting_pgs_does_not_know_is_a_mistake():
    text = screen("# stoponerror\necho kept\n")
    assert "/s.pgs:1: no such setting: stoponerror" in text, text
    assert "kept" not in text, "the script ran anyway"


def test_a_setting_after_the_first_command_is_a_mistake():
    text = screen("echo one\n# stop-on-error\n")
    assert "/s.pgs:2: a setting after the first command: stop-on-error" in text, text


def test_a_setting_can_have_a_comment_after_it():
    source = "# stop-on-error   ;; from here on\necho one\n"
    assert run(source) == ["one"]


# --- commands ------------------------------------------------------------------------

def test_a_program_is_found_the_way_the_shell_finds_one():
    assert run("args a b\n", programs=["args"]) == ["[args][a][b]"]


def test_a_program_that_is_not_there_is_said_and_the_script_carries_on():
    text = screen("nope\necho after $?\n")
    assert "/s.pgs:1: nope: not found" in text, text
    assert "after -1" in text, text


def test_cd_and_pwd():
    assert run("cd /docs\npwd\n") == ["2:/docs"]


def test_exit_ends_the_script_with_its_status():
    text = screen("echo one\nexit 3\necho two\n")
    assert "one" in text and "two" not in text, text
    assert "pgs: exit 3" in text, text


# --- $( ) ------------------------------------------------------------------------------

def test_a_capture_of_a_builtin():
    assert run("$x = $(echo hi)\necho got $x\n") == ["got hi"]


def test_a_capture_of_a_program():
    assert run("$x = $(args one)\necho $x\n", programs=["args"]) == ["[args][one]"]


def test_a_capture_keeps_the_newlines_inside_it():
    source = "$two = $(ls /bin)\necho $two\n"
    out = run(source, files=[("/bin/one.bin", b"x"), ("/bin/two.bin", b"y")])
    assert "cat.bin" in out and "one.bin" in out, out


def test_a_capture_is_one_word_however_many_spaces_it_has():
    assert run('$x = $(echo a b c)\nargs $x\n', programs=["args"]) == ["[args][a b c]"]


def test_pwd_in_a_capture():
    assert run("cd /docs\n$here = $(pwd)\necho in $here\n") == ["in 2:/docs"]


def test_a_captured_command_sets_the_status_too():
    assert run("$x = $(moan)\necho [$x] $?\n", programs=["moan"]) == ["[moaning] 2"]


def test_a_capture_of_a_program_that_is_not_there_is_empty_and_said():
    text = screen("$x = $(nope)\necho [$x] $?\n")
    assert "/s.pgs:1: not found: nope" in text, text
    assert "[] -1" in text, text


def test_output_too_big_for_a_capture_is_cut_and_the_script_is_told():
    text = screen("$x = $(flood)\necho done\n", programs=["flood"])
    assert "/s.pgs:1: $( ) cut at 8191 bytes: flood" in text, text
    assert "done" in text, "the script should carry on"


def test_a_capture_inside_a_capture_is_refused():
    text = screen("$x = $(echo $(pwd))\n")
    assert "/s.pgs:1: $( ) inside $( )" in text, text


def test_a_capture_with_no_closing_bracket_is_refused():
    text = screen("$x = $(echo hi\n")
    assert "/s.pgs:1: no ) for $(" in text, text


# --- if, while, for, let, break --------------------------------------------------------

@cases(("text, the same", "$a == one", "yes"), ("text, not", "$a == two", "no"),
       ("not equal", "$a != two", "yes"),
       ("a number, less", "$n -lt 10", "yes"), ("a number, not less", "$n -lt 2", "no"),
       ("less or the same", "$n -le 2", "yes"), ("more", "$n -gt 1", "yes"),
       ("more or the same", "$n -ge 3", "no"),
       ("a file is there", "-e /s.pgs", "yes"), ("a file is not", "-e /nope", "no"),
       ("a directory", "-d /docs", "yes"), ("a directory is not a file", "-f /docs", "no"),
       ("a file is a file", "-f /s.pgs", "yes"),
       ("a command that works", "echo quiet", "yes"),
       ("a command that fails", "moan", "no"))
def test_every_kind_of_test(label, condition, wanted):
    source = f"$a = one\n$n = 2\nif {condition}\n    echo yes\nelse\n    echo no\nend\n"
    assert wanted in run(source, programs=["moan"]), condition


def test_an_if_with_no_else():
    source = "if 1 == 1\n    echo taken\nend\nif 1 == 2\n    echo not\nend\necho after\n"
    assert run(source) == ["taken", "after"]


def test_ifs_inside_ifs():
    source = ("$a = yes\nif $a == yes\n    if $a == no\n        echo inner\n"
              "    else\n        echo outer else\n    end\nelse\n    echo skipped\nend\n")
    assert run(source) == ["outer else"]


def test_a_branch_that_is_not_run_is_not_even_expanded():
    """An unset name in a branch that never runs is not a mistake: the line is
    skipped whole, before anything is expanded."""
    source = "if 1 == 2\n    echo $nmae\nend\necho fine\n"
    assert run(source) == ["fine"]


def test_a_while_that_counts():
    source = "$i = 0\nwhile $i -lt 3\n    echo tick $i\n    let $i = $i + 1\nend\necho done\n"
    assert run(source) == ["tick 0", "tick 1", "tick 2", "done"]


def test_a_while_whose_test_is_false_at_once_runs_nothing():
    assert run("while 1 == 2\n    echo never\nend\necho after\n") == ["after"]


def test_break_leaves_a_while():
    source = ("$i = 0\nwhile $i -lt 9\n    echo at $i\n    if $i == 1\n        break\n"
              "    end\n    let $i = $i + 1\nend\necho out\n")
    assert run(source) == ["at 0", "at 1", "out"]


def test_for_walks_the_lines_of_a_value():
    source = "$list = $(ls /docs)\nfor $name in $list\n    echo file $name\nend\n"
    out = run(source, files=[("/docs/a.txt", b"a"), ("/docs/b.txt", b"b")])
    assert out == ["file a.txt", "file b.txt", "file readme.txt"], out


def test_for_takes_its_value_once():
    """The $( ) is run when the for starts, not once a turn."""
    source = "for $line in $(args go)\n    echo [$line]\nend\n"
    assert run(source, programs=["args"]) == ["[[args][go]]"]


def test_for_over_nothing_runs_nothing():
    assert run("$empty =\nfor $x in $empty\n    echo never\nend\necho after\n") == ["after"]


def test_break_leaves_a_for():
    source = ("for $name in $(ls /bin)\n    echo $name\n    break\nend\necho out\n")
    out = run(source)
    assert len(out) == 2 and out[1] == "out", out


def test_let_does_sums():
    source = ("let $a = 2 + 3 * 4\nlet $b = ( 2 + 3 ) * 4\nlet $c = 7 % 4\n"
              "let $d = 7 / 2\necho $a $b $c $d\n")
    assert run(source) == ["14 20 3 3"]


def test_let_refuses_a_divide_by_zero():
    text = screen("let $a = 1 / 0\necho after\n")
    assert "/s.pgs:1: divide by zero" in text, text
    assert "after" not in text, text


def test_let_refuses_what_is_not_a_number():
    text = screen("$word = abc\nlet $a = $word + 1\n")
    assert "/s.pgs:2: not a number: abc" in text, text


@cases(("an end with nothing open", "end\n", "an end with no if, while or for"),
       ("an else with nothing open", "else\n", "an else with no if"),
       ("an if left open", "if 1 == 1\n    echo hi\n", "no end for this if"),
       ("a while left open", "while 1 == 2\n", "no end for this while"),
       ("a break outside a loop", "break\n", "a break outside a while or for"),
       ("a for with no in", "for $x $y\n", "for wants $name in ..."))
def test_a_block_that_does_not_add_up(label, source, message):
    text = screen(source)
    assert message in text, text


def test_blocks_more_than_eight_deep_are_refused():
    source = "".join(f"if 1 == 1\n" for _ in range(9)) + "echo deep\n"
    text = screen(source)
    assert "blocks inside blocks, more than 8 deep" in text, text


# --- read, and the two things the shell knows about .pgs -------------------------------

def test_read_takes_a_typed_line():
    with booted_with("echo who?\nread $name\necho hello $name\n") as c:
        c.type("pgs /s.pgs\n")
        assert c.run_until(lambda rows: any("who?" in row for row in rows)), c.rows()
        c.type("dano\n")
        assert c.run_until(lambda rows: any("hello dano" in row for row in rows)), c.rows()


def test_read_wants_a_name():
    text = screen("read name\n")
    assert "/s.pgs:1: read wants $name" in text, text


def test_a_pgs_typed_at_the_prompt_runs_as_a_script():
    with booted_with("echo from the script $1\n") as c:
        assert c.command("/s.pgs here") == ["from the script here"], c.rows()


def test_a_pgs_that_is_not_there_is_not_found():
    with booted_with("echo hi\n") as c:
        assert c.command("nope.pgs") == ["nope.pgs: not found"], c.rows()


def test_the_shell_runs_etc_startup_pgs_before_its_first_prompt():
    startup = b"echo the startup script ran\n"
    with booted_with("echo hi\n", files=[("/etc/startup.pgs", startup)]) as c:
        rows = c.rows()
        assert any("the startup script ran" in row for row in rows), rows
        assert c.command("echo after") == ["after"], c.rows()


def test_every_shell_runs_it_not_just_the_first():
    """exit ends the shell and the kernel starts another, which runs the
    script again -- what .bashrc does, and what docs/pgs_plan.md 4.9 says."""
    startup = b"echo ran\n"
    with booted_with("echo hi\n", files=[("/etc/startup.pgs", startup)]) as c:
        first = "".join(row.ljust(COLS) for row in c.rows())
        assert first.count("ran") == 1, first
        c.type("exit\n")
        assert c.run_until(lambda rows: "".join(row.ljust(COLS) for row in rows).count("ran") == 2), \
            c.rows()


def test_a_startup_script_that_fails_still_leaves_a_prompt():
    with booted_with("echo hi\n", files=[("/etc/startup.pgs", b"$oops\n")]) as c:
        assert c.ready(), c.rows()
        assert c.command("echo after") == ["after"], c.rows()


# --- the script the disc ships ----------------------------------------------------------

def test_the_discs_own_example_script_runs():
    """user/os/docs_hello.pgs is /docs/hello.pgs on the installed disc: the
    first script anyone runs. It prints more than the screen holds, so what
    is checked is the end of it, and that nothing complained."""
    source = (REPO_ROOT / "user" / "os" / "docs_hello.pgs").read_text()
    with booted_with(source, files=[("/etc/explorer.conf", b"EXEC = .bin\n")]) as c:
        before = c.rows()
        c.type("pgs /s.pgs\n")
        assert c.run_until(lambda rows: rows != before and PROMPT.match(last_row(rows))), c.rows()
        text = "".join(row.ljust(COLS) for row in c.rows())
    assert "tick 0" in text and "tick 2" in text, text
    assert "the explorer has its rules" in text, text
    assert "/s.pgs:" not in text, f"the script complained: {text}"


def test_the_discs_own_drawing_script_runs():
    """user/os/docs_logo.pgs is /docs/logo.pgs on the installed disc: the
    example of a `# graphics` script, with a continued line, an image and a
    key taken from -wait. It holds the screen twice, so it is read by
    watching the screen and pressing Esc, as a person would."""
    source = (REPO_ROOT / "user" / "os" / "docs_logo.pgs").read_text()
    with drawing(source, files=[("/etc/bmp/pigeon.bmp", PIGEON_BMP.read_bytes())]) as c:
        type_in(c, "pgs /s.pgs\n")
        assert wait_for(c, lambda fb: white_in(fb, 88, 98) > 5), \
            f"the logo never finished drawing: {c.rows()}"
        fb = c.machine.display_io.snapshot()
        assert rgb(fb, 150, 22) == (255, 204, 51), "the sun, from the first call"
        assert rgb(fb, 5, 95) == (30, 58, 30), "the ground, from the same one"
        # 27 is what the script then puts on the screen, in green
        assert press_until(c, KEY_ESC,
                           lambda: any_pixel(c.machine.display_io.snapshot(),
                                             (136, 255, 136))), \
            f"the key it was given never came back: {c.rows()}"
        assert press_until(c, KEY_ESC, lambda: PROMPT.match(last_row(c.rows()))), c.rows()
        text = "".join(row.ljust(COLS) for row in c.rows())
    assert "/s.pgs:" not in text, f"the script complained: {text}"


def wait_for(c, wanted, seconds=90):
    give_up = time.time() + seconds
    while time.time() < give_up:
        with contextlib.redirect_stdout(io.StringIO()):
            c.machine.run(deadline=time.time() + 0.1)
        if wanted(c.machine.display_io.snapshot()):
            return True
    return False


def white_in(fb, top, bottom):
    return sum(1 for y in range(top, bottom) for x in range(DISPLAY_W)
               if rgb(fb, x, y) == (255, 255, 255))


def any_pixel(fb, colour):
    return any(rgb(fb, x, y) == colour
               for y in range(0, DISPLAY_H, 2) for x in range(0, DISPLAY_W, 2))


# --- > >> and < in a script (docs/redirect_plan.md 3.3) ---------------------------------

def test_a_program_writes_into_a_file():
    assert wrote("args one > /out.txt\n", "/out.txt", programs=["args"]) == b"[args][one]\n"


def test_a_builtin_writes_into_a_file_too():
    """echo into a file is most of why a script wants > at all."""
    assert wrote("echo hello > /out.txt\n", "/out.txt") == b"hello\n"


def test_a_builtin_appends():
    source = "echo one > /out.txt\necho two >> /out.txt\n"
    assert wrote(source, "/out.txt") == b"one\ntwo\n"


def test_a_value_can_name_the_file():
    source = "$where = /out.txt\necho hello > $where\n"
    assert wrote(source, "/out.txt") == b"hello\n"


def test_a_script_reads_a_file_with_a_redirection():
    source = "eater < /in.txt\n"
    out = run(source, files=[("/in.txt", b"a\nb\n")], programs=["eater"])
    assert out == ["ate 2 [a]"], out


def test_a_quoted_marker_is_text_not_a_redirection():
    assert run('echo ">" done\n') == ['> done']


def test_a_redirection_with_no_file_is_a_mistake():
    text = screen("echo hi >\n")
    assert "/s.pgs:1: no file after >" in text, text


def test_a_builtin_refuses_an_input_redirection():
    text = screen("echo hi < /s.pgs\n")
    assert "/s.pgs:1: < on echo" in text, text


def test_a_value_cannot_redirect():
    """`$x = $(ls) > out.txt` looks like a redirection and is not one: the
    whole right-hand side is the value (docs/redirect_plan.md Q3)."""
    text = screen("$x = $(pwd) > /out.txt\n")
    assert "/s.pgs:1: a value cannot redirect" in text, text


def test_a_value_may_hold_the_marker_in_quotes():
    assert run('$x = "a > b"\necho $x\n') == ["a > b"]


def test_a_file_that_cannot_be_written_is_said():
    text = screen("args one > /nodir/out.txt\n", programs=["args"])
    assert "/s.pgs:1: /nodir/out.txt: not found" in text, text


# --- # graphics: a script that owns the screen (docs/graphics_plan.md 4.2) -------

def drawing(source, files=(), programs=()):
    """The kernel with graphics.bin on its disk, and the script on /s.pgs."""
    return booted_with(source, files=[("/bin/graphics.bin", shell_program("graphics"))]
                       + list(files), programs=programs)


def ends_holding(body):
    """A `# graphics` script that draws `body` and then holds the screen with
    the mark on it, which is what a test waits for."""
    return "# graphics\n" + body + f"graphics -px 191 107 {MARK} -wait\n"


def not_black(fb):
    return sum(1 for y in range(DISPLAY_H) for x in range(DISPLAY_W)
               if rgb(fb, x, y) != (0, 0, 0))


def test_a_graphics_script_keeps_what_it_drew_between_commands():
    """The whole point of the mode: without it the kernel paints its console
    back over the first call's picture before the second one runs."""
    with drawing(ends_holding(f"graphics -rect 0 0 20 20 {MAGENTA}\n"
                              f"graphics -disc 96 54 20 {GREEN}\n")) as c:
        fb, shown = held(c, "pgs /s.pgs")
        assert rgb(fb, 5, 5) == MAGENTA_RGB, "the first call's rect was wiped"
        assert rgb(fb, 96, 54) == GREEN_RGB, "the second call's disc is missing"
        assert shown == [], shown


def test_the_screen_starts_black():
    """pgs clears it once, before the first command, so the console the shell
    left behind is not sitting under the picture."""
    with drawing(ends_holding("")) as c:
        fb, _ = held(c, "pgs /s.pgs")
        assert not_black(fb) == 1, "something other than the mark is on the screen"


def test_an_echo_in_a_graphics_script_paints_nothing_and_is_not_a_mistake():
    """A script gets run both ways while it is being written; where the words
    went is in docs/pgs.md."""
    with drawing(ends_holding("echo hello\n")) as c:
        fb, shown = held(c, "pgs /s.pgs")
        assert not_black(fb) == 1, "echo painted on the screen"
        assert shown == [], shown


def test_a_program_in_a_graphics_script_paints_nothing_either():
    with drawing(ends_holding("moan\n"), programs=["moan"]) as c:
        fb, shown = held(c, "pgs /s.pgs")
        assert not_black(fb) == 1, "a program's output painted on the screen"
        assert shown == [], shown


def test_a_mistake_turns_the_mode_off_so_the_message_can_be_read():
    with drawing("# graphics\n"
                 f"graphics -rect 0 0 20 20 {MAGENTA}\n"
                 "echo $nmae\n") as c:
        shown = c.command("pgs /s.pgs")
        assert "/s.pgs:3: no such variable: $nmae" in " ".join(shown), shown
        fb = c.machine.display_io.snapshot()
        assert rgb(fb, 5, 5) != MAGENTA_RGB, "the picture outlived the script"


def test_without_the_setting_the_console_comes_back_between_commands():
    """The same script without `# graphics`: the kernel redraws after each
    program, so only the last call's picture is there to be held."""
    with drawing(f"graphics -rect 0 0 20 20 {MAGENTA}\n"
                 f"graphics -disc 96 54 20 {GREEN} -px 191 107 {MARK} -wait\n") as c:
        fb, _ = held(c, "pgs /s.pgs")
        assert rgb(fb, 96, 54) == GREEN_RGB
        assert rgb(fb, 5, 5) != MAGENTA_RGB, "the first picture should be gone"


# --- a line continued with \\ : docs/graphics_plan.md step 4 ---------------------

def test_a_line_that_ends_in_a_backslash_is_one_command():
    """What a drawing call with a shape a line is for."""
    source = "args one \\\n  two three\n"
    assert run(source, programs=["args"]) == ["[args][one][two][three]"]


def test_a_continued_line_does_not_shift_the_line_numbers():
    """The lines are joined, but a message still names the line the script
    has: a continuation that moved every number after it would be worse than
    no continuation at all."""
    source = "echo one \\\ntwo\necho $nmae\n"
    text = screen(source)
    assert "/s.pgs:3: no such variable: $nmae" in text, text


def test_an_escaped_backslash_at_the_end_is_not_a_continuation():
    source = "echo a\\\\\necho b\n"
    assert run(source) == ["a\\", "b"]


def test_a_comment_cannot_continue_a_line():
    source = "echo a ;; note \\\necho b\n"
    assert run(source) == ["a", "b"]


def test_a_backslash_inside_quotes_is_not_a_continuation():
    """Inside quotes a backslash is text, so the newline ends the line as it
    would anywhere else -- and the quote left open says so, which is how you
    can tell the next line was not swallowed into this one."""
    text = screen('echo "a \\\necho b\n')
    assert '/s.pgs:1: a " with no " after it' in text, text


if __name__ == "__main__":
    run_module(__name__)
