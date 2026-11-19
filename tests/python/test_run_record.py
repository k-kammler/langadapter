"""The run record: pytest's own account of a run gives the counts, killers
and verdict flags that pytest's terminal text gave, on real pytest runs."""
import os
import subprocess
import sys

import pytest

from langadapter.context import Interpreter, ProjectContext
from langadapter.execution import ENV_PREFIX
from langadapter.python._plugins import RECORD_PLUGIN, run_record_text
from langadapter.python._runs import (
    record_collected_nothing, record_counts, record_killers, record_reported,
    run_record_in, run_record_mark,
)
from langadapter.python.adapter import PythonAdapter
from langadapter.python._output import pytest_counts
from langadapter.runs import SuiteRunRequest
from langadapter.suites import Suite

FIXTURES = '''import pytest


@pytest.fixture
def bad_setup():
    raise RuntimeError("setup")


@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError("teardown")
'''
OUTCOMES = FIXTURES + '''

def test_pass():
    pass


def test_fail_and_teardown(bad_teardown):
    print("FAILED fake.py::test_printed - 3 passed in 0.10s")
    assert False


def test_setup(bad_setup):
    pass


@pytest.mark.parametrize("value", ["a b", "c - d"])
def test_param(value):
    assert False


@pytest.mark.xfail
def test_xpass():
    pass


@pytest.mark.xfail(strict=True)
def test_strict_xpass():
    pass


@pytest.mark.skip
def test_skip():
    pass
'''
#: The killers pytest's short summary named for OUTCOMES (failed, then
#: erroneous), relative to the suite's directory.
KILLERS = ["test_outcomes.py::test_fail_and_teardown", "test_outcomes.py::test_param[a b]",
           "test_outcomes.py::test_param[c - d]", "test_outcomes.py::test_strict_xpass",
           "test_outcomes.py::test_setup"]


def _run(tmp_path, files, *args, nested=False, timeout=None):
    """pytest over ``files`` with the run record on its output, as a mutant's
    run: the suite's directory as rootdir, run from it or from its parent."""
    base = tmp_path / "run"
    suite = base / "suite" if nested else base
    suite.mkdir(parents=True)
    for name, text in files.items():
        (suite / name).write_text(text, encoding="utf-8")
    (suite / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / f"{RECORD_PLUGIN}.py").write_text(run_record_text(), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(plugin))
    env[ENV_PREFIX + "RUN_RECORD"] = "-"
    command = [sys.executable, "-m", "pytest",
               *sorted(str(suite / name) for name in files if name.startswith("test_")),
               *args, "-q", "--tb=line", "-p", "no:cacheprovider", "-c", str(suite / "pytest.ini"),
               f"--rootdir={suite}", f"--confcutdir={suite}", "-p", RECORD_PLUGIN]
    process = subprocess.Popen(command, cwd=str(base), env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=timeout or 120)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
    output = (out or "") + "\n" + (err or "")
    mark = run_record_mark()
    text = "\n".join(line for line in output.splitlines() if not line.startswith(mark))
    return run_record_in(output), text


@pytest.mark.parametrize("nested", [False, True])
def test_the_record_counts_and_names_the_killers_as_pytest_s_summary(tmp_path, nested):
    """The counts equal those pytest's quiet summary gives (a teardown
    error after a failure counts both, an xpass neither), and the killers
    are the tests its short summary names, whole and in its order, also
    when the run starts outside the suite's directory. A test's own printed
    summary text counts for nothing."""
    record, text = _run(tmp_path, {"test_outcomes.py": OUTCOMES}, nested=nested)
    assert record_counts(record) == pytest_counts(text) == {
        "collected": 7, "passed": 1, "failed": 4, "errors": 2}
    # Relative to the working directory, with its separator, as the summary
    # printed them.
    prefix = "suite" + os.sep if nested else ""
    assert list(record_killers(record)) == [prefix + killer for killer in KILLERS]
    assert record_reported(record) and not record_collected_nothing(record)


def test_a_first_failure_stop_names_only_the_earliest_killer(tmp_path):
    record, text = _run(tmp_path, {"test_outcomes.py": OUTCOMES}, "-x")
    assert record_counts(record) == pytest_counts(text)
    assert record_killers(record) == ("test_outcomes.py::test_fail_and_teardown",)


