"""The public warmed Java benchmark: real build and fork copies around
replaced native setup and launch steps."""
from dataclasses import replace
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _linux_container
from langadapter._owned_directory import OwnershipRefused
from langadapter.capabilities import OPERATION_NAMES
from langadapter.context import Interpreter, LocalExecution, ProjectContext
from langadapter.errors import InvalidRequest
from langadapter.java import _warmed, _warmed as native
from langadapter.java._tools import Toolset
from langadapter.java._warmed import ForkExecution
from langadapter.java._warmed_fork import PreparedSetup, SetupObservation
from langadapter.java._env import Jdk
from langadapter.java._build import JavaProject
from langadapter.outcomes import Outcome
from langadapter.runs import WarmedSuiteBenchmarkRequest
from langadapter.suites import Suite

from test_warmed_protocol import report


#: A native iteration whose one test failed, with its failure evidence.
FAILED_ITERATION = {
    "outcome": "test_failures",
    "counts": {"discovered": 1, "started": 1, "passed": 0, "failed": 1, "skipped": 0,
               "aborted": 0, "container_failures": 0},
    "tests": [{"unique_id": "[engine:junit-jupiter]/[class:demo.CalcTest]/[method:value()]",
               "display_name": "value()", "kind": "test", "status": "failed",
               "reason": "expected 2"},
              {"unique_id": "[engine:junit-jupiter]", "display_name": "JUnit Jupiter",
               "kind": "container", "status": "successful", "reason": ""}],
}


@pytest.fixture
def warmed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    for name in ("src/main/java/demo", "src/test/java/demo", "target/classes", "target/test-classes"):
        (project / name).mkdir(parents=True)
    (project / "src/main/java/demo/Calc.java").write_text("package demo; class Calc {}", encoding="utf-8")
    (project / "src/test/java/demo/CalcTest.java").write_text(
        "package demo; class CalcTest {}", encoding="utf-8")
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    java = home / "bin" / ("java.exe" if os.name == "nt" else "java")
    java.write_text("", encoding="utf-8")
    zone = tmp_path / "zone"
    zone.mkdir()
    context = ProjectContext(str(project), Interpreter(str(java)), LocalExecution(str(zone)),
                             interpreter_flags=("-ea",))
    state = SimpleNamespace(setups=[], launches=[], failure=None, setup_outcome=Outcome.COMPLETED,
                            setup_output="")

    def setup(build_context, executor, request, directory, *, total_deadline,
              class_names):
        state.setups.append((build_context.root, directory))
        if state.setup_outcome is not Outcome.COMPLETED:
            return SetupObservation(state.setup_outcome, "the declared suite has no classes",
                                    state.setup_output)
        directory.mkdir()
        for name, file, data in (("classes", "CalcTest.class", b"suite"),
                                 ("helper", "LangAdapterWarmed.class", b"helper"),
                                 ("tools", "launcher.jar", b"engine")):
            (directory / name).mkdir()
            (directory / name / file).write_bytes(data)
        root = Path(build_context.root)
        model = JavaProject(str(root), "maven", 17, (str(root / "src/main/java"),),
                            (str(root / "src/test/java"),), (str(root / "target/classes"),),
                            (str(root / "target/test-classes"),), (str(root / "target/classes"),),
                            test_jvm_args=("-Xmx64m",))
        return SetupObservation(Outcome.COMPLETED, prepared=PreparedSetup(
            model, Jdk(str(home), 21, "21.0.11"),
            Toolset("1.14.4", (str(directory / "tools" / "launcher.jar"),)),
            str(directory / "classes"), str(directory / "helper"), ("demo.CalcTest",)))

    def launch(fork_context, executor, model, jdk, tools, *, request, protocol, classes,
               helper_classes, class_names, channel, total_deadline, on_event=None,
               processor_prefix=()):
        assert Path(classes, "CalcTest.class").read_bytes() == b"suite"
        assert model.root == fork_context.root and class_names == ("demo.CalcTest",)
        state.launches.append(SimpleNamespace(root=fork_context.root, classes=classes,
                                              protocol=protocol, channel=channel,
                                              processor_prefix=list(processor_prefix)))
        transcript = native.Transcript(protocol)

        def accept(value):
            event = native.decode(json.dumps(value).encode(), protocol)
            transcript.accept(event)
            if on_event is not None:
                on_event(event)

        accept(report(protocol, "ready"))
        outcome = Outcome.COMPLETED
        for index in range(protocol.warmup_runs + protocol.runs):
            accept(report(protocol, "start", index))
            failed = state.failure == (protocol.fork, index)
            accept(report(protocol, "iteration", index, elapsed_ns=(index + 1) * 1_000_000,
                          **(FAILED_ITERATION if failed else {})))
            if failed:
                outcome = Outcome.TEST_FAILURES
                break
        else:
            accept(report(protocol, "complete"))
        return ForkExecution(transcript, outcome, "", ())

    monkeypatch.setattr(_warmed._warmed_forks, "prepare_setup", setup)
    monkeypatch.setattr(_warmed._warmed_launch, "execute", launch)
    monkeypatch.setattr(_warmed, "_allowed_cpus", lambda prefix, cwd, env=None: (0, 1))
    request = WarmedSuiteBenchmarkRequest(
        Suite(str(project / "src/test/java"), ("demo/CalcTest.java",)), "native_lifecycle",
        30, 120, forks=2, warmup_runs=1, runs=2)
    return lb.create("java", context), request, state, project, zone


