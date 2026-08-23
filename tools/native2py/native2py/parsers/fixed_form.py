"""FORTRAN 77 fixed-form source handling.

Fixed-form is a different dialect from the free-form .f90 the main parser
targets, and the differences are structural rather than cosmetic:

  col 1      'C', 'c', '*' or '!' marks a full-line comment
  cols 1-5   statement label
  col 6      any non-blank/non-zero char continues the PREVIOUS line
  cols 7-72  the statement
  col 73+    ignored (historically the punch-card sequence number)

A regex written for free-form silently mismatches all of this — a
continued argument list looks like two unrelated lines, and a commented-out
routine looks like a real one. So fixed-form source is normalized to
one-logical-statement-per-line before any parsing happens.

Also handles IMPLICIT typing, which fixed-form legacy code relies on
completely: `DOUBLE PRECISION FUNCTION PVTBUB (RSTGT)` declares nothing
about RSTGT — its type comes from an `IMPLICIT DOUBLE PRECISION (A-H,O-Z)`
statement (often inside an INCLUDE file) plus the first letter of the name.
Getting this wrong produces bindings that run and return wrong numbers.
"""

from __future__ import annotations

import re

_COMMENT_CHARS = frozenset("CcDd*!")

# FORTRAN's built-in default when no IMPLICIT statement says otherwise:
# I-N are INTEGER, everything else REAL.
DEFAULT_IMPLICIT = {
    **{chr(c): "real" for c in range(ord("a"), ord("z") + 1)},
    **{chr(c): "integer" for c in range(ord("i"), ord("n") + 1)},
}

