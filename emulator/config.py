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
from typing import List, Optional, Tuple

from .memory_map import (DISPLAY_H, DISPLAY_MAX_H, DISPLAY_MAX_W, DISPLAY_MODES, DISPLAY_W,
                         RAM_SIZE, VRAM_SIZE)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config.json"

# With video memory mapped above it, RAM's space is twice its size, and
# that has to fit in 32 bits (docs/gac/design.md §5.1).
MAX_RAM = 1 << 31

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
    # The second-stage BIOS (docs/os_cd.md), served read-only on channel 7.
    # Built like a C program, but for BIOS2_LOAD_ADDR, and rebuilt when it
    # or a library it includes changes. With neither a source nor a build
    # there is no second stage, and the BIOS boots channel 1 itself.
    "bios2_source": "firmware/bios2.c",
    "bios2_binary": "build/bios2.bin",

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
    # A disc to put in the drive before the machine starts, so bios2 finds
    # it at power-on (docs/os_cd.md): a path, or null for an empty drive.
    # --cd PATH does the same for one run.
    "cd": None,

    # --- memory (docs/gac/phase1_aperture.md) ---
    # "128M", "1G" or a byte count; a power of two from 128M to 2G. More
    # than 128M is allocated and addressable, but the guest's layout -- the
    # stack, bios2, the heap's end -- is still fixed at 128 MB, so nothing
    # uses the rest yet. --ram SIZE does the same for one run.
    "ram": "128M",
    # Video memory, mapped just above RAM. null or 0 for none: the machine
    # from before it existed. --vram SIZE does the same for one run.
    "vram": "16M",
    # The screen at power-on, [w, h], and the modes a program may switch to
    # (docs/gac/phase2_vram.md). Anything but 192 x 108 needs video memory,
    # and is shown out of it: programs that draw at DISPLAY_START draw where
    # nobody looks. --mode WxH picks the power-on mode for one run.
    "display_mode": [DISPLAY_W, DISPLAY_H],
    "display_modes": [list(m) for m in DISPLAY_MODES],

    # --- the debug port (docs/phase5_plan.md) ---
    # Print what the machine writes to its debug port in this terminal, each
    # line with the time since power-on. --serial does the same for one run.
    "serial": False,
    # A file to write those lines to as well, started fresh each run: a
    # path, or null for none. --serial-log PATH does the same for one run.
    "serial_log": None,
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
    cd: Optional[Path]
    program_dirs: List[Path]
    build_dir: Path
    disk: Path
    bios_source: Path
    bios_binary: Path
    bios2_source: Path
    bios2_binary: Path
    auto_build: bool
    serial: bool = False
    serial_log: Optional[Path] = None
    ram: int = RAM_SIZE
    vram: int = VRAM_SIZE                # 0: no video memory
    display_mode: Tuple[int, int] = (DISPLAY_W, DISPLAY_H)
    display_modes: List[Tuple[int, int]] = field(default_factory=lambda: list(DISPLAY_MODES))
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
        # A size from a flag is parsed and checked as the config key is,
        # so --ram and "ram" cannot disagree about what is allowed.
        if "ram" in given:
            given["ram"] = _ram_size(given["ram"], "--ram")
        if "vram" in given:
            given["vram"] = _vram_size(given["vram"], "--vram")
        if "display_mode" in given:
            given["display_mode"] = _mode(given["display_mode"], "--mode")
        _check_vram(given.get("vram", self.vram), given.get("ram", self.ram))
        _check_modes(given.get("display_mode", self.display_mode), self.display_modes,
                     given.get("vram", self.vram))
        for key in ("program_dirs", "build_dir", "disk", "bios_source", "bios_binary",
                    "bios2_source", "bios2_binary", "cd_dirs", "cd_upload_dir",
                    "cd_root", "cd", "serial_log"):
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


def _ram_size(value, name: str) -> int:
    size = _size(value, name)
    if size & (size - 1):
        raise ConfigError(f"{name} must be a power of two, got {value!r} "
                          f"(128M, 256M, 512M, 1G or 2G)")
    if size < RAM_SIZE:
        raise ConfigError(f"{name} must be at least 128M, got {value!r}: the stack "
                          f"and the second-stage BIOS live just below 128 MB")
    if size > MAX_RAM:
        raise ConfigError(f"{name} can be at most 2G, got {value!r}: the video "
                          f"memory above RAM has to fit in 32 bits")
    return size


