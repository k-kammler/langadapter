"""Public assessment uses one registry, selection, budget and native owner."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import setup
from langadapter import setup as _setup


@pytest.fixture
def context(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "Target.fixture").write_text("selected source\n")
    (source / "framework.ini").write_text("[fixture]\n")
    return lb.ProjectContext(str(source), lb.Interpreter(sys.executable),
                             test_framework=lb.TestFrameworkConfig(path="framework.ini"),
                             environment=lb.Environment({"PATH": "selected", "API_KEY": "private-value"}, inherit=False))


def request(context, **changes):
    values = {"context": context, "source_files": ["Target.fixture"], "required_operations": ["tests.run_suite"]}
    return lb.ProjectSetupRequest(**(values | changes))


@pytest.fixture
def mechanism():
    calls = []

    class Native(lb.LangAdapter):
        language = "setup-dispatch-fixture"

        def __init__(self, context):
            super().__init__(context)
            calls.append(("constructor", context))

        def inspect_project(self, requested):
            return self._assess(requested, "inspection")

        def verify_project(self, requested):
            return self._assess(requested, "verification")

        def _assess(self, requested, scope):
            calls.append((scope, requested))
            requested.remaining_timeout()
            return lb.ProjectSetupReport(scope, lb.Outcome.COMPLETED, "", requested.context, requested.inputs,
                [lb.ProjectSetupItem("runtime", True, lb.Availability.AVAILABLE, lb.Outcome.COMPLETED,
                                      selected=requested.context.interpreter.path, version="fixture-observed")],
                [lb.OperationCapability("tests.run_suite", True, readiness=lb.Availability.AVAILABLE)])

    lb.register(Native.language, [".setup-dispatch-fixture"], Native)
    try:
        yield Native, calls
    finally:
        lb.unregister(Native.language)


@pytest.mark.parametrize("scope", ["inspection", "verification"])
def test_public_dispatch_preserves_original_selection_source_only_constructor_effective_inputs_and_budget(context, mechanism, scope):
    native, calls = mechanism
    original = request(context, inputs=lb.ProjectInputSelection([".venv"]), allow_acquisition=True)
    value = (lb.inspect_project if scope == "inspection" else lb.verify_project)(native.language, original)
    assert calls[0][0] == "constructor" and calls[0][1].interpreter is None
    assert calls[0][1].environment is context.environment
    assert calls[0][1].execution is context.execution
    invoked = calls[1][1]
    assert invoked.context is context and invoked is not original
    assert invoked.allow_acquisition is (scope == "verification")
    assert original.allow_acquisition is True and original._deadline is None
    assert value.context is context and value.inputs.required == ("Target.fixture", "framework.ini")
    assert value.inputs.exclude == (".venv",)
    assert value.items[0].version == "fixture-observed" and value.outcome is lb.Outcome.COMPLETED


@pytest.mark.parametrize("scope", ["inspection", "verification"])
def test_an_adapters_own_method_runs_the_same_assessment(context, mechanism, scope):
    """An adapter's own ``inspect_project`` or ``verify_project`` runs the
    module-level assessment: the hook gets the prepared request, with its
    deadline, from an adapter the assessment made, and the report is the same."""
    native, calls = mechanism
    original = request(context)
    name = "inspect_project" if scope == "inspection" else "verify_project"
    adapter = native(context)
    del calls[:]
    value = getattr(adapter, name)(original)
    assert [call[0] for call in calls] == ["constructor", scope]
    invoked = calls[1][1]
    assert invoked is not original and invoked._deadline is not None
    assert original._deadline is None
    expected = getattr(lb, name)(native.language, original)
    assert value.outcome is expected.outcome is lb.Outcome.COMPLETED
    assert value.safe_export() == expected.safe_export()


def test_the_built_in_adapters_inspect_as_the_module_does(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    context = lb.ProjectContext(str(root), lb.Interpreter(sys.executable))
    requested = lb.ProjectSetupRequest(context, source_files=(),
                                       required_operations=("tests.run_suite",))
    report = lb.create("python", context).inspect_project(requested)
    assert report.outcome is lb.Outcome.COMPLETED
    assert report.safe_export() == lb.inspect_project("python", requested).safe_export()


@pytest.mark.parametrize("missing", ["root", "source", "framework"])
def test_missing_selected_input_is_a_setup_fact_before_provider_construction(context, mechanism, missing):
    native, calls = mechanism
    if missing == "root":
        context = replace(context, root=str(Path(context.root) / "missing"))
    else:
        (Path(context.root) / ("Target.fixture" if missing == "source" else "framework.ini")).unlink()
    value = lb.inspect_project(native.language, request(context))
    assert value.outcome is lb.Outcome.MISSING_SETUP and value.context is None
    assert value.items[0].readiness is lb.Availability.MISSING_SETUP
    assert calls == []


def test_missing_runtime_is_not_provisioned_or_adopted_by_common_dispatch(context, mechanism):
    native, calls = mechanism

    def inspect(self, requested):
        assert requested.context.interpreter.path.endswith("missing-python")
        assert self.context.interpreter is None
        return self._unsupported_setup(requested, "inspection")

    native.inspect_project = inspect
    context = replace(context, interpreter=lb.Interpreter(str(Path(context.root) / "missing-python")))
    value = lb.inspect_project(native.language, request(context))
    assert value.outcome is lb.Outcome.UNSUPPORTED and len(calls) == 1


@pytest.mark.parametrize("field,value", [("test_framework", {}), ("root", ""), ("interpreter", {}),
                                        ("execution", {}), ("environment", {"PATH": 1})])
def test_structural_context_errors_refuse_even_when_root_is_missing(context, mechanism, field, value):
    native, calls = mechanism
    context = replace(context, root=str(Path(context.root) / "missing"), **{field: value}) if field != "root" else replace(context, root=value)
    with pytest.raises(lb.InvalidRequest):
        lb.inspect_project(native.language, request(context))
    assert calls == []


@pytest.mark.parametrize("path", ["Target.fixture", "framework.ini"])
def test_exclusion_conflict_refuses_before_provider_or_execution(context, mechanism, path):
    native, calls = mechanism
    with pytest.raises(lb.InvalidRequest, match="required input"):
        lb.inspect_project(native.language, request(context, inputs=lb.ProjectInputSelection([path])))
    assert calls == []


def test_link_root_before_parent_alias_resolution_is_refused(context, mechanism, tmp_path):
    native, calls = mechanism
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(context.root, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit the selected root symlink")
    # Resolution would hide the selected link before the parent component.
    context = replace(context, root=str(linked / ".." / "source"))
    value = lb.inspect_project(native.language, request(context))
    assert value.outcome is lb.Outcome.MISSING_SETUP and calls == []
    assert value.items[0].reason == f"selected project input cannot be a link: {linked}"


def _dir_link(at, target):
    """A link to the directory ``target``: a junction on Windows, which needs no
    symlink right, else a symbolic link."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(at))
    else:
        at.symlink_to(target, target_is_directory=True)


