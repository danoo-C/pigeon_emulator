"""cc.py --project: a project file in, an installation disc out.

Phase 5 of docs/os_cd.md. compiler/project.py reads a project file --
reporting every mistake with its line, before anything is built -- then
builds the installer and the files it names, and writes a PigeonFS disc:
/install.bin first, /pigeon.txt, the files, and block 0 made to boot the
installer. The launcher's --cd puts that disc in the drive at power-on.

The discs built here are booted the whole way, through the BIOS, bios2
and the boot sector, into their installers.

    python3 tests/test_project.py      (or: python3 -m pytest tests/)
"""
import contextlib
import dataclasses
import io
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                                 # noqa: E402
from compiler import cc                                               # noqa: E402
from compiler.program_file import Header                              # noqa: E402
from compiler.project import (                                        # noqa: E402
    INSTALLER_PATH, METADATA_PATH, SYSTEM_PATH, ProjectError, build_disc, read_project)
from emulator.cli import disc_drive                                   # noqa: E402
from emulator.config import load_config                               # noqa: E402
from emulator.memory_map import BOOT_CODE, PROGRAM_MAX_SIZE           # noqa: E402
from emulator.programs import Program                                 # noqa: E402
from pfs import PgfsImage, boot_sector                                # noqa: E402
from test_bios2 import ENTER, power_on                                # noqa: E402

EXAMPLE = REPO_ROOT / "user" / "os" / "pigeon_compiler_init.txt"
ANSWER = 0x1257A11              # what the tiny installer returns

TINY_INSTALLER = "int main(void) { return 0x1257A11; }\n"
TINY_TOOL = ".ORG PROGRAM_LOAD_ADDR\n    MOV A #42\n    HALT\n"
NOTES = "hello from the disc\n"

GOOD = """\
# a project for the tests
[project]
name    = TestOS
version = 2.5
label   = TESTOS              # the volume label

[boot]
installer = installer.c

[files]
/bin/tool.bin   = tool.asm    # built
/docs/notes.txt = notes.txt   # copied
"""

# Four lines that make a project on their own; [files] cases go after them.
BASE = "[project]\nname = X\n[boot]\ninstaller = installer.c\n"


def write(folder, name, text):
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def project_dir(folder, text=GOOD):
    write(folder, "installer.c", TINY_INSTALLER)
    write(folder, "tool.asm", TINY_TOOL)
    write(folder, "notes.txt", NOTES)
    return write(folder, "project.txt", text)


def quiet(_line):
    pass


def built(folder, source, relocatable=False):
    """What the launcher builds from `source`: the bytes the disc must hold."""
    source = Path(source)
    binary = Path(folder) / "reference" / f"{source.name}{'.reloc' if relocatable else ''}.bin"
    return Program(name=source.stem, source=source, binary=binary,
                   relocatable=relocatable).ensure_built(quiet=True).read_bytes()


# --- reading a project -------------------------------------------------------------

def test_a_project_file_reads_as_written():
    with tempfile.TemporaryDirectory() as t:
        project = read_project(project_dir(t))
    assert (project.name, project.version, project.label) == ("TestOS", "2.5", "TESTOS")
    assert project.installer.name == "installer.c" and project.bootsector is None
    assert [disc_path for disc_path, _ in project.files] == ["/bin/tool.bin", "/docs/notes.txt"]
    assert project.metadata().decode().splitlines()[0] == "TestOS 2.5"
    assert project.slug == "testos"


def test_a_label_left_out_comes_from_the_name():
    with tempfile.TemporaryDirectory() as t:
        project = read_project(project_dir(t, "[project]\nname = My OS!\n[boot]\n"
                                              "installer = installer.c\n"))
    assert project.label == "MYOS" and project.slug == "my-os"