def test_each_fork_measures_in_its_own_copy_of_one_build(warmed):
    adapter, request, state, project, zone = warmed
    events = []
    result = adapter.tests.benchmark_warmed(
        WarmedSuiteBenchmarkRequest(**{**request.__dict__, "on_progress": events.append}))
    assert result.outcome is Outcome.COMPLETED and result.validity == "complete", result.reason
    assert (result.forks_started, result.forks_completed) == (2, 2)
    assert (result.warmup_completed, result.measurements_completed) == (2, 4)
    # Samples are 2 and 3 ms in each fork, without the 1 ms warmup.
    assert result.mean_of_fork_means_ms == 2.5 and result.sample_min_ms == 2
    assert result.allowed_cpus == (0, 1)
    (build_root, setup_directory), = state.setups
    roots = {Path(launch.root) for launch in state.launches}
    assert len(roots) == 2 and Path(build_root) not in roots and project not in roots
    assert all(Path(launch.classes).is_relative_to(launch.root) for launch in state.launches)
    assert [launch.protocol.fork for launch in state.launches] == [0, 1]
    assert len({launch.protocol.token for launch in state.launches}) == 2
    # The copies are removed and the project receives no build output.
    assert not Path(build_root).exists() and not any(root.exists() for root in roots)
    assert not (project / _warmed.SETUP_DIRECTORY).exists()
    identity = result.identity
    assert identity.toolchain.version == "21.0.11"
    assert identity.toolchain.tools == (("junit-platform", "1.14.4"),)
    assert identity.build_tool == "maven" and identity.jvm_args == ("-Xmx64m", "-ea")
    assert [item.path for item in identity.project_inputs] == ["src/main/java/demo/Calc.java"]
    stages = [event.stage for event in events]
    assert stages[0] == "setup" and stages[-1] == "complete"
    assert stages.count("fork_start") == stages.count("fork_end") == 2
    assert stages.count("iteration_start") == stages.count("iteration_end") == 6
    assert (events[-1].forks_completed, events[-1].measurements_completed) == (2, 4)


def test_a_failing_iteration_ends_the_measurement_without_a_retry(warmed):
    adapter, request, state, project, zone = warmed
    state.failure = (0, 1)
    result = adapter.tests.benchmark_warmed(request)
    # The failure changes the iterations' outcome pattern.
    assert result.outcome is Outcome.TEST_FAILURES and result.validity == "non_repeatable"
    assert len(state.launches) == 1 and result.forks_started == 1
    assert result.mean_of_fork_means_ms is None


def test_a_failed_setup_answers_without_any_fork(warmed):
    adapter, request, state, project, zone = warmed
    state.setup_outcome = Outcome.COLLECTION_FAILED
    result = adapter.tests.benchmark_warmed(request)
    assert result.outcome is Outcome.COLLECTION_FAILED and result.validity == "no_samples"
    assert not state.launches and result.identity.toolchain.version == "unknown"
    (build_root, _), = state.setups
    assert not Path(build_root).exists()


