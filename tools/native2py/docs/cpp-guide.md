# Exposing C++

## What the parser supports

native2py parses C++ with **Clang** — the same front end that compiles the
code — through libclang. The preprocessor runs, so `#include`s, `#define`s and
`#ifdef` branches are resolved before anything is bound, and the AST answers
questions about templates, typedefs, overloads, access and completeness
instead of native2py guessing them from token shapes.

Install it with the `clang` extra (already in `requirements.txt`):

```bash
pip install "native2py[clang]"
```

**Handled:**

- multiple classes and structs per header, and multiple headers per service
- `#include`d types, macro-generated declarations (the `F77_NAME(...)` bridge
  pattern), and `#ifdef`-guarded code — with the macros your build defines,
  via `clang.defines` in `native2py.yaml`
- `typedef` and `using` aliases, resolved to their canonical type
  (`typedef double Pressure;` → `float`)
- anonymous `typedef struct { ... } Point;` — bound under the typedef name
- access specifiers wherever they appear; private members are never bound
- classes default to private members, structs to public, as in C++
- constructors with arguments; `= default` / `= delete`; copy and move
  constructors are recognised and not bound as `py::init`
- abstract classes — never given a `py::init<>` that would not compile
- overloads (same name, different signature)
- `const` / reference qualifiers stripped: `const double&` → `float`
- `const char*` and `std::string` (in any spelling) → `str`
- **a raw `T*` paired with a length argument** — `double sum(const double*
  data, int n)`, `void scale(double* data, int n, double k)`. The pairing is
  inferred from the names (`n`, `n_data`, `data_len`, ...) and the length then
  **disappears from the Python signature**, because it is read off the array
  the caller actually passed. A length the caller cannot state is a length
  that cannot disagree with the data
- **operator overloading**, bound as Python special methods: `operator+` →
  `__add__`, `operator[]` → `__getitem__`, `operator==` → `__eq__`, and so on.
  The mapping is keyed on argument count, so a unary `operator-` becomes
  `__neg__` and the binary one `__sub__`
- **`std::vector<T>`** as a parameter (by value or `const&`) and as a return,
  for numeric elements and `std::string` — converted to and from a Python
  `list` by `<pybind11/stl.h>`. The generated endpoint annotates it
  `list[float]` / `list[int]` and applies the `MAX_ARRAY_ITEMS` cap, so an
  unbounded request body cannot exhaust the worker
- enums, scoped or not, as `int`
- public single and multiple inheritance — emitted as
  `py::class_<Derived, Base>` so inherited methods are callable from Python
  and `isinstance(derived, Base)` holds; bases are always registered before
  the classes deriving from them
- structs as method return types, in either declaration order
- pointers to classes defined in the service, as *parameters*
- nested namespaces, `extern "C"` blocks, inline bodies, comments

**Not handled** — these are *skipped with a reason*, not silently dropped:

- templates: `template <typename T> class Buffer` and
  `template <typename T> T clamp(...)` have no single instantiation to bind.
  Add a non-template wrapper (or a typedef for the instantiation) and expose
  that.
- **non-const `std::vector<T>&`** — refused deliberately, and this is the one
  worth understanding. pybind11 converts the caller's list into a *temporary*
  vector, so a function that writes through the reference writes into the
  temporary and the temporary dies on return. Measured against a real
  pybind11 module:

  ```python
  >>> data = [1.0, 2.0, 3.0]
  >>> probe.scale_in_place(data, 10.0)   # void f(std::vector<double>&, double)
  >>> data
  [1.0, 2.0, 3.0]                        # writes discarded — no error at all
  ```

  It compiles, it runs, it returns unmodified data. Take
  `const std::vector<T>&` if the argument is input, or **return** the modified
  vector
- nested containers (`std::vector<std::vector<T>>`) and vectors of records —
  pybind11 would convert them, but the IR has no shape for a list of lists so
  the generated Pydantic model would be wrong
- other STL containers with no `ir.TYPE_MAP` entry (`std::map`, `std::array`,
  `std::span`)
- private and protected inheritance: the `Derived*` -> `Base*` conversion is
  inaccessible, so pybind11 cannot be told about it. The derived class is
  still bound, with its own members only.
- inheriting from a class the service does not bind: reported, and the
  relationship dropped — expose the base too if Python needs its methods
- operator overloading — **now supported**, see below
- raw numeric pointers whose length **nothing in the signature names**. See
  below — a pointer paired with a length argument *is* now bindable
- pointer *returns* of class types — ownership ambiguity
- types that are only forward-declared and never defined in the service

