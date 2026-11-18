"""Collect-only observations do not claim test execution or a passing suite."""
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.outcomes import Outcome
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import CollectionRequest
from langadapter.suites import Suite


def prepared(tmp_path, source, executor=None):
    test = tmp_path / "test_one.py"
    test.write_text(source, encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)), executor)
    return adapter, CollectionRequest(Suite(str(tmp_path), (test.name,)), 60)


def test_collection_never_runs_test_bodies_or_leaves_report_files(tmp_path):
    adapter, request = prepared(tmp_path, "def test_one():\n    assert False\n")
    result = adapter.collect_suite(request)
    assert result.outcome == Outcome.COMPLETED and result.diagnostic == ""
    assert result.returncode == 0 and result.wall_time_s is not None
    assert result.identity.suite[0].path == "test_one.py"
    assert result.side_effects.files_written == ()
    # Collection imports the test module, so Python caches its bytecode.
    assert {file.name for file in tmp_path.iterdir()} - {"__pycache__"} == {"test_one.py"}


@pytest.mark.parametrize("source, outcome, diagnostic", [
    ("def helper():\n    pass\n", Outcome.NO_TESTS, "no tests collected"),
    ("MISSING_NAME\n", Outcome.COLLECTION_FAILED, "NameError"),
])
def test_empty_collection_is_distinct_from_a_collection_error(
        tmp_path, source, outcome, diagnostic):
    adapter, request = prepared(tmp_path, source)
    result = adapter.collect_suite(request)
    assert result.outcome == outcome
    assert diagnostic in result.diagnostic


@pytest.mark.parametrize("failure, outcome", [
    (FileNotFoundError("interpreter absent"), Outcome.INFRASTRUCTURE_FAILURE),
    (subprocess.TimeoutExpired("pytest", 60), Outcome.TIMED_OUT),
])
def test_unrun_collection_has_no_success_verdict(tmp_path, failure, outcome):
    class Executor:
        def run(self, *args, **kwargs):
            raise failure

    adapter, request = prepared(tmp_path, "def test_one(): pass\n", Executor())
    result = adapter.collect_suite(request)
    assert result.outcome == outcome and result.reason and result.diagnostic
    assert result.returncode is None and result.wall_time_s is None


def test_crashed_collector_is_not_bad_generated_code_and_keeps_middle_diagnostic(tmp_path):
    class Executor:
        def run(self, command, **kwargs):
            if "--collect-only" not in command:
                return subprocess.CompletedProcess(command, 0, "{}", "")
            output = "before\n" * 2000 + "E   ValueError: middle exception\n" + "after\n" * 2000
            return subprocess.CompletedProcess(command, -11, output, "")

    adapter, request = prepared(tmp_path, "def test_one(): pass\n", Executor())
    result = adapter.collect_suite(request)
    assert result.outcome == Outcome.INFRASTRUCTURE_FAILURE
    # The output comes back whole, and the diagnostic names the middle line.
    assert result.returncode == -11 and len(result.output) > 24000
    assert result.diagnostic == "E   ValueError: middle exception"
    assert "middle exception" in result.output


def test_invalid_request_is_refused_before_starting_a_process(tmp_path):
    adapter, request = prepared(tmp_path, "def test_one(): pass\n")
    with pytest.raises(InvalidRequest, match="timeout_s"):
        adapter.collect_suite(CollectionRequest(request.suite, 0))


def test_the_tool_checks_collection_turns_off_the_plugins_a_run_turns_off(tmp_path, monkeypatch):
    """The tool check's collection stands for the runs, so its pytest command
    turns off the plugins theirs turn off."""
    from langadapter.python._run_definition import DISABLED_PLUGINS
    from langadapter.python._runs import verification_collection

    adapter, _request = prepared(tmp_path, "def test_one():\n    assert True\n")
    executor = adapter._executor()
    seen = []

    class Recorded:
        def __getattr__(self, name):
            return getattr(executor, name)

        def run(self, command, **kwargs):
            seen.append(list(command))
            return executor.run(command, **kwargs)

    monkeypatch.setattr(adapter, "_executor", lambda: Recorded())
    run, _refusal, _first = verification_collection(adapter, "test_one.py", 60)
    assert run.outcome is Outcome.COMPLETED, run.reason
    (command,) = [c for c in seen if "--collect-only" in c]
    pairs = list(zip(command, command[1:]))
    assert all(("-p", f"no:{plugin}") in pairs for plugin in DISABLED_PLUGINS)
