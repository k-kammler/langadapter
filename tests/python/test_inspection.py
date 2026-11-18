"""Real public setup dispatch with isolated native observations controlled."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import site
import subprocess
import sys
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter.capabilities import Availability
from langadapter.execution import ContainerExecutor, LocalExecutor
from langadapter.python import _setup as _inspection


@pytest.fixture
def selected(tmp_path, monkeypatch):
    (tmp_path / "subject.py").write_text("value = 1\n", encoding="ascii")
    # The selected interpreter is a file: the stand-in executor answers for it.
    (tmp_path / "runtime").write_text("selected interpreter", encoding="ascii")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(str(tmp_path / "runtime")),
        environment=lb.Environment({"PATH": "selected", "API_KEY": "private"}, inherit=False),
        interpreter_flags=("-X", "selected-option"))
    request = lb.ProjectSetupRequest(context, ("subject.py",),
        ("tests.run_suite", "tests.per_test_evidence", "mutations.run"), allow_acquisition=True)
    state = {"calls": [], "returncode": 0, "error": None, "stdout": json.dumps({
        "implementation": "CPython", "version": "3.10.19",
        "tools": {"pytest": "8.3.5", "coverage": "7.6.1", "cosmic-ray": "8.4.3"}})}

    def run(executor, argv, **options):
        state["calls"].append((executor._context, argv, options))
        if state["error"] is not None:
            raise state["error"]
        return subprocess.CompletedProcess(argv, state["returncode"], state["stdout"], "private raw diagnostic")

    monkeypatch.setattr(LocalExecutor, "run", run)
    monkeypatch.setenv("PATH", "later ambient")
    return request, state


def _without_action(report, operations):
    """Required items and operations that are not confirmed yet name no next step."""
    return ([item.key for item in report.items if item.required and item.action is None
             and not (item.readiness is Availability.AVAILABLE and item.outcome is lb.Outcome.COMPLETED)]
            + [item.name for item in report.operations if item.name in operations
               and item.action is None and item.readiness is not Availability.AVAILABLE])


def test_public_inspection_observes_metadata_without_claiming_execution_readiness(selected):
    request, state = selected
    report = lb.inspect_project("python", request)
    assert report.scope == "inspection" and report.outcome is lb.Outcome.COMPLETED
    assert report.context is request.context and report.inputs.required == ("subject.py",)
    assert len(state["calls"]) == 1
    context, argv, options = state["calls"][0]
    assert context is request.context
    assert argv == [request.context.interpreter.path, "-I", "-S", "-c", _inspection._PROBE]
    assert "selected-option" not in argv and "private raw diagnostic" not in repr(report.safe_export())
    assert "env" not in options and 0 < options["timeout"] <= 10
    items = {item.key: item for item in report.items}
    assert items["runtime"].version == "CPython 3.10.19"
    assert items["runtime"].readiness is Availability.AVAILABLE
    assert all(items[name].required and items[name].readiness is None
               for name in ("pytest", "coverage", "cosmic-ray"))
    assert all(item.readiness is None for item in report.operations if item.supported)
    # Installed but not tried: the verification check is the next step.
    assert {items[name].action for name in ("pytest", "coverage", "cosmic-ray")} == {
        "run the tool check to try it"}
    assert {items[name].tried_by for name in ("pytest", "coverage", "cosmic-ray")} == {"verification"}
    assert {item.tried_by for item in report.operations if item.name in request.required_operations} == {
        "verification"}
    assert not _without_action(report, request.required_operations)


@pytest.mark.parametrize("facets", [(), (("tests.run_suite", ("coverage",)),)])
def test_required_coverage_facet_makes_the_suite_run_need_coverage(selected, facets):
    request, state = selected
    value = json.loads(state["stdout"])
    value["tools"]["coverage"] = None
    state["stdout"] = json.dumps(value)
    request = replace(request, required_operations=("tests.run_suite",), required_facets=facets)
    report = lb.inspect_project("python", request)
    items = {item.key: item for item in report.items}
    operation = next(item for item in report.operations if item.name == "tests.run_suite")
    assert report.outcome is lb.Outcome.COMPLETED
    assert items["coverage"].required is bool(facets)
    assert items["coverage"].readiness is Availability.MISSING_SETUP
    # A generic suite-run consumer is unaffected by an absent coverage tool.
    assert operation.readiness is (Availability.MISSING_SETUP if facets else None)


def test_tool_metadata_is_optional_when_only_static_operations_are_requested(selected):
    request, state = selected
    value = json.loads(state["stdout"])
    value["tools"] = dict.fromkeys(value["tools"])
    state["stdout"] = json.dumps(value)
    report = lb.inspect_project("python", replace(request, required_operations=("sources.metrics",)))
    assert report.outcome is lb.Outcome.COMPLETED
    assert not any(item.required for item in report.items)


def test_captured_named_runtime_binds_only_the_selected_host_executable(selected, monkeypatch, tmp_path):
    request, state = selected
    tools = tmp_path / "tools"
    tools.mkdir()
    executable = tools / ("python.EXE" if os.name == "nt" else "python")
    executable.write_text("selected interpreter", encoding="ascii")
    executable.chmod(0o755)
    context = replace(request.context, interpreter=lb.Interpreter("python"), environment=lb.Environment(
        {"PATH": str(tools), "PATHEXT": ".EXE"}, inherit=False))
    monkeypatch.setenv("PATH", str(tmp_path / "ambient"))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.COMPLETED
    assert report.context is context and report.items[0].selected == str(executable)
    assert state["calls"][0][1][0] == str(executable)
    assert report.context.environment is context.environment


@pytest.mark.parametrize("path", ["python", "relative/python", "../python", "absolute"])
def test_missing_captured_runtime_never_uses_ambient_or_current_directory(selected, path, tmp_path,
                                                                          monkeypatch):
    """An interpreter that does not exist reads missing setup and starts no
    command, by name, relative path or absolute path. A name is looked up in
    the captured environment, never the ambient PATH. A relative path is bound
    to the directory the context was made in, so a file of that name where a
    later command runs is not used."""
    request, state = selected
    made_in, later = tmp_path / "made-in" / "sub", tmp_path / "later" / "sub"
    ambient = tmp_path / "ambient"
    for directory in (made_in, later / "relative", ambient):
        directory.mkdir(parents=True)
    for planted in (later / "relative" / "python", later.parent / "python", ambient / "python",
                    ambient / "python.exe"):
        planted.write_text("not the selected interpreter", encoding="ascii")
    monkeypatch.setenv("PATH", str(ambient))
    path = str(tmp_path / "nowhere" / "python") if path == "absolute" else path
    monkeypatch.chdir(made_in)
    context = replace(request.context, interpreter=lb.Interpreter(path), environment=lb.Environment(inherit=False))
    assert context.interpreter.path == (os.path.abspath(path) if os.path.dirname(path) else path)
    monkeypatch.chdir(later)
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    assert not state["calls"]
    assert all(item.readiness is Availability.MISSING_SETUP for item in report.operations
               if item.supported and item.execution_backed)


@pytest.mark.skipif(os.name != "nt", reason="Windows process creation adds .exe")
@pytest.mark.parametrize("form", ["absolute", "relative"])
def test_a_windows_interpreter_path_without_exe_is_the_file_its_start_runs(selected, tmp_path,
                                                                          monkeypatch, form):
    """On Windows a process start adds .exe to a file name without an
    extension, as in .venv\\Scripts\\python, so the look before the start
    finds python.exe there. The report and the start keep the path as given."""
    request, state = selected
    (tmp_path / "venv" / "Scripts").mkdir(parents=True)
    (tmp_path / "venv" / "Scripts" / "python.exe").write_text("selected interpreter", encoding="ascii")
    monkeypatch.chdir(tmp_path)
    path = str(tmp_path / "venv" / "Scripts" / "python") if form == "absolute" else r"venv\Scripts\python"
    context = replace(request.context, interpreter=lb.Interpreter(path))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.COMPLETED, report.reason
    given = str(tmp_path / "venv" / "Scripts" / "python")
    assert report.items[0].selected == given and state["calls"][0][1][0] == given


@pytest.mark.skipif(os.name != "nt", reason="Windows environment-name semantics")
def test_actual_python_inspection_binds_mixed_case_captured_host_selector(selected, tmp_path):
    request, state = selected
    tools = tmp_path / "tools"
    tools.mkdir()
    executable = tools / "python.EXE"
    executable.write_text("selected interpreter", encoding="ascii")
    context = replace(request.context, interpreter=lb.Interpreter("python"), environment=lb.Environment(
        {"Path": str(tools), "Pathext": ".EXE"}, inherit=False))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.context is context and report.items[0].selected == str(executable)
    assert state["calls"][0][1][0] == str(executable)


def test_container_metadata_uses_selected_container_runtime_environment_and_controlled_input_admission(selected, monkeypatch):
    request, state = selected
    execution = lb.ContainerExecution("borrowed", request.context.root, container_work_dir="/zone",
                                      helper_python="/control/python")
    context = replace(request.context, root="/project", interpreter=lb.Interpreter("/subject/bin/python"),
                      execution=execution)
    calls = []

    def run(executor, argv, **options):
        calls.append((executor, argv, options))
        assert "env" not in options and executor._context.environment is context.environment
        output = state["stdout"] if argv[-1] == _inspection._PROBE else '{"ok":true}'
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(ContainerExecutor, "run", run)
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.COMPLETED and report.context is context
    assert len(calls) == 2 and all(isinstance(executor, ContainerExecutor) for executor, _, _ in calls)
    assert calls[-1][1] == ["/subject/bin/python", "-I", "-S", "-c", _inspection._PROBE]
    assert not state["calls"]


@pytest.mark.parametrize("tool,affected", [
    ("pytest", {"tests.run_suite", "tests.per_test_evidence", "mutations.run"}),
    ("coverage", {"tests.per_test_evidence"}), ("cosmic-ray", {"mutations.run"})])
def test_missing_installed_metadata_reports_required_dependencies_not_a_measured_result(selected, tool, affected):
    request, state = selected
    value = json.loads(state["stdout"])
    value["tools"][tool] = None
    state["stdout"] = json.dumps(value)
    report = lb.inspect_project("python", request)
    assert report.outcome is lb.Outcome.COMPLETED and report.context is request.context
    item = next(item for item in report.items if item.key == tool)
    assert item.required and item.readiness is Availability.MISSING_SETUP and item.version is None
    # The advice installs the version LangAdapter pins for that Python.
    from langadapter.python import _pinned_tools

    pin = f"{tool}=={_pinned_tools(value['version'])[tool]}"
    assert item.action.endswith(f"-m pip install {pin}")
    operations = {item.name: item for item in report.operations}
    for name in request.required_operations:
        assert operations[name].readiness is (Availability.MISSING_SETUP if name in affected else None)
        assert (f"install {pin} " in operations[name].action) is (name in affected)


@pytest.mark.parametrize("stdout", ["", "[]", "null", "{}", '{"implementation":"CPython"}',
    '{"implementation":"CPython","version":"3.12","tools":{}}',
    '{"implementation":"CPython","version":"3.12","tools":{"pytest":true,"coverage":null,"cosmic-ray":null}}'])
def test_invalid_metadata_never_becomes_available(selected, stdout):
    request, state = selected
    state["stdout"] = stdout
    report = lb.inspect_project("python", request)
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE and report.context is None
    assert report.items[0].version is None and report.items[0].readiness is None
    assert all(item.readiness is None for item in report.operations
               if item.name in request.required_operations)


@pytest.mark.parametrize("failure,outcome,reason", [
    (FileNotFoundError("private"), lb.Outcome.MISSING_SETUP, "the selected interpreter file does not exist"),
    (PermissionError("private"), lb.Outcome.INFRASTRUCTURE_FAILURE,
     "the selected file cannot be run: permission to execute it is missing"),
    (OSError("private"), lb.Outcome.INFRASTRUCTURE_FAILURE,
     "the selected file could not be started as a program, so it may not be a Python interpreter"),
    (subprocess.TimeoutExpired("private", 7.5), lb.Outcome.TIMED_OUT,
     "the selected interpreter did not answer within 7.5 seconds"),
    (subprocess.SubprocessError("private"), lb.Outcome.INFRASTRUCTURE_FAILURE,
     "the selected interpreter could not be started")])
def test_failed_native_observation_is_typed_and_retains_no_false_version(selected, failure, outcome, reason):
    request, state = selected
    state["error"] = failure
    report = lb.inspect_project("python", request)
    assert report.outcome is outcome and report.context is None
    assert report.items[0].key == "runtime" and report.items[0].version is None
    assert "private" not in repr(report.safe_export())
    # Plain words for what happened, and what to do about it.
    assert report.items[0].reason == report.reason == reason and report.items[0].action
    expected = Availability.MISSING_SETUP if outcome is lb.Outcome.MISSING_SETUP else None
    assert report.items[0].readiness is expected
    assert all(item.readiness is expected for item in report.operations
               if item.supported and item.execution_backed)


def test_a_missing_interpreter_file_is_named_plainly(tmp_path):
    """A real check of an interpreter path that does not exist."""
    missing = tmp_path / ("no-such-python.exe" if os.name == "nt" else "no-such-python")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=str(missing)))
    report = lb.inspect_project("python", lb.ProjectSetupRequest(context, (), ("tests.run_suite",)))
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    runtime = report.items[0]
    assert runtime.key == "runtime" and runtime.selected == str(missing)
    assert runtime.reason == "the selected interpreter file does not exist"
    assert runtime.action == "select an existing interpreter by its absolute path"


def test_a_program_that_is_not_python_is_named_plainly(selected):
    request, state = selected
    state["returncode"] = 2
    report = lb.inspect_project("python", request)
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert report.items[0].reason == ("the selected program exited with code 2 instead of reporting "
                                      "its Python version, so it may not be a Python interpreter")
    state["returncode"], state["stdout"] = 0, "Hello from another program"
    report = lb.inspect_project("python", request)
    assert report.items[0].reason == "the selected program ran but did not answer as a Python interpreter does"


@pytest.mark.parametrize("marker", ["_langadapter_command", "_langadapter_prepared_copy",
                                      "_langadapter_prepared_project", "_langadapter_owned_directories"])
def test_unresolved_owner_is_never_converted_to_a_report(selected, marker):
    request, state = selected
    error = OSError("same owned primary")
    owner = object()
    setattr(error, marker, (owner,) if marker.endswith("directories") else owner)
    state["error"] = error
    with pytest.raises(OSError) as caught:
        lb.inspect_project("python", request)
    assert caught.value is error


def test_native_cancellation_preserves_original_exception(selected):
    request, state = selected
    error = KeyboardInterrupt("original cancellation")
    state["error"] = error
    with pytest.raises(KeyboardInterrupt) as caught:
        lb.inspect_project("python", request)
    assert caught.value is error


def test_nonzero_probe_does_not_parse_success_looking_output(selected):
    request, state = selected
    state["returncode"] = 3
    report = lb.inspect_project("python", request)
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert report.context is None and report.items[0].version is None
    assert all(item.readiness is None for item in report.operations if item.supported)


@pytest.mark.parametrize("observation", ["missing_runtime", "timeout", "malformed", "nonzero",
                                         "infrastructure", "subprocess", "missing_tool", "success"])
def test_public_failure_missing_and_static_operation_controls_are_distinct(selected, monkeypatch, observation):
    request, state = selected
    if observation == "missing_runtime":
        state["error"] = FileNotFoundError("selected runtime is missing")
    elif observation == "timeout":
        state["error"] = subprocess.TimeoutExpired("selected probe", 1)
    elif observation == "malformed":
        state["stdout"] = "unreadable metadata"
    elif observation == "nonzero":
        state["returncode"] = 3
    elif observation == "infrastructure":
        state["error"] = OSError("probe infrastructure failed")
    elif observation == "subprocess":
        state["error"] = subprocess.SubprocessError("probe failed")
    elif observation == "missing_tool":
        value = json.loads(state["stdout"])
        value["tools"]["pytest"] = None
        state["stdout"] = json.dumps(value)
    provider = type(lb.create("python", replace(request.context, interpreter=None)))
    capabilities = provider.operation_capabilities

    def declared(adapter):
        return tuple(replace(item, supported=False, readiness=Availability.UNSUPPORTED,
                             reason="fixture-declared unsupported operation", action="fixture-declared next step")
                     if item.name == "tests.benchmark" else item for item in capabilities(adapter))

    monkeypatch.setattr(provider, "operation_capabilities", declared)
    operations = (*request.required_operations, "sources.metrics", "tests.benchmark")
    static = lb.create("python", replace(request.context, interpreter=None)).operation_capabilities()
    report = lb.inspect_project("python", replace(request, required_operations=operations))
    reported = {item.name: item for item in report.operations}
    assert len(reported) == len(static)
    assert any(not item.supported for item in static)
    for original in static:
        result = reported[original.name]
        if not original.supported:
            assert result == original
        elif not original.execution_backed:
            assert result.readiness is None
        elif observation == "missing_runtime":
            assert result.readiness is Availability.MISSING_SETUP
        elif observation in {"timeout", "malformed", "nonzero", "infrastructure", "subprocess", "success"}:
            assert result.readiness is None
    if observation == "missing_tool":
        assert reported["tests.run_suite"].readiness is Availability.MISSING_SETUP
        assert reported["sources.metrics"].readiness is None
    # Whatever was observed, each unconfirmed requirement names a next step.
    assert not _without_action(report, operations)


def test_verification_uses_an_in_project_interpreter_in_place(selected):
    """An in-project interpreter outside any environment directory is used in
    place: verification imports with it and writes no bytecode."""
    request, state = selected
    Path(request.context.interpreter.path).write_bytes(b"controlled interpreter placeholder\n")
    report = lb.verify_project("python", replace(request, allow_acquisition=False))
    assert report.scope == "verification"
    assert not any(item.key == "runtime_role" for item in report.items)
    imports = [argv for _, argv, _ in state["calls"][1:]]
    assert imports and all(argv[0] == request.context.interpreter.path and "-B" in argv for argv in imports)


def _environment_python(home, *, conda=False):
    # Windows Conda environments keep python.exe at their root, venvs in Scripts.
    executable = home / ("bin/python" if os.name != "nt" else
                         "python.exe" if conda else "Scripts/python.exe")
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"environment interpreter placeholder\n")
    return executable


def test_provision_requested_runtime_is_honestly_unattempted(selected):
    request, state = selected
    context = replace(request.context, interpreter=lb.Interpreter(provisioning=lb.ProvisioningRequest("3.10")))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.UNSUPPORTED and report.context is None
    assert not state["calls"] and report.items[0].outcome is None and report.items[0].required


def test_unselected_runtime_without_project_environment_is_missing_setup(selected, monkeypatch):
    request, state = selected
    monkeypatch.setattr(sys, "executable", str(Path(request.context.root) / "framework-python"))
    context = replace(request.context, interpreter=None,
                      environment=lb.Environment({"VIRTUAL_ENV": str(Path(request.context.root) / "chosen")}, inherit=False))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    item = report.items[0]
    assert not state["calls"] and item.required and item.readiness is Availability.MISSING_SETUP
    assert len(item.candidates) == 2 and all(Path(path).is_absolute() for path in item.candidates)
    assert item.action and "framework-python" not in repr(report.safe_export())


def test_unselected_runtime_selects_the_one_project_environment_with_its_reason(selected):
    request, state = selected
    root = Path(request.context.root)
    executable = _environment_python(root / ".venv")
    # The activated environment is the same .venv: one environment, not two.
    context = replace(request.context, interpreter=None,
                      environment=lb.Environment({"VIRTUAL_ENV": str(root / ".venv")}, inherit=False))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.COMPLETED
    assert report.context == replace(context, interpreter=lb.Interpreter(str(executable)))
    assert [argv[0] for _, argv, _ in state["calls"]] == [str(executable)]
    runtime = report.items[0]
    assert runtime.selected == str(executable) and "only project environment" in runtime.reason


def test_the_caller_s_own_environment_is_chosen_only_inside_the_project(selected, tmp_path,
                                                                       monkeypatch):
    """The caller's activated environment is chosen only inside the project."""
    request, state = selected
    own = tmp_path.parent / (tmp_path.name + "-own")
    executable = _environment_python(own)
    monkeypatch.setattr(sys, "prefix", str(own))
    context = replace(request.context, interpreter=None,
                      environment=lb.Environment({"VIRTUAL_ENV": str(own)}, inherit=False))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    runtime = report.items[0]
    assert not state["calls"] and runtime.readiness is Availability.MISSING_SETUP
    assert "the one the caller runs in" in runtime.reason and str(executable) in runtime.candidates
    inside = Path(request.context.root) / "tools-env"
    executable = _environment_python(inside)
    monkeypatch.setattr(sys, "prefix", str(inside))
    context = replace(context, environment=lb.Environment({"VIRTUAL_ENV": str(inside)},
                                                          inherit=False))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.COMPLETED and report.items[0].selected == str(executable)


