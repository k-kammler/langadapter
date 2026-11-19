"""The hooks a consumer builds its runs with: the request fields of each
kind of run, the setup files beside the tests, a directory's test files and a
run's counts, by default and as Python and Java give them."""
import importlib.util
import os
import sys
import subprocess

import pytest

import langadapter as lb
from langadapter.adapter import RUN_PURPOSES, LangAdapter, SalvageReport
from langadapter.c.adapter import _PYTHON_SOURCE_HOOKS
from langadapter.errors import InvalidRequest
from langadapter.java._build import BOTH_BUILDS, BOTH_BUILDS_ACTION
from langadapter.java.adapter import JavaAdapter
from langadapter.python._profile import PythonProfile
from langadapter.python.adapter import PythonAdapter
from langadapter.python._output import pytest_counts
from langadapter.python.project import PytestOptions
from langadapter.runs import RunCounts
from langadapter.sources import SourceResult, StructureCheck
from langadapter.versions import python_line


class Plain(LangAdapter):
    language = "plain"
    file_extensions = (".pl",)


def _adapter(cls, root):
    return cls(lb.ProjectContext(str(root)))


def _killing(file, qualname):
    return {"file": file, "qualname": qualname, "params": None, "text": ""}


def test_stored_kills_attribute_to_the_tests_the_run_recorded(tmp_path):
    """Each row's recorded killing tests give their kill tokens as
    test_id_text writes them, matched exactly or by a longer or shorter
    dotted id. Killers no test was recorded for are unmatched, and so are a
    row's killers stored without killing tests. A pool's kills go to the
    suite of each test's file, matched there by its qualified name when the
    pooled file has another name."""
    matrix = {"mutants": [
        {"id": "a", "killers": ["k1", "k2"], "killing_tests": [
            _killing("tests/test_x.py", "TestX.test_a"), _killing("tests/test_seed_002.py", "test_b")]},
        {"id": "b", "killers": ["k3", "k4"], "unmatched_killers": ["k4"],
         "killing_tests": [_killing("tests/test_x.py", "TestX.test_a")]},
        {"id": "c", "killers": ["stored before"]},
        {"id": "d", "status": "survived"},
    ]}
    python = _adapter(PythonAdapter, tmp_path)
    tokens, unmatched = python.kill_tokens(matrix, {"tests.test_x.TestX.test_a", "x.test_y.test_b"})
    assert tokens == {"tests.test_x.TestX.test_a": frozenset({"K:a", "K:b"})}
    assert unmatched == ("tests/test_seed_002.py::test_b", "k4", "stored before")
    assert python.pool_kills(matrix, {"test_x.py": 0, "test_seed_002.py": 1},
                             {0: {"tests.test_x.TestX.test_a"}, 1: {"x.test_y.test_b"}}) == {
        0: {"tests.test_x.TestX.test_a": {"K:a", "K:b"}}, 1: {"x.test_y.test_b": {"K:a"}}}
    java = _adapter(JavaAdapter, tmp_path)
    tokens, unmatched = java.kill_tokens({"mutants": [{"id": "m", "killers": ["k"], "killing_tests": [
        {"file": "demo/CalcTest.java", "qualname": "CalcTest.Clamping.keepsInside", "params": None,
         "text": "demo.CalcTest$Clamping#keepsInside()"}]}]}, {"demo.CalcTest.Clamping.keepsInside"})
    assert tokens == {"demo.CalcTest.Clamping.keepsInside": frozenset({"K:m"})} and unmatched == ()


PLUGINS = ("logfire", "anyio")


def test_python_s_requests_are_today_s(tmp_path):
    python = _adapter(PythonAdapter, tmp_path)
    assert python.request_options("run", target="calc.py", traceback="long",
                                  stop_at_first_failure=True, record_leaks=True) == {
        "isolation_config": True, "module_under_test": "calc.py", "record_leaks": True,
        "remove_created_files": True, "runner_options": PytestOptions(
            traceback="long", stop_at_first_failure=True, disabled_plugins=PLUGINS)}
    for purpose in ("failing", "evidence"):
        assert python.request_options(purpose, target="calc.py") == {
            "isolation_config": True, "module_under_test": "calc.py",
            "remove_created_files": True,
            "runner_options": PytestOptions(disabled_plugins=PLUGINS)}
    assert python.request_options("delivered", session_seed=7, reverse_order=True) == {
        "isolation_config": True, "remove_created_files": True, "session_seed": 7,
        "reverse_order": True,
        "runner_options": PytestOptions(traceback="auto", disabled_plugins=PLUGINS)}
    # A run as delivered that is the caller's normal run: its traceback, its
    # first-failure stop and the read-only leak observer.
    assert python.request_options("delivered", target="calc.py", traceback="line",
                                  stop_at_first_failure=True, session_seed=1,
                                  record_leaks=True) == {
        "isolation_config": True, "remove_created_files": True, "session_seed": 1,
        "reverse_order": False, "record_leaks": True, "module_under_test": "calc.py",
        "runner_options": PytestOptions(traceback="line", stop_at_first_failure=True,
                                        disabled_plugins=PLUGINS)}
    # The module under test follows the target, not the leak record: in a
    # prepared project it also decides which file the run imports (E's A1).
    assert python.request_options("delivered", target="calc.py", session_seed=1) == {
        "isolation_config": True, "remove_created_files": True, "session_seed": 1,
        "reverse_order": False, "module_under_test": "calc.py",
        "runner_options": PytestOptions(traceback="auto", disabled_plugins=PLUGINS)}
    assert python.request_options("delivered", session_seed=1, record_leaks=True) == {
        "isolation_config": True, "remove_created_files": True, "session_seed": 1,
        "reverse_order": False, "record_leaks": True,
        "runner_options": PytestOptions(traceback="auto", disabled_plugins=PLUGINS)}
    assert python.request_options("run", target="calc.py") == {
        "isolation_config": True, "module_under_test": "calc.py", "record_leaks": False,
        "remove_created_files": True,
        "runner_options": PytestOptions(traceback="short", disabled_plugins=PLUGINS)}
    assert python.request_options("collect") == {
        "isolation_config": True, "runner_options": PytestOptions(disabled_plugins=PLUGINS)}
    for purpose in ("benchmark", "coverage_matrix"):
        assert python.request_options(purpose, target="calc.py") == {
            "isolation_config": True, "module_under_test": "calc.py",
            "runner_options": PytestOptions(traceback="no", disabled_plugins=PLUGINS)}


def test_any_purpose_can_leave_out_the_timing_plugins(tmp_path):
    """A caller that holds no Python code, such as a timing tool, asks for
    the plugins left out through request_options. Java has none and adds
    nothing for it."""
    python = _adapter(PythonAdapter, tmp_path)
    assert python.request_options("benchmark", target="calc.py", leave_out_timing_plugins=True) == {
        "isolation_config": True, "module_under_test": "calc.py",
        "runner_options": PytestOptions(traceback="no", disabled_plugins=PLUGINS,
                                        leave_out_timing_plugins=True)}
    for purpose in RUN_PURPOSES:
        assert python.request_options(
            purpose, session_seed=1, leave_out_timing_plugins=True)["runner_options"].leave_out_timing_plugins
        assert python.request_options(purpose, session_seed=1) == python.request_options(
            purpose, session_seed=1, leave_out_timing_plugins=False)
    java = _adapter(JavaAdapter, tmp_path)
    assert all(java.request_options(purpose, leave_out_timing_plugins=True) == java.request_options(purpose)
               for purpose in RUN_PURPOSES)


def test_the_project_s_own_run_keeps_its_configuration_and_plugins(tmp_path):
    """The project's own run, as a timing tool that leaves the project as it
    is asks for it, disables no plugin and uses no isolated configuration,
    and takes the timing switch. An anyio test runs under it, where the
    measurement purposes, the benchmark among them, disable anyio's plugin."""
    pytest.importorskip("anyio")
    import sys

    from langadapter.runs import SuiteRunRequest

    python = _adapter(PythonAdapter, tmp_path)
    assert python.request_options("project", target="calc.py") == {"runner_options": PytestOptions()}
    assert python.request_options("project", leave_out_timing_plugins=True) == {
        "runner_options": PytestOptions(leave_out_timing_plugins=True)}
    root = tmp_path / "project"
    root.mkdir()
    (root / "test_async.py").write_text(
        "import anyio\nimport pytest\n\n\n@pytest.mark.anyio\nasync def test_sleeps():\n"
        "    await anyio.sleep(0)\n", encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(root), ("test_async.py",))
    outcomes = {purpose: [case.outcome for case in adapter.run_suite(SuiteRunRequest(
        suite, 120, **adapter.request_options(purpose))).tests] for purpose in ("project", "benchmark")}
    assert outcomes["project"] == ["passed"] and outcomes["benchmark"] != ["passed"]


