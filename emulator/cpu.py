# cpu.py
from registers import Registers
from instruction_set import INSTRUCTIONS_BY_OPCODE, INSTR_SIZE, decode
from memory_map import STACK_TOP


class CPU:
    def __init__(self, ram, register_count=5):
        self.ram = ram
        self.reg = Registers(register_count)
        self.pc = 0
        self.sp = STACK_TOP  # stack starts at the top of RAM, grows downward
        self.zero_flag = False
        self.negative_flag = False  # set by CMP: True when src1 < src2
        self.halted = False

    def step(self):
        if self.halted:
            return

        chunk = self.ram.mem[self.pc:self.pc + INSTR_SIZE]
        if len(chunk) < INSTR_SIZE:
            raise RuntimeError(f"Fetch past end of memory at PC={self.pc:#06x}")

        opcode, dst, src1, src2, imm = decode(chunk)
        self.pc += INSTR_SIZE  # default; branch instructions overwrite this

        instr = INSTRUCTIONS_BY_OPCODE.get(opcode)
        if instr is None:
            raise RuntimeError(f"Unknown opcode {opcode} at PC={self.pc - INSTR_SIZE:#06x}")

        instr.handler(self, dst, src1, src2, imm)

    def run(self):
        if self.halted:
            return 1
        self.step()
        return 0
        

    def dump(self):
        #:#010x means "0x" prefix, 10 characters wide, zero-padded, lowercase hex
        return f"PC={self.pc:#010x} CC={self.ram.read_word(self.pc):#010x} SP={self.sp:#010x} ZF={self.zero_flag} NF={self.negative_flag} {self.reg}"