"""Prepared copies and leases of warmed forks, without native process launches."""
from dataclasses import replace
from pathlib import Path, PurePosixPath
import subprocess
import shutil
import time
from types import SimpleNamespace

import pytest

from langadapter._owned_directory import OwnershipRefused
from langadapter.context import (
    ContainerExecution, Interpreter, LocalExecution, ProjectContext,
)
from langadapter.errors import InvalidRequest
from langadapter.java._tools import Toolset
from langadapter.java import _warmed_fork as forks
from langadapter.java._warmed_fork import PreparedSetup
from langadapter.java._env import Jdk
from langadapter.java._build import JavaProject


@pytest.fixture
def built(tmp_path):
    root = tmp_path / "built"
    root.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    jdk = tmp_path / "selected-java"
    jdk.write_text("")
    for name in ("src", "tests", "target/classes", "target/test-classes",
                 ".warmed/classes", ".warmed/helper", ".warmed/tools"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "src" / "Source.java").write_text("source bytes")
    (root / "target/classes" / "Source.class").write_bytes(b"build output")
    (root / ".warmed/classes" / "Example.class").write_bytes(b"suite output")
    (root / ".warmed/helper" / "LangAdapterWarmed.class").write_bytes(b"helper output")
    tool = root / ".warmed/tools/launcher.jar"
    tool.write_bytes(b"selected engine")
    external = tmp_path / "dependency.jar"
    external.write_bytes(b"dependency")
    context = ProjectContext(str(root), Interpreter(str(jdk)), LocalExecution(str(work)),
                             interpreter_flags=("-ea",), environment={"selected": "value"})
    project = JavaProject(str(root), "maven", 8, (str(root / "src"),),
        (str(root / "tests"),), (str(root / "target/classes"),),
        (str(root / "target/test-classes"),),
        (str(root / "target/classes"), str(external)), test_jvm_args=("-Xmx64m",),
        test_system_properties={"native.setting": "relative/path"})
    prepared = PreparedSetup(project, Jdk(str(tmp_path / "jdk"), 21, "21.0.11"),
        Toolset("1.14.4", (str(tool),)), str(root / ".warmed/classes"),
        str(root / ".warmed/helper"), ("Example",))
    return context, prepared, root


def create(built):
    context, prepared, root = built
    return forks.prepare(context, prepared, built_root=root, total_deadline=time.monotonic() + 30)


def test_separate_copies_keep_identical_build_inputs_and_expire_actual_contexts(built):
    first, second = create(built), create(built)
    original_context, model, root = built
    contexts = []
    try:
        for fork in (first, second):
            with fork.use() as current:
                contexts.append(current.context)
                current.executor._check_context()
                assert current.prepared.project.root == current.context.root == str(fork.owner.root)
                assert current.prepared.jdk is model.jdk
                assert current.context.interpreter == original_context.interpreter
                assert current.context.environment == original_context.environment
                assert current.prepared.project.test_jvm_args == ("-Xmx64m",)
                assert current.prepared.project.test_classpath[-1] == model.project.test_classpath[-1]
                for name, expected in ((current.prepared.classes, b"suite output"),
                        (current.prepared.helper_classes, b"helper output"),
                        (current.prepared.tools.jars[0], b"selected engine")):
                    output_path = Path(name)
                    actual = next(output_path.iterdir()).read_bytes() if output_path.is_dir() else output_path.read_bytes()
                    assert actual == expected
                (Path(fork.owner.root) / "target/classes/Source.class").write_bytes(b"fork state")
                (Path(fork.owner.root) / ".warmed/classes/Example.class").write_bytes(b"fork suite state")
            with pytest.raises(OwnershipRefused):
                current.executor._check_context()
        assert first.owner.root != second.owner.root
        assert (root / "target/classes/Source.class").read_bytes() == b"build output"
        assert (root / ".warmed/classes/Example.class").read_bytes() == b"suite output"
    finally:
        first.close()
        second.close()
    assert not Path(first.owner.root).exists() and not Path(second.owner.root).exists()


def test_exception_retains_actual_owner_and_lease(built):
    fork = create(built)
    error = OwnershipRefused("actual command still writing")
    try:
        with pytest.raises(OwnershipRefused) as captured:
            with fork.use():
                raise error
        assert captured.value is error and error._langadapter_prepared_project is fork.owner
        assert Path(fork.owner.root).exists() and fork.owner._prepared_copy.lease.exists()
    finally:
        fork.close()


def test_caught_tracked_command_still_blocks_copy_release_until_same_owner_finishes(built):
    from langadapter import _execution_scope

    fork = create(built)
    command = SimpleNamespace()
    try:
        with pytest.raises(OwnershipRefused):
            with fork.use():
                _execution_scope._CURRENT.get().track(command, "unfinished owned command")
                try:
                    raise OSError("caught native failure")
                except OSError:
                    pass
        with pytest.raises(OwnershipRefused):
            fork.close()
        assert Path(fork.owner.root).exists() and fork.owner._prepared_copy.lease.exists()
        _execution_scope.completed(command)
    finally:
        fork.close()


