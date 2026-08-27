"""C++ parsing of pre-standard/legacy headers.

Exercised against libraries/petro — a 1990s-vintage C++ layer over F77:
no STL, no namespaces, manual memory, access specifiers on their own lines,
`extern "C"` bridge declarations, plain data structs.
"""

from pathlib import Path

import pytest

from nativegate.config import ExposeConfig
from nativegate.ir import ClassDef, Method, ModuleIR
from nativegate.generators import pybind_gen, python_pkg_gen
from nativegate.parsers import cpp as cpp_parser

PETRO_INCLUDE = Path(__file__).parents[3] / "libraries" / "petro" / "cpp" / "include"


def parse(source: str, tmp_path: Path, name: str = "x.hpp", expose=None):
    path = tmp_path / name
    path.write_text(source)
    return cpp_parser.parse_header(path, expose or ExposeConfig())


# --- access specifiers --------------------------------------------------


def test_access_specifier_on_own_line_is_not_a_return_type(tmp_path):
    # The previous parser matched `public:` as <returns> and raised
    # "Unsupported native type 'public:'", failing the entire header.
    module = parse(
        """
        class Widget {
        public:
            double square(double x);
        };
        """,
        tmp_path,
    )

    assert [m.name for m in module.classes[0].methods] == ["square"]


def test_private_members_are_not_bound(tmp_path):
    # Binding a private method generates C++ that does not compile, and
    # binding a private field exposes internals the class never offered.
    module = parse(
        """
        class Widget {
        public:
            double area() const;
        private:
            double helper(double x);
            double m_size;
        };
        """,
        tmp_path,
    )

    cls = module.classes[0]
    assert [m.name for m in cls.methods] == ["area"]
    assert cls.fields == []


def test_class_members_default_to_private(tmp_path):
    module = parse("class Widget { double hidden(); };", tmp_path)

    assert module.classes[0].methods == []


def test_struct_members_default_to_public(tmp_path):
    module = parse("struct Point { double x; double y; };", tmp_path)

    assert [f.name for f in module.structs[0].fields] == ["x", "y"]


# --- constructors -------------------------------------------------------


def test_constructor_with_arguments_is_captured(tmp_path):
    module = parse(
        """
        class Fluid {
        public:
            Fluid(double api, double sg, int correlation);
            double value() const;
        };
        """,
        tmp_path,
    )

    cls = module.classes[0]
    assert cls.has_default_constructor is False
    assert len(cls.constructors) == 1
    assert [p.name for p in cls.constructors[0]] == ["api", "sg", "correlation"]
    assert [p.type for p in cls.constructors[0]] == ["float", "float", "int"]
    # The constructor must not also show up as a method named "Fluid".
    assert [m.name for m in cls.methods] == ["value"]


def test_explicit_default_constructor_keeps_py_init(tmp_path):
    module = parse(
        "class Widget { public: Widget(); Widget(int n); double v(); };", tmp_path
    )

    assert module.classes[0].has_default_constructor is True


def test_destructors_and_operators_are_ignored(tmp_path):
    module = parse(
        """
        class Widget {
        public:
            ~Widget();
            Widget& operator=(const Widget& other);
            double v();
        };
        """,
        tmp_path,
    )

    assert [m.name for m in module.classes[0].methods] == ["v"]


# --- types --------------------------------------------------------------


def test_const_and_reference_qualifiers_are_stripped(tmp_path):
    module = parse(
        """
        class Widget {
        public:
            double scale(const double& factor, const int count);
        };
        """,
        tmp_path,
    )

    method = module.classes[0].methods[0]
    assert [p.type for p in method.parameters] == ["float", "int"]


def test_const_char_pointer_maps_to_str(tmp_path):
    module = parse("class W { public: const char* name() const; };", tmp_path)

    assert module.classes[0].methods[0].returns == "str"


def test_pointer_parameter_is_skipped_with_a_reason(tmp_path):
    # A bare `double*` with NO length anywhere has no safe binding. Refusing
    # it is correct — but it must be reported, not silently dropped. (With a
    # length argument, `set_values(double* values, int n)`, the pair now binds
    # as a numpy buffer instead — covered in test_cpp_ast.py.)
    module = parse(
        """
        class Grid {
        public:
            int set_values(double* values);
            int size() const;
        };
        """,
        tmp_path,
    )

    assert [m.name for m in module.classes[0].methods] == ["size"]
    assert [s.name for s in module.skipped] == ["Grid::set_values"]
    assert "pointer" in module.skipped[0].reason


