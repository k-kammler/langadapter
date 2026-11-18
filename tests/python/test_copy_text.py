"""A run in a prepared copy prints what the same run prints in place.

The project and a suite outside it are copied below their shared directory,
and the copy's paths, tracebacks included, print as the original's."""
import json
import ntpath
import os
import posixpath
import re
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter._owned_directory import OwnershipRefused
from langadapter._prepared_copy import _mirrored
from langadapter.preparation import original_text, prepare_project
from langadapter.runs import SuiteRunRequest

MODULE = ("def add(a, b):\n    return a + b\n\n\n"
          "def boom():\n    raise ValueError('boom')\n")
SUITES = {
    "red": ("from mod import add, boom\n\n\n"
            "def test_add_wrong():\n    assert add(1, 2) == 4\n\n\n"
            "def test_boom():\n    boom()\n\n\n"
            "def test_ok():\n    assert add(1, 1) == 2\n"),
    "collection_error": ("import not_a_module_anywhere\n\n\n"
                         "def test_a():\n    pass\n"),
    # A test's message names a path of the copy.
    "message_path": "def test_where():\n    assert False, __file__\n",
    # An error prints a path of the copy as a repr. A long repr that pytest
    # shortens in its middle stays different and is not tested here.
    "file_repr": ("import os\n\n\n"
                  "def test_missing():\n"
                  "    open(os.path.join(os.path.dirname(__file__), 'missing_beside.txt'))\n"),
}


def _layout(tmp_path, suite):
    work = tmp_path / "work"
    (work / "proj").mkdir(parents=True)
    (work / "tests").mkdir()
    (work / "proj" / "mod.py").write_text(MODULE, encoding="utf-8")
    (work / "tests" / "test_x.py").write_text(SUITES[suite], encoding="utf-8")
    return work


def _text(run):
    return re.sub(r"in \d+\.\d+s", "in N.NNs", run.output)


@pytest.mark.parametrize("suite", sorted(SUITES))
def test_a_run_in_a_copy_prints_what_an_in_place_run_prints(tmp_path, suite):
    _compare(_layout(tmp_path, suite), suite)


def _compare(work, suite, parent=None):
    """The suite run in place and in a copy below ``parent`` print the same."""
    context = lb.ProjectContext(str(work / "proj"), lb.Interpreter(path=sys.executable))
    in_place = lb.create("python", context).tests.run_suite(SuiteRunRequest(
        lb.Suite(str(work / "tests"), ("test_x.py",)), timeout_s=120))
    owner = prepare_project(context, companions=(str(work / "tests"),), parent=parent)
    try:
        with owner.use() as scoped:
            suite_copy = owner.companion_path(str(work / "tests"))
            assert Path(suite_copy).parent == Path(scoped.root).parent
            copied = lb.create("python", scoped).tests.run_suite(SuiteRunRequest(
                lb.Suite(suite_copy, ("test_x.py",)), timeout_s=120))
    finally:
        owner.close()
    assert copied.outcome is in_place.outcome
    assert _text(copied) == _text(in_place)
    assert "langadapter_project_" not in copied.output
    assert [str(t.test) for t in copied.tests] == [str(t.test) for t in in_place.tests]
    assert [t.message for t in copied.tests] == [t.message for t in in_place.tests]
    if suite == "red":
        assert f"FAILED ..{os.sep}tests{os.sep}test_x.py::test_add_wrong" in copied.output


@pytest.mark.parametrize("columns", ["80", "120", "160"])
def test_a_message_naming_a_path_is_cut_as_in_place_at_any_width(
        tmp_path, monkeypatch, columns, system_temporary_dir):
    """pytest cuts a failure's message to the terminal width in its summary
    line, so the run maps a copy path when the report is made, before the
    cut. The copy goes to the system temporary directory, apart from the long
    original path, so the cut falls where the two paths differ."""
    monkeypatch.setenv("COLUMNS", columns)
    # About 150 characters to the in-place directory, within Windows' 260.
    if os.name == "nt" and len(str(tmp_path)) > 180:
        pytest.skip("pytest's base directory leaves too little of Windows' 260 characters")
    pad = "o" * max(0, 140 - len(str(tmp_path)))
    work = _layout(tmp_path / ("in_place_" + pad), "message_path")
    _compare(work, "message_path", parent=system_temporary_dir)


def test_a_copy_below_a_short_named_temporary_dir_prints_the_same(tmp_path, monkeypatch):
    """Below a temporary directory spelled in 8.3 names, the copy takes the
    long spelling pytest prints, so failing paths print as in place."""
    import ctypes
    import tempfile

    if os.name != "nt":
        pytest.skip("8.3 names are a Windows spelling")
    temporary = tmp_path / "a_long_temporary_dir"
    temporary.mkdir()
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(temporary), buffer, len(buffer))
    if not 0 < length < len(buffer) or "~" not in buffer.value:
        pytest.skip("the volume makes no 8.3 names")
    work = _layout(tmp_path, "red")
    context = lb.ProjectContext(str(work / "proj"), lb.Interpreter(path=sys.executable))
    in_place = lb.create("python", context).tests.run_suite(SuiteRunRequest(
        lb.Suite(str(work / "tests"), ("test_x.py",)), timeout_s=120))
    monkeypatch.setattr(tempfile, "tempdir", buffer.value)
    owner = prepare_project(context, companions=(str(work / "tests"),))
    try:
        with owner.use() as scoped:
            assert "~" not in scoped.root
            copied = lb.create("python", scoped).tests.run_suite(SuiteRunRequest(
                lb.Suite(owner.companion_path(str(work / "tests")), ("test_x.py",)),
                timeout_s=120))
    finally:
        owner.close()
    assert _text(copied) == _text(in_place)