def test_the_caller_s_own_environment_beside_the_project_s_venv_is_no_candidate(
        selected, tmp_path, monkeypatch):
    """A tool run from its own activated environment, outside the project,
    checks a project with a .venv: the .venv is selected, not a choice
    between the two."""
    request, state = selected
    venv = _environment_python(Path(request.context.root) / ".venv")
    own = tmp_path.parent / (tmp_path.name + "-own")
    _environment_python(own)
    monkeypatch.setattr(sys, "prefix", str(own))
    context = replace(request.context, interpreter=None,
                      environment=lb.Environment({"VIRTUAL_ENV": str(own)}, inherit=False))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.COMPLETED
    runtime = report.items[0]
    assert runtime.selected == str(venv) and not runtime.ambiguous
    assert [argv[0] for _, argv, _ in state["calls"]] == [str(venv)]


def test_several_project_environments_need_an_explicit_choice(selected, tmp_path):
    request, state = selected
    root = Path(request.context.root)
    first, second = _environment_python(root / ".venv"), _environment_python(tmp_path / "activated")
    context = replace(request.context, interpreter=None,
                      environment=lb.Environment({"VIRTUAL_ENV": str(tmp_path / "activated")}, inherit=False))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.context is None and not state["calls"]
    runtime = report.items[0]
    assert runtime.ambiguous and runtime.required and runtime.selected is None
    assert set(runtime.candidates) == {str(first), str(second)}
    # Before the choice, both scopes point the operations at it, untried.
    for scope in (lb.inspect_project, lb.verify_project):
        report = scope("python", replace(request, context=context))
        operations = [item for item in report.operations if item.name in request.required_operations]
        assert {(item.action, item.tried_by) for item in operations} == {("select one interpreter", None)}


