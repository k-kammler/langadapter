"""Readiness uses checked native prerequisites, never a successful probe alone."""
import dataclasses
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.capabilities import CAPABILITIES, Capability, CapabilityReport, OperationCapability, ToolVersion


def report(language, *, states=None, tools=None, outcome=lb.Outcome.COMPLETED, runtime="fixture runtime"):
    states = states or {}
    tools = tools if tools is not None else (
        {"pytest": "8.1.1", "coverage": "7.6.8", "cosmic-ray": "8.4.1"} if language == "python"
        else {"junit-platform-console": "1.14.4", "jacoco": "0.8.15", "pitest": "1.30.0",
              "tree-sitter-java": "fixture-grammar"})
    return CapabilityReport(language, sys.executable, outcome, "probe detail", runtime,
                            tuple(Capability(name, states.get(name, lb.Availability.AVAILABLE), f"{name} detail")
                                  for name in CAPABILITIES),
                            tuple(ToolVersion(name, None, found) for name, found in tools.items()),
                            "native", ())


def adapter_with_report(tmp_path, monkeypatch, language, native):
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    calls = []
    monkeypatch.setattr(adapter, "capabilities", lambda: calls.append("probe") or native)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("extra readiness probe launched"))
    return adapter, calls


@pytest.mark.parametrize("language", ["python", "java"])
def test_static_discovery_stays_unknown_until_explicit_probe(tmp_path, monkeypatch, language):
    native = report(language)
    adapter, calls = adapter_with_report(tmp_path, monkeypatch, language, native)
    assert all(entry.readiness is None for entry in adapter.operation_capabilities() if entry.supported)
    assert calls == []
    resolved = adapter.probe_capabilities()
    assert calls == ["probe"]
    assert resolved.capabilities == native.capabilities and resolved.tools == native.tools
    assert resolved.runtime == native.runtime and resolved.outcome == native.outcome
    for name in ("sources.validate_source", "sources.merge_sources", "sources.resolve_collisions",
                 "tests.run_suite", "tests.benchmark", "tests.per_test_evidence", "mutations.operators",
                 "mutations.open_session", "mutations.run", "mutations.per_suite"):
        assert resolved.operation(name).readiness is lb.Availability.AVAILABLE, name
    for name in ("sources.prune_tests", "sources.finalize_source", "sources.metrics", "mutations.engines"):
        assert resolved.operation(name).readiness is None, name
    assert adapter.operation_capabilities() != resolved.operations
    assert all(entry.readiness is None for entry in adapter.operation_capabilities() if entry.supported)


@pytest.mark.parametrize("language", ["python", "java"])
def test_missing_suite_tools_do_not_disable_native_syntax_or_operator_catalogue(tmp_path, monkeypatch, language):
    states = {name: lb.Availability.MISSING_SETUP for name in
              ("suite_runs", "coverage", "per_test_evidence", "mutation", "per_test_kill_attribution")}
    adapter, _ = adapter_with_report(tmp_path, monkeypatch, language, report(language, states=states))
    result = adapter.probe_capabilities()
    assert result.operation("sources.validate_source").readiness is lb.Availability.AVAILABLE
    assert result.operation("sources.merge_sources").readiness is lb.Availability.AVAILABLE
    assert result.operation("mutations.operators").readiness is lb.Availability.AVAILABLE
    assert result.operation("tests.run_suite").readiness is lb.Availability.MISSING_SETUP
    assert result.operation("sources.prune_collection_breakers").readiness is lb.Availability.MISSING_SETUP
    assert result.operation("mutations.run").readiness is lb.Availability.MISSING_SETUP


@pytest.mark.parametrize("language", ["python", "java"])
def test_missing_coverage_is_not_a_missing_test_runner(tmp_path, monkeypatch, language):
    adapter, _ = adapter_with_report(tmp_path, monkeypatch, language, report(language, states={
        "coverage": lb.Availability.MISSING_SETUP, "per_test_evidence": lb.Availability.MISSING_SETUP}))
    result = adapter.probe_capabilities()
    assert result.operation("tests.run_suite").readiness is lb.Availability.AVAILABLE
    assert result.operation("tests.per_test_evidence").readiness is lb.Availability.MISSING_SETUP
    assert result.operation("tests.per_test_evidence").reason == "per_test_evidence detail"
    assert result.operation("mutations.run").readiness is lb.Availability.AVAILABLE


