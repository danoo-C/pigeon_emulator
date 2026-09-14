#!/usr/bin/env python3
"""pigeon-cc -- a small C compiler for the pigeon machine.

    python3 compiler/cc.py program.c -o build/program.bin
    python3 compiler/cc.py program.c -S            # keep the assembly
    python3 compiler/cc.py firmware/bios2.c --org BIOS2_LOAD_ADDR -o build/bios2.bin
    python3 compiler/cc.py program.c --relocatable   # a program file the kernel loads anywhere
    python3 compiler/cc.py --project user/os/pigeon_compiler_init.txt   # an install disc

Emits assembly text and hands it to assembler/assembler.py, which already
owns encoding, label resolution and the memory map -- and is pinned by
byte-exact golden tests.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compiler import program_file                     # noqa: E402
from compiler.analyzer import analyze                 # noqa: E402
from compiler.codegen import generate                 # noqa: E402
from compiler.lexer import CompileError, tokenize     # noqa: E402
from compiler.parser import parse                     # noqa: E402
from compiler.preprocess import Preprocessor          # noqa: E402
from emulator import memory_map                       # noqa: E402

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"

# The memory map, predefined for every translation unit -- the same names
# the assembler injects as symbols, from the same rule. Macro bodies are
# text, so the ints are formatted here.
#
# Without this, C had no way to learn the machine's layout and
# lib/pigeon/display.h carried a hand-written copy of the display
# geometry. Nothing checked the two against each other, so changing the
# resolution in memory_map.py left every C program drawing at the old
# one -- the exact failure the assembler's symbol injection exists to
# prevent.
BUILTIN_DEFINES = {name: str(value) for name, value in memory_map.symbols().items()}

# Where a program is built to run unless --org says otherwise. A name
# rather than a number, so the assembly says what it means.
DEFAULT_ORIGIN = "PROGRAM_LOAD_ADDR"


def origin_of(text: str) -> str:
    """What --org puts after .ORG: a memory-map name, kept as the name, or a
    number. Either must be a multiple of 8 inside RAM, an address an
    instruction can start at.

    Only the code and data move. The frame stack and heap stay at
    HEAP_START, so a program built for anywhere else must not reach into
    them -- bios2 (docs/os_cd.md) is built for BIOS2_LOAD_ADDR, far above.
    """
    text = text.strip()
    symbols = memory_map.symbols()
    if text in symbols:
        value, emitted = symbols[text], text
    else:
        try:
            value = int(text, 0)
        except ValueError:
            raise ValueError(f"--org wants a number or a memory-map name such as "
                             f"BIOS2_LOAD_ADDR, not {text!r}") from None
        emitted = f"{value:#x}"
    if not 0 <= value < memory_map.RAM_SIZE or value % 8:
        raise ValueError(f"--org {text}: not a multiple of 8 inside RAM")
    return emitted


def _remap(error: CompileError, origins) -> CompileError:
    """Rewrite a diagnostic's position back to the file the user wrote.

    #include expands inline, so a line number in the preprocessed stream
    is meaningless to the reader -- an error in demo.c would name a line
    number that only exists after three headers were pasted in front.
    """
    index = error.line - 1
    if 0 <= index < len(origins):
        filename, line = origins[index]
        return CompileError(error.message, line, error.column, filename)
    return error


def _origin_for(origin: str, relocatable: bool) -> str:
    """A relocatable program has no origin of its own. It is emitted for
    program_file.LINK_ADDR, and program_file.build() builds it from there."""
    if not relocatable:
        return origin
    if origin != DEFAULT_ORIGIN:
        raise ValueError("a relocatable program runs wherever the kernel loads it, "
                         "so it takes no origin")
    return f"{program_file.LINK_ADDR:#x}"


def compile_to_asm(source: str, filename: str = "<source>", include_paths=None,
                   origin: str = DEFAULT_ORIGIN, relocatable: bool = False) -> str:
    """C text -> pigeon assembly text."""
    origin = _origin_for(origin, relocatable)
    pre = Preprocessor(include_paths or [LIB_DIR], BUILTIN_DEFINES)
    text, origins = pre.process(source, filename, Path(filename).parent)
    try:
        program = analyze(parse(tokenize(text, filename)))
    except CompileError as e:
        raise _remap(e, origins) from None
    return generate(program, text.splitlines(), origin, relocatable, _assembly(pre))


def _assembly(pre: Preprocessor):
    """What the #asm lines named, as (path, text), in the order named."""
    return [(str(path), path.read_text()) for path in pre.assembly]