def test_a_project_below_a_linked_dir_is_admitted_as_for_a_run(context, mechanism, tmp_path):
    """Preparation refuses a link only at the root and below it, so the
    check admits a project whose parent directory is a link (/var on macOS)."""
    native, calls = mechanism
    _dir_link(tmp_path / "alias", tmp_path)
    value = lb.inspect_project(native.language, request(replace(context, root=str(tmp_path / "alias" / "source"))))
    assert value.outcome is lb.Outcome.COMPLETED and [call[0] for call in calls] == ["constructor", "inspection"]


def test_only_a_dir_a_later_parent_component_climbs_out_of_must_not_be_a_link(context, mechanism, tmp_path):
    """In alias/sub/../source only sub is climbed out of: the link alias
    above it reads the same through the kernel and in the text."""
    native, calls = mechanism
    (tmp_path / "sub").mkdir()
    _dir_link(tmp_path / "alias", tmp_path)
    selected = replace(context, root=str(tmp_path / "alias" / "sub" / ".." / "source"))
    value = lb.inspect_project(native.language, request(selected))
    assert value.outcome is lb.Outcome.COMPLETED and [call[0] for call in calls] == ["constructor", "inspection"]


@pytest.mark.parametrize("linked", ["root", "input"])
def test_a_linked_project_dir_or_input_is_refused_with_the_link_named(context, mechanism, tmp_path, linked):
    native, calls = mechanism
    if linked == "root":
        link = tmp_path / "linked"
        _dir_link(link, context.root)
        context = replace(context, root=str(link))
    else:
        (tmp_path / "dir").mkdir()
        _dir_link(Path(context.root) / "inputs", tmp_path / "dir")
        # Inputs are admitted below the canonical root.
        link = Path(context.root).resolve() / "inputs"
    value = lb.inspect_project(native.language, request(context, inputs=lb.ProjectInputSelection(required=("inputs",))))
    assert value.outcome is lb.Outcome.MISSING_SETUP and calls == []
    (item,) = value.items
    assert item.key == "inputs" and item.reason == f"selected project input cannot be a link: {link}"
    assert "the link" in item.action


