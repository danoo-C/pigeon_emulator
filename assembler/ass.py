#!/usr/bin/env python3
"""
Pigeon Emulator Assembler

Two-pass assembler:
  Pass 1: Preprocess (strip comments, evaluate static defs, collect labels)
  Pass 2: Assemble (parse mnemonics, resolve operands, encode to binary)

Usage:
  asm = Assembler("bios.asm")
  binary = asm.assemble()
  with open("bios.bin", "wb") as f:
      f.write(binary)
"""

import re
import sys
from typing import Dict, List, Tuple, Optional
from pathlib import Path

# Handle imports for both standalone and integrated use
try:
    # Try direct import first (works if in same directory as instruction_set.py)
    from instruction_set import INSTRUCTIONS_BY_NAME, NONE_REG, encode
except ImportError:
    # If that fails, try parent directory (works if in pigeon-emulator/assembler/)
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
        print(f"Parsing operands for {mnemonic}: '{operand_str}'")  # Debugging line
        """
        Parse operands based on the instruction's expected format.
        Returns (dst, src1, src2, imm) with NONE_REG for unused slots.
        
        Common patterns:
          MOV dst, src        -> (dst_reg, src_reg/imm, NONE, imm)
          ADD dst, src1, src2 -> (dst_reg, src1_reg, src2_reg/imm, imm)
          MWW [addr], src     -> (addr_reg/imm, src_reg/imm, NONE, imm)
          MR dst, [addr]      -> (dst_reg, addr_reg/imm, NONE, imm)
          JMP addr            -> (NONE, NONE, NONE, addr_imm)
          CMP src1, src2      -> (NONE, src1_reg, src2_reg/imm, imm)
        """
        # Tokenize operands, splitting smartly on commas and #
        operand_tokens = self._split_operands(operand_str)
        
        dst = src1 = src2 = NONE_REG
        imm = 0
        
        # Instruction-specific parsing
        if mnemonic in ('NOP', 'HALT'):
            # No operands
            pass
        
        elif mnemonic == 'MOV':
            # MOV dst, src
            dst = self._parse_operand(operand_tokens[0])
            src1, imm = self._parse_operand_or_imm(operand_tokens[1])
        
        elif mnemonic in ('ADD', 'SUB', 'MUL', 'DIV'):
            # ADD/SUB/MUL/DIV dst, src1, src2
            # Also support shorthand: "SUB C #1" means "SUB C C #1" (C = C - 1)
            dst = self._parse_operand(operand_tokens[0])
            if len(operand_tokens) == 2:
                # Shorthand: dst op operand -> dst op dst operand
                src1 = dst
                src2, imm = self._parse_operand_or_imm(operand_tokens[1])
            else:
                # Full form: dst, src1, src2
                src1 = self._parse_operand(operand_tokens[1])
                src2, imm = self._parse_operand_or_imm(operand_tokens[2])
        
        elif mnemonic == 'JMP':
            # JMP addr
            _, imm = self._parse_operand_or_imm(operand_tokens[0])
        
        elif mnemonic in ('JZ', 'JNZ', 'JL', 'JG', 'JLE', 'JGE'):
            # Conditional jumps: addr
            _, imm = self._parse_operand_or_imm(operand_tokens[0])
        
        elif mnemonic == 'CMP':
            # CMP src1, src2
            src1 = self._parse_operand(operand_tokens[0])
            src2, imm = self._parse_operand_or_imm(operand_tokens[1])
        
        elif mnemonic == 'MR':
            # MR dst, [addr]
            dst = self._parse_operand(operand_tokens[0])
            src1, imm = self._parse_operand_or_imm(operand_tokens[1])
        
        elif mnemonic == 'MW':
            # MW [addr], src
            # addr goes in imm, src can be register (src1) or small immediate (src2)
            dst, imm_addr = self._parse_operand_or_imm(operand_tokens[0])
            src_reg, src_imm = self._parse_operand_or_imm(operand_tokens[1])
            
            if dst != NONE_REG:
                # addr is a register
                imm = 0
            else:
                # addr is immediate
                imm = imm_addr
            
            if src_reg != NONE_REG:
                # value is a register
                src1 = src_reg
            elif src_imm <= 0xFF:
                # value fits in src2 (small immediate)
                src2 = src_imm
            else:
                # value is too large for src2, auto-generate MOV
                # Pick a temp register that's not the address register
                addr_reg = dst if dst != NONE_REG else NONE_REG
                temp_reg = self._find_available_register(addr_reg)
                # Emit MOV temp_reg, #src_imm first
                mov_binary = encode('MOV', dst=temp_reg, src1=NONE_REG, src2=NONE_REG, imm=src_imm)
                self.instructions.append(mov_binary)
                self.current_addr += 8
                # Now emit MW with temp_reg as the value
                src1 = temp_reg
        
        elif mnemonic == 'MRW':
            # MRW dst, [addr]
            dst = self._parse_operand(operand_tokens[0])
            src1, imm = self._parse_operand_or_imm(operand_tokens[1])
        
        elif mnemonic == 'MWW':
            # MWW [addr], src
            # Complication: both addr and value are immediates, but only one fits in imm
            # Solution: auto-generate MOV if needed
            dst, imm_addr = self._parse_operand_or_imm(operand_tokens[0])
            src_reg, src_imm = self._parse_operand_or_imm(operand_tokens[1])
            
            if dst != NONE_REG:
                # addr is a register
                imm = 0
            else:
                # addr is immediate
                imm = imm_addr
            
            if src_reg != NONE_REG:
                # value is a register
                src1 = src_reg
            elif src_imm <= 0xFF:
                # value fits in src2 (small immediate)
                src2 = src_imm
            else:
                # value is too large for src2
                # Auto-generate: MOV temp_reg, #value; then MWW [addr], temp_reg
                # Pick a temp register that's not the address register
                addr_reg = dst if dst != NONE_REG else NONE_REG
                temp_reg = self._find_available_register(addr_reg)
                # Emit MOV temp_reg, #src_imm first
                mov_binary = encode('MOV', dst=temp_reg, src1=NONE_REG, src2=NONE_REG, imm=src_imm)
                self.instructions.append(mov_binary)
                self.current_addr += 8
                # Now emit MWW with temp_reg
                src1 = temp_reg
        
        elif mnemonic in ('PUSH', 'POP'):
            # PUSH src / POP dst
            if mnemonic == 'PUSH':
                src1 = self._parse_operand(operand_tokens[0])
            else:
                dst = self._parse_operand(operand_tokens[0])
        
        else:
            raise ValueError(f"Don't know how to parse operands for {mnemonic}")
        print(f"Parsed operands: dst={dst}, src1={src1}, src2={src2}, imm={imm}\n")  # Debugging line
        return dst, src1, src2, imm
    
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
            print(f"Evaluating immediate expression: {operand}")  # Debugging line
            value = self._eval_expr(operand)
            print(f"Evaluated immediate value: {value}")  # Debugging line
            return NONE_REG, value
        
        # Check if it's a register
        if operand.upper() in REG_NAME_TO_IDX:
            return REG_NAME_TO_IDX[operand.upper()], 0
        
        # Otherwise try to parse as immediate (no # prefix)
        try:
            value = self._eval_expr(operand)
            return NONE_REG, value
        except:
            raise ValueError(f"Invalid operand: {operand}")


if __name__ == '__main__':
    import sys
    
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
