"""Fortran source parser (design.md section 8).

Unlike the C++ parser, this one does NOT scan the whole file into an AST.
Legacy oil & gas Fortran sources (reservoir simulators, well-test templates,
etc.) commonly run tens of thousands of lines with hundreds of subroutines,
and the only thing native2py needs is the one or two routines a service
actually calls. So `native2py.yaml`'s `expose.functions:` list is REQUIRED
for Fortran (there is no "expose everything" fallback like C++ has) — each
requested name drives one targeted regex search for that routine's
`function ... end function` / `subroutine ... end subroutine` block, and
everything else in the file is left untouched. A 20,000-line template with
one exposed routine costs roughly the same as a 200-line one.
"""

from __future__ import annotations

import re
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

_DECL_RE = re.compile(
    r"^\s*(?P<type>real|integer|logical|character|complex|double\s+precision)"
    r"(?:\s*\((?P<kind>[^)]*)\))?"
    r"(?P<attrs>(?:\s*,\s*\w+(?:\([^)]*\))?)*)"
    r"\s*::\s*(?P<names>[^\n!]+)",
    re.IGNORECASE | re.MULTILINE,
)

_INTENT_RE = re.compile(r"intent\s*\(\s*(in|out|inout)\s*\)", re.IGNORECASE)

_MODULE_BLOCK_RE = re.compile(
    r"^[ \t]*module\s+(?P<name>\w+)\b.*?^[ \t]*end\s+module\s+(?P=name)\b",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)

# A return-type prefix can be more than one token: `double precision function`,
# `real(kind=8) function`, `character*(*) function`. Allowing only a single
# `[\w()]+` token silently skipped every `DOUBLE PRECISION FUNCTION` in the
# file. `(?!function\b|subroutine\b)` keeps the optional prefix from eating
# the keyword itself when a routine has no return type.
_TYPE_PREFIX = r"(?:(?!function\b|subroutine\b)[\w*()=]+[ \t]+)*"

_ROUTINE_DECL_RE = re.compile(
    rf"^[ \t]*{_TYPE_PREFIX}(?:function|subroutine)\s+(?P<name>\w+)\s*\(",
    re.IGNORECASE | re.MULTILINE,
)


def list_routine_names(path: Path) -> list[str]:
    """Full-file scan for every `function`/`subroutine` name, for `quickstart`
    on small/unfamiliar files where you don't yet know what to ask for by
    name. NOT used by parse_source's targeted extraction — deliberately
    separate so the "huge legacy template" path never accidentally pays for
    a full scan (see module docstring).

    Fixed-form source must be normalized first, and for the same reasons
    parse_source normalizes it: a continued declaration
    (`DOUBLE PRECISION FUNCTION FOO (A,\\n     &  B)`) spans two physical
    lines, and a commented-out routine in column 1 looks real. Scanning raw
    text with the free-form regex silently under-reports — against
    libraries/petro it found 25 of 48 routines, missing every
    `DOUBLE PRECISION FUNCTION`, so `quickstart` would generate a service
    exposing half the API with no warning that anything was skipped.
    """
    if is_fixed_form(path):
        normalized = fixed_form.normalize_fixed_form(path.read_text())
        return fixed_form.list_routine_names(normalized)
    return [m.group("name") for m in _ROUTINE_DECL_RE.finditer(path.read_text())]


