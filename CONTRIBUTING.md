# Contributing to nativegate

The generator lives in `tools/nativegate/`. Everything below runs from there
unless stated otherwise.

## Prerequisites

The test suite compiles real extension modules, so it needs a real toolchain:

- Python 3.10, 3.11, or 3.12 (CI covers all three on Linux and macOS)
- a C++ compiler, plus `cmake` and `ninja`
- `gfortran` for the Fortran/f2py paths

On Debian/Ubuntu: `sudo apt-get install gfortran cmake ninja-build`.
On macOS: `brew install gcc cmake ninja` (gcc supplies gfortran).

## Setting up

Install the package **editable, with all the extras**. This is not optional:

```bash
cd tools/nativegate
python -m venv .venv && source .venv/bin/activate
pip install -e ".[clang,build,test]"
```

Why the extras matter:

- `clang` pulls in libclang. Without it the C++ front end silently falls back
  to the regex reader, which has no preprocessor and no template support. Real
  parsing coverage disappears and the suite still passes.
- `build` pulls in scikit-build-core, pybind11, numpy, fastapi, uvicorn — the
  build and service generation paths.
- `test` pulls in pytest and httpx.

The editable install also creates the `ngate` console script. The tests in
`tests/test_quickstart.py` and `tests/test_multifile_cpp.py` shell out to it,
looking for it *next to `sys.executable`* — so it must be installed into the
very interpreter you run pytest with, and they **fail without it**. Those
failures are not environmental noise: they are the only end-to-end coverage of
the CLI. Do not dismiss them; fix the install.

Sanity-check both before you start:

```bash
command -v nativegate
python -c "from nativegate.parsers import cpp_ast; print(cpp_ast.is_available())"   # must print True
```

If that prints `False`, print `cpp_ast.unavailable_reason()` to see why, and
set `NATIVEGATE_LIBCLANG` to your libclang shared library if it cannot be found
automatically.

## Running the tests

```bash
cd tools/nativegate
python -m pytest -q
```

All tests must pass — the whole suite, with no expected failures. CI
(`.github/workflows/ci.yml`) runs exactly this across
{ubuntu-latest, macos-latest} × {3.10, 3.11, 3.12}, plus an explicit assertion
that `cpp_ast.is_available()` is `True`, so a machine that quietly lost
libclang fails the build instead of testing a weaker parser.

Run a single file or test while iterating:

```bash
python -m pytest -q tests/test_quickstart.py
python -m pytest -q tests/test_cpp_ast.py -k template
```

## Code review before commit

This repo gates commits on a completed code review. `.claude/hooks/` holds a
`PreToolUse(Bash)` hook that blocks `git commit` unless an approval has been
recorded for the **exact current diff**:

- `.claude/hooks/require-code-review.sh` — hashes the pending diff (tracked
  changes plus untracked, non-ignored files) with SHA-256 and denies the commit
  unless a marker file matching that hash exists. It fails **closed**: if it
  cannot determine the answer, it denies.
- `.claude/hooks/mark-reviewed.sh` — writes that marker.

The required order is:

1. `git add` everything you intend to commit.
2. Run `/code-review` on the pending diff and resolve or explicitly dismiss
   every finding.
3. Only after a genuine review, run `bash .claude/hooks/mark-reviewed.sh`.
4. Commit.

Two things to know:

- **If the diff changes after marking, the approval is void.** Review and mark
  again.
- **Approvals are per-worktree.** Run `mark-reviewed.sh` from the same
  directory the commit runs in.

Do not run `mark-reviewed.sh` without actually performing the review. The hook
is the only thing standing between an unreviewed change and `main`; defeating
it defeats the point of having it.

## Pull requests

- Branch off `main`; keep the change focused.
- Add or update tests in `tools/nativegate/tests/` for any behaviour change.
- Make sure the full suite passes locally before you open the PR — CI will run
  it on six configurations and any failure blocks the merge.
- `@ravikings` owns the repo (see `.github/CODEOWNERS`) and reviews every PR.
- Security issues do not belong in a PR or a public issue. See
  [SECURITY.md](SECURITY.md).
