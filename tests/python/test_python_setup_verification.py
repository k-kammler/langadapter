"""Public verification with real owned copies and controlled native probes."""
from contextlib import redirect_stdout
from dataclasses import replace
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _execution_scope, preparation, setup
from langadapter.capabilities import Availability
from langadapter.execution import LocalExecutor
from langadapter.python import _setup as _inspection
from langadapter.python import _setup as _verification


@pytest.fixture
def selected(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "subject.py").write_bytes(b"value = 1\n")
    (source / "pytest.ini").write_bytes(b"[pytest]\n")
    (tmp_path / "runtime").write_bytes(b"controlled interpreter placeholder\n")
    context = lb.ProjectContext(str(source), lb.Interpreter(str(tmp_path / "runtime")),
        execution=lb.LocalExecution(str(tmp_path)),
        test_framework=lb.TestFrameworkConfig("project", "pytest.ini"),
        environment=lb.Environment({"PATH": "selected", "API_KEY": "private"}, inherit=False),
        interpreter_flags=("-X", "selected-option"))
    request = lb.ProjectSetupRequest(context, ("subject.py",),
        ("tests.run_suite", "tests.per_test_evidence", "mutations.run"), allow_acquisition=True)
    state = SimpleNamespace(calls=[], owners=[], statuses={}, code=0, output=None, error=None,
                            after_import=None, tools={"pytest": "8.3.5", "coverage": "7.6.1", "cosmic-ray": "8.4.3"})
    real_prepare = _verification.prepare_project

    def prepare(context, **options):
        owner = real_prepare(context, **options)
        state.owners.append((owner, options))
        return owner

    def run(executor, argv, **options):
        state.calls.append((executor._context, argv, options))
        if argv[-1] == _inspection._PROBE:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"implementation": "CPython",
                "version": "3.10.19", "tools": state.tools}), "")
        context = executor._context
        context._require_execution()
        assert context.root != request.context.root
        expected = b"value = 1\n" if len(state.calls) == 2 else b"copy-only import side effect\n"
        assert Path(context.root, "subject.py").read_bytes() == expected
        assert context.test_framework.path == str(Path(context.root, "pytest.ini"))
        Path(context.root, "subject.py").write_bytes(b"copy-only import side effect\n")
        Path(context.root, "import-created.txt").write_bytes(b"temporary\n")
        if state.error is not None:
            raise state.error
        if state.after_import is not None:
            state.after_import()
        output = state.output if state.output is not None else json.dumps({
            "status": state.statuses.get(argv[-1], "imported")})
        return subprocess.CompletedProcess(argv, state.code, output, "private raw stderr")

    monkeypatch.setattr(_verification, "prepare_project", prepare)
    monkeypatch.setattr(LocalExecutor, "run", run)
    # The collection of the copy's tests has tests of its own with real runs
    # (test_editable_install.py): these control the tool imports.
    monkeypatch.setattr(_verification, "_collection_items", lambda adapter, request: [])
    monkeypatch.setenv("PATH", "later ambient")
    return request, state


def _without_action(report, operations):
    """Required items and operations that are not confirmed yet name no next step."""
    return ([item.key for item in report.items if item.required and item.action is None
             and not (item.readiness is Availability.AVAILABLE and item.outcome is lb.Outcome.COMPLETED)]
            + [item.name for item in report.operations if item.name in operations
               and item.action is None and item.readiness is not Availability.AVAILABLE])


def _verification_hints(report):
    """Entries of a verification report that say to run the verification."""
    return [entry for entry in (*report.items, *report.operations)
            if entry.action == "run the tool check to try it"]


def _not_run_only(report, operations):
    """Required items and operations unconfirmed for any reason but waiting for a run."""
    return ([item.key for item in report.items if item.required and item.tried_by != "run"
             and not (item.readiness is Availability.AVAILABLE and item.outcome is lb.Outcome.COMPLETED)]
            + [item.name for item in report.operations if item.name in operations
               and item.tried_by != "run" and item.readiness is not Availability.AVAILABLE])


