"""Public Java container support separates implementation from execution setup."""
import dataclasses
import subprocess
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.execution import ContainerExecutor, LocalExecutor
from langadapter.java.adapter import JavaAdapter
from langadapter.runs import BenchmarkRequest, CollectionRequest, PerTestEvidenceRequest, SuiteRunRequest


@pytest.fixture
def selected(tmp_path):
    test = tmp_path / "CalcTest.java"
    test.write_text("class CalcTest { @Test void checks() {} }", encoding="utf-8")
    context = lb.ProjectContext("/project", lb.Interpreter(path="/selected/jdk/bin/java"),
        execution=lb.ContainerExecution("selected", work_dir=str(tmp_path), container_work_dir="/workzone"))
    return context, lb.Suite(str(tmp_path), (test.name,))


def test_missing_helper_is_known_setup_not_absent_implementation(selected, monkeypatch):
    context, suite = selected
    adapter = lb.create("java", context)

    def forbidden(*args, **kwargs):
        pytest.fail("missing container control setup must refuse before any tool or target probe")

    monkeypatch.setattr(adapter._executor, "run", forbidden)
    monkeypatch.setattr(adapter, "_read_jdk", forbidden)
    operations = {item.name: item for item in adapter.operation_capabilities()}
    for name in ("sources.validate_source", "sources.target_identity", "sources.merge_sources",
                 "tests.run_suite", "tests.collect_suite", "tests.run_as_delivered", "tests.benchmark",
                 "tests.per_test_evidence", "mutations.operators", "mutations.open_session",
                 "mutations.run"):
        assert operations[name].supported, name
        assert operations[name].readiness is lb.Availability.MISSING_SETUP, name
        assert "helper_python" in operations[name].reason
    assert operations["sources.skeleton"].readiness is None
    assert adapter.sources.skeleton("class Example {}").completed
    assert adapter.mutations.engines() == ("pit",)
    for result in (adapter.sources.validate_source("class Example {}"),
                   adapter.sources.target_identity("Calc.java")):
        assert result.status == "missing_setup" and result.value is None
    assert adapter.sources.merge_sources("class A {}", "class B {}").status == "missing_setup"
    requests = [
        (adapter.tests.run_suite, SuiteRunRequest(suite, 10, coverage_target="Calc.java")),
        (adapter.tests.collect_suite, CollectionRequest(suite, 10)),
        (adapter.tests.run_as_delivered, SuiteRunRequest(suite, 10)),
        (adapter.tests.benchmark, BenchmarkRequest(suite, 1, 10)),
        (adapter.tests.per_test_evidence, PerTestEvidenceRequest(suite, "Calc.java", 10)),
    ]
    for call, request in requests:
        result = call(request)
        assert result.outcome is lb.Outcome.MISSING_SETUP and "helper_python" in result.reason
        if hasattr(result, "coverage"):
            assert result.coverage is None
        if hasattr(result, "counts"):
            assert result.counts.passed is None
    assert adapter.mutations.operators("pit").outcome is lb.Outcome.MISSING_SETUP
    with adapter.mutations.open_session("pit") as session:
        assert session.outcome is lb.Outcome.MISSING_SETUP and session.workers_started == 0
    report = adapter.probe_capabilities()
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.runtime is None
    assert report.operation("tests.run_suite").supported
    assert adapter.project().outcome is lb.Outcome.MISSING_SETUP


def test_flat_missing_helper_calls_do_not_bypass_preflight(selected, monkeypatch):
    context, suite = selected
    adapter = lb.create("java", context)
    monkeypatch.setattr(adapter._executor, "run", lambda *a, **k: pytest.fail("unsafe flat launch"))
    assert adapter.run_suite(SuiteRunRequest(suite, 10, coverage_target="Calc.java")).outcome is lb.Outcome.MISSING_SETUP
    assert adapter.collect_suite(CollectionRequest(suite, 10)).outcome is lb.Outcome.MISSING_SETUP
    assert adapter.per_test_evidence(PerTestEvidenceRequest(suite, "Calc.java", 10)).outcome is lb.Outcome.MISSING_SETUP
    assert adapter.run_as_delivered(SuiteRunRequest(suite, 10)).outcome is lb.Outcome.MISSING_SETUP
    assert adapter.benchmark(BenchmarkRequest(suite, 1, 10)).outcome is lb.Outcome.MISSING_SETUP
    assert adapter.mutation_operators("pit").outcome is lb.Outcome.MISSING_SETUP
    with adapter.open_mutation_session("pit") as session:
        assert session.outcome is lb.Outcome.MISSING_SETUP and session.workers_started == 0
    assert adapter.validate_source("class Example {}").status == "infrastructure_failure"
    with pytest.raises(lb.InvalidRequest, match="helper_python"):
        adapter.target_identity("Calc.java")


@pytest.mark.parametrize("changed", ["local", "container", "runner", "host_mount", "container_mount", "helper"])
def test_wrong_injected_executor_is_refused_before_work(selected, monkeypatch, changed):
    context, suite = selected
    execution = dataclasses.replace(context.execution, helper_python="/control/python")
    context = dataclasses.replace(context, execution=execution)
    executor = (LocalExecutor() if changed == "local" else ContainerExecutor(
        "other" if changed == "container" else "selected",
        runner="podman" if changed == "runner" else "docker",
        work_dir=str(Path(execution.work_dir) / "foreign") if changed == "host_mount" else execution.work_dir,
        container_work_dir="/foreign" if changed == "container_mount" else "/workzone",
        helper_python="/other/python" if changed == "helper" else "/control/python"))
    monkeypatch.setattr(executor, "run", lambda *a, **k: pytest.fail("changed selected execution launched"))
    adapter = JavaAdapter(context, executor=executor)
    assert not adapter.tests.supports("run_suite")
    result = adapter.tests.run_suite(SuiteRunRequest(suite, 10))
    assert result.outcome is lb.Outcome.UNSUPPORTED
    assert adapter.project().outcome is lb.Outcome.UNSUPPORTED
    assert adapter.runtime_version() is None


def test_selected_container_discovery_is_pure_and_readiness_is_unknown(selected, monkeypatch):
    context, _ = selected
    context = dataclasses.replace(context, execution=dataclasses.replace(
        context.execution, helper_python="/control/python"))
    adapter = lb.create("java", context)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("static support started a tool"))
    # Java offers no suite profile.
    implemented = [item for item in adapter.operation_capabilities()
                   if item.execution_backed and item.name != "tests.profile_suite"]
    assert "tests.benchmark_warmed" in {item.name for item in implemented}
    assert implemented and all(item.supported and item.readiness is None for item in implemented)
    assert all(item.execution_kinds == ("local", "container") for item in implemented)
    assert adapter._executor.helper_python == "/control/python"
