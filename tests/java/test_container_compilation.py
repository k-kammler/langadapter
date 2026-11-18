"""Declared suite inputs, compilation failures and Linux container path handling."""
import os
import subprocess
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.execution import (
    ContainerExecutor, LocalExecutor, executor_for, work_dir_tempdir,
)
from langadapter.java import _env as jdk
from langadapter.java._build import (
    JavaProject, compile_suite, compile_tests, tool_environment,
)
from langadapter.outcomes import Outcome
from langadapter.suites import Suite


class RecordingContainer(ContainerExecutor):
    def __init__(self, work_dir):
        super().__init__("selected", work_dir=work_dir,
                         container_work_dir="/workzone")
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")


def _project(**changes):
    values = dict(root="/project", build_tool="maven", release=11,
                  main_sources=(), test_sources=(), main_classes=(),
                  test_classes=(), test_classpath=())
    values.update(changes)
    return JavaProject(**values)


def test_container_classpath_translates_each_entry_before_joining(tmp_path):
    zone = str(tmp_path)
    executor = RecordingContainer(zone)
    classes = str(tmp_path / "classes")
    helpers = str(tmp_path / "helpers")
    project = _project(test_sources=(helpers, "/project/src/test/java"),
                       test_classpath=(classes, "/opt/tools/junit.jar"))
    runtime = jdk._ContainerJdk("/opt/jdk", 21, "21.0.7")
    result = compile_tests(project, runtime, [str(tmp_path / "Example.java")],
                           str(tmp_path / "output"), executor)
    assert result.outcome is Outcome.COMPLETED
    argv, options = executor.calls[0]
    assert argv[argv.index("-cp") + 1] == "/workzone/classes:/opt/tools/junit.jar"
    assert argv[argv.index("-sourcepath") + 1] == (
        "/workzone/helpers:/project/src/test/java")
    assert argv[0] == "/opt/jdk/bin/javac"
    assert options["cwd"] == "/project"
    assert options["env"]["PATH"] == "/opt/jdk/bin:/usr/local/bin:/usr/bin:/bin"


def test_windows_classpath_entries_do_not_keep_drive_colons():
    executor = RecordingContainer("C:/zone")
    from langadapter.java._env import JavaEnv

    assert JavaEnv(executor).path_list(["C:/zone/classes", "C:/zone/lib/a.jar", "/opt/b.jar"]) == (
        "/workzone/classes:/workzone/lib/a.jar:/opt/b.jar")


def test_container_environment_uses_only_explicit_values(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "host-only-path")
    monkeypatch.setenv("HOST_PRIVATE", "not-forwarded")
    runtime = jdk._ContainerJdk("/opt/jdk", 21, "21.0.7")
    environment = tool_environment(runtime, {"SELECTED": "value", "PATH": "/tools"},
                                   executor=RecordingContainer(str(tmp_path)))
    assert environment == {"SELECTED": "value", "JAVA_HOME": "/opt/jdk",
                           "PATH": "/opt/jdk/bin:/tools"}


