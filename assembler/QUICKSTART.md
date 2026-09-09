# Pigeon Assembler Quick Start

## Installation

The assembler is a standalone Python script with no external dependencies:

```bash
cp assembler.py /path/to/your/pigeon-emulator/
```

## Basic Usage

### From Command Line

Assemble a file:
```bash
python assembler.py program.asm
# Creates program.bin by default

python assembler.py program.asm output.bin
# Or specify output name
```

### From Python Code

```python
from assembler import Assembler

# Assemble a file
asm = Assembler("program.asm")
binary = asm.assemble()

# Write to disk
with open("program.bin", "wb") as f:
    f.write(binary)

# Or use it directly with your emulator
from cpu import CPU
from ram import RAM

ram = RAM()
ram.load_bytes(binary, start=0x1000)  # Load at address 0x1000
cpu = CPU(ram)
cpu.run()
```

## Writing Assembly Programs

### Hello World (simple loop)

```asm
.ORG 0x0

START:
    MOV A #0           ; A = 0
    MOV B #10          ; B = 10

LOOP:
    ADD A #1           ; A += 1
    CMP A B            ; Compare A with 10
    JNZ LOOP           ; Loop until A == 10
    
    HALT
```

### Memory Operations

```asm
.ORG 0x0

; Read/write memory
BUFFER = 0x1000

START:
    MOV A #0xFF        ; A = 255
    MWW #BUFFER A      ; Write A to memory at BUFFER
    
    MRW B #BUFFER      ; Read word from BUFFER into B
    ; Now B == 255
    
    HALT
```

### Arithmetic with Expressions

```asm
.ORG 0x0

; Use expressions in definitions
WIDTH = 16
HEIGHT = 32
TOTAL = WIDTH * HEIGHT   ; = 512

ARRAY_BASE = 0x1000
ARRAY_END = ARRAY_BASE + TOTAL   ; = 0x1200

INIT_VALUE = 42

START:
    MOV A #INIT_VALUE
    MOV B #ARRAY_BASE
    MOV C #ARRAY_END

FILL_LOOP:
    MWW B A           ; memory[B] = A
    ADD B B #4        ; B += 4 (next word)
    CMP B C
    JL FILL_LOOP
    
    HALT
```

### Conditional Execution

```asm
.ORG 0x0

START:
    MOV A #5
    MOV B #10
    
    CMP A B            ; Compare A and B
    JL LESS            ; Jump if A < B
    JG GREATER         ; Jump if A > B
    
    ; If we reach here, A == B
    MOV C #0
    JMP DONE
    
LESS:
    MOV C #1           ; A is smaller
    JMP DONE
    
GREATER:
    MOV C #2           ; A is larger
    
DONE:
    HALT
```

## Common Patterns

### Increment a Register
```asm
ADD A A #1    ; Shorthand: ADD A #1 (auto-expands to ADD A A #1)
SUB B B #1    ; Shorthand: SUB B #1 (auto-expands to SUB B B #1)
```

### Loop N Times
```asm
MOV C #10        ; Loop counter
LOOP:
    ; ... do work ...
    SUB C #1     ; Decrement
    CMP C #0     ; Check if done
    JNZ LOOP     ; Jump if not zero
```

### Read/Write Data Structures
```asm
; Assuming structure at BUFFER, 8 bytes each:
; [0] - value1 (4 bytes)
; [4] - value2 (4 bytes)

MOV B #BUFFER
MRW A [B]           ; Read value1
ADD B B #4
MRW C [B]           ; Read value2
```

### Call-Return Pattern (using stack)
```asm
PUSH A              ; Save A on stack
PUSH B              ; Save B on stack

; Call some code...

POP B               ; Restore B
POP A               ; Restore A (LIFO order)
```

## Debugging

### Enable Verbose Output
Currently, the assembler is quiet on success. Edit `assembler.py` to print:

```python
# In __main__:
print(f"Labels: {asm.symbols}")
print(f"Definitions: {asm.static_defs}")
print(f"Instructions: {len(asm.instructions)}")
```

### Disassemble Output
Use Python to read and inspect the binary:

```python
from instruction_set import INSTRUCTIONS_BY_OPCODE

with open("program.bin", "rb") as f:
    data = f.read()

for offset in range(0, len(data), 8):
    instr = data[offset:offset+8]
    opcode = instr[0]
    print(f"[{offset:04x}] {INSTRUCTIONS_BY_OPCODE[opcode].name}")
```

## Tips & Tricks

1. **Use symbolic constants** instead of magic numbers
   ```asm
   BAD:  JMP #0x10000
   GOOD: PROGRAM_ADDR = 0x10000
         JMP #PROGRAM_ADDR
   ```

2. **Compute derived values** at assembly time
   ```asm
   FRAME_WIDTH = 320
   FRAME_HEIGHT = 240
   FRAME_SIZE = FRAME_WIDTH * FRAME_HEIGHT
   ```

3. **Comments are your friend**
   ```asm
   ; This value MUST match the kernel's CONFIG_BASE
   KERNEL_CONFIG = 0xFFFFC000
   ```

4. **Keep labels meaningful**
   ```asm
   BAD:   LOOP1:
   GOOD:  COPY_BYTES_LOOP:
   ```

5. **Pre-load large immediates if reusing**
   ```asm
   MOV E #LARGE_ADDRESS
   MWW E #VALUE1
   MWW E #VALUE2    ; Reuse E instead of loading again
   ```

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Unknown mnemonic: FOO` | Typo in instruction | Check spelling (case-insensitive) |
| `Undefined symbol: BAR` | Label or definition doesn't exist | Add definition or check spelling |
| `Expected register, got: 123` | Need register but got number | Use A-E, not numeric indices |
| `Invalid operand: [0xGG]` | Bad hex number | Use `0x` prefix for hex |
| `Failed to evaluate: ...` | Math in definition broke | Check syntax: `WIDTH * HEIGHT` not `WIDTH*HEIGHT` |

## Performance Notes

- **Assembly is fast**: Even 10,000-line programs assemble in <100ms
- **Symbol resolution**: O(n) where n = number of symbols
- **No optimization**: Output is 1:1 with source (one line → one instruction, mostly)
- **Binary size**: Every instruction is exactly 8 bytes

## Next Steps

1. Write a small test program
2. Assemble it: `python assembler.py test.asm test.bin`
3. Load it in your emulator and trace execution
4. Iterate and debug

For more details, see `ASSEMBLER_README.md`.
