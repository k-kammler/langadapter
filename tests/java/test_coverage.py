"""Java coverage through JaCoCo: executable lines, covered lines and branch
counters, equal for the Maven and Gradle forms of a project.

In the fixture's ``Calc``, lines 8 and 11 branch. The suite calls
``sign(5)``, ``sign(-5)`` and ``clamp(50, 0, 10)``, so ``return 0`` (line
14) never runs and line 11 branches one way only. JaCoCo filters out the
private empty constructor (line 4)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.runs import SuiteRunRequest

TARGET = "src/main/java/demo/Calc.java"


def _adapter(project, jdks, maven, gradle, feature=21):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))


def _covered_run(project, adapter, target=TARGET):
    return adapter.run_suite(SuiteRunRequest(
        suite=lb.Suite(str(project / "src" / "test" / "java"),
                       ("demo/CalcTest.java",)),
        timeout_s=300, coverage_target=target))


def test_the_coverage_of_the_target_is_jacocos_and_alike_for_both_builds(
        java_project, jdks, maven, gradle):
    observations = {}
    for build in ("maven", "gradle"):
        project = java_project(build)
        run = _covered_run(project, _adapter(project, jdks, maven, gradle))
        assert run.outcome is lb.Outcome.COMPLETED, (run.reason, run.output)
        coverage = run.coverage
        assert coverage is not None, run.output
        assert run.coverage_report_s > 0 and run.coverage_statements_s is None
        assert run.identity.target.path == TARGET
        assert coverage.target.path == TARGET
        assert {8, 9, 11, 12, 14, 18} <= set(coverage.statements)
        assert set(coverage.covered) == {8, 9, 11, 12, 18}
        assert coverage.missing() == (14,)
        assert 4 not in coverage.statements
        assert (len(coverage.branches), len(coverage.taken)) == (4, 3)
        assert {b.source for b in coverage.missing_branches()} == {11}
        observations[build] = coverage.to_coverage_matrix()
    assert observations["maven"] == observations["gradle"]


def test_a_coverage_target_that_does_not_exist_is_a_request_error(
        java_project, jdks, maven, gradle):
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    with pytest.raises(lb.InvalidRequest, match="coverage_target"):
        _covered_run(project, adapter, "src/main/java/demo/Missing.java")


def test_a_run_without_a_target_measures_no_coverage(java_project, jdks, maven, gradle):
    project = java_project("gradle")
    run = _covered_run(project, _adapter(project, jdks, maven, gradle), None)
    assert run.outcome is lb.Outcome.COMPLETED
    assert run.coverage is None and run.identity.target is None
    assert Path(project).is_dir()


#: A target whose file declares a second, non-public top-level type.
TWO_TYPES = """\
package demo;

public final class Calc {
    private Calc() {
    }

    public static int twice(int x) {
        return CalcHelper.add(x, x);
    }
}

final class CalcHelper {
    private CalcHelper() {
    }

    static int add(int a, int b) {
        return a + b;
    }
}
"""

TWO_TYPES_TEST = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTest {
    @Test
    void twice() {
        assertEquals(4, Calc.twice(2));
    }
}
"""


def test_a_second_top_level_type_of_the_target_counts_its_execution(
        java_project, jdks, maven, gradle):
    """JaCoCo records every type the target's file declares, so a line of a
    second top-level type that a test runs is covered, in a coverage run and
    in the test's evidence."""
    from langadapter.runs import PerTestEvidenceRequest

    project = java_project("maven")
    (project / TARGET).write_text(TWO_TYPES, encoding="utf-8", newline="\n")
    (project / "src/test/java/demo/CalcTest.java").write_text(
        TWO_TYPES_TEST, encoding="utf-8", newline="\n")
    adapter = _adapter(project, jdks, maven, gradle)
    run = _covered_run(project, adapter)
    assert run.outcome is lb.Outcome.COMPLETED, (run.reason, run.output)
    assert {8, 17} <= set(run.coverage.covered), run.coverage.to_coverage_matrix()
    evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
        suite=lb.Suite(str(project / "src" / "test" / "java"), ("demo/CalcTest.java",)),
        target=TARGET, timeout_s=300))
    assert evidence.outcome is lb.Outcome.COMPLETED, evidence.reason
    lines = {item.line for test in evidence.tests for item in test.items if hasattr(item, "line")}
    assert {8, 17} <= lines, lines