def test_a_failed_setup_keeps_the_first_compiler_error(warmed):
    adapter, request, state, project, zone = warmed
    state.setup_outcome = Outcome.COLLECTION_FAILED
    state.setup_output = "Helper.java:1: error: the first error explains the rest\n" + "later error\n" * 1000
    result = adapter.tests.benchmark_warmed(request)
    (diagnostic,) = result.diagnostics
    assert diagnostic.startswith("Helper.java:1: error: the first error") and len(diagnostic) < 6000


def test_a_processor_set_without_taskset_is_refused_before_any_fork(warmed, monkeypatch):
    adapter, request, state, project, zone = warmed
    monkeypatch.setattr(_warmed, "selected_executable", lambda name, environment: None)
    result = adapter.tests.benchmark_warmed(
        WarmedSuiteBenchmarkRequest(**{**request.__dict__, "cpus": (0,)}))
    assert result.outcome is Outcome.UNSUPPORTED and "taskset" in result.reason
    assert not state.launches


def test_a_failing_progress_observer_is_only_a_diagnostic(warmed):
    adapter, request, state, project, zone = warmed

    def observer(event):
        raise RuntimeError("display unavailable")

    result = adapter.tests.benchmark_warmed(
        WarmedSuiteBenchmarkRequest(**{**request.__dict__, "on_progress": observer}))
    assert result.outcome is Outcome.COMPLETED and result.validity == "complete"
    assert any("display unavailable" in note for note in result.diagnostics)


def test_a_failing_observer_of_the_final_event_is_in_the_result(warmed):
    adapter, request, state, project, zone = warmed

    def observer(event):
        if event.stage == "complete":
            raise RuntimeError("display closed")

    result = adapter.tests.benchmark_warmed(
        WarmedSuiteBenchmarkRequest(**{**request.__dict__, "on_progress": observer}))
    assert result.outcome is Outcome.COMPLETED and result.validity == "complete"
    assert any("display closed" in note for note in result.diagnostics)


def test_the_total_timeout_before_a_fork_retains_the_scratch(warmed, monkeypatch, recwarn):
    adapter, request, state, project, zone = warmed

    def expired(*args, **kwargs):
        raise subprocess.TimeoutExpired("warmed fork copy admission", 0)

    monkeypatch.setattr(_warmed._warmed_forks, "prepare", expired)
    result = adapter.tests.benchmark_warmed(request)
    assert result.outcome is Outcome.TIMED_OUT and result.validity == "no_samples"
    assert any("retained" in str(warning.message) for warning in recwarn)


def test_runner_options_are_refused_before_any_work(warmed):
    adapter, request, state, project, zone = warmed
    with pytest.raises(InvalidRequest, match="runner_options"):
        adapter.tests.benchmark_warmed(
            WarmedSuiteBenchmarkRequest(**{**request.__dict__, "runner_options": object()}))
    assert not state.setups


def test_python_answers_unsupported_without_running_a_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a tool was started"))
    test = tmp_path / "test_x.py"
    test.write_text("def test_x():\n    pass\n", encoding="utf-8")
    adapter = lb.create("python", ProjectContext(str(tmp_path), Interpreter(sys.executable)))
    result = adapter.tests.benchmark_warmed(WarmedSuiteBenchmarkRequest(
        Suite(str(tmp_path), ("test_x.py",)), "native_lifecycle", 30, 120))
    assert result.outcome is Outcome.UNSUPPORTED and result.validity == "no_samples"
    supported = {item.name: item.supported for item in adapter.operation_capabilities()}
    assert "tests.benchmark_warmed" in OPERATION_NAMES and not supported["tests.benchmark_warmed"]


def test_java_reports_the_warmed_operation(warmed):
    adapter = warmed[0]
    supported = {item.name: item.supported for item in adapter.operation_capabilities()}
    assert supported["tests.benchmark_warmed"]


