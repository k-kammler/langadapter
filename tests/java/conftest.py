"""Java fixture projects and the tools the Java tests use.

One small project as a Maven or a Gradle project, at a Java release, with
JUnit 4, 5 or 6 tests. Both forms have the same sources and dependencies, so
a result that differs between them is the adapter's difference.

The tools come from the environment, and the tests skip where one is missing:

``LANGADAPTER_TEST_JAVA_HOMES``
    JDK homes, separated by ``os.pathsep``.
``LANGADAPTER_TEST_MAVEN``
    The ``mvn`` launcher (``mvn.cmd`` on Windows).
``LANGADAPTER_TEST_GRADLE``
    The ``gradle`` launcher (``gradle.bat`` on Windows).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional

import pytest

MAIN = """\
package demo;

public final class Calc {
    private Calc() {
    }

    public static int sign(int x) {
        if (x > 0) {
            return 1;
        }
        if (x < 0) {
            return -1;
        }
        return 0;
    }

    public static int clamp(int x, int lo, int hi) {
        return Math.max(lo, Math.min(hi, x));
    }
}
"""

#: The tests leave ``sign(0)`` and the lower clamp untested, so a mutation
#: run has genuine survivors as well as kills.
TESTS = {
    "junit4": """\
package demo;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class CalcTest {
    @Test
    public void positive() {
        assertEquals(1, Calc.sign(5));
    }

    @Test
    public void negative() {
        assertEquals(-1, Calc.sign(-5));
    }

    @Test
    public void clampsHigh() {
        assertEquals(10, Calc.clamp(50, 0, 10));
    }
}
""",
    "junit5": """\
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

    @Test
    void clampsHigh() {
        assertEquals(10, Calc.clamp(50, 0, 10));
    }
}
""",
}
TESTS["junit6"] = TESTS["junit5"]
TESTS["testng"] = """\
package demo;

import static org.testng.Assert.assertEquals;

import org.testng.annotations.Test;

public class CalcTest {
    @Test
    public void positive() {
        assertEquals(Calc.sign(5), 1);
    }
}
"""

#: Each flavor's test dependency.
JUNIT = {
    "junit4": ("junit", "junit", "4.13.2"),
    "junit5": ("org.junit.jupiter", "junit-jupiter", "5.14.4"),
    "junit6": ("org.junit.jupiter", "junit-jupiter", "6.1.3"),
    "testng": ("org.testng", "testng", "7.10.2"),
}

POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>demo</groupId>
  <artifactId>calc</artifactId>
  <version>1.0</version>
  <properties>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    <maven.compiler.release>{release}</maven.compiler.release>
  </properties>
  <dependencies>
    <dependency>
      <groupId>{group}</groupId>
      <artifactId>{artifact}</artifactId>
      <version>{version}</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.6.0</version>
      </plugin>
    </plugins>
  </build>
</project>
"""

GRADLE_BUILD = """\
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
    options.release = {release}
    options.encoding = 'UTF-8'
}}

test {{
    {platform}
}}
"""


def write_project(root: Path, build: str, release: int = 11,
                  junit: str = "junit5") -> Path:
    """Write the fixture project under ``root`` as a ``maven`` or a
    ``gradle`` project and return ``root``."""
    main = root / "src" / "main" / "java" / "demo"
    test = root / "src" / "test" / "java" / "demo"
    main.mkdir(parents=True, exist_ok=True)
    test.mkdir(parents=True, exist_ok=True)
    (main / "Calc.java").write_text(MAIN, encoding="utf-8", newline="\n")
    (test / "CalcTest.java").write_text(TESTS[junit], encoding="utf-8",
                                        newline="\n")
    group, artifact, version = JUNIT[junit]
    if build == "maven":
        (root / "pom.xml").write_text(POM.format(
            release=release, group=group, artifact=artifact, version=version),
            encoding="utf-8", newline="\n")
    elif build == "gradle":
        if junit in ("junit4", "testng"):
            dependencies = f"    testImplementation '{group}:{artifact}:{version}'"
            platform = "useJUnit()" if junit == "junit4" else "useTestNG()"
        else:
            dependencies = (
                f"    testImplementation platform('org.junit:junit-bom:{version}')\n"
                "    testImplementation 'org.junit.jupiter:junit-jupiter'\n"
                "    testRuntimeOnly 'org.junit.platform:junit-platform-launcher'")
            platform = "useJUnitPlatform()"
        (root / "build.gradle").write_text(GRADLE_BUILD.format(
            dependencies=dependencies, release=release, platform=platform),
            encoding="utf-8", newline="\n")
        (root / "settings.gradle").write_text(
            "rootProject.name = 'calc'\n", encoding="utf-8", newline="\n")
    else:
        raise ValueError(f"unknown build tool {build!r}")
    return root


def java_homes() -> Dict[int, Path]:
    """The configured JDK homes by feature version."""
    homes: Dict[int, Path] = {}
    for entry in os.environ.get("LANGADAPTER_TEST_JAVA_HOMES", "").split(os.pathsep):
        if not entry.strip():
            continue
        home = Path(entry.strip())
        release = home / "release"
        if not release.is_file():
            continue
        match = re.search(r'JAVA_VERSION="(1\.)?(\d+)',
                          release.read_text(encoding="utf-8", errors="replace"))
        if match:
            homes[int(match.group(2))] = home
    return homes


def tool(variable: str) -> Optional[str]:
    value = os.environ.get(variable, "").strip()
    return value if value and Path(value).exists() else None


@pytest.fixture
def jdks() -> Dict[int, Path]:
    homes = java_homes()
    if not homes:
        pytest.skip("needs LANGADAPTER_TEST_JAVA_HOMES")
    return homes


@pytest.fixture
def maven() -> str:
    launcher = tool("LANGADAPTER_TEST_MAVEN")
    if launcher is None:
        pytest.skip("needs LANGADAPTER_TEST_MAVEN")
    return launcher


@pytest.fixture
def gradle() -> str:
    launcher = tool("LANGADAPTER_TEST_GRADLE")
    if launcher is None:
        pytest.skip("needs LANGADAPTER_TEST_GRADLE")
    return launcher


@pytest.fixture
def java_project(tmp_path):
    """``java_project(build, release=11, junit="junit5")`` writes the fixture
    project in a new directory and returns it."""
    def make(build: str, release: int = 11, junit: str = "junit5") -> Path:
        return write_project(tmp_path / f"calc-{build}-{release}-{junit}",
                             build, release, junit)
    return make


def build(project: Path, launcher: str, java_home: Path, *tasks: str,
          timeout: int = 900) -> subprocess.CompletedProcess:
    """Run the project's own build tool with ``java_home``."""
    env = dict(os.environ, JAVA_HOME=str(java_home))
    return subprocess.run([launcher, *tasks], cwd=project, env=env,
                          capture_output=True, text=True, timeout=timeout)
