"""Intermediate representation shared by all language parsers and binding generators.

Parsers (cpp.py, fortran.py, ...) normalize source into these dataclasses.
Generators (pybind_gen.py, f2py_gen.py, ...) consume them without ever
knowing which language the API came from.
"""

from __future__ import annotations

import re
import warnings
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
    # The type as the native source spells it ("std::uint64_t", "Correlation",
    # "type(pvt_state)"). `type` above is the *Python* mapping and is lossy by
    # construction: uint64_t, int and an unscoped enum all collapse to "int".
    # Generators that must emit native code — pybind11's `py::init<...>`, which
    # cannot recover the type from a function pointer — need the real spelling
    # or they guess, truncate a 64-bit id, and select the wrong overload.
    native_type: str | None = None
    # `const` in C++. A const data member bound with def_readwrite is a compile
    # error inside pybind11's templates.
    is_const: bool = False
    # Fortran `optional` attribute. Without it an optional argument is
    # generated as required and the caller cannot express "absent".
    is_optional: bool = False

    def cpp_spelling(self, fallback: str) -> str:
        """The C++ type to emit, preferring the recorded native spelling."""
        return self.native_type or fallback


@dataclass
class Method:
    name: str
    parameters: list[Parameter] = field(default_factory=list)
    returns: str = "void"
    is_static: bool = False
    # `double f() const` — required to disambiguate an overload, because
    # py::overload_cast needs py::const_ for the const member of a const/
    # non-const pair.
    is_const: bool = False
    # Set when this class declares more than one method of this name. Taking
    # `&Cls::name` is then ambiguous and does not compile; the binding has to
    # go through py::overload_cast<...> with the argument types spelled out.
    is_overloaded: bool = False


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
    # False when the struct has a const or reference member, or otherwise
    # declares no default constructor. Emitting py::init<>() for one is a
    # compile error, and it was emitted unconditionally.
    has_default_constructor: bool = True


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
    # Enclosing C++ namespace. Classes and structs have always carried one;
    # free functions did not, so `m.def("f", &f)` was emitted unqualified and
    # any function outside the global namespace failed to compile with
    # "use of undeclared identifier".
    namespace: str | None = None
    # Set when the translation unit declares more than one function of this
    # name — same ambiguity as Method.is_overloaded.
    is_overloaded: bool = False
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


# --- Python identifier safety -------------------------------------------
#
# Native names become Python identifiers in the generated package, the router
# and the smoke tests. Neither source language reserves Python's keywords, and
# `lambda` in particular is near-universal in engineering C++ — so a perfectly
# ordinary header produced `def attenuate(lambda: float)`, a SyntaxError that
# surfaced when the container started rather than when the code was generated.
#
# Note this is NOT solved by generating through `ast`: ast.unparse happily
# emits `lambda` for ast.Name(id="lambda"). It has to be an explicit check.

import keyword


def python_identifier(name: str) -> str:
    """A safe Python spelling of `name`, per PEP 8's trailing underscore.

    Only the Python-side name changes. Callers keep the original for the native
    symbol lookup and for the HTTP route, so the wire contract is unaffected.
    """
    # Only TRUE keywords. Soft keywords (`match`, `case`, `type`, `_`) are not
    # reserved — `def f(match: float)` is valid Python — so escaping them buys
    # nothing and costs something real: the escaped name becomes the FastAPI
    # request-body field, so a Fortran argument named `match` in a service that
    # already worked would silently have its wire field renamed to `match_` on
    # the next regenerate. True keywords carry no such risk, because a service
    # exposing one never generated importable Python in the first place.
    if keyword.iskeyword(name):
        return f"{name}_"
    return name


def is_valid_python_name(name: str) -> bool:
    return bool(name) and name.isidentifier() and not keyword.iskeyword(name)


@dataclass
class Problem:
    """A reason this IR cannot be turned into a working service."""

    symbol: str
    message: str


