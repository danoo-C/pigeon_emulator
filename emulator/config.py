"""Configuration: defaults, config.json, and the precedence between them.

Settings come from three places, later winning over earlier:

    1. DEFAULTS below
    2. config.json at the repo root (or --config PATH)
    3. command-line flags

So config.json is where you change something permanently, and a flag is
how you override it once. A missing or partial config.json is fine --
every key falls back to its default.

All paths in config.json are relative to the repo root unless absolute,
so the file works the same no matter which directory you run from.
"""
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config.json"

DEFAULTS = {
    # Where the display, input and CD HTTP servers listen. Each needs its
    # own port: they are separate uvicorn instances on separate threads.
    "host": "127.0.0.1",
    "display_port": 8000,
    "hid_port": 8001,
    "cd_port": 8002,

    # Folders scanned for programs to run. Add your own here; the launcher
    # lists everything it finds across all of them.
    "program_dirs": ["user"],

    # Assembled output: anything in it can be rebuilt, so a clean may
    # delete it at any time.
    "build_dir": "build",
    # The channel-2 disk. NOT under build/: it is a disk, and what
    # programs save on it (docs/filesystem.md) must survive a clean.
    "disk": "disks/hdd.img",

    # The BIOS. Rebuilt automatically when the binary is older than the
    # source, since build/ is gitignored and a fresh clone has neither.
    "bios_source": "firmware/bios.asm",
    "bios_binary": "build/bios.bin",
    "auto_build": True,

    # --- the CD drive (docs/cd-drive.md) ---
    # Discs may be inserted from anywhere under cd_root. "." is the repo
    # root, so everything in the project is reachable and nothing outside
    # it is; null allows the whole filesystem. The front ends' file dialog
    # opens here, and a path outside it is refused with a message naming
    # this key.
    "cd_root": ".",
    # Folders the "Load from server" picker lists, non-recursively.
    "cd_dirs": ["cds", "build"],
    # Where an upload from the browser is written before it is inserted.
    # It keeps its own name, so an uploaded disc stays in the picker.
    "cd_upload_dir": "cds",
    # The upload path is the only one that writes to the host disk, so it
    # is the only one with a ceiling. "64M", "512K" or a byte count.
    "cd_max_upload": "64M",
}


class ConfigError(Exception):
    """config.json is present but unusable."""


@dataclass
class Config:
    host: str
    display_port: int
    hid_port: int
    cd_port: int
    cd_root: Optional[Path]
    cd_dirs: List[Path]
    cd_upload_dir: Path
    cd_max_upload: int
    program_dirs: List[Path]
    build_dir: Path
    disk: Path
    bios_source: Path
    bios_binary: Path
    auto_build: bool
    source_path: Optional[Path] = None   # which config.json this came from
    unknown_keys: List[str] = field(default_factory=list)

    @property
    def display_url(self) -> str:
        return f"http://{self.host}:{self.display_port}"

    @property
    def hid_url(self) -> str:
        return f"http://{self.host}:{self.hid_port}"

    @property
    def cd_url(self) -> str:
        return f"http://{self.host}:{self.cd_port}"

    def override(self, **kwargs) -> "Config":
        """Apply command-line flags. None means 'not given, keep config'."""
        given = {k: v for k, v in kwargs.items() if v is not None}
        for key in ("program_dirs", "build_dir", "disk", "bios_source", "bios_binary",
                    "cd_dirs", "cd_upload_dir", "cd_root"):
            if key in given:
                given[key] = ([_resolve(p) for p in given[key]]
                              if key in ("program_dirs", "cd_dirs")
                              else _resolve(given[key]))
        return replace(self, **given)


