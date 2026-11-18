"""python_test_environment: the variables the Python adapter's own runs give
the project's processes, as NAME=value words, read by the parser every
adapter's test environment setting uses."""
import sys

import pytest

import langadapter as lb
from langadapter.adapter import environment_assignments
from langadapter.errors import InvalidRequest
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import SuiteRunRequest


def test_the_setting_is_declared_and_read_as_name_value_words():
    assert "python_test_environment" in {setting.name for setting in PythonAdapter.project_settings}
    assert PythonAdapter.context_environment(
        {"python_test_environment": "PURE_PYTHON=1 GREETING='a b' EMPTY="}) == {
        "PURE_PYTHON": "1", "GREETING": "a b", "EMPTY": ""}
    assert PythonAdapter.context_environment({"python_test_environment": "  "}) == {}
    assert PythonAdapter.context_environment({}) == {}


def test_a_backslash_and_a_hash_stay_as_written():
    """A Windows path keeps its backslashes, quoted or not, and a '#' is
    part of a value, not a comment."""
    assert PythonAdapter.context_environment(
        {"python_test_environment": 'DATA_DIR=C:\\data OTHER="D:\\x y" TAG=a#b'}) == {
        "DATA_DIR": "C:\\data", "OTHER": "D:\\x y", "TAG": "a#b"}


@pytest.mark.parametrize("value", ["PURE_PYTHON", "=1", "1X=2", "A=1 'B"])
def test_a_word_that_sets_no_variable_is_refused(value):
    with pytest.raises(InvalidRequest) as refused:
        PythonAdapter.context_environment({"python_test_environment": value})
    assert refused.value.field == "python_test_environment"


def test_another_adapters_setting_is_read_by_the_same_parser():
    """An adapter reads its own setting with environment_assignments, and a
    word that sets no variable is refused under that setting's name."""
    assert environment_assignments("A=1 B='x y' EMPTY=", "c_test_environment") == {
        "A": "1", "B": "x y", "EMPTY": ""}
    with pytest.raises(InvalidRequest) as refused:
        environment_assignments("A", "c_test_environment")
    assert refused.value.field == "c_test_environment"


def test_the_variables_reach_the_python_adapters_runs(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_env.py").write_text(
        "import os\n\n\ndef test_env():\n    assert os.environ.get('PURE_PYTHON') == '1'\n",
        encoding="utf-8")
    environment = lb.Environment(PythonAdapter.context_environment(
        {"python_test_environment": "PURE_PYTHON=1"}))
    adapter = PythonAdapter(lb.ProjectContext(
        str(root), lb.Interpreter(path=sys.executable), environment=environment))
    run = adapter.run_suite(SuiteRunRequest(
        lb.Suite(str(root / "tests"), ("test_env.py",)), timeout_s=120))
    assert run.outcome is lb.Outcome.COMPLETED, run.output
