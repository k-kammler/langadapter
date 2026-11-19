"""The two observations of a consumer's portability check, with real pytest:
the leaks an instrumented run records, and a test file run as delivered,
without the instrument and with a session seed, in its order or reversed."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.execution import Executor
from langadapter.outcomes import Outcome
from langadapter.python._runs import LeakRecord, read_leak_journal
from langadapter.python.project import _setup_test_env
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import (
    FAILED, PASSED, Leak, PerTestEvidenceRequest, SuiteRunRequest,
)
from langadapter.suites import TestId

#: The determinism instrument as a suite delivered with it carries it.
CARRIED_INSTRUMENT = Path(__file__).with_name("carried_instrument.txt").read_text(
    encoding="utf-8")

TARGET = ('import warnings\n\n\n'
          'class Box:\n    pass\n\n\n'
          'def make_box():\n    return Box()\n\n\n'
          'def add(a, b):\n    return a + b\n\n\n'
          'def warn():\n    warnings.warn("calc warns", UserWarning)\n'
          '    return 1\n')

LEAKS = '''import importlib
import os
import warnings

import calc


def test_reload():
    importlib.reload(calc)


def test_patch(monkeypatch):
    monkeypatch.setattr(calc, "add", lambda a, b: 0)
    assert calc.add(1, 2) == 0


def test_delete():
    del calc.add


def test_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert calc.warn() == 1
    assert "__warningregistry__" in vars(calc)


def test_clean():
    assert calc.add(1, 2) == 3


def test_the_record_exists_only_when_asked(request):
    asked = bool(os.environ.get("LANGADAPTER_LEAK_JOURNAL"))
    assert ("_leak_record" in request.fixturenames) == asked
'''

#: Stand-ins for the two libraries whose state the instrument restores.
NUMPY = '''_ERR = {"divide": "warn", "invalid": "warn", "over": "warn",
        "under": "ignore"}
_PRINT = {"precision": 8, "threshold": 1000}


def geterr():
    return dict(_ERR)


def seterr(**changes):
    old = dict(_ERR)
    _ERR.update(changes)
    return old


def get_printoptions():
    return dict(_PRINT)


def set_printoptions(**changes):
    _PRINT.update(changes)


class random:
    @staticmethod
    def seed(value):
        pass
'''
XARRAY_OPTIONS = '''OPTIONS = {"display_width": 80, "keep_attrs": "default"}


def set_options(**changes):
    OPTIONS.update(changes)
'''
LIBRARIES = '''import numpy
import xarray


def test_option():
    xarray.set_options(display_width=100)


def test_errors():
    numpy.seterr(divide="raise")


def test_printing():
    numpy.set_printoptions(precision=3)


def test_restored():
    assert xarray.OPTIONS["display_width"] == 80
    assert numpy.geterr()["divide"] == "warn"
    assert numpy.get_printoptions()["precision"] == 8
'''

DELIVERED = '''import importlib
import random

import calc
from calc import Box

FIRST_DRAW_AFTER_SEED_0 = 0.8444218515250481


def test_seed_zero():
    assert random.random() == FIRST_DRAW_AFTER_SEED_0


def test_reload():
    importlib.reload(calc)


def test_victim():
    assert isinstance(calc.make_box(), Box)
'''

CLEAN = '''import os
import random

import calc

FIRST_DRAW_AFTER_SEED_1 = 0.13436424411240122


def test_session_seed(request):
    assert os.environ["PYTHONHASHSEED"] == "1"
    assert random.random() == FIRST_DRAW_AFTER_SEED_1
    assert request.config.getoption("hypothesis_seed") == "1"


def test_add():
    assert calc.add(1, 2) == 3
'''

#: A stand-in for hypothesis's pytest plugin: it registers the seed option.
SEED_OPTION = '''
def pytest_addoption(parser):
    parser.addoption("--hypothesis-seed", action="store", default=None)

'''

ORDER = '''STATE = []


def test_first():
    STATE.append(1)


def test_second():
    assert STATE == [1]
'''

#: A project's own setup file that finds the sources in ``../src`` from its
#: own location, and a fixture that reads that location when it runs.
RELATIVE_SETUP = '''"""The sources lie in ../src."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def suite_dir() -> Path:
    return Path(__file__).resolve().parent
'''

RELATIVE = '''from arith import add


def test_add():
    assert add(2, 3) == 5


def test_the_setup_file_lies_beside_the_suite(suite_dir):
    assert suite_dir.name == "tests"