def test_no_run_request_asks_for_the_determinism_instrument():
    """No run places the instrument: no request has the field. A
    suite whose setup file carries one is recognized instead."""
    from dataclasses import fields

    from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest, SuiteRunRequest

    for request in (SuiteRunRequest, PerTestEvidenceRequest, BenchmarkRequest):
        assert "instrument" not in {field.name for field in fields(request)}, request


def test_java_and_the_default_add_nothing_and_a_purpose_is_checked(tmp_path):
    plain = _adapter(Plain, tmp_path)
    assert all(plain.request_options(purpose, target="x") == {} for purpose in RUN_PURPOSES)
    java = _adapter(JavaAdapter, tmp_path)
    assert {purpose: java.request_options(purpose, target="x", session_seed=1,
                                          reverse_order=True) for purpose in RUN_PURPOSES} == {
        "run": {"remove_created_files": True}, "failing": {"remove_created_files": True},
        "evidence": {"remove_created_files": True}, "delivered": {"remove_created_files": True},
        "benchmark": {}, "coverage_matrix": {}, "collect": {}, "project": {}}
    assert java.delivered_order_refusal(True) == (
        "JUnit orders a class's tests itself, so a reverse-order run is not offered for Java")
    assert java.delivered_order_refusal(False) is None and plain.delivered_order_refusal(True) is None
    with pytest.raises(InvalidRequest, match="purpose"):
        _adapter(PythonAdapter, tmp_path).request_options("everything")


