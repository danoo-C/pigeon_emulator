"""The type system.

Everything is 32 bits or smaller. `int` is 4 bytes, `char` is 1, every
pointer is 4 -- the address space is 128 MB, so 32 bits addresses it
exactly.

Signedness is load-bearing here in a way it is not on most targets: the
machine's CMP is unsigned, so a signed comparison costs two extra XORs
(see compiler/design/04-codegen.md). Declaring a loop counter `unsigned`
produces measurably smaller code, and `is_signed` is what codegen checks
to decide.
"""
from dataclasses import dataclass, field
from typing import List, Optional

WORD = 4


@dataclass(frozen=True)
class Type:
    name: str
    size: int
    is_signed: bool = True

    @property
    def is_pointer(self) -> bool:
        return False

    @property
    def is_scalar(self) -> bool:
        return True

    def __str__(self):
        return self.name


INT = Type("int", 4, True)
UINT = Type("unsigned int", 4, False)
CHAR = Type("char", 1, True)
UCHAR = Type("unsigned char", 1, False)
VOID = Type("void", 0, False)


@dataclass(frozen=True)
class PointerType(Type):
    target: Optional[Type] = None

    @property
    def is_pointer(self) -> bool:
        return True

    def __str__(self):
        if isinstance(self.target, FunctionType):
            return str(self.target)          # already spelt "int (*)(int)"
        return f"{self.target}*"


def pointer_to(target: Type) -> PointerType:
    # Pointers are unsigned: an address is never negative, and making them
    # unsigned keeps pointer comparisons off the signed-compare path.
    return PointerType(f"{target}*", WORD, False, target)


@dataclass(frozen=True)
class ArrayType(Type):
    element: Optional[Type] = None
    count: int = 0

    @property
    def is_scalar(self) -> bool:
        return False

    def __str__(self):
        return f"{self.element}[{self.count}]"


def array_of(element: Type, count: int) -> ArrayType:
    return ArrayType(f"{element}[{count}]", element.size * count, False, element, count)


@dataclass
class Field:
    name: str
    type: Type
    offset: int


@dataclass(frozen=True)
class StructType(Type):
    tag: str = ""
    fields: tuple = ()

    @property
    def is_scalar(self) -> bool:
        return False

    def field(self, name: str) -> Optional[Field]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def __str__(self):
        return f"struct {self.tag}" if self.tag else "struct"


def layout_struct(tag: str, members: List[tuple]) -> StructType:
    """Lay members out in declaration order, each aligned to its own size.

    The machine does not fault on an unaligned word access (RAM.read_word
    masks per byte), so this is a convention for speed and predictability
    rather than a hardware requirement.
    """
    fields, offset = [], 0
    for name, type_ in members:
        align = min(type_.size if type_.is_scalar else WORD, WORD) or 1
        offset = (offset + align - 1) // align * align
        fields.append(Field(name, type_, offset))
        offset += type_.size
    size = (offset + WORD - 1) // WORD * WORD if offset else 0
    return StructType(f"struct {tag}" if tag else "struct", size, False,
                      tag, tuple(fields))


@dataclass
class FunctionType:
    returns: Type
    params: List[Type] = field(default_factory=list)
    name: str = ""

    size = WORD
    is_signed = False
    is_pointer = False
    is_scalar = True

    def __str__(self):
        return f"{self.returns} (*)({', '.join(str(p) for p in self.params)})"


def is_integer(type_: Type) -> bool:
    return type_ in (INT, UINT, CHAR, UCHAR)


def decays(type_: Type) -> Type:
    """An array used as a value becomes a pointer to its first element."""
    return pointer_to(type_.element) if isinstance(type_, ArrayType) else type_


def common_type(left: Type, right: Type) -> Type:
    """The type a binary operation is performed in.

    Pointer wins over integer (pointer arithmetic); otherwise if either
    side is unsigned the operation is unsigned, which is what decides
    whether codegen emits the signed-compare sequence.
    """
    left, right = decays(left), decays(right)
    if left.is_pointer:
        return left
    if right.is_pointer:
        return right
    if not left.is_signed or not right.is_signed:
        return UINT
    return INT
