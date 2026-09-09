# 4. Code generation

Codegen walks the typed AST and emits assembly text. No IR, no register
allocator — with five usable registers neither pays for itself.

## The model

**Every expression leaves its value in `A`.** A binary operation evaluates its
left side into `A`, parks it on the hardware stack, evaluates its right side
into `A`, moves that to `B`, pops the left back into `A`, and applies the
operator:

```asm
    <eval left>            ; -> A
    PUSH A
    <eval right>           ; -> A
    MOV  B, A
    POP  A
    ADD  A, A, B           ; the operator
```

Correct for arbitrary nesting, and the hardware stack depth is the expression
depth. The obvious peephole — when the right side is a constant or a simple
variable, skip the push/pop and load straight into `B` — removes most of the
traffic and is listed under [Peepholes](#peepholes).

## Loading and storing

| Source | Emitted |
|---|---|
| integer constant | `MOV A, #k` |
| global `int g` | `MRW A, #g` — loads take an immediate address |
| global `char g` | `MR A, #g` |
| local at frame offset `n` | `MOV C, F` / `ADD C, C, #n` / `MRW A, C` |
| local, `n == 0` | `MRW A, F` — the common case, one instruction |
| `*p` (int) | `MRW A, A` |
| `*p` (char) | `MR A, A` |

Stores always need the address in a register first ([01](01-overview.md) §3):

| Target | Emitted |
|---|---|
| global `int g` | `MOV C, #g` / `MWW C, A` |
| local at offset `n` | `MOV C, F` / `ADD C, C, #n` / `MWW C, A` |
| `*p = v` | address in `C`, value in `A`, `MWW C, A` |

## Operators

### Arithmetic and bitwise

Direct: `ADD` `SUB` `MUL` `DIV` `AND` `OR` `XOR` `SHL` `SHR`, all `A = A op B`.

Unary `-x` is `SUB A, <zero>, A` via a temporary, since `SUB` needs a register
first operand. `~x` is `NOT A, A`.

### Modulo — verified

No `MOD` instruction. `a % b` lowers to `a - (a / b) * b`:

```asm
    DIV  C, A, B
    MUL  C, C, B
    SUB  A, A, C
```

Checked against `17%5`, `100%7`, `9%3`, `0%4`, `255%16`.

### Comparison

`CMP` sets `zero_flag` and `less_flag`, and `less_flag` is **unsigned**.

*Unsigned* operands compare directly:

```asm
    CMP  A, B
    JL   .true             ; or JG / JZ / JNZ / JLE / JGE
```

*Signed* operands need both values biased by the sign bit first — verified
across all sign combinations including `-1 < 0` and `0 < -1`:

```asm
    XOR  A, A, #0x80000000
    XOR  B, B, #0x80000000
    CMP  A, B
    JL   .true
```

A comparison used for its *value* rather than a branch materialises 0 or 1:

```asm
    CMP  A, B
    MOV  A, #0
    JNZ  .done
    MOV  A, #1
.done:
```

### `&&` and `||`

Short-circuit, with a branch over the right operand:

```asm
    <eval left>            ; A
    CMP  A, #0
    JZ   .false            ; && : left is false, whole thing is false
    <eval right>
    CMP  A, #0
    JZ   .false
    MOV  A, #1
    JMP  .done
.false:
    MOV  A, #0
.done:
```

## Control flow

```c
if (c) X else Y                 while (c) X                  for (i; c; s) X
```

```asm
    <c>                          .top:                        <i>
    CMP A, #0                        <c>                      .top:
    JZ  .else                        CMP A, #0                    <c>
    <X>                              JZ  .end                     CMP A, #0
    JMP .end                         <X>                          JZ  .end
.else:                           .cont:                           <X>
    <Y>                              JMP .top                 .cont:
.end:                            .end:                            <s>
                                                                  JMP .top
                                                              .end:
```

`break` jumps to the enclosing `.end`, `continue` to `.cont`; codegen keeps a
stack of the two labels. Labels are `.L<n>` with a per-function counter, which
the assembler resolves in its first pass.

## Arrays and structs

No indexed addressing, so every subscript is explicit address arithmetic.
`a[i]` where `a` is `int*`:

```asm
    <eval a>               ; A = base
    PUSH A
    <eval i>               ; A = index
    SHL  A, A, #2          ; * sizeof(int) -- SHL, not MUL, for powers of two
    POP  B
    ADD  A, A, B           ; A = &a[i]
    MRW  A, A              ; load (omit when this is an assignment target)
```

`s.field` adds a constant offset; `p->field` is a load followed by the same:

```asm
    <eval p>               ; A = pointer
    ADD  A, A, #offset
    MRW  A, A
```

Member offsets come from the layout pass: declaration order, each member aligned
to its own size, `char` members packed.

## Function calls

Per [03-abi.md](03-abi.md), with `S` the calling function's frame size:

```asm
    ; arguments, one at a time -- each may itself contain a call, so the
    ; argument value is computed BEFORE C is loaded with the target slot
    <eval arg0>            ; -> A
    MOV  C, F
    ADD  C, C, #S
    MWW  C, A
    <eval arg1>
    MOV  C, F
    ADD  C, C, #(S+4)
    MWW  C, A
    ; transfer
    ADD  F, F, #S
    CALL fn
    SUB  F, F, #S          ; A now holds the return value
```

Reloading `C` for every argument rather than keeping a running pointer is
deliberate: an argument expression may contain a nested call, which clobbers
every register. Correctness first; the peephole below recovers the cost when the
argument is simple.

`return e` evaluates `e` into `A` and emits `RET`. A function that falls off the
end gets a `RET` with `A` undefined.

Indirect calls work — `CALL` accepts a register — so function pointers need no
special handling beyond evaluating the callee expression into a register that
is not `F`.

## Peepholes

Cheap, local, and worth having from day one:

1. **Constant right operand** — `x + 1` becomes `ADD A, A, #1`, skipping the
   push/pop entirely. This is most binary operations in practice.
2. **Simple right operand** — a local or global on the right loads straight into
   `B`; no stack traffic.
3. **`MOV C, F` / `ADD C, C, #0`** collapses to `MOV C, F`, and a load from
   frame offset 0 collapses to `MRW A, F`.
4. **Power-of-two `*` and `/`** become `SHL` / `SHR`.
5. **`MOV A, B` where `B` was just loaded from `A`** — drop it.
6. **Jump to the next instruction** — drop it.
7. **Constant folding** in the front end, so `DISPLAY_W * 4` never reaches
   codegen.

## Debug output

`-S` keeps the `.asm`, and the compiler interleaves the C source as comments:

```asm
    ; 14: for (unsigned i = 0; i < n; i++) {
.L3:
    MRW  A, F
    ...
```

Since the machine's only debugger is a single-stepper printing one disassembled
instruction at a time, being able to read the generated assembly next to its
source is the difference between a tractable and an intractable debugging
session. `tools/disasm.py` prints the same format from a `.bin`.
