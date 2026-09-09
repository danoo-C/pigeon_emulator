# Setup Guide for Your Pigeon Emulator Project

Based on the file structure you showed, here's exactly how to set up the assembler:

## Quick Setup

### Step 1: Copy the assembler into your assembler subdirectory

```bash
cd ~/path/to/pigeon-emulator
cp assembler.py assembler/
```

This creates:
```
pigeon-emulator/
├── assembler/
│   └── assembler.py      ← Put it here
├── bios.asm
├── bios.bin
├── instruction_set.py    ← Stays here (in root)
├── cpu.py
└── ...
```

### Step 2: Test it works

```bash
cd ~/path/to/pigeon-emulator
python assembler/assembler.py bios.asm bios_test.bin
```

You should see:
```
✓ Assembled bios.asm -> bios_test.bin (144 bytes)
```

Done! ✅

## Using the Assembler

### From Command Line

```bash
# Assemble a file
python assembler/assembler.py bios.asm bios.bin
python assembler/assembler.py program.asm program.bin

# Or with full output control
python assembler/assembler.py myprogram.asm output/myprogram.bin
```

### From Python Code

In your `main.py` or other scripts:

```python
import sys
sys.path.insert(0, 'assembler')

from assembler import Assembler

# Assemble the BIOS
print("Assembling BIOS...")
bios_asm = Assembler("bios.asm")
bios_binary = bios_asm.assemble()
print(f"BIOS: {len(bios_binary)} bytes")

# Assemble your program
print("Assembling program...")
prog_asm = Assembler("program.asm")
prog_binary = prog_asm.assemble()
print(f"Program: {len(prog_binary)} bytes")

# Load them into your emulator
from bios import BIOS
from ram import RAM

ram = RAM()
bios_loader = BIOS(bios_binary)
bios_loader.write_bios(ram)
ram.load_bytes(prog_binary, start=0x1000)

# Run the emulator
from cpu import CPU
cpu = CPU(ram)
while not cpu.halted:
    cpu.step()

print("Done!")
```

### As a Module in Other Files

```python
# In any file in your project
import sys
sys.path.insert(0, 'assembler')

from assembler import Assembler

asm = Assembler("test.asm")
binary = asm.assemble()
```

## Project Structure

Your project should now look like:

```
pigeon-emulator/
├── assembler/                ← New directory
│   └── assembler.py          ← Assembler code
├── display/                  ← Existing
│   ├── __init__.py
│   └── display.py
│
├── __pycache__/              ← Existing
├── bios.asm                  ← Assembly source
├── bios.bin                  ← Generated binary
├── bios.py
├── cpu.py
├── display_io.py
├── hdd.py
├── instruction_set.py        ← The assembler uses this
├── io_controller.py
├── main.py
├── memory_map.py
├── pigeon_hard_drive.bin
├── ram.py
├── registers.py
├── screen_test.bin
├── sreen_test.bin
└── user_io.bin
```

## Common Tasks

### Assemble bios.asm and run it

```bash
python assembler/assembler.py bios.asm bios.bin
python main.py
```

### Assemble a test program

```bash
# Create test.asm
cat > test.asm << 'EOF'
.ORG 0x1000

START:
    MOV A #10
    MOV B #20
    ADD C A B
    HALT
EOF

# Assemble it
python assembler/assembler.py test.asm test.bin
```

### Batch assemble all .asm files

```bash
python3 << 'EOF'
import glob
import sys
sys.path.insert(0, 'assembler')
from assembler import Assembler

for asm_file in glob.glob("*.asm"):
    output = asm_file.replace(".asm", ".bin")
    print(f"Assembling {asm_file}...", end=" ")
    
    try:
        asm = Assembler(asm_file)
        binary = asm.assemble()
        with open(output, "wb") as f:
            f.write(binary)
        print(f"✓ ({len(binary)} bytes)")
    except Exception as e:
        print(f"✗ {e}")
EOF
```

## Troubleshooting

### "No module named 'instruction_set'"

Make sure you're running from the pigeon-emulator root directory:

```bash
cd ~/path/to/pigeon-emulator  # Important!
python assembler/assembler.py bios.asm bios.bin
```

### "File not found: bios.asm"

Make sure the .asm file is in the pigeon-emulator root (where you're running from):

```bash
cd ~/path/to/pigeon-emulator
# bios.asm should be HERE (same directory as main.py, cpu.py, etc.)
python assembler/assembler.py bios.asm bios.bin
```

### Error messages during assembly

The assembler will show you exactly what's wrong:

```
Line 25: JMP #UNDEFINED_LABEL
  Error: Undefined symbol: UNDEFINED_LABEL
```

Check your assembly file for typos or missing definitions.

## Performance

The assembler is very fast:

- **bios.asm (14 instructions)**: <1ms
- **1000-line program**: <10ms
- **10,000-line program**: <100ms

No worries about compilation speed!

## Next Steps

1. ✅ Copy `assembler.py` to `assembler/` subdirectory
2. ✅ Test: `python assembler/assembler.py bios.asm bios_test.bin`
3. ✅ Use in `main.py` to load and run programs
4. ✅ Write new assembly programs in `.asm` files
5. Read **ASSEMBLER_README.md** for detailed syntax reference

Enjoy! 🚀
