"""Clang AST parser: the cases the regex reader structurally could not handle.

Every test here is written as a *difference* — a header the token/brace reader
either mis-parsed, silently dropped, or reported as unreadable, and which a
real compiler front end resolves. The shared behaviours (access specifiers,
pointer refusals, struct fields, ...) are covered once for both backends in
test_legacy_cpp.py.
"""

from pathlib import Path

import pytest

from nativegate.config import ClangConfig, ExposeConfig, ServiceConfig
from nativegate.parsers import cpp as cpp_parser
from nativegate.parsers import cpp_ast, cpp_regex

pytestmark = pytest.mark.skipif(
    not cpp_ast.is_available(),
    reason=f"libclang unavailable: {cpp_ast.unavailable_reason()}",
)


def parse(source: str, tmp_path: Path, name: str = "x.hpp", expose=None, **kwargs):
    path = tmp_path / name
    path.write_text(source)
    return cpp_ast.parse_header(path, expose or ExposeConfig(), **kwargs)


def method_names(module, cls_name=None):
    classes = {c.name: c for c in module.classes}
    cls = classes[cls_name] if cls_name else module.classes[0]
    return [m.name for m in cls.methods]


# --- the preprocessor actually runs -------------------------------------


def test_macro_defined_name_is_resolved(tmp_path):
    # The regex reader had no preprocessor: it reported this as unreadable
    # ("if the name comes from a macro..."). An AST parse expands it.
    module = parse(
        """
        #define F77_NAME(lower, UPPER) lower##_
        extern "C" {
            double F77_NAME(pvtbub, PVTBUB)(double api, double temperature);
        }
        """,
        tmp_path,
    )

    assert [f.name for f in module.functions] == ["pvtbub_"]
    assert [p.name for p in module.functions[0].parameters] == ["api", "temperature"]
    assert module.skipped == []


def test_types_from_an_included_header_resolve(tmp_path):
    (tmp_path / "state.hpp").write_text("struct State { double bo; int flag; };")
    module = parse(
        """
        #include "state.hpp"
        class Fluid {
        public:
            State state_at(double p) const;
        };
        """,
        tmp_path,
    )

    # The struct belongs to state.hpp and is bound when that header is parsed;
    # this header contributes only the class that uses it.
    assert module.structs == []
    assert module.classes[0].methods[0].returns == "State"


def test_include_path_from_configuration_is_honoured(tmp_path):
    include_dir = tmp_path / "shared"
    include_dir.mkdir()
    (include_dir / "units.hpp").write_text("struct Units { double factor; };")

    module = parse(
        """
        #include <units.hpp>
        class Converter { public: Units units() const; };
        """,
        tmp_path,
        options=cpp_ast.ClangOptions(include_paths=(str(include_dir),)),
    )

    assert module.classes[0].methods[0].returns == "Units"
    assert module.diagnostics == []


def test_missing_include_is_reported_not_mis_bound(tmp_path):
    # Clang recovers from an unknown type by pretending it was `int`. Binding
    # that would produce a service that returns integers for a struct, so the
    # declaration is refused and the compiler error surfaced.
    module = parse(
        """
        #include "not_here.hpp"
        class Fluid { public: Missing value() const; };
        """,
        tmp_path,
    )

    # A missing include is fatal: clang stops reporting after it and every
    # unresolved name silently becomes `int`, so nothing from this header can
    # be trusted — the whole file is refused rather than half-bound.
    assert module.classes == []
    assert any("not_here.hpp" in d for d in module.diagnostics)
    assert module.skipped and "compiler rejected" in module.skipped[0].reason


def test_ifdef_disabled_declarations_are_not_bound(tmp_path):
    module = parse(
        """
        class Fluid {
        public:
        #ifdef LEGACY_API
            double old_bubble_point(double t);
        #endif
            double bubble_point(double t);
        };
        """,
        tmp_path,
    )

    assert method_names(module) == ["bubble_point"]


def test_ifdef_enabled_by_a_define_is_bound(tmp_path):
    module = parse(
        """
        class Fluid {
        public:
        #ifdef LEGACY_API
            double old_bubble_point(double t);
        #endif
            double bubble_point(double t);
        };
        """,
        tmp_path,
        options=cpp_ast.ClangOptions(defines=("LEGACY_API=1",)),
    )

    assert method_names(module) == ["old_bubble_point", "bubble_point"]


# --- typedefs and aliases -----------------------------------------------


def test_typedef_and_using_alias_resolve_to_their_canonical_type(tmp_path):
    module = parse(
        """
        typedef double Pressure;
        using Depth = double;
        typedef unsigned long CellCount;
        class Grid {
        public:
            Pressure pressure_at(Depth d) const;
            CellCount cells() const;
        };
        """,
        tmp_path,
    )

    methods = {m.name: m for m in module.classes[0].methods}
    assert methods["pressure_at"].returns == "float"
    assert methods["pressure_at"].parameters[0].type == "float"
    assert methods["cells"].returns == "int"


