"""Current explicitly selected suite inputs within one owned use, no processes."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter import _execution_scope as life
from langadapter._owned_directory import OwnershipRefused
from langadapter.operations import _UnavailableSession


@pytest.fixture(autouse=True)
def no_native(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("Suite materialization starts no native process")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "subject.py").write_bytes(b"stable target")
    (root / "test_subject.py").write_bytes(b"first tests")
    (root / "setup.txt").write_bytes(b"first setup")
    context = lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable))
    return root, lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))


def test_selected_edits_new_files_and_removed_setup_preserve_snapshot_and_idle_pool(project):
    original, owner = project
    suite = lb.Suite(str(original), ("test_subject.py",), ("setup.txt",))
    with owner.use() as context:
        session = _UnavailableSession(lb.Outcome.UNSUPPORTED, "controlled idle registered pool", 1)
        lease = owner._prepared_copy.lease.read_bytes()
        copied = Path(context.root)
        protected = (str(copied / "subject.py"),)
        original.joinpath("subject.py").write_bytes(b"do not refresh this target")
        for number in range(2):
            original.joinpath("test_subject.py").write_bytes(f"tests {number}".encode())
            original.joinpath("setup.txt").write_bytes(f"setup {number}".encode())
            staged = owner.stage_suite(context, suite, protected_paths=protected)
            assert tuple(path.read_bytes() for path in staged.files()) == (
                f"tests {number}".encode(), f"setup {number}".encode())
            assert staged.identity() == suite.identity()
            assert copied.joinpath("subject.py").read_bytes() == b"stable target"
            assert owner._prepared_copy.inputs.joinpath("test_subject.py").read_bytes() == b"first tests"
            assert not session._closed and owner._prepared_copy.lease.read_bytes() == lease
        original.joinpath("new").mkdir()
        original.joinpath("new/test_new.py").write_bytes(b"new tests")
        original.joinpath("setup.txt").unlink()
        staged = owner.stage_suite(context, lb.Suite(str(original), ("new/test_new.py",)),
                                   protected_paths=protected, absent_files=("setup.txt",))
        assert staged.files()[0].read_bytes() == b"new tests" and staged.identity() == (
            lb.Suite(str(original), ("new/test_new.py",)).identity())
        assert not copied.joinpath("setup.txt").exists()
        session.close()
    owner.close()


def test_external_suite_stays_external_without_writing_the_copy(project, tmp_path):
    _, owner = project
    root = tmp_path / "external"
    root.mkdir()
    path = root / "test.py"
    path.write_bytes(b"external")
    with owner.use() as context:
        before = sorted(Path(context.root).rglob("*"))
        staged = owner.stage_suite(context, lb.Suite(str(root), (path.name,)))
        assert staged.root == str(root) and staged.files() == (path,)
        assert sorted(Path(context.root).rglob("*")) == before
    owner.close()


@pytest.mark.parametrize("kind", ["busy", "unresolved", "protected", "copied-context", "pending-command"])
def test_staging_refuses_without_changing_copy_and_retains_same_owner(project, kind):
    original, owner = project
    session = None
    resource = None
    with owner.use() as context:
        copied = Path(context.root) / "test_subject.py"
        original.joinpath("test_subject.py").write_bytes(b"edited")
        asked = context
        paths = ()
        if kind in ("busy", "unresolved"):
            session = _UnavailableSession(lb.Outcome.UNSUPPORTED, "controlled pool", 1)
            if kind == "busy":
                session._operation.acquire()
            else:
                session._cleanup_failure = OSError("pool cleanup unresolved")
        elif kind == "protected":
            paths = (str(copied),)
        elif kind == "copied-context":
            asked = replace(context)
        else:
            resource = type("Pending", (), {})()
            life.track(resource, "pending command")
        try:
            with pytest.raises((OwnershipRefused, lb.InvalidRequest)):
                owner.stage_suite(asked, lb.Suite(str(original), ("test_subject.py",)), protected_paths=paths)
            assert copied.read_bytes() == b"first tests"
        finally:
            if session is not None:
                if kind == "busy":
                    session._operation.release()
                session._cleanup_failure = None
                session.close()
            if resource is not None:
                life.completed(resource)
    owner.close()


def test_partial_write_failure_revokes_admission_and_preserves_primary_and_receipt(project, monkeypatch):
    original, owner = project
    primary = OSError("copied setup write failed")
    with owner.use() as context:
        copied = Path(context.root)
        written = Path.write_bytes
        def write(path, data):
            if path == copied / "setup.txt":
                raise primary
            return written(path, data)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "write_bytes", write)
            with pytest.raises(OSError) as caught:
                owner.stage_suite(context, lb.Suite(str(original), ("test_subject.py",), ("setup.txt",)))
        assert caught.value is primary and primary._langadapter_prepared_project is owner
        assert Path(owner._prepared_copy.owned.receipt).is_file()
        with pytest.raises(OwnershipRefused):
            lb.create("python", context)
    owner.close()


def test_staged_file_link_refuses_without_writing_its_destination(project, tmp_path):
    original, owner = project
    foreign = tmp_path / "foreign.txt"
    foreign.write_bytes(b"keep")
    with owner.use() as context:
        copied = Path(context.root) / "test_subject.py"
        copied.unlink()
        try:
            copied.symlink_to(foreign)
        except OSError:
            copied.write_bytes(b"first tests")
            pytest.skip("host does not permit file symlinks")
        with pytest.raises(OwnershipRefused):
            owner.stage_suite(context, lb.Suite(str(original), ("test_subject.py",)))
        assert foreign.read_bytes() == b"keep"
        copied.unlink()
    owner.close()


def test_adapter_ownership_metadata_cannot_be_replaced_by_suite_inputs(project):
    original, owner = project
    marker = ".langadapter-project-context"
    with owner.use() as context:
        copied = owner._prepared_copy.owned.root / marker
        before = copied.read_bytes()
        original.joinpath(marker).write_bytes(b"not adapter ownership")
        with pytest.raises(lb.InvalidRequest, match="protected"):
            owner.stage_suite(context, lb.Suite(str(original), ("test_subject.py",), (marker,)))
        assert copied.read_bytes() == before
        assert not (Path(context.root) / marker).exists()
    owner.close()
