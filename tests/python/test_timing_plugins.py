"""A run with ``PytestOptions.leave_out_timing_plugins`` leaves pytest-cov's
and pytest-benchmark's measurement and pytest-xdist's workers out where the
project's pytest loads them, and keeps the project's options for them
valid. Real pytest runs, the project's interpreter this one. Each run
without the option shows the plugin at work."""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.python import _runs, adapter as python_adapter
from langadapter.python.project import PytestOptions
from langadapter.runs import BenchmarkRequest, SuiteProfileRequest, SuiteRunRequest

# pytest-xdist gives a worker's configuration its worker input. The
# environment variable would also reach a run started inside a worker.
IN_THE_MAIN_PROCESS = ("def test_in_the_main_process(request):\n"
                       "    assert not hasattr(request.config, 'workerinput')\n")


@pytest.fixture(autouse=True)
def stand_in_pytest_configuration():
    """pytest itself answers here, in place of the session's stand-in."""


def _project(tmp_path: Path, addopts: str, test: str) -> Path:
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text(
        f"[pytest]\naddopts = {addopts}\nfilterwarnings = error\n", encoding="utf-8")
    (root / "tests" / "test_one.py").write_text(test, encoding="utf-8")
    return root


def _adapter(root: Path):
    return lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))


def _suite(root: Path) -> lb.Suite:
    return lb.Suite(str(root), ("tests/test_one.py",))


def _run(root: Path, leave_out: bool):
    return _adapter(root).tests.run_suite(SuiteRunRequest(
        _suite(root), timeout_s=120.0,
        runner_options=PytestOptions(leave_out_timing_plugins=leave_out)))


def test_a_projects_coverage_option_traces_nothing_and_warns_nothing(tmp_path):
    """--no-cov follows the project's --cov, so pytest-cov starts no tracer,
    writes no data file and raises no CovDisabledWarning, which the strict
    warning filter would turn into an error."""
    pytest.importorskip("pytest_cov")
    root = _project(tmp_path, "--cov=calc", "from calc import total\n\n\n"
                    "def test_total():\n    assert total([1, 2]) == 3\n")
    (root / "calc.py").write_text("def total(v):\n    return sum(v)\n", encoding="utf-8")

    measured = _run(root, leave_out=False)
    assert measured.outcome is lb.Outcome.COMPLETED, measured.reason
    assert (root / ".coverage").is_file()
    (root / ".coverage").unlink()

    run = _run(root, leave_out=True)
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert not (root / ".coverage").exists()
    assert "CovDisabledWarning" not in run.output


def test_a_projects_workers_are_left_out(tmp_path):
    """-n 0 follows the project's -n 2, so the tests run in the main
    process."""
    pytest.importorskip("xdist")
    root = _project(tmp_path, "-n 2 --dist loadfile", IN_THE_MAIN_PROCESS)

    assert _run(root, leave_out=False).outcome is lb.Outcome.TEST_FAILURES
    run = _run(root, leave_out=True)
    assert run.outcome is lb.Outcome.COMPLETED, run.reason


def test_a_projects_benchmarks_run_once_and_save_nothing(tmp_path):
    """--benchmark-disable keeps the benchmark test and its verdict, runs
    the function once and saves no run, without a warning the strict filter
    would turn into an error."""
    pytest.importorskip("pytest_benchmark")
    root = _project(tmp_path, "--benchmark-autosave --benchmark-json=benchmark.json",
                    "def test_sum(benchmark):\n    assert benchmark(sum, [1, 2]) == 3\n")

    measured = _run(root, leave_out=False)
    assert measured.outcome is lb.Outcome.COMPLETED, measured.reason
    assert (root / "benchmark.json").stat().st_size > 0
    assert list((root / ".benchmarks").rglob("*.json"))
    (root / "benchmark.json").unlink()
    shutil.rmtree(root / ".benchmarks")

    run = _run(root, leave_out=True)
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert run.counts.passed == 1
    # No data goes into the --benchmark-json file. pytest-benchmark 5.1
    # creates it empty when it reads its options (argparse.FileType), 5.3
    # opens it only to save, so it is absent.
    json_file = root / "benchmark.json"
    assert not json_file.exists() or json_file.read_bytes() == b""
    assert not list((root / ".benchmarks").rglob("*.json"))


