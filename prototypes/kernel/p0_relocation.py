"""P0 (docs/kernel_exec.md §5): a program can be relocated by assembling it
twice, at two addresses, and comparing the images.

1. Every C program in user/, with its libraries: every word that differs
   must differ by exactly the distance, and no other byte may differ.
2. A test program patched from 0x20000 to 0x01021238 runs, and is byte for
   byte the build made at that address.
3. The same program, with its frame stack and heap moved after its image,
   runs there without writing to the fixed program or frame-stack regions.
"""
import re
import struct

from proto import (CPU, PROGRAM_LOAD_ADDR, RAM, RAM_SIZE, ROOT, STACK_TOP,
                   asm_from_c, assemble, compile_units, libraries_for)
from emulator.memory_map import HEAP_START

BASE, ALT = PROGRAM_LOAD_ADDR, 0x01021238
DELTA = ALT - BASE


def compare(a, b):
    """Offsets of words holding an address, other words that differ, and
    bytes that differ outside those words."""
    assert len(a) == len(b), (len(a), len(b))
    addresses, other, covered = [], [], set()
    for off in range(0, len(a) - 3, 4):
        wa = struct.unpack_from("<I", a, off)[0]
        wb = struct.unpack_from("<I", b, off)[0]
        if wa == wb:
            continue
        if (wb - wa) & 0xFFFFFFFF == DELTA:
            addresses.append(off)
            covered.update(range(off, off + 4))
        else:
            other.append(off)
    stray = sum(1 for i in range(len(a)) if a[i] != b[i] and i not in covered)
    return addresses, other, stray


def patch(image, addresses):
    out = bytearray(image)
    for off in addresses:
        word = struct.unpack_from("<I", out, off)[0]
        struct.pack_into("<I", out, off, (word + DELTA) & 0xFFFFFFFF)
    return bytes(out)


def run_at(image, addr):
    ram = RAM(RAM_SIZE)
    ram.load_bytes(image, addr)
    cpu = CPU(ram)
    cpu.pc = addr
    for _ in range(3_000_000):
        if cpu.run() == 1:
            return cpu, ram
    raise AssertionError("did not halt")


print("1. every C program in user/, built at 0x20000 and at 0x01021238")
print(f"   {'program':8} {'bytes':>8} {'addresses':>10} {'in an instruction':>18} "
      f"{'other words':>12} {'other bytes':>12}")
for name in ("files", "disc", "graph", "cube", "demo"):
    source = ROOT / "user" / f"{name}.c"
    asm = compile_units([source, *libraries_for(source)])
    a, _ = assemble(asm, BASE)
    b, _ = assemble(asm, ALT)
    addresses, other, stray = compare(a, b)
    in_instruction = sum(1 for off in addresses if off % 8 == 4)
    print(f"   {name:8} {len(a):>8,} {len(addresses):>10,} {in_instruction:>18,} "
          f"{len(other):>12} {stray:>12}")

TEST = r'''
#include <pigeon/mem.h>
#include <pigeon/string.h>
char *names[3] = {"alpha", "beta", "gamma"};
int counter;
int twice(int x) { return x * 2; }
int fact(int n) { if (n <= 1) return 1; return n * fact(n - 1); }
int main(void) {
    int (*f)(int) = twice;
    char *buf = (char *)malloc(32);
    strlcpy(buf, names[2], 32);
    counter = counter + (int)strlen(buf);
    return f(fact(5)) + counter + ((unsigned)buf > (unsigned)&counter ? 1000 : 0);
}
'''
asm = asm_from_c([("reloc.c", TEST)])

a, _ = assemble(asm, BASE)
b, _ = assemble(asm, ALT)
addresses, other, stray = compare(a, b)
cpu, _ = run_at(patch(a, addresses), ALT)
print(f"2. patched test program: returned {cpu.reg.read(0)} (want 245: its heap is still at the "
      f"fixed address, below its data); stack balanced={cpu.sp == STACK_TOP}; "
      f"identical to a build at that address={patch(a, addresses) == b}")

try:
    assemble(asm.replace("__frame_base  = HEAP_START", "__image_end:\n__frame_base  = __image_end"), BASE)
    print("   a label in NAME = expression: accepted")
except ValueError as e:
    print(f"   a label in NAME = expression: refused -- {str(e).splitlines()[-1].strip()}")

lines = [l for l in asm.splitlines()
         if not re.match(r"\s*__(frame_base|frame_limit|heap_base)\s*=", l)]
moved = "\n".join(lines) + "\n__image_end:\n"
moved = re.sub(r"\b__heap_base\b", "(__image_end + 262144)", moved)
moved = re.sub(r"\b__frame_base\b", "__image_end", moved)
a, sym = assemble(moved, BASE)
b, _ = assemble(moved, ALT)
addresses, other, stray = compare(a, b)
cpu, ram = run_at(patch(a, addresses), ALT)
image_end = ALT + (sym["__image_end"] - BASE)
fixed_program = sum(1 for x in range(PROGRAM_LOAD_ADDR, PROGRAM_LOAD_ADDR + 0x40000, 4) if ram.read_word(x))
fixed_frames = sum(1 for x in range(HEAP_START, HEAP_START + 0x60000, 4) if ram.read_word(x))
print(f"3. frame stack and heap after the image: returned {cpu.reg.read(0)} (want 1245: its heap is "
      f"above its data); F={cpu.reg.read(5):#x}, image ends at {image_end:#x}; "
      f"stack balanced={cpu.sp == STACK_TOP}; words written to 0x20000-0x5FFFF: {fixed_program}, "
      f"to 0x120000-0x17FFFF: {fixed_frames}")
