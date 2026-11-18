"""Owned scratch cleanup does not claim a replacement or foreign directory."""
import json
import os
import stat
from pathlib import Path

import pytest

from langadapter._owned_directory import OwnedDirectory, OwnershipRefused


def _owned(tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir(exist_ok=True)
    return OwnedDirectory.create(parent=str(tmp_path), receipt_parent=str(receipts))


def test_creation_receipt_is_outside_the_disposable_directory(tmp_path):
    owned = _owned(tmp_path)
    assert not owned.receipt.is_relative_to(owned.root)
    record = json.loads(owned.receipt.read_text(encoding="utf-8"))
    assert record["root"] == str(owned.root)
    assert (record["device"], record["inode"]) == (
        owned.root.stat().st_dev, owned.root.stat().st_ino)
    (owned.root / "temporary.txt").write_text("temporary", encoding="utf-8")
    foreign = tmp_path / "durable-result.json"
    foreign.write_text("result", encoding="utf-8")
    owned.remove()
    assert not owned.root.exists()
    assert not owned.receipt.exists()
    assert foreign.read_text(encoding="utf-8") == "result"


def test_readonly_owned_files_are_removed_without_changing_external_inputs(tmp_path):
    owned = _owned(tmp_path)
    original = tmp_path / "original.txt"
    original.write_bytes(b"original\n")
    copied = owned.root / "copied.txt"
    copied.write_bytes(original.read_bytes())
    original.chmod(stat.S_IRUSR)
    copied.chmod(stat.S_IRUSR)
    mode = original.stat().st_mode
    try:
        owned.remove()
        assert not owned.root.exists() and not owned.receipt.exists()
        assert original.read_bytes() == b"original\n" and original.stat().st_mode == mode
    finally:
        original.chmod(mode | stat.S_IWUSR)


@pytest.mark.skipif(os.name != "nt", reason="Windows readonly deletion callback")
@pytest.mark.parametrize("kind", ["outside", "writable", "nonpermission", "other_function", "hardlink", "junction"])
def test_readonly_retry_refuses_foreign_or_unjudged_permission_changes(tmp_path, monkeypatch, kind):
    import _winapi
    from langadapter import _owned_directory as module

    owned = _owned(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    external = foreign / "input.txt"
    external.write_bytes(b"outside\n")
    candidate = owned.root / "selected.txt"
    if kind == "hardlink":
        os.link(external, candidate)
    elif kind == "junction":
        link = owned.root / "junction"
        _winapi.CreateJunction(str(foreign), str(link))
        candidate = link / "input.txt"
    elif kind == "outside":
        candidate = external
    else:
        candidate.write_bytes(b"owned\n")
    if kind != "writable":
        candidate.chmod(stat.S_IRUSR)
    modes = (candidate.stat().st_mode, external.stat().st_mode)
    primary = OSError("not a permission judgment") if kind == "nonpermission" else PermissionError("retained")
    calls = []

    def removed(root, *, onerror):
        function = os.scandir if kind == "other_function" else os.unlink
        onerror(function, str(candidate), (type(primary), primary, None))

    with monkeypatch.context() as patch:
        patch.setattr(module.shutil, "rmtree", removed)
        patch.setattr(module.os, "chmod", lambda *args: calls.append(args))
        with pytest.raises(type(primary)) as caught:
            owned.remove()
        assert caught.value is primary and not calls
    assert (candidate.stat().st_mode, external.stat().st_mode) == modes
    assert external.read_bytes() == b"outside\n"
    owned.verify()
    candidate.chmod(modes[0] | stat.S_IWUSR)
    owned.remove()


@pytest.mark.skipif(os.name != "nt", reason="Windows readonly deletion callback")
def test_readonly_retry_preserves_primary_and_receipt_when_retry_fails(tmp_path, monkeypatch):
    from langadapter import _owned_directory as module

    owned = _owned(tmp_path)
    candidate = owned.root / "selected.txt"
    candidate.write_bytes(b"owned\n")
    candidate.chmod(stat.S_IRUSR)
    primary = PermissionError("initial deletion failed")
    secondary = PermissionError("retry still refused")
    unlink = module.os.unlink

    def refused(path, *args, **kwargs):
        if Path(path) == candidate:
            raise secondary
        return unlink(path, *args, **kwargs)

    def removed(root, *, onerror):
        onerror(module.os.unlink, str(candidate), (PermissionError, primary, None))

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "unlink", refused)
        patch.setattr(module.shutil, "rmtree", removed)
        with pytest.raises(PermissionError) as caught:
            owned.remove()
        assert caught.value is primary and primary.__cause__ is secondary
    owned.verify()
    assert candidate.read_bytes() == b"owned\n"
    owned.remove()


def test_replaced_root_and_original_evidence_are_preserved(tmp_path):
    owned = _owned(tmp_path)
    original = tmp_path / "interrupted-run"
    owned.root.rename(original)
    owned.root.mkdir()
    foreign = owned.root / "user-evidence.txt"
    foreign.write_text("foreign", encoding="utf-8")
    with pytest.raises(OwnershipRefused, match="receipt does not match"):
        owned.remove()
    assert original.is_dir()
    assert foreign.read_text(encoding="utf-8") == "foreign"
    assert owned.receipt.is_file()


def test_modified_external_receipt_refuses_deletion(tmp_path):
    owned = _owned(tmp_path)
    record = json.loads(owned.receipt.read_text(encoding="utf-8"))
    record["root"] = str(tmp_path)
    owned.receipt.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(OwnershipRefused):
        owned.remove()
    assert owned.root.is_dir()
    assert owned.receipt.is_file()


def test_modified_internal_marker_refuses_deletion(tmp_path):
    owned = _owned(tmp_path)
    owned.marker.write_text("another-owner", encoding="ascii")
    with pytest.raises(OwnershipRefused, match="creation marker does not match"):
        owned.remove()
    assert owned.root.is_dir()
    assert owned.receipt.is_file()


def test_failed_deletion_preserves_the_creation_receipt(tmp_path, monkeypatch):
    owned = _owned(tmp_path)

    def refused(root, **options):
        raise PermissionError("not removed")

    monkeypatch.setattr("langadapter._owned_directory.shutil.rmtree", refused)
    with pytest.raises(PermissionError, match="not removed"):
        owned.remove()
    owned.verify()
    assert owned.root.is_dir()


def test_interrupted_deletion_preserves_the_creation_receipt(tmp_path, monkeypatch):
    owned = _owned(tmp_path)

    def interrupted(root, **options):
        raise KeyboardInterrupt()

    monkeypatch.setattr("langadapter._owned_directory.shutil.rmtree", interrupted)
    with pytest.raises(KeyboardInterrupt):
        owned.remove()
    owned.verify()
    assert owned.root.is_dir()


def test_root_replaced_by_an_escaped_link_is_not_followed(tmp_path):
    owned = _owned(tmp_path)
    original = tmp_path / "original"
    owned.root.rename(original)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    evidence = foreign / "user-evidence.txt"
    evidence.write_text("foreign", encoding="utf-8")
    try:
        os.symlink(foreign, owned.root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    with pytest.raises(OwnershipRefused):
        owned.remove()
    assert evidence.read_text(encoding="utf-8") == "foreign"
    assert owned.root.is_symlink()


def test_escaped_child_link_does_not_delete_its_destination(tmp_path):
    owned = _owned(tmp_path)
    evidence = tmp_path / "user-evidence.txt"
    evidence.write_text("foreign", encoding="utf-8")
    try:
        os.symlink(evidence, owned.root / "link.txt")
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")
    owned.remove()
    assert evidence.read_text(encoding="utf-8") == "foreign"


def test_an_arbitrary_directory_has_no_creation_receipt(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    identity = foreign.stat()
    pretend = OwnedDirectory(foreign, Path(tmp_path / "absent.json"),
                             identity.st_dev, identity.st_ino, "not-issued")
    with pytest.raises(OwnershipRefused):
        pretend.remove()
    assert foreign.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions only")
def test_windows_root_replaced_by_a_junction_is_preserved(tmp_path):
    import _winapi

    owned = _owned(tmp_path)
    owned.root.rename(tmp_path / "original")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    evidence = foreign / "user-evidence.txt"
    evidence.write_text("foreign", encoding="utf-8")
    _winapi.CreateJunction(str(foreign), str(owned.root))
    with pytest.raises(OwnershipRefused):
        owned.remove()
    assert evidence.read_text(encoding="utf-8") == "foreign"


@pytest.mark.parametrize("stage", ["receipt-create", "receipt-write", "marker-create", "marker-write"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_partial_creation_retains_exact_allocation_and_retries_cleanup(tmp_path, monkeypatch,
                                                                     stage, interrupt):
    from langadapter import _owned_directory as module
    primary = KeyboardInterrupt() if interrupt else OSError("interrupted creation")
    with monkeypatch.context() as patch:
        if stage == "receipt-create":
            patch.setattr(module.tempfile, "mkstemp", lambda **kw: (_ for _ in ()).throw(primary))
        elif stage == "marker-create":
            opened = module.os.open
            def opening(path, *args, **kwargs):
                if Path(path).name == ".langadapter-owner":
                    raise primary
                return opened(path, *args, **kwargs)
            patch.setattr(module.os, "open", opening)
        else:
            write = module._DirectoryCreation._write_acquired
            def writing(path, identity, value):
                if (path.name == ".langadapter-owner") == (stage == "marker-write"):
                    raise primary
                return write(path, identity, value)
            patch.setattr(module._DirectoryCreation, "_write_acquired", staticmethod(writing))
        with pytest.raises(type(primary)) as caught:
            _owned(tmp_path)
    assert caught.value is primary
    (allocation,) = primary._langadapter_owned_directories
    assert allocation.root.is_dir()
    identity = allocation.root.lstat()
    assert (identity.st_dev, identity.st_ino) == (
        allocation.identity.st_dev, allocation.identity.st_ino)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep", encoding="ascii")
    allocation.remove()
    allocation.remove()
    assert not allocation.root.exists()
    assert allocation.receipt is not None and not allocation.receipt.exists()
    assert unrelated.read_text() == "keep"


@pytest.mark.parametrize("replacement", ["root", "receipt", "marker"])
def test_partial_creation_cannot_adopt_replaced_allocations(tmp_path, monkeypatch, replacement):
    from langadapter import _owned_directory as module
    write = module._DirectoryCreation._write_acquired
    primary = OSError("marker write failed")
    def writing(path, identity, value):
        if path.name == ".langadapter-owner":
            raise primary
        return write(path, identity, value)
    with monkeypatch.context() as patch:
        patch.setattr(module._DirectoryCreation, "_write_acquired", staticmethod(writing))
        with pytest.raises(OSError):
            _owned(tmp_path)
    (allocation,) = primary._langadapter_owned_directories
    selected = allocation.root if replacement == "root" else (
        allocation.receipt if replacement == "receipt" else allocation.root / ".langadapter-owner")
    selected.rename(tmp_path / "original-allocation")
    if replacement == "root":
        selected.mkdir()
    else:
        selected.write_text("foreign", encoding="ascii")
    with pytest.raises(OwnershipRefused):
        allocation.remove()
    assert selected.exists() and not allocation.removed


def test_uncaptured_root_identity_retains_without_adopting_later_stat(tmp_path, monkeypatch):
    primary = OSError("initial root identity unavailable")
    stat_path = Path.lstat
    def observed(path, *args, **kwargs):
        if path.name.startswith("langadapter_"):
            raise primary
        return stat_path(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", observed)
        with pytest.raises(OSError) as caught:
            _owned(tmp_path)
    assert caught.value is primary
    (allocation,) = primary._langadapter_owned_directories
    assert allocation.root.is_dir() and allocation.identity is None
    with pytest.raises(OwnershipRefused, match="identity was not captured"):
        allocation.remove()
    assert allocation.root.is_dir()


def test_partial_cleanup_retries_receipt_removal_after_root_is_gone(tmp_path, monkeypatch):
    from langadapter import _owned_directory as module
    primary = OSError("first receipt creation failed")
    with monkeypatch.context() as patch:
        patch.setattr(module.tempfile, "mkstemp", lambda **kw: (_ for _ in ()).throw(primary))
        with pytest.raises(OSError):
            _owned(tmp_path)
    (allocation,) = primary._langadapter_owned_directories
    unlink = Path.unlink
    def denied(path, *args, **kwargs):
        if path == allocation.receipt:
            raise PermissionError("receipt removal failed")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", denied)
        with pytest.raises(PermissionError):
            allocation.remove()
    assert not allocation.root.exists() and allocation.receipt.is_file()
    allocation.remove()
    allocation.remove()
    assert not allocation.receipt.exists() and allocation.removed


def _short_name(target_dir: Path) -> str:
    """The directory's Windows 8.3 spelling, skipping where the volume makes
    none."""
    if os.name != "nt":
        pytest.skip("8.3 names are a Windows spelling")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(target_dir), buffer, len(buffer))
    if not 0 < length < len(buffer) or "~" not in buffer.value:
        pytest.skip("the volume makes no 8.3 names")
    return buffer.value


def test_an_owned_dir_below_a_short_name_is_spelled_long(tmp_path):
    """A parent spelled with 8.3 names (``C:\\Users\\LONGNA~1``) gives the
    copy the long spelling that the runs in it print."""
    parent = tmp_path / "a_long_dir_name"
    parent.mkdir()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    owned = OwnedDirectory.create(parent=_short_name(parent), receipt_parent=str(receipts))
    try:
        assert owned.root.parent == parent and "~" not in str(owned.root)
    finally:
        owned.remove()


def test_a_work_dir_named_short_holds_its_dirs_spelled_long(tmp_path):
    """An owned directory spelled long in a work directory given in 8.3 names still
    rebases onto the container's work directory, and the reverse."""
    from langadapter.execution import rebase_path

    zone = tmp_path / "a_long_work_dir"
    zone.mkdir()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    short = _short_name(zone)
    owned = OwnedDirectory.create(parent=short, receipt_parent=str(receipts))
    try:
        name = owned.root.name
        assert rebase_path(str(owned.root), short, "/workzone") == f"/workzone/{name}"
        assert rebase_path(f"{short}\\{name}\\x.py", str(zone), "/workzone") == (
            f"/workzone/{name}/x.py")
        # A path not yet made keeps its spelling.
        assert rebase_path(f"{short}\\new\\x.py", short, "/workzone") == "/workzone/new/x.py"
    finally:
        owned.remove()
