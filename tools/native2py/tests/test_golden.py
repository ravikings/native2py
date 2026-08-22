"""The numerical regression harness (native2py/golden.py).

The harness exists to answer one question for a re-hosted engineering code:
did the answers change? These tests hold it to that — including the cases
where an over-eager implementation would answer "no" when it should say
"yes", or vice versa.

A fake package stands in for a built extension: the harness only ever touches
a module object with attributes, so a stub proves the wiring without a
compiler in the loop. The real thing is exercised end-to-end against the
built `pvt` (F77) and C++ services.
"""

import types

import pytest

from native2py import golden
from native2py.generators import golden_gen
from native2py.ir import (
    ClassDef,
    FunctionDef,
    Method,
    ModuleIR,
    Parameter,
    StructDef,
    module_from_dict,
    module_to_dict,
)


def module(**kwargs) -> ModuleIR:
    base = dict(name="pvt", language="cpp", source_file="pvt.hpp")
    base.update(kwargs)
    return ModuleIR(**base)


# --- deterministic inputs ------------------------------------------------


def test_sample_inputs_are_reproducible():
    params = [Parameter(name="a", type="float"), Parameter(name="n", type="int")]

    assert golden.sample_arguments(params, {}) == golden.sample_arguments(params, {})


def test_sample_inputs_differ_per_position():
    # Identical values for every argument would let a swapped argument pair
    # through: f(1.0, 1.0) is the same call either way round.
    params = [Parameter(name="a", type="float"), Parameter(name="b", type="float")]

    values = golden.sample_arguments(params, {})

    assert values[0] != values[1]


def test_array_inputs_have_distinct_elements():
    params = [Parameter(name="v", type="float", is_array=True)]

    assert len(set(golden.sample_arguments(params, {})[0])) == 3


def test_struct_inputs_are_built_field_by_field():
    state = StructDef(
        name="State",
        fields=[Parameter(name="bo", type="float"), Parameter(name="n", type="int")],
    )
    params = [Parameter(name="s", type="State")]

    value = golden.sample_arguments(params, {"State": state})[0]

    assert value["__struct__"] == "State"
    assert set(value["fields"]) == {"bo", "n"}


def test_unsupported_parameter_type_is_reported_not_guessed():
    params = [Parameter(name="m", type="PvtModel")]

    with pytest.raises(golden.Unsupported):
        golden.sample_arguments(params, {})


# --- the plan ------------------------------------------------------------


def test_plan_covers_methods_functions_and_statics():
    mod = module(
        classes=[
            ClassDef(
                name="Units",
                methods=[
                    Method(name="pressure", parameters=[Parameter(name="p", type="float")], returns="float"),
                    Method(name="to_bar", returns="float", is_static=True),
                ],
            )
        ],
        functions=[FunctionDef(name="flash", returns="float")],
    )

    calls, skips = golden.plan(mod)

    assert [c.key for c in calls] == ["Units.pressure", "Units.to_bar", "flash"]
    assert skips == []


def test_plan_skips_what_it_cannot_call_and_says_why():
    mod = module(
        classes=[
            ClassDef(
                name="Correlation",
                methods=[Method(name="apply", returns="float")],
                has_default_constructor=False,
                constructors=[],
            ),
            ClassDef(
                name="Registry",
                methods=[
                    Method(
                        name="adopt",
                        parameters=[Parameter(name="model", type="Correlation")],
                        returns="None",
                    )
                ],
            ),
        ]
    )

    calls, skips = golden.plan(mod)

    assert calls == []
    reasons = {skip.key: skip.reason for skip in skips}
    assert "no public constructor" in reasons["Correlation.apply"]
    assert "Correlation" in reasons["Registry.adopt"]


def test_plan_supplies_constructor_arguments():
    mod = module(
        classes=[
            ClassDef(
                name="PvtModel",
                methods=[Method(name="bo", returns="float")],
                has_default_constructor=False,
                constructors=[[Parameter(name="api", type="float")]],
            )
        ]
    )

    calls, _ = golden.plan(mod)

    assert calls[0].constructor_arguments == [1.0]