def test_activated_conda_environment_is_discovered_at_its_platform_address(selected, tmp_path):
    request, state = selected
    executable = _environment_python(tmp_path / "conda", conda=True)
    # A Scripts\python.exe beside it is not a Conda interpreter address.
    _environment_python(tmp_path / "conda")
    context = replace(request.context, interpreter=None,
                      environment=lb.Environment({"CONDA_PREFIX": str(tmp_path / "conda")}, inherit=False))
    report = lb.inspect_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.COMPLETED
    assert report.items[0].selected == str(executable)
    assert [argv[0] for _, argv, _ in state["calls"]] == [str(executable)]


def test_public_discovery_counts_each_environment_once_and_ignores_relative_homes(tmp_path):
    from langadapter.python.project import _discover_environments, _environment_addresses

    root = tmp_path / "project"
    venv = _environment_python(root / ".venv")
    found = _discover_environments(str(root), {"VIRTUAL_ENV": str(root / ".venv"),
                                              "CONDA_PREFIX": "relative/conda"})
    assert found.candidates == (str(venv),) and found.existing == (str(venv),)
    assert found.selected == str(venv)
    conda = _environment_python(tmp_path / "conda", conda=True)
    found = _discover_environments(str(root), {"CONDA_PREFIX": str(tmp_path / "conda")})
    assert found.existing == (str(venv), str(conda)) and found.selected is None
    assert _environment_addresses("/container/project", {"CONDA_PREFIX": "/opt/conda"}, posix=True) == (
        "/container/project/.venv/bin/python", "/opt/conda/bin/python")


