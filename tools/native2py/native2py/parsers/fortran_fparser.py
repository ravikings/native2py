"""Fortran parser built on the fparser2 parse tree (design.md section 8).

This is the tree parser that supersedes `fortran_regex.py`. Both produce the
same `ir.ModuleIR`, so generators and the CLI do not care which one ran — see
`fortran.py` for the selection logic.

What a real parse tree buys, concretely — each of these is something the
regex reader reconstructs by pattern and can therefore get wrong:

* **Routine boundaries come from the grammar.** `Subroutine_Subprogram` /
  `Function_Subprogram` nodes have exact extents, so `end`, `end function`,
  `end function foo`, a bare F77 `END` and a nested `end do` are distinguished
  by the parser rather than by a regex that has to guess which `end` is which.
* **Continuations are gone before parsing starts.** Free-form `&` and
  fixed-form column-6 continuation are the reader's job in fparser, not a
  normalisation pass native2py has to maintain in two dialect-specific copies.
* **Internal procedures are a separate node.** The declarations of a
  `contains`ed helper live in `Internal_Subprogram_Part`, not in the outer
  routine's `Specification_Part`, so a helper's `integer :: a` can no longer
  leak onto an outer dummy argument named `a`. The regex reader needed an
  explicit cut at `contains` to avoid exactly that.
* **Declaration attributes are structured.** `Intent_Attr_Spec`,
  `Dimension_Attr_Spec`, `Attr_Spec('OPTIONAL')` and per-entity array shapes
  are nodes, not substrings of an attribute blob.
* **Module nesting is structural.** The enclosing `Module` is found by walking
  up parents, so `end module` with the name omitted needs no special case.

What it still refuses to bind, deliberately (the same refusals the regex
reader makes, because they are f2py constraints and not parsing limits):

* derived-type (`type(...)`/`class(...)`) arguments and results,
* types with no entry in `ir.FORTRAN_TYPE_MAP`,
* under `implicit none`, an argument or result with no explicit declaration.

All of those are reported through `ModuleIR.skipped` — one unbindable routine
never costs you the rest of the file.

Two things this backend deliberately does NOT do differently yet, because
they are the next phase and changing them here would make parity
unmeasurable:

* **IMPLICIT typing** is still applied by `fixed_form.parse_implicit_map` /
  `implicit_type_of`. fparser2 does not apply implicit rules for you, and the
  rules are dialect-independent, so the statements are read off the tree and
  handed to the existing engine rather than duplicated.
* **F77 intent inference** still runs `fixed_form.infer_intents` over the
  normalised statement text. Rewriting that onto the tree is its own change
  with its own risk; until then the fixed-form path takes structure and types
  from the tree and intents from the existing inferencer, which is what keeps
  the two backends byte-identical on the legacy corpus.

Requires the `fparser` package (``pip install "native2py[fparser]"``).
`is_available()` reports whether it imported; `fortran.py` falls back to the
regex reader when it did not.

NOTE: fparser is not yet declared in pyproject.toml. It must be added as an
optional `fparser` extra before `auto` can ever resolve here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ExposeConfig
from ..discovery import is_fixed_form
from ..ir import (
    FunctionDef,
    ModuleIR,
    NativeTypeError,
    Parameter,
    SkippedSymbol,
    map_fortran_type,
)
from ..preprocess import expand_includes
from . import fixed_form
from .fortran_regex import _RoutineSkipped

# --- availability -------------------------------------------------------

_LOAD_ERROR: str | None = None
_LOADED = False
_F = None  # fparser.two.Fortran2003
_walk = None
_SYMBOL_TABLES = None
_FortranFileReader = None
_FortranStringReader = None
_ParserFactory = None


def _load() -> bool:
    """Import fparser once, recording why if it is not usable."""
    global _LOADED, _LOAD_ERROR, _F, _walk, _SYMBOL_TABLES
    global _FortranFileReader, _FortranStringReader, _ParserFactory

    if _LOADED:
        return True
    if _LOAD_ERROR is not None:
        return False

    try:
        from fparser.common.readfortran import FortranFileReader, FortranStringReader
        from fparser.two import Fortran2003
        from fparser.two.parser import ParserFactory
        from fparser.two.symbol_table import SYMBOL_TABLES
        from fparser.two.utils import walk
    except Exception as exc:  # ImportError, and anything the package raises on import
        _LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        return False

    _F = Fortran2003
    _walk = walk
    _SYMBOL_TABLES = SYMBOL_TABLES
    _FortranFileReader = FortranFileReader
    _FortranStringReader = FortranStringReader
    _ParserFactory = ParserFactory
    _LOADED = True
    return True


def is_available() -> bool:
    """True if an fparser2 parse can actually run on this machine."""
    return _load()


def unavailable_reason() -> str:
    return _LOAD_ERROR or "unknown"


def fparser_version() -> str:
    if not _load():
        return "unavailable"
    try:
        from importlib.metadata import version

        return f"fparser {version('fparser')}"
    except Exception:
        return "unknown version"


# --- parsing ------------------------------------------------------------

DEFAULT_STD = "f2008"

# fparser2's SYMBOL_TABLES is a module-level global singleton, and the parser
# appends to it rather than replacing it. Two parses in one process therefore
# share state: the second file sees the first file's module and routine
# symbols, and a name that exists in neither resolves to the wrong one. That
# is a silent wrong answer, not a crash, so the table is cleared before every
# parse and the whole parse is serialised — the singleton is not thread-safe
# and there is no per-parser instance to isolate.
_PARSE_LOCK = threading.RLock()


def _parse(path: Path, source: str | None = None):
    """Parse `path` (or `source`, attributed to `path`) into an fparser2 tree."""
    if not _load():
        raise RuntimeError(f"fparser is not available: {unavailable_reason()}")

    from fparser.two.utils import FortranSyntaxError

    with _PARSE_LOCK:
        # Never merge two files' symbols. See _PARSE_LOCK above.
        _SYMBOL_TABLES.clear()
        try:
            if source is None:
                reader = _FortranFileReader(str(path), ignore_comments=False)
            else:
                # The reader picks fixed vs free form from the file name, so
                # the expanded text is attributed back to the original path.
                reader = _FortranStringReader(
                    source, ignore_comments=False, include_dirs=[str(path.parent)]
                )
                reader.id = str(path)
                reader.set_format(_source_format(path))
            return _ParserFactory().create(std=DEFAULT_STD)(reader)
        except FortranSyntaxError as exc:
            # A real grammar rejects source the regex reader would have
            # happily pattern-matched — a declaration after an executable
            # statement, an IMPLICIT outside any program unit. Say which
            # file and offer the reader that does not care, instead of
            # letting an fparser traceback out.
            raise ValueError(
                f"{path.name} is not valid Fortran and the fparser2 parser "
                f"cannot read it: {exc}. Fix the source, or set "
                "parser: regex in native2py.yaml to use the pattern-matching "
                "reader, which has no grammar and will accept it."
            ) from exc
        finally:
            _SYMBOL_TABLES.clear()


def _source_format(path: Path):
    from fparser.common.sourceinfo import FortranFormat

    return FortranFormat(not is_fixed_form(path), False)


# --- tree navigation ----------------------------------------------------


def _name_of(node) -> str:
    """The source spelling of a `Name` child, preserving case."""
    return str(node)


def _subprogram_stmt(subprogram):
    """The `Subroutine_Stmt`/`Function_Stmt` opening a subprogram node.

    Scanned for rather than taken as `children[0]`: the tree is built with
    comments retained, so a routine preceded by a banner comment has
    `Comment` nodes ahead of its opening statement. Only direct children are
    considered, so an internal procedure's statement can never be picked up.
    """
    for child in subprogram.children:
        if isinstance(child, (_F.Subroutine_Stmt, _F.Function_Stmt)):
            return child
    raise ValueError(f"malformed subprogram node: {type(subprogram).__name__}")


def _routine_name(subprogram) -> str:
    return _name_of(_subprogram_stmt(subprogram).items[1])


def _dummy_args(subprogram) -> list[str]:
    """Dummy argument names, in order.

    An absent argument list means none — `subroutine reset` with no
    parentheses is legal and is the standard idiom for a routine that works
    purely through module variables or COMMON.

    A single argument is a bare `Name` rather than a `Dummy_Arg_List`, which
    is why this walks rather than iterating children. An F77 alternate-return
    marker (`*`) is not a `Name` and is dropped; f2py cannot bind one anyway,
    and none appears in the corpus.
    """
    arg_list = _subprogram_stmt(subprogram).items[2]
    if arg_list is None:
        return []
    return [_name_of(item) for item in _walk(arg_list, _F.Name)]


def _result_name(subprogram) -> str | None:
    """The `result(x)` name of a function, if it has one.

    `Suffix` may instead (or also) carry a `bind(c)` spec, whose language
    binding name is not the result variable — so only a direct `Name` item
    counts.
    """
    stmt = _subprogram_stmt(subprogram)
    suffix = stmt.items[3] if len(stmt.items) > 3 else None
    if suffix is None:
        return None
    for child in getattr(suffix, "items", ()):
        if isinstance(child, _F.Name):
            return _name_of(child)
    return None


def _prefix_spec(subprogram):
    """The return-type spec on `real(8) function foo(...)`, if any."""
    stmt = _subprogram_stmt(subprogram)
    prefix = stmt.items[0]
    if prefix is None:
        return None
    for child in _walk(prefix, (_F.Intrinsic_Type_Spec, _F.Declaration_Type_Spec)):
        return child
    return None


def _specification_part(subprogram):
    """The subprogram's OWN declarations.

    Internal procedures live in `Internal_Subprogram_Part`, a sibling node, so
    they are excluded structurally — no `contains` cut needed.
    """
    for child in subprogram.children:
        if isinstance(child, _F.Specification_Part):
            return child
    return None


def _enclosing_module(subprogram) -> str | None:
    """Name of the `module X` this subprogram is nested in, if any.

    f2py nests a module's routines under `X.<routine>` in the compiled
    extension (unlike bare routines, which land at the top level), and the
    generated __init__.py has to know which.
    """
    node = subprogram.parent
    while node is not None:
        if isinstance(node, _F.Module):
            for stmt in _walk(node, _F.Module_Stmt):
                return _name_of(stmt.items[1])
            return None
        node = node.parent
    return None


def _subprograms(tree, kind: str | None = None) -> list:
    """Every function/subroutine subprogram node, in source order."""
    wanted = {
        "function": (_F.Function_Subprogram,),
        "subroutine": (_F.Subroutine_Subprogram,),
        None: (_F.Function_Subprogram, _F.Subroutine_Subprogram),
    }[kind]
    return _walk(tree, wanted)


def _find_subprogram(tree, name: str, kind: str | None = None):
    for subprogram in _subprograms(tree, kind):
        if _routine_name(subprogram).lower() == name.lower():
            return subprogram
    return None


# --- IMPLICIT -----------------------------------------------------------


def _implicit_map(tree) -> dict[str, str]:
    """First-letter -> Fortran base type, from the IMPLICIT statements in `tree`.

    The statements are read off the tree; the *rules* are applied by
    `fixed_form.parse_implicit_map`, which is dialect-independent and already
    handles `IMPLICIT NONE`, ranges, and the `REAL*8` length spelling. fparser2
    does not apply implicit typing itself, so without this an undeclared
    argument would have no type at all.

    Like the regex reader, this is one map for the whole file: an
    `implicit none` anywhere makes every undeclared argument a reported skip
    rather than a silent float.
    """
    statements = "\n".join(str(stmt) for stmt in _walk(tree, _F.Implicit_Stmt))
    return fixed_form.parse_implicit_map(statements)


# --- declarations -------------------------------------------------------


def _is_derived(type_spec) -> bool:
    """True for `type(x)` / `class(x)` — recognised precisely so it can be refused."""
    return isinstance(type_spec, (_F.Declaration_Type_Spec, _F.Derived_Type_Spec))


def _base_type_word(type_spec) -> str:
    """The bare Fortran type word of an intrinsic type spec.

    `Intrinsic_Type_Spec` keeps the word and its selector as separate items, so
    `REAL(KIND=dp)`, `CHARACTER*8` and `CHARACTER*(*)` all reduce here to
    "real"/"character" — which is what `ir.FORTRAN_TYPE_MAP` is keyed on.
    Stringifying the whole node instead leaves the length suffix attached and
    `CHARACTER*(*)` fails to map, which is how an assumed-length dummy
    argument used to fall through to IMPLICIT and come back an integer.
    """
    return str(type_spec.items[0]).strip().lower()


def _derived_spelling(type_spec) -> str:
    """`type(pvt_state)` as the IR reports it, whitespace removed."""
    return str(type_spec).replace(" ", "").lower()


def _attributes(decl) -> tuple[str, bool, bool]:
    """(intent, is_optional, has_dimension_attr) from a declaration's attribute list."""
    intent, optional, dimension = "in", False, False
    attr_list = decl.items[1]
    if attr_list is None:
        return intent, optional, dimension

    for attr in _walk(attr_list, _F.Intent_Attr_Spec):
        intent = str(attr.items[1]).strip().lower()
    for attr in _walk(attr_list, _F.Attr_Spec):
        if str(attr).strip().lower() == "optional":
            optional = True
    if _walk(attr_list, _F.Dimension_Attr_Spec):
        dimension = True
    return intent, optional, dimension


