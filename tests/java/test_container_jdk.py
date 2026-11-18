"""Container toolchain discovery uses container files and POSIX tool paths."""
import os
import subprocess
from pathlib import Path

import pytest

import langadapter as lb
from langadapter import execution
from langadapter.java import _env as jdk


class JdkProbeExecutor:
    def __init__(self, version='21.0.7', *, compiler_exit=0):
        self.version = version
        self.compiler_exit = compiler_exit
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv == ["cat", "/opt/jdk/release"]:
            return subprocess.CompletedProcess(
                argv, 0, f'JAVA_VERSION="{self.version}"\n', "")
        if argv == ["/opt/jdk/bin/javac", "-version"]:
            return subprocess.CompletedProcess(argv, self.compiler_exit,
                                               "javac 21.0.7\n", "")
        raise AssertionError(f"unexpected container command: {argv!r}")


@pytest.mark.parametrize("path", ["/opt/jdk", "/opt/jdk/bin/java"])
def test_container_jdk_does_not_use_host_paths_or_executable_suffix(path):
    executor = JdkProbeExecutor()
    found = jdk.read_in_container(path, executor, environment={"JAVA_EXTRA": "selected"})
    assert found.home == "/opt/jdk"
    assert found.feature == 21
    assert found.version == "21.0.7"
    assert found.tool("java") == "/opt/jdk/bin/java"
    assert found.tool("javac") == "/opt/jdk/bin/javac"
    assert all(options["env"] == {"JAVA_EXTRA": "selected"}
               for _, options in executor.calls)


@pytest.mark.parametrize("version", ["unknown", "", "1.bad", "21.bad\nextra"])
def test_unreadable_container_version_is_missing_setup(version):
    assert jdk.read_in_container("/opt/jdk", JdkProbeExecutor(version)) is None


def test_container_jdk_requires_a_working_compiler():
    assert jdk.read_in_container("/opt/jdk", JdkProbeExecutor(compiler_exit=127)) is None


@pytest.mark.parametrize("path", ["relative/jdk", "C:/jdk/bin/java", "/opt/../jdk"])
def test_invalid_container_paths_do_not_start_commands(path):
    executor = JdkProbeExecutor()
    assert jdk.read_in_container(path, executor) is None
    assert executor.calls == []


def test_malformed_native_release_is_missing_setup(tmp_path):
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / ("javac.exe" if jdk.os.name == "nt" else "javac")).touch()
    (home / "release").write_text('JAVA_VERSION="unknown"\n', encoding="utf-8")
    assert jdk.read(str(home)) is None


def test_container_probe_uses_only_the_selected_container(monkeypatch, tmp_path):
    calls = []

    def capture(argv, **kwargs):
        calls.append(argv)
        assert argv[:2] == ["docker", "exec"]
        assert "selected-container" in argv
        assert "JAVA_EXTRA=selected" in argv
        container = argv[argv.index("selected-container") + 1:]
        return JdkProbeExecutor().run(container)

    monkeypatch.setattr(execution, "run_capture", capture)
    context = lb.ProjectContext(
        root="/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution(
            container="selected-container", work_dir=str(tmp_path)),
        environment={"JAVA_EXTRA": "selected"})
    found = jdk.read_in_container(context.interpreter.path, execution.executor_for(context),
                           environment=context.environment)
    assert found.tool("javac") == "/opt/jdk/bin/javac"
    assert len(calls) == 2


@pytest.mark.skipif(os.name == "nt", reason="requires a real Linux executor")
def test_real_linux_executor_can_probe_a_prepared_jdk(jdks):
    if not jdks:
        pytest.skip("needs LANGADAPTER_TEST_JAVA_HOMES")
    home = str(next(iter(jdks.values())))
    native = jdk.read(home)
    container = jdk.read_in_container(home, execution.LocalExecutor())
    assert container is not None
    assert (container.home, container.feature, container.version) == (
        native.home, native.feature, native.version)
    assert container.tool("javac") == native.tool("javac")


def test_real_docker_container_jdk_matches_the_selected_runtime(tmp_path):
    container = os.environ.get("LANGADAPTER_TEST_JAVA_CONTAINER")
    java = os.environ.get("LANGADAPTER_TEST_CONTAINER_JAVA")
    version = os.environ.get("LANGADAPTER_TEST_CONTAINER_JAVA_VERSION")
    if not (container and java and version):
        pytest.skip("needs an explicit prepared Java container and expected version")
    context = lb.ProjectContext(
        root="/project", interpreter=lb.Interpreter(path=java),
        execution=lb.ContainerExecution(container, work_dir=str(tmp_path)))
    found = jdk.read_in_container(java, execution.executor_for(context))
    assert found.version == version
    completed = execution.executor_for(context).run(
        [found.tool("java"), "-version"], timeout=120)
    assert completed.returncode == 0
    assert version in completed.stdout + completed.stderr


def test_real_docker_scratch_receipt_uses_the_declared_mount(tmp_path):
    container = os.environ.get("LANGADAPTER_TEST_JAVA_CONTAINER")
    zone = os.environ.get("LANGADAPTER_TEST_CONTAINER_WORK_DIR")
    if not (container and zone):
        pytest.skip("needs an explicit container and mounted host work directory")
    context = lb.ProjectContext(
        root="/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution(
            container, work_dir=zone, container_work_dir="/workzone"))
    executor = execution.executor_for(context)
    with execution.work_dir_tempdir(context, executor) as directory:
        artifact = Path(directory) / "observed.txt"
        artifact.write_text("known scratch", encoding="ascii")
        observed = executor.run(["cat", str(artifact)], timeout=120)
        assert observed.returncode == 0
        assert observed.stdout == "known scratch"
    assert not Path(directory).exists()
