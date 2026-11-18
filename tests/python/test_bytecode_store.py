"""A run in an owned copy reuses bytecode that earlier copies compiled, as a
run in place reuses its own, but never a module compiled from other source
text or by another interpreter."""
import importlib.util
import marshal
import os
import py_compile
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.preparation import prepare_project
from langadapter.python import _bytecode_helper as helper
from langadapter.python._cr_session_runner import _clone_one
from langadapter.python.project import BytecodeStore
from langadapter.runs import FAILED, PASSED, SuiteRunRequest

MODULE = "def sign(x):\n    if x > 0:\n        return 1\n    return -1\n"
TEST = "from mod import sign\n\n\ndef test_sign():\n    assert sign(2) == 1\n"
PLAIN = f"mod.{sys.implementation.cache_tag}.pyc"


@pytest.fixture
def store(tmp_path):
    created = BytecodeStore.create(parent=str(tmp_path), receipt_parent=str(tmp_path))
    yield created
    created.close()


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "mod.py").write_text(MODULE, encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text(TEST, encoding="utf-8")
    return lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable))


def _pycs(root):
    """Each pyc below ``root`` with its time and bytes, so a recompiled module shows."""
    return {path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
            for path in Path(root).rglob("*.pyc")}


def _measured(context, store, inspect=None):
    """The pycs before, the run and the pycs after, in a fresh prepared copy with ``store``."""
    owner = prepare_project(context, bytecode=store)
    try:
        with owner.use() as scoped:
            root = Path(scoped.root)
            before = _pycs(root)
            if inspect is not None:
                inspect(root)
            run = lb.create("python", scoped).tests.run_suite(SuiteRunRequest(
                lb.Suite(str(root / "tests"), ("test_mod.py",)), timeout_s=120))
            return before, run, _pycs(root)
    finally:
        owner.close()


def test_a_later_copy_runs_with_the_bytecode_an_earlier_copy_compiled(tmp_path, store):
    context = _project(tmp_path)
    before, first, compiled = _measured(context, store)
    assert before == {} and first.outcome is lb.Outcome.COMPLETED
    rewritten = [name for name in compiled if "-pytest-" in name]
    assert f"__pycache__/{PLAIN}" in compiled and len(rewritten) == 1

    def located(root):
        # The stored pytest-rewritten module names this copy's test file.
        code = marshal.loads((root / rewritten[0]).read_bytes()[16:])
        assert code.co_filename == str(root / "tests" / "test_mod.py")

    seeded, second, after = _measured(context, store, located)
    assert set(seeded) == set(compiled)
    # Nothing compiled again: the run loaded the stored bytecode.
    assert after == seeded
    assert second.outcome is lb.Outcome.COMPLETED and second.unmatched == ()
    assert [(str(t.test), t.outcome) for t in second.tests] == [("test_mod.py::test_sign", PASSED)]


def test_a_copy_without_a_suite_beside_it_is_seeded_from_one_with(tmp_path, store):
    """A measurement with an outside suite mirrors the project below the owned
    directory (``proj``), and a mutation session's copy (``project``) has none.
    The session's copy still receives the measurement's stored bytecode."""
    context = _project(tmp_path)
    outside = tmp_path / "suite"
    outside.mkdir()
    (outside / "test_outside.py").write_text(TEST, encoding="utf-8")
    owner = prepare_project(context, bytecode=store, companions=(str(outside),))
    try:
        with owner.use() as scoped:
            staged = owner.stage_suite(scoped, lb.Suite(str(outside), ("test_outside.py",)))
            assert Path(staged.root) != outside
            run = lb.create("python", scoped).tests.run_suite(
                SuiteRunRequest(staged, timeout_s=120))
            assert run.outcome is lb.Outcome.COMPLETED, run.reason
    finally:
        owner.close()
    seeded, run, _ = _measured(context, store)
    assert f"__pycache__/{PLAIN}" in seeded
    assert run.outcome is lb.Outcome.COMPLETED


def test_a_changed_module_never_loads_an_old_cache(tmp_path, store):
    """A changed module with the old size and time stamp, which a pyc's own
    check accepts, does not receive the stored bytecode."""
    context = _project(tmp_path)
    _measured(context, store)
    source = Path(context.root) / "mod.py"
    status = source.stat()
    source.write_text(MODULE.replace("return 1", "return 7"), encoding="utf-8")
    os.utime(source, ns=(status.st_atime_ns, status.st_mtime_ns))
    assert source.stat().st_size == status.st_size
    seeded, run, _ = _measured(context, store)
    assert f"__pycache__/{PLAIN}" not in seeded and len(seeded) == 1
    assert [t.outcome for t in run.tests] == [FAILED]


