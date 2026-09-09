# The Pigeon Assembler

A two-pass assembler for the pigeon instruction set.

```bash
python3 assembler/assembler.py source.asm output.bin
```

Omit the output and it writes `source.bin` next to the input. It refuses to
write to anything ending in `.py`, `.asm`, `.c`, `.h` or `.md` — five 1 MB
files of pure null bytes were once found committed to this repo, each exactly
`PROGRAM_MAX_SIZE`, which is what output redirected onto its own input looks
like.

> **History:** this directory used to hold four assemblers (`ass.py`, `ass2.py`,
> `assembler.py`, `assemblercopy.py`, 62–83% identical) and four `.md` files
> documenting syntax that most of them rejected. Only `ass2.py` assembled every
> program in `user/`; `ass.py` silently emitted a *different* BIOS (544 bytes
> instead of 528) and `assembler.py` crashed on everything. This is `ass2.py`,
> fixed and renamed. The others are in git history.

---

## Syntax

```asm
.ORG 0x20000                    ; where this code will be loaded

PIXEL_SIZE = 4                  ; static definition
ROW_BYTES  = DISPLAY_W * PIXEL_SIZE   ; arithmetic, and built-in symbols

START:                          ; label -> resolves to an address
    MOV A #DISPLAY_START        ; '#' marks an immediate
    MOV B #0
LOOP:
    MWW A #0xFF0000FF           ; write a word to the address held in A
    ADD A A #PIXEL_SIZE         ; three operands...
    ADD B #1                    ; ...or two, duplicating the destination
    CMP B #100
    JL LOOP                     ; labels need no '#'
    HALT
```

- `;` starts a comment.
- `#` marks an immediate. Bare names resolve as labels, static definitions, or
  built-in symbols.
- Expressions support `+ - * / % << >> | & ^ ~` and parentheses. They are
  evaluated by walking the AST, not with `eval()` — the previous version ran
  `eval()` on text straight from the source file, where a stray `X = 9**9**9`
  would hang the assembler forever.
- Registers are `A`–`F`. Naming a register the CPU doesn't have is an assembly
  error, not a runtime crash. A *label* named `A`–`F` is also an error rather
  than being silently shadowed by the register.
- `.ORG` takes any constant expression (`.ORG PROGRAM_LOAD_ADDR`), and must come
  before any code or data — the image is one contiguous block and cannot jump.
- Labels may contain `.`, so a compiler can emit `.L1:`, `.L2:` locals.
- A duplicate label is an error; it used to be accepted with the last one
  winning.

### Two-operand shorthand

`ADD`, `SUB`, `MUL`, `DIV`, `OR`, `AND`, `XOR`, `SHL` and `SHR` accept a
two-operand form that duplicates the destination into the first source:

```asm
ADD C #1        ; identical encoding to  ADD C C #1
SUB D E         ; identical encoding to  SUB D D E
```

---

## Static data

Every emitted line is bytes in a flat image that gets DMA'd contiguously to
`PROGRAM_LOAD_ADDR`, so padding is real zero bytes, never a gap.

| Directive | Emits |
|---|---|
| `.byte 1, 2, 0xFF` | one byte per value (`0..255`, or `-128..-1`) |
| `.word 0x1234, LABEL` | four bytes per value, little-endian |
| `.ascii "hi"` | the characters, no terminator |
| `.asciz "hi"` | the characters plus a `\0` (also spelled `.string`) |
| `.space 64` | that many zero bytes |
| `.align 8` | zeros up to the next 8-byte boundary |

```asm
MSG:    .asciz "pigeon"          ; MSG points at the 'p'
TABLE:  .word 0x11111111, 0x22222222
FLAGS:  .byte 1, 0, 1
BUF:    .space 256               ; 256 bytes of scratch
        .align 8                 ; instructions must be 8-byte aligned
CODE:   MOV A, #1
```

