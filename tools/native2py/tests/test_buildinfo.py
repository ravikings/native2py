"""buildinfo.py: extracted flags, the safety gate, codegen subsetting, env.

No compiler is invoked anywhere in this file — every test works from a
hand-written `compile_commands.json` fixture, the way the extension's real
build would leave one behind (CMake with `CMAKE_EXPORT_COMPILE_COMMANDS=ON`,
or the Meson build `f2py -c --backend meson` drives). See
`design-verification-layers.md` section 2.3 (extraction), 2.8 (hard
preconditions) and section 4 rules 2-3 (determinism).
"""

from __future__ import annotations

import json

import pytest

from native2py import buildinfo


def write_compile_commands(tmp_path, entries):
    path = tmp_path / "compile_commands.json"
    path.write_text(json.dumps(entries))
    return path


def command_entry(directory, command, file):
    return {"directory": str(directory), "command": command, "file": file}


def arguments_entry(directory, arguments, file):
    return {"directory": str(directory), "arguments": arguments, "file": file}


# --- extraction -------------------------------------------------------------


def test_extract_flags_from_command_string(tmp_path):
    path = write_compile_commands(
        tmp_path,
        [
            command_entry(
                tmp_path,
                "gfortran -DFOO=1 -O2 -fPIC -std=legacy -c -o pvtcor.f.o pvtcor.f",
                "pvtcor.f",
            )
        ],
    )
    flags = buildinfo.extract_flags(path, "pvtcor.f")
    assert flags == ["-DFOO=1", "-O2", "-fPIC", "-std=legacy"]


def test_extract_flags_from_arguments_array(tmp_path):
    path = write_compile_commands(
        tmp_path,
        [
            arguments_entry(
                tmp_path,
                ["clang++", "-O3", "-c", "-o", "flash.o", "flash.cpp"],
                "flash.cpp",
            )
        ],
    )
    flags = buildinfo.extract_flags(path, "flash.cpp")
    assert flags == ["-O3"]


def test_extract_flags_matches_by_basename_regardless_of_directory_prefix(tmp_path):
    path = write_compile_commands(
        tmp_path,
        [
            command_entry(
                tmp_path,
                "gfortran -O2 -c -o out.o /abs/native/_expanded/pvtcor.f",
                "/abs/native/_expanded/pvtcor.f",
            )
        ],
    )
    flags = buildinfo.extract_flags(path, "pvtcor.f")
    assert flags == ["-O2"]


def test_extract_flags_missing_source_names_the_file_and_what_was_recorded(tmp_path):
    path = write_compile_commands(
        tmp_path,
        [command_entry(tmp_path, "gfortran -O2 -c -o a.o a.f", "a.f")],
    )
    with pytest.raises(KeyError, match="wellib.f"):
        buildinfo.extract_flags(path, "wellib.f")


def test_extract_flags_ambiguous_basename_is_an_error(tmp_path):
    path = write_compile_commands(
        tmp_path,
        [
            command_entry(tmp_path, "gfortran -O2 -c -o a.o dir1/a.f", "dir1/a.f"),
            command_entry(tmp_path, "gfortran -O3 -c -o a.o dir2/a.f", "dir2/a.f"),
        ],
    )
    with pytest.raises(KeyError, match="more than one"):
        buildinfo.extract_flags(path, "a.f")


def test_load_compile_commands_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        buildinfo.load_compile_commands(tmp_path / "does_not_exist.json")


def test_load_compile_commands_malformed_json(tmp_path):
    path = tmp_path / "compile_commands.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        buildinfo.load_compile_commands(path)


def test_load_compile_commands_not_a_list(tmp_path):
    path = tmp_path / "compile_commands.json"
    path.write_text(json.dumps({"oops": "this is an object, not a list"}))
    with pytest.raises(ValueError, match="array"):
        buildinfo.load_compile_commands(path)


def test_entry_without_command_or_arguments_is_an_error(tmp_path):
    path = write_compile_commands(
        tmp_path, [{"directory": str(tmp_path), "file": "a.f"}]
    )
    with pytest.raises(ValueError, match="command.*arguments|arguments.*command"):
        buildinfo.extract_flags(path, "a.f")


