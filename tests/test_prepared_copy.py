"""Private prepared copy ownership does not depend on Git or inferred authorship."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.execution import ContainerExecutor, LocalExecutor, executor_for
from langadapter._owned_directory import OwnershipRefused
from langadapter._prepared_copy import PreparedCopy, WorkspaceBusy, WorkspacePreparationFailed


@pytest.fixture
def snapshot(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "input.txt").write_text("declared input", encoding="ascii")
    zone = tmp_path / "zone"
    zone.mkdir()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    prepared_copy = PreparedCopy.create(
        str(source), parent=str(zone), receipt_parent=str(receipts))
    return source, prepared_copy


def _remove(prepared_copy):
    """Remove the copy and its baseline, as a prepared project's close does."""
    prepared_copy.owned.remove()
    prepared_copy.baseline.remove()


def test_snapshot_is_a_copy_with_external_baseline_and_receipts(snapshot):
    source, prepared_copy = snapshot
    assert prepared_copy.root != source
    assert not prepared_copy.baseline.root.is_relative_to(prepared_copy.owned.root)
    assert not prepared_copy.owned.receipt.is_relative_to(prepared_copy.owned.root)
    (prepared_copy.root / "input.txt").write_text("modified", encoding="ascii")
    assert (source / "input.txt").read_text(encoding="ascii") == "declared input"
    assert (prepared_copy.inputs / "input.txt").read_text(encoding="ascii") == "declared input"
    _remove(prepared_copy)
    assert source.is_dir()


def test_reuse_restores_modified_deleted_and_added_inputs(snapshot, tmp_path):
    source, prepared_copy = snapshot
    durable = tmp_path / "results.json"
    durable.write_text("successful evidence", encoding="ascii")
    with prepared_copy.use() as root:
        (root / "input.txt").unlink()
        (root / "output.txt").write_text("test output", encoding="ascii")
    with prepared_copy.use() as root:
        assert (root / "input.txt").read_text(encoding="ascii") == "declared input"
        assert not (root / "output.txt").exists()
        (root / "input.txt").write_text("modified", encoding="ascii")
    with prepared_copy.use() as root:
        assert (root / "input.txt").read_text(encoding="ascii") == "declared input"
    _remove(prepared_copy)
    assert durable.read_text(encoding="ascii") == "successful evidence"
    assert (source / "input.txt").read_text(encoding="ascii") == "declared input"


def test_a_second_instance_cannot_acquire_an_active_prepared_copy(snapshot):
    _, prepared_copy = snapshot
    other = PreparedCopy(prepared_copy.owned, prepared_copy.baseline)
    with prepared_copy.use():
        with pytest.raises(WorkspaceBusy):
            with other.use():
                pytest.fail("overlap must be refused")
    _remove(prepared_copy)


def test_another_process_cannot_acquire_an_active_prepared_copy(snapshot):
    _, prepared_copy = snapshot
    script = (
        "from pathlib import Path; "
        "from langadapter._prepared_copy import PreparedCopy; "
        "from langadapter._owned_directory import OwnedDirectory; "
        "import contextlib,json,sys; "
        "a,b=json.loads(sys.argv[1]); "
        "w=PreparedCopy(OwnedDirectory(Path(a['root']),Path(sys.argv[2]),"
        "a['device'],a['inode'],a['token']),OwnedDirectory(Path(b['root']),"
        "Path(sys.argv[3]),b['device'],b['inode'],b['token'])); "
        "contextlib.ExitStack().enter_context(w.use())"
    )
    records = json.dumps([prepared_copy.owned._record(), prepared_copy.baseline._record()])
    with prepared_copy.use():
        done = subprocess.run(
            [sys.executable, "-c", script, records, str(prepared_copy.owned.receipt),
             str(prepared_copy.baseline.receipt)], capture_output=True, text=True,
            timeout=30)
        assert done.returncode != 0
        assert "WorkspaceBusy" in done.stderr
        assert prepared_copy.root.is_dir()
    _remove(prepared_copy)


@pytest.mark.parametrize("error", [RuntimeError("failed command"), KeyboardInterrupt()])
def test_interrupted_operation_retains_receipt_and_uncertain_lease(snapshot, error):
    _, prepared_copy = snapshot
    with pytest.raises(type(error)):
        with prepared_copy.use():
            raise error
    assert prepared_copy.owned.receipt.is_file()
    assert prepared_copy.baseline.receipt.is_file()
    assert prepared_copy.lease.is_file()
    with pytest.raises(WorkspaceBusy):
        with prepared_copy.use():
            pytest.fail("an uncertain lease must refuse a new use")


