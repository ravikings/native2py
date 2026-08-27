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
# `character*32`, `character(len=32)`, `character(32)` — a DEFINITE length,
# which is what makes a CHARACTER output bindable. `character*(*)` and
# `character(len=*)` are assumed-length: f2py builds them and then silently
# returns b'' — measured. One pattern, used by BOTH Fortran backends, so the
# rule cannot drift between them.
CHAR_FIXED_LEN_RE = re.compile(r"^character(\*\d+|\(len=\d+\)|\(\d+\))$")

_LENGTH_SUFFIX = r"(?P<length>\s*\*\s*(?:\d+|\(\s*\*\s*\)))?"

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


# --- documentation comments ---------------------------------------------
#
# The routine's comment header becomes `FunctionDef.doc`, which the generator
# turns into the wrapper's Python docstring — and that docstring is what
# FastAPI publishes as the route `description`, which FastMCP hands to a model
# as the MCP tool description. So this is not cosmetic: it is the only text a
# model gets about what a native routine actually computes.
#
# Both Fortran backends land here. The cleaning rules are the interesting part
# and they are dialect-independent, so they live in this module — shared —
# rather than being written twice and drifting. What each backend supplies is
# only the line span of the routine's opening statement: the fparser2 reader
# takes it from the parse tree, the regex reader recovers it with
# `find_header_lines`. The differential harness compares whole `ModuleIR`s, so
# any disagreement between the two is a test failure rather than a surprise in
# someone's tool description.
#
# Comments are read off the source TEXT, by line number, and not out of any
# parse structure. That is not laziness: measured on
# libraries/petro/fortran/pvtcor.f, the header sitting inside SUBROUTINE
# PVTINI is not a child of fparser2's `Specification_Part` at all — it is
# buried inside an `Implicit_Part`, because where a comment node lands depends
# on which grammar rule happened to be open when the reader reached it.
# Walking for that is a promise to re-learn fparser2's node placement every
# release, and it would give the regex reader nothing.

# A fixed-form comment is any line with C, c, *, or ! in column 1. 'D'/'d' in
# column 1 is a debug line, not a comment, and is deliberately not treated as
# documentation.
_FIXED_COMMENT_RE = re.compile(r"^[Cc*!]\s?(.*)$")
_FREE_COMMENT_RE = re.compile(r"^\s*!+\s?(.*)$")

# How far to look. A header longer than this is a change log or a table of
# COMMON block layouts, not a description, and pasting 200 lines of it into a
# tool description is actively harmful to the model reading it. Both limits
# are truncation points, not rejections: the first N lines of a long header
# are still the useful part.
_MAX_DOC_LINES = 30
_MAX_DOC_CHARS = 2000


# `preprocess.expand_includes` splices the include file in as a comment-marked
# block, and on the fixed-form path that text is what the reader (and so this
# module) sees. An INCLUDE almost always sits directly under the routine's
# header comment, so without this the include file's own banner — 8 lines
# about COMMON block ordering in PETRO.INC — is appended to the description of
# every routine in every deck that includes it. The marker line terminates the
# comment run, exactly as a line of code would.
_INCLUDE_MARKER_RE = re.compile(r"nativegate: (expanded|end) INCLUDE ")


def comment_text(line: str, fixed: bool) -> str | None:
    """The prose in `line`, or None if `line` is not a comment line."""
    if _INCLUDE_MARKER_RE.search(line):
        return None
    match = (_FIXED_COMMENT_RE if fixed else _FREE_COMMENT_RE).match(line)
    if match is None:
        # In fixed form a `!` can also open a comment after column 1 — most
        # F77 that has been touched since 1990 has picked up bang comments.
        if fixed:
            stripped = line.lstrip()
            if stripped.startswith("!"):
                return stripped.lstrip("!").strip()
        return None
    return match.group(1).strip()


