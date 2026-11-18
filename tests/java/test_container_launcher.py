"""JUnit launch preparation follows the selected execution environment."""
from __future__ import annotations

import os
import subprocess
from pathlib import PurePosixPath

import pytest

from langadapter.execution import ContainerExecutor
from langadapter import _process_tree
from langadapter.java import _tools as _junit, _tools


class LauncherContainer(ContainerExecutor):
    def __init__(self, zone):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone")
        self.calls = []

    def run(self, argv, **options):
        self.calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, "selected output", "")


def test_container_argument_contents_translate_paths_before_joining(tmp_path):
    container = LauncherContainer(tmp_path)
    jars = [str(tmp_path / "lib" / "first.jar"), "/opt/lib/second.jar"]
    arguments = _junit.arguments(
        jars, ["demo.Example"], tmp_path / "reports", executor=container,
        jvm_args=[f"-javaagent:{tmp_path}/agent.jar=destfile={tmp_path}/coverage.exec"],
        system_properties={"fixture": str(tmp_path / "fixture.txt")})
    assert arguments[arguments.index("-cp") + 1] == "/workzone/lib/first.jar:/opt/lib/second.jar"
    assert "--reports-dir=/workzone/reports" in arguments
    assert "-javaagent:/workzone/agent.jar=destfile=/workzone/coverage.exec" in arguments
    assert "-Dfixture=/workzone/fixture.txt" in arguments
    assert all(str(tmp_path) not in argument for argument in arguments)


def test_container_launch_uses_selected_executor_and_container_argument_file(tmp_path):
    container = LauncherContainer(tmp_path)
    launch = _junit.launch(
        "/opt/jdk/bin/java", [str(tmp_path / "classes")], ["demo.Example"],
        tmp_path / "reports", cwd="/project", env={"JAVA_EXTRA": "selected"},
        timeout=13, executor=container)
    assert launch.returncode == 0
    assert launch.output == "selected output"
    assert container.calls == [(["/opt/jdk/bin/java", "@/workzone/reports.args"], {
        "cwd": "/project", "env": {"JAVA_EXTRA": "selected"}, "timeout": 13})]
    contents = (tmp_path / "reports.args").read_text(encoding="utf-8")
    assert '"/workzone/classes"' in contents
    assert '"--reports-dir=/workzone/reports"' in contents


def test_unmounted_report_path_is_refused_before_writes_or_execution(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    container = LauncherContainer(zone)
    reports = tmp_path / "foreign" / "reports"
    with pytest.raises(OSError, match="work folder"):
        _junit.launch("/opt/jdk/bin/java", [], [], reports, executor=container,
                      cwd="/project", env={}, timeout=1)
    assert not reports.parent.exists()
    assert not container.calls


def test_argument_file_cannot_escape_mount_when_reports_are_zone_root(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    container = LauncherContainer(zone)
    with pytest.raises(OSError, match="work folder"):
        _junit.launch("/opt/jdk/bin/java", [], [], zone, executor=container,
                      cwd="/project", env={}, timeout=1)
    assert not (tmp_path / "zone.args").exists()
    assert not container.calls


@pytest.mark.parametrize("output,stderr", [(b"partial output", None),
                                          (None, b"partial error"),
                                          (b"output", b"error"),
                                          ("output", "error")])
@pytest.mark.parametrize("native", [False, True])
def test_timeout_preserves_output_and_written_reports(tmp_path, monkeypatch, output, stderr, native):
    container = LauncherContainer(tmp_path)

    def timeout(argv, **options):
        (tmp_path / "reports" / "TEST-demo.xml").write_text("<testsuite/>")
        raise subprocess.TimeoutExpired(argv, 1, output=output, stderr=stderr)

    monkeypatch.setattr(_process_tree if native else container,
                        "run", timeout)
    execution = {} if native else {"executor": container}
    observed = _junit.launch("/opt/jdk/bin/java", [], [], tmp_path / "reports",
                             cwd="/project", env={}, timeout=1, **execution)
    expected = "".join(value.decode() if isinstance(value, bytes) else value or ""
                       for value in (output, stderr))
    assert observed.timed_out
    assert observed.returncode is None
    assert observed.output == expected
    assert observed.reports == (tmp_path / "reports" / "TEST-demo.xml",)


def test_native_argument_contents_keep_host_classpath_and_paths(tmp_path):
    classpath = [str(tmp_path / "classes"), str(tmp_path / "library.jar")]
    arguments = _junit.arguments(classpath, ["Example"], tmp_path / "reports")
    assert arguments[arguments.index("-cp") + 1] == os.pathsep.join(classpath)
    assert f"--reports-dir={tmp_path / 'reports'}" in arguments


def test_native_launch_still_uses_process_tree(tmp_path, monkeypatch):
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 1, "failed test", "diagnostic")

    monkeypatch.setattr(_process_tree, "run", run)
    result = _junit.launch("java", [], ["Example"], tmp_path / "reports",
                           cwd=str(tmp_path), env={}, timeout=5)
    assert result.returncode == 1
    assert result.output == "failed testdiagnostic"
    assert calls[0][0] == ["java", f"@{tmp_path / 'reports.args'}"]


def test_real_offline_container_launcher(tmp_path, monkeypatch):
    container = os.environ.get("LANGADAPTER_TEST_CONTAINER")
    java = os.environ.get("LANGADAPTER_TEST_CONTAINER_JAVA")
    libraries = os.environ.get("LANGADAPTER_TEST_CONTAINER_JUNIT_CLASSPATH")
    zone = os.environ.get("LANGADAPTER_TEST_WORK_DIR")
    cache = os.environ.get("LANGADAPTER_TEST_TOOL_CACHE")
    if not all((container, java, libraries, zone, cache)):
        pytest.skip("requires a prepared offline container JDK and JUnit classpath")

    def no_download(*args, **kwargs):
        pytest.fail("offline fixture attempted a tool download")

    monkeypatch.setattr(_tools.urllib.request, "urlopen", no_download)
    container_executor = ContainerExecutor(container, work_dir=zone, container_work_dir="/workzone")
    root = tmp_path / "fixture"
    root.mkdir()
    source = root / "Example.java"
    source.write_text('''import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
public class Example {
    @Test void passes() { assertEquals("selected", System.getProperty("fixture")); }
    @Test void fails() { assertEquals(1, 2); }
}
''', encoding="utf-8")
    classes = root / "classes"
    compiled = container_executor.run([str(PurePosixPath(java).with_name("javac")), "-cp", libraries,
                          "-d", str(classes), str(source)], timeout=60)
    assert compiled.returncode == 0, (compiled.stdout, compiled.stderr)
    project_jars = [path for path in libraries.split(":") if not any(
        PurePosixPath(path).name.startswith(prefix) for prefix in (
            "junit-platform-console-", "junit-platform-reporting-",
            "junit-platform-launcher-", "open-test-reporting-tooling-spi-"))]
    tools = _junit.toolset(project_jars, {"LANGADAPTER_JAVA_TOOLS": cache},
                          executor=container_executor, scratch=root / "tools")
    assert all(path.startswith("/workzone/") for path in tools.jars)
    result = _junit.launch(java, [*tools.jars, str(classes), *project_jars], ["Example"],
                           root / "reports", executor=container_executor, cwd="/workzone", env={},
                           system_properties={"fixture": "selected"}, timeout=60)
    assert not result.timed_out
    assert result.returncode == 1, result.output
    assert sorted(case.outcome for case in _junit.read_reports(result.reports)) == ["failed", "passed"]