def test_public_discovery_names_the_caller_s_own_environment_apart(tmp_path, monkeypatch):
    """The activated environment this process runs in, outside the project,
    is looked at but not among the project's environments."""
    from langadapter.python.project import _discover_environments

    root = tmp_path / "project"
    venv = _environment_python(root / ".venv")
    own = _environment_python(tmp_path / "own")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "own"))
    found = _discover_environments(str(root), {"VIRTUAL_ENV": str(tmp_path / "own")})
    assert found.candidates == (str(venv), str(own))
    assert found.existing == (str(venv),) and found.selected == str(venv)
    assert found.callers_own == str(own)
    found = _discover_environments(str(tmp_path / "plain"), {"VIRTUAL_ENV": str(tmp_path / "own")})
    assert found.existing == () and found.callers_own == str(own)


def test_two_environments_from_one_base_python_need_an_explicit_choice(tmp_path, monkeypatch):
    from langadapter.python import project as environments

    root = tmp_path / "project"
    venv = _environment_python(root / ".venv")
    other = _environment_python(tmp_path / "activated")
    native = os.path.realpath
    base = str(tmp_path / "base-python")
    # On Linux each venv's bin/python links to the base interpreter it was made from.
    monkeypatch.setattr(environments.os.path, "realpath",
                        lambda path: base if Path(path) in (venv, other) else native(path))
    found = environments._discover_environments(
        str(root), {"VIRTUAL_ENV": str(tmp_path / "activated")})
    assert found.existing == (str(venv), str(other)) and found.selected is None