@pytest.mark.skipif(os.name == "nt", reason="the container's helper Python runs on Linux")
def test_the_container_admits_a_project_below_a_linked_dir_and_names_a_linked_root(
        context, mechanism, tmp_path, monkeypatch):
    native, _ = mechanism
    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "linked").symlink_to(context.root, target_is_directory=True)

    def run(argv, **options):
        # The helper's own script, run by this Python in place of the container's.
        return subprocess.run([sys.executable, *argv[1:]], capture_output=True, text=True,
                              timeout=options["timeout"], check=False)

    monkeypatch.setattr(_setup, "executor_for", lambda selected: SimpleNamespace(run=run))
    execution = lb.ContainerExecution("fixture", str(tmp_path), helper_python="/control/python")
    reports = {name: lb.inspect_project(native.language, request(replace(
        context, root=str(tmp_path / name), execution=execution, interpreter=lb.Interpreter("/runtime/python"))))
        for name in ("alias/source", "linked")}
    assert reports["alias/source"].outcome is lb.Outcome.COMPLETED
    (item,) = reports["linked"].items
    assert reports["linked"].outcome is lb.Outcome.MISSING_SETUP
    assert item.reason == f"selected project input cannot be a link: {tmp_path / 'linked'}"


def test_valid_root_alias_and_external_config_keep_original_selection(context, mechanism, tmp_path):
    native, _ = mechanism
    parent = tmp_path / "parent"
    parent.mkdir()
    config = tmp_path / "external.ini"
    config.write_text("external\n")
    context = replace(context, root=str(parent / ".." / "source"),
                       test_framework=lb.TestFrameworkConfig(path=str(config)))
    value = lb.inspect_project(native.language, request(context))
    assert value.context is context and value.inputs.required == ("Target.fixture",)
    assert value.context.test_framework.path == str(config)


@pytest.mark.parametrize("error", [OSError("native raw output contains private-value"),
                                   subprocess.TimeoutExpired("private-value", 1), FileNotFoundError("private-value")])
def test_confirmed_failed_assessment_returns_typed_fact_without_native_output(context, mechanism, monkeypatch, error):
    native, _ = mechanism
    monkeypatch.setattr(native, "inspect_project", lambda self, requested: (_ for _ in ()).throw(error))
    value = lb.inspect_project(native.language, request(context))
    assert value.outcome in (lb.Outcome.INFRASTRUCTURE_FAILURE, lb.Outcome.TIMED_OUT, lb.Outcome.MISSING_SETUP)
    assert "private-value" not in json.dumps(value.safe_export()) and value.context is None


@pytest.mark.parametrize("marker", ["_langadapter_prepared_copy", "_langadapter_prepared_project",
                                    "_langadapter_owned_directories",
                                    "_langadapter_mutation_session", "_langadapter_linux_command", "_langadapter_command"])
