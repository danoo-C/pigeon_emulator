# cpu.py
import struct

from .registers import Registers
from .instruction_set import INSTRUCTIONS_BY_OPCODE, INSTR_SIZE
from .memory_map import (
    FLAG_IE, FLAG_LESS, FLAG_ZERO, REGISTER_COUNT, STACK_TOP, VEC_BAD_FETCH, VEC_BAD_OPCODE,
)

# Unpack an instruction straight out of the RAM bytearray: 4 operand bytes
# plus a little-endian 32-bit immediate. Reading through unpack_from avoids
# allocating a fresh slice for every instruction executed.
_UNPACK = struct.Struct("<4BI").unpack_from


def _bad_opcode(cpu, dst, src1, src2, imm):
    """What an opcode with no instruction runs: the VEC_BAD_OPCODE fault."""
    pc = cpu.pc - INSTR_SIZE
    cpu.fault(VEC_BAD_OPCODE, pc,
              RuntimeError(f"Unknown opcode {cpu.ram.mem[pc]} at PC={pc:#06x}"))


# Opcode -> handler, as a flat list. Indexing a list beats a dict lookup on
# the hot path, and every opcode is a byte, so 256 slots covers the space.
# The slots with no instruction fault, so neither loop checks for one.
_HANDLERS = [_bad_opcode] * 256
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
        # Interrupts and faults (docs/kernel.md §13). `ie` is whether
        # interrupts are taken, `ivt` the vector table's address -- 0 until
        # SETIV -- and `pending` one bit per vector raised and not yet taken.
        self.ie = False
        self.ivt = 0
        self.pending = 0
        # (handler, SP) just after a fault handler was entered: a fault found
        # there is a fault on that handler's first instruction.
        self._fault_entry = None

    def run(self):
        """Execute one instruction, taking a pending interrupt first.
        Returns 1 once halted, else 0.

        Fetch, decode and dispatch are inlined here rather than split
        across run()/step()/decode(): at roughly a million instructions a
        second, three Python calls per instruction was about half the
        total runtime. Machine.run() inlines this again and must keep
        matching it.
        """
        if self.halted:
            return 1
        if self.pending and self.ie:
            self.take_interrupt()

        pc = self.pc
        try:
            opcode, dst, src1, src2, imm = _UNPACK(self.ram.mem, pc)
        except struct.error:
            self.fetch_fault(pc)
            return 0

        self.pc = pc + INSTR_SIZE  # branch handlers overwrite this
        _HANDLERS[opcode](self, dst, src1, src2, imm)
        return 0

    def step(self):
        """Single-step, ignoring the halted return value."""
        self.run()

    # --- interrupts and faults ----------------------------------------------

    def interrupt(self, vector):
        """Raise interrupt `vector`. It waits while interrupts are off, and
        is taken before the next instruction once they are on, the lowest
        pending vector first."""
        self.pending |= 1 << vector

    def take_interrupt(self):
        """Enter the handler for the lowest pending interrupt. The loops
        call this only with interrupts on and one pending."""
        vector = (self.pending & -self.pending).bit_length() - 1
        self.pending &= ~(1 << vector)
        handler = self._handler(vector)
        if handler is None:
            raise RuntimeError(f"Interrupt {vector} at PC={self.pc:#06x} has no handler")
        self._enter(handler, self.pc)

    def fault(self, vector, pc, error):
        """Deliver fault `vector` for the instruction at `pc`, whether
        interrupts are on or not. The handler returns to that instruction,
        so it could retry it.

        With no handler for the vector, `error` is raised: the emulator
        stops, as it did before there were vectors. So does a fault on a
        fault handler's first instruction -- a vector pointing at no code --
        rather than entering that handler over and over.
        """
        handler = self._handler(vector)
        if handler is None:
            raise error
        if self._fault_entry == (pc, self.sp):
            raise RuntimeError(f"Double fault: {error}, on the first instruction "
                               f"of a fault handler") from error
        self._enter(handler, pc)
        self._fault_entry = (handler, self.sp)

    def fetch_fault(self, pc):
        """Fetching the instruction at `pc` ran past the end of memory."""
        self.fault(VEC_BAD_FETCH, pc, RuntimeError(f"Fetch past end of memory at PC={pc:#06x}"))

    def _handler(self, vector):
        """The address in vector `vector`, or None: no table, or a 0 entry."""
        if not self.ivt:
            return None
        return self.ram.read_word(self.ivt + 4 * vector) or None

    def _enter(self, handler, return_pc):
        """Push the flags word, then the address to return to; turn
        interrupts off and jump to the handler. IRET undoes all four."""
        flags = ((FLAG_ZERO if self.zero_flag else 0) | (FLAG_LESS if self.less_flag else 0)
                 | (FLAG_IE if self.ie else 0))
        ram = self.ram
        self.sp -= 4
        ram.write_word(self.sp, flags)
        self.sp -= 4
        ram.write_word(self.sp, return_pc)
        self.ie = False
        self.pc = handler

    def dump(self):
        #:#010x means "0x" prefix, 10 characters wide, zero-padded, lowercase hex
        return (f"PC={self.pc:#010x} CC={self.ram.read_word(self.pc):#010x} "
                f"SP={self.sp:#010x} ZF={self.zero_flag} LF={self.less_flag} "
                f"IE={self.ie} IV={self.ivt:#010x} PENDING={self.pending:#x} {self.reg}")
