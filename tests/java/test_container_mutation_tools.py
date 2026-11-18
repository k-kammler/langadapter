"""Selected container PIT tools retain native catalogue, launch and verdict rules."""
import os
import subprocess
from pathlib import PurePosixPath

import pytest

from langadapter import _process_tree
from langadapter.execution import ContainerExecutor
from langadapter.java import _pit, _tools, _env as jdk


class MutationContainer(ContainerExecutor):
    def __init__(self, zone, *, code=0, timed_out=False):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone")
        self.calls = []
        self.code = code
        self.timed_out = timed_out

    def run(self, argv, **options):
        argv = [self._to_container(str(part)) for part in argv]
        self.calls.append((argv, options))
        if self.timed_out:
            raise subprocess.TimeoutExpired(argv, options["timeout"], output=b"partial", stderr=b" detail")
        if PurePosixPath(argv[0]).name == "javac":
            return subprocess.CompletedProcess(argv, self.code, "", "compiler detail")
        output = "ID_B\nID_A\n"
        return subprocess.CompletedProcess(argv, self.code, output, "tool detail")


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
        pytest.fail("container PIT attempted host execution")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(_process_tree, "run", forbidden)
    return cache


def test_container_catalogue_uses_selected_runtime_and_staged_tools(tmp_path, cached_tools):
    zone = tmp_path / "zone"
    zone.mkdir()
    container = MutationContainer(zone)
    result = _pit.mutator_ids("/opt/jdk/bin/java", ("DEFAULTS",), zone,
                              environment={}, env={"SELECTED": "yes"},
                              executor=container)
    assert result == ("ID_A", "ID_B")
    command, options = container.calls[0]
    assert command[0] == "/opt/jdk/bin/java"
    assert command[-2:] == ["/workzone/LangAdapterPitMutators.java", "DEFAULTS"]
    assert len(command[2].split(":")) == len(_pit.JARS)
    assert all(path.startswith("/workzone/tools/java-tools-") for path in command[2].split(":"))
    assert options == {"env": {"SELECTED": "yes"}, "timeout": _pit._LISTING_TIMEOUT_S}
    assert (zone / "LangAdapterPitMutators.java").read_text() == _pit._LISTER


def test_container_order_plugin_is_compiled_in_mapped_scratch(tmp_path, cached_tools):
    container = MutationContainer(tmp_path)
    plugin = _pit.test_order("/opt/jdk/bin/javac", {}, {"SELECTED": "yes"},
                             executor=container, scratch=tmp_path / "order")
    command, options = container.calls[0]
    assert command[:5] == ["/opt/jdk/bin/javac", "--release", "11", "-nowarn", "-d"]
    assert command[5].startswith("/workzone/order/pit-order-")
    assert len(command[7].split(":")) == len(_pit.JARS)
    assert options == {"env": {"SELECTED": "yes"}, "timeout": 300}
    service = plugin / "META-INF/services/org.pitest.mutationtest.build.TestPrioritiserFactory"
    assert service.read_text() == "LangAdapterTestOrder\n"
    assert (plugin.parent / "LangAdapterTestOrder.java").read_text() == _pit._ORDER


def launch(scratch, *, executor=None, target_classes=("demo.Calc",), **extra):
    """One PIT launch (``_pit.run`` asked for every killer makes two)."""
    return _pit._launch(
        "/opt/jdk/bin/java", scratch=scratch, target_classes=target_classes,
        test_classes=("demo.CalcTest",), source_dirs=(str(scratch / "src"), "/project/src"),
        test_classpath=(str(scratch / "classes"), "/project/libraries/test.jar"),
        mutable_paths=(str(scratch / "classes"),), mutators=("DEFAULTS",), all_killers=True,
        threads=2, timeout_factor=1.25, timeout_constant_s=4,
        jvm_args=(f"-javaagent:{scratch / 'agent.jar'}=output=file",),
        system_properties={"fixture.path": str(scratch / "data")}, encoding="UTF-8",
        cwd=str(scratch), env={"SELECTED": "yes"}, environment={}, wall_clock_s=19,
        plugins=(str(scratch / "plugin"),), executor=executor, **extra)


def test_pit_mutates_every_top_level_type_of_the_target(tmp_path, cached_tools):
    """PIT targets every type the target's file declares, each with its
    nested classes, as JaCoCo records them."""
    container = MutationContainer(tmp_path)
    launch(tmp_path, executor=container, target_classes=("demo.Calc", "demo.CalcHelper"))
    assert ('"--targetClasses"\n"demo.Calc,demo.Calc$*,demo.CalcHelper,demo.CalcHelper$*"'
            in (tmp_path / "pit.args").read_text())


