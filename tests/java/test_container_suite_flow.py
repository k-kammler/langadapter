"""Selected-executor suite orchestration without enabling public container support."""
import hashlib
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter.execution import ContainerExecutor, OwnedDirectory
from langadapter.java import (
    _benchmark, _jacoco, _tools as _junit, _pit, _tools, _mutation,
    _runs as _collection, _runs as _evidence, adapter as java_adapter,
)
from langadapter.java._build import Compilation, JavaProject, Resolution
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.runs import (
    BenchmarkRequest, CollectionRequest, PerTestEvidenceRequest, SuiteRunRequest,
)


class FlowContainer(ContainerExecutor):
    def __init__(self, zone):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone",
                         helper_python="/control/python")
        self.source = b"package example;\r\nclass Calc {}\r\n"
        self.calls = []

    def run(self, argv, **options):
        self.calls.append((argv, options))
        if argv[0] == "cat" and Path(argv[-1]).name == ".langadapter-owner":
            output = Path(argv[-1]).read_text(encoding="ascii")
        elif argv[:3] == ["rm", "-rf", "--"]:
            assert Path(argv[-1]).is_relative_to(Path(self._host_work_dir))
            assert (Path(argv[-1]) / ".langadapter-owner").is_file()
            output = ""
        elif argv[0] == "realpath":
            output = "".join(path + "\0" for path in argv[4:])
        elif argv[0] == "sha256sum":
            output = hashlib.sha256(self.source).hexdigest() + f"  {argv[-1]}\0"
        elif argv[0] == "cat":
            output = self.source.decode("utf-8")
        else:
            assert argv[:2] == ["test", "-f"]
            output = ""
        return subprocess.CompletedProcess(argv, 0, output, "")


@pytest.fixture
def owned_stages(tmp_path, monkeypatch):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    owners = []
    create = OwnedDirectory.create

    def allocate(**options):
        owned = create(**options, receipt_parent=str(receipts))
        owners.append(owned)
        return owned

    monkeypatch.setattr(OwnedDirectory, "create", allocate)
    return owners


@pytest.fixture
def flow(tmp_path, monkeypatch):
    zone = tmp_path / "zone"
    zone.mkdir()
    tests = zone / "suite"
    tests.mkdir()
    (tests / "CalcTest.java").write_text("package example; class CalcTest {}", encoding="utf-8")
    container = FlowContainer(zone)
    adapter = java_adapter.JavaAdapter(lb.ProjectContext(
        root="/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution(container="selected", image="selected-image",
                                        work_dir=str(zone), container_work_dir="/workzone",
                                        helper_python="/control/python"),
        environment={"JAVA_EXTRA": "selected"}), executor=container)
    jdk = SimpleNamespace(version="21", home="/opt/jdk", tool=lambda name: "/opt/jdk/bin/" + name)
    monkeypatch.setattr(adapter, "_jdk", lambda: jdk)
    # Mechanism fixture only: public container operations remain refused.
    monkeypatch.setattr(adapter, "_request_issue", lambda request: "")
    project = JavaProject("/project", "maven", 17, (), (), ("/project/target/classes",), (),
                          ("/opt/lib/project.jar",), test_jvm_args=("-ea",),
                          test_system_properties={"fixture": "selected"})
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.COMPLETED, project))
    calls = {}

    def compile(project, jdk, suite, output, executor, **options):
        assert executor is container
        Path(output).mkdir()
        calls["compile"] = (output, options)
        return Compilation(lb.Outcome.COMPLETED, output)

    def tools(classpath, environment, **options):
        assert options["executor"] is container
        assert options["scratch"].is_relative_to(zone)
        calls["tools"] = options
        return _junit.Toolset("1.14.4", ("/workzone/tools/console.jar",))

    def agent(destfile, target_classes, environment, **options):
        assert options["executor"] is container
        assert target_classes == ("example.Calc",)
        destfile.write_bytes(b"fixture coverage")
        calls["agent"] = options
        return "-javaagent:/workzone/tools/jacoco.jar=destfile=" + container._to_container(str(destfile))

    def launch(java, classpath, classes, reports, **options):
        assert options["executor"] is container
        assert java == "/opt/jdk/bin/java"
        assert classes == ["example.CalcTest"]
        assert "/workzone/tools/console.jar" in classpath
        calls["launch"] = options
        return _junit.Launch(0, "fixture", ())

    def analyze(jdk, mode, data, target, main_classes, **options):
        assert options["executor"] is container
        assert str(target) == "/project/Calc.java" and mode == "run"
        assert options["scratch"].is_relative_to(zone)
        calls["analyze"] = options
        return {"suite": ["5:1:2:0"]}, ""

    monkeypatch.setattr(java_adapter, "compile_suite", compile)
    monkeypatch.setattr(_junit, "toolset", tools)
    monkeypatch.setattr(_jacoco, "agent_argument", agent)
    monkeypatch.setattr(_jacoco, "analyze", analyze)
    monkeypatch.setattr(_junit, "launch", launch)
    monkeypatch.setattr(_junit, "read_reports", lambda paths: [
        _junit.Case("example.CalcTest", "passes()", "passed", 0.01, "")])
    return adapter, lb.Suite(str(tests), ("CalcTest.java",)), container, calls


def test_suite_flow_uses_selected_container_for_every_tool_and_target(flow, monkeypatch):
    adapter, suite, container, calls = flow
    monkeypatch.setenv("HOST_ONLY_PROBE", "not-forwarded")
    run = adapter.run_suite(SuiteRunRequest(suite, 10, coverage_target="Calc.java"))
    assert run.outcome is lb.Outcome.COMPLETED
    assert set(calls) == {"compile", "tools", "agent", "launch", "analyze"}
    assert calls["launch"]["env"]["JAVA_EXTRA"] == "selected"
    assert "HOST_ONLY_PROBE" not in calls["launch"]["env"]
    assert "-ea" in calls["launch"]["jvm_args"]
    assert calls["launch"]["system_properties"] == {"fixture": "selected"}
    assert run.identity.execution.kind == "container"
    assert run.identity.target.sha256 == hashlib.sha256(container.source).hexdigest()
    assert run.coverage.target == run.identity.target
    assert run.coverage.covered == (5,)
    assert run.counts.passed == 1