def test_a_collection_without_a_verdict_blames_no_test_module(monkeypatch):
    """A collection that gave no verdict (a pytest crash, an exit with no
    collection error) points at its reason, not at a test module, and leaves
    the origin to the first run."""
    from langadapter.python import _runs

    failed = SimpleNamespace(outcome=lb.Outcome.INFRASTRUCTURE_FAILURE,
                             reason="collection exited with 3", diagnostic="")
    monkeypatch.setattr(_runs, "verification_collection",
                        lambda adapter, selected, timeout_s: (failed, None, False))
    request = SimpleNamespace(source_files=("subject.py",), remaining_timeout=lambda *a, **k: 60.0)
    collection, origin = _verification._collection_items(None, request)
    assert collection.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert collection.action == "fix what the reason names, then run the tool check again"
    assert (origin.outcome, origin.tried_by) == (None, "run")


def test_public_tool_verification_keeps_original_selection_and_required_unknowns(selected):
    request, state = selected
    request = replace(request, required_operations=("tests.run_suite",))
    report = lb.verify_project("python", request)
    assert report.scope == "verification" and report.outcome is lb.Outcome.COMPLETED
    assert report.context is request.context and report.inputs.required == ("pytest.ini", "subject.py")
    assert len(state.calls) == 2 and len(state.owners) == 1
    copy, argv, options = state.calls[1]
    assert argv == [request.context.interpreter.path, "-X", "selected-option", "-B", "-c", _verification._IMPORT, "pytest"]
    assert options["cwd"] == copy.root and "env" not in options
    assert copy.environment is request.context.environment
    assert 0 < options["timeout"] <= request.probe_timeout_s
    owner, prepare_options = state.owners[0]
    assert prepare_options["inputs"] == report.inputs and 0 < prepare_options["timeout_s"] <= 120
    assert not Path(owner.root).exists() and not Path(owner._prepared_copy.baseline.root).exists()
    assert Path(request.context.root, "subject.py").read_bytes() == b"value = 1\n"
    assert not Path(request.context.root, "import-created.txt").exists()
    items = {item.key: item for item in report.items}
    assert items["pytest"].readiness is Availability.AVAILABLE and items["pytest"].version == "8.3.5"
    assert items["coverage"].readiness is None and not items["coverage"].required
    assert items["native_execution"].required and items["native_execution"].outcome is None
    assert all(item.readiness is not Availability.AVAILABLE for item in report.operations)
    assert "private" not in repr(report.safe_export())
    # An unused tool needs nothing, and a run tries the rest.
    run = "start a run, which tries it for the first time"
    assert items["coverage"].action is None and items["native_execution"].action == run
    suite = next(item for item in report.operations if item.name == "tests.run_suite")
    assert suite.action == run and suite.reason == "its tools load, and the operation itself is not tried yet"
    assert not _without_action(report, request.required_operations)
    # What a completed verification leaves unconfirmed only a run can try.
    assert _not_run_only(report, request.required_operations) == []


def test_a_check_started_inside_the_project_verifies_it(selected):
    """With PWD and OLDPWD naming the project directory, as a shell started there
    exports them, the check still prepares its copy and passes the captured
    environment to its commands unchanged."""
    request, state = selected
    root = request.context.root
    environment = lb.Environment({"PATH": "selected", "PWD": root, "OLDPWD": root},
                                 inherit=False)
    request = replace(request, context=replace(request.context, environment=environment),
                      required_operations=("tests.run_suite",))
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.COMPLETED, report.reason
    assert len(state.owners) == 1
    copy, _argv, options = state.calls[1]
    assert "env" not in options and copy.environment is environment