def test_benchmark_only_refuses_the_option(tmp_path):
    """pytest-benchmark refuses --benchmark-disable beside a project's
    --benchmark-only, and the run says so."""
    pytest.importorskip("pytest_benchmark")
    root = _project(tmp_path, "--benchmark-only",
                    "def test_sum(benchmark):\n    assert benchmark(sum, [1]) == 1\n")

    run = _run(root, leave_out=True)
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "--benchmark-only and --benchmark-disable" in run.reason_text(None)


def test_a_profiled_run_gets_the_flags(tmp_path):
    """profile_suite passes its runner options to its run."""
    pytest.importorskip("xdist")
    root = _project(tmp_path, "-n 2", IN_THE_MAIN_PROCESS)

    def profile(leave_out):
        return _adapter(root).tests.profile_suite(SuiteProfileRequest(
            _suite(root), 120.0, runner_options=PytestOptions(leave_out_timing_plugins=leave_out)))

    assert profile(False).outcome is not lb.Outcome.COMPLETED
    profiled = profile(True)
    assert profiled.outcome is lb.Outcome.COMPLETED, profiled.reason


def test_without_the_plugins_or_the_option_the_arguments_stay(tmp_path, monkeypatch):
    """Off, a run starts no plugin probe and gets today's arguments. On, it
    adds only the flags of the plugins the project's pytest loads, after
    the run's own arguments."""
    root = _project(tmp_path, "-ra", "def test_one():\n    pass\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    probes = []
    probe = python_adapter._loaded_timing_plugins
    monkeypatch.setattr(python_adapter, "_loaded_timing_plugins",
                        lambda *args, **options: probes.append(args) or probe(*args, **options))

    def arguments(leave_out, disabled=()):
        return _runs._SuiteRunner(_adapter(root), SuiteRunRequest(
            _suite(root), 60.0, runner_options=PytestOptions(
                disabled_plugins=disabled, leave_out_timing_plugins=leave_out))
        )._pytest_args(scratch)

    off = arguments(False)
    assert probes == []
    loaded = {name: flags for name, flags in (
        ("pytest_cov", ["--no-cov"]), ("xdist", ["-n", "0"]),
        ("pytest_benchmark", ["--benchmark-disable"]))
        if importlib.util.find_spec(name)}
    assert arguments(True) == off + [flag for flags in loaded.values() for flag in flags]
    assert len(probes) == 1
    if "xdist" in loaded:
        assert "-n" not in arguments(True, disabled=("xdist",))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    assert arguments(True) == off


def test_the_probe_ends_before_a_timed_command_is_built(tmp_path, monkeypatch):
    """A run and a benchmark take their start time right after building
    the command, and the plugin probe runs before that, so their wall time
    leaves it out."""
    root = _project(tmp_path, "-ra", "def test_one():\n    pass\n")
    events = []
    probe, command = python_adapter._loaded_timing_plugins, _runs.pytest_command
    monkeypatch.setattr(python_adapter, "_loaded_timing_plugins",
                        lambda *args, **options: events.append("probe") or probe(*args, **options))
    monkeypatch.setattr(_runs, "pytest_command",
                        lambda *args: events.append("command") or command(*args))
    options = PytestOptions(leave_out_timing_plugins=True)

    run = _adapter(root).tests.run_suite(SuiteRunRequest(_suite(root), 120.0,
                                                         runner_options=options))
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert events == ["probe", "command"]
    events.clear()
    timed = _adapter(root).tests.benchmark(BenchmarkRequest(
        _suite(root), runs=2, timeout_s=120.0, warmup_runs=0, runner_options=options))
    assert timed.outcome is lb.Outcome.COMPLETED, timed.reason
    assert events[0] == "probe" and events.count("probe") == 1 and "command" in events