def test_unresolved_native_ownership_propagates_exact_primary_not_a_value_only_report(context, mechanism, monkeypatch, marker):
    native, _ = mechanism
    primary = OSError("unresolved owner")
    owner = SimpleNamespace(_langadapter_command_disposed=False)
    setattr(primary, marker, owner)
    monkeypatch.setattr(native, "inspect_project", lambda self, requested: (_ for _ in ()).throw(primary))
    with pytest.raises(OSError) as caught:
        lb.inspect_project(native.language, request(context))
    assert caught.value is primary and getattr(caught.value, marker) is owner


def test_interruption_propagates_exact_primary(context, mechanism, monkeypatch):
    native, _ = mechanism
    primary = KeyboardInterrupt()
    monkeypatch.setattr(native, "verify_project", lambda self, requested: (_ for _ in ()).throw(primary))
    with pytest.raises(KeyboardInterrupt) as caught:
        lb.verify_project(native.language, request(context))
    assert caught.value is primary


def test_retained_owner_truth_value_is_not_release_authority(context, mechanism, monkeypatch):
    native, _ = mechanism

    class Owner:
        def __bool__(self):
            return False

    primary = OSError("retained workspace")
    primary._langadapter_prepared_copy = Owner()
    monkeypatch.setattr(native, "inspect_project", lambda self, requested: (_ for _ in ()).throw(primary))
    with pytest.raises(OSError) as caught:
        lb.inspect_project(native.language, request(context))
    assert caught.value is primary


def test_one_entry_deadline_includes_constructor_and_hook_and_reusable_request(context, mechanism, monkeypatch):
    native, calls = mechanism
    clock = [100.0]
    monkeypatch.setattr(setup.time, "monotonic", lambda: clock[0])
    original = native.__init__

    def slow(self, selected):
        original(self, selected)
        clock[0] += 10

    monkeypatch.setattr(native, "__init__", slow)
    value = lb.inspect_project(native.language, request(context, total_timeout_s=5))
    assert value.outcome is lb.Outcome.TIMED_OUT and len(calls) == 1


@pytest.mark.parametrize("scope", ["inspection", "verification"])
@pytest.mark.parametrize("runtime", [{}, lb.Interpreter(""), lb.Interpreter(12),
                                    lb.Interpreter(provisioning={"version": "3.10"})])
def test_returned_runtime_must_be_structurally_valid_without_native_paths(context, mechanism, monkeypatch, scope, runtime):
    native, _ = mechanism
    hook = "inspect_project" if scope == "inspection" else "verify_project"
    original = getattr(native, hook)

    def changed(self, selected):
        return replace(original(self, selected), context=replace(selected.context, interpreter=runtime))

    monkeypatch.setattr(native, hook, changed)
    with pytest.raises(lb.InvalidRequest, match="interpreter"):
        getattr(lb, hook)(native.language, request(context))


@pytest.mark.parametrize("scope", ["inspection", "verification"])
@pytest.mark.parametrize("selection", ["path", "provisioning"])
@pytest.mark.parametrize("changed", [None, lb.Interpreter("/other/runtime")])
def test_provider_cannot_replace_or_remove_an_explicit_runtime(context, mechanism, monkeypatch, scope, selection, changed):
    native, _ = mechanism
    if selection == "provisioning":
        context = replace(context, interpreter=lb.Interpreter(provisioning=lb.ProvisioningRequest("3.10")))
    hook = "inspect_project" if scope == "inspection" else "verify_project"

    def assessed(self, selected):
        return lb.ProjectSetupReport(scope, lb.Outcome.COMPLETED, "", replace(selected.context, interpreter=changed),
            selected.inputs, (), (lb.OperationCapability("tests.run_suite", True),))

    monkeypatch.setattr(native, hook, assessed)
    with pytest.raises(lb.InvalidRequest, match="explicitly selected"):
        getattr(lb, hook)(native.language, request(context))