@cases(("an unknown section", BASE + "[extras]\n", 5, "unknown section"),
       ("an unknown key", "[project]\nname = X\ncolour = blue\n[boot]\ninstaller = installer.c\n",
        3, "unknown key 'colour'"),
       ("a key before any section", "name = X\n" + BASE, 1, "before any [section]"),
       ("a line that is not key = value", BASE + "just words\n", 5, "key = value"),
       ("a key twice", "[project]\nname = X\nname = Y\n[boot]\ninstaller = installer.c\n",
        3, "appears twice"),
       ("no installer", "[project]\nname = X\n[boot]\n", 0, "needs installer"),
       ("an installer of the wrong kind", "[project]\nname = X\n[boot]\ninstaller = notes.txt\n",
        4, "must be a .c or .asm or .bin"),
       ("a label over 15 bytes",
        "[project]\nname = X\nlabel = ABCDEFGHIJKLMNOP\n[boot]\ninstaller = installer.c\n",
        3, "at most 15"),
       ("a file that is not there", BASE + "[files]\n/a.txt = nope.txt\n", 6, "no such file"),
       ("a path without a leading slash", BASE + "[files]\nbin/a.txt = notes.txt\n",
        6, "must start with /"),
       ("a name over 31 bytes", BASE + "[files]\n/" + "x" * 32 + " = notes.txt\n",
        6, "at most 31"),
       ("the installer's own path", BASE + "[files]\n/install.bin = notes.txt\n",
        6, "the disc's own"),
       ("the system's own path", BASE + "[files]\n/boot.bin = notes.txt\n",
        6, "the disc's own"),
       ("the same path twice", BASE + "[files]\n/a.txt = notes.txt\n/./a.txt = notes.txt\n",
        7, "already on line 6"),
       ("a file where a directory must go",
        BASE + "[files]\n/docs = notes.txt\n/docs/b.txt = notes.txt\n", 7, "cannot go inside"))
def test_each_mistake_is_reported_with_its_line(label, text, line, fragment):
    with tempfile.TemporaryDirectory() as t:
        path = project_dir(t, text)
        try:
            read_project(path)
        except ProjectError as e:
            found = [(number, message) for number, message in e.mistakes if fragment in message]
            assert found, f"{label}: nothing mentions {fragment!r}: {e.mistakes}"
            assert found[0][0] == line, f"{label}: on line {found[0][0]}, want {line}"
            return
    raise AssertionError(f"{label}: no mistake was reported")


def test_every_mistake_is_reported_at_once():
    text = "[project]\ncolour = blue\n[boot]\ninstaller = gone.c\n[files]\nbad = notes.txt\n"
    with tempfile.TemporaryDirectory() as t:
        try:
            read_project(project_dir(t, text))
        except ProjectError as e:
            lines = e.lines()
        else:
            raise AssertionError("the mistakes were not reported")
    assert len(lines) == 4, lines          # the name, the key, the installer, the path
    assert any(":2: unknown key" in line for line in lines), lines
    assert any(":4: installer gone.c" in line for line in lines), lines
    assert any(":6: bad" in line for line in lines), lines


# --- building a disc ---------------------------------------------------------------

def test_the_disc_holds_what_the_project_names():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        source = t / "src"
        disc = build_disc(read_project(project_dir(source)), t / "disc.img", t / "build", quiet)
        with PgfsImage(disc) as img:
            assert img.label == "TESTOS"
            assert [e.name for e in img.listdir("/")] == ["install.bin", "pigeon.txt", "bin", "docs"]
            installer = img.read_file(INSTALLER_PATH)
            assert installer == built(t, source / "installer.c"), "not the launcher's build"
            assert img.read_file("/bin/tool.bin") == built(t, source / "tool.asm")
            assert img.read_file("/docs/notes.txt") == NOTES.encode()
            assert img.read_file(METADATA_PATH).decode().startswith("TestOS 2.5\n")
            assert img.boot_record() == (img.stat(INSTALLER_PATH).first, len(installer))
            assert img.fsck().clean
        sector = boot_sector()
        assert disc.read_bytes()[BOOT_CODE:BOOT_CODE + len(sector)] == sector
        assert not (t / "disc.img.partial").exists()