def _resolve(value) -> Path:
    """Interpret a configured path relative to the repo root."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _size(value, name: str) -> int:
    """'64M', '512K', '4MiB' or a byte count -> bytes. Units are binary.

    Deliberately a copy of tools/pfs.py's parse_size rather than an import
    of it: pfs.py imports THIS module for the default image path, and the
    cycle would be real.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        number, scale = value, 1
    elif isinstance(value, str):
        text = value.strip().upper().removesuffix("IB").removesuffix("B")
        scale = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(text[-1:], 1)
        try:
            number = int(text[:-1] if scale != 1 else text)
        except ValueError:
            raise ConfigError(f"{name} is not a size: {value!r} (try 64M, 512K "
                              f"or a byte count)") from None
    else:
        raise ConfigError(f"{name} is not a size: {value!r} (try 64M, 512K "
                          f"or a byte count)")
    if number <= 0:
        raise ConfigError(f"{name} must be positive, got {value!r}")
    return number * scale


def _port(value, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{name} must be a whole number, got {value!r}")
    if not 1 <= value <= 65535:
        raise ConfigError(f"{name} must be between 1 and 65535, got {value}")
    return value


def load_config(path: Optional[Path] = None) -> Config:
    """Read config.json, falling back to DEFAULTS for anything absent.

    Raises ConfigError with an actionable message when the file exists but
    is malformed -- a typo in config.json should say so, not surface later
    as a confusing TypeError.
    """
    explicit = path is not None
    path = Path(path) if explicit else CONFIG_PATH

    settings = dict(DEFAULTS)
    unknown: List[str] = []

    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path} is not valid JSON: {e}") from None
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a JSON object, got "
                              f"{type(loaded).__name__}")
        unknown = sorted(set(loaded) - set(DEFAULTS))
        settings.update({k: v for k, v in loaded.items() if k in DEFAULTS})
    elif explicit:
        raise ConfigError(f"No such config file: {path}")

    dirs = settings["program_dirs"]
    if isinstance(dirs, str):        # a bare string is an easy mistake to make
        dirs = [dirs]
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise ConfigError("program_dirs must be a list of folder names, e.g. "
                          f'["user", "demos"] -- got {dirs!r}')

    if not isinstance(settings["auto_build"], bool):
        raise ConfigError(f"auto_build must be true or false, got "
                          f"{settings['auto_build']!r}")

    display_port = _port(settings["display_port"], "display_port")
    hid_port = _port(settings["hid_port"], "hid_port")
    cd_port = _port(settings["cd_port"], "cd_port")
    ports = {"display_port": display_port, "hid_port": hid_port, "cd_port": cd_port}
    if len(set(ports.values())) != len(ports):
        clash = ", ".join(f"{k}={v}" for k, v in ports.items())
        raise ConfigError(f"the server ports must differ ({clash}); each server "
                          "is its own uvicorn instance and needs its own port")

    cd_dirs = settings["cd_dirs"]
    if isinstance(cd_dirs, str):
        cd_dirs = [cd_dirs]
    if not isinstance(cd_dirs, list) or not all(isinstance(d, str) for d in cd_dirs):
        raise ConfigError("cd_dirs must be a list of folder names, e.g. "
                          f'["cds", "build"] -- got {cd_dirs!r}')
    cd_root = settings["cd_root"]
    if cd_root is not None and not isinstance(cd_root, str):
        raise ConfigError("cd_root must be a folder name, or null for anywhere -- "
                          f"got {cd_root!r}")

    return Config(
        host=str(settings["host"]),
        display_port=display_port,
        hid_port=hid_port,
        cd_port=cd_port,
        cd_root=None if cd_root is None else _resolve(cd_root),
        cd_dirs=[_resolve(d) for d in cd_dirs],
        cd_upload_dir=_resolve(settings["cd_upload_dir"]),
        cd_max_upload=_size(settings["cd_max_upload"], "cd_max_upload"),
        program_dirs=[_resolve(d) for d in dirs],
        build_dir=_resolve(settings["build_dir"]),
        disk=_resolve(settings["disk"]),
        bios_source=_resolve(settings["bios_source"]),
        bios_binary=_resolve(settings["bios_binary"]),
        auto_build=bool(settings["auto_build"]),
        source_path=path if path.exists() else None,
        unknown_keys=unknown,
    )