@pytest.mark.parametrize("scope", ["inspection", "verification"])
@pytest.mark.parametrize("selection", ["missing", "provisioning", "absent"])
def test_matching_unobserved_runtime_or_explicit_absent_discovery_remains_permitted(context, mechanism, monkeypatch, scope, selection):
    native, _ = mechanism
    runtime = (lb.Interpreter(str(Path(context.root) / "missing-runtime")) if selection == "missing" else
               lb.Interpreter(provisioning=lb.ProvisioningRequest("3.10")) if selection == "provisioning" else None)
    context = replace(context, interpreter=runtime)
    hook = "inspect_project" if scope == "inspection" else "verify_project"
    selected_runtime = runtime or lb.Interpreter(str(Path(context.root) / "discovered-runtime"))

    def assessed(self, selected):
        return lb.ProjectSetupReport(scope, lb.Outcome.COMPLETED, "", replace(selected.context, interpreter=selected_runtime),
            selected.inputs, (lb.ProjectSetupItem("runtime", True, None, None),),
            (lb.OperationCapability("tests.run_suite", True),))

    monkeypatch.setattr(native, hook, assessed)
    report = getattr(lb, hook)(native.language, request(context))
    assert report.context.interpreter is selected_runtime
    assert report.operations[0].readiness is None and report.items[0].outcome is None


@pytest.mark.parametrize("alteration", ["scope", "environment", "execution", "root", "inputs", "operations", "not_report"])
def test_provider_cannot_replace_selected_execution_protected_inputs_or_assessment_scope(context, mechanism, monkeypatch, alteration):
    native, _ = mechanism
    original = native.inspect_project

    def changed(self, selected):
        value = original(self, selected)
        if alteration == "scope":
            return replace(value, scope="verification")
        if alteration == "environment":
            return replace(value, context=replace(context, environment=dict(context.environment)))
        if alteration == "execution":
            return replace(value, context=replace(context, execution=lb.LocalExecution(work_dir=context.root)))
        if alteration == "root":
            return replace(value, context=replace(context, root=context.root + "/another"))
        if alteration == "inputs":
            return replace(value, inputs=lb.ProjectInputSelection())
        if alteration == "operations":
            return replace(value, operations=())
        return {}

    monkeypatch.setattr(native, "inspect_project", changed)
    with pytest.raises(lb.InvalidRequest):
        lb.inspect_project(native.language, request(context))


def test_report_with_no_selected_context_retains_redaction_of_original_captured_input(context, mechanism, monkeypatch):
    native, _ = mechanism
    original = native.inspect_project

    def observed(self, selected):
        return replace(original(self, selected), context=None, reason="private-value")

    monkeypatch.setattr(native, "inspect_project", observed)
    value = lb.inspect_project(native.language, request(context))
    assert value.reason == "private-value" and value.safe_export()["reason"] == "[redacted]"


@pytest.mark.parametrize("scope", ["inspection", "verification"])
@pytest.mark.parametrize("selected_context", [True, False])
@pytest.mark.parametrize("container", [False, True])
def test_public_export_redacts_all_operation_metadata_and_authorization_with_or_without_selection(context, mechanism, monkeypatch, scope, selected_context, container):
    native, _ = mechanism
    if container:
        context = replace(context, root="/project", interpreter=lb.Interpreter("/runtime/python"),
            execution=lb.ContainerExecution("borrowed", context.root, helper_python="/control/python",
                control_environment={"ACCESS_TOKEN": "host-control-private"}), test_framework=lb.TestFrameworkConfig())

        def admitted(selected):
            value = replace(selected, inputs=lb.ProjectInputSelection(required=selected.source_files))
            object.__setattr__(value, "_deadline", selected._deadline)
            return value

        monkeypatch.setattr(_setup, "_container_inputs", admitted)
    text = "private-value host-control-private Authorization: Basic other-private-credential"

    def assessed(self, selected):
        return lb.ProjectSetupReport(scope, lb.Outcome.COMPLETED, text,
            selected.context if selected_context else None, selected.inputs,
            (lb.ProjectSetupItem("runtime", True, None, None, reason=text),),
            (lb.OperationCapability("tests.run_suite", True, reason=text,
                execution_kinds=(text,), evidence_granularity=(text,), resolution_methods=(text,)),))

    monkeypatch.setattr(native, "inspect_project" if scope == "inspection" else "verify_project", assessed)
    value = getattr(lb, "inspect_project" if scope == "inspection" else "verify_project")(native.language, request(context))
    output = json.dumps(value.safe_export())
    assert "private-value" not in output and "other-private-credential" not in output
    assert ("host-control-private" not in output) is container
    assert value.operations[0].execution_kinds == (text,) and value.reason == text


