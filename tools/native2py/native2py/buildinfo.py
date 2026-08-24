"""What the build actually did: extracted flags, safety gate, pinned env.

Layer 2 (the oracle, `design-verification-layers.md` section 2) makes a
bitwise claim — the driver and the extension must be *the same machine code*
called two ways. That claim is only as good as the flags it is checked
against, and checking it against `native2py.yaml` or any other restated
configuration measures the configuration, not the build: a flag can be added
in a CMake cache variable, a toolchain file, an environment default, or a
compiler wrapper without ever touching the files this tool wrote. So this
module reads the build system's own record of what it ran —
`compile_commands.json`, the de-facto standard JSON Compilation Database that
CMake (with `CMAKE_EXPORT_COMPILE_COMMANDS=ON`) and Meson (which `f2py -c
--backend meson` drives, and which writes one unconditionally into its build
directory) both produce — and extracts flags from *that*.

Four things live here, matching spec sections 2.3, 2.8 and 4 rules 2-3:

* **Extraction** (`load_compile_commands`, `flags_for_source`): turn a
  `compile_commands.json` entry for one source file into a flat flag list,
  compiler executable and output/source bookkeeping tokens stripped out.
* **The safety gate** (`refuse_unsafe`): `-ffast-math`, `-Ofast` and
  `-funsafe-math-optimizations` discard IEEE semantics, and a bitwise
  comparison under them proves nothing. This is a hard error, checked against
  the extracted flags, never a warning and never checked against config.
* **The codegen subset** (`codegen_flags`): the flags that can move a bit
  pattern for otherwise-identical source — optimization level, FP
  contraction, target/arch, and `-std=`. T4 compiles the driver translation
  unit with exactly this subset of the extension's own extracted flags, and
  diffs it against a driver-flags record to catch divergence before it shows
  up as an unexplained last-bit mismatch.
* **The pinned environment** (`pinned_environment`): the harness *sets*
  `OMP_NUM_THREADS=1` and the BLAS thread-count equivalents in both processes
  it runs — it does not check whether they are already set and refuse if not.
  Reduction order changes bits, and there is no configuration in which the
  harness wants more than one thread; setting is strictly better than
  refusing, because a refusal would require every caller to already know to
  set these before the harness ever gets a chance to.

No new runtime dependency: `compile_commands.json` is parsed with `json` and
`shlex`, both stdlib, and hashing is `hashlib.sha256` — the same primitives
`golden.py` already uses for source digests.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import Sequence

# spec design-verification-layers.md section 2.8 / section 4 rule 2 — checked
# against extracted flags, never against native2py.yaml or any other config.
_UNSAFE_FLAGS = frozenset({
    "-ffast-math",
    "-Ofast",
    "-funsafe-math-optimizations",
})

# Prefixes of flags that affect code generation for otherwise-identical
# source (spec section 2.3's "the extracted flags" and T4's "differ in any
# way that affects code generation"). Exact-match entries are compared
# case-sensitively against a whole token; prefix entries match `str.startswith`.
#
# -O<n>/-Os/-Og/-Ofast    optimization level (the codegen the compiler picks)
# -ffast-math, -funsafe-* also codegen-affecting, but they are refused
#                          outright by `refuse_unsafe` rather than compared —
#                          they never reach a driver build to diverge on.
# -ffp-contract=, -mfma   FP contraction / fused multiply-add selection
# -march=, -mtune=, -mcpu=, -target, --target=, -arch  target/ISA selection
# -std=                   language standard, which can change constant
#                          folding and intrinsic selection
_CODEGEN_EXACT = frozenset({
    "-Os",
    "-Og",
})
_CODEGEN_PREFIXES = (
    "-O",  # -O0 .. -O3, -Ofast (also unsafe; caught by refuse_unsafe too)
    "-ffp-contract=",
    "-mfma",
    "-march=",
    "-mtune=",
    "-mcpu=",
    "-target",
    "--target=",
    "-arch",
    "-std=",
)

# Tokens that are part of the compile *invocation* rather than a flag: the
# "-c" mode switch and the "-o <file>" output pair. The compiler executable
# (argv[0]) and the source file argument are stripped by position/identity
# in `_strip_invocation`, not here, since they carry no flag prefix to match.
_INVOCATION_ONLY = frozenset({"-c"})


class UnsafeFlagError(RuntimeError):
    """Raised by `refuse_unsafe` when a fast-math flag is present.

    A hard error, not a warning (spec section 2.8): the oracle's whole claim
    is that no tolerance is needed because the same machine code produced
    both sides, and a fast-math flag makes that claim false regardless of
    what the bits happen to show on a given run.
    """


def load_compile_commands(path: Path) -> list[dict]:
    """Parse a JSON Compilation Database.

    Just `json.loads` plus a clearer error: a missing or malformed
    `compile_commands.json` should say so, not surface as a bare
    `JSONDecodeError` deep in a caller that has no idea what file it was
    reading.
    """
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise FileNotFoundError(f"no compile_commands.json at {path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON array of compile command entries")
    return data


def _entry_argv(entry: dict) -> list[str]:
    """The full argv for one compile command entry, compiler included.

    CMake's `CMAKE_EXPORT_COMPILE_COMMANDS` and Meson (and therefore `f2py -c
    --backend meson`, which shells out to Meson/ninja) both write a `command`
    field holding a single shell-quoted string. Newer CMake can instead write
    an `arguments` array directly. Support both rather than assuming one.
    """
    if "arguments" in entry:
        return list(entry["arguments"])
    if "command" in entry:
        return shlex.split(entry["command"])
    raise ValueError(f"compile command entry has neither 'command' nor 'arguments': {entry!r}")


def _matches_source(entry: dict, source_name: str) -> bool:
    """Match an entry to a requested source by filename.

    Entries record `file` as a path — sometimes absolute, sometimes relative
    to `directory`, and in a driver build the requested name may be given as
    a bare filename (`"pvtcor.f"`) without knowing which of those forms the
    build recorded. Matching on the final path component is the only thing
    guaranteed stable across CMake/Meson's differing path conventions; a
    caller wanting to disambiguate identically-named sources in different
    directories should match on the full path it already knows and pass that
    through instead of relying on this alone.
    """
    entry_file = entry.get("file", "")
    return Path(entry_file).name == Path(source_name).name


def find_entry(commands: Sequence[dict], source_name: str) -> dict:
    """The single compile command entry for `source_name`.

    Raises `KeyError` naming the file actually being looked for and the
    filenames that *were* recorded, so a mismatch (wrong service directory,
    stale compile_commands.json) is diagnosable rather than a bare "not
    found".
    """
    matches = [entry for entry in commands if _matches_source(entry, source_name)]
    if not matches:
        available = sorted({Path(e.get("file", "")).name for e in commands})
        raise KeyError(
            f"no compile command for {source_name!r} in compile_commands.json "
            f"(recorded sources: {available})"
        )
    if len(matches) > 1:
        # Two sources with the same basename in different directories. T1's
        # contract is "the extension's actual flags for each native source";
        # ambiguity here is a caller bug (it should have disambiguated with a
        # fuller path), not something to guess at silently.
        available = [entry.get("file", "") for entry in matches]
        raise KeyError(
            f"{source_name!r} matches more than one compile command entry: {available}"
        )
    return matches[0]


def flags_for_source(commands: Sequence[dict], source_name: str) -> list[str]:
    """The extracted, order-preserving flag list for one native source.

    Strips the compiler executable (argv[0]), the `-c` mode switch, the `-o
    <output>` pair, and the source file argument itself, leaving exactly the
    flags — the same list `refuse_unsafe` and `codegen_flags` are meant to be
    called with. Order is preserved as the build system recorded it; nothing
    here re-sorts or de-duplicates, because flag order is sometimes
    semantically meaningful (an `-I` search path list, a later `-O` winning
    over an earlier one) and re-ordering would misrepresent what the compiler
    actually saw.
    """
    entry = find_entry(commands, source_name)
    argv = _entry_argv(entry)
    if not argv:
        return []

    source_file = entry.get("file", "")
    flags: list[str] = []
    skip_next = False
    for i, token in enumerate(argv):
        if i == 0:
            continue  # compiler executable
        if skip_next:
            skip_next = False
            continue
        if token in _INVOCATION_ONLY:
            continue
        if token == "-o":
            skip_next = True
            continue
        if token == source_file or Path(token).name == Path(source_file).name:
            continue
        flags.append(token)
    return flags


def extract_flags(compile_commands_path: Path, source_name: str) -> list[str]:
    """Convenience: load + find + strip in one call."""
    commands = load_compile_commands(Path(compile_commands_path))
    return flags_for_source(commands, source_name)


# --- the safety gate ------------------------------------------------------


def refuse_unsafe(flags: Sequence[str]) -> None:
    """Hard error if a fast-math flag is present. Never a warning.

    Checked by exact token match, not substring, so a hypothetical
    `-fmy-ffast-math-thing` (not a real GCC/Clang flag, but the principle
    holds for anything that merely contains the substring) does not trip a
    check meant for the literal flag.
    """
    present = sorted(set(flags) & _UNSAFE_FLAGS)
    if present:
        raise UnsafeFlagError(
            "refusing to run the oracle: unsafe floating-point flag(s) "
            f"{present} discard IEEE semantics, so a bitwise comparison "
            "under them would prove nothing (design-verification-layers.md "
            "section 2.8)"
        )


# --- the codegen subset ----------------------------------------------------


# "-target" and "-arch" (unlike "-march="/"-mtune="/"--target=", which carry
# their value after "=" in the same token) take the value as a *separate*
# following argv token ("-target x86_64-apple-darwin", "-arch arm64"). Both
# tokens have to travel together or the subset is unparseable nonsense.
_CODEGEN_TAKES_NEXT_TOKEN = frozenset({"-target", "-arch"})


def codegen_flags(flags: Sequence[str]) -> list[str]:
    """The subset of `flags` that can move a bit pattern for identical source.

    Order-preserving subsequence of the input — used by T4 to compile the
    driver TU with the same codegen-affecting flags as the extension, and to
    detect divergence by comparing this subset rather than the full
    (much noisier — include paths, warnings, dependency-file flags) list.
    """
    result = []
    take_next = False
    for flag in flags:
        if take_next:
            result.append(flag)
            take_next = False
            continue
        if flag in _CODEGEN_EXACT or flag.startswith(_CODEGEN_PREFIXES):
            result.append(flag)
            if flag in _CODEGEN_TAKES_NEXT_TOKEN:
                take_next = True
    return result


# --- the pinned environment -------------------------------------------------


def pinned_environment() -> dict[str, str]:
    """The env dict the harness runs native code under, in both processes.

    Spec design-verification-layers.md section 2.8 / section 4 rule 3: the
    harness *sets* these rather than checking they are already set and
    refusing otherwise — reduction order changes bits, and there is no
    configuration in which the harness wants more than one thread, so there
    is nothing to legitimately opt out of.
    """
    return {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
    }


# --- hashes, for provenance -------------------------------------------------


def flags_hash(flags: Sequence[str]) -> str:
    """SHA-256 of the extracted flag list, in the order given.

    "Canonical ordering" here means the order the build system itself
    recorded (`flags_for_source`'s output), not a re-sort: flag order can be
    semantically meaningful (repeated `-I`/`-D`, a later `-O` overriding an
    earlier one), and hashing a sorted copy would call two builds identical
    when the compiler would not have treated them that way. Callers that
    want the hash to be stable across two extractions of the *same* build
    get that for free, because a JSON Compilation Database's `command`/
    `arguments` for one source is itself deterministic.
    """
    payload = "\n".join(flags).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def link_target_hash(paths: Sequence[Path]) -> str:
    """SHA-256 over a set of linked archive/object files, for provenance.

    Unlike `flags_hash`, the input here is a *set* of files (the objects/
    archive the driver links against, per spec section 2.3) rather than an
    ordered sequence with meaning of its own, so this sorts by path first —
    the hash should not depend on the order a glob happened to return them
    in. Each file's path (relative form, as given) and content both feed the
    digest, so renaming one of two identical-content objects changes the
    hash — appropriate for provenance, where "which file" is part of what is
    being attested to.
    """
    hasher = hashlib.sha256()
    for path in sorted((Path(p) for p in paths), key=str):
        hasher.update(str(path).encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()
