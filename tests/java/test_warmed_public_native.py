"""The public warmed Java benchmark on the fixture project, built by Maven:
real build, helper compilation and fork JVMs. Runs where the Java test tools
are set (``LANGADAPTER_TEST_JAVA_HOMES``, ``LANGADAPTER_TEST_MAVEN``)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.runs import WarmedSuiteBenchmarkRequest


def test_the_fixture_suite_is_timed_in_warmed_forks(java_project, jdks, maven):
    if 21 not in jdks:
        pytest.skip("needs a JDK 21")
    project = java_project("maven")
    java = jdks[21] / "bin" / ("java.exe" if os.name == "nt" else "java")
    adapter = lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven}))
    events = []
    result = adapter.tests.benchmark_warmed(WarmedSuiteBenchmarkRequest(
        lb.Suite(str(project / "src" / "test" / "java"), ("demo/CalcTest.java",)),
        "native_lifecycle", iteration_timeout_s=120, total_timeout_s=900,
        forks=2, warmup_runs=1, runs=3, on_progress=events.append))
    assert result.outcome is lb.Outcome.COMPLETED and result.validity == "complete", (
        result.reason, result.diagnostics)
    assert (result.forks_completed, result.measurements_completed) == (2, 6)
    assert all(len(fork.samples_ns) == 3 for fork in result.forks)
    assert result.mean_of_fork_means_ms is not None and result.between_fork_mean_sd_ms is not None
    assert result.identity.build_tool == "maven" and result.disposal_confirmed
    executed = {test.unique_id for item in result.iterations for test in item.tests
                if test.kind == "test"}
    assert executed and all("CalcTest" in unique for unique in executed)
    assert [event.stage for event in events][-1] == "complete"
    assert not (Path(project) / ".langadapter-warmed").exists()


# Test classes whose warmed timing must fail in a stated way. Each runs alone.
_FAILING = {
    # Every iteration outlives the iteration timeout.
    "SlowTest": """package demo;
import org.junit.jupiter.api.Test;
class SlowTest {
    @Test void slow() throws InterruptedException { Thread.sleep(60_000); }
}
""",
    # The suite's own test fails from the first iteration on.
    "BrokenTest": """package demo;
import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;
class BrokenTest {
    @Test void broken() { assertEquals(1, 2); }
}
""",
    # Each iteration discovers one more dynamic test: the population changes.
    "GrowingTest": """package demo;
import java.util.stream.IntStream;
import java.util.stream.Stream;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
class GrowingTest {
    static int calls;
    @TestFactory Stream<DynamicTest> grows() {
        calls++;
        return IntStream.range(0, calls).mapToObj(i -> DynamicTest.dynamicTest("case " + i, () -> { }));
    }
}
""",
    # The fork's JVM stops in the middle of its second iteration.
    "HaltTest": """package demo;
import org.junit.jupiter.api.Test;
class HaltTest {
    static int calls;
    @Test void halts() { if (++calls == 2) { Runtime.getRuntime().halt(3); } }
}
""",
    # Fails once, then would pass: no retry may make it a passing sample.
    "FlakyTest": """package demo;
import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;
class FlakyTest {
    static int calls;
    @Test void flaky() { assertEquals(2, ++calls); }
}
""",
}

#: Per case: outcome, validity, a part of the reason, and the received
#: iteration records as (kind, outcome, number of tests).
_EXPECTED = {
    "SlowTest": (lb.Outcome.TIMED_OUT, "incomplete", "time limit", ()),
    "BrokenTest": (lb.Outcome.TEST_FAILURES, "incomplete", "a test or a test container failed",
                   (("warmup", lb.Outcome.TEST_FAILURES, 1),)),
    "GrowingTest": (lb.Outcome.COMPLETED, "non_repeatable", "changed within a fork",
                    (("warmup", lb.Outcome.COMPLETED, 1), ("measurement", lb.Outcome.COMPLETED, 2))),
    "HaltTest": (lb.Outcome.INFRASTRUCTURE_FAILURE, "incomplete", "exited without",
                 (("warmup", lb.Outcome.COMPLETED, 1),)),
    "FlakyTest": (lb.Outcome.TEST_FAILURES, "incomplete", "a test or a test container failed",
                  (("warmup", lb.Outcome.TEST_FAILURES, 1),)),
}


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_a_failing_warmed_timing_ends_without_a_summary_or_another_fork(
        java_project, jdks, maven, name):
    """On real fork JVMs, each failure in _FAILING ends the measurement in
    the first fork, keeps exactly the records received and gives no
    summary. Nothing is retried."""
    outcome, validity, reason, records = _EXPECTED[name]
    if 21 not in jdks:
        pytest.skip("needs a JDK 21")
    project = java_project("maven")
    (project / "src" / "test" / "java" / "demo" / f"{name}.java").write_text(
        _FAILING[name], encoding="utf-8", newline="\n")
    java = jdks[21] / "bin" / ("java.exe" if os.name == "nt" else "java")
    adapter = lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven}))
    result = adapter.tests.benchmark_warmed(WarmedSuiteBenchmarkRequest(
        lb.Suite(str(project / "src" / "test" / "java"), (f"demo/{name}.java",)),
        "native_lifecycle", iteration_timeout_s=5, total_timeout_s=600,
        forks=2, warmup_runs=1, runs=2))
    assert (result.outcome, result.validity) == (outcome, validity), (result.reason, result.diagnostics)
    assert reason in result.reason, result.reason
    assert result.forks_started == 1 and result.forks_completed == 0
    assert result.mean_of_fork_means_ms is None and result.between_fork_mean_sd_ms is None
    assert result.disposal_confirmed
    received = tuple((item.kind, item.outcome, sum(test.kind == "test" for test in item.tests))
                     for item in result.iterations)
    assert received == records
    assert all(item.fork == 0 for item in result.iterations)
    assert result.warmup_completed == sum(kind == "warmup" for kind, _, _ in records)
    assert result.measurements_completed == sum(kind == "measurement" for kind, _, _ in records)


#: A test that leaves a non-daemon thread running after it passes.
LEAKS_A_THREAD = """package demo;
import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;
class LeakTest {
    @Test void leaves_a_thread() {
        new Thread(() -> {
            try { Thread.sleep(Long.MAX_VALUE); } catch (InterruptedException ignored) { }
        }).start();
        assertEquals(1, Calc.sign(5));
    }
}
"""


def test_a_thread_a_test_leaves_running_does_not_hold_the_fork(java_project, jdks, maven):
    """A fork's JVM ends once it has written its records, so a non-daemon
    thread a test left running neither keeps it alive nor turns a finished
    timing into a timeout."""
    if 21 not in jdks:
        pytest.skip("needs a JDK 21")
    project = java_project("maven")
    (project / "src" / "test" / "java" / "demo" / "LeakTest.java").write_text(
        LEAKS_A_THREAD, encoding="utf-8", newline="\n")
    java = jdks[21] / "bin" / ("java.exe" if os.name == "nt" else "java")
    adapter = lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven}))
    result = adapter.tests.benchmark_warmed(WarmedSuiteBenchmarkRequest(
        lb.Suite(str(project / "src" / "test" / "java"), ("demo/LeakTest.java",)),
        "native_lifecycle", iteration_timeout_s=30, total_timeout_s=90,
        forks=2, warmup_runs=1, runs=2))
    assert result.outcome is lb.Outcome.COMPLETED and result.validity == "complete", (
        result.reason, result.diagnostics)
    assert (result.forks_completed, result.measurements_completed) == (2, 4)
    assert result.disposal_confirmed
