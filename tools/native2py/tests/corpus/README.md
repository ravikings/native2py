# The differential IR corpus

The acceptance gate for a parser replacement (ROADMAP 1.4b / 3.2).

`harness.py` parses every source in `libraries/petro` and `libraries/geometry`
under an **explicitly named** parser backend and snapshots the resulting
`ir.ModuleIR` as JSON under `snapshots/<language>-<backend>/`. Swapping a front
end then has a falsifiable acceptance criterion: *no symbol in the corpus
changed, and here is the list of the ones that did.*

## Invoking it

Standalone — this is the mode the parser swap runs in, repeatedly, outside the
suite:

```
cd tools/native2py
python tests/corpus/harness.py list                       # corpus + backends available here
python tests/corpus/harness.py check                      # live parse vs committed snapshots
python tests/corpus/harness.py snapshot --update          # re-record (all backends)
python tests/corpus/harness.py snapshot --update -b cpp:clang
python tests/corpus/harness.py diff cpp:clang cpp:regex   # per-symbol, parsed live
python tests/corpus/harness.py diff fortran:regex fortran:fparser2 --fail-on-diff
```

As pytest: `pytest tests/corpus`. It asserts the same thing `check` does, for
every backend that can run here *and* has committed snapshots. A backend with
no snapshots is skipped, not failed — that is the normal state of a front end
on the day it is introduced; `snapshot --update -b <key>` adopts it.

## Backends are named, never inferred

Backend keys are `<language>:<name>`, discovered from the front door's own
`BACKENDS` tuple (`parsers/cpp.py`, `parsers/fortran.py`) and filtered through
its `resolve_backend`. `auto` is excluded on purpose: a snapshot taken under
"whichever parser happened to be installed" is not evidence of anything. A new
backend name — a Fortran fparser2/flang front end, a second C++ reader —
becomes a new snapshot directory with no change to the harness.

Fortran has no expose-everything mode (deliberately: a 20,000-line deck must
not cost a full parse), so the harness asks `list_routine_names` what is in
each file and exposes all of it.

## Snapshot format

`snapshots/<language>-<backend>/<repo-relative source>.json`, `sort_keys=True`,
indent 2. Every string is rewritten repo-relative — Clang names an anonymous
record `(unnamed struct at /abs/path/FortranBridge.hpp:115:8)`, and diagnostics
quote the include that failed, so normalizing only `source_file` is not enough.
`native2py_version` is stripped (it would churn every snapshot on an unrelated
release); `schema_version` is kept, because a change to *that* is a change to
the contract these snapshots describe.

## What the clang-vs-regex diff shows today

`diff cpp:clang cpp:regex` over the 7-header C++ corpus:
**144 differing symbols across 7 files**, all of them in the same direction —
`cpp:regex` sees strictly less. This is expected and is not a failure; it is
the calibration reading for how much a real front end is worth.

| kind | count | what it means |
|---|---|---|
| `native_type` lost | 104 | regex records no native spelling, so every parameter/field is `None`. `pybind_gen` needs it for `py::init<...>`, which cannot recover a type from a function pointer — this is the difference between a correct constructor overload and a truncated 64-bit id. |
| `is_const` lost | 63 | every `double f() const` reads as non-const. `py::overload_cast` needs `py::const_` to pick the const member of a const/non-const pair, so the wrong overload binds — or none compiles. |
| 13 free functions missing | 13 | all 11 free functions in `Units.hpp` (`ft_to_m`, `psi_to_bar`, …) plus `fipoil_`/`trncal_` in `FortranBridge.hpp`. The regex reader never finds them; they simply do not exist in the generated package. |
| 26 skips missing | 26 | the `extern "C"` F77 declarations in `FortranBridge.hpp` (`pvtini_`, `krwat_`, `step_`, …). Clang reports each as an unbindable pointer argument *with a reason*; regex says nothing at all, which is the failure mode `_report_skipped` exists to prevent. |
| 3 anonymous structs missing | 3 | the unnamed COMMON-block mirrors in `FortranBridge.hpp`. |
| 11 skip reasons worded differently | 11 | cosmetic: `'double *'` (Clang's canonical spelling) vs `'double*'` (source text). |
| 1 skip only in regex | 1 | regex reports `F77_NAME` — the macro itself — as a skipped symbol. Clang runs the preprocessor, so the macro is expanded and never appears as a declaration. A false positive, and a good illustration of the class. |

The single most consequential line is the free functions: a service generated
from `Units.hpp` with the regex reader is missing eleven entry points, and
nothing anywhere says so.

## When a diff appears in `check`

It means a parser change moved an answer. Read the per-symbol report, decide
whether the move is a fix or a regression, and only then re-record. Re-recording
first is how a regression becomes the new truth.
