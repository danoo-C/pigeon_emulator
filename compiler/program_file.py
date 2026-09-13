"""Program files: C programs the kernel can load at any address.

docs/kernel.md §8 and docs/kernel_exec.md §5-7. The compiler emits a
relocatable program -- startup code that returns to its caller, and a frame
stack and heap after the image -- and this module assembles it twice, at
LINK_ADDR and at LINK_ALT, and compares the images. A word that differs
holds an address, and nothing else may differ. The file is

    header    eight words
    image     the program, as built for LINK_ADDR
    patches   one word for each address in the image: its offset

and the kernel adds (where it put the image - the link address) to the word
at each offset. The header:

    0   PROGRAM_FILE_MAGIC     "PGEX"
    4   PROGRAM_FILE_VERSION
    8   image size             bytes, a whole number of words
    12  entry                  offset of __start: call it as entry(argc, argv)
    16  patch count
    20  frame stack size       bytes, straight after the image
    24  heap offset            where __heap_ptr is; __heap_limit is the next word
    28  link address           where the image was built for

    python3 compiler/cc.py program.c --relocatable -o build/program.bin
"""
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from emulator.memory_map import (PROGRAM_FILE_HEADER, PROGRAM_FILE_MAGIC,
                                 PROGRAM_FILE_VERSION)

# Built for where the kernel starts the shell, so that load patches nothing.
LINK_ADDR = 0x01000000
# The second build is this far above. The distance has bits set in every
# byte, so a piece of an address -- `.byte label & 0xFF` -- differs between
# the builds too, and is refused, instead of matching and being missed.
LINK_ALT = LINK_ADDR + 0x01021238

_HEADER = struct.Struct("<8I")
assert _HEADER.size == PROGRAM_FILE_HEADER


class ProgramFileError(ValueError):
    """Not a program file, or assembly that cannot become one."""


@dataclass(frozen=True)
class Header:
    image_size: int
    entry: int
    patch_count: int
    frame_size: int
    heap_offset: int
    link: int = LINK_ADDR

    def pack(self) -> bytes:
        return _HEADER.pack(PROGRAM_FILE_MAGIC, PROGRAM_FILE_VERSION, self.image_size,
                            self.entry, self.patch_count, self.frame_size,
                            self.heap_offset, self.link)

    @classmethod
    def read(cls, blob: bytes) -> "Header":
        """The header of `blob`, checked the way a loader has to check it."""
        if len(blob) < PROGRAM_FILE_HEADER:
            raise ProgramFileError(f"{len(blob)} bytes is too short to be a program file")
        magic, version, size, entry, count, frame, heap, link = _HEADER.unpack_from(blob)
        if magic != PROGRAM_FILE_MAGIC:
            raise ProgramFileError(f"not a program file: it starts {magic:#010x}")
        if version != PROGRAM_FILE_VERSION:
            raise ProgramFileError(f"a version {version} program file; this is version "
                                   f"{PROGRAM_FILE_VERSION}")
        if len(blob) != PROGRAM_FILE_HEADER + size + 4 * count:
            raise ProgramFileError(f"the header describes {PROGRAM_FILE_HEADER + size + 4 * count} "
                                   f"bytes, and the file is {len(blob)}")
        if size % 4 or entry % 8 or entry >= size or heap % 4 or heap + 8 > size:
            raise ProgramFileError("the header's entry or heap offset is not inside the image")
        return cls(size, entry, count, frame, heap, link)


def addresses(image: bytes, moved: bytes, distance: int) -> List[int]:
    """Offsets of the words in `image` that hold an address, found by
    comparing it with `moved`: the same program, built `distance` higher.

    Every word that differs must differ by exactly `distance`. Anything else
    was computed from an address -- shifted, masked, cut into bytes, or not
    lined up on a word -- and adding the distance to it would be wrong, so
    the program is refused rather than half relocated."""
    if len(image) != len(moved):
        raise ProgramFileError(f"the two builds are {len(image)} and {len(moved)} bytes long; "
                               f"the program's size depends on its address")
    if len(image) % 4:
        raise ProgramFileError(f"the image is {len(image)} bytes, not a whole number of words")
    found = []
    for offset in range(0, len(image), 4):
        word, = struct.unpack_from("<I", image, offset)
        other, = struct.unpack_from("<I", moved, offset)
        if word == other:
            continue
        if (other - word) & 0xFFFFFFFF != distance:
            raise ProgramFileError(
                f"the word at offset {offset:#x} is {word:#x} in one build and {other:#x} "
                f"in the other: computed from an address, so it cannot be patched")
        found.append(offset)
    return found


def build(asm_path) -> bytes:
    """Relocatable assembly -> a program file.

    `asm_path` holds what the compiler emits with relocatable=True. Its
    frame stack must start at __image_end: a program with its frame stack at
    HEAP_START would relocate too, and then share it with whoever ran it."""
    from assembler.assembler import Assembler

    first = Assembler(str(asm_path), origin=LINK_ADDR)
    image = first.assemble()
    moved = Assembler(str(asm_path), origin=LINK_ALT).assemble()

    symbols = {**first.static_defs, **first.symbols}
    missing = [name for name in ("__start", "__heap_ptr", "__heap_limit",
                                 "__frame_base", "__frame_limit") if name not in symbols]
    if missing:
        raise ProgramFileError(f"{asm_path}: no {', '.join(missing)}; not compiled C")
    if symbols["__frame_base"] != symbols.get("__image_end"):
        raise ProgramFileError(f"{asm_path}: its frame stack is at {symbols['__frame_base']:#x}, "
                               f"not after its image; compile it relocatable")
    if symbols["__image_end"] != LINK_ADDR + len(image):
        raise ProgramFileError(f"{asm_path}: __image_end is not at the end of the image")
    if symbols["__heap_limit"] != symbols["__heap_ptr"] + 4:
        raise ProgramFileError(f"{asm_path}: __heap_limit is not the word after __heap_ptr")

    patches = addresses(image, moved, LINK_ALT - LINK_ADDR)
    header = Header(image_size=len(image),
                    entry=symbols["__start"] - LINK_ADDR,
                    patch_count=len(patches),
                    frame_size=symbols["__frame_limit"] - symbols["__frame_base"],
                    heap_offset=symbols["__heap_ptr"] - LINK_ADDR)
    return header.pack() + image + struct.pack(f"<{len(patches)}I", *patches)


def relocate(blob: bytes, base: int) -> Tuple[bytes, Header]:
    """The image in program file `blob`, patched to run at `base` -- what the
    kernel will do after loading one (docs/kernel.md §11), done on the host."""
    header = Header.read(blob)
    if base % 8:
        raise ProgramFileError(f"{base:#x} is not a multiple of 8, where an instruction can start")
    start = PROGRAM_FILE_HEADER
    image = bytearray(blob[start:start + header.image_size])
    distance = base - header.link
    table = start + header.image_size
    for index in range(header.patch_count):
        offset, = struct.unpack_from("<I", blob, table + 4 * index)
        if offset % 4 or offset + 4 > header.image_size:
            raise ProgramFileError(f"patch {index} is at {offset:#x}, not a word in the image")
        word, = struct.unpack_from("<I", image, offset)
        struct.pack_into("<I", image, offset, (word + distance) & 0xFFFFFFFF)
    return bytes(image), header


def is_program_file(path) -> bool:
    """Whether the file at `path` starts with a program file's magic."""
    with open(Path(path), "rb") as f:
        head = f.read(4)
    return len(head) == 4 and struct.unpack("<I", head)[0] == PROGRAM_FILE_MAGIC
