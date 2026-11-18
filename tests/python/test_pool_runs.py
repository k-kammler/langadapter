"""A pooled run of Python suites staged by the adapter, with real Cosmic Ray
and pytest: suites sharing a file and test name get their pooled kills
mapped back to their own tests, named as in their standalone runs, and the
verdicts derived per independent suite equal the standalone ones."""
import json
import re
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.sources import PoolMember
from langadapter.python import _cosmic_ray as engine_module
from langadapter.sources import SourceStatus

TARGET = ("def add(a, b):\n    return a + b\n\n\n"
          "def sub(a, b):\n    return a - b\n")
# Both starter suites stage test_candidate.py with a test named test_value.
ADDS = "from arith import add\n\n\ndef test_value():\n    assert add(2, 3) == 5\n"
SUBS = "from arith import sub\n\n\ndef test_value():\n    assert sub(5, 3) == 2\n"
OPERATORS = ("core/ReplaceBinaryOperator_Add_Sub", "core/ReplaceBinaryOperator_Sub_Add")


def _cosmic_ray_runs() -> bool:
    executable = engine_module._project_bin("cosmic-ray", sys.executable)
    try:
        return subprocess.run([executable, "--help"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [pytest.mark.skipif(
    not _cosmic_ray_runs(), reason="needs cosmic-ray beside the interpreter"),
    pytest.mark.mutation_tool]


def _dir(path, files):
    path.mkdir(parents=True)
    for name, text in files.items():
        (path / name).write_text(text, encoding="utf-8")
    return path


def test_a_pooled_run_maps_back_to_each_suites_standalone_run(tmp_path):
    root = _dir(tmp_path / "proj", {"arith.py": TARGET})
    adapter = lb.create("python", lb.ProjectContext(
        str(root), lb.Interpreter(path=sys.executable)))
    alone = {"seed_000": _dir(tmp_path / "seed_000", {"test_candidate.py": ADDS}),
             "seed_001": _dir(tmp_path / "seed_001", {"test_candidate.py": SUBS})}
    observation = adapter.sources.stage_pool(
        [PoolMember(slot, "test_candidate.py", (seed_dir / "test_candidate.py").read_text(
            encoding="utf-8")) for slot, seed_dir in alone.items()])
    assert observation.status is SourceStatus.COMPLETED, observation.reason
    pool = observation.value
    pooled_root = tmp_path / "pool"
    pooled_root.mkdir()
    suite = pool.write(str(pooled_root))

    def request(test_suite):
        return MutationRequest("arith.py", test_suite, Catalogue(operators=OPERATORS),
                               attribution="all", per_mutant_timeout=30)

    with adapter.mutations.open_session("cosmic_ray") as session:
        pooled = session.run(request(suite))
        standalone = {slot: session.run(request(lb.Suite(str(seed_dir), ("test_candidate.py",))))
                      for slot, seed_dir in alone.items()}
    assert pooled.outcome is lb.Outcome.COMPLETED and pooled.complete, pooled.reason
    assert len(pooled.mutants) == 2
    for slot, run in standalone.items():
        assert run.outcome is lb.Outcome.COMPLETED, run.reason
        measured = {(m.key, test) for m in run.mutants for test in m.killing_tests}
        mapped = {(m.key, pool.original_test(test)[1]) for m in pooled.mutants
                  for test in m.killing_tests if pool.slot_of(test.file) == slot}
        # Each suite kills its own function's mutant, named as alone.
        assert mapped == measured and len(measured) == 1
        killers = [pool.original_text(killer, slot) for m in pooled.mutants
                   for killer in m.killers if pool.slot_of_killer(killer) == slot]
        # pytest prints the path with the platform's separator, and the
        # pooled file's name reads as the suite's own.
        assert [re.split(r"[\\/]", k)[-1] for k in killers] == ["test_candidate.py::test_value"]
        # The derivation by the shared rule gives the suite's own verdicts.
        kept = {test for m in pooled.mutants for test in m.killing_tests
                if pool.slot_of(test.file) == slot}
        derived = pooled.derive_for_tests(kept, covered_lines=())
        assert ({m.key: m.status for m in derived.mutants}
                == {m.key: m.status for m in run.mutants})
        # The same from the saved matrices, compared by each row's key.
        saved = pool.derive(pooled.to_kill_matrix(), slot, covered_lines=())
        assert ({json.dumps(row["key"], sort_keys=True): row["status"]
                 for row in saved["mutants"]}
                == {json.dumps(row["key"], sort_keys=True): row["status"]
                    for row in run.to_kill_matrix()["mutants"]})
