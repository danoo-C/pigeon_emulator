"""A small C preprocessor: #include, object-like #define, #if(n)def.

Enough to make headers work: object-like and function-like macros,
conditional inclusion, and #include. Not the real thing -- no #if
arithmetic, no token pasting, no stringification, no variadic macros.

Line directives are not emitted; instead every produced line carries the
file and line it came from, so a diagnostic in an included header names
the header rather than the line of the file that included it.

Comments are stripped HERE, before directives are read, exactly as a real
C preprocessor does -- not later in the lexer. Doing it later meant
`#define FP 8  /* fractional bits */` captured the comment as part of the
macro body, so every use of FP pasted a comment in with it. Harmless in an
expression; fatal when the expansion landed inside another comment, since
that produced a nested `/*` the lexer cannot parse.
"""
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .lexer import CompileError

INCLUDE_RE = re.compile(r'^\s*#\s*include\s+(?:"([^"]+)"|<([^>]+)>)\s*$')
# The '(' must follow the name with NO space to make a macro function-like:
# `#define NULL ((void *)0)` is object-like, its body merely starts with '('.
DEFINE_RE = re.compile(r'^\s*#\s*define\s+([A-Za-z_]\w*)(\(?)\s*(.*)$')
UNDEF_RE = re.compile(r'^\s*#\s*undef\s+([A-Za-z_]\w*)\s*$')
IFDEF_RE = re.compile(r'^\s*#\s*if(n?)def\s+([A-Za-z_]\w*)\s*$')
ELSE_RE = re.compile(r'^\s*#\s*else\s*$')
ENDIF_RE = re.compile(r'^\s*#\s*endif\b.*$')
PRAGMA_RE = re.compile(r'^\s*#\s*pragma\s+(.*)$')
ANY_DIRECTIVE_RE = re.compile(r'^\s*#')
IDENTIFIER_RE = re.compile(r'\b[A-Za-z_]\w*\b')

MAX_INCLUDE_DEPTH = 32