@pytest.fixture
def container(warmed, tmp_path, monkeypatch):
    """The warmed fixture in a borrowed container: the project is
    /subject/project, the work directory is mapped at /mapped, and container
    commands act on their host directories."""
    adapter, request, state, project, zone = warmed
    context = ProjectContext("/subject/project", Interpreter("/jdk/bin/java"), lb.ContainerExecution(
        "borrowed", str(zone), repository_dir="/subject/project", container_work_dir="/mapped",
        helper_python="/control/python3"), interpreter_flags=("-ea",))
    state.container_calls, state.taskset, state.links, state.copy_fails = [], "", "", False

    def forbidden(*args, **kwargs):
        pytest.fail("a container's warmed timing started a host command")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    def host(path):
        path = PurePosixPath(path)
        if path == PurePosixPath(context.root) or PurePosixPath(context.root) in path.parents:
            return project / path.relative_to(context.root)
        return zone / path.relative_to("/mapped")

    def run(executor, argv, *, helper_python, **options):
        assert helper_python == "/control/python3" and executor.container == "borrowed"
        args = [executor._to_container(str(arg)) for arg in argv]
        state.container_calls.append(args)
        output = ""
        if args[0] == "test":
            assert args[1] == "-d" and host(args[2]).is_dir()
        elif args[0] == "find":
            output = state.links
        elif args[0] == "cp":
            assert args[1:4] == ["-R", "-P", "--"] and args[4].endswith("/.")
            if state.copy_fails:
                return subprocess.CompletedProcess(args, 1, "", "cp: no space left on device")
            shutil.copytree(host(args[4][:-2]), host(args[5]))
        elif args[:2] == ["sh", "-c"]:
            output = state.taskset
        elif args[-2:] == ["cat", "/proc/self/status"]:
            output = "Name:\tcat\nCpus_allowed_list:\t0-1\n"
        elif args == ["cat", "/jdk/release"]:
            # The container JDK's version for the identity.
            output = 'JAVA_VERSION="21.0.11"\n'
        elif args == ["/jdk/bin/javac", "-version"]:
            output = "javac 21.0.11"
        elif args[0] == "cat":
            output = host(args[1]).read_text(encoding="ascii")
        elif args[0] == "rm":
            assert args[1:3] == ["-rf", "--"] and host(args[3]).resolve().is_relative_to(zone.resolve())
            shutil.rmtree(host(args[3]), ignore_errors=True)
        else:
            pytest.fail(f"unexpected container command: {args}")
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(_linux_container, "run", run)

    def setup(build_context, executor, request, directory, *, total_deadline,
              class_names):
        assert isinstance(build_context.execution, lb.ContainerExecution)
        assert build_context.execution.repository_dir == build_context.root
        state.setups.append((build_context.root, directory))
        directory.mkdir()
        for name, file, data in (("classes", "CalcTest.class", b"suite"),
                                 ("helper", "LangAdapterWarmed.class", b"helper")):
            (directory / name).mkdir()
            (directory / name / file).write_bytes(data)
        root = build_context.root
        model = JavaProject(root, "maven", 17, (f"{root}/src/main/java",), (f"{root}/src/test/java",),
                            (f"{root}/target/classes",), (f"{root}/target/test-classes",),
                            (f"{root}/target/classes", "/cache/engine.jar"), test_jvm_args=("-Xmx64m",))
        return SetupObservation(Outcome.COMPLETED, prepared=PreparedSetup(
            model, Jdk("/jdk", 21, "21.0.11"), Toolset("1.14.4", ("/cache/launcher.jar",)),
            str(directory / "classes"), str(directory / "helper"), ("demo.CalcTest",)))

    monkeypatch.setattr(_warmed._warmed_forks, "prepare_setup", setup)
    suite = zone / "suite"
    (suite / "demo").mkdir(parents=True)
    (suite / "demo" / "CalcTest.java").write_text("package demo; class CalcTest {}", encoding="utf-8")
    request = WarmedSuiteBenchmarkRequest(Suite(str(suite), ("demo/CalcTest.java",)), "native_lifecycle",
                                          30, 120, forks=2, warmup_runs=1, runs=2)
    return lb.create("java", context), request, state, project, zone