def test_plan_ignores_fortran_output_arguments():
    # An intent(out) argument is supplied by Fortran, not by the caller;
    # passing one would be an argument-count error at every call.
    mod = module(
        language="fortran",
        functions=[
            FunctionDef(
                name="blend",
                parameters=[
                    Parameter(name="a", type="float"),
                    Parameter(name="mixed", type="float", intent="out"),
                ],
                is_subroutine=True,
            )
        ],
    )

    calls, _ = golden.plan(mod)

    assert len(calls[0].arguments) == 1


# --- recording and replaying --------------------------------------------


def fake_package(**attributes):
    return types.SimpleNamespace(**attributes)


def test_record_then_replay_round_trips():
    mod = module(functions=[FunctionDef(name="double_it", parameters=[Parameter(name="x", type="float")], returns="float")])
    package = fake_package(double_it=lambda x: x * 2)

    entries, skips = golden.run(mod, package)
    document = golden.build_document(mod, entries, skips)
    results, errors = golden.replay(document, package)

    assert errors == {}
    assert golden.compare(document, results, errors) == []


def test_a_changed_answer_is_reported():
    mod = module(functions=[FunctionDef(name="f", parameters=[Parameter(name="x", type="float")], returns="float")])
    document = golden.build_document(mod, *golden.run(mod, fake_package(f=lambda x: x * 2)))

    drifted = fake_package(f=lambda x: x * 2.000001)
    results, errors = golden.replay(document, drifted)
    differences = golden.compare(document, results, errors)

    assert len(differences) == 1
    assert "f:" in differences[0]


def test_a_difference_below_tolerance_is_not_a_regression():
    # Exact equality would fail on any platform with a different libm; the
    # tolerance is what makes "unchanged" a decidable claim.
    mod = module(functions=[FunctionDef(name="f", parameters=[Parameter(name="x", type="float")], returns="float")])
    document = golden.build_document(
        mod, *golden.run(mod, fake_package(f=lambda x: 1.0)), rtol=1e-6
    )

    results, errors = golden.replay(document, fake_package(f=lambda x: 1.0 + 1e-9))

    assert golden.compare(document, results, errors) == []


def test_an_entry_point_that_disappeared_is_a_difference():
    # An API that silently loses a symbol between regenerates is exactly the
    # drift this is here to catch — a shrinking golden file must not pass.
    mod = module(functions=[FunctionDef(name="f", parameters=[Parameter(name="x", type="float")], returns="float")])
    document = golden.build_document(mod, *golden.run(mod, fake_package(f=lambda x: 1.0)))

    results, errors = golden.replay(document, fake_package())

    differences = golden.compare(document, results, errors)
    assert len(differences) == 1
    assert "cannot call it" in differences[0]


def test_call_order_is_preserved_for_stateful_code():
    # Legacy F77 keeps state in COMMON blocks: an init routine sets up what
    # the next call reads. Sorting the entries alphabetically made `verify`
    # fail immediately after `record` against an unchanged build.
    mod = module(
        language="fortran",
        functions=[
            FunctionDef(name="ZINIT", returns="None"),
            FunctionDef(name="AREAD", returns="float"),
        ],
    )

    state = {"ready": False}

    def zinit():
        state["ready"] = True

    def aread():
        return 42.0 if state["ready"] else -1.0

    package = fake_package(ZINIT=zinit, AREAD=aread)
    document = golden.build_document(mod, *golden.run(mod, package))

    assert list(document["entries"]) == ["ZINIT", "AREAD"]

    state["ready"] = False
    results, errors = golden.replay(document, package)
    assert golden.compare(document, results, errors) == []


def test_a_raising_entry_point_is_skipped_not_fatal():
    mod = module(
        functions=[
            FunctionDef(name="ok", returns="float"),
            FunctionDef(name="boom", returns="float"),
        ]
    )

    def boom():
        raise RuntimeError("native abort")

    entries, skips = golden.run(mod, fake_package(ok=lambda: 1.0, boom=boom))

    assert set(entries) == {"ok"}
    assert any("native abort" in skip.reason for skip in skips)


