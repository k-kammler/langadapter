"""python_import_root: the Python adapter imports the project's package from
the directory below the project root, while the runs' working directory, pytest
configuration and copy stay the project's."""
import os
import sys

import pytest

from langadapter.python.cosmic_ray import CosmicRaySettings
import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.python import _cosmic_ray as cre
from langadapter.python.adapter import IMPORT_ROOT_VARIABLE, PythonAdapter
from langadapter.runs import SuiteRunRequest

#: A src layout whose pytest configuration registers a marker and turns
#: every warning into an error, as markupsafe's does.
PYPROJECT = ("[tool.pytest.ini_options]\n"
             "markers = [\"slow: a slow test\"]\n"
             "filterwarnings = [\"error\"]\n")
TESTS = ("import warnings\n\nimport pytest\n\nfrom pkg import double\n\n\n"
         "@pytest.mark.slow\ndef test_double():\n    assert double(2) == 4\n\n\n"
         "def test_a_warning_is_an_error():\n    warnings.warn('the configuration applies')\n")


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text(
        "def double(x):\n    return 2 * x\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_pkg.py").write_text(TESTS, encoding="utf-8")
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    return root


def _adapter(root, **values):
    return PythonAdapter(lb.ProjectContext(
        str(root), lb.Interpreter(path=sys.executable),
        environment=lb.Environment(PythonAdapter.context_environment(values))))


@pytest.mark.parametrize("value, expected", [
    ("src", "src"), ("src/", "src"), ("./lib/python", "lib/python"), (" src ", "src")])
def test_the_setting_names_a_dir_below_the_project(value, expected):
    assert PythonAdapter.context_environment({"python_import_root": value}) == {
        IMPORT_ROOT_VARIABLE: expected}


@pytest.mark.parametrize("value", ["", ".", "none", "./"])
def test_the_project_dir_itself_needs_no_variable(value):
    assert PythonAdapter.context_environment({"python_import_root": value}) == {}


@pytest.mark.parametrize("value", ["../src", "src/../..", "C:/src", "/", "/home/u/proj/src",
                                   "//server/share/src", "\\\\server\\share"])
def test_a_dir_outside_the_project_is_refused(value, tmp_path):
    """The runs and the setup's check refuse the same values: an absolute
    directory is not read as one below the project."""
    from langadapter.python._questions import problem

    with pytest.raises(InvalidRequest) as refused:
        PythonAdapter.context_environment({"python_import_root": value})
    assert refused.value.field == "python_import_root"
    machine = type("Machine", (), {"is_dir": staticmethod(lambda path: True)})()
    assert problem(PythonAdapter, "python_import_root", value, tmp_path, machine) == refused.value.message


def test_a_src_layout_runs_with_the_projects_configuration(tmp_path):
    """From the project directory as root, the tests import the package from
    src and run under the project's pytest configuration: the registered
    marker raises no warning, and a warning is an error."""
    root = _project(tmp_path)
    run = _adapter(root, python_import_root="src").run_suite(SuiteRunRequest(
        lb.Suite(str(root / "tests"), ("test_pkg.py",)), timeout_s=120))
    outcomes = {case.test.qualname: case.outcome for case in run.tests}
    assert outcomes == {"test_double": "passed", "test_a_warning_is_an_error": "failed"}, run.output


def test_the_target_identity_names_the_module_from_the_import_root(tmp_path):
    """The validity gate's identity names the module as the tests import it
    (pkg, not src.pkg), with the import root as its root, as it did when the
    consumer passed src as the root."""
    root = _project(tmp_path)
    identity = _adapter(root, python_import_root="src").sources.target_identity(
        "src/pkg/__init__.py").value
    assert identity.module_name == "pkg"
    assert identity.project_root == str((root / "src").resolve())


def test_the_import_root_goes_first_and_the_root_follows(tmp_path):
    root = _project(tmp_path)
    env = _adapter(root, python_import_root="src")._build_env(str(root))
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(root / "src"), str(root)]
    assert _adapter(root)._build_env(str(root))["PYTHONPATH"].split(os.pathsep)[0] == str(root)


def test_cosmic_rays_runs_and_workers_import_from_the_import_root(tmp_path):
    """The session's environment puts the import root first, and each
    worker's clone puts its own copy of that directory first."""
    root = _project(tmp_path)
    engine = cre.CosmicRayEngine(CosmicRaySettings()._engine_config("local"), adapter=_adapter(root, python_import_root="src"))
    env = engine._build_env(root)
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(root / "src"), str(root)]
    roots, _ = engine._import_from_the_copy(root, dict(env))
    assert "src" in roots
    clone = tmp_path / "clone"
    assert cre._clone_pythonpath(env["PYTHONPATH"], root, clone, roots).split(
        os.pathsep)[:2] == [str(clone / "src"), str(clone)]
