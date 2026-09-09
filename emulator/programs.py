"""Finding and building the programs the emulator can run.

A program is whatever the configured `program_dirs` contain: an assembly
source, a prebuilt binary, or a source with its build sitting in
`build_dir`. This module works out which, notices when a build has gone
stale, and assembles on demand.
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import REPO_ROOT, Config


def short(path: Path) -> str:
    """Path relative to the repo root when it is inside it, else absolute."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


@dataclass
class Program:
    """One runnable thing: a source, a binary, or both.

    A source is either assembly (.asm, handed to the assembler) or C
    (.c, handed to the compiler, which produces assembly and then hands
    THAT to the assembler).
    """
    name: str
    source: Optional[Path] = None    # .asm or .c
    binary: Optional[Path] = None    # .bin, built or prebuilt
    prebuilt: bool = False           # a .bin with no source next to it

    @property
    def language(self) -> str:
        if self.source is None:
            return "bin"
        return "c" if self.source.suffix.lower() == ".c" else "asm"

    @property
    def built(self) -> bool:
        return self.binary is not None and self.binary.exists()

    @property
    def stale(self) -> bool:
        """The binary exists but a source has been edited since.

        For a C program the libraries count too: editing display.c must
        rebuild every program that uses it.
        """
        if self.source is None or not self.built:
            return False
        built_at = self.binary.stat().st_mtime
        if self.source.stat().st_mtime > built_at:
            return True
        if self.language == "c":
            return any(lib.stat().st_mtime > built_at
                       for lib in libraries_for(self.source))
        return False

    @property
    def status(self) -> str:
        if self.prebuilt:
            return "prebuilt"
        if not self.built:
            return "not built"
        return "stale" if self.stale else "built"

    def ensure_built(self, quiet: bool = False) -> Path:
        """Build if needed, and return a path to a runnable binary."""
        if self.source is None:
            if not self.built:
                raise FileNotFoundError(f"{self.name}: no source and no binary")
            return self.binary
        if self.built and not self.stale:
            return self.binary

        from assembler.assembler import assemble_file

        if self.language == "c":
            from compiler.cc import compile_units

            units = [self.source, *libraries_for(self.source)]
            if not quiet and len(units) > 1:
                names = ", ".join(u.name for u in units[1:])
                print(f"Compiling {self.source.name} with {names}")
            asm_path = self.binary.with_suffix(".asm")
            asm_path.parent.mkdir(parents=True, exist_ok=True)
            asm_path.write_text(compile_units(units))
            assemble_file(asm_path, self.binary, quiet=quiet)
        else:
            assemble_file(self.source, self.binary, quiet=quiet)
        return self.binary


def discover(config: Config) -> List[Program]:
    """Every program across the configured folders, sorted by name.

    A `.asm` and a same-named `.bin` are one entry, not two. Sources win
    the name; a stray binary only appears when nothing builds to it.
    """
    by_name: dict[str, Program] = {}
    missing_dirs = []

    for folder in config.program_dirs:
        if not folder.is_dir():
            missing_dirs.append(folder)
            continue

        for source in sorted(list(folder.glob("*.asm")) + list(folder.glob("*.c"))):
            program = by_name.setdefault(source.stem, Program(name=source.stem))
            if program.source is None:
                program.source = source
                program.binary = config.build_dir / f"{source.stem}.bin"
                program.prebuilt = False

        for binary in sorted(folder.glob("*.bin")):
            program = by_name.get(binary.stem)
            if program is None:
                by_name[binary.stem] = Program(
                    name=binary.stem, binary=binary, prebuilt=True)

    programs = sorted(by_name.values(), key=lambda p: p.name)
    for program in programs:
        program.missing_dirs = missing_dirs  # noqa: B010 - for the CLI's warning
    return programs


def find(config: Config, wanted: str) -> Optional[Program]:
    """Resolve a name typed at the prompt or passed to --program.

    Accepts a program name ("screen"), a filename ("screen.asm"), or a
    1-based index into the listing.
    """
    programs = discover(config)

    if wanted.isdigit():
        index = int(wanted)
        return programs[index - 1] if 1 <= index <= len(programs) else None

    stem = Path(wanted).stem.lower()
    for program in programs:
        if program.name.lower() == stem:
            return program
    return None


INCLUDE_RE = re.compile(r'^\s*#\s*include\s*<pigeon/(\w+)\.h>', re.MULTILINE)


def libraries_for(source: Path) -> List[Path]:
    """Which lib/pigeon/*.c a C program needs, from its #includes.

    There is no linker -- units are compiled together -- so the driver has
    to be told every source. Reading it off the includes means `run demo`
    works without anyone having to remember the list.
    """
    lib_dir = REPO_ROOT / "lib" / "pigeon"
    found: List[Path] = []
    pending = [Path(source)]
    seen = set()
    while pending:
        current = pending.pop()
        try:
            text = current.read_text()
        except OSError:
            continue
        for name in INCLUDE_RE.findall(text):
            implementation = lib_dir / f"{name}.c"
            if implementation.is_file() and implementation not in seen:
                seen.add(implementation)
                found.append(implementation)
                pending.append(implementation)
    return sorted(found)


def from_path(config: Config, path: Path) -> Program:
    """Wrap an explicit path, which need not live in a program folder."""
    path = Path(path)
    if path.suffix.lower() in (".asm", ".c"):
        return Program(name=path.stem, source=path,
                       binary=config.build_dir / f"{path.stem}.bin")
    return Program(name=path.stem, binary=path, prebuilt=True)