def test_delivered_suite_stages_replacements_and_helpers_in_selected_container(flow, monkeypatch):
    adapter, suite, container, calls = flow
    root = Path(suite.root)
    helper = root / "support" / "Helper.java"
    helper.parent.mkdir()
    helper.write_text("package example; class Helper {}", encoding="utf-8")
    suite = lb.Suite(suite.root, suite.test_files, ("support/Helper.java",))
    source = "package example; class CalcTest { int replacement = 2; }\n"
    original = (root / "CalcTest.java").read_bytes()
    seen = []
    compile = java_adapter.compile_suite

    def staged(project, jdk, delivered, output, executor, **options):
        staged_root = Path(delivered.root)
        assert staged_root.is_relative_to(root.parent)
        assert container._to_container(str(staged_root)).startswith("/workzone/")
        assert (staged_root / "CalcTest.java").read_text(encoding="utf-8") == source
        assert (staged_root / "support/Helper.java").read_bytes() == helper.read_bytes()
        assert delivered.setup_files == ("support/Helper.java",)
        seen.append(delivered.root)
        return compile(project, jdk, delivered, output, executor, **options)

    monkeypatch.setattr(java_adapter, "compile_suite", staged)
    run = adapter.run_as_delivered(SuiteRunRequest(suite, 10), sources={"CalcTest.java": source})
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert run.counts.passed == 1 and run.coverage is None
    assert run.identity.execution.kind == "container"
    identities = {item.path: item.sha256 for item in run.identity.suite}
    assert identities["CalcTest.java"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert identities["support/Helper.java"] == hashlib.sha256(helper.read_bytes()).hexdigest()
    assert (root / "CalcTest.java").read_bytes() == original
    assert seen and set(calls) == {"compile", "tools", "launch"}


def test_delivered_container_refuses_unmapped_scratch_before_copy(flow, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from langadapter.java import _runs as _delivered

    adapter, suite, _, calls = flow
    outside = tmp_path / "outside-zone"
    outside.mkdir()

    @contextmanager
    def scratch(_name):
        yield str(outside)

    def forbidden(*args, **kwargs):
        pytest.fail("unmapped delivered scratch must be refused before copying inputs")

    monkeypatch.setattr(adapter, "_scratch", scratch)
    monkeypatch.setattr(_delivered.shutil, "copyfile", forbidden)
    run = adapter.run_as_delivered(SuiteRunRequest(suite, 10))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "work folder" in run.reason
    assert run.counts.passed is None and run.wall_time_s is None
    assert calls == {} and list(outside.iterdir()) == []


def test_delivered_subprocess_preparation_failure_is_not_a_test_failure(flow, monkeypatch):
    adapter, suite, _, _ = flow

    def fail(_request):
        raise subprocess.TimeoutExpired("preparation", 60)

    monkeypatch.setattr(adapter, "run_suite", fail)
    run = adapter.run_as_delivered(SuiteRunRequest(suite, 10))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "preparation" in run.reason
    assert run.counts.passed is None and run.wall_time_s is None


def test_delivered_journal_presence_accumulates_across_files(flow, monkeypatch):
    from langadapter.runs import RunCounts, SideEffects, SuiteRun

    adapter, suite, _, _ = flow
    root = Path(suite.root)
    second = root / "OtherTest.java"
    second.write_text("package example; class OtherTest {}", encoding="utf-8")
    suite = lb.Suite(suite.root, (*suite.test_files, second.name))
    calls = []

    def run(request):
        calls.append(request.suite.test_files)
        return SuiteRun(
            lb.Outcome.COMPLETED, reason="", counts=RunCounts(passed=1), wall_time_s=1,
            tests=(), unmatched=(), output="", coverage=None,
            identity=adapter._observation_identity(request.suite),
            side_effects=SideEffects(timeout_journal=len(calls) == 1))

    monkeypatch.setattr(adapter, "run_suite", run)
    delivered = adapter.run_as_delivered(SuiteRunRequest(suite, 10))
    assert calls == [("CalcTest.java",), ("OtherTest.java",)]
    assert delivered.outcome is lb.Outcome.COMPLETED
    assert delivered.counts.passed == 2 and delivered.wall_time_s == 2
    assert delivered.side_effects.timeout_journal


@pytest.mark.parametrize("spent, given, outcome, reason", [
    ((3, 1), [10, 7], lb.Outcome.COMPLETED, ""),
    ((3, 9), [10, 7], lb.Outcome.TIMED_OUT,
     "OtherTest.java: the suite's timeout ended the test run"),
    ((10, 1), [10], lb.Outcome.TIMED_OUT,
     "the timeout of 10 s ended before OtherTest.java ran"),
])
def test_delivered_timeout_bounds_the_whole_run(flow, monkeypatch, spent, given, outcome, reason):
    """Each file runs in what is left of the request's timeout; a run whose
    time is spent ends timed out before the next file."""
    from langadapter.runs import RunCounts, SuiteRun

    adapter, suite, _, _ = flow
    root = Path(suite.root)
    (root / "OtherTest.java").write_text("package example; class OtherTest {}", encoding="utf-8")
    suite = lb.Suite(suite.root, (*suite.test_files, "OtherTest.java"))
    clock = SimpleNamespace(now=100.0)
    limits = []

    def run(request):
        # The file takes its time of the clock, and past its limit times out.
        limits.append(request.timeout_s)
        took = spent[len(limits) - 1]
        clock.now += min(took, request.timeout_s)
        timed_out = took > request.timeout_s
        return SuiteRun(
            lb.Outcome.TIMED_OUT if timed_out else lb.Outcome.COMPLETED,
            reason="the suite's timeout ended the test run" if timed_out else "",
            counts=RunCounts(passed=0 if timed_out else 1),
            wall_time_s=None if timed_out else took, tests=(), unmatched=(), output="",
            coverage=None, identity=adapter._observation_identity(request.suite))

    monkeypatch.setattr(_collection, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(adapter, "run_suite", run)
    delivered = adapter.run_as_delivered(SuiteRunRequest(suite, 10))
    assert limits == given
    assert (delivered.outcome, delivered.reason) == (outcome, reason)


def test_container_coverage_genuine_zero_is_measured(flow, monkeypatch):
    adapter, suite, _, _ = flow
    monkeypatch.setattr(_jacoco, "analyze", lambda *args, **options: ({"suite": ["5:0:2:"]}, ""))
    run = adapter.run_suite(SuiteRunRequest(suite, 10, coverage_target="Calc.java"))
    assert run.outcome is lb.Outcome.COMPLETED
    assert run.coverage.covered == () and run.coverage.taken == ()
    assert run.coverage.statements == (5,) and len(run.coverage.branches) == 2


@pytest.mark.parametrize("stage", ["scratch", "compile", "tools", "agent", "classes"])
def test_container_preparation_failure_is_not_a_test_failure(flow, monkeypatch, stage):
    adapter, suite, _, calls = flow

    def broken(*args, **options):
        raise OSError("preparation unavailable")

    if stage == "scratch":
        monkeypatch.setattr(adapter, "_scratch", broken)
    elif stage == "compile":
        monkeypatch.setattr(java_adapter, "compile_suite", broken)
    elif stage == "tools":
        monkeypatch.setattr(_junit, "toolset", broken)
    elif stage == "agent":
        monkeypatch.setattr(_jacoco, "agent_argument", broken)
    else:
        monkeypatch.setattr(java_adapter, "_class_names", broken)
    run = adapter.run_suite(SuiteRunRequest(suite, 10, coverage_target="Calc.java"))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "preparation unavailable" in run.reason
    assert run.coverage is None and run.counts.collected is None
    assert "launch" not in calls


@pytest.mark.parametrize("operation", ["suite", "delivered", "collection", "evidence", "benchmark"])
@pytest.mark.parametrize("disposed", [False, True])
def test_public_container_observation_keeps_unresolved_command_error(
        flow, owned_stages, monkeypatch, operation, disposed, recwarn):
    adapter, suite, _, _ = flow
    error = OSError("compiler ownership unresolved")
    handle = SimpleNamespace(_langadapter_command_disposed=disposed)
    error._langadapter_command = handle

    def fail(*args, **kwargs):
        raise error

    module = {"suite": java_adapter, "delivered": java_adapter, "collection": _collection,
              "evidence": _evidence, "benchmark": _benchmark}[operation]
    monkeypatch.setattr(module, "compile_suite", fail)
    method, request = {
        "suite": (adapter.tests.run_suite, SuiteRunRequest(suite, 10)),
        "delivered": (adapter.tests.run_as_delivered, SuiteRunRequest(suite, 10)),
        "collection": (adapter.tests.collect_suite, CollectionRequest(suite, 10)),
        "evidence": (adapter.tests.per_test_evidence, PerTestEvidenceRequest(suite, "Calc.java", 10)),
        "benchmark": (adapter.tests.benchmark, BenchmarkRequest(suite, 1, 10, warmup_runs=0)),
    }[operation]
    if disposed:
        assert method(request).outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    else:
        with pytest.raises(OSError) as caught:
            method(request)
        assert caught.value is error and caught.value._langadapter_command is handle
    assert owned_stages
    for owner in owned_stages:
        owner.verify()
        assert owner.root.is_dir() and owner.receipt.is_file()


def test_unreadable_container_target_has_no_score_or_host_fallback(flow, monkeypatch):
    adapter, suite, container, calls = flow
    run = container.run

    def unavailable(argv, **options):
        if argv[0] == "sha256sum":
            raise subprocess.TimeoutExpired(argv, 60)
        return run(argv, **options)

    monkeypatch.setattr(container, "run", unavailable)
    measured = adapter.run_suite(SuiteRunRequest(suite, 10, coverage_target="Calc.java"))
    assert measured.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert measured.identity.target is None and measured.coverage is None
    assert measured.counts.collected is None and not calls


def test_unobtainable_container_tools_keep_missing_setup_distinct(flow, monkeypatch):
    adapter, suite, _, calls = flow

    def missing(*args, **options):
        raise _tools.ToolUnavailable("missing cached launcher")

    monkeypatch.setattr(_junit, "toolset", missing)
    measured = adapter.run_suite(SuiteRunRequest(suite, 10))
    assert measured.outcome is lb.Outcome.MISSING_SETUP
    assert "missing cached launcher" in measured.reason and "launch" not in calls


@pytest.fixture
def evidence_flow(flow, monkeypatch):
    adapter, suite, container, calls = flow
    cache = Path(suite.root).parent.parent / "external-cache"
    cache.mkdir()
    agent = cache / "agent.jar"
    agent.write_bytes(b"fixture agent")
    monkeypatch.setattr(_tools, "jar", lambda *args, **options: agent)
    monkeypatch.setattr(_evidence, "compile_suite", java_adapter.compile_suite)

    def helper(jdk, environment, env, **options):
        assert options["executor"] is container
        assert options["scratch"].is_relative_to(Path(suite.root).parent)
        path = options["scratch"] / "listener"
        path.mkdir()
        calls["listener"] = options
        return path

    def analyzer(jdk, environment, env, **options):
        assert options["executor"] is container
        calls["analyzer"] = options
        return options["scratch"] / "analyzer", ()

    launches = []

    def launch(java, classpath, classes, reports, **options):
        assert options["executor"] is container
        assert java == "/opt/jdk/bin/java" and classes == ["example.CalcTest"]
        assert "-ea" in options["jvm_args"]
        assert "output=none" in options["jvm_args"][0]
        assert options["jvm_args"][0].startswith("-javaagent:/workzone/")
        assert "/tools/" in options["jvm_args"][0]
        data = Path(next(value.split("=", 1)[1] for value in options["jvm_args"]
                         if value.startswith(f"-D{_evidence.PROPERTY}=")))
        assert f"-D{_evidence.PRELOAD}=example.Calc" in options["jvm_args"]
        unique = "[engine:junit-jupiter]/[class:example.CalcTest]/[method:passes()]"
        (data / "index.tsv").write_text(f"1\t{unique}\n", encoding="utf-8")
        (data / "times.tsv").write_text(f"{unique}\t10000000\n", encoding="utf-8")
        launches.append(options)
        return _junit.Launch(0, "fixture", ())

    def analyze(jdk, mode, data, target, main_classes, **options):
        assert options["executor"] is container and mode == "tests"
        calls["analyze"] = options
        return {"suite": ["5:1:2:0"], "1": ["5:0"]}, ""

    monkeypatch.setattr(_evidence, "listener", helper)
    monkeypatch.setattr(_jacoco, "analyzer", analyzer)
    monkeypatch.setattr(_jacoco, "analyze", analyze)
    monkeypatch.setattr(_junit, "launch", launch)
    return adapter, suite, container, calls, launches


@pytest.mark.parametrize("warmup", [False, True])
def test_container_per_test_evidence_routes_tools_and_keeps_only_measured_pass(evidence_flow, warmup):
    adapter, suite, container, calls, launches = evidence_flow
    result = adapter.per_test_evidence(PerTestEvidenceRequest(suite, "Calc.java", 10, warmup=warmup))
    assert result.outcome is lb.Outcome.COMPLETED
    assert len(launches) == (2 if warmup else 1)
    assert set(calls) == {"compile", "tools", "listener", "analyzer", "analyze"}
    assert result.identity.target == result.coverage.target
    assert result.identity.target.sha256 == hashlib.sha256(container.source).hexdigest()
    assert result.tests[0].test.file == "CalcTest.java" and result.tests[0].time_ms == 10
    assert result.suite_time_ms == 10
    assert {item.file for item in result.tests[0].items} == {"Calc.java"}
    assert len(result.tests[0].items) == 2


@pytest.mark.parametrize("stage", ["scratch", "compile", "listener", "analyzer"])
def test_container_evidence_preparation_failure_is_not_complete(
        evidence_flow, monkeypatch, owned_stages, recwarn, stage):
    adapter, suite, _, _, launches = evidence_flow

    def unavailable(*args, **options):
        raise OSError("evidence preparation unavailable")

    if stage == "scratch":
        monkeypatch.setattr(adapter, "_scratch", unavailable)
    elif stage == "compile":
        monkeypatch.setattr(_evidence, "compile_suite", unavailable)
    elif stage == "listener":
        monkeypatch.setattr(_evidence, "listener", unavailable)
    else:
        monkeypatch.setattr(_jacoco, "analyzer", unavailable)
    result = adapter.per_test_evidence(PerTestEvidenceRequest(suite, "Calc.java", 10))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "evidence preparation unavailable" in result.reason
    assert result.tests == () and result.coverage is None and not launches
    assert len(owned_stages) == (0 if stage == "scratch" else 1)
    for owned in owned_stages:
        owned.verify()
    assert bool(recwarn.list) is bool(owned_stages)


def test_container_evidence_rejects_malformed_observation_without_partial_items(evidence_flow, monkeypatch):
    adapter, suite, _, _, _ = evidence_flow
    monkeypatch.setattr(_jacoco, "analyze", lambda *args, **options: (
        {"suite": ["5:1:2:0"], "1": ["not-a-line:0"]}, ""))
    result = adapter.per_test_evidence(PerTestEvidenceRequest(suite, "Calc.java", 10))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "coverage observation" in result.reason and result.tests == ()


@pytest.mark.parametrize("warmup", [False, True])
def test_container_evidence_timeout_keeps_no_measured_evidence(
        evidence_flow, monkeypatch, owned_stages, recwarn, warmup):
    adapter, suite, _, _, _ = evidence_flow
    monkeypatch.setattr(_junit, "launch", lambda *args, **options: _junit.Launch(
        None, "partial output", (), timed_out=True))
    result = adapter.per_test_evidence(PerTestEvidenceRequest(suite, "Calc.java", 10, warmup=warmup))
    assert result.outcome is lb.Outcome.TIMED_OUT
    assert ("warm-up" if warmup else "measured") + " run" in result.reason
    assert result.tests == () and result.coverage is None
    assert len(owned_stages) == 1
    owned_stages[0].verify()
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.mark.parametrize("warmup", [False, True])
@pytest.mark.parametrize("failure", [OSError, subprocess.SubprocessError])
def test_container_evidence_launch_failure_retains_stage_without_fabricated_data(
        evidence_flow, monkeypatch, owned_stages, recwarn, warmup, failure):
    adapter, suite, _, _, _ = evidence_flow

    def unavailable(*args, **options):
        raise failure("controlled launcher unavailable")

    monkeypatch.setattr(_junit, "launch", unavailable)
    result = adapter.per_test_evidence(PerTestEvidenceRequest(suite, "Calc.java", 10, warmup=warmup))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert result.tests == () and result.coverage is None and result.suite_time_ms is None
    assert "controlled launcher unavailable" in result.reason
    assert len(owned_stages) == 1
    owned_stages[0].verify()
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.fixture
def benchmark_flow(flow, monkeypatch):
    adapter, suite, container, calls = flow
    monkeypatch.setattr(_benchmark, "compile_suite", java_adapter.compile_suite)
    monkeypatch.setattr(_benchmark, "selected_executable", lambda *args:
                        pytest.fail("a container benchmark must not resolve a host placement tool"))
    monkeypatch.setattr(_benchmark.placement, "run_timed", lambda *args, **options:
                        pytest.fail("a container benchmark must not use the host timed launcher"))
    original = container.run
    launches = []

    def run(argv, **options):
        if argv == ["sh", "-c", "command -v taskset"]:
            calls["taskset"] = options
            return subprocess.CompletedProcess(argv, 0, "/usr/bin/taskset\n", "")
        if "/proc/self/status" in argv:
            calls["affinity"] = (argv, options)
            return subprocess.CompletedProcess(argv, 0, "Name:\tcat\nCpus_allowed_list:\t3-4,8\n", "")
        if "/opt/jdk/bin/java" in argv:
            assert options["cwd"] == "/project" and options["timeout"] == 10
            assert options["env"]["JAVA_EXTRA"] == "selected"
            mapped = argv[-1]
            assert mapped.startswith("@/workzone/")
            argfile = Path(container._host_work_dir) / mapped.removeprefix("@/workzone/")
            content = argfile.read_text(encoding="utf-8")
            assert "/workzone/" in content and str(container._host_work_dir) not in content
            assert '"-ea"' in content and '"-Dfixture=selected"' in content
            launches.append((argv, options, content))
            return subprocess.CompletedProcess(argv, 0, "benchmark fixture", "")
        return original(argv, **options)

    monkeypatch.setattr(container, "run", run)
    clock = iter(range(100))
    monkeypatch.setattr(_benchmark.time, "perf_counter", lambda: next(clock))
    return adapter, suite, container, calls, launches


def test_container_benchmark_keeps_warmup_progress_affinity_and_selected_launch(benchmark_flow):
    adapter, suite, _, calls, launches = benchmark_flow
    progress = []
    result = adapter.benchmark(BenchmarkRequest(
        suite, 2, 10, warmup_runs=1, cpus="3-4,8", progress=progress.append))
    assert result.outcome is lb.Outcome.COMPLETED and result.times_ms == (1000, 1000)
    assert result.mean_ms == result.min_ms == result.max_ms == 1000
    assert result.cpus == "3-4,8" and result.allowed_cpus == (3, 4, 8)
    assert result.returncode == 0 and result.identity.execution.kind == "container"
    assert "host-to-guest execution wrapper" in result.includes
    assert len(launches) == 3 and all(argv[:3] == ["/usr/bin/taskset", "-c", "3-4,8"]
                                      for argv, _, _ in launches)
    assert calls["affinity"][0] == ["/usr/bin/taskset", "-c", "3-4,8", "cat", "/proc/self/status"]
    assert [(event.kind, event.index, event.total) for event in progress] == [
        ("warmup", 1, 1), ("measured", 1, 2), ("measured", 2, 2)]


@pytest.mark.parametrize("address", ["taskset", "/tool/../taskset", "/usr/bin/taskset\n/other/taskset"])
def test_container_benchmark_refuses_unbound_placement_address(benchmark_flow, monkeypatch, address):
    adapter, suite, container, _, launches = benchmark_flow
    original = container.run

    def run(argv, **options):
        if argv == ["sh", "-c", "command -v taskset"]:
            return subprocess.CompletedProcess(argv, 0, address, "")
        return original(argv, **options)

    monkeypatch.setattr(container, "run", run)
    result = adapter.benchmark(BenchmarkRequest(suite, 1, 10, warmup_runs=0, cpus="2"))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert result.times_ms == () and not launches


@pytest.mark.parametrize("failure", ["timeout", "start", "tests"])
def test_container_benchmark_keeps_completed_samples_without_partial_statistics(
        benchmark_flow, monkeypatch, owned_stages, recwarn, failure):
    adapter, suite, container, _, launches = benchmark_flow
    original = container.run

    def fail_second(argv, **options):
        if "/opt/jdk/bin/java" in argv and len(launches) == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 10)
            if failure == "start":
                raise OSError("second launcher unavailable")
            monkeypatch.setattr(_junit, "read_reports", lambda paths: [
                _junit.Case("example.CalcTest", "fails()", "failed", 0.01, "fixture failure")])
            return subprocess.CompletedProcess(argv, 1, "second test failure", "")
        return original(argv, **options)

    monkeypatch.setattr(container, "run", fail_second)
    result = adapter.benchmark(BenchmarkRequest(suite, 2, 10, warmup_runs=0))
    assert result.outcome is {"timeout": lb.Outcome.TIMED_OUT,
                              "start": lb.Outcome.INFRASTRUCTURE_FAILURE,
                              "tests": lb.Outcome.TEST_FAILURES}[failure]
    assert result.times_ms == (1000,) and result.mean_ms is result.min_ms is result.max_ms is None
    assert result.returncode == (1 if failure == "tests" else None)
    assert len(owned_stages) == 1
    if failure == "tests":
        assert not owned_stages[0].root.exists() and not owned_stages[0].receipt.exists()
        assert not recwarn.list
    else:
        owned_stages[0].verify()
        assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.mark.parametrize("stage", ["scratch", "compile", "tools", "classes", "arguments"])
def test_container_benchmark_preparation_failure_has_no_measurement(benchmark_flow, monkeypatch, stage):
    adapter, suite, _, _, launches = benchmark_flow

    def unavailable(*args, **options):
        raise OSError("benchmark preparation unavailable")

    if stage == "scratch":
        monkeypatch.setattr(adapter, "_scratch", unavailable)
    elif stage == "compile":
        monkeypatch.setattr(_benchmark, "compile_suite", unavailable)
    elif stage == "tools":
        monkeypatch.setattr(_junit, "toolset", unavailable)
    elif stage == "classes":
        monkeypatch.setattr(adapter, "_class_names", unavailable)
    else:
        monkeypatch.setattr(_junit, "argfile", unavailable)
    result = adapter.benchmark(BenchmarkRequest(suite, 1, 10, warmup_runs=0))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "benchmark preparation unavailable" in result.reason
    assert result.times_ms == () and result.mean_ms is None and not launches


def test_container_benchmark_later_argument_failure_keeps_samples_not_stale_exitcode(benchmark_flow, monkeypatch):
    adapter, suite, _, _, launches = benchmark_flow
    original = _junit.argfile

    def unavailable(path, arguments):
        if launches:
            raise OSError("second arguments unavailable")
        return original(path, arguments)

    monkeypatch.setattr(_junit, "argfile", unavailable)
    result = adapter.benchmark(BenchmarkRequest(suite, 2, 10, warmup_runs=0))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert result.times_ms == (1000,) and result.returncode is None and result.mean_ms is None


def test_container_benchmark_missing_taskset_is_not_a_host_probe(benchmark_flow, monkeypatch):
    adapter, suite, container, _, launches = benchmark_flow
    monkeypatch.setattr(container, "run", lambda argv, **options:
                        subprocess.CompletedProcess(argv, 1, "", "not installed"))
    result = adapter.benchmark(BenchmarkRequest(suite, 1, 10, cpus="3"))
    assert result.outcome is lb.Outcome.UNSUPPORTED
    assert "selected container" in result.reason and result.cpus == "3" and not launches


def test_container_benchmark_failed_taskset_probe_is_infrastructure(benchmark_flow, monkeypatch):
    adapter, suite, container, _, launches = benchmark_flow

    def unavailable(*args, **options):
        raise OSError("container unavailable")

    monkeypatch.setattr(container, "run", unavailable)
    result = adapter.benchmark(BenchmarkRequest(suite, 1, 10, cpus="3"))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "container unavailable" in result.reason and not launches


def test_container_benchmark_unmounted_scratch_is_refused_before_compile(benchmark_flow, tmp_path, monkeypatch):
    from contextlib import nullcontext

    adapter, suite, _, calls, launches = benchmark_flow
    monkeypatch.setattr(adapter, "_scratch", lambda name: nullcontext(tmp_path / "unmounted"))
    result = adapter.benchmark(BenchmarkRequest(suite, 1, 10, warmup_runs=0))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "compile" not in calls and not launches and not (tmp_path / "unmounted").exists()


def test_container_benchmark_unreadable_affinity_is_missing_not_zero(benchmark_flow, monkeypatch):
    adapter, suite, container, _, _ = benchmark_flow
    original = container.run

    def no_affinity(argv, **options):
        if "/proc/self/status" in argv:
            raise OSError("affinity unavailable")
        return original(argv, **options)

    monkeypatch.setattr(container, "run", no_affinity)
    result = adapter.benchmark(BenchmarkRequest(suite, 1, 10, warmup_runs=0))
    assert result.outcome is lb.Outcome.COMPLETED and result.allowed_cpus is None
    assert result.times_ms == (1000,)


@pytest.mark.parametrize("report,code,expected", [
    ("Cpus_allowed_list:\t2-4,9\n", 0, (2, 3, 4, 9)),
    ("Cpus_allowed_list:\t0\n", 0, (0,)),
    ("Cpus_allowed_list:\t2\n", 1, None),
    ("Cpus_allowed_list:\t\n", 0, None),
    ("Cpus_allowed:\tff\n", 0, None),
    ("Cpus_allowed_list:\t1\nCpus_allowed_list:\t2\n", 0, None),
    ("Cpus_allowed_list:\t4-2\n", 0, None),
    ("Cpus_allowed_list:\t1,1\n", 0, None),
    ("Cpus_allowed_list:\t-1\n", 0, None),
    ("Cpus_allowed_list:\t1;touch injected\n", 0, None),
])
def test_container_affinity_probe_uses_no_container_python(report, code, expected):
    def run(argv, **options):
        assert argv == ["taskset", "-c", "2", "cat", "/proc/self/status"]
        assert options == {"cwd": "/project", "env": {"FIXTURE": "selected"}, "timeout": 60}
        return subprocess.CompletedProcess(argv, code, report, "")

    result = _benchmark._container_allowed_cpus(SimpleNamespace(run=run), ["taskset", "-c", "2"],
                                            "/project", {"FIXTURE": "selected"})
    assert result == expected


@pytest.fixture
def mutation_flow(flow, monkeypatch):
    adapter, suite, container, calls = flow
    # Also bypasses the session's own public context guard for this fixture.
    monkeypatch.setattr(adapter, "_context_issue", lambda: "")
    monkeypatch.setattr(_mutation, "compile_suite", java_adapter.compile_suite)
    operator = "org.pitest.mutationtest.engine.gregor.mutators.NegateConditionalsMutator"

    def ids(java, names, scratch, **options):
        assert options["executor"] is container and java == "/opt/jdk/bin/java"
        assert names == ["NEGATE_CONDITIONALS"]
        calls["ids"] = options
        return (operator,)

    def order(java, environment, env, **options):
        assert options["executor"] is container and java == "/opt/jdk/bin/javac"
        assert options["scratch"].is_relative_to(Path(suite.root).parent)
        calls["order"] = options
        return options["scratch"]

    launches = []

    def run(java, **options):
        assert options["executor"] is container and java == "/opt/jdk/bin/java"
        assert options["env"]["JAVA_EXTRA"] == "selected" and options["cwd"] == "/project"
        assert options["target_classes"] == ("example.Calc",)
        assert options["test_classes"] == ["example.CalcTest"]
        assert options["mutators"] == ["NEGATE_CONDITIONALS"]
        assert options["threads"] == 1 and options["all_killers"] is True
        assert options["timeout_factor"] == 1.25 and options["timeout_constant_s"] == 4
        assert options["jvm_args"] == ["-ea"] and options["system_properties"] == {"fixture": "selected"}
        assert options["wall_clock_s"] == 11
        launches.append(options)
        root = ET.Element("mutations")
        for index, status in enumerate(("KILLED", "SURVIVED")):
            node = ET.SubElement(root, "mutation", status=status, numberOfTestsRun="1")
            for key, value in {"mutatedClass": "example.Calc", "mutatedMethod": "sign",
                               "methodDescription": "(I)I", "lineNumber": "1", "mutator": operator,
                               "description": "negated conditional",
                               "killingTest": ("[engine:junit-jupiter]/[class:example.CalcTest]/[method:passes()]"
                                               if status == "KILLED" else "")}.items():
                ET.SubElement(node, key).text = value
            ET.SubElement(ET.SubElement(node, "indexes"), "index").text = str(index)
        report = options["scratch"] / "mutations.xml"
        ET.ElementTree(root).write(report, encoding="utf-8")
        return _pit.Report(0, "fixture", report)

    monkeypatch.setattr(_pit, "mutator_ids", ids)
    monkeypatch.setattr(_pit, "test_order", order)
    monkeypatch.setattr(_pit, "run", run)
    request = MutationRequest("Calc.java", suite, catalogue=Catalogue(operators=(operator,)),
                              attribution="all", workers=1, wall_clock_limit_s=11)
    return adapter, request, container, calls, launches


def test_container_pit_session_routes_tools_target_and_full_measured_lineage(mutation_flow):
    adapter, request, container, calls, launches = mutation_flow
    with adapter.open_mutation_session("pit", workers=2) as session:
        first = session.run(request)
        second = session.run(request)
        assert session.workers_started == 2 and len(launches) == 2
    assert first.outcome is second.outcome is lb.Outcome.COMPLETED
    assert first.complete and first.score() == second.score() == 50
    assert first.identity == second.identity
    assert first.identity.observation.execution.kind == "container"
    assert first.identity.observation.execution.image == "selected-image"
    assert first.identity.observation.target.path == "Calc.java"
    assert first.identity.observation.target.sha256 == hashlib.sha256(container.source).hexdigest()
    assert {mutant.key.file for mutant in first.mutants} == {"Calc.java"}
    assert {mutant.row["native_status"] for mutant in first.mutants} == {"KILLED", "SURVIVED"}
    assert first.mutants[0].killing_tests[0].qualname == "CalcTest.passes"
    assert first.resolution == second.resolution
    assert second.resolution.requested == "measure" and second.resolution.method == "measured"
    assert set(calls) == {"ids", "order", "tools", "compile"}
    assert not session.is_open and session._prepared is None


def test_pit_run_with_first_killers_only_records_attribution_first(mutation_flow, monkeypatch):
    """When PIT's run of every covering test gives no report, each kill names
    its first killing test only, and the run's identity says so."""
    adapter, request, _container, _calls, _launches = mutation_flow
    launch = _pit.run
    monkeypatch.setattr(_pit, "run", lambda java, **options: _pit.Report(
        0, "fixture", launch(java, **options).mutations, note="first killers only"))
    with adapter.open_mutation_session("pit", workers=1) as session:
        run = session.run(request)
    assert run.outcome is lb.Outcome.COMPLETED and run.reason == "first killers only"
    assert run.identity.attribution == "first"


@pytest.mark.parametrize("stage", ["scratch", "ids", "order", "tools", "compile", "classes", "target"])
def test_container_pit_preparation_failure_has_no_scored_observation(
        mutation_flow, owned_stages, monkeypatch, recwarn, stage):
    adapter, request, container, _, launches = mutation_flow

    def unavailable(*args, **options):
        raise OSError("PIT preparation unavailable")

    if stage == "scratch":
        monkeypatch.setattr(adapter, "_scratch", unavailable)
    elif stage == "ids":
        monkeypatch.setattr(_pit, "mutator_ids", unavailable)
    elif stage == "order":
        monkeypatch.setattr(_pit, "test_order", unavailable)
    elif stage == "tools":
        monkeypatch.setattr(_junit, "toolset", unavailable)
    elif stage == "compile":
        monkeypatch.setattr(_mutation, "compile_suite", unavailable)
    elif stage == "classes":
        monkeypatch.setattr(adapter, "_class_names", unavailable)
    else:
        original = container.run

        def no_identity(argv, **options):
            if argv[0] == "sha256sum":
                raise OSError("PIT preparation unavailable")
            return original(argv, **options)

        monkeypatch.setattr(container, "run", no_identity)
    with adapter.open_mutation_session("pit") as session:
        result = session.run(request)
        assert session.is_open
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "PIT preparation unavailable" in result.reason and not launches
    assert result.score() is None and not result.measured and result.identity is None
    assert len(owned_stages) == (0 if stage == "scratch" else 1)
    for owned in owned_stages:
        owned.verify()
    assert sum("retained after interrupted operation" in str(w.message)
               for w in recwarn.list) == len(owned_stages)


def test_container_pit_transient_target_probe_is_failed_result_and_session_remains_reusable(mutation_flow, monkeypatch):
    adapter, request, _, _, launches = mutation_flow
    original = adapter._project_file
    probes = []

    def probe(*args):
        probes.append(args)
        if len(probes) == 1:
            raise subprocess.TimeoutExpired(["test", "-f"], 60)
        return original(*args)

    monkeypatch.setattr(adapter, "_project_file", probe)
    with adapter.open_mutation_session("pit") as session:
        failed = session.run(request)
        assert failed.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE and failed.score() is None
        assert failed.settings["workers"].value == 1 and session.is_open and not launches
        complete = session.run(request)
        assert complete.outcome is lb.Outcome.COMPLETED and len(launches) == 1


def test_container_pit_empty_catalogue_checks_selected_compiled_target(mutation_flow, monkeypatch):
    adapter, request, container, _, _ = mutation_flow
    monkeypatch.setattr(_pit, "run", lambda *args, **options: _pit.Report(0, _pit.NO_MUTATIONS, None))
    with adapter.open_mutation_session("pit") as session:
        result = session.run(request)
    assert result.outcome is lb.Outcome.COMPLETED and result.complete and result.score() is None
    assert result.reason == "the catalogue has no mutant for this target"
    assert any(argv == ["test", "-f", "/project/target/classes/example/Calc.class"]
               for argv, _ in container.calls)


def test_container_pit_no_mutations_does_not_hide_missing_container_class(
        mutation_flow, owned_stages, monkeypatch, recwarn):
    adapter, request, container, _, _ = mutation_flow
    original = container.run

    def no_class(argv, **options):
        if argv[-1].endswith("Calc.class"):
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original(argv, **options)

    monkeypatch.setattr(container, "run", no_class)
    monkeypatch.setattr(_pit, "run", lambda *args, **options: _pit.Report(0, _pit.NO_MUTATIONS, None))
    with adapter.open_mutation_session("pit") as session:
        result = session.run(request)
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE and not result.measured
    assert "not among" in result.reason and result.score() is None
    assert len(owned_stages) == 1
    owned_stages[0].verify()
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


def test_container_pit_report_source_failure_keeps_identity_but_no_score(
        mutation_flow, owned_stages, monkeypatch, recwarn):
    adapter, request, container, _, launches = mutation_flow
    original = container.run

    def source_unavailable(argv, **options):
        if argv == ["cat", "--", "/project/Calc.java"] and launches:
            raise OSError("source observation unavailable")
        return original(argv, **options)

    monkeypatch.setattr(container, "run", source_unavailable)
    with adapter.open_mutation_session("pit") as session:
        result = session.run(request)
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "source observation unavailable" in result.reason and not result.measured
    assert result.score() is None and result.identity.observation.execution.kind == "container"
    assert len(owned_stages) == 1
    owned_stages[0].verify()
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


def test_container_pit_wall_timeout_retains_incomplete_native_outcome(
        mutation_flow, owned_stages, monkeypatch, recwarn):
    adapter, request, _, _, _ = mutation_flow
    monkeypatch.setattr(_pit, "run", lambda *args, **options: _pit.Report(None, "partial", None, timed_out=True))
    with adapter.open_mutation_session("pit") as session:
        result = session.run(request)
    assert result.outcome is lb.Outcome.TIMED_OUT and not result.complete and result.score() is None
    assert result.identity.observation.execution.kind == "container"
    assert result.incomplete["reason"] == "wall_clock_limit_before_report"
    assert len(owned_stages) == 1
    owned_stages[0].verify()
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.mark.parametrize("disposed", [False, True])
def test_public_container_mutation_failure_preserves_owner_without_a_score(
        mutation_flow, owned_stages, monkeypatch, disposed, recwarn):
    adapter, request, _, _, _ = mutation_flow
    error = subprocess.TimeoutExpired(["pit"], 11)
    owner = SimpleNamespace(_langadapter_command_disposed=disposed)
    error._langadapter_command = owner

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(_pit, "run", fail)
    session = adapter.mutations.open_session("pit")
    if disposed:
        result = session.run(request)
        assert result.score() is None and not result.measured
        assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
        session.close()
    else:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            session.run(request)
        assert caught.value is error and caught.value._langadapter_command is owner
    assert not session.is_open
    assert owned_stages and all(item.receipt.is_file() for item in owned_stages)


@pytest.mark.parametrize("stage", ["launch", "malformed", "missing", "unresolved", "untested"])
def test_container_pit_incomplete_execution_preserves_its_owned_report(
        mutation_flow, owned_stages, monkeypatch, recwarn, stage):
    adapter, request, _, _, _ = mutation_flow
    run = _pit.run

    def report(java, **options):
        if stage == "launch":
            raise subprocess.SubprocessError("controlled PIT communication failure")
        if stage == "missing":
            return _pit.Report(0, "no report", None)
        result = run(java, **options)
        if stage == "malformed":
            result.mutations.write_text("<mutations>", encoding="utf-8")
        else:
            tree = ET.parse(result.mutations)
            first = tree.getroot()[0]
            if stage == "unresolved":
                first.find("killingTest").text = ""
            else:
                first.set("status", "NOT_STARTED")
                first.find("killingTest").text = ""
            tree.write(result.mutations, encoding="utf-8")
        return result

    monkeypatch.setattr(_pit, "run", report)
    if stage == "unresolved":
        from dataclasses import replace
        request = replace(request, fail_on_unresolved=True)
    with adapter.open_mutation_session("pit") as session:
        result = session.run(request)
        assert session.is_open
    assert result.outcome is (lb.Outcome.COMPLETED if stage == "untested"
                              else lb.Outcome.INFRASTRUCTURE_FAILURE)
    assert result.score() is None
    if stage == "untested":
        assert not result.complete and result.summary["untested"] == 1
        assert result.summary["survived"] == 1 and len(result.mutants) == 2
    assert len(owned_stages) == 1
    owned_stages[0].verify()
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.mark.parametrize("status", ["TIMED_OUT", "SURVIVED", "NON_VIABLE", "RED_SUITE"])
def test_container_pit_native_verdict_is_not_an_infrastructure_retention_signal(
        mutation_flow, owned_stages, monkeypatch, recwarn, status):
    adapter, request, _, _, _ = mutation_flow
    run = _pit.run

    def report(java, **options):
        if status == "RED_SUITE":
            return _pit.Report(1, _pit.RED_SUITE, None)
        result = run(java, **options)
        tree = ET.parse(result.mutations)
        for node in tree.getroot():
            node.set("status", status)
            node.find("killingTest").text = ""
        tree.write(result.mutations, encoding="utf-8")
        return result

    monkeypatch.setattr(_pit, "run", report)
    with adapter.open_mutation_session("pit") as session:
        result = session.run(request)
    assert result.outcome is (lb.Outcome.TEST_FAILURES if status == "RED_SUITE" else lb.Outcome.COMPLETED)
    # A red suite measured no mutant, so its run is not complete, and no run
    # carries the incomplete mark that retains the scratch.
    assert result.complete is (status != "RED_SUITE") and result.incomplete is None
    if status == "TIMED_OUT":
        assert result.score() == 100
        assert all(m.row["native_status"] == "TIMED_OUT" for m in result.mutants)
    elif status == "SURVIVED":
        assert result.score() == 0
    elif status == "NON_VIABLE":
        assert result.score() == 100
        assert all(m.row["native_status"] == "NON_VIABLE" for m in result.mutants)
    else:
        assert result.score() is None
    assert len(owned_stages) == 1
    assert not owned_stages[0].root.exists() and not owned_stages[0].receipt.exists()
    assert not recwarn.list


@pytest.mark.mutation_tool
@pytest.mark.parametrize("operation", ["run", "evidence", "benchmark", "mutation", "delivered"])
def test_real_offline_container_observations(tmp_path, monkeypatch, operation):
    container = os.environ.get("LANGADAPTER_TEST_CONTAINER")
    java = os.environ.get("LANGADAPTER_TEST_CONTAINER_JAVA")
    libraries = os.environ.get("LANGADAPTER_TEST_CONTAINER_JUNIT_CLASSPATH")
    zone = os.environ.get("LANGADAPTER_TEST_WORK_DIR")
    cache = os.environ.get("LANGADAPTER_TEST_TOOL_CACHE")
    helper = os.environ.get("LANGADAPTER_TEST_CONTAINER_HELPER_PYTHON")
    if not all((container, java, libraries, zone, cache, helper)):
        pytest.skip("requires prepared offline container JDK, JUnit, coverage tools and control helper")

    def forbidden(*args, **options):
        pytest.fail("container observation attempted a download or direct host Java launch")

    monkeypatch.setattr(_tools.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    root = tmp_path / "fixture"
    root.mkdir()
    target = root / "Calc.java"
    target.write_text("""public class Calc {
    public static int sign(int value) {
        if (value > 0) return 1;
        return -1;
    }
}
""", encoding="utf-8")
    test = root / "CalcTest.java"
    test.write_text("""import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
public class CalcTest {
    @Test void positive() { assertEquals(1, Calc.sign(1)); }
    @Test void negative() { assertEquals(-1, Calc.sign(-1)); }
}
""", encoding="utf-8")
    container_executor = ContainerExecutor(container, work_dir=zone, container_work_dir="/workzone",
                              helper_python=helper)
    adapter = java_adapter.JavaAdapter(lb.ProjectContext(
        root=container_executor._to_container(str(root)), interpreter=lb.Interpreter(path=java),
        execution=lb.ContainerExecution(container, zone, container_work_dir="/workzone",
                                        helper_python=helper),
        environment={"LANGADAPTER_JAVA_TOOLS": cache}), executor=container_executor)
    selected = adapter._jdk()
    classes = root / "main-classes"
    compiled = container_executor.run([selected.tool("javac"), "--release", "17", "-g",
                          "-d", str(classes), str(target)], timeout=60)
    assert compiled.returncode == 0, (compiled.stdout, compiled.stderr)
    container_classes = container_executor._to_container(str(classes))
    project = JavaProject(adapter.context.root, "maven", 17, (), (), (container_classes,), (),
                          (container_classes, *libraries.split(":")))
    # Execution is real. Project resolution and public admission are separate gates.
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.COMPLETED, project))
    monkeypatch.setattr(adapter, "_request_issue", lambda request: "")
    suite = lb.Suite(str(root), (test.name,))
    if operation == "delivered":
        original = test.read_bytes()
        replacement = test.read_text(encoding="utf-8") + "\n"
        result = adapter.run_as_delivered(
            SuiteRunRequest(suite, 60, timeout_journal=True), sources={test.name: replacement})
        assert result.outcome is lb.Outcome.COMPLETED, (result.reason, result.output)
        assert result.counts.passed == 2 and len(result.tests) == 2
        assert {item.test.qualname for item in result.tests} == {"CalcTest.positive", "CalcTest.negative"}
        assert result.identity.execution.kind == "container" and result.identity.target is None
        assert result.coverage is None and result.side_effects.timeout_journal
        assert result.identity.suite[0].sha256 == hashlib.sha256(replacement.encode("utf-8")).hexdigest()
        assert test.read_bytes() == original
        return
    if operation == "mutation":
        monkeypatch.setattr(adapter, "_context_issue", lambda: "")
        boundary = "org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator"
        request = MutationRequest(target.name, suite, catalogue=Catalogue(operators=(boundary,)),
                                  attribution="all", workers=1, wall_clock_limit_s=120)
        with adapter.open_mutation_session("pit", workers=1) as session:
            first = session.run(request)
            second = session.run(request)
        assert first.outcome is second.outcome is lb.Outcome.COMPLETED, (first.reason, second.reason)
        assert first.complete and second.complete and first.score() == second.score() == 0
        assert len(first.mutants) == len(second.mutants) == 1
        assert first.mutants[0].row["native_status"] == second.mutants[0].row["native_status"] == "SURVIVED"
        assert first.identity == second.identity
        assert first.identity.observation.execution.kind == "container"
        assert first.identity.observation.target.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
        assert first.identity.observation.target.path == target.name
        assert second.resolution.method == "measured" and not session.is_open
        return
    if operation == "benchmark":
        env = java_adapter.tool_environment(selected, dict(adapter.context.environment), executor=container_executor)
        allowed = _benchmark._container_allowed_cpus(container_executor, [], project.root, env)
        assert allowed, "the prepared Linux container must expose allowed CPU affinity"
        progress = []
        result = adapter.benchmark(BenchmarkRequest(
            suite, 2, 60, warmup_runs=1, cpus=str(allowed[0]), progress=progress.append))
        assert result.outcome is lb.Outcome.COMPLETED, result.reason
        assert result.allowed_cpus == (allowed[0],) and result.cpus == str(allowed[0])
        assert len(result.times_ms) == 2 and all(time > 0 for time in result.times_ms)
        assert result.min_ms <= result.mean_ms <= result.max_ms and result.returncode == 0
        assert result.identity.execution.kind == "container" and result.identity.target is None
        assert "host-to-guest execution wrapper" in result.includes
        assert [(event.kind, event.index) for event in progress] == [
            ("warmup", 1), ("measured", 1), ("measured", 2)]
        return
    if operation == "run":
        result = adapter.run_suite(SuiteRunRequest(
            suite, 60, coverage_target=target.name, timeout_journal=True))
        assert result.outcome is lb.Outcome.COMPLETED, (result.reason, result.output)
        assert result.counts.passed == 2
        assert {item.test.qualname for item in result.tests} == {"CalcTest.positive", "CalcTest.negative"}
    else:
        result = adapter.per_test_evidence(PerTestEvidenceRequest(suite, target.name, 60, warmup=True))
        assert result.outcome is lb.Outcome.COMPLETED, result.reason
        assert len(result.tests) == 2 and not result.untimed and not result.unmatched_contexts
        assert all(item.items for item in result.tests)
    assert result.identity.execution.kind == "container"
    assert result.identity.target == result.coverage.target
    assert result.identity.target.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    assert (len(result.coverage.branches), len(result.coverage.taken)) == (2, 2)


def test_a_container_jdk_is_read_once_and_a_probe_that_cannot_run_fails_the_resolution(
        tmp_path, monkeypatch):
    """A container JDK probe whose command could not run is no answer about
    the JDK: the resolution is an infrastructure failure, not a request
    error. A JDK once found is not read again."""
    zone = tmp_path / "zone"
    zone.mkdir()

    class Probe(ContainerExecutor):
        def __init__(self):
            super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone",
                             helper_python="/control/python")
            self.calls, self.down = [], True

        def run(self, argv, **options):
            self.calls.append(argv)
            if self.down:
                raise OSError("container stopped")
            output = 'JAVA_VERSION="21.0.2"\n' if argv[0] == "cat" else "javac 21.0.2"
            return subprocess.CompletedProcess(argv, 0, output, "")

    container = Probe()
    adapter = java_adapter.JavaAdapter(lb.ProjectContext(
        root="/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution(container="selected", image="selected-image",
                                        work_dir=str(zone), container_work_dir="/workzone",
                                        helper_python="/control/python")), executor=container)
    monkeypatch.setattr(adapter, "_context_issue", lambda: "")
    monkeypatch.setattr(adapter, "execution_setup_issue", lambda: "")
    resolution = adapter.project()
    assert resolution.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "could not be probed" in resolution.reason
    assert adapter._read_jdk() is None
    container.down = False
    container.calls.clear()
    assert adapter._jdk().home == "/opt/jdk" and adapter._jdk() is adapter._jdk()
    assert sum(argv[0] == "cat" for argv in container.calls) == 1


