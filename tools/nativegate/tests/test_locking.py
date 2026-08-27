"""Hash-pinned dependency locks.

The resolution itself needs the network and PyPI, so it is exercised once in a
marked test and the rest work on the data structures — which is where the
mistakes that matter live: a lock that pins a version the image would never
have chosen, or one that silently drops an architecture.
"""

import pytest

from nativegate import locking
from nativegate.locking import LockError, LockedPackage


def test_the_lock_targets_the_image_not_the_developer_machine():
    # The lock is consumed inside python:3.12-slim on Linux. Resolving with the
    # local interpreter would pin macOS wheels, or wheels for whatever Python
    # the developer runs, and the build would fail — or worse, succeed with a
    # different set.
    assert locking.LOCK_PYTHON_VERSION == "3.12"
    assert all("linux" in platform for platform in locking.LOCK_PLATFORMS)


def test_both_image_architectures_are_covered():
    # BASE_IMAGE is a multi-arch index, so a lock naming only x86_64 wheels
    # breaks every arm64 build.
    assert any(p.endswith("x86_64") for p in locking.LOCK_PLATFORMS)
    assert any(p.endswith("aarch64") for p in locking.LOCK_PLATFORMS)


def test_newer_manylinux_tags_are_offered_not_just_the_oldest():
    # `--platform` is an EXACT tag match, not a floor. Asking only for
    # manylinux2014 excluded numpy 2.5.2, which publishes manylinux_2_28 only —
    # so the first version of this locked numpy 2.2.6 while the image itself
    # resolved 2.5.2. A lock that pins something the image would not have
    # chosen is worse than no lock: it looks authoritative and is wrong.
    assert any("2_28" in platform for platform in locking.LOCK_PLATFORMS)


def test_a_version_split_across_architectures_is_refused(monkeypatch):
    # Locking one side would make the image's contents depend on where it was
    # built, which is the thing a lock exists to prevent.
    calls = {"n": 0}

    def fake_resolve(requirements, platform, pip):
        calls["n"] += 1
        version = "1.0" if calls["n"] == 1 else "2.0"
        return {"widget": LockedPackage("widget", version, {"a" * 64})}

    monkeypatch.setattr(locking, "_resolve", fake_resolve)

    with pytest.raises(LockError, match="one image architecture"):
        locking.resolve_locked(["widget"])


def test_hashes_from_every_architecture_are_merged(monkeypatch):
    # --require-hashes accepts several hashes for one requirement and matches
    # whichever artifact it downloads, so one entry covers both architectures.
    digests = iter(["a" * 64, "b" * 64])

    monkeypatch.setattr(
        locking,
        "_resolve",
        lambda r, p, pip: {"widget": LockedPackage("widget", "1.0", {next(digests)})},
    )

    packages = locking.resolve_locked(["widget"])

    assert len(packages) == 1
    assert packages[0].hashes == {"a" * 64, "b" * 64}


def test_a_missing_hash_is_refused_rather_than_written(monkeypatch):
    # A partial lock is a lock that lies: pip would accept the entries that
    # have hashes and the rest would resolve freely.
    class Result:
        returncode = 0
        stdout = '{"install": [{"metadata": {"name": "widget", "version": "1.0"}}]}'
        stderr = ""

    monkeypatch.setattr(locking.subprocess, "run", lambda *a, **k: Result())

    with pytest.raises(LockError, match="refusing to write a partial lock"):
        locking.resolve_locked(["widget"])


def test_the_rendered_lock_is_in_pips_require_hashes_format():
    text = locking.render_lock(
        [LockedPackage("numpy", "2.5.2", {"b" * 64, "a" * 64})], "petro_api"
    )

    assert "numpy==2.5.2 \\\n    --hash=sha256:" + "a" * 64 in text
    assert "--hash=sha256:" + "b" * 64 in text
    # Sorted, so re-running the lock over unchanged input does not churn the
    # file and every diff means something really changed.
    assert text.index("a" * 64) < text.index("b" * 64)


def test_names_are_normalised_so_one_package_is_one_entry():
    # `uvicorn-worker` and `uvicorn_worker` are the same distribution; treating
    # them as two would produce a lock with a duplicate and no hash agreement.
    assert locking._normalise("uvicorn_worker") == locking._normalise("uvicorn-worker")
    assert locking._normalise("Typing.Extensions") == "typing-extensions"


def test_an_empty_requirement_list_locks_nothing():
    # A C++ service with no declared dependencies must not shell out to pip and
    # must not write a lock file full of nothing.
    assert locking.resolve_locked([]) == []