def validate(module: "ModuleIR") -> list[Problem]:
    """Structural problems that would produce broken generated code.

    Run by the CLI after parsing and before generating, so a name that cannot
    be spelled in Python fails the build instead of the container.
    """
    problems: list[Problem] = []

    def check_params(owner: str, params: list[Parameter]) -> None:
        for param in params:
            # Escape first, then check — the generator emits the escaped
            # spelling, so a parameter named `lambda` is fine and one named
            # `2x` is not. Checking `param.name.isidentifier()` directly is
            # the trap here: str.isidentifier() returns True for every Python
            # keyword ("lambda".isidentifier() is True), so it passes exactly
            # the names that produce a SyntaxError downstream.
            if not is_valid_python_name(python_identifier(param.name)):
                problems.append(
                    Problem(
                        f"{owner}({param.name})",
                        f"parameter name '{param.name}' cannot be spelled as a "
                        "Python identifier, even after escaping; exclude this "
                        "symbol in native2py.yaml.",
                    )
                )

    exported: dict[str, tuple[str, str]] = {}
    for kind, items in (
        ("struct", module.structs),
        ("class", module.classes),
        ("function", module.functions),
    ):
        for item in items:
            if not is_valid_python_name(python_identifier(item.name)):
                problems.append(
                    Problem(
                        item.name,
                        f"{kind} name '{item.name}' cannot be spelled as a Python "
                        "identifier; exclude it in native2py.yaml.",
                    )
                )
            # Keyed on the ESCAPED spelling, not the native one: the
            # generators emit python_identifier(name), so a class `lambda` and
            # a struct `lambda_` both land on `lambda_` in the generated
            # package and one silently shadows the other. Comparing raw names
            # would see two distinct keys and miss it.
            emitted = python_identifier(item.name)
            previous = exported.get(emitted)
            if previous is not None:
                previous_kind, previous_name = previous
                detail = (
                    f"'{item.name}' and '{previous_name}' both become '{emitted}'"
                    if previous_name != item.name
                    else f"'{item.name}'"
                )
                problems.append(
                    Problem(
                        item.name,
                        f"{kind} {detail} collides with a {previous_kind} in the "
                        "generated package namespace; one would silently shadow "
                        "the other.",
                    )
                )
            exported[emitted] = (kind, item.name)

    for fn in module.functions:
        check_params(fn.name, fn.parameters)
    for cls in module.classes:
        for method in cls.methods:
            check_params(f"{cls.name}.{method.name}", method.parameters)

    return problems


# --- serialisation ------------------------------------------------------
#
# `generate` writes the IR next to the service so later tooling can answer
# "what does this service expose?" without re-parsing the native sources —
# which would need the compiler, the include paths and the same libclang all
# over again, on a machine that may only have the built wheel.



# --- schema version -----------------------------------------------------
#
# `ir.json` is a *committed artifact*: services/<name>/.native2py/ir.json is
# checked in, is read by `golden record` on machines that have neither the
# headers nor libclang, and outlives the native2py that wrote it. Without a
# version stamp, a document written by a future native2py deserialises into a
# subtly different ModuleIR — every field this reader has never heard of takes
# its "unknown, behave as before" default and nothing says so.
#
# The version is MAJOR.MINOR and describes the *document*, not the tool:
#
#   MAJOR  changes when the meaning of existing fields changes, or a field is
#          removed or renamed. A reader cannot honestly interpret a document
#          whose major it does not know, so it refuses it.
#   MINOR  changes when fields are added. Additive-only, so:
#            - an OLDER minor loads: the fields it lacks take the documented
#              defaults below (this is exactly what a version-less pre-1.0
#              file gets, and why the already-committed ir.json keeps working);
#            - a NEWER minor loads too, but the keys this reader does not know
#              are reported rather than dropped in silence.
#
# Unknown keys at a version we *do* fully understand are an error, not a
# forward-compatibility question: they mean the document is corrupt or
# hand-edited, and quietly ignoring them is how a typo'd "is_cosnt: true"
# becomes a wrong binding.
SCHEMA_VERSION_MAJOR = 1
SCHEMA_VERSION_MINOR = 0
SCHEMA_VERSION = f"{SCHEMA_VERSION_MAJOR}.{SCHEMA_VERSION_MINOR}"

