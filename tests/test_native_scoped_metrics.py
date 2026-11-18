"""Native scoped metrics are pure, occurrence-aware source observations."""
from dataclasses import FrozenInstanceError

import pytest

import langadapter as lb
from langadapter.sources import STANDARD_METRICS


@pytest.fixture(params=("python", "java"))
def adapter(request, tmp_path):
    result = lb.create(request.param, lb.ProjectContext(str(tmp_path)))
    executor = result.executor if request.param == "python" else result._executor
    executor.run = lambda *args, **kwargs: pytest.fail("source metrics launched a runtime")
    return result


def counts(scope):
    return {item.definition.name: item.value for item in scope.metrics}


def test_file_values_remain_flat_and_scoped_values_have_native_provenance(adapter):
    source = ("def f(x):\n    if x: return x\n    return 0\n" if adapter.language == "python"
              else "class Calc { int f(int x) { return x > 0 ? x : 0; } }")
    result = adapter.sources.metrics(source)
    assert result.status == "completed" and not result.value.unavailable
    assert counts(result.value.file) == adapter.optimization_metrics(source)
    function, = result.value.functions
    assert counts(function)["cyclomatic_complexity"] == 2
    assert counts(function)["function_count"] == 1
    assert all(item.provider == "langadapter.structural" and
               item.parser == adapter.structural_parser_identity() for item in function.metrics)
    assert function.start_line == 1 and function.end_line == len(source.splitlines())
    with pytest.raises(FrozenInstanceError):
        function.name = "changed"


def test_the_definitions_list_what_the_metrics_operation_measures_as_the_table_defines(adapter):
    definitions = adapter.sources.metric_definitions()
    assert [item.name for item in definitions] == sorted(
        name for name, standard in STANDARD_METRICS.items() if standard.operation == "metrics")
    assert all((item.unit, item.scopes) == (STANDARD_METRICS[item.name].unit,
                                            STANDARD_METRICS[item.name].scopes) for item in definitions)
    assert [name for name, standard in STANDARD_METRICS.items()
            if standard.operation == "call_statistics"] == [
                "call_site_count_estimate", "calling_file_count_estimate"]


def test_a_call_statistic_requested_from_metrics_names_its_operation(adapter):
    source = "def f(): pass\n" if adapter.language == "python" else "class C { void f() {} }"
    result = adapter.sources.metrics(source, names=["call_site_count_estimate", "loop_count"])
    assert counts(result.value.file) == {"loop_count": 0}
    missing, = result.value.unavailable
    assert (missing.definition.name, missing.provider, missing.status) == (
        "call_site_count_estimate", "langadapter.standard", "unsupported")
    assert missing.reason == (f"the {adapter.language} adapter's metrics operation does not report "
                              "call_site_count_estimate, call_statistics does")


def test_function_only_selection_empty_applicability_and_real_zero(adapter):
    empty = adapter.sources.metrics("", names=["cyclomatic_complexity"])
    assert not empty.value.file.metrics and not empty.value.functions and not empty.value.unavailable
    source = "def f(): pass\n" if adapter.language == "python" else "class C { void f() {} }"
    result = adapter.sources.metrics(source, names=["loop_count"])
    assert counts(result.value.file) == {"loop_count": 0}
    assert counts(result.value.functions[0]) == {"loop_count": 0}


def test_python_nested_and_repeated_functions_keep_original_spans(tmp_path):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path)))
    source = ("class C:\n    def f(self):\n        def inner(x):\n"
              "            if x: return 1\n            return 0\n        return inner(1)\n"
              "    def f(self):\n        return 2\n")
    result = adapter.sources.metrics(source).value
    assert [(item.name, item.start_line, item.end_line) for item in result.functions] == [
        ("C.f", 2, 6), ("C.f", 7, 8), ("C.f.inner", 3, 5)]
    first, _, inner = result.functions
    assert counts(first)["cyclomatic_complexity"] == 1
    assert counts(first)["cyclomatic_sum"] == 3
    assert counts(first)["function_count"] == 2
    assert counts(inner)["cyclomatic_complexity"] == 2


def test_java_constructors_overloads_and_same_line_declarations_keep_signatures(tmp_path):
    adapter = lb.create("java", lb.ProjectContext(str(tmp_path)))
    source = "class C { C() {} int f(int x) { return x; } int f(String x) { return 1; } }"
    result = adapter.sources.metrics(source).value
    assert [(item.name, item.signature) for item in result.functions] == [
        ("C.C", "()"), ("C.f", "(String)"), ("C.f", "(int)")]
    assert all(item.start_line == item.end_line == 1 for item in result.functions)
    assert not result.unavailable