The last four are pybind11 constraints, not parser limits: they stay refused
no matter how good the parser gets.

### Array arguments: raw pointers

A C numerical API passes arrays as a pointer plus a count:

```cpp
double sum(const double* data, int n);
void   scale(double* data, int n, double k);   // writes in place
```

Both bind. The pointer becomes a numpy array and **the length argument is not
part of the Python signature** — it is read off the array you passed:

```python
>>> arr.sum([1.0, 2.0, 3.0])
6.0
>>> import numpy as np
>>> d = np.array([1.0, 2.0, 3.0])
>>> arr.scale(d, 10.0)      # no `n`
>>> d
array([10., 20., 30.])      # written in place
```

Dropping the length is the point: a count the caller cannot state is a count
that cannot disagree with the data, which is the whole failure mode of a C
array API.

#### When the pairing is refused

The length is inferred from argument names — `n`, `n_values`, `values_len`,
`count`, `size` and similar, matched against the array's name. Where that is
ambiguous the function is **skipped with a reason** rather than guessed:

```cpp
double dot(const double* a, const double* b, int n);   // refused
double first(const double* data);                      // refused
```

`dot` has one integer and two arrays. A human reads `n` as the length of both;
the inference will not, because a false pairing binds the wrong argument as an
extent and reads past the end of a buffer. Give the arguments matching names
(`n_a`, `n_b`), take `std::vector`, or expose a wrapper.

Buffer arguments on **class methods** are also refused for now — the generated
lambda that reads the length off the array is emitted for free functions only.

#### Read-only and writable buffers are bound differently

This is not a style choice, and it is worth knowing before you debug a
"why did my array not change" report.

A **`const T*`** is input. pybind11 may convert a list or a wrong-dtype array
into a temporary, because nothing is written back — so these endpoints accept
plain Python lists.

A **`T*`** is one the native code writes through. Conversion there is
catastrophic *and silent*: pybind11 writes into the temporary and discards it.
Measured, with a real module — `py::array_t<double>` handed an `int64` array
accepted it, converted, and lost every write; and a plain `request()` wrote
straight through an array with `writeable=False`. So a writable buffer refuses
rather than converts:

```python
>>> arr.scale(np.array([1, 2, 3]), 10.0)          # int64
TypeError: scale(): 'data' must be a numpy array of dtype double; native2py
will not convert it because this function writes through the buffer and a
converted array is a temporary whose writes would be discarded.

>>> ro = np.array([1.0, 2.0]); ro.flags.writeable = False
>>> arr.scale(ro, 10.0)
ValueError: buffer source array is read-only
```

Over HTTP the same rule applies from the other side: an endpoint whose
argument is a writable buffer **returns it in the response**, because the
array it wrote into does not outlive the request.

```
POST /scale  {"data": [1.0, 2.0, 3.0], "k": 10.0}
      ->     {"data": [10.0, 20.0, 30.0]}
```

#### It costs a numpy dependency

`pybind11/numpy.h` needs numpy's headers to build and numpy to import, so a
C++ service that binds a raw pointer needs `numpy` in its `pyproject.toml` —
in `[build-system] requires` and `[project] dependencies`. `generate` warns
when a binding needs it and the file does not declare it; it does not edit
`pyproject.toml`, which is yours to hand-edit. A C++ service that binds no
pointers does not acquire the dependency.

### Operators

Operators bind as Python special methods, so a C++ value type behaves like one
in Python:

```cpp
class Vec2 {
public:
    Vec2 operator+(const Vec2& o) const;
    Vec2 operator*(double s) const;
    bool operator==(const Vec2& o) const;
    double operator[](int i) const;
};
```

```python
>>> (Vec2(1, 2) + Vec2(10, 20)).x()
11.0
>>> Vec2(1, 2)[1]
2.0
>>> Vec2(1, 2) == Vec2(1, 2)
True
```

Arithmetic (`+ - * / %`), comparison (`== != < <= > >=`), `operator[]`,
`operator()` and unary `+`/`-` are mapped. The mapping is keyed on **argument
count**, because a member `operator-` with one argument is subtraction and with
none it is negation — binding one as the other compiles and then computes
something else.

Not mapped, and reported with the reason rather than dropped:

| | Why |
|---|---|
| `operator=` | assignment has no Python equivalent — Python rebinds names, it does not assign through them |
| `operator+=` and friends | Python's `__iadd__` must return the mutated object, and a C++ `T&` return needs a return-value policy native2py cannot infer |
| `operator++`, `operator--` | Python has no increment or decrement operator |
| `operator->`, `new`, `delete` | not things Python may call |

