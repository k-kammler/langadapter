"""Test discovery through the selected executor, container and native."""
from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

import langadapter as lb
from langadapter.execution import ContainerExecutor
from langadapter import _process_tree
from langadapter.java import _tools as _junit, _tools, _runs as _collection
from langadapter.java import _benchmark, _env, _warmed_fork
from langadapter.java.adapter import JavaAdapter
from langadapter.java._build import Compilation, JavaProject, Resolution
from langadapter.runs import CollectionRequest


class DiscoveryContainer(ContainerExecutor):
    def __init__(self, zone):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone",
                         helper_python="/control/python")
        self.calls = []
        self.ownership_calls = []
        self.scratch = None
        self.failure = None
        self.outcome = "completed"
        self.arguments = ""

    def run(self, argv, **options):
        if argv[0] == "cat" and Path(argv[-1]).name == ".langadapter-owner":
            self.ownership_calls.append((argv, options))
            return subprocess.CompletedProcess(argv, 0, Path(argv[-1]).read_text(encoding="ascii"), "")
        if argv[:3] == ["rm", "-rf", "--"]:
            self.ownership_calls.append((argv, options))
            assert Path(argv[-1]).is_relative_to(Path(self._host_work_dir))
            assert (Path(argv[-1]) / ".langadapter-owner").is_file()
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.calls.append((argv, options))
        if argv[0].endswith("/java"):
            self.arguments = (self.scratch / "discover.args").read_text(encoding="utf-8")
            if self.failure:
                raise self.failure
            (self.scratch / "discovery.json").write_text(
                '{"outcome":"' + self.outcome + '"}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "selected stdout", "selected stderr")


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    zone = tmp_path / "zone"
    zone.mkdir()
    suite_file = zone / "Example.java"
    suite_file.write_text("class Example {}", encoding="utf-8")
    executor = DiscoveryContainer(zone)
    context = lb.ProjectContext(
        "/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        interpreter_flags=(f"-Dcontext={zone}/context.txt",),
        execution=lb.ContainerExecution("selected", str(zone), container_work_dir="/workzone",
                                        helper_python="/control/python"),
        environment={"JAVA_EXTRA": "selected"})
    adapter = JavaAdapter(context, executor=executor)
    jdk = SimpleNamespace(home="/opt/jdk", version="21", tool=lambda name: "/opt/jdk/bin/" + name)
    project = JavaProject(
        "/project", "maven", 17, (), (), (), (),
        (str(zone / "project.jar"), "/opt/deps/library.jar"),
        test_jvm_args=(f"-Dbuild={zone}/build.txt",),
        test_system_properties={"fixture": str(zone / "fixture.txt")})
    monkeypatch.setattr(adapter, "_jdk", lambda: jdk)
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.COMPLETED, project))
    # Bypass the public container gate to test the mechanism.
    monkeypatch.setattr(adapter, "_request_issue", lambda request: "")
    original_scratch = adapter._scratch

    @contextmanager
    def scratch(name):
        with original_scratch(name) as value:
            executor.scratch = Path(value)
            yield value

    monkeypatch.setattr(adapter, "_scratch", scratch)

    def compile_suite(project, jdk, suite, out_dir, selected, **kwargs):
        assert selected is executor
        Path(out_dir).mkdir()
        return Compilation(lb.Outcome.COMPLETED, out_dir)

    monkeypatch.setattr(_collection, "compile_suite", compile_suite)
    tools_calls = []

    def toolset(classpath, environment, **kwargs):
        tools_calls.append((classpath, environment, kwargs))
        return _junit.Toolset("1.14.4", ("/workzone/tools/launcher.jar",))

    monkeypatch.setattr(_junit, "toolset", toolset)
    monkeypatch.setattr(_process_tree, "run", lambda *args, **kwargs: pytest.fail(
        "container discovery launched a host process"))
    return adapter, lb.Suite(str(zone), ("Example.java",)), executor, tools_calls


def test_discovery_uses_mapped_scratch_tools_arguments_and_explicit_container_environment(discovery):
    adapter, suite, executor, tools_calls = discovery
    result = _collection.collect_suite(adapter, CollectionRequest(suite, timeout_s=13))
    assert result.outcome is lb.Outcome.COMPLETED
    assert result.identity.execution.kind == "container"
    assert result.identity.toolchain.tools == (("junit-platform", "1.14.4"),)
    compile_call, launch_call = executor.calls
    compile_args, env_options = compile_call
    container_scratch = executor._to_container(str(executor.scratch))
    assert container_scratch.startswith("/workzone/")
    assert compile_args[compile_args.index("-d") + 1] == container_scratch
    assert compile_args[compile_args.index("-cp") + 1] == (
        "/workzone/tools/launcher.jar:/workzone/project.jar:/opt/deps/library.jar")
    assert compile_args[-1] == container_scratch + "/LangAdapterDiscover.java"
    assert launch_call[0] == ["/opt/jdk/bin/java", "@" + container_scratch + "/discover.args"]
    assert launch_call[1]["timeout"] == 13
    assert '"-Dbuild=/workzone/build.txt"' in executor.arguments
    assert '"-Dcontext=/workzone/context.txt"' in executor.arguments
    assert '"-Dfixture=/workzone/fixture.txt"' in executor.arguments
    assert '"' + container_scratch + '/discovery.json"' in executor.arguments
    assert str(Path(suite.root)).replace("\\", "/") not in executor.arguments
    assert env_options["env"] == {
        "JAVA_EXTRA": "selected", "JAVA_HOME": "/opt/jdk",
        "PATH": "/opt/jdk/bin:/usr/local/bin:/usr/bin:/bin"}
    assert tools_calls[0][2] == {"executor": executor, "scratch": executor.scratch / "tools"}