def test_a_container_measures_with_a_build_copy_made_in_the_container(container):
    adapter, request, state, project, zone = container
    report = {item.name: item for item in adapter.operation_capabilities()}
    assert report["tests.benchmark_warmed"].supported
    assert report["tests.benchmark_warmed"].execution_kinds == ("local", "container")
    result = adapter.tests.benchmark_warmed(request)
    assert result.outcome is Outcome.COMPLETED and result.validity == "complete", result.reason
    assert (result.forks_started, result.forks_completed) == (2, 2)
    assert result.mean_of_fork_means_ms == 2.5 and result.allowed_cpus == (0, 1)
    assert result.identity.execution.kind == "container"
    assert [item.path for item in result.identity.project_inputs] == ["src/main/java/demo/Calc.java"]
    (build_root, setup_directory), = state.setups
    assert PurePosixPath(build_root).is_relative_to("/mapped")
    # The container copies the project into the build, then the build per fork.
    copies = [args for args in state.container_calls if args[0] == "cp"]
    assert [args[4] for args in copies] == ["/subject/project/.", f"{build_root}/.", f"{build_root}/."]
    assert copies[0][5] == build_root
    roots = {launch.root for launch in state.launches}
    assert len(roots) == 2 and build_root not in roots
    assert all(PurePosixPath(root).is_relative_to("/mapped") for root in roots)
    assert all(Path(launch.classes).is_relative_to(zone) for launch in state.launches)
    # The scratch and the fork copies are removed, the project is unchanged.
    assert not any(zone.glob("langadapter_*"))
    assert not (project / _warmed.SETUP_DIRECTORY).exists()


@pytest.mark.parametrize("taskset", ["", "/usr/bin/taskset"])
def test_a_containers_processor_set_uses_the_containers_taskset(container, taskset):
    adapter, request, state, project, zone = container
    state.taskset = taskset
    result = adapter.tests.benchmark_warmed(
        WarmedSuiteBenchmarkRequest(**{**request.__dict__, "cpus": (1,)}))
    if not taskset:
        assert result.outcome is Outcome.UNSUPPORTED and "selected container" in result.reason
        assert not state.launches
        return
    assert result.outcome is Outcome.COMPLETED, result.reason
    probes = [args for args in state.container_calls if args[-2:] == ["cat", "/proc/self/status"]]
    assert len(probes) == 2 and all(args[:3] == ["/usr/bin/taskset", "-c", "1"] for args in probes)
    # Each fork starts under the taskset the affinity check used.
    assert [launch.processor_prefix for launch in state.launches] == [
        ["/usr/bin/taskset", "-c", "1"]] * len(state.launches) and state.launches


def test_a_container_project_with_a_link_is_refused_before_anything_is_copied(container):
    adapter, request, state, project, zone = container
    state.links = "/subject/project/link\0"
    with pytest.raises(OwnershipRefused, match="links"):
        adapter.tests.benchmark_warmed(request)
    assert not state.setups and not any(args[0] == "cp" for args in state.container_calls)


def test_a_failed_copy_in_a_container_is_an_infrastructure_failure(container, recwarn):
    adapter, request, state, project, zone = container
    state.copy_fails = True
    result = adapter.tests.benchmark_warmed(request)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE and "no space left" in result.reason
    assert not state.setups
    assert any("retained" in str(warning.message) for warning in recwarn)


def test_a_failed_local_copy_is_raised_as_before(warmed, monkeypatch):
    adapter, request, state, project, zone = warmed

    def failed(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(_warmed, "_copy_tree", failed)
    with pytest.raises(OSError, match="no space left"):
        adapter.tests.benchmark_warmed(request)
    assert not state.setups


def test_a_container_without_its_helper_python_runs_nothing(container):
    adapter, request, state, project, zone = container
    context = adapter.context
    unsupervised = lb.create("java", replace(context, execution=replace(
        context.execution, helper_python=None)))
    result = unsupervised.tests.benchmark_warmed(request)
    assert result.outcome is Outcome.MISSING_SETUP and "helper_python" in result.reason
    assert not state.container_calls and not state.setups
