"""Suite runs of the Python adapter with real pytest and coverage.py in the
calling interpreter: every outcome from a small real fixture, the per-test
outcomes by identifier, the coverage observation, the setup files, and the
side effects, which an ordinary run does not apply. Reports of a controlled
runner keep their execution failures and owned diagnostics."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.execution import LocalExecutor
from langadapter.outcomes import Outcome
from langadapter.python import _project_files
from langadapter.execution import ENV_PREFIX
from langadapter.python._plugins import JOURNAL_PLUGIN, instrument_identity
from langadapter.python._runs import PytestOptions, _SuiteRunner
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import (
    ERROR, FAILED, PASSED, SKIPPED, XFAILED, CoverageObservation,
    BenchmarkRequest, PerTestEvidenceRequest, SuiteRunRequest, with_detail,
)
from langadapter.suites import BranchItem, TestId
from langadapter import execution

TARGET = ("def sign(x):\n"
          "    if x > 0:\n"
          "        return 1\n"
          "    return -1\n")


def _project(tmp_path, tests, *, setup=None):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "sign.py").write_text(TARGET, encoding="utf-8")
    for name, text in tests.items():
        path = root / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for name, text in (setup or {}).items():
        (root / "tests" / name).write_text(text, encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(root / "tests"), tuple(tests), tuple(setup or ()))
    return adapter, root, suite


def _tree(root):
    """Every file with its modification time, and every directory, without
    bytecode caches."""
    return {p.relative_to(root).as_posix(): (p.stat().st_mtime_ns if p.is_file() else None)
            for p in root.rglob("*") if "__pycache__" not in p.relative_to(root).parts}


def _written(paths):
    """The files a run removed, without the bytecode an earlier run wrote."""
    return tuple(path for path in paths if "__pycache__" not in path.split("/"))


def _run(adapter, suite, **fields):
    fields.setdefault("timeout_s", 120)
    return adapter.run_suite(SuiteRunRequest(suite=suite, **fields))


GREEN = ("from sign import sign\n\n\n"
         "def test_positive():\n    assert sign(2) == 1\n\n\n"
         "def test_negative():\n    assert sign(-2) == -1\n")


def test_a_green_run_reports_every_test_and_writes_nothing(tmp_path):
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    before = _tree(root)
    run = _run(adapter, suite)
    assert _tree(root) == before
    assert run.outcome is Outcome.COMPLETED and run.reason == ""
    assert [(str(t.test), t.outcome) for t in run.tests] == [
        ("test_sign.py::test_positive", PASSED),
        ("test_sign.py::test_negative", PASSED)]
    assert (run.counts.collected, run.counts.passed, run.counts.failed,
            run.counts.errors, run.counts.skipped) == (2, 2, 0, 0, 0)
    assert run.wall_time_s > 0 and run.unmatched == ()
    assert run.coverage is None and run.identity.target is None
    assert run.coverage_report_s is None and run.coverage_statements_s is None
    assert [f.path for f in run.identity.suite] == ["test_sign.py"]
    assert run.identity.instrument is None
    effects = run.side_effects
    assert (effects.instrument, effects.files_written,
            effects.files_removed) == (None, (), ())


#: A setup file that makes pytest-benchmark's default storage in the run's
#: working directory when pytest configures, as that plugin does.
STORAGE = ("import os\n\n\n"
           "def pytest_configure(config):\n"
           "    os.makedirs('.benchmarks', exist_ok=True)\n")
#: The same, and a saved result in it, as the plugin's autosave writes.
SAVED_STORAGE = STORAGE + "    open(os.path.join('.benchmarks', 'saved.json'), 'w').close()\n"


def test_a_run_removes_the_empty_benchmark_storage_it_created(tmp_path):
    """pytest-benchmark makes .benchmarks in the working directory whenever
    pytest configures. A run removes it when the run made it and it is
    empty, so the run writes nothing into the project."""
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN},
                                    setup={"conftest.py": STORAGE})
    before = _tree(root)
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert _tree(root) == before


def test_a_benchmark_storage_the_run_found_or_filled_stays(tmp_path):
    """A storage that was there before the run, or that holds a saved
    result, is the project's and stays."""
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN},
                                    setup={"conftest.py": STORAGE})
    (root / ".benchmarks").mkdir()
    assert _run(adapter, suite).outcome is Outcome.COMPLETED
    assert (root / ".benchmarks").is_dir()
    adapter, root, suite = _project(tmp_path / "saved", {"test_sign.py": GREEN},
                                    setup={"conftest.py": SAVED_STORAGE})
    assert _run(adapter, suite).outcome is Outcome.COMPLETED
    assert [path.name for path in (root / ".benchmarks").iterdir()] == ["saved.json"]


def test_a_run_without_the_storage_starts_only_its_own_commands(tmp_path, monkeypatch):
    """On the host the storage check starts no process: a run that makes no
    storage runs the commands it ran before and removes nothing."""
    commands = []
    started = LocalExecutor.run
    monkeypatch.setattr(LocalExecutor, "run", lambda self, argv, **options: (
        commands.append(list(argv)) or started(self, argv, **options)))
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    before = _tree(root)
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED and _tree(root) == before
    assert run.side_effects.files_removed == ()
    assert [command for command in commands if "-c" in command
            and any(_project_files.BENCHMARK_STORAGE in str(part) or "rmdir" in str(part)
                    for part in command)] == []
    assert sum(any(command[i:i + 2] == ["-m", "pytest"] for i in range(len(command)))
               for command in commands) == 1


def test_in_a_container_the_storage_is_checked_and_removed_by_the_interpreter(
        tmp_path, monkeypatch):
    """In a container the project's interpreter checks and removes the
    storage, here on a host path, as it would on the container's."""
    adapter, root, _ = _project(tmp_path, {"test_sign.py": GREEN})
    monkeypatch.setattr(_project_files, "in_container", lambda context: True)
    assert _project_files.storage_exists(adapter) is False
    (root / ".benchmarks").mkdir()
    _project_files.remove_created_storage(adapter, False)
    assert not (root / ".benchmarks").exists()
    (root / ".benchmarks" / "kept").mkdir(parents=True)
    _project_files.remove_created_storage(adapter, False)
    assert _project_files.storage_exists(adapter) is True
    assert (root / ".benchmarks" / "kept").is_dir()


@pytest.mark.parametrize("changed", ["target", "test file"])
def test_a_same_size_rewrite_in_the_cached_second_runs_the_new_code(tmp_path, changed):
    """CPython reuses a compiled module, and pytest a rewritten test module,
    whose recorded size and modification time match the source. A rewrite of
    the same size that keeps the time runs the new code in the next run and
    the next benchmark."""
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    target, test = root / "calc.py", root / "tests" / "test_calc.py"
    target.write_text("X = 10\n", encoding="utf-8")
    test.write_text("from calc import X\n\n\ndef test_x():\n    assert X == 10\n",
                    encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(root), ("tests/test_calc.py",))
    assert _run(adapter, suite).outcome is Outcome.COMPLETED
    path = target if changed == "target" else test
    recorded = path.stat()
    path.write_text(path.read_text(encoding="utf-8").replace("10", "20"), encoding="utf-8")
    os.utime(path, ns=(recorded.st_atime_ns, recorded.st_mtime_ns))
    run = _run(adapter, suite)
    assert run.outcome is Outcome.TEST_FAILURES, run.reason
    timed = adapter.benchmark(BenchmarkRequest(suite, runs=1, timeout_s=120, warmup_runs=0))
    assert timed.outcome is Outcome.TEST_FAILURES, timed.reason


