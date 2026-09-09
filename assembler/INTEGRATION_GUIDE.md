# Integrating the Assembler into Your Project

The assembler is designed to work both as a **standalone command-line tool** and as an **importable module** in your Pigeon emulator project.

## Installation

Choose your preferred structure:

### Option A: Same Directory (Simplest)
Put `assembler.py` in the root with the other modules:

```bash
pigeon-emulator/
├── assembler.py          ← Here
├── instruction_set.py
├── cpu.py
└── ...
```

```bash
cp assembler.py ~/path/to/pigeon-emulator/
```

### Option B: In a Subdirectory (Organized)
Put it in `pigeon-emulator/assembler/`:

```bash
pigeon-emulator/
├── assembler/
│   └── assembler.py      ← Here
├── instruction_set.py
├── cpu.py
└── ...
```

```bash
mkdir -p ~/path/to/pigeon-emulator/assembler
cp assembler.py ~/path/to/pigeon-emulator/assembler/
```

**No additional configuration needed** — the assembler automatically finds `instruction_set.py` in the parent directory!

## Usage Methods

### Method 1: Command Line (Standalone)

**If assembler.py is in the root directory:**
```bash
cd ~/path/to/pigeon-emulator
python assembler.py bios.asm bios.bin
python assembler.py program.asm program.bin
```

**If assembler.py is in pigeon-emulator/assembler/:**
```bash
cd ~/path/to/pigeon-emulator
python assembler/assembler.py bios.asm bios.bin
python assembler/assembler.py program.asm program.bin
```

**Works from anywhere** with full path:
```bash
python ~/path/to/pigeon-emulator/assembler/assembler.py myprogram.asm myprogram.bin
```

### Method 2: Import as Module (Integrated)

**If assembler.py is in the root directory:**
```python
from assembler import Assembler

# Assemble a file
asm = Assembler("program.asm")
binary = asm.assemble()
```

**If assembler.py is in pigeon-emulator/assembler/:**
```python
import sys
sys.path.insert(0, 'assembler')  # or full path
from assembler import Assembler

# Assemble a file
asm = Assembler("program.asm")
binary = asm.assemble()
```

### Method 3: Integrated into Main Script

Add assembler support to your `main.py`:

```python
#!/usr/bin/env python3

import sys
from assembler import Assembler
from bios import BIOS
from ram import RAM
from cpu import CPU
from main import Emulator

def assemble_and_run(asm_file, bios_asm_file):
    """Assemble both the BIOS and the program, then run."""
    
    # Assemble BIOS
    print(f"Assembling BIOS: {bios_asm_file}")
    bios_asm = Assembler(bios_asm_file)
    bios_binary = bios_asm.assemble()
    
    # Assemble program
    print(f"Assembling program: {asm_file}")
    prog_asm = Assembler(asm_file)
    prog_binary = prog_asm.assemble()
    
    # Create emulator and load binaries
    emulator = Emulator()
    emulator.load_bios(bios_binary)
    emulator.load_program(prog_binary)
    
    # Run
    print("Starting emulation...")
    emulator.run()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python main.py program.asm")
        sys.exit(1)
    
    program_file = sys.argv[1]
    assemble_and_run(program_file, "bios.asm")
```

Then run:
```bash
python main.py myprogram.asm
```

## Common Workflows

### Development Workflow

```bash
# Edit your assembly file
nano program.asm

# Assemble it
python assembler.py program.asm program.bin

# Run in emulator
python main.py program.asm  # If integrated

# Or test directly
python3 << 'EOF'
from assembler import Assembler
from cpu import CPU
from ram import RAM

asm = Assembler("program.asm")
binary = asm.assemble()

ram = RAM()
ram.load_bytes(binary, start=0x0)
cpu = CPU(ram)

# Run a few steps
for _ in range(100):
    cpu.step()
    if cpu.halted:
        break
    print(cpu.dump())
EOF
```

### Batch Assembly

Assemble multiple files:

```python
import glob
from assembler import Assembler

# Assemble all .asm files in current directory
for asm_file in glob.glob("*.asm"):
    output = asm_file.replace(".asm", ".bin")
    print(f"Assembling {asm_file}...")
    
    asm = Assembler(asm_file)
    binary = asm.assemble()
    
    with open(output, "wb") as f:
        f.write(binary)
    
    print(f"  → {output} ({len(binary)} bytes)")
```

### Error Handling