def test_environment_dir_names_only_an_in_project_environment(tmp_path):
    from langadapter.python.project import environment_dir

    root = tmp_path / "project"
    venv = _environment_python(root / ".venv")
    (root / ".venv" / "pyvenv.cfg").write_text("home = base\n", encoding="utf-8")
    assert environment_dir(str(venv), str(root)) == ".venv"
    conda = _environment_python(root / "envs" / "dev", conda=True)
    (root / "envs" / "dev" / "conda-meta").mkdir()
    assert environment_dir(str(conda), str(root)) == "envs/dev"
    outside = _environment_python(tmp_path / "elsewhere")
    (tmp_path / "elsewhere" / "pyvenv.cfg").write_text("home = base\n", encoding="utf-8")
    assert environment_dir(str(outside), str(root)) is None
    plain = root / "tools" / "python.exe"
    plain.parent.mkdir(parents=True)
    plain.write_bytes(b"")
    assert environment_dir(str(plain), str(root)) is None
    # A project that is itself the environment is not excluded from itself.
    assert environment_dir(str(venv), str(root / ".venv")) is None


@pytest.mark.parametrize("needed", [False, True])
def test_container_runtime_is_never_discovered(selected, tmp_path, needed):
    request, state = selected
    context = lb.ProjectContext("/container/project", None, execution=lb.ContainerExecution(
        "borrowed", str(tmp_path), helper_python="/usr/bin/python3"))
    operations = (lb.OperationCapability("tests.run_suite", True, execution_backed=True),)
    report = _inspection.inspect(SimpleNamespace(operation_capabilities=lambda: operations),
                                 replace(request, context=context,
                                         required_operations=("tests.run_suite",) if needed else ()))
    # Missing setup when the operations need an interpreter, else complete.
    assert report.outcome is (lb.Outcome.MISSING_SETUP if needed else lb.Outcome.COMPLETED)
    assert report.context is (None if needed else context) and not state["calls"]
    assert report.items[0].outcome is lb.Outcome.MISSING_SETUP and report.items[0].required is needed


