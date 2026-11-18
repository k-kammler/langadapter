"""The shared contract checks on the built-in adapters, the external
examples, and doubles that break one rule each."""
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.conformance import (
    AdapterExample, check_adapter, check_metric_provider, compare_runs,
)
from langadapter.runs import RunCounts
from langadapter.sources import SourceResult

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
#: The environment that runs the examples against the tree under test.
TREE = {**os.environ, "PYTHONPATH": str(Path(lb.__file__).resolve().parents[1])}
TOY_TESTS = "test adds: 1 + 1 == 2\ntest wrong: 2 * 2 == 5\n"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"example_{name}", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def toy():
    module = _load("toy_adapter")
    module.register()
    yield module
    for name in ("toy", "broken"):
        if name in lb.names():
            lb.unregister(name)


def _example(language, root, test_file, expected, **options):
    context = lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable))
    return AdapterExample(language, context, lb.Suite(str(root), (test_file,)),
                          expected={lb.TestId(test_file, name): outcome
                                    for name, outcome in expected.items()}, **options)


def test_the_external_toy_adapter_keeps_the_contract(toy, tmp_path):
    (tmp_path / "test_math.toy").write_text(TOY_TESTS, encoding="utf-8")
    example = _example("toy", tmp_path, "test_math.toy",
                       {"adds": "passed", "wrong": "failed"},
                       coverage_target="test_math.toy")
    assert check_adapter(example) == ()


def test_a_setting_of_an_unknown_kind_is_a_problem(toy, tmp_path, monkeypatch):
    """A project setting whose kind is none of the four is named, while the
    dataclass itself takes it."""
    from langadapter.adapter import ProjectSetting

    (tmp_path / "test_math.toy").write_text(TOY_TESTS, encoding="utf-8")
    monkeypatch.setattr(toy.ToyAdapter, "project_settings", (
        ProjectSetting("node", "The node program", "program"),
        ProjectSetting("toy_home", "The Toy's directory", "path")))
    example = _example("toy", tmp_path, "test_math.toy", {"adds": "passed"})
    assert check_adapter(example) == (
        "project_settings: the kind 'program' of node is not path, executable, choice or "
        "text",)


def test_a_suite_outside_the_project_returns_its_problems(toy, tmp_path):
    """A suite outside the project is a legitimate example: the setup
    inspection takes none of its files, and the check returns its problems
    instead of raising on a path that leaves the project."""
    project, suite = tmp_path / "project", tmp_path / "suite"
    project.mkdir()
    suite.mkdir()
    (suite / "test_math.toy").write_text(TOY_TESTS, encoding="utf-8")
    context = lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable))
    example = AdapterExample("toy", context, lb.Suite(str(suite), ("test_math.toy",)),
                             expected={lb.TestId("test_math.toy", "adds"): "passed",
                                       lb.TestId("test_math.toy", "wrong"): "failed"})
    assert check_adapter(example) == ()


def test_the_python_adapter_keeps_the_contract(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def sign(x):\n    return 1 if x > 0 else -1\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import sign\n\n\ndef test_positive():\n    assert sign(2) == 1\n\n\n"
        "def test_wrong():\n    assert sign(-1) == 1\n", encoding="utf-8")
    example = _example("python", tmp_path, "test_calc.py",
                       {"test_positive": "passed", "test_wrong": "failed"},
                       coverage_target="calc.py",
                       collection_breaker=("from calc import sign\n\nBROKEN = 1 / 0\n\n\n"
                                           "def test_positive():\n    assert sign(2) == 1\n"))
    assert check_adapter(example) == ()


