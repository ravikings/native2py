# `native2py.yaml` reference

One file per service, at `services/<name>/native2py.yaml`. It is the only
hand-edited file in a service directory — everything else under
`bindings/`, `python/`, and `CMakeLists.txt` is regenerated.

```yaml
name: petro
language: fortran

expose:
  functions:
    - PVTINI
    - PVTRS

include_paths:
  - libraries/petro/fortran/include

libraries:
  - common-cpp

parser: auto
clang:
  std: c++17
```

## `name`

The Python package name. Defaults to the directory name, but they don't have
to match — `services/reservoir/` with `name: physics` produces
`from physics import ...`.

Also determines the compiled extension name (`<name>_cpp` for C++,
`<name>` for Fortran) and the generated bindings filename.

## `language`

`cpp` or `fortran`. Set by `create-service --language`; changing it later
means the existing generated files no longer apply, so regenerate.

## `expose`

Which symbols to bind.

```yaml
expose:
  classes:
    - Calculator      # C++ only
  functions:
    - calculate_pressure
```

| Language | Empty `expose:` means |
|---|---|
| C++ | expose everything found in the headers |
| Fortran | **error** — you must name routines explicitly |

The Fortran requirement is deliberate. Legacy sources run to tens of
thousands of lines and hundreds of routines; naming what you want keeps
parsing targeted, and for fixed-form F77 it's a *correctness* requirement
too — wrapping a whole file makes f2py emit C referencing PARAMETER
constants it never defines. See
[Exposing Fortran](fortran-guide.md#whole-file-wrapping-fails-per-routine-works).

For C++, listing even one name turns `expose:` into a filter: only listed
symbols are bound. Leave it empty to take everything.

Names are matched as written in the source. Fortran is case-insensitive, so
`PVTINI` in config binds `pvtini` in the extension — the generated
`__init__.py` aliases it back to the spelling you used.

## `include_paths`

Extra header/INCLUDE search directories, **relative to the repo root**.

```yaml
include_paths:
  - libraries/petro/fortran/include
```

| Language | Effect |
|---|---|
| C++ | added as `target_include_directories`, so a source can `#include` a header outside `native/` |
| Fortran | searched when expanding `INCLUDE 'FOO.INC'` statements |

A path that isn't a directory is an error at `generate` time rather than a
compiler failure later.

For C++, this makes the header available **to the compiler** — native2py
still only parses headers inside `native/`, so classes defined in an
external header don't get bindings of their own.

For fixed-form Fortran this is usually mandatory: `.INC` files hold the
COMMON blocks and IMPLICIT statements that determine parameter types, and
native2py must expand them itself because
[f2py mishandles in-body INCLUDEs](fortran-guide.md#the-include-trap).

## `libraries`

Shared native libraries under `libraries/` that this service links against.

```yaml
libraries:
  - common-cpp        # -> libraries/common-cpp/
```

Each entry must be a directory containing its own `CMakeLists.txt` defining
a target. The directory name may contain hyphens; the CMake target must not
(`common-cpp` → target `common_cpp`).

**C++ only.** The Fortran path builds through `f2py -c`, which doesn't take
`add_subdirectory`.

Declaring libraries changes the Docker build context to the repo root, since
`COPY` can't reach outside a service directory — `native2py docker` prints
the correct command. Full details in
[Deployment topologies](deployment-topologies.md#sharing-native-code-librariescommon-cpp).

## Field summary

| Field | Required | C++ | Fortran |
|---|---|---|---|
| `name` | defaults to dir name | ✅ | ✅ |
| `language` | ✅ | ✅ | ✅ |
| `expose` | Fortran only | optional filter | **required** |
| `include_paths` | no | header search | INCLUDE search |
| `libraries` | no | ✅ | ❌ not supported |

## `parser` (C++ only)

Which C++ front end to use.

| Value | Meaning |
|---|---|
| `auto` (default) | the Clang AST parser when libclang is importable, otherwise the regex reader |
| `clang` | require the Clang AST parser; a missing libclang is an error, not a downgrade |
| `regex` | force the token/brace fallback reader |

Set `clang` in CI. Under `auto`, a machine without libclang silently loses
every macro-defined and `#include`d symbol, and the build succeeds with a
smaller API than the one you tested. `$NATIVE2PY_CPP_PARSER` overrides this
file, and `native2py inspect --parser` overrides both.

## `clang` (C++ only)

Flags the AST parser needs to read your headers the way your build does.
Ignored by the regex reader.

```yaml
clang:
  std: c++20                          # default: c++17
  include_paths:                      # extra -I directories
    - libraries/common-cpp/include
  defines:                            # -D flags the build sets
    - LEGACY_API=1
  extra_args:
    - -Wno-deprecated
```

The header's own directory is always on the include path, so sibling headers
need no configuration. Note this is a *separate* key from the top-level
`include_paths`, which controls what CMake and the Fortran INCLUDE expander
see — the parser and the compiler can legitimately need different sets, and
conflating them would make a parse-only path change the build.

If clang cannot open an include, native2py reports the compiler error and
binds nothing from that header — see
[Exposing C++](cpp-guide.md#telling-the-parser-what-your-build-knows).