@pytest.mark.parametrize("operation", ["suite", "coverage", "evidence"])
def test_a_prepared_copy_leaves_a_copied_test_cache_behind(tmp_path, operation):
    """pytest reuses a cached test module whose recorded size and
    modification time match. A run compiles into a bytecode cache of its own,
    so a copied stale cache decides neither the run in place nor the run in a
    prepared copy, which leaves bytecode caches out: both run the file's own
    tests."""
    original = tmp_path / "original" / "test_sign.py"
    original.parent.mkdir()
    original.write_text(GREEN, encoding="utf-8")
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    env = dict(os.environ, PYTHONPATH=str(root), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    compiled = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(original)],
        cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    assert list((original.parent / "__pycache__").glob("*-pytest-*.pyc"))
    # The project's file renames a test, keeping the size and modification
    # time the cache records.
    current = root / "tests" / original.name
    current.write_text(GREEN.replace("test_positive", "test_pozitive"), encoding="utf-8")
    recorded = original.stat()
    os.utime(current, ns=(recorded.st_atime_ns, recorded.st_mtime_ns))
    shutil.copytree(original.parent / "__pycache__", root / "tests" / "__pycache__")

    def measure(selected, selected_suite):
        if operation == "evidence":
            return selected.per_test_evidence(PerTestEvidenceRequest(
                selected_suite, "sign.py", timeout_s=60, warmup=False))
        return _run(selected, selected_suite, coverage_target=(
            "sign.py" if operation == "coverage" else None))

    in_place = measure(adapter, suite)
    assert in_place.outcome is Outcome.COMPLETED
    assert {str(test.test) for test in in_place.tests} == {
        "test_sign.py::test_pozitive", "test_sign.py::test_negative"}

    prepared = lb.prepare_project(adapter.context)
    with prepared.use() as context:
        assert not (Path(context.root) / "tests" / "__pycache__").exists()
        run = measure(lb.create("python", context),
                      lb.Suite(str(Path(context.root) / "tests"), ("test_sign.py",)))
    prepared.close()
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert {str(test.test) for test in run.tests} == {
        "test_sign.py::test_pozitive", "test_sign.py::test_negative"}
    assert (run.unmatched_contexts if operation == "evidence" else run.unmatched) == ()
    if operation == "evidence":
        assert all(test.time_ms is not None and test.items for test in run.tests)
        assert run.untimed == ()


def test_a_run_without_a_bytecode_store_compiles_outside_the_project(tmp_path):
    """A run in the project directory compiles into a cache directory of its own
    (PYTHONPYCACHEPREFIX): neither the project's module nor pytest's
    rewritten test leaves a cache in the project."""
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert not list(root.rglob("*.pyc")) and not list(root.rglob("__pycache__"))


def test_a_red_suite_names_its_failing_tests(tmp_path):
    red = ("def test_one():\n    assert 1 == 2\n\n\n"
           "def test_two():\n    raise ValueError('boom')\n\n\n"
           "def test_three():\n    pass\n")
    adapter, _root, suite = _project(tmp_path, {"test_red.py": red})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.TEST_FAILURES
    assert [str(t) for t in run.failing_tests()] == [
        "test_red.py::test_one", "test_red.py::test_two"]
    assert run.counts.failed == 2 and run.counts.passed == 1
    # A failure's trace is its report's text, the stack included.
    one, two, three = run.tests
    assert "assert 1 == 2" in one.trace and "test_red.py" in one.trace
    assert "ValueError: boom" in two.trace
    assert three.trace == "" and three.exception == ""


def test_a_runner_that_cannot_start_reports_no_test_verdict(tmp_path):
    adapter, _root, suite = _project(tmp_path, {"test_sign.py": GREEN})

    class CannotStart(LocalExecutor):
        def run(self, command, *args, **kwargs):
            if "pytest" in command:
                raise FileNotFoundError("runner unavailable")
            return super().run(command, *args, **kwargs)

    adapter.executor = CannotStart()
    run = _run(adapter, suite)
    assert run.outcome == Outcome.INFRASTRUCTURE_FAILURE
    assert "runner unavailable" in run.reason
    assert run.tests == () and run.counts.collected is None
    assert run.coverage is None and run.wall_time_s is None


def test_a_tests_own_line_like_the_report_line_stays(tmp_path):
    """Only the line naming this run's own report goes, not a test's
    output shaped like it."""
    lookalike = "----- generated xml file: /elsewhere/report.xml -----"
    adapter, _root, suite = _project(tmp_path, {"test_print.py": (
        "def test_prints():\n"
        f"    print({lookalike!r})\n"
        "    assert False\n")})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.TEST_FAILURES
    assert lookalike in run.output
    assert sum("generated xml file" in line for line in run.output.splitlines()) == 1


def test_a_suite_run_takes_the_commands_own_duration(tmp_path):
    """Where the executor reports the command's own duration, that is the
    run's time, which sets the per-mutant timeout, without the supervisor's
    start and stop."""
    adapter, _root, suite = _project(tmp_path, {"test_sign.py": GREEN})

    class Timed(LocalExecutor):
        def run(self, command, *args, **kwargs):
            result = super().run(command, *args, **kwargs)
            if "pytest" in command:
                result.native_elapsed_s = 0.123
            return result

    adapter.executor = Timed()
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED and run.wall_time_s == 0.123


def test_the_whole_output_is_kept_without_the_report_line(tmp_path):
    """The output is kept whole, without the line naming the adapter's
    JUnit report."""
    adapter, _root, suite = _project(tmp_path, {
        "test_warn.py": "import warnings\n"
        "def test_warning():\n"
        "    warnings.warn(\"coroutine 'f' was never awaited\", RuntimeWarning)\n"})
    seen = []

    class PaddedOutput(LocalExecutor):
        def run(self, command, *args, **kwargs):
            result = super().run(command, *args, **kwargs)
            if "pytest" in command:
                result.stdout = "H" * 6000 + "\n" + result.stdout + "\n" + "T" * 6000
                seen.append(result.stdout + "\n" + (result.stderr or ""))
            return result

    adapter.executor = PaddedOutput()
    run = _run(adapter, suite)
    assert run.outcome == Outcome.COMPLETED and run.returncode == 0
    whole, = seen
    report_line, = [line for line in whole.splitlines(keepends=True)
                    if "generated xml file" in line]
    assert run.output == whole.replace(report_line, "")
    diagnostic, = run.diagnostics
    assert diagnostic.kind == "unsound_test"
    assert diagnostic.test == run.tests[0].test
    assert diagnostic.native_test == "test_warn.py::test_warning"
    assert diagnostic.message == "coroutine 'f' was never awaited"


