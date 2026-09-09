# pigeon-cc

A small C compiler for the pigeon machine.

Usually you do not invoke it directly — put a `.c` file in `user/` and the
launcher compiles it for you, working out its libraries from the
`#include <pigeon/…>` lines:

```bash
python3 start_emulator.py demo --run
```

By hand, when you want the assembly or a specific output path (there is no
linker, so name every unit):

```bash
python3 compiler/cc.py user/demo.c lib/pigeon/display.c lib/pigeon/input.c \
        lib/pigeon/mem.c -o build/demo.bin
python3 compiler/cc.py program.c -S -o program.asm    # stop at assembly
```

It emits **assembly text** and hands it to `assembler/assembler.py`, so it
never touches opcodes, encoding, or the memory map — and the output stays
readable, with the C source interleaved as comments:

```asm
    ; 4: return a * b;
    MRW A, F
    PUSH A
    MOV C, F
    ADD C, C, #4
    MRW A, C
    MOV B, A
    POP A
    MUL A, A, B
    RET
```

That matters more here than on most targets: the only debugger is a
single-stepper printing one instruction at a time.

## Status

**Working** — verified by compiling and *running* 63 programs on the emulator
(`tests/test_compiler.py`):

int/unsigned/char, globals, locals, all arithmetic and bitwise operators,
`%` (lowered to `a - (a/b)*b`, there is no MOD), comparisons including correct
**signed** comparison, `&&`/`||` with short-circuit, `?:`, `sizeof`, casts,
`if`/`else`, `while`, `do`/`while`, `for`, `break`, `continue`, functions,
arguments, **recursion**, mutual recursion, nested calls, `++`/`--` in both
positions, and every compound assignment.

**Not yet wired up** — parsed and laid out, but without execution tests:
pointers, arrays, structs, `->`, string literals, function pointers. These are
the next stage.

**Not supported by design**: floating point (no FP hardware), `goto`, `switch`,
varargs, `long long`. Each is rejected by name with a reason.

## The one thing worth knowing

The stack pointer **cannot be read** on this machine — no instruction moves SP
into a register. So locals cannot live at SP-relative addresses, and there is no
conventional C stack frame.

Instead there are two stacks:

| | Hardware stack (SP) | Software frame stack (F) |
|---|---|---|
| Holds | return addresses, anonymous expression temporaries | parameters and locals |
| Addressable | no — strictly LIFO | yes, `[F + constant]` |

A caller writes the callee's arguments at `[F + its own frame_size]`, adds that
constant to `F`, calls, and subtracts it again on return. The callee does
nothing on entry: `F` already points at its frame. This works because
`frame_size` is a compile-time constant *of the caller* — which is exactly why
the caller has to do the adjusting, since the callee cannot know it.

Full reasoning in [design/03-abi.md](design/03-abi.md).

## Layout

| File | Job |
|---|---|
| `lexer.py` | tokens, with `file:line:col` on every one |
| `parser.py` | recursive descent → AST |
| `ast_nodes.py` | node definitions |
| `typesys.py` | types, sizes, struct layout (named this, not `types.py`, because that shadows the stdlib module) |
| `analyzer.py` | name resolution, type checking, frame-slot assignment |
| `codegen.py` | AST → assembly |
| `cc.py` | the driver |
