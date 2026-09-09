"""Recursive-descent parser producing the AST in ast_nodes.py.

Expression parsing is precedence-climbing over the table below, which is
C's, minus the pieces the machine cannot support.
"""
from typing import List, Optional

from . import ast_nodes as A
from .lexer import CompileError, Token
from .typesys import (CHAR, INT, UCHAR, UINT, VOID, FunctionType, Type, array_of,
                    layout_struct, pointer_to)

# Binary operator precedence, loosest first. '&&' and '||' short-circuit and
# are handled here so codegen can emit the branch form directly.
PRECEDENCE = [
    ["||"], ["&&"], ["|"], ["^"], ["&"],
    ["==", "!="], ["<", ">", "<=", ">="],
    ["<<", ">>"], ["+", "-"], ["*", "/", "%"],
]

ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}

TYPE_KEYWORDS = {"int", "unsigned", "signed", "char", "short", "long", "void",
                 "struct", "union", "enum"}
QUALIFIERS = {"const", "volatile"}
STORAGE = {"static", "extern"}
REJECTED_TYPES = {"float", "double"}


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0
        self.typedefs = {}        # name -> Type
        self.structs = {}         # tag -> StructType

    # --- token helpers -----------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 0) -> Token:
        index = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[index]

    def at(self, kind: str, value=None) -> bool:
        token = self.current
        return token.kind == kind and (value is None or token.value == value)

    def at_op(self, *values) -> bool:
        return self.current.kind == "op" and self.current.value in values

    def at_kw(self, *values) -> bool:
        return self.current.kind == "kw" and self.current.value in values

    def take(self) -> Token:
        token = self.current
        self.pos += 1
        return token

    def accept(self, kind: str, value=None) -> Optional[Token]:
        if self.at(kind, value):
            return self.take()
        return None

    def expect(self, kind: str, value=None) -> Token:
        if not self.at(kind, value):
            wanted = f"{value!r}" if value is not None else kind
            got = self.current.value if self.current.kind != "eof" else "end of file"
            raise self.current.error(f"expected {wanted}, got {got!r}")
        return self.take()

    # --- entry point -------------------------------------------------------

    def parse(self) -> A.Program:
        program = A.Program(token=self.current)
        while not self.at("eof"):
            if self.at_kw("typedef"):
                self._typedef()
                continue
            self._top_level(program)
        return program

    def _typedef(self):
        start = self.take()                       # 'typedef'
        base = self._type_specifier()
        type_, name = self._declarator(base)
        if not name:
            raise start.error("typedef needs a name")
        self.expect("op", ";")
        self.typedefs[name] = type_

    def _top_level(self, program: A.Program):
        storage = self._storage_class()
        is_static = storage == "static"
        is_extern = storage == "extern"
        base = self._type_specifier()

        if self.at_op(";"):        # `struct P { ... };` -- a type, no variable
            self.take()
            return

        while True:
            start = self.current
            type_, name = self._declarator(base)
            if not name:
                raise start.error("declaration needs a name")

            if self.at_op("("):                   # a function
                params, variadic = self._parameter_list()
                if self.at_op("{"):
                    body = self._block()
                    program.functions.append(A.FunctionDef(
                        token=start, name=name, returns=type_, params=params,
                        body=body, is_static=is_static))
                else:
                    self.expect("op", ";")        # a prototype: record nothing
                return

            init = self._initializer() if self.accept("op", "=") else None
            type_ = _size_from_initializer(type_, init)
            program.globals.append(A.VarDecl(token=start, name=name,
                                             decl_type=type_, init=init,
                                             is_static=is_static,
                                             is_extern=is_extern))
            if not self.accept("op", ","):
                break
        self.expect("op", ";")

    def _storage_class(self) -> Optional[str]:
        if self.at_kw(*STORAGE):
            return self.take().value
        return None

    # --- types -------------------------------------------------------------

    def _looks_like_type(self) -> bool:
        token = self.current
        if token.kind == "kw":
            return (token.value in TYPE_KEYWORDS or token.value in QUALIFIERS
                    or token.value in REJECTED_TYPES or token.value in STORAGE)
        return token.kind == "id" and token.value in self.typedefs

    def _type_specifier(self) -> Type:
        """Parse a base type. Qualifiers are accepted and discarded --
        `const` and `volatile` matter to codegen only through the lvalue
        they qualify, and this compiler never caches a load across a
        statement boundary, so treating volatile as ordinary is safe."""
        while self.at_kw(*QUALIFIERS):
            self.take()

        token = self.current
        if token.kind == "kw" and token.value in REJECTED_TYPES:
            raise token.error(
                f"'{token.value}' is not supported: the machine has no "
                f"floating-point hardware")

        if self.at_kw("struct") or self.at_kw("union"):
            return self._struct_specifier()

        signed = None
        base = None
        while self.at_kw("unsigned", "signed", "int", "char", "short", "long", "void"):
            word = self.take().value
            if word == "unsigned":
                signed = False
            elif word == "signed":
                signed = True
            elif word in ("short", "long"):
                base = base or "int"          # short/long are aliases for int here
            else:
                base = word

        if base is None and signed is None:
            if token.kind == "id" and token.value in self.typedefs:
                self.take()
                result = self.typedefs[token.value]
                while self.at_kw(*QUALIFIERS):
                    self.take()
                return result
            raise token.error(f"expected a type, got {token.value!r}")

        base = base or "int"
        while self.at_kw(*QUALIFIERS):
            self.take()

        if base == "void":
            return VOID
        if base == "char":
            return CHAR if signed is not False else UCHAR
        return INT if signed is not False else UINT

    def _struct_specifier(self) -> Type:
        keyword = self.take()                       # 'struct' / 'union'
        if keyword.value == "union":
            raise keyword.error("union is not supported yet")

        tag = self.take().value if self.at("id") else ""
        if not self.at_op("{"):
            if tag in self.structs:
                return self.structs[tag]
            raise keyword.error(f"unknown struct tag {tag!r}")

        self.expect("op", "{")
        members = []
        while not self.at_op("}"):
            base = self._type_specifier()
            while True:
                member_type, name = self._declarator(base)
                if not name:
                    raise self.current.error("struct member needs a name")
                members.append((name, member_type))
                if not self.accept("op", ","):
                    break
            self.expect("op", ";")
        self.expect("op", "}")

        struct = layout_struct(tag, members)
        if tag:
            self.structs[tag] = struct
        return struct

    def _declarator(self, base: Type):
        """Parse pointer stars, a name, and any array or function suffix."""
        type_ = base
        while self.at_op("*"):
            self.take()
            while self.at_kw(*QUALIFIERS):
                self.take()
            type_ = pointer_to(type_)

        # `int (*f)(int)` -- a pointer to a function. The parenthesised
        # declarator binds the star to the NAME, not to the return type,
        # which is why this cannot be handled by the star loop above.
        if self.at_op("(") and self.peek(1).kind == "op" and self.peek(1).value == "*":
            self.take()                       # '('
            stars = 0
            while self.at_op("*"):
                self.take()
                stars += 1
            inner_name = self.take().value if self.at("id") else None
            self.expect("op", ")")
            params, _ = self._parameter_list()
            signature = FunctionType(type_, [p.type for p in params])
            result = pointer_to(signature)
            for _ in range(stars - 1):
                result = pointer_to(result)
            return result, inner_name

        name = self.take().value if self.at("id") else None

        while self.at_op("["):
            self.take()
            if self.at_op("]"):
                self.take()
                type_ = pointer_to(type_)         # `T a[]` is `T *a`
                continue
            count_token = self.current
            count = self._constant_expression()
            self.expect("op", "]")
            if count <= 0:
                raise count_token.error(f"array size must be positive, got {count}")
            type_ = array_of(type_, count)
        return type_, name

    def _constant_expression(self) -> int:
        """A compile-time integer: array sizes and the like."""
        node = self._ternary()
        return _fold_constant(node)

    def _parameter_list(self):
        self.expect("op", "(")
        params, variadic = [], False
        if self.at_kw("void") and self.peek(1).kind == "op" and self.peek(1).value == ")":
            self.take()
        while not self.at_op(")"):
            if self.at_op("..."):
                self.take()
                variadic = True
                break
            base = self._type_specifier()
            type_, name = self._declarator(base)
            params.append(A.Param(name or f"_arg{len(params)}", type_))
            if not self.accept("op", ","):
                break
        self.expect("op", ")")
        if variadic:
            raise self.current.error(
                "variadic functions are not supported: the frame layout has "
                "no way to walk an unknown argument count")
        return params, variadic

    def _initializer(self) -> A.Node:
        if self.at_op("{"):
            start = self.take()
            values = []
            while not self.at_op("}"):
                values.append(self._initializer())
                if not self.accept("op", ","):
                    break
            self.expect("op", "}")
            return A.InitList(token=start, values=values)
        return self._assignment()

    # --- statements --------------------------------------------------------

    def _block(self) -> A.Block:
        start = self.expect("op", "{")
        statements = []
        while not self.at_op("}"):
            if self.at("eof"):
                raise start.error("unclosed '{'")
            statements.append(self._statement())
        self.expect("op", "}")
        return A.Block(token=start, statements=statements)

    def _statement(self) -> A.Node:
        token = self.current

        if self.at_op("{"):
            return self._block()
        if self.at_op(";"):
            self.take()
            return A.Empty(token=token)
        if self.at_kw("if"):
            return self._if()
        if self.at_kw("while"):
            return self._while()
        if self.at_kw("do"):
            return self._do_while()
        if self.at_kw("for"):
            return self._for()
        if self.at_kw("return"):
            self.take()
            value = None if self.at_op(";") else self._expression()
            self.expect("op", ";")
            return A.Return(token=token, value=value)
        if self.at_kw("break"):
            self.take()
            self.expect("op", ";")
            return A.Break(token=token)
        if self.at_kw("continue"):
            self.take()
            self.expect("op", ";")
            return A.Continue(token=token)
        if self.at_kw("switch"):
            raise token.error("switch is not supported yet; use if/else")
        if self.at_kw("goto"):
            raise token.error("goto is not supported")
        if self._looks_like_type():
            return self._local_declaration()

        expr = self._expression()
        self.expect("op", ";")
        return A.ExprStatement(token=token, expr=expr)

    def _local_declaration(self) -> A.Node:
        token = self.current
        is_static = self._storage_class() == "static"
        base = self._type_specifier()
        if self.at_op(";"):        # a struct declaration inside a function
            self.take()
            return A.Empty(token=token)
        declarations = []
        while True:
            type_, name = self._declarator(base)
            if not name:
                raise token.error("declaration needs a name")
            init = self._initializer() if self.accept("op", "=") else None
            declarations.append(A.VarDecl(token=token, name=name, decl_type=type_,
                                          init=init, is_static=is_static))
            if not self.accept("op", ","):
                break
        self.expect("op", ";")
        if len(declarations) == 1:
            return declarations[0]
        return A.Block(token=token, statements=declarations, is_scope=False)

    def _if(self) -> A.If:
        token = self.take()
        self.expect("op", "(")
        condition = self._expression()
        self.expect("op", ")")
        then = self._statement()
        otherwise = None
        if self.at_kw("else"):
            self.take()
            otherwise = self._statement()
        return A.If(token=token, condition=condition, then=then, otherwise=otherwise)

    def _while(self) -> A.While:
        token = self.take()
        self.expect("op", "(")
        condition = self._expression()
        self.expect("op", ")")
        return A.While(token=token, condition=condition, body=self._statement())

    def _do_while(self) -> A.While:
        token = self.take()
        body = self._statement()
        if not self.at_kw("while"):
            raise self.current.error("expected 'while' after 'do' body")
        self.take()
        self.expect("op", "(")
        condition = self._expression()
        self.expect("op", ")")
        self.expect("op", ";")
        return A.While(token=token, condition=condition, body=body, is_do_while=True)

    def _for(self) -> A.For:
        token = self.take()
        self.expect("op", "(")
        if self.at_op(";"):
            self.take()
            init = None
        elif self._looks_like_type():
            init = self._local_declaration()      # consumes its own ';'
        else:
            init = A.ExprStatement(token=self.current, expr=self._expression())
            self.expect("op", ";")
        condition = None if self.at_op(";") else self._expression()
        self.expect("op", ";")
        step = None if self.at_op(")") else self._expression()
        self.expect("op", ")")
        return A.For(token=token, init=init, condition=condition, step=step,
                     body=self._statement())

    # --- expressions -------------------------------------------------------

    def _expression(self) -> A.Node:
        node = self._assignment()
        while self.at_op(","):
            token = self.take()
            node = A.Binary(token=token, op=",", left=node, right=self._assignment())
        return node

    def _assignment(self) -> A.Node:
        left = self._ternary()
        if self.current.kind == "op" and self.current.value in ASSIGN_OPS:
            token = self.take()
            return A.Assign(token=token, op=token.value, target=left,
                            value=self._assignment())
        return left

    def _ternary(self) -> A.Node:
        condition = self._binary(0)
        if self.at_op("?"):
            token = self.take()
            then = self._expression()
            self.expect("op", ":")
            return A.Conditional(token=token, condition=condition, then=then,
                                 otherwise=self._ternary())
        return condition

    def _binary(self, level: int) -> A.Node:
        if level >= len(PRECEDENCE):
            return self._unary()
        node = self._binary(level + 1)
        while self.current.kind == "op" and self.current.value in PRECEDENCE[level]:
            token = self.take()
            right = self._binary(level + 1)
            node = A.Binary(token=token, op=token.value, left=node, right=right)
        return node

    def _unary(self) -> A.Node:
        token = self.current

        if self.at_op("++", "--"):
            self.take()
            return A.IncDec(token=token, op=token.value, operand=self._unary(),
                            prefix=True)
        if self.at_op("-", "+", "!", "~", "*", "&"):
            self.take()
            return A.Unary(token=token, op=token.value, operand=self._unary())
        if self.at_kw("sizeof"):
            self.take()
            if self.at_op("(") and self._type_follows():
                self.take()
                type_ = self._abstract_type()
                self.expect("op", ")")
                return A.SizeOf(token=token, of_type=type_)
            return A.SizeOf(token=token, operand=self._unary())
        if self.at_op("(") and self._type_follows():
            self.take()
            type_ = self._abstract_type()
            self.expect("op", ")")
            return A.Cast(token=token, to=type_, operand=self._unary())

        return self._postfix()

    def _type_follows(self) -> bool:
        """Is the '(' at the cursor the start of a cast or sizeof(type)?"""
        token = self.peek(1)
        if token.kind == "kw":
            return (token.value in TYPE_KEYWORDS or token.value in QUALIFIERS
                    or token.value in REJECTED_TYPES)
        return token.kind == "id" and token.value in self.typedefs

    def _abstract_type(self) -> Type:
        base = self._type_specifier()
        type_, name = self._declarator(base)
        if name:
            raise self.current.error("unexpected name in a type")
        return type_

    def _postfix(self) -> A.Node:
        node = self._primary()
        while True:
            token = self.current
            if self.at_op("("):
                self.take()
                args = []
                while not self.at_op(")"):
                    args.append(self._assignment())
                    if not self.accept("op", ","):
                        break
                self.expect("op", ")")
                node = A.Call(token=token, callee=node, args=args)
            elif self.at_op("["):
                self.take()
                index = self._expression()
                self.expect("op", "]")
                node = A.Index(token=token, base=node, index=index)
            elif self.at_op("."):
                self.take()
                node = A.Member(token=token, obj=node,
                                name=self.expect("id").value, arrow=False)
            elif self.at_op("->"):
                self.take()
                node = A.Member(token=token, obj=node,
                                name=self.expect("id").value, arrow=True)
            elif self.at_op("++", "--"):
                self.take()
                node = A.IncDec(token=token, op=token.value, operand=node,
                                prefix=False)
            else:
                return node

    def _primary(self) -> A.Node:
        token = self.current
        if token.kind == "int":
            self.take()
            return A.IntLiteral(token=token, value=token.value)
        if token.kind == "str":
            self.take()
            return A.StringLiteral(token=token, value=token.value)
        if token.kind == "id":
            self.take()
            return A.Identifier(token=token, name=token.value)
        if self.at_op("("):
            self.take()
            node = self._expression()
            self.expect("op", ")")
            return node
        raise token.error(f"unexpected {token.value!r} in an expression")


