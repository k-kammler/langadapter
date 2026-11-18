"""Two build systems and TestNG: a project with both Maven and Gradle build
files is ambiguous in the setup check and refused by every run before a
command starts. TestNG without JUnit is unsupported, not a run that finds
no test."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.execution import LocalExecutor
from langadapter.java import _env as jdks_module
from langadapter.java._profile import JavaProfile
from langadapter.java._build import (
    BOTH_BUILDS, BOTH_BUILDS_ACTION, build_systems, build_tools, resolve,
)
from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest, SuiteRunRequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import write_project

REFUSED = f"{BOTH_BUILDS}: {BOTH_BUILDS_ACTION}"
TESTNG = ("TestNG is not supported: the test classpath has TestNG 7.10.2 and no JUnit, "
          "and Java operations run JUnit 4, 5 and 6 tests")


def _jdk(tmp_path):
    home = tmp_path / "jdk-21"
    (home / "bin").mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    for tool in ("java", "javac"):
        (home / "bin" / f"{tool}{suffix}").write_text("", encoding="utf-8")
    (home / "release").write_text('JAVA_VERSION="21.0.11"\n', encoding="utf-8")
    return str(home / "bin" / f"java{suffix}")


def _both(tmp_path):
    project = write_project(tmp_path / "calc", "maven", 11, "junit5")
    (project / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
    return project


def test_one_detection_names_the_build_systems():
    assert build_systems(["pom.xml"]) == ("maven",)
    assert build_systems(["settings.gradle.kts", "README.md"]) == ("gradle",)
    assert build_systems(["build.gradle", "pom.xml"]) == ("maven", "gradle")
    assert build_systems([]) == ()


def test_both_build_systems_are_ambiguous_in_the_setup_check(tmp_path):
    project = _both(tmp_path)
    assert build_tools(str(project), LocalExecutor()) == ("maven", "gradle")
    context = lb.ProjectContext(str(project), lb.Interpreter(path=_jdk(tmp_path)))
    report = lb.inspect_project("java", lb.ProjectSetupRequest(
        context, ("src/main/java/demo/Calc.java",), ("tests.run_suite",)))
    build = next(item for item in report.items if item.key == "build_descriptor")
    assert build.ambiguous and build.reason == BOTH_BUILDS and build.action == BOTH_BUILDS_ACTION


def test_every_run_refuses_two_build_systems_before_a_command_starts(tmp_path, monkeypatch):
    def started(*args, **kwargs):
        pytest.fail("a command started for a project with two build systems")

    project = _both(tmp_path)
    adapter = lb.create("java", lb.ProjectContext(str(project), lb.Interpreter(path=_jdk(tmp_path))))
    monkeypatch.setattr(subprocess, "Popen", started)
    suite = lb.Suite(str(project / "src" / "test" / "java"), ("demo/CalcTest.java",))
    results = [
        adapter.tests.run_suite(SuiteRunRequest(suite, 60)),
        adapter.tests.per_test_evidence(PerTestEvidenceRequest(suite, "src/main/java/demo/Calc.java", 60)),
        adapter.tests.benchmark(BenchmarkRequest(suite, 1, 60)),
    ]
    for result in results:
        assert result.outcome is lb.Outcome.UNSUPPORTED and REFUSED in result.reason, result


@pytest.mark.parametrize("classpath, refusal", [
    (("/lib/testng-7.10.2.jar", "/lib/jcommander-1.82.jar"), TESTNG),
    (("/lib/testng-7.10.2.jar", "/lib/junit-jupiter-api-5.14.4.jar"), ""),
    (("/lib/testng-7.10.2.jar", "/lib/junit-4.13.2.jar"), ""),
    (("/lib/testng-7.10.2.jar", "/lib/hamcrest-junit-2.0.0.0.jar"), TESTNG),
    (("/lib/junit-jupiter-api-5.14.4.jar",), ""),
])
def test_testng_without_junit_is_refused(classpath, refusal):
    assert JavaProfile.framework_refusal(classpath) == refusal


def _real_jdk(jdks):
    if 21 not in jdks:
        pytest.skip("needs a JDK 21 in LANGADAPTER_TEST_JAVA_HOMES")
    return jdks_module.read(str(jdks[21]))


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_a_testng_project_is_named_unsupported_after_its_build_resolves_it(
        java_project, jdks, maven, gradle, tmp_path, build):
    project = java_project(build, 11, "testng")
    resolution = resolve(str(project), _real_jdk(jdks), LocalExecutor(), str(tmp_path / "scratch"),
                         environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle})
    assert resolution.outcome is lb.Outcome.UNSUPPORTED and resolution.reason == TESTNG, (
        resolution.reason, resolution.output)


def test_a_testng_suite_run_is_unsupported(java_project, jdks, maven):
    java = os.path.join(_real_jdk(jdks).home, "bin", "java.exe" if os.name == "nt" else "java")
    project = java_project("maven", 11, "testng")
    adapter = lb.create("java", lb.ProjectContext(str(project), lb.Interpreter(path=java),
                                                  environment={"LANGADAPTER_MAVEN": maven}))
    run = adapter.tests.run_suite(SuiteRunRequest(
        lb.Suite(str(project / "src" / "test" / "java"), ("demo/CalcTest.java",)), 600))
    assert run.outcome is lb.Outcome.UNSUPPORTED and TESTNG in run.reason, (run.reason, run.output)
