data = open('bios.bin', 'rb').read()
print(f"Total: {len(data)} bytes ({len(data)//8} instructions)")
print()

# Find all 0xFF bytes that are in opcode position (every 8 bytes, at offset 0)
bad_opcodes = []
for i in range(0, len(data), 8):
    opcode = data[i]
    if opcode == 0xFF:
        bad_opcodes.append(i)
        print(f"0x{i:04x}: Opcode is 0xFF (NONE_REG) -- THIS IS WRONG")
        chunk = data[i:i+8]
        print(f"       Bytes: {' '.join(f'{b:02x}' for b in chunk)}")

if bad_opcodes:
    print(f"\nFound {len(bad_opcodes)} bad opcode(s)")
else:
    print("No 0xFF opcodes found -- binary looks clean")

# Also dump first 10 instructions for sanity check
print("\n--- First 10 instructions ---")
from instruction_set import INSTRUCTIONS_BY_OPCODE, NONE_REG
for i in range(min(10, len(data)//8)):
    offset = i * 8
    chunk = data[offset:offset+8]
    opcode = chunk[0]
    if opcode in INSTRUCTIONS_BY_OPCODE:
        name = INSTRUCTIONS_BY_OPCODE[opcode].name
    else:
        name = f"UNKNOWN({opcode})"
    dst, src1, src2 = chunk[1], chunk[2], chunk[3]
    imm = int.from_bytes(chunk[4:8], 'little')
    def fmt(x):
        return '.' if x == 0xFF else str(x)
    print(f"0x{offset:04x}: {name:<6} dst={fmt(dst):<3} src1={fmt(src1):<3} src2={fmt(src2):<3} imm=0x{imm:08x}")