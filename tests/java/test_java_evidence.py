"""Java per-test evidence from one JaCoCo run with a listener: each test
method's covered target lines and branches, its time and its kills, equal
for both builds and all JUnit generations, with static initialization in
no test."""
from __future__ import annotations

import os

import pytest

import langadapter as lb
from langadapter.mutation import MutationRequest
from langadapter.runs import PerTestEvidenceRequest, SuiteRunRequest


def test_tests_configured_to_run_in_parallel_run_one_at_a_time(
        java_project, jdks, maven, gradle):
    """Jupiter's parallel execution is off in every run, as under PIT,
    whatever the project configures: two tests that wait for each other
    fail in the normal run and in the per-test evidence alike. Run in
    parallel they passed, and the evidence reported their coverage as
    inseparable. The listener's overlap check stays for an engine that
    runs tests together."""
    from dataclasses import replace
    import os

    import langadapter as lb
    from langadapter.java.adapter import JavaAdapter
    from langadapter.runs import PerTestEvidenceRequest

    if 21 not in jdks:
        pytest.skip("needs a JDK 21")
    root = java_project("maven")
    test_root = root / "src/test/java"
    (test_root / "demo/ParallelTest.java").write_text('''package demo;
import java.util.concurrent.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.parallel.*;
import static org.junit.jupiter.api.Assertions.*;
@Execution(ExecutionMode.CONCURRENT)
class ParallelTest {
    static final CountDownLatch started = new CountDownLatch(2);
    void meet() throws Exception {
        started.countDown();
        assertTrue(started.await(2, TimeUnit.SECONDS));
    }
    @Test void first() throws Exception { meet(); assertEquals(1, Calc.sign(1)); }
    @Test void second() throws Exception { meet(); assertEquals(-1, Calc.sign(-1)); }
}
''', encoding="utf-8")
    java = jdks[21] / "bin" / ("java.exe" if os.name == "nt" else "java")
    adapter = JavaAdapter(lb.ProjectContext(str(root), lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))
    resolved = adapter.project()
    adapter._resolution = replace(resolved, project=replace(resolved.project,
        test_system_properties={"junit.jupiter.execution.parallel.enabled": "true",
                                "junit.jupiter.execution.parallel.config.strategy": "fixed",
                                "junit.jupiter.execution.parallel.config.fixed.parallelism": "2"}))
    suite = lb.Suite(str(test_root), ("demo/ParallelTest.java",))
    normal = adapter.run_suite(SuiteRunRequest(suite, timeout_s=120))
    assert normal.outcome is lb.Outcome.TEST_FAILURES, normal
    result = adapter.per_test_evidence(PerTestEvidenceRequest(
        suite, "src/main/java/demo/Calc.java", 120))
    assert result.outcome is lb.Outcome.TEST_FAILURES, result
    assert not result.tests

TARGET = "src/main/java/demo/Calc.java"
F = "src/main/java/demo/Calc.java"

MORE = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class CalcMoreTest {
    @ParameterizedTest
    @ValueSource(ints = {3, 7})
    void positives(int value) {
        assertEquals(1, Calc.sign(value));
    }

    @Nested
    class Clamping {
        @Test
        void keepsInside() {
            assertEquals(5, Calc.clamp(5, 0, 10));
        }
    }
}
"""

REGISTRY = """\
package demo;

public final class Registry {
    static final int[] TABLE = {1, 2, 3};

    private Registry() {
    }

    public static int first() {
        return TABLE[0];
    }

    public static int size() {
        return TABLE.length;
    }
}
"""

REGISTRY_TEST = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class RegistryTest {
    @Test
    void first() {
        assertEquals(1, Registry.first());
    }

    @Test
    void size() {
        assertEquals(3, Registry.size());
    }
}
"""

#: The target lines and branches each fixture test covers. ``positive``
#: and ``negative`` take different branches of line 8.
EXPECTED = {
    ("CalcTest.positive", ): {f"L:{F}:8", f"L:{F}:9", f"B:{F}:8->0"},
    ("CalcTest.negative", ): {f"L:{F}:8", f"L:{F}:11", f"L:{F}:12",
                              f"B:{F}:8->1", f"B:{F}:11->0"},
    ("CalcTest.clampsHigh", ): {f"L:{F}:18"},
}


def _adapter(project, jdks, maven, gradle, feature=21):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))


def _write(project, name, text, main=False):
    base = project / "src" / ("main" if main else "test") / "java" / "demo"
    (base / name).write_text(text, encoding="utf-8", newline="\n")
    return f"demo/{name}"


def _suite(project, *files):
    return lb.Suite(str(project / "src" / "test" / "java"),
                    files or ("demo/CalcTest.java",))


def _tokens(evidence):
    return {(e.test.qualname,): set(e.tokens()) for e in evidence.tests}


