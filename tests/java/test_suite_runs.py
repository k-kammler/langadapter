"""Java suite runs through the JUnit Platform's console launcher: the same
outcomes for Maven and Gradle, failures, errors, uncompilable and empty
suites, parameters and timeouts reported as such, and JUnit 4 and 6 through
their own engines."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.java.adapter import JavaAdapter
from langadapter.runs import SuiteRunRequest

EXTRA = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class CalcExtraTest {
    @Test
    void wrongSign() {
        assertEquals(1, Calc.sign(0), "zero has no sign");
    }

    @Test
    void throwsInstead() {
        throw new IllegalStateException("not a value");
    }

    @ParameterizedTest
    @ValueSource(ints = {3, 7})
    void positives(int value) {
        assertEquals(1, Calc.sign(value));
    }
}
"""

EMPTY = "package demo;\n\nclass CalcEmptyTest {\n}\n"

LOOPING = """\
package demo;

import org.junit.jupiter.api.Test;

class CalcLoopTest {
    @Test
    void loops() {
        while (true) {
            Thread.onSpinWait();
        }
    }
}
"""


def _adapter(project: Path, jdks, feature, maven, gradle) -> JavaAdapter:
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))


def _run(adapter, project, *files, timeout=300):
    root = project / "src" / "test" / "java"
    return adapter.run_suite(SuiteRunRequest(
        suite=lb.Suite(str(root), tuple(files)), timeout_s=timeout))


def _write(project: Path, name: str, text: str) -> str:
    path = project / "src" / "test" / "java" / "demo" / name
    path.write_text(text, encoding="utf-8")
    return f"demo/{name}"


def _summary(run):
    return (run.outcome, sorted((t.test.qualname, t.test.params, t.outcome)
                                for t in run.tests))


def test_the_fixture_suite_passes_alike_for_maven_and_gradle(
        java_project, jdks, maven, gradle):
    runs = {}
    for build in ("maven", "gradle"):
        project = java_project(build)
        runs[build] = _run(_adapter(project, jdks, 21, maven, gradle),
                           project, "demo/CalcTest.java")
    run = runs["maven"]
    assert run.outcome is lb.Outcome.COMPLETED, (run.reason, run.output)
    assert (run.counts.collected, run.counts.passed) == (3, 3)
    assert {t.test.qualname for t in run.tests} == {
        "CalcTest.positive", "CalcTest.negative", "CalcTest.clampsHigh"}
    assert all(t.test.file == "demo/CalcTest.java" for t in run.tests)
    assert run.unmatched == ()
    assert _summary(runs["maven"]) == _summary(runs["gradle"])
    assert dict(run.identity.toolchain.tools)["junit-platform"] == "1.14.4"


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_failures_errors_and_parameters_are_reported_per_test(
        java_project, jdks, maven, gradle, build):
    project = java_project(build)
    extra = _write(project, "CalcExtraTest.java", EXTRA)
    run = _run(_adapter(project, jdks, 21, maven, gradle), project,
               "demo/CalcTest.java", extra)
    assert run.outcome is lb.Outcome.TEST_FAILURES
    outcomes = {(t.test.qualname, t.test.params): t for t in run.tests}
    assert outcomes[("CalcExtraTest.wrongSign", None)].outcome == "failed"
    assert "zero has no sign" in outcomes[("CalcExtraTest.wrongSign", None)].message
    assert outcomes[("CalcExtraTest.throwsInstead", None)].outcome == "error"
    # A failure's trace is its report's stack, its exception the type.
    wrong = outcomes[("CalcExtraTest.wrongSign", None)]
    assert "CalcExtraTest.wrongSign" in wrong.trace and "\tat " in wrong.trace
    assert wrong.exception == "org.opentest4j.AssertionFailedError"
    assert outcomes[("CalcExtraTest.positives", "[1]")].trace == ""
    assert outcomes[("CalcExtraTest.positives", "[1]")].outcome == "passed"
    assert outcomes[("CalcExtraTest.positives", "[2]")].outcome == "passed"
    assert (run.counts.passed, run.counts.failed, run.counts.errors) == (5, 1, 1)
    assert [str(t) for t in run.failing_tests()] or run.failing_tests()