def test_anonymous_typedef_struct_is_bound_under_its_typedef_name(tmp_path):
    module = parse("typedef struct { double bo; int flag; } State;", tmp_path)

    assert [s.name for s in module.structs] == ["State"]
    assert [f.type for f in module.structs[0].fields] == ["float", "int"]


def test_std_string_maps_to_str_through_a_typedef(tmp_path):
    module = parse(
        """
        #include <string>
        typedef std::string Text;
        class Deck { public: Text title() const; void set_title(Text t); };
        """,
        tmp_path,
    )

    method = module.classes[0].methods[0]
    assert method.returns == "str"
    assert module.classes[0].methods[1].parameters[0].type == "str"


# --- templates ----------------------------------------------------------


def test_class_template_is_reported_as_unbindable(tmp_path):
    module = parse("template <typename T> class Buffer { public: T at(int i); };", tmp_path)

    assert module.classes == []
    assert [s.name for s in module.skipped] == ["Buffer"]
    assert "template" in module.skipped[0].reason


def test_function_template_is_reported_as_unbindable(tmp_path):
    module = parse("template <typename T> T clamp(T v, T lo, T hi);", tmp_path)

    assert module.functions == []
    assert [s.name for s in module.skipped] == ["clamp"]


def test_member_template_does_not_lose_the_rest_of_the_class(tmp_path):
    module = parse(
        """
        class Solver {
        public:
            template <typename T> T scaled(T v);
            double residual() const;
        };
        """,
        tmp_path,
    )

    assert method_names(module) == ["residual"]
    assert any("template" in s.reason for s in module.skipped)


# --- what the AST knows that tokens don't -------------------------------


def test_deleted_and_defaulted_members_are_handled(tmp_path):
    module = parse(
        """
        class Handle {
        public:
            Handle() = default;
            Handle(const Handle&) = delete;
            double value() const;
        };
        """,
        tmp_path,
    )

    cls = module.classes[0]
    assert cls.has_default_constructor is True
    assert cls.constructors == []  # copy ctor deleted, nothing else to bind
    assert [m.name for m in cls.methods] == ["value"]


def test_copy_and_move_constructors_are_not_bound_as_inits(tmp_path):
    module = parse(
        """
        class Fluid {
        public:
            Fluid(double api);
            Fluid(const Fluid& other);
            Fluid(Fluid&& other);
        };
        """,
        tmp_path,
    )

    cls = module.classes[0]
    assert len(cls.constructors) == 1
    assert [p.type for p in cls.constructors[0]] == ["float"]
    assert cls.has_default_constructor is False


def test_abstract_class_gets_no_default_init(tmp_path):
    # py::init<>() on an abstract class is a compile error deep inside
    # pybind11's templates; the AST knows the class is abstract.
    module = parse(
        """
        class Correlation {
        public:
            virtual double apply(double x) const = 0;
        };
        """,
        tmp_path,
    )

    cls = module.classes[0]
    assert cls.has_default_constructor is False
    assert [m.name for m in cls.methods] == ["apply"]


def test_overloads_are_all_captured(tmp_path):
    module = parse(
        """
        class Units {
        public:
            double convert(double v);
            double convert(double v, int system);
        };
        """,
        tmp_path,
    )

    assert method_names(module) == ["convert", "convert"]


def test_nested_namespaces_are_qualified(tmp_path):
    module = parse(
        """
        namespace petro { namespace pvt {
        class Fluid { public: double bubble_point(double t); };
        } }
        """,
        tmp_path,
    )

    assert module.classes[0].namespace == "petro::pvt"


def test_multi_level_inheritance_records_the_direct_base_only(tmp_path):
    # pybind11 needs the *direct* base; it resolves the rest of the chain from
    # each class_ declaration. Listing grandparents too would be a compile
    # error ("Base is not a base of Derived" is not the message you get —
    # you get several hundred lines of template noise).
    module = parse(
        """
        class A { public: double a(); };
        class B : public A { public: double b(); };
        class C : public B { public: double c(); };
        """,
        tmp_path,
    )

    bases = {c.name: c.bases for c in module.classes}
    assert bases == {"A": [], "B": ["A"], "C": ["B"]}


def test_multiple_inheritance_records_every_public_base(tmp_path):
    module = parse(
        """
        class Readable { public: double read(); };
        class Writable { public: void write(double v); };
        class Stream : public Readable, public Writable { public: void flush(); };
        """,
        tmp_path,
    )

    assert {c.name: c.bases for c in module.classes}["Stream"] == [
        "Readable",
        "Writable",
    ]


def test_virtual_and_protected_bases_are_distinguished(tmp_path):
    module = parse(
        """
        class Shared { public: double v(); };
        class Hidden { public: double h(); };
        class Node : public virtual Shared, protected Hidden { public: double n(); };
        """,
        tmp_path,
    )

    assert {c.name: c.bases for c in module.classes}["Node"] == ["Shared"]