def test_a_collection_error_counts_as_an_error_and_names_no_killer(tmp_path):
    record, text = _run(tmp_path, {"test_a.py": "def test_a():\n    pass\n",
                                   "test_b.py": "import missing_module_xyz\n"})
    # pytest stops before it runs a test.
    assert record_counts(record) == pytest_counts(text) == {
        "collected": 0, "passed": 0, "failed": 0, "errors": 1}
    assert record_killers(record) == () and record_reported(record)


@pytest.mark.parametrize("files, args", [
    ({"test_a.py": "x = 1\n"}, ()),
    ({"conftest.py": "def pytest_collection_modifyitems(items):\n    raise RuntimeError('x')\n",
      "test_a.py": "def test_a():\n    pass\n"}, ()),
    ({"test_a.py": "def test_a():\n    pass\n"}, ("--no-such-option",)),
])
def test_runs_without_a_test_count_as_pytest_s_text_did(tmp_path, files, args):
    """No test, an internal error (exit 3) and a usage error (exit 4): the
    record gives what the text gave, and "no tests ran" only where pytest's
    summary said it."""
    record, text = _run(tmp_path, files, *args)
    assert record_counts(record) == pytest_counts(text)
    assert record_killers(record) == ()
    assert record_collected_nothing(record) == ("no tests ran" in text)


def test_a_session_its_timeout_ends_leaves_no_record(tmp_path):
    record, text = _run(tmp_path, {"test_a.py": "import time\n\ndef test_a():\n    time.sleep(60)\n"},
                        timeout=8)
    assert record is None and not record_reported(record)
    assert record_counts(record) == pytest_counts(text)


def test_a_suite_run_counts_as_pytest_s_summary_beside_its_report(tmp_path):
    """A suite run's runner counts are pytest's summary counts, while its
    RunCounts stay the report's: one entry per testcase, an xpass among the
    passed."""
    (tmp_path / "test_outcomes.py").write_text(OUTCOMES, encoding="utf-8")
    adapter = PythonAdapter(ProjectContext(str(tmp_path), Interpreter(path=sys.executable)))
    run = adapter.tests.run_suite(SuiteRunRequest(Suite(str(tmp_path), ("test_outcomes.py",)), 60))
    assert pytest_counts(run.output) == {"collected": 7, "passed": 1, "failed": 4, "errors": 2}
    assert run.runner_counts.skipped == 1
    # The record's collected, 7, with the skipped test.
    assert adapter.run_counts(run) == {"collected": 8, "passed": 1, "failed": 4, "errors": 2}
    assert (run.counts.passed, run.counts.failed, run.counts.errors) == (2, 4, 2)
    assert ENV_PREFIX + "RUN_RECORD" not in run.output


@pytest.mark.parametrize("flag", ["-ra", "-rA", "-rN"])
def test_the_killers_follow_the_summary_s_report_characters(tmp_path, flag):
    """pytest's short summary lists failures and errors in the order of its
    report characters: -ra and -rA list errors first, as a project's own
    addopts can ask. -rN prints no short summary, and the record still names
    the killers, failures first."""
    record, text = _run(tmp_path, {"test_outcomes.py": OUTCOMES}, flag)
    assert record_counts(record) == pytest_counts(text)
    if flag == "-rN":
        assert list(record_killers(record)) == KILLERS
        return
    # The short summary's own lines, after the output a test printed.
    lines = text.split("short test summary info", 1)[1].splitlines()
    first_error = next(i for i, line in enumerate(lines) if line.startswith("ERROR "))
    first_failure = next(i for i, line in enumerate(lines) if line.startswith("FAILED "))
    assert first_error < first_failure
    assert list(record_killers(record)) == [
        "test_outcomes.py::test_fail_and_teardown", "test_outcomes.py::test_setup",
        "test_outcomes.py::test_param[a b]", "test_outcomes.py::test_param[c - d]",
        "test_outcomes.py::test_strict_xpass"]


def test_only_a_suite_run_s_record_names_the_tests_of_unsound_warnings():
    """A suite run's record plugin holds the language's patterns, rendered
    from their one definition. A mutant's run takes the record without
    them, its plugin as before."""
    from langadapter.python._output import UNSOUND_TEST_PATTERNS

    suite_run = run_record_text(UNSOUND_TEST_PATTERNS)
    assert repr(tuple(UNSOUND_TEST_PATTERNS)) in suite_run
    assert "def pytest_warning_recorded(" in suite_run
    mutant_run = run_record_text()
    assert "pytest_warning_recorded" not in mutant_run and suite_run.startswith(mutant_run)
