"""config.json loading, validation, and program discovery.

    python3 tests/test_config.py      (or: python3 -m pytest tests/)
"""
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import cases, run_module                              # noqa: E402
from emulator.config import DEFAULTS, ConfigError, load_config     # noqa: E402
from emulator.programs import (Program, discover, find, from_path,  # noqa: E402
                               libraries_for)


def write_config(directory, settings):
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(settings) if isinstance(settings, (dict, list))
                    else settings)
    return path


# --- loading ----------------------------------------------------------------

def test_shipped_config_is_valid():
    """config.json at the repo root must always load."""
    config = load_config()
    assert config.source_path == REPO_ROOT / "config.json"
    assert not config.unknown_keys, f"unknown keys: {config.unknown_keys}"


def test_absent_config_falls_back_to_defaults():
    """No config.json at all is fine -- every key has a default."""
    import emulator.config as config_mod
    original = config_mod.CONFIG_PATH
    try:
        with tempfile.TemporaryDirectory() as d:
            config_mod.CONFIG_PATH = Path(d) / "not-created.json"
            config = load_config()
        assert config.source_path is None
        assert config.display_port == DEFAULTS["display_port"]
        assert config.host == DEFAULTS["host"]
    finally:
        config_mod.CONFIG_PATH = original


def test_explicit_missing_file_is_an_error():
    with tempfile.TemporaryDirectory() as d:
        try:
            load_config(Path(d) / "nope.json")
        except ConfigError as e:
            assert "No such config file" in str(e)
            return
    raise AssertionError("a missing --config file should raise ConfigError")


def test_partial_config_keeps_defaults_for_the_rest():
    with tempfile.TemporaryDirectory() as d:
        config = load_config(write_config(d, {"display_port": 9999}))
    assert config.display_port == 9999
    assert config.hid_port == DEFAULTS["hid_port"], "untouched key lost its default"
    assert config.host == DEFAULTS["host"]


def test_paths_resolve_against_the_repo_root():
    """So the config means the same thing from any working directory."""
    with tempfile.TemporaryDirectory() as d:
        config = load_config(write_config(d, {"build_dir": "somewhere"}))
    assert config.build_dir == REPO_ROOT / "somewhere"
    assert config.build_dir.is_absolute()


def test_absolute_paths_are_left_alone():
    with tempfile.TemporaryDirectory() as d:
        config = load_config(write_config(d, {"build_dir": "/tmp/pigeon-build"}))
    assert config.build_dir == Path("/tmp/pigeon-build")


def test_program_dirs_accepts_a_bare_string():
    """An easy mistake; coerced rather than rejected."""
    with tempfile.TemporaryDirectory() as d:
        config = load_config(write_config(d, {"program_dirs": "user"}))
    assert config.program_dirs == [REPO_ROOT / "user"]


def test_unknown_keys_are_reported_not_fatal():
    with tempfile.TemporaryDirectory() as d:
        config = load_config(write_config(d, {"prot_dirs": ["user"], "host": "1.2.3.4"}))
    assert config.unknown_keys == ["prot_dirs"]
    assert config.host == "1.2.3.4", "a typo'd key should not block the valid ones"


def test_urls():
    with tempfile.TemporaryDirectory() as d:
        config = load_config(write_config(d, {"host": "0.0.0.0", "display_port": 1234,
                                              "hid_port": 5678}))
    assert config.display_url == "http://0.0.0.0:1234"
    assert config.hid_url == "http://0.0.0.0:5678"


# --- validation -------------------------------------------------------------

@cases(
    ({"display_port": 8000, "hid_port": 8000}, "must differ"),
    ({"cd_port": 8001}, "must differ"),              # clashes with hid_port
    ({"cd_port": "nope"}, "whole number"),
    ({"cd_max_upload": "lots"}, "not a size"),
    ({"cd_max_upload": 0}, "must be positive"),
    ({"cd_dirs": "cds"}, None),                      # a bare string is allowed
    ({"cd_dirs": [1]}, "list of folder names"),
    ({"cd_root": 7}, "folder name"),
    ({"display_port": 99999}, "between 1 and 65535"),
    ({"display_port": 0}, "between 1 and 65535"),
    ({"display_port": "8000"}, "whole number"),
    ({"display_port": True}, "whole number"),
    ({"auto_build": "yes"}, "true or false"),
    ({"program_dirs": [1, 2]}, "list of folder names"),
    ("{bad json,}", "not valid JSON"),
    ([1, 2, 3], "must contain a JSON object"),
)
def test_bad_config_is_rejected_with_a_useful_message(settings, expected):
    """expected=None means the value is fine and must NOT be rejected."""
    with tempfile.TemporaryDirectory() as d:
        try:
            load_config(write_config(d, settings))
        except ConfigError as e:
            if expected is None:
                raise AssertionError(f"{settings!r} should have been accepted: {e}")
            assert expected in str(e), f"expected {expected!r} in: {e}"
            return
    if expected is not None:
        raise AssertionError(f"{settings!r} should have been rejected")


