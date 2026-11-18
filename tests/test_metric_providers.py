"""Metric composition preserves scope, parser, selection and failure evidence."""
from dataclasses import FrozenInstanceError
from itertools import permutations

import pytest

import langadapter as lb
from langadapter.sources import STANDARD_METRICS


SOURCE = "\n".join("source" for _ in range(20))


class Provider:
    def __init__(self, name, definitions, *, parser=None, scopes=None, failure=None):
        self.name = name
        self.items = list(definitions)
        self.parser = parser or name + "-parser"
        self.scopes = scopes
        self.failure = failure
        self.calls = []

    def definitions(self):
        return self.items

    def measure(self, source, *, language, parser):
        self.calls.append((source, language, parser))
        if isinstance(self.failure, BaseException):
            raise self.failure
        if self.failure:
            return lb.SourceObservation(self.failure, parser=self.parser, reason="provider detail")
        scopes = self.scopes
        if scopes is None:
            values = tuple(lb.MetricValue(item, self.name, self.parser, "completed", 0)
                           for item in self.items)
            scopes = (lb.MetricScope("file", "", None, None, None, values),)
        return lb.SourceObservation("completed", scopes, parser="envelope-context")


@pytest.fixture
def sources(tmp_path):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path)))
    adapter.language = "partial"
    adapter.structural_parser_identity = lambda: "host-context"
    return adapter.sources


def definition(name, scopes=("file",)):
    return lb.MetricDefinition(name, "count", "1", scopes)


def function(provider, definition, *, start=2, end=5, signature="(int)", name="C.f", value=1):
    metric = lb.MetricValue(definition, provider, provider + "-parser", "completed", value)
    return lb.MetricScope("function", name, start, end, signature, (metric,))


def test_two_providers_keep_actual_parser_and_definition_provenance(sources):
    first = Provider("first", (definition("a"),))
    second = Provider("second", (definition("b"),))
    sources.register_metric_provider(first)
    sources.register_metric_provider(second)
    result = sources.metrics(SOURCE)
    assert result.status == "completed"
    assert result.parser is None
    assert [(value.definition.name, value.provider, value.parser, value.value)
            for value in result.value.file.metrics] == [
                ("a", "first", "first-parser", 0), ("b", "second", "second-parser", 0)]
    assert first.calls == second.calls == [(SOURCE, "partial", "host-context")]
    assert sources.adapter.operation_capabilities()[0].name.startswith("sources.")
    assert next(item for item in sources.adapter.operation_capabilities()
                if item.name == "sources.metrics").supported


def test_empty_and_unknown_selection_never_call_provider_or_context_parser(sources):
    provider = Provider("a", (definition("a"),))
    sources.register_metric_provider(provider)
    sources.adapter.structural_parser_identity = lambda: pytest.fail("empty selection parsed")
    result = sources.metrics(SOURCE, names=[])
    assert result.status == "completed"
    assert not result.value.file.metrics and not result.value.functions and not result.value.unavailable
    assert not provider.calls
    with pytest.raises(lb.InvalidRequest, match="unknown"):
        sources.metrics(SOURCE, names=["unknown"])
    assert not provider.calls


def test_a_standard_metric_no_provider_measures_is_unavailable_and_a_misspelt_one_refused(sources):
    provider = Provider("a", (definition("a"),))
    nothing = sources.metrics(SOURCE, names=["nloc"])
    assert [(item.definition.name, item.status, item.value) for item in nothing.value.unavailable] == [
        ("nloc", "unsupported", None)]
    sources.register_metric_provider(provider)
    assert sources.metric_definitions() == (definition("a"),)
    result = sources.metrics(SOURCE, names=["loop_count", "a"])
    assert [item.definition.name for item in result.value.file.metrics] == ["a"]
    missing, = result.value.unavailable
    assert (missing.definition, missing.provider, missing.status, missing.value) == (
        lb.MetricDefinition("loop_count", "count", "standard-metrics-1", ("file", "function")),
        "langadapter.standard", "unsupported", None)
    assert missing.reason == "the partial adapter's metrics operation does not report loop_count"
    called = len(provider.calls)
    sources.metrics(SOURCE, names=["loop_count"])
    assert len(provider.calls) == called
    with pytest.raises(lb.InvalidRequest, match="loop_nesting"):
        sources.metrics(SOURCE, names=["loop_nesting"])