def test_public_tool_verification_closes_readonly_copy_without_unlocking_original(selected, monkeypatch):
    request, state = selected
    original = Path(request.context.root, "subject.py")
    original.chmod(stat.S_IRUSR)
    mode = original.stat().st_mode
    runtime_metadata = LocalExecutor.run

    def run(executor, argv, **options):
        if argv[-1] == _inspection._PROBE:
            return runtime_metadata(executor, argv, **options)
        executor._context._require_execution()
        assert Path(executor._context.root, "subject.py").read_bytes() == b"value = 1\n"
        assert original.stat().st_mode == mode
        return subprocess.CompletedProcess(argv, 0, '{"status":"imported"}', "")

    monkeypatch.setattr(LocalExecutor, "run", run)
    try:
        report = lb.verify_project("python", request)
        assert report.outcome is lb.Outcome.COMPLETED and report.context is request.context
        owner, _ = state.owners[0]
        assert not Path(owner.root).exists() and not owner._prepared_copy.baseline.root.exists()
        assert original.read_bytes() == b"value = 1\n" and original.stat().st_mode == mode
        assert next(item for item in report.items if item.key == "native_execution").readiness is None
    finally:
        original.chmod(mode | stat.S_IWUSR)


def test_required_coverage_facet_imports_coverage_for_the_suite_run(selected):
    request, state = selected
    request = replace(request, required_operations=("tests.run_suite",),
                      required_facets={"tests.run_suite": ("coverage",)})
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.COMPLETED
    assert [argv[-1] for _, argv, _ in state.calls[1:]] == ["pytest", "coverage"]
    items = {item.key: item for item in report.items}
    assert items["coverage"].required and items["coverage"].readiness is Availability.AVAILABLE
    state.calls.clear()
    state.statuses["coverage"] = "missing"
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.MISSING_SETUP
    operation = next(item for item in report.operations if item.name == "tests.run_suite")
    assert operation.readiness is Availability.MISSING_SETUP and "coverage" in operation.reason


def test_imports_share_one_copy_and_do_not_install_or_run_native_measurements(selected):
    request, state = selected
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.COMPLETED
    imports = state.calls[1:]
    assert [argv[-1] for _, argv, _ in imports] == ["pytest", "coverage", "cosmic_ray"]
    assert len({context.root for context, _, _ in imports}) == 1 and len(state.owners) == 1
    assert all("-m" not in argv and "pip" not in argv for _, argv, _ in imports)


@pytest.mark.parametrize("output", ["", "[]", "null", "{}", '{"status":[]}',
                                   '{"status":true}', '{"status":"invented"}'])
def test_unreadable_import_never_becomes_available(selected, output):
    request, state = selected
    state.output = output
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE and report.context is None
    assert next(item for item in report.items if item.key == "pytest").readiness is None
    assert len(state.calls) == 2 and not Path(state.owners[0][0].root).exists()


@pytest.mark.parametrize("status,outcome", [("missing", lb.Outcome.MISSING_SETUP),
                                           ("failed", lb.Outcome.INFRASTRUCTURE_FAILURE)])
def test_failed_import_stops_later_probes_but_cleans_confirmed_copy(selected, status, outcome):
    request, state = selected
    state.statuses["pytest"] = status
    report = lb.verify_project("python", request)
    assert report.outcome is outcome and report.context is None and len(state.calls) == 2
    assert not Path(state.owners[0][0].root).exists()
    item = next(item for item in report.items if item.key == "pytest")
    assert item.outcome is outcome and item.version is None
    operation = next(item for item in report.operations if item.name == "tests.run_suite")
    assert operation.readiness is (Availability.MISSING_SETUP if status == "missing" else None)
    # The failed tool comes first, and the tools it stopped are tried after it.
    assert ("pip install pytest==" if status == "missing" else 'import pytest') in item.action
    assert next(item for item in report.items if item.key == "coverage").action == (
        "fix pytest first, then run the tool check again")
    assert "pytest" in operation.action and not _without_action(report, request.required_operations)
    # A stopped verification is not waiting only for a run.
    assert "tests.run_suite" in _not_run_only(report, request.required_operations)