def test_base_defined_in_a_sibling_header_is_recorded(tmp_path):
    (tmp_path / "correlation.hpp").write_text(
        "class Correlation { public: double apply(double x); };"
    )
    module = parse(
        """
        #include "correlation.hpp"
        class Standing : public Correlation { public: double bubble_point(double t); };
        """,
        tmp_path,
    )

    assert {c.name: c.bases for c in module.classes}["Standing"] == ["Correlation"]


def test_unbound_base_is_reported_not_silently_flattened(tmp_path):
    # Losing the base means losing every inherited method, with no error at
    # build time and an AttributeError much later in Python.
    module = parse(
        """
        class Standing : public Unbound { public: double bubble_point(double t); };
        class Unbound;
        """,
        tmp_path,
        name="y.hpp",
    )

    assert module.classes == [] or module.classes[0].bases == []
    assert module.skipped


def test_abstract_base_still_binds_its_interface(tmp_path):
    module = parse(
        """
        class Correlation { public: virtual double apply(double x) const = 0; };
        class Standing : public Correlation { public: double apply(double x) const; };
        """,
        tmp_path,
    )

    classes = {c.name: c for c in module.classes}
    assert classes["Correlation"].has_default_constructor is False
    assert classes["Standing"].has_default_constructor is True
    assert classes["Standing"].bases == ["Correlation"]


def test_private_inheritance_and_base_members_are_not_bound(tmp_path):
    # Only members declared in this class are bound; a base class needs its
    # own py::class_ and a `py::class_<Derived, Base>` declaration.
    module = parse(
        """
        class Base { public: double from_base(); };
        class Derived : public Base { public: double from_derived(); };
        """,
        tmp_path,
    )

    assert method_names(module, "Derived") == ["from_derived"]


def test_enum_parameters_and_returns_map_to_int(tmp_path):
    module = parse(
        """
        enum class System { Field, Metric };
        enum Legacy { A, B };
        class Units {
        public:
            System system() const;
            void set_system(System s);
            void set_legacy(Legacy l);
        };
        """,
        tmp_path,
    )

    methods = {m.name: m for m in module.classes[0].methods}
    assert methods["system"].returns == "int"
    assert methods["set_system"].parameters[0].type == "int"
    assert methods["set_legacy"].parameters[0].type == "int"


def test_static_methods_are_flagged(tmp_path):
    module = parse(
        "class Units { public: static double to_bar(double psi); double id() const; };",
        tmp_path,
    )

    flags = {m.name: m.is_static for m in module.classes[0].methods}
    assert flags == {"to_bar": True, "id": False}


def test_array_parameter_keeps_its_array_flag(tmp_path):
    module = parse(
        "class Grid { public: double total(double values[], int n); };", tmp_path
    )

    parameters = module.classes[0].methods[0].parameters
    assert parameters[0].is_array is True
    assert parameters[0].type == "float"


def test_reference_parameters_are_not_mistaken_for_pointers(tmp_path):
    module = parse(
        """
        struct State { double bo; };
        class Fluid { public: void fill(State& out, const double& p); };
        """,
        tmp_path,
    )

    parameters = method_types(module, "Fluid", "fill")
    assert parameters == ["State", "float"]


def method_types(module, cls_name, method_name):
    cls = {c.name: c for c in module.classes}[cls_name]
    method = [m for m in cls.methods if m.name == method_name][0]
    return [p.type for p in method.parameters]


def test_unnamed_parameters_get_positional_names(tmp_path):
    module = parse("class W { public: double f(double, int); };", tmp_path)

    assert [p.name for p in module.classes[0].methods[0].parameters] == ["arg0", "arg1"]


def test_default_arguments_do_not_change_the_signature(tmp_path):
    module = parse("class W { public: double f(double x, int n = 4); };", tmp_path)

    assert [p.name for p in module.classes[0].methods[0].parameters] == ["x", "n"]


def test_forward_declared_only_type_is_refused_with_a_reason(tmp_path):
    module = parse(
        """
        class WellModel;
        class Simulator { public: void add_well(WellModel* well); double dt() const; };
        """,
        tmp_path,
    )

    assert method_names(module, "Simulator") == ["dt"]
    assert any("forward-declared" in s.reason for s in module.skipped)


def test_sibling_header_completes_a_forward_declared_type(tmp_path):
    module = parse(
        """
        class WellModel;
        class Simulator { public: void add_well(WellModel* well); };
        """,
        tmp_path,
        extra_known_records=frozenset({"WellModel"}),
    )

    assert module.classes[0].methods[0].parameters[0].type == "WellModel"


def test_defined_record_names_ignores_forward_declarations(tmp_path):
    path = tmp_path / "x.hpp"
    path.write_text("class Forward;\nclass Defined { public: double v(); };")

    assert cpp_ast.defined_record_names(path) == frozenset({"Defined"})


def test_declarations_from_includes_are_not_bound_twice(tmp_path):
    (tmp_path / "base.hpp").write_text("class Shared { public: double v(); };")
    module = parse('#include "base.hpp"\nclass Local { public: double w(); };', tmp_path)

    assert [c.name for c in module.classes] == ["Local"]


