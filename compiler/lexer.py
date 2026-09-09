"""Tokeniser for pigeon-cc.

Tokens carry their source line and column so every later stage can point
at the offending text. On a machine whose only debugger is a
single-stepper, a compiler error that names the wrong line costs an hour.
"""
from dataclasses import dataclass
from typing import List, Optional

KEYWORDS = {
    "int", "unsigned", "signed", "char", "short", "long", "void",
    "if", "else", "while", "do", "for", "return", "break", "continue",
    "struct", "union", "enum", "typedef", "sizeof",
    "switch", "case", "default", "goto",   # lexed so the parser can name them
    "const", "static", "volatile", "extern",
    "float", "double",           # lexed so the parser can reject them by name
}

# Longest first: '<<=' must win over '<<', which must win over '<'.
OPERATORS = [
    "<<=", ">>=", "...",
    "->", "++", "--", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    "+", "-", "*", "/", "%", "=", "<", ">", "!", "~", "&", "|", "^",
    "?", ":", ";", ",", ".", "(", ")", "[", "]", "{", "}",
]

_SIMPLE_ESCAPES = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39,
                   '"': 34, "a": 7, "b": 8, "f": 12, "v": 11, "e": 27}


class CompileError(Exception):
    """A diagnostic that names a source position."""

    def __init__(self, message, line=0, column=0, filename="<source>"):
        self.message, self.line, self.column, self.filename = message, line, column, filename
        super().__init__(f"{filename}:{line}:{column}: {message}")


@dataclass
class Token:
    kind: str          # 'int', 'char', 'str', 'id', 'kw', 'op', 'eof'
    value: object
    line: int
    column: int
    filename: str = "<source>"

    def __repr__(self):
        return f"{self.kind}:{self.value!r}@{self.line}:{self.column}"

    def error(self, message) -> CompileError:
        return CompileError(message, self.line, self.column, self.filename)


class Lexer:
    def __init__(self, source: str, filename: str = "<source>"):
        self.src, self.filename = source, filename
        self.pos, self.line, self.col = 0, 1, 1

    # --- character helpers -------------------------------------------------

    def _peek(self, offset: int = 0) -> str:
        index = self.pos + offset
        return self.src[index] if index < len(self.src) else ""

    def _at(self, chars: str) -> bool:
        """Is the next character one of `chars`?

        Not `self._peek() in chars`: at end of input _peek() returns "",
        and Python says the empty string is in every string. That made
        each scanning loop below spin forever on a truncated file.
        """
        ch = self._peek()
        return ch != "" and ch in chars

    def _advance(self, count: int = 1) -> str:
        text = self.src[self.pos:self.pos + count]
        for ch in text:
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        self.pos += count
        return text

    def _error(self, message) -> CompileError:
        return CompileError(message, self.line, self.col, self.filename)

    # --- main loop ---------------------------------------------------------

    def tokens(self) -> List[Token]:
        out: List[Token] = []
        while True:
            self._skip_trivia()
            if self.pos >= len(self.src):
                out.append(Token("eof", None, self.line, self.col, self.filename))
                return out

            line, col = self.line, self.col
            ch = self._peek()

            if ch.isdigit() or (ch == "." and self._peek(1).isdigit()):
                out.append(self._number(line, col))
            elif ch.isalpha() or ch == "_":
                out.append(self._identifier(line, col))
            elif ch == "'":
                out.append(self._char_literal(line, col))
            elif ch == '"':
                out.append(self._string_literal(line, col))
            else:
                out.append(self._operator(line, col))

    def _skip_trivia(self):
        """Whitespace, // comments, /* comments */, and stray # lines."""
        while self.pos < len(self.src):
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
            elif ch == "/" and self._peek(1) == "/":
                while self.pos < len(self.src) and self._peek() != "\n":
                    self._advance()
            elif ch == "/" and self._peek(1) == "*":
                start_line, start_col = self.line, self.col
                self._advance(2)
                while True:
                    if self.pos >= len(self.src):
                        raise CompileError("unterminated /* comment", start_line,
                                           start_col, self.filename)
                    if self._peek() == "*" and self._peek(1) == "/":
                        self._advance(2)
                        break
                    self._advance()
            else:
                return

    # --- literals ----------------------------------------------------------

    def _number(self, line, col) -> Token:
        start = self.pos
        if self._peek() == "0" and self._peek(1) in "xX":
            self._advance(2)
            digits = self.pos
            while self._at("0123456789abcdefABCDEF"):
                self._advance()
            if self.pos == digits:
                raise self._error("hex literal has no digits")
            value = int(self.src[digits:self.pos], 16)
        elif self._peek() == "0" and self._peek(1) in "bB":
            self._advance(2)
            digits = self.pos
            while self._at("01"):
                self._advance()
            if self.pos == digits:
                raise self._error("binary literal has no digits")
            value = int(self.src[digits:self.pos], 2)
        else:
            while self._peek().isdigit():
                self._advance()
            text = self.src[start:self.pos]
            if self._at(".eE"):
                raise CompileError(
                    "floating point is not supported: the machine has no FP "
                    "hardware and there is no soft-float library",
                    line, col, self.filename)
            value = int(text, 8) if len(text) > 1 and text[0] == "0" else int(text)

        while self._at("uUlL"):      # integer suffixes: accepted, ignored
            self._advance()
        return Token("int", value & 0xFFFFFFFF, line, col, self.filename)

    def _escape(self) -> int:
        """Read one escape sequence, the backslash already consumed."""
        ch = self._advance()
        if ch == "x":
            digits = ""
            while self._at("0123456789abcdefABCDEF") and len(digits) < 2:
                digits += self._advance()
            if not digits:
                raise self._error(r"\x needs at least one hex digit")
            return int(digits, 16) & 0xFF
        if ch in "01234567":
            digits = ch
            while self._at("01234567") and len(digits) < 3:
                digits += self._advance()
            return int(digits, 8) & 0xFF
        if ch in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[ch]
        raise self._error(f"unknown escape \\{ch}")

    def _char_literal(self, line, col) -> Token:
        self._advance()                                  # opening quote
        if self._peek() == "'":
            raise self._error("empty character literal")
        value = self._escape() if self._advance_if("\\") else ord(self._advance())
        if self._peek() != "'":
            raise self._error("unterminated character literal")
        self._advance()
        return Token("int", value & 0xFF, line, col, self.filename)

    def _string_literal(self, line, col) -> Token:
        self._advance()                                  # opening quote
        out = bytearray()
        while True:
            if self.pos >= len(self.src) or self._peek() == "\n":
                raise CompileError("unterminated string literal", line, col,
                                   self.filename)
            if self._peek() == '"':
                self._advance()
                return Token("str", bytes(out), line, col, self.filename)
            if self._advance_if("\\"):
                out.append(self._escape())
            else:
                out += self._advance().encode("utf-8")

    def _advance_if(self, ch: str) -> bool:
        if self._peek() == ch:
            self._advance()
            return True
        return False

    # --- words and punctuation ---------------------------------------------

    def _identifier(self, line, col) -> Token:
        start = self.pos
        while self._peek().isalnum() or self._peek() == "_":
            self._advance()
        name = self.src[start:self.pos]
        return Token("kw" if name in KEYWORDS else "id", name, line, col, self.filename)

    def _operator(self, line, col) -> Token:
        for op in OPERATORS:
            if self.src.startswith(op, self.pos):
                self._advance(len(op))
                return Token("op", op, line, col, self.filename)
        raise self._error(f"unexpected character {self._peek()!r}")


def tokenize(source: str, filename: str = "<source>") -> List[Token]:
    return Lexer(source, filename).tokens()
