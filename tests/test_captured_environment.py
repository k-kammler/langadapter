"""Captured launch selection remains explicit across real callers and leases."""
from dataclasses import replace
import copy
import os
from pathlib import Path
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter import _linux_container, _process_tree
from langadapter._environment import launch_environment
from langadapter._owned_directory import OwnershipRefused
from langadapter.context import Environment
from langadapter.execution import ContainerExecutor, executor_for
from langadapter.java import _build as project, _tools
from langadapter.java._env import Jdk
from langadapter.python._cosmic_ray import CosmicRayEngine
from langadapter.python._run_definition import RUN_VARIABLES


def test_environment_snapshot_copy_identity_and_repr():
    supplied = {"TOKEN": "private", "PATH": "/selected"}
    value = Environment(supplied, inherit=False)
    supplied["TOKEN"] = "later"
    assert value["TOKEN"] == "private" and not value.inherit
    assert Environment(value) == value and hash(Environment(value)) == hash(value)
    assert copy.deepcopy(value) == value and copy.copy(value) is value
    assert Environment(dict(value)) != value and dict(value) != value
    assert "private" not in repr(value)
    assert Environment({"X": "1"}) == {"X": "1"}
    with pytest.raises(AttributeError):
        value.inherit = True
    with pytest.raises(TypeError):
        value._map["X"] = "changed"


@pytest.mark.parametrize("mode", [0, 1, "false", [], {}])
def test_nonboolean_environment_mode_is_refused(mode):
    with pytest.raises(lb.InvalidRequest, match="environment.inherit"):
        Environment(inherit=mode)


@pytest.mark.parametrize("native", [False, True])
def test_capture_never_revives_ambient_values(monkeypatch, native):
    monkeypatch.setenv("TOKEN", "ambient")
    selected = Environment({"PATH": "/selected"}, inherit=False)
    first = launch_environment(selected, native=native)
    first["PATH"] = "edited"
    monkeypatch.setenv("TOKEN", "later")
    assert launch_environment(selected, native=native) == {"PATH": "/selected"}
    assert launch_environment({}, native=True)["TOKEN"] == "later"
    assert launch_environment({}, native=False) == {}


def test_local_probe_captures_absence_and_preserves_explicit_native_flags(tmp_path, monkeypatch):
    values = {"SELECTED": "first"}
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(sys.executable),
                               environment=Environment(values, inherit=False))
    calls = []

    def capture(argv, **options):
        calls.append(options["env"])
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr("langadapter.execution.run_capture", capture)
    monkeypatch.setenv("JAVA_HOME", "/ambient/jdk")
    values["SELECTED"] = "later"
    executor = executor_for(context)
    executor.run([sys.executable, "--version"])
    executor.run([sys.executable, "native"], env={"NATIVE": "operation flag"})
    assert calls == [{"SELECTED": "first"}, {"NATIVE": "operation flag"}]


def test_an_inheriting_context_environment_reaches_commands_run_without_env(tmp_path, monkeypatch):
    """Without a caller's env the executor runs a command with the context's
    environment: an inheriting one over the caller's environment, and in a
    container its values forwarded. A caller's env still replaces it."""
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(sys.executable),
                               environment=Environment({"SELECTED": "overlay"}))
    calls = []

    def capture(argv, **options):
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("langadapter.execution.run_capture", capture)
    monkeypatch.setenv("AMBIENT", "kept")
    executor = executor_for(context)
    executor.run([sys.executable, "--version"])
    executor.run([sys.executable, "native"], env={"NATIVE": "operation flag"})
    assert calls[0][1]["env"] == {**os.environ, "SELECTED": "overlay"}
    assert calls[1][1]["env"] == {"NATIVE": "operation flag"}
    calls.clear()
    executor_for(container_context(tmp_path, supervisor=False, target=False)).run(["native"])
    assert "SELECTED=container" in calls[0][0] and "PATH=/container/tools" in calls[0][0]


@pytest.mark.parametrize("values", [{}, {"SELECTED": "overlay"}])
def test_setup_probes_start_in_the_callers_environment(tmp_path, monkeypatch, values):
    """Both interpreter probes of a setup check start, as every command does,
    with the caller's environment and an inheriting context's values over it.
    Python 3.10 on Windows does not start without SYSTEMROOT."""
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(sys.executable),
                               environment=Environment(values))
    calls = []

    def capture(argv, **options):
        calls.append(options["env"])
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr("langadapter.execution.run_capture", capture)
    monkeypatch.setenv("AMBIENT", "kept")
    lb.inspect_project("python", lb.ProjectSetupRequest(context, (), ("tests.run_suite",)))
    assert len(calls) == 2
    for env in calls:
        started = os.environ if env is None else env
        assert started["AMBIENT"] == "kept" and started.get("SELECTED") == values.get("SELECTED")


