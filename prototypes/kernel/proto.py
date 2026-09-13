"""Prototype for docs/kernel.md: interrupts, faults and stack-pointer
instructions, added at runtime. Nothing in the repository is modified.

New instructions (appended after SHR, so every existing opcode keeps its number):
    GETSP r      r = SP
    SETSP r|#    SP = value
    EI / DI      enable / disable interrupts
    IRET         pop PC, pop flags word (zero, less, interrupt-enable)
    SETIV r|#    address of the vector table

Entering an interrupt or fault: push flags word, push PC, clear IE, jump to
vector[n]. A fault pushes the faulting instruction's own address.
"""
import re
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from emulator import instruction_set as IS                      # noqa: E402
from emulator import cpu as cpu_mod                             # noqa: E402
from emulator.cpu import CPU, _UNPACK                           # noqa: E402
from emulator.ram import RAM                                    # noqa: E402
from emulator.memory_map import PROGRAM_LOAD_ADDR, RAM_SIZE, STACK_TOP  # noqa: E402,F401
from assembler import assembler as asm_mod                      # noqa: E402
from compiler.cc import compile_units                           # noqa: E402
from emulator.programs import libraries_for                     # noqa: E402

OLD_OPCODE_COUNT = len(IS.INSTRUCTIONS_BY_OPCODE)


# --------------------------------------------------------------- instructions
@IS.instruction("GETSP")
def op_getsp(cpu, dst, src1, src2, imm):
    cpu.reg.values[dst] = cpu.sp


@IS.instruction("SETSP")
def op_setsp(cpu, dst, src1, src2, imm):
    cpu.sp = IS.reg_or_imm(cpu, src1, imm)


@IS.instruction("EI")
def op_ei(cpu, dst, src1, src2, imm):
    cpu.ie = True


@IS.instruction("DI")
def op_di(cpu, dst, src1, src2, imm):
    cpu.ie = False


@IS.instruction("IRET")
def op_iret(cpu, dst, src1, src2, imm):
    ram = cpu.ram
    cpu.pc = ram.read_word(cpu.sp)
    flags = ram.read_word(cpu.sp + 4)
    cpu.sp += 8
    if cpu.restore_flags:                 # False only for the negative control
        cpu.zero_flag = bool(flags & 1)
        cpu.less_flag = bool(flags & 2)
    cpu.ie = bool(flags & 4)


@IS.instruction("SETIV")
def op_setiv(cpu, dst, src1, src2, imm):
    cpu.ivt = IS.reg_or_imm(cpu, src1, imm)


for _op, _ins in IS.INSTRUCTIONS_BY_OPCODE.items():
    cpu_mod._HANDLERS[_op] = _ins.handler
asm_mod.SYNTAX.update({
    "GETSP": [("dst", "reg")],
    "SETSP": [("src1", "reg_or_imm")],
    "EI": [], "DI": [], "IRET": [],
    "SETIV": [("src1", "reg_or_imm")],
})
IRET_OP = IS.INSTRUCTIONS_BY_NAME["IRET"].opcode

# vector numbers
DIV0, BADOP, BADFETCH, TIMER, BREAK = 0, 1, 2, 3, 4


class Unhandled(Exception):
    pass


def new_cpu(ram):
    cpu = CPU(ram)
    cpu.ie = False
    cpu.ivt = 0
    cpu.pending = 0
    cpu.restore_flags = True
    return cpu


def enter(cpu, vector, return_pc):
    ram = cpu.ram
    flags = (1 if cpu.zero_flag else 0) | (2 if cpu.less_flag else 0) | (4 if cpu.ie else 0)
    cpu.sp -= 4
    ram.write_word(cpu.sp, flags)
    cpu.sp -= 4
    ram.write_word(cpu.sp, return_pc)
    cpu.ie = False
    cpu.pc = ram.read_word(cpu.ivt + 4 * vector)


def fault(cpu, vector, pc):
    if cpu.ivt == 0:
        raise Unhandled(f"fault {vector} at {pc:#x} and no vector table")
    enter(cpu, vector, pc)


def run(cpu, io=None, limit=60_000_000, tick=None):
    """Run to HALT. tick(cpu, opcode) runs after each instruction and may
    raise interrupts by setting bits in cpu.pending."""
    ram = cpu.ram
    mem = ram.mem
    handlers = cpu_mod._HANDLERS
    n = 0
    while not cpu.halted:
        if n >= limit:
            raise Unhandled(f"no HALT within {limit:,} instructions, PC={cpu.pc:#x}")
        if cpu.pending and cpu.ie:
            vector = (cpu.pending & -cpu.pending).bit_length() - 1
            cpu.pending &= ~(1 << vector)
            enter(cpu, vector, cpu.pc)
        pc = cpu.pc
        n += 1
        try:
            opcode, dst, src1, src2, imm = _UNPACK(mem, pc)
        except struct.error:
            fault(cpu, BADFETCH, pc)
            continue
        cpu.pc = pc + 8
        handler = handlers[opcode]
        if handler is None:
            fault(cpu, BADOP, pc)
            continue
        try:
            handler(cpu, dst, src1, src2, imm)
        except ZeroDivisionError:
            fault(cpu, DIV0, pc)
            continue
        if ram.io_pending and io is not None:
            io.update()
        if tick is not None:
            tick(cpu, opcode)
    return n


