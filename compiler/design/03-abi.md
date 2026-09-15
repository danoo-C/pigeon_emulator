# 3. ABI

> **Verified.** The convention below was written as assembly, assembled, and run
> on the emulator — including recursion. `fact(6)` returns 720 with the frame
> pointer restored and the hardware stack balanced. The listing is at the end.

## Why this is unusual

The stack pointer cannot be read ([01-overview.md](01-overview.md) §2). Nothing
in the ISA moved `SP` into a register, so no SP-relative address could be
formed, so the ordinary C stack frame was impossible. `GETSP` came later, for
the kernel ([docs/kernel.md](../../docs/kernel.md) §13), and the frame stack
stays.

Two stacks, with different jobs:

| | Hardware stack (`SP`) | Software frame stack (`F`) |
|---|---|---|
| Moved by | `PUSH` `POP` `CALL` `RET` | `ADD F` / `SUB F` |
| Holds | return addresses, anonymous expression temporaries | named storage: parameters and locals |
| Addressable | **no** — strictly LIFO | yes, `[F + constant]` |
| Grows | down from `STACK_TOP` | up from `FRAME_BASE` |

Expression temporaries can live on the hardware stack precisely because they are
anonymous: pushed and popped in matching order, never referred to by address.

## Registers

| Reg | Role |
|---|---|
| `A` | accumulator — every expression result lands here; also the return value |
| `B` | right-hand operand of a binary operation |
| `C` | address scratch (stores need the address in a register) |
| `D` | scratch — library loops, helper sequences |
| `E` | scratch |
| `F` | **frame pointer — reserved, never allocated to an expression** |

Registers are caller-save in the sense that nothing survives a `CALL` except
`F` (which the caller restores itself) and `A` (which holds the return value).
Any value the caller still needs must be in a local or on the hardware stack.

## Frame layout

A frame is parameters followed by locals, all 4-byte slots, addressed as
positive offsets from `F`:

```
F + 0                     parameter 0        <- written by the CALLER
F + 4                     parameter 1
...
F + 4*(nparams-1)         parameter n-1
F + 4*nparams             local 0
F + ...                   local 1
...
F + frame_size            where this function's own callees' frames begin
```

`frame_size = 4 * nparams + sizeof(locals)`, rounded up to 4. It is a compile-
time constant for each function, which is the whole trick — see below.

There is **no saved frame pointer** and no return-address slot in the frame. The
return address is on the hardware stack (put there by `CALL`), and the frame
pointer needs no saving because the caller restores it with a constant it
already knows.

## Calling convention

The caller owns the transition. For a call from a function whose own frame size
is `S`:

```asm
    ; 1. write the arguments into where the callee's frame will be
    MOV  C, F
    ADD  C, C, #S                 ; C -> callee frame base
    MWW  C, <arg0>
    ADD  C, C, #4
    MWW  C, <arg1>
    ...
    ; 2. hand over the frame pointer, call, take it back
    ADD  F, F, #S
    CALL callee
    SUB  F, F, #S
    ; 3. return value is in A
```

The callee does nothing on entry — `F` already points at its frame, and its
parameters are already in it. On exit it puts the result in `A` and executes
`RET`.

- **Prologue:** none.
- **Epilogue:** `RET`. A `void` function still `RET`s; `A` is undefined.

Recursion works because each level advances `F` by its own constant and rewinds
it on return. Depth is bounded by the frame-stack region, not by anything the
convention does.

### Why the caller adjusts `F`, not the callee

The callee cannot compute its own frame base: that requires the *caller's* frame
size, which the callee does not know (and which differs per call site). The
caller does know it — it is a constant of the calling function. So the
adjustment has to happen on the caller's side.

## Variadic functions

Added for `printf`, in the kernel's phase 4
([docs/phase4_plan.md](../../docs/phase4_plan.md) step 1). A function declared
with `...` has eight more slots, straight after its named parameters:

```
F + 0 … F + 4*(n-1)       the n named parameters
F + 4*n … F + 4*(n+7)     eight slots for extra arguments
F + 4*(n+8)               local 0
```

- **The calling convention doesn't change.** The caller writes argument *i* at
  `F + S + 4i` whatever it calls, so an extra argument lands in the slot after
  the last named one.
- **`frame_size` counts all eight slots,** whether a call fills them or not,
  so the function's own calls never write over them, and `frame_size` stays a
  constant.
- **Every extra argument is one word:** an integer, a `char`, a pointer or a
  function. A struct is refused at the call, and so is a ninth extra
  argument.
