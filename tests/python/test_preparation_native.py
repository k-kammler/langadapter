"""Opt-in public prepared-project callers with installed pytest and Cosmic Ray."""
import json
import os
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter._owned_directory import OwnershipRefused
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.runs import SuiteRunRequest


pytestmark = pytest.mark.skipif(
    os.environ.get("LANGADAPTER_TEST_PREPARED") != "1",
    reason="requires explicitly scheduled prepared-project native verification")


def test_public_python_copy_restores_native_input_changes_and_keeps_durable_evidence(tmp_path):
    source = tmp_path / "source"
    (source / "tests").mkdir(parents=True)
    (source / "arith.py").write_text("value = 1\n", encoding="ascii")
    (source / "fixture.txt").write_text("original resource", encoding="ascii")
    (source / "pytest.ini").write_text("[pytest]\n", encoding="ascii")
    proof = tmp_path / "native-proof.json"
    (source / "tests" / "test_arith.py").write_text(
        "import json, os\nfrom pathlib import Path\nimport arith\n\n"
        "def test_resource():\n"
        "    root = Path(arith.__file__).resolve().parent\n"
        "    assert arith.value == 1\n"
        "    assert (root / 'fixture.txt').read_text() == 'original resource'\n"
        "    Path(os.environ['PREPARED_PROOF']).write_text(json.dumps({'root': str(root)}))\n"
        "    (root / 'fixture.txt').unlink()\n"
        "    (root / 'arith.py').write_text('value = 2\\n')\n", encoding="ascii")
    context = lb.ProjectContext(str(source), lb.Interpreter(path=sys.executable),
        test_framework=lb.TestFrameworkConfig(path="pytest.ini"),
        environment={"PREPARED_PROOF": str(proof)})
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))
    results = []
    for _ in range(2):
        with owner.use() as selected:
            adapter = lb.create("python", selected)
            suite = lb.Suite(str(Path(selected.root) / "tests"), ("test_arith.py",))
            run = adapter.tests.run_suite(SuiteRunRequest(suite, timeout_s=60))
            assert run.outcome is lb.Outcome.COMPLETED, (run.reason, run.output)
            assert run.counts.passed == 1
            assert json.loads(proof.read_text())["root"] == str(Path(owner.root).resolve())
            assert not (Path(owner.root) / "fixture.txt").exists()
            assert (Path(owner.root) / "arith.py").read_text() == "value = 2\n"
            results.append(run)
        with pytest.raises(OwnershipRefused):
            adapter.tests.run_suite(SuiteRunRequest(suite, timeout_s=60))
    owner.close()
    assert proof.is_file() and len(results) == 2
    assert (source / "fixture.txt").read_text() == "original resource"
    assert (source / "arith.py").read_text() == "value = 1\n"


@pytest.mark.mutation_tool
def test_public_python_mutation_session_closes_before_prepared_copy_release(tmp_path):
    source = tmp_path / "source"
    (source / "tests").mkdir(parents=True)
    target = source / "arith.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="ascii")
    (source / "tests" / "test_arith.py").write_text(
        "from arith import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="ascii")
    original = target.read_bytes()
    owner = lb.prepare_project(lb.ProjectContext(str(source), lb.Interpreter(path=sys.executable)),
                               parent=str(tmp_path), receipt_parent=str(tmp_path))
    with owner.use() as selected:
        adapter = lb.create("python", selected)
        request = MutationRequest("arith.py", lb.Suite(str(Path(selected.root) / "tests"),
            ("test_arith.py",)), catalogue=Catalogue(operators=("core/ReplaceBinaryOperator_Add_Sub",)))
        with adapter.mutations.open_session("cosmic_ray") as session:
            measured = session.run(request)
        assert measured.outcome is lb.Outcome.COMPLETED and measured.complete, measured.reason
        assert measured.mutants and measured.score() == 100.0
        assert not session.is_open
        assert (Path(owner.root) / "arith.py").read_bytes() == original
    owner.close()
    assert target.read_bytes() == original
    assert measured.to_kill_matrix()["summary"]["total"] == len(measured.mutants)
