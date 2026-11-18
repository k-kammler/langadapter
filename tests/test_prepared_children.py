"""Explicit independent copies and retained parent/child recovery, no processes."""
from dataclasses import replace
from pathlib import Path
import sys
import threading

import pytest

import langadapter as lb
from langadapter import _execution_scope as life, _prepared_copy
from langadapter._owned_directory import OwnershipRefused
from langadapter.operations import _UnavailableSession


@pytest.fixture(autouse=True)
def no_native(monkeypatch):
    import subprocess
    def refuse(*args, **kwargs):
        pytest.fail("Independent child fixtures start no process")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "subject.py").write_text("original", encoding="ascii")
    context = lb.ProjectContext(str(source), lb.Interpreter(path=sys.executable))
    return context, tmp_path


def prepare(project):
    context, temporary = project
    return lb.prepare_project(context, parent=str(temporary), receipt_parent=str(temporary))


@pytest.mark.parametrize("language", ["python", "java", "prepared-child-fixture"])
def test_independent_child_preserves_parent_copy_and_idle_session(project, language):
    class Minimal(lb.LangAdapter):
        language = "prepared-child-fixture"
    if language == Minimal.language:
        lb.register(language, (".child-fixture",), Minimal)
    parent = prepare(project)
    try:
        with parent.use() as context:
            session = _UnavailableSession(lb.Outcome.UNSUPPORTED, "controlled persistent owner", 1)
            path = Path(context.root) / "subject.py"
            path.write_text("mutable parent", encoding="ascii")
            parent_scope = life._CURRENT.get()
            child = prepare(project)
            with child.use() as copied:
                assert life._CURRENT.get() is not parent_scope
                assert copied.root != context.root
                assert lb.create(language, copied).context is copied
                assert (Path(copied.root) / "subject.py").read_text() == "original"
                (Path(copied.root) / "subject.py").write_text("child changed", encoding="ascii")
                with pytest.raises(OwnershipRefused):
                    lb.create("python", context)
            assert life._CURRENT.get() is parent_scope
            assert path.read_text() == "mutable parent"
            child.close()
            child.close()
            assert not Path(child.root).exists()
            assert path.read_text() == "mutable parent"
            assert lb.create(language, context).context is context
            session.close()
        parent.close()
    finally:
        if language == Minimal.language:
            lb.unregister(language)


def test_prepared_child_created_before_parent_use_can_enter_explicitly(project):
    parent, child = prepare(project), prepare(project)
    with parent.use() as context:
        with child.use() as copied:
            assert lb.create("python", copied).context is copied
        child.close()
        assert lb.create("python", context).context is context
    parent.close()


@pytest.mark.parametrize("kind", ["mutable", "ancestor", "work-parent", "receipt-parent"])
def test_overlapping_sources_and_ownership_are_refused_before_copy(project, monkeypatch, kind):
    parent = prepare(project)
    with parent.use() as context:
        requested, temporary = project
        options = {"parent": str(temporary), "receipt_parent": str(temporary)}
        if kind == "mutable":
            requested = replace(context, _prepared_copy_binding=None)
        elif kind == "ancestor":
            requested = replace(requested, root=str(Path(context.root).parent.parent))
        elif kind == "work-parent":
            options["parent"] = context.root
        else:
            options["receipt_parent"] = context.root
        def no_copy(*args, **kwargs):
            pytest.fail("Overlap was not refused before allocation")
        with monkeypatch.context() as patch:
            patch.setattr(_prepared_copy.PreparedCopy, "create", no_copy)
            with pytest.raises(OwnershipRefused):
                lb.prepare_project(requested, **options)
        assert lb.create("python", context).context is context
    parent.close()


def test_raw_scope_cannot_authorize_an_independent_child(project):
    with life._Scope():
        with pytest.raises(OwnershipRefused, match="explicit prepared"):
            prepare(project)


def test_copied_host_alias_of_mutable_root_is_refused_before_allocation(project, monkeypatch):
    parent = prepare(project)
    with parent.use() as context:
        alias = Path(context.root).parent / "alias"
        try:
            alias.symlink_to(context.root, target_is_directory=True)
        except OSError:
            pytest.skip("Host does not permit directory symlinks")
        selected = replace(project[0], root=str(alias))
        def no_copy(*args, **kwargs):
            pytest.fail("Alias reached allocation")
        with monkeypatch.context() as patch:
            patch.setattr(_prepared_copy.PreparedCopy, "create", no_copy)
            with pytest.raises(OwnershipRefused):
                lb.prepare_project(selected, parent=str(project[1]), receipt_parent=str(project[1]))
        alias.unlink()
    parent.close()


def test_other_scope_cannot_adopt_a_retained_child(project):
    first, second = prepare(project), prepare(project)
    with pytest.raises(OwnershipRefused):
        with first.use():
            child = prepare(project)
    with second.use():
        with pytest.raises(OwnershipRefused, match="another containing"):
            with child.use():
                pytest.fail("Retained child changed parent")
    second.close()
    child.close()
    first.close()