Strings take `\n \t \r \0 \\ \" \a \b \f \v \e`, `\xNN` (exactly two hex
digits) and 1–3 digit octal. An unknown escape is an error — silently dropping
the backslash is how a table ends up one byte short. A `;` inside a string is
part of the string, not a comment.

**Instructions must land on an 8-byte boundary.** Data of any other length
leaves the address odd, and the assembler says so rather than emitting code the
CPU cannot execute:

```
Error: instruction lands at 0x3, which is not a multiple of 8 -- data earlier
in the file left the address odd. Put '.align 8' before this code.
```

A label before `.align` points at the padding; a label after it points at the
aligned address. Put the label where you want it to resolve.

> **Program size.** The BIOS loads in 4 KB chunks and will read a program of any
> size up to `PROGRAM_MAX_SIZE` (1 MB). A large `.space` costs real bytes in the
> image, so a big BSS block is better zeroed at runtime than reserved here.

---

## Built-in symbols

Every uppercase integer in `emulator/memory_map.py` is predefined, so sources
never retype an address:

| | |
|---|---|
| Layout | `RAM_SIZE` `BIOS_START` `BIOS_MAX` `IO_START` `IO_SIZE` `DISPLAY_START` `DISPLAY_SIZE` `DISPLAY_W` `DISPLAY_H` `PROGRAM_LOAD_ADDR` `PROGRAM_MAX_SIZE` `HEAP_START` `STACK_TOP` `REGISTER_COUNT` |
| IO header offsets | `IO_CHANNEL` `IO_R_W` `IO_COMMAND` `IO_LENGTH` `IO_ADDRESS` `IO_RETURN_DATA` `IO_USABLE_AFTER` |
| IO channels | `CH_USERPROG` `CH_HDD` `CH_HID` `CH_TIMER` |

A source may still define its own value for one of these, but redefining it to
a **different** value prints a warning:

```
warning: DISPLAY_START is redefined as 0x1218, but the machine's memory map
         says 0x1418 -- this source has drifted
```

That check is not hypothetical. `user/ui.asm` hardcoded `DISPLAY_START = 0x1218`
and `IO_POINTER = 0x200` — a layout two generations stale — so every pixel it
drew landed inside the IO region instead of the framebuffer, and it displayed
nothing at all. `tests/test_golden.py` fails if any source in `firmware/` or
`user/` drifts again.

---

## How it works

**Parse once.** Each line becomes one `Item` — a label, an instruction, or a
directive — that knows both its size and its bytes.

**Pass 1, `_layout`.** Walk the items assigning addresses, asking each its size.

**Pass 2, `_emit`.** Walk them again collecting bytes, and **check that every
item emitted exactly the number of bytes pass 1 sized it**.

That check is the point of the whole structure. The two passes used to read the
source text independently and each assume "8 bytes per line"; once variable-width
data exists, any disagreement between them silently relocates every later label,
and the program fails somewhere unrelated. Parsing once makes drift impossible,
and the assertion catches it anyway if a future directive gets it wrong.

Instructions look their mnemonic up in `SYNTAX` — a table naming the ordered
operand slots each expects — then emit through `encode()` from
`emulator/instruction_set.py`.

`SYNTAX` is explicit because operand *order* and *role* can't be recovered from
the instruction table alone: `MW`'s first operand is a register, but it holds an
address, not a destination. A mnemonic in the instruction set with no `SYNTAX`
row raises at import, so adding an instruction can't half-land.

---

## Adding an instruction

1. Add the handler in `emulator/instruction_set.py`, **at the end**. Opcodes are
   assigned in declaration order, so inserting one renumbers every opcode after
   it and invalidates every previously assembled binary.
2. Add its `SYNTAX` row here.
3. Run `python3 tests/test_golden.py` — the golden binaries prove existing
   opcodes didn't move. `tests/golden/bios_v1.asm` is a frozen source kept
   purely to pin the assembler; `firmware/bios.asm` is free to change.
