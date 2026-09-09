#!/usr/bin/env python3
"""
Pigeon Emulator Assembler

Two-pass assembler:
  Pass 1: Preprocess (strip comments, evaluate static defs, collect labels)
  Pass 2: Assemble (parse mnemonics, resolve operands, encode to binary)

Usage:
  python assembler.py <source.asm> [output.bin]
"""

import re
import sys
from typing import Dict, List, Tuple, Optional
from pathlib import Path

# Handle imports for both standalone and integrated use
try:
    from instruction_set import INSTRUCTIONS_BY_NAME, NONE_REG, encode
except ImportError:
    parent_dir = Path(__file__).parent.parent
    sys.path.insert(0, str(parent_dir))
    try:
        from instruction_set import INSTRUCTIONS_BY_NAME, NONE_REG, encode
    except ImportError:
        print("Error: Could not find instruction_set.py")
        print(f"  Searched in: {Path(__file__).parent}")
        print(f"  Searched in: {parent_dir}")
        sys.exit(1)

# Register name to index mapping (A=0, B=1, etc.)
REG_NAME_TO_IDX = {chr(ord('A') + i): i for i in range(26)}


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
}


class Assembler:
    def __init__(self, source_file: str):
        """Load source assembly file."""
        with open(source_file, 'r') as f:
            self.source_lines = f.readlines()
        self.symbols: Dict[str, int] = {}  # label name -> address
        self.static_defs: Dict[str, int] = {}  # static definition name -> value
        self.org_addr = 0  # origin address
        self.instructions: List[bytes] = []  # collected instructions
        self.current_addr = 0  # current write address

    def assemble(self) -> bytes:
        """Two-pass assembly: preprocess + assemble."""
        # Pass 1: Preprocess, collect symbols
        preprocessed = self._preprocess()
        self._collect_symbols(preprocessed)

        # Pass 2: Assemble to binary
        self.instructions = []
        self.current_addr = self.org_addr
        self.current_line_num = 0  # For error reporting
        for line_num, line in enumerate(preprocessed, 1):
            self.current_line_num = line_num
            line = line.strip()
            if not line:
                continue
            try:
                self._assemble_line(line)
            except Exception as e:
                raise ValueError(f"Line {line_num}: {line}\n  Error: {e}")

        return b''.join(self.instructions)

    def _preprocess(self) -> List[str]:
        """
        Strip comments, evaluate static definitions, handle .ORG.
        Returns cleaned lines ready for label collection and assembly.
        """
        lines = []
        for line in self.source_lines:
            # Strip comments
            if ';' in line:
                line = line[:line.index(';')]

            line = line.strip()
            if not line:
                continue

            # Handle .ORG directive
            if line.upper().startswith('.ORG'):
                match = re.match(r'\.ORG\s+(0x[0-9a-fA-F]+|\d+)', line, re.IGNORECASE)
                if match:
                    self.org_addr = int(match.group(1), 0)
                    self.current_addr = self.org_addr
                continue

            # Handle static definitions (NAME = expression)
            if '=' in line and ':' not in line:  # colon indicates a label, not a definition
                match = re.match(r'(\w+)\s*=\s*(.+)', line)
                if match:
                    name = match.group(1)
                    expr = match.group(2).strip()
                    value = self._eval_expr(expr)
                    self.static_defs[name] = value
                    continue

            lines.append(line)

        return lines

    def _eval_expr(self, expr: str) -> int:
        """
        Evaluate a static definition expression: supports +, -, *, /
        and symbol references (both static defs and labels).
        Example: "0x100 + 20", "IO_POINTER + #IO_R_W"
        """
        expr = expr.strip()

        # Replace symbol references
        def replace_symbol(match):
            symbol = match.group(1)
            if symbol in self.static_defs:
                return str(self.static_defs[symbol])
            if symbol in self.symbols:
                return str(self.symbols[symbol])
            raise ValueError(f"Undefined symbol: {symbol}")

        # Remove '#' prefix (used in assembly, not in definitions)
        expr = expr.replace('#', '')

        # Replace all symbol references
        expr = re.sub(r'\b([A-Za-z_]\w*)\b', replace_symbol, expr)

        # Safely evaluate the expression
        try:
            result = eval(expr)
            return int(result) & 0xFFFFFFFF
        except Exception as e:
            raise ValueError(f"Failed to evaluate expression '{expr}': {e}")

    def _collect_symbols(self, lines: List[str]):
        """
        First pass: collect label addresses and verify all static defs.
        Labels end with ':' and are on their own line (or before a mnemonic).
        """
        self.current_addr = self.org_addr

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Check for label
            if ':' in line:
                # Label can be "LABEL:" or "LABEL: MNEMONIC operands"
                parts = line.split(':', 1)
                label_name = parts[0].strip()
                self.symbols[label_name] = self.current_addr

                # Check if there's an instruction on the same line
                remainder = parts[1].strip()
                if remainder:
                    self.current_addr += 8  # Each instruction is 8 bytes
            else:
                # Regular instruction
                self.current_addr += 8

    def _assemble_line(self, line: str):
        """
        Assemble a single line: parse mnemonic and operands, encode to binary.
        Handles labels (skips them), and instructions.
        """
        line = line.strip()
        if not line:
            return

        # Skip labels
        if ':' in line:
            parts = line.split(':', 1)
            remainder = parts[1].strip()
            if not remainder:
                return  # Label only, no instruction
            line = remainder  # Assemble the instruction part

        # Parse mnemonic and operands
        tokens = line.split()
        if not tokens:
            return

        mnemonic = tokens[0].upper()

        if mnemonic not in INSTRUCTIONS_BY_NAME:
            raise ValueError(f"Unknown mnemonic: {mnemonic}")

        # Get operands (everything after mnemonic, rejoin in case of commas)
        operand_str = ' '.join(tokens[1:])
        # Parse operands based on instruction type
        dst, src1, src2, imm = self._parse_operands(mnemonic, operand_str)
        # Encode the instruction
        binary = encode(mnemonic, dst=dst, src1=src1, src2=src2, imm=imm)
        self.instructions.append(binary)
        self.current_addr += 8

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

    def _find_available_register(self, avoid_reg: int) -> int:
        """
        Find an available temporary register that's not the given register.
        Tries F, G, H, etc. first, then falls back to other registers.
        This is best-effort to avoid clobbering important values.
        """
        # Prefer high registers (F=5, G=6, H=7, etc.) as they're less commonly used
        for candidate in range(5, 26):  # F to Z
            if candidate != avoid_reg:
                return candidate
        # If we get here, all registers would conflict. Use A as fallback (should rarely happen)
        return 0

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

        if len(tokens) != len(template):
            raise ValueError(
                f"{mnemonic} expects {len(template)} operand(s) {template}, "
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
        if operand.upper() in REG_NAME_TO_IDX:
            return REG_NAME_TO_IDX[operand.upper()]

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
            return REG_NAME_TO_IDX[operand.upper()], 0

        # Otherwise try to parse as immediate (no # prefix)
        try:
            value = self._eval_expr(operand)
            return NONE_REG, value
        except Exception:
            raise ValueError(f"Invalid operand: {operand}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python assembler.py <source.asm> [output.bin]")
        sys.exit(1)

    source_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else source_file.replace('.asm', '.bin')

    try:
        asm = Assembler(source_file)
        binary = asm.assemble()
        with open(output_file, 'wb') as f:
            f.write(binary)
        print(f"✓ Assembled {source_file} -> {output_file} ({len(binary)} bytes)")
    except Exception as e:
        print(f"✗ Assembly failed: {e}", file=sys.stderr)
        sys.exit(1)