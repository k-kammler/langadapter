"""Explicit local membership uses the existing receipt-backed copy and reuse."""
import os
import io
import json
from pathlib import Path
import stat
import shutil
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter import _input_copy, _prepared_copy
from langadapter._prepared_copy import PreparedCopy, WorkspacePreparationFailed
from langadapter.context import ContainerExecution, ProjectContext
from langadapter.execution import executor_for
from langadapter.context import ProjectInputSelection


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name in ("src", ".venv", ".git", "build", "target"):
        (root / name).mkdir(parents=True)
        (root / name / "resource.txt").write_bytes((name + " declared\n").encode())
    return root


def _remove(prepared_copy):
    """Remove the copy and its baseline, as a prepared project's close does."""
    prepared_copy.owned.remove()
    prepared_copy.baseline.remove()


def test_explicit_selection_is_used_once_then_reuse_restores_only_selected_bytes(source, tmp_path):
    before = {file.relative_to(source).as_posix(): (file.read_bytes(), stat.S_IMODE(file.stat().st_mode))
              for file in source.rglob("*.txt")}
    value = ProjectInputSelection((".venv", ".git"), ("src/resource.txt", "build/resource.txt"))
    prepared_copy = PreparedCopy.create(str(source), parent=str(tmp_path),
                                         receipt_parent=str(tmp_path), inputs=value)
    try:
        assert prepared_copy.selection == value
        for _ in range(2):
            with prepared_copy.use() as root:
                assert not (root / ".venv").exists() and not (root / ".git").exists()
                assert (root / "build/resource.txt").read_bytes() == b"build declared\n"
                assert (root / "target/resource.txt").read_bytes() == b"target declared\n"
                (root / "src/resource.txt").unlink()
                (root / "unselected-output.txt").write_bytes(b"owned output")
        assert before == {file.relative_to(source).as_posix(): (file.read_bytes(), stat.S_IMODE(file.stat().st_mode))
                          for file in source.rglob("*.txt")}
    finally:
        _remove(prepared_copy)


def test_excluded_environment_is_not_inspected_or_followed(source, tmp_path, monkeypatch):
    ordinary, scandir = _prepared_copy._ordinary, os.scandir

    def inspect(path):
        if Path(path) == source / ".venv" or source / ".venv" in Path(path).parents:
            pytest.fail("excluded environment must not be inspected")
        return ordinary(path)

    def enumerate_(path):
        # Removal on Linux enumerates open directory descriptors, not paths.
        if not isinstance(path, int) and Path(path) == source / ".venv":
            pytest.fail("excluded environment must not be enumerated")
        return scandir(path)

    monkeypatch.setattr(_prepared_copy, "_ordinary", inspect)
    monkeypatch.setattr(_prepared_copy.os, "scandir", enumerate_)
    prepared_copy = PreparedCopy.create(str(source), parent=str(tmp_path),
        inputs=ProjectInputSelection((".venv",)))
    _remove(prepared_copy)


def test_missing_required_input_refuses_before_any_allocation(source, tmp_path, monkeypatch):
    monkeypatch.setattr(_prepared_copy.OwnedDirectory, "create",
                        lambda **kw: pytest.fail("required input must be present before allocation"))
    with pytest.raises(WorkspacePreparationFailed, match="before workspace allocation"):
        PreparedCopy.create(str(source), parent=str(tmp_path),
            inputs=ProjectInputSelection(required=("src/missing.py",)))


def test_selected_input_replacement_after_preflight_retains_original_partial_owner(source, tmp_path, monkeypatch):
    actual = _prepared_copy._copy_tree
    visits = []

    def copied(first, second, **kwargs):
        visits.append((first, second))
        if first == source:
            raise PermissionError("selected file changed after preflight")
        return actual(first, second, **kwargs)

    monkeypatch.setattr(_prepared_copy, "_copy_tree", copied)
    with pytest.raises(WorkspacePreparationFailed) as caught:
        PreparedCopy.create(str(source), parent=str(tmp_path),
            inputs=ProjectInputSelection((".venv",)))
    owner = caught.value._langadapter_prepared_copy
    assert owner.selection.exclude == (".venv",)
    assert owner.owned.receipt.exists() and owner.baseline.receipt.exists()
    # No operation/native writer ran, so only these receipt-verified allocations are removed.
    owner.owned.remove()
    owner.baseline.remove()
    assert (source / "src/resource.txt").read_bytes() == b"src declared\n"


