"""Per-test evidence of the Python adapter with real pytest and coverage.py:
the recorded measurement's tokens, parametrized cases as one test,
import-time code excluded, absent times listed, kill items only when every
killer maps to a test, and nothing written."""
import dataclasses
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.outcomes import Outcome
from langadapter.python import _cosmic_ray as engine_module
from langadapter.python import _runs
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import (
    KILLS_NOT_REQUESTED, KILLS_UNUSABLE, KILLS_USABLE, PerTestEvidenceRequest,
    SuiteRunRequest,
)
from langadapter.suites import CrashItem, KillItem, LineItem, TestId

TARGET = ("def sign(x):\n"
          "    if x > 0:\n"
          "        return 1\n"
          "    return -1\n")
TESTS = ("import pytest\n\nfrom sign import sign\n\n\n"
         "def test_positive():\n    assert sign(2) == 1\n\n\n"
         "@pytest.mark.parametrize('x', [-1, -5])\n"
         "def test_negative(x):\n    assert sign(x) == -1\n\n\n"
         "class TestZero:\n"
         "    def test_zero(self):\n        assert sign(0) == -1\n")
POSITIVE = TestId("test_sign.py", "test_positive")
NEGATIVE = TestId("test_sign.py", "test_negative")
ZERO = TestId("test_sign.py", "TestZero.test_zero")

#: A recorded measurement of this suite, under its own test names: each
#: test's tokens and the suite's covered lines and taken branches.
RECORDED_TOKENS = {
    "test_sign.test_positive": frozenset({
        "B:sign.py:-1->2", "B:sign.py:2->3", "B:sign.py:3->-1",
        "L:sign.py:2", "L:sign.py:3"}),
    "test_sign.test_negative": frozenset({
        "B:sign.py:-1->2", "B:sign.py:2->4", "B:sign.py:4->-1",
        "L:sign.py:2", "L:sign.py:4"}),
    "test_sign.TestZero.test_zero": frozenset({
        "B:sign.py:-1->2", "B:sign.py:2->4", "B:sign.py:4->-1",
        "L:sign.py:2", "L:sign.py:4"}),
}
RECORDED_COVERED_LINES = (1, 2, 3, 4)
RECORDED_TAKEN_BRANCHES = ("2->3", "2->4")


