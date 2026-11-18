"""Only an active prepared copy grants cleanup authority, never an inventory."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter._owned_directory import OwnershipRefused
from langadapter._prepared_copy import CreatedFiles
from langadapter.python._runs import _SuiteRunner


@pytest.fixture(autouse=True)
def no_native_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Cleanup authority/inventory must not run native tools")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "input.txt").write_bytes(b"original")
    context = lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable))
    return root, context


@pytest.fixture
def prepared(project, tmp_path):
    owner = lb.prepare_project(project[1], parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        yield owner
    finally:
        owner.close()


@pytest.mark.parametrize("context", ["absent", "shared"])
def test_inventory_of_a_shared_tree_cannot_grant_cleanup_authority(project, context):
    root, selected = project
    foreign = root / "foreign.txt"
    foreign.write_text("preserve")
    records = CreatedFiles(selected if context == "shared" else None)

    with pytest.raises(OwnershipRefused, match="prepare_project"):
        records.begin(str(root))
    assert foreign.read_text() == "preserve" and (root / "input.txt").read_bytes() == b"original"


def test_public_suite_refuses_shared_cleanup_before_any_runner_setup(project, monkeypatch, tmp_path):
    adapter = lb.create("python", project[1])
    monkeypatch.setattr(adapter, "_executor", lambda: pytest.fail("No runner setup before ownership admission"))
    from langadapter.runs import SuiteRunRequest
    (tmp_path / "test_example.py").write_text("def test_example(): pass\n")
    with pytest.raises(OwnershipRefused, match="prepare_project"):
        _SuiteRunner(adapter, SuiteRunRequest(lb.Suite(str(tmp_path), ("test_example.py",)), 1,
                                            remove_created_files=True))


@pytest.mark.parametrize("borrow", [False, True])
def test_public_observation_may_inventory_after_its_own_commands_finish(prepared, tmp_path, monkeypatch, borrow):
    from contextlib import nullcontext
    from langadapter.python.adapter import PythonAdapter
    from langadapter.runs import RunCounts, SuiteRun, SuiteRunRequest

    calls = []

    def run(adapter, request):
        records = CreatedFiles(adapter.context)
        root = Path(adapter.context.root)
        assert records.begin(str(root)) == []
        output = root / "created.txt"
        output.write_text("output")
        records.end(str(root))
        assert records.begin(str(root)) == ["created.txt"]
        assert not output.exists() and (root / "input.txt").read_bytes() == b"original"
        calls.append(root)
        return SuiteRun(lb.Outcome.COMPLETED, "", RunCounts(collected=1, passed=1),
                        (), (), 0.1, "native result", None, adapter.tests._identity(request), returncode=0)

    monkeypatch.setattr(PythonAdapter, "run_suite", run)
    (tmp_path / "test_example.py").write_text("def test_example(): pass\n")
    with prepared.use() as context:
        adapter = lb.create("python", context)
        with prepared.worker(context) if borrow else nullcontext():
            result = adapter.tests.run_suite(SuiteRunRequest(lb.Suite(str(tmp_path), ("test_example.py",)), 1,
                                                           remove_created_files=True))
            assert result.outcome == lb.Outcome.COMPLETED and result.returncode == 0
    assert len(calls) == 1


@pytest.mark.parametrize("borrow", [False, True])
@pytest.mark.parametrize("pending", ["command", "session"])
def test_observation_inventory_still_refuses_actual_unfinished_resources(prepared, borrow, pending):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from langadapter import _execution_scope as life
    from langadapter.operations import _UnavailableSession

    with prepared.use() as context:
        with prepared.worker(context) if borrow else nullcontext():
            with life.operation(context):
                resource = (_UnavailableSession(lb.Outcome.UNSUPPORTED, "controlled", 1) if pending == "session"
                            else life.track(SimpleNamespace(), "pending command"))
                try:
                    with pytest.raises(OwnershipRefused):
                        CreatedFiles(context).begin(context.root)
                finally:
                    resource.close() if pending == "session" else life.completed(resource)


def test_cleanup_treats_file_names_as_literal_paths(prepared):
    with prepared.use() as context:
        root = Path(context.root)
        records = CreatedFiles(context)
        assert records.begin(str(root)) == []
        created = root / "left[1].txt"
        created.write_text("output")
        records.end(str(root))
        foreign = root / "left1.txt"
        foreign.write_text("preserve")
        assert records.begin(str(root)) == ["left[1].txt"]
        assert not created.exists() and foreign.read_text() == "preserve"
        assert (root / "input.txt").read_bytes() == b"original"


def test_cleanup_failure_is_explicit_and_same_owner_can_retry(prepared, monkeypatch):
    with prepared.use() as context:
        root = Path(context.root)
        records = CreatedFiles(context)
        records.begin(str(root))
        created = root / "created.txt"
        created.write_text("output")
        records.end(str(root))
        real_unlink = Path.unlink
        with monkeypatch.context() as patch:
            def refused(path, *args, **kwargs):
                if path == created:
                    raise PermissionError("cleanup refused")
                return real_unlink(path, *args, **kwargs)
            patch.setattr(Path, "unlink", refused)
            with pytest.raises(PermissionError, match="cleanup refused"):
                records.begin(str(root))
        assert created.exists()
        assert records.begin(str(root)) == ["created.txt"]
        assert not created.exists()


def test_new_file_inside_an_observed_output_directory_is_not_recursively_removed(prepared):
    with prepared.use() as context:
        root = Path(context.root)
        records = CreatedFiles(context)
        records.begin(str(root))
        output = root / "output"
        output.mkdir()
        (output / "run.txt").write_text("output")
        records.end(str(root))
        (output / "later.txt").write_text("later write")
        assert records.begin(str(root)) == ["output/run.txt"]
        assert (output / "later.txt").read_text() == "later write"


@pytest.mark.parametrize("directory", [False, True])
def test_output_inventory_refuses_links_without_touching_their_target(prepared, tmp_path, directory):
    outside = tmp_path / "outside"
    outside.mkdir()
    preserved = outside / "preserved.txt"
    preserved.write_bytes(b"outside input")
    with prepared.use() as context:
        root = Path(context.root)
        records = CreatedFiles(context)
        records.begin(context.root)
        link = root / "output_link"
        try:
            link.symlink_to(outside if directory else preserved, target_is_directory=directory)
        except OSError as error:
            pytest.skip(f"Host cannot create this link: {error}")
        try:
            with pytest.raises(OwnershipRefused, match="links"):
                records.end(context.root)
            assert preserved.read_bytes() == b"outside input"
        finally:
            link.unlink()
        records.end(context.root)
        assert records.begin(context.root) == []


def test_partial_removal_keeps_only_pending_outputs_for_retry(prepared, monkeypatch):
    with prepared.use() as context:
        root = Path(context.root)
        records = CreatedFiles(context)
        records.begin(context.root)
        for name in ("a.txt", "b.txt"):
            (root / name).write_bytes(b"disposable")
        records.end(context.root)
        unlink = Path.unlink
        with monkeypatch.context() as patch:
            def refused(path, *args, **kwargs):
                if path == root / "b.txt":
                    raise PermissionError("second output refused")
                return unlink(path, *args, **kwargs)
            patch.setattr(Path, "unlink", refused)
            with pytest.raises(PermissionError, match="second output"):
                records.begin(context.root)
        assert not (root / "a.txt").exists() and (root / "b.txt").is_file()
        (root / "a.txt").write_bytes(b"later caller write")
        assert records.begin(context.root) == ["b.txt"]
        assert (root / "a.txt").read_bytes() == b"later caller write"


def test_cleanup_refuses_another_root_or_dropped_binding(prepared, project):
    with prepared.use() as context:
        for selected, root in ((context, str(project[0])),
                               (replace(context, _prepared_copy_binding=None), context.root)):
            with pytest.raises(OwnershipRefused):
                CreatedFiles(selected).begin(root)


def test_prior_context_cannot_clean_during_a_later_use(prepared):
    with prepared.use() as context:
        records = CreatedFiles(context)
        records.begin(context.root)
        records.end(context.root)
    with prepared.use() as current:
        later = Path(current.root) / "later.txt"
        later.write_text("current use")
        with pytest.raises(OwnershipRefused):
            records.begin(context.root)
        assert later.read_text() == "current use"


def test_unresolved_command_refuses_inventory_before_any_removal(prepared):
    from langadapter import _execution_scope
    with prepared.use() as context:
        records = CreatedFiles(context)
        records.begin(context.root)
        output = Path(context.root) / "created.txt"
        output.write_text("output")
        records.end(context.root)
        command = type("OwnedCommand", (), {})()
        _execution_scope.track(command, "unresolved command")
        try:
            with pytest.raises(OwnershipRefused, match="unresolved"):
                records.begin(context.root)
            assert output.read_text() == "output"
        finally:
            _execution_scope.completed(command)
        assert records.begin(context.root) == ["created.txt"]


def test_snapshot_reuse_restores_modified_and_deleted_inputs(prepared, project):
    with prepared.use() as context:
        root = Path(context.root)
        records = CreatedFiles(context)
        records.begin(context.root)
        (root / "input.txt").unlink()
        (root / "created.txt").write_text("output")
        records.end(context.root)
    with prepared.use() as context:
        assert (Path(context.root) / "input.txt").read_bytes() == b"original"
        assert not (Path(context.root) / "created.txt").exists()
    assert (project[0] / "input.txt").read_bytes() == b"original"


def test_owned_run_selects_the_copy_without_rewriting_generated_setup(prepared, tmp_path):
    from langadapter.runs import SuiteRunRequest
    external = tmp_path / "delivered"
    external.mkdir()
    (external / "test_example.py").write_text("def test_example(): pass\n")
    with prepared.use() as context:
        adapter = lb.create("python", context)
        original_setup = adapter.prepare_suite(str(external))
        assert original_setup.outcome is lb.Outcome.COMPLETED
        setup = external / "conftest.py"
        original = setup.read_bytes()
        suite = lb.Suite(str(external), ("test_example.py",), ("conftest.py",))
        runner = _SuiteRunner(adapter, SuiteRunRequest(suite, 1, remove_created_files=True))
        scratch = tmp_path / "observation"
        scratch.mkdir()
        env, _, _ = runner._prepare(scratch)
        assert env["LANGADAPTER_MUTATION_SOURCE_ROOT"] == context.root
        assert setup.read_bytes() == original
        assert "LANGADAPTER_MUTATION_SOURCE_ROOT" in original.decode()