- **`...` needs a named parameter before it,** since the extra slots are
  found by that parameter's address.
- **A call through a function pointer** to a variadic type works the same.

`<pigeon/stdarg.h>` is macros, with no `.c`:

```c
typedef unsigned *va_list;
#define va_start(ap, last) ((ap) = (unsigned *)&(last) + 1)
#define va_arg(ap, type)   ((type)*(ap)++)
```

Nothing tells the callee how many arguments came: as in C, a count or a
format string says. A slot the call didn't fill holds whatever was there.

## Memory layout

```
0x00020000  PROGRAM_LOAD_ADDR   code + string literals + initialised globals
                                (1 MB, PROGRAM_MAX_SIZE)
0x00120000  HEAP_START ─────────────────────────────────────────────
            __globals           .bss: zero-initialised globals
            __frame_base        FRAME STACK, grows UP        (default 256 KB)
            __frame_limit
            __heap_base         MALLOC HEAP, grows UP
                 ...            free space ...
0x07FFFFFC  STACK_TOP           hardware stack, grows DOWN
```

All four symbols are emitted by the compiler and are also usable from hand-
written assembly. The frame stack is a fixed 256 KB by default
(`-fframe-size=N` to change it), giving roughly 8,000 frames of eight slots.

`__heap_limit`, the word the compiler emits after `__heap_ptr`, is how far
the heap may grow. The kernel writes it for each program it runs
([docs/kernel.md](../../docs/kernel.md) Q7). Left at 0, as every image
starts, `mem.c` stops a megabyte below the top of RAM.

A **relocatable** program (`cc.py --relocatable`) has the same regions,
moved: its frame stack starts at `__image_end`, straight after its code and
data, and its heap follows the frame stack. The assembler settles
`__frame_base = __image_end` once labels have addresses.

**Frame-stack overflow is not detected by default.** Growing `F` past
`__frame_limit` walks into the malloc heap and corrupts it silently. `-fstack-
check` emits a compare against `__frame_limit` in every non-leaf prologue, at
three instructions per call. Recommended while developing.

## Startup

```asm
.ORG PROGRAM_LOAD_ADDR
__start:
    MOV  F, #__frame_base       ; establish the frame pointer, once
    MOV  C, #__heap_ptr         ; heap allocator init
    MOV  A, #__heap_base
    MWW  C, A
    CALL main
    HALT                        ; exit status left in A
```

A relocatable program is called like any function, as `entry(argc, argv)`,
so its startup takes its arguments from the caller's frame and returns:

```asm
__start:
    MRW  A, F                   ; argc, in the caller's frame
    ADD  C, F, #4
    MRW  B, C                   ; argv
    PUSH F                      ; the caller's frame pointer
    MOV  F, #__frame_base       ; its own frame stack: __image_end
    MWW  F, A                   ; main's argc
    ADD  C, F, #4
    MWW  C, B                   ; main's argv
    MOV  C, #__heap_ptr
    MOV  A, #__heap_base
    MWW  C, A
    CALL main
    POP  F                      ; the caller gets its F back
    RET                         ; main's value is in A
```

A `main(void)` never reads the two slots: they are where its locals start.

## The verified listing

This assembled and ran on the emulator; `fact(6)` produced 720, `F` came back to
`__frame_base`, and `SP` came back to `STACK_TOP`.

```asm
.ORG 0x20000
FRAME_BASE = HEAP_START

    MOV F #FRAME_BASE          ; establish the frame pointer once, at startup
    MOV C F
    MWW C #6                   ; arg0 = 6
    CALL FACT
    HALT                       ; result in A

; int fact(int n)    frame: [F+0] = n (param), frame_size = 8
FACT:
    MRW A F                    ; A = n
    CMP A #1
    JG RECURSE
    MOV A #1                   ; base case
    RET
RECURSE:
    PUSH A                     ; n -> hardware stack (anonymous temporary)
    SUB A A #1
    MOV C F
    ADD C C #8                 ; callee frame base = F + frame_size
    MWW C A                    ; arg0 = n - 1
    ADD F F #8
    CALL FACT
    SUB F F #8                 ; restore my frame pointer
    POP B                      ; B = n
    MUL A A B
    RET
```

Note `PUSH A` / `POP B` around the recursive call: `n` is live across the call
and no register survives one, so it goes to the hardware stack. A compiler would
instead keep `n` in its frame slot at `[F+0]`, which is already there — the
push/pop is shown to exercise the hardware stack in the same test.