# A coroutine never awaited, destroyed in leave(): every test that calls it
# leaves the same warning, from the same line.
LEAVES = ("async def pending():\n    pass\n\n\n"
          "async def earlier():\n    pass\n\n\n"
          "def leave():\n    pending()\n\n\n")


def _unsound(run):
    """The run's unsound_test entries as (node id, warning), after checking
    that each names the run's own test of its node id, or none."""
    tests = {outcome.test.text: outcome.test for outcome in run.tests}
    entries = [item for item in run.diagnostics if item.kind == "unsound_test"]
    assert [item.test for item in entries] == [tests.get(item.native_test) for item in entries]
    return [(item.native_test, item.message) for item in entries]


def test_two_tests_leaving_one_warning_are_both_named(tmp_path):
    """pytest's warnings summary lists the tests of one warning above it, so
    reading the summary named only the last. The run's record names each."""
    adapter, _root, suite = _project(tmp_path, {"test_warn.py": LEAVES +
        "def test_first():\n    leave()\n\n\n"
        "def test_second():\n    leave()\n"})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED
    assert _unsound(run) == [
        ("test_warn.py::test_first", "coroutine 'pending' was never awaited"),
        ("test_warn.py::test_second", "coroutine 'pending' was never awaited")]


def test_eleven_tests_after_an_earlier_one_are_named_and_the_earlier_is_not(tmp_path):
    """From ten tests on, pytest's summary counts a warning's tests per file
    and names none, so reading it kept the earlier warning's test."""
    adapter, _root, suite = _project(tmp_path, {"test_warn.py": "import pytest\n\n\n" + LEAVES +
        "def test_earlier():\n    earlier()\n\n\n"
        "@pytest.mark.parametrize('n', range(11))\n"
        "def test_many(n):\n    leave()\n"})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED
    assert _unsound(run) == [
        ("test_warn.py::test_earlier", "coroutine 'earlier' was never awaited"),
        *((f"test_warn.py::test_many[{n}]", "coroutine 'pending' was never awaited")
          for n in range(11))]


def test_an_exception_raised_nowhere_in_a_teardown_names_its_test(tmp_path):
    adapter, _root, suite = _project(tmp_path, {"test_warn.py": "import pytest\n\n\n"
        "class Lost:\n    def __del__(self):\n        raise ValueError('lost')\n\n\n"
        "@pytest.fixture\ndef leaves():\n    yield\n    Lost()\n\n\n"
        "def test_clean():\n    pass\n\n\n"
        "def test_teardown(leaves):\n    pass\n"})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED
    assert _unsound(run) == [("test_warn.py::test_teardown", "PytestUnraisableExceptionWarning")]


def test_one_test_per_warning_is_named_as_the_summary_named_it(tmp_path):
    """Where the summary named every test right, the entries are the same:
    warnings in the order they first appear, each with its test."""
    adapter, _root, suite = _project(tmp_path, {"test_warn.py": LEAVES +
        "def test_first():\n    earlier()\n\n\n"
        "def test_second():\n    leave()\n"})
    run = _run(adapter, suite)
    assert _unsound(run) == [
        ("test_warn.py::test_first", "coroutine 'earlier' was never awaited"),
        ("test_warn.py::test_second", "coroutine 'pending' was never awaited")]


def test_a_run_as_delivered_names_the_test_of_its_second_file(tmp_path):
    adapter, _root, suite = _project(tmp_path, {
        "test_a.py": "def test_clean():\n    pass\n",
        "test_b.py": LEAVES + "def test_leaves():\n    leave()\n"})
    run = adapter.tests.run_as_delivered(SuiteRunRequest(suite=suite, timeout_s=120,
                                                         session_seed=1))
    assert run.outcome is Outcome.COMPLETED
    assert _unsound(run) == [("test_b.py::test_leaves", "coroutine 'pending' was never awaited")]


def test_a_destroyed_pending_task_stays_without_a_test(tmp_path):
    """asyncio reports a destroyed pending task on its logger, not as a
    warning, so no test is named for it."""
    adapter, _root, suite = _project(
        tmp_path, {"test_clean.py": "def test_clean():\n    pass\n"},
        setup={"conftest.py": "import sys\n\n\ndef pytest_unconfigure(config):\n"
               "    sys.stderr.write('Task was destroyed but it is pending!\\n')\n"})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED
    assert _unsound(run) == [(None, "Task was destroyed but it is pending!")]


def test_traced_lines_do_not_change_the_statement_basis(tmp_path):
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    (root / "sign.py").write_text(
        "def sign(x):\n"
        "    if x > 0:  # pragma: no cover\n"
        "        return 1\n"
        "    return -1\n", encoding="utf-8")
    run = _run(adapter, suite, coverage_target="sign.py")
    assert run.outcome == Outcome.COMPLETED
    # The report steps after the run are timed apart from the runner.
    assert run.coverage_report_s > 0 and run.coverage_statements_s > 0
    assert run.coverage.traced_lines == (1, 2, 3, 4)
    assert run.coverage.covered == (1, 4)
    assert run.coverage.line_percent == 100.0
    assert [line["line"] for line in run.coverage.to_coverage_matrix()["lines"]] == [1, 4]


def test_a_crashed_runner_keeps_partial_tests_without_a_decided_run(tmp_path):
    adapter, _root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    runner = _SuiteRunner(adapter, SuiteRunRequest(suite, timeout_s=30))
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="test_sign" '
        'name="test_positive" file="test_sign.py" time="0.1">'
        '<failure message="failed"/></testcase></testsuite></testsuites>', encoding="utf-8")
    output = "first line\n\nsecond\nthird\nfourth\n"
    outcome, reason, detail, counts, tests, _ = runner._classify(-11, report, output)
    assert outcome == Outcome.INFRASTRUCTURE_FAILURE
    assert reason == "the runner exited with status -11" and counts.failed == 1
    assert tests[0].outcome == FAILED
    # The detail is every non-blank line, and the consumer shows as many as
    # it chooses (three by default).
    assert detail == ("first line", "second", "third", "fourth")
    assert with_detail(reason, detail) == (
        "the runner exited with status -11: second | third | fourth")
    assert with_detail(reason, detail, None).endswith(": first line | second | third | fourth")
    assert with_detail(reason, detail, 0) == reason
    # Fewer lines than the limit show all of them.
    assert with_detail(reason, detail[:2]) == (
        "the runner exited with status -11: first line | second")
    assert runner._classify(-11, report, "  \n")[1:3] == (
        "the runner exited with status -11: (no output)", ())


