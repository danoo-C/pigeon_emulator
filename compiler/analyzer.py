"""Semantic analysis: resolve names, check types, lay out frames.

The frame layout is the interesting part. Per compiler/design/03-abi.md,
the stack pointer cannot be read on this machine, so locals cannot live at
SP-relative addresses. They live on a software frame stack addressed
through F, at fixed positive offsets assigned here:

    F + 0                    parameter 0     (written by the CALLER)
    F + 4*(nparams-1)        parameter n-1
    F + 4*nparams            local 0
    ...
    F + frame_size           where this function's callees' frames begin

`frame_size` is a compile-time constant per function, which is what makes
the whole convention work: the caller adjusts F by its own constant around
each call, because the callee cannot know it.
"""
from typing import Dict, List, Optional

from . import ast_nodes as A
from .lexer import CompileError
from .typesys import (CHAR, INT, UCHAR, UINT, VA_SLOTS, VOID, ArrayType, FunctionType,
                    PointerType, StructType, Type, WORD, common_type, decays,
                    is_integer, pointer_to)


class Symbol:
    """A named thing and where it lives."""

    def __init__(self, name, type_, storage, offset=0, label=""):
        self.name = name
        self.type = type_
        self.storage = storage      # 'local', 'param', 'global', 'function'
        self.offset = offset        # frame offset for local/param
        self.label = label          # assembly label for global/function

    def __repr__(self):
        where = f"F+{self.offset}" if self.storage in ("local", "param") else self.label
        return f"<{self.storage} {self.name}: {self.type} @ {where}>"


class Scope:
    def __init__(self, parent: Optional["Scope"] = None):
        self.parent = parent
        self.names: Dict[str, Symbol] = {}

    def declare(self, symbol: Symbol, token):
        if symbol.name in self.names:
            raise token.error(f"'{symbol.name}' is already declared in this scope")
        self.names[symbol.name] = symbol
        return symbol

    def lookup(self, name: str) -> Optional[Symbol]:
        scope = self
        while scope:
            if name in scope.names:
                return scope.names[name]
            scope = scope.parent
        return None