def test_actual_python_probe_and_environments_use_the_capture(tmp_path, monkeypatch):
    selected = Environment({"SELECTED": "first"}, inherit=False)
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(sys.executable), environment=selected)
    adapter = lb.create("python", context)
    monkeypatch.setenv("LANGADAPTER_MUTATION_TARGET", "ambient target")
    calls = []

    def capture(argv, **options):
        calls.append(options["env"])
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr("langadapter.execution.run_capture", capture)
    assert adapter._probe() == ({}, "")
    assert calls == [{"SELECTED": "first"}]
    command = adapter._build_env(str(tmp_path))
    mutation = CosmicRayEngine({}, adapter=adapter)._build_env(tmp_path)
    for value in (command, mutation):
        assert value["SELECTED"] == "first"
        assert value["PYTHONPATH"] == str(tmp_path)
        assert value["PYTHONIOENCODING"] == "utf-8" and value["PYTHONUTF8"] == "1"
        assert "LANGADAPTER_MUTATION_TARGET" not in value
    # One test run: the mutants' environment has the normal run's fixed variables.
    for value in (command, mutation):
        assert {name: value.get(name) for name in RUN_VARIABLES} == RUN_VARIABLES


@pytest.mark.parametrize("captured", [False, True])
def test_python_conventions_apply_on_top_of_the_selected_environment(tmp_path, monkeypatch,
                                                                     captured):
    """The launch environment: the project root goes ahead of a selected
    PYTHONPATH, and the hash seed stays 0."""
    monkeypatch.setenv("AMBIENT", "native")
    selected = Environment({"PYTHONPATH": "/explicit", "PYTHONHASHSEED": "9"}, inherit=not captured)
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(sys.executable),
                                                  environment=selected))
    value = adapter._build_env(str(tmp_path))
    assert value["PYTHONPATH"] == f"{tmp_path}{os.pathsep}/explicit"
    assert value["PYTHONHASHSEED"] == "0"
    assert ("AMBIENT" in value) is not captured


def test_public_prepared_lease_refuses_captured_to_overlay_forgery(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "subject.py").write_text("value = 1\n", encoding="ascii")
    context = lb.ProjectContext(str(source), lb.Interpreter(sys.executable),
                               environment=Environment({"VALUE": "1"}, inherit=False))
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with owner.use() as copied:
            assert copied.environment is context.environment
            assert lb.create("python", replace(copied)).context.environment == context.environment
            with pytest.raises(OwnershipRefused, match="environment"):
                lb.create("python", replace(copied, environment=dict(copied.environment)))
    finally:
        owner.close()


def test_java_tools_use_selected_values_not_ambient_or_container_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVA_HOME", "/ambient/jdk")
    monkeypatch.setenv("PATH", "/ambient/bin")
    monkeypatch.setenv("LANGADAPTER_BUILD_JAVA_HOME", "/ambient/build")
    selected = Environment({"VALUE": "1"}, inherit=False)
    jdk = Jdk("/selected/jdk", 21, "21")
    native = project.tool_environment(jdk, selected)
    container = project.tool_environment(jdk, selected, executor=ContainerExecutor("borrowed"))
    for value in (native, container):
        assert value["JAVA_HOME"] == "/selected/jdk"
        assert value["PATH"].replace("\\", "/") == "/selected/jdk/bin"
        assert "LANGADAPTER_BUILD_JAVA_HOME" not in value
        assert isinstance(value, Environment) and not value.inherit
    assert "/ambient/bin" in project.tool_environment(jdk, {})["PATH"]
    assert "/usr/local/bin:/usr/bin:/bin" in project.tool_environment(
        jdk, {}, executor=ContainerExecutor("borrowed"))["PATH"]


def test_java_captured_launcher_never_consults_ambient_path_or_pathext(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".BAT")
    monkeypatch.setattr(project.shutil, "which", lambda *a, **kw: pytest.fail("ambient lookup"))
    assert project.launcher(str(tmp_path), "maven", Environment(inherit=False)) is None
    tools = tmp_path / "tools"
    tools.mkdir()
    launcher = tools / ("mvn.BAT" if os.name == "nt" else "mvn")
    launcher.write_text("selected", encoding="ascii")
    launcher.chmod(0o755)
    selected = Environment({"PATH": str(tools), "PATHEXT": ".BAT"}, inherit=False)
    assert project.launcher(str(tmp_path), "maven", selected) == str(launcher)


