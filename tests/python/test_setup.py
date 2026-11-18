"""Public preparation of generated suites and explicit staged refreshes."""
from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from langadapter.context import ContainerExecution, Interpreter, ProjectContext
from langadapter.errors import InvalidRequest
from langadapter.outcomes import Outcome
from langadapter.python import project as project_module
from langadapter.python.adapter import PythonAdapter
from langadapter.python.project import (
    _INSTRUMENT_MARKER,
)


def _adapter(project, execution=None):
    kwargs = {"execution": execution} if execution is not None else {}
    return PythonAdapter(ProjectContext(
        root=str(project), interpreter=Interpreter(path=sys.executable), **kwargs))


def test_no_preparation_places_the_determinism_instrument():
    """The instrument is placed by no preparation: the adapter
    has no ensure_suite_instrument, prepare_suite takes no instrument and
    its result reports none, and the hook names prepare_suite alone."""
    import dataclasses
    import inspect

    from langadapter import hooks
    from langadapter.python.project import SuitePreparation

    assert not hasattr(PythonAdapter, "ensure_suite_instrument")
    assert "instrument" not in inspect.signature(PythonAdapter.prepare_suite).parameters
    assert "instrument" not in {field.name for field in dataclasses.fields(SuitePreparation)}
    assert hooks.hook("prepare_suite").methods == ("prepare_suite",)


@pytest.mark.parametrize("isolation", [False, True])
def test_preparation_options_are_explicit(tmp_path, isolation):
    project = tmp_path / "project"
    suite = project / "tests"
    suite.mkdir(parents=True)
    result = _adapter(project).prepare_suite(str(suite), isolation_config=isolation)
    assert result.outcome == Outcome.COMPLETED
    assert result.setup_files == (str(suite / "conftest.py"),)
    body = (suite / "conftest.py").read_text(encoding="utf-8")
    assert "LANGADAPTER_MUTATION_SOURCE_ROOT" in body
    assert "TESTUARY_" not in body
    assert _INSTRUMENT_MARKER not in body
    assert (suite / "pytest.ini").is_file() == isolation
    assert result.isolation_files == ((str(suite / "pytest.ini"),) if isolation else ())


def test_preparation_survives_a_worker_clone(tmp_path):
    project = tmp_path / "original" / "project"
    suite = project / "tests"
    suite.mkdir(parents=True)
    (project / "clone_probe.py").write_text("VALUE = 'original'\n", encoding="utf-8")
    result = _adapter(project).prepare_suite(str(suite))
    assert result.outcome == Outcome.COMPLETED
    clone = tmp_path / "clone"
    shutil.copytree(project, clone)
    (clone / "clone_probe.py").write_text("VALUE = 'mutated'\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", "import runpy; runpy.run_path('conftest.py'); "
         "import clone_probe; assert clone_probe.VALUE == 'mutated'"],
        cwd=clone / "tests", capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_container_preparation_uses_executing_paths(tmp_path):
    zone = tmp_path / "zone"
    suite = zone / "tests"
    suite.mkdir(parents=True)
    adapter = _adapter("/testbed", ContainerExecution(
        container="project", work_dir=str(zone), container_work_dir="/workzone"))
    result = adapter.prepare_suite(str(suite))
    assert result.outcome == Outcome.COMPLETED
    body = (suite / "conftest.py").read_text(encoding="utf-8")
    assert "'testbed'" in body
    assert str(tmp_path) not in body
    assert "\\\\" not in body


def test_preparation_requires_an_existing_directory(tmp_path):
    adapter = _adapter(tmp_path)
    with pytest.raises(InvalidRequest, match="suite_root"):
        adapter.prepare_suite(str(tmp_path / "absent"))


def test_container_preparation_rejects_a_directory_outside_the_work_dir(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    adapter = _adapter("/testbed", ContainerExecution(container="project", work_dir=str(zone)))
    with pytest.raises(InvalidRequest, match="shared work folder"):
        adapter.prepare_suite(str(tmp_path))


def test_preparation_reports_a_failed_write(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)

    def failed_write(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(project_module, "_setup_test_env", failed_write)
    result = adapter.prepare_suite(str(tmp_path))
    assert result.outcome == Outcome.INFRASTRUCTURE_FAILURE
    assert result.reason == "write failed"
    assert result.setup_files == ()


def test_setup_discovery_lists_only_the_test_paths_ancestors(tmp_path):
    from langadapter.python.project import loaded_setup_files

    child = tmp_path / "child"
    sibling = tmp_path / "sibling"
    child.mkdir()
    sibling.mkdir()
    for directory in (tmp_path, child, sibling):
        (directory / "conftest.py").write_text("# setup\n", encoding="utf-8")
    test = child / "test_one.py"
    test.write_text("def test_one(): pass\n", encoding="utf-8")
    assert loaded_setup_files(tmp_path, [test]) == sorted([
        tmp_path / "conftest.py", child / "conftest.py",
    ])
    with pytest.raises(InvalidRequest, match="under"):
        loaded_setup_files(child, [sibling / "test_elsewhere.py"])


def test_the_instrument_marker_divides_the_shim_from_the_instrument(tmp_path,
                                                                   carried_instrument):
    """What precedes the marker in the setup file of a suite delivered with
    the instrument is the import shim, what follows it the instrument; a
    prepared setup file has no marker."""
    suite = tmp_path / "project" / "tests"
    suite.mkdir(parents=True)
    _adapter(tmp_path / "project").prepare_suite(str(suite))
    conftest = suite / "conftest.py"
    prepared = conftest.read_text(encoding="utf-8")
    assert _INSTRUMENT_MARKER not in prepared
    conftest.write_text(prepared + carried_instrument, encoding="utf-8")
    head, marker, tail = conftest.read_text(encoding="utf-8").partition(_INSTRUMENT_MARKER)
    assert marker and head.startswith(prepared)
    assert "LANGADAPTER_MUTATION_SOURCE_ROOT" in head
    assert "def _deterministic_random" in tail
