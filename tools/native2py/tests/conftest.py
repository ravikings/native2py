"""Shared fixtures.

Tests own their fixture source rather than reading from services/ or
libraries/. Those directories hold real user work — services get renamed,
replaced, and deleted — and a test suite that breaks when someone
reorganizes their own project is testing the wrong thing.

The exception is tests/test_fixed_form.py, which deliberately parses
libraries/petro because the point of those tests is "does this work against
real legacy F77". Those skip cleanly if the library isn't present.
"""

import pytest

from native2py.parsers import fortran_fparser

# fparser2 is an OPTIONAL extra, so a test that needs it must SKIP when it is
# absent, not fail. Ten tests across four modules used to fail on an install
# without the extra — which reads as a regression and buries any real one.
#
# The inverse rule still holds and is the reason this is a shared marker
# rather than a try/except in each module: a skip is not a pass. CI installs
# the extra, so these run there; a bare `pip install -e .` skips them and says
# so, naming the reason.
requires_fparser = pytest.mark.skipif(
    not fortran_fparser.is_available(),
    reason=f"fparser is not installed: {fortran_fparser.unavailable_reason()}",
)

CALCULATOR_HPP = """#pragma once

namespace math {

class Calculator {
public:
    double add(double a, double b);
    double multiply(double a, double b);
};

}  // namespace math
"""

CALCULATOR_CPP = """#include "calculator.hpp"

namespace math {

double Calculator::add(double a, double b) { return a + b; }
double Calculator::multiply(double a, double b) { return a * b; }

}  // namespace math
"""


@pytest.fixture
def calculator_header(tmp_path):
    """A self-contained calculator.hpp, written fresh for each test."""
    header = tmp_path / "calculator.hpp"
    header.write_text(CALCULATOR_HPP)
    (tmp_path / "calculator.cpp").write_text(CALCULATOR_CPP)
    return header


# The routines here are load-bearing for tests/test_fortran_pipeline.py:
#   - the `module physics` wrapper drives the f2py nesting fix (routines
#     compile to <ext>.physics.<name>, not <ext>.<name>);
#   - `calculate_pressure` is a function with a result variable;
#   - `normalize` is a subroutine taking an assumed-size array plus its
#     length, which is what makes the generated smoke test numpy-aware.
# The filename stem matters too: module.name follows it, so the f2py
# extension is `-m pressure`.
PRESSURE_F90 = """module physics
    implicit none
contains

    function calculate_pressure(density, temperature) result(pressure)
        real(8), intent(in) :: density
        real(8), intent(in) :: temperature
        real(8) :: pressure

        pressure = density * temperature * 8.314d0
    end function calculate_pressure

    subroutine normalize(values, n)
        integer, intent(in) :: n
        real(8), intent(inout) :: values(n)
        real(8) :: total
        integer :: i

        total = 0.0d0
        do i = 1, n
            total = total + values(i)
        end do

        if (total /= 0.0d0) then
            do i = 1, n
                values(i) = values(i) / total
            end do
        end if
    end subroutine normalize

end module physics
"""


@pytest.fixture
def reservoir_source(tmp_path):
    """A module-wrapped pressure.f90, written fresh for each test."""
    source = tmp_path / "pressure.f90"
    source.write_text(PRESSURE_F90)
    return source
