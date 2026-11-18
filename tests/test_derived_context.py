"""A context derived for an adapter that runs another adapter on the same
project: what it may add, inside a prepared use and outside one, and the
refusals of a prepared context that stay as they were."""
import os
import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

import langadapter as lb
from langadapter._environment import launch_environment
from langadapter._owned_directory import OwnershipRefused
from langadapter.errors import InvalidRequest
from langadapter.execution import execution_identity, executor_for, work_dir_tempdir
from langadapter.runs import SuiteRunRequest

SRC = Path(lb.__file__).parent


def _base(root, **fields):
    return lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable), **fields)


def test_a_derived_context_adds_variables_and_its_own_work_dir(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    base = _base(tmp_path, environment=lb.Environment({"KEPT": "1"}, inherit=False),
                 interpreter_flags=("-X", "frozen_modules=off"))
    plugin = str(zone / "plugin")
    derived = base.derived(environment={"ADDED": plugin}, work_dir=str(zone))
    assert dict(derived.environment) == {"KEPT": "1", "ADDED": plugin}
    assert not derived.environment.inherit
    assert derived.execution == lb.LocalExecution(work_dir=str(zone))
    assert (derived.root, derived.interpreter, derived.interpreter_flags,
            derived.test_framework) == (base.root, base.interpreter, base.interpreter_flags,
                                        base.test_framework)
    # The observations made in it record what was added, the work directory by name.
    assert execution_identity(derived).to_dict()["environment"] == {
        "ADDED": "<work_dir>" + os.sep + "plugin"}
    assert "environment" not in execution_identity(base).to_dict()


def test_a_derived_context_adds_only_new_variables_on_local_execution(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    base = _base(tmp_path, environment=lb.Environment({"KEPT": "1"}, inherit=False))
    with pytest.raises(InvalidRequest, match="already sets KEPT"):
        base.derived(environment={"KEPT": "2"}, work_dir=str(zone))
    with pytest.raises(InvalidRequest, match="work_dir"):
        base.derived(environment={"ADDED": "1"}, work_dir=str(tmp_path / "missing"))
    with pytest.raises(InvalidRequest, match="at least one"):
        base.derived(environment={}, work_dir=str(zone))
    derived = base.derived(environment={"ADDED": "1"}, work_dir=str(zone))
    with pytest.raises(InvalidRequest, match="not derived again"):
        derived.derived(environment={"MORE": "1"}, work_dir=str(zone))
    container = lb.ProjectContext("/project", execution=lb.ContainerExecution(
        container="c", work_dir=str(zone)))
    with pytest.raises(InvalidRequest, match="local execution"):
        container.derived(environment={"ADDED": "1"}, work_dir=str(zone))


def test_a_derived_variable_overlays_the_callers_as_the_contexts_own_do(tmp_path, monkeypatch):
    # A shell with PYTHONPATH or PYTEST_PLUGINS set must not stop a run.
    zone = tmp_path / "zone"
    zone.mkdir()
    monkeypatch.setenv("LANGADAPTER_TEST_CALLER", "1")
    inheriting = _base(tmp_path, environment=lb.Environment({"KEPT": "1"}))
    with pytest.raises(InvalidRequest, match="already sets KEPT"):
        inheriting.derived(environment={"KEPT": "2"}, work_dir=str(zone))
    derived = inheriting.derived(environment={"LANGADAPTER_TEST_CALLER": "2"},
                                 work_dir=str(zone))
    assert dict(derived.environment) == {"KEPT": "1", "LANGADAPTER_TEST_CALLER": "2"}
    assert derived.environment.inherit
    done = executor_for(derived).run(
        [sys.executable, "-c", "import os; print(os.environ['LANGADAPTER_TEST_CALLER'])"],
        cwd=derived.root, env=launch_environment(derived.environment), timeout=60)
    assert done.stdout.strip() == "2"
    assert execution_identity(derived).to_dict()["environment"] == {
        "LANGADAPTER_TEST_CALLER": "2"}


def test_a_variable_the_context_holds_is_extended_where_asked(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    captured = _base(tmp_path, environment=lb.Environment(
        {"PYTHONPATH": "/user/lib", "PYTEST_PLUGINS": "user_plugin", "KEPT": "1"},
        inherit=False))
    extend = {"PYTHONPATH": os.pathsep, "PYTEST_PLUGINS": ","}
    derived = captured.derived(
        environment={"PYTHONPATH": "/plugin", "PYTEST_PLUGINS": "c_plugin", "ADDED": "1"},
        work_dir=str(zone), extend=extend)
    assert dict(derived.environment) == {
        "PYTHONPATH": "/plugin" + os.pathsep + "/user/lib",
        "PYTEST_PLUGINS": "c_plugin,user_plugin", "KEPT": "1", "ADDED": "1"}
    assert not derived.environment.inherit
    # The observations record what the derivation adds, not the user's part.
    assert execution_identity(derived).to_dict()["environment"] == {
        "PYTHONPATH": "/plugin", "PYTEST_PLUGINS": "c_plugin", "ADDED": "1"}
    with pytest.raises(InvalidRequest, match="already sets KEPT"):
        captured.derived(environment={"KEPT": "2"}, work_dir=str(zone), extend=extend)
    # A variable the context does not hold is set as given.
    empty = _base(tmp_path, environment=lb.Environment({}, inherit=False))
    plain = empty.derived(environment={"PYTHONPATH": "/plugin"}, work_dir=str(zone),
                          extend=extend)
    assert dict(plain.environment) == {"PYTHONPATH": "/plugin"}


@pytest.mark.skipif(os.name != "nt", reason="variable names fold case on Windows only")
def test_extend_names_fold_case_as_the_clash_check_does(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    captured = _base(tmp_path, environment=lb.Environment({"PYTHONPATH": "user_lib"},
                                                          inherit=False))
    derived = captured.derived(environment={"PythonPath": "plugin"}, work_dir=str(zone),
                               extend={"PYTHONPATH": os.pathsep})
    assert dict(derived.environment)["PythonPath"] == "plugin" + os.pathsep + "user_lib"


def test_a_derived_contexts_owned_dir_keeps_its_receipt_in_the_work_dir(
        tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    zone = tmp_path / "zone"
    zone.mkdir()
    derived = _base(tmp_path).derived(environment={"ADDED": "1"}, work_dir=str(zone))
    # A run that ends by an exception keeps its directory, and its receipt.
    with pytest.raises(RuntimeError, match="the run failed"):
        with work_dir_tempdir(derived, executor_for(derived)) as made:
            raise RuntimeError("the run failed")
    assert Path(made).is_dir() and list(zone.glob("langadapter-ownership-*.json"))
    assert not list(temp.glob("langadapter-ownership-*"))
    # A context that is not derived keeps its receipt in the temp directory.
    plain = _base(tmp_path)
    with pytest.raises(RuntimeError, match="the run failed"):
        with work_dir_tempdir(plain, executor_for(plain)):
            raise RuntimeError("the run failed")
    assert len(list(temp.glob("langadapter-ownership-*.json"))) == 1


@pytest.fixture
def prepared(tmp_path):
    project = tmp_path / "source"
    project.mkdir()
    (project / "target.py").write_bytes(b"value = 1\n")
    owner = lb.prepare_project(_base(project), parent=str(tmp_path), receipt_parent=str(tmp_path))
    yield owner
    owner.close()


def test_a_derived_context_runs_inside_a_prepared_use_and_ends_with_it(prepared, tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    with prepared.use() as used:
        derived = used.derived(environment={"LANGADAPTER_TEST_ADDED": "yes"},
                               work_dir=str(zone))
        done = executor_for(derived).run(
            [sys.executable, "-c", "import os; print(os.environ['LANGADAPTER_TEST_ADDED'])"],
            cwd=derived.root, env={**os.environ, **derived.environment}, timeout=60)
        assert done.stdout.strip() == "yes"
        # Changed afterwards, it is refused as any other context of the use.
        for changed, field in (
                (replace(derived, environment=lb.Environment(
                    {**derived.environment, "OTHER": "1"})), "environment"),
                (replace(derived, execution=lb.LocalExecution(work_dir=str(tmp_path))),
                 "execution"),
                (replace(derived, interpreter_flags=("-I",)), "interpreter_flags")):
            with pytest.raises(OwnershipRefused, match=f"changed its selected {field}"):
                executor_for(changed)
    with pytest.raises(OwnershipRefused, match="current workspace use"):
        executor_for(derived)


def test_a_prepared_context_refuses_every_change_it_refused_before(prepared, tmp_path):
    with prepared.use() as used:
        refused = (
            (replace(used, interpreter=lb.Interpreter(path=sys.executable + "x")),
             "changed its selected interpreter"),
            (replace(used, execution=lb.LocalExecution(work_dir=str(tmp_path))),
             "changed its selected execution"),
            (replace(used, interpreter_flags=("-I",)), "changed its selected interpreter_flags"),
            (replace(used, test_framework=lb.TestFrameworkConfig(source="isolated")),
             "changed its selected test_framework"),
            (replace(used, environment=lb.Environment({"ADDED": "1"})),
             "changed its selected environment"),
            (replace(used, root=str(tmp_path)), "obsolete or names a different project root"),
            (lb.ProjectContext(used.root, used.interpreter),
             "requires the context yielded by use"),
        )
        for context, reason in refused:
            with pytest.raises(OwnershipRefused, match=reason):
                executor_for(context)


def test_python_and_java_never_derive_a_context(tmp_path, monkeypatch):
    callers = sorted(path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                     if re.search(r"\.derived\(\s*environment=", path.read_text(encoding="utf-8")))
    assert callers == ["c/_extension.py"]

    def forbidden(self, **kwargs):
        pytest.fail("a Python run derived a context")

    monkeypatch.setattr(lb.ProjectContext, "derived", forbidden)
    (tmp_path / "calc.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import one\n\n\ndef test_one():\n    assert one() == 1\n", encoding="utf-8")
    python = lb.create("python", _base(tmp_path))
    run = python.tests.run_suite(SuiteRunRequest(lb.Suite(str(tmp_path), ("test_calc.py",)),
                                                    timeout_s=120))
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