def test_one_bad_signature_does_not_lose_the_rest_of_the_class(tmp_path):
    module = parse(
        """
        class W {
        public:
            double a();
            std::vector<double> b();
            double c();
        };
        """,
        tmp_path,
    )

    assert [m.name for m in module.classes[0].methods] == ["a", "c"]
    assert [s.name for s in module.skipped] == ["W::b"]


# --- structs ------------------------------------------------------------


def test_struct_returned_by_a_method_resolves(tmp_path):
    # State is *defined* after the class that returns it, so the parser needs
    # two passes to resolve it. The forward declaration is what makes this
    # legal C++ — the AST parser is a real compiler front end and rejects the
    # declaration without it, exactly as the eventual build would.
    module = parse(
        """
        struct State;
        class Fluid {
        public:
            State state_at(double p) const;
        };
        struct State { double bo; int flag; };
        """,
        tmp_path,
    )

    assert module.classes[0].methods[0].returns == "State"
    assert [f.type for f in module.structs[0].fields] == ["float", "int"]


def test_pointer_to_known_class_is_ok_as_a_parameter(tmp_path):
    module = parse(
        """
        class Fluid { public: double v(); };
        class Sim { public: void set_fluid(Fluid* fluid); };
        """,
        tmp_path,
    )

    sim = [c for c in module.classes if c.name == "Sim"][0]
    assert sim.methods[0].parameters[0].type == "Fluid"


def test_pointer_return_of_known_class_is_refused(tmp_path):
    # The default return-value policy would take ownership of a pointer C++
    # still owns; the second free crashes far from here.
    module = parse(
        """
        class Fluid { public: double v(); };
        class Sim { public: Fluid* fluid() const; };
        """,
        tmp_path,
    )

    sim = [c for c in module.classes if c.name == "Sim"][0]
    assert sim.methods == []
    assert any("ownership" in s.reason for s in module.skipped)


# --- inheritance --------------------------------------------------------


def test_public_base_class_is_recorded(tmp_path):
    # Without `py::class_<Derived, Base>` a Python caller holding a Derived
    # cannot reach anything it inherits, and the failure is invisible until
    # someone calls the missing method.
    module = parse(
        """
        class Correlation { public: double apply(double x); };
        class Standing : public Correlation { public: double bubble_point(double t); };
        """,
        tmp_path,
    )

    classes = {c.name: c for c in module.classes}
    assert classes["Standing"].bases == ["Correlation"]
    assert classes["Correlation"].bases == []
    # Inherited members belong to the base's own binding, not the derived one.
    assert [m.name for m in classes["Standing"].methods] == ["bubble_point"]


def test_private_inheritance_is_not_recorded(tmp_path):
    # The Derived* -> Base* conversion is inaccessible, so naming the base in
    # py::class_ would not compile.
    module = parse(
        """
        class Correlation { public: double apply(double x); };
        class Standing : private Correlation { public: double bubble_point(double t); };
        """,
        tmp_path,
    )

    assert {c.name: c.bases for c in module.classes}["Standing"] == []


def test_unbound_base_class_is_not_recorded(tmp_path):
    module = parse(
        "class Standing : public NotBound { public: double bubble_point(double t); };",
        tmp_path,
    )

    assert module.classes[0].bases == []


def test_pybind_emits_base_before_derived():
    # A base must be *defined* before the class deriving from it, so this
    # ordering comes from merging headers rather than from one file: headers
    # are merged in alphabetical order, which routinely puts the derived class
    # first. py::class_<Derived, Base> then fails at import — long after a
    # successful build — with "type Base is not registered".
    module = ModuleIR(name="animals", language="cpp", source_file="animals.hpp")
    module.classes = [
        ClassDef(name="Zebra", methods=[Method(name="stripes", returns="float")], bases=["Antelope"]),
        ClassDef(name="Antelope", methods=[Method(name="horns", returns="float")]),
    ]
    bindings = pybind_gen.generate_bindings(module, "animals.hpp")

    assert "py::class_<Zebra, Antelope>" in bindings
    assert bindings.index("py::class_<Antelope>") < bindings.index("py::class_<Zebra")


def test_pybind_emits_plain_classes_unchanged(tmp_path):
    module = parse("class Widget { public: double v(); };", tmp_path, name="w.hpp")
    bindings = pybind_gen.generate_bindings(module, "w.hpp")

    assert 'py::class_<Widget>(m, "Widget")' in bindings


# --- lexical ------------------------------------------------------------


def test_commented_out_declarations_are_ignored(tmp_path):
    module = parse(
        """
        class W {
        public:
            // double old_api(double x);
            /* double older_api(double x); */
            double current(double x);
        };
        """,
        tmp_path,
    )

    assert [m.name for m in module.classes[0].methods] == ["current"]


