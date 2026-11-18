"""Known unsupported inputs fail before any prepared copy is made or allocated."""
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _prepared_copy
from langadapter._owned_directory import OwnershipRefused
from langadapter._prepared_copy import PreparedCopy, WorkspacePreparationFailed


def _source(tmp_path):
    source = tmp_path / "source"
    (source / "resources").mkdir(parents=True)
    (source / "resources" / "fixture.dat").write_bytes(b"declared resource\n")
    (source / "target.py").write_bytes(b"value = 1\n")
    return source


def _forbid_allocation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid inputs must fail before allocation or copying")

    monkeypatch.setattr(_prepared_copy.OwnedDirectory, "create", forbidden)
    monkeypatch.setattr(_prepared_copy, "_copy_tree", forbidden)


@pytest.mark.parametrize("invalid", ["link", "reparse", "special", "unreadable"])
def test_invalid_nested_input_fails_before_allocating(tmp_path, monkeypatch, invalid):
    source = _source(tmp_path)
    entry = source / "resources" / "fixture.dat"
    before = entry.read_bytes(), stat.S_IMODE(entry.stat().st_mode)
    ordinary = _prepared_copy._ordinary

    def inspect(path):
        if path == entry:
            if invalid == "unreadable":
                raise PermissionError("input cannot be inspected")
            mode = stat.S_IFLNK if invalid == "link" else stat.S_IFIFO
            attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            identity = SimpleNamespace(st_mode=mode, st_file_attributes=0)
            if invalid == "reparse":
                identity.st_mode = stat.S_IFREG
                identity.st_file_attributes = attributes
                monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", attributes, raising=False)
            monkeypatch.setattr(Path, "lstat", lambda self: identity if self == entry else original_lstat(self))
        return ordinary(path)

    original_lstat = Path.lstat
    monkeypatch.setattr(_prepared_copy, "_ordinary", inspect)
    _forbid_allocation(monkeypatch)
    with pytest.raises(WorkspacePreparationFailed, match="before workspace allocation") as caught:
        PreparedCopy.create(str(source), parent=str(tmp_path))
    assert isinstance(caught.value.__cause__, (OwnershipRefused, PermissionError))
    assert not hasattr(caught.value, "_langadapter_prepared_copy")
    monkeypatch.setattr(Path, "lstat", original_lstat)
    assert (entry.read_bytes(), stat.S_IMODE(entry.stat().st_mode)) == before
    assert not list(tmp_path.glob("langadapter_*"))


def test_unreadable_directory_fails_before_allocating(tmp_path, monkeypatch):
    source = _source(tmp_path)
    scandir = os.scandir

    def inspect(path):
        if Path(path) == source / "resources":
            raise PermissionError("directory cannot be enumerated")
        return scandir(path)

    monkeypatch.setattr(_prepared_copy.os, "scandir", inspect)
    _forbid_allocation(monkeypatch)
    with pytest.raises(WorkspacePreparationFailed, match="directory cannot be enumerated"):
        PreparedCopy.create(str(source), parent=str(tmp_path))


def test_preflight_rechecks_directories_before_enumerating(tmp_path, monkeypatch):
    source = _source(tmp_path)
    ordinary = _prepared_copy._ordinary
    visits = 0

    def inspect(path):
        nonlocal visits
        if path == source / "resources":
            visits += 1
            if visits == 2:
                raise OwnershipRefused("directory became a link before enumeration")
        return ordinary(path)

    monkeypatch.setattr(_prepared_copy, "_ordinary", inspect)
    _forbid_allocation(monkeypatch)
    with pytest.raises(WorkspacePreparationFailed, match="became a link before enumeration"):
        PreparedCopy.create(str(source), parent=str(tmp_path))
    assert visits == 2


@pytest.mark.parametrize("language", ["python", "java"])
def test_public_factory_preflights_before_copying_all_declared_inputs(tmp_path, monkeypatch, language):
    source = _source(tmp_path)
    # Ordinary directories such as .venv are copied as inputs.
    (source / ".venv").mkdir()
    (source / ".venv" / "settings.txt").write_bytes(b"not silently ignored\n")
    events = []
    preflight = _prepared_copy._preflight_tree
    copy = _prepared_copy._copy_tree

    def inspect(path):
        assert path == source
        assert not list(tmp_path.glob("langadapter_*"))
        preflight(path)
        events.append("preflight")

    def copied(first, second):
        assert events and events[0] == "preflight"
        events.append("copy")
        copy(first, second)

    monkeypatch.setattr(_prepared_copy, "_preflight_tree", inspect)
    monkeypatch.setattr(_prepared_copy, "_copy_tree", copied)
    context = lb.ProjectContext(str(source), lb.Interpreter(sys.executable))
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    with owner.use() as prepared:
        adapter = lb.create(language, prepared)
        assert adapter.context.root == prepared.root
        assert (Path(prepared.root) / "resources" / "fixture.dat").read_bytes() == b"declared resource\n"
        assert (Path(prepared.root) / ".venv" / "settings.txt").read_bytes() == b"not silently ignored\n"
    owner.close()
    assert events[0] == "preflight"
    assert source.is_dir()
    assert (source / "target.py").read_bytes() == b"value = 1\n"


def test_copy_rechecks_inputs_after_preflight_and_retains_partial_owner(tmp_path, monkeypatch):
    source = _source(tmp_path)
    ordinary = _prepared_copy._ordinary
    preflight = _prepared_copy._preflight_tree
    checked = False

    def inspect(path):
        if checked and path == source / "resources" / "fixture.dat":
            raise OwnershipRefused("input became a link after preflight")
        return ordinary(path)

    def checked_tree(path):
        nonlocal checked
        preflight(path)
        checked = True

    monkeypatch.setattr(_prepared_copy, "_ordinary", inspect)
    monkeypatch.setattr(_prepared_copy, "_preflight_tree", checked_tree)
    with pytest.raises(WorkspacePreparationFailed, match="retained roots") as caught:
        PreparedCopy.create(str(source), parent=str(tmp_path), receipt_parent=str(tmp_path))
    prepared_copy = caught.value._langadapter_prepared_copy
    assert prepared_copy.owned.receipt.is_file()
    assert prepared_copy.baseline.receipt.is_file()
    prepared_copy.verify_ownership()
    assert (source / "resources" / "fixture.dat").read_bytes() == b"declared resource\n"


def test_preflight_interruption_has_no_allocations_and_keeps_primary(tmp_path, monkeypatch):
    source = _source(tmp_path)
    error = KeyboardInterrupt("stopped inspection")

    def interrupted(path):
        raise error

    monkeypatch.setattr(_prepared_copy, "_preflight_tree", interrupted)
    _forbid_allocation(monkeypatch)
    with pytest.raises(KeyboardInterrupt) as caught:
        PreparedCopy.create(str(source), parent=str(tmp_path))
    assert caught.value is error