```python
from assembler import Assembler

try:
    asm = Assembler("program.asm")
    binary = asm.assemble()
except FileNotFoundError:
    print("Error: Source file not found")
except ValueError as e:
    print(f"Assembly error: {e}")
except Exception as e:
    print(f"Unexpected error: {e}")
```

## Project Structure

### Option A: Assembler in Root Directory

```
pigeon-emulator/
├── assembler.py          ← New!
├── instruction_set.py
├── cpu.py
├── ram.py
├── registers.py
├── memory_map.py
├── display_io.py
├── hdd.py
├── io_controller.py
├── main.py
│
├── bios.asm              ← Your assembly files
├── bios.bin              ← Generated binaries
├── program.asm
├── program.bin
│
└── [other files...]
```

### Option B: Assembler in Subdirectory (Organized)

```
pigeon-emulator/
├── assembler/            ← New!
│   └── assembler.py
├── instruction_set.py
├── cpu.py
├── ram.py
├── registers.py
├── memory_map.py
├── display_io.py
├── hdd.py
├── io_controller.py
├── main.py
│
├── bios.asm              ← Your assembly files
├── bios.bin              ← Generated binaries
├── program.asm
├── program.bin
│
└── [other files...]
```

Both structures work! The assembler automatically finds `instruction_set.py`.

## Troubleshooting

### Import Error: "No module named 'instruction_set'"

**Cause**: Assembler can't find `instruction_set.py`

**Solution**: 

1. **If using subdirectory**, run from project root:
```bash
cd ~/path/to/pigeon-emulator
python assembler/assembler.py program.asm program.bin
```

2. **If importing**, make sure to add the assembler to path:
```python
import sys
sys.path.insert(0, 'assembler')  # or full path
from assembler import Assembler
```

3. **Check the search paths** — the assembler will tell you where it looked:
```
Error: Could not find instruction_set.py
  Searched in: /path/to/pigeon-emulator/assembler
  Searched in: /path/to/pigeon-emulator
```

Make sure `instruction_set.py` exists in the pigeon-emulator root directory.

### "Unknown mnemonic: FOO"

**Cause**: Typo in your assembly file

**Solution**: Check `instruction_set.py` for supported instructions, or use:
```python
from instruction_set import INSTRUCTIONS_BY_NAME
print(sorted(INSTRUCTIONS_BY_NAME.keys()))
```

### "Undefined symbol: BAR"

**Cause**: Label or static definition doesn't exist

**Solution**: Add it to your assembly:
```asm
BAR = 0x1000      ; Add this definition
LOOP:             ; Or add this label
    ...
```

## Performance

The assembler is very fast:

- **1000-line program**: <10ms
- **10,000-line program**: <100ms
- **Memory use**: Proportional to source size (~10x)

No optimization needed for typical use.

## Examples

### Simple Test Program

Create `test.asm`:
```asm
.ORG 0x0

START:
    MOV A #1
    MOV B #10
    ADD A B
    HALT
```

Assemble and run:
```bash
python assembler.py test.asm test.bin
python3 << 'EOF'
from assembler import Assembler
from cpu import CPU
from ram import RAM

asm = Assembler("test.asm")
binary = asm.assemble()

ram = RAM()
ram.load_bytes(binary, start=0x0)
cpu = CPU(ram)
cpu.run()

print(f"A = {cpu.reg.read('A')}")  # Should be 11
EOF
```

### Using Assembler in Tests

```python
import unittest
from assembler import Assembler
from cpu import CPU
from ram import RAM

class TestPrograms(unittest.TestCase):
    def test_simple_add(self):
        asm_code = """
        .ORG 0x0
        MOV A #5
        MOV B #3
        ADD C A B
        HALT
        """
        
        # Write to temp file
        with open("/tmp/test.asm", "w") as f:
            f.write(asm_code)
        
        # Assemble and run
        asm = Assembler("/tmp/test.asm")
        binary = asm.assemble()
        
        ram = RAM()
        ram.load_bytes(binary, start=0x0)
        cpu = CPU(ram)
        
        # Run until HALT
        while not cpu.halted:
            cpu.step()
        
        # Check result
        self.assertEqual(cpu.reg.read('C'), 8)
```

## Next Steps

1. **Copy assembler.py** to your project
2. **Test standalone**: `python assembler.py bios.asm bios.bin`
3. **Test integration**: `python3 -c "from assembler import Assembler; print('OK')"`
4. **Update your workflow** to use the assembler
5. **Read ASSEMBLER_README.md** for detailed reference

Enjoy automated assembly! 🚀