def test_prepared_copy_is_not_exported_as_original_setup_selection(context, tmp_path, mechanism):
    native, calls = mechanism
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with owner.use() as selected:
            with pytest.raises(lb.InvalidRequest, match="original project"):
                lb.inspect_project(native.language, request(selected))
    finally:
        owner.close()
    assert calls == []


def test_public_container_admission_uses_only_selected_container_control_and_paths(context, tmp_path, mechanism, monkeypatch):
    native, calls = mechanism
    execution = lb.ContainerExecution("fixture", str(tmp_path), repository_dir="/repo",
        helper_python="/control/python", control_environment={"PATH": "host-control"})
    context = replace(context, root="/repo", execution=execution, interpreter=lb.Interpreter("/runtime/python"))
    launches = []

    def run(argv, **options):
        launches.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

    monkeypatch.setattr("langadapter.execution.executor_for", lambda selected: SimpleNamespace(run=run, _to_container=lambda value: value))
    monkeypatch.setattr(_setup, "executor_for", lambda selected: SimpleNamespace(run=run, _to_container=lambda value: value))
    value = lb.inspect_project(native.language, request(context, total_timeout_s=9))
    assert value.context is context and calls[0][1].interpreter is None
    assert len(launches) == 1 and launches[0][0][:4] == ["/control/python", "-I", "-S", "-c"]
    assert launches[0][0][-3:] == ["/repo", '["Target.fixture", "framework.ini"]', '["/repo"]']
    assert 0 < launches[0][1]["timeout"] <= 9
    assert "env" not in launches[0][1]


def test_container_missing_control_is_missing_setup_without_host_fallback(context, tmp_path, mechanism, monkeypatch):
    native, calls = mechanism
    context = replace(context, root="/repo", execution=lb.ContainerExecution("fixture", str(tmp_path)),
                       interpreter=lb.Interpreter("/runtime/python"), test_framework=lb.TestFrameworkConfig())
    monkeypatch.setattr(_setup, "executor_for", lambda selected: pytest.fail("unsupervised command"))
    value = lb.inspect_project(native.language, request(context))
    assert value.outcome is lb.Outcome.MISSING_SETUP and calls == []
    # The missing helper is named, not reported as a missing project file.
    (item,) = value.items
    assert item.key == "guest_control" and item.reason == "no Python control helper is selected for the container"
    assert "execution.helper_python" in item.action


def test_a_refusing_container_helper_is_missing_setup_not_a_traceback(context, tmp_path, mechanism, monkeypatch):
    """A helper Python below 3.10 (the project's own interpreter, say)
    refuses: the report says the setup is missing, and no traceback
    reaches the caller."""
    from langadapter._linux_job import SupervisionUnavailable

    native, calls = mechanism
    execution = lb.ContainerExecution("fixture", str(tmp_path), repository_dir="/repo",
                                      helper_python="/runtime/python")
    context = replace(context, root="/repo", execution=execution,
                      interpreter=lb.Interpreter("/runtime/python"))

    def run(argv, **options):
        refused = SupervisionUnavailable("Supervision requires Linux and Python 3.10+")
        # As the container route raises it: with its command owner, resolved.
        refused._langadapter_linux_command = SimpleNamespace(_langadapter_command_disposed=True)
        raise refused

    monkeypatch.setattr(_setup, "executor_for", lambda selected: SimpleNamespace(run=run))
    value = lb.inspect_project(native.language, request(context))
    assert value.outcome is lb.Outcome.MISSING_SETUP and calls == []
    (item,) = value.items
    assert item.key == "guest_control" and "Python 3.10+" in item.reason


