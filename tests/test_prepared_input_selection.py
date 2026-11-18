"""Public preparation preserves one explicit plan and selected framework inputs."""
from pathlib import Path
import stat
import sys

import pytest

import langadapter as lb
from langadapter import _prepared_copy
from langadapter._owned_directory import OwnershipRefused


@pytest.fixture
def context(tmp_path):
    source = tmp_path / "source"
    for directory in ("src", ".venv", ".git", "build", "target"):
        (source / directory).mkdir(parents=True)
        (source / directory / "resource.txt").write_bytes((directory + " input\n").encode())
    (source / "pytest.ini").write_bytes(b"[pytest]\n")
    return lb.ProjectContext(str(source), lb.Interpreter(sys.executable),
                             test_framework=lb.TestFrameworkConfig(path="pytest.ini"))


@pytest.mark.parametrize("language", ["python", "java"])
def test_actual_public_context_reuse_preserves_plan_inputs_original_modes_and_tools(context, tmp_path, language):
    source = Path(context.root)
    before = {file.relative_to(source).as_posix(): (file.read_bytes(), stat.S_IMODE(file.stat().st_mode))
              for file in source.rglob("*") if file.is_file()}
    plan = lb.ProjectInputSelection((".venv", ".git"), ("src/resource.txt", "build/resource.txt"))
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path), inputs=plan)
    try:
        assert owner.inputs == plan.effective(("pytest.ini",))
        assert owner.inputs.required == ("build/resource.txt", "pytest.ini", "src/resource.txt")
        assert plan.required == ("build/resource.txt", "src/resource.txt")
        for _ in range(2):
            with owner.use() as selected:
                adapter = lb.create(language, selected)
                assert adapter.context is selected and selected.interpreter is context.interpreter
                assert not (Path(selected.root) / ".venv").exists()
                assert not (Path(selected.root) / ".git").exists()
                assert (Path(selected.root) / "target/resource.txt").read_bytes() == b"target input\n"
                assert Path(selected.test_framework.path).read_bytes() == b"[pytest]\n"
                (Path(selected.root) / "src/resource.txt").unlink()
        assert before == {file.relative_to(source).as_posix(): (file.read_bytes(), stat.S_IMODE(file.stat().st_mode))
                          for file in source.rglob("*") if file.is_file()}
    finally:
        owner.close()
    assert not Path(owner.root).exists()


@pytest.mark.parametrize("absolute", [False, True])
def test_excluding_selected_framework_refuses_before_allocation(context, tmp_path, monkeypatch, absolute):
    from dataclasses import replace

    if absolute:
        context = replace(context, test_framework=lb.TestFrameworkConfig(path=str(Path(context.root) / "pytest.ini")))
    monkeypatch.setattr(_prepared_copy.OwnedDirectory, "create", lambda **kw: pytest.fail("allocation before conflict refusal"))
    with pytest.raises(lb.InvalidRequest, match="required input: pytest.ini"):
        lb.prepare_project(context, parent=str(tmp_path), inputs=lb.ProjectInputSelection(("pytest.ini",)))


def test_external_framework_and_tool_paths_are_not_rebased_or_added_to_inputs(context, tmp_path):
    from dataclasses import replace

    config = tmp_path / "external.ini"
    config.write_bytes(b"external framework\n")
    context = replace(context, test_framework=lb.TestFrameworkConfig(path=str(config)))
    plan = lb.ProjectInputSelection((".venv", "pytest.ini"))
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path), inputs=plan)
    try:
        assert owner.inputs == plan
        with owner.use() as selected:
            assert selected.interpreter is context.interpreter
            assert selected.test_framework.path == str(config)
            assert not (Path(selected.root) / "pytest.ini").exists()
    finally:
        owner.close()
    assert config.read_bytes() == b"external framework\n"


@pytest.mark.parametrize("inputs", [{}, (), "src", False])
def test_untyped_public_plan_is_refused_before_allocation(context, tmp_path, monkeypatch, inputs):
    monkeypatch.setattr(_prepared_copy.OwnedDirectory, "create", lambda **kw: pytest.fail("untyped allocation"))
    with pytest.raises(lb.InvalidRequest, match="ProjectInputSelection"):
        lb.prepare_project(context, parent=str(tmp_path), inputs=inputs)


def test_default_public_copy_keeps_whole_tree_without_claiming_explicit_membership(context, tmp_path):
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        assert owner.inputs is None
        with owner.use() as selected:
            assert (Path(selected.root) / ".venv/resource.txt").read_bytes() == b".venv input\n"
    finally:
        owner.close()


def test_selected_missing_framework_is_not_silently_excluded(context, tmp_path, monkeypatch):
    (Path(context.root) / "pytest.ini").unlink()
    monkeypatch.setattr(_prepared_copy.OwnedDirectory, "create", lambda **kw: pytest.fail("missing selected framework allocation"))
    with pytest.raises(_prepared_copy.WorkspacePreparationFailed, match="before workspace allocation"):
        lb.prepare_project(context, parent=str(tmp_path), inputs=lb.ProjectInputSelection())