@pytest.mark.parametrize("item", [lb.MetricDefinition("nloc", "count", "1", ("file",)),
                                  lb.MetricDefinition("cyclomatic_complexity", "count", "1", ("file",)),
                                  lb.MetricDefinition("call_site_count_estimate", "count", "1", ("file",))])
def test_a_provider_of_a_standard_name_keeps_its_unit_scopes_and_operation(sources, item):
    with pytest.raises(lb.InvalidRequest, match=f"standard metric {item.name} is reported by "
                                                f"{STANDARD_METRICS[item.name].operation}"):
        sources.register_metric_provider(Provider("standard", (item,)))
    assert not sources.metric_definitions()
    kept = lb.MetricDefinition("nloc", "lines", "1", ("file",))
    sources.register_metric_provider(Provider("standard", (kept,)))
    assert sources.metric_definitions() == (kept,)


class LongLines:
    """A language package's own metric: the lines longer than 80 characters."""

    name = "long-lines"
    definition = lb.MetricDefinition("long_lines", "lines", "1", ("file",))

    def definitions(self):
        return (self.definition,)

    def measure(self, source, *, language, parser):
        count = sum(len(line) > 80 for line in source.splitlines())
        return lb.SourceObservation("completed", (lb.MetricScope("file", "", None, None, None, (
            lb.MetricValue(self.definition, self.name, None, "completed", count),)),))


def test_a_language_packages_own_metric_is_on_every_adapter_of_the_language(tmp_path):
    from langadapter import hooks
    from langadapter.python.adapter import PythonAdapter

    class Measured(PythonAdapter):
        def metric_providers(self):
            return (LongLines(),)

    context = lb.ProjectContext(str(tmp_path))
    source = "x = 1\n" + "#" * 90 + "\n"
    for adapter in (Measured(context), Measured(context)):
        assert LongLines.definition in adapter.sources.metric_definitions()
        assert [(item.definition.name, item.value)
                for item in adapter.sources.metrics(source, names=["long_lines"]).value.file.metrics] == [
                    ("long_lines", 1)]
    other = lb.create("python", context)
    assert LongLines.definition not in other.sources.metric_definitions()
    with pytest.raises(lb.InvalidRequest, match="long_lines"):
        other.sources.metrics(source, names=["long_lines"])
    entry = hooks.hook("metric_providers")
    assert (entry.family, hooks.hook("scoped_metrics").title) == ("metric providers", "Metrics per function")
    assert hooks._present(Measured, entry) and not hooks._present(PythonAdapter, entry)


def test_a_language_provider_of_a_registered_name_is_refused(tmp_path):
    from langadapter.python.adapter import PythonAdapter

    class Twice(PythonAdapter):
        def metric_providers(self):
            return (Provider("again", (lb.MetricDefinition("nloc", "lines", "1", ("file",)),)),)

    with pytest.raises(lb.InvalidRequest, match="already registered: nloc"):
        Twice(lb.ProjectContext(str(tmp_path))).sources


def test_selection_deduplicates_names_filters_bundles_and_skips_unrequested_provider(sources):
    first = Provider("first", (definition("b"), definition("a")))
    unrequested = Provider("unused", (definition("unused"),), failure=AssertionError("not requested"))
    sources.register_metric_provider(first)
    sources.register_metric_provider(unrequested)
    result = sources.metrics(SOURCE, names=["b", "b"])
    assert [metric.definition.name for metric in result.value.file.metrics] == ["b"]
    assert len(first.calls) == 1 and not unrequested.calls


def test_partial_replacement_retains_other_original_names(sources):
    first = Provider("first", (definition("a"), definition("b")))
    replacement = Provider("replacement", (definition("a"),))
    sources.register_metric_provider(first)
    with pytest.raises(lb.InvalidRequest, match="already registered"):
        sources.register_metric_provider(replacement)
    sources.register_metric_provider(replacement, replace=True)
    result = sources.metrics(SOURCE)
    assert [(value.definition.name, value.provider) for value in result.value.file.metrics] == [
        ("a", "replacement"), ("b", "first")]


@pytest.mark.parametrize("status", ["parse_error", "timeout", "unsupported", "missing_setup",
                                   "infrastructure_failure", "refused"])
