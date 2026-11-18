"""Shared edits preserve native algorithms and distinguish unavailable validation."""
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.capabilities import SOURCE_EDITS
from langadapter.sources import SOURCE_STATUSES, SourceResult, SourceValidation


EDIT_ARGUMENTS = {
    "prune_tests": (("test_example",),),
    "merge_sources": ("second",),
}
EDIT_KEYWORDS = {"prune_collection_breakers": {"timeout_s": 2}}
EXECUTION_EDITS = tuple(name for name in SOURCE_EDITS if name not in {"prune_tests", "finalize_source"})


def invoke(group, name, source="input"):
    return getattr(group, name)(source, *EDIT_ARGUMENTS.get(name, ()), **EDIT_KEYWORDS.get(name, {}))


class EditAdapter(lb.LangAdapter):
    language = "edit-fixture"
    file_extensions = (".edit-fixture",)
    source_merge_separator = "--"


@pytest.mark.parametrize("name", SOURCE_EDITS)
def test_edits_forward_native_arguments_and_snapshot_results(tmp_path, name):
    calls = []
    removed = ["test_example"]
    native = SourceResult("applied", "changed", "native-parser", removed=removed)
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    setattr(adapter, name, lambda *args, **kwargs: calls.append((args, kwargs)) or native)
    result = invoke(adapter.sources, name)
    expected_keywords = dict(EDIT_KEYWORDS.get(name, {}))
    if name == "merge_sources":
        expected_keywords["separator"] = "--"
    assert calls == [(("input", *EDIT_ARGUMENTS.get(name, ())), expected_keywords)]
    assert result == native and result is not native
    removed.append("another")
    assert result.removed == ["test_example"]
    assert adapter.sources.supports(name)


@pytest.mark.parametrize("language", ["python", "java"])
@pytest.mark.parametrize("name", EXECUTION_EDITS)
def test_runtime_edits_need_setup_without_constructing_native_facade(tmp_path, monkeypatch, language, name):
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path)))

    def forbidden(*args, **kwargs):
        pytest.fail("missing-runtime edit instantiated or launched the native operation")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    # Both languages' source operations are their adapter's methods.
    monkeypatch.setattr(adapter, name, forbidden)
    result = invoke(adapter.sources, name)
    assert result.status == "missing_setup" and result.source == "input"
    assert result.parser == "" and result.detail
    entry = next(item for item in adapter.operation_capabilities() if item.name == f"sources.{name}")
    assert entry.supported and entry.readiness is lb.Availability.MISSING_SETUP


@pytest.mark.parametrize("language, source, test", [
    ("python", "def test_example():\n    assert True\n", "test_example"),
    ("java", "class Example { @Test void testExample() { assert true; } }", "Example.testExample"),
])
def test_pure_edits_match_native_values_without_runtime(tmp_path, monkeypatch, language, source, test):
    def forbidden(*args, **kwargs):
        pytest.fail("parser-only edit launched a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path)))
    assert adapter.sources.prune_tests(source, [test]) == adapter.prune_tests(source, [test])
    assert adapter.sources.prune_tests(source, [test]).status == "applied"
    assert adapter.sources.finalize_source(source) == adapter.finalize_source(source)
    assert adapter.sources.prune_tests("class {;", [test]).status == "parse_error"
    for name in ("prune_tests", "finalize_source"):
        entry = next(item for item in adapter.operation_capabilities() if item.name == f"sources.{name}")
        assert entry.supported and not entry.execution_backed and entry.readiness is None


@pytest.mark.parametrize("status", SOURCE_STATUSES)
def test_native_edit_statuses_are_not_reinterpreted(tmp_path, status):
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path)))
    adapter.finalize_source = lambda source: SourceResult(status, source, "native", detail="native detail")
    result = adapter.sources.finalize_source("input")
    assert result.status == status and result.source == "input" and result.detail == "native detail"


@pytest.mark.parametrize("error, status", [
    (TimeoutError("validator expired"), "timeout"),
    (subprocess.TimeoutExpired("validator", 2), "timeout"),
    (ModuleNotFoundError("parser absent"), "missing_setup"),
    (OSError("could not launch"), "infrastructure_failure"),
])
@pytest.mark.parametrize("metadata", [False, True])
def test_failed_edit_or_merge_metadata_keeps_input(tmp_path, error, status, metadata):
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))

    def failed(*args, **kwargs):
        raise error

    if metadata:
        class MetadataFailure(EditAdapter):
            source_merge_separator = property(failed)
        adapter = MetadataFailure(adapter.context)
        adapter.merge_sources = lambda *args, **kwargs: pytest.fail("metadata failure still merged")
        result = adapter.sources.merge_sources("input", "second")
    else:
        adapter.normalize_shadowed = failed
        result = adapter.sources.normalize_shadowed("input")
    assert result.status == status and result.source == "input"
    assert result.parser == "" and result.detail


@pytest.mark.parametrize("bad", [None, "wrong", SourceResult("unknown", "changed", "parser"),
                                 SourceResult("applied", None, "parser"),
                                 SourceResult("applied", "changed", None)])
