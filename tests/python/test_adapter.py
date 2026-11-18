"""The Python adapter's language facts and capability report come from the
context's own interpreter."""
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.execution import Executor
from langadapter.python._profile import TOOL_NAMES
from langadapter.python.adapter import PythonAdapter


def _context(root, interpreter=sys.executable, **changes):
    return lb.ProjectContext(root=str(root),
                             interpreter=lb.Interpreter(path=str(interpreter)),
                             **changes)


def _fake_interpreter(directory: Path, version_line: str) -> Path:
    """An executable that answers only ``--version``, like an interpreter."""
    if os.name == "nt":
        path = directory / "python.cmd"
        path.write_text(f"@echo {version_line}\r\n", encoding="ascii")
    else:
        path = directory / "python"
        path.write_text(f"#!/bin/sh\necho '{version_line}'\n", encoding="ascii")
        path.chmod(0o755)
    return path


def test_python_is_registered_when_the_package_is_imported(tmp_path):
    assert "python" in lb.names()
    assert lb.language_for("pkg/module.py") == "python"
    assert isinstance(lb.create("python", _context(tmp_path)), PythonAdapter)


def test_the_facts_are_data(tmp_path):
    facts = lb.create("python", _context(tmp_path)).language_facts()
    assert facts.language == "Python"
    assert facts.test_framework == "pytest"
    assert facts.code_fence == "python"
    assert facts.runtime_version == f"Python {sys.version.split()[0]}"
    assert facts.language_level == f"Python {sys.version_info[0]}.{sys.version_info[1]}"
    assert facts.unmatched_evidence_kept is True
    assert "test_*.py" in facts.test_file_patterns
    # The timeout mark only where the project's pytest has it.
    from importlib import metadata
    has_mark = any(point.name == "timeout"
                   for point in metadata.entry_points(group="pytest11"))
    assert (facts.timeout_marker.format(seconds=120) if has_mark
            else facts.timeout_marker) == ("@pytest.mark.timeout(120)" if has_mark else None)


def test_two_instances_report_their_own_interpreter(tmp_path):
    other = _fake_interpreter(tmp_path, "Python 3.99.1")
    ours = lb.create("python", _context(tmp_path))
    theirs = lb.create("python", _context(tmp_path, interpreter=other))
    assert theirs.language_facts().runtime_version == "Python 3.99.1"
    assert ours.language_facts().runtime_version == \
        f"Python {sys.version.split()[0]}"


def test_a_source_only_context_reports_the_facts_without_a_runtime(tmp_path):
    facts = lb.create("python", lb.ProjectContext(root=str(tmp_path))).language_facts()
    assert (facts.language, facts.code_fence, facts.runtime_version, facts.language_level,
            facts.timeout_marker) == ("Python", "python", None, None, None)
    assert "test_*.py" in facts.test_file_patterns


def test_a_stated_runtime_version_is_not_probed(tmp_path):
    class Refusing(Executor):
        def run(self, argv, **kw):
            raise AssertionError("the interpreter must not be started")

    adapter = PythonAdapter(
        _context(tmp_path, runtime_version="Python 3.10.16"),
        executor=Refusing())
    assert adapter.language_facts().runtime_version == "Python 3.10.16"


def test_the_report_of_a_complete_environment(tmp_path):
    report = lb.create("python", _context(tmp_path)).capabilities()
    assert report.outcome is lb.Outcome.COMPLETED
    assert report.runtime.endswith(sys.version.split()[0])
    assert report.branch_coverage == "arcs"
    names = [capability.name for capability in report.capabilities]
    assert set(names) == {
        "suite_runs", "coverage", "per_test_evidence",
        "per_test_kill_attribution", "mutation", "source_merging",
        "generated_suite_operations"}
    found = {tool.name: tool.found for tool in report.tools}
    assert set(found) == set(TOOL_NAMES)
    if found["pytest"]:
        assert report.capability("suite_runs").available


def test_coverage_missing_in_the_project_interpreter(tmp_path):
    """A project environment without the caller's coverage: the report
    names the tool and the interpreter."""
    pytest.importorskip("coverage")
    env_dir = tmp_path / "bare"
    # Symlinked on POSIX, as ``python -m venv`` makes it: a copied
    # relocatable interpreter (uv's) cannot find its standard library.
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(
        env_dir)
    python = env_dir / ("Scripts/python.exe" if os.name == "nt"
                        else "bin/python")
    report = lb.create("python", _context(tmp_path,
                                          interpreter=python)).capabilities()
    assert report.outcome is lb.Outcome.COMPLETED
    coverage = report.capability("coverage")
    assert coverage.availability is lb.Availability.MISSING_SETUP
    assert "coverage" in coverage.reason and str(python) in coverage.reason
    assert report.capability("mutation").availability is \
        lb.Availability.MISSING_SETUP
    assert report.capability("source_merging").available
    assert report.mutation_engines == ()
    assert all(tool.found is None and tool.pinned is not None
               for tool in report.tools)