def _entities(decl):
    """(name, is_array) for each entity in a declaration."""
    for entity in _walk(decl.items[2], _F.Entity_Decl):
        yield _name_of(entity.items[0]), entity.items[1] is not None


@dataclass
class _Declarations:
    """What one subprogram's own specification part says about its names.

    All keys are lowercased, because Fortran is case-insensitive and the two
    dialects in the corpus disagree on case for the same symbol.
    """

    # name -> fully-formed Parameter (type, array-ness, intent, optional).
    declared: dict[str, Parameter] = field(default_factory=dict)
    # name -> the Fortran base type as spelled ("double precision"). Needed
    # separately because a function's return type resolution works in Fortran
    # types, not the Python mapping.
    base_types: dict[str, str] = field(default_factory=dict)
    # name -> native spelling of a declaration f2py cannot carry. Collected
    # precisely so it can be refused: a missed lookup used to fall through to
    # `float`, which produces a service that builds, imports, passes its smoke
    # test and returns a wrong number.
    unbindable: dict[str, str] = field(default_factory=dict)
    # Names given a shape by a standalone DIMENSION statement.
    dimensioned: set[str] = field(default_factory=set)


def _parse_declarations(spec_part) -> _Declarations:
    """Read one subprogram's own declarations off the tree."""
    result = _Declarations()
    if spec_part is None:
        return result

    # A DIMENSION statement makes a separately-declared (or IMPLICIT-typed)
    # name an array. F77 decks use it constantly.
    for stmt in _walk(spec_part, _F.Dimension_Stmt):
        for item in stmt.items[0]:
            result.dimensioned.add(_name_of(item[0]).lower())

    for decl in _walk(spec_part, _F.Type_Declaration_Stmt):
        type_spec = decl.items[0]
        if _is_derived(type_spec):
            spelling = _derived_spelling(type_spec)
            for name, _ in _entities(decl):
                result.unbindable[name.lower()] = spelling
            continue

        base_type = _base_type_word(type_spec)
        py_type = map_fortran_type(base_type)
        intent, optional, has_dimension = _attributes(decl)

        for name, entity_is_array in _entities(decl):
            key = name.lower()
            result.base_types[key] = base_type
            result.declared[key] = Parameter(
                name=name,
                type=py_type,
                is_array=entity_is_array
                or has_dimension
                or key in result.dimensioned,
                intent=intent,
                is_optional=optional,
            )

    # DIMENSION may follow the type declaration, so apply it once more at the
    # end rather than depending on statement order.
    for key in result.dimensioned:
        if key in result.declared:
            result.declared[key].is_array = True

    return result


