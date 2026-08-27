# Troubleshooting

## `ngate build` fails: `pybind11_DIR` not found / `find_package(pybind11 CONFIG REQUIRED)` fails

You need `pybind11`'s CMake config, not just the Python package alone in
some environments. If `pip install pybind11` isn't enough:

```bash
pip install pybind11
python3 -c "import pybind11; print(pybind11.get_cmake_dir())"
```

and pass that path explicitly if CMake still can't find it:

```bash
cmake -S . -B build -Dpybind11_DIR="$(python3 -c 'import pybind11; print(pybind11.get_cmake_dir())')"
```

## CMake picks the wrong Python interpreter

If you have multiple Python installs (pyenv, conda, system), CMake's
`find_package(Python)` may pick one that doesn't have `pybind11`/`numpy`
installed. Pin it explicitly:

```bash
cmake -S . -B build -DPYTHON_EXECUTABLE="$(python3 -c 'import sys; print(sys.executable)')"
```

## `gfortran: command not found`

Fortran services need a real Fortran compiler. On macOS:

```bash
brew install gcc   # provides gfortran, gfortran-<version>, etc.
```

## `cmake: command not found`

```bash
brew install cmake
```

## Build fails with `lipo: can't open input file: ninja`

`scikit-build-core` (which `ngate build`/`quickstart --build` use under
the hood) defaults to the Ninja generator. Install it:

```bash
brew install ninja        # macOS
apt-get install ninja-build   # Debian/Ubuntu
```

## `ModuleNotFoundError` for a compiled extension after `ngate generate`