def test_active_or_expired_public_context_cannot_become_an_idle_build_source(built):
    fork = create(built)
    try:
        with fork.use() as current:
            with pytest.raises(OwnershipRefused, match="idle unbound"):
                forks.prepare(current.context, current.prepared,
                    built_root=fork.owner._prepared_copy.root, total_deadline=time.monotonic() + 30)
        with pytest.raises(OwnershipRefused):
            forks.prepare(current.context, current.prepared,
                built_root=fork.owner._prepared_copy.root, total_deadline=time.monotonic() + 30)
    finally:
        fork.close()


@pytest.mark.parametrize("field", ["main_sources", "test_sources", "main_classes", "test_classes", "classes", "helper_classes"])
def test_writable_build_paths_outside_copy_refuse_before_allocation(built, monkeypatch, field):
    context, prepared, root = built
    outside = str(root.parent / "outside")
    if field in ("classes", "helper_classes"):
        prepared = replace(prepared, **{field: outside})
    else:
        prepared = replace(prepared, project=replace(prepared.project, **{field: (outside,)}))
    monkeypatch.setattr(forks, "prepare_project", lambda *a, **kw: pytest.fail("no allocation"))
    with pytest.raises(InvalidRequest, match="outside"):
        create((context, prepared, root))


@pytest.mark.parametrize("field", ["test_jvm_args", "test_system_properties", "interpreter_flags"])
def test_arbitrary_jvm_build_references_are_not_silently_rewritten(built, monkeypatch, field):
    context, prepared, root = built
    value = f"-Dembedded=prefix:{root}:suffix"
    if field == "interpreter_flags":
        context = replace(context, interpreter_flags=(value,))
    else:
        selected = {"native.setting": value} if field == "test_system_properties" else (value,)
        prepared = replace(prepared, project=replace(prepared.project, **{field: selected}))
    monkeypatch.setattr(forks, "prepare_project", lambda *a, **kw: pytest.fail("no allocation"))
    with pytest.raises(InvalidRequest, match="project-relative"):
        create((context, prepared, root))


def test_expired_copy_completion_retains_received_public_owner(built, monkeypatch):
    actual = forks.prepare_project
    owner = None
    calls = []

    def copy(context):
        nonlocal owner
        owner = actual(context)
        return owner

    def budget(*args):
        calls.append(args)
        if len(calls) == 2:
            raise subprocess.TimeoutExpired("copy completion", 0)

    monkeypatch.setattr(forks, "prepare_project", copy)
    monkeypatch.setattr(forks, "remaining", budget)
    try:
        with pytest.raises(subprocess.TimeoutExpired) as captured:
            create(built)
        assert owner is not None and captured.value._langadapter_prepared_project is owner
        assert Path(owner.root).exists()
    finally:
        owner.close()


def test_mapping_has_distinct_host_container_typed_paths_and_unchanged_external_tools(built):
    context, prepared, root = built
    execution = ContainerExecution("selected", str(root.parent), container_work_dir="/zone",
                                   helper_python="/control/python")
    context = replace(context, root="/zone/built", execution=execution)
    project = replace(prepared.project, root=context.root, main_sources=("/zone/built/src",),
        main_classes=("/zone/built/target/classes",), test_sources=("/zone/built/tests",),
        test_classes=("/zone/built/target/test-classes",),
        test_classpath=("/zone/built/target/classes", "/cache/native-dependency.jar"))
    prepared = replace(prepared, project=project,
                       tools=replace(prepared.tools, jars=("/zone/built/.warmed/tools/launcher.jar",)))
    target = root.parent / "copy"
    selected = replace(context, root="/zone/copy")
    result = forks._mapped(context, prepared, root, selected, target)
    assert result.project.main_sources == ("/zone/copy/src",)
    assert result.project.test_classpath == ("/zone/copy/target/classes", "/cache/native-dependency.jar")
    assert result.tools.jars == ("/zone/copy/.warmed/tools/launcher.jar",)
    assert result.classes == str(target / ".warmed/classes")
    assert result.jdk is prepared.jdk


