"""Prepared launch with actual argument/controller paths and no native process."""
from dataclasses import replace
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from langadapter.context import (
    ContainerExecution, Interpreter, LocalExecution, ProjectContext,
)
from langadapter.errors import InvalidRequest
from langadapter.execution import executor_for
from langadapter._owned_directory import OwnershipRefused
from langadapter.runs import WarmedSuiteBenchmarkRequest
from langadapter.java import _warmed_fork as launch, _warmed as native, _tools as _junit
from langadapter.java._env import Jdk
from langadapter.java._build import JavaProject
from langadapter.outcomes import Outcome
from langadapter.suites import Suite

from test_warmed_protocol import report


@pytest.fixture(params=[False, True], ids=["local", "container"])
def fork(request, tmp_path, monkeypatch):
    container = request.param
    root = tmp_path / "project"
    root.mkdir()
    channel = tmp_path / "channel"
    channel.mkdir()
    execution = (ContainerExecution("selected", str(tmp_path), container_work_dir="/mounted",
                                    helper_python="/control/python") if container
                 else LocalExecution())
    context = ProjectContext(
        "/mounted/project" if container else str(root), Interpreter("/selected/jdk/bin/java"),
        execution=execution, interpreter_flags=("-ea",), environment={"NATIVE": "selected"})
    executor = executor_for(context)
    project = JavaProject(context.root, "maven", 8, (), (), (), (), ("/cache/engine.jar",),
                          test_jvm_args=("-Xmx96m",),
                          test_system_properties={"resource": str(root / "resource.txt")})
    suite_file = root / "ExampleTest.java"
    suite_file.write_text("class ExampleTest {}", encoding="utf-8")
    command_request = WarmedSuiteBenchmarkRequest(
        Suite(str(root), (str(suite_file),)), "native_lifecycle", 2, 30,
        startup_timeout_s=4, forks=2, warmup_runs=1, runs=2)
    protocol = native.ForkProtocol("a" * 32, 0, 1, 2)
    state = SimpleNamespace(calls=[], clock=100.0, container=container, context=context,
                            executor=executor, project=project, channel=channel,
                            classes=str(tmp_path / "classes"),
                            helpers=str(tmp_path / "helpers"),
                            request=command_request, protocol=protocol)
    monkeypatch.setattr(launch.time, "monotonic", lambda: state.clock)

    def forbidden(*args, **kwargs):
        pytest.fail("Controlled launch cannot execute a native process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    class Owner:
        args, returncode = ["selected-java"], None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            assert timeout > 0
            self.returncode = 0
            return self.returncode

    owner = Owner()
    state.owner = owner

    def start(command, **options):
        state.calls.append((command, options))
        assert options["stdout"] != subprocess.PIPE and options["stderr"] != subprocess.PIPE
        options["stdout"].write(b"test output\n" * 50000)
        options["stderr"].write(b"native stderr\n")
        for event, index in [("ready", None), ("start", 0), ("iteration", 0),
                             ("start", 1), ("iteration", 1), ("start", 2),
                             ("iteration", 2), ("complete", None)]:
            filename = event if index is None else f"{event}-{index}"
            (channel / f"{filename}.json").write_text(
                json.dumps(report(protocol, event, index)), encoding="utf-8")
        return owner

    def container_start(executor, command, **options):
        assert executor is state.executor
        assert options.pop("helper_python") == "/control/python"
        return start(command, **options)

    monkeypatch.setattr(launch._process_tree, "start", start)
    monkeypatch.setattr(launch._linux_container, "start", container_start)
    monkeypatch.setattr(launch._process_tree, "drain_output", lambda received: (None, None))
    monkeypatch.setattr(launch._process_tree, "finish", lambda received, **kw:
                        setattr(received, "_langadapter_command_disposed", True) or True)
    return state


def execute(fork, **changes):
    options = dict(context=fork.context, executor=fork.executor, project=fork.project,
                   jdk=Jdk("/selected/jdk", 21, "21.0.11"),
                   tools=_junit.Toolset("1.14.4", ("/cache/launcher.jar",)),
                   request=fork.request, protocol=fork.protocol,
                   classes=fork.classes, helper_classes=fork.helpers,
                   class_names=("ExampleTest",), channel=fork.channel, total_deadline=120)
    options.update(changes)
    return launch.execute(**options)


def test_launch_uses_selected_flags_classpath_and_file_output_with_actual_controller(fork):
    events = []
    result = execute(fork, on_event=events.append)
    assert result.outcome is Outcome.COMPLETED
    assert fork.owner._langadapter_command_disposed
    assert result.transcript.finished and len(result.transcript.iterations) == 3
    assert (fork.channel / "stdout.log").read_bytes() == b"test output\n" * 50000
    assert (fork.channel / "stderr.log").read_bytes() == b"native stderr\n"
    command, options = fork.calls[0]
    assert command[-1] == "@" + ("/mounted/channel/launch.args" if fork.container else str(fork.channel / "launch.args"))
    assert options["cwd"] == fork.project.root
    assert options["env"]["NATIVE"] == "selected"
    assert options["stdout"].closed and options["stderr"].closed
    arguments = (fork.channel / "launch.args").read_text(encoding="utf-8").splitlines()
    assert arguments[:2] == ['"-Xmx96m"', '"-ea"']
    # The argument file escapes each backslash, so java reads the path as given.
    resource = "/mounted/project/resource.txt" if fork.container else str(
        os.path.join(fork.project.root, "resource.txt")).replace("\\", "\\\\")
    assert arguments[2] == f'"-Dresource={resource}"'
    expected = ("/mounted/helpers:/mounted/classes:/cache/launcher.jar:/cache/engine.jar" if fork.container
                else os.pathsep.join((fork.helpers, fork.classes, "/cache/launcher.jar",
                                      "/cache/engine.jar")).replace("\\", "\\\\"))
    assert arguments[4] == f'"{expected}"'
    assert [event.event for event in events] == [
        "ready", "start", "iteration", "start", "iteration", "start", "iteration", "complete"]


def test_request_cpu_prefix_and_current_deadline_are_not_silently_replaced(fork):
    """The fork starts under the affinity check's taskset prefix, not one found again by name."""
    request = replace(fork.request, cpus=(2, 7))
    checked = ["/selected/bin/taskset", "-c", "2,7"]
    execute(fork, request=request, total_deadline=100.5, processor_prefix=checked)
    command, options = fork.calls[0]
    assert command[:3] == checked
    if fork.container:
        assert options["startup"] == 0.5


def test_a_processor_set_without_its_checked_prefix_is_refused_before_launch(fork):
    with pytest.raises(InvalidRequest, match="checked taskset prefix"):
        execute(fork, request=replace(fork.request, cpus=(2, 7)))
    assert fork.calls == [] and tuple(fork.channel.iterdir()) == ()


@pytest.mark.parametrize("change", ["protocol", "fork", "classes", "root",
                                   "executor", "expired", "deadline"])
def test_invalid_or_expired_admission_never_launches_or_overwrites_evidence(fork, change):
    options = {
        "protocol": {"protocol": native.ForkProtocol("a" * 32, 0, 2, 2)},
        "fork": {"protocol": native.ForkProtocol("a" * 32, 2, 1, 2)},
        "classes": {"class_names": ("LangAdapterWarmed",)},
        "root": {"project": replace(fork.project, root="/other/project")},
        "executor": {"executor": executor_for(replace(fork.context))},
        "expired": {"total_deadline": 100},
        "deadline": {"total_deadline": float("nan")},
    }[change]
    with pytest.raises((InvalidRequest, OwnershipRefused, subprocess.TimeoutExpired)):
        execute(fork, **options)
    assert fork.calls == [] and tuple(fork.channel.iterdir()) == ()


def test_existing_fork_evidence_refuses_launch_without_replacement(fork):
    path = fork.channel / "launch.args"
    path.write_bytes(b"previous evidence")
    with pytest.raises(FileExistsError):
        execute(fork)
    assert path.read_bytes() == b"previous evidence"
    assert fork.calls == []


def test_observer_failure_does_not_change_native_samples(fork):
    def broken(event):
        raise OSError("display fixture")
    result = execute(fork, on_event=broken)
    assert result.outcome is Outcome.COMPLETED
    assert len(result.diagnostics) == 8
    assert [item.elapsed_ns for item in result.transcript.iterations] == [123] * 3


def test_changed_container_executor_selection_is_refused_before_files(fork):
    if not fork.container:
        pytest.skip("container-only selector")
    fork.executor.container = "other"
    with pytest.raises(OwnershipRefused):
        execute(fork)
    assert fork.calls == [] and tuple(fork.channel.iterdir()) == ()


def test_actual_prepared_context_cannot_launch_after_its_use_expires(fork):
    if fork.container:
        pytest.skip("container preparation requires the separate native gate")
    from langadapter import prepare_project

    original = replace(fork.context, interpreter=Interpreter(sys.executable))
    owner = prepare_project(original)
    try:
        with owner.use() as scoped:
            executor = executor_for(scoped)
            project = replace(fork.project, root=scoped.root)
        with pytest.raises(OwnershipRefused):
            execute(fork, context=scoped, executor=executor, project=project)
        assert fork.calls == [] and tuple(fork.channel.iterdir()) == ()
    finally:
        owner.close()


def test_failed_acquisition_preserves_the_actual_error_and_no_controller_run(fork, monkeypatch):
    error = OSError("controlled acquired launch failure")
    retained = object()
    error._langadapter_command = retained

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(launch._linux_container, "start", fail)
    monkeypatch.setattr(launch._process_tree, "start", fail)
    monkeypatch.setattr(launch._warmed_controller, "drive", lambda *a, **kw:
                        pytest.fail("A failed acquisition cannot enter the controller"))
    with pytest.raises(OSError) as captured:
        execute(fork)
    assert captured.value is error and captured.value._langadapter_command is retained
    assert (fork.channel / "launch.args").is_file()
    assert (fork.channel / "stdout.log").is_file()


@pytest.mark.parametrize("value", ["ExampleTest", b"ExampleTest"])
def test_native_class_names_cannot_be_a_string_of_individual_characters(fork, value):
    with pytest.raises(InvalidRequest):
        execute(fork, class_names=value)
    assert fork.calls == [] and tuple(fork.channel.iterdir()) == ()
