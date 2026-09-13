"""Harness smoke test: new opcodes, a relocatable app called like a function."""
from proto import *  # noqa: F403
from emulator.memory_map import HEAP_START

names = ["SHR", "GETSP", "SETSP", "EI", "DI", "IRET", "SETIV"]
print("instructions before:", OLD_OPCODE_COUNT,
      {n: IS.INSTRUCTIONS_BY_NAME[n].opcode for n in names})

blob = build_app(r'''
#include <pigeon/mem.h>
int g;
int main(int argc, char **argv) {
    char *p = (char *)malloc(16u);
    g = argc * 10;
    return g + argv[0][0] + (p != (char *)0);
}
''')
ram = RAM(RAM_SIZE)
info = load_app(ram, blob, 0x01000000)
ram.load_bytes(b"A\0", 0x00016100)
ram.write_word(0x00016200, 0x00016100)
caller = f""".ORG PROGRAM_LOAD_ADDR
    MOV F, #0x120000
    MOV C, F
    MWW C, #3
    ADD C, C, #4
    MOV A, #0x16200
    MWW C, A
    MOV E, #{info['entry']:#x}
    CALL E
    GETSP B
    HALT
"""
image, _ = assemble(caller, PROGRAM_LOAD_ADDR)
ram.load_bytes(image, PROGRAM_LOAD_ADDR)
cpu = new_cpu(ram)
cpu.pc = PROGRAM_LOAD_ADDR
n = run(cpu)
print(f"app returned {cpu.reg.read(0)} (want 96), SP after={cpu.reg.read(1):#x} "
      f"(want {STACK_TOP:#x}), F={cpu.reg.read(5):#x} (want {HEAP_START:#x}), {n} instructions, "
      f"{len(blob)} byte file")