def test_active_child_refuses_parent_worker_until_it_closes(project):
    parent = prepare(project)
    with parent.use() as context:
        child = prepare(project)
        failures = []
        with child.use():
            def borrow():
                try:
                    with parent.worker(context):
                        pytest.fail("worker entered while child was active")
                except BaseException as error:
                    failures.append(error)
            worker = threading.Thread(target=borrow)
            worker.start()
            worker.join(3)
            assert not worker.is_alive() and len(failures) == 1
            assert isinstance(failures[0], OwnershipRefused)
        with pytest.raises(OwnershipRefused):
            with parent.worker(context):
                pytest.fail("unclosed dependency was treated as disposed")
        child.close()
        with parent.worker(context):
            assert lb.create("python", context).context is context
    parent.close()


def test_child_pre_yield_validation_failure_retains_lease_and_owner(project, monkeypatch):
    parent = prepare(project)
    with parent.use():
        child = prepare(project)
        original = OSError("controlled context validation failed")
        validate = lb.ProjectContext.validate
        def validated(context):
            if context._prepared_copy_binding is not None and context.root == child.root:
                raise original
            return validate(context)
        with monkeypatch.context() as patch:
            patch.setattr(lb.ProjectContext, "validate", validated)
            with pytest.raises(OSError) as caught:
                with child.use():
                    pytest.fail("failed validation yielded context")
        assert caught.value is original
        assert original._langadapter_prepared_project is child
        assert child._prepared_copy.lease.exists()
        with pytest.raises(OwnershipRefused):
            parent._use_scope.require_quiescent()
        child.close()
    parent.close()


def test_child_failed_close_preserves_dependency_and_retries_same_owner(project, monkeypatch):
    from langadapter._owned_directory import OwnedDirectory
    parent = prepare(project)
    with parent.use():
        child = prepare(project)
        with child.use():
            pass
        remove = OwnedDirectory.remove
        original = OSError("controlled removal failed")
        def removed(directory):
            if directory is child._prepared_copy.baseline:
                raise original
            return remove(directory)
        with monkeypatch.context() as patch:
            patch.setattr(OwnedDirectory, "remove", removed)
            with pytest.raises(OSError) as caught:
                child.close()
        assert caught.value is original and original._langadapter_prepared_project is child
        assert child._prepared_copy.baseline.receipt.exists()
        with pytest.raises(OwnershipRefused):
            parent._use_scope.require_quiescent()
        child.close()
        child.close()
    parent.close()


def test_caught_child_failure_keeps_receipts_and_parent_dependency(project):
    parent = prepare(project)
    with parent.use() as context:
        child = prepare(project)
        original = RuntimeError("child body failed")
        with pytest.raises(RuntimeError) as caught:
            with child.use():
                raise original
        assert caught.value is original and caught.value._langadapter_prepared_project is child
        assert Path(child._prepared_copy.owned.receipt).is_file() and Path(parent._prepared_copy.owned.receipt).is_file()
        with pytest.raises(OwnershipRefused):
            lb.create("python", context)
        child.close()
        with pytest.raises(OwnershipRefused):
            lb.create("python", context)
    parent.close()


def test_unclosed_child_blocks_parent_release_until_actual_session_cleanup(project):
    parent = prepare(project)
    with pytest.raises(OwnershipRefused):
        with parent.use():
            child = prepare(project)
            with pytest.raises(OwnershipRefused):
                with child.use():
                    session = _UnavailableSession(lb.Outcome.UNSUPPORTED, "controlled", 1)
            with pytest.raises(OwnershipRefused):
                child.close()
            session.close()
            assert session._closed
            assert Path(child.root).exists()
            # Leaving the dependency open still refuses parent release.
    with pytest.raises(OwnershipRefused):
        parent.close()
    child.close()
    parent.close()


@pytest.mark.parametrize("stage", ["snapshot", "second-directory", "marker"])
def test_failed_factory_keeps_partial_ownership_and_retryable_cleanup(project, monkeypatch, stage):
    from langadapter._owned_directory import OwnedDirectory
    parent = prepare(project)
    with parent.use():
        original = OSError("controlled factory failure")
        with monkeypatch.context() as patch:
            if stage == "snapshot":
                patch.setattr(_prepared_copy, "_copy_tree", lambda *a, **k: (_ for _ in ()).throw(original))
            elif stage == "second-directory":
                create = OwnedDirectory.create.__func__
                def created(cls, **kwargs):
                    if kwargs.get("prefix") == "langadapter_inputs_":
                        raise original
                    return create(cls, **kwargs)
                patch.setattr(OwnedDirectory, "create", classmethod(created))
            else:
                opened = Path.open
                def opening(path, *args, **kwargs):
                    if path.name == ".langadapter-project-context":
                        raise original
                    return opened(path, *args, **kwargs)
                patch.setattr(Path, "open", opening)
            with pytest.raises(Exception) as caught:
                prepare(project)
        recovery = caught.value._langadapter_prepared_project
        with pytest.raises(OwnershipRefused):
            parent._use_scope.require_quiescent()
        if isinstance(recovery, lb.PreparedProject):
            assert Path(recovery._prepared_copy.owned.receipt).is_file()
            with pytest.raises(OwnershipRefused, match="Incomplete"):
                with recovery.use():
                    pytest.fail("partial factory became usable")
        recovery.close()
        recovery.close()
    parent.close()


