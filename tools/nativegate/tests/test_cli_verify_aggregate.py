"""T12 -- `ngate verify`'s aggregate ordering and per-layer reporting.

Spec: design-verification-layers.md section 2.6 ("`ngate verify <name>`
... runs `oracle check` if a toolchain is present, then golden, then
invariants if `invariants.json` exists -- and reports each separately.
Which layer failed IS the diagnostic.") and section 5's CI ordering: oracle
-> golden -> invariants.

Each of the three layers is monkeypatched to fail independently -- proving
one layer's failure is named without masking (or being masked by) the
others, per the task's acceptance criteria.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from nativegate import cli
from nativegate import oracle as oracle_lib


def _service(root: Path, name: str = "svc") -> Path:
    directory = root / "services" / name
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def all_layers_pass(monkeypatch):
    """Baseline: every layer reports success, so a single monkeypatched
    failure is unambiguously attributable to that one layer."""
    monkeypatch.setattr(cli, "_toolchain_present", lambda: True)
    monkeypatch.setattr(
        oracle_lib,
        "oracle_check",
        lambda name, service_dir=None, keep_build=False: oracle_lib.OracleReport(
            covered=3, skipped={}, failures=[]
        ),
    )
    monkeypatch.setattr(cli, "_golden_verify", lambda name: click.echo("3 entry point(s) unchanged (0 not covered)."))
    monkeypatch.setattr(cli, "_invariants_verify", lambda name: click.echo("1 function(s) checked, 0 uncovered."))
    # `verify` only runs the invariants layer at all if nativegate.yaml
    # declares something -- patch ServiceConfig.load so it always looks
    # declared, without needing a real nativegate.yaml on disk.
    fake_config = type("FakeConfig", (), {"verification": type("V", (), {"is_empty": False})()})()
    monkeypatch.setattr(cli.ServiceConfig, "load", classmethod(lambda cls, service_dir: fake_config))


def test_all_layers_pass_is_reported_as_such(tmp_path, all_layers_pass):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        _service(Path(cwd))
        result = runner.invoke(cli.main, ["verify", "svc"])
    assert result.exit_code == 0, result.output
    assert "oracle: passed" in result.output
    assert "golden: passed" in result.output
    assert "invariants: passed" in result.output


def test_oracle_failure_is_named_and_does_not_mask_the_others(tmp_path, all_layers_pass, monkeypatch):
    monkeypatch.setattr(
        oracle_lib,
        "oracle_check",
        lambda name, service_dir=None, keep_build=False: oracle_lib.OracleReport(
            covered=0, skipped={}, failures=["solution_gor: argument order or intent is wrong"]
        ),
    )
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        _service(Path(cwd))
        result = runner.invoke(cli.main, ["verify", "svc"])
    assert result.exit_code != 0
    assert "oracle: FAILED" in result.output
    assert "argument order or intent is wrong" in result.output
    assert "golden: passed" in result.output
    assert "invariants: passed" in result.output
    assert "oracle" in result.output.splitlines()[-1]


def test_golden_failure_is_named_and_does_not_mask_the_others(tmp_path, all_layers_pass, monkeypatch):
    def _fail_golden(name):
        raise click.ClickException("3 numerical difference(s) found.")

    monkeypatch.setattr(cli, "_golden_verify", _fail_golden)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        _service(Path(cwd))
        result = runner.invoke(cli.main, ["verify", "svc"])
    assert result.exit_code != 0
    assert "oracle: passed" in result.output
    assert "golden: FAILED" in result.output
    assert "3 numerical difference(s) found." in result.output
    assert "invariants: passed" in result.output
    assert "golden" in result.output.splitlines()[-1]


def test_invariants_failure_is_named_and_does_not_mask_the_others(tmp_path, all_layers_pass, monkeypatch):
    def _fail_invariants(name):
        raise click.ClickException("Declared/structural invariants failed for 'svc'.")

    monkeypatch.setattr(cli, "_invariants_verify", _fail_invariants)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        _service(Path(cwd))
        result = runner.invoke(cli.main, ["verify", "svc"])
    assert result.exit_code != 0
    assert "oracle: passed" in result.output
    assert "golden: passed" in result.output
    assert "invariants: FAILED" in result.output
    assert "invariants" in result.output.splitlines()[-1]


def test_no_toolchain_is_reported_visibly_not_silently(tmp_path, all_layers_pass, monkeypatch):
    monkeypatch.setattr(cli, "_toolchain_present", lambda: False)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        _service(Path(cwd))
        result = runner.invoke(cli.main, ["verify", "svc"])
    assert result.exit_code == 0, result.output
    assert "oracle: skipped, no toolchain" in result.output