def test_container_mapping_does_not_adopt_host_spelled_native_dependencies(built, monkeypatch):
    context, prepared, _ = built
    context = replace(context, root="/zone/built", execution=ContainerExecution(
        "selected", str(Path(context.root).parent), container_work_dir="/zone"))
    project = replace(prepared.project, root=context.root,
        main_sources=("/zone/built/src",), test_sources=("/zone/built/tests",),
        main_classes=("/zone/built/target/classes",), test_classes=("/zone/built/target/test-classes",),
        test_classpath=("/zone/built/target/classes", "/tmp/built/native-dependency.jar"))
    prepared = replace(prepared, project=project,
        classes="/tmp/built/.warmed/classes", helper_classes="/tmp/built/.warmed/helper",
        tools=replace(prepared.tools, jars=("/zone/built/launcher.jar", "/tmp/built/external.jar"),
                      overrides=("/tmp/built/override.jar",)))
    # Exercise the identical POSIX spelling on either host platform.
    monkeypatch.setattr(forks, "Path", PurePosixPath)
    result = forks._mapped(context, prepared, PurePosixPath("/tmp/built"),
                           replace(context, root="/zone/fork"), PurePosixPath("/tmp/fork"))
    assert result.project.test_classpath == ("/zone/fork/target/classes", "/tmp/built/native-dependency.jar")
    assert result.tools.jars == ("/zone/fork/launcher.jar", "/tmp/built/external.jar")
    assert result.tools.overrides == ("/tmp/built/override.jar",)
    assert result.classes == "/tmp/fork/.warmed/classes"
    assert result.helper_classes == "/tmp/fork/.warmed/helper"


@pytest.mark.parametrize("field", ["main_sources", "test_sources", "main_classes", "test_classes"])
def test_container_writable_native_fields_cannot_use_host_root_alias(built, monkeypatch, field):
    context, prepared, _ = built
    context = replace(context, root="/zone/built", execution=ContainerExecution(
        "selected", str(Path(context.root).parent), container_work_dir="/zone"))
    project = replace(prepared.project, root=context.root,
        main_sources=("/zone/built/src",), test_sources=("/zone/built/tests",),
        main_classes=("/zone/built/target/classes",), test_classes=("/zone/built/target/test-classes",))
    project = replace(project, **{field: ("/tmp/built/foreign-writable",)})
    prepared = replace(prepared, project=project,
                       classes="/tmp/built/.warmed/classes", helper_classes="/tmp/built/.warmed/helper")
    monkeypatch.setattr(forks, "Path", PurePosixPath)
    with pytest.raises(InvalidRequest, match="foreign-writable"):
        forks._mapped(context, prepared, PurePosixPath("/tmp/built"),
                      replace(context, root="/zone/fork"), PurePosixPath("/tmp/fork"))


def test_actual_container_copy_and_lease_keep_host_class_outputs_mapped(built, monkeypatch):
    from langadapter.execution import ContainerExecutor

    context, prepared, root = built
    execution = ContainerExecution("selected", str(root.parent), container_work_dir="/zone",
                                   repository_dir="/zone/built", helper_python="/control/python")
    context = replace(context, root="/zone/built", execution=execution,
                      interpreter=Interpreter("/jdk/bin/java"))
    project = replace(prepared.project, root=context.root,
        main_sources=("/zone/built/src",), test_sources=("/zone/built/tests",),
        main_classes=("/zone/built/target/classes",), test_classes=("/zone/built/target/test-classes",),
        test_classpath=("/zone/built/target/classes", "/cache/native.jar"))
    prepared = replace(prepared, project=project,
                       tools=replace(prepared.tools, jars=("/zone/built/.warmed/tools/launcher.jar",),
                                     overrides=("/zone/built/.warmed/tools/launcher.jar",)))
    calls = []

    def host(value):
        value = str(value)
        if value.startswith("/zone/"):
            return root.parent / value[len("/zone/"):]
        return Path(value)

    def run(executor, argv, **options):
        executor._check_context()
        calls.append(argv)
        if argv[0] == "test":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "find":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "cp":
            shutil.copytree(host(argv[-2]), host(argv[-1]))
        elif argv[0] == "cat":
            return subprocess.CompletedProcess(argv, 0, host(argv[-1]).read_text(), "")
        elif argv[0] == "rm":
            shutil.rmtree(host(argv[-1]))
        else:
            pytest.fail(f"unexpected native route: {argv}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ContainerExecutor, "run", run)
    fork = create((context, prepared, root))
    try:
        with fork.use() as current:
            current.executor._check_context()
            assert current.prepared.classes == str(fork.owner._prepared_copy.root / ".warmed/classes")
            assert current.prepared.project.root == current.context.root == fork.owner.root
            assert current.prepared.project.main_classes == (fork.owner.root + "/target/classes",)
            assert current.prepared.tools.overrides == (fork.owner.root + "/.warmed/tools/launcher.jar",)
            assert Path(current.prepared.classes).joinpath("Example.class").read_bytes() == b"suite output"
        with pytest.raises(OwnershipRefused):
            current.executor._check_context()
    finally:
        fork.close()
    assert len([argv for argv in calls if argv[0] == "cp"]) == 1
    assert not fork.owner._prepared_copy.root.exists()
    assert (root / "src/Source.java").read_text() == "source bytes"