def test_the_same_project_builds_the_same_disc():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        project = read_project(project_dir(t / "src"))
        first = build_disc(project, t / "one.img", t / "build-one", quiet).read_bytes()
        second = build_disc(project, t / "two.img", t / "build-two", quiet).read_bytes()
    assert first == second, "two builds of one project differ"


def test_a_source_changed_to_another_is_built_again():
    """A build's name carries its source's name, so pointing a path at
    another source never reuses the old build -- even when the new source
    is older than it."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        source = t / "src"
        older = write(source, "other.asm", ".ORG PROGRAM_LOAD_ADDR\n    MOV A #7\n    HALT\n")
        path = project_dir(source)
        build_disc(read_project(path), t / "one.img", t / "build", quiet)
        path.write_text(GOOD.replace("tool.asm", "other.asm"))
        import os
        os.utime(older, (1, 1))
        disc = build_disc(read_project(path), t / "two.img", t / "build", quiet)
        with PgfsImage(disc) as img:
            assert img.read_file("/bin/tool.bin") == built(t, older)


@cases(("an assembled boot sector", "sector.asm", ".ORG BOOT_ENTRY\n    HALT\n", None),
       ("one too big for block 0", "sector.bin", "x" * 385, "room for 384"))
def test_a_boot_sector_the_project_names(label, name, text, error):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        write(t / "src", name, text)
        project = read_project(project_dir(t / "src", GOOD.replace(
            "installer = installer.c", f"installer = installer.c\nbootsector = {name}")))
        try:
            disc = build_disc(project, t / "disc.img", t / "build", quiet)
        except ProjectError as e:
            assert error and error in str(e), f"{label}: {e}"
            assert not (t / "disc.img").exists(), f"{label}: a disc was left behind"
            return
        assert error is None, f"{label}: built a disc"
        assert disc.read_bytes()[BOOT_CODE:BOOT_CODE + 16] == \
            b"\x16\xff\xff\xff\x00\x00\x00\x00" + bytes(8), f"{label}: not the named sector"


def test_the_disc_boots_its_installer():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        disc = build_disc(read_project(project_dir(t / "src")), t / "disc.img", t / "build", quiet)
        with power_on(t, disc=disc, keys=[ENTER]) as p:
            assert p.run(steps=4_000_000), p.rows()
            assert p.a == ANSWER, f"A={p.a:#x}, want {ANSWER:#x}"


@cases(("a system", "system = tiny.c", None),
       ("a system too big to boot", "system = big.bin", "loads at most"))
def test_a_system_goes_on_the_disc_as_boot_bin(label, line, error):
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        source = t / "src"
        write(source, "tiny.c", "int main(void) { return 5; }\n")
        (source / "big.bin").write_bytes(bytes(PROGRAM_MAX_SIZE + 1))
        project = read_project(project_dir(source, GOOD.replace(
            "installer = installer.c", f"installer = installer.c\n{line}")))
        try:
            disc = build_disc(project, t / "disc.img", t / "build", quiet)
        except ProjectError as e:
            assert error and error in str(e), f"{label}: {e}"
            return
        assert error is None, f"{label}: built a disc"
        with PgfsImage(disc) as img:
            assert img.read_file(SYSTEM_PATH) == built(t, source / "tiny.c"), label


def test_a_c_program_in_files_is_a_program_file_and_the_rest_are_images():
    """docs/kernel.md Q6. A .c in [files] is built for the kernel to load
    anywhere. The installer and the system are loaded at PROGRAM_LOAD_ADDR
    by a boot sector, and an .asm has no startup code that returns to a
    kernel, so those stay images."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        source = t / "src"
        write(source, "hello.c", "int main(int argc, char **argv) { return argc; }\n")
        write(source, "tiny.c", "int main(void) { return 5; }\n")
        text = GOOD.replace("installer = installer.c", "installer = installer.c\nsystem = tiny.c") \
                   .replace("[files]\n", "[files]\n/bin/hello.bin  = hello.c\n")
        disc = build_disc(read_project(project_dir(source, text)), t / "disc.img", t / "build", quiet)
        with PgfsImage(disc) as img:
            hello = img.read_file("/bin/hello.bin")
            assert hello == built(t, source / "hello.c", relocatable=True)
            assert Header.read(hello).patch_count > 0
            assert img.read_file(INSTALLER_PATH) == built(t, source / "installer.c")
            assert img.read_file(SYSTEM_PATH) == built(t, source / "tiny.c")
            assert img.read_file("/bin/tool.bin") == built(t, source / "tool.asm")
            for path in (INSTALLER_PATH, SYSTEM_PATH, "/bin/tool.bin"):
                assert img.read_file(path)[:4] != b"PGEX", f"{path} is a program file"