def test_replaced_root_preserves_original_and_foreign_files(snapshot, tmp_path):
    _, prepared_copy = snapshot
    original = tmp_path / "original-workspace"
    prepared_copy.owned.root.rename(original)
    prepared_copy.owned.root.mkdir()
    foreign = prepared_copy.owned.root / "foreign.txt"
    foreign.write_text("foreign", encoding="ascii")
    with pytest.raises(OwnershipRefused):
        prepared_copy.owned.remove()
    assert foreign.read_text(encoding="ascii") == "foreign"
    assert original.is_dir()


def test_failed_restore_retains_lease_and_never_reports_success(snapshot, monkeypatch):
    _, prepared_copy = snapshot

    def failed(*args):
        raise OSError("copy refused")

    monkeypatch.setattr("langadapter._prepared_copy._copy_tree", failed)
    with pytest.raises(OSError, match="copy refused"):
        with prepared_copy.use():
            pytest.fail("failed restoration must not run tests")
    assert prepared_copy.lease.is_file()
    assert prepared_copy.owned.receipt.is_file()


def test_failed_preparation_retains_external_receipts(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    receipts = tmp_path / "receipts"
    receipts.mkdir()

    def failed(*args):
        raise OSError("copy refused")

    monkeypatch.setattr("langadapter._prepared_copy._copy_tree", failed)
    with pytest.raises(WorkspacePreparationFailed, match="retained roots"):
        PreparedCopy.create(str(source), parent=str(tmp_path),
                                receipt_parent=str(receipts))
    records = list(receipts.glob("*.json"))
    assert len(records) == 2
    for record in records:
        assert Path(json.loads(record.read_text(encoding="ascii"))["root"]).is_dir()


def test_private_copy_parent_cannot_be_inside_the_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(OwnershipRefused, match="outside the source"):
        PreparedCopy.create(str(source), parent=str(source))
    assert list(source.iterdir()) == []


def test_a_source_containing_the_baseline_parent_is_refused(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr("langadapter._owned_directory.tempfile.gettempdir", lambda: str(source))
    with pytest.raises(OwnershipRefused, match="outside the source"):
        PreparedCopy.create(str(source), parent=str(tmp_path))
    assert list(source.iterdir()) == []


def test_replaced_project_link_preserves_its_destination(snapshot, tmp_path):
    _, prepared_copy = snapshot
    original = prepared_copy.root.with_name("original-project")
    prepared_copy.root.rename(original)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    evidence = foreign / "input.txt"
    evidence.write_text("foreign", encoding="ascii")
    try:
        os.symlink(foreign, prepared_copy.root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(OwnershipRefused, match="cannot contain links"):
        with prepared_copy.use():
            pytest.fail("must not execute an escaped project")
    assert evidence.read_text(encoding="ascii") == "foreign"
    assert original.is_dir()
    assert prepared_copy.owned.receipt.is_file()


def test_changed_lease_is_retained_instead_of_releasing_another_owner(snapshot):
    _, prepared_copy = snapshot
    with pytest.raises(OwnershipRefused, match="lease changed"):
        with prepared_copy.use():
            prepared_copy.lease.write_text('{"pid": 0, "token": "another-owner"}', encoding="ascii")
    assert prepared_copy.lease.is_file()
    assert prepared_copy.root.is_dir()


def test_source_link_is_refused_without_modifying_its_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("foreign", encoding="ascii")
    try:
        os.symlink(foreign, source / "link.txt")
    except OSError:
        pytest.skip("file symlinks unavailable")
    with pytest.raises(WorkspacePreparationFailed, match="cannot contain links"):
        PreparedCopy.create(str(source), parent=str(tmp_path))
    assert foreign.read_text(encoding="ascii") == "foreign"


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions only")
def test_source_junction_is_refused_without_following_it(tmp_path):
    import _winapi

    source = tmp_path / "source"
    source.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    evidence = foreign / "input.txt"
    evidence.write_text("foreign", encoding="ascii")
    _winapi.CreateJunction(str(foreign), str(source / "junction"))
    with pytest.raises(WorkspacePreparationFailed, match="cannot contain links"):
        PreparedCopy.create(str(source), parent=str(tmp_path))
    assert evidence.read_text(encoding="ascii") == "foreign"


def _container(tmp_path):
    return lb.ProjectContext(
        root="/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution("selected", work_dir=str(tmp_path),
                                        container_work_dir="/workzone"))


def test_container_prepared_copy_never_falls_back_to_a_local_executor(tmp_path):
    with pytest.raises(OwnershipRefused, match="context's container executor"):
        PreparedCopy.create_in_container(_container(tmp_path), LocalExecutor())
    assert list(tmp_path.iterdir()) == []


def test_container_prepared_copy_refuses_another_container_before_commands(tmp_path):
    executor = ContainerExecutor("another", work_dir=str(tmp_path),
                                 container_work_dir="/workzone")
    with pytest.raises(OwnershipRefused, match="context's container executor"):
        PreparedCopy.create_in_container(_container(tmp_path), executor)
    assert list(tmp_path.iterdir()) == []


class RecordingContainer(ContainerExecutor):
    def __init__(self, zone, *, marker="matching", irregular="", copy_exit=0):
        super().__init__("selected", work_dir=zone, container_work_dir="/workzone")
        self.marker = marker
        self.irregular = irregular
        self.copy_exit = copy_exit
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        output = ""
        code = 0
        if argv[0] == "find":
            output = self.irregular
        elif argv[0] == "cat":
            output = Path(argv[1]).read_text(encoding="ascii") if self.marker == "matching" else self.marker
        elif argv[0] == "cp":
            code = self.copy_exit
        return subprocess.CompletedProcess(argv, code, output, "")


def test_mismapped_container_preparation_never_copies_or_removes(tmp_path):
    executor = RecordingContainer(str(tmp_path), marker="another-mount")
    with pytest.raises(WorkspacePreparationFailed, match="mount does not match"):
        PreparedCopy.create_in_container(_container(tmp_path), executor)
    assert not any(argv[0] in {"cp", "rm"} for argv in executor.calls)
    roots = list(tmp_path.glob("langadapter_project_*"))
    assert len(roots) == 1
    assert roots[0].is_dir()


def test_container_links_are_refused_before_allocating_a_prepared_copy(tmp_path):
    executor = RecordingContainer(str(tmp_path), irregular="/project/link\0")
    with pytest.raises(OwnershipRefused, match="links/special files"):
        PreparedCopy.create_in_container(_container(tmp_path), executor)
    assert list(tmp_path.iterdir()) == []
    assert not any(argv[0] in {"cp", "rm"} for argv in executor.calls)


def test_failed_container_copy_retains_roots_instead_of_executing_locally(tmp_path):
    executor = RecordingContainer(str(tmp_path), copy_exit=1)
    with pytest.raises(WorkspacePreparationFailed, match="container project copy failed"):
        PreparedCopy.create_in_container(_container(tmp_path), executor)
    assert any(argv[0] == "cp" for argv in executor.calls)
    assert not any(argv[0] == "rm" for argv in executor.calls)
    assert len(list(tmp_path.glob("langadapter_project_*"))) == 1


def test_real_docker_prepared_copy_copies_and_restores_selected_container_inputs(tmp_path):
    container = os.environ.get("LANGADAPTER_TEST_JAVA_CONTAINER")
    zone = os.environ.get("LANGADAPTER_TEST_CONTAINER_WORK_DIR")
    if not (container and zone):
        pytest.skip("needs an explicit prepared container and mounted host work directory")
    # The external source remains in the declared mount; its project copy and
    # durable result are separate siblings and are never cleanup candidates.
    source = Path(tempfile.mkdtemp(prefix="source-", dir=zone))
    (source / "input.txt").write_text("container input", encoding="ascii")
    context = lb.ProjectContext(
        root="/workzone/" + source.name,
        interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution(container, work_dir=zone,
                                        container_work_dir="/workzone"))
    executor = executor_for(context)
    prepared_copy = PreparedCopy.create_in_container(context, executor)
    durable = Path(zone) / ("result-" + source.name + ".json")
    durable.write_text("accepted evidence", encoding="ascii")
    with prepared_copy.use() as root:
        observed = executor.run(["cat", prepared_copy.executing_root + "/input.txt"],
                                timeout=120)
        assert observed.returncode == 0
        assert observed.stdout == "container input"
        (root / "input.txt").write_text("modified", encoding="ascii")
        (root / "output.txt").write_text("new output", encoding="ascii")
    with prepared_copy.use() as root:
        assert (root / "input.txt").read_text(encoding="ascii") == "container input"
        assert not (root / "output.txt").exists()
    assert (source / "input.txt").read_text(encoding="ascii") == "container input"
    assert durable.read_text(encoding="ascii") == "accepted evidence"
