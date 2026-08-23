"""The IR is a versioned contract — these are its terms.

`services/<name>/.native2py/ir.json` is committed, is read by `golden record`
on machines that have neither the headers nor libclang, and outlives the
native2py that wrote it. That makes serialisation a compatibility surface, not
an implementation detail, so each rule is pinned here:

  * round trip: `module_from_dict(module_to_dict(m)) == m` for a module that
    exercises every field, so a new field cannot be added to a dataclass and
    forgotten in the reader;
  * an older (or version-less) document loads, with the documented defaults;
  * a newer minor loads, reporting the fields it had to drop;
  * an unknown major is refused, naming both versions;
  * unknown keys at a version we implement are refused, not ignored;
  * the artifact actually committed in this repo still loads.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from native2py.ir import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_KEY,
    SCHEMA_VERSION_MAJOR,
    SCHEMA_VERSION_MINOR,
    WRITER_VERSION_KEY,
    ClassDef,
    FunctionDef,
    IRSchemaError,
    IRSchemaWarning,
    Method,
    ModuleIR,
    Parameter,
    SkippedSymbol,
    StructDef,
    module_from_dict,
    module_to_dict,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMITTED_IR = REPO_ROOT / "services" / "petro_api" / ".native2py" / "ir.json"


def every_field_module() -> ModuleIR:
    """A module in which no IR field is left at its default.

    Written out longhand on purpose: a field that gains a default and is never
    exercised here round-trips "correctly" by accident.
    """
    param = Parameter(
        name="pressure",
        type="float",
        is_array=True,
        intent="inout",
        native_type="std::uint64_t",
        is_const=True,
        is_optional=True,
        length_param="n",
        is_mutable_buffer=True,
        is_scalar_ref=True,
    )
    other = Parameter(name="lambda", type="float", native_type="double")
    return ModuleIR(
        name="everything",
        language="cpp",
        source_file="services/everything/native/everything.hpp",
        classes=[
            ClassDef(
                name="Engine",
                namespace="petro::core",
                methods=[
                    Method(
                        name="advance",
                        parameters=[param, other],
                        returns="float",
                        is_static=True,
                        is_const=True,
                        is_overloaded=True,
                        returns_array=True,
                        cpp_name="operator+",
                    ),
                    Method(name="reset"),
                ],
                has_default_constructor=False,
                constructors=[[param], [param, other]],
                fields=[Parameter(name="steps", type="int", native_type="int")],
                bases=["Base", "Other"],
            )
        ],
        functions=[
            FunctionDef(
                name="solve",
                parameters=[param],
                returns="float",
                is_subroutine=True,
                namespace="petro",
                is_overloaded=True,
                returns_array=True,
                cpp_name="solve_n2p",
                fortran_module="physics",
            )
        ],
        fortran_shims=["    subroutine solve_n2p(x)\n    end subroutine"],
        structs=[
            StructDef(
                name="State",
                namespace="petro",
                fields=[param, other],
                has_default_constructor=False,
            )
        ],
        fortran_module="physics",
        skipped=[SkippedSymbol(name="raw_ptr", reason="'double *' is a pointer")],
        diagnostics=["everything.hpp:3: 'missing.hpp' file not found"],
    )


# --- round trip ---------------------------------------------------------


def test_round_trip_preserves_every_field():
    module = every_field_module()
    assert module_from_dict(module_to_dict(module)) == module


def test_round_trip_through_json_text():
    """The real path: the dict is written to disk and read back."""
    module = every_field_module()
    text = json.dumps(module_to_dict(module), indent=2)
    assert module_from_dict(json.loads(text)) == module


def test_every_dataclass_field_is_exercised():
    """Guards the fixture above rather than the code.

    If someone adds `Parameter.is_pointer` and does not exercise it here, the
    round-trip test above would still pass while proving nothing about it.
    """
    import dataclasses

    document = module_to_dict(every_field_module())

    def assert_non_default(instance_dict: dict, cls, where: str) -> None:
        for f in dataclasses.fields(cls):
            value = instance_dict[f.name]
            default = f.default if f.default is not dataclasses.MISSING else None
            assert value != default or f.default is dataclasses.MISSING, (
                f"{where}.{f.name} is left at its default in every_field_module(); "
                "the round-trip test cannot prove it survives serialisation."
            )

    assert_non_default(document["classes"][0], ClassDef, "ClassDef")
    assert_non_default(document["classes"][0]["methods"][0], Method, "Method")
    assert_non_default(document["functions"][0], FunctionDef, "FunctionDef")
    assert_non_default(document["structs"][0], StructDef, "StructDef")
    assert_non_default(document["functions"][0]["parameters"][0], Parameter, "Parameter")


# --- the version stamp --------------------------------------------------


def test_written_documents_carry_the_version_and_the_writer():
    document = module_to_dict(every_field_module())
    assert document[SCHEMA_VERSION_KEY] == SCHEMA_VERSION
    assert document[WRITER_VERSION_KEY]


def test_committed_petro_api_ir_loads_and_is_versioned():
    if not COMMITTED_IR.exists():
        pytest.skip(f"{COMMITTED_IR} is not present in this checkout")
    document = json.loads(COMMITTED_IR.read_text())
    assert document[SCHEMA_VERSION_KEY] == SCHEMA_VERSION
    module = module_from_dict(document)
    assert module.name == "petro_api"
    assert module.language == "fortran"
    assert module.functions, "the committed artifact exposes no functions"


# --- older / version-less documents -------------------------------------


def test_version_less_document_loads_with_documented_defaults():
    """Every ir.json written before this change has no schema_version at all."""
    legacy = {
        "name": "legacy",
        "language": "cpp",
        "source_file": "native/legacy.hpp",
        "classes": [
            {
                "name": "Old",
                "methods": [{"name": "value", "returns": "double"}],
            }
        ],
        "functions": [{"name": "free_fn"}],
        "structs": [{"name": "Plain", "fields": [{"name": "x", "type": "float"}]}],
    }
    module = module_from_dict(legacy)

    cls = module.classes[0]
    assert cls.namespace is None
    assert cls.has_default_constructor is True
    assert cls.constructors == [] and cls.fields == [] and cls.bases == []
    method = cls.methods[0]
    assert (method.is_static, method.is_const, method.is_overloaded) == (
        False,
        False,
        False,
    )
    fn = module.functions[0]
    assert (fn.returns, fn.is_subroutine, fn.namespace, fn.is_overloaded) == (
        "void",
        False,
        None,
        False,
    )
    assert fn.fortran_module is None
    field = module.structs[0].fields[0]
    assert (field.is_array, field.intent, field.native_type) == (False, "in", None)
    assert (field.is_const, field.is_optional) == (False, False)
    assert module.structs[0].has_default_constructor is True
    assert module.skipped == [] and module.diagnostics == []


def test_older_minor_loads():
    document = module_to_dict(every_field_module())
    document[SCHEMA_VERSION_KEY] = f"{SCHEMA_VERSION_MAJOR}.0"
    assert module_from_dict(document) == every_field_module()


def test_older_minor_missing_a_field_takes_its_default():
    document = module_to_dict(every_field_module())
    document[SCHEMA_VERSION_KEY] = f"{SCHEMA_VERSION_MAJOR}.0"
    del document["functions"][0]["parameters"][0]["is_optional"]
    assert module_from_dict(document).functions[0].parameters[0].is_optional is False


# --- newer documents ----------------------------------------------------


def test_newer_minor_loads_but_reports_the_fields_it_dropped():
    document = module_to_dict(every_field_module())
    document[SCHEMA_VERSION_KEY] = f"{SCHEMA_VERSION_MAJOR}.{SCHEMA_VERSION_MINOR + 7}"
    document[WRITER_VERSION_KEY] = "9.9.9"
    document["functions"][0]["parameters"][0]["array_extent"] = 12

    with pytest.warns(IRSchemaWarning) as caught:
        module = module_from_dict(document)

    message = str(caught[0].message)
    assert "array_extent" in message
    assert "9.9.9" in message
    # Everything this native2py *does* understand still arrived intact.
    assert module.functions[0].parameters[0].native_type == "std::uint64_t"


def test_unknown_major_is_refused_naming_both_versions():
    document = module_to_dict(every_field_module())
    document[SCHEMA_VERSION_KEY] = f"{SCHEMA_VERSION_MAJOR + 1}.0"
    document[WRITER_VERSION_KEY] = "7.0.1"

    with pytest.raises(IRSchemaError) as excinfo:
        module_from_dict(document)

    message = str(excinfo.value)
    assert "7.0.1" in message, "the message must name the native2py that wrote it"
    assert SCHEMA_VERSION in message, "and the schema this native2py reads"
    assert f"{SCHEMA_VERSION_MAJOR + 1}.0" in message
    assert "native2py generate" in message, "and what to do about it"


def test_unknown_major_without_a_writer_version_still_explains_itself():
    document = module_to_dict(every_field_module())
    document[SCHEMA_VERSION_KEY] = "99.0"
    del document[WRITER_VERSION_KEY]
    with pytest.raises(IRSchemaError, match="unknown native2py version"):
        module_from_dict(document)


def test_older_major_is_refused_too():
    """A 0.x document is not a "load it anyway" case: 1.0 changed a meaning."""
    document = module_to_dict(every_field_module())
    document[SCHEMA_VERSION_KEY] = "0.9"
    with pytest.raises(IRSchemaError, match="older major version"):
        module_from_dict(document)


def test_unreadable_version_string_is_refused():
    document = module_to_dict(every_field_module())
    document[SCHEMA_VERSION_KEY] = "one-point-oh"
    with pytest.raises(IRSchemaError, match="Unreadable schema_version"):
        module_from_dict(document)


# --- unknown keys -------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d.update(clases=[]), "the module"),
        (lambda d: d["classes"][0].update(method=[]), "class 'Engine'"),
        (lambda d: d["classes"][0]["methods"][0].update(is_cosnt=True), "method 'advance'"),
        (lambda d: d["structs"][0].update(namspace="x"), "struct 'State'"),
        (lambda d: d["functions"][0].update(is_subrutine=True), "function 'solve'"),
        (lambda d: d["functions"][0]["parameters"][0].update(is_aray=True), "a parameter"),
        (lambda d: d["skipped"][0].update(resaon="x"), "a skipped symbol"),
    ],
)
def test_unknown_keys_do_not_pass_silently(mutate, needle):
    """A typo'd field is corruption, not forward compatibility.

    At a version this native2py fully implements there is no honest reading of
    an unknown key: ignoring `is_cosnt: true` would produce a binding that
    silently disagrees with the document that describes it.
    """
    document = module_to_dict(every_field_module())
    mutate(document)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(IRSchemaError) as excinfo:
            module_from_dict(document)
    assert needle in str(excinfo.value)


# --- validate() ---------------------------------------------------------


def test_validate_accepts_a_keyword_named_parameter():
    """`lambda` is escaped to `lambda_`, so it is legal — see python_identifier."""
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        functions=[FunctionDef(name="attenuate", parameters=[Parameter("lambda", "float")])],
    )
    assert validate(module) == []


def test_validate_catches_escaped_name_collision():
    """`class lambda` and `struct lambda_` both emit `lambda_`; one shadows the other."""
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        classes=[ClassDef(name="lambda")],
        structs=[StructDef(name="lambda_")],
    )
    problems = validate(module)
    assert len(problems) == 1
    message = problems[0].message
    assert "lambda_" in message and "collides" in message
    # Both spellings named, or the report does not say what to rename.
    assert "'lambda'" in message and "'lambda_'" in message


def test_validate_catches_plain_duplicate_names():
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        structs=[StructDef(name="State")],
        functions=[FunctionDef(name="State")],
    )
    problems = validate(module)
    assert len(problems) == 1
    assert "collides with a struct" in problems[0].message


@pytest.mark.parametrize("name", ["2x", "with space", "", "a-b"])
def test_validate_catches_unspellable_symbol_names(name):
    module = ModuleIR(
        name="m", language="cpp", source_file="m.hpp", classes=[ClassDef(name=name)]
    )
    problems = validate(module)
    assert problems and "cannot be spelled" in problems[0].message


@pytest.mark.parametrize("name", ["2x", "with space", ""])
def test_validate_catches_unspellable_parameter_names(name):
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        classes=[
            ClassDef(
                name="Engine",
                methods=[Method(name="run", parameters=[Parameter(name, "float")])],
            )
        ],
    )
    problems = validate(module)
    assert problems
    assert problems[0].symbol.startswith("Engine.run(")
    assert "cannot be spelled" in problems[0].message


def test_validate_is_quiet_on_a_healthy_module():
    assert validate(every_field_module()) == []