@pytest.mark.parametrize("selected", [False, True])
def test_links_only_in_explicitly_excluded_entries_may_be_skipped(source, tmp_path, selected):
    external = tmp_path / "external"
    external.mkdir()
    (external / "kept.txt").write_bytes(b"unrelated")
    (source / ".venv/resource.txt").unlink()
    try:
        (source / ".venv/resource.txt").symlink_to(external / "kept.txt")
    except OSError:
        pytest.skip("host does not permit this test's explicit file symlink")
    if selected:
        with pytest.raises(WorkspacePreparationFailed, match="cannot be a link"):
            PreparedCopy.create(str(source), parent=str(tmp_path), inputs=ProjectInputSelection())
    else:
        owner = PreparedCopy.create(str(source), parent=str(tmp_path), inputs=ProjectInputSelection((".venv",)))
        _remove(owner)
    assert (external / "kept.txt").read_bytes() == b"unrelated"


@pytest.fixture
def container(source, tmp_path, monkeypatch):
    zone = tmp_path / "zone"
    zone.mkdir()
    context = ProjectContext("/source", execution=ContainerExecution("selected", str(zone),
        container_work_dir="/zone", helper_python="/control/python"))
    executor = executor_for(context)
    calls = []

    def host(value):
        if value == "/source":
            return str(source)
        if value.startswith("/zone/"):
            return str(zone / value[len("/zone/"):])
        return value

    def run(argv, **options):
        executor._check_context()
        calls.append((argv, options))
        if argv[0] == "test":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "/control/python":
            assert argv[1:4] == ["-I", "-S", "-c"] and "langadapter" not in argv[4]
            with monkeypatch.context() as selected:
                selected.setattr(sys, "argv", ["selected-copy", *[host(value) for value in argv[5:]]])
                selected.setattr(sys, "stdin", io.StringIO(options["input"]))
                try:
                    exec(compile(argv[4], "container-selected-copy", "exec"), {"__name__": "__main__"})
                except OSError as error:
                    return subprocess.CompletedProcess(argv, 1, "", str(error))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "cat":
            return subprocess.CompletedProcess(argv, 0, Path(host(argv[-1])).read_text(), "")
        if argv[0] == "rm":
            target = Path(host(argv[-1]))
            assert target == zone or zone in target.parents
            if target.exists():
                shutil.rmtree(target)
            return subprocess.CompletedProcess(argv, 0, "", "")
        pytest.fail(f"unexpected container route: {argv}")

    monkeypatch.setattr(executor, "run", run)
    return context, executor, calls


def test_container_uses_the_same_explicit_plan_and_standalone_selected_copy_program(container, source, tmp_path):
    context, executor, calls = container
    plan = ProjectInputSelection((".venv", ".git"), ("src/resource.txt", "build/resource.txt"))
    before = {file: file.read_bytes() for file in source.rglob("*.txt")}
    prepared_copy = PreparedCopy.create_in_container(context, executor, inputs=plan, receipt_parent=str(tmp_path))
    try:
        assert prepared_copy.selection == plan
        assert not (prepared_copy.root / ".venv").exists() and not (prepared_copy.inputs / ".git").exists()
        assert (prepared_copy.root / "build/resource.txt").read_bytes() == b"build declared\n"
        assert (prepared_copy.root / "target/resource.txt").read_bytes() == b"target declared\n"
        script_calls = [item for item in calls if item[0][0] == "/control/python"]
        assert [item[0][5] for item in script_calls] == ["inspect", "copy"]
        # Isolated, without site, outside the project: the default helper is
        # the subject's own interpreter.
        assert all(item[0][1:4] == ["-I", "-S", "-c"] and item[1]["cwd"] == "/"
                   for item in script_calls)
        assert all(json.loads(item[1]["input"]) == plan.to_dict() for item in script_calls)
        assert before == {file: file.read_bytes() for file in source.rglob("*.txt")}
    finally:
        prepared_copy.owned.remove()
        prepared_copy.baseline.remove()