`generate` only regenerates *source* (bindings, CMake, `__init__.py`) — it
does not compile anything. Run `ngate build <name>` (or `cmake --build`
directly) afterward, then make sure the resulting `.so` actually ends up
under `python/<name>/_native/` (that's where the generated `__init__.py`
looks for it — `scikit-build-core`'s `install(TARGETS ...)` destination in
the generated `CMakeLists.txt` handles this when building the wheel, but a
manual `cmake --build` alone won't place it there for you).

## `symbol not found in flat namespace` after a successful build

```
ImportError: dlopen(.../widget_cpp.cpython-311-darwin.so, 0x0002):
symbol not found in flat namespace '__ZN6Widget6squareEd'
```

Your C++ class has methods **declared** in the header but never **defined**
anywhere (no `.cpp`, and no inline body in the header). This compiles and
links "successfully" on macOS — undefined symbols in a Python extension
bundle are resolved lazily at `dlopen` time, not at link time — so
`ngate build` reports success right up until the first `import`.

Add the missing `.cpp` implementation under `services/<name>/native/`
(same stem as the header, e.g. `widget.hpp` → `widget.cpp`) and re-run
`ngate generate <name>` then `build`. `ngate quickstart` does this
copy automatically when given a header that has a sibling `.cpp` file next
to it — see [CLI reference](cli-reference.md#nativegate-quickstart-source).

## Fortran: `AttributeError: module 'physics' has no attribute 'calculate_pressure'`

This means you're importing the **raw f2py extension** directly instead of
going through the generated package. If your Fortran source wraps its
routines in a `module` block, f2py nests them one level deeper
(`physics.physics.calculate_pressure`, not `physics.calculate_pressure`).
Import from the generated Python package instead — `python/<name>/__init__.py`
already handles this nesting for you. See
[Exposing Fortran](fortran-guide.md#the-module-wrapper-gotcha) for details.

## `generate` fails: "Fortran services require an `expose.functions:` list"

The full message is:

```
Fortran services require an `expose.functions:` list in nativegate.yaml naming
exactly the routines to bind — there is no expose-everything fallback, so
large legacy templates only pay for what they use.
```

This is intentional, not a bug — see
[Exposing Fortran](fortran-guide.md#designed-for-large-legacy-files). Add
the routine names you want under `expose.functions:` in `nativegate.yaml`.

## `ExposeWarning`: "`expose:` is empty, so every symbol will be bound"

C++ only, and a warning rather than an error. An empty `expose:` block still
binds everything the parser finds, but the warning exists because a whole
header should not get bound on an unstated default. Say what you mean:
`expose: all` to take everything, `expose: false` to take nothing, or list
names under `expose.classes:` / `expose.functions:`.

## `undefined symbol: <routine>_` at import, after a clean Fortran build

The build compiled, but the routines your source *calls* were never compiled
in with it. For a Fortran service that depends on shared decks under
`libraries/`, declare them:

```yaml
libraries:
  - petro
```

`generate` then copies that library's `.f`/`.f90`/`.f77`/`.for` sources into
`services/<name>/native/_expanded/` and compiles them into the extension. If
`native/_expanded/` is missing or empty, `generate` has not been run since the
last `clean` — `clean` deletes it.

## A Fortran service is slower under load than one call suggests

Expected. Every generated Fortran endpoint holds one process-wide lock across
its native call, so native execution is one call at a time per process
regardless of how many worker threads FastAPI has. This is deliberate: COMMON
blocks are process-global storage, and concurrent calls would silently return
each other's numbers.

Add processes (more gunicorn workers, more pods), not threads. Do not remove
the lock. See
[Architecture](architecture.md#the-concurrency-model-one-native-call-per-process-fortran).

C++ services have no such lock — they construct a fresh instance per request
instead. If a C++ library keeps file-scope static state, that state *is*
shared across concurrent requests and nothing generated guards it; the parser
cannot see file-scope statics to know it should.

## `422 Unprocessable Entity` on a request with a large array

The array exceeded `MAX_ARRAY_ITEMS` (default 65536), and Pydantic rejected it
during request validation, before the array was ever materialised for the
native call. Raise it if your inputs are genuinely that large:

```bash
NATIVEGATE_MAX_ARRAY_ITEMS=500000 uvicorn <name>.service:app
```

Note what this is *not*: a check that the array matches the extent the routine
declares. The IR records that a parameter is an array, not how long the
routine expects it to be, so one configurable cap stands in for all of them.
It is a memory guard against an unbounded request body. Passing an array of
the wrong length for the routine is still your responsibility.

## `NativeTypeError: Unsupported native type '...'`

The parser found a parameter or return type not in `ir.TYPE_MAP` (C++) or
`ir.FORTRAN_TYPE_MAP` (Fortran) — e.g. pointers, `std::vector<T>`, custom
structs. Either add a mapping in `nativegate/ir.py`, or exclude that
class/function from `nativegate.yaml`'s `expose:` block for now. This error
is deliberate: nativegate refuses to silently generate a binding it can't
guarantee is safe.

## C++: `generate` reports "compiler error: ... file not found"

The Clang parser could not open a header yours `#include`s, so it refuses to
bind anything from that file. Add the missing directory to the `clang:` block
in `nativegate.yaml`:

```yaml
clang:
  include_paths:
    - libraries/common-cpp/include
```

Refusing the whole header is deliberate: clang recovers from a failed include
by treating every unknown name as `int`, so the alternative is bindings whose
signatures silently disagree with the C++ they call.

## C++: a symbol vanished after moving to another machine

Check the parser line printed by `generate`:

```
Parsing C++ with regex reader (libclang unavailable: ...)
```

The fallback reader has no preprocessor, so macro-generated declarations (the
`F77_NAME(...)` bridge pattern) and types from `#include`s are skipped there.
Install the AST parser and set `parser: clang` so this fails loudly instead of
shrinking your API:

```bash
pip install "nativegate[clang]"
```

If libclang is installed but its shared library is not found, point
`NATIVEGATE_LIBCLANG` at it (e.g.
`/Library/Developer/CommandLineTools/usr/lib/libclang.dylib`).

## C++: "is a template; pybind11 binds instantiations, not templates"

There is no single symbol to bind for `template <typename T> ...`. Add a
non-template wrapper for the instantiation you actually call, and expose that:

```cpp
double clamp_double(double v, double lo, double hi);   // bindable
```

## I deleted part of a service — what brings it back?

`ngate generate <name>` regenerates the bindings, `CMakeLists.txt`, the
Python package, the tests and `.nativegate/ir.json`, and restores
`pyproject.toml` if it is missing (it never overwrites one you have edited).
`ngate docker <name>` rewrites the Dockerfile. A deleted gateway comes
back with `ngate gateway <name> --service ...`.

What does **not** come back is anything you wrote: `native/` sources,
`nativegate.yaml`, and the recorded answers in `golden.json`.

`ngate init` will not help here. It only creates the empty top-level
directories (`services/`, `libraries/`, `tools/`, `infrastructure/*`) and is
harmless to re-run, but it restores no service content. See
[Architecture](architecture.md#what-is-generated-what-is-yours) for the full
table.

## `ngate build` fails with "no pyproject.toml"

It was deleted. Run `ngate generate <name>` — it writes a fresh one when
the file is missing.