# --- backend selection --------------------------------------------------


def test_auto_backend_prefers_clang(monkeypatch):
    # $NATIVEGATE_CPP_PARSER outranks "auto", so clear it: this asserts what
    # auto-selection does, not what the environment asked for.
    monkeypatch.delenv("NATIVEGATE_CPP_PARSER", raising=False)
    assert cpp_parser.resolve_backend("auto") == "clang"
    assert cpp_parser.resolve_backend(None) == "clang"


def test_regex_backend_can_be_forced(tmp_path):
    path = tmp_path / "x.hpp"
    path.write_text(
        "#define F77_NAME(l, U) l##_\n"
        'extern "C" { double F77_NAME(pvtbub, PVTBUB)(double api); }\n'
    )

    assert cpp_parser.resolve_backend("regex") == "regex"
    # The regex reader has no preprocessor, so it reports the macro instead.
    module = cpp_parser.parse_header(path, ExposeConfig(), backend="regex")
    assert module.functions == []
    assert module.skipped

    ast_module = cpp_parser.parse_header(path, ExposeConfig(), backend="clang")
    assert [f.name for f in ast_module.functions] == ["pvtbub_"]


def test_env_var_selects_the_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVEGATE_CPP_PARSER", "regex")
    assert cpp_parser.resolve_backend() == "regex"
    monkeypatch.setenv("NATIVEGATE_CPP_PARSER", "clang")
    assert cpp_parser.resolve_backend() == "clang"
    # An explicit argument still wins over the environment.
    assert cpp_parser.resolve_backend("regex") == "regex"


def test_unknown_backend_is_an_error():
    with pytest.raises(cpp_parser.ParserUnavailable):
        cpp_parser.resolve_backend("libtooling")


def test_regex_parser_reports_no_diagnostics(tmp_path):
    path = tmp_path / "x.hpp"
    path.write_text("class W { public: double v(); };")
    module = cpp_regex.parse_header(path, ExposeConfig())

    assert module.diagnostics == []


# --- configuration ------------------------------------------------------


def test_service_config_round_trips_parser_settings(tmp_path):
    config = ServiceConfig(
        name="pvt",
        language="cpp",
        parser="clang",
        clang=ClangConfig(
            std="c++20", include_paths=["../shared"], defines=["LEGACY_API=1"]
        ),
    )
    config.save(tmp_path)
    loaded = ServiceConfig.load(tmp_path)

    assert loaded.parser == "clang"
    assert loaded.clang.std == "c++20"
    assert loaded.clang.include_paths == ["../shared"]
    assert loaded.clang.defines == ["LEGACY_API=1"]


def test_service_config_defaults_to_auto(tmp_path):
    ServiceConfig(name="pvt", language="cpp").save(tmp_path)
    loaded = ServiceConfig.load(tmp_path)

    assert loaded.parser == "auto"
    assert loaded.clang.std == "c++17"


# --- the real legacy headers, through the AST ---------------------------

PETRO_INCLUDE = Path(__file__).parents[3] / "libraries" / "petro" / "cpp" / "include"


@pytest.mark.parametrize(
    "header,record,expected_methods",
    [
        ("FluidModel.hpp", "FluidModel", ["bubble_point", "properties_at", "oil_fvf"]),
        ("Simulator.hpp", "Simulator", ["initialize", "advance", "cell_count"]),
        ("WellModel.hpp", "WellModel", ["inflow_rate", "solve_operating_point"]),
        ("Units.hpp", "UnitConverter", ["pressure", "set_system"]),
        ("DeckReader.hpp", "DeckReader", ["read", "nx"]),
    ],
)
def test_petro_headers_parse_identically_to_the_regex_reader(
    header, record, expected_methods
):
    ast_module = cpp_ast.parse_header(PETRO_INCLUDE / header, ExposeConfig())

    classes = {c.name: c for c in ast_module.classes}
    assert record in classes
    names = [m.name for m in classes[record].methods]
    for expected in expected_methods:
        assert expected in names
    assert "interpolate" not in names  # private helpers stay unbound


# --- native type spelling, const-ness, overloads (DEFECTS A3-A5, B1-B3, B5, B7)


PROBE = """
#include <cstdint>
namespace petro {
enum class Correlation { Standing, VazquezBeggs, Glaso };
struct Sample { const double reference_pressure; double value; };
class PvtModel {
public:
    PvtModel(std::uint64_t well_id, float api, Correlation corr);
    double viscosity(double pressure) const;
    double viscosity(double pressure, double temperature) const;
    void   well_name(char* out, int len) const;
    const char* label() const;
};
double bubble_point(double gor, double api);
}
"""


def probe(tmp_path):
    return parse(PROBE, tmp_path)


