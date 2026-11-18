"""Public preparation uses actual copied paths and per-use execution authority."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os
import stat
import sys
import threading

import pytest

import langadapter as lb
from langadapter import _process_tree
from langadapter._owned_directory import OwnershipRefused
from langadapter._prepared_copy import WorkspaceBusy
from langadapter.execution import executor_for
from langadapter.mutation import MutationCleanupError, MutationSession
from langadapter.runs import RunCounts, SuiteRun, SuiteRunRequest


@pytest.fixture(autouse=True)
def no_native(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Preparation source checks start no native process")

    monkeypatch.setattr(_process_tree.subprocess, "Popen", forbidden)
    monkeypatch.setattr(_process_tree.subprocess, "run", forbidden)


@pytest.fixture
def prepared(tmp_path):
    project = tmp_path / "source"
    project.mkdir()
    (project / "target.py").write_bytes(b"value = 1\n")
    (project / "pytest.ini").write_bytes(b"[pytest]\n")
    (project / "mvnw").write_bytes(b"project wrapper\n")
    (project / "fixture.txt").write_bytes(b"project resource\n")
    context = lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable),
                                test_framework=lb.TestFrameworkConfig(path="pytest.ini"))
    result = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    return result, context


def request(tmp_path):
    (tmp_path / "test_target.py").write_text("def test_target(): pass\n", encoding="ascii")
    return SuiteRunRequest(lb.Suite(str(tmp_path), ("test_target.py",)), timeout_s=1)


def test_public_use_restores_inputs_keeps_selected_tools_and_external_artifacts(prepared, tmp_path):
    owner, original = prepared
    contexts = []
    durable = tmp_path / "delivered.py"
    with owner.use() as context:
        contexts.append(context)
        assert context.root == owner.root != original.root
        assert context.interpreter is original.interpreter
        assert context.test_framework.path == str(Path(context.root) / "pytest.ini")
        assert (Path(context.root) / "mvnw").read_bytes() == b"project wrapper\n"
        assert (Path(context.root) / "fixture.txt").read_bytes() == b"project resource\n"
        (Path(context.root) / "target.py").write_bytes(b"changed")
        (Path(context.root) / "fixture.txt").unlink()
        durable.write_text("retained suite", encoding="ascii")
    with owner.use() as context:
        contexts.append(context)
        assert (Path(context.root) / "target.py").read_bytes() == b"value = 1\n"
        assert (Path(context.root) / "fixture.txt").read_bytes() == b"project resource\n"
        assert context != contexts[0] and len(set(contexts)) == 2
    owner.close()
    owner.close()
    assert not Path(owner.root).exists() and durable.read_text() == "retained suite"
    assert (Path(original.root) / "target.py").read_bytes() == b"value = 1\n"


def test_public_readonly_snapshot_repeated_use_and_close_preserve_original_modes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    original = source / "subject.py"
    original.write_bytes(b"value = 1\n")
    original.chmod(stat.S_IRUSR)
    mode = original.stat().st_mode
    owner = lb.prepare_project(lb.ProjectContext(str(source)), parent=str(tmp_path),
                               receipt_parent=str(tmp_path))
    try:
        for _ in range(2):
            with owner.use() as context:
                assert Path(context.root, "subject.py").read_bytes() == b"value = 1\n"
                assert original.stat().st_mode == mode
        owner.close()
        owner.close()
        assert not Path(owner.root).exists() and not owner._prepared_copy.baseline.root.exists()
        assert original.read_bytes() == b"value = 1\n" and original.stat().st_mode == mode
    finally:
        original.chmod(mode | stat.S_IWUSR)


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="POSIX directory modes, for a user other than root")
def test_a_read_only_source_dir_is_restored_and_removed(tmp_path):
    """Source directories without write, one inside the other, keep their modes
    in the copy and the snapshot, which each use still restores and close()
    still removes."""
    source = tmp_path / "source"
    data_dir = source / "data"
    nested = data_dir / "nested"
    nested.mkdir(parents=True)
    (data_dir / "table.txt").write_bytes(b"1\n")
    (nested / "row.txt").write_bytes(b"2\n")
    nested.chmod(0o555)
    data_dir.chmod(0o555)
    try:
        owner = lb.prepare_project(lb.ProjectContext(str(source)), parent=str(tmp_path),
                                   receipt_parent=str(tmp_path))
        for _ in range(2):
            with owner.use() as context:
                assert Path(context.root, "data", "table.txt").read_bytes() == b"1\n"
                assert Path(context.root, "data", "nested", "row.txt").read_bytes() == b"2\n"
        owner.close()
        assert not Path(owner.root).exists() and not owner._prepared_copy.baseline.root.exists()
        assert stat.S_IMODE(data_dir.stat().st_mode) == stat.S_IMODE(nested.stat().st_mode) == 0o555
    finally:
        data_dir.chmod(0o755)
        nested.chmod(0o755)


@pytest.mark.parametrize("language", ["python", "java"])
def test_public_builtin_groups_and_executor_refuse_obsolete_context_before_launch(prepared, tmp_path, language):
    owner, _ = prepared
    asked = request(tmp_path)
    with owner.use() as context:
        adapter = lb.create(language, context)
        copied = replace(context)
        executor = executor_for(context)
        if language == "python":
            assert adapter.sources.check_structure("value = 1").completed
    for active in (False, True):
        def refused():
            with pytest.raises(OwnershipRefused):
                lb.create(language, copied)
            with pytest.raises(OwnershipRefused):
                adapter.tests.run_suite(asked)
            with pytest.raises(OwnershipRefused):
                adapter.sources.validate_source("class Sample {}" if language == "java" else "value = 1")
            with pytest.raises(OwnershipRefused):
                adapter.mutations.operators("pit" if language == "java" else "cosmic_ray")
            with pytest.raises(OwnershipRefused):
                executor.run(["native"], timeout=1)

        if active:
            with owner.use():
                refused()
        else:
            refused()
    owner.close()


def test_a_context_built_on_the_input_snapshot_is_refused(prepared):
    """The snapshot every use restores from is no project of its own: runs in
    it would change the bytes that later uses restore."""
    owner, original = prepared
    snapshot = lb.ProjectContext(str(owner._prepared_copy.inputs), original.interpreter)
    with pytest.raises(OwnershipRefused, match="context yielded by use"):
        lb.create("python", snapshot)
    owner.close()


def test_context_cannot_drop_binding_change_root_or_execute_on_unregistered_thread(prepared):
    owner, original = prepared
    with owner.use() as context:
        for copied in (replace(context, _prepared_copy_binding=None),
                       replace(context, root=original.root),
                       lb.ProjectContext(context.root, original.interpreter)):
            with pytest.raises(OwnershipRefused):
                lb.create("python", copied)
        failures = []

        def execute_elsewhere():
            try:
                lb.create("python", context)
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=execute_elsewhere)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and len(failures) == 1
        assert isinstance(failures[0], OwnershipRefused)
        with pytest.raises(WorkspaceBusy):
            owner.close()
    owner.close()


def test_unrelated_registered_adapter_uses_same_public_execution_lease(prepared, tmp_path, monkeypatch):
    owner, _ = prepared
    asked = request(tmp_path)
    calls = []
    from langadapter import execution

    monkeypatch.setattr(execution, "run_capture", lambda argv, **options:
                        calls.append((argv, options)) or SimpleNamespace(returncode=0, stdout="", stderr=""))

    class Independent(lb.LangAdapter):
        language = "prepared-fixture"

        def run_suite(self, request):
            executor_for(self.context).run(["fixture"], cwd=self.context.root, timeout=1)
            return SuiteRun(lb.Outcome.COMPLETED, "", RunCounts(), (), (), None, "", None,
                            self.tests._identity(request))

    lb.register(Independent.language, [".prepared-fixture"], Independent)
    try:
        with owner.use() as context:
            adapter = lb.create(Independent.language, context)
            result = adapter.tests.run_suite(asked)
            assert result.outcome is lb.Outcome.COMPLETED and calls[0][1]["cwd"] == context.root
        with pytest.raises(OwnershipRefused):
            adapter.tests.run_suite(asked)
        with pytest.raises(OwnershipRefused):
            lb.create(Independent.language, context)
        assert len(calls) == 1
        owner.close()
    finally:
        lb.unregister(Independent.language)


def test_same_resource_recovery_then_public_close_is_retryable(prepared, monkeypatch):
    owner, _ = prepared
    command = SimpleNamespace(returncode=0)
    disposed = [False]
    monkeypatch.setattr(_process_tree, "_start", lambda *a, **kw: command)
    monkeypatch.setattr(_process_tree, "_dispose", lambda *a, **kw: disposed[0])
    with pytest.raises(OwnershipRefused) as caught:
        with owner.use():
            _process_tree.start(["controlled"])
            assert _process_tree.finish(command) is False
    assert caught.value._langadapter_prepared_project is owner
    with pytest.raises(OwnershipRefused):
        owner.close()
    assert Path(owner._prepared_copy.owned.receipt).is_file() and Path(owner.root).is_dir()
    disposed[0] = True
    assert _process_tree.finish(command) is True
    owner.close()
    assert not Path(owner.root).exists()


def test_returned_session_cannot_measure_outside_use_but_can_close_for_recovery(prepared):
    owner, _ = prepared

    class ControlledSession(MutationSession):
        def measure(self, request):
            pytest.fail("No mutation run was requested")

        def release(self):
            pass

    with pytest.raises(OwnershipRefused):
        with owner.use():
            session = ControlledSession()
    with pytest.raises(OwnershipRefused):
        session.run(None)
    session.close()
    owner.close()


def test_failed_disposal_retries_only_remaining_owned_root(prepared, monkeypatch):
    owner, _ = prepared
    from langadapter._owned_directory import OwnedDirectory

    remove = OwnedDirectory.remove
    remaining = owner._prepared_copy.baseline
    failed = [True]

    def dispose(directory):
        if directory is remaining and failed[0]:
            raise OSError("baseline removal failed")
        return remove(directory)

    monkeypatch.setattr(OwnedDirectory, "remove", dispose)
    with pytest.raises(OSError, match="baseline removal failed"):
        owner.close()
    assert not Path(owner.root).exists() and remaining.receipt.is_file()
    failed[0] = False
    owner.close()
    owner.close()
    assert not remaining.root.exists()


def test_original_project_environment_reference_is_not_silently_rebased(prepared):
    owner, original = prepared
    with pytest.raises(lb.InvalidRequest, match="original project"):
        lb.prepare_project(replace(original, environment={"PYTHONPATH": original.root}))
    owner.close()


def spelled(path, separator):
    """``path`` with ``separator`` between its parts."""
    return path.replace("\\", "/").replace("/", separator)


@pytest.mark.parametrize("separator", ["/", "\\"])
def test_a_sibling_whose_name_starts_with_the_projects_is_an_external_tool(
        prepared, tmp_path, separator):
    """A directory beside the project whose name begins with the project's
    (ROOT-jdk, ROOTs/bin, ROOT2, ROOT.old/lib) names an external tool: the
    copy is prepared and its commands get the value unchanged, alone or in a
    path list."""
    owner, original = prepared
    owner.close()
    root = spelled(original.root, separator)
    captured = {
        "JAVA_HOME": root + "-jdk",
        "VIRTUAL_ENV": root + "-env",
        "PATH": os.pathsep.join([root + "s" + separator + "bin", root + "-env" + separator + "bin"]),
        "LANGADAPTER_JAVA_TOOLS": root + "-cache" + separator + "java",
        "CLASSPATH": os.pathsep.join([root + "2", root + ".old" + separator + "lib"]),
    }
    copy = lb.prepare_project(
        replace(original, environment=lb.Environment(captured, inherit=False)),
        parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with copy.use() as context:
            assert dict(context.environment) == captured
    finally:
        copy.close()


@pytest.mark.parametrize("separator", ["/", "\\"])
@pytest.mark.parametrize("name, value", [
    ("JAVA_TOOL_OPTIONS", "-javaagent:{root}{s}agent.jar"),
    ("PYTHONPATH", "{root}{s}src"),
    ("CLASSPATH", "{root}{p}{other}"),
    ("PATH", "{other}{p}{root}"),
    ("VIRTUAL_ENV", "{root}{s}tool-env"),
    ("PATH", "{root}{s}tool-env{s}bin{p}{other}"),
    ("PYTEST_ADDOPTS", "--rootdir={root} -x"),
    ("JAVA_TOOL_OPTIONS", '-Dbasedir="{root}"'),
    ("SOURCES", "{root},{other}"),
])
def test_a_value_naming_the_project_or_a_path_inside_it_is_refused(
        prepared, tmp_path, separator, name, value):
    """The root itself or a path below it, alone, in a path list or in an
    option, followed by any delimiter, such as an environment created
    inside the project."""
    owner, original = prepared
    owner.close()
    value = value.format(root=spelled(original.root, separator), s=separator, p=os.pathsep,
                         other=spelled(str(tmp_path / "elsewhere"), separator))
    with pytest.raises(lb.InvalidRequest, match=f"{name} refers to the original project"):
        lb.prepare_project(replace(original, environment=lb.Environment({name: value}, inherit=False)),
                           parent=str(tmp_path), receipt_parent=str(tmp_path))


def test_the_shells_location_in_the_project_is_passed_on_unchanged(prepared, tmp_path):
    """PWD and OLDPWD name the starting shell's directories, not the project, so
    a check started inside the project prepares its copy, whose commands get
    them unchanged. Another variable naming the project is still refused."""
    owner, original = prepared
    owner.close()
    captured = {"PWD": original.root, "OLDPWD": original.root}
    copy = lb.prepare_project(
        replace(original, environment=lb.Environment(captured, inherit=False)),
        parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with copy.use() as context:
            assert dict(context.environment) == captured
            assert context.environment.inherit is False
    finally:
        copy.close()
    with pytest.raises(lb.InvalidRequest, match="HOME refers to the original project"):
        lb.prepare_project(replace(original, environment=lb.Environment(
            {**captured, "HOME": original.root}, inherit=False)),
            parent=str(tmp_path), receipt_parent=str(tmp_path))


@pytest.mark.parametrize("changed", ["interpreter", "execution", "interpreter_flags",
                                    "test_framework", "environment", "container"])
def test_copied_context_cannot_change_selected_execution_inputs(prepared, tmp_path, changed):
    owner, original = prepared
    with owner.use() as context:
        values = {
            "interpreter": lb.Interpreter(path="foreign-tool"),
            "execution": lb.LocalExecution(work_dir=str(tmp_path / "foreign")),
            "interpreter_flags": ("-I",),
            "test_framework": lb.TestFrameworkConfig(path=str(Path(original.root) / "pytest.ini")),
            "environment": {"PYTHONPATH": original.root},
            "container": lb.ContainerExecution("foreign", str(tmp_path)),
        }
        field = "execution" if changed == "container" else changed
        copied = replace(context, **{field: values[changed]})
        with pytest.raises(OwnershipRefused, match="changed its selected"):
            copied._require_execution()
        with pytest.raises(OwnershipRefused):
            executor_for(copied)
        executor_for(context)
    owner.close()


def test_prepared_mount_cannot_drop_context_binding_through_container_selection(prepared, tmp_path):
    owner, _ = prepared
    execution = lb.ContainerExecution("fixture", str(tmp_path), container_work_dir="/mapped")
    with owner.use():
        container_root = "/mapped/" + Path(owner.root).relative_to(tmp_path).as_posix()
        reconstructed = lb.ProjectContext(container_root, lb.Interpreter(path="/tool/python"),
                                          execution=execution)
        with pytest.raises(OwnershipRefused, match="context yielded"):
            executor_for(reconstructed)
    owner.close()


@pytest.mark.parametrize("language", ["python", "java"])
def test_group_lease_guard_precedes_even_a_native_method_override(prepared, tmp_path, monkeypatch, language):
    owner, original = prepared
    owner.close()
    owner = lb.prepare_project(replace(original, test_framework=lb.TestFrameworkConfig()),
                               parent=str(tmp_path), receipt_parent=str(tmp_path))
    asked = request(tmp_path)
    received = []
    with owner.use() as context:
        adapter = lb.create(language, context)
        result = adapter.tests._suite_failure(asked, lb.Outcome.COMPLETED, "fixture result")
        monkeypatch.setattr(adapter, "run_suite", lambda actual:
                            received.append(actual) or result)
        assert adapter.tests.run_suite(asked).outcome is lb.Outcome.COMPLETED
    with pytest.raises(OwnershipRefused):
        adapter.tests.run_suite(asked)
    assert received == [asked]
    owner.close()


def test_project_alias_cannot_reconstruct_an_unbound_prepared_context(prepared, tmp_path):
    owner, original = prepared
    link = tmp_path / "project-alias"
    try:
        link.symlink_to(owner.root, target_is_directory=True)
    except OSError as error:
        owner.close()
        pytest.skip(f"host does not permit directory symlinks: {error}")
    with owner.use():
        reconstructed = lb.ProjectContext(str(link), original.interpreter)
        with pytest.raises(OwnershipRefused, match="context yielded"):
            lb.create("python", reconstructed)
    link.unlink()
    owner.close()


def test_project_framework_path_is_normalized_before_mapping(prepared, tmp_path):
    owner, original = prepared
    selected = lb.prepare_project(replace(original, test_framework=lb.TestFrameworkConfig(
        path="../source/pytest.ini")), parent=str(tmp_path), receipt_parent=str(tmp_path))
    with selected.use() as context:
        assert context.test_framework.path == str(Path(context.root) / "pytest.ini")
        assert Path(context.test_framework.path).read_bytes() == b"[pytest]\n"
    selected.close()
    owner.close()


@pytest.mark.parametrize("absolute_config", [False, True])
def test_lexical_source_alias_maps_project_config_to_actual_copy(prepared, tmp_path, absolute_config):
    owner, original = prepared
    parent = tmp_path / "parent"
    parent.mkdir()
    source = Path(original.root)
    alias = parent / ".." / source.name
    config = str(source / "pytest.ini") if absolute_config else "pytest.ini"
    selected = lb.prepare_project(replace(original, root=str(alias),
        test_framework=lb.TestFrameworkConfig(path=config)),
        parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with selected.use() as context:
            copied = Path(context.root) / "pytest.ini"
            assert Path(context.test_framework.path) == copied
            copied.write_bytes(b"copied configuration\n")
            assert (source / "pytest.ini").read_bytes() == b"[pytest]\n"
    finally:
        selected.close()
        owner.close()


@pytest.mark.parametrize("environment_spelling", ["canonical", "selected_alias"])
def test_lexical_source_alias_refuses_original_root_environment_before_allocation(
    prepared, tmp_path, monkeypatch, environment_spelling,
):
    owner, original = prepared
    parent = tmp_path / "parent"
    parent.mkdir()
    alias = str(parent / ".." / Path(original.root).name)
    value = original.root if environment_spelling == "canonical" else alias
    from langadapter.preparation import PreparedCopy

    def forbidden(*args, **kwargs):
        pytest.fail("An original-root environment reference must fail before allocation")

    monkeypatch.setattr(PreparedCopy, "create", forbidden)
    with pytest.raises(lb.InvalidRequest, match="original project"):
        lb.prepare_project(replace(original, root=alias, environment={"PYTHONPATH": value}))
    owner.close()


def test_lexical_source_alias_preserves_external_config_and_tool_selection(prepared, tmp_path):
    owner, original = prepared
    parent = tmp_path / "parent"
    parent.mkdir()
    config = tmp_path / "external.ini"
    config.write_bytes(b"external config\n")
    selected = lb.prepare_project(replace(original,
        root=str(parent / ".." / Path(original.root).name),
        test_framework=lb.TestFrameworkConfig(path=str(config)),
        environment={"EXTERNAL_SETTING": str(config)}),
        parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with selected.use() as context:
            assert context.test_framework.path == str(config.resolve())
            assert context.environment == {"EXTERNAL_SETTING": str(config)}
            assert context.interpreter is original.interpreter
    finally:
        selected.close()
        owner.close()


def test_source_root_link_is_refused_before_canonicalization(prepared, tmp_path, monkeypatch):
    owner, original = prepared
    link = tmp_path / "linked-source"
    try:
        link.symlink_to(original.root, target_is_directory=True)
    except OSError as error:
        owner.close()
        pytest.skip(f"host does not permit directory symlinks: {error}")
    from langadapter.preparation import PreparedCopy

    def forbidden(*args, **kwargs):
        pytest.fail("Source links must fail before allocation")

    monkeypatch.setattr(PreparedCopy, "create", forbidden)
    try:
        with pytest.raises(OwnershipRefused, match="cannot contain links"):
            lb.prepare_project(replace(original, root=str(link)))
    finally:
        link.unlink()
        owner.close()


def test_same_owner_concurrent_close_refuses_without_stealing_disposal(prepared, monkeypatch):
    owner, _ = prepared
    from langadapter._owned_directory import OwnedDirectory

    remove = OwnedDirectory.remove
    entered = threading.Event()
    release = threading.Event()
    failures = []

    def blocked(directory):
        entered.set()
        assert release.wait(timeout=2)
        return remove(directory)

    def close_elsewhere():
        try:
            owner.close()
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(OwnedDirectory, "remove", blocked)
    thread = threading.Thread(target=close_elsewhere)
    thread.start()
    try:
        assert entered.wait(timeout=2)
        with pytest.raises(WorkspaceBusy):
            owner.close()
        assert Path(owner.root).is_dir()
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive() and not failures
    owner.close()


class _MutatingSession(MutationSession):
    """A session that mutated the copy's target and whose close fails until
    ``failing`` is reset, leaving the mutant in place, as a Cosmic Ray serial
    run does until it confirms its workers stopped."""

    def __init__(self, target):
        super().__init__()
        self.target, self.failing = target, True

    def measure(self, request):
        pytest.fail("No native mutation measurement requested")

    def release(self):
        if self.failing:
            raise OSError("workers not confirmed stopped")
        self.target.write_bytes(b"value = 1\n")


def test_a_failed_session_cleanup_refuses_ordinary_operations_until_it_closes(prepared, tmp_path,
                                                                             monkeypatch):
    """A run after a failed cleanup would measure the mutant the session left,
    so it refuses at once, naming the session to close. A loan, a child
    preparation, a child's use and suite staging refuse the same way. close()
    retries, and the use goes on once it succeeds."""
    owner, original = prepared
    owner.close()
    owner = lb.prepare_project(replace(original, test_framework=lb.TestFrameworkConfig()),
                               parent=str(tmp_path), receipt_parent=str(tmp_path))
    asked = request(tmp_path)
    measured = []
    with owner.use() as context:
        target = Path(context.root) / "target.py"
        adapter = lb.create("python", context)
        result = adapter.tests._suite_failure(asked, lb.Outcome.COMPLETED, "fixture result")
        monkeypatch.setattr(adapter, "run_suite", lambda actual:
                            measured.append(target.read_bytes()) or result)
        child = lb.prepare_project(original, parent=str(tmp_path), receipt_parent=str(tmp_path))
        session = _MutatingSession(target)
        target.write_bytes(b"value = 2\n")
        with pytest.raises(MutationCleanupError):
            session.close()
        with pytest.raises(OwnershipRefused, match="mutation session") as refused:
            adapter.tests.run_suite(asked)
        assert measured == []
        assert lb.recovery_owners(refused.value) == (session,)
        suite = tmp_path / "suite"
        suite.mkdir()
        (suite / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
        for refused_call in (
                lambda: owner.worker(context).__enter__(),
                lambda: lb.prepare_project(original, parent=str(tmp_path), receipt_parent=str(tmp_path)),
                lambda: child.use().__enter__(),
                lambda: owner.stage_suite(context, lb.Suite(str(suite), ("test_x.py",)))):
            with pytest.raises(OwnershipRefused, match="mutation session") as refused:
                refused_call()
            assert lb.recovery_owners(refused.value) == (session,)
        session.failing = False
        session.close()
        child.close()
        assert adapter.tests.run_suite(asked).outcome is lb.Outcome.COMPLETED
        assert measured == [b"value = 1\n"]
        owner.stage_suite(context, lb.Suite(str(suite), ("test_x.py",)))
    owner.close()
