#!/usr/bin/env python3
"""
Pigeon Emulator Assembler

Two-pass assembler:
  Pass 1: Preprocess (strip comments, evaluate static defs, collect labels)
  Pass 2: Assemble (parse mnemonics, resolve operands, encode to binary)

Usage:
  python assembler/assembler.py <source.asm> [output.bin]

Every UPPERCASE integer in emulator/memory_map.py is predefined as a
symbol here, so sources can write DISPLAY_START instead of retyping
0x1418. A source may still define its own value for one of those names,
but doing so with a *different* value prints a warning -- that is how
user/ui.asm silently drifted to a stale display address and spent its
life drawing into the IO region.
"""

import ast
import operator
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
from pathlib import Path

# emulator/ lives one level up from assembler/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emulator import memory_map
from emulator.instruction_set import INSTRUCTIONS_BY_NAME, NONE_REG, encode
from emulator.instruction_set import INSTR_SIZE
from emulator.memory_map import PROGRAM_MAX_SIZE, REGISTER_COUNT

# Register name to index mapping (A=0, B=1, ...), capped at what the CPU
# actually has. Encoding a register the machine lacks used to assemble
# cleanly and only fail at runtime.
REG_NAME_TO_IDX = {chr(ord('A') + i): i for i in range(REGISTER_COUNT)}


# Every uppercase int in the memory map, plus the IO header offsets. The
# rule lives in memory_map.symbols() rather than here so the C compiler
# can inject exactly the same names as macros -- see compiler/cc.py.
BUILTIN_SYMBOLS: Dict[str, int] = memory_map.symbols()

LABEL_RE = re.compile(r'^([A-Za-z_.$][\w.$]*)\s*:\s*')
DIRECTIVE_RE = re.compile(r'^(\.\w+)\s*(.*)$', re.DOTALL)
STATIC_DEF_RE = re.compile(r'^(\w+)\s*=\s*(.+)$')

_ESCAPES = {'n': 10, 't': 9, 'r': 13, '0': 0, '\\': 92, '"': 34, "'": 39,
            'a': 7, 'b': 8, 'f': 12, 'v': 11, 'e': 27}


def _strip_comment(text: str) -> str:
    """Cut at the first ';' that is not inside a double-quoted string.

    `.asciz "a;b"` is a four-byte string, not a two-byte one followed by a
    comment. Only '"' opens a string, so an apostrophe in a comment stays
    ordinary text.
    """
    out, i, n, in_string = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == '\\' and i + 1 < n:      # a backslash-escaped quote
                out.append(text[i:i + 2]); i += 2; continue
            if ch == '"':
                in_string = False
        elif ch == ';':
            break
        elif ch == '"':
            in_string = True
        out.append(ch)
        i += 1
    return ''.join(out)


def _split_data_operands(text: str) -> List[str]:
    """Split on commas outside quotes and parentheses.

    Not _split_operands(): that one groups on '#' boundaries for the
    instruction syntax and would mangle `.byte 1, -2`. Data operands
    require commas, so `.byte 1 2` is an error rather than one silent value.
    """
    operands, current, depth, in_string = [], [], 0, False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            current.append(ch)
            if ch == '\\' and i + 1 < len(text):
                current.append(text[i + 1]); i += 2; continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True; current.append(ch)
        elif ch in '([':
            depth += 1; current.append(ch)
        elif ch in ')]':
            depth -= 1; current.append(ch)
        elif ch == ',' and depth == 0:
            operands.append(''.join(current).strip()); current = []
        else:
            current.append(ch)
        i += 1
    if in_string:
        raise ValueError("unterminated string literal")
    tail = ''.join(current).strip()
    if tail:
        operands.append(tail)
    if any(not op for op in operands):
        raise ValueError("empty operand (a stray or doubled comma?)")
    return operands


