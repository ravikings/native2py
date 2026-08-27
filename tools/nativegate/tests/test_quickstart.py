import subprocess
import sys
from pathlib import Path

CALCULATOR_HEADER = Path(__file__).parents[3] / "services" / "calculator" / "native" / "calculator.hpp"


def _run_cli(args, cwd):
    venv_bin = Path(sys.executable).parent
    result = subprocess.run(
        [str(venv_bin / "nativegate"), *args], cwd=cwd, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_quickstart_scaffolds_and_generates_cpp(tmp_path):
    repo_root = tmp_path
    (repo_root / "services").mkdir()

    header = repo_root / "widget.hpp"
    header.write_text(
        """
        class Widget {
        public:
            double square(double x);
        };
        """
    )

    _run_cli(["quickstart", str(header)], cwd=repo_root)

    service_dir = repo_root / "services" / "widget"
    assert (service_dir / "pyproject.toml").exists()
    assert (service_dir / "native" / "widget.hpp").exists()

    init_py = (service_dir / "python" / "widget" / "__init__.py").read_text()
    assert "Widget" in init_py


def test_quickstart_auto_exposes_all_fortran_routines(tmp_path):
    repo_root = tmp_path
    (repo_root / "services").mkdir()

    source = repo_root / "mixer.f90"
    source.write_text(
        """
    function blend(a, b, ratio) result(mixed)
        real(8), intent(in) :: a
        real(8), intent(in) :: b
        real(8), intent(in) :: ratio
        real(8) :: mixed

        mixed = a * ratio + b * (1.0d0 - ratio)
    end function blend
    """
    )

    _run_cli(["quickstart", str(source)], cwd=repo_root)

    service_dir = repo_root / "services" / "mixer"
    config_text = (service_dir / "nativegate.yaml").read_text()
    assert "blend" in config_text

    init_py = (service_dir / "python" / "mixer" / "__init__.py").read_text()
    assert "from ._native.mixer import blend" in init_py


def test_quickstart_copies_sibling_cpp_implementation(tmp_path):
    # A header with declarations only (no inline bodies) needs its matching
    # .cpp to actually compile. Found by building the generated wheel for
    # real: without this, `ngate build` succeeded but the resulting
    # extension failed at import time with an undefined-symbol dlopen error
    # (macOS defers symbol resolution to load time instead of failing at
    # link time). quickstart must pull the sibling .cpp in automatically.
    repo_root = tmp_path
    (repo_root / "services").mkdir()

    header = repo_root / "widget.hpp"
    header.write_text("class Widget { public: double square(double x); };")
    impl = repo_root / "widget.cpp"
    impl.write_text(
        '#include "widget.hpp"\n\ndouble Widget::square(double x) { return x * x; }\n'
    )

    _run_cli(["quickstart", str(header)], cwd=repo_root)

    service_dir = repo_root / "services" / "widget"
    assert (service_dir / "native" / "widget.cpp").exists()
    cmake = (service_dir / "CMakeLists.txt").read_text()
    assert "native/widget.cpp" in cmake


def test_quickstart_warns_when_no_cpp_implementation_found(tmp_path):
    repo_root = tmp_path
    (repo_root / "services").mkdir()

    header = repo_root / "widget.hpp"
    header.write_text("class Widget { public: double square(double x); };")

    output = _run_cli(["quickstart", str(header)], cwd=repo_root)

    assert "WARNING" in output
    assert "no implementation file found" in output


def test_quickstart_name_override(tmp_path):
    repo_root = tmp_path
    (repo_root / "services").mkdir()

    header = repo_root / "widget.hpp"
    header.write_text("class Widget { public: double square(double x); };")

    _run_cli(["quickstart", str(header), "--name", "custom_name"], cwd=repo_root)

    assert (repo_root / "services" / "custom_name").exists()
    assert not (repo_root / "services" / "widget").exists()


def test_generate_restores_deleted_generated_files(tmp_path):
    # Deleting one generated file used to leave `generate` unable to restore
    # pyproject.toml, with `create-service --force` — which deletes native/ —
    # as the only way back.
    repo_root = tmp_path
    (repo_root / "services").mkdir()

    header = repo_root / "widget.hpp"
    header.write_text("class Widget { public: double square(double x); };")
    _run_cli(["quickstart", str(header)], cwd=repo_root)

    service_dir = repo_root / "services" / "widget"
    import shutil

    shutil.rmtree(service_dir / "python")
    shutil.rmtree(service_dir / "bindings")
    (service_dir / "pyproject.toml").unlink()
    (service_dir / "CMakeLists.txt").unlink()

    _run_cli(["generate", "widget"], cwd=repo_root)

    assert (service_dir / "pyproject.toml").exists()
    assert (service_dir / "CMakeLists.txt").exists()
    assert (service_dir / "python" / "widget" / "__init__.py").exists()
    assert (service_dir / "bindings" / "generated" / "widget_bindings.cpp").exists()


def test_generate_does_not_overwrite_an_edited_pyproject(tmp_path):
    # pyproject.toml is generated but hand-editable — a pinned dependency or a
    # version bump must survive a regenerate.
    repo_root = tmp_path
    (repo_root / "services").mkdir()

    header = repo_root / "widget.hpp"
    header.write_text("class Widget { public: double square(double x); };")
    _run_cli(["quickstart", str(header)], cwd=repo_root)

    pyproject = repo_root / "services" / "widget" / "pyproject.toml"
    edited = pyproject.read_text().replace('version = "1.0.0"', 'version = "2.4.1"')
    pyproject.write_text(edited)

    _run_cli(["generate", "widget"], cwd=repo_root)

    assert 'version = "2.4.1"' in pyproject.read_text()


def test_init_is_repeatable_and_touches_nothing_else(tmp_path):
    # `init` is the command people reach for after deleting something. It
    # creates the empty skeleton and nothing more — knowing that is the
    # difference between "re-run init" and "restore from git".
    repo_root = tmp_path
    (repo_root / "services" / "keepme").mkdir(parents=True)
    (repo_root / "services" / "keepme" / "marker.txt").write_text("mine")

    _run_cli(["init"], cwd=repo_root)
    _run_cli(["init"], cwd=repo_root)

    for expected in ("services", "libraries", "tools", "infrastructure/docker"):
        assert (repo_root / expected).is_dir()
    assert (repo_root / "services" / "keepme" / "marker.txt").read_text() == "mine"
    assert not (repo_root / "gateways").exists()