def test_nonzero_import_cannot_credit_success_looking_output(selected):
    request, state = selected
    state.code = 3
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE and report.context is None


@pytest.mark.parametrize("error,outcome", [(FileNotFoundError("private"), lb.Outcome.MISSING_SETUP),
    (OSError("private"), lb.Outcome.INFRASTRUCTURE_FAILURE),
    (subprocess.TimeoutExpired("private", 1), lb.Outcome.TIMED_OUT)])
def test_confirmed_native_failures_are_typed_after_copy_cleanup(selected, error, outcome):
    request, state = selected
    state.error = error
    report = lb.verify_project("python", request)
    assert report.outcome is outcome and report.context is None
    assert not Path(state.owners[0][0].root).exists()
    assert "private" not in repr(report.safe_export())


def test_import_completion_exhaustion_stops_the_next_probe_and_closes_copy(selected, monkeypatch):
    request, state = selected
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(setup, "time", SimpleNamespace(monotonic=lambda: clock.now))
    state.after_import = lambda: setattr(clock, "now", 121.0)
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.TIMED_OUT and len(state.calls) == 2
    assert not Path(state.owners[0][0].root).exists()


@pytest.mark.parametrize("tool", ["pytest", "coverage", "cosmic-ray"])
def test_missing_required_metadata_does_not_allocate_or_import(selected, tool):
    request, state = selected
    state.tools[tool] = None
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    assert len(state.calls) == 1 and not state.owners
    # Nothing was loaded or copied, and the reason says so.
    assert report.reason == "the check stopped before it copied the project, so no tool was loaded"
    # The untried tools point at the missing one, not at the check just run.
    others = [item for item in report.items if item.key in ("pytest", "coverage", "cosmic-ray") and item.key != tool]
    assert {item.action for item in others} == {f"install {tool} first, then run the tool check again"}
    assert _verification_hints(report) == []


def test_static_request_does_not_allocate_or_import_tools(selected):
    request, state = selected
    report = lb.verify_project("python", replace(request, required_operations=("sources.metrics",)))
    assert report.outcome is lb.Outcome.COMPLETED and report.context is request.context
    assert len(state.calls) == 1 and not state.owners
    assert report.reason == ("the requested operations need no measurement tool, and the project's "
                             "tests were not run")
    assert not next(item for item in report.items if item.key == "native_execution").required


@pytest.mark.parametrize("alias", [False, True])
def test_an_in_project_runtime_is_used_in_place_with_its_environment_left_out(selected, monkeypatch, alias):
    """As runs do: the selected interpreter stays where it is, its
    environment directory is not copied, and the imports write no bytecode."""
    request, state = selected
    environment = Path(request.context.root, ".venv")
    environment.mkdir()
    (environment / "pyvenv.cfg").write_bytes(b"home = base\n")
    (environment / "python").write_bytes(b"controlled interpreter placeholder\n")
    runtime = str(environment / "python")
    if alias:
        # A POSIX system resolves the path as written, so the directory must exist.
        Path(request.context.root, "child").mkdir()
        runtime = str(Path(request.context.root, "child", "..", ".venv", "python"))
    copies = []
    observed = LocalExecutor.run

    def run(executor, argv, **options):
        if argv[-1] in _verification.TOOL_MODULES.values():
            copies.append(Path(executor._context.root, ".venv").exists())
        return observed(executor, argv, **options)

    monkeypatch.setattr(LocalExecutor, "run", run)
    context = replace(request.context, interpreter=lb.Interpreter(runtime))
    report = lb.verify_project("python", replace(request, context=context))
    assert report.outcome is lb.Outcome.COMPLETED, _explained(report)
    assert len(state.owners) == 1 and ".venv" in state.owners[0][1]["inputs"].exclude
    imports = [argv for _, argv, _ in state.calls[1:]]
    assert len(imports) == 3 and copies == [False] * 3
    assert all(argv[0] == runtime and "-B" in argv for argv in imports)
    assert not _without_action(report, request.required_operations)
    assert _verification_hints(report) == []


