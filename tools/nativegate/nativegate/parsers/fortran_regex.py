"""Regex/line-oriented Fortran reader (design.md section 8).

This was nativegate's only Fortran front end; `fortran_fparser.py` is the real
parse tree that supersedes it, and `fortran.py` picks between them. It is kept
because it has no third-party dependency and because reproducing its exact
behaviour is how the fparser2 backend's parity is measured.

It does NOT scan the whole file into a tree. Each name in `expose.functions:`
drives one targeted regex search for that routine's
`function ... end function` / `subroutine ... end subroutine` block, and
everything else in the file is left untouched — so a 20,000-line template
with one exposed routine costs roughly the same as a 200-line one.

That property turns out to be a premature optimisation at the sizes nativegate
actually sees: fparser2 parses every deck in libraries/petro whole in well
under 0.15s each. It is documented here as a description of this reader, not
as a constraint on the replacement.

`expose.functions:` is still REQUIRED for Fortran (there is no
"expose everything" fallback like C++ has), under both backends.
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


def _strip_inline_comment(
    line: str, open_quote: str | None = None
) -> tuple[str, str | None]:
    """Drop a free-form `!` comment, respecting character literals.

    Returns (text, quote_still_open). `open_quote` carries a character literal
    that a previous physical line left open across a `&` continuation.

    `write (*,*) 'flow rate ! per day'` has no comment on it, and cutting at
    the bare `!` would truncate the statement mid-string.

    Doubled quotes (`'don''t'`) need no special case: the second quote closes
    the literal and the third reopens it, which leaves the parity — the only
    thing this needs — correct either way.
    """
    quote = open_quote
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "!":
            return line[:index], None
    # A literal still open at end of line is only meaningful when the line
    # continues; the caller decides, and passes it back in on the next line.
    return line, quote


def normalize_free_form(source: str) -> str:
    """Collapse free-form source into one logical statement per line.

    Free-form Fortran continues a statement by ending the line with `&`, and
    a continuation line may re-open with its own leading `&` to mark exactly
    where the statement resumes. Nothing downstream of here handles that: the
    routine finder captures its argument list with `(?P<params>[^)]*)`, which
    matches newlines, so a wrapped argument list

        function tubing_bhp(wellhead_p, q_oil, q_water, q_gas, diameter, &
                            roughness, tvd, md, nseg) result(pbh)

    yielded a parameter literally named "&\\n roughness". That name reached
    the generated FastAPI router as `diameter: float, &` — a Python
    SyntaxError, so the service could not even be imported, and the only
    workarounds were to reformat the Fortran onto one line or hand-edit
    generated code. Both mean touching source nativegate promises not to
    touch.

    This is the free-form counterpart of `fixed_form.normalize_fixed_form`
    and exists for the same reason: dialect structure is normalized once, up
    front, so no downstream pattern has to know about it.
    """
    out: list[str] = []
    pending = ""
    # Carried across physical lines: a character literal may itself be split
    # over a continuation (`'rate&` / `&! per day'`), and resetting the quote
    # state per line made the `!` inside that still-open literal look like a
    # comment, silently truncating the string.
    open_quote: str | None = None

    for raw in source.splitlines():
        line, open_quote = _strip_inline_comment(raw, open_quote)

        if pending:
            # A whole-line comment is legal *between* continuation lines and
            # does not end the statement. `_strip_inline_comment` reduces such
            # a line to blank; flushing on it closed the statement early and
            # truncated the argument list — so while a continuation is pending,
            # a blank line is skipped rather than treated as the final segment.
            if not line.strip():
                continue
            # A leading `&` marks where the statement resumes and is not part
            # of it. Without one, the statement resumes at the first
            # non-blank character.
            resumed = line.lstrip()
            line = resumed[1:] if resumed.startswith("&") else resumed

        trimmed = line.rstrip()
        if trimmed.endswith("&"):
            pending += trimmed[:-1]
            continue

        if pending:
            out.append(pending + trimmed)
            pending = ""
        else:
            # Leading whitespace is preserved: the declaration and routine
            # patterns anchor with `^\s*`, and blank lines keep the source
            # roughly line-aligned for error messages.
            out.append(line)

    # A trailing `&` with nothing after it is malformed Fortran; keep what was
    # accumulated so the caller reports "routine not found" rather than
    # silently dropping the last statement.
    if pending:
        out.append(pending)

    return "\n".join(out)


_DECL_RE = re.compile(
    r"^\s*(?P<type>real|integer|logical|character|complex|double\s+precision)"
    r"(?:\s*\((?P<kind>[^)]*)\))?"
    r"(?P<attrs>(?:\s*,\s*\w+(?:\([^)]*\))?)*)"
    r"\s*::\s*(?P<names>[^\n!]+)",
    re.IGNORECASE | re.MULTILINE,
)

_INTENT_RE = re.compile(r"intent\s*\(\s*(in|out|inout)\s*\)", re.IGNORECASE)

# `optional` is an attribute in its own right and never a prefix of another
# one, so a word-boundary match against the attribute list is exact.
_OPTIONAL_RE = re.compile(r"\boptional\b", re.IGNORECASE)

# A derived-type (or polymorphic) declaration: `type(pvt_state) :: st`.
# Recognised deliberately — f2py cannot pass or return one, and the whole
# point of matching it is to say so rather than let the declaration lookup
# miss and fall through to a default. `type :: pvt_state` (a type
# *definition*) carries no parenthesis and is not matched here.
_DERIVED_DECL_RE = re.compile(
    r"^\s*(?P<type>(?:type|class)\s*\(\s*[^)]*\))"
    r"(?P<attrs>(?:\s*,\s*\w+(?:\([^)]*\))?)*)"
    r"\s*::\s*(?P<names>[^\n!]+)",
    re.IGNORECASE | re.MULTILINE,
)

# An intrinsic type word anywhere in a routine's return-type prefix
# (`real(8) function foo(...)`, `double precision function bar(...)`).
_PREFIX_TYPE_RE = re.compile(
    r"\b(real|integer|logical|character|complex|double\s+precision)\b",
    re.IGNORECASE,
)
_PREFIX_DERIVED_RE = re.compile(r"^\s*(?:type|class)\s*\(", re.IGNORECASE)

# The trailing name on `end module` is optional in the standard, and a module
# that omits it must still be recognised or every routine inside it is
# mis-attributed to the top level — which sends generate_init_py looking for
# the symbol where f2py has not put it.
_MODULE_BLOCK_RE = re.compile(
    r"^[ \t]*module\s+(?P<name>\w+)[ \t]*$"
    r".*?"
    r"^[ \t]*end[ \t]+module(?:[ \t]+\w+)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)

# A return-type prefix can be more than one token: `double precision function`,
# `real(kind=8) function`, `character*(*) function`. Allowing only a single
# `[\w()]+` token silently skipped every `DOUBLE PRECISION FUNCTION` in the
# file. `(?!function\b|subroutine\b)` keeps the optional prefix from eating
# the keyword itself when a routine has no return type; `end\b` is excluded
# for a related reason — once the argument list became optional below,
# `end subroutine reset` otherwise parsed as prefix "end" plus a fresh
# routine declaration, and every routine was discovered twice.
_TYPE_PREFIX = r"(?:(?!function\b|subroutine\b|end\b)[\w*()=]+[ \t]+)*"

# The argument list is optional: `subroutine reset` is legal and is the
# standard idiom for a routine that works purely through module variables or
# COMMON. Requiring "(" made such a routine invisible to discovery *and*
# unfindable by name, so this pattern and `_find_block` are fixed together —
# fixing only discovery converts a silent omission into a hard error.
# (fixed_form.py was already fixed for the same thing; this is the free-form
# half.)
_ROUTINE_DECL_RE = re.compile(
    rf"^[ \t]*{_TYPE_PREFIX}(?:function|subroutine)\s+(?P<name>\w+)[ \t]*(?:\(|$)",
    re.IGNORECASE | re.MULTILINE,
)

# `end`, `end function` and `end function foo` are all legal terminators. The
# bare form is the delicate one: `end` also closes `if`/`do`/`select`/`where`/
# `associate`/`block`/`type`/`interface`, so it is accepted only when the line
# is *exactly* `end` — which no construct terminator ever is, since they all
# carry their keyword and the one-word spellings are `endif`/`enddo`.
_END_BLOCK = r"^[ \t]*end(?:[ \t]+{keyword}(?:[ \t]+{name})?)?[ \t]*$"


class _RoutineSkipped(Exception):
    """A routine that was found and understood, but cannot be bound by f2py.

    Raised out of extraction and turned into a `ModuleIR.skipped` entry by
    `parse_source`. Defaulting an unresolvable type to `float` instead
    produces a service that builds, imports, passes its smoke test and
    returns a wrong number; this is the alternative.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    # Free-form needs its own normalization for the same reason: a routine
    # whose argument list wraps has its `function` keyword and its name on one
    # physical line but is only a single statement, and a commented-out
    # routine must not be discovered.
    return [
        m.group("name")
        for m in _ROUTINE_DECL_RE.finditer(normalize_free_form(path.read_text()))
    ]


