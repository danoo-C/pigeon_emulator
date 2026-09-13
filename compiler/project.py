"""cc.py --project: build an installation disc from a project file.

    python3 compiler/cc.py --project user/os/pigeon_compiler_init.txt
    python3 compiler/cc.py --project user/os/pigeon_compiler_init.txt -o cds/os.img

docs/os_cd.md, section 7, has the design. A project file names the
installer, optionally a boot sector, and the files the installer puts on
the hard disk:

    [project]
    name    = PigeonOS
    version = 0.1
    label   = PIGEONOS              # the disc's volume label, 15 bytes at most

    [boot]
    installer  = installer.c        # the disc boots this
    bootsector = boot.asm           # optional: firmware/boot.asm otherwise

    [files]
    /bin/files.bin   = ../files.c   # a .c or .asm is built; anything else is copied
    /docs/readme.txt = readme.txt

A '#' starts a comment at the start of a line or after a space. Paths on
the right are relative to the project file. Every mistake in the file is
reported, each with its line, before anything is built.

The disc is a PigeonFS image, written in this order:

    /install.bin   the installer, first, so its blocks are contiguous: the
                   boot sector cannot follow the FAT
    /pigeon.txt    the title the installer shows, then the [project] keys
    [files]        in the order given, with directories made as needed

and block 0 is made to boot /install.bin. The same project builds the
same disc, byte for byte.
"""
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import pfs                                                            # noqa: E402
from emulator.memory_map import BOOT_CODE                             # noqa: E402
from pfs import BLOCK, LABEL_MAX, MIN_BLOCKS, PgfsError, PgfsImage    # noqa: E402

INSTALLER_PATH = "/install.bin"
METADATA_PATH = "/pigeon.txt"
BUILT = (".c", ".asm")              # anything else goes on the disc as it is

# section -> {key: required}. [files] takes any key: a path on the disc.
SECTIONS = {
    "project": {"name": True, "version": False, "label": False},
    "boot": {"installer": True, "bootsector": False},
    "files": None,
}
_SECTION = re.compile(r"\[\s*([A-Za-z]\w*)\s*\]")
_COMMENT = re.compile(r"(?:^|\s)#")


class ProjectError(Exception):
    """What is wrong, as (line, message) pairs. Line 0 is the file as a
    whole: a key that is missing, or a problem found while building."""

    def __init__(self, path, mistakes):
        self.path = Path(path)
        self.mistakes = sorted(mistakes, key=lambda mistake: mistake[0])
        super().__init__("\n".join(self.lines()))

    def lines(self) -> List[str]:
        return [f"{self.path}:{line}: {message}" if line else f"{self.path}: {message}"
                for line, message in self.mistakes]


@dataclass
class Project:
    path: Path
    name: str
    version: str
    label: str
    installer: Path
    bootsector: Optional[Path]
    files: List[Tuple[str, Path]]           # (path on the disc, file on the host)

    @property
    def title(self) -> str:
        return f"{self.name} {self.version}".strip()

    @property
    def slug(self) -> str:
        """The name, fit for a file name: build/<slug>.img."""
        return re.sub(r"[^a-z0-9._-]+", "-", self.name.lower()).strip("-.") or "disc"

    def metadata(self) -> bytes:
        """/pigeon.txt: the title the installer shows, then the [project] keys."""
        return (f"{self.title}\nname = {self.name}\nversion = {self.version}\n"
                f"label = {self.label}\n").encode("utf-8")


