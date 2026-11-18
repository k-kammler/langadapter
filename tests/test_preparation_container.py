"""Public mapped preparation with controlled container commands, no native tools."""
from dataclasses import replace
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _execution_scope, _linux_container
from langadapter._owned_directory import OwnershipRefused
from langadapter._prepared_copy import PreparedCopy
from langadapter.execution import ContainerExecutor, LocalExecutor, executor_for


@pytest.fixture
def container(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_bytes(b"value = 1\n")
    (source / "pytest.ini").write_bytes(b"[pytest]\n")
    (source / "mvnw").write_bytes(b"selected project wrapper\n")
    zone = tmp_path / "zone"
    zone.mkdir()
    context = lb.ProjectContext("/subject/project", lb.Interpreter(path="/subject/python"),
        execution=lb.ContainerExecution("borrowed", str(zone), repository_dir="/subject/project",
            container_work_dir="/mapped", helper_python="/control/python3"),
        test_framework=lb.TestFrameworkConfig(path="pytest.ini"),
        environment={"EXTERNAL_SETTING": "/external/config"})
    state = SimpleNamespace(calls=[], failure=None, resource=None, mismatch=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Mapped preparation source tests launch no native command")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    def host(path):
        path = PurePosixPath(path)
        if path == PurePosixPath(context.root):
            return source
        return zone / path.relative_to("/mapped")

    def run(executor, argv, *, helper_python, **options):
        assert helper_python == "/control/python3" and executor.container == "borrowed"
        args = [executor._to_container(str(arg)) for arg in argv]
        state.calls.append((args, options))
        resource = _execution_scope.track(SimpleNamespace(), "controlled container command")
        if state.failure == args[0]:
            state.resource = resource
            raise OSError("controlled container ownership remains unresolved")
        output = ""
        if args[0] == "test":
            assert args[1] == "-d" and host(args[2]).is_dir()
        elif args[0] == "find":
            assert args[1:3] == ["-P", context.root]
            output = "".join(str(path) + "\0" for path in source.rglob("*") if path.is_symlink())
        elif args[0] == "cp":
            assert args[1:4] == ["-R", "-P", "--"]
            shutil.copytree(source, host(args[-1]), symlinks=True)
        elif args[0] == "cat":
            output = "foreign" if state.mismatch else host(args[1]).read_text(encoding="ascii")
        elif args[0] == "rm":
            path = host(args[-1])
            assert args[1:4] == ["-rf", "--", args[-1]]
            assert path.resolve().is_relative_to(zone.resolve())
            assert path.name == "project" and path.parent.name.startswith("langadapter_project_")
            if path.exists():
                shutil.rmtree(path)
        else:
            pytest.fail(f"Unexpected controlled container command: {args}")
        _execution_scope.completed(resource)
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(_linux_container, "run", run)
    return SimpleNamespace(context=context, source=source, zone=zone, state=state,
                           tmp=tmp_path, host=host)


def test_container_preparation_uses_one_remaining_budget(container, monkeypatch):
    from langadapter import _prepared_copy, preparation

    state = SimpleNamespace(now=0.0)
    clock = SimpleNamespace(monotonic=lambda: state.now)
    monkeypatch.setattr(preparation, "time", clock)
    monkeypatch.setattr(_prepared_copy, "time", clock)
    original = _linux_container.run

    def observed(executor, argv, **options):
        assert options["timeout"] == 10 - state.now
        completed = original(executor, argv, **options)
        state.now += 1
        return completed

    monkeypatch.setattr(_linux_container, "run", observed)
    owner = lb.prepare_project(container.context, receipt_parent=str(container.tmp), timeout_s=10)
    assert [options["timeout"] for _, options in container.state.calls] == [10, 9, 8, 7]
    monkeypatch.setattr(_linux_container, "run", original)
    owner.close()


def test_container_receipt_deadline_retains_owner_before_copy(container, monkeypatch):
    from langadapter import _prepared_copy, preparation

    state = SimpleNamespace(now=0.0)
    clock = SimpleNamespace(monotonic=lambda: state.now)
    monkeypatch.setattr(preparation, "time", clock)
    monkeypatch.setattr(_prepared_copy, "time", clock)
    original = _linux_container.run

    def delayed(executor, argv, **options):
        completed = original(executor, argv, **options)
        if argv[0] == "cat":
            state.now = 11
        return completed

    monkeypatch.setattr(_linux_container, "run", delayed)
    with pytest.raises(subprocess.TimeoutExpired) as failed:
        lb.prepare_project(container.context, receipt_parent=str(container.tmp), timeout_s=10)
    assert not any(args[0] == "cp" for args, _ in container.state.calls)
    owner = failed.value._langadapter_prepared_project
    owner._prepared_copy.owned.verify()
    monkeypatch.setattr(_linux_container, "run", original)
    owner.close()


@pytest.mark.parametrize("absolute_config", [False, True])
def test_public_container_preparation_maps_inputs_and_restores_borrowed_project(container, absolute_config):
    config = "/subject/project/pytest.ini" if absolute_config else "pytest.ini"
    context = replace(container.context, test_framework=lb.TestFrameworkConfig(path=config))
    owner = lb.prepare_project(context, receipt_parent=str(container.tmp))
    receipt = Path(owner._prepared_copy.owned.receipt)
    records = []
    durable = container.tmp / "delivered.json"
    try:
        with owner.use() as selected:
            records.append(selected)
            assert selected.root == owner.root and selected.execution.repository_dir == selected.root
            assert selected.interpreter is context.interpreter
            assert selected.environment == context.environment
            assert selected.execution.helper_python == "/control/python3"
            assert selected.test_framework.path == selected.root + "/pytest.ini"
            root = container.host(selected.root)
            assert (root / "mvnw").read_bytes() == b"selected project wrapper\n"
            (root / "target.py").unlink()
            (root / "pytest.ini").write_bytes(b"changed config\n")
            durable.write_text(json.dumps({"measured": 0}), encoding="ascii")
        with owner.use() as selected:
            assert selected != records[0]
            assert (container.host(selected.root) / "target.py").read_bytes() == b"value = 1\n"
            assert (container.host(selected.root) / "pytest.ini").read_bytes() == b"[pytest]\n"
            with pytest.raises(OwnershipRefused):
                executor_for(records[0])
            with pytest.raises(OwnershipRefused):
                executor_for(replace(selected, _prepared_copy_binding=None))
        owner.close()
        owner.close()
        assert not container.host(owner.root).exists()
        assert not receipt.exists() and json.loads(durable.read_text()) == {"measured": 0}
        assert (container.source / "pytest.ini").read_bytes() == b"[pytest]\n"
        assert (container.source / "target.py").read_bytes() == b"value = 1\n"
        assert all(call[0][0] in {"test", "find", "cp", "cat", "rm"} for call in container.state.calls)
    finally:
        owner.close()


def test_public_mapped_container_stages_only_current_host_suite_inputs(container, monkeypatch):
    source = container.zone / "source"
    shutil.copytree(container.source, source)
    tests = source / "tests"
    tests.mkdir()
    test = tests / "test_target.py"
    test.write_bytes(b"at open")
    context = replace(container.context, root="/mapped/source", execution=replace(
        container.context.execution, repository_dir="/mapped/source"))
    run = _linux_container.run
    def selected(executor, argv, *, helper_python, **options):
        args = [executor._to_container(str(arg)) for arg in argv]
        if args[0] in ("test", "find", "cp"):
            assert helper_python == context.execution.helper_python
            assert executor.container == context.execution.container
            container.state.calls.append((args, options))
            if args[0] == "test":
                assert container.host(args[2]).is_dir()
            elif args[0] == "find":
                assert args[2] == context.root
            else:
                assert args[-2] == context.root + "/."
                shutil.copytree(source, container.host(args[-1]))
            return subprocess.CompletedProcess(args, 0, "", "")
        return run(executor, argv, helper_python=helper_python, **options)
    monkeypatch.setattr(_linux_container, "run", selected)
    owner = lb.prepare_project(context, receipt_parent=str(container.tmp))
    with owner.use() as copied:
        lease = owner._prepared_copy.lease.read_bytes()
        test.write_bytes(b"edited after open")
        staged = owner.stage_suite(copied, lb.Suite(str(tests), (test.name,)),
                                   protected_paths=(copied.root + "/target.py",))
        assert staged.files()[0].read_bytes() == b"edited after open"
        assert staged.files()[0] == container.host(copied.root) / "tests/test_target.py"
        assert owner._prepared_copy.lease.read_bytes() == lease
        assert (container.host(copied.root) / "target.py").read_bytes() == b"value = 1\n"
    owner.close()
    assert test.read_bytes() == b"edited after open"


@pytest.mark.parametrize("invalid", ["helper", "repository", "parent", "environment", "root"])
def test_public_container_preparation_refuses_ambiguous_setup_before_commands(container, invalid):
    context = container.context
    options = {}
    if invalid == "helper":
        context = replace(context, execution=replace(context.execution, helper_python=None))
    elif invalid == "repository":
        context = replace(context, execution=replace(context.execution, repository_dir="/different/project"))
    elif invalid == "parent":
        options["parent"] = str(container.tmp)
    elif invalid == "environment":
        context = replace(context, environment={"PYTHONPATH": context.root})
    else:
        context = replace(context, root="/subject/../subject/project")
    with pytest.raises(lb.InvalidRequest):
        lb.prepare_project(context, **options)
    assert container.state.calls == []


@pytest.mark.parametrize("value", [
    "/subject/project:/usr/bin", "/subject/project/src", "file:///subject/project/x",
    "-javaagent:/subject/project/a.jar", "--rootdir=/subject/project -x",
])
def test_a_container_value_naming_the_project_is_refused_before_any_command(container, value):
    """The container's root alone, in a ":" path list, as a URL or in an
    option, or a path below it, on any host."""
    inside = replace(container.context, environment={"SETTING": value})
    with pytest.raises(lb.InvalidRequest, match="SETTING refers to the original project"):
        lb.prepare_project(inside, receipt_parent=str(container.tmp))
    assert container.state.calls == []


@pytest.mark.parametrize("inside", ["receipts", "temporary directory"])
@pytest.mark.parametrize("holder", ["project", "companion"])
def test_a_dir_inside_a_project_in_the_work_dir_is_refused_before_any_command(
        container, monkeypatch, inside, holder):
    """A project in the mapped work directory, and a companion directory, lie on the
    host too, so the receipts' directory and the temporary directory must lie
    outside them, as on the host."""
    from langadapter import _owned_directory, _prepared_copy

    held = container.zone / holder
    inside_dir = held / "inside"
    inside_dir.mkdir(parents=True)
    receipts = container.tmp
    if inside == "receipts":
        receipts = inside_dir
    else:
        for module in (_prepared_copy, _owned_directory):
            monkeypatch.setattr(module, "_temporary_root", lambda: str(inside_dir))
    if holder == "project":
        context = replace(container.context, root="/mapped/project", execution=replace(
            container.context.execution, repository_dir="/mapped/project"))
        companions = ()
    else:
        context, companions = container.context, (str(held),)
    with pytest.raises(OwnershipRefused, match="workspace and receipt parents must be outside "
                                               "the source tree"):
        lb.prepare_project(context, receipt_parent=str(receipts), companions=companions)
    assert container.state.calls == [] and list(inside_dir.iterdir()) == []


@pytest.mark.parametrize("value", ["/.venv", "/opt/tool-env/bin:/usr/bin"])
def test_a_root_ending_in_a_slash_refuses_the_paths_below_it(container, value):
    """A root that ends in a slash, here the container's filesystem root and
    on a host a drive root such as P:/, is matched without it, so the first
    character of a path below it does not make that path another directory."""
    root = replace(container.context, root="/", execution=replace(
        container.context.execution, repository_dir="/"), environment={"SETTING": value})
    with pytest.raises(lb.InvalidRequest, match="SETTING refers to the original project"):
        lb.prepare_project(root, receipt_parent=str(container.tmp))
    assert container.state.calls == []


def test_a_root_ending_in_a_slash_admits_values_without_it(container, monkeypatch):
    """Values that do not hold the root's text pass the check for a root that
    ends in a slash: the preparation goes on to make the copy."""
    class Copying(Exception):
        pass

    def copy(*args, **kwargs):
        raise Copying
    monkeypatch.setattr(PreparedCopy, "create_in_container", copy)
    root = replace(container.context, root="/", execution=replace(
        container.context.execution, repository_dir="/"),
        environment={"EMPTY": "", "LIST": "a, b", "OPTION": '-Dx="y"'})
    with pytest.raises(Copying):
        lb.prepare_project(root, receipt_parent=str(container.tmp))


def test_a_container_sibling_or_longer_path_holding_the_root_is_prepared(container):
    """A directory whose name continues the root's (ROOT-jdk, ROOT-tools/bin) and
    a longer path that holds the root's text (/opt/ROOT/bin) are other directories."""
    outside = replace(container.context, environment={
        "PATH": "/subject/project-tools/bin:/opt/subject/project/bin:/usr/bin",
        "JAVA_HOME": "/subject/project-jdk"})
    owner = lb.prepare_project(outside, receipt_parent=str(container.tmp))
    owner.close()


def test_created_file_cleanup_uses_only_the_active_mapped_copy(container):
    from langadapter._prepared_copy import CreatedFiles

    owner = lb.prepare_project(container.context, receipt_parent=str(container.tmp))
    try:
        with owner.use() as context:
            records = CreatedFiles(context)
            root = container.host(context.root)
            assert records.begin(context.root) == []
            (root / "disposable.txt").write_bytes(b"completed output")
            records.end(context.root)
            (root / "later.txt").write_bytes(b"later caller write")
            with pytest.raises(OwnershipRefused, match="current prepared"):
                records.begin(container.context.root)
            assert records.begin(context.root) == ["disposable.txt"]
            assert (root / "later.txt").read_bytes() == b"later caller write"
            assert (container.source / "target.py").read_bytes() == b"value = 1\n"
        owner.close()
        assert not root.exists()
    finally:
        owner.close()


@pytest.mark.parametrize("failure_point", ["before_lease", "before_yield", "disposal"])
def test_container_failed_probe_or_disposal_keeps_same_resource_and_refuses_retry(container, monkeypatch, failure_point):
    owner = lb.prepare_project(container.context, receipt_parent=str(container.tmp))
    if failure_point == "before_yield":
        from langadapter._prepared_copy import PreparedCopy

        verify = PreparedCopy.verify
        calls = []

        def second_check(prepared_copy):
            calls.append(prepared_copy)
            if len(calls) == 2:
                container.state.failure = "cat"
            return verify(prepared_copy)

        monkeypatch.setattr(PreparedCopy, "verify", second_check)
    else:
        container.state.failure = "cat" if failure_point == "before_lease" else "rm"
    with pytest.raises(OSError, match="unresolved") as caught:
        if failure_point == "disposal":
            owner.close()
        else:
            with owner.use():
                pytest.fail("Failed setup cannot yield an executing context")
    assert caught.value._langadapter_prepared_project is owner
    assert container.state.resource is not None and Path(owner._prepared_copy.owned.receipt).is_file()
    command_count = len(container.state.calls)
    with pytest.raises(OwnershipRefused, match="unresolved resources"):
        owner.close()
    assert len(container.state.calls) == command_count and container.host(owner.root).exists()
    container.state.failure = None
    _execution_scope.completed(container.state.resource)
    owner.close()
    assert not container.host(owner.root).exists()


@pytest.mark.parametrize("changed", ["container", "runner", "helper_python", "local"])
def test_prepared_container_executor_cannot_change_execution_or_drop_supervision(container, changed):
    owner = lb.prepare_project(container.context, receipt_parent=str(container.tmp))
    try:
        with owner.use() as selected:
            executor = executor_for(selected)
            assert isinstance(executor, ContainerExecutor)
            if changed == "local":
                executor = LocalExecutor(selected)
            else:
                setattr(executor, changed, None if changed == "helper_python" else "foreign")
            calls = len(container.state.calls)
            with pytest.raises(OwnershipRefused, match="execution context"):
                executor.run(["native"], timeout=1)
            # Guard admission checks the receipt but must not run the requested native command.
            assert all(call[0][0] == "cat" for call in container.state.calls[calls:])
    finally:
        owner.close()


def test_public_container_child_uses_distinct_mapped_copy_and_restores_parent(container):
    parent = lb.prepare_project(container.context, receipt_parent=str(container.tmp))
    with parent.use() as context:
        outer = _execution_scope._CURRENT.get()
        child = lb.prepare_project(container.context, receipt_parent=str(container.tmp))
        with child.use() as copied:
            assert _execution_scope._CURRENT.get() is not outer
            assert copied.root != context.root
            executor_for(copied).run(["test", "-d", copied.root], timeout=1)
            (container.host(copied.root) / "target.py").write_bytes(b"child change\n")
        assert _execution_scope._CURRENT.get() is outer
        assert (container.host(context.root) / "target.py").read_bytes() == b"value = 1\n"
        child.close()
        assert not container.host(child.root).exists()
    parent.close()


def test_container_factory_failure_before_owner_keeps_child_command_dependency(container):
    parent = lb.prepare_project(container.context, receipt_parent=str(container.tmp))
    with parent.use():
        container.state.failure = "test"
        with pytest.raises(OSError) as caught:
            lb.prepare_project(container.context, receipt_parent=str(container.tmp))
        recovery = caught.value._langadapter_prepared_project
        command_count = len(container.state.calls)
        with pytest.raises(OwnershipRefused):
            recovery.close()
        assert len(container.state.calls) == command_count
        container.state.failure = None
        _execution_scope.completed(container.state.resource)
        recovery.close()
        recovery.close()
    parent.close()