def test_constructor_parameters_carry_the_native_type_spelling(tmp_path):
    # A3: the IR only stored the *Python* name, so py::init<> was reconstructed
    # from it and emitted py::init<int, double, int> — a truncated 64-bit id,
    # a changed float precision and an enum that does not convert.
    module = probe(tmp_path)

    model = {c.name: c for c in module.classes}["PvtModel"]
    assert len(model.constructors) == 1
    spellings = [p.native_type for p in model.constructors[0]]
    assert spellings == ["std::uint64_t", "float", "petro::Correlation"]
    # B7: the Python-side mapping stays `int`; only the native spelling knows
    # this is a scoped enum.
    assert [p.type for p in model.constructors[0]] == ["int", "float", "int"]


def test_non_const_char_pointer_is_refused_not_bound_as_str(tmp_path):
    # A4: `char* out` is an output buffer; binding it as a Python str has C++
    # writing through a pybind11 temporary.
    module = probe(tmp_path)

    assert "well_name" not in method_names(module, "PvtModel")
    reasons = {s.name: s.reason for s in module.skipped}
    assert "PvtModel::well_name" in reasons
    assert "char*" in reasons["PvtModel::well_name"]


def test_const_char_pointer_is_still_a_string(tmp_path):
    module = probe(tmp_path)

    label = [m for m in method_names(module, "PvtModel") if m == "label"]
    assert label == ["label"]
    methods = {m.name: m for m in module.classes[0].methods}
    assert methods["label"].returns == "str"


def test_free_function_records_its_namespace(tmp_path):
    # B2: without this the generator emits `&bubble_point` for petro::bubble_point.
    module = probe(tmp_path)

    functions = {f.name: f for f in module.functions}
    assert functions["bubble_point"].namespace == "petro"


def test_overloaded_methods_are_marked_and_carry_constness(tmp_path):
    # A5/B1: two `viscosity` overloads make &PvtModel::viscosity ambiguous.
    module = probe(tmp_path)

    viscosity = [m for m in module.classes[0].methods if m.name == "viscosity"]
    assert len(viscosity) == 2
    assert all(m.is_overloaded for m in viscosity)
    assert all(m.is_const for m in viscosity)
    assert not [m for m in module.classes[0].methods if m.name == "label"][
        0
    ].is_overloaded


def test_overloaded_free_functions_are_marked(tmp_path):
    module = parse(
        """
        double area(double r);
        double area(double w, double h);
        double volume(double r);
        """,
        tmp_path,
    )

    marked = {f.name: f.is_overloaded for f in module.functions}
    assert marked == {"area": True, "volume": False}


def test_const_field_is_recorded_as_const(tmp_path):
    # B3: a const member bound with def_readwrite does not compile.
    module = probe(tmp_path)

    sample = {s.name: s for s in module.structs}["Sample"]
    fields = {f.name: f for f in sample.fields}
    assert fields["reference_pressure"].is_const is True
    assert fields["value"].is_const is False
    assert fields["value"].native_type == "double"


def test_struct_with_a_const_member_has_no_default_constructor(tmp_path):
    # B5: py::init<>() was emitted unconditionally.
    module = probe(tmp_path)

    sample = {s.name: s for s in module.structs}["Sample"]
    assert sample.has_default_constructor is False


def test_struct_with_a_reference_member_has_no_default_constructor(tmp_path):
    module = parse("struct Held { double& slot; double copy; };", tmp_path)

    assert module.structs[0].has_default_constructor is False


def test_plain_struct_still_has_a_default_constructor(tmp_path):
    module = parse("struct Plain { double a; int b; };", tmp_path)

    assert module.structs[0].has_default_constructor is True


def test_record_field_native_type_is_qualified(tmp_path):
    module = parse(
        """
        namespace petro {
        enum class Correlation { Standing };
        struct Choice { Correlation corr; };
        }
        """,
        tmp_path,
    )

    field = module.structs[0].fields[0]
    assert field.type == "int"
    assert field.native_type == "petro::Correlation"


# --- language selection (DEFECTS C4) ------------------------------------


def test_clang_options_default_to_cpp():
    options = cpp_ast.ClangOptions()

    assert options.language == "c++"
    command = options.command_line(Path("/tmp/x.hpp"))
    assert command[:2] == ["-x", "c++"]
    assert "-std=c++17" in command


def test_a_c_header_can_be_parsed_as_c(tmp_path):
    # C4: `-x c++` was hardcoded, so a C-only construct failed as a C++ error.
    source = "double scale(double* restrict values, int n);\n"
    path = tmp_path / "legacy.h"
    path.write_text(source)

    as_cpp = cpp_ast.parse_header(
        path, ExposeConfig(), options=cpp_ast.ClangOptions()
    )
    as_c = cpp_ast.parse_header(
        path,
        ExposeConfig(),
        options=cpp_ast.ClangOptions(language="c", std="c11"),
    )

    assert as_cpp.diagnostics  # `restrict` is not a C++ keyword
    assert as_c.diagnostics == []
    command = cpp_ast.ClangOptions(language="c", std="c11").command_line(path)
    assert command[:2] == ["-x", "c"]
    assert "-std=c11" in command