def _vram_size(value, name: str) -> int:
    """A size, or null / 0 for no video memory at all."""
    if value is None or (isinstance(value, (int, str)) and not isinstance(value, bool)
                         and str(value).strip() == "0"):
        return 0
    return _size(value, name)


def _check_vram(vram: int, ram: int) -> None:
    if vram > ram:
        raise ConfigError(f"vram ({vram} bytes) cannot be larger than ram ({ram} "
                          f"bytes): it is mapped into the space just above RAM, "
                          f"which is as big as RAM")


def _mode(value, name: str) -> Tuple[int, int]:
    """[640, 360], or "640x360" from a flag."""
    if isinstance(value, str):
        w, _, h = value.lower().partition("x")
        value = [int(w), int(h)] if w.isdigit() and h.isdigit() else value
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0
                       for n in value)):
        raise ConfigError(f"{name} must be a width and a height, like [640, 360] "
                          f"or 640x360 -- got {value!r}")
    w, h = value
    if w > DISPLAY_MAX_W or h > DISPLAY_MAX_H:
        raise ConfigError(f"{name} {w}x{h} is larger than {DISPLAY_MAX_W}x"
                          f"{DISPLAY_MAX_H}, the largest screen programs are built for")
    return (w, h)


def _check_modes(mode: Tuple[int, int], modes: List[Tuple[int, int]], vram: int) -> None:
    if mode not in modes:
        raise ConfigError(f"the display mode {mode[0]}x{mode[1]} is not one of "
                          f"display_modes ({', '.join(f'{w}x{h}' for w, h in modes)})")
    if not vram and mode != (DISPLAY_W, DISPLAY_H):
        raise ConfigError(f"a {mode[0]}x{mode[1]} screen needs video memory; with "
                          f"vram off the screen is {DISPLAY_W}x{DISPLAY_H}")
    if vram and mode[0] * mode[1] * 4 > vram:
        raise ConfigError(f"a {mode[0]}x{mode[1]} screen does not fit in {vram} bytes "
                          f"of video memory")


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
    if not isinstance(settings["serial"], bool):
        raise ConfigError(f"serial must be true or false, got {settings['serial']!r}")
    serial_log = settings["serial_log"]
    if serial_log is not None and not isinstance(serial_log, str):
        raise ConfigError("serial_log must be the path of a file to write, or null for "
                          f"none -- got {serial_log!r}")
    disc = settings["cd"]
    if disc is not None and not isinstance(disc, str):
        raise ConfigError("cd must be the path of a disc image, or null for an empty "
                          f"drive -- got {disc!r}")

    ram = _ram_size(settings["ram"], "ram")
    vram = _vram_size(settings["vram"], "vram")
    _check_vram(vram, ram)
    modes = settings["display_modes"]
    if not isinstance(modes, list) or not modes:
        raise ConfigError(f"display_modes must be a list of [w, h] pairs, e.g. "
                          f"[[192, 108], [640, 360]] -- got {modes!r}")
    modes = [_mode(m, "display_modes") for m in modes]
    mode = _mode(settings["display_mode"], "display_mode")
    _check_modes(mode, modes, vram)

    return Config(
        host=str(settings["host"]),
        display_port=display_port,
        hid_port=hid_port,
        cd_port=cd_port,
        cd_root=None if cd_root is None else _resolve(cd_root),
        cd_dirs=[_resolve(d) for d in cd_dirs],
        cd_upload_dir=_resolve(settings["cd_upload_dir"]),
        cd_max_upload=_size(settings["cd_max_upload"], "cd_max_upload"),
        cd=None if disc is None else _resolve(disc),
        program_dirs=[_resolve(d) for d in dirs],
        build_dir=_resolve(settings["build_dir"]),
        disk=_resolve(settings["disk"]),
        bios_source=_resolve(settings["bios_source"]),
        bios_binary=_resolve(settings["bios_binary"]),
        bios2_source=_resolve(settings["bios2_source"]),
        bios2_binary=_resolve(settings["bios2_binary"]),
        auto_build=bool(settings["auto_build"]),
        serial=settings["serial"],
        serial_log=None if serial_log is None else _resolve(serial_log),
        ram=ram,
        vram=vram,
        display_mode=mode,
        display_modes=modes,
        source_path=path if path.exists() else None,
        unknown_keys=unknown,
    )
