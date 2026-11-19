"""The tutorial's example language: each step's adapter, checked against
the example project, adds its hook, and the Toy's runs, coverage, mutants
and load repair behave as the steps say."""
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter import check, consumers
from langadapter.conformance import AdapterExample
from langadapter.mutation import MutationRequest
from langadapter.runs import PerTestEvidenceRequest, SuiteRunRequest
from langadapter.suites import KillItem, LineItem

EXAMPLE = Path(lb.__file__).parent / "tutorial" / "example"
STEPS = sorted((EXAMPLE / "steps").glob("0*.py"))
PROFILE = {"schema": 1, "consumer": "tool", "title": "Tool", "levels": [
    {"level": 1, "title": "Run, coverage, mutation", "families": [
        {"family": "run tests", "tier": "required"},
        {"family": "coverage", "tier": "required"},
        {"family": "mutation", "tier": "required"}]},
    {"level": 2, "title": "Load repair", "families": [
        {"family": "load repair", "tier": "optional",
         "improves": "keeps test files that fail to load",
         "fallback": "a test file that does not load is discarded"}]}]}


def _step(path):
    spec = importlib.util.spec_from_file_location(f"toy_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ToyAdapter


@pytest.fixture
def project(tmp_path):
    project_dir = tmp_path / "project"
    shutil.copytree(EXAMPLE / "project", project_dir)
    return project_dir


def _example(project_dir):
    context = lb.ProjectContext(str(project_dir), lb.Interpreter(path=sys.executable))
    return AdapterExample("toy", context, lb.Suite(str(project_dir), ("test_calc.toy",)),
                          coverage_target="calc.toy")


def _status(report, name):
    return next(i.status for i in report.items if i.name == name)


@pytest.mark.parametrize("index, added, reached", [
    (0, "run_suite", 0), (1, "test_units", 0), (2, "coverage", 0),
    (3, "open_mutation_session", 1), (4, "prune_collection_breakers", 2), (5, "support", 2),
])
def test_each_step_adds_its_hook(project, tmp_path, index, added, reached):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(PROFILE), encoding="utf-8")
    adapter = _step(STEPS[index])
    result = check.report(adapter, _example(project), profiles=[profile],
                          environment=consumers.Installed([tmp_path / "none"]))
    assert _status(result, added) == check.PRESENT, [i.message for i in result.items]
    if index + 1 < len(STEPS):
        later = {0: "test_units", 1: "coverage", 2: "open_mutation_session", 3: "prune_collection_breakers",
                 4: "support"}[index]
        assert _status(result, later) != check.PRESENT
    assert result.reached == reached
    assert _status(result, "runtime") == check.PRESENT


@pytest.mark.parametrize("path", STEPS, ids=lambda path: path.stem)
def test_each_step_shows_a_model_the_toy_test_form_with_one_example(project, path):
    """The one-file rule gives the Toy's test form and one example test,
    which the Toy's own loader reads as a test, and the import hint says that
    a Toy test needs no import line, which the loader refuses."""
    from langadapter import vocabulary
    from langadapter.tutorial.example import tools

    adapter_class = _step(path)
    adapter = adapter_class(lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable)))
    words = adapter.test_vocabulary()
    assert vocabulary.problems(words) == ()
    rule = words["single_file_rule"]
    assert "``test <name>: <left> == <right>``" in rule and "import" not in rule
    hint = adapter_class.import_statement(str(project / "calc.toy"), str(project))
    assert hint.startswith("none; ") and "without an import line" in hint
    example = re.search(r"such as ``([^`]+)``", rule).group(1)
    assert [test.name for test in tools.load_tests(example + "\n")] == ["one"]
    with pytest.raises(tools.LoadError):
        tools.load_tests("import calc\n" + example + "\n")


@pytest.mark.parametrize("path", STEPS, ids=lambda path: path.stem)
def test_each_step_says_that_the_toy_keeps_no_timeout_journal(project, path):
    """The Toy refuses the journal and declares so, so that a consumer does
    not ask for it."""
    adapter = _step(path)(lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable)))
    assert adapter.provides_timeout_journal is False
    run = adapter.tests.run_suite(SuiteRunRequest(
        lb.Suite(str(project), ("test_calc.toy",)), timeout_s=60, timeout_journal=True))
    assert run.outcome is lb.Outcome.UNSUPPORTED


