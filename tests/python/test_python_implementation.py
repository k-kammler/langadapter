"""The Python implementation with a project context: real pytest and
coverage in the calling interpreter, no consumer configuration."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.outcomes import Outcome
from langadapter.python._plugins import SUITE_INI_TEXT
from langadapter.python.project import _setup_test_env
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import PerTestEvidenceRequest, SuiteRunRequest

pytest.importorskip("coverage")

TARGET = "def add(a, b):\n    if a < 0:\n        return 0\n    return a + b\n"
TESTS = ("from calc import add\n\n"
         "def test_add():\n    assert add(1, 2) == 3\n")


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(TARGET, encoding="utf-8")
    test = root / "tests" / "test_calc.py"
    test.write_text(TESTS, encoding="utf-8")
    return root, test


def _adapter(root):
    context = lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        test_framework=lb.TestFrameworkConfig("isolated"))
    return PythonAdapter(context)


def test_validation_in_the_project_interpreter(tmp_path):
    root, _ = _project(tmp_path)
    adapter = _adapter(root)
    assert adapter._validate_test_source("x = 1\n", str(root)).status == "valid"
    broken = adapter._validate_test_source("def f(:\n    pass\n", str(root))
    assert broken.status == "syntax_error" and broken.line == 1


def test_the_journal_names_the_module_by_its_text(tmp_path):
    """The journal's header names the module under mutation by the sha of its
    text as the engine reads it (UTF-8, universal newlines), so line ends do
    not matter. In a suite delivered with the instrument the module resolves
    against the setup file's source root, wherever the run's working
    directory is."""
    root, test = _project(tmp_path)
    (root / "calc.py").write_bytes(
        TARGET.replace("\n", "\r\n").encode("utf-8"))
    _setup_test_env(_adapter(root), str(test.parent), str(root))
    conftest = test.parent / "conftest.py"
    conftest.write_text(conftest.read_text(encoding="utf-8") + Path(__file__).with_name(
        "carried_instrument.txt").read_text(encoding="utf-8"), encoding="utf-8")
    evidence = tmp_path / "evidence"
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("LANGADAPTER_")}
    env.update(LANGADAPTER_TIMEOUT_EVIDENCE_DIR=str(evidence),
               LANGADAPTER_JOURNAL_MODULE="calc.py")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pytest", str(test.parent), "-q",
         "-p", "no:cacheprovider"],
        cwd=elsewhere, env=env, capture_output=True, text=True, timeout=120,
        check=True)
    header = json.loads(
        (evidence / "journal.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert header["module_sha"] == hashlib.sha1(
        TARGET.encode("utf-8")).hexdigest()


def test_the_prepared_test_directory_gets_the_anchor_and_the_prefix(tmp_path):
    root, test = _project(tmp_path)
    _setup_test_env(_adapter(root), str(test.parent), str(root))
    assert (test.parent / "pytest.ini").read_text(encoding="utf-8") == SUITE_INI_TEXT
    conftest = (test.parent / "conftest.py").read_text(encoding="utf-8")
    assert "LANGADAPTER_MUTATION_SOURCE_ROOT" in conftest and "@PREFIX@" not in conftest


def test_a_suite_dir_without_a_configuration_gets_the_anchor(tmp_path):
    """A run anchors a suite directory that has no pytest.ini with the
    anchor text the prepared test directory gets."""
    root, test = _project(tmp_path)
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    run = adapter.run_suite(SuiteRunRequest(
        lb.Suite(str(test.parent), (test.name,)), 120, isolation_config=True))
    assert run.tests, run.reason
    assert (test.parent / "pytest.ini").read_text(encoding="utf-8") == SUITE_INI_TEXT


@pytest.mark.parametrize("remove", [True, False])
def test_files_a_run_created_are_removed_only_when_asked(tmp_path, remove):
    root, test = _project(tmp_path)
    (root / "mine.txt").write_text("the project's own", encoding="utf-8")
    test.write_text(TESTS + (
        "\n\ndef test_writes():\n"
        "    import pathlib\n"
        "    made = pathlib.Path(__file__).parents[1] / 'made.txt'\n"
        "    assert not made.exists()\n"
        "    made.write_text('x')\n"), encoding="utf-8")
    owner = lb.prepare_project(_adapter(root).context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    try:
        with owner.use() as context:
            adapter = PythonAdapter(context)
            copy = Path(context.root)
            request = SuiteRunRequest(
                suite=lb.Suite(str(copy / "tests"), (test.name,)),
                timeout_s=120, remove_created_files=remove)
            first = adapter.run_suite(request)
            assert first.outcome is Outcome.COMPLETED and (copy / "made.txt").exists()
            (copy / "between.txt").write_text("the caller's", encoding="utf-8")
            second = adapter.run_suite(request)
            assert second.outcome is (Outcome.COMPLETED if remove
                                      else Outcome.TEST_FAILURES)
            assert (copy / "mine.txt").read_text(encoding="utf-8") == "the project's own"
            assert (copy / "between.txt").read_text(encoding="utf-8") == "the caller's"
        assert not (root / "made.txt").exists() and not (root / "between.txt").exists()
    finally:
        owner.close()


def test_the_per_test_evidence_of_a_suite(tmp_path):
    root, test = _project(tmp_path)
    evidence = PythonAdapter(_adapter(root).context).per_test_evidence(
        PerTestEvidenceRequest(suite=lb.Suite(str(test.parent), (test.name,)),
                               target="calc.py", timeout_s=120))
    assert evidence.outcome is Outcome.COMPLETED, evidence.reason
    (entry,) = evidence.tests
    assert entry.test.qualname == "test_add"
    tokens = evidence.tokens()[entry.test]
    assert "L:calc.py:4" in tokens and "L:calc.py:3" not in tokens
    assert entry.time_ms >= 0
    # coverage.py's statements of the target: four, the early return not covered.
    assert (len(evidence.coverage.statements),
            len(evidence.coverage.covered)) == (4, 3)




def test_a_suite_dir_without_a_common_path_names_the_root_absolutely(tmp_path, monkeypatch):
    """A suite directory that shares no path with the project (another drive on
    Windows) gets a conftest naming the project root as an absolute path,
    which the original work directory resolves."""
    project = tmp_path / "project"
    project.mkdir()
    suite = tmp_path / "suite"
    suite.mkdir()

    def no_common_path(paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(os.path, "commonpath", no_common_path)
    _setup_test_env(PythonAdapter(lb.ProjectContext(
        str(project), lb.Interpreter(path=sys.executable))), str(suite), str(project))
    text = (suite / "conftest.py").read_text(encoding="utf-8")
    assert repr(str(project)) in text
