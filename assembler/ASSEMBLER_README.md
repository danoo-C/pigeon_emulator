# Pigeon Emulator Assembler

A two-stage assembler for the Pigeon 32-bit emulator ISA, written in Python.

## Features

### Stage 1: Preprocessing
- **Comment removal**: Lines starting with `;` or comments after code
- **Static definitions**: Evaluate arithmetic expressions in constants
  - `SYMBOL = value` or `SYMBOL = expr + expr`
  - Supports `+`, `-`, `*`, `/` operators
  - Forward references to other symbols are resolved recursively
- **Origin directive**: `.ORG address` sets the code start address
- **Label collection**: First pass collects all label offsets for later resolution

### Stage 2: Assembly
- **Operand parsing**: Handles both simple and complex forms
  - Register operands: `A`, `B`, `C`, etc. (maps to 0-25)
  - Immediate values: `#value` or `#SYMBOL` or `#expr + expr`
  - Memory references: `[address]` or `[register]`
- **Shorthand syntax**: Auto-expands common patterns
  - `ADD C #1` → `ADD C C #1` (increment)
  - `SUB C #1` → `SUB C C #1` (decrement)
- **Smart instruction encoding**:
  - Uses `instruction_set.py` helpers for correct 8-byte encoding
  - Automatically generates helper MOV instructions when needed
- **Binary output**: Produces raw 32-bit little-endian machine code

## Instruction Format

Each instruction is 8 bytes:
```
[opcode:1][dst:1][src1:1][src2:1][imm:4 LE]
```

- **opcode**: Instruction type (NOP=0, MOV=1, ADD=2, etc.)
- **dst**: Destination register (0-25) or `0xFF` (unused)
- **src1**: Source register or `0xFF` (unused)
- **src2**: Source register/immediate (0-254) or `0xFF` (unused)
- **imm**: 32-bit immediate value (little-endian)

## Supported Instructions

### Arithmetic
- `ADD dst, src1, src2` — dst = src1 + src2
- `SUB dst, src1, src2` — dst = src1 - src2
- `MUL dst, src1, src2` — dst = src1 * src2
- `DIV dst, src1, src2` — dst = src1 / src2

### Memory
- `MR dst, [addr]` — Read 1 byte from address
- `MW [addr], src` — Write 1 byte to address
- `MRW dst, [addr]` — Read 4 bytes (word) from address
- `MWW [addr], src` — Write 4 bytes (word) to address

### Movement
- `MOV dst, src` — Copy value into register
- `PUSH src` — Push register onto stack
- `POP dst` — Pop from stack into register

### Control Flow
- `JMP addr` — Unconditional jump
- `JZ addr` — Jump if zero flag set (after CMP)
- `JNZ addr` — Jump if zero flag clear
- `JL addr` — Jump if less than flag set
- `JG addr` — Jump if greater than (neither flag set)
- `JLE addr` — Jump if less or equal
- `JGE addr` — Jump if greater or equal
- `CMP src1, src2` — Compare and set flags
- `HALT` — Stop execution
- `NOP` — No operation

## Usage

### Command Line
```bash
python assembler.py input.asm [output.bin]
```

If no output file is specified, defaults to `input.bin`.

### Python API
```python
from assembler import Assembler

asm = Assembler("program.asm")
binary = asm.assemble()

with open("program.bin", "wb") as f:
    f.write(binary)
```

## Assembly Syntax

### Basic Structure
```asm
; Comments start with semicolon
.ORG 0x0                    ; Set origin address

; Static definitions (evaluated at assembly time)
BUFFER_SIZE = 256
IO_BASE = 0x1000
CONFIG = IO_BASE + 0x10

; Labels mark code locations
LOOP:
    ADD A A #1              ; Increment A
    CMP A #BUFFER_SIZE
    JNZ LOOP                ; Jump back to LOOP if not equal

    JMP #BUFFER_SIZE        ; Jump to immediate address
```