def test_container_run_translates_each_path_and_preserves_pit_options(tmp_path, cached_tools):
    container = MutationContainer(tmp_path)
    report = launch(tmp_path, executor=container)
    assert report.returncode == 0
    command, options = container.calls[0]
    assert command == ["/opt/jdk/bin/java", "@/workzone/pit.args"]
    assert options == {"timeout": 19, "cwd": str(tmp_path), "env": {"SELECTED": "yes"}}
    arguments = (tmp_path / "pit.args").read_text()
    assert str(tmp_path) not in arguments
    assert '"/workzone/src,/project/src"' in arguments
    assert '"/workzone/pit-classpath.txt"' in arguments
    assert '"/workzone/classes"' in arguments
    assert '"/opt/jdk/bin/java"' in arguments
    assert '"--fullMutationMatrix"\n"true"' in arguments
    assert '"--timeoutConst"\n"4000"' in arguments
    assert '"--threads"\n"2"' in arguments
    assert '"--mutationUnitSize"\n"0"' in arguments
    assert '-javaagent:/workzone/agent.jar=output=file -Dfixture.path=/workzone/data' in arguments
    assert (tmp_path / "pit-classpath.txt").read_text() == (
        "/workzone/classes\n/project/libraries/test.jar\n")


def test_a_unit_size_reaches_pit(tmp_path, cached_tools):
    launch(tmp_path, executor=MutationContainer(tmp_path), unit_size=10)
    assert '"--mutationUnitSize"\n"10"' in (tmp_path / "pit.args").read_text()


@pytest.mark.parametrize("operation", ["classpath", "order", "ids", "run"])
def test_unmapped_scratch_refused_before_fetch_write_or_launch(tmp_path, monkeypatch, operation):
    zone = tmp_path / "zone"
    zone.mkdir()
    container = MutationContainer(zone)
    outside = tmp_path / "outside"

    def forbidden(*args, **kwargs):
        pytest.fail("PIT fetched before output validation")

    monkeypatch.setattr(_tools, "jar", forbidden)
    with pytest.raises(OSError, match="work folder"):
        if operation == "classpath":
            _pit.classpath({}, executor=container, scratch=outside)
        elif operation == "order":
            _pit.test_order("javac", {}, {}, executor=container, scratch=outside)
        elif operation == "ids":
            _pit.mutator_ids("java", (), outside, environment={}, env={}, executor=container)
        else:
            launch(outside, executor=container)
    assert not outside.exists()
    assert not container.calls


def test_failed_plugin_and_catalogue_preserve_native_failure(tmp_path, cached_tools):
    """The lister exits 2 for a name PIT does not know. Any other exit is a
    failed listing, an OSError the run reports as an infrastructure
    failure."""
    container = MutationContainer(tmp_path, code=1)
    with pytest.raises(_tools.ToolUnavailable, match="compiler detail"):
        _pit.test_order("javac", {}, {}, executor=container, scratch=tmp_path)
    with pytest.raises(OSError, match=r"(?s)listing failed \(exit 1\): .*tool detail"):
        _pit.mutator_ids("java", ("DEFAULTS",), tmp_path, environment={}, env={}, executor=container)
    container = MutationContainer(tmp_path, code=2)
    with pytest.raises(_pit.UnknownMutator, match="tool detail"):
        _pit.mutator_ids("java", ("UNKNOWN",), tmp_path, environment={}, env={}, executor=container)


@pytest.mark.parametrize("stdout,stderr", [(b"partial", None), (None, b" detail"),
                                           (b"partial", b" detail"), ("partial", " detail")])
def test_native_timeout_retains_output_and_partial_xml(tmp_path, monkeypatch, stdout, stderr):
    monkeypatch.setattr(_pit, "classpath", lambda environment, **kwargs: ("pit.jar",))

    def expired(*args, **kwargs):
        report = tmp_path / "pit/mutations.xml"
        report.parent.mkdir(exist_ok=True)
        report.write_text("<mutations/>")
        raise subprocess.TimeoutExpired(args[0], 19, output=stdout, stderr=stderr)

    monkeypatch.setattr(_process_tree, "run", expired)
    report = launch(tmp_path)
    assert report.returncode is None and report.timed_out
    expected = "".join(part.decode() if isinstance(part, bytes) else part or ""
                       for part in (stdout, stderr))
    assert report.output == expected
    assert report.mutations == tmp_path / "pit/mutations.xml"


def test_container_timeout_retains_output(tmp_path, cached_tools):
    report = launch(tmp_path, executor=MutationContainer(tmp_path, timed_out=True))
    assert report.returncode is None and report.timed_out
    assert report.output == "partial detail"
    assert report.mutations is None


