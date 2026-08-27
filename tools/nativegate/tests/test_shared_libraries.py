from pathlib import Path

import pytest
import yaml

from nativegate.config import ExposeConfig, ServiceConfig
from nativegate.generators import cmake_gen, docker_gen
from nativegate.parsers import cpp as cpp_parser



def _module(header):
    return cpp_parser.parse_header(header, ExposeConfig(classes=["Calculator"]))


def test_config_roundtrips_libraries(tmp_path):
    config = ServiceConfig(name="demo", language="cpp", libraries=["common-cpp"])
    config.save(tmp_path)

    assert yaml.safe_load((tmp_path / "nativegate.yaml").read_text())["libraries"] == ["common-cpp"]
    assert ServiceConfig.load(tmp_path).libraries == ["common-cpp"]


def test_config_without_libraries_omits_the_key(tmp_path):
    ServiceConfig(name="demo", language="cpp").save(tmp_path)

    assert "libraries" not in yaml.safe_load((tmp_path / "nativegate.yaml").read_text())
    assert ServiceConfig.load(tmp_path).libraries == []


def test_cmake_links_shared_library(calculator_header):
    cmake = cmake_gen.generate_cmake(
        _module(calculator_header), "calculator", ["native/calculator.cpp"], libraries=["common-cpp"]
    )

    # Directory name keeps the hyphen; the CMake target must not.
    assert "libraries/common-cpp" in cmake
    assert "target_link_libraries(calculator_cpp PRIVATE common_cpp)" in cmake
    # Out-of-tree source needs an explicit binary dir, or CMake errors.
    assert "${CMAKE_CURRENT_BINARY_DIR}/_libraries/common-cpp" in cmake


def test_cmake_without_libraries_is_unchanged(calculator_header):
    cmake = cmake_gen.generate_cmake(_module(calculator_header), "calculator", ["native/calculator.cpp"])

    assert "add_subdirectory" not in cmake
    assert "target_link_libraries" not in cmake


def test_dockerfile_uses_repo_root_context_when_libraries_present():
    # Shared libraries live outside the service dir, so COPY can only reach
    # them if the build context is the repo root — and the paths must then
    # be repo-relative, not service-relative.
    dockerfile = docker_gen.generate_dockerfile(
        "demo", "cpp", "demo", libraries=["common-cpp"]
    )

    assert "COPY libraries/common-cpp ./libraries/common-cpp" in dockerfile
    assert "COPY services/demo ./services/demo" in dockerfile
    assert "docker build -f services/demo/Dockerfile" in dockerfile
    assert "\nCOPY . .\n" not in dockerfile


def test_dockerfile_keeps_simple_context_without_libraries():
    dockerfile = docker_gen.generate_dockerfile("demo", "cpp", "demo")

    assert "COPY . ." in dockerfile
    assert "COPY libraries/" not in dockerfile


def test_missing_library_raises_before_cmake_runs(tmp_path, monkeypatch):
    import click

    from nativegate import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "libraries").mkdir()

    config = ServiceConfig(name="demo", language="cpp", libraries=["does-not-exist"])
    with pytest.raises(click.ClickException, match="does not exist"):
        cli._validated_libraries(config)


def test_library_without_cmakelists_raises(tmp_path, monkeypatch):
    import click

    from nativegate import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "libraries" / "broken-lib").mkdir(parents=True)

    config = ServiceConfig(name="demo", language="cpp", libraries=["broken-lib"])
    with pytest.raises(click.ClickException, match="no CMakeLists.txt"):
        cli._validated_libraries(config)