def test_failed_function_only_provider_has_unscoped_missing_values_and_keeps_success(sources, status):
    good = Provider("good", (definition("good"),))
    bad = Provider("bad", (definition("bad", ("function",)),), failure=status)
    sources.register_metric_provider(good)
    sources.register_metric_provider(bad)
    result = sources.metrics(SOURCE)
    assert result.status == "completed"
    assert result.value.file.metrics[0].value == 0
    assert result.value.functions == ()
    missing, = result.value.unavailable
    assert missing.value is None and missing.status == status
    assert missing.provider == "bad" and missing.parser == "bad-parser"
    assert missing.reason == "provider detail"


@pytest.mark.parametrize("failure,status", [(TimeoutError("slow"), "timeout"),
                                          (OSError("broken"), "infrastructure_failure"),
                                          (lb.InvalidRequest("result", "bad"), "infrastructure_failure")])
def test_provider_exceptions_are_failed_collection_not_caller_request_errors(sources, failure, status):
    provider = Provider("bad", (definition("bad"),), failure=failure)
    sources.register_metric_provider(provider)
    result = sources.metrics(SOURCE)
    assert not result.value.file.metrics
    assert result.value.unavailable[0].status == status
    assert result.value.unavailable[0].value is None


def test_repeated_definitions_and_native_overloads_keep_full_scope_keys(sources):
    item = definition("complexity", ("function",))
    scopes = (function("native", item, start=12, end=15),
              function("native", item),
              function("native", item, signature="(str)"))
    provider = Provider("native", (item,), scopes=scopes)
    sources.register_metric_provider(provider)
    result = sources.metrics(SOURCE)
    assert len(result.value.functions) == 3
    assert [scope.start_line for scope in result.value.functions] == [2, 2, 12]
    assert {scope.signature for scope in result.value.functions} == {"(int)", "(str)"}


@pytest.mark.parametrize("difference", ["span", "signature", "start"])
def test_conflicting_function_occurrences_refuse_both_providers_in_either_order(tmp_path, difference):
    a, b = definition("a", ("function",)), definition("b", ("function",))
    first = Provider("first", (a,), scopes=(function("first", a),))
    options = {"span": {"end": 6}, "signature": {"signature": "(str)"}, "start": {"start": 3}}
    second = Provider("second", (b,), scopes=(function("second", b, **options[difference]),))
    for providers in permutations((first, second)):
        adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path)))
        adapter.language = "partial"
        for provider in providers:
            adapter.sources.register_metric_provider(provider)
        adapter.sources.register_metric_provider(Provider("good", (definition("good"),)))
        result = adapter.sources.metrics(SOURCE)
        assert not result.value.functions
        assert [metric.definition.name for metric in result.value.unavailable] == ["a", "b"]
        assert all(metric.status == "refused" for metric in result.value.unavailable)
        assert result.value.file.metrics[0].definition.name == "good"


def test_matching_overload_sets_compose_without_conflict(sources):
    for name in ("a", "b"):
        item = definition(name, ("function",))
        provider = Provider(name, (item,), scopes=tuple(
            function(name, item, signature=signature) for signature in ("(int)", "(str)")))
        sources.register_metric_provider(provider)
    result = sources.metrics(SOURCE)
    assert len(result.value.functions) == 2 and not result.value.unavailable
    assert all(len(scope.metrics) == 2 for scope in result.value.functions)


def test_unrequested_function_bundle_does_not_create_false_scope_conflicts(sources):
    for name, end in (("a", 5), ("b", 6)):
        file_item = definition(name)
        function_item = definition(name + ".function", ("function",))
        file = lb.MetricScope("file", "", None, None, None,
                              (lb.MetricValue(file_item, name, "own-parser", "completed", 1),))
        provider = Provider(name, (file_item, function_item), scopes=(
            file, function(name, function_item, end=end)))
        sources.register_metric_provider(provider)
    result = sources.metrics(SOURCE, names=("a", "b"))
    assert [metric.definition.name for metric in result.value.file.metrics] == ["a", "b"]
    assert not result.value.functions and not result.value.unavailable


@pytest.mark.parametrize("malformation", ["span", "missing", "definition", "provider", "duplicate"])
def test_malformed_result_becomes_unavailable_not_omitted(sources, malformation):
    item = definition("a", ("file", "function"))
    provider = Provider("owner", (item,))
    if malformation == "span":
        provider.scopes = (function("owner", item, end=21),)
    elif malformation == "missing":
        provider.scopes = ()
    elif malformation == "definition":
        other = lb.MetricDefinition("a", "different", "1", ("file",))
        provider.scopes = (lb.MetricScope("file", "", None, None, None,
                                         (lb.MetricValue(other, "owner", "own", "completed", 1),)),)
    elif malformation == "provider":
        provider.scopes = (function("other", item),)
    else:
        scope = function("owner", item)
        provider.scopes = (scope, scope)
    sources.register_metric_provider(provider)
    result = sources.metrics(SOURCE)
    assert not result.value.file.metrics and not result.value.functions
    assert result.value.unavailable[0].status == "infrastructure_failure"