@pytest.mark.parametrize("defect, problem", [
    ("counts", "must add up to collected"),
    ("outcome", "test_failures exactly when"),
    ("foreign_test", "is not in a declared test file"),
    ("coverage", "an adapter that measures coverage reports it available in capabilities()"),
    ("unclaimed", "is not claimed by broken"),
    ("nothing", "no test outcome for the declared test file test_math.brk"),
    ("support", "support must be a LanguageSupport"),
    ("journal", "journal run: the example suite did not run (unsupported"),
    ("journal_type", "provides_timeout_journal must be true or false"),
    ("setup", "setup: inspection must return a valid inspection ProjectSetupReport (report: "),
])
def test_each_broken_rule_is_named(toy, tmp_path, defect, problem):
    class Broken(toy.ToyAdapter):
        language = "broken"
        file_extensions = (".brk",)

        def run_suite(self, request):
            if defect == "coverage" and request.coverage_target is not None:
                request = replace(request, coverage_target=None)
            run = super().run_suite(request)
            if defect == "nothing" and run.tests:
                # Answers completed without tests, where no_tests is due.
                return replace(run, outcome=lb.Outcome.COMPLETED, tests=(),
                               counts=RunCounts(0, 0, 0, 0, 0))
            if not run.tests:  # a refused run has no counts or tests to break
                return run
            if defect == "counts":
                return replace(run, counts=replace(run.counts, passed=run.counts.passed + 1))
            if defect == "outcome":
                return replace(run, outcome=lb.Outcome.COMPLETED)
            if defect == "foreign_test":
                moved = replace(run.tests[0], test=lb.TestId("elsewhere.brk", "adds"))
                return replace(run, tests=(moved, *run.tests[1:]))
            return run

    if defect == "support":
        Broken.support = "Python 3.12"
    if defect == "journal":
        # Claims the journal that its run refuses.
        Broken.provides_timeout_journal = True
    if defect == "journal_type":
        Broken.provides_timeout_journal = "yes"
    if defect == "setup":
        # No report, which the setup assessment refuses.
        Broken.inspect_project = lambda self, request: None
    lb.register("broken", [".brk"], Broken)
    name = "test_math.toy" if defect == "unclaimed" else "test_math.brk"
    (tmp_path / name).write_text(TOY_TESTS, encoding="utf-8")
    problems = check_adapter(_example("broken", tmp_path, name, {"adds": "passed"},
                                      coverage_target=name))
    assert any(problem in item for item in problems), problems


BREAKER = "test adds: 1 + 1 == 2\nnot a test\n"


@pytest.mark.parametrize("unit, breaker, keeps, problem", [
    ("a line that is not a test", BREAKER, False, None),
    ("", BREAKER, False, "declares the unit it removes"),
    ("a line that is not a test", None, False, "gives one (AdapterExample.collection_breaker)"),
    ("a line that is not a test", BREAKER, True, "must be removed and named, not unchanged"),
])
def test_an_adapter_that_prunes_collection_breakers_declares_its_unit(
        toy, tmp_path, unit, breaker, keeps, problem):
    class Broken(toy.ToyAdapter):
        """A Toy double that prunes the lines its loading rejects: those
        that are not a test, blank or a comment. With ``keeps`` it removes
        nothing."""

        language = "broken"
        file_extensions = (".brk",)
        collection_unit = unit

        def prune_collection_breakers(self, source, *, timeout_s):
            lines = source.splitlines(keepends=True)
            bad = [line for line in lines
                   if line.strip() and not line.startswith(("test ", "#"))]
            if not bad or keeps:
                return SourceResult("unchanged", source, "the Toy double")
            return SourceResult("applied", "".join(x for x in lines if x not in bad),
                                   "the Toy double", removed=tuple(x.strip() for x in bad))

    lb.register("broken", [".brk"], Broken)
    (tmp_path / "test_math.brk").write_text(TOY_TESTS, encoding="utf-8")
    problems = check_adapter(_example("broken", tmp_path, "test_math.brk", {"adds": "passed"},
                                      collection_breaker=breaker))
    if problem is None:
        assert problems == ()
    else:
        assert [item for item in problems if problem in item], problems


@pytest.mark.parametrize("value, expected", [
    (1, ()),
    (None, ("metric provider toy-lines: measurement: lines is unavailable (parse_error: "
            "the double reads nothing)",
            "metric provider toy-lines: measurement: file metric lines has no value")),
])
def test_the_languages_own_metric_providers_measure_the_examples_test_file(toy, tmp_path, value,
                                                                           expected):
    definition = lb.MetricDefinition("lines", "lines", "1", ("file",))

    class Lines:
        name = "toy-lines"

        def definitions(self):
            return (definition,)

        def measure(self, source, *, language, parser):
            if value is None:
                return lb.SourceObservation("parse_error", reason="the double reads nothing")
            return lb.SourceObservation("completed", (lb.MetricScope("file", "", None, None, None, (
                lb.MetricValue(definition, self.name, None, "completed", value),)),))

    class Measured(toy.ToyAdapter):
        language = "broken"
        file_extensions = (".brk",)

        def metric_providers(self):
            return (Lines(),)

    lb.register("broken", [".brk"], Measured)
    (tmp_path / "test_math.brk").write_text(TOY_TESTS, encoding="utf-8")
    problems = check_adapter(_example("broken", tmp_path, "test_math.brk", {"adds": "passed"}))
    assert problems == expected


def test_an_unregistered_language_is_one_problem(tmp_path):
    example = _example("unregistered", tmp_path, "test_x.unknown", {})
    assert check_adapter(example) == ("registration: unregistered is not registered",)


