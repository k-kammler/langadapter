"""Every Java run of a suite is the one test run: PIT measures the classes the
normal run runs, on its classpath in its order, with its parallel setting,
and a suite PIT refuses names the tests that stopped it."""
from __future__ import annotations

import os

import pytest

import langadapter as lb
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.runs import FAILED, SuiteRunRequest

pytestmark = pytest.mark.mutation_tool

NEGATE = "org.pitest.mutationtest.engine.gregor.mutators.NegateConditionalsMutator"


def _adapter(project, jdks, maven, gradle, feature=21):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))


def _write(project, name, text):
    path = project / "src" / "test" / "java" / "demo" / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return f"demo/{name}"


def _suite(project, *files):
    return lb.Suite(str(project / "src" / "test" / "java"), files)


def _mutate(adapter, suite):
    with adapter.open_mutation_session("pit", workers=1) as session:
        return session.run(MutationRequest(
            target="src/main/java/demo/Calc.java", suite=suite,
            catalogue=Catalogue(operators=(NEGATE,))))


RED = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcRedTest {
    @Test
    void positive() {
        assertEquals(1, Calc.sign(5));
    }

    @Test
    void firstWrong() {
        assertEquals(2, Calc.sign(5));
    }

    @Test
    void secondWrong() {
        assertEquals(-2, Calc.sign(-5));
    }
}
"""


def test_a_suite_pit_refuses_names_the_tests_that_stopped_it(java_project, jdks, maven, gradle):
    """PIT's "did not pass without mutation" lines name the tests, as the
    normal run's failed tests."""
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    suite = _suite(project, _write(project, "CalcRedTest.java", RED))
    normal = adapter.run_suite(SuiteRunRequest(suite, timeout_s=600))
    failed = sorted(str(outcome.test) for outcome in normal.tests if outcome.outcome == FAILED)
    assert len(failed) == 2, normal.output
    run = _mutate(adapter, suite)
    assert run.outcome is lb.Outcome.TEST_FAILURES, run.reason
    assert sorted(str(test) for test in run.stopping_tests) == failed


CLASSPATH = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.File;
import org.junit.jupiter.api.Test;

class CalcClasspathTest {
    @Test
    void positive() {
        assertEquals(1, Calc.sign(5));
    }

    @Test
    void findsTheLauncherBeforeTheProjectsJars() {
        String path = System.getProperty("java.class.path");
        int launcher = -1;
        int api = -1;
        String[] entries = path.split(File.pathSeparator);
        for (int i = 0; i < entries.length; i++) {
            String name = new File(entries[i]).getName();
            if (launcher < 0 && name.startsWith("junit-platform-launcher-")) {
                launcher = i;
            }
            if (api < 0 && name.startsWith("junit-jupiter-api-")) {
                api = i;
            }
        }
        assertTrue(launcher >= 0 && api >= 0 && launcher < api, path);
    }
}
"""


def test_pit_runs_the_tests_on_the_normal_runs_classpath(java_project, jdks, maven, gradle):
    """The normal run puts the JUnit tools before the project's classpath,
    PIT put them after it: a test reading the order passed the normal run
    and stopped PIT. PIT's own jars still come first (inherent)."""
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    suite = _suite(project, _write(project, "CalcClasspathTest.java", CLASSPATH))
    assert adapter.run_suite(SuiteRunRequest(suite, timeout_s=600)).outcome is lb.Outcome.COMPLETED
    run = _mutate(adapter, suite)
    assert run.outcome is lb.Outcome.COMPLETED, run.reason


NESTED_JUNIT4 = """\
package demo;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.fail;

import org.junit.Test;

public class CalcNestedTest {
    @Test
    public void positive() {
        assertEquals(1, Calc.sign(5));
    }

    public static class Helper {
        @Test
        public void notATestOfTheSuite() {
            fail("the normal run does not run a JUnit 4 nested class");
        }
    }
}
"""


def test_pit_runs_the_junit_4_classes_the_normal_run_runs(java_project, jdks, maven, gradle):
    """PIT targeted ``T$*`` too, so it ran a JUnit 4 nested class the normal
    run does not, whose failing test stopped it."""
    project = java_project("maven", junit="junit4")
    adapter = _adapter(project, jdks, maven, gradle)
    suite = _suite(project, _write(project, "CalcNestedTest.java", NESTED_JUNIT4))
    normal = adapter.run_suite(SuiteRunRequest(suite, timeout_s=600))
    assert normal.outcome is lb.Outcome.COMPLETED, normal.output
    run = _mutate(adapter, suite)
    assert run.outcome is lb.Outcome.COMPLETED, run.reason


ONE_AT_A_TIME = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

class CalcSequentialTest {
    static final AtomicInteger RUNNING = new AtomicInteger();

    @Test
    void positive() throws InterruptedException {
        alone();
        assertEquals(1, Calc.sign(5));
    }

    @Test
    void negative() throws InterruptedException {
        alone();
        assertEquals(-1, Calc.sign(-5));
    }

    static void alone() throws InterruptedException {
        try {
            assertEquals(1, RUNNING.incrementAndGet());
            Thread.sleep(500);
        } finally {
            RUNNING.decrementAndGet();
        }
    }
}
"""


def test_the_normal_run_runs_one_test_at_a_time_as_pit_does(java_project, jdks, maven, gradle):
    """PIT turns Jupiter's parallel execution off; the normal run followed
    the project's setting, so two tests that must not overlap failed there."""
    project = java_project("maven")
    resources = project / "src" / "test" / "resources"
    resources.mkdir(parents=True)
    (resources / "junit-platform.properties").write_text(
        "junit.jupiter.execution.parallel.enabled=true\n"
        "junit.jupiter.execution.parallel.mode.default=concurrent\n", encoding="utf-8")
    adapter = _adapter(project, jdks, maven, gradle)
    suite = _suite(project, _write(project, "CalcSequentialTest.java", ONE_AT_A_TIME))
    normal = adapter.run_suite(SuiteRunRequest(suite, timeout_s=600))
    assert normal.outcome is lb.Outcome.COMPLETED, normal.output
    run = _mutate(adapter, suite)
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
