"""Execution contracts independent of installed Java tools."""
from pathlib import Path
from dataclasses import replace
import os
from types import SimpleNamespace
import subprocess
import xml.etree.ElementTree as ET

import pytest

import langadapter as lb
from langadapter.java import _jacoco, _tools as _junit, adapter as java_adapter
from langadapter.java._build import Compilation, JavaProject, Resolution
from langadapter.runs import BenchmarkRequest, SuiteRunRequest


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "Calc.java").write_text("class Calc {}", encoding="utf-8")
    suite_root = tmp_path / "suite"
    suite_root.mkdir()
    (suite_root / "CalcTest.java").write_text(
        "class CalcTest {}", encoding="utf-8")
    adapter = java_adapter.JavaAdapter(lb.ProjectContext(
        str(root), lb.Interpreter(path="unused-java")))
    jdk = SimpleNamespace(version="21", home=str(tmp_path),
                          tool=lambda name: name)
    monkeypatch.setattr(adapter, "_jdk", lambda: jdk)
    monkeypatch.setattr(adapter, "project", lambda: Resolution(
        lb.Outcome.COMPLETED, JavaProject(
            str(root), "maven", 17, (), (), (), (), ())))
    monkeypatch.setattr(_junit, "toolset", lambda *args, **kwargs: _junit.Toolset(
        "1.14.4", ()))

    def compile_suite(project, jdk, suite, out_dir, executor, **kwargs):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return Compilation(lb.Outcome.COMPLETED, out_dir)

    monkeypatch.setattr(java_adapter, "compile_suite", compile_suite)
    monkeypatch.setattr(_junit, "launch", lambda *args, **kwargs:
                        _junit.Launch(0, "", ()))
    monkeypatch.setattr(_junit, "read_reports", lambda paths: [
        _junit.Case("CalcTest", "passes()", "passed", 0.01, "")])

    def agent(destfile, *args, **options):
        destfile.write_bytes(b"coverage")
        return "-javaagent:fixture"

    monkeypatch.setattr(_jacoco, "agent_argument", agent)
    return adapter, lb.Suite(str(suite_root), ("CalcTest.java",))


@pytest.mark.parametrize("timeout", [0, -1])
def test_invalid_timeout_is_refused_before_execution(configured, timeout):
    adapter, suite = configured
    with pytest.raises(lb.InvalidRequest) as error:
        adapter.run_suite(SuiteRunRequest(suite, timeout))
    assert error.value.field == "timeout_s"


def test_target_outside_project_names_the_request_field(configured, tmp_path):
    adapter, suite = configured
    outside = tmp_path / "Outside.java"
    outside.write_text("class Outside {}", encoding="utf-8")
    with pytest.raises(lb.InvalidRequest) as error:
        adapter.run_suite(SuiteRunRequest(
            suite, 10, coverage_target="../Outside.java"))
    assert error.value.field == "coverage_target"


def test_undefined_runner_options_are_not_silently_ignored(configured):
    adapter, suite = configured
    with pytest.raises(lb.InvalidRequest) as error:
        adapter.run_suite(SuiteRunRequest(suite, 10, runner_options={"bogus": 1}))
    assert error.value.field == "runner_options"


def test_setup_files_are_part_of_the_measured_identity(configured):
    adapter, suite = configured
    (Path(suite.root) / "Helper.java").write_text(
        "class Helper {}", encoding="utf-8")
    suite = lb.Suite(suite.root, suite.test_files, ("Helper.java",))
    run = adapter.run_suite(SuiteRunRequest(suite, 10))
    assert run.identity.suite == suite.identity()


@pytest.mark.parametrize("failure", [
    (None, "analysis process failed"), ({}, ""),
])
def test_failed_coverage_preserves_tests_but_fails_measurement(
        configured, monkeypatch, failure):
    adapter, suite = configured
    monkeypatch.setattr(_jacoco, "analyze", lambda *args, **kwargs: failure)
    run = adapter.run_suite(SuiteRunRequest(suite, 10, coverage_target="Calc.java"))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert run.coverage is None
    assert run.counts.passed == 1
    assert run.tests[0].outcome == "passed"
    assert run.reason


def test_missing_coverage_data_is_not_success(configured, monkeypatch):
    adapter, suite = configured
    monkeypatch.setattr(_jacoco, "agent_argument", lambda *args, **kwargs: "-javaagent:fixture")
    run = adapter.run_suite(SuiteRunRequest(suite, 10, coverage_target="Calc.java"))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert run.coverage is None
    assert run.counts.passed == 1


_READ_REPORTS = _junit.read_reports