# --- shared resolution --------------------------------------------------


def _resolve_parameters(
    param_names: list[str],
    declared: dict[str, Parameter],
    unbindable: dict[str, str],
    implicit_map: dict[str, str],
) -> list[Parameter]:
    """Type every dummy argument, or refuse the routine.

    Three sources in decreasing authority: an explicit declaration, a
    recognised-but-unbindable declaration (refuse), and IMPLICIT typing. There
    is deliberately no fourth "assume float" step — the reason the reasons
    below exist at all.
    """
    parameters: list[Parameter] = []
    for param_name in param_names:
        key = param_name.lower()
        if key in declared:
            parameters.append(declared[key])
            continue
        if key in unbindable:
            raise _RoutineSkipped(
                f"argument '{param_name}' is declared '{unbindable[key]}'; "
                "f2py cannot pass a derived type."
            )

        base_type = fixed_form.implicit_type_of(param_name, implicit_map)
        if base_type is None:
            raise _RoutineSkipped(
                f"argument '{param_name}' has no explicit declaration and the "
                "source uses IMPLICIT NONE, so its type cannot be determined. "
                "Declare it in the Fortran source."
            )
        parameters.append(Parameter(name=param_name, type=map_fortran_type(base_type)))
    return parameters


def _prefix_return_type(subprogram) -> str | None:
    """Fortran base type from a `real(8) function ...` prefix, if any."""
    spec = _prefix_spec(subprogram)
    if spec is None:
        return None
    if _is_derived(spec):
        raise _RoutineSkipped(
            f"declared as returning '{str(spec).strip()}'; "
            "f2py cannot return a derived type."
        )
    return _base_type_word(spec)