_IMPLICIT_RE = re.compile(
    r"^\s*IMPLICIT\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

# "DOUBLE PRECISION (A-H,O-Z)" / "INTEGER (I-N)" / "REAL*8 (A-H)"
_IMPLICIT_SPEC_RE = re.compile(
    r"(?P<type>double\s+precision|real|integer|logical|character|complex)"
    r"(?:\s*\*\s*\d+)?"
    r"\s*\((?P<ranges>[^)]*)\)",
    re.IGNORECASE,
)


def normalize_fixed_form(source: str) -> str:
    """Collapse fixed-form source into one logical statement per line.

    Strips comment lines, truncates the ignored column-73+ region, and joins
    continuation lines onto the statement they continue.
    """
    logical_lines: list[str] = []

    for raw in source.splitlines():
        if not raw.strip():
            continue

        # A comment marker only counts in column 1 in fixed form.
        if raw[0] in _COMMENT_CHARS:
            continue

        # Columns 73+ are sequence numbers, not code.
        line = raw[:72]

        is_continuation = len(line) > 5 and line[5] not in (" ", "0")
        if is_continuation and logical_lines:
            logical_lines[-1] += " " + line[6:].strip()
        else:
            # Drop the statement-label field so labels can't be mistaken for
            # part of the statement.
            logical_lines.append(line[6:].strip() if len(line) > 6 else line.strip())

    return "\n".join(logical_lines)


def parse_implicit_map(source: str) -> dict[str, str]:
    """Build first-letter -> Fortran base type from IMPLICIT statements.

    `source` should already have INCLUDEs expanded, since that's usually
    where the IMPLICIT statements live in legacy code.
    """
    implicit_map = dict(DEFAULT_IMPLICIT)

    for line in source.splitlines():
        match = _IMPLICIT_RE.match(line)
        if not match:
            continue

        rest = match.group("rest")
        if rest.strip().lower().startswith("none"):
            # IMPLICIT NONE: every symbol must be declared explicitly, so an
            # undeclared parameter is an error rather than a silent default.
            return {}

        for spec in _IMPLICIT_SPEC_RE.finditer(rest):
            base_type = re.sub(r"\s+", " ", spec.group("type").strip().lower())
            for part in spec.group("ranges").split(","):
                part = part.strip().lower()
                if not part:
                    continue
                if "-" in part:
                    start, _, end = part.partition("-")
                    start, end = start.strip(), end.strip()
                    if len(start) == 1 and len(end) == 1:
                        for c in range(ord(start), ord(end) + 1):
                            implicit_map[chr(c)] = base_type
                elif len(part) == 1:
                    implicit_map[part] = base_type

    return implicit_map


def implicit_type_of(name: str, implicit_map: dict[str, str]) -> str | None:
    """Fortran base type for `name` under these IMPLICIT rules, or None under IMPLICIT NONE."""
    if not name:
        return None
    return implicit_map.get(name[0].lower())


# --- routine extraction -------------------------------------------------
#
# F77 routines end with a bare `END`, not `END FUNCTION <name>`, so the
# free-form block finder never matches them. Everything below works on
# already-normalized (comment-stripped, continuation-joined) source.

_ROUTINE_START_RE_TEMPLATE = (
    r"^(?:(?P<prefix>[\w\s*]*?)\s+)?"
    r"(?P<kind>SUBROUTINE|FUNCTION)\s+{name}\s*"
    r"(?:\((?P<params>[^)]*)\))?\s*$"
)

_END_RE = re.compile(r"^END\s*$", re.IGNORECASE)

# Old-style declarations have no "::" — "DOUBLE PRECISION X, Y(10)".
#
# The length suffix has three legacy spellings and all three appear in real
# decks: `REAL*8 X`, `CHARACTER*8 NAME`, and `CHARACTER*(*) MSG` (assumed
# length, for a dummy argument). Matching only `*<digits>` left
# `CHARACTER*(*) MSG` unmatched, so MSG fell through to IMPLICIT typing and
# came back as an *integer* — a binding that compiles, runs, and passes a
# pointer where the callee expects a string descriptor.
_LENGTH_SUFFIX = r"(?:\s*\*\s*(?:\d+|\(\s*\*\s*\)))?"

_OLD_DECL_RE = re.compile(
    r"^(?P<type>DOUBLE\s+PRECISION|REAL|INTEGER|LOGICAL|CHARACTER|COMPLEX)"
    rf"{_LENGTH_SUFFIX}\s+"
    r"(?!FUNCTION\b)(?P<names>[A-Z0-9_,()\s*]+)$",
    re.IGNORECASE,
)

_DIMENSION_RE = re.compile(r"^DIMENSION\s+(?P<names>.+)$", re.IGNORECASE)


def _split_declarator_list(text: str) -> list[str]:
    """Split "A(N), B, C(10,2)" on top-level commas only."""
    items, depth, current = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        items.append(current.strip())
    return items


def find_routine(normalized: str, name: str) -> dict | None:
    """Locate one routine in normalized fixed-form source.

    Returns {kind, params, body, declared_types, arrays, prefix} or None.
    """
    start_re = re.compile(
        _ROUTINE_START_RE_TEMPLATE.format(name=re.escape(name)), re.IGNORECASE
    )

    lines = normalized.splitlines()
    start_idx = None
    match = None
    for i, line in enumerate(lines):
        m = start_re.match(line.strip())
        if m:
            start_idx, match = i, m
            break

    if start_idx is None or match is None:
        return None

    body_lines: list[str] = []
    for line in lines[start_idx + 1 :]:
        if _END_RE.match(line.strip()):
            break
        body_lines.append(line.strip())

    body = "\n".join(body_lines)

    declared_types: dict[str, str] = {}
    arrays: set[str] = set()

    for line in body_lines:
        decl = _OLD_DECL_RE.match(line)
        if decl:
            base = re.sub(r"\s+", " ", decl.group("type").strip().lower())
            for item in _split_declarator_list(decl.group("names")):
                var = item.split("(")[0].strip()
                if not var or not var[0].isalpha():
                    continue
                declared_types[var.lower()] = base
                if "(" in item:
                    arrays.add(var.lower())
            continue

        dim = _DIMENSION_RE.match(line)
        if dim:
            for item in _split_declarator_list(dim.group("names")):
                var = item.split("(")[0].strip()
                if var and var[0].isalpha() and "(" in item:
                    arrays.add(var.lower())

    params = [p.strip() for p in (match.group("params") or "").split(",") if p.strip()]

    return {
        "kind": match.group("kind").lower(),
        "prefix": (match.group("prefix") or "").strip(),
        "params": params,
        "body": body,
        "declared_types": declared_types,
        "arrays": arrays,
    }


# The argument list is optional: `SUBROUTINE TRNCAL` (no parentheses at all)
# is legal F77 and common for routines that work purely through COMMON.
# find_routine has always accepted it, so requiring "(" here made discovery
# disagree with extraction — the routine was invisible to `quickstart` but
# bound fine once you named it by hand.
_ROUTINE_NAME_RE = re.compile(
    r"^(?:[\w\s*()]*?\s+)?(?:SUBROUTINE|FUNCTION)\s+(?P<name>\w+)\s*(?:\(|$)",
    re.IGNORECASE,
)


# --- intent inference ---------------------------------------------------
#
# F77 has no `intent` attribute: every dummy argument is passed by reference
# and any of them may be an output. f2py, given no other information, treats
# scalars as intent(in) — so a routine like
#
#     SUBROUTINE STEP (DTIN, DTOUT, ICONV)
#
# binds as `step(dtin, dtout, iconv)` returning None, and both results are
# silently discarded. Nothing crashes; the caller just never sees the answer.
#
# The intent is recoverable from the body: an argument that is assigned to is
# an output, and one that is read before it is assigned is also an input.
# That is what the rules below reconstruct.

# Statement keywords that are never assignments. `IF` is handled separately
# because a logical IF can carry an assignment as its statement.
_NON_ASSIGNMENT_KEYWORDS = frozenset(
    {
        "IF", "THEN", "ELSE", "ELSEIF", "END", "ENDIF", "DO", "ENDDO",
        "CALL", "RETURN", "GOTO", "GO", "CONTINUE", "STOP", "PAUSE",
        "WRITE", "READ", "PRINT", "FORMAT", "DATA", "COMMON", "DIMENSION",
        "PARAMETER", "EXTERNAL", "INTRINSIC", "SAVE", "IMPLICIT", "INCLUDE",
        "EQUIVALENCE", "ENTRY", "SUBROUTINE", "FUNCTION", "PROGRAM", "BLOCK",
        "REAL", "INTEGER", "LOGICAL", "CHARACTER", "COMPLEX", "DOUBLE",
    }
)

# Statements that mention an argument without using its value. Treating a
# declaration as a read makes every declared output look like an input.
_DECLARATION_KEYWORDS = frozenset(
    {
        "REAL", "INTEGER", "LOGICAL", "CHARACTER", "COMPLEX", "DOUBLE",
        "DIMENSION", "COMMON", "DATA", "SAVE", "EXTERNAL", "INTRINSIC",
        "PARAMETER", "IMPLICIT", "INCLUDE", "EQUIVALENCE", "FORMAT",
    }
)

_LOGICAL_IF_RE = re.compile(r"^IF\s*\(", re.IGNORECASE)

# "X = ..." or "X(I,J) = ..." but not "X == ..." or ".EQ."
_ASSIGNMENT_RE = re.compile(r"^(?P<target>\w+)\s*(?:\((?P<subscript>.*)\))?\s*=(?!=)")

_CALL_RE = re.compile(r"^CALL\s+(?P<name>\w+)\s*(?:\((?P<args>.*)\))?\s*$", re.IGNORECASE)

_READ_RE = re.compile(r"^READ\s*(?:\([^)]*\)|\s*\S+\s*,)\s*(?P<targets>.*)$", re.IGNORECASE)


def _split_logical_if(statement: str) -> tuple[str, str]:
    """Split `IF (cond) stmt` into (condition, guarded statement).

    Both halves matter: `IF (N .LT. 1) N = 20` reads N in the condition and
    writes it in the guarded statement, which together make it inout. Looking
    at either half alone gets the intent wrong.

    `IF (...) THEN` opens a block, so it has no guarded statement — only a
    condition.
    """
    depth = 0
    for i, ch in enumerate(statement):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                condition = statement[: i + 1]
                tail = statement[i + 1 :].strip()
                if tail.upper() == "THEN":
                    return condition, ""
                return condition, tail
    return statement, ""


def _mentions(name: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE) is not None


def _callee_intents(
    callee: str, normalized: str | None, seen: frozenset[str]
) -> dict[str, str] | None:
    """Intents of a CALLed routine, if it lives in the same source.

    Without this, every argument handed to a CALL has to be assumed both read
    and written. `CALL PVTSET (P)` would make BBDPDZ's P an output even though
    PVTSET only reads it — technically safe, but it clutters the generated
    signature with returns that are really inputs. Resolving the callee turns
    the guess into an answer.
    """
    if normalized is None or callee.lower() in seen:
        return None
    routine = find_routine(normalized, callee)
    if routine is None:
        return None
    return infer_intents(routine, normalized, seen | {callee.lower()})


def _actual_argument_names(args: str) -> list[str | None]:
    """Positional actual arguments, or None where the argument is an
    expression (`P+1`, `A(I)`, `2.0D0`) and therefore cannot be an output."""
    names: list[str | None] = []
    for arg in _split_declarator_list(args):
        arg = arg.strip()
        names.append(arg.lower() if re.fullmatch(r"\w+", arg) else None)
    return names


def infer_intents(
    routine: dict,
    normalized: str | None = None,
    _seen: frozenset[str] = frozenset(),
    statement_functions: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Infer "in" / "out" / "inout" for each dummy argument of `routine`.

    Rules, in order of application to each statement of the body:

    * assigned to           -> output
    * read before assigned  -> also an input, so "inout"
    * passed to a CALL      -> conservatively both: the callee may read it
                               and may write it, so an argument handed to a
                               CALL before being assigned is "inout"
    * never assigned        -> "in"

    Conservative by construction: when a use is ambiguous the argument is
    widened to "inout", which keeps it in the Python signature. The failure
    mode of guessing "out" is a caller that cannot pass a value it needs to
    pass; the failure mode of guessing "inout" is a redundant argument.
    """
    params = [p.lower() for p in routine["params"]]
    if not params:
        return {}

    written: set[str] = set()
    read_before_write: set[str] = set()

    def note_reads(text: str) -> None:
        for param in params:
            if param not in written and _mentions(param, text):
                read_before_write.add(param)

    for raw_statement in routine["body"].splitlines():
        statement = raw_statement.strip()
        if not statement:
            continue

        keyword = re.match(r"^(\w+)", statement)
        keyword_text = keyword.group(1).upper() if keyword else ""

        # Declarations mention arguments without using them. Counting
        # `DOUBLE PRECISION X` or `DIMENSION A(N)` as a read makes every
        # declared output look like an input.
        if keyword_text in _DECLARATION_KEYWORDS:
            continue

        # A logical IF is two things: a condition (reads) and a guarded
        # statement (which may assign). `IF (N .LT. 1) N = 20` both reads and
        # writes N, and dropping the condition loses the read.
        if _LOGICAL_IF_RE.match(statement):
            condition, guarded = _split_logical_if(statement)
            note_reads(condition)
            if not guarded:
                continue
            statement = guarded
            keyword = re.match(r"^(\w+)", statement)
            keyword_text = keyword.group(1).upper() if keyword else ""

        if keyword_text == "READ":
            read_targets = _READ_RE.match(statement)
            if read_targets:
                for param in params:
                    if _mentions(param, read_targets.group("targets")):
                        written.add(param)
                continue

        call = _CALL_RE.match(statement)
        if call:
            args = call.group("args") or ""
            callee = _callee_intents(call.group("name"), normalized, _seen)

            if callee is None:
                # Callee not available: pass-by-reference means it may read
                # and may write every argument, so widen conservatively.
                note_reads(args)
                for param in params:
                    if _mentions(param, args):
                        written.add(param)
                continue

            # Resolved: map actual arguments onto the callee's intents.
            callee_intents = list(callee.values())
            actuals = _actual_argument_names(args)
            for position, actual in enumerate(actuals):
                intent = (
                    callee_intents[position]
                    if position < len(callee_intents)
                    else "inout"
                )
                if actual is None or actual not in params:
                    continue
                if intent in ("in", "inout"):
                    note_reads(actual)
                if intent in ("out", "inout"):
                    written.add(actual)

            # Arguments passed inside expressions are reads regardless.
            for position, actual in enumerate(actuals):
                if actual is None:
                    note_reads(_split_declarator_list(args)[position])
            continue

        if keyword_text not in _NON_ASSIGNMENT_KEYWORDS:
            assignment = _ASSIGNMENT_RE.match(statement)
            if assignment:
                target = assignment.group("target").lower()
                # `F(X) = X*2.0*B` where F is a STATEMENT FUNCTION is a
                # declaration wearing an assignment's clothes: nothing
                # executes here, so nothing is read. Counting its RHS as a
                # read marks a pure-output dummy (B above) as "inout" —
                # widened, so never a wrong answer, but a signature demanding
                # a value the routine never consumes. The names come from the
                # fparser2 tree, which recognises Stmt_Function_Stmt exactly.
                if target in statement_functions:
                    continue
                rest = statement[assignment.end() :]
                if assignment.group("subscript"):
                    # A subscript is a read of whatever indexes it, and an
                    # element assignment does not define the whole array.
                    rest = assignment.group("subscript") + " " + rest
                note_reads(rest)
                if target in params:
                    written.add(target)
                continue

        note_reads(statement)

    intents = {}
    for param in params:
        if param not in written:
            intents[param] = "in"
        elif param in read_before_write:
            intents[param] = "inout"
        else:
            intents[param] = "out"
    return intents


def list_routine_names(normalized: str) -> list[str]:
    """Every SUBROUTINE/FUNCTION name in normalized fixed-form source."""
    return [
        m.group("name")
        for m in (_ROUTINE_NAME_RE.match(line.strip()) for line in normalized.splitlines())
        if m
    ]


# --- f2py directive injection -------------------------------------------

# f2py reads `Cf2py`/`cf2py` comment lines in fixed-form source as signature
# directives. Writing the inferred intents there is what actually makes the
# outputs come back to Python; the IR alone changes nothing about the build.
#
# Mapping, and why:
#   scalar out    -> intent(out)      dropped from the call, returned
#   scalar inout  -> intent(in,out)   passed in, returned (value semantics)
#   array  either -> intent(in,out)   the caller supplies the storage
#
# Arrays never get a bare intent(out): they are routinely dimensioned by a
# PARAMETER from an INCLUDE deck (`AE(MXCELL)`), and asking f2py to allocate
# one means allocating MXCELL elements per call. Requiring the caller to pass
# the array keeps the size where the caller can see it.

_DIRECTIVE_PREFIX = "Cf2py"


def _f2py_intent(intent: str, is_array: bool) -> str | None:
    if intent == "in":
        return None
    if is_array:
        return "in,out"
    return "out" if intent == "out" else "in,out"


def _routine_statement_end(lines: list[str], start: int) -> int:
    """Index of the last physical line of the routine's opening statement.

    A continued SUBROUTINE statement spans several lines; the directives have
    to go after all of them or they land in the middle of the argument list.
    """
    index = start
    while index + 1 < len(lines):
        nxt = lines[index + 1]
        if nxt[:1] in _COMMENT_CHARS or not nxt.strip():
            break
        if len(nxt) > 5 and nxt[5] not in (" ", "0"):
            index += 1
            continue
        break
    return index


def inject_intent_directives(
    source: str, routines: dict[str, dict[str, tuple[str, bool]]]
) -> str:
    """Insert `Cf2py intent(...)` lines into fixed-form `source`.

    `routines` maps routine name -> {argument: (intent, is_array)}. Applied to
    a generated copy, never to the original file.
    """
    lines = source.splitlines()
    insertions: dict[int, list[str]] = {}

    for name, arguments in routines.items():
        directives = []
        for argument, (intent, is_array) in arguments.items():
            spec = _f2py_intent(intent, is_array)
            if spec:
                directives.append(f"{_DIRECTIVE_PREFIX} intent({spec}) {argument}")
        if not directives:
            continue

        start_re = re.compile(
            rf"^\s*(?:[\w*()]+\s+)*(?:SUBROUTINE|FUNCTION)\s+{re.escape(name)}\b",
            re.IGNORECASE,
        )
        for index, line in enumerate(lines):
            if line[:1] in _COMMENT_CHARS or not line.strip():
                continue
            # Skip the label field so a statement label can't shift the match.
            statement = line[6:] if len(line) > 6 and line[5] in (" ", "0") else line
            if start_re.match(statement):
                insertions[_routine_statement_end(lines, index)] = directives
                break

    if not insertions:
        return source

    out: list[str] = []
    for index, line in enumerate(lines):
        out.append(line)
        out.extend(insertions.get(index, []))
    return "\n".join(out) + "\n"
