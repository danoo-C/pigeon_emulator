#!/usr/bin/env python3
"""pigeon-cc -- a small C compiler for the pigeon machine.

    python3 compiler/cc.py program.c -o build/program.bin
    python3 compiler/cc.py program.c -S            # keep the assembly

Emits assembly text and hands it to assembler/assembler.py, which already
owns encoding, label resolution and the memory map -- and is pinned by
byte-exact golden tests.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def compile_to_asm(source: str, filename: str = "<source>", include_paths=None) -> str:
    """C text -> pigeon assembly text."""
    text, origins = Preprocessor(include_paths or [LIB_DIR], BUILTIN_DEFINES).process(
        source, filename, Path(filename).parent)
    try:
        program = analyze(parse(tokenize(text, filename)))
    except CompileError as e:
        raise _remap(e, origins) from None
    return generate(program, text.splitlines())


def compile_units(paths, include_paths=None) -> str:
    """Compile several .c files as ONE translation unit.

    There is no linker: the machine has no relocation and the assembler
    emits one flat image. Compiling everything together is simpler than
    inventing an object format, and for programs this size costs nothing.
    """
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
    return generate(program, combined.splitlines())


def compile_file(path, asm_out=None, bin_out=None, keep_asm=False, extra=()):
    path = Path(path)
    asm_text = compile_units([path, *extra])

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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pigeon-cc", description=__doc__.split("\n")[0])
    parser.add_argument("sources", type=Path, nargs="+",
                        help="one or more .c files, compiled as a single unit")
    parser.add_argument("-o", "--output", type=Path, help="the .bin to write")
    parser.add_argument("-S", "--assembly", action="store_true",
                        help="stop after generating assembly, and keep it")
    parser.add_argument("-I", "--include", type=Path, action="append", default=[],
                        metavar="DIR", help="add an include search path")
    args = parser.parse_args(argv)
    args.source = args.sources[0]

    try:
        asm_text = compile_units(args.sources, args.include)

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

        from assembler.assembler import assemble_file
        binary = assemble_file(asm_path, output, quiet=True)
        print(f"Compiled {' '.join(str(s) for s in args.sources)} -> {output} "
              f"({len(binary)} bytes)")
        return 0
    except CompileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