def test_malformed_native_edit_never_returns_success(tmp_path, bad):
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path)))
    adapter.finalize_source = lambda source: bad
    result = adapter.sources.finalize_source("input")
    assert result.status == "infrastructure_failure" and result.source == "input" and result.detail


@pytest.mark.parametrize("tests", [None, "test_example", b"test_example", [None], [1], [" "],
                                   [lb.TestId("test.py", 1)]])
def test_malformed_prune_request_is_refused_even_when_unsupported(tmp_path, tests):
    with pytest.raises(lb.InvalidRequest, match="tests"):
        EditAdapter(lb.ProjectContext(str(tmp_path))).sources.prune_tests("input", tests)


def test_prune_snapshots_generator_and_preserves_test_id(tmp_path):
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path)))
    test = lb.TestId("test.py", "test_example")
    calls = []
    adapter.prune_tests = lambda source, tests: calls.append(tests) or SourceResult("unchanged", source, "native")
    adapter.sources.prune_tests("input", (item for item in (test, "test_other")))
    assert calls == [(test, "test_other")]


@pytest.mark.parametrize("separator", [1, "-- one\n-- two", "-- one\r-- two", "# wrong"])
def test_separator_request_is_refused_before_native_edit(tmp_path, separator):
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    adapter.merge_sources = lambda *args, **kwargs: pytest.fail("invalid separator reached native merge")
    with pytest.raises(lb.InvalidRequest, match="separator"):
        adapter.sources.merge_sources("input", "second", separator=separator)


@pytest.mark.parametrize("prefix", [1, "", "--\n"])
def test_malformed_language_separator_metadata_is_failure(tmp_path, prefix):
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    adapter.source_merge_separator = prefix
    adapter.merge_sources = lambda *args, **kwargs: pytest.fail("invalid metadata reached native merge")
    result = adapter.sources.merge_sources("input", "second")
    assert result.status == "infrastructure_failure" and result.source == "input"


def test_external_adapter_can_supply_explicit_separator_without_metadata(tmp_path):
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    adapter.source_merge_separator = None
    calls = []
    adapter.merge_sources = lambda first, second, **kwargs: calls.append(kwargs) or SourceResult("applied", second, "native")
    with pytest.raises(lb.InvalidRequest, match="explicit comment separator"):
        adapter.sources.merge_sources("input", "second")
    assert adapter.sources.merge_sources("input", "second", separator="; chosen").applied
    assert calls == [{"separator": "; chosen"}]


def test_unsupported_execution_route_does_not_access_metadata(tmp_path):
    adapter = EditAdapter(lb.ProjectContext("/project", lb.Interpreter(path="/runtime"),
                          execution=lb.ContainerExecution("not-started", str(tmp_path))))
    adapter.source_execution_kinds = ("local",)
    adapter.source_merge_separator = None
    adapter._sources = lambda: pytest.fail("unsupported route looked up metadata")
    adapter.merge_sources = lambda *args, **kwargs: pytest.fail("unsupported route executed")
    result = adapter.sources.merge_sources("input", "second")
    assert result.status == "unsupported" and result.source == "input"


def test_cancellation_and_native_request_errors_are_not_hidden(tmp_path):
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path)))
    for error in (KeyboardInterrupt(), lb.InvalidRequest("native", "rejected")):
        def failed(source):
            raise error
        adapter.finalize_source = failed
        with pytest.raises(type(error)):
            adapter.sources.finalize_source("input")


def test_real_python_merge_uses_native_delimiter_and_validation(tmp_path):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    first = "def test_first():\n    assert True\n"
    second = "def test_second():\n    assert True\n"
    result = adapter.sources.merge_sources(first, second)
    assert result == adapter.merge_sources(first, second, separator="#")
    assert result.applied and "test_first" in result.source and "test_second" in result.source
    assert adapter.sources.normalize_shadowed(first) == adapter.normalize_shadowed(first)


def test_java_merge_uses_native_delimiter_and_algorithm(tmp_path, monkeypatch):
    from langadapter.java._sources import JavaSources

    calls = []
    def validate(self, sources):
        calls.append(tuple(sources))
        return [SourceValidation("valid", "", 0, 0, "fixture-javac") for _ in sources]

    monkeypatch.setattr(JavaSources, "_validate_sources", validate)
    adapter = lb.create("java", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    first = "class First { @Test void testFirst() { assert true; } }"
    second = "class Second { @Test void testSecond() { assert true; } }"
    result = adapter.sources.merge_sources(first, second)
    assert result == adapter.merge_sources(first, second, separator="//")
    assert result.applied and "testFirst" in result.source and "testSecond" in result.source
    assert calls[0] == calls[1] and len(calls[0]) == 3


@pytest.mark.parametrize("timeout_s", [10**400, float("inf"), 0, -1, True, "2"])
def test_a_prune_timeout_that_is_no_positive_finite_number_is_refused(tmp_path, timeout_s):
    """An integer too large for a float is refused as the other runs refuse
    it, not raised as an OverflowError."""
    adapter = EditAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    adapter.prune_collection_breakers = lambda *args, **kwargs: pytest.fail("the edit ran")
    with pytest.raises(lb.InvalidRequest, match="timeout_s: must be a positive finite number"):
        adapter.sources.prune_collection_breakers("input", timeout_s=timeout_s)