def test_an_interpreter_that_cannot_be_probed(tmp_path):
    fake = _fake_interpreter(tmp_path, "Python 3.99.1")
    report = lb.create("python", _context(tmp_path,
                                          interpreter=fake)).capabilities()
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert str(fake) in report.reason
    assert report.capability("suite_runs").availability is \
        lb.Availability.MISSING_SETUP
    assert report.runtime is None


def test_a_container_probe_runs_in_the_container(tmp_path):
    seen = []

    class Recording(Executor):
        def run(self, argv, **kw):
            seen.append(list(argv))
            payload = {"implementation": "CPython", "version": "3.10.16",
                       "tools": {"pytest": "8.1.1", "coverage": "7.6.8",
                                 "cosmic-ray": "8.3.0"}}
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(payload) + "\n", "")

    context = lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
        execution=lb.ContainerExecution(container="c1",
                                        work_dir=str(tmp_path)),
        interpreter_flags=("-X", "frozen_modules=off"))
    report = PythonAdapter(context, executor=Recording()).capabilities()
    assert seen[0][:4] == ["/venv/bin/python", "-X", "frozen_modules=off",
                           "-c"]
    assert report.runtime == "CPython 3.10.16"
    assert report.mutation_engines == ("cosmic_ray",)
    mismatched = [t for t in report.tools if t.pinned not in (None, t.found)]
    assert [(t.name, t.pinned, t.found) for t in mismatched] == [
        ("cosmic-ray", "8.4.1", "8.3.0")]


def test_every_interpreter_start_takes_the_contexts_flags(tmp_path, monkeypatch):
    """Each probe and helper that starts the project interpreter, here and
    in a container, gives it the context's interpreter_flags right after
    its path."""
    from langadapter.execution import LocalExecutor
    from langadapter.python import _cosmic_ray, _project_files, adapter, project
    from langadapter.python.project import PytestOptions
    from langadapter.runs import SuiteRunRequest

    # pytest itself names the configuration, in place of the session's stand-in.
    monkeypatch.setattr(adapter, "project_pytest_config", project.project_pytest_config)

    flags = ["-X", "frozen_modules=off"]
    # This interpreter by another spelling, so its version is probed.
    python = os.path.join(os.path.dirname(sys.executable), ".", os.path.basename(sys.executable))
    commands = []

    class Recording(LocalExecutor):
        def __init__(self, answered):
            super().__init__()
            self.answered = answered

        def run(self, argv, **options):
            commands.append(list(argv))
            if self.answered:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return super().run(argv, **options)

    root = tmp_path / "project"
    root.mkdir()
    (root / "test_calc.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    local = PythonAdapter(_context(root, interpreter=python, interpreter_flags=tuple(flags)),
                          executor=Recording(answered=False))
    container = PythonAdapter(lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
        execution=lb.ContainerExecution(container="c1", work_dir=str(tmp_path)),
        interpreter_flags=tuple(flags)), executor=Recording(answered=True))
    operations = {
        "version": local.runtime_version,
        "validation": lambda: local.validate_source("x = 1\n"),
        "operators": local.mutation_operators,
        "configuration and timing plugins": lambda: local.run_suite(SuiteRunRequest(
            lb.Suite(str(root), ("test_calc.py",)), 120,
            runner_options=PytestOptions(leave_out_timing_plugins=True))),
        "container read": lambda: container._read_file_content("/testbed/calc.py"),
        "container file": lambda: _project_files.file_identity(container, _project_files.project_file(
            container, "target", "/testbed/calc.py")),
        "shadowed roots": lambda: _cosmic_ray.CosmicRayEngine(
            {}, adapter=container)._source_dirs(Path("/testbed"), {}),
        "allowed processors": lambda: container._allowed_cpus([], "/testbed", {}),
    }
    lacking = {}
    for name, operation in operations.items():
        del commands[:]
        operation()
        started = [argv[:3] for argv in commands if argv[0] in (python, "/venv/bin/python")]
        if not started or any(argv[1:] != flags for argv in started):
            lacking[name] = started
    assert lacking == {}
