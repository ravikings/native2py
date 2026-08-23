"""The generated numerical-regression test (see native2py/golden.py).

Deliberately self-contained: it reads `golden.json` and replays the calls
recorded there with nothing but the standard library and the service's own
package. A generated service is a deployable artifact — making its test suite
import native2py would mean shipping the code generator alongside the thing
it generated, or a CI job that passes locally and fails in the container.

The test itself lives in `native2py/templates/golden_test_template.py` as a
real, compilable `.py` file rather than a string literal here, so that this
project's own tooling can see inside it. This module only substitutes the
three sentinel names in it; see that package's docstring for why substitution
is textual rather than `str.format`.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "golden_test_template.py"

#: Sentinel name in the template -> name of the substitution that replaces it.
PLACEHOLDERS = {
    "NATIVE2PY_PACKAGE": "package",
    "NATIVE2PY_SERVICE_NAME": "service",
    "NATIVE2PY_GOLDEN_FILENAME": "golden_filename",
}

_PLACEHOLDER_RE = re.compile(
    "|".join(sorted(map(re.escape, PLACEHOLDERS), key=len, reverse=True))
)


def _read_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def generate_golden_test(package_name: str, service_name: str, golden_filename: str) -> str:
    values = {
        "package": package_name,
        "service": service_name,
        "golden_filename": golden_filename,
    }
    # One pass, so a substituted value that happens to contain a sentinel is
    # never itself rewritten by a later replacement.
    return _PLACEHOLDER_RE.sub(
        lambda match: values[PLACEHOLDERS[match.group(0)]],
        _read_template(),
    )