def is_banner(text: str) -> bool:
    """True for a separator rule rather than prose.

    Legacy decks are full of `C=======`, `C*******` and `C-------`. They carry
    no information, and left in they dominate the tool description — the
    PVTINI header in pvtcor.f is three lines of prose between two 71-character
    rules. The test is "contains no letter or digit", which drops rules of any
    punctuation character and any length while keeping anything with a word in
    it (so `C--- 05-FEB-1996: FIXED DIVIDE BY ZERO` survives).
    """
    return not any(ch.isalnum() for ch in text)


def clean_doc(raw_lines: list[str], fixed: bool) -> str | None:
    """Comment lines -> docstring prose, or None if nothing survives."""
    kept: list[str] = []
    for line in raw_lines:
        text = comment_text(line, fixed)
        if text is None:
            continue
        # Tabs are legal in fixed form and wreck alignment once the text is
        # re-indented inside a generated docstring; a tab counts as a space.
        text = text.replace("\t", " ").rstrip()
        # Blank comment lines (`C` alone) and rules are dropped outright
        # rather than kept as paragraph breaks: keeping them means a
        # description that opens with an empty line more often than not.
        if not text or is_banner(text):
            continue
        kept.append(text)
        if len(kept) >= _MAX_DOC_LINES:
            break

    if not kept:
        # No header, or a header that was pure ASCII art. Never invent text:
        # the generator has its own truthful skeleton for that case.
        return None

    doc = "\n".join(kept)[:_MAX_DOC_CHARS].rstrip()

    # Carriage returns from CRLF decks are dropped: they are invisible and make
    # generated files diff badly. That is the ONLY character this function
    # rewrites.
    #
    # In particular it does NOT neutralise a `\"\"\"` or a trailing backslash,
    # even though both would break the docstring this text is interpolated
    # into. Doing so here was tried and reverted: it is lossy, and it rewrites
    # a routine's documentation to suit ONE consumer's quoting rules. The IR
    # promises the deck's own words, and every other consumer — JSON, an
    # OpenAPI description, a future doc site — inherits the damage. Escaping
    # belongs to the layer that owns the quoting context, so it lives in
    # generators/python_pkg_gen._docstring_literal, which emits repr() for
    # anything carrying a quote, a backslash or a control character and is
    # tested against hostile text in tests/test_endpoint_docstrings.py. The
    # C++ parser's _sanitise_doc made the same call, for the same reason.
    doc = doc.replace("\r", "")
    return doc or None



def doc_comment_at(
    source: str, first_line: int, last_line: int, fixed: bool
) -> str | None:
    """The comment header attached to a routine, cleaned, or None.

    `first_line`/`last_line` are 1-based line numbers of the routine's opening
    SUBROUTINE/FUNCTION statement in `source` (equal for a statement that is
    not continued). Two positions are conventional and both are read:

    * *inside* the routine, immediately after the opening statement —
      overwhelmingly the F77 style, and what every deck in
      libraries/petro/fortran does;
    * *above* the routine — the free-form style.

    The inside block wins when both are present. Above-the-routine comments
    are the riskier source: the run above a routine also picks up the previous
    routine's trailing notes and the file banner, because a blank line is not
    required between program units and legacy decks rarely leave one. Both
    runs stop at the first line that is not a comment, so neither can walk
    past the nearest piece of code.

    Both Fortran backends call this, on the same text, so the doc they put in
    the IR is identical by construction — the differential harness in
    tests/test_fortran_fparser.py compares whole `ModuleIR`s, so a doc the two
    readers disagreed about would be a parity failure.
    """
    lines = source.splitlines()
    if not (1 <= first_line <= len(lines)):
        return None

    # Inside: the contiguous comment run starting on the line after the
    # opening statement. Anchored on `last_line`, so a continued statement
    # (`SUBROUTINE FOO(A,` / `     * B)`) is skipped whole.
    inside: list[str] = []
    idx = last_line
    while idx < len(lines) and comment_text(lines[idx], fixed) is not None:
        inside.append(lines[idx])
        idx += 1
    doc = clean_doc(inside, fixed)
    if doc is not None:
        return doc

    # Above: the contiguous comment run ending on the line before the opening
    # statement, walked backwards and put back in source order.
    above: list[str] = []
    idx = first_line - 2
    while idx >= 0 and comment_text(lines[idx], fixed) is not None:
        above.append(lines[idx])
        idx -= 1
    return clean_doc(list(reversed(above)), fixed)