ASSERTING = """\
package demo;

import org.junit.jupiter.api.Test;

class CalcAssertTest {
    @Test
    void assertionsAreOn() {
        assert Calc.sign(-1) > 0 : "the build runs its tests with assertions enabled";
    }
}
"""


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_the_tests_run_with_assertions_as_the_build_runs_them(
        java_project, jdks, maven, gradle, build):
    """Surefire and Gradle's test task enable assertions by default, so a
    Java assert statement in a test or in the code under test fails it."""
    project = java_project(build)
    run = _run(_adapter(project, jdks, 21, maven, gradle), project,
               _write(project, "CalcAssertTest.java", ASSERTING))
    assert run.outcome is lb.Outcome.TEST_FAILURES, (run.reason, run.output)
    assert "assertions enabled" in run.tests[0].message


def test_the_test_jvm_takes_the_builds_assertion_setting():
    import xml.etree.ElementTree as ET

    from langadapter.java import _build

    ns = "http://maven.apache.org/POM/4.0.0"

    def surefire(configuration):
        return ET.fromstring(f'<plugin xmlns="{ns}"><configuration>{configuration}'
                             "</configuration></plugin>")

    assert _build._maven_jvm_args(None, {}) == ("-ea",)
    assert _build._maven_jvm_args(surefire("<argLine>-Xmx1g @{argLine}</argLine>"), {}) == (
        "-ea", "-Xmx1g")
    assert _build._maven_jvm_args(surefire("<enableAssertions>false</enableAssertions>"
                                           "<argLine>-Xmx1g</argLine>"), {}) == ("-Xmx1g",)
    assert _build._maven_jvm_args(None, {"enableAssertions": "false"}) == ()
    assert _build._gradle_jvm_args({}) == ("-ea",)
    assert _build._gradle_jvm_args({"enableAssertions": True, "jvmArgs": ["-Xmx1g"]}) == (
        "-ea", "-Xmx1g")
    assert _build._gradle_jvm_args({"enableAssertions": False, "jvmArgs": ["-Xmx1g"]}) == (
        "-Xmx1g",)
    # The build's own global assertion flags stand as given. A flag for a
    # package or a class is no global switch: it follows the global -ea.
    assert _build._gradle_jvm_args({"jvmArgs": ["-ea"]}) == ("-ea",)
    assert _build._gradle_jvm_args({"jvmArgs": ["-disableassertions"]}) == (
        "-disableassertions",)
    assert _build._maven_jvm_args(surefire("<argLine>-da:demo.Calc</argLine>"), {}) == (
        "-ea", "-da:demo.Calc")
    assert _build._gradle_jvm_args({"jvmArgs": ["-ea:demo..."]}) == ("-ea", "-ea:demo...")


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_a_suite_that_does_not_compile_is_a_collection_failure(
        java_project, jdks, maven, gradle, build):
    project = java_project(build)
    broken = _write(project, "CalcBrokenTest.java",
                    EMPTY.replace("CalcEmptyTest {", "CalcBrokenTest { int x = ;"))
    run = _run(_adapter(project, jdks, 21, maven, gradle), project, broken)
    assert run.outcome is lb.Outcome.COLLECTION_FAILED
    assert "CalcBrokenTest.java" in run.output


def test_a_suite_without_tests_has_no_tests(java_project, jdks, maven, gradle):
    project = java_project("maven")
    empty = _write(project, "CalcEmptyTest.java", EMPTY)
    run = _run(_adapter(project, jdks, 21, maven, gradle), project, empty)
    assert run.outcome is lb.Outcome.NO_TESTS, (run.reason, run.output)
    assert run.counts.collected == 0


