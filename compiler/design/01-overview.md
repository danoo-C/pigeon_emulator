# 1. Overview

## Goal

Compile a useful subset of C to pigeon assembly, well enough to write the
display and input libraries in C instead of hand-written assembly. Correctness
and legibility of output over speed of output.

## Non-goals

Optimisation beyond constant folding. Floating point (there is no FP hardware
and no plan for soft-float). The full C preprocessor. `long long`, bitfields,
variadic functions, `setjmp`. A linker — everything is compiled in one unit.

## Pipeline

```
program.c
   │
   ├─ 1. preprocess   #include, #define (object-like), #ifdef      → one token stream
   ├─ 2. lex          tokens
   ├─ 3. parse        AST
   ├─ 4. analyse      symbol tables, type checking, layout, sizes
   ├─ 5. codegen      walk the AST, emit assembly text
   │
program.asm
   │
   └─ assembler/assembler.py                                       → build/program.bin
```

**Emit assembly text, not bytes.** The existing assembler already handles
encoding, two-pass label resolution, expression evaluation, and predefines every
constant from `memory_map.py` — and it is locked down by byte-exact golden
tests. Reusing it removes an entire class of bug from the compiler, and makes
the compiler's output readable, which matters enormously when debugging
generated code on a machine with no debugger beyond a single-stepper.

The compiler therefore never needs to know an opcode number, an instruction
width, or where the framebuffer lives.

The assembler gained what a code generator needs while this design was being
written: `.byte` / `.word` / `.ascii` / `.asciz` / `.space` / `.align` for static
data, dotted local labels (`.L1:`), and `.ORG` that accepts an expression — this
document originally said the compiler should emit `.ORG PROGRAM_LOAD_ADDR`, which
at the time silently set the origin to **0**. The BIOS also loads in chunks now,
so a program is bounded by `PROGRAM_MAX_SIZE` (1 MB) rather than by one 4 KB DMA
window. See [assembler/README.md](../../assembler/README.md).

## The five ISA facts that shape everything

Each was checked against the running machine.

### 1. Six registers, A–F

`REGISTER_COUNT = 6`. There is no register allocator worth writing for six
registers, one of which is reserved. Expressions are evaluated on an accumulator
with the hardware stack for temporaries — see [04-codegen.md](04-codegen.md).

### 2. The stack pointer cannot be read

`PUSH`, `POP`, `CALL` and `RET` move `cpu.sp`, and **nothing else can observe
it**. There is no `MOV A, SP`. A compiler therefore cannot form an SP-relative
address, which rules out the ordinary C stack frame.

Consequence: locals and arguments live on a **software frame stack** addressed
through a frame pointer held in `F`. The hardware stack is used only for return
addresses and for anonymous expression temporaries, which are pushed and popped
in strict LIFO order and never addressed. This is the single most important
decision in the design, and it is forced. See [03-abi.md](03-abi.md).

### 3. Loads take an immediate address; stores do not

```
MRW A, #0x1418      ; fine   -- one instruction to load a global
MWW #0x1418, A      ; rejected: "Expected register"
```

Every store costs an extra instruction to materialise the address:

```
MOV C, #0x1418
MWW C, A
```

So global *reads* are cheap and global *writes* are not. Worth knowing when
choosing how the libraries hold state.

### 4. Comparison is unsigned; there is no modulo

`CMP` sets `zero_flag` (equal) and `less_flag` (`a < b`, **unsigned** — it is a
borrow, not a sign bit).

Signed comparison lowers to a sign-bit flip and then an unsigned compare.
Verified against all sign combinations:

```
XOR A, A, #0x80000000
XOR B, B, #0x80000000
CMP A, B
JL  is_less              ; now a true signed <
```

Modulo lowers to `a - (a / b) * b`. Verified.

```
DIV C, A, B
MUL C, C, B
SUB A, A, C              ; A = A % B
```

`DIV` by zero raises a Python exception and kills the emulator rather than
setting a flag, so the compiler emits a guard in debug builds and documents the
hazard otherwise.

### 5. No indexed addressing, no `[reg + offset]`

Every address is computed into a register first. `a[i]` is three instructions
before the access, and `s->field` is two. This is the main reason generated code
is large; it is not worth fighting.

## Word size and layout

| C type | Size | Notes |
|---|---|---|
| `char` | 1 | `MR` / `MW` |
| `int`, `unsigned` | 4 | `MRW` / `MWW`, little-endian |
| any pointer | 4 | the address space is 128 MB, so 32 bits is exact |

Everything is 4-byte aligned except `char` arrays and struct members declared
`char`. The machine does not fault on unaligned word access — `read_word` masks
per byte — so alignment is a layout convention, not a hardware requirement.