@pytest.mark.mutation_tool
def test_real_offline_container_pit(tmp_path, monkeypatch):
    container = os.environ.get("LANGADAPTER_TEST_CONTAINER")
    java = os.environ.get("LANGADAPTER_TEST_CONTAINER_JAVA")
    zone = os.environ.get("LANGADAPTER_TEST_WORK_DIR")
    cache = os.environ.get("LANGADAPTER_TEST_TOOL_CACHE")
    classpath = os.environ.get("LANGADAPTER_TEST_CONTAINER_JUNIT_CLASSPATH")
    if not all((container, java, zone, cache, classpath)):
        pytest.skip("requires prepared offline container JDK, JUnit and PIT tools")

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
  public static int sign(int x) { return x > 0 ? 1 : -1; }
}
""")
    test = root / "CalcTest.java"
    test.write_text("""package demo;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
public class CalcTest {
  @Test void positive() { assertEquals(1, Calc.sign(2)); }
  @Test void negative() { assertEquals(-1, Calc.sign(-2)); }
}
""")
    classes = root / "classes"
    tests = root / "tests"
    compiled = container_executor.run([selected.tool("javac"), "--release", "11", "-g", "-d",
                          str(classes), str(source)], timeout=60)
    assert compiled.returncode == 0, (compiled.stdout, compiled.stderr)
    libraries = classpath.split(":")
    test_path = ":".join([container_executor._to_container(str(classes)), *libraries])
    compiled = container_executor.run([selected.tool("javac"), "--release", "11", "-g", "-d",
                          str(tests), "-cp", test_path, str(test)], timeout=60)
    assert compiled.returncode == 0, (compiled.stdout, compiled.stderr)
    environment = {"LANGADAPTER_JAVA_TOOLS": cache}
    env = {"JAVA_HOME": "/opt/jdk", "PATH": "/opt/jdk/bin:/usr/bin:/bin"}
    operators = _pit.mutator_ids(java, (), root, environment=environment, env=env, executor=container_executor)
    assert operators
    assert set(operators) <= set(_pit.MUTATORS.values())
    plugin = _pit.test_order(selected.tool("javac"), environment, env, executor=container_executor, scratch=root)
    # The normal run's launcher jars, as the adapter's PIT route passes them
    # (java/_mutation.py), without the console launcher PIT does not use.
    tools = _tools.toolset(libraries, environment, executor=container_executor, scratch=root / "tools")
    launcher = [jar for jar in tools.jars if not jar.rsplit("/", 1)[-1].startswith("junit-platform-console-")]
    report = _pit.run(
        java, scratch=root, target_classes=("demo.Calc",), test_classes=("demo.CalcTest",),
        source_dirs=(str(root),), test_classpath=(str(tests), *launcher, str(classes), *libraries),
        mutable_paths=(str(classes),), mutators=(), all_killers=True, threads=1,
        timeout_factor=1.25, timeout_constant_s=4, jvm_args=("-Xmx128m",),
        system_properties={}, encoding="UTF-8", cwd=str(root), env=env,
        environment=environment, wall_clock_s=120, plugins=(str(plugin),), executor=container_executor)
    assert report.returncode == 0 and not report.timed_out, report.output
    assert report.mutations is not None, report.output
    records = _pit.read(report.mutations)
    assert records and any(record.status == "KILLED" and record.killers for record in records)
    assert all(_pit.classify(record.status, record.killers).status != "untested" for record in records)


def test_the_default_catalogue_of_a_container_project_runs_in_the_container(
        tmp_path, cached_tools, monkeypatch):
    """mutation_operators lists PIT's default mutators with the container's JDK
    through the context's executor, as a mutation session does, never with
    the container's java on the host."""
    import contextlib
    from types import SimpleNamespace

    import langadapter as lb
    from langadapter.java.adapter import JavaAdapter

    zone = tmp_path / "zone"
    zone.mkdir()
    container = MutationContainer(zone)
    context = lb.ProjectContext("/project", lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution("selected", work_dir=str(zone), container_work_dir="/workzone"))
    adapter = JavaAdapter(context, executor=container)
    monkeypatch.setattr(adapter, "_context_issue", lambda: "")
    monkeypatch.setattr(adapter, "execution_setup_issue", lambda: "")
    monkeypatch.setattr(adapter, "_jdk", lambda: SimpleNamespace(
        tool=lambda name: f"/opt/jdk/bin/{name}", home="/opt/jdk"))
    monkeypatch.setattr(adapter, "_scratch", lambda name: contextlib.nullcontext(str(zone)))
    catalogue = adapter.mutation_operators()
    assert catalogue.operators == ("ID_A", "ID_B"), catalogue
    assert container.calls and container.calls[0][0][0] == "/opt/jdk/bin/java"