def test_the_copy_mirrors_the_project_and_its_suite_below_their_shared_dir(tmp_path):
    work = _layout(tmp_path, "red")
    context = lb.ProjectContext(str(work / "proj"), lb.Interpreter(path=sys.executable))
    owner = prepare_project(context, companions=(str(work / "tests"),))
    try:
        with owner.use() as scoped:
            root = Path(scoped.root)
            suite_copy = Path(owner.companion_path(str(work / "tests" / "test_x.py")))
            assert root.name == "proj" and suite_copy.parent.name == "tests"
            assert suite_copy.read_text(encoding="utf-8") == SUITES["red"]
            # A file a test writes lands in the copy, and the next use
            # restores the suite.
            (suite_copy.parent / "written.txt").write_text("x", encoding="utf-8")
            text = f"{root}{os.sep}mod.py and {root.as_posix()}/mod.py"
            assert original_text(scoped, text) == (
                f"{work / 'proj'}{os.sep}mod.py and {(work / 'proj').as_posix()}/mod.py")
        with owner.use():
            assert not (suite_copy.parent / "written.txt").exists()
        assert not (work / "tests" / "written.txt").exists()
    finally:
        owner.close()


def test_a_suite_on_another_drive_or_around_the_project_is_refused():
    def no_common(paths):
        raise ValueError("Paths don't have the same drive")

    with pytest.raises(OwnershipRefused, match="share no common directory"):
        _mirrored("C:/work/proj", ["D:/suites/tests"], common=no_common)
    with pytest.raises(OwnershipRefused, match="beside the project"):
        _mirrored("/work/proj", ["/work"], common=posixpath.commonpath,
                  relative=posixpath.relpath)
    with pytest.raises(OwnershipRefused, match="beside the project"):
        _mirrored("/work/proj", ["/work/proj/tests"], common=posixpath.commonpath,
                  relative=posixpath.relpath)


def test_a_container_layout_is_mirrored_below_the_container_root():
    suite = "/workzone/tests"
    layout, placed, shared = _mirrored(
        "/home/swe-bench/pydata__xarray", [suite],
        common=posixpath.commonpath, relative=posixpath.relpath)
    assert (layout, placed, shared) == (("home", "swe-bench", "pydata__xarray"),
                                        (tuple(suite.split("/")[1:]),), "/")


@pytest.mark.parametrize("root, project, suite, owned", [
    ("C:\\", "C:\\Users\\x\\proj", "C:\\suite", "C:\\Temp\\langadapter_project_ab12"),
    ("/", "/home/swe-bench/pydata__xarray", "/workzone/tests", "/var/tmp/langadapter_project_ab12"),
], ids=["drive", "posix"])
def test_a_suite_sharing_only_the_root_maps_without_a_doubled_separator(
        root, project, suite, owned, monkeypatch):
    """With only the root shared, the copy's paths map back without a
    doubled separator, tracebacks included."""
    import types

    from langadapter._prepared_copy import PreparedCopy
    from langadapter.python._plugins import _PATHS_PLUGIN_TEXT

    separator = "\\" if root.endswith("\\") else "/"
    # Each layout with its own platform's paths, on any host.
    paths = ntpath if separator == "\\" else posixpath
    layout, placed, shared = _mirrored(project, [suite], common=paths.commonpath,
                                       relative=paths.relpath)
    assert shared == root
    prepared_copy = PreparedCopy(types.SimpleNamespace(root=Path(owned)), None, layout=layout,
                                 companions=((suite, placed[0]),), original=shared)
    copied = owned + separator + separator.join(placed[0]) + separator + "test_x.py"
    monkeypatch.setattr(PreparedCopy, "text_map", lambda self: ((owned, root),))
    assert prepared_copy.original_text(f"{copied}:3: in test_a") == (
        f"{suite}{separator}test_x.py:3: in test_a")
    assert prepared_copy.original_text(f"rootdir: {owned}") == f"rootdir: {root}"
    plugin = types.ModuleType("plugin")
    monkeypatch.setenv("FIXTURE_ORIGINAL_PATHS",
                       json.dumps({"cwd": project, "pairs": [[owned, root]]}))
    exec(_PATHS_PLUGIN_TEXT.replace("@PREFIX@", "FIXTURE_"), plugin.__dict__)
    assert plugin._original(copied) == f"{suite}{separator}test_x.py"
    assert plugin._original(owned) == root


def test_a_run_as_delivered_prints_its_files_as_in_place(tmp_path):
    """A run as delivered stages each file in its own directory, and prints
    paths, tracebacks and the summary as the original file, in place and
    in a copy."""
    from dataclasses import replace

    work = _layout(tmp_path, "red")
    context = lb.ProjectContext(str(work / "proj"), lb.Interpreter(path=sys.executable))
    request = SuiteRunRequest(lb.Suite(str(work / "tests"), ("test_x.py",)),
                              timeout_s=120, session_seed=0)
    in_place = lb.create("python", context).tests.run_as_delivered(request)
    owner = prepare_project(context, companions=(str(work / "tests"),))
    try:
        with owner.use() as scoped:
            suite_copy = owner.companion_path(str(work / "tests"))
            copied = lb.create("python", scoped).tests.run_as_delivered(
                replace(request, suite=lb.Suite(suite_copy, ("test_x.py",))))
    finally:
        owner.close()
    for run in (in_place, copied):
        assert "langadapter_" not in run.output
        assert f"FAILED ..{os.sep}tests{os.sep}test_x.py::test_add_wrong" in run.output
    assert _text(copied) == _text(in_place)