def _size_from_initializer(type_, init):
    """`int v[] = {1,2,3}` -- an unsized array takes its length from the
    initialiser. The declarator parsed `[]` as a pointer, so rebuild it."""
    if init is None or not type_.is_pointer:
        return type_
    if isinstance(init, A.InitList):
        return array_of(type_.target, max(len(init.values), 1))
    if isinstance(init, A.StringLiteral):
        return array_of(type_.target, len(init.value) + 1)
    return type_


def _fold_constant(node: A.Node) -> int:
    """Evaluate a constant expression at parse time (array sizes)."""
    if isinstance(node, A.IntLiteral):
        return node.value
    if isinstance(node, A.Unary):
        value = _fold_constant(node.operand)
        return {"-": -value, "+": value, "~": ~value, "!": int(not value)}[node.op]
    if isinstance(node, A.Binary):
        left, right = _fold_constant(node.left), _fold_constant(node.right)
        ops = {"+": lambda: left + right, "-": lambda: left - right,
               "*": lambda: left * right, "/": lambda: left // right,
               "%": lambda: left % right, "<<": lambda: left << right,
               ">>": lambda: left >> right, "|": lambda: left | right,
               "&": lambda: left & right, "^": lambda: left ^ right}
        if node.op in ops:
            return ops[node.op]()
    raise node.token.error("expected a compile-time constant")


def parse(tokens: List[Token]) -> A.Program:
    return Parser(tokens).parse()