@pytest.mark.parametrize("report", [
    '<?xml version="1.0"?>\n<testsuite name="CalcTest">\n<testcase classname="CalcTest" name="pa',
    None])
def test_a_timeout_is_the_reason_even_when_it_cut_the_report(configured, monkeypatch, report):
    """A timeout during or before the report writing makes the run timed
    out, whatever is left of the report."""
    adapter, suite = configured

    def launch(java, classpath, classes, reports, **options):
        Path(reports).mkdir(parents=True, exist_ok=True)
        written = ()
        if report is not None:
            (Path(reports) / "TEST-CalcTest.xml").write_text(report, encoding="utf-8")
            written = (Path(reports) / "TEST-CalcTest.xml",)
        return _junit.Launch(None, "", written, timed_out=True)

    monkeypatch.setattr(_junit, "launch", launch)
    monkeypatch.setattr(_junit, "read_reports", _READ_REPORTS)
    with pytest.warns(RuntimeWarning, match="scratch directory retained"):
        run = adapter.run_suite(SuiteRunRequest(suite, 10))
    assert run.outcome is lb.Outcome.TIMED_OUT
    assert run.reason.startswith("the suite's timeout ended the run")
    if report is None:
        assert run.reason == "the suite's timeout ended the run"
    else:
        assert "(its launcher report could not be read: " in run.reason


@pytest.mark.parametrize("failure", [OSError("cannot start"),
                                    ET.ParseError("broken report")])
def test_tool_errors_are_outcomes(configured, monkeypatch, failure):
    adapter, suite = configured

    def broken(*args, **kwargs):
        raise failure

    if isinstance(failure, OSError):
        monkeypatch.setattr(_junit, "launch", broken)
    else:
        monkeypatch.setattr(_junit, "read_reports", broken)
    run = adapter.run_suite(SuiteRunRequest(suite, 10))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert run.counts.collected is None
    assert str(failure) in run.reason


def test_coverage_analysis_timeout_is_not_a_green_measurement(configured, monkeypatch):
    adapter, suite = configured

    def broken(*args, **kwargs):
        raise subprocess.TimeoutExpired("jacoco", 10)

    monkeypatch.setattr(_jacoco, "analyze", broken)
    run = adapter.run_suite(SuiteRunRequest(suite, 10, coverage_target="Calc.java"))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert run.counts.passed == 1


def test_successful_launcher_without_test_report_is_not_no_tests(tmp_path):
    launch = _junit.Launch(0, "", ())
    identity = SimpleNamespace()
    run = java_adapter._suite_run(launch, [], {}, str(tmp_path), 0.1, identity)
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize("operation", ["benchmark", "evidence"])
@pytest.mark.parametrize("failure", [OSError("cannot start"),
                                    ET.ParseError("broken report")])
def test_other_measurements_return_tool_failure_outcomes(
        configured, monkeypatch, tmp_path, operation, failure):
    from langadapter.java import _benchmark
    from langadapter.java import _runs as _evidence
    from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest

    adapter, suite = configured
    monkeypatch.setattr(_benchmark, "compile_suite", java_adapter.compile_suite)
    monkeypatch.setattr(_evidence, "compile_suite", java_adapter.compile_suite)
    monkeypatch.setattr(_benchmark, "_allowed_cpus", lambda *args: None)
    monkeypatch.setattr(_evidence, "listener", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(_jacoco, "analyzer", lambda *args, **kwargs: None)
    monkeypatch.setattr(_evidence._tools, "jar", lambda *args: tmp_path / "agent.jar")
    monkeypatch.setattr(_benchmark.placement, "run_timed", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="", stderr=""))

    def broken(*args, **kwargs):
        raise failure

    if isinstance(failure, OSError):
        monkeypatch.setattr(_junit, "launch", broken)
        monkeypatch.setattr(_benchmark.placement, "run_timed", broken)
    else:
        monkeypatch.setattr(_junit, "read_reports", broken)
    request = (BenchmarkRequest(suite, 1, 10, warmup_runs=0) if operation == "benchmark"
               else PerTestEvidenceRequest(suite, "Calc.java", 10))
    result = getattr(adapter, "benchmark" if operation == "benchmark"
                     else "per_test_evidence")(request)
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert str(failure) in result.reason