@pytest.mark.parametrize("output,stderr", [(b"partial out", None), (None, b"partial err"),
                                          (b"out", b"err"), ("out", "err")])
def test_discovery_timeout_preserves_partial_diagnostics(discovery, recwarn, output, stderr):
    adapter, suite, executor, _ = discovery
    executor.failure = subprocess.TimeoutExpired("java", 13, output=output, stderr=stderr)
    result = _collection.collect_suite(adapter, CollectionRequest(suite, timeout_s=13))
    assert result.outcome is lb.Outcome.TIMED_OUT
    assert result.output == "".join(value.decode() if isinstance(value, bytes) else value or ""
                                  for value in (output, stderr))
    assert result.wall_time_s is not None
    assert executor.scratch.is_dir() and (executor.scratch / ".langadapter-owner").is_file()
    assert [argv[0] for argv, _ in executor.ownership_calls] == ["cat"]
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.mark.parametrize("outcome", ["no_tests", "collection_failed", "unexpected"])
def test_discovery_report_retains_native_non_success_outcomes(discovery, outcome):
    adapter, suite, executor, _ = discovery
    executor.outcome = outcome
    result = _collection.collect_suite(adapter, CollectionRequest(suite, timeout_s=13))
    expected = {"no_tests": lb.Outcome.NO_TESTS, "collection_failed": lb.Outcome.COLLECTION_FAILED}
    assert result.outcome is expected.get(outcome, lb.Outcome.INFRASTRUCTURE_FAILURE)


def test_unmounted_discovery_output_is_refused_before_compile_fetch_or_writes(discovery, tmp_path, monkeypatch):
    adapter, suite, executor, tools_calls = discovery
    foreign = tmp_path / "foreign"
    foreign.mkdir()

    @contextmanager
    def scratch(name):
        yield foreign

    monkeypatch.setattr(adapter, "_scratch", scratch)
    monkeypatch.setattr(_collection, "compile_suite", lambda *args, **kwargs: pytest.fail(
        "unmounted discovery compiled the suite"))
    result = _collection.collect_suite(adapter, CollectionRequest(suite, timeout_s=13))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "work folder" in result.reason
    assert list(foreign.iterdir()) == [] and not executor.calls and not tools_calls


@pytest.mark.parametrize("stage", ["compile", "source", "classes", "arguments"])
def test_preparation_filesystem_failures_are_reported_without_a_discovery_launch(discovery, monkeypatch, stage):
    adapter, suite, executor, _ = discovery

    def failure(*args, **kwargs):
        raise OSError("fixture filesystem unavailable")

    if stage == "compile":
        monkeypatch.setattr(_collection, "compile_suite", failure)
    elif stage == "classes":
        monkeypatch.setattr(adapter, "_class_names", failure)
    elif stage == "arguments":
        monkeypatch.setattr(_junit, "argfile", failure)
    else:
        write = Path.write_text

        def source_write(path, *args, **options):
            if path.name == "LangAdapterDiscover.java":
                return failure()
            return write(path, *args, **options)

        monkeypatch.setattr(Path, "write_text", source_write)
    result = _collection.collect_suite(adapter, CollectionRequest(suite, 13))
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "fixture filesystem unavailable" in result.reason
    assert not any(argv[0].endswith("/java") for argv, _ in executor.calls)