@pytest.mark.parametrize("junit", ["junit4", "junit5", "junit6"])
def test_collection_discovers_without_running_test_bodies(
        java_project, jdks, maven, gradle, junit):
    from langadapter.runs import CollectionRequest

    project = java_project("maven", release=17, junit=junit)
    path = project / "src/test/java/demo/CalcTest.java"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("assertEquals(1, Calc.sign(5));",
                                  'throw new AssertionError("test body executed");'),
                    encoding="utf-8")
    adapter = _adapter(project, jdks, 21, maven, gradle)
    suite = lb.Suite(str(project / "src/test/java"), ("demo/CalcTest.java",))
    collected = adapter.collect_suite(CollectionRequest(suite, 120))
    assert collected.outcome is lb.Outcome.COMPLETED, (collected.reason, collected.output)
    assert adapter.run_suite(SuiteRunRequest(suite, 120)).outcome is lb.Outcome.TEST_FAILURES


def test_collection_distinguishes_empty_and_broken_sources(java_project, jdks, maven, gradle):
    from langadapter.runs import CollectionRequest

    project = java_project("maven")
    path = _write(project, "CalcEmptyTest.java", EMPTY)
    adapter = _adapter(project, jdks, 21, maven, gradle)
    suite = lb.Suite(str(project / "src/test/java"), (path,))
    assert adapter.collect_suite(CollectionRequest(suite, 120)).outcome is lb.Outcome.NO_TESTS
    _write(project, "CalcEmptyTest.java", EMPTY.replace("{", "{ int x = ;"))
    assert adapter.collect_suite(CollectionRequest(suite, 120)).outcome is lb.Outcome.COLLECTION_FAILED


def test_journal_names_the_test_running_when_the_jvm_exits(
        java_project, jdks, maven, gradle):
    project = java_project("maven")
    path = _write(project, "CalcCrashTest.java", '''package demo;
import org.junit.jupiter.api.Test;
class CalcCrashTest {
    @Test void crashes() { System.exit(0); }
}
''')
    suite = lb.Suite(str(project / "src/test/java"), (path,))
    adapter = _adapter(project, jdks, 21, maven, gradle)
    run = adapter.run_suite(SuiteRunRequest(suite, 120, timeout_journal=True))
    assert run.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE, run
    assert run.counts.collected is None
    assert run.active_at_end is not None
    assert run.active_at_end.qualname == "CalcCrashTest.crashes"
    assert run.side_effects.timeout_journal


@pytest.mark.mutation_tool
@pytest.mark.parametrize("operation", ["run", "benchmark", "evidence", "mutation", "delivered"])
def test_explicit_helpers_resources_and_jvm_flags_reach_every_operation(
        java_project, jdks, maven, gradle, tmp_path, operation):
    from dataclasses import replace

    from langadapter.mutation import MutationRequest
    from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest

    project = java_project("maven")
    context = _adapter(project, jdks, 21, maven, gradle).context
    adapter = JavaAdapter(replace(context, interpreter_flags=("-Dfixture.value=yes",)))
    root = tmp_path / "external-suite"
    package = root / "demo"
    package.mkdir(parents=True)
    (package / "Helper.java").write_text(
        "package demo; class Helper { static int value() { return -5; } }",
        encoding="utf-8")
    (package / "input.txt").write_text("yes", encoding="utf-8")
    (package / "PortableTest.java").write_text('''package demo;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
class PortableTest {
    @Test void portable() throws Exception {
        assertEquals("yes", System.getProperty("fixture.value"));
        try (var input = getClass().getResourceAsStream("/demo/input.txt")) {
            assertNotNull(input);
            assertEquals("yes", new String(input.readAllBytes(), "UTF-8"));
        }
        assertEquals(-1, Calc.sign(Helper.value()));
    }
}
''', encoding="utf-8")
    suite = lb.Suite(str(root), ("demo/PortableTest.java",),
                     ("demo/Helper.java", "demo/input.txt"))
    target = "src/main/java/demo/Calc.java"
    if operation == "run":
        result = adapter.run_suite(SuiteRunRequest(suite, 120, coverage_target=target))
    elif operation == "benchmark":
        result = adapter.benchmark(BenchmarkRequest(suite, 1, 120, warmup_runs=0))
    elif operation == "evidence":
        result = adapter.per_test_evidence(PerTestEvidenceRequest(suite, target, 120))
    elif operation == "delivered":
        result = adapter.run_as_delivered(SuiteRunRequest(suite, 120))
    else:
        with adapter.open_mutation_session("pit", workers=1) as session:
            result = session.run(MutationRequest(target=target, suite=suite))
    assert result.outcome is lb.Outcome.COMPLETED, (result.reason, result)
    identity = (result.identity.observation if operation == "mutation"
                else result.identity)
    assert identity.suite == tuple(sorted(suite.identity(), key=lambda file: file.path))
    assert "junit-platform" in dict(identity.toolchain.tools)
    if operation in {"run", "evidence"}:
        assert result.coverage is not None
        assert "jacoco" in dict(identity.toolchain.tools)