def test_compiler_refuses_an_unmounted_output_before_writing(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    executor = RecordingContainer(str(zone))
    output = tmp_path / "foreign-output"
    result = compile_tests(_project(), jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                           [], str(output), executor)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "mounted host work folder" in result.diagnostics
    assert not output.exists()
    assert not executor.calls


def test_compiler_output_creation_failure_is_not_a_collection_failure(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("preserved", encoding="ascii")
    executor = RecordingContainer(str(tmp_path))
    result = compile_tests(_project(), jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                           [], str(blocked / "classes"), executor)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert result.classes is None
    assert result.diagnostics
    assert blocked.read_text(encoding="ascii") == "preserved"
    assert not executor.calls


@pytest.fixture
def declared_suite(tmp_path):
    root = tmp_path / "inputs"
    (root / "data").mkdir(parents=True)
    (root / "Example.java").write_text("class Example {}", encoding="ascii")
    (root / "Helper.java").write_text("class Helper {}", encoding="ascii")
    (root / "Ignored.java").write_text("invalid unlisted source", encoding="ascii")
    (root / "data" / "value.txt").write_text("declared", encoding="ascii")
    (root / "ignored.txt").write_text("not declared", encoding="ascii")
    return Suite(str(root), ("Example.java",), ("Helper.java", "data/value.txt"))


def test_only_declared_sources_and_resources_reach_compilation(declared_suite, tmp_path):
    executor = RecordingContainer(str(tmp_path))
    output = tmp_path / "classes"
    result = compile_suite(_project(), jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                           declared_suite, str(output), executor)
    assert result.outcome is Outcome.COMPLETED
    assert result.classes == str(output)
    argv, _ = executor.calls[0]
    assert [Path(path).name for path in argv if path.endswith(".java")] == [
        "Example.java", "Helper.java"]
    assert (output / "data" / "value.txt").read_text(encoding="ascii") == "declared"
    assert not (output / "ignored.txt").exists()
    assert not (output / "Ignored.java").exists()


def test_failed_compilation_does_not_stage_resources(declared_suite, tmp_path, monkeypatch):
    executor = RecordingContainer(str(tmp_path))
    monkeypatch.setattr(executor, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 1, "", "compiler error"))
    output = tmp_path / "classes"
    result = compile_suite(_project(), jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                           declared_suite, str(output), executor)
    assert result.outcome is Outcome.COLLECTION_FAILED
    assert result.diagnostics == "compiler error"
    assert not (output / "data" / "value.txt").exists()


def test_resource_cannot_replace_compiled_output(declared_suite, tmp_path, monkeypatch):
    root = Path(declared_suite.root)
    (root / "Example.class").write_bytes(b"resource")
    suite = Suite(str(root), declared_suite.test_files, ("Example.class",))
    executor = RecordingContainer(str(tmp_path))
    output = tmp_path / "classes"

    def compile_class(argv, **kwargs):
        (output / "Example.class").write_bytes(b"compiled")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(executor, "run", compile_class)
    result = compile_suite(_project(), jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                           suite, str(output), executor)
    assert result.outcome is Outcome.COLLECTION_FAILED
    assert "replace compiled output" in result.diagnostics
    assert (output / "Example.class").read_bytes() == b"compiled"


def test_resource_staging_failure_is_an_execution_failure(declared_suite, tmp_path, monkeypatch):
    from langadapter.java import _build as project

    def cannot_copy(*args, **kwargs):
        raise PermissionError("resource destination refused")

    monkeypatch.setattr(project.shutil, "copyfile", cannot_copy)
    result = compile_suite(_project(), jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                           declared_suite, str(tmp_path / "classes"),
                           RecordingContainer(str(tmp_path)))
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert result.classes is None
    assert "resource destination refused" in result.diagnostics


def test_real_native_compilation_includes_helper_and_resource(tmp_path, jdks):
    runtime = jdk.read(str(jdks[max(jdks)]))
    assert runtime is not None
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "Example.java").write_text(
        'public class Example { public static void main(String[] a) throws Exception { '
        'System.out.print(Helper.value() + ":" + '
        'new String(Example.class.getResourceAsStream("/fixture.txt").readAllBytes(), '
        'java.nio.charset.StandardCharsets.UTF_8)); } }', encoding="ascii")
    (inputs / "Helper.java").write_text(
        'class Helper { static String value() { return "helper"; } }', encoding="ascii")
    (inputs / "fixture.txt").write_text("resource", encoding="ascii")
    (inputs / "Ignored.java").write_text("not valid Java", encoding="ascii")
    suite = Suite(str(inputs), ("Example.java",), ("Helper.java", "fixture.txt"))
    output = tmp_path / "classes"
    executor = LocalExecutor()
    result = compile_suite(_project(root=str(tmp_path)), runtime, suite,
                           str(output), executor)
    assert result.outcome is Outcome.COMPLETED, result.diagnostics
    assert (output / "Helper.class").is_file()
    assert not (output / "Ignored.class").exists()
    executed = executor.run([runtime.tool("java"), "-cp", str(output), "Example"],
                            cwd=str(tmp_path), timeout=120)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert executed.stdout == "helper:resource"


def test_real_docker_compiles_declared_sources_and_resources():
    container = os.environ.get("LANGADAPTER_TEST_JAVA_CONTAINER")
    zone = os.environ.get("LANGADAPTER_TEST_CONTAINER_WORK_DIR")
    java = os.environ.get("LANGADAPTER_TEST_CONTAINER_JAVA")
    if not (container and zone and java):
        pytest.skip("needs an explicit container JDK and mounted host work directory")
    context = lb.ProjectContext(
        root="/workzone", interpreter=lb.Interpreter(path=java),
        execution=lb.ContainerExecution(
            container, work_dir=zone, container_work_dir="/workzone"))
    executor = executor_for(context, forward_keys=("PATH", "JAVA_HOME"))
    runtime = jdk.read_in_container(java, executor)
    assert runtime is not None
    with work_dir_tempdir(context, executor, prefix="java_compile_") as directory:
        root = Path(directory)
        inputs = root / "inputs"
        inputs.mkdir()
        (inputs / "Example.java").write_text(
            'public class Example { public static void main(String[] a) { '
            'System.out.print("container-compiled"); } }', encoding="ascii")
        (inputs / "fixture.txt").write_text("declared resource", encoding="ascii")
        (inputs / "ignored.txt").write_text("not declared", encoding="ascii")
        suite = Suite(str(inputs), ("Example.java", "fixture.txt"))
        output = root / "classes"
        compiled = compile_suite(_project(root="/workzone"), runtime, suite,
                                 str(output), executor)
        assert compiled.outcome is Outcome.COMPLETED, compiled.diagnostics
        assert (output / "Example.class").is_file()
        assert (output / "fixture.txt").read_text(encoding="ascii") == "declared resource"
        assert not (output / "ignored.txt").exists()
        executed = executor.run([runtime.tool("java"), "-cp", str(output), "Example"],
                                cwd="/workzone", timeout=120)
        assert executed.returncode == 0, executed.stdout + executed.stderr
        assert executed.stdout == "container-compiled"
