"""The project context is explicit, and an invalid field is refused by
name."""
import sys

import pytest

import langadapter as lb


def _context(tmp_path, **changes):
    fields = {"root": str(tmp_path),
              "interpreter": lb.Interpreter(path=sys.executable)}
    fields.update(changes)
    return lb.ProjectContext(**fields)


def test_a_local_context_is_valid(tmp_path):
    context = _context(tmp_path, interpreter_flags=["-X", "frozen_modules=off"],
                       environment={"A": "1"})
    context.validate()
    assert context.interpreter_flags == ("-X", "frozen_modules=off")
    assert context.environment == {"A": "1"}
    assert context.execution.kind == "local"
    assert context.test_framework.source == "project"


def test_a_context_is_immutable_and_hashable(tmp_path):
    context = _context(tmp_path, environment={"B": "2", "A": "1"})
    same = _context(tmp_path, environment={"A": "1", "B": "2"})
    assert context == same and hash(context) == hash(same)
    with pytest.raises(TypeError):
        context.environment["C"] = "3"
    with pytest.raises(AttributeError):
        context.root = "elsewhere"


@pytest.mark.parametrize("changes, field", [
    ({"root": ""}, "root"),
    ({"interpreter": lb.Interpreter()}, "interpreter"),
    ({"interpreter": lb.Interpreter(
        path=sys.executable,
        provisioning=lb.ProvisioningRequest("3.10"))}, "interpreter"),
    ({"interpreter": lb.Interpreter(path="/no/such/python")},
     "interpreter.path"),
    ({"interpreter": lb.Interpreter(
        provisioning=lb.ProvisioningRequest(""))},
     "interpreter.provisioning.version"),
    ({"test_framework": lb.TestFrameworkConfig(source="shared")},
     "test_framework.source"),
    ({"test_framework": lb.TestFrameworkConfig(
        source="isolated", path="pytest.ini")}, "test_framework.path"),
    ({"interpreter_flags": ("",)}, "interpreter_flags"),
    ({"environment": {"": "x"}}, "environment"),
    ({"execution": "local"}, "execution"),
])
def test_an_invalid_field_is_named(tmp_path, changes, field):
    with pytest.raises(lb.InvalidRequest) as refused:
        _context(tmp_path, **changes).validate()
    assert refused.value.field == field


def test_a_container_context_without_a_container_is_refused(tmp_path):
    context = _context(
        tmp_path, root="/testbed",
        interpreter=lb.Interpreter(path="/opt/venv/bin/python"),
        execution=lb.ContainerExecution(container="", work_dir=str(tmp_path)))
    with pytest.raises(lb.InvalidRequest) as refused:
        context.validate()
    assert refused.value.field == "execution.container"


@pytest.mark.parametrize("helper", ["python3", "/control/../python3", "C:\\python.exe", "", "/control/python\0"])
def test_container_supervisor_selection_requires_explicit_normalized_container_path(tmp_path, helper):
    context = lb.ProjectContext("/container/project", lb.Interpreter(path="/jdk/bin/java"),
        execution=lb.ContainerExecution("borrowed", str(tmp_path), helper_python=helper))
    with pytest.raises(lb.InvalidRequest) as refused:
        context.validate()
    assert refused.value.field == "execution.helper_python"


def test_container_supervisor_is_not_the_selected_subject_runtime(tmp_path):
    execution = lb.ContainerExecution("borrowed", str(tmp_path), helper_python="/control/python3")
    context = lb.ProjectContext("/container/project", lb.Interpreter(path="/jdk/bin/java"), execution=execution)
    context.validate()
    assert context.interpreter.path == "/jdk/bin/java"
    assert context.execution.helper_python == "/control/python3"
    assert context.execution != lb.ContainerExecution("borrowed", str(tmp_path))


def test_a_container_context_uses_container_paths(tmp_path):
    context = _context(
        tmp_path, root="/testbed",
        interpreter=lb.Interpreter(path="/opt/venv/bin/python"),
        execution=lb.ContainerExecution(
            container="sweb.eval.x", work_dir=str(tmp_path),
            repository_dir="/testbed"))
    context.validate()
    assert context.execution.kind == "container"
    relative_root = _context(
        tmp_path, root="testbed",
        interpreter=lb.Interpreter(path="/opt/venv/bin/python"),
        execution=context.execution)
    with pytest.raises(lb.InvalidRequest) as refused:
        relative_root.validate()
    assert refused.value.field == "root"


def test_a_relative_local_root_is_resolved_when_the_context_is_made(tmp_path, monkeypatch):
    # The commands run in other directories and read a relative root from there.
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path)
    context = lb.ProjectContext("project", lb.Interpreter(path=sys.executable))
    assert context.root == str(tmp_path / "project")
    context.validate()


def test_a_relative_interpreter_path_runs_from_where_it_was_given(tmp_path, monkeypatch):
    """README's form: an interpreter path relative to the current directory,
    written with forward slashes, runs the suite, though the run starts in
    another directory. A bare program name stays a name that PATH finds."""
    import os

    from langadapter.runs import SuiteRunRequest

    (tmp_path / "project" / "tests").mkdir(parents=True)
    (tmp_path / "project" / "tests" / "test_one.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    relative = os.path.relpath(sys.executable, tmp_path).replace(os.sep, "/")
    context = lb.ProjectContext("project", lb.Interpreter(path=relative))
    assert context.interpreter.path == os.path.abspath(relative)
    run = lb.create("python", context).tests.run_suite(SuiteRunRequest(
        lb.Suite(str(tmp_path / "project"), ("tests/test_one.py",)), timeout_s=120))
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert lb.ProjectContext("project", lb.Interpreter(path="python")).interpreter.path == "python"


def test_a_path_outside_the_work_dir_is_named(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    context = _context(
        tmp_path, root="/testbed",
        interpreter=lb.Interpreter(path="/opt/venv/bin/python"),
        execution=lb.ContainerExecution(container="c", work_dir=str(zone)))
    assert context.within_work_dir(str(zone / "suite" / "test_a.py"))
    assert not context.within_work_dir(str(tmp_path / "test_a.py"))
    assert _context(tmp_path).within_work_dir(str(tmp_path / "anywhere"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process creation adds .exe")
def test_an_interpreter_path_without_exe_names_the_file_its_start_runs(tmp_path):
    """On Windows the context's check finds python.exe for a path without the
    extension, as the start runs it, and keeps the path as given. A path that
    names no file either way is still refused."""
    (tmp_path / "Scripts").mkdir()
    (tmp_path / "Scripts" / "python.exe").write_text("", encoding="ascii")
    given = str(tmp_path / "Scripts" / "python")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(given))
    context.validate()
    assert context.interpreter.path == given
    with pytest.raises(lb.InvalidRequest, match="no file at"):
        lb.ProjectContext(str(tmp_path), lb.Interpreter(str(tmp_path / "Scripts" / "absent"))).validate()