def test_each_test_method_has_its_lines_and_branches_alike_for_both_builds(
        java_project, jdks, maven, gradle):
    tokens = {}
    for build in ("maven", "gradle"):
        project = java_project(build)
        more = _write(project, "CalcMoreTest.java", MORE)
        adapter = _adapter(project, jdks, maven, gradle)
        evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
            _suite(project, "demo/CalcTest.java", more), TARGET, timeout_s=300))
        assert evidence.outcome is lb.Outcome.COMPLETED, evidence.reason
        found = _tokens(evidence)
        for test, expected in EXPECTED.items():
            assert found[test] == expected, test
        assert found[("CalcMoreTest.positives",)] == EXPECTED[("CalcTest.positive",)]
        assert found[("CalcMoreTest.Clamping.keepsInside",)] == {f"L:{F}:18"}
        assert all(e.test.params is None for e in evidence.tests)
        assert evidence.untimed == () and evidence.unmatched_contexts == ()
        assert evidence.stopping_tests == ()
        assert evidence.suite_time_ms == pytest.approx(
            sum(e.time_ms for e in evidence.tests))
        assert evidence.kills == "not_requested"
        assert evidence.coverage.covered == (8, 9, 11, 12, 18)
        assert {f"B:{F}:{b.source}->{b.destination}"
                for b in evidence.coverage.taken} == {
            f"B:{F}:8->0", f"B:{F}:8->1", f"B:{F}:11->0"}
        tokens[build] = found
    assert tokens["maven"] == tokens["gradle"]


@pytest.mark.parametrize(("junit", "release"), [("junit4", 11), ("junit6", 17)])
def test_junit_4_and_6_suites_have_junit_5s_evidence(java_project, jdks, maven,
                                                     gradle, junit, release):
    project = java_project("maven", release=release, junit=junit)
    evidence = _adapter(project, jdks, maven, gradle).per_test_evidence(
        PerTestEvidenceRequest(_suite(project), TARGET, timeout_s=300))
    assert evidence.outcome is lb.Outcome.COMPLETED, evidence.reason
    assert _tokens(evidence) == EXPECTED


@pytest.mark.mutation_tool
def test_a_mutation_run_adds_each_tests_kills(java_project, jdks, maven, gradle):
    project = java_project("gradle")
    adapter = _adapter(project, jdks, maven, gradle)
    with adapter.open_mutation_session("pit") as session:
        run = session.run(MutationRequest(target=TARGET, suite=_suite(project),
                                          attribution="all"))
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
        _suite(project), TARGET, timeout_s=300, mutation_run=run))
    assert evidence.kills == "usable" and evidence.unmapped_killers == ()
    kills = {e.test.qualname: {t for t in e.tokens() if t.startswith("K:")}
             for e in evidence.tests}
    for mutant in run.mutants:
        killers = {t.qualname for t in mutant.killing_tests}
        for test, found in kills.items():
            assert (f"K:{mutant.engine_id}" in found) == (test in killers)
    assert sum(len(found) for found in kills.values()) == 6


def test_static_initialization_belongs_to_no_test(java_project, jdks, maven,
                                                  gradle):
    project = java_project("maven")
    _write(project, "Registry.java", REGISTRY, main=True)
    tests = _write(project, "RegistryTest.java", REGISTRY_TEST)
    evidence = _adapter(project, jdks, maven, gradle).per_test_evidence(
        PerTestEvidenceRequest(_suite(project, tests),
                               "src/main/java/demo/Registry.java",
                               timeout_s=300))
    assert evidence.outcome is lb.Outcome.COMPLETED, evidence.reason
    registry = "src/main/java/demo/Registry.java"
    found = {e.test.qualname: set(e.tokens()) for e in evidence.tests}
    assert found == {"RegistryTest.first": {f"L:{registry}:10"},
                     "RegistryTest.size": {f"L:{registry}:14"}}
    assert 4 in evidence.coverage.covered
    # The static initializer's line belongs to no test: it is reported.
    from langadapter.suites import LineItem
    assert LineItem(registry, 4) in evidence.outside_items
    assert not {LineItem(registry, 10), LineItem(registry, 14)} & evidence.outside_items


def test_a_red_suite_has_no_evidence(java_project, jdks, maven, gradle):
    project = java_project("maven")
    red = _write(project, "CalcRedTest.java", (
        "package demo;\n\nimport org.junit.jupiter.api.Test;\n\n"
        "class CalcRedTest {\n    @Test\n    void fails() {\n"
        "        throw new AssertionError(\"red\");\n    }\n}\n"))
    evidence = _adapter(project, jdks, maven, gradle).per_test_evidence(
        PerTestEvidenceRequest(_suite(project, red), TARGET, timeout_s=300))
    assert evidence.outcome is lb.Outcome.TEST_FAILURES
    assert evidence.tests == () and evidence.coverage is None
    red_run = _adapter(project, jdks, maven, gradle).run_suite(
        SuiteRunRequest(_suite(project, red), timeout_s=300))
    assert [t.qualname for t in evidence.stopping_tests] == ["CalcRedTest.fails"]
    assert evidence.stopping_tests == red_run.failing_tests()
    run = _adapter(project, jdks, maven, gradle).run_suite(
        SuiteRunRequest(_suite(project), timeout_s=300))
    assert run.outcome is lb.Outcome.COMPLETED