def test_probe_metadata_uses_selected_venv_precedence_without_importing_tools(tmp_path, monkeypatch):
    venv, base = tmp_path / "venv", tmp_path / "base"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("include-system-site-packages = true\n", encoding="ascii")
    selected_sites = [venv / "site-packages", base / "site-packages"]
    for site_dir, version in zip(selected_sites, ("8.3.5", "7.0.0")):
        metadata = site_dir / f"pytest-{version}.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text(f"Name: pytest\nVersion: {version}\n", encoding="ascii")
        (site_dir / "pytest.py").write_text("raise AssertionError('tool import is forbidden')\n", encoding="ascii")
        (site_dir / "startup.pth").write_text("import pytest\n", encoding="ascii")
    monkeypatch.setattr(sys, "executable", str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    monkeypatch.setattr(sys, "prefix", str(base))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    seen = []

    def sites(prefixes):
        seen.append(prefixes)
        return [str(Path(prefix, "site-packages")) for prefix in prefixes]

    monkeypatch.setattr(site, "getsitepackages", sites)
    output = io.StringIO()
    with redirect_stdout(output):
        exec(_inspection._PROBE, {})
    assert seen == [[str(venv)], [str(base)]]
    value = json.loads(output.getvalue())
    assert value["tools"] == {"pytest": "8.3.5", "coverage": None, "cosmic-ray": None}


def test_probe_finds_the_environment_dir_a_patched_site_leaves_out(tmp_path, monkeypatch):
    """Debian's site names an environment's own directory only after site has
    run, and the probe runs with -S. The base's copy of a tool must not win."""
    venv, base = tmp_path / "venv", tmp_path / "base"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("include-system-site-packages = true\n", encoding="ascii")
    release = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    own = venv / ("Lib" if os.name == "nt" else f"lib/{release}") / "site-packages"
    for site_dir, version in ((own, "8.3.5"), (base / "dist-packages", "7.0.0")):
        metadata = site_dir / f"pytest-{version}.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text(f"Name: pytest\nVersion: {version}\n", encoding="ascii")
    monkeypatch.setattr(sys, "executable", str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    monkeypatch.setattr(sys, "prefix", str(base))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(site, "getsitepackages",
                        lambda prefixes: [str(Path(prefix, "dist-packages")) for prefix in prefixes])
    output = io.StringIO()
    with redirect_stdout(output):
        exec(_inspection._PROBE, {})
    assert json.loads(output.getvalue())["tools"]["pytest"] == "8.3.5"


@pytest.mark.parametrize("config, base_included", [
    ("home = /base\n", True),
    ("include-system-site-packages = TRUE\n", True),
    ("include-system-site-packages = false\n", False),
    ("include-system-site-packages = yes\n", False),
    ("home = /base\nno separator\ninclude-system-site-packages = true\n"
     "include-system-site-packages = false\n", False),
], ids=["absent", "upper-case-true", "false", "yes", "last-line"])
def test_probe_reads_pyvenv_cfg_as_the_interpreters_site_does(tmp_path, monkeypatch, config, base_included):
    """The probe runs with -S and reads pyvenv.cfg as site.py does: the last
    include-system-site-packages line decides, only "true" in any letter case
    includes the base, and lines without "=" are skipped."""
    venv, base = tmp_path / "venv", tmp_path / "base"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(config, encoding="ascii")
    monkeypatch.setattr(sys, "executable", str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    monkeypatch.setattr(sys, "prefix", str(base))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    seen = []

    def sites(prefixes):
        seen.append(prefixes)
        return [str(Path(prefix, "site-packages")) for prefix in prefixes]

    monkeypatch.setattr(site, "getsitepackages", sites)
    with redirect_stdout(io.StringIO()):
        exec(_inspection._PROBE, {})
    assert seen == ([[str(venv)], [str(base)]] if base_included else [[str(venv)]])


def test_probe_takes_the_environment_above_the_executables_dir(tmp_path, monkeypatch):
    """site.py also reads a pyvenv.cfg beside the executable, and the
    environment is still the directory above the executable's directory."""
    venv, base = tmp_path / "venv", tmp_path / "base"
    executable = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable.parent.mkdir(parents=True)
    (executable.parent / "pyvenv.cfg").write_text("include-system-site-packages = false\n", encoding="ascii")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "prefix", str(base))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    seen = []

    def sites(prefixes):
        seen.append(prefixes)
        return [str(Path(prefix, "site-packages")) for prefix in prefixes]

    monkeypatch.setattr(site, "getsitepackages", sites)
    with redirect_stdout(io.StringIO()):
        exec(_inspection._PROBE, {})
    assert seen == [[str(venv)]]


@pytest.mark.parametrize("config, user_seen", [
    (None, True),
    ("include-system-site-packages = true\n", True),
    ("include-system-site-packages = false\n", False),
], ids=["system", "venv-with-system", "venv"])
def test_probe_lists_the_user_site_dir_where_runs_see_it(tmp_path, monkeypatch, config, user_seen):
    """Runs import without -s, so a tool installed with pip --user counts
    outside a virtual environment and in one that includes the system's
    packages, though the probe itself runs with -I."""
    home, user = tmp_path / "python", tmp_path / "user-site"
    metadata = user / "pytest-6.0.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: pytest\nVersion: 6.0.0\n", encoding="ascii")
    executable = home / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable.parent.mkdir(parents=True)
    if config is not None:
        (home / "pyvenv.cfg").write_text(config, encoding="ascii")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "prefix", str(home))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setattr(site, "getsitepackages",
                        lambda prefixes: [str(Path(prefix, "site-packages")) for prefix in prefixes])
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(user))
    output = io.StringIO()
    with redirect_stdout(output):
        exec(_inspection._PROBE, {})
    assert json.loads(output.getvalue())["tools"]["pytest"] == ("6.0.0" if user_seen else None)