def _explained(report):
    """The report's reason with every item's, for a failure on another system."""
    return [report.reason, *((item.key, item.outcome, item.reason) for item in report.items)]


def _tree(root_dir):
    """Every entry under ``root_dir`` with its size and modification time."""
    return {path.relative_to(root_dir).as_posix(): (path.is_dir(), path.lstat().st_size, path.lstat().st_mtime_ns)
            for path in root_dir.rglob("*")}


def test_verification_changes_nothing_in_an_in_project_environment(tmp_path):
    """A real project .venv with an importable stand-in for pytest:
    verification imports it in place and leaves the environment as it was."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "subject.py").write_bytes(b"value = 1\n")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")],
                   check=True, timeout=180, capture_output=True)
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    packages = Path(subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                   check=True, timeout=60, capture_output=True, text=True).stdout.strip())
    (packages / "pytest").mkdir()
    (packages / "pytest" / "__init__.py").write_text("VALUE = 1\n", encoding="ascii")
    (packages / "pytest-8.3.5.dist-info").mkdir()
    (packages / "pytest-8.3.5.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pytest\nVersion: 8.3.5\n", encoding="ascii")
    before = _tree(root / ".venv")
    context = lb.ProjectContext(str(root), lb.Interpreter(path=str(python)))
    report = lb.verify_project("python", lb.ProjectSetupRequest(context, ("subject.py",), ("tests.run_suite",)))
    assert report.outcome is lb.Outcome.COMPLETED, _explained(report)
    assert next(item for item in report.items if item.key == "pytest").readiness is Availability.AVAILABLE
    assert _tree(root / ".venv") == before
    assert not (packages / "pytest" / "__pycache__").exists()


@pytest.mark.parametrize("runtime,outcome", [
    (None, lb.Outcome.MISSING_SETUP),  # no project environment exists to discover
    (lb.Interpreter(provisioning=lb.ProvisioningRequest("3.12")), lb.Outcome.UNSUPPORTED),
])
def test_verification_does_not_provision_or_adopt_framework_python(selected, runtime, outcome):
    request, state = selected
    report = lb.verify_project("python", replace(request, context=replace(request.context, interpreter=runtime)))
    assert report.outcome is outcome and report.context is None
    assert not state.calls and not state.owners
    if runtime is not None:
        # A requested version is no selection: the operations wait for one.
        assert {(item.action, item.tried_by) for item in report.operations
                if item.name in request.required_operations} == {
            ("select an installed interpreter by its path", None)}


def test_cancellation_retains_the_same_primary_and_closes_only_idle_copy(selected):
    request, state = selected
    state.error = KeyboardInterrupt("same cancellation")
    with pytest.raises(KeyboardInterrupt) as caught:
        lb.verify_project("python", request)
    assert caught.value is state.error and not Path(state.owners[0][0].root).exists()


def test_unresolved_command_retains_primary_and_actual_copy_until_recovery(selected, monkeypatch):
    request, state = selected
    command = SimpleNamespace(_langadapter_command_disposed=False)
    error = OSError("same unresolved command")
    error._langadapter_command = command
    real_run = LocalExecutor.run

    def run(executor, argv, **options):
        if argv[-1] != _inspection._PROBE:
            _execution_scope.track(command, "verification command")
            state.error = error
        return real_run(executor, argv, **options)

    monkeypatch.setattr(LocalExecutor, "run", run)
    with pytest.raises(OSError) as caught:
        lb.verify_project("python", request)
    owner = state.owners[0][0]
    assert caught.value is error and error._langadapter_prepared_project is owner
    assert error._langadapter_command is command
    if hasattr(error, "add_note"):
        assert any("Verification copy cleanup failed" in note for note in error.__notes__)
    assert Path(owner.root).is_dir() and Path(owner._prepared_copy.owned.receipt).is_file()
    _execution_scope.completed(command)
    command._langadapter_command_disposed = True
    owner.close()
    assert not Path(owner.root).exists()


def test_post_allocation_factory_failure_is_not_converted_to_a_missing_item(selected, monkeypatch):
    request, state = selected
    failure = OSError("after owned allocation")
    real = preparation._verify_independent

    def fail(prepared_copy, parent):
        real(prepared_copy, parent)
        raise failure

    monkeypatch.setattr(preparation, "_verify_independent", fail)
    with pytest.raises(OSError) as caught:
        lb.verify_project("python", request)
    assert caught.value is failure and len(state.calls) == 1
    owner = failure._langadapter_prepared_project
    assert Path(owner.root).is_dir() and Path(owner._prepared_copy.owned.receipt).is_file()
    owner.close()


def test_failed_final_close_cannot_return_a_success_report(selected, monkeypatch):
    request, state = selected
    failure = OSError("copy disposal failed")
    real_close = preparation.PreparedProject._close
    pending = True

    def close(owner):
        if pending:
            raise failure
        return real_close(owner)

    monkeypatch.setattr(preparation.PreparedProject, "_close", close)
    with pytest.raises(OSError) as caught:
        lb.verify_project("python", replace(request, required_operations=("tests.run_suite",)))
    owner = state.owners[0][0]
    assert caught.value is failure and failure._langadapter_prepared_project is owner
    assert Path(owner.root).is_dir() and Path(owner._prepared_copy.owned.receipt).is_file()
    pending = False
    owner.close()
    owner.close()
    assert not Path(owner.root).exists()


def test_runtime_disappearing_after_metadata_is_a_missing_fact_not_an_invalid_request(selected, monkeypatch):
    request, state = selected
    real_prepare = _verification.prepare_project

    def missing(context, **options):
        Path(context.interpreter.path).unlink()
        return real_prepare(context, **options)

    monkeypatch.setattr(_verification, "prepare_project", missing)
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    assert next(item for item in report.items if item.key == "preparation_input").required
    assert len(state.calls) == 1 and not state.owners


def test_work_dir_disappearing_after_metadata_is_a_missing_fact_not_an_invalid_request(
        selected, tmp_path, monkeypatch):
    request, state = selected
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    request = replace(request, context=replace(request.context, execution=lb.LocalExecution(str(work_dir))))
    real_prepare = _verification.prepare_project

    def missing(context, **options):
        work_dir.rmdir()
        return real_prepare(context, **options)

    monkeypatch.setattr(_verification, "prepare_project", missing)
    report = lb.verify_project("python", request)
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    assert next(item for item in report.items if item.key == "preparation_input").required
    assert len(state.calls) == 1 and not state.owners


def test_original_root_environment_reference_remains_a_policy_refusal(selected):
    request, state = selected
    context = replace(request.context, environment=lb.Environment(
        {"PYTHONPATH": request.context.root}, inherit=False))
    with pytest.raises(lb.InvalidRequest, match="environment"):
        lb.verify_project("python", replace(request, context=context))
    assert len(state.calls) == 1 and not state.owners


@pytest.mark.parametrize("failure,status", [(None, "imported"),
    (ModuleNotFoundError(name="pytest"), "missing"),
    (ModuleNotFoundError(name="dependency"), "failed"), (RuntimeError("private"), "failed")])
def test_probe_classifies_only_direct_missing_tool_as_missing(monkeypatch, failure, status):
    def imported(name):
        assert name == "pytest"
        if failure is not None:
            raise failure

    monkeypatch.setattr(importlib, "import_module", imported)
    monkeypatch.setattr(sys, "argv", ["probe", "pytest"])
    output = io.StringIO()
    with redirect_stdout(output):
        exec(_verification._IMPORT, {})
    assert json.loads(output.getvalue()) == {"status": status}