@pytest.mark.parametrize("language", ["python", "java"])
def test_runtime_or_project_probe_failure_does_not_erase_pure_reads(tmp_path, monkeypatch, language):
    native = report(language, outcome=lb.Outcome.INFRASTRUCTURE_FAILURE, runtime=None)
    adapter, _ = adapter_with_report(tmp_path, monkeypatch, language, native)
    result = adapter.probe_capabilities()
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    for name in ("sources.validate_source", "sources.merge_sources", "tests.run_suite", "mutations.run"):
        assert result.operation(name).readiness is lb.Availability.MISSING_SETUP
        assert result.operation(name).reason == "probe detail"
    assert result.operation("sources.skeleton").supported
    assert result.operation("sources.skeleton").readiness is None


def test_java_no_build_keeps_native_syntax_fallback_but_not_project_execution(tmp_path, monkeypatch):
    native = report("java", states={name: lb.Availability.UNSUPPORTED for name in CAPABILITIES},
                    tools={"tree-sitter-java": "grammar", "pitest": None})
    adapter, _ = adapter_with_report(tmp_path, monkeypatch, "java", native)
    result = adapter.probe_capabilities()
    assert result.operation("sources.validate_source").readiness is lb.Availability.AVAILABLE
    assert result.operation("sources.merge_sources").readiness is lb.Availability.AVAILABLE
    assert result.operation("sources.prune_unresolvable_imports").readiness is lb.Availability.UNSUPPORTED
    assert result.operation("sources.prune_collection_breakers").readiness is lb.Availability.UNSUPPORTED
    assert result.operation("tests.run_suite").supported  # implementation is present
    assert result.operation("tests.run_suite").readiness is lb.Availability.UNSUPPORTED
    assert result.operation("mutations.operators").readiness is None  # PIT jars were not probed


@pytest.mark.parametrize("no_build", [False, True])
@pytest.mark.parametrize("grammar, readiness", [
    ("unchecked", None), (None, lb.Availability.MISSING_SETUP),
    ("fixture-grammar", lb.Availability.AVAILABLE),
])
def test_java_syntax_readiness_requires_checked_filename_parser(
        tmp_path, monkeypatch, no_build, grammar, readiness):
    tools = {} if grammar == "unchecked" else {"tree-sitter-java": grammar}
    states = {name: lb.Availability.UNSUPPORTED for name in CAPABILITIES} if no_build else {}
    native = report("java", tools=tools, states=states)
    adapter, calls = adapter_with_report(tmp_path, monkeypatch, "java", native)
    static = {entry.name: entry for entry in adapter.operation_capabilities()}
    assert static["sources.validate_source"].supported
    assert static["sources.validate_source"].readiness is None and calls == []
    result = adapter.probe_capabilities()
    validation = result.operation("sources.validate_source")
    assert validation.supported and validation.readiness is readiness
    if readiness is lb.Availability.MISSING_SETUP:
        assert "tree-sitter-java is not installed" in validation.reason
    assert calls == ["probe"] and result.tools == native.tools
    assert result.capabilities == native.capabilities


@pytest.mark.parametrize("language, tools", [
    ("python", {"cosmic-ray": None}), ("java", {"pitest": None, "tree-sitter-java": "grammar"}),
])
def test_checked_missing_operator_tool_is_missing_setup(tmp_path, monkeypatch, language, tools):
    native = report(language, tools=tools, states={"mutation": lb.Availability.MISSING_SETUP})
    adapter, _ = adapter_with_report(tmp_path, monkeypatch, language, native)
    result = adapter.probe_capabilities()
    assert result.operation("mutations.operators").readiness is lb.Availability.MISSING_SETUP
    assert result.operation("mutations.operators").reason


@pytest.mark.parametrize("language", ["python", "java"])
def test_unreported_prerequisites_remain_unknown(tmp_path, monkeypatch, language):
    native = dataclasses.replace(report(language, tools={}), capabilities=())
    adapter, _ = adapter_with_report(tmp_path, monkeypatch, language, native)
    result = adapter.probe_capabilities()
    for name in ("tests.run_suite", "tests.per_test_evidence", "mutations.operators", "mutations.run"):
        assert result.operation(name).readiness is None
    assert result.operation("sources.merge_sources").readiness is None


def test_java_framework_override_restrictions_are_static_and_do_not_block_pure_sources(tmp_path, monkeypatch):
    adapter = lb.create("java", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable),
                        test_framework=lb.TestFrameworkConfig(source="isolated")))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("static discovery ran a tool"))
    entries = {entry.name: entry for entry in adapter.operation_capabilities()}
    for name in ("sources.validate_source", "sources.resolve_collisions", "tests.run_suite", "mutations.run"):
        assert not entries[name].supported
        assert entries[name].readiness is lb.Availability.UNSUPPORTED
        assert "test_framework" in entries[name].reason
    assert entries["sources.prune_tests"].supported and entries["sources.metrics"].supported
    assert entries["mutations.engines"].supported
    assert adapter.sources.validate_source("class Example {}").status is lb.SourceStatus.UNSUPPORTED