**Destructors are not a gap.** pybind11 destroys the held object itself, so
there is nothing for a binding to do; they are ignored rather than reported,
because reporting them would imply a missing capability.

**The regex fallback reader cannot bind operators.** Its declarator pattern
matches `~?\w+` for a name, which cannot match the `+` in `operator+`. It
reports them as skipped, naming each one and pointing at
`pip install "native2py[clang]"`. This is one of the few places the two C++
backends genuinely differ, and it differs safely — the fallback binds strictly
less.

### Telling the parser what your build knows

A compiler front end needs what a compiler is given. If your headers include
files from elsewhere, or depend on macros the build defines, say so in
`native2py.yaml`:

```yaml
name: sim
language: cpp
clang:
  std: c++20                       # default: c++17
  include_paths:                   # extra -I directories
    - libraries/common-cpp/include
  defines:                         # -D flags
    - LEGACY_API=1
  extra_args:                      # anything else clang should see
    - -Wno-deprecated
```

If a header cannot be parsed — a missing include, usually — native2py says so
and binds **nothing** from that file:

```
$ native2py generate sim
Parsing C++ with clang AST (libclang 18.1.1).
  Simulator.hpp: compiler error: Simulator.hpp:4: 'petro/grid.hpp' file not found
  Simulator.hpp: skipped 1 declaration(s):
    - Simulator.hpp: the compiler rejected this declaration ('petro/grid.hpp'
      file not found); it cannot be bound until it compiles. ...
```

That is deliberate. Clang recovers from an unknown type by pretending it was
`int`, so a header with an unreachable include still produces a plausible AST —
and bindings whose signatures quietly disagree with the C++ they call. Fix the
include path rather than shipping that.

### The fallback reader

Without libclang, native2py falls back to `cpp_regex.py`, the original
token/brace reader: no preprocessor, no templates, no typedef resolution, and
one namespace per file. It is chosen automatically, and named in the output so
you always know which parser ran:

```
$ native2py inspect native/Simulator.hpp
parser: clang AST (libclang 18.1.1)
$ native2py inspect --parser regex native/Simulator.hpp
parser: regex reader (selected explicitly)
```

Pick a backend explicitly with `parser: clang | regex | auto` in
`native2py.yaml`, `--parser` on `inspect`, or `$NATIVE2PY_CPP_PARSER`.
`parser: clang` makes a missing libclang an error instead of a silent
downgrade — worth setting in CI, where a quietly-degraded parse would drop
every macro-defined symbol from the build. If libclang is installed but its
shared library cannot be found, point `NATIVE2PY_LIBCLANG` at it.

## Nothing is dropped silently

Every declaration native2py recognises but cannot bind is reported at
`generate` time with the reason:

```
$ native2py generate sim
  - Simulator::set_permeability_z: 'double*' is a pointer; native2py cannot
    infer its length. Wrap it in a type that carries its own size
    (std::vector, a span, or a struct) or exclude the symbol in native2py.yaml.
  - Simulator::add_well: 'WellModel*' refers to 'WellModel', which is only
    forward-declared (an incomplete type). pybind11 needs the full definition
    to bind it. Add the header that defines 'WellModel' to this service's
    native/ directory, or exclude the symbol in native2py.yaml.
Generated bindings, CMake, Python package, and tests for 'sim'.
```

A bad signature costs you that one method, not the class it lives in and
not the rest of the header.

## Two ways to expose a symbol

**1. Config-driven** (works on code you can't or don't want to annotate):

```yaml
# native2py.yaml
name: calculator
language: cpp

expose:
  classes:
    - Calculator
  functions:
    - some_free_function
```

**2. Source annotation:**

```cpp
[[native2py::expose]]
class Calculator {
public:
    [[native2py::expose]]
    double add(double a, double b);
};
```

If `expose:` is empty, everything found is exposed. As soon as you list even
one name, only listed names are exposed — config becomes a filter once used.

## Type mapping

Parameter and return types map deterministically (`native2py/ir.py::TYPE_MAP`):

| C++ | Python |
|-----|--------|
| `int`, `int32_t`, `int64_t` | `int` |
| `float`, `double` | `float` |
| `bool` | `bool` |
| `std::string`, `const char*` | `str` |
| `void` | `None` |
| a struct/class defined in the service | itself |

Anything else raises `NativeTypeError` and the symbol is skipped with that
message. That's deliberate: a guessed binding compiles, imports, and returns
wrong numbers, which is far worse than a clear refusal.

