"""JDK, Maven and Gradle resolution into one project model, and test
compilation against it. A project's Maven and Gradle forms resolve to the
same model, up to paths and the project's own launcher jar, which a run
replaces."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from langadapter.execution import LocalExecutor
from langadapter.java import _env as jdks_module
from langadapter.java._env import Jdk, feature_of
from langadapter.java._build import (
    BUILD_JDK, Resolution, compile_tests, resolve, start_failure,
)
from langadapter.outcomes import Outcome

GENERATED = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcGeneratedTest {
    @Test
    void clampsLow() {
        assertEquals(0, Calc.clamp(-5, 0, 10));
    }
}
"""


@pytest.mark.parametrize(("text", "feature"), [
    ("1.8.0_412", 8), ("11.0.32.1", 11), ("17", 17), ("21.0.11", 21), ("25.0.4.1", 25)])
def test_the_feature_version_of_a_version_text(text, feature):
    assert feature_of(text) == feature


def test_a_jdk_is_read_from_its_home_or_its_java(tmp_path):
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    suffix = ".exe" if jdks_module.os.name == "nt" else ""
    (home / "bin" / f"javac{suffix}").write_text("")
    (home / "bin" / f"java{suffix}").write_text("")
    (home / "release").write_text('IMPLEMENTOR="Eclipse"\nJAVA_VERSION="17.0.20.1"\n')
    found = jdks_module.read(str(home))
    assert found == Jdk(str(home), 17, "17.0.20.1")
    assert jdks_module.read(str(home / "bin" / f"java{suffix}")) == found
    assert jdks_module.read(str(tmp_path)) is None


def test_a_directory_without_a_build_file_is_not_supported(tmp_path):
    (tmp_path / "Calc.java").write_text("class Calc {}\n")
    resolution = resolve(str(tmp_path), Jdk("/none", 21, "21"), LocalExecutor(),
                         str(tmp_path / "scratch"))
    assert resolution.outcome is Outcome.UNSUPPORTED
    assert "no Maven or Gradle build file" in resolution.reason


@pytest.mark.parametrize("environment", [{}, {BUILD_JDK: ""}])
def test_explicit_resolution_does_not_reread_an_ambient_build_jdk(
        tmp_path, monkeypatch, environment):
    from langadapter.java import _build as project

    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    monkeypatch.setenv(BUILD_JDK, str(tmp_path / "ambient-missing-jdk"))
    monkeypatch.setattr(project, "launcher", lambda *args: "selected-maven")
    seen = {}

    def resolved(tool, root, command, env, executor, scratch, timeout, **options):
        seen.update(env)
        return Resolution(Outcome.COMPLETED)

    monkeypatch.setattr(project, "resolve_with", resolved)
    selected = Jdk(str(tmp_path / "selected-jdk"), 21, "21.0.7")
    result = resolve(str(tmp_path), selected, LocalExecutor(),
                     str(tmp_path / "scratch"), environment=environment)
    assert result.outcome is Outcome.COMPLETED
    assert seen["JAVA_HOME"] == selected.home


