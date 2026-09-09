"""Finding and building the programs the emulator can run.

A program is whatever the configured `program_dirs` contain: an assembly
source, a prebuilt binary, or a source with its build sitting in
`build_dir`. This module works out which, notices when a build has gone
stale, and assembles on demand.
"""
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
    """One runnable thing: a source, a binary, or both."""
    name: str
    source: Optional[Path] = None    # .asm
    binary: Optional[Path] = None    # .bin, built or prebuilt
    prebuilt: bool = False           # a .bin with no source next to it

    @property
    def built(self) -> bool:
        return self.binary is not None and self.binary.exists()

    @property
    def stale(self) -> bool:
        """The binary exists but the source has been edited since."""
        if self.source is None or not self.built:
            return False
        return self.source.stat().st_mtime > self.binary.stat().st_mtime

    @property
    def status(self) -> str:
        if self.prebuilt:
            return "prebuilt"
        if not self.built:
            return "not built"
        return "stale" if self.stale else "built"

    def ensure_built(self, quiet: bool = False) -> Path:
        """Assemble if needed, and return a path to a runnable binary."""
        if self.source is None:
            if not self.built:
                raise FileNotFoundError(f"{self.name}: no source and no binary")
            return self.binary
        if self.built and not self.stale:
            return self.binary

        from assembler.assembler import assemble_file

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

        for source in sorted(folder.glob("*.asm")):
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


def from_path(config: Config, path: Path) -> Program:
    """Wrap an explicit path, which need not live in a program folder."""
    path = Path(path)
    if path.suffix.lower() == ".asm":
        return Program(name=path.stem, source=path,
                       binary=config.build_dir / f"{path.stem}.bin")
    return Program(name=path.stem, binary=path, prebuilt=True)
