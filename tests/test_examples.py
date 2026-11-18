"""The examples: an adapter for a made-up language registered from outside
the package, and a short use of the Python adapter, each run as a user runs
it."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.runs import SuiteRunRequest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
#: The examples run as scripts against the tree under test, not an
#: installed copy.
TREE = {**os.environ, "PYTHONPATH": str(Path(lb.__file__).resolve().parents[1])}


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"example_{name}", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def toy():
    module = _load("toy_adapter")
    yield module
    if "toy" in lb.names():
        lb.unregister("toy")


def _write(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text, encoding="utf-8")


def test_the_toy_adapter_runs_its_tests_through_the_public_types(toy, tmp_path):
    _write(tmp_path, "test_math.toy",
           "# arithmetic\n"
           "test adds: 1 + 1 == 2\n"
           "\n"
           "test multiplies: 6 * 7 == 41\n"
           "test divides: 1 // 0 == 0\n")
    run = toy.run(str(tmp_path))
    assert run.outcome is lb.Outcome.TEST_FAILURES
    assert (run.counts.collected, run.counts.passed, run.counts.failed,
            run.counts.errors) == (3, 1, 1, 1)
    assert [str(t) for t in run.failing_tests()] == [
        "test_math.toy::multiplies", "test_math.toy::divides"]
    assert run.outcome_of(lb.TestId("test_math.toy", "adds")) == "passed"
    assert run.identity.suite[0].path == "test_math.toy"
    assert run.coverage is None


def test_the_toy_adapter_reports_collection_and_empty_suites(toy, tmp_path):
    _write(tmp_path, "test_bad.toy", "test ok: 1 == 1\nprint 3\n")
    run = toy.run(str(tmp_path))
    assert run.outcome is lb.Outcome.COLLECTION_FAILED
    assert "test_bad.toy:2" in run.reason
    (tmp_path / "test_bad.toy").write_text("# nothing\n", encoding="utf-8")
    assert toy.run(str(tmp_path)).outcome is lb.Outcome.NO_TESTS


def test_the_toy_adapter_refuses_what_it_does_not_provide(toy, tmp_path):
    """A request for coverage is answered with the unsupported outcome, as
    the capability report says, not run without it."""
    _write(tmp_path, "test_one.toy", "test one: 1 == 1\n")
    toy.register()
    adapter = lb.create("toy", lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)))
    report = adapter.capabilities()
    assert report.capability("suite_runs").available
    assert not report.capability("mutation").available
    assert not report.capability("coverage").available
    run = adapter.run_suite(SuiteRunRequest(
        suite=lb.Suite(root=str(tmp_path), test_files=("test_one.toy",)),
        timeout_s=10, coverage_target="test_one.toy"))
    assert run.outcome is lb.Outcome.UNSUPPORTED
    assert "coverage" in run.reason
    assert lb.language_for("game/test_one.toy") == "toy"


def test_the_python_example_runs_a_suite_with_coverage(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def sign(x):\n    if x > 0:\n        return 1\n    return -1\n",
        encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import sign\n\n\ndef test_positive():\n"
        "    assert sign(2) == 1\n", encoding="utf-8")
    run = _load("python_suite").run(str(tmp_path), "test_calc.py", "calc.py")
    assert run.outcome is lb.Outcome.COMPLETED
    assert (run.counts.collected, run.counts.passed) == (1, 1)
    assert run.coverage is not None
    assert run.coverage.missing() == (4,)


def _cosmic_ray_runs() -> bool:
    from langadapter.python._cosmic_ray import _project_bin
    try:
        return subprocess.run([_project_bin("cosmic-ray", sys.executable),
                               "--help"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.mutation_tool
@pytest.mark.skipif(not _cosmic_ray_runs(),
                    reason="needs cosmic-ray beside the interpreter")
def test_the_python_example_measures_mutants(tmp_path):
    """A suite that checks the sign of 2 and -2 kills the mutants of the
    returned values and misses the one that moves the boundary to 0."""
    source = "def sign(x):\n    if x > 0:\n        return 1\n    return -1\n"
    (tmp_path / "calc.py").write_text(source, encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import sign\n\n\ndef test_signs():\n"
        "    assert sign(2) == 1\n    assert sign(-2) == -1\n",
        encoding="utf-8")
    run = _load("python_mutation").run(
        str(tmp_path), "test_calc.py", "calc.py",
        ["core/ReplaceComparisonOperator_Gt_GtE", "core/NumberReplacer"])
    assert run.outcome is lb.Outcome.COMPLETED
    killed = [m for m in run.mutants if m.killing_tests]
    survived = [m for m in run.mutants
                if m.classification.subcategory == "survived"]
    assert killed and survived
    assert all("test_signs" in str(test)
               for m in killed for test in m.killing_tests)
    assert 0 < run.score() < 100
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == source


def test_the_examples_run_as_scripts(tmp_path):
    _write(tmp_path, "test_one.toy", "test one: 2 + 2 == 4\n")
    done = subprocess.run(
        [sys.executable, str(EXAMPLES / "toy_adapter.py"), str(tmp_path)],
        capture_output=True, text=True, timeout=120, env=TREE)
    assert done.returncode == 0, done.stderr
    assert "completed" in done.stdout


@pytest.mark.parametrize("example,arguments", [
    ("python_suite.py", []), ("python_mutation.py", ["project"]),
    ("metric_provider.py", []), ("toy_adapter.py", []),
    ("toy_adapter.py", ["--check"]),
])
def test_an_example_without_its_arguments_shows_its_usage(example, arguments):
    done = subprocess.run(
        [sys.executable, str(EXAMPLES / example), *arguments],
        capture_output=True, text=True, timeout=120, env=TREE)
    assert done.returncode == 1
    assert "Traceback" not in done.stderr and "examples/" in done.stderr


def test_the_readme_example_runs(tmp_path, capsys):
    """The README's one example, run on a small project of passing tests."""
    import re

    readme = (EXAMPLES.parent / "README.md").read_text(encoding="utf-8")
    (code,) = re.findall(r"```python\n(.*?)```", readme, flags=re.DOTALL)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_example.py").write_text(
        "def test_one():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    # A project with pytest: here the test environment's interpreter stands in.
    code = code.replace('"path/to/project/.venv/bin/python"', repr(sys.executable))
    exec(code.replace('"path/to/project"', repr(str(tmp_path))), {})
    assert capsys.readouterr().out.split() == ["completed", "1", "passed"]


def test_the_metric_provider_example_loads_as_an_extension(tmp_path, registry_kept):
    """The example names its provider as langadapter_metric_provider, so an
    adapter created after the load measures long_lines."""
    loaded = lb.load_extension(str(EXAMPLES / "metric_provider.py"))
    assert loaded.metric_providers == ("example.long_lines",)
    source = tmp_path / "wide.py"
    source.write_text("x = 1\n" + "y = '" + "a" * 90 + "'\n", encoding="utf-8")
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path)))
    observation = adapter.sources.metrics(source.read_text(encoding="utf-8"), names=("long_lines",))
    assert [value.value for value in observation.value.file.metrics] == [1]