def _compiled_from_other_text(root):
    """``root/mod.py`` with a pyc compiled from other text of the same size
    and time stamp, as a mutant restored within the same second leaves."""
    source = root / "mod.py"
    root.mkdir(parents=True, exist_ok=True)
    source.write_text(MODULE.replace("return 1", "return 7"), encoding="utf-8")
    py_compile.compile(str(source), cfile=importlib.util.cache_from_source(str(source)),
                       doraise=True)
    status = source.stat()
    source.write_text(MODULE, encoding="utf-8")
    os.utime(source, ns=(status.st_atime_ns, status.st_mtime_ns))
    return source


def test_a_module_compiled_from_other_text_is_never_stored(tmp_path):
    entries = tmp_path / "store"
    root = tmp_path / "copy"
    source = _compiled_from_other_text(root)
    assert helper.harvest(str(entries), str(root)) == 0
    py_compile.compile(str(source), cfile=importlib.util.cache_from_source(str(source)),
                       doraise=True)
    assert helper.harvest(str(entries), str(root)) == 1


def test_another_interpreter_never_seeds(tmp_path):
    entries = tmp_path / "store"
    root = tmp_path / "copy"
    root.mkdir()
    source = root / "mod.py"
    source.write_text(MODULE, encoding="utf-8")
    py_compile.compile(str(source), cfile=importlib.util.cache_from_source(str(source)),
                       doraise=True)
    assert helper.harvest(str(entries), str(root)) == 1
    entry, = (entries / PLAIN).iterdir()
    # Another interpreter's magic number under this tag, this entry under another.
    entry.rename(entry.with_name("00000000" + entry.name[8:]))
    other = entries / "mod.cpython-399.pyc"
    other.mkdir()
    (other / entry.name).write_bytes(b"not this interpreter's")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    shutil.copy2(source, fresh / "mod.py")
    assert helper.seed(str(entries), str(fresh)) == 0
    assert not (fresh / "__pycache__").exists()


def test_concurrent_harvests_never_corrupt_the_store(tmp_path):
    entries = tmp_path / "store"
    roots = []
    for index in range(6):
        root = tmp_path / f"copy{index}"
        (root / "pkg").mkdir(parents=True)
        for name in ("a", "b", "c"):
            path = root / "pkg" / f"{name}.py"
            path.write_text(f"VALUE = {name!r}\n" + MODULE, encoding="utf-8")
            py_compile.compile(str(path), cfile=importlib.util.cache_from_source(str(path)),
                               doraise=True)
        roots.append(root)
    text = Path(helper.__file__).read_text(encoding="utf-8")

    def harvest(root):
        return subprocess.run([sys.executable, "-I", "-c", text, "harvest", str(entries),
                               str(root)], capture_output=True, text=True, timeout=120)

    with ThreadPoolExecutor(len(roots)) as pool:
        done = list(pool.map(harvest, roots))
    assert all(result.returncode == 0 for result in done), [r.stderr for r in done]
    stored = sorted(path for path in entries.rglob("*") if path.is_file())
    assert len(stored) == 3 and not [path for path in entries.rglob("*.tmp")]
    for path in stored:
        name = path.parent.name.split(".")[0]
        assert marshal.loads(path.read_bytes()) == compile(
            f"VALUE = {name!r}\n" + MODULE, "x.py", "exec", dont_inherit=True)
    fresh = tmp_path / "fresh"
    shutil.copytree(roots[0], fresh, ignore=shutil.ignore_patterns("__pycache__"))
    assert helper.seed(str(entries), str(fresh)) == 3
    assert subprocess.run([sys.executable, "-B", "-c", "import pkg.a, pkg.b, pkg.c"],
                          cwd=fresh, timeout=60).returncode == 0


def test_a_mutation_clone_of_a_seeded_copy_is_warm(tmp_path, store):
    """A worker clone of a prepared copy keeps its stored bytecode."""
    context = _project(tmp_path)
    _measured(context, store)
    owner = prepare_project(context, bytecode=store)
    try:
        with owner.use() as scoped:
            clone = tmp_path / "clone"
            clone.mkdir()
            _clone_one(Path(scoped.root), clone)
            pyc = clone / "__pycache__" / PLAIN
            assert pyc.read_bytes()[8:16] == helper.stamp(str(clone / "mod.py"))
    finally:
        owner.close()
