# `nativegate.yaml` reference

One file per service, at `services/<name>/nativegate.yaml`. Along with your
`native/` sources and `golden.json`, it is what you own — everything under
`bindings/`, `python/`, `tests/`, and `CMakeLists.txt` is regenerated on every
`generate` and hand-edits there are lost. (`pyproject.toml` is a half
exception: generated once, then yours, and restored only if you delete it.)

This is the real `services/petro_api/nativegate.yaml`, the one service
committed in this repo:

```yaml
name: petro_api
language: fortran
expose:
  classes: []
  functions:
  - pvt_set_fluid
  - solution_gor
  - oil_fvf
  - oil_viscosity
  - gas_z_factor
  - bubble_point
  - pvt_state
  - tubing_bhp
  - vogel_rate
  - last_error
libraries:
- petro
```

A C++ service uses the same file with a different set of keys:

```yaml
name: calculator
language: cpp
expose: all
parser: clang
clang:
  std: c++20
  include_paths:
    - libraries/common-cpp/include
libraries:
  - common-cpp
api:
  auth: api_key
```

## `name`

The Python package name. Defaults to the directory name, but they don't have
to match — `services/petro_api/` with `name: physics` would produce
`from physics import ...`.

Also determines the compiled extension name (`<name>_cpp` for C++,
`<name>` for Fortran) and the generated bindings filename.

## `language`

`cpp` or `fortran`. Set by `create-service --language`; changing it later
means the existing generated files no longer apply, so regenerate.

Omitting it is an error unless it can be inferred unambiguously from the
sources under `native/` — it never silently defaults to `cpp`. If the sources
are mixed, or `language:` names a language the sources contradict, `generate`
stops rather than guessing.

## `expose`

Which symbols to bind.

```yaml
expose:
  classes:
    - Calculator      # C++ only
  functions:
    - calculate_pressure
```

"Bind everything" is an explicit opt-in. Say it as `expose: all`, or
`expose: {all: true}`:

| Value | Meaning |
|---|---|
| `expose: all` / `expose: {all: true}` | bind the whole public surface |
| `expose: false` / `expose: {all: false}` | bind nothing |
| `classes:` / `functions:` lists | bind only the names listed |
| empty `expose:` block | still binds everything found, but **warns** that the intent was never stated |

The warning on an empty block exists because the permissive default is load
bearing — the C++ parsers fall back to `[[nativegate::expose]]` annotations in
the source — but a whole header should not get bound on an unstated default.
Anything else (`expose: everything`, a non-boolean `all:`) is an error rather
than a silent fallback.

| Language | Empty `expose:` means |
|---|---|
| C++ | expose everything found in the headers, with a warning |
| Fortran | **error** — you must name routines explicitly |

