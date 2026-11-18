"""Grouped source reads distinguish missing observations and preserve flat results."""
import dataclasses
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.capabilities import OPERATION_NAMES
from langadapter.sources import SourceObservation, SourceStatus, SourceValidation, StructureCheck


class PartialAdapter(lb.LangAdapter):
    language = "partial-fixture"
    file_extensions = (".partial-fixture",)

    def check_structure(self, source):
        return StructureCheck(source != "broken", "invalid" if source == "broken" else "",
                              1 if source == "broken" else 0, 0, "fixture-parser")

    def skeleton(self, source):
        return () if not source else ("example",)


def test_registered_source_only_adapter_has_stable_partial_group(tmp_path):
    lb.register(PartialAdapter.language, PartialAdapter.file_extensions, PartialAdapter)
    try:
        adapter = lb.create(PartialAdapter.language, lb.ProjectContext(str(tmp_path)))
        assert adapter.sources is adapter.sources
        assert adapter.sources.check_structure("broken").completed
        assert adapter.sources.check_structure("broken").value.ok is False
        assert adapter.sources.skeleton("").value == ()
        assert adapter.sources.skeleton("broken").status is SourceStatus.PARSE_ERROR
        for result in (adapter.sources.imports(""), adapter.sources.metrics(""),
                       adapter.sources.resolve_collisions([]), adapter.sources.validate_source("")):
            assert result.status is SourceStatus.UNSUPPORTED
            assert result.value is None and result.reason
    finally:
        lb.unregister(PartialAdapter.language)


@pytest.mark.parametrize("language, valid, invalid", [
    ("python", "def test_example():\n    assert True\n", "def broken(:"),
    ("java", "class Example { @Test void testExample() { assert true; } }", "class {;"),
])
def test_builtin_reads_match_flat_values_without_runtime(tmp_path, monkeypatch, language, valid, invalid):
    def no_process(*args, **kwargs):
        pytest.fail("static read launched a process")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path)))
    group = adapter.sources
    assert group.check_structure(valid).value == adapter.check_structure(valid)
    assert group.test_metrics(valid).value == adapter.test_metrics(valid)
    assert group.skeleton(valid).value == adapter.skeleton(valid)
    assert group.function_fingerprints(valid).value == tuple(sorted(adapter.function_fingerprints(valid).items()))
    assert group.imports(valid).value == adapter.imports(valid)
    assert group.duplicate_test_names(valid).value == tuple(adapter.duplicate_test_names(valid))
    assert group.same_skeleton(valid, valid).value == adapter.same_skeleton(valid, valid)
    assert group.merge_collisions(valid, valid).value == adapter.merge_collisions(valid, valid)
    assert group.is_test_file("not-a-test.txt").value is False
    for method in (group.skeleton, group.test_metrics, group.function_fingerprints, group.imports):
        result = method(invalid)
        assert result.status is SourceStatus.PARSE_ERROR
        assert result.value is None
    assert group.check_structure(invalid).completed
    assert group.check_structure(invalid).value.ok is False
    assert group.validate_source(valid).status is SourceStatus.MISSING_SETUP


@pytest.mark.parametrize("verdict, expected", [
    (SourceValidation("valid", "", 0, 0, "runtime"), SourceStatus.COMPLETED),
    (SourceValidation("syntax_error", "bad syntax", 2, 3, "runtime"), SourceStatus.COMPLETED),
    (SourceValidation("timeout", "wait expired", 0, 0, "runtime"), SourceStatus.TIMEOUT),
    (SourceValidation("infrastructure_failure", "could not start", 0, 0, "runtime"),
     SourceStatus.INFRASTRUCTURE_FAILURE),
])
def test_validator_judgment_is_not_confused_with_failed_execution(tmp_path, verdict, expected):
    class ValidatorAdapter(PartialAdapter):
        def validate_source(self, source):
            return verdict

    adapter = ValidatorAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    result = adapter.sources.validate_source("source")
    assert result.status is expected
    if expected is SourceStatus.COMPLETED:
        assert result.value == verdict
        assert result.parser == "runtime"
    else:
        assert result.value is None
        assert result.reason == verdict.message


@pytest.mark.parametrize("exception, expected", [
    (OSError("filesystem failed"), SourceStatus.INFRASTRUCTURE_FAILURE),
    (TimeoutError("wait expired"), SourceStatus.TIMEOUT),
    (ModuleNotFoundError("parser absent"), SourceStatus.MISSING_SETUP),
])
def test_failed_static_collection_has_no_empty_success(tmp_path, exception, expected):
    class FailedAdapter(PartialAdapter):
        def check_structure(self, source):
            raise exception

    result = FailedAdapter(lb.ProjectContext(str(tmp_path))).sources.skeleton("")
    assert result.status is expected
    assert result.value is None and result.reason