def test_hand_edited_inputs_survive_re_recording():
    # A generated `1.0` for an API gravity is outside every PVT correlation's
    # range. An engineer who replaced it with a real value must not lose it.
    mod = module(functions=[FunctionDef(name="f", parameters=[Parameter(name="p", type="float")], returns="float")])
    package = fake_package(f=lambda p: p * 2)
    document = golden.build_document(mod, *golden.run(mod, package))

    document["entries"]["f"]["arguments"] = [2500.0]

    entries, _ = golden.run(mod, package, document)

    assert entries["f"]["arguments"] == [2500.0]
    assert entries["f"]["result"] == 5000.0


def test_struct_results_are_compared_field_by_field():
    state = StructDef(name="State", fields=[Parameter(name="bo", type="float")])
    mod = module(
        structs=[state],
        functions=[FunctionDef(name="flash", returns="State")],
    )

    class NativeState:
        def __init__(self, bo):
            self.bo = bo

    document = golden.build_document(
        mod, *golden.run(mod, fake_package(flash=lambda: NativeState(1.1), State=NativeState))
    )
    assert document["entries"]["flash"]["result"] == {"bo": 1.1}

    results, errors = golden.replay(
        document, fake_package(flash=lambda: NativeState(1.2), State=NativeState)
    )
    assert golden.compare(document, results, errors)


def test_document_has_nothing_that_varies_between_runs():
    # A golden file diffs in review. Timestamps or machine identity in it
    # would make every re-record look like a change.
    mod = module(functions=[FunctionDef(name="f", returns="float")])
    package = fake_package(f=lambda: 1.0)

    first = golden.build_document(mod, *golden.run(mod, package))
    second = golden.build_document(mod, *golden.run(mod, package))

    assert first == second


def test_coverage_reports_recorded_and_skipped():
    mod = module(
        functions=[FunctionDef(name="f", returns="float")],
        classes=[
            ClassDef(
                name="Abstract",
                methods=[Method(name="apply", returns="float")],
                has_default_constructor=False,
                constructors=[],
            )
        ],
    )

    document = golden.build_document(mod, *golden.run(mod, fake_package(f=lambda: 1.0)))

    assert golden.coverage(document) == (1, 1)


# --- the generated test --------------------------------------------------


def test_generated_golden_test_is_self_contained():
    code = golden_gen.generate_golden_test("pvt", "pvt", "golden.json")

    # A generated service is a deployable artifact: making its test suite
    # import native2py would mean shipping the code generator with it, or a
    # CI job that passes locally and fails in the container.
    imports = [
        line.strip()
        for line in code.splitlines()
        if line.startswith(("import ", "from ")) or line.strip().startswith("import ")
    ]
    assert not [line for line in imports if "native2py" in line]
    assert "import pvt" in imports
    compile(code, "test_golden.py", "exec")


def test_generated_golden_test_skips_when_nothing_is_recorded(tmp_path):
    code = golden_gen.generate_golden_test("pvt", "pvt", "golden.json")

    assert "pytest.skip" in code
    assert "native2py golden record pvt" in code


# --- IR round-trip (what `golden record` reads) --------------------------


def test_ir_survives_a_json_round_trip():
    mod = module(
        structs=[StructDef(name="State", fields=[Parameter(name="bo", type="float")])],
        classes=[
            ClassDef(
                name="PvtModel",
                methods=[
                    Method(
                        name="properties_at",
                        parameters=[Parameter(name="p", type="float")],
                        returns="State",
                    )
                ],
                has_default_constructor=False,
                constructors=[[Parameter(name="api", type="float")]],
                bases=["Base"],
            )
        ],
        functions=[
            FunctionDef(
                name="blend",
                parameters=[Parameter(name="a", type="float", intent="out")],
                is_subroutine=True,
                fortran_module="mixer",
            )
        ],
    )

    restored = module_from_dict(module_to_dict(mod))

    assert restored == mod