def compile_units(paths, include_paths=None, origin: str = DEFAULT_ORIGIN,
                  relocatable: bool = False) -> str:
    """Compile several .c files as ONE translation unit.

    There is no linker: the assembler emits one flat image. Compiling
    everything together is simpler than inventing an object format, and
    for programs this size costs nothing.

    `origin` is what origin_of() returns: where the image is built to run.
    `relocatable` emits a program for compiler/program_file.py instead.
    """
    origin = _origin_for(origin, relocatable)
    include_paths = list(include_paths or []) + [LIB_DIR]
    pre = Preprocessor(include_paths, BUILTIN_DEFINES)
    chunks, origins = [], []
    for path in paths:
        path = Path(path)
        text, source_map = pre.process(path.read_text(), str(path), path.parent)
        chunks.append(text)
        origins.extend(source_map)
    combined = "\n".join(chunks)
    try:
        program = analyze(parse(tokenize(combined, str(paths[0]))))
    except CompileError as e:
        raise _remap(e, origins) from None
    return generate(program, combined.splitlines(), origin, relocatable, _assembly(pre))


def compile_file(path, asm_out=None, bin_out=None, keep_asm=False, extra=(),
                 origin=DEFAULT_ORIGIN):
    path = Path(path)
    asm_text = compile_units([path, *extra], origin=origin)

    asm_path = Path(asm_out) if asm_out else Path(bin_out or path).with_suffix(".asm")
    asm_path.parent.mkdir(parents=True, exist_ok=True)
    asm_path.write_text(asm_text)

    if bin_out is None:
        return asm_path, None

    from assembler.assembler import assemble_file
    binary = assemble_file(asm_path, bin_out, quiet=True)
    if not keep_asm:
        asm_path.unlink()
    return asm_path, binary


def _build_project(args) -> int:
    """--project: an installation disc from a project file (compiler/project.py)."""
    from compiler.project import ProjectError, build_disc, read_project
    from emulator.config import ConfigError, load_config

    try:
        build_dir = load_config().build_dir
    except ConfigError:
        build_dir = Path(__file__).resolve().parent.parent / "build"
    try:
        project = read_project(args.project)
        build_disc(project, args.output or build_dir / f"{project.slug}.img", build_dir)
        return 0
    except ProjectError as e:
        for line in e.lines():
            print(f"error: {line}", file=sys.stderr)
    except (CompileError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pigeon-cc", description=__doc__.split("\n")[0])
    parser.add_argument("sources", type=Path, nargs="*",
                        help="one or more .c files, compiled as a single unit")
    parser.add_argument("-o", "--output", type=Path,
                        help="the .bin to write; with --project, the disc image")
    parser.add_argument("-S", "--assembly", action="store_true",
                        help="stop after generating assembly, and keep it")
    parser.add_argument("-I", "--include", type=Path, action="append", default=[],
                        metavar="DIR", help="add an include search path")
    parser.add_argument("--org", metavar="ADDR", default=DEFAULT_ORIGIN,
                        help="where the program is built to run: a number or a "
                             "memory-map name (default PROGRAM_LOAD_ADDR). The "
                             "frame stack and heap stay at HEAP_START")
    parser.add_argument("--project", type=Path, metavar="FILE",
                        help="build an installation disc from a project file "
                             "(docs/os_cd.md): to -o, or build/<name>.img")
    parser.add_argument("--relocatable", action="store_true",
                        help="build a program file the kernel can load at any address "
                             "(docs/kernel.md §8), with its frame stack and heap after it")
    args = parser.parse_args(argv)
    if args.project is not None:
        if args.sources or args.assembly or args.org != DEFAULT_ORIGIN or args.relocatable:
            parser.error("--project takes no sources, -S, --org or --relocatable")
        return _build_project(args)
    if not args.sources:
        parser.error("name one or more .c files, or a project with --project FILE")
    if args.relocatable and (args.assembly or args.org != DEFAULT_ORIGIN):
        parser.error("--relocatable takes no -S or --org: a program file runs wherever "
                     "the kernel loads it")
    args.source = args.sources[0]
    try:
        origin = origin_of(args.org)
    except ValueError as e:
        parser.error(str(e))

    try:
        asm_text = compile_units(args.sources, args.include, origin, args.relocatable)

        if args.assembly:
            asm_path = args.output or args.source.with_suffix(".asm")
            asm_path.parent.mkdir(parents=True, exist_ok=True)
            asm_path.write_text(asm_text)
            print(f"Compiled {' '.join(str(s) for s in args.sources)} -> {asm_path}")
            return 0

        output = args.output or Path("build") / (args.source.stem + ".bin")
        asm_path = Path(output).with_suffix(".asm")
        asm_path.parent.mkdir(parents=True, exist_ok=True)
        asm_path.write_text(asm_text)

        if args.relocatable:
            blob = program_file.build(asm_path)
            Path(output).write_bytes(blob)
            header = program_file.Header.read(blob)
            print(f"Compiled {' '.join(str(s) for s in args.sources)} -> {output} "
                  f"({len(blob)} bytes: a program file, a {header.image_size}-byte image "
                  f"with {header.patch_count} addresses to patch)")
            return 0

        from assembler.assembler import assemble_file
        binary = assemble_file(asm_path, output, quiet=True)
        print(f"Compiled {' '.join(str(s) for s in args.sources)} -> {output} "
              f"({len(binary)} bytes)")
        return 0
    except (CompileError, program_file.ProgramFileError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