def test_a_test_that_fails_only_under_jacoco_is_named_by_coverage_and_evidence(
        java_project, jdks, maven, gradle):
    """JaCoCo's agent adds a synthetic method to the target class
    (``$jacocoInit``), so a test that counts them passes the suite run and
    fails both runs under the agent, each naming it."""
    project = java_project("maven")
    fields = _write(project, "CalcFieldsTest.java", (
        "package demo;\n\nimport org.junit.jupiter.api.Test;\n\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class CalcFieldsTest {\n    @Test\n    void hasNoSyntheticMethod() {\n"
        "        assertEquals(0, java.util.Arrays.stream(Calc.class.getDeclaredMethods())\n"
        "            .filter(java.lang.reflect.Method::isSynthetic).count());\n"
        "    }\n}\n"))
    adapter = _adapter(project, jdks, maven, gradle)
    plain = adapter.run_suite(SuiteRunRequest(_suite(project, fields), timeout_s=300))
    assert plain.outcome is lb.Outcome.COMPLETED, plain.reason
    covered = adapter.run_suite(SuiteRunRequest(_suite(project, fields), timeout_s=300,
                                                coverage_target=TARGET))
    assert covered.outcome is lb.Outcome.TEST_FAILURES, covered.reason
    assert [t.qualname for t in covered.failing_tests()] == ["CalcFieldsTest.hasNoSyntheticMethod"]
    evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
        _suite(project, fields), TARGET, timeout_s=300))
    assert evidence.outcome is lb.Outcome.TEST_FAILURES, evidence.reason
    assert evidence.stopping_tests == covered.failing_tests()


def test_times_have_the_clocks_resolution(java_project, jdks, maven, gradle):
    """A test's time is the listener's nanoseconds from start to end, finer
    than the launcher report's whole milliseconds."""
    project = java_project("maven")
    evidence = _adapter(project, jdks, maven, gradle).per_test_evidence(
        PerTestEvidenceRequest(_suite(project), TARGET, timeout_s=300))
    assert evidence.outcome is lb.Outcome.COMPLETED, evidence.reason
    times = [e.time_ms for e in evidence.tests]
    assert all(t is not None and t > 0 for t in times)
    assert any(abs(t - round(t)) > 1e-6 for t in times), times


def test_a_target_the_tests_cannot_load_first_has_no_evidence(java_project, jdks, maven,
                                                               gradle, monkeypatch):
    """The first test using unloaded target classes would include their
    loading, so the evidence fails with the cause."""
    from langadapter.java import _runs as _evidence

    monkeypatch.setattr(_evidence._jacoco, "class_names", lambda *args, **kwargs: ("demo.Missing",))
    project = java_project("maven")
    evidence = _adapter(project, jdks, maven, gradle).per_test_evidence(
        PerTestEvidenceRequest(_suite(project), TARGET, timeout_s=300))
    assert evidence.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert evidence.reason.startswith(
        "the target's classes could not be loaded before the tests: demo.Missing: "
        "java.lang.ClassNotFoundException"), evidence.reason


PARAMETERIZED = """\
package demo;

import static org.junit.Assert.assertEquals;

import java.util.Arrays;
import java.util.Collection;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.junit.runners.Parameterized;
import org.junit.runners.Parameterized.Parameters;

@RunWith(Parameterized.class)
public class CalcParamTest {
    @Parameters(name = "{index}: sign of {0} / 100%")
    public static Collection<Object[]> data() {
        return Arrays.asList(new Object[][] {{2, 1}, {-2, -1}});
    }

    private final int value;
    private final int expected;

    public CalcParamTest(int value, int expected) {
        this.value = value;
        this.expected = expected;
    }

    @Test
    public void signs() {
        assertEquals(expected, Calc.sign(value));
    }
}
"""


def test_a_junit_4_parameterized_test_is_one_test_with_its_cases(java_project, jdks, maven,
                                                                gradle):
    """A Vintage test's cases map to their test although JUnit Platform
    percent-encodes their names in the unique id
    ("signs%5B0%3A sign of 2 %2F 100%25%5D")."""
    project = java_project("maven", junit="junit4")
    tests = _write(project, "CalcParamTest.java", PARAMETERIZED)
    evidence = _adapter(project, jdks, maven, gradle).per_test_evidence(
        PerTestEvidenceRequest(_suite(project, tests), TARGET, timeout_s=300))
    assert evidence.outcome is lb.Outcome.COMPLETED, evidence.reason
    assert evidence.unmatched_contexts == () and evidence.untimed == ()
    found = {e.test.qualname: set(e.tokens()) for e in evidence.tests}
    assert set(found) == {"CalcParamTest.signs"}
    assert found["CalcParamTest.signs"] == (
        EXPECTED[("CalcTest.positive",)] | EXPECTED[("CalcTest.negative",)])