def test_the_timeout_ends_a_looping_suite_and_its_jvm(
        java_project, jdks, maven, gradle):
    project = java_project("maven")
    looping = _write(project, "CalcLoopTest.java", LOOPING)
    adapter = _adapter(project, jdks, 21, maven, gradle)
    adapter.project()       # resolve outside the timed run
    started = time.monotonic()
    run = _run(adapter, project, looping, timeout=20)
    assert run.outcome is lb.Outcome.TIMED_OUT
    assert time.monotonic() - started < 120
    if os.name != "nt":
        assert _processes_naming(b"CalcLoopTest") == []


def _processes_naming(text):
    """The processes whose command line holds ``text``, skipping any that
    end while the listing is read."""
    alive = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if text in command:
            alive.append(entry)
    return alive


@pytest.mark.parametrize(("junit", "release", "feature", "platform"), [
    ("junit4", 8, 17, "1.14.4"), ("junit6", 17, 25, "6.1.3")])
@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_junit_4_and_6_projects_run_through_their_own_engines(
        java_project, jdks, maven, gradle, build, junit, release, feature,
        platform):
    project = java_project(build, release, junit)
    run = _run(_adapter(project, jdks, feature, maven, gradle), project,
               "demo/CalcTest.java")
    assert run.outcome is lb.Outcome.COMPLETED, (run.reason, run.output)
    assert run.counts.passed == 3
    assert dict(run.identity.toolchain.tools)["junit-platform"] == platform


@pytest.mark.mutation_tool
@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_a_junit_4_older_than_4_12_runs_as_4_13_2(java_project, jdks, maven,
                                                  gradle, build):
    """A JUnit 4.11 project runs with 4.13.2 ahead of its own jar, as the
    Vintage engine needs 4.12 or newer. The toolchain records it, and PIT
    mutates alike."""
    from langadapter.mutation import MutationRequest

    project = java_project(build, junit="junit4")
    build_file = project / ("pom.xml" if build == "maven" else "build.gradle")
    text = build_file.read_text(encoding="utf-8")
    build_file.write_text(text.replace("4.13.2", "4.11"), encoding="utf-8")
    adapter = _adapter(project, jdks, 21, maven, gradle)
    run = _run(adapter, project, "demo/CalcTest.java")
    assert run.outcome is lb.Outcome.COMPLETED, (run.reason, run.output)
    assert run.counts.passed == 3
    assert dict(run.identity.toolchain.tools)["junit4"] == "4.13.2 in place of 4.11"
    with adapter.open_mutation_session("pit", workers=1) as session:
        mutation = session.run(MutationRequest(
            target="src/main/java/demo/Calc.java",
            suite=lb.Suite(str(project / "src" / "test" / "java"),
                           ("demo/CalcTest.java",))))
    assert mutation.outcome is lb.Outcome.COMPLETED, mutation.reason
    assert sum(m.status == "killed" for m in mutation.mutants) == 5


