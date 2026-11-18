"""A pooled PIT run of two Java suites that declare the same test class in
the same package: the classes compile side by side, kills map back to each
suite's tests as its standalone run names them, and per-suite verdicts of
independent suites equal the standalone ones."""
from __future__ import annotations

import json
import os

import pytest

import langadapter as lb
from langadapter.mutation import MutationRequest
from langadapter.sources import PoolMember
from langadapter.sources import SourceStatus

pytestmark = pytest.mark.mutation_tool

TARGET = "src/main/java/demo/Calc.java"

SIGNS = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTest {
    @Test
    void positive() {
        assertEquals(1, Calc.sign(5));
    }

    @Test
    void negative() {
        assertEquals(-1, Calc.sign(-5));
    }
}
"""

CLAMPS = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTest {
    @Test
    void clampsHigh() {
        assertEquals(10, Calc.clamp(50, 0, 10));
    }
}
"""


def _adapter(project, jdks, maven, feature=21):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven}))


def test_a_pooled_run_of_colliding_classes_maps_back_to_each_suite(
        java_project, jdks, maven, tmp_path):
    adapter = _adapter(java_project("maven"), jdks, maven)
    sources = {"seed_000": SIGNS, "seed_001": CLAMPS}
    alone = {}
    for slot, text in sources.items():
        (tmp_path / slot / "demo").mkdir(parents=True)
        (tmp_path / slot / "demo" / "CalcTest.java").write_text(text, encoding="utf-8")
        alone[slot] = lb.Suite(str(tmp_path / slot), ("demo/CalcTest.java",))
    observation = adapter.sources.stage_pool(
        [PoolMember(slot, "demo/CalcTest.java", text) for slot, text in sources.items()])
    assert observation.status is SourceStatus.COMPLETED, observation.reason
    pool = observation.value
    assert [f.path for f in pool.files] == ["demo/CalcTest_seed_000.java",
                                            "demo/CalcTest_seed_001.java"]
    (tmp_path / "pool").mkdir()
    suite = pool.write(str(tmp_path / "pool"))

    with adapter.mutations.open_session("pit") as session:
        pooled = session.run(MutationRequest(TARGET, suite, attribution="all"))
        standalone = {slot: session.run(MutationRequest(TARGET, slot_suite, attribution="all"))
                      for slot, slot_suite in alone.items()}
    assert pooled.outcome is lb.Outcome.COMPLETED and pooled.complete, pooled.reason
    for slot, run in standalone.items():
        assert run.outcome is lb.Outcome.COMPLETED, run.reason
        measured = {(m.key, test) for m in run.mutants for test in m.killing_tests}
        mapped = {(m.key, pool.original_test(test)[1]) for m in pooled.mutants
                  for test in m.killing_tests if pool.slot_of(test.file) == slot}
        assert mapped == measured and measured
        for mutant in pooled.mutants:
            for killer in mutant.killers:
                if pool.slot_of_killer(killer) == slot:
                    assert pool.original_text(killer, slot).startswith("demo.CalcTest#")
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