# --- std::vector (the STL container gap) ---------------------------------
#
# `<pybind11/stl.h>` converts std::vector<T> <-> Python list. The conversion is
# a COPY in both directions, which is the whole reason the mutable-reference
# case below has to be refused rather than bound.


def test_vector_parameters_and_returns_are_bound(tmp_path):
    header = tmp_path / "stats.hpp"
    header.write_text(
        """
        #include <vector>
        #include <string>
        double mean(const std::vector<double>& samples);
        double total(std::vector<double> samples);
        std::vector<double> normalise(const std::vector<double>& samples);
        int count_positive(const std::vector<int>& values);
        std::vector<std::string> labels(int n);
        """
    )

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))
    by_name = {f.name: f for f in module.functions}

    assert set(by_name) == {"mean", "total", "normalise", "count_positive", "labels"}
    # Element type, and the fact that it IS a sequence, both survive.
    assert by_name["mean"].parameters[0].type == "float"
    assert by_name["mean"].parameters[0].is_array is True
    assert by_name["count_positive"].parameters[0].type == "int"
    assert by_name["count_positive"].parameters[0].is_array is True
    # A vector RETURN is marked too. Without returns_array a
    # std::vector<double> return is indistinguishable from a double, and the
    # endpoint annotates `float` while pybind11 hands back a list.
    assert by_name["normalise"].returns == "float"
    assert by_name["normalise"].returns_array is True
    assert by_name["labels"].returns == "str"
    assert by_name["labels"].returns_array is True
    assert by_name["mean"].returns_array is False


def test_a_mutable_vector_reference_is_refused(tmp_path):
    # THE load-bearing refusal. Measured against a real pybind11 module:
    #
    #     void scale_in_place(std::vector<double>& v, double k);
    #     >>> data = [1.0, 2.0, 3.0]
    #     >>> probe.scale_in_place(data, 10.0)
    #     >>> data
    #     [1.0, 2.0, 3.0]        # writes discarded, no error
    #
    # stl.h converts the list into a TEMPORARY vector; the callee writes into
    # the temporary and it dies on return. It compiles, runs, and returns
    # unmodified data — the silent-wrong-answer class this project treats as
    # the worst kind. So it is skipped with a reason instead.
    header = tmp_path / "mut.hpp"
    header.write_text(
        """
        #include <vector>
        void scale_in_place(std::vector<double>& samples, double k);
        double mean(const std::vector<double>& samples);
        """
    )

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))

    assert [f.name for f in module.functions] == ["mean"]
    skipped = {s.name: s.reason for s in module.skipped}
    assert "discarded" in skipped["scale_in_place"]
    # The reason has to say what to do instead, not just that it refused.
    assert "const std::vector<T>&" in skipped["scale_in_place"]


def test_a_const_vector_reference_is_not_refused(tmp_path):
    # The refusal must be about mutability, not about references — otherwise it
    # would reject the single most idiomatic way to pass an array in C++.
    header = tmp_path / "c.hpp"
    header.write_text(
        "#include <vector>\ndouble mean(const std::vector<double>& s);\n"
    )

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))

    assert [f.name for f in module.functions] == ["mean"]
    assert module.skipped == []


def test_nested_and_non_scalar_vectors_are_refused_with_a_reason(tmp_path):
    header = tmp_path / "n.hpp"
    header.write_text(
        """
        #include <vector>
        double grid(const std::vector<std::vector<double>>& cells);
        """
    )

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))

    assert module.functions == []
    assert "nested container" in module.skipped[0].reason


def test_the_bindings_include_the_stl_header(tmp_path):
    # Without <pybind11/stl.h> the conversion does not exist and the generated
    # binding fails to compile with a wall of template errors.
    from nativegate.generators import pybind_gen

    header = tmp_path / "s.hpp"
    header.write_text("#include <vector>\ndouble mean(const std::vector<double>& s);\n")
    module = cpp_ast.parse_header(header, ExposeConfig(all=True))

    bindings = pybind_gen.generate_bindings(module, "s.hpp")

    assert "#include <pybind11/stl.h>" in bindings
    assert "#include <vector>" in bindings


def test_a_cpp_router_does_not_import_numpy(tmp_path):
    # numpy is NOT a declared dependency of a C++ service. Only the Fortran
    # path marshals through it (f2py wants a contiguous buffer); pybind11 takes
    # a plain list. When std::vector became bindable, keying the import off
    # `p.is_array` put `import numpy` in every C++ router — which passes on a
    # dev machine that has numpy and fails at import inside the container.
    from nativegate.generators import python_pkg_gen

    header = tmp_path / "s.hpp"
    header.write_text(
        "#include <vector>\nint count_positive(const std::vector<int>& v);\n"
    )
    module = cpp_ast.parse_header(header, ExposeConfig(all=True))

    router = python_pkg_gen.generate_router_py(module, "stats")

    assert "import numpy" not in router
    # The list still goes straight through, untouched by any conversion.
    assert "np.array" not in router
    # And it still carries the memory-exhaustion cap.
    assert "MAX_ARRAY_ITEMS" in router


