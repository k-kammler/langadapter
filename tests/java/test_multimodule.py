"""One module of a multi-module build as the project, in Maven and Gradle
form, where ``core`` uses a class of ``util``. The module resolves with its
dependencies built and on its classpath, and its suite runs, coverage,
mutation and per-test evidence agree for both forms. The build's top, which
only aggregates modules, is refused with them named."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.mutation import MutationRequest
from langadapter.runs import PerTestEvidenceRequest, SuiteRunRequest

TARGET = "src/main/java/demo/Calc.java"

SIGNS = """\
package demo.util;

public final class Signs {
    private Signs() {
    }

    public static int of(int x) {
        if (x > 0) {
            return 1;
        }
        return x < 0 ? -1 : 0;
    }
}
"""

CALC = """\
package demo;

import demo.util.Signs;

public final class Calc {
    private Calc() {
    }

    public static int sign(int x) {
        return Signs.of(x);
    }

    public static int twice(int x) {
        if (x > 0) {
            return 2 * x;
        }
        return 0;
    }
}
"""

CALC_TEST = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTest {
    @Test
    void positive() {
        assertEquals(1, Calc.sign(5));
    }

    @Test
    void twicePositive() {
        assertEquals(6, Calc.twice(3));
    }
}
"""

PARENT_POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>demo</groupId>
  <artifactId>parent</artifactId>
  <version>1.0</version>
  <packaging>pom</packaging>
  <modules>
    <module>util</module>
    <module>core</module>
  </modules>
  <properties>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <maven.compiler.release>11</maven.compiler.release>
  </properties>
</project>
"""

MODULE_POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>demo</groupId>
    <artifactId>parent</artifactId>
    <version>1.0</version>
  </parent>
  <artifactId>{artifact}</artifactId>
  <dependencies>
{dependencies}
  </dependencies>
</project>
"""

CORE_DEPENDENCIES = """\
    <dependency>
      <groupId>demo</groupId>
      <artifactId>util</artifactId>
      <version>1.0</version>
    </dependency>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.14.4</version>
      <scope>test</scope>
    </dependency>"""

GRADLE_MODULE = """\
plugins {{
    id 'java'
}}

repositories {{
    mavenCentral()
}}

dependencies {{
{dependencies}
}}

tasks.withType(JavaCompile).configureEach {{
    options.release = 11
    options.encoding = 'UTF-8'
}}

test {{
    useJUnitPlatform()
}}
"""