def test_a_helper_without_child_accounting_is_missing_setup_with_its_owner(
        context, tmp_path, mechanism, monkeypatch):
    """Without procfs child accounting the refusal cannot confirm the
    helper has no child, so its owner stays unresolved. The check reports
    the missing setup, keeps the owner on the report and raises nothing."""
    from langadapter._linux_job import SupervisionUnavailable

    native, calls = mechanism
    execution = lb.ContainerExecution("fixture", str(tmp_path), repository_dir="/repo",
                                      helper_python="/runtime/python")
    context = replace(context, root="/repo", execution=execution,
                      interpreter=lb.Interpreter("/runtime/python"))
    owner = SimpleNamespace(_langadapter_command_disposed=False)

    def run(argv, **options):
        refused = SupervisionUnavailable(
            "Supervisor failed: Supervision requires procfs child accounting")
        refused._langadapter_linux_command = owner
        raise refused

    monkeypatch.setattr(_setup, "executor_for", lambda selected: SimpleNamespace(run=run))
    value = lb.inspect_project(native.language, request(context))
    assert value.outcome is lb.Outcome.MISSING_SETUP and calls == []
    (item,) = value.items
    assert item.key == "guest_control" and "child accounting" in item.reason
    assert value._langadapter_linux_command is owner
    assert owner._langadapter_command_disposed is False
    assert "execution.helper_python" in item.action


@pytest.mark.parametrize("scope", ["inspection", "verification"])
def test_a_host_that_cannot_supervise_is_named_as_the_host(context, mechanism, monkeypatch, scope):
    """Without procfs child accounting the library's own supervisor refuses
    a native run's command: the report names the host, not a container's
    control helper or its selection."""
    from langadapter._linux_job import SupervisionUnavailable

    native, calls = mechanism

    def refuse(self, requested):
        raise SupervisionUnavailable(
            "Supervisor failed: Supervision requires procfs child accounting")

    monkeypatch.setattr(native, "inspect_project" if scope == "inspection" else "verify_project",
                        refuse)
    value = (lb.inspect_project if scope == "inspection" else lb.verify_project)(
        native.language, request(context))
    assert value.outcome is lb.Outcome.MISSING_SETUP
    (item,) = value.items
    assert item.key == "supervision"
    assert item.reason.startswith("this host cannot supervise the project's commands: ")
    assert "child accounting" in item.reason
    assert "container" not in item.reason + item.action
    assert "helper_python" not in item.action
    assert "Python 3.10" in item.action and "/proc/<pid>/task/<tid>/children" in item.action


@pytest.mark.parametrize("stdout,code,outcome", [("{\"ok\": false, \"error\": \"FileNotFoundError\"}", 0, lb.Outcome.MISSING_SETUP),
    ("{\"ok\": false, \"error\": \"ValueError\"}", 0, lb.Outcome.INFRASTRUCTURE_FAILURE),
    ("not json", 0, lb.Outcome.INFRASTRUCTURE_FAILURE), ("[]", 0, lb.Outcome.INFRASTRUCTURE_FAILURE),
    ("{}", 0, lb.Outcome.INFRASTRUCTURE_FAILURE), ('{"ok": true}', 1, lb.Outcome.INFRASTRUCTURE_FAILURE)])
def test_container_admission_unjudged_or_missing_inputs_never_reach_provider(context, tmp_path, mechanism, monkeypatch, stdout, code, outcome):
    native, calls = mechanism
    context = replace(context, root="/repo", execution=lb.ContainerExecution("fixture", str(tmp_path), helper_python="/control/python"),
                       interpreter=lb.Interpreter("/runtime/python"), test_framework=lb.TestFrameworkConfig())
    monkeypatch.setattr(_setup, "executor_for", lambda selected: SimpleNamespace(run=lambda *a, **kw: subprocess.CompletedProcess(a, code, stdout, "")))
    assert lb.inspect_project(native.language, request(context)).outcome is outcome
    assert calls == []


def test_java_verification_missing_jdk_metadata_is_not_readiness(context):
    value = lb.verify_project("java", request(context))
    assert value.outcome is lb.Outcome.MISSING_SETUP and value.context is None
    assert any(item.required and item.readiness is lb.Availability.MISSING_SETUP for item in value.items)
    assert all(item.outcome is None for item in value.items if item.key in {"jvm", "compiler", "native_execution"})
    assert all(item.readiness is not lb.Availability.AVAILABLE for item in value.operations)
