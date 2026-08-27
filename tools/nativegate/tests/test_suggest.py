"""Discovery of implementation files, and ranking of candidate headers.

The split include/ + src/ layout is the case worth pinning down: it is what
every conventional C++ project uses, and getting it wrong produces a service
that builds cleanly and then fails on first import with an undefined symbol —
the most expensive failure this tool can hand someone.
"""

from pathlib import Path

from nativegate.discovery import find_implementation_files
from nativegate.suggest import POOR, READY, WORKABLE, analyse_tree

HEADER = """#pragma once
class Widget {
public:
    double square(double x);
};
"""

IMPL = """#include "Widget.hpp"
double Widget::square(double x) { return x * x; }
"""


def _split_layout(root: Path, name: str = "Widget") -> Path:
    """include/<name>.hpp + src/<name>.cpp, the conventional layout."""
    (root / "include").mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    header = root / "include" / f"{name}.hpp"
    header.write_text(HEADER.replace("Widget", name))
    (root / "src" / f"{name}.cpp").write_text(IMPL.replace("Widget", name))
    return header


def test_finds_impl_beside_header(tmp_path):
    header = tmp_path / "Widget.hpp"
    header.write_text(HEADER)
    impl = tmp_path / "Widget.cpp"
    impl.write_text(IMPL)

    assert find_implementation_files(header) == [impl]


def test_finds_impl_in_sibling_src_dir(tmp_path):
    header = _split_layout(tmp_path)
    assert find_implementation_files(header) == [tmp_path / "src" / "Widget.cpp"]


def test_finds_impl_in_nested_src_subdirectory(tmp_path):
    """src/ is often subdivided to mirror include/."""
    (tmp_path / "include").mkdir()
    (tmp_path / "src" / "geometry").mkdir(parents=True)
    header = tmp_path / "include" / "Widget.hpp"
    header.write_text(HEADER)
    impl = tmp_path / "src" / "geometry" / "Widget.cpp"
    impl.write_text(IMPL)

    assert find_implementation_files(header) == [impl]


def test_no_impl_for_header_only_library(tmp_path):
    header = tmp_path / "Widget.hpp"
    header.write_text(HEADER)
    assert find_implementation_files(header) == []


def test_unrelated_src_file_is_not_matched(tmp_path):
    """Matching is by stem — a src/ full of other files must not produce hits."""
    header = _split_layout(tmp_path)
    (tmp_path / "src" / "Unrelated.cpp").write_text("int f() { return 0; }")

    found = find_implementation_files(header)
    assert [p.name for p in found] == ["Widget.cpp"]


def test_suggest_ranks_self_contained_header_first(tmp_path):
    """A header needing another subsystem loses to a standalone one.

    Depending on another local header costs the whole of that header's build;
    it must outrank a smaller signal like one skipped method.
    """
    _split_layout(tmp_path, "Standalone")

    # Binds fine, but drags in Standalone.hpp.
    (tmp_path / "include" / "Coupled.hpp").write_text(
        '#include "Standalone.hpp"\n'
        "class Coupled {\npublic:\n    double scale(double x);\n};\n"
    )
    (tmp_path / "src" / "Coupled.cpp").write_text(
        '#include "Coupled.hpp"\ndouble Coupled::scale(double x) { return x; }\n'
    )

    ranked = analyse_tree(tmp_path)
    by_name = {c.path.name: c for c in ranked}

    assert ranked[0].path.name == "Standalone.hpp"
    assert by_name["Standalone.hpp"].verdict == READY
    assert by_name["Coupled.hpp"].local_includes == ["Standalone.hpp"]
    # Depending on another header is not "turnkey", even with nothing skipped.
    assert by_name["Coupled.hpp"].verdict == WORKABLE


def test_suggest_reports_skipped_declarations(tmp_path):
    """An unbindable declaration must be counted, not hidden.

    `fill(double* values, int n)` no longer qualifies — a pointer paired with
    a length argument binds as a numpy buffer now — so the probe uses a
    pointer with no length anywhere, which stays genuinely refusable.
    """
    (tmp_path / "Arrays.hpp").write_text(
        "class Arrays {\npublic:\n"
        "    double total(double x);\n"
        "    double mean(double x);\n"
        "    double peak(double x);\n"
        "    void fill(double* values);\n"
        "};\n"
    )

    (candidate,) = analyse_tree(tmp_path)
    assert candidate.skipped == 1
    assert candidate.methods == 3
    assert candidate.verdict == WORKABLE  # binds more than it skips


def test_suggest_calls_a_header_poor_when_half_of_it_is_unbindable(tmp_path):
    """Binding one method and dropping the other is not a usable service."""
    (tmp_path / "Halved.hpp").write_text(
        "class Halved {\npublic:\n"
        "    double total(double x);\n"
        "    void fill(double* values);\n"
        "};\n"
    )

    (candidate,) = analyse_tree(tmp_path)
    assert (candidate.bound, candidate.skipped) == (1, 1)
    assert candidate.verdict == POOR


def test_suggest_marks_header_that_binds_nothing_as_poor(tmp_path):
    (tmp_path / "Empty.hpp").write_text("#pragma once\n// nothing to bind\n")

    (candidate,) = analyse_tree(tmp_path)
    assert candidate.verdict == POOR
    assert candidate.bound == 0


def test_suggest_ignores_cpp_when_headers_exist(tmp_path):
    """You point nativegate at an interface, not at an implementation file."""
    _split_layout(tmp_path)

    names = {c.path.name for c in analyse_tree(tmp_path)}
    assert names == {"Widget.hpp"}


def test_suggest_notes_missing_implementation(tmp_path):
    (tmp_path / "Widget.hpp").write_text(HEADER)

    (candidate,) = analyse_tree(tmp_path)
    assert candidate.impl_files == []
    assert "no .cpp found" in candidate.notes()


def test_suggest_fortran_notes_do_not_mention_cpp(tmp_path):
    """Fortran routines bind straight from the .f90 - no .cpp pairing exists,
    so the C++-only "no .cpp found" hint must not leak into their notes."""
    (tmp_path / "petro_api.f90").write_text(
        "module petro_api\ncontains\n"
        "  subroutine compute(x, y)\n"
        "    real :: x, y\n"
        "    y = x * 2\n"
        "  end subroutine compute\n"
        "end module petro_api\n"
    )

    (candidate,) = analyse_tree(tmp_path)
    assert candidate.language == "fortran"
    assert candidate.functions == 1
    assert candidate.impl_files == []
    assert "no .cpp found" not in candidate.notes()