@pytest.mark.parametrize("cpus", ["2,7", None])
def test_native_benchmark_uses_one_selected_taskset_and_environment_for_every_probe_and_sample(
        configured, monkeypatch, tmp_path, cpus):
    from langadapter.java import _benchmark

    adapter, suite = configured
    selected = tmp_path / "selected"
    selected.mkdir()
    taskset = selected / ("taskset.EXE" if os.name == "nt" else "taskset")
    taskset.write_text("selected placement", encoding="ascii")
    taskset.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "ambient"))
    monkeypatch.setenv("PATHEXT", ".BAT")
    adapter.context = replace(adapter.context, environment=lb.Environment(
        {"PATH": str(selected), "PATHEXT": ".EXE", "SELECTED": "first"}, inherit=False))
    monkeypatch.setattr(_benchmark, "compile_suite", java_adapter.compile_suite)
    observed = []

    def allowed(argv, **options):
        observed.append(("affinity", argv, options["env"]))
        return subprocess.CompletedProcess(argv, 0, "[2,7]\n", "")

    def timed(argv, **options):
        observed.append(("repetition", argv, options["env"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(_benchmark._process_tree, "run", allowed)
    monkeypatch.setattr(_benchmark.placement, "run_timed", timed)
    result = adapter.benchmark(BenchmarkRequest(suite, 2, 10, warmup_runs=1, cpus=cpus))
    assert result.outcome is lb.Outcome.COMPLETED and len(result.times_ms) == 2
    assert result.allowed_cpus == (2, 7)
    assert [kind for kind, _, _ in observed] == ["affinity", "repetition", "repetition", "repetition"]
    prefix = [str(taskset), "-c", cpus] if cpus is not None else []
    assert all(argv[:len(prefix)] == prefix for _, argv, _ in observed)
    assert all(dict(env) == observed[0][2] for _, _, env in observed)
    assert observed[0][2]["SELECTED"] == "first"
    assert str(tmp_path / "ambient") not in observed[0][2]["PATH"]


def test_native_benchmark_capture_cannot_revive_ambient_taskset(configured, monkeypatch, tmp_path):
    from langadapter.java import _benchmark

    adapter, suite = configured
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    tool = ambient / ("taskset.EXE" if os.name == "nt" else "taskset")
    tool.write_text("ambient", encoding="ascii")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(ambient))
    monkeypatch.setenv("PATHEXT", ".EXE")
    adapter.context = replace(adapter.context, environment=lb.Environment(inherit=False))
    monkeypatch.setattr(_benchmark, "compile_suite", lambda *a, **kw: pytest.fail("no suite compilation"))
    result = adapter.benchmark(BenchmarkRequest(suite, 1, 10, warmup_runs=0, cpus="2"))
    assert result.outcome is lb.Outcome.UNSUPPORTED and result.times_ms == ()


@pytest.mark.parametrize("field,value", [
    ("isolation_config", True), ("record_leaks", True),
    ("session_seed", 0), ("reverse_order", True),
])
def test_unsupported_suite_controls_do_not_run_something_else(
        configured, monkeypatch, field, value):
    adapter, suite = configured
    monkeypatch.setattr(adapter, "project", lambda: pytest.fail("must not execute"))
    run = adapter.run_suite(SuiteRunRequest(suite, 10, **{field: value}))
    assert run.outcome is lb.Outcome.UNSUPPORTED
    assert field in run.reason


@pytest.mark.parametrize("operation", ["run_suite", "benchmark", "per_test_evidence"])
def test_removing_created_files_needs_the_prepared_copy(configured, monkeypatch, operation):
    """Created files are removed only in a held copy from ``prepare_project``,
    and the original project's context is refused with OwnershipRefused
    before anything runs."""
    from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest

    adapter, suite = configured
    monkeypatch.setattr(adapter, "project", lambda: pytest.fail("must not execute"))
    request = {"run_suite": SuiteRunRequest(suite, 10, remove_created_files=True),
               "benchmark": BenchmarkRequest(suite, 1, 10, remove_created_files=True),
               "per_test_evidence": PerTestEvidenceRequest(
                   suite, "Calc.java", 10, remove_created_files=True)}[operation]
    with pytest.raises(lb.OwnershipRefused, match="prepare_project") as refused:
        getattr(adapter, operation)(request)
    assert isinstance(refused.value, lb.LangAdapterError)


@pytest.mark.parametrize("operation", ["benchmark", "per_test_evidence"])
def test_unsupported_measurement_controls_are_explicit(
        configured, monkeypatch, operation):
    from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest

    adapter, suite = configured
    monkeypatch.setattr(adapter, "project", lambda: pytest.fail("must not execute"))
    request = (BenchmarkRequest(suite, 1, 10, isolation_config=True)
               if operation == "benchmark"
               else PerTestEvidenceRequest(suite, "Calc.java", 10, isolation_config=True))
    result = getattr(adapter, operation)(request)
    assert result.outcome is lb.Outcome.UNSUPPORTED
    assert "isolation_config" in result.reason


def test_container_context_does_not_execute_on_host(configured, monkeypatch, tmp_path):
    from dataclasses import replace

    adapter, suite = configured
    adapter = java_adapter.JavaAdapter(replace(adapter.context, execution=lb.ContainerExecution(
        "fixture", str(tmp_path), image="fixture-image")))
    monkeypatch.setattr(adapter, "_jdk", lambda: pytest.fail("must not inspect host JDK"))
    run = adapter.run_suite(SuiteRunRequest(suite, 10))
    assert run.outcome is lb.Outcome.MISSING_SETUP
    assert "helper_python" in run.reason
    assert run.identity.execution.kind == "container"
    assert run.identity.execution.image == "fixture-image"
    assert adapter.project().outcome is lb.Outcome.MISSING_SETUP
    # Java offers no suite profile.
    assert all(item.supported and item.readiness is lb.Availability.MISSING_SETUP
               for item in adapter.operation_capabilities()
               if item.execution_backed and item.name != "tests.profile_suite")
    assert adapter.mutation_operators().outcome is lb.Outcome.MISSING_SETUP
    validation = adapter.validate_source("class Calc {}")
    assert not validation.valid
    assert "helper_python" in validation.message


def test_journal_does_not_attribute_concurrent_active_tests_to_one(tmp_path):
    import base64
    import json

    from langadapter.java import _runs

    names = [f"[engine:junit-jupiter]/[class:CalcTest]/[method:{method}()]"
             for method in ("first", "second")]
    path = tmp_path / "active.json"
    path.write_text(json.dumps({"active": [base64.b64encode(name.encode()).decode()
                                          for name in names]}), encoding="utf-8")
    observation = _runs.read_journal(path, {"CalcTest": tmp_path / "CalcTest.java"}, str(tmp_path))
    assert observation.observed
    assert observation.active is None
    assert [entry.native_test for entry in observation.diagnostics] == names


def test_delivered_files_use_private_copies_and_original_relative_ids(configured, monkeypatch):
    adapter, suite = configured
    root = Path(suite.root)
    (root / "OtherTest.java").write_text("class OtherTest {}", encoding="utf-8")
    (root / "helper.txt").write_text("resource", encoding="utf-8")
    suite = lb.Suite(suite.root, ("CalcTest.java", "OtherTest.java"), ("helper.txt",))
    run_suite = adapter.run_suite
    observed = []

    def record(request):
        staged = Path(request.suite.root)
        assert staged != root
        assert len(request.suite.test_files) == 1
        assert (staged / "helper.txt").read_text() == "resource"
        name = request.suite.test_files[0]
        observed.append((name, (staged / name).read_text()))
        monkeypatch.setattr(_junit, "read_reports", lambda paths: [
            _junit.Case(Path(name).stem, "passes()", "passed", 0.01, "")])
        return run_suite(request)

    monkeypatch.setattr(adapter, "run_suite", record)
    run = adapter.run_as_delivered(SuiteRunRequest(suite, 10),
                                  sources={"OtherTest.java": "class OtherTest { int x; }"})
    assert run.outcome is lb.Outcome.COMPLETED
    assert [item.test.file for item in run.tests] == ["CalcTest.java", "OtherTest.java"]
    assert run.counts.passed == 2
    assert observed[-1] == ("OtherTest.java", "class OtherTest { int x; }")
    assert (root / "OtherTest.java").read_text() == "class OtherTest {}"
    assert next(item for item in run.identity.suite if item.path == "OtherTest.java").sha256 \
        != next(item for item in suite.identity() if item.path == "OtherTest.java").sha256


def test_delivered_source_override_must_be_a_declared_test(configured):
    adapter, suite = configured
    with pytest.raises(lb.InvalidRequest) as error:
        adapter.run_as_delivered(SuiteRunRequest(suite, 10), sources={"../other.java": ""})
    assert error.value.field == "sources"


def test_journal_unfinished_execution_is_not_success(tmp_path):
    from langadapter.java import _runs
    from langadapter.runs import RunDiagnostic

    run = java_adapter._suite_run(_junit.Launch(0, "", ()), [
        _junit.Case("CalcTest", "passes()", "passed", 0.01, "")],
        {"CalcTest": tmp_path / "CalcTest.java"}, str(tmp_path), 0.1, None)
    result = _runs.apply_journal(run, _runs.JournalObservation(observed=True, diagnostics=(
        RunDiagnostic("active_at_end", "a test has not finished", native_test="native-id"),)))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert result.counts.passed == 1


@pytest.mark.parametrize("stage,failure", [
    ("prepare", OSError("cannot start mutator helper")),
    ("prepare", subprocess.TimeoutExpired("mutators", 10)),
    ("run", OSError("cannot start PIT")),
    ("report", ET.ParseError("broken mutations.xml")),
])
def test_mutation_execution_failures_return_no_score(configured, monkeypatch, tmp_path, stage, failure):
    from langadapter.java import _pit
    from langadapter.java import _mutation
    from langadapter.mutation import MutationRequest

    adapter, suite = configured
    monkeypatch.setattr(_mutation, "compile_suite", java_adapter.compile_suite)
    monkeypatch.setattr(_pit, "mutator_ids", lambda *args, **kwargs: ("mutator",))
    monkeypatch.setattr(_pit, "test_order", lambda *args, **kwargs: tmp_path / "order.jar")
    monkeypatch.setattr(_pit, "run", lambda *args, **kwargs: _pit.Report(0, "", tmp_path / "mutations.xml"))

    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(_pit, {"prepare": "mutator_ids", "run": "run", "report": "read"}[stage], broken)
    with adapter.open_mutation_session() as session:
        result = session.run(MutationRequest("Calc.java", suite))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert result.score() is None
    assert str(failure) in result.reason


@pytest.mark.parametrize("warmup,expected", [(True, 2), (False, 1)])
def test_evidence_honors_the_discarded_warmup(configured, monkeypatch, tmp_path, warmup, expected):
    from langadapter.java import _runs as _evidence
    from langadapter.runs import PerTestEvidenceRequest

    adapter, suite = configured
    monkeypatch.setattr(_evidence, "compile_suite", java_adapter.compile_suite)
    monkeypatch.setattr(_evidence, "listener", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(_jacoco, "analyzer", lambda *args, **kwargs: None)
    monkeypatch.setattr(_evidence._tools, "jar", lambda *args: tmp_path / "agent.jar")
    monkeypatch.setattr(_jacoco, "analyze", lambda *args, **kwargs:
                        ({"suite": ["1:1:0:"], "1": ["1:"]}, ""))
    launches = []

    def launch(*args, **kwargs):
        data = Path(next(arg.split("=", 1)[1] for arg in kwargs["jvm_args"]
                         if arg.startswith(f"-D{_evidence.PROPERTY}=")))
        (data / "index.tsv").write_text(
            "1\t[engine:junit-jupiter]/[class:CalcTest]/[method:passes()]\n", encoding="utf-8")
        launches.append(data)
        return _junit.Launch(0, "", ())

    monkeypatch.setattr(_junit, "launch", launch)
    result = adapter.per_test_evidence(PerTestEvidenceRequest(suite, "Calc.java", 10, warmup=warmup))
    assert result.outcome is lb.Outcome.COMPLETED, result
    assert len(launches) == expected
    assert len(set(launches)) == expected


@pytest.mark.parametrize("index", [
    "1\t[engine:junit-jupiter]/[class:CalcTest]/[method:passes()]\n",
    "1\t[engine:junit-jupiter]/[class:CalcTest]/[method:passes()]\n1\tbroken\n",
])
def test_evidence_refuses_a_missing_test_or_duplicate_index(
        configured, monkeypatch, tmp_path, index):
    from langadapter.java import _runs as _evidence
    from langadapter.runs import PerTestEvidenceRequest

    adapter, suite = configured
    monkeypatch.setattr(_evidence, "compile_suite", java_adapter.compile_suite)
    monkeypatch.setattr(_evidence, "listener", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(_jacoco, "analyzer", lambda *args, **kwargs: None)
    monkeypatch.setattr(_evidence._tools, "jar", lambda *args: tmp_path / "agent.jar")
    monkeypatch.setattr(_jacoco, "analyze", lambda *args, **kwargs:
                        ({"suite": ["1:1:0:"], "1": ["1:"]}, ""))
    monkeypatch.setattr(_junit, "read_reports", lambda paths: [
        _junit.Case("CalcTest", "passes()", "passed", 0.01, ""),
        _junit.Case("CalcTest", "other()", "passed", 0.01, ""),
    ])

    def launch(*args, **kwargs):
        data = Path(next(arg.split("=", 1)[1] for arg in kwargs["jvm_args"]
                         if arg.startswith(f"-D{_evidence.PROPERTY}=")))
        (data / "index.tsv").write_text(index, encoding="utf-8")
        return _junit.Launch(0, "", ())

    monkeypatch.setattr(_junit, "launch", launch)
    result = adapter.per_test_evidence(PerTestEvidenceRequest(
        suite, "Calc.java", 10, warmup=False))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert not result.tests
    assert "per-test coverage" in result.reason
