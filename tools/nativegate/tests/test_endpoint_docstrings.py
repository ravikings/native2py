"""Native documentation reaching the generated endpoint's docstring (ROADMAP 3.4).

The docstring is not decoration. FastAPI publishes it as the route's OpenAPI
`description`, and FastMCP hands that to a model as the MCP tool description
(generators/mcp_gen.py) — so this text is what an LLM reads before deciding how
to call a numerical routine. Two properties matter, and they pull against each
other, which is why both are pinned here:

* **the native source's own words survive verbatim.** nativegate does not
  paraphrase a routine's contract; a plausible-sounding paraphrase is worse
  than silence.
* **that text cannot break the file it lands in.** It comes from a header or a
  Fortran deck nobody vetted. A `\"\"\"` closes the docstring early and turns the
  remainder of a comment into executable Python; a trailing backslash escapes
  the closing quotes and swallows the code after it. Both occur in real
  sources — `\\` continues a macro line, and Doxygen examples quote things — so
  this is an injection channel, not a hypothetical.

The hostile cases below are executed, not merely compiled: a test that only
checked `compile()` would pass on source that parses *and* runs an injected
statement.
"""

from __future__ import annotations

import pytest

from nativegate.generators.python_pkg_gen import generate_router_py
from nativegate.ir import FunctionDef, ModuleIR, Parameter


def _module(doc: str | None) -> ModuleIR:
    return ModuleIR(
        name="svc",
        language="fortran",
        source_file="svc.f90",
        functions=[
            FunctionDef(
                name="solution_gor",
                parameters=[Parameter(name="pressure", type="float")],
                returns="float",
                doc=doc,
            )
        ],
    )


def _endpoint_doc(doc: str | None):
    """Generate, execute, and return the endpoint's real `__doc__`.

    Executed with the REAL fastapi APIRouter — the generated module imports it
    itself, so a stub would be overridden anyway — and the native symbol
    injected, because `from . import solution_gor` has no package to resolve
    against here.

    Returns (docstring, namespace) so a caller can also assert that nothing
    which should not have run, ran.
    """
    source = generate_router_py(_module(doc), "svc")
    body = "\n".join(
        line for line in source.splitlines() if not line.startswith("from . import")
    )
    namespace: dict = {"solution_gor": lambda pressure: pressure}
    exec(compile(body, "router.py", "exec"), namespace)  # noqa: S102
    endpoint = {
        route.path: route.endpoint for route in namespace["router"].routes
    }["/solution_gor"]
    return endpoint.__doc__, namespace


# --- the native text survives -------------------------------------------


def test_the_native_comment_becomes_the_endpoint_description():
    doc, _ = _endpoint_doc("Compute the solution gas-oil ratio.")

    assert "Compute the solution gas-oil ratio." in doc
    # nativegate's own line is kept as well: a native comment rarely names the
    # symbol it belongs to, and the model needs that to reason about the call.
    assert "`solution_gor`" in doc


def test_a_multi_line_comment_keeps_its_structure():
    doc, _ = _endpoint_doc(
        "Solution GOR.\n\nUses Standing's correlation.\nValid 100-3000 psia."
    )

    assert "Solution GOR." in doc
    assert "Uses Standing's correlation." in doc
    assert "Valid 100-3000 psia." in doc


@pytest.mark.parametrize("doc", [None, "", "   ", "\n\n"], ids=repr)
def test_no_comment_falls_back_to_nativegates_own_line(doc):
    """A routine with no documentation still gets a usable description.

    An empty docstring would be worse than the fallback: FastMCP would then
    derive a title-cased function name ("Solution Gor Endpoint"), which tells
    the model nothing.
    """
    result, _ = _endpoint_doc(doc)

    assert result is not None
    assert "Call the native routine `solution_gor`." in result


# --- the native text cannot break the file ------------------------------


# Quote handling is the fiddly part, so the boundaries are enumerated rather
# than sampled. Only `\"\"\"` and a TRAILING `\"` can actually end the literal;
# a quote anywhere else is ordinary text, and rejecting it would push perfectly
# normal prose into the unreadable repr() form for nothing.
HOSTILE = {
    "triple_quote": 'He said """ then BREACH = 1',
    "quadruple_quote": 'a """" b',
    "trailing_quote": 'He said "',
    "leading_quote": '"quoted start',
    "two_trailing_quotes": 'ends with ""',
    "interior_quotes": 'The "pressure" argument',
    "trailing_backslash": "Path is C:\\data\\",
    "escape_sequences": "Uses \\n and \\t and C:\\temp",
    "invalid_escape_N_brace": "bad \\N{ escape",
    "carriage_return_and_tab": "line1\r\n\tline2",
    "docstring_then_code": '"""\nimport os\nBREACH = 1\n"""',
    "multiline_with_quotes": 'Solution GOR.\n\nThe "pressure" is psia.\nValid 100-3000.',
}


@pytest.mark.parametrize("doc", HOSTILE.values(), ids=list(HOSTILE))
def test_hostile_comment_text_produces_valid_python_and_executes_nothing(doc):
    result, namespace = _endpoint_doc(doc)

    # Reaching here at all means it compiled AND executed without a SyntaxError.
    assert "BREACH" not in namespace, (
        "comment text escaped the docstring and executed as code"
    )
    assert result is not None


@pytest.mark.parametrize("doc", HOSTILE.values(), ids=list(HOSTILE))
def test_hostile_comment_text_is_still_carried_faithfully(doc):
    """Escaping must not silently drop the documentation it is protecting.

    The easy wrong fix is to strip anything awkward, which quietly throws away
    a routine's real contract. The text is preserved; only its *encoding* in
    the generated file changes.
    """
    result, _ = _endpoint_doc(doc)

    # Compared on non-whitespace content: the generator re-indents multi-line
    # text to sit inside the function, so exact whitespace legitimately differs.
    assert "".join(doc.split()) in "".join(result.split())


def test_generation_remains_byte_identical_when_repeated():
    """Generated output is diffed by CI; the escaping choice must be stable."""
    doc = 'He said """ and then C:\\data\\'
    assert generate_router_py(_module(doc), "svc") == generate_router_py(
        _module(doc), "svc"
    )


def test_ordinary_prose_with_quotes_stays_a_readable_block():
    """Precision here is a readability property, not just a safety one.

    A 20-line Fortran header rendered as a single-line repr() is hard to read
    in a file people debug even though they may not edit it. Since an interior
    quote cannot end the literal, it must not trigger that fallback.
    """
    from nativegate.generators.python_pkg_gen import generate_router_py

    source = generate_router_py(_module('The "pressure" argument, psia.'), "svc")
    body = source.split("def solution_gor_endpoint")[1]

    assert '"""The "pressure" argument, psia.' in body, (
        "an interior quote pushed ordinary prose into the repr() fallback"
    )