def _project(tmp_path, name="proj", tests=TESTS):
    root = tmp_path / name
    (root / "tests").mkdir(parents=True)
    (root / "sign.py").write_text(TARGET, encoding="utf-8")
    (root / "tests" / "test_sign.py").write_text(tests, encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    return adapter, root, lb.Suite(str(root / "tests"), ("test_sign.py",))


def _evidence(adapter, suite, **fields):
    fields.setdefault("timeout_s", 120)
    fields.setdefault("target", "sign.py")
    return adapter.per_test_evidence(PerTestEvidenceRequest(
        suite=suite, **fields))


def _tree(root):
    """Every file with its modification time, and every directory, without
    bytecode caches."""
    return {p.relative_to(root).as_posix(): (p.stat().st_mtime_ns if p.is_file() else None)
            for p in root.rglob("*") if "__pycache__" not in p.relative_to(root).parts}


def test_the_tokens_equal_the_recorded_measurement_of_the_same_suite(
        tmp_path):
    adapter, _root, suite = _project(tmp_path)
    evidence = _evidence(adapter, suite)
    assert evidence.outcome is Outcome.COMPLETED
    by_recorded_name = {"test_sign.test_positive": POSITIVE,
                         "test_sign.test_negative": NEGATIVE,
                         "test_sign.TestZero.test_zero": ZERO}
    assert set(RECORDED_TOKENS) == set(by_recorded_name)
    tokens = evidence.tokens()
    for name, test in by_recorded_name.items():
        assert tokens[test] == RECORDED_TOKENS[name], name
    assert {e.test for e in evidence.tests} == set(by_recorded_name.values())
    assert evidence.coverage.covered == tuple(sorted(RECORDED_COVERED_LINES))
    assert {b.token()[2:].split(":", 1)[1] for b in evidence.coverage.taken} \
        == set(RECORDED_TAKEN_BRANCHES)


def test_the_calling_process_needs_no_coverage(tmp_path, monkeypatch):
    """The project interpreter's coverage writes and reads the data, so a
    caller without coverage gets the same evidence."""
    adapter, root, suite = _project(tmp_path)
    expected = _evidence(adapter, suite).tokens()
    monkeypatch.setitem(sys.modules, "coverage", None)
    evidence = _evidence(adapter, suite)
    assert evidence.outcome is Outcome.COMPLETED, evidence.reason
    assert evidence.tokens() == expected


def test_parametrized_cases_are_one_test_and_import_time_is_excluded(
        tmp_path):
    adapter, root, suite = _project(tmp_path)
    before = _tree(root)
    evidence = _evidence(adapter, suite)
    assert _tree(root) == before
    by_test = {e.test: e for e in evidence.tests}
    assert set(by_test) == {POSITIVE, NEGATIVE, ZERO}
    negative = by_test[NEGATIVE]
    assert negative.test.params is None and negative.time_ms is not None
    assert LineItem("sign.py", 4) in negative.items
    assert LineItem("sign.py", 3) not in negative.items
    # The def line runs at import: in the suite's coverage, in no test's.
    assert all(LineItem("sign.py", 1) not in e.items for e in evidence.tests)
    assert 1 in evidence.coverage.covered
    assert evidence.untimed == () and evidence.unmatched_contexts == ()
    assert evidence.kills == KILLS_NOT_REQUESTED
    assert evidence.suite_time_ms == pytest.approx(
        sum(e.time_ms for e in evidence.tests))
    assert evidence.identity.target.path == "sign.py"
    assert evidence.stopping_tests == ()


def test_a_failing_case_is_named_as_the_suite_run_names_it(tmp_path):
    """Evidence that does not complete names the failing parametrized case by
    the test id the suite run gives it, parameters included."""
    adapter, root, suite = _project(tmp_path, tests=TESTS.replace("[-1, -5]", "[-1, 5]"))
    evidence = _evidence(adapter, suite)
    run = adapter.run_suite(SuiteRunRequest(suite, timeout_s=120))
    assert evidence.outcome is Outcome.TEST_FAILURES, evidence.reason
    assert len(run.failing_tests()) == 1 and run.failing_tests()[0].params is not None
    assert evidence.stopping_tests == run.failing_tests()


def test_times_have_the_clocks_resolution(tmp_path):
    """A test's time is its phases' durations as pytest measures them, not
    the JUnit report's whole milliseconds."""
    adapter, _root, suite = _project(tmp_path)
    evidence = _evidence(adapter, suite)
    times = [e.time_ms for e in evidence.tests]
    assert all(t is not None and t > 0 for t in times)
    assert any(abs(t - round(t)) > 1e-6 for t in times), times


def test_a_test_without_a_matched_time_has_none_and_is_listed(
        tmp_path, monkeypatch):
    adapter, _root, suite = _project(tmp_path)
    real = _runs._eviction_times

    def without_zero(path):
        times = real(path)
        times.pop("test_sign.TestZero.test_zero")
        return times

    monkeypatch.setattr(_runs, "_eviction_times",
                        staticmethod(without_zero))
    evidence = _evidence(adapter, suite)
    zero = next(e for e in evidence.tests if e.test == ZERO)
    assert zero.time_ms is None and zero.items
    assert evidence.untimed == (ZERO,)
    assert all(e.time_ms is not None for e in evidence.tests if e.test != ZERO)
    # Without any time the suite's time is absent, not zero.
    monkeypatch.setattr(_runs, "_eviction_times",
                        staticmethod(lambda path: {}))
    untimed = _evidence(adapter, suite)
    assert untimed.suite_time_ms is None
    assert set(untimed.untimed) == {POSITIVE, NEGATIVE, ZERO}


#: A target whose functions the tests reach through fixtures and helpers.
HELPED = ("def sign(x):\n"
          "    return 1 if x > 0 else -1\n\n\n"
          "def prepare():\n"
          "    return 7\n\n\n"
          "def release(value):\n"
          "    return value - 7\n\n\n"
          "def check(value):\n"
          "    return value > 0\n")


def _helped(tmp_path, tests, helpers=None):
    adapter, root, suite = _project(tmp_path, tests=tests)
    (root / "sign.py").write_text(HELPED, encoding="utf-8")
    if helpers is not None:
        (root / "tests" / "helpers.py").write_text(helpers, encoding="utf-8")
    evidence = _evidence(adapter, suite)
    assert evidence.outcome is Outcome.COMPLETED, evidence.reason
    return evidence, {e.test.qualname: e.items for e in evidence.tests}


def test_a_fixture_s_setup_and_teardown_belong_to_its_test(tmp_path):
    """Each test's context spans its setup and teardown, so its fixture's
    code is its own and only import-time code stays outside every test."""
    tests = ("import pytest\n\nfrom sign import prepare, release, sign\n\n\n"
             "def test_first():\n    assert sign(1) == 1\n\n\n"
             "@pytest.fixture\ndef seven():\n    value = prepare()\n    yield value\n"
             "    release(value)\n\n\n"
             "def test_second(seven):\n    assert sign(seven) == 1\n")
    evidence, items = _helped(tmp_path, tests)
    for line in (6, 10):  # prepare() in the setup, release() in the teardown
        assert LineItem("sign.py", line) in items["test_second"], line
        assert LineItem("sign.py", line) not in items["test_first"], line
        assert LineItem("sign.py", line) not in evidence.outside_items, line
    assert LineItem("sign.py", 2) in items["test_first"]
    assert {LineItem("sign.py", 1), LineItem("sign.py", 5)} <= evidence.outside_items
    assert evidence.unmatched_contexts == ()


def test_a_helper_named_like_a_test_belongs_to_the_calling_test(tmp_path):
    """A helper module's function named test_* is no test: what it runs,
    from a fixture or from the test, belongs to the test it serves, and no
    context is left unmatched."""
    helpers = "from sign import check\n\n\ndef test_like(value):\n    return check(value)\n"
    tests = ("import pytest\n\nimport helpers\nfrom sign import sign\n\n\n"
             "@pytest.fixture\ndef checked():\n    return helpers.test_like(3)\n\n\n"
             "def test_uses_the_fixture(checked):\n    assert checked\n\n\n"
             "def test_calls_the_helper():\n    assert helpers.test_like(sign(3))\n")
    evidence, items = _helped(tmp_path, tests, helpers=helpers)
    assert LineItem("sign.py", 14) in items["test_uses_the_fixture"]
    assert LineItem("sign.py", 14) in items["test_calls_the_helper"]
    assert evidence.unmatched_contexts == () and evidence.unmatched_items == ()


def test_a_module_fixture_belongs_to_the_test_that_sets_it_up(tmp_path):
    """A module-scoped fixture's lines belong to the first test that uses
    it, which sets it up."""
    tests = ("import pytest\n\nfrom sign import prepare, sign\n\n\n"
             "@pytest.fixture(scope='module')\ndef seven():\n    return prepare()\n\n\n"
             "def test_one(seven):\n    assert sign(seven) == 1\n\n\n"
             "def test_two(seven):\n    assert sign(-seven) == -1\n")
    _evidence, items = _helped(tmp_path, tests)
    assert LineItem("sign.py", 6) in items["test_one"]
    assert LineItem("sign.py", 6) not in items["test_two"]


def test_a_run_without_coverage_measuring_is_no_evidence(tmp_path, monkeypatch):
    """Without a coverage measurement the evidence fails with the reason,
    never reads empty."""
    adapter, _root, suite = _project(tmp_path)
    from langadapter.python import _plugins
    monkeypatch.setattr(_plugins, "_EVIDENCE_TIMES_PLUGIN",
                        _plugins._EVIDENCE_TIMES_PLUGIN.replace(
                            "return Coverage.current()", "return None"))
    evidence = _evidence(adapter, suite, warmup=False)
    assert evidence.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "found no coverage measurement" in evidence.reason


def test_a_red_suite_has_no_evidence(tmp_path):
    red = TESTS.replace("assert sign(2) == 1", "assert sign(2) == 2")
    adapter, _root, suite = _project(tmp_path, tests=red)
    evidence = _evidence(adapter, suite)
    assert evidence.outcome is Outcome.TEST_FAILURES
    assert evidence.tests == () and evidence.coverage is None
    assert evidence.stopping_tests == (POSITIVE,)


def test_a_timeout_in_the_warmup_run(tmp_path):
    slow = ("import time\n\nfrom sign import sign\n\n\n"
            "def test_slow():\n    time.sleep(30)\n    assert sign(1) == 1\n")
    adapter, _root, suite = _project(tmp_path, tests=slow)
    evidence = _evidence(adapter, suite, timeout_s=3)
    assert evidence.outcome is Outcome.TIMED_OUT
    assert evidence.reason.startswith("the warm-up run")
    assert evidence.stopping_tests == ()


def test_an_invalid_target_names_its_field(tmp_path):
    adapter, _root, suite = _project(tmp_path)
    with pytest.raises(InvalidRequest) as refused:
        _evidence(adapter, suite, target="missing.py")
    assert refused.value.field == "target"


def test_unmatched_context_items_are_preserved_without_a_test(tmp_path, monkeypatch):
    adapter, _root, suite = _project(tmp_path)
    native = {"unreported.test_helper": frozenset({"L:sign.py:3"})}
    monkeypatch.setattr(_runs, "_eviction_tokens_from_coverage",
                        lambda self, data, target: native)
    evidence = _evidence(adapter, suite, warmup=False)
    assert evidence.outcome == Outcome.COMPLETED
    assert evidence.unmatched_contexts == ("unreported.test_helper",)
    context, = evidence.unmatched_items
    assert context.context == "unreported.test_helper"
    assert context.items == frozenset({LineItem("sign.py", 3)})
    assert {item.test for item in evidence.tests} == {POSITIVE, NEGATIVE, ZERO}
    assert all(not item.items for item in evidence.tests)


def test_unread_per_test_coverage_is_no_evidence(tmp_path, monkeypatch):
    """Coverage data that cannot be read is an infrastructure failure with
    its cause, not empty tokens that read as tests covering nothing."""
    adapter, _root, suite = _project(tmp_path)
    monkeypatch.setattr(_runs, "_EVICTION_TOKENS_READER",
                        "import sys\nsys.exit('the data is damaged')\n")
    evidence = _evidence(adapter, suite, warmup=False)
    assert evidence.outcome == Outcome.INFRASTRUCTURE_FAILURE
    assert evidence.reason == ("the per-test coverage could not be read (exit 1): "
                               "the data is damaged")
    assert evidence.tests == () and evidence.coverage is None
    assert evidence.stopping_tests == ()


def test_runner_startup_failure_is_not_test_evidence(tmp_path, monkeypatch):
    adapter, _root, suite = _project(tmp_path)

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing project interpreter")

    monkeypatch.setattr(adapter.executor, "run", missing)
    evidence = _evidence(adapter, suite, warmup=False)
    assert evidence.outcome == Outcome.INFRASTRUCTURE_FAILURE
    assert "could not execute" in evidence.reason
    assert evidence.tests == () and evidence.coverage is None
    assert evidence.suite_time_ms is None


def _cosmic_ray_runs() -> bool:
    executable = engine_module._project_bin("cosmic-ray", sys.executable)
    try:
        return subprocess.run([executable, "--help"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(),
                    reason="needs cosmic-ray beside the interpreter")
def test_kill_items_come_only_from_killers_that_map_to_tests(tmp_path):
    adapter, _root, suite = _project(tmp_path)
    with adapter.open_mutation_session() as session:
        run = session.run(MutationRequest(
            target="sign.py", suite=suite, attribution="all",
            catalogue=Catalogue(operators=(
                "core/ReplaceComparisonOperator_Gt_Lt",))))
    # x < 0 fails sign(2) and both negative cases; sign(0) is still -1.
    (mutant,) = run.mutants
    assert set(mutant.killing_tests) == {
        TestId("test_sign.py", "test_positive"),
        TestId("test_sign.py", "test_negative", "-1"),
        TestId("test_sign.py", "test_negative", "-5")}
    evidence = _evidence(adapter, suite, mutation_run=run)
    assert evidence.kills == KILLS_USABLE
    kill = KillItem(str(mutant.engine_id))
    assert {e.test for e in evidence.tests if kill in e.items} == {
        POSITIVE, NEGATIVE}
    unplaced = dataclasses.replace(
        mutant, killers=mutant.killers + ("elsewhere.py::test_x",),
        unmatched_killers=("elsewhere.py::test_x",))
    partial = dataclasses.replace(run, mutants=(unplaced,))
    unusable = _evidence(adapter, suite, mutation_run=partial)
    assert unusable.kills == KILLS_UNUSABLE
    assert unusable.unmapped_killers == ("elsewhere.py::test_x",)
    assert all(not isinstance(i, KillItem)
               for e in unusable.tests for i in e.items)
    # A kill without a named killer whose run ended mid-test gives the test
    # that was running then its crash observation, not a kill item.
    crash = dataclasses.replace(
        mutant, killers=(), killing_tests=(), unmatched_killers=(),
        row={**mutant.row, "killer": None, "killers": [],
             "active_at_end": "../scratch/test_sign.py::TestZero::test_zero"})
    observed = _evidence(adapter, suite,
                         mutation_run=dataclasses.replace(run, mutants=(crash,)))
    assert observed.kills == KILLS_USABLE
    observation = CrashItem(kill.mutant)
    assert {e.test for e in observed.tests
            if observation in e.items} == {ZERO}
    assert not any(isinstance(i, KillItem)
                   for e in observed.tests for i in e.items)
    assert observation.token() in observed.tokens()[ZERO]


SAME_NAME_TARGET = ("def pick(x):\n"
                    "    if x > 0:\n"
                    "        return 'up'\n"
                    "    return 'down'\n")
SAME_NAME_OTHER = ("def other(x):\n"
                   "    total = 0\n"
                   "    for i in range(x):\n"
                   "        if i % 2:\n"
                   "            total += i\n"
                   "        else:\n"
                   "            total -= 1\n"
                   "    return total\n")


def test_a_file_of_the_same_name_in_another_package_is_not_the_target(tmp_path):
    """With pkg/asyncio/retry.py measured too, the coverage of the target
    pkg/retry.py is its own and equals a suite run's."""
    root = tmp_path / "proj"
    for sub_dir in ("pkg/asyncio", "tests"):
        (root / sub_dir).mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "asyncio" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "retry.py").write_text(SAME_NAME_TARGET, encoding="utf-8")
    (root / "pkg" / "asyncio" / "retry.py").write_text(SAME_NAME_OTHER, encoding="utf-8")
    (root / "tests" / "test_retry.py").write_text(
        "from pkg.retry import pick\nfrom pkg.asyncio.retry import other\n\n\n"
        "def test_up():\n    assert pick(1) == 'up'\n    assert other(3) == -1\n",
        encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(root / "tests"), ("test_retry.py",))
    evidence = _evidence(adapter, suite, target="pkg/retry.py")
    assert evidence.outcome is Outcome.COMPLETED, evidence.reason
    coverage = evidence.coverage
    assert coverage.statements == (1, 2, 3, 4) and coverage.covered == (1, 2, 3)
    assert {(b.source, b.destination) for b in coverage.branches} == {(2, 3), (2, 4)}
    assert {(b.source, b.destination) for b in coverage.taken} == {(2, 3)}
    run = adapter.tests.run_suite(SuiteRunRequest(suite, 120, coverage_target="pkg/retry.py"))
    assert run.coverage.statements == coverage.statements
    assert run.coverage.covered == coverage.covered
    assert ({(b.source, b.destination) for b in run.coverage.branches}
            == {(b.source, b.destination) for b in coverage.branches})


def test_a_report_entry_is_found_by_its_path_never_by_its_name_alone(tmp_path):
    from langadapter.python._runs import _file_entry

    target = str(tmp_path / "pkg" / "retry.py")
    files = {"pkg/asyncio/retry.py": "other", "pkg/retry.py": "target"}
    assert _file_entry(files, target, str(tmp_path)) == "target"
    assert _file_entry({target: "exact"}, target, str(tmp_path)) == "exact"
    assert _file_entry({"pkg/asyncio/retry.py": "other"}, target, str(tmp_path)) is None
    twice = {"pkg/retry.py": "one", "pkg/../pkg/retry.py": "two"}
    assert _file_entry(twice, target, str(tmp_path)) is None


def test_the_evidence_run_s_suite_coverage_is_the_suite_run_s(tmp_path):
    """Both build the target's coverage alike: branch items named by its
    path from the root, with the traced lines, for a target below it."""
    from langadapter.runs import SuiteRunRequest

    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "sign.py").write_text(
        "def sign(x):\n    if x > 0:\n        return 1\n    return -1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_sign.py").write_text(
        "from pkg.sign import sign\n\n\ndef test_positive():\n    assert sign(2) == 1\n",
        encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(root / "tests"), ("test_sign.py",))
    evidence = adapter.per_test_evidence(PerTestEvidenceRequest(suite=suite, timeout_s=120,
                                                                target="pkg/sign.py"))
    run = adapter.run_suite(SuiteRunRequest(suite, 120, coverage_target="pkg/sign.py"))
    assert evidence.outcome is Outcome.COMPLETED and run.coverage is not None, (evidence.reason, run.reason)
    assert {branch.file for branch in evidence.coverage.branches} == {"pkg/sign.py"}
    assert evidence.coverage == run.coverage