'''


def _adapter(root):
    return PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        test_framework=lb.TestFrameworkConfig("isolated")))


def _public_adapter(root):
    """The public adapter on the same context as ``_adapter``'s."""
    return PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        test_framework=lb.TestFrameworkConfig("isolated")))


def _project(tmp_path, tests):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(TARGET, encoding="utf-8")
    paths = []
    for name, text in tests.items():
        path = root / "tests" / name
        path.write_text(text, encoding="utf-8")
        paths.append(str(path))
    return root, paths


# ---- the leak record -------------------------------------------------------

def test_library_state_left_changed_is_named(tmp_path):
    """The read-only observer names a test that left xarray's options or
    numpy's error or print settings changed."""
    root, _paths = _project(tmp_path, {"test_libraries.py": LIBRARIES})
    (root / "numpy").mkdir()
    (root / "numpy" / "__init__.py").write_text(NUMPY, encoding="utf-8")
    (root / "xarray" / "core").mkdir(parents=True)
    (root / "xarray" / "__init__.py").write_text(
        "from .core.options import OPTIONS, set_options\n", encoding="utf-8")
    (root / "xarray" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "xarray" / "core" / "options.py").write_text(
        XARRAY_OPTIONS, encoding="utf-8")
    run = _public_adapter(root).run_suite(_request(
        lb.Suite(str(root / "tests"), ("test_libraries.py",)),
        module_under_test="calc.py", record_leaks=True))

    def test(name):
        return TestId("test_libraries.py", name, text=f"test_libraries.py::{name}")

    # It restores nothing, so the test after them sees the changed option.
    assert run.counts.passed == 3, run.output
    assert run.outcome_of(test("test_restored")) == FAILED

    assert {leak.test.qualname: leak for leak in run.leaks} == {
        "test_option": Leak(test("test_option"),
                            xarray_options=("display_width",)),
        "test_errors": Leak(test("test_errors"), numpy_errors=("divide",)),
        "test_printing": Leak(test("test_printing"),
                              numpy_print=("precision",)),
    }


def test_no_other_run_records_even_when_the_parent_names_a_journal(
        tmp_path, monkeypatch, carry_instrument):
    """A leak journal the calling process names reaches no run that did
    not ask for the record, also where a suite carries the instrument,
    whose record would otherwise write into it."""
    root, _paths = _project(tmp_path, {"test_leaks.py": LEAKS})
    journal = tmp_path / "parent_journal.jsonl"
    monkeypatch.setenv("LANGADAPTER_LEAK_JOURNAL", str(journal))
    adapter = _public_adapter(root)
    carry_instrument(root / "tests")
    suite = lb.Suite(str(root / "tests"), ("test_leaks.py",), ("conftest.py",))
    evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
        suite=suite, target="calc.py", timeout_s=120,
        module_under_test="calc.py"))
    assert evidence.outcome is Outcome.COMPLETED, evidence.reason
    assert any(entry.time_ms is not None for entry in evidence.tests)
    plain = adapter.run_suite(_request(suite, module_under_test="calc.py"))
    assert plain.outcome is Outcome.COMPLETED and plain.leaks is None
    assert not journal.exists()


def test_a_journal_without_its_mark_is_no_record(tmp_path):
    journal = tmp_path / "leaks.jsonl"
    assert read_leak_journal(str(journal)) is None
    journal.write_text('{"test": "t.py::test_a", "module": '
                       '{"added": ["x"], "rebound": [], "removed": []}}\n',
                       encoding="utf-8")
    assert read_leak_journal(str(journal)) is None
    journal.write_text('{"record": 1}\n' + journal.read_text(encoding="utf-8")
                       + "not json\n", encoding="utf-8")
    assert read_leak_journal(str(journal)) == (
        LeakRecord(test="t.test_a", nodeid="t.py::test_a", added=("x",)),)


# ---- the run as delivered --------------------------------------------------

def _generated_suite(tmp_path, tests, *, head_extra=""):
    """Tests in the layout of a suite delivered with the instrument: a
    setup file holding the import part ``_setup_test_env`` writes, then the
    instrument."""
    root, paths = _project(tmp_path, tests)
    adapter = _adapter(root)
    _setup_test_env(adapter, str(root / "tests"), str(root))
    conftest = root / "tests" / "conftest.py"
    conftest.write_text(conftest.read_text(encoding="utf-8") + CARRIED_INSTRUMENT,
                        encoding="utf-8")
    if head_extra:
        conftest = root / "tests" / "conftest.py"
        text = conftest.read_text(encoding="utf-8")
        marker = text.index("# Deterministic random state for every test.")
        conftest.write_text(text[:marker] + head_extra + text[marker:],
                            encoding="utf-8")
    return adapter, root, paths


def test_a_clean_file_passes_with_the_session_seed(tmp_path):
    _setup, root, _paths = _generated_suite(
        tmp_path, {"test_clean.py": CLEAN}, head_extra=SEED_OPTION)
    suite = lb.Suite(str(root / "tests"), ("test_clean.py",), ("conftest.py",))
    run = _public_adapter(root).run_as_delivered(_request(suite, session_seed=1))
    assert run.outcome is Outcome.COMPLETED, run.output
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("test_session_seed", PASSED), ("test_add", PASSED)]


def _container_suite(tmp_path):
    """A suite in a work directory and an executor that records each command a
    container would run, answering like a passing run and like a CPython
    interpreter to the runs' probes."""
    zone = tmp_path / "wz"
    suite = zone / "tests"
    suite.mkdir(parents=True)
    test = suite / "test_c.py"
    test.write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (suite / "conftest.py").write_text(
        "import sys\nSETUP_FILE = __file__\n"
        "# Deterministic random state for every test.\nimport pytest\n",
        encoding="utf-8")
    calls = []

    class Recording(Executor):
        def run(self, argv, *, cwd=None, env=None, timeout=None, input=None):
            argv = list(argv)
            if argv[0] == "cat" and Path(argv[-1]).name == ".langadapter-owner":
                # The owned work-dir scratch verifies its receipt in the container.
                return subprocess.CompletedProcess(
                    argv, 0, Path(argv[-1]).read_text(encoding="ascii"), "")
            if argv[:3] == ["rm", "-rf", "--"]:
                # The mounted work directory: the container's removal is the host's.
                shutil.rmtree(argv[-1])
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:2] == ["cat", "/testbed/pyproject.toml"] or argv[0] in (
                    "cat", "rm"):
                return subprocess.CompletedProcess(argv, 1, "", "")
            if "-c" in argv and "-m" not in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps(
                    {"implementation": "CPython", "version": "3.12.0",
                     "tools": {}}) + "\n", "")
            report = next(a.split("=", 1)[1] for a in argv
                          if a.startswith("--junit-xml="))
            stage = Path(argv[argv.index("pytest") + 1]).parent
            plugin = Path(report).parent / "delivered_plugin"
            calls.append({
                "argv": argv, "cwd": cwd, "env": dict(env),
                "plugin_dir": str(plugin),
                "staged": sorted(p.name for p in stage.iterdir()),
                "head": (stage / "conftest.py").read_text(encoding="utf-8"),
                "plugin": sorted(p.name for p in plugin.iterdir())})
            Path(report).write_text(
                '<testsuites><testsuite><testcase classname="test_c" '
                'name="test_a"/></testsuite></testsuites>', encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    return zone, test, calls, Recording


def _staged_setup(call) -> dict:
    staged_setup = {"__name__": "conftest", "__file__": "staged"}
    exec(call["head"], staged_setup)
    return staged_setup


def test_the_command_in_a_container(tmp_path):
    """The run as delivered in a container: the staged file, import part and
    plugin lie in the work directory, and the command runs the context's
    interpreter from the project root with the seed and the plugin's
    directory on the import path, without the instrument's variables."""
    zone, test, calls, Recording = _container_suite(tmp_path)
    adapter = PythonAdapter(lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
        execution=lb.ContainerExecution(container="c1", work_dir=str(zone),
                                        container_work_dir="/wz")),
        executor=Recording())
    suite = lb.Suite(str(test.parent), (test.name,), ("conftest.py",))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LANGADAPTER_TARGET_MODULE_FILE", "/testbed/m.py")
        run = adapter.run_as_delivered(
            _request(suite, session_seed=1, reverse_order=True))
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.side_effects.reverse_order
    (call,) = calls
    argv = call["argv"]
    assert argv[:3] == ["/venv/bin/python", "-m", "pytest"]
    staged = Path(argv[3])
    assert staged.name == "test_c.py" and zone in staged.parents
    plugins = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-p"]
    assert "_langadapter_delivered" in plugins
    assert f"--rootdir={staged.parent.as_posix()}" in [
        a.replace("\\", "/") for a in argv]
    assert call["cwd"] == "/testbed"
    env = call["env"]
    assert env["PYTHONHASHSEED"] == "1"
    assert env["LANGADAPTER_DELIVERED_SEED"] == "1"
    assert env["LANGADAPTER_DELIVERED_ORDER"] == "reverse"
    assert "LANGADAPTER_TARGET_MODULE_FILE" not in env
    assert call["plugin_dir"] in env["PYTHONPATH"]
    assert env["PYTHONPATH"].endswith(":/testbed")
    assert call["staged"] == ["conftest.py", "test_c.py"]
    # The staged setup file runs only the import part, located at the suite
    # setup file's path in the container's work directory.
    staged_setup = _staged_setup(call)
    assert staged_setup["SETUP_FILE"] == "/wz/tests/conftest.py"
    assert staged_setup["__file__"] == "staged"
    assert "sys" in staged_setup and "pytest" not in staged_setup
    assert call["plugin"] == ["_langadapter_delivered.py"]


# ---- the same observations on the public suite run ------------------------

def _public(tmp_path, tests):
    root, _paths = _project(tmp_path, tests)
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    return adapter, root, lb.Suite(str(root / "tests"), tuple(tests))


def _request(suite, **fields):
    return SuiteRunRequest(suite=suite, timeout_s=120, **fields)


def test_a_carried_instrument_records_the_leaks_itself(tmp_path, carry_instrument):
    """A suite that carries the instrument records the leaks with it, and
    only on request. The run writes nothing beside the suite."""
    adapter, root, suite = _public(tmp_path, {"test_leaks.py": LEAKS})
    carry_instrument(root / "tests")
    suite = lb.Suite(suite.root, suite.test_files, ("conftest.py",))
    run = adapter.run_suite(_request(suite, module_under_test="calc.py",
                                     record_leaks=True))
    assert run.outcome is Outcome.COMPLETED, run.output
    assert run.leaks == (
        Leak(TestId("test_leaks.py", "test_reload",
                    text="test_leaks.py::test_reload"),
             rebound=("Box", "add", "make_box", "warn")),
        Leak(TestId("test_leaks.py", "test_delete",
                    text="test_leaks.py::test_delete"), removed=("add",)))
    assert run.side_effects.leak_record and run.side_effects.instrument_source == "suite"
    plain = adapter.run_suite(_request(suite, module_under_test="calc.py"))
    assert plain.outcome is Outcome.COMPLETED and plain.leaks is None
    assert not plain.side_effects.leak_record
    # Nothing but the bytecode the runs write.
    assert sorted(p.name for p in (root / "tests").iterdir()
                  if p.name != "__pycache__") == ["conftest.py", "test_leaks.py"]


def test_a_public_run_as_delivered_and_its_reverse(tmp_path):
    adapter, _root, suite = _public(tmp_path, {
        "test_delivered.py": DELIVERED, "test_order.py": ORDER})
    run = adapter.run_suite(_request(suite, session_seed=1))
    assert run.outcome is Outcome.TEST_FAILURES
    assert sorted(t.test.qualname for t in run.tests
                  if t.outcome == FAILED) == ["test_seed_zero", "test_victim"]
    assert run.side_effects.session_seed == 1
    assert run.side_effects.instrument is None
    reverse = adapter.run_suite(_request(suite, reverse_order=True))
    assert reverse.side_effects.reverse_order
    assert [t.test.qualname for t in reverse.tests
            if t.test.file == "test_order.py"] == ["test_second", "test_first"]
    assert reverse.outcome_of(next(
        t.test for t in reverse.tests
        if t.test.qualname == "test_second")) == FAILED


def _leak(name, **fields):
    return Leak(TestId("test_leaks.py", name, text=f"test_leaks.py::{name}"), **fields)


@pytest.mark.parametrize("seed", [1, None])
def test_a_run_without_the_instrument_records_leaks_with_the_read_only_observer(
        tmp_path, seed):
    """Without the instrument, a run (as delivered or not) that asks
    for the leak record loads the read-only observer from the command line.
    It names the same leaks as the instrument's record, restores nothing (the
    test after the removal fails) and writes nothing beside the suite."""
    adapter, root, suite = _public(tmp_path, {"test_leaks.py": LEAKS})
    run = adapter.run_suite(_request(suite, session_seed=seed, module_under_test="calc.py",
                                     record_leaks=True))
    assert run.leaks == (_leak("test_reload", rebound=("Box", "add", "make_box", "warn")),
                         _leak("test_delete", removed=("add",)))
    assert run.side_effects.leak_record and run.side_effects.instrument is None
    assert run.outcome_of(_leak("test_clean").test) == FAILED
    assert run.outcome_of(_leak("test_the_record_exists_only_when_asked").test) == PASSED
    assert sorted(p.name for p in (root / "tests").iterdir()
                  if p.name != "__pycache__") == ["test_leaks.py"]


def test_a_run_as_delivered_takes_the_observer_and_the_journal(tmp_path):
    """The normal run is a run as delivered with the leak record
    and the timeout journal: each staged file's leaks come back named as the
    delivered file's tests, and nothing is written beside the suite."""
    adapter, root, suite = _public(tmp_path, {"test_leaks.py": LEAKS})
    run = adapter.run_as_delivered(_request(
        suite, session_seed=1, module_under_test="calc.py", record_leaks=True,
        timeout_journal=True))
    assert run.leaks == (_leak("test_reload", rebound=("Box", "add", "make_box", "warn")),
                         _leak("test_delete", removed=("add",)))
    assert run.side_effects.leak_record and run.side_effects.timeout_journal
    assert run.side_effects.session_seed == 1 and run.identity.instrument is None
    assert run.outcome_of(_leak("test_clean").test) == FAILED
    assert sorted(p.name for p in (root / "tests").iterdir()
                  if p.name != "__pycache__") == ["test_leaks.py"]
    plain = adapter.run_as_delivered(_request(suite, session_seed=1))
    assert plain.leaks is None and not plain.side_effects.leak_record


def test_the_observer_reads_loaded_libraries_options_and_restores_none(tmp_path):
    root, _paths = _project(tmp_path, {"test_libraries.py": LIBRARIES})
    (root / "numpy").mkdir()
    (root / "numpy" / "__init__.py").write_text(NUMPY, encoding="utf-8")
    (root / "xarray" / "core").mkdir(parents=True)
    (root / "xarray" / "__init__.py").write_text(
        "from .core.options import OPTIONS, set_options\n", encoding="utf-8")
    (root / "xarray" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "xarray" / "core" / "options.py").write_text(
        XARRAY_OPTIONS, encoding="utf-8")
    run = _public_adapter(root).run_suite(_request(
        lb.Suite(str(root / "tests"), ("test_libraries.py",)),
        session_seed=1, module_under_test="calc.py", record_leaks=True))

    def test(name):
        return TestId("test_libraries.py", name, text=f"test_libraries.py::{name}")

    assert run.outcome_of(test("test_restored")) == FAILED, run.output
    assert {leak.test.qualname: leak for leak in run.leaks} == {
        "test_option": Leak(test("test_option"), xarray_options=("display_width",)),
        "test_errors": Leak(test("test_errors"), numpy_errors=("divide",)),
        "test_printing": Leak(test("test_printing"), numpy_print=("precision",)),
    }


QUIET = '''import os

