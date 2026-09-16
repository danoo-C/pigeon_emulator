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
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                   # noqa: E402
from test_kernel import (                                              # noqa: E402
    COLS, STANDINS, Console, make_disk, shell_program, standin)

# A program that says what arguments it was given: "[args][a][b]".
STANDINS["args"] = r"""#include <pigeon/sys.h>
int main(int argc, char **argv) {
    int i;
    for (i = 0; i < argc; i++) { print("["); print(argv[i]); print("]"); }
    print("\n");
    return 0;
}
"""

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
        console = Console(make_disk(Path(t) / "hdd.img", extra))
        try:
            assert console.ready(), console.rows()
            yield console
        finally:
            console.close()


def run(source, files=(), programs=(), args=""):
    """A script's output, line by line, as the console shows it."""
    with booted_with(source, files, programs) as c:
        return c.command("pgs /s.pgs" + (" " + args if args else ""))


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


if __name__ == "__main__":
    run_module(__name__)