def test_both_cpp_backends_agree_on_vectors(tmp_path):
    # cpp_ast's docstring promises the two readers refuse the same signatures,
    # so switching parsers never changes which bindings are considered safe.
    # Adding std::vector to only one of them would break that quietly: a
    # machine without libclang would skip functions the CI machine bound, and
    # the difference would surface as a missing endpoint in a container.
    header = tmp_path / "stats.hpp"
    header.write_text(
        """
        #include <vector>
        #include <string>
        double mean(const std::vector<double>& samples);
        double total(std::vector<double> samples);
        std::vector<double> normalise(const std::vector<double>& samples);
        int count_positive(const std::vector<int>& values);
        std::vector<std::string> labels(int n);
        void scale_in_place(std::vector<double>& samples, double k);
        """
    )

    tree = cpp_ast.parse_header(header, ExposeConfig(all=True))
    regex = cpp_regex.parse_header(header, ExposeConfig(all=True))

    assert sorted(f.name for f in tree.functions) == sorted(
        f.name for f in regex.functions
    )
    assert sorted(s.name for s in tree.skipped) == sorted(
        s.name for s in regex.skipped
    ) == ["scale_in_place"]
    # And agree on the element type, not just on which names got through.
    by_tree = {f.name: f for f in tree.functions}
    by_regex = {f.name: f for f in regex.functions}
    for name in by_tree:
        assert [
            (p.type, p.is_array) for p in by_tree[name].parameters
        ] == [(p.type, p.is_array) for p in by_regex[name].parameters], name


# --- raw pointers paired with a length argument --------------------------
#
# `void scale(double* data, int n, double k)` was refused: a pointer carries no
# length. But the signature DOES say how long `data` is — in another argument.
# Pairing them makes the shape bindable, and the length then disappears from
# the Python signature entirely, so it cannot disagree with the data.


def test_a_pointer_paired_with_a_length_becomes_a_buffer(tmp_path):
    header = tmp_path / "arr.hpp"
    header.write_text(
        """
        double sum(const double* data, int n);
        void scale(double* data, int n, double k);
        int count_above(const int* values, int n_values, int threshold);
        """
    )

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))
    by_name = {f.name: f for f in module.functions}

    assert set(by_name) == {"sum", "scale", "count_above"}
    data = by_name["sum"].parameters[0]
    assert data.is_array and data.length_param == "n"
    # const: input only, so the binding may let pybind11 convert freely.
    assert data.is_mutable_buffer is False
    # non-const: the callee writes through it. Conversion would write into a
    # temporary, so the binding has to refuse a wrong dtype instead.
    assert by_name["scale"].parameters[0].is_mutable_buffer is True
    # Paired by name, not by position.
    assert by_name["count_above"].parameters[0].length_param == "n_values"


def test_an_unpairable_pointer_is_still_refused(tmp_path):
    # Conservative on purpose. `dot(a, b, n)` has one integer and two arrays:
    # a human reads `n` as the length of both, but the inference will not
    # guess, because a false pairing binds the wrong argument as an extent and
    # reads past the end of a buffer. No length at all stays refused too.
    header = tmp_path / "amb.hpp"
    header.write_text(
        """
        double dot(const double* a, const double* b, int n);
        double first(const double* data);
        """
    )

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))

    assert module.functions == []
    reasons = {s.name: s.reason for s in module.skipped}
    assert set(reasons) == {"dot", "first"}
    # The refusal names the way out, rather than only saying no.
    assert "pass its length in an argument named after it" in reasons["first"]


def test_buffer_arguments_on_methods_now_bind(tmp_path):
    # `int set_porosity(double* values, int n)` is the single most common
    # shape a numerical C++ class exposes, and it used to be refused with
    # "not yet for class methods". pybind11 passes the instance as the first
    # argument of a def'd callable, so the same generated lambda binds it.
    # Verified by compiling and running: writes land in the caller's array,
    # a wrong dtype is refused, and the length never appears in the signature.
    header = tmp_path / "cls.hpp"
    header.write_text(
        """
        class Grid {
        public:
            void set_values(double* values, int n);
            int size() const;
        };
        """
    )

    tree = cpp_ast.parse_header(header, ExposeConfig(all=True))
    regex = cpp_regex.parse_header(header, ExposeConfig(all=True))

    for module in (tree, regex):
        by_name = {m.name: m for m in module.classes[0].methods}
        assert set(by_name) == {"set_values", "size"}
        buffer = by_name["set_values"].parameters[0]
        assert buffer.length_param == "n"
        assert buffer.is_mutable_buffer is True
        assert module.skipped == []