### Operand Forms

**Registers (names A-E):**
```asm
MOV A B              ; Copy B into A
ADD C A B            ; C = A + B
```

**Immediates (with # prefix):**
```asm
MOV A #10            ; A = 10
MWW #0x100 #5        ; Write 5 to address 0x100
```

**Symbol references:**
```asm
PROG_SIZE = 4096
MOV A #PROG_SIZE     ; A = 4096
JMP #START           ; Jump to START label
```

**Arithmetic expressions:**
```asm
IO_BASE = 0x1000
IO_OFFSET = 0x10
CONFIG_ADDR = #IO_BASE + #IO_OFFSET  ; Address arithmetic
MWW #CONFIG_ADDR #1  ; Write to computed address
```

**Memory references:**
```asm
MR A [B]             ; Read byte at address in B
MWW [0x100] A        ; Write A to address 0x100
MOV C #0x200
MR D [C]             ; Read byte at address in C
```

## Limitations & Notes

### Immediate Value Encoding
- Most instructions support 32-bit immediates via the `imm` field
- `src2` field holds 0-254 for small immediates (e.g., `src2` byte value)
- If you need two large 32-bit immediates in one instruction (rare), the assembler will auto-generate a helper MOV instruction
  - Example: `MWW #LARGE_ADDR #LARGE_VALUE` becomes:
    ```
    MOV E #LARGE_VALUE      ; Load into temp register E
    MWW #LARGE_ADDR E       ; Use register instead
    ```

### Register Limitations
- 5 registers available by default: A (0), B (1), C (2), D (3), E (4)
- Register E is used as a temporary when auto-generating helper MOV instructions
- This can be configured in `cpu.py` (the `register_count` parameter)

### No External Libraries
- Pure Python, only uses standard library
- Uses `instruction_set.py` helpers for correct binary encoding

## Example: BIOS Loader

The included `bios.asm` demonstrates loading and executing a program:

```asm
.ORG 0x0
SOURCE_PTR = 0
PROG_SIZE = 0x1000
PROGRAM_LOAD_ADDR = 0x10000
IO_POINTER = 0x100

START:
    ; Set up IO to read from hard drive
    MWW #IO_POINTER + #4 #1              ; Set IO mode to READ
    MWW #IO_POINTER + #8 #2              ; HDD READ command
    MWW #IO_POINTER + #12 #PROG_SIZE     ; Set data length
    MWW #IO_POINTER + #16 #SOURCE_PTR   ; Read from offset 0

    ; Copy program into RAM
    MOV B #IO_POINTER + 20
    MOV C #PROG_SIZE
    MOV D #PROGRAM_LOAD_ADDR

COPY_LOOP:
    MR A B
    MW D A
    ADD B B #1
    ADD D D #1
    SUB C #1
    CMP C #0
    JNZ COPY_LOOP
    
    JMP #PROGRAM_LOAD_ADDR
HALT
```

## Error Handling

The assembler provides detailed error messages:
```
Line 22: MWW #UNDEFINED #1
  Error: Undefined symbol: UNDEFINED
```

Common errors:
- **Unknown mnemonic**: Instruction name not recognized
- **Undefined symbol**: Label or static definition not found
- **Invalid operand**: Malformed register, immediate, or address
- **Expression evaluation**: Arithmetic in constants failed

## Performance

- **Linear time** in source size (two passes)
- **No external dependencies** beyond Python standard library
- **Fast expression evaluation** using Python's `eval()` on sanitized expressions

## Future Enhancements

- [ ] Macro system for code reuse
- [ ] Conditional assembly (`.IF`, `.ENDIF`)
- [ ] Data directives (`.BYTE`, `.WORD`, `.STRING`)
- [ ] Disassembler (binary → assembly)
- [ ] Symbol table output for debugging
- [ ] Line-by-line assembly to RAM (live debugging)
