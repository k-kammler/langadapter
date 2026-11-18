"""A configure build outside the source tree, kept for later adapters."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import langadapter as la
from langadapter.runs import SuiteRunRequest

from conftest import needs_gcc

pytestmark = needs_gcc

TESTS = """\
#include "utest.h"
#include "calc.h"

UTEST(calc, adds) {
    EXPECT_EQ(5, add(2, 3));
}

UTEST_MAIN()
"""


def _run(adapter, suite):
    return adapter.run_suite(SuiteRunRequest(suite, timeout_s=180, coverage_target="src/calc.c"))


def test_configure_runs_outside_the_source_tree(project, adapter, suite):
    root = project("calc_configure")
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    run = _run(adapter(root), suite(TESTS))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert set(run.coverage.covered) == {3, 4}
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


def test_a_kept_build_is_used_again(project, adapter, suite, tmp_path):
    root = project("calc_configure")
    cache = tmp_path / "builds"
    tests = suite(TESTS)
    assert _run(adapter(root, c_build_cache=str(cache)), tests).outcome is la.Outcome.COMPLETED
    [scratch_dir] = cache.iterdir()
    record = scratch_dir / "record.jsonl"
    recorded = record.read_text()
    again = _run(adapter(root, c_build_cache=str(cache)), tests)
    assert again.outcome is la.Outcome.COMPLETED, again.reason_text(None)
    assert record.read_text() == recorded
    assert (scratch_dir / "built").is_file()


def test_an_interrupted_copy_of_a_kept_build_is_made_again(project, adapter, suite, tmp_path,
                                                         monkeypatch):
    """A snapshot copy that stopped part way (a full disk) is not the
    snapshot: the next adapter copies the tree again and builds."""
    root = project("calc_configure")
    cache = tmp_path / "builds"
    tests = suite(TESTS)
    copy = shutil.copytree

    def stopped(source, destination, *positional, **options):
        # copytree calls itself for each directory: only the first call stops.
        monkeypatch.setattr(shutil, "copytree", copy)
        copy(source, destination, *positional, **options)
        (Path(destination) / "src" / "calc.c").unlink()
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", stopped)
    with pytest.raises(OSError):
        _run(adapter(root, c_build_cache=str(cache)), tests)
    monkeypatch.undo()
    run = _run(adapter(root, c_build_cache=str(cache)), tests)
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    [scratch_dir] = cache.iterdir()
    assert (scratch_dir / "source" / "src" / "calc.c").is_file()


def test_a_kept_build_serves_every_copy_of_the_tree(project, adapter, suite, tmp_path):
    root = project("calc_configure")
    # A directory named build may hold the build's own scripts (CPython's Tools/build).
    (root / "tools" / "build").mkdir(parents=True)
    (root / "tools" / "build" / "step.txt").write_text("kept\n")
    cache = tmp_path / "builds"
    tests = suite(TESTS)
    first = tmp_path / "a-copy"
    shutil.copytree(root, first)
    assert _run(adapter(first, c_build_cache=str(cache)), tests).outcome is la.Outcome.COMPLETED
    # The tree it was built from is gone, as a consumer's temporary copy goes.
    shutil.rmtree(first)
    [scratch_dir] = cache.iterdir()
    recorded = (scratch_dir / "record.jsonl").read_text()
    assert (scratch_dir / "source" / "tools" / "build" / "step.txt").is_file()
    run = _run(adapter(root, c_build_cache=str(cache)), tests)
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert set(run.coverage.covered) == {3, 4}
    assert list(cache.iterdir()) == [scratch_dir]
    assert (scratch_dir / "record.jsonl").read_text() == recorded
    # Another tree, with a changed source, gets a build of its own.
    target = root / "src" / "calc.c"
    target.write_text(target.read_text() + "\n")
    assert _run(adapter(root, c_build_cache=str(cache)), tests).outcome is la.Outcome.COMPLETED
    assert len(list(cache.iterdir())) == 2