def _logical_lines_with_spans(source: str, fixed: bool) -> list[tuple[str, int, int]]:
    """(statement, first_line, last_line) for every logical line in RAW source.

    A local, span-preserving re-run of what `normalize_fixed_form` /
    `fortran_regex.normalize_free_form` already do. Those two throw the line
    numbers away — reasonably, since nothing else needed them — and the
    comment header is found by line number, so the join is repeated here with
    the mapping kept. Comment and blank lines are dropped, exactly as they are
    there, which is what stops a commented-out header from being matched.

    Deliberately not a full normalisation: string literals are not tracked and
    inline comments are not stripped, because the only consumer is a routine
    header match and neither can appear in one.
    """
    out: list[tuple[str, int, int]] = []
    lines = source.splitlines()
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        if fixed:
            if raw[0] in _COMMENT_CHARS:
                continue
            # Columns 1-6 are label and continuation, 73+ the sequence number.
            body = raw[:72]
            continues = len(body) > 5 and body[5] not in (" ", "0")
            text = (body[6:] if len(body) > 6 else "").strip()
            if continues and out:
                stmt, first, _ = out[-1]
                out[-1] = (stmt + " " + text, first, i + 1)
                continue
            out.append((text, i + 1, i + 1))
        else:
            if raw.lstrip().startswith("!"):
                continue
            text = raw.strip()
            # A free-form statement continues when the PREVIOUS line ended
            # with `&`; a leading `&` on this one only marks where it resumes.
            if out and out[-1][0].endswith("&"):
                stmt, first, _ = out[-1]
                resumed = text[1:] if text.startswith("&") else text
                out[-1] = (stmt[:-1] + " " + resumed, first, i + 1)
                continue
            out.append((text, i + 1, i + 1))
    return out


def find_header_lines(
    source: str, name: str, fixed: bool = True
) -> tuple[int, int] | None:
    """1-based line span of `name`'s opening statement in RAW source.

    The fparser2 backend gets this for free from the parse tree's line spans.
    The regex backend does not: it works off normalised text — comments
    stripped, continuations joined — whose line numbers no longer correspond
    to the file, and the comment header is precisely what normalisation threw
    away. So the raw text is scanned once more here.

    The span covers the whole statement including its continuation lines, so
    the "inside" comment run starts after the header however it was wrapped —
    `SUBROUTINE NODAL (PRAVG, QMAX, ...` in wellib.f wraps over three lines
    and its header comment is under the third.
    """
    if fixed:
        start_re = re.compile(
            _ROUTINE_START_RE_TEMPLATE.format(name=re.escape(name)), re.IGNORECASE
        )
    else:
        # Free form: same shape, but a `result(...)` suffix may follow the
        # argument list, so the statement is not required to end after it.
        start_re = re.compile(
            r"^[\w\s*(),=]*?\b(?:SUBROUTINE|FUNCTION)\s+"
            + re.escape(name)
            + r"\s*(?:\(|$)",
            re.IGNORECASE,
        )

    for stmt, first, last in _logical_lines_with_spans(source, fixed):
        if start_re.match(stmt.strip()):
            return first, last
    return None



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
    # CHARACTER length spellings ("character*32", "character*(*)"), kept
    # separately because the LENGTH decides whether f2py can return the value
    # — see the demotion in both backends.
    char_lengths: dict[str, str] = {}
    arrays: set[str] = set()

    for line in body_lines:
        decl = _OLD_DECL_RE.match(line)
        if decl:
            base = re.sub(r"\s+", " ", decl.group("type").strip().lower())
            length = (decl.group("length") or "").replace(" ", "")
            for item in _split_declarator_list(decl.group("names")):
                var = item.split("(")[0].strip()
                if not var or not var[0].isalpha():
                    continue
                declared_types[var.lower()] = base
                if base == "character":
                    char_lengths[var.lower()] = f"character{length}"
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
        "char_lengths": char_lengths,
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