def _decode_string(text: str) -> bytes:
    """Decode a double-quoted literal into raw bytes.

    Supports \\n \\t \\r \\0 \\\\ \\" \\a \\b \\f \\v \\e, \\xNN (exactly two hex
    digits -- a variable-length \\x is a footgun), and 1-3 digit octal so a
    C compiler's \\012 means what C says. An unknown escape raises: silently
    dropping the backslash is how a table ends up one byte short.
    """
    text = text.strip()
    if len(text) < 2 or not text.startswith('"') or not text.endswith('"'):
        raise ValueError(f'expected a double-quoted string, got {text!r}')
    body, out, i = text[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != '\\':
            out += ch.encode('utf-8'); i += 1; continue
        i += 1
        if i >= len(body):
            raise ValueError("string ends with a lone backslash")
        esc = body[i]
        if esc == 'x':
            digits = body[i + 1:i + 3]
            if len(digits) != 2 or not all(c in '0123456789abcdefABCDEF' for c in digits):
                raise ValueError(r"\x needs exactly two hex digits")
            out.append(int(digits, 16)); i += 3
        elif esc in '01234567' and body[i:i + 2].isdigit() and len(body[i:i + 2]) == 2:
            octal = body[i:i + 3] if body[i:i + 3].isdigit() else body[i:i + 2]
            out.append(int(octal, 8) & 0xFF); i += len(octal)
        elif esc in _ESCAPES:
            out.append(_ESCAPES[esc]); i += 1
        else:
            raise ValueError(f"unknown escape \\{esc}")
    return bytes(out)


def _pack_int(value: int, width: int, where: str) -> bytes:
    """Range-check then pack little-endian.

    _eval_expr masks to 32 bits, so -1 arrives as 0xFFFFFFFF and a negative
    is indistinguishable from a large positive. For a byte, accept 0..0xFF
    or a sign-extended -128..-1; `.byte 256` should be an error, not a
    silently truncated table entry.
    """
    if width == 1:
        if 0 <= value <= 0xFF:
            return bytes((value,))
        if value >= 0xFFFFFF80:                  # sign-extended -128..-1
            return bytes((value & 0xFF,))
        raise ValueError(f".byte value out of range: {where!r} = {value} "
                         f"({value:#x}); a byte holds 0..255 or -128..-1")
    return (value & 0xFFFFFFFF).to_bytes(width, "little")



# --------------------------------------------------------------------------
# Operand syntax templates: one row per mnemonic, describing the *ordered*
# list of operand slots it expects in assembly source.
#
#   ("dst",  "reg")         -- token must be a plain register -> dst
#   ("src1", "reg")         -- token must be a plain register -> src1
#   ("src1", "reg_or_imm")  -- register OR immediate -> src1 / imm
#   ("src2", "reg_or_imm")  -- register OR immediate -> src2 / imm
#   ("imm",  "imm")         -- always a literal/label address -> imm
#
# This is explicit (like size/opcode already are in instruction_set.py)
# because operand *order* and *role* aren't recoverable from the flat
# {dst,src1,src2,imm} set alone -- e.g. MW's first operand is a register,
# but it holds an *address*, not a "destination".
# --------------------------------------------------------------------------
SYNTAX = {
    "NOP":  [],
    "HALT": [],

    "MOV":  [("dst", "reg"), ("src1", "reg_or_imm")],

    "ADD":  [("dst", "reg"), ("src1", "reg"), ("src2", "reg_or_imm")],
    "SUB":  [("dst", "reg"), ("src1", "reg"), ("src2", "reg_or_imm")],
    "MUL":  [("dst", "reg"), ("src1", "reg"), ("src2", "reg_or_imm")],
    "DIV":  [("dst", "reg"), ("src1", "reg"), ("src2", "reg_or_imm")],

    "OR":  [("dst", "reg"), ("src1", "reg"), ("src2", "reg_or_imm")],
    "AND":  [("dst", "reg"), ("src1", "reg"), ("src2", "reg_or_imm")],
    "XOR":  [("dst", "reg"), ("src1", "reg"), ("src2", "reg_or_imm")],
    "NOT":  [("dst", "reg"), ("src1", "reg_or_imm")],

    "MR":   [("dst", "reg"), ("src1", "reg_or_imm")],   # MR dst, [addr]
    "MRW":  [("dst", "reg"), ("src1", "reg_or_imm")],   # MRW dst, [addr]

    # NOTE: op_mw / op_mww currently do `cpu.reg.read(dst)` for the address
    # -- register only, no immediate addresses -- so the first slot here is
    # "reg", not "reg_or_imm". If you revert those handlers to use
    # reg_or_imm(cpu, dst, imm) for the address (matching the docstrings'
    # `MW [0xF000], A` example), change this row's first slot to
    # ("dst", "reg_or_imm") to match.
    "MW":   [("dst", "reg"), ("src1", "reg_or_imm")],   # MW [addr_reg], src
    "MWW":  [("dst", "reg"), ("src1", "reg_or_imm")],   # MWW [addr_reg], src

    "CMP":  [("src1", "reg"), ("src2", "reg_or_imm")],  # no dst

    "JMP":  [("src1", "reg_or_imm")],
    "JZ":   [("src1", "reg_or_imm")],
    "JNZ":  [("src1", "reg_or_imm")],
    "JL":   [("src1", "reg_or_imm")],
    "JG":   [("src1", "reg_or_imm")],
    "JLE":  [("src1", "reg_or_imm")],
    "JGE":  [("src1", "reg_or_imm")],

    "PUSH": [("src1", "reg")],
    "POP":  [("dst", "reg")],

    "CALL": [("src1", "reg_or_imm")],
    "RET":  [],
    "SHL":  [("dst", "reg"), ("src1", "reg"), ("src2", "reg_or_imm")],
    "SHR":  [("dst", "reg"), ("src1", "reg"), ("src2", "reg_or_imm")],
}

# Mnemonics whose 3-operand form may be written with 2, duplicating the
# destination into src1: `ADD C #1` means `ADD C C #1`. Long documented
# and used by assembler/example.asm; only the since-deleted ass.py ever
# implemented it.
SHORTHAND = {"ADD", "SUB", "MUL", "DIV", "OR", "AND", "XOR", "SHL", "SHR"}


_missing = set(INSTRUCTIONS_BY_NAME) - set(SYNTAX)
if _missing:
    raise RuntimeError(f"Instructions with no SYNTAX row: {sorted(_missing)}")



# --------------------------------------------------------------------------
# One parse, one size, one emission.
#
# Pass 1 assigns addresses from each item's size; pass 2 asks the same item
# for its bytes. If the two ever disagreed, every label after the offending
# line would be silently wrong and the program would fail somewhere else
# entirely -- the worst thing to debug on a machine whose only tool is a
# single-stepper. So a line is parsed EXACTLY once, into an Item that knows
# both, and _emit() checks the two halves against each other.
#
# The rule for subclasses: anything emit() cannot recompute from the parsed
# fields alone -- a pad length, a reservation size -- must be cached on self
# during place(), never recalculated.
# --------------------------------------------------------------------------

@dataclass
class Item:
    line_num: int          # ORIGINAL source line number, for error messages
    text: str              # cleaned source text, for error messages
    addr: int = 0          # assigned by _layout
    size: int = 0          # assigned by _layout; emit() must match exactly

    def place(self, asm: "Assembler", addr: int) -> int:
        """Pass 1: return this item's byte count. Called once, in order."""
        return 0

    def emit(self, asm: "Assembler") -> bytes:
        """Pass 2: return exactly self.size bytes."""
        return b""


@dataclass
class Label(Item):
    name: str = ""

    def place(self, asm, addr):
        if self.name in asm.symbols:
            raise ValueError(
                f"Duplicate label: {self.name} (already at "
                f"{asm.symbols[self.name]:#x}). It used to be accepted "
                f"silently, with the last one winning.")
        asm.symbols[self.name] = addr
        return 0


@dataclass
class Instr(Item):
    def place(self, asm, addr):
        if addr % INSTR_SIZE:
            raise ValueError(
                f"instruction lands at {addr:#x}, which is not a multiple of "
                f"{INSTR_SIZE} -- data earlier in the file left the address "
                f"odd. Put '.align {INSTR_SIZE}' before this code.")
        return INSTR_SIZE

    def emit(self, asm):
        return asm._encode_instruction(self.text)


@dataclass
class Ints(Item):
    """.byte / .word -- size depends on operand COUNT, never on their values."""
    width: int = 1
    exprs: List[str] = field(default_factory=list)

    def place(self, asm, addr):
        return self.width * len(self.exprs)

    def emit(self, asm):
        return b"".join(_pack_int(asm._eval_expr(e), self.width, e)
                        for e in self.exprs)


@dataclass
class Raw(Item):
    """.ascii / .asciz -- the bytes are decoded at parse time."""
    data: bytes = b""

    def place(self, asm, addr):
        return len(self.data)

    def emit(self, asm):
        return self.data


@dataclass
class Space(Item):
    """.space N -- N zero bytes, actually written to the image."""
    expr: str = ""
    count: int = 0

    def place(self, asm, addr):
        self.count = asm._eval_expr(self.expr)
        if self.count > PROGRAM_MAX_SIZE:
            raise ValueError(
                f".space {self.count} is larger than the whole program area "
                f"({PROGRAM_MAX_SIZE} bytes) -- a negative count masks to a "
                f"huge unsigned value, so check for that.")
        return self.count

    def emit(self, asm):
        return b"\x00" * self.count


@dataclass
class Align(Item):
    """.align N -- pad with zeros up to the next N-byte boundary.

    N is a byte count, not a power (`.align 8`, not `.align 3`). Padding is
    real bytes in the file, not a gap: the image is DMA'd contiguously to
    PROGRAM_LOAD_ADDR, so file offset and runtime address must stay in step.
    """
    expr: str = ""
    count: int = 0

    def place(self, asm, addr):
        n = asm._eval_expr(self.expr)
        if n == 0 or n & (n - 1):
            raise ValueError(f".align takes a power-of-two byte boundary, got {n}")
        self.count = (-addr) % n
        return self.count

    def emit(self, asm):
        return b"\x00" * self.count


class Assembler:
    def __init__(self, source_file: str):
        """Load source assembly file."""
        with open(source_file, 'r') as f:
            self.source_lines = f.readlines()
        self.symbols: Dict[str, int] = {}  # label name -> address
        self.warnings: List[str] = []
        self.static_defs: Dict[str, int] = {}  # static definition name -> value
        self.org_addr = 0  # origin address
        self.instructions: List[bytes] = []  # collected instructions
        self.current_addr = 0  # current write address

    def assemble(self) -> bytes:
        """Parse once, lay out, emit."""
        self.symbols.clear()
        lines = self._preprocess()          # [(source line number, text)]
        items = self._parse_items(lines)    # parsed EXACTLY once
        self._layout(items)                 # pass 1: addresses, sizes, labels
        return self._emit(items)            # pass 2: bytes

    def _layout(self, items: List[Item]) -> None:
        addr = self.org_addr
        for item in items:
            item.addr = addr
            try:
                item.size = item.place(self, addr)
            except Exception as e:
                raise ValueError(f"Line {item.line_num}: {item.text}\n  Error: {e}") from None
            addr += item.size
        self.end_addr = addr

    def _emit(self, items: List[Item]) -> bytes:
        """Pass 2, plus the two checks that are the whole point of Item.

        The output is a flat image DMA'd contiguously to PROGRAM_LOAD_ADDR,
        so one invariant must hold for every byte:

            file_offset == addr - org_addr

        If a line's pass-1 size ever disagrees with its pass-2 bytes that
        breaks, every later label becomes a lie, and the program fails
        somewhere unrelated. Catch it here instead.
        """
        out = bytearray()
        for item in items:
            try:
                chunk = item.emit(self)
            except Exception as e:
                raise ValueError(f"Line {item.line_num}: {item.text}\n  Error: {e}") from None

            if len(chunk) != item.size:
                raise AssertionError(
                    f"assembler bug, line {item.line_num} ({item.text!r}): pass 1 "
                    f"sized this {item.size} bytes, pass 2 emitted {len(chunk)}")
            if self.org_addr + len(out) != item.addr:
                raise AssertionError(
                    f"assembler bug, line {item.line_num} ({item.text!r}): laid out "
                    f"at {item.addr:#x} but lands at file offset "
                    f"{self.org_addr + len(out):#x}")
            out += chunk

        self.instructions = [bytes(out)]
        return bytes(out)

    def _parse_items(self, lines: List[Tuple[int, str]]) -> List[Item]:
        items: List[Item] = []
        for line_num, text in lines:
            match = LABEL_RE.match(text)
            if match:
                items.append(Label(line_num, text, name=match.group(1)))
                text = text[match.end():].strip()
                if not text:
                    continue
            directive = DIRECTIVE_RE.match(text)
            if directive:
                try:
                    items.append(self._parse_directive(
                        line_num, text, directive.group(1).lower(), directive.group(2)))
                except Exception as e:
                    raise ValueError(f"Line {line_num}: {text}\n  Error: {e}") from None
            else:
                items.append(Instr(line_num, text))
        return items

    def _parse_directive(self, line_num: int, text: str, name: str, args: str) -> Item:
        if name in (".byte", ".word"):
            width = 1 if name == ".byte" else 4
            exprs = _split_data_operands(args)
            if not exprs:
                raise ValueError(f"{name} needs at least one value")
            return Ints(line_num, text, width=width, exprs=exprs)

        if name in (".ascii", ".asciz", ".string"):
            terminate = name != ".ascii"
            operands = _split_data_operands(args)
            if not operands:
                raise ValueError(f"{name} needs a string")
            data = b"".join(_decode_string(op) + (b"\0" if terminate else b"")
                            for op in operands)
            return Raw(line_num, text, data=data)

        if name == ".space":
            if not args.strip():
                raise ValueError(".space needs a byte count")
            return Space(line_num, text, expr=args.strip())

        if name == ".align":
            if not args.strip():
                raise ValueError(".align needs a boundary, e.g. '.align 8'")
            return Align(line_num, text, expr=args.strip())

        raise ValueError(f"Unknown directive: {name}")

    def _preprocess(self) -> List[Tuple[int, str]]:
        """Strip comments, fix the origin, collect static definitions.

        Returns (source line number, text) so errors can name the real
        line. They used to be numbered by position in this filtered list,
        so a bad line 30 was reported as "Line 3".

        Directives other than .ORG deliberately fall through untouched --
        _parse_items owns the directive table, so nothing here can eat one.
        """
        lines: List[Tuple[int, str]] = []
        emitted_any = False

        for source_line_num, raw in enumerate(self.source_lines, 1):
            line = _strip_comment(raw).strip()
            if not line:
                continue

            if line.upper().startswith('.ORG'):
                if emitted_any:
                    raise ValueError(
                        f"Line {source_line_num}: .ORG must come before any code or "
                        f"data. The output is one contiguous image, so it cannot "
                        f"jump -- use .space or .align to leave a hole.")
                argument = line[4:].strip()
                if not argument:
                    raise ValueError(f"Line {source_line_num}: .ORG needs an address")
                # Evaluated, not regex-matched: `.ORG PROGRAM_LOAD_ADDR` used
                # to fail the literal-only regex and be silently dropped,
                # leaving the origin at 0 and every label wrong.
                self.org_addr = self.current_addr = self._eval_expr(argument)
                continue

            # Static definition: NAME = expression. Checked before the label
            # rule and anchored, so `.asciz "a: b"` is not mistaken for either.
            definition = STATIC_DEF_RE.match(line)
            if definition and not LABEL_RE.match(line):
                name, expr = definition.group(1), definition.group(2).strip()
                value = self._eval_expr(expr)
                builtin = BUILTIN_SYMBOLS.get(name)
                if builtin is not None and builtin != value:
                    self.warnings.append(
                        f"{name} is redefined as {value:#x}, but the machine's "
                        f"memory map says {builtin:#x} -- this source has drifted")
                self.static_defs[name] = value
                continue

            lines.append((source_line_num, line))
            emitted_any = True

        return lines

    # Only these node types and operators survive the AST walk below.
    _BINOPS = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.FloorDiv: operator.floordiv, ast.Div: operator.floordiv,
        ast.Mod: operator.mod, ast.LShift: operator.lshift,
        ast.RShift: operator.rshift, ast.BitOr: operator.or_,
        ast.BitAnd: operator.and_, ast.BitXor: operator.xor,
    }
    _UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg,
                 ast.Invert: operator.invert}

    def _eval_expr(self, expr: str) -> int:
        """Evaluate a constant expression: + - * / % << >> | & ^ and symbols.

        Walks the AST rather than calling eval(). The previous version ran
        eval() on text straight out of the .asm file; a stray `X = 9**9**9`
        would hang the assembler forever computing a giant integer, and the
        only thing standing between it and arbitrary code was a regex that
        happened to reject bare identifiers.
        """
        expr = expr.strip()
        # '#' marks an immediate in instruction operands; it carries no
        # meaning inside an expression.
        expr = expr.replace('#', '')

        # A bare symbol resolves directly. ast.parse cannot handle the
        # dotted names compilers emit for local labels (.L3), and it would
        # reject them as a syntax error rather than look them up.
        bare = expr.strip()
        if bare and (bare in self.static_defs or bare in self.symbols
                     or bare in BUILTIN_SYMBOLS):
            return self._lookup_symbol(bare) & 0xFFFFFFFF

        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as e:
            raise ValueError(f"Cannot parse expression '{expr}': {e}") from None

        return self._eval_node(tree.body, expr) & 0xFFFFFFFF

    def _eval_node(self, node, expr: str) -> int:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int):
                raise ValueError(f"Only integers are allowed in '{expr}', "
                                 f"got {node.value!r}")
            return node.value

        if isinstance(node, ast.Name):
            return self._lookup_symbol(node.id)

        if isinstance(node, ast.BinOp):
            op = self._BINOPS.get(type(node.op))
            if op is None:
                raise ValueError(f"Operator not allowed in '{expr}': "
                                 f"{type(node.op).__name__}")
            right = self._eval_node(node.right, expr)
            if op in (operator.floordiv, operator.mod) and right == 0:
                raise ValueError(f"Division by zero in '{expr}'")
            if op in (operator.lshift, operator.rshift) and not 0 <= right < 64:
                raise ValueError(f"Shift out of range in '{expr}': {right}")
            return op(self._eval_node(node.left, expr), right)

        if isinstance(node, ast.UnaryOp):
            op = self._UNARYOPS.get(type(node.op))
            if op is None:
                raise ValueError(f"Operator not allowed in '{expr}': "
                                 f"{type(node.op).__name__}")
            return op(self._eval_node(node.operand, expr))

        raise ValueError(f"Not allowed in an expression '{expr}': "
                         f"{type(node).__name__}")

    def _lookup_symbol(self, name: str) -> int:
        if name in self.static_defs:
            return self.static_defs[name]
        if name in self.symbols:
            return self.symbols[name]
        if name in BUILTIN_SYMBOLS:
            return BUILTIN_SYMBOLS[name]
        raise ValueError(f"Undefined symbol: {name}")

    def _encode_instruction(self, text: str) -> bytes:
        """Encode one instruction line. Callers have already stripped any
        label, so `text` starts at the mnemonic."""
        tokens = text.split()
        mnemonic = tokens[0].upper()
        if mnemonic not in INSTRUCTIONS_BY_NAME:
            raise ValueError(f"Unknown mnemonic: {mnemonic}")

        dst, src1, src2, imm = self._parse_operands(mnemonic, ' '.join(tokens[1:]))
        return encode(mnemonic, dst=dst, src1=src1, src2=src2, imm=imm)

    def _split_operands(self, operand_str: str) -> List[str]:
        """
        Split operand string into individual operands, handling both simple and complex cases.
        - "A B C" -> ["A", "B", "C"]
        - "A, B, C" -> ["A", "B", "C"]
        - "#IO_POINTER + #IO_COMMAND #2" -> ["#IO_POINTER + #IO_COMMAND", "#2"]
        """
        operand_str = operand_str.strip()

        # If there are explicit commas, use them
        if ',' in operand_str:
            return [op.strip() for op in operand_str.split(',')]

        # No commas: smart split for expressions vs simple operands
        # If no operators, just split on whitespace
        if not any(op in operand_str for op in ['+', '-', '*', '/']):
            return operand_str.split()

        # Has operators: need to group them with their operands
        # Strategy: split on # boundaries (marks new operand)
        # "#IO_POINTER + #IO_COMMAND #2" -> ["#IO_POINTER + #IO_COMMAND", "#2"]
        tokens = []
        current_operand = []

        parts = operand_str.split()
        for part in parts:
            # If this token starts with # and current_operand is non-empty
            # and the previous part wasn't an operator, start a new operand
            if (part.startswith('#') and current_operand and
                    current_operand[-1] not in ['+', '-', '*', '/']):
                tokens.append(' '.join(current_operand))
                current_operand = [part]
            else:
                current_operand.append(part)

        if current_operand:
            tokens.append(' '.join(current_operand))

        return tokens

    def _parse_operands(self, mnemonic: str, operand_str: str) -> Tuple[int, int, int, int]:
        """
        Parse operands according to this mnemonic's syntax template (see
        SYNTAX above). Returns (dst, src1, src2, imm) with NONE_REG for
        unused register slots.
        """
        if mnemonic not in INSTRUCTIONS_BY_NAME:
            raise ValueError(f"Unknown mnemonic: {mnemonic}")
        if mnemonic not in SYNTAX:
            raise ValueError(f"No syntax template defined for {mnemonic} -- add one to SYNTAX")

        template = SYNTAX[mnemonic]
        tokens = self._split_operands(operand_str)

        # Shorthand: `ADD C #1` -> `ADD C C #1`. Only when exactly one
        # operand is missing from a 3-slot arithmetic instruction, and only
        # when the destination is a register we can duplicate.
        if (mnemonic in SHORTHAND and len(template) == 3
                and len(tokens) == 2 and tokens):
            tokens = [tokens[0], tokens[0], tokens[1]]

        if len(tokens) != len(template):
            expected = ", ".join(f"<{field}>" for field, _ in template)
            raise ValueError(
                f"{mnemonic} expects {len(template)} operand(s) ({expected}), "
                f"got {len(tokens)}: {tokens}"
            )

        dst = src1 = src2 = NONE_REG
        imm = 0

        for (field, kind), token in zip(template, tokens):
            if kind == "reg":
                value = self._parse_operand(token)
            elif kind == "reg_or_imm":
                value, imm_val = self._parse_operand_or_imm(token)
                if value == NONE_REG:
                    imm = imm_val
            elif kind == "imm":
                imm = self._parse_imm(token)
                continue  # pure-imm slots have no register field to fill
            else:
                raise ValueError(f"Unknown operand kind '{kind}' for {mnemonic}")

            if field == "dst":
                dst = value
            elif field == "src1":
                src1 = value
            elif field == "src2":
                src2 = value

        return dst, src1, src2, imm

    def _parse_imm(self, operand: str) -> int:
        """Parse a pure immediate/address operand -- used for jump targets.
        Always evaluated as a literal or label, never as a register, so a
        label that happens to share a name with a register (e.g. a label
        called 'A') still resolves correctly."""
        return self._eval_expr(operand.strip())

    def _parse_operand(self, operand: str) -> int:
        """
        Parse a single operand (register or label reference).
        Returns register index (0-25) or raises error.
        """
        operand = operand.strip()

        # Remove bracket notation [...]
        if operand.startswith('[') and operand.endswith(']'):
            operand = operand[1:-1].strip()

        # Remove # prefix if present
        if operand.startswith('#'):
            operand = operand[1:].strip()

        # Check if it's a register
        name = operand.upper()
        if name in REG_NAME_TO_IDX:
            return REG_NAME_TO_IDX[name]
        if len(name) == 1 and name.isalpha():
            raise ValueError(
                f"Register {name} does not exist: this CPU has {REGISTER_COUNT} "
                f"registers (A-{chr(ord('A') + REGISTER_COUNT - 1)})")

        raise ValueError(f"Expected register, got: {operand}")

    def _parse_operand_or_imm(self, operand: str) -> Tuple[int, int]:
        """
        Parse an operand that can be either a register or an immediate value.
        Returns (reg_idx, imm_value) where:
          - If register: (reg_idx, 0)
          - If immediate: (NONE_REG, value)
        """
        operand = operand.strip()

        # Remove bracket notation [...]
        if operand.startswith('[') and operand.endswith(']'):
            operand = operand[1:-1].strip()

        # Check if it starts with # (immediate marker)
        if operand.startswith('#'):
            operand = operand[1:].strip()
            # Evaluate the expression
            value = self._eval_expr(operand)
            return NONE_REG, value

        # Check if it's a register
        if operand.upper() in REG_NAME_TO_IDX:
            # A label with the same name as a register used to lose
            # silently: `JMP A` became a register-indirect jump through A
            # rather than a jump to the label A.
            if operand in self.symbols:
                raise ValueError(
                    f"'{operand}' is both a register and a label (defined at "
                    f"{self.symbols[operand]:#x}) -- rename the label; "
                    f"A-{chr(ord('A') + REGISTER_COUNT - 1)} are register names")
            return REG_NAME_TO_IDX[operand.upper()], 0

        # Otherwise try to parse as immediate (no # prefix)
        try:
            value = self._eval_expr(operand)
            return NONE_REG, value
        except Exception:
            raise ValueError(f"Invalid operand: {operand}")