## Multiple headers

A service's headers all compile into **one** extension. `foo.hpp` and
`bar.hpp` in the same `native/` directory produce a single
`<service>_bindings.cpp` with one `PYBIND11_MODULE`, and both classes land
in the same Python package.

This is forced by the platform, not a design preference: a Python extension
carries exactly one module-init symbol, so one bindings file per header
would leave all but one orphaned.

Two consequences:

- **Symbol names must be unique across a service's headers.** Two classes
  called `Result` in different headers is an error at `generate` time, not a
  confusing link failure later.
- **A type forward-declared in one header and defined in another is fine.**
  Since they compile together, it's complete by the time pybind11 sees it,
  so it isn't skipped.

```
$ native2py generate multitest
Merged 2 headers: engine.hpp, vec3.hpp
```

## Forward declarations are incomplete types

```cpp
class FluidModel;              // forward declaration — incomplete

class Simulator {
public:
    void set_fluid(FluidModel* fluid);
};
```

pybind11 needs the *full definition* to build a type caster. Binding
`set_fluid` here fails at compile time with a wall of template errors
pointing into pybind11's internals:

```
error: incomplete type 'FluidModel' used in type trait expression
error: 'typeid' of incomplete type 'FluidModel'
```

native2py skips such symbols instead, naming the actual cause. To bind them,
add the header that *defines* the type to the same service's `native/`
directory — then it's complete and the method comes through.

## Headers outside the service

To `#include` a header that doesn't live in `native/`, add its directory:

```yaml
# native2py.yaml
name: extdep
language: cpp
include_paths:
  - libraries/extheaders
```

Entries are repo-root-relative and become `target_include_directories`
entries in the generated CMake. A path that doesn't exist is an error at
`generate` time, not a compiler failure later.

Note the scope: this makes the header available **to the compiler**.
native2py still only *parses* headers inside `native/`, so classes defined
in an external header won't get Python bindings of their own — your code can
use them internally, but they can't cross the binding boundary unless their
types are in `TYPE_MAP`.

## Declarations need definitions

A header full of declarations with no matching `.cpp` links "successfully"
on macOS — undefined symbols in a module bundle are resolved lazily at
`dlopen` — and then fails on first import:

```
ImportError: symbol not found in flat namespace '__ZN9Simulator10advance_toEd'
```

`generate` warns when a service has bindable methods but no
`.cpp`/`.cc`/`.cxx` files at all:

```
WARNING: no .cpp/.cc/.cxx implementation files in services/sim/native, but the
headers declare methods without inline bodies. `native2py build` will succeed
and then fail on first import with an undefined-symbol error.
```

`native2py quickstart` also picks up a same-stem `.cpp` sitting next to the
header you point it at.

## What `generate` produces

For a service at `services/<name>/`:

```
bindings/generated/<name>_bindings.cpp   one pybind11 PYBIND11_MODULE for the service
CMakeLists.txt                           pybind11_add_module(<name>_cpp ...)
python/<name>/__init__.py                re-exports from ._native.<name>_cpp
python/<name>/router.py                  APIRouter with one POST per method
python/<name>/service.py                 standalone FastAPI app + /healthz
tests/test_python_api.py                 import + call smoke test
```

Everything under `bindings/generated/` is rewritten on every `generate`,
including clearing stale files from previous runs. Don't edit them.

The compiled extension is `<name>_cpp` (e.g. `fluid_cpp`), distinct from the
Python package name so `__init__.py` can re-export without a name clash —
callers never see the `_cpp` suffix.

## Linking shared native libraries

To share C++ across services, put it in `libraries/<name>/` with its own
`CMakeLists.txt` and declare it:

```yaml
libraries:
  - common-cpp
```

`generate` emits the `add_subdirectory` + `target_link_libraries` wiring.
Full setup, the `STATIC`/`POSITION_INDEPENDENT_CODE` requirements, and the
Docker build-context change are in
[Deployment topologies](deployment-topologies.md#sharing-native-code-librariescommon-cpp).

## Verified against real builds

Both the multi-header case and the shared-library case have been compiled
with real `cmake` + `clang++` + `pybind11`, imported, and called — not just
unit-tested:

```
Vec3.norm(3,4,0)       = 5.0
Engine.distance(3,4,0) = 5.0     # Engine constructs a Vec3 from the other header
User.compute(4.0)      = 40.0    # via a header outside native/, per include_paths
```

See [Architecture](architecture.md#verified-not-just-tested).