def _environment(maven, gradle):
    return {"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}


def _jdk(homes, feature):
    if feature not in homes:
        pytest.skip(f"needs a JDK {feature} in LANGADAPTER_TEST_JAVA_HOMES")
    return jdks_module.read(str(homes[feature]))


#: Annotation-only jars in JUnit's Maven test scope that its Gradle metadata
#: keeps off the runtime classpath. No test runs their code.
COMPILE_ONLY = ("apiguardian-api-", "jspecify-")


def _shape(project):
    """The model without paths and compile-only jars: what must not depend
    on the build tool."""
    root = Path(project.root)

    def relative(path):
        return Path(path).resolve().relative_to(root).as_posix()

    return {
        "release": project.release,
        "main_sources": [relative(p) for p in project.main_sources],
        "test_sources": [relative(p) for p in project.test_sources],
        "jars": sorted(Path(entry).name for entry in project.run_classpath()
                       if entry.endswith(".jar")
                       and not Path(entry).name.startswith(COMPILE_ONLY)),
        "encoding": project.encoding.upper(),
        "jvm_args": list(project.test_jvm_args),
        "system_properties": dict(project.test_system_properties),
    }


@pytest.mark.parametrize(("release", "junit", "feature"), [
    (11, "junit5", 21), (8, "junit4", 17), (17, "junit6", 25)])
def test_maven_and_gradle_resolve_the_same_project_alike(
        java_project, jdks, maven, gradle, tmp_path, release, junit, feature):
    jdk = _jdk(jdks, feature)
    models = {}
    for build in ("maven", "gradle"):
        resolution = resolve(str(java_project(build, release, junit)), jdk,
                             LocalExecutor(), str(tmp_path / f"scratch-{build}"),
                             environment=_environment(maven, gradle))
        assert resolution.outcome is Outcome.COMPLETED, (
            resolution.reason, resolution.output)
        project = resolution.project
        assert project.build_tool == build
        assert any((Path(d) / "demo" / "Calc.class").is_file()
                   for d in project.main_classes)
        models[build] = project
    assert _shape(models["maven"]) == _shape(models["gradle"])
    assert models["maven"].release == release
    jars = {build: {Path(e).name for e in m.run_classpath() if e.endswith(".jar")}
            for build, m in models.items()}
    assert jars["gradle"] <= jars["maven"]
    assert all(name.startswith(COMPILE_ONLY)
               for name in jars["maven"] - jars["gradle"])


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_generated_tests_compile_against_the_resolved_project(
        java_project, jdks, maven, gradle, tmp_path, build):
    jdk = _jdk(jdks, 21)
    project = resolve(str(java_project(build)), jdk, LocalExecutor(),
                      str(tmp_path / "scratch"),
                      environment=_environment(maven, gradle)).project
    source = tmp_path / "generated" / "demo" / "CalcGeneratedTest.java"
    source.parent.mkdir(parents=True)
    source.write_text(GENERATED, encoding="utf-8")
    compiled = compile_tests(project, jdk, [str(source)],
                             str(tmp_path / "generated-classes"), LocalExecutor())
    assert compiled.outcome is Outcome.COMPLETED, compiled.diagnostics
    assert (Path(compiled.classes) / "demo" / "CalcGeneratedTest.class").is_file()

    broken = source.with_name("CalcBrokenTest.java")
    broken.write_text(GENERATED.replace("CalcGeneratedTest", "CalcBrokenTest")
                      .replace("Calc.clamp(-5, 0, 10)", "Calc.missing()"),
                      encoding="utf-8")
    failed = compile_tests(project, jdk, [str(broken)],
                           str(tmp_path / "broken-classes"), LocalExecutor())
    assert failed.outcome is Outcome.COLLECTION_FAILED
    assert "missing" in failed.diagnostics


def test_a_launcher_that_cannot_start_is_named_with_its_cause(tmp_path):
    """A wrapper with Windows line ends exists but POSIX cannot start it:
    the reason names that cause, not a missing file."""
    crlf = tmp_path / "mvnw"
    crlf.write_bytes(b'#!/bin/sh\r\nexec mvn "$@"\r\n')
    reason = start_failure("Maven", str(crlf), FileNotFoundError(
        2, "No such file or directory", str(crlf)))
    assert reason.startswith("Maven did not run: FileNotFoundError")
    assert "mvnw has Windows line ends" in reason
    plain = tmp_path / "gradlew"
    plain.write_bytes(b'#!/bin/sh\nexec gradle "$@"\n')
    reason = start_failure("Gradle", str(plain), PermissionError(
        13, "Permission denied", str(plain)))
    if os.name != "nt":
        assert reason.endswith("; gradlew is not executable")
    missing = start_failure("Maven", str(tmp_path / "mvn"), FileNotFoundError(
        2, "No such file or directory"))
    assert missing == ("Maven did not run: FileNotFoundError: [Errno 2] No "
                       "such file or directory")


def test_a_posix_wrapper_runs_through_the_shell():
    """A POSIX wrapper runs through sh, so a copy on a mount without
    execution (a cluster's /dev/shm, many /tmp directories) still builds.
    Windows wrappers and other launchers start directly."""
    from langadapter.java._build import launch_argv

    for wrapper in ("/copy/proj/mvnw", "/copy/proj/gradlew"):
        argv = launch_argv(wrapper)
        assert argv[:2] == ["sh", "-c"] and argv[3:] == [wrapper]
    assert launch_argv(r"C:\copy\proj\mvnw.cmd") == [r"C:\copy\proj\mvnw.cmd"]
    assert launch_argv(r"C:\copy\proj\gradlew.bat") == [r"C:\copy\proj\gradlew.bat"]
    assert launch_argv("/usr/bin/mvn") == ["/usr/bin/mvn"]
    assert launch_argv("/opt/gradle/bin/gradle") == ["/opt/gradle/bin/gradle"]


EFFECTIVE_POM = '''<project xmlns="http://maven.apache.org/POM/4.0.0">
<artifactId>example</artifactId><properties><maven.compiler.release>11</maven.compiler.release>
</properties><build><sourceDirectory>SRC/main/java</sourceDirectory>
<testSourceDirectory>SRC/test/java</testSourceDirectory>
<outputDirectory>ROOT/target/classes</outputDirectory>
<testOutputDirectory>ROOT/target/test-classes</testOutputDirectory></build></project>'''


@pytest.mark.skipif(os.name == "nt", reason="a POSIX wrapper and file modes")
def test_a_wrapper_that_cannot_be_executed_still_resolves_the_project(tmp_path):
    """A wrapper without its executable bit, as in a copy on a noexec
    mount, runs through sh and the resolution completes."""
    from langadapter.java._build import resolve_with

    root = tmp_path / "proj"
    (root / "src" / "main" / "java").mkdir(parents=True)
    (root / "src" / "test" / "java").mkdir(parents=True)
    (root / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<artifactId>example</artifactId></project>", encoding="utf-8")
    pom = EFFECTIVE_POM.replace("SRC", str(root / "src")).replace("ROOT", str(root))
    wrapper = root / "mvnw"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        f"    -Doutput=*) printf '%s' '{pom}' > \"${{arg#-Doutput=}}\" ;;\n"
        '    -Dmdep.outputFile=*) : > "${arg#-Dmdep.outputFile=}" ;;\n'
        "  esac\n"
        "done\n", encoding="utf-8", newline="\n")
    os.chmod(wrapper, 0o644)
    result = resolve_with("maven", str(root), str(wrapper), dict(os.environ), LocalExecutor(),
                          str(tmp_path / "reports"), 120)
    assert result.outcome is Outcome.COMPLETED, result.reason + "\n" + result.output


def _launch(wrapper: Path, *args: str) -> str:
    import subprocess

    from langadapter.java._build import launch_argv

    done = subprocess.run([*launch_argv(str(wrapper)), *args], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.skipif(os.name == "nt", reason="a POSIX wrapper and file modes")
def test_a_wrapper_runs_with_the_interpreter_its_first_line_names(tmp_path):
    """A wrapper runs with the interpreter and argument its #! line names,
    a trailing carriage return dropped, so a Gradle 2/3 bash wrapper runs
    where sh is dash (WSL Ubuntu). Without a usable #! line it runs through
    sh. The wrappers lack their executable bit."""
    interpreter = tmp_path / "interp"
    interpreter.write_text("#!/bin/sh\nprintf 'interp:%s\n' \"$@\"\n", encoding="utf-8",
                           newline="\n")
    os.chmod(interpreter, 0o755)
    named = tmp_path / "named" / "mvnw"
    named.parent.mkdir()
    named.write_bytes(f"#! {interpreter} -x\r\nexit 3\r\n".encode())
    plain = tmp_path / "plain" / "gradlew"
    plain.parent.mkdir()
    plain.write_text('echo "sh:$#:$1"\n', encoding="utf-8", newline="\n")
    bare = tmp_path / "bare" / "mvnw"
    bare.parent.mkdir()
    bare.write_text('#! \necho "bare:$#"\n', encoding="utf-8", newline="\n")
    for wrapper in (named, plain, bare):
        os.chmod(wrapper, 0o644)
    assert _launch(named, "a b", "c").splitlines() == [
        "interp:-x", f"interp:{named}", "interp:a b", "interp:c"]
    assert _launch(plain, "a b", "c") == "sh:2:a b\n"
    assert _launch(bare, "a b", "c") == "bare:2\n"


@pytest.mark.skipif(os.name == "nt", reason="a POSIX wrapper and file modes")
def test_a_bash_wrapper_runs_under_bash(tmp_path):
    """Bash syntax that dash refuses, run without the executable bit."""
    import shutil

    if shutil.which("bash") is None:
        pytest.skip("the host has no bash")
    wrapper = tmp_path / "gradlew"
    wrapper.write_text('#!/usr/bin/env bash\nargs=("$@")\n'
                       'echo "bash:${#args[@]}:${args[0]}:${BASH_VERSION:+yes}"\n',
                       encoding="utf-8", newline="\n")
    os.chmod(wrapper, 0o644)
    assert _launch(wrapper, "a b", "c") == "bash:2:a b:yes\n"