PROTECTED_SUFFIXES = {".py", ".asm", ".c", ".h", ".md"}


def assemble_file(source_file, output_file=None, quiet=False):
    """Assemble `source_file`, writing to `output_file`.

    Refuses to write over a source file. Five 1 MB files of pure null
    bytes -- including a sincos.py and a screen.asm -- were found
    committed to this repo, each exactly PROGRAM_MAX_SIZE, which is
    what an assembler output redirected onto its own input looks like.
    """
    source = Path(source_file)
    output = Path(output_file) if output_file else source.with_suffix(".bin")

    if output.suffix.lower() in PROTECTED_SUFFIXES:
        raise ValueError(
            f"Refusing to write assembler output to {output.name}: "
            f"{output.suffix} is a source file extension, not a binary one")
    if output.resolve() == source.resolve():
        raise ValueError(f"Refusing to overwrite the source file {source.name}")

    asm = Assembler(str(source))
    binary = asm.assemble()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(binary)

    if not quiet:
        for warning in asm.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        print(f"Assembled {source} -> {output} "
              f"({len(binary)} bytes, {len(binary) // 8} instructions)")
    return binary


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python assembler/assembler.py <source.asm> [output.bin]",
              file=sys.stderr)
        sys.exit(2)

    try:
        assemble_file(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    except Exception as e:
        print(f"Assembly failed: {e}", file=sys.stderr)
        sys.exit(1)
        sys.exit(1)