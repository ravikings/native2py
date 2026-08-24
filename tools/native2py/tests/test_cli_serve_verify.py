"""`native2py serve` and `native2py verify` must fail with instructions.

Both run against the *installed* wheel, which is the failure people hit
first: `serve` used to be `uvicorn <name>.service:app` typed by hand, and a
missing install surfaced as an ImportError traceback from inside uvicorn.
The command has to say "build and install the service" instead - and it has
to check the service exists before it checks anything else, so a typo in the
name doesn't get reported as a packaging problem.
"""

from pathlib import Path

from click.testing import CliRunner

from native2py.cli import main


def _service(root, name="unbuilt_demo"):
    directory = root / "services" / name
    directory.mkdir(parents=True)
    return directory


def test_serve_reports_a_missing_service_by_name(tmp_path):
    with CliRunner().isolated_filesystem(temp_dir=tmp_path):
        result = CliRunner().invoke(main, ["serve", "nosuch"])
    assert result.exit_code != 0
    assert "No service 'nosuch'" in result.output


def test_serve_explains_how_to_install_an_unbuilt_service(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        _service(Path(cwd))
        result = runner.invoke(main, ["serve", "unbuilt_demo"])
    assert result.exit_code != 0
    assert "native2py build unbuilt_demo" in result.output
    assert "dist/*.whl" in result.output


def test_verify_needs_a_recorded_golden_file(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        _service(Path(cwd))
        result = runner.invoke(main, ["verify", "unbuilt_demo"])
    assert result.exit_code != 0
    assert "native2py golden record unbuilt_demo" in result.output


def test_verify_is_the_same_check_as_golden_verify(tmp_path):
    """Both spellings must reach one implementation, not two that can drift."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        _service(Path(cwd))
        short = runner.invoke(main, ["verify", "unbuilt_demo"])
        long = runner.invoke(main, ["golden", "verify", "unbuilt_demo"])
    assert short.output == long.output