def test_ambiguous_java_line_identity_is_refused_without_collapsing_counts(tmp_path):
    adapter = lb.create("java", lb.ProjectContext(str(tmp_path)))
    source = "class C { void f(int x) {} void f(int y) {} }"
    result = adapter.sources.metrics(source).value
    assert not result.file.metrics and not result.functions
    assert len(result.unavailable) == 9
    assert all(item.status == "refused" and item.value is None and "ambiguous" in item.reason
               for item in result.unavailable)
    assert adapter.optimization_metrics(source)["function_count"] == 2


def test_missing_parser_is_missing_setup_not_zero_or_execution_failure(adapter, monkeypatch):
    def missing(source):
        raise ModuleNotFoundError("native source grammar is absent")

    monkeypatch.setattr(adapter, "scoped_optimization_metrics", missing)
    result = adapter.sources.metrics("", names=["loop_count"])
    assert not result.value.file.metrics and not result.value.functions
    unavailable, = result.value.unavailable
    assert unavailable.status == "missing_setup" and unavailable.value is None
    assert "native source grammar" in unavailable.reason


def test_external_file_only_adapter_keeps_its_eight_definitions(tmp_path):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path)))
    adapter.language = "external"
    adapter.optimization_metrics = lambda source: dict.fromkeys((
        "cyclomatic_sum", "loop_count", "loop_max_nesting", "recursive_function_count",
        "function_count", "class_count", "io_call_count", "nloc"), 0)
    adapter.structural_parser_identity = lambda: "external parser"
    result = adapter.sources.metrics("").value
    assert len(result.file.metrics) == 8 and not result.functions and not result.unavailable
    assert all(item.definition.scopes == ("file",) for item in result.file.metrics)


def test_the_scoped_metrics_hook_has_the_name_the_table_and_the_scaffold_give():
    """The hook table, the scaffold, the adapters and the metrics all use
    the name scoped_optimization_metrics, so the check finds the hook and an
    adapter that follows the scaffold gets per-function metrics."""
    from langadapter import hooks
    from langadapter.java.adapter import JavaAdapter
    from langadapter.sources import _StructuralProvider
    from langadapter.python.adapter import PythonAdapter

    entry = hooks.hook("scoped_metrics")
    assert hooks._present(PythonAdapter, entry) and hooks._present(JavaAdapter, entry)

    class Scaffolded:
        def scoped_optimization_metrics(self, source):
            return None

    scopes = {d.name: d.scopes for d in _StructuralProvider(Scaffolded()).definitions()}
    assert scopes["loop_count"] == ("file", "function")


def test_the_counters_lizard_counts_name_its_version_in_their_definition(adapter):
    """Lines of code and complexity are lizard's: their definitions name the
    installed lizard, the other counters the tree-sitter grammar's rules."""
    from importlib.metadata import version

    lizard = f"lizard-{version('lizard')}"
    result = adapter.sources.metrics("def f():\n    return 1\n" if adapter.language == "python"
                                     else "class C { int f() { return 1; } }")
    found = {item.definition.name: item.definition.definition_version
             for scope in (result.value.file, *result.value.functions) for item in scope.metrics}
    assert found["nloc"] == found["cyclomatic_sum"] == f"{lizard}-1"
    assert found["cyclomatic_complexity"] == f"{lizard}-own-body-1"
    assert {version for name, version in found.items()
            if name not in ("nloc", "cyclomatic_sum", "cyclomatic_complexity")} == {
        "tree-sitter-structural-1"}


def test_an_adapter_that_counts_its_own_metrics_keeps_the_tree_sitter_tags():
    """Only an adapter whose counts are lizard's (Python's and Java's declare
    ``_lizard_metrics``) has lizard's tags; another adapter's own counts
    keep the tags they had."""
    from langadapter.sources import _StructuralProvider

    class Own:
        def scoped_optimization_metrics(self, source):
            return None

    versions = {d.name: d.definition_version for d in _StructuralProvider(Own()).definitions()}
    assert versions["nloc"] == versions["cyclomatic_sum"] == "tree-sitter-structural-1"
    assert versions["cyclomatic_complexity"] == "tree-sitter-own-body-1"


def test_a_declaration_without_a_body_has_no_complexity_of_its_own(adapter):
    """The provider reports the own complexity of a declaration without a
    body (a Python stub, a Java interface method) as unsupported, with the
    reason, rather than a number lizard did not count."""
    result = adapter.sources.metrics("class P:\n    def f(self) -> int: ...\n"
                                     if adapter.language == "python"
                                     else "interface P { int f(); }")
    function, = result.value.functions
    value, = [item for item in function.metrics
              if item.definition.name == "cyclomatic_complexity"]
    assert (value.status, value.value) == ("unsupported", None)
    assert value.reason == "lizard finds no function in this declaration"
    assert counts(result.value.file)["cyclomatic_sum"] == 0