def test_a_collection_error_is_not_a_failing_test(tmp_path):
    adapter, _root, suite = _project(tmp_path, {
        "test_sign.py": GREEN,
        "test_broken.py": "import no_such_module\n\n\ndef test_x():\n    pass\n"})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COLLECTION_FAILED
    assert "test_broken.py" in run.reason
    assert "no_such_module" in run.reason
    assert run.failing_tests() == ()


def test_a_suite_without_tests(tmp_path):
    adapter, _root, suite = _project(tmp_path, {
        "test_empty.py": "VALUE = 1\n"})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.NO_TESTS
    assert run.counts.collected == 0


@pytest.mark.parametrize("tests, outcome", [
    ({"test_broken.py": "import no_such_module\n\n\ndef test_x():\n    pass\n"},
     Outcome.COLLECTION_FAILED),
    ({"test_empty.py": "VALUE = 1\n"}, Outcome.NO_TESTS)])
def test_a_run_whose_tests_never_ran_reports_no_coverage(tmp_path, tests, outcome):
    """Only import-time coverage exists when no test ran, and a missing
    measurement is never reported as a value, 0% included."""
    adapter, _root, suite = _project(tmp_path, tests)
    run = _run(adapter, suite, coverage_target="sign.py")
    assert run.outcome is outcome
    assert run.coverage is None


def test_a_run_that_reaches_its_timeout_has_no_counts(tmp_path):
    slow = ("import time\n\n\n"
            "def test_slow():\n    time.sleep(30)\n")
    adapter, _root, suite = _project(tmp_path, {"test_slow.py": slow})
    run = _run(adapter, suite, timeout_s=3)
    assert run.outcome is Outcome.TIMED_OUT
    assert run.wall_time_s is None
    assert run.counts.collected is None and run.counts.passed is None
    assert "3 s" in run.reason
    # Without the journal no test is named.
    assert run.active_at_end is None
    assert run.side_effects.timeout_journal is False


HANGS = ("import time\n\n\n"
         "def test_a_passes():\n    assert True\n\n\n"
         "def test_b_hangs():\n    time.sleep(120)\n")


@pytest.mark.parametrize("carried", [False, True])
def test_the_timeout_journal_names_the_test_the_timeout_ended(
        tmp_path, carried, carried_instrument):
    """On request, the journal names the test running at the timeout,
    written by the journal plugin, or by the instrument a suite carries,
    and nothing is written into the project."""
    adapter, root, suite = _project(
        tmp_path, {"test_hang.py": HANGS},
        setup={"conftest.py": carried_instrument} if carried else None)
    before = _tree(root)
    run = _run(adapter, suite, timeout_s=20, timeout_journal=True)
    assert _tree(root) == before
    assert run.outcome is Outcome.TIMED_OUT
    assert run.active_at_end == TestId("test_hang.py", "test_b_hangs")
    assert run.side_effects.timeout_journal is True


#: A test skipped in setup, then a test whose teardown hangs.
SKIP_THEN_TEARDOWN_HANG = ("import time\n\nimport pytest\n\n\n"
                           "@pytest.fixture\ndef hangs_after():\n    yield\n    time.sleep(120)\n\n\n"
                           "@pytest.mark.skip(reason='skipped in setup')\n"
                           "def test_a_skipped():\n    pass\n\n\n"
                           "def test_b_hangs_in_teardown(hangs_after):\n    pass\n")


def test_a_test_skipped_in_setup_is_not_the_test_that_hung(tmp_path):
    """The journal ends a test at its teardown report, so a test skipped in
    setup has ended, and the test whose teardown hangs is named."""
    adapter, _root, suite = _project(tmp_path, {"test_hang.py": SKIP_THEN_TEARDOWN_HANG})
    run = _run(adapter, suite, timeout_s=20, timeout_journal=True)
    assert run.outcome is Outcome.TIMED_OUT
    assert run.active_at_end == TestId("test_hang.py", "test_b_hangs_in_teardown")


def test_a_journal_of_a_finished_run_names_no_test(tmp_path):
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    before = _tree(root)
    run = _run(adapter, suite, timeout_journal=True)
    assert _tree(root) == before
    assert run.outcome is Outcome.COMPLETED
    assert run.side_effects.timeout_journal is True
    assert run.active_at_end is None


def test_the_journal_plugin_loads_only_without_an_instrument(tmp_path,
                                                             carried_instrument):
    """Where a suite carries the instrument, its hooks write the journal,
    and the plugin beside it would write every line twice."""
    variable = ENV_PREFIX + "TIMEOUT_EVIDENCE_DIR"
    for carried, loaded in ((False, True), (True, False)):
        adapter, _root, suite = _project(
            tmp_path / f"project_{carried}", {"test_sign.py": GREEN},
            setup={"conftest.py": carried_instrument} if carried else None)
        scratch = tmp_path / f"scratch_{carried}"
        scratch.mkdir()
        runner = _SuiteRunner(adapter, SuiteRunRequest(
            suite=suite, timeout_s=60, timeout_journal=True))
        env, args, _report = runner._prepare(scratch)
        assert (JOURNAL_PLUGIN in args) is loaded
        assert env[variable] == str(scratch / "journal")


def test_an_inherited_journal_directory_is_not_written(tmp_path, monkeypatch,
                                                       carried_instrument):
    """A journal directory the calling process names does not reach an
    ordinary run, whose carried instrument would otherwise write into it."""
    parent = tmp_path / "parent_journal"
    monkeypatch.setenv(ENV_PREFIX + "TIMEOUT_EVIDENCE_DIR",
                       str(parent))
    adapter, _root, suite = _project(tmp_path, {"test_seed.py": SEEDED},
                                     setup={"conftest.py": carried_instrument})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED
    assert not parent.exists()
    assert run.side_effects.timeout_journal is False


def test_an_unreadable_report_is_an_infrastructure_failure(tmp_path):
    corrupt = ("def pytest_unconfigure(config):\n"
               "    path = config.option.xmlpath\n"
               "    if path:\n"
               "        open(path, 'w').write('not a report')\n")
    adapter, _root, suite = _project(
        tmp_path, {"test_sign.py": GREEN}, setup={"conftest.py": corrupt})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "report.xml" in run.reason and run.reason_detail
    assert run.reason_text(None).endswith(run.reason_detail[-1])
    assert run.reason_text() == with_detail(run.reason, run.reason_detail[-3:])
    assert run.counts.collected is None


def test_coverage_that_fails_after_green_tests_keeps_the_outcomes(tmp_path):
    # The process exits before coverage.py saves its data.
    exit_early = ("import os\n\n\n"
                  "def pytest_unconfigure(config):\n"
                  "    os._exit(0)\n")
    adapter, _root, suite = _project(
        tmp_path, {"test_sign.py": GREEN}, setup={"conftest.py": exit_early})
    run = _run(adapter, suite, coverage_target="sign.py")
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert run.reason.startswith("every test passed, but the coverage tool")
    assert [t.outcome for t in run.tests] == [PASSED, PASSED]
    assert run.coverage is None