def test_unknown_external_readiness_and_malformed_probe_remain_explicit(tmp_path):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    adapter.run_suite = lambda request: None
    adapter.capabilities = lambda: report("external")
    assert adapter.probe_capabilities().operation("tests.run_suite").readiness is None
    adapter.operation_readiness = lambda name, report: Capability("wrong", lb.Availability.AVAILABLE)
    with pytest.raises(lb.InvalidRequest, match="readiness"):
        adapter.probe_capabilities()
    adapter.capabilities = lambda: "not a report"
    with pytest.raises(TypeError, match="CapabilityReport"):
        adapter.probe_capabilities()


@pytest.mark.parametrize("field", ["execution_kinds", "evidence_granularity", "resolution_methods"])
@pytest.mark.parametrize("values", [None, "local", [""], [1], ["duplicate", "duplicate"]])
def test_operation_metadata_requires_distinct_names(field, values):
    with pytest.raises(lb.InvalidRequest, match=field):
        OperationCapability("tests.run_suite", True, **{field: values})


@pytest.mark.parametrize("action", ["", "  ", 1, ("step",)])
def test_operation_next_step_is_text_or_absent(action):
    with pytest.raises(lb.InvalidRequest, match="action"):
        OperationCapability("tests.run_suite", True, action=action)
    assert OperationCapability("tests.run_suite", True).action is None


@pytest.mark.parametrize("changes", [{"tried_by": "later"}, {"tried_by": "run", "supported": False},
    {"tried_by": "run", "readiness": lb.Availability.AVAILABLE},
    {"tried_by": "verification", "readiness": lb.Availability.MISSING_SETUP}])
def test_only_a_supported_operation_not_tried_yet_names_what_tries_it(changes):
    with pytest.raises(lb.InvalidRequest, match="tried_by"):
        OperationCapability(**({"name": "tests.run_suite", "supported": True} | changes))
    assert OperationCapability("tests.run_suite", True, tried_by="run").tried_by == "run"


def test_implemented_but_probed_unsupported_is_distinct_from_no_implementation():
    implemented = OperationCapability("tests.run_suite", True, "project setup unsupported", lb.Availability.UNSUPPORTED)
    absent = OperationCapability("tests.run_suite", False, "no implementation")
    assert implemented.supported and not absent.supported
    assert implemented.readiness is absent.readiness is lb.Availability.UNSUPPORTED


def test_real_python_probe_binds_only_its_native_prerequisites(tmp_path):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    result = adapter.probe_capabilities()
    assert result.outcome is lb.Outcome.COMPLETED, result.reason
    assert result.runtime and result.operation("sources.validate_source").readiness is lb.Availability.AVAILABLE
    assert result.operation("tests.run_suite").readiness is result.capability("suite_runs").availability
    assert result.operation("tests.per_test_evidence").readiness is result.capability("per_test_evidence").availability
    assert result.operation("mutations.run").readiness is result.capability("mutation").availability
    assert result.operation("sources.metrics").readiness is None


def test_a_container_runner_not_on_path_is_missing_setup_and_leaves_nothing(tmp_path,
                                                                             monkeypatch):
    """A container context whose runner program is not on PATH reports
    missing setup, from probe_capabilities and from an operation, before
    anything starts, and leaves nothing in the temporary directory."""
    import tempfile

    from langadapter.runs import SuiteRunRequest

    (tmp_path / "work").mkdir()
    (tmp_path / "scratch").mkdir()
    for name in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path / "scratch"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    context = lb.ProjectContext(
        "/projects/example", lb.Interpreter(path="/tools/python"),
        execution=lb.ContainerExecution("existing-container", str(tmp_path / "work"),
                                        helper_python="/control/python3",
                                        runner="langadapter-no-such-runner"))
    adapter = lb.create("python", context)
    report = adapter.probe_capabilities()
    assert (report.outcome, report.reason) == (
        lb.Outcome.MISSING_SETUP, "the container runner langadapter-no-such-runner is not on PATH")
    suite = tmp_path / "work" / "suite"
    (suite / "tests").mkdir(parents=True)
    (suite / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    run = adapter.tests.run_suite(SuiteRunRequest(lb.Suite(str(suite), ("tests/test_a.py",)),
                                                  timeout_s=10))
    assert (run.outcome, run.reason) == (
        lb.Outcome.MISSING_SETUP, "the container runner langadapter-no-such-runner is not on PATH")
    assert not list((tmp_path / "scratch").iterdir())
