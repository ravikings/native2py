"""Intermediate representation shared by all language parsers and binding generators.

Parsers (cpp.py, fortran.py, ...) normalize source into these dataclasses.
Generators (pybind_gen.py, f2py_gen.py, ...) consume them without ever
knowing which language the API came from.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Deterministic native -> Python type mapping (design.md section 15).
# Unsupported types raise NativeTypeError rather than silently binding unsafely.
TYPE_MAP = {
    "int": "int",
    "int32_t": "int",
    "int64_t": "int",
    "float": "float",
    "double": "float",
    "bool": "bool",
    "void": "None",
    "std::string": "str",
    # Integer spellings that are ordinary in pre-standard C++ and were
    # previously fatal to the whole header.
    "short": "int",
    "long": "int",
    "long long": "int",
    "unsigned": "int",
    "unsigned int": "int",
    "unsigned short": "int",
    "unsigned long": "int",
    "size_t": "int",
    "std::size_t": "int",
    "int8_t": "int",
    "int16_t": "int",
    "uint8_t": "int",
    "uint16_t": "int",
    "uint32_t": "int",
    "uint64_t": "int",
    "long double": "float",
    "char": "str",
}


class NativeTypeError(ValueError):
    """Raised when a native type has no deterministic Python/pybind11 mapping."""


def map_type(native_type: str) -> str:
    normalized = native_type.strip().rstrip("&").strip()
    if normalized not in TYPE_MAP:
        raise NativeTypeError(
            f"Unsupported native type '{native_type}': add a mapping in ir.TYPE_MAP "
            "or exclude this symbol from native2py.yaml."
        )
    return TYPE_MAP[normalized]


# Fortran -> Python type mapping (design.md section 15). Kind parameters
# (real(8), integer(4), ...) are normalized to the base type; arrays are
# handled separately via Parameter.is_array, not encoded in the type name.
FORTRAN_TYPE_MAP = {
    "real": "float",
    "double precision": "float",
    "integer": "int",
    "logical": "bool",
    "character": "str",
    "complex": "complex",
}


def map_fortran_type(native_type: str) -> str:
    normalized = native_type.strip().lower()
    normalized = re.sub(r"\(.*\)", "", normalized).strip()
    if normalized not in FORTRAN_TYPE_MAP:
        raise NativeTypeError(
            f"Unsupported Fortran type '{native_type}': add a mapping in ir.FORTRAN_TYPE_MAP "
            "or exclude this symbol from native2py.yaml."
        )
    return FORTRAN_TYPE_MAP[normalized]


@dataclass
class Parameter:
    name: str
    type: str
    is_array: bool = False
    intent: str = "in"  # "in" | "out" | "inout" — Fortran calling convention


@dataclass
class Method:
    name: str
    parameters: list[Parameter] = field(default_factory=list)
    returns: str = "void"
    is_static: bool = False


@dataclass
class ClassDef:
    name: str
    namespace: str | None = None
    methods: list[Method] = field(default_factory=list)
    has_default_constructor: bool = True
    # Parameter lists of the public constructors that take arguments. A class
    # whose only constructors take arguments has has_default_constructor=False
    # and would otherwise be unconstructible from Python.
    constructors: list[list[Parameter]] = field(default_factory=list)
    # Public data members, bound read/write.
    fields: list[Parameter] = field(default_factory=list)
    # Public base classes, in declaration order, that are themselves bound by
    # this service. pybind11 needs them named in the class_ declaration
    # (`py::class_<Derived, Base>`) or a Python caller holding a Derived
    # cannot call anything it inherits — and a C++ API that hands back a
    # Base& gets a Python object with none of the derived methods.
    bases: list[str] = field(default_factory=list)


@dataclass
class StructDef:
    """A plain data struct — no methods, just public fields.

    Numerical APIs return these constantly (a PVT state, a solver result) and
    without an IR node for them the methods that return one are unbindable.
    """

    name: str
    namespace: str | None = None
    fields: list[Parameter] = field(default_factory=list)


@dataclass
class SkippedSymbol:
    """A declaration the parser recognised but cannot bind, and why.

    Reported rather than raised: one unsupported signature should not cost
    you every other symbol in the same header.
    """

    name: str
    reason: str


@dataclass
class FunctionDef:
    name: str
    parameters: list[Parameter] = field(default_factory=list)
    returns: str = "void"
    is_subroutine: bool = False  # Fortran subroutines have no return value; results come back via intent(out/inout)
    # Enclosing `module X ... end module X`, if any. f2py nests those routines
    # under `<ext>.X.<name>` while bare F77 routines land at the top level, so
    # this has to be tracked per routine: a modern F90 facade sitting on top of
    # fixed-form decks legitimately has both in one service.
    fortran_module: str | None = None


@dataclass
class ModuleIR:
    """Everything exposed from one source file, in language-neutral form."""

    name: str
    language: str  # "cpp" | "fortran"
    source_file: str
    classes: list[ClassDef] = field(default_factory=list)
    functions: list[FunctionDef] = field(default_factory=list)
    structs: list[StructDef] = field(default_factory=list)
    fortran_module: str | None = None  # name of the enclosing `module X` block, if any (f2py nests under it)
    # Declarations recognised but not bindable, with the reason. Surfaced by
    # the CLI so "nothing was generated" is never silent.
    skipped: list[SkippedSymbol] = field(default_factory=list)
    # Compiler errors from the parse itself ("'foo.hpp' file not found"), when
    # the parser is a real compiler front end. A header that doesn't compile
    # still yields a partial AST, and binding half a header silently is worse
    # than saying so. Always empty for the regex-based parsers.
    diagnostics: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.classes and not self.functions and not self.structs


# --- serialisation ------------------------------------------------------
#
# `generate` writes the IR next to the service so later tooling can answer
# "what does this service expose?" without re-parsing the native sources —
# which would need the compiler, the include paths and the same libclang all
# over again, on a machine that may only have the built wheel.


def module_to_dict(module: ModuleIR) -> dict:
    return asdict(module)


def module_from_dict(data: dict) -> ModuleIR:
    def parameters(items) -> list[Parameter]:
        return [Parameter(**item) for item in items]

    return ModuleIR(
        name=data["name"],
        language=data["language"],
        source_file=data["source_file"],
        classes=[
            ClassDef(
                name=cls["name"],
                namespace=cls.get("namespace"),
                methods=[
                    Method(
                        name=m["name"],
                        parameters=parameters(m.get("parameters", [])),
                        returns=m.get("returns", "void"),
                        is_static=m.get("is_static", False),
                    )
                    for m in cls.get("methods", [])
                ],
                has_default_constructor=cls.get("has_default_constructor", True),
                constructors=[parameters(c) for c in cls.get("constructors", [])],
                fields=parameters(cls.get("fields", [])),
                bases=list(cls.get("bases", [])),
            )
            for cls in data.get("classes", [])
        ],
        functions=[
            FunctionDef(
                name=fn["name"],
                parameters=parameters(fn.get("parameters", [])),
                returns=fn.get("returns", "void"),
                is_subroutine=fn.get("is_subroutine", False),
                fortran_module=fn.get("fortran_module"),
            )
            for fn in data.get("functions", [])
        ],
        structs=[
            StructDef(
                name=struct["name"],
                namespace=struct.get("namespace"),
                fields=parameters(struct.get("fields", [])),
            )
            for struct in data.get("structs", [])
        ],
        fortran_module=data.get("fortran_module"),
        skipped=[SkippedSymbol(**item) for item in data.get("skipped", [])],
        diagnostics=list(data.get("diagnostics", [])),
    )