def test_coverage_in_the_tools_own_terms(tmp_path):
    only_positive = ("from sign import sign\n\n\n"
                     "def test_positive():\n    assert sign(2) == 1\n")
    adapter, root, suite = _project(tmp_path, {"test_sign.py": only_positive})
    before = _tree(root)
    run = _run(adapter, suite, coverage_target="sign.py")
    assert _tree(root) == before
    assert run.outcome is Outcome.COMPLETED
    coverage = run.coverage
    assert coverage.target.path == "sign.py"
    assert run.identity.target == coverage.target
    assert coverage.statements == (1, 2, 3, 4)
    assert coverage.covered == (1, 2, 3)
    assert coverage.missing() == (4,)
    assert coverage.line_percent == 75.0
    assert set(coverage.branches) == {BranchItem("sign.py", 2, 3),
                                      BranchItem("sign.py", 2, 4)}
    assert coverage.taken == (BranchItem("sign.py", 2, 3),)
    assert coverage.branch_percent == 50.0
    matrix = coverage.to_coverage_matrix()
    assert matrix["summary"] == {
        "total_lines": 4, "covered_lines": 3, "missing_lines": 1,
        "covered_branches": 1, "missing_branches": 1}
    assert {"from": 2, "to": 4, "status": "missing"} in matrix["branches"]


def test_only_the_target_is_traced(tmp_path, monkeypatch):
    """coverage.py ignores ``--include`` beside ``--source``: the data
    holds the target alone."""
    from langadapter.python import _runs

    adapter, root, suite = _project(tmp_path, {"test_sign.py": (
        "from sign import sign\n\n\n"
        "def test_positive():\n    assert sign(2) == 1\n")})
    (root / "sign.py").write_text("import helper\n\n\n" + TARGET.replace(
        "return 1", "return helper.one()"), encoding="utf-8")
    (root / "helper.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    coverage = pytest.importorskip("coverage")
    measured = []
    report = _runs._coverage_json

    def observed(self, data, *args, **kwargs):
        recorded = coverage.CoverageData(basename=data)
        recorded.read()
        measured.extend(sorted(Path(name).name for name in recorded.measured_files()))
        return report(self, data, *args, **kwargs)

    monkeypatch.setattr(_runs, "_coverage_json", observed)
    run = _run(adapter, suite, coverage_target="sign.py")
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert measured == ["sign.py"]
    assert run.coverage.statements == (1, 4, 5, 6, 7)
    assert run.coverage.covered == (1, 4, 5, 6)
    assert run.coverage.taken == (BranchItem("sign.py", 5, 6),)


def test_a_target_the_tests_never_import_measures_zero(tmp_path):
    """A target no test imports is not in the run's data and still reports
    0 % of its statements and branches."""
    adapter, root, suite = _project(tmp_path, {"test_other.py": (
        "def test_nothing():\n    assert True\n")})
    run = _run(adapter, suite, coverage_target="sign.py")
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.coverage.statements == (1, 2, 3, 4) and run.coverage.covered == ()
    assert set(run.coverage.branches) == {BranchItem("sign.py", 2, 3),
                                          BranchItem("sign.py", 2, 4)}
    assert run.coverage.taken == () and run.coverage.line_percent == 0.0


def test_the_covered_lines_a_reader_gets_are_statements(tmp_path):
    """coverage.py traces a module docstring's line, but the covered lines
    a reader gets hold statements only."""
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    (root / "sign.py").write_text('"""Signs."""\n\n\n' + TARGET, encoding="utf-8")
    run = _run(adapter, suite, coverage_target="sign.py")
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert 1 in run.coverage.traced_lines and 1 not in run.coverage.statements
    assert adapter.coverage_texts(run.coverage)["covered_lines"] == run.coverage.covered


def test_tests_whose_code_lives_in_another_file_are_the_suites(tmp_path):
    """An inherited test method and an imported test function are read
    from the report's class name, the suite's module, not from their code's
    file."""
    adapter, root, suite = _project(tmp_path, {"test_inherit.py": (
        "from base_cases import BaseCases\n"
        "from shared_checks import test_shared\n\n\n"
        "class TestSign(BaseCases):\n"
        "    def test_own(self):\n        assert True\n")})
    (root / "tests" / "base_cases.py").write_text(
        "from sign import sign\n\n\nclass BaseCases:\n"
        "    def test_negative(self):\n        assert sign(-2) == 1\n", encoding="utf-8")
    (root / "tests" / "shared_checks.py").write_text(
        "def test_shared():\n    assert False\n", encoding="utf-8")
    run = _run(adapter, suite)
    assert run.outcome is Outcome.TEST_FAILURES and run.unmatched == ()
    assert sorted((str(t.test), t.outcome) for t in run.tests) == [
        ("test_inherit.py::TestSign::test_negative", FAILED),
        ("test_inherit.py::TestSign::test_own", PASSED),
        ("test_inherit.py::test_shared", FAILED)]


def test_a_test_inherited_from_another_suite_file_is_the_inheriting_files(tmp_path):
    """pytest names test_a.py, the code's file, for test_b's inherited
    test, while its class name names test_b."""
    adapter, root, suite = _project(tmp_path, {
        "test_a.py": ("from sign import sign\n\n\nclass TestBase:\n"
                      "    def test_x(self):\n        assert sign(-2) == 1\n"),
        "test_b.py": ("from test_a import TestBase\n\n\nclass TestChild(TestBase):\n"
                      "    def test_own(self):\n        assert True\n")})
    run = _run(adapter, suite)
    assert run.unmatched == ()
    # test_b also collects the TestBase it imports.
    assert sorted((str(t.test), t.outcome) for t in run.tests) == [
        ("test_a.py::TestBase::test_x", FAILED),
        ("test_b.py::TestBase::test_x", FAILED),
        ("test_b.py::TestChild::test_own", PASSED),
        ("test_b.py::TestChild::test_x", FAILED)]


def test_percentages_scale_before_dividing():
    """23 of 40 is exactly 57.5 %, which dividing first shows as 57.4 %,
    and 210 of 213 differs in the last place."""
    target = lb.FileIdentity("accessor_str.py", "fixture")
    branches = tuple(BranchItem("accessor_str.py", n, n + 1) for n in range(1, 41))
    coverage = CoverageObservation(
        target, tuple(range(1, 214)), tuple(range(1, 211)), branches, branches[:23])
    assert coverage.line_percent == 98.59154929577464
    assert coverage.branch_percent == 57.5 and f"{coverage.branch_percent:.1f}" == "57.5"


def test_an_arc_that_leaves_the_function_keeps_its_negative_destination(
        tmp_path):
    adapter, root, suite = _project(tmp_path, {"test_early.py": (
        "from early import first\n\n\n"
        "def test_first():\n    assert first([]) is None\n")})
    (root / "early.py").write_text(
        "def first(items):\n"
        "    for item in items:\n"
        "        return item\n", encoding="utf-8")
    run = _run(adapter, suite, coverage_target="early.py")
    exits = [b for b in run.coverage.branches if b.leaves_code_object]
    assert exits == [BranchItem("early.py", 2, -1)]
    assert {"from": 2, "to": -1, "status": "taken"} in (
        run.coverage.to_coverage_matrix()["branches"])


def test_each_report_shape_maps_to_one_test_identifier(tmp_path):
    shapes = ("import pytest\n\n\n"
              "@pytest.mark.parametrize('x', [1, 'a b'])\n"
              "def test_param(x):\n    assert x\n\n\n"
              "@pytest.mark.skip(reason='not here')\n"
              "def test_skip():\n    pass\n\n\n"
              "@pytest.mark.xfail(reason='known')\n"
              "def test_xfail():\n    assert 0\n\n\n"
              "@pytest.fixture\n"
              "def broken():\n    yield\n    raise RuntimeError('teardown')\n\n\n"
              "def test_teardown_error(broken):\n    pass\n\n\n"
              "class TestOuter:\n"
              "    class TestInner:\n"
              "        def test_nested(self):\n            pass\n")
    adapter, _root, suite = _project(tmp_path, {"sub/test_shapes.py": shapes})
    run = _run(adapter, suite)
    got = {str(t.test): t.outcome for t in run.tests}
    assert got == {
        "sub/test_shapes.py::test_param[1]": PASSED,
        "sub/test_shapes.py::test_param[a b]": PASSED,
        "sub/test_shapes.py::test_skip": SKIPPED,
        "sub/test_shapes.py::test_xfail": XFAILED,
        "sub/test_shapes.py::test_teardown_error": ERROR,
        "sub/test_shapes.py::TestOuter::TestInner::test_nested": PASSED,
    }
    nested = next(t.test for t in run.tests if t.test.qualname.endswith(
        "test_nested"))
    assert nested == TestId("sub/test_shapes.py",
                            "TestOuter.TestInner.test_nested")
    param = run.tests[1].test
    assert (param.qualname, param.params) == ("test_param", "a b")
    assert run.outcome is Outcome.TEST_FAILURES
    assert run.counts.skipped == 2 and run.counts.errors == 1


def test_a_setup_file_takes_part_and_is_named(tmp_path):
    uses_fixture = "def test_value(value):\n    assert value == 3\n"
    fixture = ("import pytest\n\n\n"
               "@pytest.fixture\n"
               "def value():\n    return 3\n")
    adapter, _root, suite = _project(
        tmp_path, {"test_fixture.py": uses_fixture},
        setup={"conftest.py": fixture})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED
    assert [f.path for f in run.identity.suite] == [
        "conftest.py", "test_fixture.py"]


def test_setup_files_pytest_would_load_must_be_listed(tmp_path):
    adapter, root, _ = _project(tmp_path, {"test_sign.py": GREEN},
                                setup={"conftest.py": "X = 1\n"})
    unlisted = lb.Suite(str(root / "tests"), ("test_sign.py",))
    with pytest.raises(InvalidRequest) as refused:
        _run(adapter, unlisted)
    assert refused.value.field == "suite.setup_files"
    assert "conftest.py" in refused.value.message
    (root / "tests" / "other").mkdir()
    (root / "tests" / "other" / "conftest.py").write_text("Y = 2\n",
                                                          encoding="utf-8")
    off_path = lb.Suite(str(root / "tests"), ("test_sign.py",),
                        ("conftest.py", "other/conftest.py"))
    with pytest.raises(InvalidRequest) as refused:
        _run(adapter, off_path)
    assert "other/conftest.py" in refused.value.message
    with pytest.raises(InvalidRequest) as refused:
        _run(adapter, lb.Suite(str(root / "tests"), ("test_sign.py",),
                               ("conftest.py",)), timeout_s=0)
    assert refused.value.field == "timeout_s"


SEEDED = ("import random\n\n\n"
          "def test_seeded():\n"
          "    assert random.random() == 0.8444218515250481\n")


def test_a_carried_instrument_restores_the_module_under_test(tmp_path,
                                                             carried_instrument):
    """The run names the module under test to an instrument a suite
    carries, which restores it before each test."""
    leaky = ("import sign\n\n\n"
             "def test_a_rebinds():\n"
             "    sign.sign = lambda x: 0\n"
             "    assert sign.sign(5) == 0\n\n\n"
             "def test_b_expects_the_module():\n"
             "    assert sign.sign(5) == 1\n")
    adapter, _root, suite = _project(tmp_path, {"test_leak.py": leaky},
                                     setup={"conftest.py": carried_instrument})
    assert _run(adapter, suite).outcome is Outcome.TEST_FAILURES
    restored = _run(adapter, suite, module_under_test="sign.py")
    assert restored.outcome is Outcome.COMPLETED


def test_an_instrument_the_suite_carries_is_reported_as_the_suites(tmp_path,
                                                                  carried_instrument):
    adapter, _root, suite = _project(tmp_path, {"test_seed.py": SEEDED},
                                     setup={"conftest.py": carried_instrument})
    run = _run(adapter, suite)
    assert run.outcome is Outcome.COMPLETED
    assert run.side_effects.instrument_source == "suite"
    assert run.identity.instrument == instrument_identity(carried_instrument)


def test_the_isolating_configuration_is_written_and_reported(tmp_path):
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\nmarkers = ['slow: slow tests']\n",
        encoding="utf-8")
    run = _run(adapter, suite, isolation_config=True)
    assert run.outcome is Outcome.COMPLETED
    assert run.side_effects.files_written == (
        "project_pytest.toml", "pytest.ini")
    assert (root / "tests" / "project_pytest.toml").read_text(
        encoding="utf-8") == (root / "pyproject.toml").read_text(
            encoding="utf-8")
    again = _run(adapter, suite, isolation_config=True)
    assert again.side_effects.files_written == ()