class Analyzer:
    def __init__(self, require_main: bool = True):
        self.require_main = require_main
        self.globals = Scope()
        self.scope = self.globals
        self.function: Optional[A.FunctionDef] = None
        self.frame_top = 0          # next free frame offset in the current function
        self.max_frame = 0
        self.loop_depth = 0
        self.strings: List[tuple] = []
        self._string_count = 0

    # --- scope helpers -----------------------------------------------------

    def push_scope(self):
        self.scope = Scope(self.scope)

    def pop_scope(self):
        self.scope = self.scope.parent

    def _alloc_frame(self, size: int) -> int:
        offset = self.frame_top
        self.frame_top += (size + WORD - 1) // WORD * WORD
        self.max_frame = max(self.max_frame, self.frame_top)
        return offset

    # --- entry point -------------------------------------------------------

    def analyze(self, program: A.Program) -> A.Program:
        # Declare every function first, so calls may appear before definitions
        # and mutual recursion works.
        for function in program.functions:
            # A struct moves four bytes at a time everywhere it is copied
            # (docs/compiler_plan.md §2), and the ABI is a word a slot, so
            # by-value parameters and returns are refused rather than
            # silently truncated. Assignment is the one that is implemented.
            if isinstance(function.returns, StructType):
                raise function.token.error(
                    f"'{function.name}' cannot return {function.returns} by value; "
                    f"fill one through a pointer parameter")
            for param in function.params:
                if isinstance(param.type, StructType):
                    raise function.token.error(
                        f"parameter '{param.name}' cannot take {param.type} by value; "
                        f"take '{param.type} *' instead")
            signature = FunctionType(function.returns,
                                     [p.type for p in function.params], function.name,
                                     function.variadic)
            if self.globals.lookup(function.name):
                raise function.token.error(f"'{function.name}' is defined twice")
            function.label = _function_label(function.name)
            self.globals.declare(
                Symbol(function.name, signature, "function", label=function.label),
                function.token)

        for declaration in program.globals:
            self._global(declaration)

        for function in program.functions:
            self._function(function)

        program.strings = self.strings
        if self.require_main and not self.globals.lookup("main"):
            raise program.token.error("no main() -- the startup stub has nothing to call")
        return program

    def _global(self, declaration: A.VarDecl):
        if declaration.decl_type == VOID:
            raise declaration.token.error(f"'{declaration.name}' cannot be void")
        if self.globals.lookup(declaration.name):
            # A repeated declaration of the same extern is normal when two
            # translation units are compiled as one.
            existing = self.globals.lookup(declaration.name)
            if declaration.is_extern or getattr(existing, "is_extern", False):
                declaration.symbol = existing
                return
        # `extern unsigned x;` names a symbol defined elsewhere -- in the
        # assembly the compiler itself emits, or in another unit -- so it
        # keeps its own name and reserves no storage here.
        label = declaration.name if declaration.is_extern else f"__g_{declaration.name}"
        symbol = self.globals.declare(
            Symbol(declaration.name, declaration.decl_type, "global", label=label),
            declaration.token)
        symbol.is_extern = declaration.is_extern
        declaration.symbol = symbol
        if declaration.init is not None:
            if isinstance(declaration.init, A.InitList):
                self._init_list(declaration)
            elif isinstance(declaration.init, A.StringLiteral) \
                    and isinstance(declaration.decl_type, ArrayType):
                pass                        # emitted directly as .asciz
            else:
                # A global's value is built into the image, so it has to be
                # known now. Everything but a bare literal used to be
                # dropped to zero in silence (docs/compiler_plan.md §3); it
                # is folded here instead, and refused when it cannot be.
                declaration.init = self._const_init(self._expr(declaration.init),
                                                    declaration.decl_type)

    def _const_init(self, value: A.Node, target: Type) -> A.Node:
        """A global's initialiser as something the image can hold: a folded
        IntLiteral, or an AddressLiteral naming a label."""
        address = _address_constant(value)
        if address is not None:
            if not (decays(target).is_pointer or is_integer(target)):
                raise value.token.error(f"cannot initialise {target} with an address")
            return address
        try:
            number = _fold_const_expr(value)
        except CompileError:
            raise
        except Exception:
            number = None
        if number is None:
            raise value.token.error(
                "a global initialiser must be a compile-time constant")
        folded = A.IntLiteral(token=value.token, value=number & 0xFFFFFFFF)
        folded.type = INT
        return folded

    def _init_list(self, declaration: A.VarDecl):
        """Only constants, and only on a global: the value has to be known
        when the image is built, because that is where it is stored."""
        target = declaration.decl_type
        if not isinstance(target, ArrayType):
            raise declaration.token.error(
                f"'{declaration.name}' is {target}; a brace initialiser needs an array")
        values = declaration.init.values
        if len(values) > target.count:
            raise declaration.token.error(
                f"'{declaration.name}' holds {target.count} elements but "
                f"{len(values)} were given")
        folded = [self._const_init(self._expr(value), target.element) for value in values]
        declaration.init.values = folded

    def _function(self, function: A.FunctionDef):
        self.function = function
        self.frame_top = 0
        self.max_frame = 0
        self.push_scope()

        for param in function.params:
            if param.type == VOID:
                raise function.token.error(f"parameter '{param.name}' cannot be void")
            offset = self._alloc_frame(max(param.type.size, WORD))
            self.scope.declare(Symbol(param.name, param.type, "param", offset=offset),
                               function.token)
        if function.variadic:
            # The extra arguments' slots, straight after the named parameters:
            # a caller writes them there like any argument, va_start finds
            # them past the last named one, and the locals start after them.
            self._alloc_frame(VA_SLOTS * WORD)

        self._statement(function.body)

        self.pop_scope()
        function.frame_size = self.max_frame
        self.function = None

    # --- statements --------------------------------------------------------

    def _statement(self, node: A.Node):
        if node is None:
            return

        if isinstance(node, A.Block):
            if not node.is_scope:           # `int a, b;` -- same scope, same frame
                for statement in node.statements:
                    self._statement(statement)
                return
            self.push_scope()
            saved = self.frame_top          # a block's locals are reclaimed at its end
            for statement in node.statements:
                self._statement(statement)
            self.frame_top = saved
            self.pop_scope()

        elif isinstance(node, A.VarDecl):
            self._local_decl(node)

        elif isinstance(node, A.If):
            node.condition = self._expr(node.condition)
            self._statement(node.then)
            self._statement(node.otherwise)

        elif isinstance(node, A.While):
            node.condition = self._expr(node.condition)
            self.loop_depth += 1
            self._statement(node.body)
            self.loop_depth -= 1

        elif isinstance(node, A.For):
            self.push_scope()
            saved = self.frame_top
            self._statement(node.init)
            if node.condition is not None:
                node.condition = self._expr(node.condition)
            if node.step is not None:
                node.step = self._expr(node.step)
            self.loop_depth += 1
            self._statement(node.body)
            self.loop_depth -= 1
            self.frame_top = saved
            self.pop_scope()

        elif isinstance(node, A.Return):
            if node.value is not None:
                node.value = self._expr(node.value)
                if self.function.returns == VOID:
                    raise node.token.error(
                        f"'{self.function.name}' returns void but this returns a value")
                if node.value.type == VOID:
                    raise node.token.error("cannot return a void value")
            elif self.function.returns != VOID:
                raise node.token.error(
                    f"'{self.function.name}' must return {self.function.returns}")

        elif isinstance(node, (A.Break, A.Continue)):
            if not self.loop_depth:
                word = "break" if isinstance(node, A.Break) else "continue"
                raise node.token.error(f"'{word}' outside a loop")

        elif isinstance(node, A.ExprStatement):
            node.expr.is_statement = True    # a struct assignment is allowed here
            node.expr = self._expr(node.expr)

        elif isinstance(node, A.Empty):
            pass

        else:
            raise node.token.error(f"cannot analyse {type(node).__name__}")

    def _local_decl(self, node: A.VarDecl):
        if node.decl_type == VOID:
            raise node.token.error(f"'{node.name}' cannot be void")
        if node.is_static:
            raise node.token.error(
                "static locals are not supported yet; use a global")
        if isinstance(node.init, A.InitList):
            raise node.token.error(
                "brace initialisers work on globals only, because the value "
                "is stored in the image; assign the elements in code instead")

        offset = self._alloc_frame(max(node.decl_type.size, WORD))
        node.symbol = self.scope.declare(
            Symbol(node.name, node.decl_type, "local", offset=offset), node.token)
        if node.init is not None:
            node.init = self._expr(node.init)
            self._check_assignable(node.decl_type, node.init, node.token)
            if isinstance(node.decl_type, StructType):
                self._check_struct_copy(node.decl_type, node.init, node.token, False)

    # --- expressions -------------------------------------------------------

    def _expr(self, node: A.Node) -> A.Node:
        method = getattr(self, f"_x_{type(node).__name__}", None)
        if method is None:
            raise node.token.error(f"cannot analyse expression {type(node).__name__}")
        return method(node)

    def _x_IntLiteral(self, node):
        node.type = INT
        return node

    def _x_StringLiteral(self, node):
        label = f"__str{self._string_count}"
        self._string_count += 1
        self.strings.append((label, node.value))
        node.label = label
        node.type = pointer_to(CHAR)
        return node

    def _x_Identifier(self, node):
        symbol = self.scope.lookup(node.name)
        if symbol is None:
            raise node.token.error(f"'{node.name}' is not declared")
        node.symbol = symbol
        node.type = symbol.type
        return node

    def _x_Unary(self, node):
        node.operand = self._expr(node.operand)
        operand_type = node.operand.type

        if node.op == "&":
            if not _is_lvalue(node.operand):
                raise node.token.error("cannot take the address of this")
            node.type = pointer_to(operand_type)
        elif node.op == "*":
            target = decays(operand_type)
            if not target.is_pointer:
                raise node.token.error(f"cannot dereference {operand_type}")
            node.type = target.target
        elif node.op == "!":
            node.type = INT
        else:                                   # '-', '+', '~'
            if not is_integer(operand_type):
                raise node.token.error(f"'{node.op}' needs an integer, got {operand_type}")
            node.type = INT if operand_type.is_signed else UINT
        return node

    def _x_Binary(self, node):
        node.left = self._expr(node.left)
        node.right = self._expr(node.right)
        left, right = decays(node.left.type), decays(node.right.type)

        if node.op == ",":
            node.type = right
            return node

        if node.op in ("&&", "||"):
            node.type = INT
            return node

        if node.op in ("==", "!=", "<", ">", "<=", ">="):
            node.type = INT
            node.operand_type = common_type(left, right)
            return node

        if node.op in ("+", "-") and (left.is_pointer or right.is_pointer):
            node.type = self._pointer_arithmetic(node, left, right)
            return node

        if not (is_integer(left) and is_integer(right)):
            raise node.token.error(
                f"'{node.op}' does not apply to {node.left.type} and {node.right.type}")
        node.type = common_type(left, right)
        # The machine's DIV and SHR are unsigned, so codegen puts the sign
        # back where the types say there is one (docs/compiler_plan.md §4,
        # §5). A shift takes its signedness from the value being shifted,
        # not from the count -- `x >> n` is about x.
        if node.op in ("/", "%"):
            node.operand_type = node.type
        elif node.op in ("<<", ">>"):
            node.operand_type = left
        return node

    def _pointer_arithmetic(self, node, left, right):
        if left.is_pointer and right.is_pointer:
            if node.op != "-":
                raise node.token.error("cannot add two pointers")
            node.pointer_diff = left.target.size
            return INT
        pointer, integer = (left, right) if left.is_pointer else (right, left)
        if not is_integer(integer):
            raise node.token.error(f"cannot offset a pointer by {integer}")
        if left.is_pointer is False and node.op == "-":
            raise node.token.error("cannot subtract a pointer from an integer")
        node.scale = pointer.target.size          # codegen multiplies the index by this
        return pointer

    def _x_Assign(self, node):
        node.target = self._expr(node.target)
        node.value = self._expr(node.value)
        if not _is_lvalue(node.target):
            raise node.token.error("cannot assign to this")
        if isinstance(node.target.type, ArrayType):
            raise node.token.error("cannot assign to an array")
        if isinstance(decays(node.target.type), StructType):
            self._check_struct_copy(node.target.type, node.value, node.token,
                                    node.op != "=" or not getattr(node, "is_statement", False))
        self._check_assignable(node.target.type, node.value, node.token)
        node.type = node.target.type
        if node.op != "=":
            # `a /= b` is `a = a / b`, and codegen rebuilds that Binary --
            # so it needs the same signedness a written-out one would have
            # (docs/compiler_plan.md §4, §5).
            left, right = decays(node.target.type), decays(node.value.type)
            if node.op[:-1] in ("/", "%") and is_integer(left) and is_integer(right):
                node.operand_type = common_type(left, right)
            elif node.op[:-1] in ("<<", ">>"):
                node.operand_type = left
        return node

    def _x_IncDec(self, node):
        node.operand = self._expr(node.operand)
        if not _is_lvalue(node.operand):
            raise node.token.error(f"cannot apply '{node.op}' to this")
        operand_type = decays(node.operand.type)
        node.scale = operand_type.target.size if operand_type.is_pointer else 1
        node.type = node.operand.type
        return node

    def _x_Conditional(self, node):
        node.condition = self._expr(node.condition)
        node.then = self._expr(node.then)
        node.otherwise = self._expr(node.otherwise)
        node.type = common_type(node.then.type, node.otherwise.type)
        return node

    def _x_Call(self, node):
        node.callee = self._expr(node.callee)
        node.args = [self._expr(arg) for arg in node.args]

        signature = node.callee.type
        if isinstance(signature, PointerType) and isinstance(signature.target, FunctionType):
            signature = signature.target
        if not isinstance(signature, FunctionType):
            raise node.token.error(f"{node.callee.type} is not callable")

        named = len(signature.params)
        extra = len(node.args) - named
        if extra < 0 or (extra > 0 and not signature.variadic) or extra > VA_SLOTS:
            name = getattr(node.callee, "name", "this function")
            if signature.variadic:
                raise node.token.error(
                    f"{name} takes {named} argument(s) and up to {VA_SLOTS} more, "
                    f"got {len(node.args)}")
            raise node.token.error(
                f"{name} takes {named} argument(s), got {len(node.args)}")
        for arg, expected in zip(node.args, signature.params):
            self._check_assignable(expected, arg, node.token)
        for arg in node.args[named:]:
            passed = decays(arg.type)
            if not (is_integer(passed) or passed.is_pointer or isinstance(passed, FunctionType)):
                raise node.token.error(
                    f"cannot pass {arg.type} as an extra argument: each one is a word, "
                    f"an integer, a char or a pointer")
        node.type = signature.returns
        return node

    def _x_Index(self, node):
        node.base = self._expr(node.base)
        node.index = self._expr(node.index)
        base = decays(node.base.type)
        if not base.is_pointer:
            raise node.token.error(f"cannot subscript {node.base.type}")
        if not is_integer(decays(node.index.type)):
            raise node.token.error(f"array index must be an integer, got {node.index.type}")
        node.scale = base.target.size
        node.type = base.target
        return node

    def _x_Member(self, node):
        node.obj = self._expr(node.obj)
        owner = node.obj.type
        if node.arrow:
            owner = decays(owner)
            if not owner.is_pointer:
                raise node.token.error(f"'->' needs a pointer, got {node.obj.type}")
            owner = owner.target
        if not isinstance(owner, StructType):
            raise node.token.error(
                f"'{'->' if node.arrow else '.'}' needs a struct, got {node.obj.type}")
        field = owner.field(node.name)
        if field is None:
            raise node.token.error(f"{owner} has no member '{node.name}'")
        node.field = field
        node.type = field.type
        return node

    def _x_Cast(self, node):
        node.operand = self._expr(node.operand)
        node.type = node.to
        return node

    def _x_SizeOf(self, node):
        if node.of_type is None:
            node.operand = self._expr(node.operand)
            node.of_type = node.operand.type
        node.type = UINT
        return node

    # --- type checking -----------------------------------------------------

    def _check_struct_copy(self, target: Type, value: A.Node, token, as_value: bool):
        """A whole struct is copied word by word (docs/compiler_plan.md §2),
        which needs an address to copy FROM and nothing to leave behind: so
        the source is an lvalue, and the copy is a statement rather than a
        value."""
        if as_value:
            raise token.error(
                f"a {target} can only be copied as a whole statement -- "
                f"'a = b;', not part of a larger expression")
        if not _is_lvalue(value):
            raise value.token.error(
                f"a {target} can only be copied from a variable, "
                f"since the copy reads it through its address")

    def _check_assignable(self, target: Type, value: A.Node, token):
        source = decays(value.type)
        target = decays(target)
        if target == VOID or source == VOID:
            raise token.error("cannot use a void value")
        if target.is_pointer and isinstance(source, FunctionType):
            return                              # a function name decays to a pointer
        if target.is_pointer and source.is_pointer:
            return                              # pointer conversions are permitted
        if target.is_pointer and is_integer(source):
            return                              # an integer address, as MMIO needs
        if is_integer(target) and source.is_pointer:
            return
        if is_integer(target) and is_integer(source):
            return
        if isinstance(target, StructType) and target == source:
            return
        raise token.error(f"cannot assign {value.type} to {target}")


