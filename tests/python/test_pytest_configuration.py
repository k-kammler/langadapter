"""The project's pytest configuration is the file pytest itself reads when it
starts in the project's root (its own order and rules), asked of the
project's interpreter, here this one; a staged suite's runs name its copy
beside the suite."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.python import project
from langadapter.python.adapter import PythonAdapter
from langadapter.python.project import isolation_args
from langadapter.runs import BenchmarkRequest, SuiteRunRequest


@pytest.fixture(autouse=True)
def stand_in_pytest_configuration():
    """pytest itself answers here, in place of the session's stand-in."""


def _configuration(root: Path):
    context = lb.ProjectContext(root=str(root), interpreter=lb.Interpreter(path=sys.executable))
    found = PythonAdapter(context)._pytest_config_path(str(root))
    return Path(found).resolve() if found else None


def test_an_empty_pytest_ini_is_the_configuration_pytest_reads(tmp_path):
    """pytest reads a pytest.ini before any other file, even an empty one."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text("", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"-q\"\n", encoding="utf-8")

    assert _configuration(project) == (project / "pytest.ini").resolve()


def test_a_section_named_only_in_a_comment_makes_no_configuration(tmp_path):
    """A pyproject.toml without the [tool.pytest.ini_options] table is no
    configuration, so pytest goes on to the directory above."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "# pytest settings would go in [tool.pytest.ini_options]\n[project]\nname = \"p\"\n",
        encoding="utf-8")

    assert _configuration(project) == (tmp_path / "pytest.ini").resolve()