GRADLE_CORE_DEPENDENCIES = """\
    implementation project(':util')
    testImplementation platform('org.junit:junit-bom:5.14.4')
    testImplementation 'org.junit.jupiter:junit-jupiter'
    testRuntimeOnly 'org.junit.platform:junit-platform-launcher'"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _build(root: Path, build: str) -> Path:
    _write(root / "util/src/main/java/demo/util/Signs.java", SIGNS)
    _write(root / "core/src/main/java/demo/Calc.java", CALC)
    _write(root / "core/src/test/java/demo/CalcTest.java", CALC_TEST)
    if build == "maven":
        _write(root / "pom.xml", PARENT_POM)
        _write(root / "util/pom.xml",
               MODULE_POM.format(artifact="util", dependencies=""))
        _write(root / "core/pom.xml",
               MODULE_POM.format(artifact="core", dependencies=CORE_DEPENDENCIES))
    else:
        _write(root / "settings.gradle",
               "rootProject.name = 'parent'\ninclude 'util', 'core'\n")
        _write(root / "util/build.gradle", GRADLE_MODULE.format(dependencies=""))
        _write(root / "core/build.gradle",
               GRADLE_MODULE.format(dependencies=GRADLE_CORE_DEPENDENCIES))
    return root


def _adapter(root, jdks, maven, gradle, feature=21):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))


def _suite(module):
    return lb.Suite(str(module / "src" / "test" / "java"), ("demo/CalcTest.java",))


@pytest.mark.mutation_tool
def test_a_module_measures_with_the_modules_it_uses_alike_for_both_builds(
        tmp_path, jdks, maven, gradle):
    found = {}
    for build in ("maven", "gradle"):
        root = _build(tmp_path / build, build)
        core = root / "core"
        adapter = _adapter(core, jdks, maven, gradle)
        resolution = adapter.project()
        assert resolution.outcome is lb.Outcome.COMPLETED, (
            resolution.reason, resolution.output)
        project = resolution.project
        assert project.root == str(core.resolve())
        assert all(Path(d).resolve().is_relative_to(core.resolve())
                   for d in project.main_classes)
        assert any(Path(e).resolve().is_relative_to((root / "util").resolve())
                   for e in project.test_classpath)
        run = adapter.run_suite(SuiteRunRequest(_suite(core), timeout_s=300,
                                                coverage_target=TARGET))
        assert run.outcome is lb.Outcome.COMPLETED, (run.reason, run.output)
        assert (run.counts.passed, run.counts.failed) == (2, 0)
        coverage = run.coverage
        assert set(coverage.covered) == {10, 14, 15}
        assert coverage.missing() == (17,)
        with adapter.open_mutation_session("pit", workers=1) as session:
            mutation = session.run(MutationRequest(
                target=TARGET, suite=_suite(core), attribution="all"))
        assert mutation.outcome is lb.Outcome.COMPLETED, mutation.reason
        assert any(m.status == "killed" for m in mutation.mutants)
        # twice(3) cannot tell ``x > 0`` from ``x >= 0``.
        assert {m.key.line for m in mutation.survivors()} == {14}
        evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
            _suite(core), TARGET, timeout_s=300))
        assert evidence.outcome is lb.Outcome.COMPLETED, evidence.reason
        found[build] = (
            coverage.to_coverage_matrix(), mutation.to_kill_matrix(),
            {e.test.qualname: set(e.tokens()) for e in evidence.tests})
    assert found["maven"] == found["gradle"]


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_the_top_of_a_build_that_only_aggregates_is_refused(
        tmp_path, jdks, maven, gradle, build):
    root = _build(tmp_path / build, build)
    resolution = _adapter(root, jdks, maven, gradle).project()
    assert resolution.outcome is lb.Outcome.UNSUPPORTED
    assert "core" in resolution.reason and "util" in resolution.reason


@pytest.mark.parametrize("namespace", [None, "http://maven.apache.org/POM/4.1.0"])
def test_poms_without_the_4_0_0_namespace_are_read_alike(tmp_path, namespace):
    """Maven also reads a POM without its namespace, and Maven 4 one of
    model 4.1.0 in that model's namespace. The top that only aggregates is
    refused with its modules named, a module finds its build's top, and a
    module's POM picks its project from Maven's effective POM, which Maven 4
    writes in 4.1.0's namespace."""
    from langadapter.java._build import (
        _effective_pom, _maven_command_line, _module_pom, maven_build_top)
    from langadapter.java._env import JavaEnv

    root = _build(tmp_path / "maven", "maven")
    declared = f' xmlns="{namespace}"' if namespace else ""
    for pom in root.rglob("pom.xml"):
        _write(pom, re.sub(r"<project[^>]*>", f"<project{declared}>",
                           pom.read_text(encoding="utf-8"), count=1))
    files = JavaEnv(None)
    refusal = _maven_command_line(str(root), tmp_path / "out", files, None)
    assert isinstance(refusal, str) and "core" in refusal and "util" in refusal
    assert maven_build_top(root / "core", files) == files.path(root)
    report = tmp_path / "maven-effective-pom.xml"
    written = namespace or "http://maven.apache.org/POM/4.0.0"
    _write(report, f'<?xml version="1.0" encoding="UTF-8"?><projects>'
                   f'<project xmlns="{written}"><artifactId>util</artifactId></project>'
                   f'<project xmlns="{written}"><artifactId>core</artifactId>'
                   f'<build><outputDirectory>out/classes</outputDirectory></build>'
                   f'</project></projects>')
    module = _module_pom(_effective_pom(report), root / "core" / "pom.xml", files)
    assert module.findtext("{http://maven.apache.org/POM/4.0.0}artifactId") == "core"
    assert module.findtext("{http://maven.apache.org/POM/4.0.0}build/"
                           "{http://maven.apache.org/POM/4.0.0}outputDirectory") == "out/classes"


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_a_module_uses_the_wrapper_of_its_build(tmp_path, build):
    from langadapter.java._build import launcher

    root = _build(tmp_path / build, build)
    name = {"maven": "mvnw", "gradle": "gradlew"}[build]
    wrapper = root / (name + {"maven": ".cmd", "gradle": ".bat"}[build]
                      if os.name == "nt" else name)
    wrapper.write_text("", encoding="utf-8")
    assert launcher(str(root / "core"), build, {}) == str(wrapper.resolve())