def every(k, vector=TIMER):
    """Raise `vector` after every k-th instruction that runs with interrupts
    enabled. IRET never counts, so at least one interrupted instruction runs
    between two interrupts even at k=1."""
    state = {"count": 0, "raised": 0}

    def tick(cpu, opcode):
        if cpu.ie and opcode != IRET_OP:
            state["count"] += 1
            if state["count"] % k == 0:
                cpu.pending |= 1 << vector
                state["raised"] += 1
    tick.state = state
    return tick


# --------------------------------------------------------------------- builds
def asm_from_c(sources):
    """[(filename, text)] -> assembly, with the libraries their includes name."""
    with tempfile.TemporaryDirectory() as d:
        units = []
        for name, text in sources:
            p = Path(d) / name
            p.write_text(text)
            units.append(p)
        for p in list(units):
            for lib in libraries_for(p):
                if lib not in units:
                    units.append(lib)
        return compile_units(units)


def insert_code(asm, extra):
    """Hand-written routines go before the static data, among the code."""
    marker = asm.index("; --- static data ---")
    return asm[:marker] + extra.strip("\n") + "\n\n" + asm[marker:]


def assemble(asm, org):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.asm"
        p.write_text(asm.replace(".ORG PROGRAM_LOAD_ADDR", f".ORG {org:#x}", 1))
        a = asm_mod.Assembler(str(p))
        image = a.assemble()
        return image, dict(a.symbols)


def build_fixed(c_text, extra_asm="", org=PROGRAM_LOAD_ADDR):
    """A program at a fixed address with today's startup code (ends in HALT)."""
    asm = asm_from_c([("k.c", c_text)])
    if extra_asm:
        asm = insert_code(asm, extra_asm)
    return assemble(asm, org)


APP_START = """__start:
    MRW A, F
    ADD C, F, #4
    MRW B, C
    PUSH F
    MOV F, #__image_end
    MWW F, A
    ADD C, F, #4
    MWW C, B
    MOV C, #__heap_ptr
    MOV A, #(__image_end + FRAME_BYTES)
    MWW C, A
    CALL main
    POP F
    RET"""

LINK_A, LINK_B = 0x00400000, 0x00701238
MAGIC = 0x31584750          # "PGX1"
HEADER_WORDS = 8


def build_app(c_text, frame_bytes=262144, extra_asm=""):
    """A relocatable program file: header, image, list of offsets to patch."""
    asm = asm_from_c([("app.c", c_text)])
    start = asm.index("__start:")
    end = asm.index("HALT", start) + len("HALT")
    asm = asm[:start] + APP_START.replace("FRAME_BYTES", str(frame_bytes)) + asm[end:]
    if extra_asm:
        asm = insert_code(asm, extra_asm)
    lines = [l for l in asm.splitlines()
             if not re.match(r"\s*__(frame_base|frame_limit|heap_base)\s*=", l)]
    asm = "\n".join(lines) + "\n__image_end:\n"
    asm = re.sub(r"\b__heap_base\b", f"(__image_end + {frame_bytes})", asm)
    asm = re.sub(r"\b__frame_base\b", "__image_end", asm)

    a, sym = assemble(asm, LINK_A)
    b, _ = assemble(asm, LINK_B)
    assert len(a) == len(b)
    delta = LINK_B - LINK_A
    relocs, covered = [], set()
    for off in range(0, len(a) - 3, 4):
        wa = struct.unpack_from("<I", a, off)[0]
        wb = struct.unpack_from("<I", b, off)[0]
        if wa != wb:
            assert (wb - wa) & 0xFFFFFFFF == delta, f"not an address at {off:#x}"
            relocs.append(off)
            covered.update(range(off, off + 4))
    assert all(a[i] == b[i] for i in range(len(a)) if i not in covered)
    image = a + bytes((-len(a)) % 4)
    header = struct.pack("<8I", MAGIC, 1, len(image), sym["__start"] - LINK_A,
                         len(relocs), frame_bytes, sym["__heap_ptr"] - LINK_A, LINK_A)
    return header + image + struct.pack(f"<{len(relocs)}I", *relocs)


def load_app(ram, blob, base):
    """Host-side loader, used where the kernel under test isn't the loader."""
    magic, _, size, entry, nrel, frame, heap_ptr, link = struct.unpack_from("<8I", blob, 0)
    assert magic == MAGIC
    image = bytearray(blob[32:32 + size])
    for i in range(nrel):
        off = struct.unpack_from("<I", blob, 32 + size + 4 * i)[0]
        w = struct.unpack_from("<I", image, off)[0]
        struct.pack_into("<I", image, off, (w + base - link) & 0xFFFFFFFF)
    ram.load_bytes(bytes(image), base)
    return {"entry": base + entry, "heap_ptr": base + heap_ptr, "end": base + size,
            "frame": frame}


def c_string(ram, addr, limit=1 << 16):
    out = bytearray()
    while len(out) < limit:
        b = ram.read_byte(addr + len(out))
        if b == 0:
            break
        out.append(b)
    return out.decode("latin-1")