def _function_label(name: str) -> str:
    """Assembly label for a function.

    Names are kept verbatim so generated assembly stays readable -- except
    single letters that collide with a register name, where the assembler
    (rightly) refuses the ambiguity: `CALL f` cannot mean both "call the
    function f" and "call through register F".
    """
    if len(name) == 1 and name.upper() in "ABCDEF":
        return f"__fn_{name}"
    return name


def _address_constant(value: A.Node) -> Optional[A.AddressLiteral]:
    """`&global`, a function's name, an array's name or a string literal as
    a global initialiser: the label the image should hold, or None if this
    is not one of those.

    Only a bare address -- `&arr[2]` and `name + 1` are refused, because
    the point of this is to catch what used to be a silent zero, not to
    grow a link-time expression evaluator.
    """
    if isinstance(value, A.StringLiteral):
        return A.AddressLiteral(token=value.token, label=value.label, type=value.type)
    if isinstance(value, A.Unary) and value.op == "&":
        target = value.operand
        if isinstance(target, A.Identifier) and target.symbol.storage == "global":
            return A.AddressLiteral(token=value.token, label=target.symbol.label,
                                    type=value.type)
        return None
    if isinstance(value, A.Identifier) and value.symbol.storage == "function":
        return A.AddressLiteral(token=value.token, label=value.symbol.label,
                                type=value.type)
    if isinstance(value, A.Identifier) and value.symbol.storage == "global" \
            and isinstance(value.symbol.type, ArrayType):
        return A.AddressLiteral(token=value.token, label=value.symbol.label,
                                type=value.type)
    return None