def test_a_configuration_above_the_project_root_is_the_one_pytest_reads(tmp_path):
    (tmp_path / "tox.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    project = tmp_path / "packages" / "project"
    project.mkdir(parents=True)

    assert _configuration(project) == (tmp_path / "tox.ini").resolve()


def test_options_for_plugins_the_environment_lacks_do_not_hide_the_configuration(tmp_path):
    """The configuration's addopts may name a plugin's options, which an
    environment without the plugin rejects: the file is still the one."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"--benchmark-autosave\"\n", encoding="utf-8")

    assert _configuration(project) == (project / "pyproject.toml").resolve()


def test_a_configured_path_or_isolation_overrides_the_lookup(tmp_path):
    """The test framework setting names a configuration, or isolation for
    pytest's defaults, in place of pytest's own choice."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    def lookup(framework):
        context = lb.ProjectContext(root=str(tmp_path), interpreter=lb.Interpreter(
            path=sys.executable), test_framework=framework)
        return PythonAdapter(context)._pytest_config_path(str(tmp_path))

    assert lookup(lb.TestFrameworkConfig(path="/forced/cfg.ini")) == "/forced/cfg.ini"
    assert lookup(lb.TestFrameworkConfig(source="isolated")) is None


# isolation_args: the configuration's copy beside a staged suite, named with -c.

def _read(path):
    return Path(path).read_text(encoding="utf-8") if Path(path).is_file() else None


def test_the_configuration_is_copied_beside_the_suite_and_named_with_c(tmp_path):
    config = tmp_path / "setup.cfg"
    config.write_text("[tool:pytest]\nfilterwarnings =\n    default\n", encoding="utf-8")
    suite = tmp_path / "suite"
    suite.mkdir()

    assert isolation_args(str(config), _read, str(suite)) == [
        "-p", "no:cacheprovider", "-c", str(suite / "project_pytest.cfg")]
    assert (suite / "project_pytest.cfg").read_text(encoding="utf-8") == config.read_text(
        encoding="utf-8")
    # As a worker clone or a container names the suite's directory.
    assert isolation_args(str(config), _read, str(suite),
                          execution_suite_root="/worker/tests")[-1] == (
        "/worker/tests/project_pytest.cfg")
    # Without a configuration: no copy (the caller anchors at the suite).
    assert isolation_args(None, _read) == ["-p", "no:cacheprovider"]


def test_the_copy_needs_a_suite_root_and_is_not_rewritten_unchanged(tmp_path, monkeypatch):
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\naddopts = -ra\n", encoding="utf-8")
    suite = tmp_path / "suite"
    suite.mkdir()

    with pytest.raises(ValueError, match="requires suite_root"):
        isolation_args(str(config), _read)
    expected = str(suite / "project_pytest.ini")
    assert isolation_args(str(config), _read, str(suite))[-1] == expected
    monkeypatch.setattr(project.os, "replace",
                        lambda *_: pytest.fail("an unchanged config must not be rewritten"))
    assert isolation_args(str(config), _read, str(suite))[-1] == expected


@pytest.mark.parametrize("name", ["pytest.txt", "project.yaml"])
def test_a_configuration_of_another_kind_is_refused(tmp_path, name):
    suite = tmp_path / "suite"
    suite.mkdir()
    with pytest.raises(RuntimeError, match="unsupported pytest config extension"):
        isolation_args(str(tmp_path / name), lambda _path: "[pytest]\n", str(suite))


def test_a_configuration_that_cannot_be_read_or_copied_stops_the_run(tmp_path, monkeypatch):
    suite = tmp_path / "suite"
    suite.mkdir()
    config = str(tmp_path / "pytest.ini")
    with pytest.raises(RuntimeError, match="could not read resolved pytest config"):
        isolation_args(config, lambda _path: None, str(suite))
    with pytest.raises(RuntimeError, match="suite root is not a directory"):
        isolation_args(config, lambda _path: "[pytest]\n", str(tmp_path / "missing"))
    monkeypatch.setattr(project.tempfile, "NamedTemporaryFile",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only")))
    with pytest.raises(RuntimeError, match="could not materialize pytest config"):
        isolation_args(config, lambda _path: "[pytest]\n", str(suite))


def test_the_copy_keeps_a_staged_suite_apart_from_the_projects_tests(tmp_path):
    """With the copy named by -c and the suite as rootdir, pytest runs the
    staged suite alone, although the project's testpaths name its own tests."""
    project_root = tmp_path / "project"
    (project_root / "poison").mkdir(parents=True)
    (project_root / "poison" / "test_poison.py").write_text(
        "raise RuntimeError('project tests must not be collected')\n", encoding="utf-8")
    (project_root / "setup.cfg").write_text("[tool:pytest]\ntestpaths = poison\n",
                                            encoding="utf-8")
    suite = tmp_path / "external_suite"
    suite.mkdir()
    passing = suite / "test_generated.py"
    passing.write_text("def test_generated():\n    assert 2 + 2 == 4\n", encoding="utf-8")

    args = isolation_args(str(project_root / "setup.cfg"), _read, str(suite))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(passing), "-q", *args, f"--rootdir={suite}"],
        cwd=project_root, env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "project tests must not be collected" not in result.stdout + result.stderr


def test_an_interpreter_without_pytest_misses_setup_and_leaves_nothing(tmp_path, monkeypatch):
    """A project interpreter without pytest gives a run and a benchmark the
    outcome missing_setup, with pytest and the interpreter named, before any
    scratch directory or receipt is made."""
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def total(v):\n    return sum(v)\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        "from calc import total\n\n\ndef test_total():\n    assert total([1]) == 1\n",
        encoding="utf-8")
    bare = tmp_path / "bare"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)], check=True)
    python = str(bare / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    adapter = lb.create("python", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=python)))
    suite = lb.Suite(str(root), ("tests/test_calc.py",))
    for result in (adapter.tests.run_suite(SuiteRunRequest(suite, timeout_s=60.0)),
                   adapter.tests.benchmark(BenchmarkRequest(
                       suite, runs=1, timeout_s=60.0, warmup_runs=0))):
        assert result.outcome is lb.Outcome.MISSING_SETUP, result.reason
        assert "pytest" in result.reason and python in result.reason
    assert list(temporary.iterdir()) == []


def test_only_a_missing_pytest_is_told_apart_from_other_probe_failures():
    """The probe's failure is ``_PytestMissing`` only when the interpreter
    cannot import pytest. Any other failure stays a RuntimeError."""
    class Executor:
        def __init__(self, stderr):
            self.stderr = stderr

        def run(self, argv, cwd, timeout):
            return subprocess.CompletedProcess(argv, 1, "", self.stderr)

    with pytest.raises(project._PytestMissing,
                       match="pytest is not installed in the project's interpreter /p"):
        project.project_pytest_config(
            Executor("ModuleNotFoundError: No module named 'pytest'\n"), "/p", "/root",
            flags=())
    with pytest.raises(RuntimeError) as raised:
        project.project_pytest_config(Executor("ERROR: usage: pytest [options]\n"), "/p", "/root",
                                      flags=())
    assert not isinstance(raised.value, project._PytestMissing)