def test_the_external_metric_provider_keeps_the_contract():
    provider = _load("metric_provider").LongLines()
    source = "x = 1\n" + "y = '" + "a" * 90 + "'\n"
    assert check_metric_provider(provider, language="python", source=source) == ()


class _Provider:
    name = "double.provider"

    def __init__(self, definitions, *, value=1, file_scope=True, completed=True):
        self.items, self.value, self.file_scope = definitions, value, file_scope
        self.completed = completed

    def definitions(self):
        return self.items

    def measure(self, source, *, language, parser):
        values = tuple(lb.MetricValue(item, self.name, None, "completed", self.value) if self.completed
                       else lb.MetricValue(item, self.name, None, "parse_error", None, "not read")
                       for item in self.items)
        scopes = (lb.MetricScope("file", "", None, None, None, values if self.file_scope else ()),)
        return lb.SourceObservation("completed", scopes)


@pytest.mark.parametrize("provider, problem", [
    (_Provider((lb.MetricDefinition("same", "count", "1", ("file",)),) * 2), "registration"),
    (_Provider((lb.MetricDefinition("absent", "count", "1", ("file",)),), file_scope=False),
     "has no value"),
    # A file value without a number is no value.
    (_Provider((lb.MetricDefinition("unread", "count", "1", ("file",)),), completed=False),
     "file metric unread has no value"),
    (_Provider((lb.MetricDefinition("guessed", "count", "1", ("file",)),)),
     "reports a number for source it cannot read"),
    # A built-in metric's name clashes on the language's real registration path.
    (_Provider((lb.MetricDefinition("nloc", "lines", "1", ("file",)),)), "registration"),
])
def test_each_broken_metric_rule_is_named(provider, problem):
    problems = check_metric_provider(provider, language="python", source="x = 1\n",
                                     unparsable="def (:\n")
    assert any(problem in item for item in problems), problems


@pytest.mark.parametrize("mode", [["--check"], []], ids=["check", "run"])
def test_the_toy_example_refuses_a_directory_without_tests_in_one_line(mode, tmp_path):
    """A missing directory and one without test_*.toy files each give one
    line that names the directory, not a traceback."""
    (tmp_path / "empty").mkdir()
    for name, expected in (("absent", "is no directory"), ("empty", "holds no test_*.toy file")):
        refused = subprocess.run(
            [sys.executable, str(EXAMPLES / "toy_adapter.py"), *mode, str(tmp_path / name)],
            capture_output=True, text=True, timeout=120, env=TREE)
        assert refused.returncode == 1, refused.stdout + refused.stderr
        assert refused.stderr == f"{tmp_path / name} {expected}\n", refused.stderr


def test_the_extension_examples_run_as_scripts(tmp_path):
    (tmp_path / "test_one.toy").write_text("test one: 2 + 2 == 4\n", encoding="utf-8")
    checked = subprocess.run(
        [sys.executable, str(EXAMPLES / "toy_adapter.py"), "--check", str(tmp_path)],
        capture_output=True, text=True, timeout=120, env=TREE)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "keeps the adapter contract" in checked.stdout
    source = tmp_path / "wide.py"
    source.write_text("x = 1\n" + "#" * 100 + "\n", encoding="utf-8")
    measured = subprocess.run(
        [sys.executable, str(EXAMPLES / "metric_provider.py"), str(source)],
        capture_output=True, text=True, timeout=120, env=TREE)
    assert measured.returncode == 0, measured.stdout + measured.stderr
    assert measured.stdout.splitlines() == ["contract: no problem", "long_lines: 1 lines"]


# compare_runs: each tool's run of the unchanged suite against the normal run.

@pytest.mark.mutation_tool
@pytest.mark.parametrize("wrong", [False, True], ids=["green", "one-failing"])
def test_the_python_adapters_tools_agree_with_its_normal_run(tmp_path, wrong):
    """The coverage run, the per-test evidence and Cosmic Ray's check before
    its first mutant give the example suite the normal run's verdicts: a
    green suite is measured, a failing one refused naming its failing test."""
    if not wrong:
        pytest.importorskip("cosmic_ray")
    (tmp_path / "calc.py").write_text(
        "def sign(x):\n    return 1 if x > 0 else -1\n", encoding="utf-8")
    tests = "from calc import sign\n\n\ndef test_positive():\n    assert sign(2) == 1\n"
    if wrong:
        tests += "\n\ndef test_wrong():\n    assert sign(-1) == 1\n"
    (tmp_path / "test_calc.py").write_text(tests, encoding="utf-8")
    example = _example("python", tmp_path, "test_calc.py", {}, coverage_target="calc.py")
    assert compare_runs(example) == ()


