# instruction_set.py
"""
The instruction set as a self-registering table.

Each instruction is defined once, right next to its own logic, using the
@instruction(...) decorator. That single declaration is the source of truth
for:
  - its name          (used by the assembler and disassembler)
  - its opcode         (assigned automatically, in declaration order)
  - its size in bytes  (how many bytes the CPU reads per instruction)
  - its handler        (the function the CPU actually calls)

cpu.py never needs a big if/elif chain -- it just looks the instruction up
by opcode and calls .handler(cpu, dst, src1, src2, imm).
"""

from dataclasses import dataclass
from typing import Callable

NONE_REG = 0xFF          # sentinel: "this operand slot is unused"
INSTR_SIZE = 8            # [opcode:1][dst:1][src1:1][src2:1][imm:4 LE]


@dataclass
class Instruction:
    name: str
    opcode: int
    handler: Callable
    size: int = INSTR_SIZE


INSTRUCTIONS_BY_NAME = {}
INSTRUCTIONS_BY_OPCODE = {}

_next_opcode = 0


def instruction(name, size=INSTR_SIZE):
    """Register a function as an instruction handler.

    Opcodes are handed out in declaration order, so APPENDING an
    instruction is safe but INSERTING one renumbers every opcode after it
    and invalidates every previously assembled binary.
    """
    def decorator(func):
        global _next_opcode
        opcode = _next_opcode
        _next_opcode += 1
        instr = Instruction(name=name, opcode=opcode, handler=func, size=size)
        INSTRUCTIONS_BY_NAME[name] = instr
        INSTRUCTIONS_BY_OPCODE[opcode] = instr
        return func
    return decorator


def reg_or_imm(cpu, reg_slot, imm):
    """Resolve an operand that could be a register OR an immediate.
    Most instructions let their last operand be EITHER a register OR a
    literal constant -- e.g. `ADD A, B, C` (register) vs `ADD A, B, #5`
    (immediate). Whichever the assembler didn't fill in gets NONE_REG,
    and this falls back to the immediate value instead."""
    if reg_slot == NONE_REG:
        return imm
    return cpu.reg.read(reg_slot)


# --------------------------------------------------------------------------
# Instruction definitions
# Every handler has the same signature: (cpu, dst, src1, src2, imm)
# so the CPU can call any of them the same way, regardless of which
# operands a given instruction actually uses.
# --------------------------------------------------------------------------

@instruction("NOP")
def op_nop(cpu, dst, src1, src2, imm):
    """NOP -- No OPeration. Does nothing, just advances PC.
    Usage:   NOP
    Useful as a placeholder, or for padding/timing."""
    pass


@instruction("MOV")
def op_mov(cpu, dst, src1, src2, imm):
    """MOV -- copy a value into a register.
    Usage:   MOV dst, src        (dst = value currently in register src)
             MOV dst, #imm       (dst = a literal constant)
    Example: MOV A, #10          -> A = 10
             MOV B, A            -> B = whatever A currently holds"""
    cpu.reg.write(dst, reg_or_imm(cpu, src1, imm))


@instruction("ADD")
def op_add(cpu, dst, src1, src2, imm):
    """ADD -- addition. dst = src1 + src2 (or src1 + immediate).
    Usage:   ADD dst, src1, src2
             ADD dst, src1, #imm
    Example: ADD C, A, B         -> C = A + B
             ADD A, A, #1        -> A = A + 1  (increment)"""
    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    cpu.reg.write(dst, a + b)


@instruction("SUB")
def op_sub(cpu, dst, src1, src2, imm):
    """SUB -- subtraction. dst = src1 - src2 (or src1 - immediate).
    Usage:   SUB dst, src1, src2
             SUB dst, src1, #imm
    Example: SUB C, A, B         -> C = A - B
             SUB A, A, #1        -> A = A - 1  (decrement)
    Note: registers are unsigned 32-bit -- going below 0 wraps around
    to a very large number rather than becoming negative."""
    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    cpu.reg.write(dst, a - b)


@instruction("MUL")
def op_mul(cpu, dst, src1, src2, imm):
    """MUL -- multiplication. dst = src1 * src2 (or src1 * immediate).
    Usage:   MUL dst, src1, src2
             MUL dst, src1, #imm
    Example: MUL C, A, B         -> C = A * B"""
    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    cpu.reg.write(dst, a * b)


