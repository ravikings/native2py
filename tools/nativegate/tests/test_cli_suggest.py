"""`ngate suggest` output must be directly copy-pasteable.

Regression for the table stripping the root the user pointed it at: rows used
to print paths relative to PATH (e.g. "modern/petro_api.f90"), which only
works if you happen to already be sitting in that root. The "Start with ..."
line below the table was never stripped, so the two disagreed. Rows must
match it - printed relative to the cwd, root included, exactly like the
runnable command underneath them.
"""

from click.testing import CliRunner

from nativegate.cli import main

FORTRAN_SOURCE = """module widget
contains
  subroutine square(x, y)
    real :: x, y
    y = x * x
  end subroutine square
end module widget
"""


def test_suggest_table_rows_include_the_root_path(tmp_path):
    nested = tmp_path / "fortran" / "modern"
    nested.mkdir(parents=True)
    source = nested / "petro_api.f90"
    source.write_text(FORTRAN_SOURCE)

    # Rich truncates cells to the terminal width, and CliRunner has no real
    # terminal - force it wide enough that the (long) tmp_path isn't elided.
    result = CliRunner(env={"COLUMNS": "400"}).invoke(main, ["suggest", str(tmp_path)])

    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", "")
    table_part, _, rest = flat.partition("Start with")

    # The table row itself must carry the full path, not one stripped of the
    # root the user passed in - it must match the "Start with" command below.
    assert str(source) in table_part
    assert f"ngate quickstart {source}" in rest