def test_empty_function_applicability_is_not_failed_measurement(sources):
    provider = Provider("empty", (definition("a", ("function",)),), scopes=())
    sources.register_metric_provider(provider)
    result = sources.metrics("")
    assert result.status == "completed"
    assert not result.value.functions and not result.value.unavailable


@pytest.mark.parametrize("number", [None, True, float("nan"), float("inf"), float("-inf")])
def test_completed_values_require_real_finite_numbers(number):
    with pytest.raises(lb.InvalidRequest, match="finite"):
        lb.MetricValue(definition("a"), "p", "parser", "completed", number)


def test_record_snapshots_caller_lists_and_is_immutable(sources):
    scopes = ["file"]
    item = definition("a", scopes)
    scopes.append("function")
    assert item.scopes == ("file",)
    provider = Provider("p", (item,))
    sources.register_metric_provider(provider)
    result = sources.metrics(SOURCE).value
    provider.items.clear()
    assert result.file.metrics[0].definition == item
    with pytest.raises(FrozenInstanceError):
        result.language = "changed"
    assert isinstance(result.file.metrics, tuple) and isinstance(result.functions, tuple)


@pytest.mark.parametrize("malformed", [
    lambda: lb.MetricDefinition("a", "count", "1", None),
    lambda: lb.MetricDefinition("a", "count", "1", [[]]),
    lambda: lb.MetricScope("file", "", None, None, None, None),
    lambda: lb.MetricScope("function", "f", True, 2, None, ()),
    lambda: lb.SourceMetrics("partial", lb.MetricScope("file", "", None, None, None, ()), None),
])
def test_malformed_public_records_raise_typed_request_errors(malformed):
    with pytest.raises(lb.InvalidRequest):
        malformed()


def test_unscoped_failure_cannot_conflict_with_a_scoped_observation():
    item = definition("a")
    value = lb.MetricValue(item, "p", "parser", "completed", 1)
    missing = lb.MetricValue(item, "p", None, "timeout", None)
    with pytest.raises(lb.InvalidRequest, match="conflict"):
        lb.SourceMetrics("partial", lb.MetricScope("file", "", None, None, None, (value,)), (), (missing,))


@pytest.mark.parametrize("names", ["a", 1, [1], [""], [" a "]])
def test_invalid_requested_names_are_request_errors(sources, names):
    sources.register_metric_provider(Provider("p", (definition("a"),)))
    with pytest.raises(lb.InvalidRequest):
        sources.metrics(SOURCE, names=names)


@pytest.mark.parametrize("language,source,invalid", [
    ("python", "def twice(x):\n    if x: return x * 2\n    return 0\n", "def broken("),
    ("java", "class Calc { int twice(int x) { return x > 0 ? x * 2 : 0; } }", "class Broken {"),
])
def test_builtin_file_counts_keep_flat_values_and_failures_separate(tmp_path, language, source, invalid):
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path)))
    expected = adapter.optimization_metrics(source)
    observed = adapter.sources.metrics(source)
    assert observed.status == "completed"
    assert {metric.definition.name: metric.value for metric in observed.value.file.metrics} == expected
    assert all(metric.parser == adapter.structural_parser_identity() for metric in observed.value.file.metrics)
    assert not observed.value.unavailable
    empty = adapter.sources.metrics("")
    assert all(metric.value == 0 for metric in empty.value.file.metrics)
    failed = adapter.sources.metrics(invalid)
    assert not failed.value.file.metrics
    assert len(failed.value.unavailable) == 9
    assert all(metric.status == "parse_error" and metric.value is None for metric in failed.value.unavailable)
    adapter.sources.register_metric_provider(Provider("independent", (definition("custom"),)))
    combined = adapter.sources.metrics(invalid)
    assert combined.value.file.metrics[0].definition.name == "custom"
    assert combined.value.file.metrics[0].value == 0
    assert len(combined.value.unavailable) == 9