def _find_block(source: str, keyword: str, name: str) -> re.Match | None:
    """Locate one `function`/`subroutine` block by name without parsing the rest of the file."""
    pattern = re.compile(
        rf"^[ \t]*{_TYPE_PREFIX}{keyword}\s+{re.escape(name)}\s*"
        rf"\((?P<params>[^)]*)\)"
        rf"(?:\s+result\s*\(\s*(?P<result>\w+)\s*\))?"
        rf"(?P<body>.*?)"
        rf"^[ \t]*end\s+{keyword}\s+{re.escape(name)}\b",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    return pattern.search(source)


def _enclosing_fortran_module(source: str, match_start: int) -> str | None:
    """f2py nests routines defined inside `module X ... end module X` under X.<routine>
    in the compiled extension (unlike bare/free routines, which land at the top level).
    The generated Python __init__.py needs to know this to re-export correctly."""
    for module_match in _MODULE_BLOCK_RE.finditer(source):
        if module_match.start() <= match_start <= module_match.end():
            return module_match.group("name")
    return None


def _parse_declarations(body: str) -> dict[str, Parameter]:
    """Map declared variable name -> Parameter, for every `type, attrs :: names` line in body."""
    declared: dict[str, Parameter] = {}
    for decl in _DECL_RE.finditer(body):
        try:
            py_type = map_fortran_type(decl.group("type"))
        except NativeTypeError:
            raise

        attrs = decl.group("attrs") or ""
        intent_match = _INTENT_RE.search(attrs)
        intent = intent_match.group(1).lower() if intent_match else "in"

        for raw_name in decl.group("names").split(","):
            raw_name = raw_name.strip()
            if not raw_name:
                continue
            is_array = "(" in raw_name or "dimension" in attrs.lower()
            var_name = raw_name.split("(")[0].strip()
            declared[var_name.lower()] = Parameter(
                name=var_name, type=py_type, is_array=is_array, intent=intent
            )
    return declared


def _extract_function(source: str, name: str) -> tuple[FunctionDef, str | None] | None:
    match = _find_block(source, "function", name)
    if match is None:
        return None

    body = match.group("body")
    declared = _parse_declarations(body)

    param_names = [p.strip() for p in match.group("params").split(",") if p.strip()]
    parameters = [
        declared.get(p.lower(), Parameter(name=p, type="float"))
        for p in param_names
    ]

    result_name = (match.group("result") or name).lower()
    result_param = declared.get(result_name)
    returns = result_param.type if result_param else "float"

    enclosing = _enclosing_fortran_module(source, match.start())
    fn = FunctionDef(
        name=name,
        parameters=parameters,
        returns=returns,
        is_subroutine=False,
        fortran_module=enclosing,
    )
    return fn, enclosing


def _extract_subroutine(source: str, name: str) -> tuple[FunctionDef, str | None] | None:
    match = _find_block(source, "subroutine", name)
    if match is None:
        return None

    body = match.group("body")
    declared = _parse_declarations(body)

    param_names = [p.strip() for p in match.group("params").split(",") if p.strip()]
    parameters = [
        declared.get(p.lower(), Parameter(name=p, type="float"))
        for p in param_names
    ]

    enclosing = _enclosing_fortran_module(source, match.start())
    fn = FunctionDef(
        name=name,
        parameters=parameters,
        returns="void",
        is_subroutine=True,
        fortran_module=enclosing,
    )
    return fn, enclosing


def _parse_fixed_form_source(
    path: Path, expose: ExposeConfig, include_paths: list[Path]
) -> ModuleIR:
    """FORTRAN 77 fixed-form path.

    INCLUDEs are expanded first because that's usually where the IMPLICIT
    statements and COMMON blocks live — without them, parameter types fall
    back to guesses and the resulting bindings return wrong numbers rather
    than failing. See preprocess.expand_includes for why f2py can't be
    trusted to do this itself.
    """
    expanded = expand_includes(path, include_paths)
    normalized = fixed_form.normalize_fixed_form(expanded)
    implicit_map = fixed_form.parse_implicit_map(normalized)

    module = ModuleIR(name=path.stem, language="fortran", source_file=str(path))
    # Fixed-form F77 predates modules; routines are always top level, so
    # f2py exposes them directly with no nesting to bridge.
    module.fortran_module = None

    for name in expose.functions:
        routine = fixed_form.find_routine(normalized, name)
        if routine is None:
            available = ", ".join(fixed_form.list_routine_names(normalized)[:15])
            raise ValueError(
                f"Could not find function or subroutine '{name}' in {path}. "
                f"Routines found: {available or '(none)'}"
            )

        # F77 declares no intent, so it is recovered from the body: an
        # argument that is assigned to is an output. Without this every
        # scalar binds as intent(in) and its result is silently discarded —
        # `STEP(DTIN, DTOUT, ICONV)` would return None.
        intents = fixed_form.infer_intents(routine, normalized)

        parameters = []
        for param_name in routine["params"]:
            key = param_name.lower()
            base_type = routine["declared_types"].get(key) or fixed_form.implicit_type_of(
                param_name, implicit_map
            )
            if base_type is None:
                raise ValueError(
                    f"{path.name}: parameter '{param_name}' of '{name}' has no explicit "
                    "declaration and the source uses IMPLICIT NONE, so its type cannot "
                    "be determined. Declare it in the Fortran source."
                )
            py_type = map_fortran_type(base_type)
            intent = intents.get(key, "in")

            # f2py cannot size a CHARACTER result argument (the length is
            # not carried through the declaration, and CHARACTER*(*) has none
            # to carry), so it cannot be an output. Leave it as input and
            # record why, rather than emitting a directive that fails to build.
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
                    is_array=key in routine["arrays"],
                    intent=intent,
                )
            )

        is_subroutine = routine["kind"] == "subroutine"
        if is_subroutine:
            returns = "void"
        else:
            declared = routine["declared_types"].get(name.lower())
            prefix = routine["prefix"]
            base = declared or (prefix.lower() if prefix else None) or fixed_form.implicit_type_of(
                name, implicit_map
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
        return _parse_fixed_form_source(path, expose, include_paths or [])

    source = path.read_text()
    module = ModuleIR(name=path.stem, language="fortran", source_file=str(path))

    fortran_module_name: str | None = "__unset__"
    for name in expose.functions:
        found = _extract_function(source, name) or _extract_subroutine(source, name)
        if found is None:
            raise ValueError(
                f"Could not find function or subroutine '{name}' in {path}. "
                "Check the name in native2py.yaml matches the Fortran source exactly."
            )
        fn, enclosing_module = found
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