# --- free-form path -----------------------------------------------------


def _free_form_routine(
    subprogram, is_subroutine: bool, implicit_map: dict[str, str]
) -> tuple[FunctionDef, str | None]:
    name = _routine_name(subprogram)
    decls = _parse_declarations(_specification_part(subprogram))
    declared, unbindable = decls.declared, decls.unbindable
    parameters = _resolve_parameters(
        _dummy_args(subprogram), declared, unbindable, implicit_map
    )
    enclosing = _enclosing_module(subprogram)

    if is_subroutine:
        returns = "void"
    else:
        result_name = (_result_name(subprogram) or name).lower()
        result_param = declared.get(result_name)
        if result_param is not None:
            returns = result_param.type
        elif result_name in unbindable:
            raise _RoutineSkipped(
                f"result variable '{result_name}' is declared "
                f"'{unbindable[result_name]}'; f2py cannot return a derived type."
            )
        else:
            base_type = _prefix_return_type(subprogram) or fixed_form.implicit_type_of(
                result_name, implicit_map
            )
            if base_type is None:
                raise _RoutineSkipped(
                    f"result variable '{result_name}' has no explicit declaration "
                    "and the source uses IMPLICIT NONE, so the return type cannot "
                    "be determined. Declare it in the Fortran source."
                )
            returns = map_fortran_type(base_type)

    return (
        FunctionDef(
            name=name,
            parameters=parameters,
            returns=returns,
            is_subroutine=is_subroutine,
            fortran_module=enclosing,
        ),
        enclosing,
    )


