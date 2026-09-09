"""AST node definitions.

Plain dataclasses. Every node carries the token it started at, so a later
stage can report `file:line:col` rather than "somewhere in your program".
"""
from dataclasses import dataclass, field
from typing import List, Optional

from .typesys import Type


@dataclass
class Node:
    token: object = None          # the Token this node starts at
    type: Optional[Type] = None   # filled in by the analyser


# --- expressions ------------------------------------------------------------

@dataclass
class IntLiteral(Node):
    value: int = 0


@dataclass
class StringLiteral(Node):
    value: bytes = b""
    label: str = ""               # assigned by the analyser


@dataclass
class Identifier(Node):
    name: str = ""
    symbol: object = None         # resolved by the analyser


@dataclass
class Unary(Node):
    op: str = ""
    operand: Node = None


@dataclass
class Binary(Node):
    op: str = ""
    left: Node = None
    right: Node = None


@dataclass
class Assign(Node):
    op: str = "="                 # '=', '+=', '<<=', ...
    target: Node = None
    value: Node = None


@dataclass
class IncDec(Node):
    op: str = "++"
    operand: Node = None
    prefix: bool = True


@dataclass
class Conditional(Node):
    condition: Node = None
    then: Node = None
    otherwise: Node = None


@dataclass
class Call(Node):
    callee: Node = None
    args: List[Node] = field(default_factory=list)


@dataclass
class Index(Node):
    base: Node = None
    index: Node = None


@dataclass
class Member(Node):
    obj: Node = None
    name: str = ""
    arrow: bool = False


@dataclass
class InitList(Node):
    """`{ 1, 2, 3 }` -- constant-only, and only on globals for now."""
    values: List[Node] = field(default_factory=list)


@dataclass
class Cast(Node):
    to: Optional[Type] = None
    operand: Node = None


@dataclass
class SizeOf(Node):
    operand: Node = None          # an expression...
    of_type: Optional[Type] = None  # ...or a type


# --- statements -------------------------------------------------------------

@dataclass
class Block(Node):
    statements: List[Node] = field(default_factory=list)
    # False for the group produced by `int a, b;` -- those names belong to
    # the ENCLOSING scope, not to a new one.
    is_scope: bool = True


@dataclass
class VarDecl(Node):
    name: str = ""
    decl_type: Optional[Type] = None
    init: Optional[Node] = None
    is_static: bool = False
    is_extern: bool = False
    symbol: object = None


@dataclass
class If(Node):
    condition: Node = None
    then: Node = None
    otherwise: Optional[Node] = None


@dataclass
class While(Node):
    condition: Node = None
    body: Node = None
    is_do_while: bool = False


@dataclass
class For(Node):
    init: Optional[Node] = None
    condition: Optional[Node] = None
    step: Optional[Node] = None
    body: Node = None


@dataclass
class Return(Node):
    value: Optional[Node] = None


@dataclass
class Break(Node):
    pass


@dataclass
class Continue(Node):
    pass


@dataclass
class ExprStatement(Node):
    expr: Node = None


@dataclass
class Empty(Node):
    pass


# --- top level --------------------------------------------------------------

@dataclass
class Param:
    name: str
    type: Type


@dataclass
class FunctionDef(Node):
    name: str = ""
    returns: Optional[Type] = None
    params: List[Param] = field(default_factory=list)
    body: Optional[Block] = None
    is_static: bool = False
    frame_size: int = 0            # filled in by the analyser
    label: str = ""                # assembly label (may be mangled)
    locals: list = field(default_factory=list)


@dataclass
class Program(Node):
    globals: List[VarDecl] = field(default_factory=list)
    functions: List[FunctionDef] = field(default_factory=list)
    strings: list = field(default_factory=list)   # (label, bytes)