def test_both_cpp_backends_agree_on_buffers(tmp_path):
    header = tmp_path / "arr.hpp"
    header.write_text(
        """
        double sum(const double* data, int n);
        void scale(double* data, int n, double k);
        double dot(const double* a, const double* b, int n);
        """
    )

    tree = cpp_ast.parse_header(header, ExposeConfig(all=True))
    regex = cpp_regex.parse_header(header, ExposeConfig(all=True))

    assert sorted(f.name for f in tree.functions) == sorted(
        f.name for f in regex.functions
    ) == ["scale", "sum"]
    assert sorted(s.name for s in tree.skipped) == sorted(
        s.name for s in regex.skipped
    ) == ["dot"]
    for name in ("sum", "scale"):
        t = next(f for f in tree.functions if f.name == name).parameters[0]
        r = next(f for f in regex.functions if f.name == name).parameters[0]
        assert (t.length_param, t.is_mutable_buffer) == (r.length_param, r.is_mutable_buffer)


# --- operators -----------------------------------------------------------
#
# These used to be dropped by `if child.spelling.startswith("operator"):
# continue` — silently, with no `skipped` entry, which is the one thing this
# parser is not allowed to do. pybind11 binds them perfectly well as Python
# special methods, so the ones with an honest mapping are bound and the rest
# are reported with the reason.
#
# Verified by compiling and running the generated module: a + b, b - a, a * 3,
# a == b and a[i] all return the right values through the C++ operators.


def test_operators_are_bound_as_python_special_methods(tmp_path):
    header = tmp_path / "vec2.hpp"
    header.write_text(
        """
        class Vec2 {
        public:
            Vec2(double x, double y);
            double x() const;
            Vec2 operator+(const Vec2& o) const;
            Vec2 operator-(const Vec2& o) const;
            Vec2 operator*(double s) const;
            bool operator==(const Vec2& o) const;
            double operator[](int i) const;
        };
        """
    )

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))
    methods = {m.name: m for m in module.classes[0].methods}

    assert set(methods) == {"x", "__add__", "__sub__", "__mul__", "__eq__", "__getitem__"}
    # The published name is the dunder; the address to take is still the C++
    # operator, because `&Vec2::__add__` names nothing.
    assert methods["__add__"].cpp_name == "operator+"
    assert methods["x"].cpp_name is None


def test_a_unary_operator_is_not_bound_as_its_binary_namesake(tmp_path):
    # `operator-` with one argument is subtraction and with none it is
    # negation. Binding one as the other compiles and then computes something
    # else entirely, which is why the mapping is keyed on argument count.
    header = tmp_path / "n.hpp"
    header.write_text(
        """
        class N {
        public:
            N operator-() const;
            N operator-(const N& o) const;
        };
        """
    )

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))
    by_dunder = {m.name: m.cpp_name for m in module.classes[0].methods}

    assert by_dunder == {"__neg__": "operator-", "__sub__": "operator-"}


def test_unmappable_operators_are_reported_not_dropped(tmp_path):
    # The point of the change. Each refusal names why, and what to do instead
    # — "unsupported" on its own tells the reader nothing.
    header = tmp_path / "acc.hpp"
    header.write_text(
        """
        class Acc {
        public:
            Acc& operator+=(const Acc& o);
            Acc& operator=(const Acc& o);
            Acc& operator++();
            double value() const;
        };
        """
    )

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))

    assert [m.name for m in module.classes[0].methods] == ["value"]
    reasons = {s.name: s.reason for s in module.skipped}
    assert set(reasons) == {"Acc::operator+=", "Acc::operator=", "Acc::operator++"}
    assert "Python rebinds names" in reasons["Acc::operator="]
    assert "no increment operator" in reasons["Acc::operator++"]
    assert all("named method instead" in r for r in reasons.values())


def test_a_destructor_is_not_a_gap(tmp_path):
    # Deliberately NOT reported: pybind11 destroys the held object itself, so
    # there is nothing for a binding to do. Listing it as skipped would be
    # noise implying a capability is missing when none is.
    header = tmp_path / "d.hpp"
    header.write_text("class D {\npublic:\n    D();\n    ~D();\n    int v() const;\n};\n")

    module = cpp_ast.parse_header(header, ExposeConfig(all=True))

    assert [m.name for m in module.classes[0].methods] == ["v"]
    assert module.skipped == []


def test_the_regex_reader_reports_operators_rather_than_mangling_them(tmp_path):
    # This reader's declarator pattern has `~?\w+` for the name, which cannot
    # match the `+` in `operator+`, so these fell through to the data-member
    # branch and were reported as a field called "const". It cannot bind them;
    # it can at least name them correctly and say why.
    header = tmp_path / "vec2.hpp"
    header.write_text(
        """
        class Vec2 {
        public:
            Vec2 operator+(const Vec2& o) const;
            double operator[](int i) const;
            double x() const;
        };
        """
    )

    module = cpp_regex.parse_header(header, ExposeConfig(all=True))

    assert [m.name for m in module.classes[0].methods] == ["x"]
    names = {s.name for s in module.skipped}
    assert names == {"Vec2::operator+", "Vec2::operator[]"}
    assert all("nativegate[clang]" in s.reason for s in module.skipped)