def test_the_projects_configuration_applies_without_a_copy(tmp_path):
    strict = ("[tool.pytest.ini_options]\n"
              "filterwarnings = ['error']\n")
    warns = ("import warnings\n\n\n"
             "def test_warns():\n"
             "    warnings.warn('careful', UserWarning)\n")
    adapter, root, suite = _project(tmp_path, {"test_warn.py": warns})
    (root / "pyproject.toml").write_text(strict, encoding="utf-8")
    assert _run(adapter, suite).outcome is Outcome.TEST_FAILURES
    isolated = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        test_framework=lb.TestFrameworkConfig(source="isolated")))
    assert _run(isolated, suite).outcome is Outcome.COMPLETED


def test_files_earlier_runs_created_are_removed_on_request(tmp_path):
    writes = ("from pathlib import Path\n\n\n"
              "def test_writes():\n"
              "    Path(__file__).resolve().parents[1].joinpath("
              "'left_behind.txt').write_text('x')\n")
    original, root, _ = _project(tmp_path, {"test_writes.py": writes})
    owner = lb.prepare_project(original.context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with owner.use() as context:
            adapter = lb.create("python", context)
            suite = lb.Suite(str(Path(context.root) / "tests"), ("test_writes.py",))
            first = _run(adapter, suite, remove_created_files=True)
            assert first.side_effects.files_removed == ()
            assert (Path(context.root) / "left_behind.txt").exists()
            second = _run(adapter, suite, remove_created_files=True)
            assert _written(second.side_effects.files_removed) == ("left_behind.txt",)
            assert second.outcome is Outcome.COMPLETED
        assert not (root / "left_behind.txt").exists()
    finally:
        owner.close()


def test_files_a_benchmark_created_are_removed_by_the_next_run(tmp_path):
    """A benchmark records the files its repetitions create, so the next
    run that asks removes them."""
    from langadapter.runs import BenchmarkRequest

    writes = ("from pathlib import Path\n\n\n"
              "def test_writes():\n"
              "    Path(__file__).resolve().parents[1].joinpath("
              "'left_behind.txt').write_text('x')\n")
    original, _, _ = _project(tmp_path, {"test_writes.py": writes})
    owner = lb.prepare_project(original.context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with owner.use() as context:
            adapter = lb.create("python", context)
            suite = lb.Suite(str(Path(context.root) / "tests"), ("test_writes.py",))
            measured = adapter.benchmark(BenchmarkRequest(
                suite, 1, 120, warmup_runs=0, remove_created_files=True))
            assert measured.outcome is Outcome.COMPLETED, measured.reason
            assert (Path(context.root) / "left_behind.txt").exists()
            after = _run(adapter, suite, remove_created_files=True)
            assert _written(after.side_effects.files_removed) == ("left_behind.txt",)
    finally:
        owner.close()


@pytest.mark.parametrize("warmup", [False, True])
def test_files_per_test_evidence_created_are_removed_by_the_next_run(tmp_path, warmup):
    """Per-test evidence records the files its runs create, so the next run
    that asks removes them, as after every other run kind."""
    writes = ("from pathlib import Path\n\n\n"
              "def test_writes():\n"
              "    Path(__file__).resolve().parents[1].joinpath("
              "'left_behind.txt').write_text('x')\n")
    original, _, _ = _project(tmp_path, {"test_writes.py": writes})
    owner = lb.prepare_project(original.context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with owner.use() as context:
            adapter = lb.create("python", context)
            suite = lb.Suite(str(Path(context.root) / "tests"), ("test_writes.py",))
            measured = adapter.per_test_evidence(PerTestEvidenceRequest(
                suite=suite, target="sign.py", timeout_s=120, warmup=warmup,
                remove_created_files=True))
            assert measured.outcome is Outcome.COMPLETED, measured.reason
            assert (Path(context.root) / "left_behind.txt").exists()
            after = _run(adapter, suite, remove_created_files=True)
            assert _written(after.side_effects.files_removed) == ("left_behind.txt",)
    finally:
        owner.close()


def test_files_written_between_runs_are_not_removed(tmp_path):
    """A file the caller writes between runs is not in the earlier run's
    record and stays. Independent processes must not write into the
    copy."""
    passing = "def test_passes():\n    assert True\n"
    original, root, _ = _project(tmp_path, {"test_passes.py": passing})
    owner = lb.prepare_project(original.context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with owner.use() as context:
            adapter = lb.create("python", context)
            suite = lb.Suite(str(Path(context.root) / "tests"), ("test_passes.py",))
            first = _run(adapter, suite, remove_created_files=True)
            assert first.outcome is Outcome.COMPLETED
            between = Path(context.root) / "written_between.txt"
            between.write_text("the caller's", encoding="utf-8")
            second = _run(adapter, suite, remove_created_files=True)
            assert _written(second.side_effects.files_removed) == ()
            assert between.read_text(encoding="utf-8") == "the caller's"
        assert not (root / "written_between.txt").exists()
    finally:
        owner.close()


def test_runner_options(tmp_path):
    red = ("def test_one():\n    assert 0\n\n\n"
           "def test_two():\n    assert 0\n")
    adapter, _root, suite = _project(tmp_path, {"test_red.py": red})
    stopped = _run(adapter, suite, runner_options=PytestOptions(
        stop_at_first_failure=True, traceback="line"))
    assert [t.outcome for t in stopped.tests] == [FAILED]
    for options, field in (
            (PytestOptions(traceback="verbose"), "runner_options.traceback"),
            (PytestOptions(disabled_plugins=("-x",)),
             "runner_options.disabled_plugins"),
            ({"traceback": "short"}, "runner_options")):
        with pytest.raises(InvalidRequest) as refused:
            _run(adapter, suite, runner_options=options)
        assert refused.value.field == field


class _Recording(LocalExecutor):
    """This machine as a container that shares its paths, recording each
    command."""

    def __init__(self):
        super().__init__()
        self.commands = []

    def run(self, argv, **kw):
        self.commands.append([str(a) for a in argv])
        return super().run(argv, **kw)


def _container(adapter, root, zone):
    executor = _Recording()
    container = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        execution=lb.ContainerExecution(container="c1", work_dir=str(zone))),
        executor=executor)
    container._toolchain = adapter.toolchain_identity()
    return container, executor


@pytest.mark.skipif(os.name == "nt", reason="a container's paths are POSIX, and "
                    "this machine stands in for one only where its paths are")
def test_a_container_context_runs_through_its_executor(tmp_path):
    """The run, its target's check and its identity go through the
    context's executor."""
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    container, executor = _container(adapter, root, tmp_path)
    run = _run(container, suite, coverage_target="sign.py")
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.counts.passed == 2
    assert run.coverage.target == lb.FileIdentity.of(root / "sign.py", root)
    assert run.coverage.covered == (1, 2, 3, 4)
    assert run.identity.execution.kind == "container"
    for script in (_project_files._IS_FILE, _project_files._READ):
        assert [c[-1] for c in executor.commands if script in c] == [
            str(root / "sign.py")]
    with pytest.raises(InvalidRequest) as refused:
        _run(container, suite, coverage_target="../sign.py")
    assert refused.value.field == "coverage_target"
    with pytest.raises(InvalidRequest) as refused:
        _run(container, suite, coverage_target="missing.py")
    assert "in the container" in refused.value.message


def test_a_container_suite_lies_in_the_work_dir(tmp_path):
    adapter, root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    zone = tmp_path / "zone"
    zone.mkdir()
    container, executor = _container(adapter, root, zone)
    with pytest.raises(InvalidRequest) as refused:
        _run(container, suite)
    assert refused.value.field == "suite.root"
    assert executor.commands == []


def test_failures_the_adapter_cannot_map_still_make_the_run_red(tmp_path):
    from langadapter.python._runs import _SuiteRunner

    adapter, _root, suite = _project(tmp_path, {"test_sign.py": GREEN})
    runner = _SuiteRunner(adapter, SuiteRunRequest(suite=suite, timeout_s=10))
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="elsewhere" '
        'name="test_x" file="elsewhere.py" time="0.1">'
        '<failure message="boom"/></testcase></testsuite></testsuites>',
        encoding="utf-8")
    outcome, reason, _detail, counts, tests, unmatched = runner._classify(1, report, "")
    assert outcome is Outcome.TEST_FAILURES
    assert unmatched == ("elsewhere::test_x",) and tests == ()
    assert "1 of them not mapped" in reason
    report.write_text(
        '<testsuites><testsuite><testcase classname="elsewhere" '
        'name="test_x" file="elsewhere.py" time="0.1"/></testsuite>'
        '</testsuites>', encoding="utf-8")
    outcome, *_ = runner._classify(0, report, "")
    assert outcome is Outcome.COMPLETED



def test_a_launch_that_may_have_left_a_command_is_raised_not_returned(tmp_path, monkeypatch):
    """A launch error whose command may still run is raised with its
    recovery handle, not returned as an infrastructure failure."""
    from pathlib import Path

    from langadapter import _process_tree
    from langadapter._owned_directory import OwnershipRefused
    from langadapter.preparation import prepare_project

    adapter, _root, _suite = _project(tmp_path, {"test_sign.py": GREEN})

    def uncertain(*args, **kwargs):
        raise OSError("the launch's outcome is unknown")

    owner = prepare_project(adapter.context)
    with pytest.raises(OSError, match="outcome is unknown") as caught:
        with owner.use() as scoped:
            monkeypatch.setattr(_process_tree, "_start", uncertain)
            PythonAdapter(scoped).run_suite(SuiteRunRequest(
                lb.Suite(str(Path(scoped.root) / "tests"), ("test_sign.py",)), timeout_s=60))
    monkeypatch.undo()
    with pytest.raises(OwnershipRefused, match="unfinished native launch"):
        caught.value._langadapter_execution_scope.require_quiescent()


def test_a_relative_root_runs_a_src_layout_from_another_dir(tmp_path, monkeypatch):
    """A project root relative to the current directory, with its code under
    src and pytest's own pythonpath setting, as the examples run it."""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "example.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (project / "tests" / "test_example.py").write_text(
        "from example import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n', encoding="utf-8")
    # A failed run keeps its scratch: keep it out of the system's directory.
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    monkeypatch.chdir(tmp_path)
    adapter = lb.create("python", lb.ProjectContext(
        "project", lb.Interpreter(path=sys.executable)))
    run = _run(adapter, lb.Suite("project", ("tests/test_example.py",)),
               coverage_target="src/example.py")
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.counts.passed == 1
    assert run.coverage is not None and run.coverage.line_percent == 100.0


# Controlled runner reports preserve execution failures and owned diagnostics.
@pytest.mark.parametrize("code", [1, 2, 3, 4, -11, 0, 5])
def test_empty_report_preserves_runner_failure_and_its_owned_receipt(
        tmp_path, monkeypatch, recwarn, code):
    root, zone, receipts = (tmp_path / name for name in ("project", "zone", "receipts"))
    for directory in (root, zone, receipts):
        directory.mkdir()
    test = root / "test_value.py"
    source = "def test_value():\n    assert True\n"
    test.write_text(source, encoding="utf-8")
    owners, commands = [], []
    original = execution.OwnedDirectory.create

    def allocate(**options):
        owner = original(**options, receipt_parent=str(receipts))
        owners.append(owner)
        return owner

    def run(_executor, command, **options):
        commands.append(tuple(command))
        report, = [arg.split("=", 1)[1] for arg in command if arg.startswith("--junit-xml=")]
        Path(report).write_text("<testsuites><testsuite/></testsuites>", encoding="utf-8")
        return subprocess.CompletedProcess(command, code, "controlled runner diagnostic", "")

    monkeypatch.setattr(execution.OwnedDirectory, "create", allocate)
    monkeypatch.setattr(execution.LocalExecutor, "run", run)
    adapter = lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        execution=lb.LocalExecution(work_dir=str(zone))))
    adapter._toolchain = lb.ToolchainIdentity("python", "controlled")
    result = adapter.tests.run_suite(SuiteRunRequest(lb.Suite(str(root), (test.name,)), 1))

    expected = lb.Outcome.NO_TESTS if code in (0, 5) else lb.Outcome.INFRASTRUCTURE_FAILURE
    assert result.outcome is expected and result.returncode == code
    assert result.tests == result.unmatched == () and result.coverage is None
    assert result.counts.collected == result.counts.passed == result.counts.failed == 0
    assert "controlled runner diagnostic" in result.output
    assert test.read_text(encoding="utf-8") == source and len(commands) == len(owners) == 1
    owner, = owners
    if expected is lb.Outcome.INFRASTRUCTURE_FAILURE:
        assert f"status {code}" in result.reason
        owner.verify()
        assert (owner.root / "report.xml").read_text(encoding="utf-8") == (
            "<testsuites><testsuite/></testsuites>")
        assert any("retained after interrupted operation" in str(item.message) for item in recwarn.list)
    else:
        assert not owner.root.exists() and not owner.receipt.exists() and not recwarn.list


