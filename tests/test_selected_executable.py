"""Captured host routes bind actual addresses before process creation."""
import os
from pathlib import Path
import subprocess

import pytest

from langadapter._environment import selected_executable
from langadapter._owned_directory import OwnershipRefused
from langadapter.context import Environment
from langadapter.execution import ContainerExecutor


def tool(tool_dir, name):
    tool_dir.mkdir(exist_ok=True)
    path = tool_dir / (name + (".EXE" if os.name == "nt" else ""))
    path.write_text("selected tool", encoding="ascii")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("runner", ["docker", "podman", "apptainer", "singularity"])
@pytest.mark.parametrize("absolute", [False, True])
def test_actual_container_command_binds_selected_host_address_and_native_flags(tmp_path, monkeypatch, runner, absolute):
    selected = tool(tmp_path / "selected", runner)
    ambient = tool(tmp_path / "ambient", runner)
    monkeypatch.setenv("PATH", str(ambient.parent))
    monkeypatch.setenv("PATHEXT", ".BAT")
    monkeypatch.chdir(ambient.parent)
    env = Environment({"PATH": str(selected.parent), "PATHEXT": ".EXE", "CONTROL": "first"}, inherit=False)
    executor = ContainerExecutor("borrowed", runner=str(selected) if absolute else runner,
                                 control_environment=env, forward_keys=("NATIVE",))
    calls = []

    def capture(argv, **options):
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, "native", "")

    monkeypatch.setattr("langadapter.execution.run_capture", capture)
    executor.run(["target"], cwd="/project", env={"NATIVE": "value"}, input="payload")
    argv, options = calls[0]
    assert argv[0] == str(selected) and options["env"] == dict(env)
    if runner in ("apptainer", "singularity"):
        assert argv == [str(selected), "exec", "--cleanenv", "--pwd", "/project", "--env",
                        "NATIVE=value", "instance://borrowed", "target"]
    else:
        assert argv == [str(selected), "exec", "-i", "-w", "/project", "-e", "NATIVE=value", "borrowed", "target"]


@pytest.mark.parametrize("path", [None, "", ".", "relative"])
def test_missing_captured_path_does_not_launch_ambient_or_current_directory(tmp_path, monkeypatch, path):
    ambient = tool(tmp_path / "ambient", "docker")
    monkeypatch.setenv("PATH", str(ambient.parent))
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.chdir(ambient.parent)
    env = {} if path is None else {"PATH": path}
    executor = ContainerExecutor("borrowed", control_environment=env)
    monkeypatch.setattr("langadapter.execution.run_capture", lambda *a, **kw: pytest.fail("must refuse before launch"))
    with pytest.raises(OwnershipRefused, match="Captured host container runner"):
        executor.run(["target"])


def test_selected_relative_executable_never_uses_current_directory(tmp_path, monkeypatch):
    selected = tool(tmp_path / "tools", "docker")
    monkeypatch.chdir(tmp_path)
    assert selected_executable(str(Path("tools") / selected.name), {"PATH": str(selected.parent)}) is None
    assert selected_executable(str(selected), {}) == str(selected)


@pytest.mark.skipif(os.name != "nt", reason="Windows PATHEXT selection")
def test_captured_pathext_absence_cannot_use_ambient_extensions(tmp_path, monkeypatch):
    selected = tool(tmp_path / "tools", "docker")
    monkeypatch.setenv("PATHEXT", ".EXE")
    assert selected_executable("docker", {"PATH": str(selected.parent)}) is None
    assert selected_executable("docker", {"PATH": str(selected.parent), "PATHEXT": ".EXE"}) == str(selected)


@pytest.mark.skipif(os.name != "nt", reason="Windows environment-name semantics")
@pytest.mark.parametrize("path_key,extension_key", [("Path", "Pathext"), ("path", "pathext"), ("PATH", "PATHEXT")])
@pytest.mark.parametrize("runner", ["docker", "apptainer"])
def test_actual_executor_binds_mixed_case_windows_selection_without_mutating_identity(tmp_path, monkeypatch, path_key, extension_key, runner):
    selected = tool(tmp_path / "selected", runner)
    values = {path_key: str(selected.parent), extension_key: ".EXE"}
    environment = Environment(values, inherit=False)
    executor = ContainerExecutor("borrowed", runner=runner, control_environment=environment)
    calls = []
    monkeypatch.setattr("langadapter.execution.run_capture", lambda argv, **kw: (
        calls.append((argv, kw)) or subprocess.CompletedProcess(argv, 0, "", "")))
    executor.run(["target"])
    assert calls[0][0][0] == str(selected)
    assert calls[0][1]["env"] == values and dict(environment) == values
    assert ("--cleanenv" in calls[0][0]) is (runner == "apptainer")


@pytest.mark.skipif(os.name != "nt", reason="Windows environment-name semantics")
@pytest.mark.parametrize("conflict", ["PATH", "PATHEXT"])
def test_conflicting_windows_name_aliases_refuse_named_selection_before_launch(tmp_path, monkeypatch, conflict):
    selected = tool(tmp_path / "selected", "docker")
    values = {"PATH": str(selected.parent), "PATHEXT": ".EXE",
              "Path" if conflict == "PATH" else "Pathext": "conflicting-value"}
    env = Environment(values, inherit=False)
    assert selected_executable("docker", env) is None
    monkeypatch.setattr("langadapter.execution.run_capture", lambda *a, **kw: pytest.fail("ambiguous selector cannot launch"))
    with pytest.raises(OwnershipRefused):
        ContainerExecutor("borrowed", control_environment=env).run(["target"])
    assert dict(env) == values
    assert selected_executable(str(selected), env) == str(selected)


@pytest.mark.skipif(os.name != "nt", reason="Windows environment-name semantics")
def test_equal_windows_name_aliases_select_without_collapsing_raw_values(tmp_path):
    selected = tool(tmp_path / "selected", "docker")
    values = {"PATH": str(selected.parent), "Path": str(selected.parent), "PATHEXT": ".EXE", "Pathext": ".EXE"}
    assert selected_executable("docker", values) == str(selected)
    assert tuple(values) == ("PATH", "Path", "PATHEXT", "Pathext")


@pytest.mark.skipif(os.name == "nt", reason="POSIX environment-name semantics")
def test_posix_mixed_case_names_do_not_become_path_or_conflict_with_path(tmp_path):
    selected = tool(tmp_path / "selected", "docker")
    assert selected_executable("docker", {"Path": str(selected.parent)}) is None
    assert selected_executable("docker", {"PATH": str(selected.parent), "Path": "other"}) == str(selected)