def _fold_const_expr(node: A.Node) -> Optional[int]:
    """Evaluate an ANALYSED expression at compile time, or None.

    parser._fold_constant does the same for array sizes, before types are
    known; this one runs after, so it can also do `sizeof` and casts. The
    two exist for that reason and not by accident.
    """
    if isinstance(node, A.IntLiteral):
        return node.value
    if isinstance(node, A.SizeOf):
        return node.of_type.size
    if isinstance(node, A.Cast):
        inner = _fold_const_expr(node.operand)
        if inner is None or not is_integer(node.to):
            return None
        bits = node.to.size * 8
        inner &= (1 << bits) - 1
        if node.to.is_signed and inner >= 1 << (bits - 1):
            inner -= 1 << bits
        return inner
    if isinstance(node, A.Unary):
        inner = _fold_const_expr(node.operand)
        if inner is None or node.op not in ("-", "+", "~", "!"):
            return None
        return {"-": -inner, "+": inner, "~": ~inner, "!": int(not inner)}[node.op]
    if isinstance(node, A.Binary):
        left, right = _fold_const_expr(node.left), _fold_const_expr(node.right)
        if left is None or right is None:
            return None
        if node.op in ("/", "%") and right == 0:
            raise node.token.error("division by zero in a constant")
        ops = {"+": lambda: left + right, "-": lambda: left - right,
               "*": lambda: left * right, "<<": lambda: left << right,
               ">>": lambda: left >> right, "|": lambda: left | right,
               "&": lambda: left & right, "^": lambda: left ^ right,
               "/": lambda: abs(left) // abs(right) * (1 if (left < 0) == (right < 0) else -1),
               "%": lambda: left - (abs(left) // abs(right)
                                    * (1 if (left < 0) == (right < 0) else -1)) * right,
               "==": lambda: int(left == right), "!=": lambda: int(left != right),
               "<": lambda: int(left < right), ">": lambda: int(left > right),
               "<=": lambda: int(left <= right), ">=": lambda: int(left >= right),
               "&&": lambda: int(bool(left) and bool(right)),
               "||": lambda: int(bool(left) or bool(right))}
        return ops[node.op]() if node.op in ops else None
    if isinstance(node, A.Conditional):
        condition = _fold_const_expr(node.condition)
        if condition is None:
            return None
        return _fold_const_expr(node.then if condition else node.otherwise)
    return None


def _is_lvalue(node: A.Node) -> bool:
    if isinstance(node, A.Identifier):
        return node.symbol.storage != "function"
    if isinstance(node, A.Unary):
        return node.op == "*"
    return isinstance(node, (A.Index, A.Member))


def analyze(program: A.Program, require_main: bool = True) -> A.Program:
    return Analyzer(require_main).analyze(program)
