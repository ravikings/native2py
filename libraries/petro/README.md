# libraries/petro — legacy reservoir engineering library

A deliberately period-accurate stand-in for a 30+ year old oilfield codebase
(1988–2004 vintage), written to stress-test `ngate` against the kind of
code it is actually meant to modernize. ~4,800 lines, C++ and Fortran,
heavily interlinked.

Everything here **compiles and runs**. It is not a mock-up: `cpp/examples/smoke.cpp`
links the whole stack and produces physically sensible black-oil numbers.

## Layout

```
fortran/include/PETRO.INC   global fluid COMMON blocks, IMPLICIT typing
fortran/include/GRID.INC    grid + solution arrays, MXCELL = 40000
fortran/pvtcor.f            black oil PVT correlations (Standing / Vazquez-Beggs / Glaso)
fortran/relperm.f           Corey + Stone I/II rel-perm, SWOF table lookup
fortran/matsol.f            Thomas, line-SOR, banded Gauss
fortran/hydrau.f            Beggs & Brill multiphase wellbore hydraulics
fortran/flash.f             Peng-Robinson two-phase flash, Rachford-Rice
fortran/simcor.f            IMPES black-oil simulator core
fortran/wellib.f            Peaceman WI, Vogel IPR, nodal analysis
fortran/modern/petro_api.f90  2003-era F90 facade (the only modern-style file)

cpp/include/FortranBridge.hpp  extern "C" declarations + name-mangling macros
cpp/include/Units.hpp          field/metric/lab conversion
cpp/include/FluidModel.hpp     OO wrapper over pvtcor.f, with a PVT table cache
cpp/include/WellModel.hpp      OO wrapper over wellib.f + hydrau.f
cpp/include/DeckReader.hpp     fixed-format keyword deck parser
cpp/include/Simulator.hpp      driver facade over simcor.f

decks/BRAZOS.DATA           sample card-image input deck
```

## The dependency graph is the point

```
        DeckReader ──┐
        Simulator ───┼──> FortranBridge (extern "C") ──┬──> simcor.f ──> matsol.f
        WellModel ───┤                                 ├──> wellib.f ──> hydrau.f
        FluidModel ──┘                                 │                    │
                                                       ├──> relperm.f       │
                                                       └──> pvtcor.f <──────┘
                                                             ▲
                                    petro_api.f90 ───────────┘
```

`pvtcor.f` is called by four of the other six decks. Nothing communicates by
argument list alone — `/FLUID/`, `/PVTOUT/`, `/DIAG/`, `/GRID/` and `/STATE/`
carry state between routines, and the C++ layer reads three of those COMMON
blocks directly through matching structs in `FortranBridge.hpp`.

## Period-accurate hazards deliberately preserved

- Fixed-form F77: column 6 continuation, `GO TO (10,20,30), I`, numbered `DO`
  loops, `BLOCK DATA`, `INCLUDE 'PETRO.INC'`.
- `IMPLICIT DOUBLE PRECISION (A-H,O-Z)` / `IMPLICIT INTEGER (I-N)` — which is
  why `KRWAT`, `KROIL` etc. need explicit declarations at every call site.
- Hidden global state: `CALL PVTSET(P)` then read `/PVTOUT/`. Not thread-safe,
  not re-entrant, and constructing two `FluidModel` objects does *not* give you
  two fluids.
- `CHARACTER*(*)` arguments with the hidden trailing length passed by value.
- Compiler-dependent name mangling handled by an `#ifdef` macro, not a
  configure step.
- Pre-standard C++: no STL, no namespaces, no templates, no exceptions,
  manual `new[]`/`delete[]`, private undefined copy constructors.

## Building

```bash
cmake -S . -B build && cmake --build build
```

or directly:

```bash
gfortran -std=legacy -c -Ifortran/include fortran/*.f
gfortran -c fortran/modern/petro_api.f90
g++ -std=c++98 -c -Icpp/include cpp/src/*.cpp cpp/examples/smoke.cpp
gfortran -o smoke *.o -lstdc++ && ./smoke
```

Expected output:

```
Pb        =    4784.82 psia
Rs(2000)  =     355.08 scf/stb
Bo(2000)  =     1.2377 rb/stb
muo(2000) =     0.7693 cp
Z(2000)   =     0.8821
OIP       =    1346325.6 stb
IPR@2500  =    3375.00 stb/d
TPC@1000  =    2728.48 psia
nodal     =    2432.48 stb/d @  2984.50 psia (iconv=0)
OIP+1d    =    1323196.7 stb
```

See `FINDINGS.md` for what happened when `ngate` was pointed at this.