def _parse_free_form(path: Path, expose: ExposeConfig) -> ModuleIR:
    tree = _parse(path)
    implicit_map = _implicit_map(tree)

    module = ModuleIR(name=path.stem, language="fortran", source_file=str(path))

    fortran_module_name: str | None = "__unset__"
    for name in expose.functions:
        subprogram = _find_subprogram(tree, name, "function")
        is_subroutine = False
        if subprogram is None:
            subprogram = _find_subprogram(tree, name, "subroutine")
            is_subroutine = True
        if subprogram is None:
            raise ValueError(
                f"Could not find function or subroutine '{name}' in {path}. "
                "Check the name in native2py.yaml matches the Fortran source exactly."
            )

        try:
            fn, enclosing_module = _free_form_routine(
                subprogram, is_subroutine, implicit_map
            )
        except (_RoutineSkipped, NativeTypeError) as skipped:
            # Recognised but not bindable: reported through the channel that
            # exists for exactly this, and omitted from module.functions so
            # nothing downstream wraps a type f2py cannot carry.
            reason = getattr(skipped, "reason", str(skipped))
            module.skipped.append(SkippedSymbol(name, reason))
            continue

        module.functions.append(fn)

        if fortran_module_name == "__unset__":
            fortran_module_name = enclosing_module
        elif fortran_module_name != enclosing_module:
            raise ValueError(
                f"'{name}' is defined in Fortran module '{enclosing_module}', but earlier "
                f"exposed routines came from '{fortran_module_name}'. native2py generates one "
                "Python package per source file and can't mix routines from different "
                "Fortran modules — split them into separate native2py.yaml services."
            )

    module.fortran_module = None if fortran_module_name == "__unset__" else fortran_module_name
    return module


# --- fixed-form path ----------------------------------------------------