# --- refuse_unsafe ------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe_flag", ["-ffast-math", "-Ofast", "-funsafe-math-optimizations"]
)
def test_refuse_unsafe_raises_on_each_flag(unsafe_flag):
    with pytest.raises(buildinfo.UnsafeFlagError, match=unsafe_flag.replace("-", r"\-")):
        buildinfo.refuse_unsafe(["-O2", "-fPIC", unsafe_flag])


def test_refuse_unsafe_passes_through_clean_flags():
    # Must not raise.
    buildinfo.refuse_unsafe(["-O2", "-fPIC", "-std=legacy", "-march=native"])


def test_refuse_unsafe_reports_all_offending_flags_present():
    with pytest.raises(buildinfo.UnsafeFlagError) as exc_info:
        buildinfo.refuse_unsafe(["-ffast-math", "-Ofast", "-O2"])
    message = str(exc_info.value)
    assert "-ffast-math" in message
    assert "-Ofast" in message


def test_refuse_unsafe_does_not_match_substrings():
    # Not a real compiler flag, but the fixture proves this is an exact-token
    # check rather than a substring search — "-ffast-math" appearing inside
    # a longer token must not trip the gate.
    buildinfo.refuse_unsafe(["-fmy-ffast-math-alternative-thing"])


# --- codegen_flags --------------------------------------------------------


def test_codegen_flags_subsets_optimization_level():
    assert buildinfo.codegen_flags(["-DFOO", "-O2", "-fPIC", "-Wall"]) == ["-O2"]


def test_codegen_flags_subsets_std_target_and_fp_contract():
    flags = [
        "-I/usr/include",
        "-std=legacy",
        "-march=native",
        "-mtune=native",
        "-ffp-contract=fast",
        "-DDEBUG",
        "-target",
        "x86_64-apple-darwin",
    ]
    assert buildinfo.codegen_flags(flags) == [
        "-std=legacy",
        "-march=native",
        "-mtune=native",
        "-ffp-contract=fast",
        "-target",
        "x86_64-apple-darwin",
    ]


def test_codegen_flags_preserves_order_and_drops_everything_else():
    flags = ["-Wall", "-O3", "-DFOO=1", "-Iinclude", "-std=c++17", "-g"]
    assert buildinfo.codegen_flags(flags) == ["-O3", "-std=c++17"]


def test_codegen_flags_of_empty_list_is_empty():
    assert buildinfo.codegen_flags([]) == []


# --- pinned_environment ---------------------------------------------------


def test_pinned_environment_sets_omp_and_blas_thread_vars():
    env = buildinfo.pinned_environment()
    assert env == {
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
    }


def test_pinned_environment_returns_a_fresh_dict_each_call():
    a = buildinfo.pinned_environment()
    b = buildinfo.pinned_environment()
    assert a is not b
    a["OMP_NUM_THREADS"] = "4"
    assert buildinfo.pinned_environment()["OMP_NUM_THREADS"] == "1"


# --- hashes ----------------------------------------------------------------


def test_flags_hash_is_deterministic_and_order_sensitive():
    h1 = buildinfo.flags_hash(["-O2", "-fPIC"])
    h2 = buildinfo.flags_hash(["-O2", "-fPIC"])
    h3 = buildinfo.flags_hash(["-fPIC", "-O2"])
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # sha256 hex digest


def test_flags_hash_of_empty_list_is_stable():
    assert buildinfo.flags_hash([]) == buildinfo.flags_hash([])


def test_link_target_hash_is_order_independent_but_content_sensitive(tmp_path):
    a = tmp_path / "a.o"
    b = tmp_path / "b.o"
    a.write_bytes(b"AAAA")
    b.write_bytes(b"BBBB")

    h_ab = buildinfo.link_target_hash([a, b])
    h_ba = buildinfo.link_target_hash([b, a])
    assert h_ab == h_ba

    b.write_bytes(b"CCCC")
    h_changed = buildinfo.link_target_hash([a, b])
    assert h_changed != h_ab


def test_link_target_hash_distinguishes_filename_with_same_content(tmp_path):
    a = tmp_path / "a.o"
    b = tmp_path / "b.o"
    a.write_bytes(b"SAME")
    b.write_bytes(b"SAME")
    # Same bytes, different names/paths: still a different provenance claim.
    assert buildinfo.link_target_hash([a]) != buildinfo.link_target_hash([b])