def test_a_timeout_cap_pit_cannot_apply_refuses_its_request_and_leaves_the_session_open(
        mutation_flow):
    """PIT times each test by a factor and a constant and has no cap: a rule
    with a cap is refused before the run starts, not dropped."""
    from dataclasses import replace

    from langadapter.errors import InvalidRequest
    from langadapter.mutation import TimeoutRule

    adapter, request, _container, _calls, launches = mutation_flow
    with adapter.open_mutation_session("pit", workers=1) as session:
        with pytest.raises(InvalidRequest, match="PIT caps no per-mutant timeout"):
            session.run(replace(request, per_mutant_timeout=TimeoutRule(2.0, 3, cap_s=60)))
        assert session.is_open and not launches
        uncapped = session._effective_settings(
            replace(request, per_mutant_timeout=TimeoutRule(2.0, 3)))["per_mutant_timeout"]
        assert (uncapped.value, dict(uncapped.inputs)) == (
            3, {"rule": "pit", "factor": 2.0, "constant_s": 3})
        assert session.run(request).outcome is lb.Outcome.COMPLETED


@pytest.mark.parametrize("catalogue", [
    Catalogue(name="NOPE"), Catalogue(operators=("MATH", "org.example.NopeMutator"))])
def test_an_unknown_mutator_refuses_its_request_and_leaves_the_session_open(
        mutation_flow, catalogue):
    """The catalogue is checked against PIT's pinned registry before the run
    starts, so a name or id PIT does not know refuses that request alone,
    without listing or launching anything, and the session runs the next."""
    from dataclasses import replace

    from langadapter.errors import InvalidRequest

    adapter, request, _container, calls, launches = mutation_flow
    with adapter.open_mutation_session("pit", workers=1) as session:
        with pytest.raises(InvalidRequest, match="knows no mutator or group"):
            session.run(replace(request, catalogue=catalogue))
        assert session.is_open and not launches and "ids" not in calls
        assert session.run(request).outcome is lb.Outcome.COMPLETED