def test_public_container_plan_protects_selected_framework_with_container_case_and_mapping(container, source, tmp_path, monkeypatch):
    from dataclasses import replace

    context, executor, calls = container
    (source / "Framework.ini").write_bytes(b"container configuration\n")
    context = replace(context, test_framework=lb.TestFrameworkConfig(path="Framework.ini"))
    monkeypatch.setattr("langadapter.execution.executor_for", lambda value: executor)
    plan = ProjectInputSelection((".venv", ".git", "framework.ini"), ("src/resource.txt",))
    owner = lb.prepare_project(context, receipt_parent=str(tmp_path), inputs=plan)
    try:
        assert owner.inputs.required == ("Framework.ini", "src/resource.txt")
        assert owner._context.test_framework.path == owner.root + "/Framework.ini"
        assert not (owner._prepared_copy.root / ".venv").exists()
        assert (owner._prepared_copy.root / "Framework.ini").read_bytes() == b"container configuration\n"
        script_calls = [item for item in calls if item[0][0] == "/control/python"]
        assert all(json.loads(item[1]["input"]) == owner.inputs.to_dict() for item in script_calls)
    finally:
        owner.close()
    assert not owner._prepared_copy.owned.root.exists() and not owner._prepared_copy.baseline.root.exists()


def test_public_container_framework_conflict_never_runs_container_setup(container, monkeypatch):
    from dataclasses import replace

    context, executor, calls = container
    context = replace(context, test_framework=lb.TestFrameworkConfig(path="src/resource.txt"))
    monkeypatch.setattr("langadapter.execution.executor_for", lambda value: executor)
    with pytest.raises(lb.InvalidRequest, match="required input"):
        lb.prepare_project(context, inputs=ProjectInputSelection(("src",)))
    assert calls == []


def test_container_preflight_missing_input_stops_before_allocations(container, monkeypatch):
    context, executor, calls = container
    monkeypatch.setattr(_prepared_copy.OwnedDirectory, "create",
                        lambda **kw: pytest.fail("container required input refuses before allocation"))
    with pytest.raises(_prepared_copy.OwnershipRefused, match="could not be read"):
        PreparedCopy.create_in_container(context, executor,
            inputs=ProjectInputSelection(required=("missing-source.java",)))
    assert [entry[0][0] for entry in calls] == ["test", "/control/python"]


def test_selected_container_cannot_run_with_an_implicit_target_or_host_python(container):
    context, executor, calls = container
    executor.helper_python = None
    with pytest.raises(_prepared_copy.OwnershipRefused, match=r"requires the helper Python \(execution\.helper_python\)"):
        PreparedCopy.create_in_container(context, executor, inputs=ProjectInputSelection())
    assert calls == []


def test_selected_copy_rechecks_changed_directory_at_enumeration(source, monkeypatch):
    ordinary = _input_copy.ordinary
    visits = 0

    def inspected(path):
        nonlocal visits
        if path == source / "build":
            visits += 1
            if visits == 2:
                raise OSError("build directory changed while selecting inputs")
        return ordinary(path)

    monkeypatch.setattr(_input_copy, "ordinary", inspected)
    with pytest.raises(OSError, match="changed"):
        _input_copy.tree(source, ProjectInputSelection().to_dict())
    assert visits == 2


def test_required_input_does_not_inspect_leaf_through_a_linked_parent(source, monkeypatch):
    ordinary = _input_copy.ordinary
    visited = []

    def inspected(path):
        visited.append(path)
        if path == source / "build":
            raise OSError("required input parent became a link")
        if path == source / "build/resource.txt":
            pytest.fail("required leaf must not be followed before ordinary parent admission")
        return ordinary(path)

    monkeypatch.setattr(_input_copy, "ordinary", inspected)
    with pytest.raises(OSError, match="parent became a link"):
        _input_copy.tree(source, ProjectInputSelection(required=("build/resource.txt",)).to_dict())
    assert source / "build/resource.txt" not in visited