@pytest.mark.parametrize("code,xml,outcome", [
    (1, '<testcase file="test_value.py" classname="test_value" name="test_value">'
        '<failure message="assertion"/></testcase>', lb.Outcome.TEST_FAILURES),
    (1, '<testcase classname="outside" name="test_other"><failure/></testcase>',
        lb.Outcome.TEST_FAILURES),
    (2, '<testcase name="test_value.py"><error message="collection failure">'
        'ImportError</error></testcase>', lb.Outcome.COLLECTION_FAILED),
    (0, '<testcase file="test_value.py" classname="test_value" name="test_value"/>',
        lb.Outcome.COMPLETED),
])
def test_native_test_and_collection_verdicts_remain_distinct(tmp_path, code, xml, outcome):
    report = tmp_path / "report.xml"
    report.write_text(f"<testsuites><testsuite>{xml}</testsuite></testsuites>", encoding="utf-8")
    runner = object.__new__(_SuiteRunner)
    runner.suite_root = tmp_path
    runner.test_files = [tmp_path / "test_value.py"]
    actual, _reason, _detail, counts, tests, unmatched = runner._classify(
        code, report, "native diagnostic")
    assert actual is outcome
    if "outside" in xml:
        assert counts.failed == 0 and tests == () and unmatched == ("outside::test_other",)
    elif outcome is lb.Outcome.COLLECTION_FAILED:
        assert counts.collected == 0 and tests == ()
    else:
        assert counts.collected == 1 and len(tests) == 1


def test_a_pytest_node_id_names_its_test_through_the_public_reader():
    """PythonAdapter.test_of_nodeid reads a node id as the adapter's runs do:
    the file, the qualified name and the parameters apart."""
    from langadapter.python.adapter import PythonAdapter

    nodeid = "tests/test_a.py::TestX::test_y[1-2]"
    assert PythonAdapter.test_of_nodeid(nodeid) == lb.TestId(
        file="tests/test_a.py", qualname="TestX.test_y", params="1-2", text=nodeid)
    assert PythonAdapter.test_of_nodeid("test_b.py::test_z") == lb.TestId(
        file="test_b.py", qualname="test_z", params=None, text="test_b.py::test_z")