class Preprocessor:
    def __init__(self, include_paths=None, defines=None):
        self.include_paths = [Path(p) for p in (include_paths or [])]
        self.macros: Dict[str, str] = dict(defines or {})
        self.pragma_once: set = set()

    def process_file(self, path) -> Tuple[str, List[Tuple[str, int]]]:
        path = Path(path)
        return self.process(path.read_text(), str(path), path.parent)

    def process(self, source: str, filename: str, directory=None):
        """Returns (text, origins) where origins[i] is the (file, line)
        that produced output line i."""
        out: List[str] = []
        origins: List[Tuple[str, int]] = []
        self._run(source, filename, Path(directory or "."), out, origins, 0)
        return "\n".join(out), origins

    # --- the worker --------------------------------------------------------

    def _strip_comments(self, source: str):
        """Remove // and /* */ comments, preserving line structure.

        String and character literals are respected, so a "//" inside a
        string survives. Block comments are replaced by a space and their
        newlines kept, so every output line still corresponds to the same
        input line -- diagnostics depend on that.
        """
        out = []
        i, n = 0, len(source)
        in_block = False
        in_string = False
        quote = ""
        while i < n:
            ch = source[i]
            nxt = source[i + 1] if i + 1 < n else ""

            if in_block:
                if ch == "*" and nxt == "/":
                    in_block = False
                    out.append(" ")
                    i += 2
                    continue
                out.append("\n" if ch == "\n" else " ")
                i += 1
                continue

            if in_string:
                out.append(ch)
                if ch == "\\" and nxt:
                    out.append(nxt)
                    i += 2
                    continue
                if ch == quote:
                    in_string = False
                i += 1
                continue

            if ch in "\"'":
                in_string, quote = True, ch
                out.append(ch)
                i += 1
                continue

            if ch == "/" and nxt == "/":
                while i < n and source[i] != "\n":
                    i += 1
                continue

            if ch == "/" and nxt == "*":
                in_block = True
                out.append(" ")
                i += 2
                continue

            out.append(ch)
            i += 1

        if in_block:
            raise CompileError("unterminated /* comment", 1, 1, "<source>")
        return "".join(out)

    def _run(self, source, filename, directory, out, origins, depth):
        if depth > MAX_INCLUDE_DEPTH:
            raise CompileError(
                f"#include nested more than {MAX_INCLUDE_DEPTH} deep -- a cycle?",
                1, 1, filename)

        try:
            source = self._strip_comments(source)
        except CompileError as e:
            raise CompileError(e.message, e.line, e.column, filename) from None

        # Each entry: (this branch is being emitted, any branch has been taken)
        conditions: List[Tuple[bool, bool]] = []

        for number, line in enumerate(source.splitlines(), 1):
            emitting = all(active for active, _ in conditions)

            match = IFDEF_RE.match(line)
            if match:
                negate, name = match.group(1) == "n", match.group(2)
                defined = name in self.macros
                taken = (not defined) if negate else defined
                conditions.append((emitting and taken, taken))
                continue

            if ELSE_RE.match(line):
                if not conditions:
                    raise CompileError("#else without #if", number, 1, filename)
                _, taken = conditions[-1]
                outer = all(active for active, _ in conditions[:-1])
                conditions[-1] = (outer and not taken, True)
                continue

            if ENDIF_RE.match(line):
                if not conditions:
                    raise CompileError("#endif without #if", number, 1, filename)
                conditions.pop()
                continue

            if not emitting:
                out.append("")
                origins.append((filename, number))
                continue

            match = INCLUDE_RE.match(line)
            if match:
                name = match.group(1) or match.group(2)
                quoted = match.group(1) is not None
                target = self._resolve(name, directory if quoted else None)
                if target is None:
                    raise CompileError(f"cannot find include {name!r}", number, 1, filename)
                if str(target) in self.pragma_once:
                    out.append("")
                    origins.append((filename, number))
                    continue
                self._run(target.read_text(), str(target), target.parent,
                          out, origins, depth + 1)
                continue

            match = DEFINE_RE.match(line)
            if match:
                name, paren, rest = match.group(1), match.group(2), match.group(3)
                if paren:
                    # `#define NAME(a, b) body` -- the '(' touches the name.
                    close = rest.find(")")
                    if close < 0:
                        raise CompileError(
                            f"macro {name!r} has no closing ')'", number, 1, filename)
                    params = [a.strip() for a in rest[:close].split(",") if a.strip()]
                    self.macros[name] = (params, rest[close + 1:].strip())
                else:
                    self.macros[name] = rest.strip()
                out.append("")
                origins.append((filename, number))
                continue

            match = UNDEF_RE.match(line)
            if match:
                self.macros.pop(match.group(1), None)
                out.append("")
                origins.append((filename, number))
                continue

            match = PRAGMA_RE.match(line)
            if match:
                if match.group(1).strip() == "once":
                    self.pragma_once.add(filename)
                out.append("")
                origins.append((filename, number))
                continue

            if ANY_DIRECTIVE_RE.match(line):
                directive = line.strip().split()[0]
                raise CompileError(f"unsupported directive {directive}",
                                   number, 1, filename)

            out.append(self._expand(line))
            origins.append((filename, number))

    def _resolve(self, name: str, local_directory) -> Optional[Path]:
        candidates = ([local_directory / name] if local_directory else [])
        candidates += [base / name for base in self.include_paths]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _expand(self, line: str) -> str:
        """Substitute macros, re-scanning until the line stops changing.

        Bounded, so a self-referential macro cannot spin forever.
        """
        if not self.macros:
            return line
        for _ in range(32):
            replaced = self._expand_once(line)
            if replaced == line:
                return line
            line = replaced
        return line

    def _expand_once(self, line: str) -> str:
        out, i = [], 0
        while i < len(line):
            match = IDENTIFIER_RE.match(line, i)
            if not match:
                out.append(line[i])
                i += 1
                continue

            name = match.group(0)
            definition = self.macros.get(name)
            if definition is None:
                out.append(name)
                i = match.end()
                continue

            if isinstance(definition, str):          # object-like
                out.append(definition)
                i = match.end()
                continue

            params, body = definition                # function-like
            after = match.end()
            while after < len(line) and line[after] in " \t":
                after += 1
            if after >= len(line) or line[after] != "(":
                out.append(name)                     # not an invocation
                i = match.end()
                continue

            args, end = _split_arguments(line, after)
            if args is None:                         # unbalanced: leave alone
                out.append(name)
                i = match.end()
                continue
            out.append(_substitute(body, params, args))
            i = end
        return "".join(out)


def _split_arguments(text: str, open_paren: int):
    """Split a macro invocation's arguments, respecting nesting.

    Commas inside parentheses, brackets or a string belong to the
    argument, not to the separator -- `F(g(a, b), c)` has two arguments.
    Returns (args, index after the ')'), or (None, _) if unbalanced.
    """
    depth, current, args = 0, [], []
    i, in_string, quote = open_paren, False, ""
    while i < len(text):
        ch = text[i]
        if in_string:
            current.append(ch)
            if ch == "\\" and i + 1 < len(text):
                current.append(text[i + 1]); i += 2; continue
            if ch == quote:
                in_string = False
        elif ch in "\"'":
            in_string, quote = True, ch
            current.append(ch)
        elif ch in "([":
            depth += 1
            if depth > 1:
                current.append(ch)
            i += 1
            continue
        elif ch in ")]":
            depth -= 1
            if depth == 0:
                args.append("".join(current).strip())
                if args == [""]:
                    args = []
                return args, i + 1
            current.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    return None, len(text)


def _substitute(body: str, params, args) -> str:
    """Replace each parameter in the body with its argument, parenthesised.

    The parentheses matter: `#define SQ(x) ((x)*(x))` is written that way
    by hand for a reason, and adding them here makes `M(a+b)` behave even
    when the macro author forgot.
    """
    if len(args) != len(params):
        return body
    mapping = {p: f"({a})" for p, a in zip(params, args)}
    return IDENTIFIER_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), body)


def preprocess(source: str, filename: str, include_paths=None, defines=None):
    return Preprocessor(include_paths, defines).process(
        source, filename, Path(filename).parent)