The Fortran requirement is deliberate. Legacy sources run to tens of
thousands of lines and hundreds of routines; naming what you want keeps
parsing targeted, and for fixed-form F77 it's a *correctness* requirement
too — wrapping a whole file makes f2py emit C referencing PARAMETER
constants it never defines. See
[Exposing Fortran](fortran-guide.md#whole-file-wrapping-fails-per-routine-works).

For C++, listing even one name turns `expose:` into a filter: only listed
symbols are bound. Use `expose: all` to take everything.

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

For C++, this makes the header available **to the compiler** — nativegate
still only parses headers inside `native/`, so classes defined in an
external header don't get bindings of their own.

For fixed-form Fortran this is usually mandatory: `.INC` files hold the
COMMON blocks and IMPLICIT statements that determine parameter types, and
nativegate must expand them itself because
[f2py mishandles in-body INCLUDEs](fortran-guide.md#the-include-trap).

## `libraries`

Shared native libraries under `libraries/` that this service links against.

```yaml
libraries:
  - common-cpp        # -> libraries/common-cpp/
  - petro             # -> libraries/petro/
```

Supported for **both** languages, by two different mechanisms:

| Language | How it links |
|---|---|
| C++ | `add_subdirectory` on the library directory, then links its CMake target. Each entry must be a directory containing its own `CMakeLists.txt` defining a target. The directory name may contain hyphens; the CMake target must not (`common-cpp` → target `common_cpp`). |
| Fortran | the library's `.f`/`.f90`/`.f77`/`.for` sources are copied into `native/_expanded/` and compiled into the extension, with the library's `include/` directories auto-discovered for INCLUDE expansion. No `CMakeLists.txt` is required. |

The Fortran path is why `services/petro_api` works: it declares
`libraries: [petro]` and the seven F77 decks under `libraries/petro/fortran/`
end up in its `native/_expanded/`. Without it the build succeeds and then
fails at import with `undefined symbol: iprvog_`.

For **C++**, declaring libraries changes the Docker build context to the repo
root, since `COPY` can't reach outside a service directory — `nativegate
docker` prints the correct command. **Fortran keeps the service-directory
context**, because the library sources are already vendored into
`native/_expanded/` by `generate`. Full details in
[Deployment topologies](deployment-topologies.md#sharing-native-code-librariescommon-cpp).

## Field summary

| Field | Required | C++ | Fortran |
|---|---|---|---|
| `name` | defaults to dir name | ✅ | ✅ |
| `language` | ✅ (or inferable) | ✅ | ✅ |
| `expose` | Fortran only | optional filter | **required** |
| `include_paths` | no | header search | INCLUDE search |
| `libraries` | no | ✅ CMake target | ✅ sources vendored + compiled |
| `parser` | no | `auto`/`clang`/`regex` | ignored — use `$NATIVEGATE_FORTRAN_PARSER` |
| `clang` | no | ✅ | ignored |
| `fortran.defines` | no | ignored | ✅ |
| `api.auth` | no | ✅ | ✅ |

## `parser` (C++ only)

Which C++ front end to use.

| Value | Meaning |
|---|---|
| `auto` (default) | the Clang AST parser when libclang is importable, otherwise the regex reader |
| `clang` | require the Clang AST parser; a missing libclang is an error, not a downgrade |
| `regex` | force the token/brace fallback reader |

Set `clang` in CI. Under `auto`, a machine without libclang silently loses
every macro-defined and `#include`d symbol, and the build succeeds with a
smaller API than the one you tested. `$NATIVEGATE_CPP_PARSER` overrides this
file, and `ngate inspect --parser` overrides both.

### Choosing the Fortran front end

Fortran has the same two-backend arrangement, but **this key does not control
it** — it is C++ only. The Fortran backend is chosen by
`$NATIVEGATE_FORTRAN_PARSER`:

| Value | Meaning |
|---|---|
| `auto` (default) | fparser2 when the package is importable, otherwise the regex reader |
| `fparser2` | require fparser2; a missing package is an error, not a downgrade |
| `regex` | force the targeted per-routine fallback reader |

The regex reader loses derived-type flattening entirely, along with
grammar-accurate routine boundaries and structured attributes.

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
  scalar_ref_functions:               # see below
    - pvtrs
    - pvtbo
```

### `clang.scalar_ref_functions`

Names of `extern "C"` functions whose bare `T*` arguments are **single scalars
passed by reference** — the Fortran-linkage convention — rather than arrays.
`"*"` asserts it for every `extern "C"` function in the service.

This is opt-in per function and never inferred, because `double* x` is also
how C passes an array whose length travels through a COMMON block or a
PARAMETER. The two are indistinguishable in a C prototype, and binding an
array as one scalar hands the callee a pointer it reads past the end of. Only
someone who has read the Fortran knows which is which.

It applies to free functions under the **clang** backend only. Class methods
are never treated as scalar-ref, and the regex reader ignores the key.

## `fortran` (Fortran only)

`-D` defines applied when nativegate runs `gfortran -cpp` over preprocessed
Fortran (`.F90`, `.F`, or a lowercase file containing `#ifdef`) to produce the
copy under `native/_expanded/`.

```yaml
fortran:
  defines:
    - DOUBLE_PRECISION=1
```

Same reason `clang.defines` exists: the branches differ, and only the build
knows which one is live.

## `api`

How the generated HTTP API authenticates callers.

```yaml
api:
  auth: api_key      # or: none (default)
```

Only the *mode* lives here. **Keys never do** — `nativegate.yaml` is committed,
and a credential in a committed file is a credential in every clone and every
image layer. The generated middleware reads keys from `NATIVEGATE_API_KEYS` (a
comma-separated list) at startup and refuses to start if the mode requires
them and none are present, so a service cannot come up silently unauthenticated.

The mode is baked in at generate time rather than read from the environment,
so a service generated to require auth cannot be downgraded by wherever it
happens to start. `/healthz` and `/readyz` are always unauthenticated — a
liveness probe that 401s is a service that looks dead and gets restarted
forever.

An unrecognised mode is an error. `auth: apikey` (a plausible typo) is
rejected rather than falling back to `none`, which is exactly the silently
open service this setting exists to prevent.

## Environment variables

Read at generate time or by the generated service. None of these belong in
`nativegate.yaml`.

| Variable | Read by | Effect |
|---|---|---|
| `NATIVEGATE_CPP_PARSER` | generator | `auto`/`clang`/`regex`; overrides `parser:` |
| `NATIVEGATE_FORTRAN_PARSER` | generator | `auto`/`fparser2`/`regex` |
| `NATIVEGATE_LIBCLANG` | generator | path to a libclang the loader cannot find itself |
| `NATIVEGATE_API_KEYS` | service | comma-separated valid API keys; required when `api.auth: api_key` |
| `NATIVEGATE_MAX_ARRAY_ITEMS` | service | cap on any array argument's length (default 65536) |
| `NATIVEGATE_MAX_REQUEST_BYTES` | service | request body size cap |
| `NATIVEGATE_RATE_LIMIT_PER_MINUTE` | service | per-client request rate cap |
| `NATIVEGATE_DEBUG_ERRORS` | service | include exception detail in error responses — never set in production |

`NATIVEGATE_MAX_ARRAY_ITEMS` is a **memory guard, not a bounds check**. The IR
records that a parameter is an array, not the extent the routine declares, so
one configurable cap stands in for all of them. Raise it for a service with
genuinely large inputs; do not remove it. Real per-argument extents are
pending work in the fparser2 front end.

The header's own directory is always on the include path, so sibling headers
need no configuration. Note this is a *separate* key from the top-level
`include_paths`, which controls what CMake and the Fortran INCLUDE expander
see — the parser and the compiler can legitimately need different sets, and
conflating them would make a parse-only path change the build.

If clang cannot open an include, nativegate reports the compiler error and
binds nothing from that header — see
[Exposing C++](cpp-guide.md#telling-the-parser-what-your-build-knows).