def test_container_collection_without_a_control_helper_is_missing_setup(tmp_path):
    # Container Java runs only through a selected container control helper.
    source = tmp_path / "Example.java"
    source.write_text("class Example {}", encoding="utf-8")
    adapter = JavaAdapter(lb.ProjectContext(
        "/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution("selected", str(tmp_path))))
    result = adapter.tests.collect_suite(CollectionRequest(lb.Suite(str(tmp_path), ("Example.java",)), 1))
    assert result.outcome is lb.Outcome.MISSING_SETUP and "helper_python" in result.reason


def test_native_discovery_keeps_host_paths_classpath_and_process_tree(tmp_path, monkeypatch):
    source = tmp_path / "Example.java"
    source.write_text("class Example {}", encoding="utf-8")
    adapter = JavaAdapter(lb.ProjectContext(
        str(tmp_path), interpreter=lb.Interpreter(path="java")))
    jdk = SimpleNamespace(home=str(tmp_path / "jdk"), version="21", tool=lambda name: name)
    project = JavaProject(str(tmp_path), "maven", 17, (), (), (), (), (str(tmp_path / "project.jar"),))
    monkeypatch.setattr(adapter, "_jdk", lambda: jdk)
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.COMPLETED, project))
    monkeypatch.setattr(_junit, "toolset",
                        lambda *args, **kwargs: _junit.Toolset("1.14.4", ("launcher.jar",)))
    monkeypatch.setattr(_collection, "compile_suite", lambda *args, **kwargs: Compilation(
        lb.Outcome.COMPLETED, str(tmp_path / "classes")))
    helper_calls = []
    adapter._executor = SimpleNamespace(run=lambda argv, **options: (
        helper_calls.append((argv, options)) or subprocess.CompletedProcess(argv, 0, "", "")))

    @contextmanager
    def scratch(name):
        yield str(tmp_path)

    monkeypatch.setattr(adapter, "_scratch", scratch)
    launches = []

    def launch(argv, **options):
        launches.append((argv, options))
        (tmp_path / "discovery.json").write_text('{"outcome":"completed"}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "native", "")

    monkeypatch.setattr(_process_tree, "run", launch)
    result = adapter.tests.collect_suite(CollectionRequest(lb.Suite(str(tmp_path), ("Example.java",)), 11))
    assert result.outcome is lb.Outcome.COMPLETED and result.output == "native"
    helper_args = helper_calls[0][0]
    assert helper_args[helper_args.index("-cp") + 1] == os.pathsep.join(
        ["launcher.jar", str(tmp_path / "project.jar")])
    assert helper_args[-1] == str(tmp_path / "LangAdapterDiscover.java")
    assert launches[0][0] == ["java", f"@{tmp_path / 'discover.args'}"]
    assert launches[0][1]["timeout"] == 11 and launches[0][1]["text"] is True


def test_container_scratch_is_created_only_under_declared_mapped_zone(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    adapter = JavaAdapter(lb.ProjectContext(
        "/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution("selected", str(zone))), executor=DiscoveryContainer(zone))
    with adapter._scratch("collect") as scratch:
        created = Path(scratch)
        assert created.parent == zone
    assert not created.exists()
    assert [argv[0] for argv, _ in adapter._executor.ownership_calls] == ["cat", "cat", "rm"]


def test_real_offline_container_discovery_does_not_execute_bodies_or_factories(tmp_path, monkeypatch):
    container = os.environ.get("LANGADAPTER_TEST_CONTAINER")
    java = os.environ.get("LANGADAPTER_TEST_CONTAINER_JAVA")
    libraries = os.environ.get("LANGADAPTER_TEST_CONTAINER_JUNIT_CLASSPATH")
    zone = os.environ.get("LANGADAPTER_TEST_WORK_DIR")
    cache = os.environ.get("LANGADAPTER_TEST_TOOL_CACHE")
    helper = os.environ.get("LANGADAPTER_TEST_CONTAINER_HELPER_PYTHON")
    if not all((container, java, libraries, zone, cache, helper)):
        pytest.skip("requires a prepared offline container JDK, JUnit classpath and helper Python")

    def forbidden(*args, **kwargs):
        pytest.fail("offline container discovery attempted a download or host Java launch")

    monkeypatch.setattr(_tools.urllib.request, "urlopen", forbidden)
    # Every host launch in java/: JavaEnv's local route, the benchmark's
    # affinity probe and the warmed fork. A container's commands run through its
    # executor.
    monkeypatch.setattr(_env, "_process_tree", SimpleNamespace(run=forbidden))
    monkeypatch.setattr(_benchmark, "_process_tree", SimpleNamespace(run=forbidden))
    monkeypatch.setattr(_warmed_fork, "_process_tree", SimpleNamespace(start=forbidden))
    root = tmp_path / "fixture"
    root.mkdir()
    marker = root / "executed.txt"
    (root / "Example.java").write_text('''
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.api.DynamicTest;
public class Example {
    @Test void body() throws Exception {
        Files.writeString(Path.of(System.getProperty("marker")), "body");
        throw new AssertionError("test body executed");
    }
    @TestFactory Stream<DynamicTest> factory() throws Exception {
        Files.writeString(Path.of(System.getProperty("marker")), "factory");
        throw new AssertionError("test factory executed");
    }
}
''', encoding="utf-8")
    adapter = JavaAdapter(lb.ProjectContext(
        "/workzone", interpreter=lb.Interpreter(path=java),
        execution=lb.ContainerExecution(container, zone, container_work_dir="/workzone",
                                        helper_python=helper),
        environment={"LANGADAPTER_JAVA_TOOLS": cache}))
    project = JavaProject("/workzone", "maven", 17, (), (), (), (), tuple(libraries.split(":")),
                          test_system_properties={"marker": str(marker)})
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.COMPLETED, project))
    monkeypatch.setattr(adapter, "_request_issue", lambda request: "")
    result = _collection.collect_suite(
        adapter, CollectionRequest(lb.Suite(str(root), ("Example.java",)), timeout_s=60))
    assert result.outcome is lb.Outcome.COMPLETED, (result.reason, result.output)
    assert result.identity.execution.kind == "container"
    assert not marker.exists()