def test_malformed_request_remains_a_request_error(tmp_path):
    adapter = PartialAdapter(lb.ProjectContext(str(tmp_path)))
    with pytest.raises(lb.InvalidRequest, match="must be text"):
        adapter.sources.check_structure(None)


def test_unsupported_edits_preserve_source_and_remain_callable(tmp_path):
    group = PartialAdapter(lb.ProjectContext(str(tmp_path))).sources
    source = "keep this input"
    results = (group.prune_tests(source, ["example"]), group.merge_sources(source, "second"),
               group.normalize_shadowed(source), group.salvage_syntax(source),
               group.prune_unresolvable_imports(source),
               group.prune_collection_breakers(source, timeout_s=1),
               group.strip_module_level_skips(source), group.finalize_source(source))
    assert all(result.status == "unsupported" and result.source == source for result in results)
    assert all(result.detail for result in results)
    with pytest.raises(lb.InvalidRequest):
        group.prune_tests(source, [""])
    with pytest.raises(lb.InvalidRequest):
        group.prune_collection_breakers(source, timeout_s=float("nan"))


@pytest.mark.parametrize("source, valid", [("x = 1\n", True), ("x = (\n", False)])
def test_real_python_validator_retains_native_judgment(tmp_path, source, valid):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    result = adapter.sources.validate_source(source)
    assert result.completed, result.reason
    assert result.value.valid is valid
    assert result.value.status == ("valid" if valid else "syntax_error")


@pytest.mark.parametrize("status, value", [("completed", None), ("unsupported", 0), ("not-a-status", None)])
def test_invalid_source_envelope_is_refused(status, value):
    with pytest.raises(lb.InvalidRequest):
        SourceObservation(status, value)


@pytest.mark.parametrize("value", [False, 0, (), ""])
def test_completed_false_and_empty_values_are_retained(value):
    result = SourceObservation("completed", value)
    assert result.completed and result.value == value
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.value = None


@pytest.mark.parametrize("language", ["python", "java", "partial-fixture"])
def test_static_discovery_never_runs_probes(tmp_path, monkeypatch, language):
    adapter = (PartialAdapter(lb.ProjectContext(str(tmp_path))) if language == "partial-fixture"
               else lb.create(language, lb.ProjectContext(str(tmp_path))))

    def forbidden(*args, **kwargs):
        pytest.fail("static discovery invoked a readiness probe")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(adapter, "capabilities", forbidden, raising=False)
    entries = {item.name: item for item in adapter.operation_capabilities()}
    assert set(entries) == set(OPERATION_NAMES)
    assert entries["sources.check_structure"].supported
    assert entries["sources.check_structure"].readiness is None
    assert entries["sources.metrics"].supported == (language != "partial-fixture")
    validator = entries["sources.validate_source"]
    assert validator.readiness is (lb.Availability.UNSUPPORTED if language == "partial-fixture"
                                  else lb.Availability.MISSING_SETUP)
    report = adapter.probe_capabilities()  # no runtime: does not launch or invent readiness
    assert report.outcome is lb.Outcome.MISSING_SETUP
    assert report.operation("sources.check_structure").readiness is None
    measurement = report.operation("mutations.run")
    assert measurement.supported == (language != "partial-fixture")
    assert measurement.readiness is (lb.Availability.UNSUPPORTED if language == "partial-fixture"
                                    else lb.Availability.MISSING_SETUP)
    with pytest.raises(lb.InvalidRequest, match="unknown operation"):
        report.operation("sources.metric")


def test_an_explicit_probe_keeps_the_adapters_report_and_labels_no_resolution_as_granularity(tmp_path):
    adapter = PartialAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    calls = []
    report = lb.CapabilityReport("partial-fixture", sys.executable, lb.Outcome.COMPLETED,
                                 "", "fixture-runtime", (), (), "none", ())
    adapter.capabilities = lambda: calls.append("probe") or report
    result = adapter.probe_capabilities()
    assert calls == ["probe"]
    assert result.runtime == report.runtime
    assert result.operations
    assert all("measured_fallback" not in item.evidence_granularity for item in result.operations)
    assert result.operation("tests.run_suite").readiness is lb.Availability.UNSUPPORTED
    with pytest.raises(KeyError):
        result.capability("unknown")


def test_java_container_validator_without_control_setup_is_not_launched(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("container validator without control setup launched")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    adapter = lb.create("java", lb.ProjectContext("/project", lb.Interpreter(path="/opt/jdk/bin/java"),
                       execution=lb.ContainerExecution("not-started", str(tmp_path))))
    assert adapter.sources.validate_source("").status is SourceStatus.MISSING_SETUP
    entry = next(item for item in adapter.operation_capabilities() if item.name == "sources.validate_source")
    assert entry.supported and entry.readiness is lb.Availability.MISSING_SETUP
    assert entry.execution_kinds == ("local", "container")