def test_python_names_the_conftest_files_pytest_loads(tmp_path):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    inner = tmp_path / "pkg"
    inner.mkdir()
    (inner / "conftest.py").write_text("", encoding="utf-8")
    (inner / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    python = _adapter(PythonAdapter, tmp_path)
    found = python.setup_files(str(tmp_path), [str(inner / "test_a.py")])
    assert sorted(found) == sorted([str(tmp_path / "conftest.py"), str(inner / "conftest.py")])
    assert python.shared_setup_files(str(tmp_path)) == (str(tmp_path / "conftest.py"),)
    plain = _adapter(Plain, tmp_path)
    assert plain.setup_files(str(tmp_path), []) == () and plain.shared_setup_files("x") == ()


@pytest.mark.parametrize("config", [
    ("pytest.ini", "[pytest]\npython_files = check_*.py\n"),
    ("pyproject.toml", '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n')])
def test_python_expands_a_dir_with_the_project_s_python_files(tmp_path, config):
    """pytest collects the directory with the project's configuration."""
    name, text = config
    (tmp_path / name).write_text(text, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "check_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    (tests / "test_b.py").write_text("def test_b():\n    pass\n", encoding="utf-8")
    assert _interpreted(tmp_path).test_files([str(tests)]) == [str(tests / "check_a.py")]
    assert _interpreted(tests).test_files([str(tests)]) == [str(tests / "test_b.py")]


def test_python_names_the_files_pytest_collects_not_those_a_pattern_matches(tmp_path):
    """A directory pytest does not enter and a test file without tests count
    for nothing. A test file pytest could not collect counts, after the
    collected ones. A file stays as given."""
    tests = tmp_path / "tests"
    (tests / "build").mkdir(parents=True)
    (tests / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    (tests / "test_empty.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "test_broken.py").write_text("import missing_module_xyz\n", encoding="utf-8")
    (tests / "build" / "test_c.py").write_text("def test_c():\n    pass\n", encoding="utf-8")
    assert _interpreted(tmp_path).test_files([str(tests), "other.py"]) == [
        str(tests / "test_a.py"), str(tests / "test_broken.py"), "other.py"]
    assert _interpreted(tmp_path).test_files([str(tests / "build" / "nothing")]) == [
        str(tests / "build" / "nothing")]


def test_python_names_why_pytest_could_not_collect_a_dir(tmp_path):
    """When pytest gives no account of a directory, as when a conftest.py that
    cannot import ends it, the error carries pytest's own diagnostic."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("import missing_conftest_dep_xyz\n", encoding="utf-8")
    (tests / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing_conftest_dep_xyz"):
        _interpreted(tmp_path).test_files([str(tests)])


@pytest.mark.parametrize("name", ["pytest.ini", "tox.ini"])
def test_python_lists_the_project_s_test_files_as_its_testpaths_bound_them(tmp_path, name):
    """pytest applies testpaths only to a collection that names no file or
    directory. The project's test files leave out a directory of test files its
    configuration does not name, such as type tests that do not collect,
    which test_files of the root names."""
    (tmp_path / name).write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    (tmp_path / "type_tests").mkdir()
    (tmp_path / "type_tests" / "test_types.py").write_text("reveal_type(1)\n", encoding="utf-8")
    adapter = _interpreted(tmp_path)
    assert adapter.project_test_files() == [str(tmp_path / "tests" / "test_a.py")]
    assert adapter.test_files([str(tmp_path)]) == [
        str(tmp_path / "tests" / "test_a.py"), str(tmp_path / "type_tests" / "test_types.py")]


def test_python_lists_the_same_files_as_test_files_without_testpaths(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for dir_name in ("tests", "other"):
        (tmp_path / dir_name).mkdir()
        (tmp_path / dir_name / f"test_{dir_name}.py").write_text(
            "def test_one():\n    pass\n", encoding="utf-8")
    adapter = _interpreted(tmp_path)
    assert adapter.project_test_files() == adapter.test_files([str(tmp_path)])
    assert len(adapter.project_test_files()) == 2


def test_java_and_the_default_list_the_project_s_test_files_as_test_files_of_its_root(tmp_path):
    """An adapter that does not override project_test_files gives test_files
    of its context's root, and the hook works by default."""
    from langadapter import hooks

    (tmp_path / "a").mkdir()
    for name in ("a/CalcTest.java", "a/test_x.pl"):
        (tmp_path / name).write_text("", encoding="utf-8")
    for cls in (JavaAdapter, Plain):
        adapter = _adapter(cls, tmp_path)
        assert adapter.project_test_files() == adapter.test_files([str(tmp_path)]) != []
    assert hooks.hook("project_test_files").works_by_default


def _interpreted(root):
    """A Python adapter for ``root`` that runs the calling interpreter."""
    import sys

    return PythonAdapter(lb.ProjectContext(str(root), interpreter=lb.Interpreter(path=sys.executable)))


def test_java_and_the_default_expand_a_dir_by_their_file_names(tmp_path):
    """Java collects the files whose names end in Test, Tests or TestCase."""
    for directory in ("a", "b"):
        (tmp_path / directory).mkdir()
    for name in ("a/CalcTest.java", "a/CalcTests.java", "b/ParserTestCase.java",
                 "Helper.java", "a/test_x.pl", "y.pl"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert _adapter(JavaAdapter, tmp_path).test_files([str(tmp_path), "Other.java"]) == [
        str(tmp_path / "a" / "CalcTest.java"), str(tmp_path / "a" / "CalcTests.java"),
        str(tmp_path / "b" / "ParserTestCase.java"), "Other.java"]
    assert _adapter(Plain, tmp_path).test_files([str(tmp_path)]) == [
        str(tmp_path / "a" / "test_x.pl")]


class _Run:
    def __init__(self, output, counts, runner_counts=None):
        self.output, self.counts, self.runner_counts = output, counts, runner_counts


def test_the_listed_failures_follow_each_language_s_rule(tmp_path):
    from types import SimpleNamespace

    from langadapter.outcomes import Outcome
    from langadapter.suites import TestId

    a, b, c = (TestId("tests/test_x.py", name) for name in ("test_a", "test_b", "test_c"))
    cases = (SimpleNamespace(test=a, outcome="failed"), SimpleNamespace(test=b, outcome="passed"),
             SimpleNamespace(test=c, outcome="error"))

    def run(outcome, tests=cases):
        return SimpleNamespace(outcome=outcome, tests=tests)

    python, java, plain = (_adapter(cls, tmp_path) for cls in (PythonAdapter, JavaAdapter, Plain))
    for outcome in Outcome:
        listed = (a, c)
        strict = listed if outcome in (Outcome.COMPLETED, Outcome.TEST_FAILURES) else None
        assert java.listed_failures(run(outcome)) == plain.listed_failures(run(outcome)) == strict
        untrusted = outcome in (Outcome.TIMED_OUT, Outcome.COLLECTION_FAILED, Outcome.NO_TESTS,
                                Outcome.MISSING_SETUP, Outcome.UNSUPPORTED)
        assert python.listed_failures(run(outcome)) == (None if untrusted else listed), outcome
    assert python.listed_failures(run(Outcome.INFRASTRUCTURE_FAILURE)) == (a, c)
    assert python.listed_failures(run(Outcome.COMPLETED, ())) is None
    assert java.listed_failures(run(Outcome.COMPLETED, ())) == ()


def test_collected_counts_every_test_with_an_outcome_and_none_unobserved(tmp_path):
    """One rule for every language: collected is the passed, failed,
    errored and skipped tests together (Python's less the files pytest
    could not collect, as its summary counts them), and a count the run did
    not observe is None, never 0."""
    run = _Run("", RunCounts(6, 2, 1, 1, 2), RunCounts(4, 2, 1, 1, 2))
    counted = {"collected": 6, "passed": 2, "failed": 1, "errors": 1}
    for cls in (JavaAdapter, Plain, PythonAdapter):
        assert _adapter(cls, tmp_path).run_counts(run) == counted, cls.__name__
    unobserved = dict.fromkeys(("collected", "passed", "failed", "errors"))
    for cls in (JavaAdapter, Plain, PythonAdapter):
        assert _adapter(cls, tmp_path).run_counts(_Run("", RunCounts())) == unobserved, cls.__name__


def test_python_counts_as_pytest_s_summary_and_the_default_as_the_run(tmp_path):
    """Python counts as pytest's own summary, from the run record
    (``runner_counts``), not from text a test printed. A run without a
    record counts nothing. Another language counts the run's report."""
    run = _Run("collected 2 items\nE   AssertionError: 7 passed, 4 errors\n"
               "\x1b[31m==== 2 failed in 0.05s ====\x1b[0m\n", RunCounts(5, 1, 1, 0, 0),
               RunCounts(2, 0, 2, 0, 1))
    assert _adapter(PythonAdapter, tmp_path).run_counts(run) == {
        "collected": 3, "passed": 0, "failed": 2, "errors": 0}
    assert _adapter(PythonAdapter, tmp_path).run_counts(_Run(run.output, run.counts)) == {
        "collected": None, "passed": None, "failed": None, "errors": None}
    assert _adapter(Plain, tmp_path).run_counts(run) == _adapter(JavaAdapter, tmp_path).run_counts(
        run) == {"collected": run.counts.passed + run.counts.failed + run.counts.errors
                 + run.counts.skipped, "passed": run.counts.passed,
                 "failed": run.counts.failed, "errors": run.counts.errors}
    assert pytest_counts("collected 5 items\n3 passed, 2 skipped in 0.04s\n") == {
        "collected": 5, "passed": 3, "failed": 0, "errors": 0}
    assert pytest_counts("2 skipped in 0.02s\n") == {
        "collected": 0, "passed": 0, "failed": 0, "errors": 0}
    assert pytest_counts("=== 1 passed, 2 errors in 0.40 seconds ===\n") == {
        "collected": 3, "passed": 1, "failed": 0, "errors": 2}
    # Only pytest's closing line counts, with its rules and colors, not a
    # test's own text that names counts.
    assert pytest_counts(
        "collected 2 items\nE   AssertionError: 7 passed, 4 errors\n"
        "\x1b[31m==== 2 failed in 0.05s ====\x1b[0m\n") == {
        "collected": 2, "passed": 0, "failed": 2, "errors": 0}


def _coverage():
    from langadapter.outcomes import FileIdentity
    from langadapter.runs import CoverageObservation
    from langadapter.suites import BranchItem

    branches = (BranchItem("calc.py", 2, 3), BranchItem("calc.py", 2, -1),
                BranchItem("calc.py", 5, 6), BranchItem("calc.py", 5, 7))
    return CoverageObservation(FileIdentity("calc.py", "0" * 64), (1, 2, 3, 5, 6, 7), (1, 2, 5, 6),
                               branches, (branches[0], branches[2]), traced_lines=(1, 2, 4, 5, 6))


def test_coverage_reads_as_each_language_s_tool_reports_it(tmp_path):
    coverage = _coverage()
    # Covered lines are covered statements, as for Java: traced line 4 is no
    # statement (a docstring's line, for one). An arc that leaves a code
    # object names its start line (here 1), so a reader can name the function.
    assert _adapter(PythonAdapter, tmp_path).coverage_texts(coverage) == {
        "covered_lines": (1, 2, 5, 6), "missing_branches": ("2->exit:1", "5->7")}
    assert _adapter(JavaAdapter, tmp_path).coverage_texts(coverage) == {
        "covered_lines": (1, 2, 5, 6),
        "missing_branches": ("2->1 of 2 branches untaken", "5->1 of 2 branches untaken")}
    assert _adapter(Plain, tmp_path).coverage_texts(coverage) == {
        "covered_lines": (1, 2, 5, 6), "missing_branches": ("2->-1", "5->7")}
    assert _adapter(Plain, tmp_path).coverage_texts(None) == {
        "covered_lines": (), "missing_branches": ()}


def test_a_delivered_test_reads_in_each_language_s_words(tmp_path):
    from langadapter.suites import TestId

    test = TestId("tests/test_calc.py", "TestCalc.test_adds", "2-3")
    assert _adapter(PythonAdapter, tmp_path).delivered_case(test, "xfailed") == (
        "test_calc.TestCalc.test_adds", "test_adds[2-3]", "skipped")
    java_test = TestId("demo/CalcTest.java", "CalcTest.adds", "[1]", text="demo.CalcTest#adds()")
    assert _adapter(JavaAdapter, tmp_path).delivered_case(java_test, "failed") == (
        "demo.CalcTest.adds", "adds[1]", "failed")
    assert _adapter(Plain, tmp_path).delivered_case(test, "xfailed") == (
        str(test), "test_adds2-3", "xfailed")


def _package(tmp_path, init):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text(init, encoding="utf-8")
    (package / "retry.py").write_text("def retry():\n    pass\n", encoding="utf-8")
    (package / "stop.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "top.py").write_text("y = 2\n", encoding="utf-8")
    return package


def test_python_s_import_line_binds_the_module_where_the_package_shadows_it(tmp_path):
    package = _package(tmp_path, "from pkg.retry import retry\n")
    root = str(tmp_path)  # asked on the class, before a project context exists
    line = PythonAdapter.import_statement
    assert line(str(package / "stop.py"), root) == "import pkg.stop as stop"
    assert line(str(package / "retry.py"), root) == (
        'import importlib\nretry = importlib.import_module("pkg.retry")')
    assert line(str(tmp_path / "top.py"), root) == "import top"
    assert line(str(tmp_path.parent / "elsewhere.py"), root) == "import elsewhere"
    (package / "__init__.py").write_text("from pkg.stop import *\n", encoding="utf-8")
    assert line(str(package / "stop.py"), root).startswith("import importlib\n")
    python = _adapter(PythonAdapter, tmp_path)
    assert python.sources.import_statement(str(package / "retry.py")).value == line(
        str(package / "retry.py"), root)


def test_python_s_import_line_does_not_shadow_the_standard_library(tmp_path):
    """A module named like a standard-library module or a built-in is bound
    as its package and name joined, in either form of the line, so it hides
    nothing of the standard library from the tests."""
    package = _package(tmp_path, "from pkg.dataclasses import field as dataclasses\n")
    for name in ("types", "dataclasses", "list"):
        (package / f"{name}.py").write_text("x = 1\n", encoding="utf-8")
    root = str(tmp_path)
    line = PythonAdapter.import_statement
    assert line(str(package / "types.py"), root) == "import pkg.types as pkg_types"
    assert line(str(package / "list.py"), root) == "import pkg.list as pkg_list"
    assert line(str(package / "dataclasses.py"), root) == (
        'import importlib\npkg_dataclasses = importlib.import_module("pkg.dataclasses")')
    assert line(str(package / "stop.py"), root) == "import pkg.stop as stop"


def test_a_package_s_init_imports_as_the_package(tmp_path):
    """Importing ``pkg/__init__.py`` as ``pkg.__init__`` would load the file
    a second time under another name, with classes of its own."""
    package = _package(tmp_path, "from pkg.retry import retry\n")
    (package / "sub").mkdir()
    (package / "sub" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    root = str(tmp_path)
    line = PythonAdapter.import_statement
    assert line(str(package / "__init__.py"), root) == "import pkg"
    assert line(str(package / "sub" / "__init__.py"), root) == "import pkg.sub as sub"
    (package / "__init__.py").write_text("from pkg.sub import x as sub\n", encoding="utf-8")
    assert line(str(package / "sub" / "__init__.py"), root) == (
        'import importlib\nsub = importlib.import_module("pkg.sub")')


def test_a_collection_error_is_no_collected_test():
    """pytest writes "collected 0 items / 1 error" for a file it could not
    collect: the 0 stands, and the error is not counted as a test."""
    assert pytest_counts("collected 0 items / 1 error\n\n1 error in 0.10s\n") == {
        "collected": 0, "passed": 0, "failed": 0, "errors": 1}
    assert pytest_counts("collected 3 items / 1 error\n1 error in 0.10s\n")["collected"] == 3
    quiet = ("_____ ERROR collecting tests/test_x.py _____\nImportError: no\n"
             "ERROR tests/test_x.py - ImportError: no\n"
             "ERROR tests/test_y.py::test_a - RuntimeError\n1 passed, 2 errors in 0.2s\n")
    assert pytest_counts(quiet) == {"collected": 2, "passed": 1, "failed": 0, "errors": 2}


@pytest.mark.parametrize("data", [
    b"package demo.calc;\n\npublic class Calc {}\n",
    b"\xef\xbb\xbfpackage demo.calc;\n\npublic class Calc {}\n",
    "// été\npackage demo.calc;\n\npublic class Calc {}\n".encode("latin-1"),
    b"/* header */ @Deprecated package demo . calc;\n\npublic class Calc {}\n",
], ids=["plain", "byte-order-mark", "latin-1", "annotated"])
def test_java_s_hint_names_the_package_a_test_declares(tmp_path, data):
    """A Java test in the target's package needs no import: the hint says
    so and names the package, read as Java reads the declaration, also
    after a byte-order mark, in a Latin-1 file or with an annotated
    declaration."""
    target = tmp_path / "Calc.java"
    target.write_bytes(data)
    assert JavaAdapter.import_statement(str(target), str(tmp_path)) == (
        "none; the test declares the target's package, demo.calc, and uses its "
        "classes directly")


def test_java_s_hint_for_the_default_package_and_an_unreadable_file(tmp_path):
    plain = tmp_path / "Plain.java"
    plain.write_text("public class Plain {}\n", encoding="utf-8")
    assert JavaAdapter.import_statement(str(plain), str(tmp_path)) == (
        "none; the test declares no package, as the target, and uses its classes directly")
    assert JavaAdapter.import_statement(str(tmp_path / "Missing.java"), str(tmp_path)) == ""


def test_the_jvm_tools_name_the_class_as_java_declares_its_package(tmp_path):
    """JaCoCo's and PIT's target class read the package the same way."""
    from langadapter.java import _jacoco

    target = tmp_path / "Calc.java"
    target.write_bytes(b"\xef\xbb\xbf/* header */ @Deprecated package demo.calc;\n"
                       b"\npublic class Calc {}\n")
    assert _jacoco.class_name(target) == "demo.calc.Calc"


def test_java_needs_no_import_line_and_the_default_offers_none(tmp_path):
    assert JavaAdapter.import_statement("Calc.java", str(tmp_path)) == ""
    plain = _adapter(Plain, tmp_path)
    assert Plain.import_statement("x.pl", str(tmp_path)) is None
    assert plain.code_smells(["x.pl"]) is None
    supported = {o.name for o in plain.operation_capabilities() if o.supported}
    assert not {"sources.import_statement", "sources.code_smells"} & supported
    python = {o.name for o in _adapter(PythonAdapter, tmp_path).operation_capabilities()
              if o.supported}
    java = {o.name for o in _adapter(JavaAdapter, tmp_path).operation_capabilities()
            if o.supported}
    assert {"sources.import_statement", "sources.code_smells"} <= python
    assert "sources.import_statement" in java and "sources.code_smells" not in java


@pytest.mark.parametrize("answer, count", [
    ((1, '[{"code": "F401"}, {"code": "E501"}, {"code": "W292"}]'), 3),
    ((0, ""), 0),
    (FileNotFoundError("ruff"), None),
    (subprocess.TimeoutExpired("ruff", 60), None),
    ((1, ""), None),
    ((2, ""), None),
    ((0, "not json"), None),
], ids=["findings", "no findings", "no ruff", "timeout", "module missing", "ruff error",
        "unreadable report"])
def test_python_counts_only_a_report_ruff_gave(tmp_path, monkeypatch, answer, count):
    """Without ruff, after a ruff error, a timeout or with an unreadable
    report the count is absent, not zero. "python -m ruff" without ruff exits
    1 with no report."""
    from types import SimpleNamespace

    from langadapter.python import _sources

    def run(*args, **kwargs):
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(returncode=answer[0], stdout=answer[1], stderr="")

    monkeypatch.setattr(_sources.subprocess, "run", run)
    source = tmp_path / "smelly.py"
    source.write_text("import os\n", encoding="utf-8")
    assert _adapter(PythonAdapter, tmp_path).code_smells([str(source)]) == count


def test_python_counts_ruff_s_findings(tmp_path):
    source = tmp_path / "smelly.py"
    source.write_text("import os\nimport sys\n", encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("x = 1\n", encoding="utf-8")
    python = _adapter(PythonAdapter, tmp_path)
    assert python.code_smells([]) == 0
    if importlib.util.find_spec("ruff") is None:
        # ruff is a development extra: without it there is no count.
        assert python.code_smells([str(source)]) is None
        assert python.sources.code_smells([str(source)]).status.value == "missing_setup"
        return
    assert python.code_smells([str(source)]) == 2
    assert python.code_smells([str(clean)]) == 0
    assert python.sources.code_smells([str(source)]).value == 2


def _python_with_interpreter(root):
    import sys
    return PythonAdapter(lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable)))


def test_python_s_source_hooks_are_today_s_static_paths(tmp_path):
    python = _python_with_interpreter(tmp_path)
    assert python.skip_removal("import pytest\npytest.importorskip('nope')\nx = 1\n") == (
        "import pytest\nx = 1\n", 1)
    valid = python.salvage_report("x = 1\n")
    assert (valid.source, valid.status, valid.repairs, valid.message) == ("x = 1\n", "valid", 0, "")
    repaired = python.salvage_report("def f():\n\nx = 1\n")
    assert repaired.status == "repaired" and repaired.repairs == 1
    assert "pass" in repaired.source
    resolved = python.collision_resolution(["def helper():\n    return 1\n\ndef test_a():\n"
                                            "    assert helper()\n",
                                            "def helper():\n    return 2\n\ndef test_b():\n"
                                            "    assert helper()\n"])
    assert resolved.completed and len(resolved.value.sources) == 2
    assert python.collision_dependents("def helper():\n    pass\n\ndef test_a():\n    helper()\n",
                                       "helper", {"test_a": {"L:x:1"}}) == ("test_a",)


def test_a_language_without_a_repair_judges_a_source_as_it_stands(tmp_path):
    """Without salvage_syntax a source the language's structure check
    accepts stays valid and one it rejects is beyond repair. Without any
    check the run judges it, so it stays."""
    class Checked(LangAdapter):
        language = "checked"
        file_extensions = (".checked",)

        def check_structure(self, source):
            if source == "unreadable":
                raise OSError("parser gone")
            return StructureCheck(source != "broken", "invalid" if source == "broken" else "",
                                  0, 0, "fixture-parser")

    class Validated(LangAdapter):
        language = "validated"
        file_extensions = (".validated",)

        def validate_source(self, source):
            pytest.fail("a validator without its runtime ran")

    checked = Checked(lb.ProjectContext(str(tmp_path)))
    assert checked.salvage_report("fine") == SalvageReport("fine", "valid", 0, "")
    assert checked.salvage_report("broken") == SalvageReport(None, "beyond_repair", 0, "")
    assert _adapter(Plain, tmp_path).salvage_report("any") == SalvageReport("any", "valid", 0, "")
    # A check that could not run gives no verdict.
    assert checked.salvage_report("unreadable") == SalvageReport(
        None, "validator_failed", 0, "OSError: parser gone")
    assert Validated(lb.ProjectContext(str(tmp_path))).salvage_report("any") == SalvageReport(
        None, "validator_failed", 0, "no project runtime is available")


def test_a_repair_s_count_is_the_result_s_repairs(tmp_path):
    """An applied repair's count is SourceResult.repairs, not a number read
    from its detail: a repair without one counts none."""
    import sys

    class Repairing(LangAdapter):
        language = "repairing"
        file_extensions = (".repairing",)

        def salvage_syntax(self, source):
            if source == "counted":
                return SourceResult("applied", "fixed", "fixture-parser", detail="two lines", repairs=2)
            return SourceResult("applied", "fixed", "fixture-parser")

    adapter = Repairing(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    assert adapter.salvage_report("counted") == SalvageReport("fixed", "repaired", 2, "two lines")
    assert adapter.salvage_report("plain") == SalvageReport("fixed", "repaired", 0, "")


@pytest.mark.parametrize("status, reported", [
    ("timeout", "validator_failed"), ("missing_setup", "validator_failed"),
    ("unsupported", "validator_failed"), ("infrastructure_failure", "validator_failed"),
    ("refused", "beyond_repair"), ("not_repaired", "beyond_repair"),
    ("parse_error", "beyond_repair")])
def test_a_repair_that_could_not_run_is_a_check_that_failed(tmp_path, status, reported):
    """A repair the language could not run, for want of time, setup or an
    allowed runtime, is a check that failed, not a source beyond repair."""
    import sys

    class Unrun(LangAdapter):
        language = "unrun"
        file_extensions = (".unrun",)

        def salvage_syntax(self, source):
            return SourceResult(status, source, "fixture-parser", detail="the reason")

    adapter = Unrun(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    assert adapter.salvage_report("source") == SalvageReport(None, reported, 0, "the reason")


def test_the_warnings_of_an_unsound_test_are_the_language_s_fact(tmp_path):
    """A language declares them in its facts, empty by default. Python's
    match a never-awaited coroutine's warning, a destroyed pending task's
    message and pytest's unraisable-exception warning. The hook table names
    the fact, and the mock warnings in an entry of their own."""
    import re

    from langadapter import hooks
    from langadapter.capabilities import LanguageFacts

    assert LanguageFacts("toy", "toy", "toy", None, (), "", None, None).unsound_test_patterns == ()
    patterns = _python_with_interpreter(tmp_path).language_facts().unsound_test_patterns
    found = [[bool(re.search(pattern, text)) for pattern in patterns] for text in (
        "test_x.py:3: RuntimeWarning: coroutine 'f' was never awaited",
        "Task was destroyed but it is pending!",
        "PytestUnraisableExceptionWarning: Exception ignored in: <function f>")]
    assert found == [[True, False, False], [False, True, False], [False, False, True]]
    assert hooks.hook("unsound_test_patterns").kind == "fact"
    assert hooks._present(PythonAdapter, hooks.hook("order_dependent_mock_warnings"))
    for removed in ("unsound_warnings", "unsound_sources"):
        assert not hasattr(LangAdapter, removed) and not hasattr(PythonAdapter, removed)
        with pytest.raises(KeyError):
            hooks.hook(removed)


def test_each_source_hook_a_c_adapter_takes_from_python_is_python_s():
    """A C adapter with Python tests takes Python's source operations by
    name, so each name it lists is a hook of Python's adapter."""
    assert [name for name in _PYTHON_SOURCE_HOOKS
            if not callable(getattr(PythonAdapter, name, None))] == []


def test_a_merge_is_the_operation_s_with_the_project_interpreter_s_check(tmp_path):
    """sources.merge_sources, checked by the project interpreter, gives the
    in-process merge's module and refuses one the interpreter rejects. No
    adapter offers a second merge."""
    from langadapter.adapter import LangAdapter
    from langadapter.python import source_analysis

    first = "import os\n\ndef test_a():\n    assert os\n"
    second = "import sys\n\ndef test_b():\n    assert sys\n"
    separator = "# === merged ==="
    python = _python_with_interpreter(tmp_path)
    merged = python.sources.merge_sources(first, second, separator=separator)
    assert merged.status == "applied" and separator in merged.source
    assert merged.source == source_analysis.merge_sources(first, second,
                                                          separator=separator).source
    broken = python.sources.merge_sources(first, "def test_c(:\n    pass\n",
                                          separator=separator)
    assert broken.status == "parse_error" and broken.source == first
    for cls in (LangAdapter, PythonAdapter, JavaAdapter):
        assert not hasattr(cls, "source_merge"), cls.__name__


class _Collection:
    def __init__(self, outcome, reason, output, diagnostic):
        self.outcome, self.reason, self.output, self.diagnostic = outcome, reason, output, diagnostic


def test_a_collection_reads_in_each_language_s_words(tmp_path):
    from langadapter.outcomes import Outcome

    failed = _Collection(Outcome.COLLECTION_FAILED, "does not compile", "x" * 700 + "\n",
                         "ImportError: nope")
    assert _adapter(PythonAdapter, tmp_path).collection_text(failed) == "ImportError: nope"
    assert _adapter(JavaAdapter, tmp_path).collection_text(failed) == "does not compile: " + "x" * 600
    done = _Collection(Outcome.COMPLETED, "", "", "")
    assert _adapter(JavaAdapter, tmp_path).collection_text(done) == ""
    assert _adapter(Plain, tmp_path).collection_text(failed) == "ImportError: nope"


def test_mutation_settings_are_each_tool_s(tmp_path):
    from langadapter.mutation import Catalogue

    python, java, plain = (_adapter(cls, tmp_path) for cls in (PythonAdapter, JavaAdapter, Plain))
    assert python.mutation_catalogue({"cosmic_ray_operators": ""}) == Catalogue()
    assert python.mutation_catalogue({"cosmic_ray_operators": "wong_mathur_selective"}) == (
        Catalogue(name="wong_mathur_selective"))
    assert python.named_operators("wong_mathur_selective")[0] == (
        "core/ReplaceBinaryOperator_Add_Sub")
    assert java.mutation_catalogue({"pit_mutators": "STRONGER"}) == Catalogue(name="STRONGER")
    assert java.mutation_catalogue({"pit_mutators": "MATH, VOID_METHOD_CALLS"}) == (
        Catalogue(operators=("MATH", "VOID_METHOD_CALLS")))
    assert java.named_operators("STRONGER") == ["STRONGER"]
    assert (java.own_mutant_timeout_rule, python.own_mutant_timeout_rule) == (True, False)
    assert (python.coverage_matrix_in_place, java.coverage_matrix_in_place) == (True, False)
    attempts = python.mutation_session_attempts(
        {"allow_sequential_fallback": "true"}, configured_workers=4,
        effective_workers=4, cache_base=str(tmp_path))
    assert [(a.options["distributor"], a.workers, a.parallel) for a in attempts] == [
        ("http", 4, True), ("local", 1, False)]
    assert attempts[0].options["allow_fewer_workers"] is True and attempts[0].warning == ""
    assert "({reason})" in attempts[1].warning and "about 4 times slower" in attempts[1].warning
    single = python.mutation_session_attempts({}, configured_workers=1, effective_workers=1,
                                              cache_base=str(tmp_path))
    assert [(a.options["distributor"], a.workers) for a in single] == [("local", 1)]
    with pytest.raises(ValueError, match="cosmic_ray_distributor = local runs one mutant"):
        python.mutation_session_attempts({"cosmic_ray_distributor": "local"},
                                         configured_workers=3, effective_workers=3,
                                         cache_base=str(tmp_path))
    assert python.mutation_workers_refusal({"cosmic_ray_distributor": "local",
                                            "engine_workers": 2}).startswith(
        "cosmic_ray_distributor = local runs one mutant at a time")
    assert python.mutation_workers_refusal({}) is None
    assert python.mutation_dir_refusal({}, create=True) is None
    assert [(a.options, a.workers, a.parallel) for a in java.mutation_session_attempts(
        {}, configured_workers=0, effective_workers=0, cache_base="")] == [
        ({"workers": 1}, 1, False)]
    assert plain.mutation_catalogue({}) == Catalogue() and plain.mutation_dir_refusal(
        {}, create=False) is None


def _interpreter(env_dir):
    """A file where an environment in ``env_dir`` keeps its interpreter."""
    import os

    executable = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    return executable


def test_python_s_runtime_is_today_s(tmp_path):
    from langadapter.context import ProvisioningRequest, TestFrameworkConfig

    python = PythonAdapter  # asked on the class, before a project context exists
    names = [setting.name for setting in python.project_settings]
    assert names == ["python_executable", "python_version", "requirements",
                     "python_import_root", "python_test_environment",
                     "frozen_module_strategy", "pytest_config"]
    # How a caller resolves a relative host path in each.
    assert {s.name: s.host_path for s in python.project_settings if s.host_path} == {
        "python_executable": "path", "requirements": "host file"}
    chosen = python.discover_runtime(str(tmp_path), {
        "python_executable": "/opt/py/bin/python3", "frozen_module_strategy": "unfreeze",
        "pytest_config": "none"}, container=True)
    assert chosen.interpreter.path == "/opt/py/bin/python3"
    assert chosen.interpreter_flags == ("-X", "frozen_modules=off")
    assert chosen.test_framework == TestFrameworkConfig(source="isolated")
    assert chosen.helper_python == "/opt/py/bin/python3"
    provisioned = python.discover_runtime(str(tmp_path), {
        "python_version": "3.12", "requirements": "req.txt", "frozen_module_strategy": "skip"})
    assert provisioned.interpreter.provisioning == ProvisioningRequest("3.12", "req.txt")
    assert provisioned.interpreter_flags == () and provisioned.helper_python is None
    assert provisioned.test_framework == TestFrameworkConfig(path=None)
    venv = _interpreter(tmp_path / ".venv")
    found = python.discover_runtime(str(tmp_path), {"pytest_config": "cfg/pytest.ini"})
    assert found.interpreter.path == str(venv)
    assert found.test_framework == TestFrameworkConfig(path="cfg/pytest.ini")
    assert python.discover_runtime(str(tmp_path), {"python_executable": r"C:\py\python.exe"},
                                   container=True).helper_python is None
    assert python.context_environment({"maven": "mvn"}) == {}


def test_python_s_selection_follows_the_rules_and_refuses_by_its_setting(tmp_path, monkeypatch):
    """A host path is read through resolve_path and must exist. Without a
    selection the project's only environment runs it, and none, several,
    the caller's own outside the project alone, or none in a container are
    refused, each naming python_executable. Beside a project environment
    the caller's own is left aside."""
    import sys

    python = PythonAdapter
    project = tmp_path / "project"
    project.mkdir()
    given = _interpreter(tmp_path / "envs" / "py")
    relative = given.relative_to(tmp_path).as_posix()
    chosen = python.discover_runtime(str(project), {"python_executable": relative},
                                     resolve_path=lambda value: tmp_path / value)
    assert chosen.interpreter.path == str(given)
    with pytest.raises(InvalidRequest, match="points at .*missing.*, which does not exist") as refused:
        python.discover_runtime(str(project), {"python_executable": "missing/python"},
                                resolve_path=lambda value: tmp_path / value)
    assert refused.value.field == "python_executable"
    with pytest.raises(InvalidRequest, match="no interpreter is selected, and no project .venv") as refused:
        python.discover_runtime(str(project), {}, environment={})
    assert refused.value.field == "python_executable" and ".venv" in refused.value.message
    with pytest.raises(InvalidRequest, match="in the container: select one") as refused:
        python.discover_runtime(str(project), {}, container=True)
    assert refused.value.field == "python_executable"
    # The activated environment runs the project when it is the only one.
    other = tmp_path / "other"
    activated = _interpreter(other)
    assert python.discover_runtime(str(project), {}, environment={
        "VIRTUAL_ENV": str(other)}).interpreter.path == str(activated)
    venv = _interpreter(project / ".venv")
    with pytest.raises(InvalidRequest, match="several interpreters could run the project"):
        python.discover_runtime(str(project), {}, environment={"VIRTUAL_ENV": str(other)})
    # The caller's own environment, activated, is refused outside the
    # project and runs a project it lies inside.
    monkeypatch.setattr(sys, "prefix", str(other))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(InvalidRequest, match="the one the caller runs in") as refused:
        python.discover_runtime(str(plain), {}, environment={"VIRTUAL_ENV": str(other)})
    assert refused.value.field == "python_executable"
    # Beside the project's .venv it is no second candidate: a tool run from
    # its own activated environment runs the project with the .venv.
    assert python.discover_runtime(str(project), {}, environment={
        "VIRTUAL_ENV": str(other)}).interpreter.path == str(venv)
    tools = plain / "tools-env"
    inside = _interpreter(tools)
    monkeypatch.setattr(sys, "prefix", str(tools))
    assert python.discover_runtime(str(plain), {}, environment={
        "VIRTUAL_ENV": str(tools)}).interpreter.path == str(inside)


def test_java_s_runtime_and_tool_variables_are_today_s(tmp_path):
    import os

    java = JavaAdapter  # asked on the class, before a project context exists
    assert [s.name for s in java.project_settings] == ["java_home", "build_java_home",
                                                         "maven", "gradle"]
    assert java.project_settings[0].alias == "JAVA_HOME"
    assert [s.host_path for s in java.project_settings] == ["path", "path", "command", "command"]
    name = "java.exe" if os.name == "nt" else "java"
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / name).write_text("")
    host = java.discover_runtime(str(tmp_path), {}, environment={"JAVA_HOME": str(home)})
    assert host.interpreter.path == str(home / "bin" / name)
    given = java.discover_runtime(str(tmp_path), {"java_home": "jdk"},
                                  resolve_path=lambda value: tmp_path / value)
    assert given.interpreter.path == str(home / "bin" / name)
    executable = java.discover_runtime(str(tmp_path), {"java_home": str(home / "bin" / name)})
    assert executable.interpreter.path == str(home / "bin" / name)
    # A JDK home named java is a directory, not the executable.
    named = tmp_path / "jvm" / "java"
    (named / "bin").mkdir(parents=True)
    (named / "bin" / name).write_text("")
    assert java.discover_runtime(str(tmp_path), {"java_home": str(named)}).interpreter.path == (
        str(named / "bin" / name))
    with pytest.raises(InvalidRequest, match="names no JDK: .* does not exist") as refused:
        java.discover_runtime(str(tmp_path), {"java_home": str(tmp_path / "jre")})
    assert refused.value.field == "java_home"
    with pytest.raises(InvalidRequest, match="no JDK is selected for the project: select one, "
                       "or set JAVA_HOME"):
        java.discover_runtime(str(tmp_path), {}, environment={})
    container = java.discover_runtime(str(tmp_path), {"java_home": "/opt/jdk"}, container=True,
                                  environment={"JAVA_HOME": "/ignored"})
    assert container.interpreter.path == "/opt/jdk/bin/java"
    assert java.discover_runtime(str(tmp_path), {"java_home": "//opt/jdk/bin/java"},
                                 container=True).interpreter.path == "/opt/jdk/bin/java"
    with pytest.raises(InvalidRequest, match="in the container: select one") as refused:
        java.discover_runtime(str(tmp_path), {}, container=True, environment={"JAVA_HOME": "/ignored"})
    assert refused.value.field == "java_home"
    for path in ("/opt/../jdk", "opt/jdk", "C:\\jdk"):
        with pytest.raises(InvalidRequest, match="a container JDK is an absolute POSIX path"):
            java.discover_runtime(str(tmp_path), {"java_home": path}, container=True)
    assert java.context_environment({"build_java_home": "/jdk17", "maven": " ", "gradle": "gw"}) == {
        "LANGADAPTER_BUILD_JAVA_HOME": "/jdk17", "LANGADAPTER_GRADLE": "gw"}


def test_python_s_description_reads_the_selection_without_checking_or_discovering(tmp_path):
    """describe_runtime states what the settings select, as a setup reports
    it before anything runs: a selected address that need not exist, an
    unexecuted provisioning request, no interpreter without a selection
    (where discover_runtime would find the project's .venv)."""
    from langadapter.context import Interpreter, ProvisioningRequest

    python = PythonAdapter
    _interpreter(tmp_path / ".venv")
    resolve = lambda value: tmp_path / value  # noqa: E731
    described = python.describe_runtime(str(tmp_path), {"python_executable": "env/bin/python",
                                                        "frozen_module_strategy": "unfreeze"},
                                        resolve_path=resolve)
    assert described.interpreter == Interpreter(path=str(tmp_path / "env" / "bin" / "python"))
    assert described.interpreter_flags == ("-X", "frozen_modules=off")
    provisioned = python.describe_runtime(str(tmp_path), {"python_version": "3.11",
                                                          "requirements": "r.txt"})
    assert provisioned.interpreter.provisioning == ProvisioningRequest("3.11", "r.txt")
    assert python.describe_runtime(str(tmp_path), {}).interpreter is None
    assert python.describe_runtime(str(tmp_path), {}, container=True).interpreter is None
    container = python.describe_runtime(str(tmp_path), {"python_executable": "/t/bin/python"},
                                    container=True)
    assert container.interpreter.path == "/t/bin/python" and container.helper_python == "/t/bin/python"


def test_java_s_description_reads_the_selection_without_checking_it(tmp_path):
    import os

    java = JavaAdapter
    name = "java.exe" if os.name == "nt" else "java"
    resolve = lambda value: tmp_path / value  # noqa: E731
    for selected, expected in (("jdk", tmp_path / "jdk" / "bin" / name),
                               ("jdk/bin/java", tmp_path / "jdk" / "bin" / "java"),
                               ("jdk/bin/java.exe", tmp_path / "jdk" / "bin" / "java.exe")):
        described = java.describe_runtime(str(tmp_path), {"java_home": selected},
                                          resolve_path=resolve)
        assert described.interpreter.path == str(expected), selected
    host = java.describe_runtime(str(tmp_path), {}, environment={"JAVA_HOME": str(tmp_path / "h")})
    assert host.interpreter.path == str(tmp_path / "h" / "bin" / name)
    assert java.describe_runtime(str(tmp_path), {}, environment={}).interpreter is None
    assert java.describe_runtime(str(tmp_path), {}, container=True,
                                 environment={"JAVA_HOME": "/ignored"}).interpreter is None
    assert java.describe_runtime(str(tmp_path), {"java_home": "/opt/jdk"},
                                 container=True).interpreter.path == "/opt/jdk/bin/java"
    with pytest.raises(InvalidRequest, match="a container JDK is an absolute POSIX path"):
        java.describe_runtime(str(tmp_path), {"java_home": "opt/jdk"}, container=True)


def test_a_language_without_its_own_description_describes_what_it_finds():
    from langadapter.adapter import LangAdapter, RuntimeChoice

    class Found(LangAdapter):
        @classmethod
        def discover_runtime(cls, root, values, **options):
            return RuntimeChoice(interpreter_flags=("found",))

    assert Found.describe_runtime(".", {}).interpreter_flags == ("found",)


def test_the_default_runtime_is_the_runtime_fact_on_path(tmp_path):
    import shutil
    import sys

    class Found(LangAdapter):
        language, file_extensions, runtime = "found", (".fd",), sys.executable

    class Missing(LangAdapter):
        language, file_extensions, runtime = "missing", (".ms",), "no-such-program-xyz"

    assert Found.discover_runtime(".", {}).interpreter.path == sys.executable
    assert _adapter(Found, tmp_path).discover_runtime(".", {}).interpreter.path == sys.executable
    assert Missing.discover_runtime(".", {}).interpreter is None
    assert Plain.discover_runtime(".", {}).interpreter is None
    assert Plain.context_environment({}) == {}
    assert shutil.which("no-such-program-xyz") is None


def test_the_runtime_hooks_must_be_class_methods():
    with pytest.raises(TypeError) as refused:
        class OnInstance(LangAdapter):
            language = "on-instance"

            def discover_runtime(self, root, values, *, container=False, environment=None,
                                 resolve_path=None):
                return None

    assert ("hook discover_runtime: OnInstance.discover_runtime is called on the class, "
            "before a project context exists: make it a classmethod.") in str(refused.value)

    with pytest.raises(TypeError, match="hook import_statement: OnInstanceLine.import_statement "
                                        "is called on the class"):
        class OnInstanceLine(LangAdapter):
            language = "on-instance-line"

            def import_statement(self, target, root):
                return ""

    class Static(LangAdapter):
        language = "static"

        @staticmethod
        def context_environment(values, *, container=False):
            return {}

    assert Static.context_environment({}) == {}


class _Machine:
    """A probe of a fixed machine for the setup questions: the real file
    system, interpreters that answer by their path, a JDK release file."""

    def __init__(self, env=None, versions=None, missing=(), which=None):
        self.env = dict(env or {})
        self.versions = dict(versions or {})
        self.missing = list(missing)
        self.paths = dict(which or {})

    def is_dir(self, path):
        return path.is_dir()

    def is_file(self, path):
        return path.is_file()

    def which(self, name):
        return self.paths.get(name, "")

    def environment(self, name):
        return self.env.get(name, "")

    def read_text(self, path):
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def run(self, argv, timeout):
        if "version_info" in argv[2]:
            version = self.versions.get(argv[0])
            return (0, version + "\n") if version else None
        return 0, " ".join(m for m in ("pytest", "coverage", "cosmic_ray")
                           if m.replace("_", "-") in self.missing or m in self.missing) + "\n"


def test_a_language_without_its_own_questions_is_asked_its_declared_settings(tmp_path):
    from langadapter.adapter import ProjectSetting

    class Settled(LangAdapter):
        language = "settled"
        project_settings = (ProjectSetting("settled_mode", "How it runs.", "choice",
                                           default="fast", choices=("fast", "slow")),)

    (question,) = Settled.setup_questions(tmp_path, _Machine())
    assert (question.key, question.prompt, question.setting, question.default,
            question.choices, question.check) == (
        "settled.settled_mode", "How it runs.", "settled_mode", "fast", ("fast", "slow"), None)
    assert Settled.setup_problem("settled_mode", "slow", tmp_path, _Machine()) is None


def test_a_settings_kind_decides_where_the_language_checks_nothing_itself(tmp_path):
    """The default check answers by the declared kind, and Python hands it
    every setting it does not check itself."""
    from langadapter.adapter import ProjectSetting

    class Settled(LangAdapter):
        language = "settled"
        project_settings = (
            ProjectSetting("settled_mode", "How it runs.", "choice", choices=("fast", "slow")),
            ProjectSetting("settled_config", "Its file.", "path"),
            ProjectSetting("settled_runner", "Its program.", "executable"))

    (tmp_path / "present.cfg").write_text("", encoding="utf-8")
    machine = _Machine(which={"runner": "/usr/bin/runner"})
    assert [Settled.setup_problem(name, value, tmp_path, machine) for name, value in (
        ("settled_mode", "medium"), ("settled_config", "absent.cfg"),
        ("settled_config", "present.cfg"), ("settled_runner", "absent"),
        ("settled_runner", "runner"), ("settled_mode", ""), ("undeclared", "x"))] == [
        "The value must be one of fast, slow.", "No such file or directory.", None,
        "No such program.", None, None, None]
    assert PythonAdapter.setup_problem("pytest_config", "absent.ini", tmp_path, machine) == (
        "No such file or directory.")


def test_java_checks_a_jdk_by_its_home_or_its_java_executable(tmp_path):
    """The JDK setting takes a home or its java executable, as the runs read
    it, and a blank value, which uses JAVA_HOME, is not checked."""
    from langadapter.java._questions import OLDEST_JDK

    home, old = tmp_path / "jdk", tmp_path / "old"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("", encoding="utf-8")
    (home / "release").write_text(f'JAVA_VERSION="{OLDEST_JDK}.0.2"\n', encoding="utf-8")
    old.mkdir()
    (old / "release").write_text('JAVA_VERSION="1.8.0_401"\n', encoding="utf-8")
    assert [JavaAdapter.setup_problem("java_home", value, tmp_path, _Machine()) for value in (
        str(home), str(home / "bin" / "java"), "", str(old))] == [
        None, None, None, f"a JDK {OLDEST_JDK} or later is needed"]
    # The build tool's JDK, which its check text names, any version.
    assert [JavaAdapter.setup_problem("build_java_home", value, tmp_path, _Machine())
            for value in (str(old), "", str(tmp_path / "absent"))] == [
        None, None, "no JDK whose release file names a version"]


def test_python_asks_the_import_dir_and_an_interpreter_that_runs_and_is_supported(tmp_path):
    """Python's questions as Testuary's setup asked them: the import directory
    (src when it exists), then the interpreter, offered from the project's
    environments and PATH when they run and the release supports them, with
    its version and the missing tools after the answer."""
    import sys

    (tmp_path / "src").mkdir()
    venv = tmp_path / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    venv.parent.mkdir(parents=True)
    venv.write_text("", encoding="utf-8")
    old = str(tmp_path / "old-python")
    machine = _Machine(versions={str(venv): "3.12.4", old: "2.7.18", sys.executable: "3.12.4"},
                       missing=("cosmic-ray",), which={"python3": old})
    root, interpreter = PythonAdapter.setup_questions(tmp_path, machine, consumer="Testuary")
    assert (root.key, root.setting, root.default) == ("python.import_root",
                                                      "python_import_root", "src")
    assert root.reason.startswith("Testuary imports the source files from there")
    assert root.check("missing") == "no such directory" and root.check(".") is None
    assert root.written(".") == "" and root.written("src") == "src"
    assert (interpreter.key, interpreter.setting, interpreter.default, interpreter.open) == (
        "python.interpreter", "python_executable", str(venv), True)
    assert interpreter.choices() == (str(venv), sys.executable)
    # One text for a refused Python: the profile's, as the setup check gives it.
    assert interpreter.check(old) == PythonProfile(
        python_line("2.7.18", "probe"), PythonAdapter.support).refusal() != ""
    assert interpreter.check(str(venv)) is None
    note = interpreter.after(str(venv))
    assert note.lines == ("  Python 3.12.4", "  Not installed in this interpreter: cosmic-ray. "
                                             "A run needs them there.")
    # The version LangAdapter pins for that Python.
    from langadapter.python import _pinned_tools

    assert note.missing == (f"install cosmic-ray=={_pinned_tools('3.12.4')['cosmic-ray']} in {venv}",)
    assert PythonAdapter.setup_problem("python_import_root", "none", tmp_path, machine) is None
    assert PythonAdapter.setup_problem("python_executable", "gone", tmp_path, machine) == (
        "this interpreter does not run")


def test_python_defaults_to_the_interpreter_inspection_discovers(tmp_path):
    """The interpreter question's default follows inspection's rule
    (_discover_environments): the project's .venv or one activated
    environment, never the caller's own, and none when several or none are
    found. The other interpreters stay among the choices."""
    import sys

    script = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"

    def environment(name):
        path = tmp_path / name / script
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
        return path

    def asked(env=None):
        _root, interpreter = PythonAdapter.setup_questions(tmp_path, _Machine(env=env),
                                                           consumer="Testuary")
        return interpreter

    nothing = asked()
    assert nothing.default == "" and "interpreter Testuary runs in" in nothing.reason
    # The environment this process runs in, activated outside the project.
    own = asked({"VIRTUAL_ENV": sys.prefix})
    assert own.default == "" and "activated in the shell" in own.reason
    environment("venv")
    assert asked().default == ""
    activated = environment("activated")
    named = {"VIRTUAL_ENV": str(activated.parent.parent)}
    assert asked(named).default == str(activated)
    project = environment(".venv")
    assert asked().default == str(project) and asked().reason == ""
    several = asked(named)
    assert several.default == "" and str(project) in several.reason
    assert str(activated) in several.reason


def test_java_asks_the_jdk_and_the_build_tool_the_project_lacks_a_wrapper_for(tmp_path):
    jdk = tmp_path / "jdk"
    jdk.mkdir()
    (jdk / "release").write_text('JAVA_VERSION="21.0.2"\n', encoding="utf-8")
    old = tmp_path / "jdk8"
    old.mkdir()
    (old / "release").write_text('JAVA_VERSION="1.8.0_402"\n', encoding="utf-8")
    machine = _Machine(env={"JAVA_HOME": str(jdk)}, which={"mvn": "/usr/bin/mvn"})
    items = list(JavaAdapter.setup_questions(tmp_path, machine))
    assert [type(item).__name__ for item in items] == ["SetupQuestion", "SetupNote"]
    home, note = items
    assert (home.key, home.setting, home.default, home.reason) == (
        "java.jdk", "java_home", str(jdk), "from JAVA_HOME")
    assert home.choices() == (str(jdk),)
    assert home.check(str(old)) == "a JDK 11 or later is needed" and home.check(str(jdk)) is None
    assert note.missing == (f"a pom.xml or build.gradle in {tmp_path}",)
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    home, build = JavaAdapter.setup_questions(tmp_path, machine)
    assert (build.key, build.setting, build.default) == ("java.build", "maven", "/usr/bin/mvn")
    assert build.prompt == "The maven executable (the project has no maven wrapper)"
    assert build.check(str(tmp_path / "missing")) == "no such file"
    # The runs' rule: only Maven's own wrapper in this platform's form spares
    # the question, not Gradle's, nor Maven's of the other platform.
    windows = os.name == "nt"
    for name in ("gradlew.bat" if windows else "gradlew", "mvnw" if windows else "mvnw.cmd"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert len(list(JavaAdapter.setup_questions(tmp_path, machine))) == 2
    (tmp_path / ("mvnw.cmd" if windows else "mvnw")).write_text("", encoding="utf-8")
    assert len(list(JavaAdapter.setup_questions(tmp_path, machine))) == 1


def test_the_jdks_offered_are_java_home_then_the_java_on_path(tmp_path):
    """Each once: a java on PATH inside JAVA_HOME adds nothing."""
    base = tmp_path.resolve()
    home, other = base / "jdk21", base / "jdk17"
    java = other / "bin" / "java"
    java.parent.mkdir(parents=True)
    question = next(iter(JavaAdapter.setup_questions(
        tmp_path, _Machine(env={"JAVA_HOME": str(home)}, which={"java": str(java)}))))
    assert question.choices() == (str(home), str(other))
    inside = next(iter(JavaAdapter.setup_questions(
        tmp_path, _Machine(env={"JAVA_HOME": str(other)}, which={"java": str(java)}))))
    assert inside.choices() == (str(other),)


def test_the_python_running_the_setup_keeps_its_own_spelling(tmp_path, monkeypatch):
    """With the interpreter's directory on PATH, as in an activated virtual
    environment, PATH can name the same interpreter spelled otherwise
    (python.EXE through PATHEXT on Windows). It is offered once, as the
    calling process spells it."""
    monkeypatch.setattr(os.path, "normcase", lambda path: path.lower())
    spelled = sys.executable[:-3] + sys.executable[-3:].upper()
    # Both spellings answer, as one interpreter does, so only the spelling
    # rule keeps the second out.
    machine = _Machine(versions={sys.executable: "3.12.4", spelled: "3.12.4"},
                       which={"python": spelled})
    _root, interpreter = PythonAdapter.setup_questions(tmp_path, machine)
    assert interpreter.choices() == (sys.executable,)


def test_java_setup_reads_the_build_files_as_the_runs_do(tmp_path):
    """A settings.gradle alone makes a Gradle project, and a project with
    both build systems' files gets the runs' refusal, not a question."""
    machine = _Machine(which={"gradle": "/usr/bin/gradle"})
    (tmp_path / "settings.gradle").write_text("", encoding="utf-8")
    _home, build = JavaAdapter.setup_questions(tmp_path, machine)
    assert (build.setting, build.default) == ("gradle", "/usr/bin/gradle")
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    _home, note = JavaAdapter.setup_questions(tmp_path, machine)
    assert note.lines == (f"  A Java run refuses the project: {BOTH_BUILDS}.",)
    assert note.missing == (BOTH_BUILDS_ACTION,)


def test_java_proposes_and_accepts_the_oldest_jdk_the_support_declaration_runs_on(tmp_path):
    """The JDK question's floor is the support declaration's: a JDK 11 on
    PATH is proposed and accepted, a JDK 10 refused."""
    def jdk(name, version):
        home = tmp_path / name
        (home / "bin").mkdir(parents=True)
        (home / "bin" / "java").write_text("", encoding="utf-8")
        (home / "release").write_text(f'JAVA_VERSION="{version}"\n', encoding="utf-8")
        return home.resolve()

    eleven, ten = jdk("jdk11", "11.0.22"), jdk("jdk10", "10.0.2")
    machine = _Machine(which={"java": str(eleven / "bin" / "java")})
    home = next(iter(JavaAdapter.setup_questions(tmp_path, machine)))
    assert (home.default, home.reason) == (str(eleven), "the java on PATH, JDK 11")
    assert home.check(str(eleven)) is None
    assert home.check(str(ten)) == "a JDK 11 or later is needed"


@pytest.mark.skipif(os.name != "nt", reason="Windows process creation adds .exe")
def test_an_absolute_runtime_fact_without_exe_is_found_as_its_start_finds_it(tmp_path):
    """The default runtime discovery finds an absolute runtime fact the way
    its start does, on Windows also as name.exe, and keeps the fact as given.
    A fact that names no file either way finds no runtime."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "mytool.exe").write_text("", encoding="ascii")

    class Found(lb.LangAdapter):
        language = "found"
        runtime = str(tmp_path / "bin" / "mytool")

    class Absent(lb.LangAdapter):
        language = "absent"
        runtime = str(tmp_path / "bin" / "absent")

    assert Found.discover_runtime(str(tmp_path), {}).interpreter.path == Found.runtime
    assert Absent.discover_runtime(str(tmp_path), {}).interpreter is None