TEXT_BLOCK = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTextBlockTest {
    @Test
    void textBlock() {
        String expected = \"\"\"
            1\"\"\";
        assertEquals(expected, String.valueOf(Calc.sign(5)));
    }
}
"""


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_tests_compile_for_the_release_the_build_gives_them(
        java_project, jdks, maven, gradle, build):
    """The main code compiles for 11, the tests for 17 (a text block)."""
    project = java_project(build)
    if build == "maven":
        pom = project / "pom.xml"
        pom.write_text(pom.read_text(encoding="utf-8").replace(
            "</maven.compiler.release>",
            "</maven.compiler.release>\n"
            "    <maven.compiler.testRelease>17</maven.compiler.testRelease>"),
            encoding="utf-8")
    else:
        with (project / "build.gradle").open("a", encoding="utf-8") as fh:
            fh.write("\ntasks.named('compileTestJava') {\n"
                     "    options.release = 17\n}\n")
    adapter = _adapter(project, jdks, 21, maven, gradle)
    resolution = adapter.project()
    assert resolution.outcome is lb.Outcome.COMPLETED, resolution.reason
    assert (resolution.project.release,
            resolution.project.release_of_tests()) == (11, 17)
    run = _run(adapter, project, _write(project, "CalcTextBlockTest.java",
                                        TEXT_BLOCK))
    assert run.outcome is lb.Outcome.COMPLETED, (run.reason, run.output)


def test_maven_reports_are_read_as_maven_writes_them(tmp_path):
    """The tests' release from the compiler's test settings, argLine
    placeholders Surefire resolves late left out, and an effective POM with
    a second XML declaration read."""
    import xml.etree.ElementTree as ET

    from langadapter.java import _build

    ns = "http://maven.apache.org/POM/4.0.0"
    compiler = ET.fromstring(
        f'<plugin xmlns="{ns}"><configuration><release>11</release>'
        f"<testRelease>17</testRelease></configuration></plugin>")
    assert _build._maven_release(compiler, {}) == 11
    assert _build._maven_release(compiler, {}, prefix="test") == 17
    assert _build._maven_release(None, {"maven.compiler.release": "11"},
                                 prefix="test") == 11
    assert [t for t in ("@{argLine}", "-Xmx1g", "${jacoco.argLine}")
            if not _build._PLACEHOLDER.fullmatch(t)] == ["-Xmx1g"]
    report = tmp_path / "effective.xml"
    report.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!-- header -->\n'
        f'<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="{ns}"/>\n',
        encoding="utf-8")
    assert _build._effective_pom(report).tag == f"{{{ns}}}project"


def test_a_failing_test_reported_under_another_class_fails_the_run(tmp_path):
    """A test class can run tests another class declares (a generated test
    suite): the launcher reports them under that class, no file of the
    suite. They are listed unmatched, and one that fails fails the run."""
    from langadapter.outcomes import ExecutionIdentity, ObservationIdentity
    from langadapter.java import _tools as _junit
    from langadapter.java.adapter import _suite_run

    source = tmp_path / "demo" / "CalcTest.java"
    source.parent.mkdir()
    source.write_text("package demo;\nclass CalcTest {}\n", encoding="utf-8")
    launch = _junit.Launch(returncode=1, output="", reports=(), timed_out=False)
    cases = [_junit.Case("demo.CalcTest", "positive()", "passed", 0.1, ""),
             _junit.Case("other.MapTester", "testPut[size: one]", "failed",
                         0.1, "expected 1")]
    run = _suite_run(launch, cases, {"demo.CalcTest": source}, str(tmp_path),
                     0.2, ObservationIdentity(
                         target=None, suite=(), toolchain=None, instrument=None,
                         execution=ExecutionIdentity("local")))
    assert run.outcome is lb.Outcome.TEST_FAILURES
    assert "other.MapTester#testPut[size: one]" in run.reason
    assert run.unmatched == ("other.MapTester#testPut[size: one]",)
    assert (run.counts.passed, run.counts.failed) == (1, 0)
