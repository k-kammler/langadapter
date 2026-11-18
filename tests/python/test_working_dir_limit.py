"""Windows starts no command in a working directory longer than 258
characters, even with long paths enabled. A prepared copy whose project
directory would be longer takes a compact layout, so a run prints what it
prints in place. A copy still too long is refused before anything is
copied, and a command in a directory past the limit is refused by name."""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _host, _process_tree, _prepared_copy
from langadapter._owned_directory import _long_path
from langadapter._prepared_copy import WorkspacePreparationFailed, _compact, _mirrored
from langadapter.preparation import original_text, prepare_project
from langadapter.runs import SuiteRunRequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_copy_text import MODULE, SUITES, _text


def test_the_compact_layout_shortens_only_the_projects_own_dirs():
    # The project's directories below the one it shares with the suite are its own.
    assert _compact(("out", "_work", "a_long_prepared_copy_name", "project"), [("suites",)]) == (
        "p", "p", "p", "project")
    # Shared directories keep their names, and the first shortened directory takes a
    # name its sibling suite directory does not use.
    assert _compact(("home", "out", "deep", "project"), [("home", "p")]) == (
        "home", "q", "p", "project")
    # Nothing of its own to shorten.
    assert _compact(("project",), [("suites",)]) == ("project",)
    assert _compact(("home", "project"), [("home", "suites")]) == ("home", "project")


def _work(tmp_path, suite="red"):
    work = tmp_path / "work"
    project = work / "deep" / ("a" * 20) / ("b" * 20) / "proj"
    project.mkdir(parents=True)
    (work / "tests").mkdir()
    (project / "mod.py").write_text(MODULE, encoding="utf-8")
    (work / "tests" / "test_x.py").write_text(SUITES[suite], encoding="utf-8")
    return work, project


def _lengths(project, tests):
    """The project copy's directory as mirrored and as compact, by length."""
    layout, placed, _ = _mirrored(str(project.resolve()), [str(tests.resolve())])
    owned = os.path.join(_long_path(os.path.abspath(tempfile.gettempdir())),
                         "langadapter_project_" + "x" * 8)
    return len(os.path.join(owned, *layout)), len(os.path.join(owned, *_compact(layout, placed)))


def _limit(monkeypatch, limit):
    monkeypatch.setattr(_prepared_copy, "_host", SimpleNamespace(working_dir_limit=lambda: limit))


def test_a_copy_past_the_limit_prints_what_an_in_place_run_prints(tmp_path, monkeypatch):
    work, project = _work(tmp_path)
    mirrored, compact = _lengths(project, work / "tests")
    assert compact < mirrored
    context = lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable))
    in_place = lb.create("python", context).tests.run_suite(SuiteRunRequest(
        lb.Suite(str(work / "tests"), ("test_x.py",)), timeout_s=120))
    _limit(monkeypatch, (mirrored + compact) // 2)
    owner = prepare_project(context, companions=(str(work / "tests"),))
    try:
        with owner.use() as scoped:
            assert Path(scoped.root).parts[-4:] == ("p", "p", "p", "proj")
            assert len(scoped.root) <= (mirrored + compact) // 2
            assert original_text(scoped, str(Path(scoped.root) / "mod.py")) == str(project / "mod.py")
            copied = lb.create("python", scoped).tests.run_suite(SuiteRunRequest(
                lb.Suite(owner.companion_path(str(work / "tests")), ("test_x.py",)), timeout_s=120))
    finally:
        owner.close()
    assert copied.outcome is in_place.outcome
    assert _text(copied) == _text(in_place)
    assert [str(t.test) for t in copied.tests] == [str(t.test) for t in in_place.tests]
    assert [t.message for t in copied.tests] == [t.message for t in in_place.tests]
    relative = os.path.join(*[".."] * 4, "tests", "test_x.py")
    assert f"FAILED {relative}::test_add_wrong" in copied.output


def test_a_copy_within_the_limit_keeps_the_mirrored_layout(tmp_path, monkeypatch):
    work, project = _work(tmp_path)
    mirrored, _ = _lengths(project, work / "tests")
    _limit(monkeypatch, mirrored)
    owner = prepare_project(lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable)),
                            companions=(str(work / "tests"),))
    try:
        with owner.use() as scoped:
            assert Path(scoped.root).parts[-4:] == ("deep", "a" * 20, "b" * 20, "proj")
    finally:
        owner.close()


def test_a_copy_still_past_the_limit_is_refused_before_anything_is_copied(tmp_path, monkeypatch):
    work, project = _work(tmp_path)
    _, compact = _lengths(project, work / "tests")
    _limit(monkeypatch, compact - 1)
    copies = tmp_path / "copies"
    copies.mkdir()
    with pytest.raises(WorkspacePreparationFailed) as refused:
        prepare_project(lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable)),
                        companions=(str(work / "tests"),), parent=str(copies))
    message = str(refused.value)
    layout, placed, _ = _mirrored(str(project.resolve()), [str((work / "tests").resolve())])
    below = os.path.join(_long_path(os.path.abspath(copies)), "langadapter_project_" + "x" * 8,
                         *_compact(layout, placed))
    assert f"would be {len(below)} characters long" in message
    assert f"longer than {compact - 1} characters" in message
    assert "choose a shorter directory for the copy (parent=, or a shorter TEMP)" in message
    assert list(copies.iterdir()) == []


def test_a_command_in_a_dir_past_the_limit_is_refused_by_name(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("the limit is Windows'")
    monkeypatch.setattr(_host, "working_dir_limit", lambda: len(str(tmp_path)) - 1)
    with pytest.raises(OSError) as refused:
        _process_tree.run([sys.executable, "-c", "pass"], cwd=str(tmp_path), timeout=60)
    assert str(refused.value) == (
        f"the working directory {tmp_path} is {len(str(tmp_path))} characters long, and Windows "
        f"starts no command in a directory longer than {len(str(tmp_path)) - 1} characters, even "
        "with long paths enabled: run from a shorter directory")


def test_windows_refuses_a_real_working_dir_past_258_characters_by_name(tmp_path):
    if os.name != "nt":
        pytest.skip("the limit is Windows'")
    long_dir = str(tmp_path)
    while len(long_dir) < 259:
        long_dir = os.path.join(long_dir, "d" * min(30, 258 - len(long_dir)))
    assert len(long_dir) == 259
    os.makedirs("\\\\?\\" + long_dir)
    within = os.path.dirname(long_dir)
    assert _process_tree.run([sys.executable, "-c", "pass"], cwd=within, timeout=60).returncode == 0
    with pytest.raises(OSError, match="is 259 characters long, and Windows starts no command"):
        _process_tree.run([sys.executable, "-c", "pass"], cwd=long_dir, timeout=60)
