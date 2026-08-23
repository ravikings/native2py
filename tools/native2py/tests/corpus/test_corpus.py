"""The differential corpus harness, as a pytest run.

`harness.py` is the real implementation and is runnable standalone (the parser
swap will run it in a loop, outside the suite). This file exists so that the
same gate fires in CI: for every named backend that (a) can run here and (b)
has committed snapshots, re-parse the corpus and assert the IR is unchanged —
reporting the difference *per symbol*, which is the only form in which a
parser regression is actionable.

Deliberately NOT asserted here: that two backends agree with each other. They
do not, and they are not supposed to — `cpp:regex` is a strictly weaker reader
than `cpp:clang`, and `tests/corpus/README.md` records exactly how. Pinning
each backend to its own snapshot is what catches a change; comparing backends
is what measures a swap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402


def _snapshotted_backends():
    return [b for b in harness.available_backends() if harness.is_snapshotted(b)]


def _ids(backends):
    return [b.key for b in backends]


BACKENDS = _snapshotted_backends()


def test_at_least_one_backend_is_snapshotted():
    """A corpus with no snapshots at all is a harness that gates nothing."""
    assert BACKENDS, (
        "No named parser backend has committed snapshots under tests/corpus/snapshots. "
        "Run: python tests/corpus/harness.py snapshot --update"
    )


def test_corpus_is_not_empty():
    for language in harness.LANGUAGES:
        assert harness.corpus_sources(language), f"no {language} sources in the corpus"


@pytest.mark.parametrize("backend", BACKENDS, ids=_ids(BACKENDS))
def test_backend_matches_committed_snapshot(backend):
    """Live parse == committed snapshot, for one named backend."""
    live = harness.live_documents(backend)
    diffs = []
    missing = []
    for source, document in live.items():
        committed = harness.read_snapshot(backend, source)
        if committed is None:
            missing.append(source.as_posix())
            continue
        diffs.append(harness.diff_documents(source, committed, document))

    assert not missing, (
        f"{backend.key}: corpus source(s) with no committed snapshot: "
        + ", ".join(missing)
        + "\nRun: python tests/corpus/harness.py snapshot --update -b "
        + backend.key
    )
    if any(diffs):
        pytest.fail(
            harness.render_diff(
                diffs, f"{backend.key} committed", f"{backend.key} live"
            )
            + "\n\nIf this change is intended, re-record with: "
            f"python tests/corpus/harness.py snapshot --update -b {backend.key}",
            pytrace=False,
        )


@pytest.mark.parametrize("backend", BACKENDS, ids=_ids(BACKENDS))
def test_snapshots_have_no_absolute_paths(backend):
    """A snapshot carrying a developer's home directory is not portable."""
    for source in harness.corpus_sources(backend.language):
        path = harness.snapshot_path(backend, source)
        if not path.exists():
            continue
        text = path.read_text()
        assert str(harness.REPO_ROOT) not in text, f"{path} contains an absolute path"


@pytest.mark.parametrize("backend", BACKENDS, ids=_ids(BACKENDS))
def test_snapshots_are_deterministic(backend):
    """Two parses of the same source under the same backend agree byte for byte."""
    source = harness.corpus_sources(backend.language)[0]
    first = harness.dump(harness.snapshot_document(harness.parse(backend, source), source))
    second = harness.dump(harness.snapshot_document(harness.parse(backend, source), source))
    assert first == second


def test_cpp_backends_differ_and_the_diff_is_legible():
    """The harness's actual job: show *what* two backends disagree about.

    Asserting the difference is non-empty is not pedantry — it is the guard on
    the harness itself. A flattener that silently produced no symbols would
    make every backend look identical and every future parser swap look clean.
    """
    clang = harness.Backend("cpp", "clang")
    regex = harness.Backend("cpp", "regex")
    if clang not in harness.available_backends("cpp"):
        pytest.skip("libclang is not available on this machine")

    diffs = harness.compare_backends(clang, regex)
    assert any(diffs), "cpp:clang and cpp:regex are not expected to agree"

    report = harness.render_diff(diffs, clang.key, regex.key)
    # Legible means: names the file, names the symbol, names the field.
    assert "libraries/petro/cpp/include/Units.hpp" in report
    assert "only in cpp:clang: function ft_to_m()" in report
    assert "is_const: True vs False" in report