def test_a_zero_operand_is_arithmetic_and_only_a_division_by_zero_fails():
    """Each operation is computed alone: a sum, difference or product with a
    right operand of 0 has its value, and only a division by 0 is an
    error."""
    from langadapter.tutorial.example import tools

    functions = tools.load_source("add(a, b) = a + b\nsub(a, b) = a - b\n"
                                  "mul(a, b) = a * b\ndiv(a, b) = a // b\n")
    tests = tools.load_tests("test add: add(3, 0) == 3\ntest sub: sub(3, 0) == 3\n"
                             "test mul: mul(3, 0) == 0\ntest div: div(6, 3) == 2\n"
                             "test zero: div(1, 0) == 0\n")
    results, _ = tools.run(tests, functions)
    assert [(test.name, outcome) for test, outcome, _ in results] == [
        ("add", "passed"), ("sub", "passed"), ("mul", "passed"), ("div", "passed"),
        ("zero", "error")]


def test_the_toy_runs_and_measures(project):
    adapter = _step(STEPS[-1])(lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(project), ("test_calc.toy",))
    run = adapter.run_suite(SuiteRunRequest(suite=suite, timeout_s=60, coverage_target="calc.toy"))
    assert run.outcome is lb.Outcome.COMPLETED and run.counts.passed == 4
    assert run.coverage.statements == (2, 3, 4) and run.coverage.covered == (2, 3, 4)
    assert run.coverage.branch_percent == 100.0
    (project / "test_calc.toy").write_text("test adds: add(2, 3) == 5\nnot a test\n",
                                           encoding="utf-8")
    broken = adapter.run_suite(SuiteRunRequest(suite=suite, timeout_s=60))
    assert broken.outcome is lb.Outcome.COLLECTION_FAILED
    assert broken.reason == "test_calc.toy:2 does not load: not a test: not a test"


def test_the_toy_mutants_carry_ids_changes_statuses_and_killers(project):
    adapter = _step(STEPS[-1])(lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable)))
    with adapter.mutations.open_session("toy") as session:
        run = session.run(MutationRequest(target="calc.toy",
                                          suite=lb.Suite(str(project), ("test_calc.toy",))))
    found = {m.engine_id: (m.status, m.diff, tuple(t.qualname for t in m.killing_tests))
             for m in run.mutants}
    assert found == {"2.1": ("survived", "line 2: * to + in double", ()),
                     "3.1": ("killed", "line 3: + to - in add", ("adds",))}
    assert run.summary["detected"] == 1 and run.score() == 50.0


@pytest.mark.parametrize("index", range(len(STEPS)), ids=lambda index: STEPS[index].stem)
def test_from_step_3_each_test_reports_its_own_coverage(project, index):
    """Each test, run alone, covers its own function's line, and the suite
    covers them all. Before step 3 there is no per-test evidence."""
    adapter = _step(STEPS[index])(lb.ProjectContext(str(project),
                                                    lb.Interpreter(path=sys.executable)))
    request = PerTestEvidenceRequest(lb.Suite(str(project), ("test_calc.toy",)), "calc.toy",
                                     timeout_s=60)
    supported = {item.name for item in adapter.operation_capabilities() if item.supported}
    available = {item.name for item in adapter.probe_capabilities().capabilities
                 if item.available}
    if index < 2:
        assert "tests.per_test_evidence" not in supported
        assert "per_test_evidence" not in available
        return
    assert "tests.per_test_evidence" in supported and "per_test_evidence" in available
    evidence = adapter.tests.per_test_evidence(request)
    assert evidence.outcome is lb.Outcome.COMPLETED and evidence.kills == "not_requested"
    lines = {e.test.qualname: {i.line for i in e.items if isinstance(i, LineItem)}
             for e in evidence.tests}
    assert lines == {"doubles": {2}, "adds": {3}, "positive": {4}, "negative": {4}}
    assert all(e.time_ms is not None for e in evidence.tests)
    assert evidence.coverage.covered == (2, 3, 4)


FAILING_TOY = ("test adds: add(2, 3) == 5\n"
               "test wrong: double(2) == 5\n"
               "test worse: add(1, 1) == 3\n")


