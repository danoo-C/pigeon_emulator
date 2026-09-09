# cpu.py
import struct

from .registers import Registers
from .instruction_set import INSTRUCTIONS_BY_OPCODE, INSTR_SIZE
from .memory_map import REGISTER_COUNT, STACK_TOP

# Unpack an instruction straight out of the RAM bytearray: 4 operand bytes
# plus a little-endian 32-bit immediate. Reading through unpack_from avoids
# allocating a fresh slice for every instruction executed.
_UNPACK = struct.Struct("<4BI").unpack_from

# Opcode -> handler, as a flat list. Indexing a list beats a dict lookup on
# the hot path, and every opcode is a byte, so 256 slots covers the space.
_HANDLERS = [None] * 256
for _op, _instr in INSTRUCTIONS_BY_OPCODE.items():
    _HANDLERS[_op] = _instr.handler


class CPU:
    def __init__(self, ram, register_count=REGISTER_COUNT):
        self.ram = ram
        self.reg = Registers(register_count)
        self.pc = 0
        self.sp = STACK_TOP  # stack starts at the top of RAM, grows downward
        self.zero_flag = False
        # Set by CMP when src1 < src2. Unsigned: this is a borrow, not a
        # sign bit -- registers are 32-bit unsigned and never go negative.
        self.less_flag = False
        self.halted = False

    def run(self):
        """Execute one instruction. Returns 1 once halted, else 0.

        Fetch, decode and dispatch are inlined here rather than split
        across run()/step()/decode(): at roughly a million instructions a
        second, three Python calls per instruction was about half the
        total runtime.
        """
        if self.halted:
            return 1

        pc = self.pc
        try:
            opcode, dst, src1, src2, imm = _UNPACK(self.ram.mem, pc)
        except struct.error:
            raise RuntimeError(f"Fetch past end of memory at PC={pc:#06x}") from None

        self.pc = pc + INSTR_SIZE  # branch handlers overwrite this

        handler = _HANDLERS[opcode]
        if handler is None:
            raise RuntimeError(f"Unknown opcode {opcode} at PC={pc:#06x}")
        handler(self, dst, src1, src2, imm)
        return 0

    def step(self):
        """Single-step, ignoring the halted return value."""
        self.run()

    def dump(self):
        #:#010x means "0x" prefix, 10 characters wide, zero-padded, lowercase hex
        return (f"PC={self.pc:#010x} CC={self.ram.read_word(self.pc):#010x} "
                f"SP={self.sp:#010x} ZF={self.zero_flag} LF={self.less_flag} {self.reg}")