def test_the_example_project_builds_and_its_installer_asks_first():
    """user/os/pigeon_compiler_init.txt, as shipped. Its installer shows
    what it will do and waits; tests/test_install.py goes on and installs."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        disc = build_disc(read_project(EXAMPLE), t / "pigeonos.img", t / "build", quiet)
        with PgfsImage(disc) as img:
            assert img.label == "PIGEONOS" and img.fsck().clean
            assert img.read_file(SYSTEM_PATH) == built(t, REPO_ROOT / "user" / "os" / "kernel.c")
            for name in ("sh", "ls", "cat", "echo", "graph", "cube", "files"):
                path = f"/bin/{name}.bin"
                assert img.read_file(path)[:4] == b"PGEX", f"{path} is not a program file"
        with power_on(t, disc=disc, keys=[ENTER]) as p:
            assert p.run_until(lambda rows: rows[11] == "ENTER install   ESC cancel",
                               steps=20_000_000), p.rows()
            rows = p.rows()
    assert rows[0] == "PigeonOS 0.1" and rows[1] == "INSTALLER", rows
    assert rows[3] == "Hard disk: 4096 K" and rows[4] == "Everything on it is erased.", rows
    assert rows[6] == "10 files to copy" and rows[7] == "It will boot /boot.bin", rows


def test_cc_py_builds_a_project_on_the_command_line():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        path, disc = project_dir(t / "src"), t / "disc.img"
        out = io.StringIO()
        # Its programs are built under the config's build_dir, which is the
        # real build/ -- where they would sit in the disc picker for good.
        config = mock.Mock(build_dir=t / "build")
        with contextlib.redirect_stdout(out), \
                mock.patch("emulator.config.load_config", return_value=config):
            assert cc.main(["--project", str(path), "-o", str(disc)]) == 0
        assert disc.is_file() and "boots /install.bin" in out.getvalue(), out.getvalue()
        assert (t / "build" / "testos").is_dir(), "built somewhere other than build_dir"

        bad = write(t / "src", "bad.txt", "[project]\nname = X\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            assert cc.main(["--project", str(bad)]) == 1
        assert "bad.txt: [boot] needs installer" in err.getvalue(), err.getvalue()

        for argv in ([], ["--project", str(path), "extra.c"]):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    cc.main(argv)
                except SystemExit as e:
                    assert e.code == 2, argv
                else:
                    raise AssertionError(f"{argv} was accepted")


# --- the launcher's --cd -----------------------------------------------------------

def test_the_launcher_puts_the_disc_in_before_power_on():
    with tempfile.TemporaryDirectory() as t:
        disc = Path(t) / "disc.img"
        disc.write_bytes(bytes(4096))
        config = load_config()
        drive = disc_drive(dataclasses.replace(config, cd=None))
        assert not drive.status()["present"], "a disc was put in with none named"
        drive.close()

        named = config.override(cd=str(disc))
        try:
            disc_drive(named)                  # cd_root is the repo, and this is not in it
        except PermissionError:
            pass
        else:
            raise AssertionError("a disc outside cd_root went in")

        drive = disc_drive(dataclasses.replace(named, cd_root=None))
        status = drive.status()
        drive.close()
        assert status["present"] and status["name"] == "disc.img", status

        try:
            disc_drive(dataclasses.replace(named, cd_root=None, cd=Path(t) / "nope.img"))
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("a missing disc was accepted")


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "cc.py --project"))
