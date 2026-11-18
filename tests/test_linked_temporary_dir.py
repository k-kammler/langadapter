"""A temporary directory reached through a link, as macOS's TMPDIR below the
link /var: owned directories take its physical path, a suite below the link is
staged, a project below it is mutated in place, and the collection check
collects in its scratch directory and in a caller's directory below the link
(beside a directory the user cannot enter, as /private/var holds, pytest's walk
of the link's spelling failed)."""
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

import langadapter as lb
from langadapter._owned_directory import OwnedDirectory, OwnershipRefused
from langadapter.python.adapter import PythonAdapter


@pytest.fixture
def linked_temp(tmp_path, monkeypatch):
    """The temporary directory as a link to a real directory."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"host does not permit directory symlinks: {error}")
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    monkeypatch.setenv("TMPDIR", str(link))
    return link, Path(os.path.realpath(real))


def _project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "subject.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable))


def test_owned_dirs_take_the_temporary_dirs_physical_path(linked_temp):
    _link, real = linked_temp
    owned = OwnedDirectory.create(prefix="linked_")
    try:
        assert owned.root.parent == real
    finally:
        owned.remove()


def test_a_suite_below_the_link_is_staged_and_a_link_inside_it_refused(linked_temp, tmp_path):
    link, real = linked_temp
    suite = link / "suite"
    suite.mkdir()
    (suite / "test_x.py").write_text(
        "from subject import f\n\n\ndef test_x():\n    assert f() == 1\n", encoding="utf-8")
    owner = lb.prepare_project(_project(tmp_path))
    try:
        assert Path(owner.root).is_relative_to(real)
        with owner.use() as context:
            staged = owner.stage_suite(context, lb.Suite(str(suite), ("test_x.py",)))
            assert staged.root == str(real / "suite")
            (suite / "test_y.py").symlink_to(suite / "test_x.py")
            with pytest.raises(OwnershipRefused, match="cannot contain links"):
                owner.stage_suite(context, lb.Suite(str(suite), ("test_y.py",)))
    finally:
        owner.close()


def _backed_up_in_place(link, make_link):
    """A mutation in place below ``link`` keeps the target's original bytes:
    the link above the project aliases nothing, a link inside the project
    (``make_link(at, target)``) still does."""
    from langadapter.python._mutation import SourceBackup

    root = link / "project"
    (root / "pkg").mkdir(parents=True)
    target = root / "pkg" / "mod.py"
    target.write_text("value = 1\n", encoding="utf-8")
    backup = SourceBackup(target, project_root=root)
    try:
        target.write_text("value = 2\n", encoding="utf-8")
        backup.restore()
        assert target.read_text(encoding="utf-8") == "value = 1\n"
    finally:
        backup.remove()
    elsewhere = link / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "mod.py").write_text("value = 1\n", encoding="utf-8")
    make_link(root / "linked", elsewhere)
    with pytest.raises(OwnershipRefused, match="unaliased"):
        SourceBackup(root / "linked" / "mod.py", project_root=root)


def test_a_project_below_the_link_is_backed_up_and_a_link_inside_it_refused(linked_temp):
    _backed_up_in_place(linked_temp[0], lambda at, target: at.symlink_to(
        target, target_is_directory=True))


def test_a_project_below_a_junction_is_backed_up_and_a_junction_inside_it_refused(tmp_path):
    """The same through Windows' directory junctions, which need no symlink
    right."""
    if os.name != "nt":
        pytest.skip("a directory junction is a Windows link")
    import _winapi

    def junction(at, target):
        _winapi.CreateJunction(str(target), str(at))

    real = tmp_path / "real"
    real.mkdir()
    junction(tmp_path / "link", real)
    _backed_up_in_place(tmp_path / "link", junction)


@pytest.fixture
def closed_dir(linked_temp):
    """A directory the user cannot enter in the linked directory's target."""
    if os.name == "nt":
        pytest.skip("a directory the user cannot enter is a POSIX mode")
    if os.geteuid() == 0:
        pytest.skip("root enters every directory")
    closed = linked_temp[1] / "agentx"
    closed.mkdir()
    (closed / "__init__.py").write_text("", encoding="utf-8")
    closed.chmod(0)
    yield closed
    closed.chmod(stat.S_IRWXU)


def test_the_collection_check_collects_below_the_link(closed_dir, tmp_path):
    context = _project(tmp_path)
    report = {}
    PythonAdapter(context)._prune_collection_breakers(
        "def test_one():\n    assert True\n", context.root, timeout=120, report=report)
    assert report["status"] == "collects", report


def test_a_callers_dir_below_the_link_is_collected(linked_temp, closed_dir, tmp_path):
    """As Testuary hands its own temporary directories to test_files."""
    link, _real = linked_temp
    suite = Path(tempfile.mkdtemp(prefix="caller_"))
    assert suite.is_relative_to(link)
    (suite / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    files = PythonAdapter(_project(tmp_path)).test_files([str(suite)])
    assert [Path(name).name for name in files] == ["test_x.py"], files