@pytest.mark.parametrize("absolute", [False, True])
def test_aliased_source_and_selected_framework_share_one_effective_identity(context, tmp_path, absolute):
    from dataclasses import replace

    parent = tmp_path / "parent"
    parent.mkdir()
    aliased = parent / ".." / "source"
    path = str(aliased / "pytest.ini") if absolute else "pytest.ini"
    context = replace(context, root=str(aliased), test_framework=lb.TestFrameworkConfig(path=path))
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path),
                               inputs=lb.ProjectInputSelection((".venv",)))
    try:
        assert owner.inputs.required == ("pytest.ini",)
        with owner.use() as selected:
            assert Path(selected.test_framework.path) == Path(selected.root) / "pytest.ini"
            assert Path(selected.test_framework.path).read_bytes() == b"[pytest]\n"
    finally:
        owner.close()


def test_registered_provider_receives_selected_inputs_under_the_same_public_lease(context, tmp_path, monkeypatch):
    from langadapter import execution
    from langadapter.runs import RunCounts, SuiteRun, SuiteRunRequest
    import subprocess

    calls = []
    monkeypatch.setattr(execution, "run_capture", lambda argv, **options:
        calls.append((argv, options)) or subprocess.CompletedProcess(argv, 0, "", ""))

    class Independent(lb.LangAdapter):
        language = "selected-input-fixture"

        def run_suite(self, request):
            assert (Path(self.context.root) / "src/resource.txt").read_bytes() == b"src input\n"
            assert not (Path(self.context.root) / ".venv").exists()
            execution.executor_for(self.context).run(["controlled-fixture"], cwd=self.context.root, timeout=1)
            return SuiteRun(lb.Outcome.COMPLETED, "", RunCounts(), (), (), None, "", None,
                            self.tests._identity(request))

    lb.register(Independent.language, (".selected-input-fixture",), Independent)
    owner = None
    try:
        owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path),
                                   inputs=lb.ProjectInputSelection((".venv",)))
        with owner.use() as selected:
            adapter = lb.create(Independent.language, selected)
            request = SuiteRunRequest(lb.Suite(selected.root, ("src/resource.txt",)), timeout_s=1)
            assert adapter.tests.run_suite(request).outcome is lb.Outcome.COMPLETED
        with pytest.raises(OwnershipRefused):
            adapter.tests.run_suite(request)
        assert len(calls) == 1
    finally:
        if owner is not None:
            owner.close()
        lb.unregister(Independent.language)


def test_inproject_linked_framework_cannot_be_treated_as_excluded_external_config(context, tmp_path):
    path = Path(context.root) / "pytest.ini"
    external = tmp_path / "external.ini"
    external.write_bytes(b"external\n")
    path.unlink()
    try:
        path.symlink_to(external)
    except OSError:
        pytest.skip("host does not permit this test's selected configuration symlink")
    with pytest.raises(lb.InvalidRequest, match="required input"):
        lb.prepare_project(context, parent=str(tmp_path), inputs=lb.ProjectInputSelection(("pytest.ini",)))
    # The refusal's class (_input_copy.LinkRefused) names the link in the message.
    with pytest.raises(_prepared_copy.WorkspacePreparationFailed,
                       match="^project input preflight failed before workspace allocation: "
                             "LinkRefused: selected project input cannot be a link: "):
        lb.prepare_project(context, parent=str(tmp_path), inputs=lb.ProjectInputSelection())
    assert external.read_bytes() == b"external\n"


def test_public_selected_copy_failure_preserves_effective_owner_and_same_owner_recovery(context, tmp_path, monkeypatch):
    original = _prepared_copy._copy_tree
    primary = PermissionError("copy was interrupted")

    def failed(source, target, **options):
        raise primary

    monkeypatch.setattr(_prepared_copy, "_copy_tree", failed)
    with pytest.raises(_prepared_copy.WorkspacePreparationFailed) as caught:
        lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path),
                           inputs=lb.ProjectInputSelection((".venv",)))
    owner = caught.value._langadapter_prepared_project
    assert caught.value.__cause__ is primary
    assert owner.inputs.required == ("pytest.ini",)
    assert owner._prepared_copy.owned.receipt.exists() and owner._prepared_copy.baseline.receipt.exists()
    with pytest.raises(OwnershipRefused):
        with owner.use():
            pytest.fail("incomplete allocation is not executable")
    monkeypatch.setattr(_prepared_copy, "_copy_tree", original)
    owner.close()
    owner.close()
    assert not owner._prepared_copy.owned.root.exists() and not owner._prepared_copy.baseline.root.exists()