def test_the_mutation_run_starts_on_a_new_adapter(tmp_path):
    """compare_runs starts its mutation run on a new adapter, as a
    consumer's first session runs, so a session that works only after a
    suite run on the same adapter object is named as a difference."""
    from langadapter.mutation import MutationRun

    steps = Path(lb.__file__).parent / "tutorial" / "example" / "steps"
    spec = importlib.util.spec_from_file_location("toy_after_run", steps / "06_versions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class AfterRunSession(module.ToySession):
        def measure(self, request):
            if not self.adapter.ran:
                return MutationRun.failed(request.target, lb.Outcome.INFRASTRUCTURE_FAILURE,
                                          "this adapter object ran no suite")
            return super().measure(request)

    class AfterRun(module.ToyAdapter):
        ran = False

        def run_suite(self, request):
            self.ran = True
            return super().run_suite(request)

        def open_mutation_session(self, engine, workers=1, **options):
            return AfterRunSession(self, workers)

    for name in ("calc.toy", "test_calc.toy"):
        shutil.copy(steps.parent / "project" / name, tmp_path / name)
    lb.register("toy_after_run", [".toy"], AfterRun)
    try:
        problems = compare_runs(_example("toy_after_run", tmp_path, "test_calc.toy", {},
                                         coverage_target="calc.toy"))
    finally:
        lb.unregister("toy_after_run")
    assert len(problems) == 1 and problems[0].startswith(
        "mutation tool's run: measured nothing of a suite the normal run passed"), problems


def test_a_parametrized_test_matches_its_functions_evidence(tmp_path, monkeypatch):
    """Per-test evidence names a parametrized function once, without its
    cases' parameters, so each case of the normal run finds it."""
    from langadapter.operations import MutationOperations

    # Cosmic Ray's check runs with the mutation_tool tests above. This
    # compares the coverage run and the per-test evidence.
    monkeypatch.setattr(MutationOperations, "engines", lambda self: ())
    (tmp_path / "calc.py").write_text(
        "def sign(x):\n    return 1 if x > 0 else -1\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "import pytest\n\nfrom calc import sign\n\n\n@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_positive(x):\n    assert sign(x) == 1\n", encoding="utf-8")
    assert compare_runs(_example("python", tmp_path, "test_calc.py", {}, coverage_target="calc.py")) == ()


@pytest.mark.mutation_tool
def test_a_coverage_run_that_changes_a_verdict_is_named(tmp_path, monkeypatch):
    from langadapter.python.adapter import PythonAdapter

    run_suite = PythonAdapter.run_suite

    def flipped(self, request):
        # The coverage tool's run gives the first test another verdict.
        run = run_suite(self, request)
        if request.coverage_target is None or not run.tests:
            return run
        return replace(run, tests=(replace(run.tests[0], outcome="failed"), *run.tests[1:]))

    monkeypatch.setattr(PythonAdapter, "run_suite", flipped)
    (tmp_path / "calc.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import one\n\n\ndef test_one():\n    assert one() == 1\n", encoding="utf-8")
    problems = compare_runs(_example("python", tmp_path, "test_calc.py", {},
                                     coverage_target="calc.py"))
    assert problems[0] == "coverage run: test_calc.py::test_one was failed, in the normal run passed"


def _refused(*tests):
    from langadapter.mutation import MutationRun

    return MutationRun.failed("calc.py", lb.Outcome.INFRASTRUCTURE_FAILURE, "refused",
                              stopping_tests=tests)


@pytest.mark.parametrize("failing, run, problem", [
    ((), "refused", "measured nothing of a suite the normal run passed"),
    ((), "stopped", "stopped by test_calc.py::test_wrong, which the normal run passed"),
    (("test_wrong",), "measured", "measured a suite whose normal run failed"),
    (("test_wrong", "test_other"), "stopped",
     "stopped by test_calc.py::test_wrong, where the normal run failed"),
    (("test_wrong",), "stopped", None),
])
def test_a_mutation_tools_check_that_differs_is_named(failing, run, problem):
    from langadapter.conformance import _mutation_problems
    from langadapter.mutation import MutationRun

    wrong = lb.TestId("test_calc.py", "test_wrong", text="test_calc.py::test_wrong")
    runs = {"refused": _refused(), "stopped": _refused(wrong),
            "measured": MutationRun("calc.py", (), {}, "", ())}
    named = frozenset(lb.TestId("test_calc.py", name, text=f"test_calc.py::{name}")
                      for name in failing)
    problems = _mutation_problems(named, runs[run])
    if problem is None:
        assert problems == []
    else:
        assert len(problems) == 1 and problem in problems[0], problems