def _find_block(source: str, keyword: str, name: str) -> re.Match | None:
    """Locate one `function`/`subroutine` block by name without parsing the rest of the file."""
    end = _END_BLOCK.format(keyword=keyword, name=re.escape(name))
    pattern = re.compile(
        rf"^[ \t]*(?P<prefix>{_TYPE_PREFIX}){keyword}\s+{re.escape(name)}[ \t]*"
        # No argument list at all is legal (`subroutine reset`).
        rf"(?:\((?P<params>[^)]*)\))?"
        rf"(?:\s*result\s*\(\s*(?P<result>\w+)\s*\))?[ \t]*$"
        rf"(?P<body>.*?)"
        rf"{end}",
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


def _parse_derived_declarations(body: str) -> dict[str, str]:
    """Map variable name -> native type spelling for every derived-type declaration.

    These are collected precisely so they can be refused: f2py has no way to
    pass or return a `type(...)`, and a missed lookup here used to fall
    through to `float`.
    """
    unbindable: dict[str, str] = {}
    for decl in _DERIVED_DECL_RE.finditer(body):
        spelling = re.sub(r"\s+", "", decl.group("type"))
        for raw_name in decl.group("names").split(","):
            var_name = raw_name.strip().split("(")[0].strip()
            if var_name:
                unbindable[var_name.lower()] = spelling
    return unbindable


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
        # Without this an `optional` argument is generated as required, and
        # the caller has no way to express "absent".
        is_optional = bool(_OPTIONAL_RE.search(attrs))

        for raw_name in decl.group("names").split(","):
            raw_name = raw_name.strip()
            if not raw_name:
                continue
            is_array = "(" in raw_name or "dimension" in attrs.lower()
            var_name = raw_name.split("(")[0].strip()
            declared[var_name.lower()] = Parameter(
                name=var_name,
                type=py_type,
                is_array=is_array,
                intent=intent,
                is_optional=is_optional,
            )
    return declared


def _resolve_parameters(
    match: re.Match,
    declared: dict[str, Parameter],
    unbindable: dict[str, str],
    implicit_map: dict[str, str],
) -> list[Parameter]:
    """Type every dummy argument, or refuse the routine.

    Three sources in decreasing authority: an explicit declaration, a
    recognised-but-unbindable declaration (refuse), and IMPLICIT typing.
    There is deliberately no fourth "assume float" step.
    """
    raw_params = match.group("params") or ""
    param_names = [p.strip() for p in raw_params.split(",") if p.strip()]

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


def _prefix_return_type(prefix: str | None) -> str | None:
    """Fortran base type from a `real(8) function ...` style prefix, if any."""
    if not prefix:
        return None
    if _PREFIX_DERIVED_RE.match(prefix):
        raise _RoutineSkipped(
            f"declared as returning '{prefix.strip()}'; "
            "f2py cannot return a derived type."
        )
    type_match = _PREFIX_TYPE_RE.search(prefix)
    return re.sub(r"\s+", " ", type_match.group(1).lower()) if type_match else None


# A `contains` statement inside a routine opens its INTERNAL procedures. Their
# declarations are not the outer routine's declarations, but `_find_block`'s
# body capture runs to the routine's end and so swallows them — and
# `_parse_declarations` keeps the last match for a name, so an internal
# procedure that happens to reuse an outer dummy argument's name silently
# retypes it. A helper declaring `integer :: a` inside a function whose own
# `a` is `real(8)` produced a binding typed `int`: it compiles, it runs, and
# it hands Fortran the wrong thing. Contamination is the same whether or not
# the internal procedure closes with a bare `end`.
#
# Everything a routine declares for itself precedes `contains`, so cutting
# there loses nothing and removes the whole class of leak.
_CONTAINS_RE = re.compile(r"^[ \t]*contains[ \t]*$", re.IGNORECASE | re.MULTILINE)


def _own_declarations(body: str) -> str:
    """`body` up to its first `contains`, i.e. excluding internal procedures."""
    match = _CONTAINS_RE.search(body)
    return body[: match.start()] if match else body


def _extract_function(
    source: str, name: str, implicit_map: dict[str, str]
) -> tuple[FunctionDef, str | None] | None:
    match = _find_block(source, "function", name)
    if match is None:
        return None

    body = _own_declarations(match.group("body"))
    declared = _parse_declarations(body)
    unbindable = _parse_derived_declarations(body)

    parameters = _resolve_parameters(match, declared, unbindable, implicit_map)

    result_name = (match.group("result") or name).lower()
    result_param = declared.get(result_name)
    if result_param is not None:
        returns = result_param.type
    elif result_name in unbindable:
        raise _RoutineSkipped(
            f"result variable '{result_name}' is declared "
            f"'{unbindable[result_name]}'; f2py cannot return a derived type."
        )
    else:
        base_type = _prefix_return_type(match.group("prefix")) or fixed_form.implicit_type_of(
            result_name, implicit_map
        )
        if base_type is None:
            raise _RoutineSkipped(
                f"result variable '{result_name}' has no explicit declaration "
                "and the source uses IMPLICIT NONE, so the return type cannot "
                "be determined. Declare it in the Fortran source."
            )
        returns = map_fortran_type(base_type)

    enclosing = _enclosing_fortran_module(source, match.start())
    fn = FunctionDef(
        name=name,
        parameters=parameters,
        returns=returns,
        is_subroutine=False,
        fortran_module=enclosing,
    )
    return fn, enclosing


def _extract_subroutine(
    source: str, name: str, implicit_map: dict[str, str]
) -> tuple[FunctionDef, str | None] | None:
    match = _find_block(source, "subroutine", name)
    if match is None:
        return None

    body = _own_declarations(match.group("body"))
    declared = _parse_declarations(body)
    unbindable = _parse_derived_declarations(body)

    parameters = _resolve_parameters(match, declared, unbindable, implicit_map)

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

            # Same rule, same message, same recorded spelling as the fparser2
            # backend — the parity harness holds both to byte-identical IR. A
            # CHARACTER output binds exactly when its length is fixed in the
            # declaration; an assumed-length output BUILDS and then silently
            # returns b'' (measured with numpy 2.5's f2py), so it is demoted
            # with the reason.
            char_spelling = (
                routine.get("char_lengths", {}).get(key)
                if py_type == "str"
                else None
            )
            if py_type == "str" and intent != "in":
                if char_spelling is None or not fixed_form.CHAR_FIXED_LEN_RE.match(
                    char_spelling
                ):
                    module.skipped.append(
                        SkippedSymbol(
                            f"{name}({param_name})",
                            f"'{param_name}' is assigned in the body but is "
                            f"declared '{char_spelling or 'implicitly'}': f2py "
                            "can only return a CHARACTER whose length is fixed "
                            "in the declaration. An assumed-length output "
                            "builds and then silently returns an empty string "
                            "— measured, not assumed. Give it a length "
                            "(CHARACTER*80) and it becomes an output.",
                        )
                    )
                    intent = "in"

            parameters.append(
                Parameter(
                    name=param_name,
                    type=py_type,
                    is_array=key in routine["arrays"],
                    native_type=char_spelling,
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
            "Fortran sources require explicit `expose.functions:` entries in nativegate.yaml "
            "(no implicit expose-everything fallback — large legacy files are parsed "
            "one requested routine at a time)."
        )

    if is_fixed_form(path):
        return _parse_fixed_form_source(path, expose, include_paths or [])

    # Continuations are joined before anything looks for a routine: every
    # pattern below treats one line as one statement, and `_find_block`'s
    # `[^)]*` argument capture spans newlines, so a wrapped argument list
    # otherwise produces a parameter named "&".
    source = normalize_free_form(path.read_text())
    module = ModuleIR(name=path.stem, language="fortran", source_file=str(path))

    # IMPLICIT typing is dialect-independent, so the fixed-form implementation
    # is reused rather than duplicated. Without it an undeclared argument in a
    # `.f90` that omits `implicit none` — legal, and common in transitional
    # 1990s code — silently bound as float; with it, `implicit none` instead
    # makes the routine a reported skip.
    implicit_map = fixed_form.parse_implicit_map(source)

    fortran_module_name: str | None = "__unset__"
    for name in expose.functions:
        try:
            found = _extract_function(source, name, implicit_map) or _extract_subroutine(
                source, name, implicit_map
            )
        except _RoutineSkipped as skipped:
            # Recognised but not bindable: reported through the channel that
            # exists for exactly this, and omitted from module.functions so
            # nothing downstream wraps a type f2py cannot carry.
            module.skipped.append(SkippedSymbol(name, skipped.reason))
            continue

        if found is None:
            raise ValueError(
                f"Could not find function or subroutine '{name}' in {path}. "
                "Check the name in nativegate.yaml matches the Fortran source exactly."
            )
        fn, enclosing_module = found
        module.functions.append(fn)

        if fortran_module_name == "__unset__":
            fortran_module_name = enclosing_module
        elif fortran_module_name != enclosing_module:
            raise ValueError(
                f"'{name}' is defined in Fortran module '{enclosing_module}', but earlier "
                f"exposed routines came from '{fortran_module_name}'. nativegate generates one "
                "Python package per source file and can't mix routines from different "
                "Fortran modules — split them into separate nativegate.yaml services."
            )

    module.fortran_module = None if fortran_module_name == "__unset__" else fortran_module_name
    return module