# What a document with no `schema_version` at all is treated as: everything
# native2py wrote before versioning existed is, by definition, 1.0 minus the
# stamp.
LEGACY_SCHEMA_VERSION = "1.0"

SCHEMA_VERSION_KEY = "schema_version"
WRITER_VERSION_KEY = "native2py_version"


class IRSchemaError(ValueError):
    """Raised when an ir.json cannot be honestly read by this native2py."""


class IRSchemaWarning(UserWarning):
    """Warned when an ir.json carries fields this native2py does not know."""


def native2py_version() -> str:
    """The installed native2py version, recorded in every ir.json we write."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("native2py")
    except Exception:  # pragma: no cover - metadata is present in any install
        return "unknown"


def parse_schema_version(raw: object) -> tuple[int, int]:
    if raw is None:
        raw = LEGACY_SCHEMA_VERSION
    text = str(raw).strip()
    match = re.fullmatch(r"(\d+)\.(\d+)", text)
    if not match:
        raise IRSchemaError(
            f"Unreadable {SCHEMA_VERSION_KEY} {raw!r} in this ir.json: expected "
            f'"MAJOR.MINOR" (this native2py writes "{SCHEMA_VERSION}"). '
            "Regenerate the service with `native2py generate <name>`."
        )
    return int(match.group(1)), int(match.group(2))


def _check_version(data: dict) -> tuple[int, int]:
    major, minor = parse_schema_version(data.get(SCHEMA_VERSION_KEY))
    if major != SCHEMA_VERSION_MAJOR:
        writer = data.get(WRITER_VERSION_KEY)
        written_by = (
            f"native2py {writer}" if writer else "an unknown native2py version"
        )
        direction = "a newer" if major > SCHEMA_VERSION_MAJOR else "an older"
        raise IRSchemaError(
            f"This ir.json uses IR schema {major}.{minor}, written by {written_by}; "
            f"native2py {native2py_version()} reads schema {SCHEMA_VERSION} and "
            f"cannot interpret {direction} major version — the meaning of the "
            "fields it contains has changed, so loading it would silently produce "
            "a different API than the one that was recorded.\n"
            + (
                f"Upgrade native2py (>= {writer}), or regenerate this service "
                "from source with `native2py generate <name>`."
                if major > SCHEMA_VERSION_MAJOR
                else "Regenerate this service from source with "
                "`native2py generate <name>`."
            )
        )
    return major, minor


# Every key each node type understands at SCHEMA_VERSION. Kept beside the
# dataclasses deliberately: adding a field without listing it here makes the
# round-trip test fail, which is the reminder to bump SCHEMA_VERSION_MINOR.
_PARAMETER_KEYS = frozenset(
    {"name", "type", "is_array", "intent", "native_type", "is_const", "is_optional"}
)
_METHOD_KEYS = frozenset(
    {"name", "parameters", "returns", "is_static", "is_const", "is_overloaded"}
)
_CLASS_KEYS = frozenset(
    {
        "name",
        "namespace",
        "methods",
        "has_default_constructor",
        "constructors",
        "fields",
        "bases",
    }
)
_STRUCT_KEYS = frozenset({"name", "namespace", "fields", "has_default_constructor"})
_FUNCTION_KEYS = frozenset(
    {
        "name",
        "parameters",
        "returns",
        "is_subroutine",
        "namespace",
        "is_overloaded",
        "fortran_module",
    }
)
_SKIPPED_KEYS = frozenset({"name", "reason"})
_MODULE_KEYS = frozenset(
    {
        "name",
        "language",
        "source_file",
        "classes",
        "functions",
        "structs",
        "fortran_module",
        "skipped",
        "diagnostics",
        SCHEMA_VERSION_KEY,
        WRITER_VERSION_KEY,
    }
)


def module_to_dict(module: ModuleIR) -> dict:
    """Serialise, stamping the schema version and the writer that produced it."""
    data = {
        SCHEMA_VERSION_KEY: SCHEMA_VERSION,
        WRITER_VERSION_KEY: native2py_version(),
    }
    data.update(asdict(module))
    return data


def module_from_dict(data: dict) -> ModuleIR:
    major, minor = _check_version(data)
    # Only a document at or below the minor we implement can have its unknown
    # keys called corrupt; above it, an unknown key is just a field added by a
    # later native2py, so it is reported and skipped rather than fatal.
    strict = minor <= SCHEMA_VERSION_MINOR
    ignored: set[str] = set()

    def check_keys(item: dict, allowed: frozenset[str], where: str) -> dict:
        unknown = sorted(set(item) - allowed)
        if not unknown:
            return item
        if strict:
            raise IRSchemaError(
                f"Unknown key(s) {', '.join(repr(k) for k in unknown)} in {where} of "
                f"this ir.json, which declares IR schema {major}.{minor} — a version "
                "this native2py fully implements, so these fields are not from a "
                "newer writer. The file is corrupt or hand-edited; regenerate it "
                "with `native2py generate <name>`."
            )
        ignored.update(f"{where}.{key}" for key in unknown)
        return item

    def parameters(items) -> list[Parameter]:
        # Explicit rather than Parameter(**item): an ir.json written by an
        # older native2py has none of the fields added since, and **item would
        # also crash on a field removed later. Defaults here are the
        # "unknown, behave as before" values.
        return [
            Parameter(
                name=check_keys(item, _PARAMETER_KEYS, "a parameter")["name"],
                type=item["type"],
                is_array=item.get("is_array", False),
                intent=item.get("intent", "in"),
                native_type=item.get("native_type"),
                is_const=item.get("is_const", False),
                is_optional=item.get("is_optional", False),
            )
            for item in items
        ]

    check_keys(data, _MODULE_KEYS, "the module")
    module = ModuleIR(
        name=data["name"],
        language=data["language"],
        source_file=data["source_file"],
        classes=[
            ClassDef(
                name=check_keys(cls, _CLASS_KEYS, f"class {cls.get('name')!r}")["name"],
                namespace=cls.get("namespace"),
                methods=[
                    Method(
                        name=check_keys(m, _METHOD_KEYS, f"method {m.get('name')!r}")["name"],
                        parameters=parameters(m.get("parameters", [])),
                        returns=m.get("returns", "void"),
                        is_static=m.get("is_static", False),
                        is_const=m.get("is_const", False),
                        is_overloaded=m.get("is_overloaded", False),
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
                name=check_keys(fn, _FUNCTION_KEYS, f"function {fn.get('name')!r}")["name"],
                parameters=parameters(fn.get("parameters", [])),
                returns=fn.get("returns", "void"),
                is_subroutine=fn.get("is_subroutine", False),
                fortran_module=fn.get("fortran_module"),
                namespace=fn.get("namespace"),
                is_overloaded=fn.get("is_overloaded", False),
            )
            for fn in data.get("functions", [])
        ],
        structs=[
            StructDef(
                name=check_keys(struct, _STRUCT_KEYS, f"struct {struct.get('name')!r}")["name"],
                namespace=struct.get("namespace"),
                fields=parameters(struct.get("fields", [])),
                has_default_constructor=struct.get("has_default_constructor", True),
            )
            for struct in data.get("structs", [])
        ],
        fortran_module=data.get("fortran_module"),
        skipped=[
            SkippedSymbol(**check_keys(item, _SKIPPED_KEYS, "a skipped symbol"))
            for item in data.get("skipped", [])
        ],
        diagnostics=list(data.get("diagnostics", [])),
    )

    if ignored:
        # A newer minor: additive by contract, so the module above is still a
        # faithful subset — but say which fields were dropped, because "the
        # binding lost an attribute" is otherwise indistinguishable from "the
        # parser never found it".
        writer = data.get(WRITER_VERSION_KEY) or "an unknown native2py version"
        warnings.warn(
            f"This ir.json declares IR schema {major}.{minor} (written by {writer}); "
            f"native2py {native2py_version()} implements {SCHEMA_VERSION} and ignored "
            f"{len(ignored)} unknown field(s): {', '.join(sorted(ignored))}. "
            "Upgrade native2py, or regenerate the service, if any of them matter.",
            IRSchemaWarning,
            stacklevel=2,
        )
    return module