def test_inline_method_bodies_do_not_confuse_the_scanner(tmp_path):
    module = parse(
        "class W { public: double a() { return 1.0; } double b(); };", tmp_path
    )

    assert [m.name for m in module.classes[0].methods] == ["a", "b"]


def test_free_functions_inside_extern_c_are_found(tmp_path):
    module = parse(
        """
        extern "C" {
            double psi_to_bar(double psi);
            int reset(void);
        }
        """,
        tmp_path,
    )

    assert [f.name for f in module.functions] == ["psi_to_bar", "reset"]
    assert module.functions[1].parameters == []


def test_unparseable_declaration_is_reported_not_silently_dropped(tmp_path):
    # Macro-mangled bridge declarations can't be resolved without a
    # preprocessor. Returning an empty module reads as "nothing to bind".
    module = parse(
        """
        extern "C" {
            void F77_NAME(pvtini, PVTINI)(double* api, int* icorr);
        }
        """,
        tmp_path,
    )

    assert module.functions == []
    assert module.skipped, "an unreadable declaration must be reported"
    assert "macro" in module.skipped[0].reason


# --- against the real legacy headers ------------------------------------


@pytest.mark.parametrize(
    "header,record,expected_methods",
    [
        ("FluidModel.hpp", "FluidModel", ["bubble_point", "properties_at", "oil_fvf"]),
        # `add_well(WellModel*)` is deliberately absent: WellModel is only
        # forward-declared in Simulator.hpp, so it's an incomplete type and
        # pybind11 cannot bind it. See test_add_well_skipped_as_incomplete.
        ("Simulator.hpp", "Simulator", ["initialize", "advance", "cell_count"]),
        ("WellModel.hpp", "WellModel", ["inflow_rate", "solve_operating_point"]),
        ("Units.hpp", "UnitConverter", ["pressure", "set_system"]),
        ("DeckReader.hpp", "DeckReader", ["read", "nx"]),
    ],
)
def test_parses_petro_headers(header, record, expected_methods):
    module = cpp_parser.parse_header(PETRO_INCLUDE / header, ExposeConfig())

    classes = {c.name: c for c in module.classes}
    assert record in classes
    names = [m.name for m in classes[record].methods]
    for expected in expected_methods:
        assert expected in names


def test_petro_private_helpers_stay_unbound():
    module = cpp_parser.parse_header(PETRO_INCLUDE / "FluidModel.hpp", ExposeConfig())

    names = [m.name for m in module.classes[0].methods]
    assert "interpolate" not in names  # private


# --- generators ---------------------------------------------------------


def test_pybind_emits_argument_constructors_and_struct_fields(tmp_path):
    module = parse(
        """
        struct State { double bo; int flag; };
        class Fluid {
        public:
            Fluid(double api, int corr);
            State state_at(double p) const;
        };
        """,
        tmp_path,
        name="fluid.hpp",
    )
    bindings = pybind_gen.generate_bindings(module, "fluid.hpp")

    assert 'py::class_<State>(m, "State")' in bindings
    assert '.def_readwrite("bo", &State::bo)' in bindings
    assert ".def(py::init<double, int>())" in bindings
    assert '.def("state_at", &Fluid::state_at)' in bindings
    # The struct must be registered before the class that returns it.
    assert bindings.index("py::class_<State>") < bindings.index("py::class_<Fluid>")


def test_init_py_exports_structs(tmp_path):
    module = parse(
        "struct State { double bo; };\nclass Fluid { public: State s(); };",
        tmp_path,
        name="fluid.hpp",
    )
    init_py = python_pkg_gen.generate_init_py(module)

    assert "State" in init_py
    assert "Fluid" in init_py


def test_add_well_skipped_as_incomplete():
    # WellModel is forward-declared in Simulator.hpp, never defined there.
    # Binding add_well(WellModel*) used to compile into pybind11 template
    # errors about an incomplete type; it must be skipped with a message
    # naming the real cause.
    module = cpp_parser.parse_header(PETRO_INCLUDE / "Simulator.hpp", ExposeConfig())

    reasons = {s.name: s.reason for s in module.skipped}
    assert any("add_well" in name for name in reasons)
    assert any("forward-declared" in r for r in reasons.values())


def test_simulator_incomplete_types_never_reach_bindings():
    module = cpp_parser.parse_header(PETRO_INCLUDE / "Simulator.hpp", ExposeConfig())
    bindings = pybind_gen.generate_bindings(module, "Simulator.hpp")

    for skipped in ("add_well", "set_fluid"):
        assert skipped not in bindings