def read_project(path) -> Project:
    """Read and check a project file. Raises ProjectError with every
    mistake in it, not just the first."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ProjectError(path, [(0, f"cannot be read: {e}")]) from None

    mistakes: List[Tuple[int, str]] = []
    sections: Dict[str, Dict[str, Tuple[str, int]]] = {}
    current: Optional[str] = None

    for number, raw in enumerate(text.splitlines(), 1):
        line = _COMMENT.split(raw, 1)[0].strip()
        if not line:
            continue
        header = _SECTION.fullmatch(line)
        if header:
            current = header.group(1).lower()
            if current not in SECTIONS:
                mistakes.append((number, f"unknown section [{header.group(1)}]; the "
                                         f"sections are [project], [boot] and [files]"))
            elif current in sections:
                mistakes.append((number, f"[{current}] appears twice"))
            sections.setdefault(current, {})
            continue
        if "=" not in line:
            mistakes.append((number, f"expected key = value, not {line!r}"))
            continue
        if current is None:
            mistakes.append((number, "a key before any [section]"))
            continue
        if current not in SECTIONS:
            continue                        # reported once, at the section
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value:
            mistakes.append((number, f"{line!r}: a key and a value are both needed"))
            continue
        allowed = SECTIONS[current]
        if allowed is not None and key not in allowed:
            mistakes.append((number, f"unknown key {key!r} in [{current}]; it takes "
                                     f"{', '.join(allowed)}"))
            continue
        if key in sections[current]:
            mistakes.append((number, f"{key} appears twice in [{current}], first on line "
                                     f"{sections[current][key][1]}"))
            continue
        sections[current][key] = (value, number)

    for section, keys in SECTIONS.items():
        for key, required in (keys or {}).items():
            if required and key not in sections.get(section, {}):
                mistakes.append((0, f"[{section}] needs {key} = ..."))

    base = path.parent

    def host_file(entry, what, suffixes=None) -> Path:
        value, number = entry
        target = base / value
        if suffixes is not None and target.suffix.lower() not in suffixes:
            mistakes.append((number, f"{what} {value}: must be a {' or '.join(suffixes)} file"))
        elif not target.is_file():
            mistakes.append((number, f"{what} {value}: no such file ({target})"))
        return target

    project = sections.get("project", {})
    boot = sections.get("boot", {})
    name = project.get("name", ("", 0))[0]
    version = project.get("version", ("", 0))[0]
    if "label" in project:
        label, number = project["label"]
        if len(label.encode("utf-8")) > LABEL_MAX:
            mistakes.append((number, f"label {label!r} is {len(label.encode('utf-8'))} bytes; "
                                     f"a volume label is at most {LABEL_MAX}"))
    else:
        label = re.sub(r"[^A-Z0-9]", "", name.upper())[:LABEL_MAX]
    installer = (host_file(boot["installer"], "installer", (".c", ".asm", ".bin"))
                 if "installer" in boot else None)
    bootsector = (host_file(boot["bootsector"], "bootsector", (".asm", ".bin"))
                  if "bootsector" in boot else None)

    files: List[Tuple[str, Path]] = []
    lines: Dict[str, int] = {}
    for disc_path, entry in sections.get("files", {}).items():
        number = entry[1]
        normal, problem = _disc_path(disc_path)
        if problem is None and normal in (INSTALLER_PATH, METADATA_PATH):
            problem = f"{normal} is the disc's own file; choose another path"
        if problem is None and normal in lines:
            problem = f"{normal} is already on line {lines[normal]}"
        if problem is not None:
            mistakes.append((number, problem))
            continue
        lines[normal] = number
        files.append((normal, host_file(entry, normal)))
    for normal, number in lines.items():
        parts = normal.split("/")
        for depth in range(2, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in lines:
                mistakes.append((max(number, lines[parent]),
                                 f"{parent} is a file, on line {lines[parent]}, so {normal} "
                                 f"cannot go inside it"))

    if mistakes:
        raise ProjectError(path, mistakes)
    return Project(path, name, version, label, installer, bootsector, files)


def _disc_path(text: str) -> Tuple[str, Optional[str]]:
    """A path on the disc, normalised -- or why it cannot be one."""
    if not text.startswith("/"):
        return text, f"{text}: a path on the disc must start with /"
    try:
        parts = pfs.split_path(text)
        for part in parts:
            pfs.check_name(part)
    except PgfsError as e:
        return text, str(e)
    if not parts:
        return text, f"{text}: that is the root, not a file"
    return "/" + "/".join(parts), None


def build_disc(project: Project, output, build_dir,
               say: Callable[[str], None] = print) -> Path:
    """Build what the project names, and write its disc to `output`.

    Programs are built into build_dir/<slug>/ the way the launcher builds
    them, and only when a source or a library it includes has changed.
    Each build's name carries its source's name as well as its path on the
    disc, so pointing a path at another source can never reuse the old
    build. The image is written beside `output` and moved into place once
    it is whole, so a failed build never leaves half a disc behind."""
    from emulator.programs import Program

    output, work = Path(output), Path(build_dir) / project.slug

    def contents_of(host: Path, disc_path: str) -> bytes:
        if host.suffix.lower() not in BUILT:
            return host.read_bytes()
        where = Path(disc_path.lstrip("/"))
        binary = work / where.parent / f"{where.name}.{host.name}.bin"
        Program(name=host.stem, source=host, binary=binary).ensure_built(quiet=True)
        return binary.read_bytes()

    contents = [(INSTALLER_PATH, contents_of(project.installer, INSTALLER_PATH),
                 project.installer),
                (METADATA_PATH, project.metadata(), None)]
    contents += [(disc_path, contents_of(host, disc_path), host)
                 for disc_path, host in project.files]
    sector = _boot_sector(project)
    size = _image_size([(disc_path, len(data)) for disc_path, data, _ in contents])

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    partial.unlink(missing_ok=True)
    try:
        with PgfsImage.mkfs(partial, size, label=project.label) as img:
            for disc_path, data, _ in contents:
                parent = disc_path.rsplit("/", 1)[0]
                if parent:
                    img.mkdir(parent, parents=True)
                img.write_file(disc_path, data)
            img.make_bootable(INSTALLER_PATH, sector)
            problems = img.fsck().problems
        if problems:
            raise ProjectError(project.path, [(0, f"the disc fails fsck: {problem.message}")
                                              for problem in problems])
        os.replace(partial, output)
    except PgfsError as e:
        raise ProjectError(project.path, [(0, f"writing the disc: {e}")]) from None
    finally:
        partial.unlink(missing_ok=True)

    say(f"{project.title}, from {project.path}")
    for disc_path, data, source in contents:
        note = f"   {source}" if source is not None else ""
        say(f"  {disc_path:<24} {len(data):>9,} B{note}")
    say(f"{output}: {pfs._human(size)}, label {project.label}, boots {INSTALLER_PATH}")
    return output


def _boot_sector(project: Project) -> bytes:
    if project.bootsector is None:
        sector, name = pfs.boot_sector(), "firmware/boot.asm"
    elif project.bootsector.suffix.lower() == ".asm":
        from assembler.assembler import Assembler
        sector, name = Assembler(str(project.bootsector)).assemble(), str(project.bootsector)
    else:
        sector, name = project.bootsector.read_bytes(), str(project.bootsector)
    room = BLOCK - BOOT_CODE
    if len(sector) > room:
        raise ProjectError(project.path, [(0, f"the boot sector {name} is {len(sector)} "
                                              f"bytes; block 0 has room for {room}")])
    return sector


def _image_size(files: List[Tuple[str, int]]) -> int:
    """Blocks enough for every file and directory, the FAT and the
    superblock, and a little to spare."""
    entries: Dict[str, int] = {"/": 0}
    data = 0
    for disc_path, size in files:
        parts = disc_path.strip("/").split("/")
        for depth in range(1, len(parts) + 1):
            here = "/" + "/".join(parts[:depth])
            parent = "/" + "/".join(parts[:depth - 1])
            if depth == len(parts) or here not in entries:
                entries[parent] = entries.get(parent, 0) + 1
                if depth < len(parts):
                    entries[here] = 0
        data += -(-size // BLOCK)
    data += sum(max(1, -(-count // 8)) for count in entries.values())
    spare = 16
    total = data + 1 + spare
    while data + 1 + -(-total // 128) + spare > total:
        total = data + 1 + -(-total // 128) + spare
    return max(total, MIN_BLOCKS) * BLOCK