def test_post_owner_factory_refusal_retains_reverse_recovery_links(project, monkeypatch):
    from langadapter import preparation
    parent = prepare(project)
    primary = OSError("post-owner verification refused")
    with parent.use():
        check = preparation._verify_independent
        def verify(prepared_copy, scope):
            if scope is not None:
                raise primary
            return check(prepared_copy, scope)
        with monkeypatch.context() as patch:
            patch.setattr(preparation, "_verify_independent", verify)
            with pytest.raises(OSError) as caught:
                prepare(project)
        owner = primary._langadapter_prepared_project
        assert caught.value is primary and owner._parent_scope is parent._use_scope
        assert owner._factory_scope is not None and owner._dependency is not None
        assert Path(owner._prepared_copy.owned.receipt).is_file()
        with pytest.raises(OwnershipRefused):
            parent._use_scope.require_quiescent()
        with pytest.raises(OwnershipRefused, match="Incomplete"):
            with owner.use():
                pytest.fail("Refused factory owner acquired execution authority")
        owner.close()
        owner.close()
        assert not Path(owner.root).exists()
    parent.close()


@pytest.mark.parametrize("selection", ["copy-parent", "receipt-parent", "baseline-source"])
def test_grandchild_refuses_every_ancestor_area_before_allocation(project, monkeypatch, selection):
    grandparent = prepare(project)
    with grandparent.use():
        parent = prepare(project)
        with parent.use():
            context, temporary = project
            options = {"parent": str(temporary), "receipt_parent": str(temporary)}
            if selection == "copy-parent":
                options["parent"] = grandparent.root
            elif selection == "receipt-parent":
                options["receipt_parent"] = grandparent.root
            else:
                context = replace(context, root=str(grandparent._prepared_copy.inputs))
            def no_copy(*args, **kwargs):
                pytest.fail("Ancestor overlap reached allocation")
            with monkeypatch.context() as patch:
                patch.setattr(_prepared_copy.PreparedCopy, "create", no_copy)
                with pytest.raises(OwnershipRefused):
                    lb.prepare_project(context, **options)
            sibling = prepare(project)
            with sibling.use() as copied:
                assert (Path(copied.root) / "subject.py").read_text() == "original"
            sibling.close()
        parent.close()
    grandparent.close()


def test_post_allocation_checks_all_ancestors_and_recovers_exact_refused_owner(project, monkeypatch):
    from langadapter._owned_directory import OwnedDirectory
    grandparent = prepare(project)
    with grandparent.use():
        parent = prepare(project)
        with parent.use():
            create = OwnedDirectory.create.__func__
            def allocated(cls, **options):
                if options.get("prefix") == "langadapter_project_":
                    options["parent"] = grandparent.root
                return create(cls, **options)
            with monkeypatch.context() as patch:
                patch.setattr(OwnedDirectory, "create", classmethod(allocated))
                with pytest.raises(OwnershipRefused, match="overlap") as caught:
                    prepare(project)
            owner = caught.value._langadapter_prepared_project
            assert Path(owner.root).is_relative_to(Path(grandparent.root))
            assert owner._dependency is not None and Path(owner._prepared_copy.owned.receipt).is_file()
            owner.close()
            owner.close()
            assert not Path(owner.root).exists()
            assert (Path(grandparent.root) / "subject.py").read_text() == "original"
        parent.close()
    grandparent.close()


@pytest.mark.parametrize("allocation", [1, 2])
@pytest.mark.parametrize("stage", ["receipt", "marker"])
def test_factory_creation_failure_retains_every_acquired_directory_and_parent(project, monkeypatch,
                                                                           allocation, stage):
    from langadapter import _owned_directory as module
    parent = prepare(project)
    primary = OSError("actual allocated directory creation failed")
    with parent.use():
        with monkeypatch.context() as patch:
            if stage == "receipt":
                create = module.tempfile.mkstemp
                calls = 0
                def created(**options):
                    nonlocal calls
                    calls += 1
                    if calls == allocation:
                        raise primary
                    return create(**options)
                patch.setattr(module.tempfile, "mkstemp", created)
            else:
                write = module._DirectoryCreation._write_acquired
                calls = 0
                def writing(path, identity, value):
                    nonlocal calls
                    if path.name == ".langadapter-owner":
                        calls += 1
                        if calls == allocation:
                            raise primary
                    return write(path, identity, value)
                patch.setattr(module._DirectoryCreation, "_write_acquired", staticmethod(writing))
            with pytest.raises(OSError) as caught:
                prepare(project)
        assert caught.value is primary
        recovery = primary._langadapter_prepared_project
        directories = primary._langadapter_owned_directories
        assert len(directories) == allocation and all(item.root.is_dir() for item in directories)
        assert recovery._dependency is not None
        with pytest.raises(OwnershipRefused):
            parent._use_scope.require_quiescent()
        recovery.close()
        recovery.close()
        assert all(not item.root.exists() for item in directories)
        parent._use_scope.require_idle()
    parent.close()
