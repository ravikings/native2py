"""Files that are shipped verbatim and rendered into a generated service.

A template here is a REAL `.py` file, not a string literal inside a
generator. That is the whole point: as a string it was invisible to this
project's own linting, type checking and test suite, so a syntax error or a
stale reference inside it only surfaced when somebody ran the generated
service. As a module it is parsed, compiled and checked like any other file.

Rendering is plain textual substitution of the sentinel names below — not
`str.format`, which would require every literal brace in the template to be
doubled and so make the file invalid Python again.

The sentinels are chosen to be syntactically valid where they appear
(`NATIVE2PY_PACKAGE` stands in for a module name, the other two live inside
string literals), which is why the file compiles on its own. It is not
*runnable* on its own: `import NATIVE2PY_PACKAGE` resolves only once the
name has been substituted for a real service package.
"""

from __future__ import annotations