@pytest.mark.parametrize("index", range(2, len(STEPS)), ids=lambda index: STEPS[index].stem)
def test_from_step_3_the_evidence_names_each_test_that_does_not_pass_alone(project, index):
    """Per-test evidence of a suite with failing tests runs every test and
    names each that does not pass, as the suite run's test ids, with the
    first failure's outcome and reason."""
    (project / "test_calc.toy").write_text(FAILING_TOY, encoding="utf-8")
    adapter = _step(STEPS[index])(lb.ProjectContext(str(project),
                                                    lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(project), ("test_calc.toy",))
    run = adapter.run_suite(SuiteRunRequest(suite=suite, timeout_s=60))
    evidence = adapter.tests.per_test_evidence(PerTestEvidenceRequest(suite, "calc.toy",
                                                                      timeout_s=60))
    assert evidence.outcome is lb.Outcome.TEST_FAILURES and evidence.tests == ()
    assert evidence.reason.startswith("wrong did not pass")
    assert [t.qualname for t in evidence.stopping_tests] == ["wrong", "worse"]
    assert evidence.stopping_tests == run.failing_tests()


@pytest.mark.parametrize("index", range(3, len(STEPS)), ids=lambda index: STEPS[index].stem)
def test_from_step_4_a_suite_that_fails_without_a_mutant_gets_no_mutants(project, index):
    """The Toy's check of the unchanged suite: a suite with failing tests
    measures no mutant and names those tests, as the suite run's ids."""
    from langadapter.mutation import FAILED_INITIALIZATION

    (project / "test_calc.toy").write_text(FAILING_TOY, encoding="utf-8")
    adapter = _step(STEPS[index])(lb.ProjectContext(str(project),
                                                    lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(project), ("test_calc.toy",))
    run = adapter.run_suite(SuiteRunRequest(suite=suite, timeout_s=60))
    with adapter.mutations.open_session("toy") as session:
        mutation = session.run(MutationRequest(target="calc.toy", suite=suite))
    assert mutation.outcome is lb.Outcome.TEST_FAILURES
    assert mutation.failure == FAILED_INITIALIZATION
    assert mutation.mutants == () and mutation.score() is None
    assert mutation.stopping_tests == run.failing_tests()
    assert mutation.reason == "2 tests do not pass without a mutant"
    (project / "test_calc.toy").write_text("test adds: add(2, 3) == 5\n"
                                           "test worse: add(1, 1) == 3\n", encoding="utf-8")
    with adapter.mutations.open_session("toy") as session:
        single = session.run(MutationRequest(target="calc.toy", suite=suite))
    assert single.reason == "1 test does not pass without a mutant"


def test_with_a_mutation_run_each_test_carries_the_mutants_it_kills(project):
    adapter = _step(STEPS[-1])(lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable)))
    suite = lb.Suite(str(project), ("test_calc.toy",))
    with adapter.mutations.open_session("toy") as session:
        run = session.run(MutationRequest(target="calc.toy", suite=suite))
    evidence = adapter.tests.per_test_evidence(PerTestEvidenceRequest(
        suite, "calc.toy", timeout_s=60, mutation_run=run))
    assert evidence.kills == "usable"
    kills = {e.test.qualname: {i.mutant for i in e.items if isinstance(i, KillItem)}
             for e in evidence.tests}
    assert kills == {"doubles": set(), "adds": {"3.1"}, "positive": set(), "negative": set()}


def test_a_suite_that_is_not_green_carries_no_evidence(project):
    (project / "test_calc.toy").write_text("test adds: add(2, 3) == 6\n", encoding="utf-8")
    adapter = _step(STEPS[-1])(lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable)))
    evidence = adapter.tests.per_test_evidence(PerTestEvidenceRequest(
        lb.Suite(str(project), ("test_calc.toy",)), "calc.toy", timeout_s=60))
    assert evidence.outcome is lb.Outcome.TEST_FAILURES and evidence.tests == ()


def test_the_toy_removes_a_test_and_repairs_loading(project):
    adapter = _step(STEPS[-1])(lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable)))
    source = (project / "test_calc.toy").read_text(encoding="utf-8")
    pruned = adapter.sources.prune_tests(source, ["adds"])
    assert pruned.status == "applied" and "test adds" not in pruned.source
    assert pruned.removed == ("adds",)
    broken = source + "this is not a test\n"
    repaired = adapter.sources.prune_collection_breakers(broken, timeout_s=60)
    assert (repaired.status, repaired.source, repaired.removed) == ("applied", source, ("line 6",))
    assert adapter.sources.prune_collection_breakers(source, timeout_s=60).status == "unchanged"