# --- flag overrides ---------------------------------------------------------

def test_flags_override_config_and_none_means_untouched():
    config = load_config().override(display_port=4321, host=None)
    assert config.display_port == 4321
    assert config.host == load_config().host, "host=None should not have changed it"


def test_the_disk_is_not_build_output():
    """The channel-2 disk holds what programs save (docs/filesystem.md),
    so it must not live where a clean build deletes things -- in the
    shipped config or in the defaults a missing config falls back to."""
    shipped = load_config()
    assert shipped.build_dir not in shipped.disk.parents
    assert not Path(DEFAULTS["disk"]).is_relative_to(DEFAULTS["build_dir"])


def test_override_resolves_paths_too():
    config = load_config().override(build_dir="elsewhere")
    assert config.build_dir == REPO_ROOT / "elsewhere"


# --- program discovery ------------------------------------------------------

def test_discovers_the_user_programs():
    names = {p.name for p in discover(load_config())}
    assert {"checkerboard", "screen", "sincos", "ui"} <= names, names


def test_discovers_c_programs_too():
    """A .c program must appear in the picker alongside .asm ones -- the
    launcher only globbed *.asm and *.bin, so user/demo.c was invisible."""
    found = {p.name: p for p in discover(load_config())}
    assert "demo" in found, f"demo.c not discovered: {sorted(found)}"
    assert found["demo"].language == "c"
    assert found["demo"].source.suffix == ".c"


def test_asm_programs_are_still_assembly():
    found = {p.name: p for p in discover(load_config())}
    assert found["screen"].language == "asm"


def test_library_dependencies_come_from_the_includes():
    """There is no linker, so every unit must be named on the command
    line. Reading them off the #includes means the user does not have to."""
    libraries = libraries_for(REPO_ROOT / "user" / "demo.c")
    names = {p.name for p in libraries}
    assert names == {"display.c", "input.c", "mem.c"}, names


def test_a_c_program_with_no_includes_needs_no_libraries():
    with tempfile.TemporaryDirectory() as d:
        plain = Path(d) / "p.c"
        plain.write_text("int main(void){ return 0; }\n")
        assert libraries_for(plain) == []


def test_c_program_is_stale_when_a_library_changes():
    """Editing display.c must rebuild every program that uses it."""
    import os
    with tempfile.TemporaryDirectory() as d:
        source = Path(d) / "p.c"
        source.write_text("#include <pigeon/display.h>\nint main(void){return 0;}\n")
        binary = Path(d) / "p.bin"
        binary.write_bytes(b"\x00" * 8)
        program = Program(name="p", source=source, binary=binary)

        os.utime(binary, None)
        os.utime(source, (0, 0))
        assert not program.stale, "nothing newer than the build"

        library = REPO_ROOT / "lib" / "pigeon" / "display.c"
        os.utime(binary, (library.stat().st_mtime - 100,) * 2)
        assert program.stale, "a newer library should make the build stale"


def test_source_and_binary_are_one_entry():
    """A .asm and its build must not show up as two programs."""
    found = discover(load_config())
    assert len(found) == len({p.name for p in found}), "duplicate names in the listing"


@cases("screen", "SCREEN", "screen.asm", "screen.bin", "2")
def test_find_accepts_names_filenames_and_indices(wanted):
    program = find(load_config(), wanted)
    assert program is not None, f"{wanted!r} matched nothing"
    if wanted != "2":
        assert program.name == "screen"


@cases("nope", "0", "999", "")
def test_find_rejects_what_it_should(wanted):
    assert find(load_config(), wanted) is None


def test_from_path_handles_asm_and_bin():
    config = load_config()
    asm = from_path(config, REPO_ROOT / "user" / "screen.asm")
    assert asm.source is not None and asm.binary == config.build_dir / "screen.bin"
    binary = from_path(config, REPO_ROOT / "build" / "screen.bin")
    assert binary.source is None and binary.prebuilt


def test_staleness_is_detected():
    with tempfile.TemporaryDirectory() as d:
        source, binary = Path(d) / "p.asm", Path(d) / "p.bin"
        source.write_text("HALT\n")
        binary.write_bytes(b"\x00" * 8)
        import os
        os.utime(binary, (0, 0))                       # binary older than source
        program = Program(name="p", source=source, binary=binary)
        assert program.built and program.stale and program.status == "stale"
        os.utime(binary, None)                         # now newer
        assert not program.stale and program.status == "built"


def test_missing_program_dir_is_reported_not_fatal():
    with tempfile.TemporaryDirectory() as d:
        config = load_config(write_config(d, {"program_dirs": ["user", "no-such-folder"]}))
    found = discover(config)
    assert found, "an existing folder should still be scanned"
    assert any("no-such-folder" in str(p) for p in found[0].missing_dirs)


if __name__ == "__main__":
    raise SystemExit(run_module(dict(globals()), "config + program discovery"))