import calc


def test_reads(request):
    assert calc.add(1, 2) == 3


def test_breaks_the_journal():
    journal = os.environ["LANGADAPTER_LEAK_JOURNAL"]
    os.remove(journal)
    os.mkdir(journal)
    calc.add = None


def test_after():
    assert calc.add is None
'''


def test_the_observer_imports_no_library_and_swallows_its_own_failures(tmp_path):
    """E's A3: a library the suite never imports stays unimported (its
    import would write a marker), and a journal the observer cannot write
    changes no verdict."""
    root, _paths = _project(tmp_path, {"test_quiet.py": QUIET})
    marker = tmp_path / "numpy-imported"
    (root / "numpy").mkdir()
    (root / "numpy" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').close()\n" + NUMPY, encoding="utf-8")
    run = _public_adapter(root).run_suite(_request(
        lb.Suite(str(root / "tests"), ("test_quiet.py",)),
        module_under_test="calc.py", record_leaks=True))
    assert run.outcome is Outcome.COMPLETED and run.counts.passed == 3, run.output
    assert not marker.exists()


def test_the_public_observations_refuse_what_they_cannot_observe(tmp_path,
                                                                 carry_instrument):
    """A suite that carries the instrument cannot run as delivered."""
    adapter, root, suite = _public(tmp_path, {"test_leaks.py": LEAKS})
    carry_instrument(root / "tests")
    carried = lb.Suite(suite.root, suite.test_files, ("conftest.py",))
    with pytest.raises(InvalidRequest) as refused:
        adapter.run_suite(_request(carried, session_seed=1))
    assert refused.value.field == "session_seed"