def test_java_cache_has_no_ambient_capture_fallback(tmp_path, monkeypatch):
    home = "USERPROFILE" if os.name == "nt" else "HOME"
    monkeypatch.setenv("LANGADAPTER_JAVA_TOOLS", str(tmp_path / "ambient"))
    monkeypatch.setenv(home, str(tmp_path / "ambient-home"))
    monkeypatch.setattr(Path, "home", lambda: pytest.fail("ambient home lookup"))
    with pytest.raises(_tools.ToolUnavailable, match="LANGADAPTER_JAVA_TOOLS or a home directory"):
        _tools.cache_dir(Environment(inherit=False))
    named = str(tmp_path / "selected")
    assert _tools.cache_dir(Environment({"LANGADAPTER_JAVA_TOOLS": named}, inherit=False)) == Path(named)
    # The documented default resolves under the captured home only.
    captured_home = Environment({home: str(tmp_path / "captured-home")}, inherit=False)
    assert _tools.cache_dir(captured_home) == tmp_path / "captured-home" / ".cache" / "langadapter" / "java"
    assert _tools.cache_dir({}) == tmp_path / "ambient"


def container_context(tmp_path, *, supervisor=True, target=True):
    tools = tmp_path / "host-tools"
    tools.mkdir(exist_ok=True)
    runner = tools / ("docker.exe" if os.name == "nt" else "docker")
    runner.write_text("selected", encoding="ascii")
    runner.chmod(0o755)
    controls = {"PATH": str(tools), "PATHEXT": ".EXE", "CONTROL": "host"}
    execution = lb.ContainerExecution("borrowed", str(tmp_path), container_work_dir="/mapped",
        helper_python="/control/python" if supervisor else None, control_environment=controls)
    controls["CONTROL"] = "later"
    return lb.ProjectContext("/container/project", lb.Interpreter("/container/jdk/bin/java"), execution=execution,
        environment=Environment({"PATH": "/container/tools", "SELECTED": "container"}, inherit=not target))


def test_container_native_capture_and_host_control_are_independent(tmp_path, monkeypatch):
    context = container_context(tmp_path)
    executor = executor_for(context)
    launched, admitted = [], []
    result = object()

    def host_start(argv, **options):
        launched.append((argv, options))
        return result

    def acquire(args, *, launch, **options):
        admitted.append((args, options))
        assert launch(tmp_path / "helper.py", tmp_path / "channel", "token", {"stdout": None}) is result
        return result

    monkeypatch.setattr(_process_tree, "start", host_start)
    monkeypatch.setattr(_linux_container._Command, "start_in_container", acquire)
    monkeypatch.setenv("PATH", "/later/host")
    env = {"PATH": "/container/native", "RESOURCE": str(tmp_path / "fixture")}
    assert _linux_container.start(executor, ["/container/jdk/bin/java"],
                             helper_python="/control/python", env=env) is result
    assert launched[0][1]["env"] == dict(context.execution.control_environment)
    assert Path(launched[0][0][0]).parent == tmp_path / "host-tools"
    assert admitted[0][1]["native_environment"] == {"PATH": "/container/native", "RESOURCE": "/mapped/fixture"}
    assert "CONTROL=host" not in launched[0][0]
    assert context.execution.control_environment["CONTROL"] == "host"


def test_container_capture_without_control_helper_refuses_before_host_launch(tmp_path, monkeypatch):
    executor = executor_for(container_context(tmp_path, supervisor=False))
    monkeypatch.setattr("langadapter.execution.run_capture", lambda *a, **kw: pytest.fail("host launch"))
    with pytest.raises(OwnershipRefused, match="helper_python"):
        executor.run(["native"])


def test_captured_host_runner_does_not_forward_host_values_to_container(tmp_path, monkeypatch):
    executor = executor_for(container_context(tmp_path, supervisor=False, target=False))
    calls = []

    def capture(argv, **options):
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("langadapter.execution.run_capture", capture)
    executor.run(["native"], env={"SELECTED": "container"})
    assert calls[0][1]["env"] == dict(executor.control_environment)
    assert Path(calls[0][0][0]).parent == tmp_path / "host-tools"
    assert "SELECTED=container" in calls[0][0] and "CONTROL=host" not in calls[0][0]