def _parse_fixed_form(path: Path, expose: ExposeConfig, include_paths: list[Path]) -> ModuleIR:
    """FORTRAN 77 fixed-form path.

    INCLUDEs are expanded before parsing because that is where the IMPLICIT
    statements and COMMON blocks live in legacy decks — without them, argument
    types fall back to guesses and the resulting bindings return wrong numbers
    rather than failing. fparser2 would leave `INCLUDE 'PETRO.INC'` as an
    unexpanded `Include_Stmt` node, so native2py's own expansion still runs
    (see preprocess.expand_includes for why f2py cannot be trusted with it).
    """
    expanded = expand_includes(path, include_paths)
    tree = _parse(path, expanded)
    implicit_map = _implicit_map(tree)

    # Intent inference still runs on normalised statement text — see the module
    # docstring. This is the one place the fixed-form path is not yet on the
    # tree, and it is deliberate: moving it is phase 1.4c, and doing it here
    # would make the two backends' output incomparable.
    normalized = fixed_form.normalize_fixed_form(expanded)

    module = ModuleIR(name=path.stem, language="fortran", source_file=str(path))
    # Fixed-form F77 predates modules; routines are always top level, so f2py
    # exposes them directly with no nesting to bridge.
    module.fortran_module = None

    for name in expose.functions:
        subprogram = _find_subprogram(tree, name)
        if subprogram is None:
            available = ", ".join(list_routine_names(path)[:15])
            raise ValueError(
                f"Could not find function or subroutine '{name}' in {path}. "
                f"Routines found: {available or '(none)'}"
            )

        is_subroutine = isinstance(subprogram, _F.Subroutine_Subprogram)
        decls = _parse_declarations(_specification_part(subprogram))
        param_names = _dummy_args(subprogram)

        # F77 declares no intent, so it is recovered from the body: an argument
        # that is assigned to is an output. Without this every scalar binds as
        # intent(in) and its result is silently discarded — `STEP(DTIN, DTOUT,
        # ICONV)` would return None.
        routine = fixed_form.find_routine(normalized, name)
        intents = fixed_form.infer_intents(routine, normalized) if routine else {}

        parameters = []
        for param_name in param_names:
            key = param_name.lower()
            declaration = decls.declared.get(key)
            if declaration is not None:
                py_type = declaration.type
            elif key in decls.unbindable:
                # F77 has no derived types, so this only fires on a deck that
                # mixes F90 declarations into fixed form. Refuse loudly rather
                # than let the IMPLICIT fallback below invent a float.
                raise ValueError(
                    f"{path.name}: parameter '{param_name}' of '{name}' is declared "
                    f"'{decls.unbindable[key]}'; f2py cannot pass a derived type."
                )
            else:
                base_type = fixed_form.implicit_type_of(param_name, implicit_map)
                if base_type is None:
                    raise ValueError(
                        f"{path.name}: parameter '{param_name}' of '{name}' has no explicit "
                        "declaration and the source uses IMPLICIT NONE, so its type cannot "
                        "be determined. Declare it in the Fortran source."
                    )
                py_type = map_fortran_type(base_type)

            intent = intents.get(key, "in")
            is_array = (
                declaration.is_array if declaration else key in decls.dimensioned
            )

            # f2py cannot size a CHARACTER result argument (the length is not
            # carried through the declaration, and CHARACTER*(*) has none to
            # carry), so it cannot be an output. Leave it as input and record
            # why, rather than emitting a directive that fails to build.
            if py_type == "str" and intent != "in":
                module.skipped.append(
                    SkippedSymbol(
                        f"{name}({param_name})",
                        "assigned in the routine body, but f2py cannot return a "
                        "CHARACTER argument; it stays an input and its value is "
                        "not returned.",
                    )
                )
                intent = "in"

            parameters.append(
                Parameter(
                    name=param_name,
                    type=py_type,
                    is_array=is_array,
                    intent=intent,
                )
            )

        if is_subroutine:
            returns = "void"
        else:
            # Same three sources, same order, as the regex reader: the result
            # variable's own declaration, then the `DOUBLE PRECISION FUNCTION`
            # prefix, then IMPLICIT typing.
            base = (
                decls.base_types.get(name.lower())
                or _prefix_return_type(subprogram)
                or fixed_form.implicit_type_of(name, implicit_map)
            )
            returns = map_fortran_type(base) if base else "float"

        module.functions.append(
            FunctionDef(
                name=name,
                parameters=parameters,
                returns=returns,
                is_subroutine=is_subroutine,
                # Fixed-form F77 predates modules: always top level.
                fortran_module=None,
            )
        )

    return module


# --- entry points -------------------------------------------------------


def list_routine_names(path: Path) -> list[str]:
    """Every function/subroutine name in the file, in source order.

    Used by `quickstart` and `suggest` on files you do not yet know by name.
    Unlike the regex reader this costs a full parse — which measured at 0.03s
    to 0.15s per deck across libraries/petro, so the reader's "only look at
    what you asked for" property buys nothing at these sizes.
    """
    tree = _parse(path)
    return [_routine_name(sub) for sub in _subprograms(tree)]


def parse_source(
    path: Path, expose: ExposeConfig, include_paths: list[Path] | None = None
) -> ModuleIR:
    if not expose.functions:
        raise ValueError(
            "Fortran sources require explicit `expose.functions:` entries in native2py.yaml "
            "(no implicit expose-everything fallback — large legacy files are parsed "
            "one requested routine at a time)."
        )

    if is_fixed_form(path):
        return _parse_fixed_form(path, expose, include_paths or [])
    return _parse_free_form(path, expose)