@instruction("DIV")
def op_div(cpu, dst, src1, src2, imm):
    """DIV -- integer division. dst = src1 // src2 (or src1 // immediate).
    Usage:   DIV dst, src1, src2
             DIV dst, src1, #imm
    Example: DIV C, A, B         -> C = A / B, rounded down
    Raises ZeroDivisionError if the divisor is 0 -- there's no DIV-by-zero
    flag yet, so this currently crashes the emulator rather than setting
    a status bit the program could check."""
    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    if b == 0:
        raise ZeroDivisionError(f"Division by zero at PC={cpu.pc:#06x}")
    cpu.reg.write(dst, a // b)

@instruction("OR")
def op_or(cpu, dst, src1, src2, imm):

    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    cpu.reg.write(dst, a | b)


@instruction("AND")
def op_and(cpu, dst, src1, src2, imm):

    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    cpu.reg.write(dst, a & b)


@instruction("XOR")
def op_xor(cpu, dst, src1, src2, imm):

    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    cpu.reg.write(dst, a ^ b)


@instruction("NOT")
def op_not(cpu, dst, src1, src2, imm):
    """
    USAGE NOT dst, src1
    Example: NOT A, B
             NOT A, #0xFF
    """
    a = reg_or_imm(cpu, src1, imm)
    cpu.reg.write(dst, ~a & 0xFFFFFFFF)  # only keep the low 32 bits, since registers are 32-bit


@instruction("JMP")
def op_jmp(cpu, dst, src1, src2, imm):
    """JMP -- unconditional jump. PC = imm (or a label the assembler
    resolves to an address).
    Usage:   JMP address
             JMP some_label
    Example: JMP loop            -> jumps to the instruction at 'loop:'
    Always jumps, no matter what any flag says -- for conditional jumps
    see JZ/JNZ/JL/JG/JLE/JGE below."""
    cpu.pc = reg_or_imm(cpu, src1, imm)  # allow jump to a register value too, for indirect jumps
    # cpu.pc = imm


@instruction("MR")
def op_mr(cpu, dst, src1, src2, imm):
    """MR -- Memory Read (1 byte). dst = the byte stored at an address.
    Usage:   MR dst, [address]
             MR dst, [reg]        (address comes from a register)
    Example: MR A, [0xF000]      -> A = whatever byte sits at 0xF000
             MR A, [B]            -> A = byte at the address stored in B
    For reading a full 32-bit value at once, see MRW."""
    addr = reg_or_imm(cpu, src1, imm)
    cpu.reg.write(dst, cpu.ram.read_byte(addr))


@instruction("MW")
def op_mw(cpu, dst, src1, src2, imm):
    """MW -- Memory Write (1 byte). Stores the low byte of a register or an
    immediate value at an address.
    Usage:   MW [address], src
             MW [address], #imm
             MW [reg], src         (address comes from a register)
    Example: MW [0xF000], A       -> writes A's value as a byte at 0xF000
             MW [0xF000], #5      -> writes the value 5 as a byte at 0xF000
             MW [B], A             -> writes A's value at the address in B
    For writing a full 32-bit value at once, see MWW."""
    addr = cpu.reg.read(dst)

    value = reg_or_imm(cpu, src1, imm)
    value &= 0xFF  # only the low byte is written
    cpu.ram.write_byte(addr, value)


@instruction("MRW")
def op_mrw(cpu, dst, src1, src2, imm):
    """MRW -- Memory Read Word (4 bytes / 32 bits) -- same as MR but reads
    a whole register-sized value in one instruction instead of one byte.
    Usage:   MRW dst, [address]
    Example: MRW A, [0xC000]     -> A = the full 32-bit value at 0xC000
    Useful for things like reading a 32-bit length field in one shot."""
    addr = reg_or_imm(cpu, src1, imm)
    cpu.reg.write(dst, cpu.ram.read_word(addr))


@instruction("MWW")
def op_mww(cpu, dst, src1, src2, imm):
    """MWW -- Memory Write Word (4 bytes / 32 bits) -- same as MW but
    writes a whole register-sized value in one instruction instead of
    one byte.
    Usage:   MWW [address], src
             MWW [address], #imm
    Example: MWW [0xC000], A     -> writes all 4 bytes of A at 0xC000
    DST - allway fed from a register
    SRC1 - fed from a register or immediate
    """
    addr = cpu.reg.read(dst)

    value = reg_or_imm(cpu, src1, imm)
    cpu.ram.write_word(addr, value)


@instruction("CMP")
def op_cmp(cpu, dst, src1, src2, imm):
    """CMP -- compare two values and set flags for a following jump.
    Doesn't touch any register -- only sets:
      zero_flag = True if src1 == src2 (or immediate)
      less_flag = True if src1 <  src2 (or immediate), unsigned
    Usage:   CMP src1, src2
             CMP src1, #imm
    Example: CMP A, B
             JL  less_branch      -> jumps if A < B
    Always pair this with one of the J** instructions below -- CMP by
    itself has no visible effect."""
    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    cpu.zero_flag = (a == b)
    cpu.less_flag = (a < b)


@instruction("JZ")
def op_jz(cpu, dst, src1, src2, imm):
    """JZ -- Jump if Zero. Jumps only if the last CMP found the values
    EQUAL (zero_flag is set).
    Usage:   JZ address / label   (use right after a CMP)
    Example: CMP A, B
             JZ  equal_branch     -> jumps only if A == B"""
    if cpu.zero_flag:
        cpu.pc = reg_or_imm(cpu, src1, imm)


@instruction("JNZ")
def op_jnz(cpu, dst, src1, src2, imm):
    """JNZ -- Jump if Not Zero. Jumps only if the last CMP found the
    values UNEQUAL (zero_flag is clear). The classic "keep looping while
    not done yet" instruction.
    Usage:   JNZ address / label  (use right after a CMP)
    Example: CMP  A, #10
             JNZ  loop            -> keep looping until A == 10"""
    if not cpu.zero_flag:
        cpu.pc = reg_or_imm(cpu, src1, imm)


@instruction("JL")
def op_jl(cpu, dst, src1, src2, imm):
    """JL -- Jump if Less than. Jumps if the last CMP's src1 < src2.
    Usage:   JL address / label   (use right after a CMP)
    Example: CMP A, #10
             JL  loop             -> loop while A < 10"""
    if cpu.less_flag:
        cpu.pc = reg_or_imm(cpu, src1, imm)


@instruction("JG")
def op_jg(cpu, dst, src1, src2, imm):
    """JG -- Jump if Greater than. Jumps if the last CMP's src1 > src2
    (strictly -- equal values do NOT jump).
    Usage:   JG address / label   (use right after a CMP)
    Example: CMP A, B
             JG  a_bigger"""
    if not cpu.zero_flag and not cpu.less_flag:
        cpu.pc = reg_or_imm(cpu, src1, imm)


@instruction("JLE")
def op_jle(cpu, dst, src1, src2, imm):
    """JLE -- Jump if Less than or Equal. Jumps if the last CMP's
    src1 <= src2.
    Usage:   JLE address / label  (use right after a CMP)
    Example: CMP A, #10
             JLE loop             -> loop while A <= 10"""
    if cpu.less_flag or cpu.zero_flag:
        cpu.pc = reg_or_imm(cpu, src1, imm)


@instruction("JGE")
def op_jge(cpu, dst, src1, src2, imm):
    """JGE -- Jump if Greater than or Equal. Jumps if the last CMP's
    src1 >= src2.
    Usage:   JGE address / label  (use right after a CMP)
    Example: CMP A, B
             JGE a_not_smaller"""
    if not cpu.less_flag:
        cpu.pc = reg_or_imm(cpu, src1, imm)


@instruction("HALT")
def op_halt(cpu, dst, src1, src2, imm):
    """HALT -- stops the CPU. cpu.run()'s loop exits once this executes.
    Usage:   HALT
    Every program should end with this, or the CPU will just keep
    fetching (and likely crashing on) whatever bytes come after it."""
    cpu.halted = True


@instruction("PUSH")
def op_push(cpu, dst, src1, src2, imm):
    """PUSH -- push a register's value onto the stack.
    Usage:   PUSH src
    Example: PUSH A              -> stores A on the stack
    Moves the stack pointer (cpu.sp) DOWN by 4 bytes first, then writes
    the 32-bit value there -- so the stack grows toward lower addresses,
    same convention as most real CPUs. Always pair pushes/pops so the
    stack pointer ends up back where it started."""
    cpu.sp -= 4
    cpu.ram.write_word(cpu.sp, cpu.reg.read(src1))


@instruction("POP")
def op_pop(cpu, dst, src1, src2, imm):
    """POP -- pop the most recently pushed value off the stack into a
    register. Values come back out in LIFO order (last pushed, first
    popped) -- like a stack of plates.
    Usage:   POP dst
    Example: PUSH A
             PUSH B
             POP  C              -> C = B's old value (pushed last)
             POP  D              -> D = A's old value (pushed first)
    Reads the 32-bit value at the current stack pointer, then moves the
    stack pointer UP by 4 bytes (undoing one PUSH's worth of movement)."""
    cpu.reg.write(dst, cpu.ram.read_word(cpu.sp))
    cpu.sp += 4


# --------------------------------------------------------------------------
# Everything below is APPENDED. Opcodes are assigned in declaration order,
# so adding here is safe; inserting anywhere above renumbers every later
# opcode and invalidates every already-assembled .bin.
# --------------------------------------------------------------------------

@instruction("CALL")
def op_call(cpu, dst, src1, src2, imm):
    """CALL -- call a subroutine. Pushes the return address, then jumps.
    Usage:   CALL address / label
             CALL reg              (indirect, for function pointers)
    Example: CALL draw_pixel
             ...
             RET                   -> comes back here
    PC already points at the following instruction by the time a handler
    runs, so that is exactly the address to push."""
    cpu.sp -= 4
    cpu.ram.write_word(cpu.sp, cpu.pc)
    cpu.pc = reg_or_imm(cpu, src1, imm)


@instruction("RET")
def op_ret(cpu, dst, src1, src2, imm):
    """RET -- return from a subroutine. Pops the address CALL pushed.
    Usage:   RET
    Pair every CALL with exactly one RET, and leave the stack balanced in
    between, or RET will jump to whatever the last PUSH happened to be."""
    cpu.pc = cpu.ram.read_word(cpu.sp)
    cpu.sp += 4


@instruction("SHL")
def op_shl(cpu, dst, src1, src2, imm):
    """SHL -- Shift Left. dst = src1 << src2 (or << immediate).
    Usage:   SHL dst, src1, src2
             SHL dst, src1, #imm
    Example: SHL A, B, #2         -> A = B * 4, without a MUL
    Shifting by 32 or more yields 0; the result keeps the low 32 bits."""
    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    cpu.reg.write(dst, 0 if b >= 32 else a << b)


@instruction("SHR")
def op_shr(cpu, dst, src1, src2, imm):
    """SHR -- Shift Right (logical: zeros shift in at the top).
    Usage:   SHR dst, src1, src2
             SHR dst, src1, #imm
    Example: SHR A, B, #2         -> A = B / 4, without a DIV"""
    a = cpu.reg.read(src1)
    b = reg_or_imm(cpu, src2, imm)
    cpu.reg.write(dst, 0 if b >= 32 else a >> b)


# --------------------------------------------------------------------------
# Encoding helpers (used by the assembler)
# --------------------------------------------------------------------------

def encode(name, dst=NONE_REG, src1=NONE_REG, src2=NONE_REG, imm=0):
    instr = INSTRUCTIONS_BY_NAME[name]
    imm &= 0xFFFFFFFF
    return bytes([instr.opcode, dst, src1, src2]) + imm.to_bytes(4, "little")


def decode(chunk):
    opcode, dst, src1, src2 = chunk[0], chunk[1], chunk[2], chunk[3]
    imm = int.from_bytes(chunk[4:8], "little")
    return opcode, dst, src1, src2, imm


def disassemble(chunk, offset=0):
    """Format one 8-byte instruction as a line of assembly-ish text.

    The single copy of this -- main.py had two and test.py a third, all
    with their own drifting format strings.
    """
    opcode, dst, src1, src2, imm = decode(chunk)
    instr = INSTRUCTIONS_BY_OPCODE.get(opcode)
    name = instr.name if instr is not None else f"UNKNOWN({opcode})"

    def fmt(x):
        return '.' if x == NONE_REG else str(x)

    return (f"0x{offset:04x}: {name:<6} dst={fmt(dst):<3} src1={fmt(src1):<3} "
            f"src2={fmt(src2):<3} imm=0x{imm:08x}")


def disassemble_range(mem, start, length):
    """Disassemble `length` bytes of `mem`, one line per instruction."""
    return [disassemble(bytes(mem[o:o + INSTR_SIZE]), o)
            for o in range(start, start + length, INSTR_SIZE)]