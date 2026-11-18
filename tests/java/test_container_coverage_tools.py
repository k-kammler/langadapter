"""JaCoCo's native helper and agent can run through a selected container executor."""
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from langadapter.execution import ContainerExecutor
from langadapter.java import _jacoco, _tools, _env as jdk


class CoverageContainer(ContainerExecutor):
    def __init__(self, zone, *, compile_code=0, analysis_code=0):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone")
        self.calls = []
        self.compile_code = compile_code
        self.analysis_code = analysis_code

    def run(self, argv, **options):
        argv = [self._to_container(str(part)) for part in argv]
        self.calls.append((argv, options))
        name = PurePosixPath(argv[0]).name
        if name == "cat":
            return subprocess.CompletedProcess(argv, 0, "package demo; public class Calc {}", "")
        if name == "find":
            return subprocess.CompletedProcess(argv, 0, "/project/classes/demo/Calc.class\n", "")
        if name == "javac":
            return subprocess.CompletedProcess(argv, self.compile_code, "", "compiler detail")
        return subprocess.CompletedProcess(argv, self.analysis_code,
                                           "suite\t4:1:2:0\ntest-0\t4:0\n", "analysis detail")


@pytest.fixture
def cached_tools(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()

    def jar(coordinate, environment):
        path = cache / (coordinate.split(":")[1] + ".jar")
        path.write_bytes(coordinate.encode())
        return path

    monkeypatch.setattr(_tools, "jar", jar)

    def forbidden(*args, **kwargs):
        pytest.fail("container coverage attempted host execution")

    monkeypatch.setattr(subprocess, "run", forbidden)
    return cache


def test_analyzer_uses_container_jdk_paths_and_classpath(tmp_path, cached_tools):
    zone = tmp_path / "zone"
    zone.mkdir()
    container = CoverageContainer(zone)
    selected = jdk._ContainerJdk("/opt/jdk", 21, "21.0.fixture")
    helper, jars = _jacoco.analyzer(selected, {}, {"SELECTED": "yes"},
                                   executor=container, scratch=zone / "coverage")
    assert helper.is_relative_to(zone)
    assert len(jars) == len(_jacoco.CORE)
    command, options = container.calls[0]
    assert command[0] == "/opt/jdk/bin/javac"
    assert command[command.index("-cp") + 1] == ":".join(jars)
    assert command[-1].startswith("/workzone/coverage/")
    assert options == {"env": {"SELECTED": "yes"}, "timeout": 300}
    assert (helper.parent / "LangAdapterCoverageAnalyzer.java").read_text() == _jacoco._ANALYZER
    assert all(str(tmp_path) not in part for part in command)


def test_analysis_reads_container_source_classes_and_staged_cache(tmp_path, cached_tools):
    container = CoverageContainer(tmp_path)
    records, reason = _jacoco.analyze(
        jdk._ContainerJdk("/opt/jdk", 21, "21"), "run", tmp_path / "run.exec",
        Path("/project/src/demo/Calc.java"), ["/project/classes"], environment={},
        env={"SELECTED": "yes"}, executor=container, scratch=tmp_path / "analysis")
    assert records == {"suite": ["4:1:2:0"], "test-0": ["4:0"]}
    assert reason == ""
    command, options = container.calls[-1]
    assert command[0] == "/opt/jdk/bin/java"
    assert command[3:] == ["LangAdapterCoverageAnalyzer", "run", "/workzone/run.exec",
                           f"/workzone/analysis/identity/jacoco-{_jacoco.VERSION}-{_jacoco.IDENTITY}",
                           "demo", "Calc.java", "/project/classes/demo/Calc.class"]
    assert command[2].startswith("/workzone/analysis/analyzer-")
    assert options == {"env": {"SELECTED": "yes"}, "timeout": _jacoco._ANALYSIS_TIMEOUT_S}


def test_container_agent_argument_stages_jar_and_destfile(tmp_path, cached_tools):
    zone = tmp_path / "zone"
    zone.mkdir()
    container = CoverageContainer(zone)
    argument = _jacoco.agent_argument(zone / "run.exec", ("demo.Calc",), {}, executor=container)
    assert argument.startswith("-javaagent:/workzone/tools/java-tools-")
    assert "=destfile=/workzone/run.exec,includes=demo.Calc:demo.Calc$*" in argument
    assert argument.endswith(",output=file,append=false")
    assert not container.calls


@pytest.mark.parametrize("operation", ["analyzer", "agent"])
def test_unmapped_outputs_are_refused_before_fetch_or_launch(tmp_path, monkeypatch, operation):
    zone = tmp_path / "zone"
    zone.mkdir()
    container = CoverageContainer(zone)

    def forbidden(*args, **kwargs):
        pytest.fail("fetched before output validation")

    monkeypatch.setattr(_tools, "jar", forbidden)
    with pytest.raises(OSError, match="scratch|work folder"):
        if operation == "analyzer":
            _jacoco.analyzer(jdk._ContainerJdk("/opt/jdk", 21, "21"), {}, {}, executor=container)
        else:
            _jacoco.agent_argument(tmp_path / "outside.exec", "demo.Calc", {}, executor=container)
    assert not container.calls


def test_failed_compilation_and_analysis_preserve_failure(tmp_path, cached_tools):
    selected = jdk._ContainerJdk("/opt/jdk", 21, "21")
    container = CoverageContainer(tmp_path, compile_code=1)
    with pytest.raises(_tools.ToolUnavailable, match="compiler detail"):
        _jacoco.analyzer(selected, {}, {}, executor=container, scratch=tmp_path / "compile")
    container = CoverageContainer(tmp_path, analysis_code=1)
    records, reason = _jacoco.analyze(selected, "run", tmp_path / "data.exec",
                                     Path("/project/Calc.java"), ["/project/classes"],
                                     environment={}, env={}, executor=container,
                                     scratch=tmp_path / "analysis")
    assert records is None
    assert "analysis detail" in reason


def test_native_agent_argument_keeps_cache_path(tmp_path, cached_tools):
    argument = _jacoco.agent_argument(tmp_path / "run.exec", "demo.Calc", {})
    assert argument.startswith(f"-javaagent:{cached_tools / 'org.jacoco.agent.jar'}=")
    assert f"destfile={tmp_path / 'run.exec'}" in argument


def test_real_offline_container_coverage(tmp_path, monkeypatch):
    container = os.environ.get("LANGADAPTER_TEST_CONTAINER")
    java = os.environ.get("LANGADAPTER_TEST_CONTAINER_JAVA")
    zone = os.environ.get("LANGADAPTER_TEST_WORK_DIR")
    cache = os.environ.get("LANGADAPTER_TEST_TOOL_CACHE")
    if not all((container, java, zone, cache)):
        pytest.skip("requires prepared offline container JDK and coverage tools")

    def no_download(*args, **kwargs):
        pytest.fail("offline fixture attempted a tool download")

    monkeypatch.setattr(_tools.urllib.request, "urlopen", no_download)
    container_executor = ContainerExecutor(container, work_dir=zone, container_work_dir="/workzone")
    selected = jdk.read_in_container(java, container_executor)
    assert selected is not None
    root = tmp_path / "fixture"
    root.mkdir()
    source = root / "Calc.java"
    source.write_text("""package demo;
public class Calc {
  public static int sign(int x) {
    if (x > 0) return 1;
    return -1;
  }
  public static void main(String[] args) { sign(1); }
}
""")
    classes = root / "classes"
    compiled = container_executor.run([selected.tool("javac"), "--release", "11", "-g",
                          "-d", str(classes), str(source)], timeout=60)
    assert compiled.returncode == 0, (compiled.stdout, compiled.stderr)
    environment = {"LANGADAPTER_JAVA_TOOLS": cache}
    data = root / "run.exec"
    argument = _jacoco.agent_argument(data, ("demo.Calc",), environment, executor=container_executor)
    done = container_executor.run([selected.tool("java"), argument, "-cp", str(classes), "demo.Calc"], timeout=60)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert data.is_file()
    records, reason = _jacoco.analyze(selected, "run", data, source, [str(classes)],
                                     environment=environment, env={}, executor=container_executor,
                                     scratch=root / "analysis")
    assert reason == ""
    coverage = _jacoco.observation(records.get("suite"), source, str(root))
    assert coverage is not None
    assert coverage.target.path == "Calc.java"
    assert coverage.missing() == (2, 5)
    assert (len(coverage.branches), len(coverage.taken)) == (2, 1)


def test_a_class_dir_without_the_target_s_package_is_skipped_in_a_container(tmp_path):
    """A Gradle project's class directory of another language can lack the
    target's package. The container listing skips it, as the host's does."""
    class Dirs(CoverageContainer):
        def run(self, argv, **options):
            argv = [self._to_container(str(part)) for part in argv]
            name = PurePosixPath(argv[0]).name
            if name in ("test", "find") and "/kotlin" in argv[-1 if name == "test" else 1]:
                self.calls.append((argv, options))
                return subprocess.CompletedProcess(argv, 1, "", "No such file or directory")
            return super().run(argv, **options)

    zone = tmp_path / "zone"
    zone.mkdir()
    container = Dirs(zone)
    found = _jacoco.class_files([str(zone / "java"), str(zone / "kotlin")],
                                zone / "src" / "Calc.java", executor=container)
    assert found == ["/project/classes/demo/Calc.class"]
    assert [argv[1] for argv, _ in container.calls
            if PurePosixPath(argv[0]).name == "find"] == ["/workzone/java/demo"]
