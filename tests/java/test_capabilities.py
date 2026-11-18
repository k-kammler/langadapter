"""The Java adapter's capability report, language facts and failures: what
a resolved project offers, and each missing piece reported with its reason
instead of failing later."""
from __future__ import annotations

import os
import sys

import pytest

import langadapter as lb
from langadapter.capabilities import Availability
from langadapter.java import _tools
from langadapter.runs import SuiteRunRequest


def _java(jdks, feature):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    return jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")


def _adapter(project, java, **environment):
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment=environment))


def _states(report):
    return {c.name: c.availability for c in report.capabilities}


def test_a_resolved_project_reports_what_java_offers(java_project, jdks, maven,
                                                     gradle):
    project = java_project("maven")
    report = _adapter(project, _java(jdks, 21), LANGADAPTER_MAVEN=maven
                      ).capabilities()
    assert report.outcome is lb.Outcome.COMPLETED, report.reason
    assert report.language == "java" and report.runtime.startswith("Java 21")
    states = _states(report)
    for name in ("suite_runs", "coverage", "mutation", "per_test_evidence",
                 "per_test_kill_attribution", "source_merging"):
        assert states[name] is Availability.AVAILABLE, name
    assert report.branch_coverage == "counters"
    assert report.mutation_engines == ("pit",)
    tools = {t.name: t for t in report.tools}
    assert tools["jacoco"].found == "0.8.15" and tools["jacoco"].pinned in (None, "0.8.15")
    assert tools["pitest"].found == "1.30.0"
    assert tools["junit-platform-console"].found == "1.14.4"
    assert tools["tree-sitter-java"].found


@pytest.mark.parametrize(("junit", "release", "framework", "marker"), [
    ("junit4", 11, "JUnit 4", "@Test(timeout = {seconds}000)"),
    ("junit5", 8, "JUnit 5", "@Timeout({seconds})"),
    ("junit5", 11, "JUnit 5", "@Timeout({seconds})"),
    ("junit6", 17, "JUnit 6", "@Timeout({seconds})"),
])
def test_the_language_facts_name_the_projects_junit(
        java_project, jdks, maven, junit, release, framework, marker):
    project = java_project("maven", release=release, junit=junit)
    facts = _adapter(project, _java(jdks, 21), LANGADAPTER_MAVEN=maven
                     ).language_facts()
    assert (facts.language, facts.code_fence) == ("Java", "java")
    assert (facts.test_framework, facts.timeout_marker) == (framework, marker)
    assert facts.runtime_version.startswith("21")
    # The tests' language is the release they compile for, not the JDK's.
    assert facts.language_level == f"Java {release}"
    assert facts.unmatched_evidence_kept is False


def test_a_path_that_is_no_jdk_is_missing_setup(java_project):
    report = _adapter(java_project("maven"), sys.executable).capabilities()
    assert report.outcome is lb.Outcome.MISSING_SETUP
    assert "no JDK" in report.reason and report.runtime is None
    assert set(_states(report).values()) == {Availability.MISSING_SETUP}


def test_a_directory_without_a_build_is_unsupported(tmp_path, jdks):
    (tmp_path / "src").mkdir()
    report = _adapter(tmp_path, _java(jdks, 21)).capabilities()
    assert report.outcome is lb.Outcome.COMPLETED
    assert set(_states(report).values()) == {Availability.UNSUPPORTED}
    assert all("no Maven or Gradle build file" in c.reason
               for c in report.capabilities)


def test_a_build_without_its_launcher_is_missing_setup(java_project, jdks,
                                                       tmp_path):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    report = _adapter(java_project("maven"), _java(jdks, 21),
                      PATH=str(empty)).capabilities()
    assert report.outcome is lb.Outcome.MISSING_SETUP
    assert "no maven launcher" in report.reason
    assert set(_states(report).values()) == {Availability.MISSING_SETUP}


def test_tool_jars_that_cannot_be_obtained_are_missing_setup(
        java_project, jdks, maven, tmp_path, monkeypatch):
    monkeypatch.setattr(_tools, "MAVEN_CENTRAL", "http://127.0.0.1:9/maven2")
    report = _adapter(java_project("maven"), _java(jdks, 21),
                      LANGADAPTER_MAVEN=maven,
                      LANGADAPTER_JAVA_TOOLS=str(tmp_path / "cache")
                      ).capabilities()
    assert report.outcome is lb.Outcome.COMPLETED
    states = _states(report)
    for name in ("suite_runs", "coverage", "mutation"):
        assert states[name] is Availability.MISSING_SETUP, name
    assert "not in the tool cache" in report.capability("mutation").reason
    assert states["source_merging"] is Availability.AVAILABLE
    assert report.mutation_engines == ()


def test_a_build_tool_that_needs_a_newer_java_runs_on_the_build_jdk(
        java_project, jdks, gradle):
    project = java_project("gradle", release=11)
    old = _adapter(project, _java(jdks, 11), LANGADAPTER_GRADLE=gradle)
    report = old.capabilities()
    assert report.outcome is lb.Outcome.MISSING_SETUP
    assert "LANGADAPTER_BUILD_JAVA_HOME" in report.reason
    built = _adapter(project, _java(jdks, 11), LANGADAPTER_GRADLE=gradle,
                     LANGADAPTER_BUILD_JAVA_HOME=str(jdks[21] if 21 in jdks
                                                      else pytest.skip("JDK 21")))
    run = built.run_suite(SuiteRunRequest(
        suite=lb.Suite(str(project / "src" / "test" / "java"),
                       ("demo/CalcTest.java",)), timeout_s=300))
    assert run.outcome is lb.Outcome.COMPLETED, (run.reason, run.output)
    assert run.identity.toolchain.version.startswith("11")
    bad = _adapter(project, _java(jdks, 11), LANGADAPTER_GRADLE=gradle,
                   LANGADAPTER_BUILD_JAVA_HOME=str(project))
    assert "names no JDK" in bad.capabilities().reason


def test_a_refused_operation_gives_the_context_s_issue(tmp_path):
    """A context the Java adapter cannot run (a test framework override)
    refuses each operation with the issue the operation report names, not
    as not implemented."""
    (tmp_path / "CalcTest.java").write_text("class CalcTest {}\n", encoding="utf-8")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable),
                                test_framework=lb.TestFrameworkConfig("isolated"))
    adapter = lb.create("java", context)
    issue = "Java test_framework overrides are not implemented; use the project's configuration"
    assert {item.name: item.reason for item in adapter.operation_capabilities()}["tests.run_suite"] == issue
    run = adapter.tests.run_suite(SuiteRunRequest(lb.Suite(str(tmp_path), ("CalcTest.java",)), 60))
    assert (run.outcome, run.reason) == (lb.Outcome.UNSUPPORTED, issue)
    validated = adapter.sources.validate_source("class CalcTest {}")
    assert (validated.status, validated.reason) == (lb.SourceStatus.UNSUPPORTED, issue)
    salvaged = adapter.sources.salvage_syntax("class CalcTest {")
    assert (salvaged.status, salvaged.detail) == ("unsupported", issue)
    catalogue = adapter.mutations.operators("pit")
    assert (catalogue.outcome, catalogue.reason) == (lb.Outcome.UNSUPPORTED, issue)
    with adapter.mutations.open_session("pit") as session:
        assert (session.outcome, session.reason) == (lb.Outcome.UNSUPPORTED, issue)
