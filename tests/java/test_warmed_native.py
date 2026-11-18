"""Opt-in native gate for the warmed helper and controller, not public API.

The fixtures need an explicit cached JDK home and JUnit classpath and use no
dependency resolver, download, project rewrite or system settings."""
import os
from pathlib import Path
import time
import uuid

import pytest

from langadapter import _process_tree
from langadapter._owned_directory import OwnedDirectory
from langadapter.java import (
    _warmed as controller, _warmed_fork as helper, _warmed as native,
)
from langadapter.java._profile import JvmProfile
from langadapter.outcomes import Outcome


_TEMPLATES = {
    "repeat_safe": '''
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
public class RepeatedTest {
    static int executions;
    static int fixtures;
    @BeforeEach void prepare() { fixtures++; }
    @Test void repeats() {
        executions++;
        assertEquals(fixtures, executions);
        System.out.println("execution=" + executions);
    }
}
''',
    "stateful": '''
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
public class RepeatedTest {
    static int executions;
    @Test void stateful() { assertEquals(1, ++executions); }
}
''',
    "container_failure": '''
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
public class RepeatedTest {
    @BeforeAll static void brokenFixture() { throw new IllegalStateException("native fixture failure"); }
    @Test void neverReached() { throw new AssertionError("must not execute"); }
}
''',
}


@pytest.mark.parametrize("case", list(_TEMPLATES))
def test_native_warmed_helper_and_owned_controller(tmp_path, case):
    home = os.environ.get("LANGADAPTER_TEST_WARMED_JAVA_HOME")
    classpath = os.environ.get("LANGADAPTER_TEST_WARMED_CLASSPATH")
    if not home or not classpath:
        pytest.skip("requires explicit cached warmed JDK/JUnit classpath")
    java = Path(home) / "bin" / ("java.exe" if os.name == "nt" else "java")
    javac = java.with_name("javac.exe" if os.name == "nt" else "javac")
    assert java.is_file() and javac.is_file(), "explicit cached JDK must exist"
    assert all(Path(entry).is_file() for entry in classpath.split(os.pathsep)), "cached jars only"
    owned = OwnedDirectory.create(parent=str(tmp_path), receipt_parent=str(tmp_path),
                                  prefix="warmed_native_")
    owners = []
    success = False
    try:
        source = owned.root / f"{helper.HELPER_CLASS}.java"
        source.write_text(helper.HELPER_SOURCE, encoding="utf-8")
        suite = owned.root / "RepeatedTest.java"
        suite.write_text(_TEMPLATES[case], encoding="utf-8")
        compiled = _process_tree.run(
            [str(javac), "--release", str(JvmProfile.helper_release), "-cp", classpath, "-d", str(owned.root),
             str(source), str(suite)], cwd=str(owned.root), timeout=60,
            text=True, encoding="utf-8", errors="replace")
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        observations = []
        for fork in range(2 if case == "repeat_safe" else 1):
            directory = owned.root / f"fork-{fork}"
            directory.mkdir()
            protocol = native.ForkProtocol(uuid.uuid4().hex, fork, 1, 2)
            command = [str(java), "-cp", os.pathsep.join((str(owned.root), classpath)),
                       helper.HELPER_CLASS, str(directory), protocol.token, str(fork), "1", "2",
                       "RepeatedTest"]
            started = time.monotonic()
            with (directory / "stdout.log").open("wb") as stdout, (
                    directory / "stderr.log").open("wb") as stderr:
                owner = _process_tree.start(command, cwd=str(directory), stdout=stdout, stderr=stderr)
                owners.append(owner)
                result = controller.drive(
                    owner, directory, protocol, total_deadline=started + 90,
                    startup_deadline=started + 30, iteration_timeout_s=15)
            assert owner._langadapter_command_disposed is True
            assert result.transcript.finished
            assert owner.returncode == 0, (directory / "stderr.log").read_text()
            observations.append(result)
            if case == "repeat_safe":
                assert result.outcome is Outcome.COMPLETED, result.reason
                assert result.transcript.repeatable is True
                assert len(result.transcript.iterations) == 3
                assert (directory / "stdout.log").read_text().splitlines() == [
                    "execution=1", "execution=2", "execution=3"]
                assert all(item.counts.passed == 1 for item in result.transcript.iterations)
            else:
                assert result.outcome is Outcome.TEST_FAILURES, result.reason
                records = result.transcript.iterations
                assert len(records) == (2 if case == "stateful" else 1)
                assert records[-1].counts.container_failures == (1 if case == "container_failure" else 0)
                assert records[-1].counts.failed == (1 if case == "stateful" else 0)
                assert not (directory / f"run-{len(records)}.permit").exists()
        if case == "repeat_safe":
            assert observations[0].transcript.jvm != observations[1].transcript.jvm
        assert suite.read_text() == _TEMPLATES[case]
        success = True
    finally:
        if success and all(getattr(owner, "_langadapter_command_disposed", False) for owner in owners):
            owned.remove()
        else:
            owned.verify()


def test_native_selected_warmed_launch_keeps_file_output_and_jvm_options(tmp_path):
    """Prepared launch only: no public build resolver or complete benchmark claim."""
    home = os.environ.get("LANGADAPTER_TEST_WARMED_JAVA_HOME")
    classpath = os.environ.get("LANGADAPTER_TEST_WARMED_CLASSPATH")
    if not home or not classpath:
        pytest.skip("requires explicit cached warmed JDK/JUnit classpath")
    from langadapter.context import Interpreter, ProjectContext
    from langadapter.execution import executor_for
    from langadapter.runs import WarmedSuiteBenchmarkRequest
    from langadapter.java import _warmed_fork as _warmed_launch
    from langadapter.java import _tools as _junit
    from langadapter.java._env import read
    from langadapter.java._build import LAUNCHER_JARS, JavaProject
    from langadapter.suites import Suite

    jdk = read(home)
    assert jdk is not None
    jars = tuple(classpath.split(os.pathsep))
    assert all(Path(path).is_file() for path in jars)
    owned = OwnedDirectory.create(parent=str(tmp_path), receipt_parent=str(tmp_path),
                                  prefix="warmed_launch_native_")
    success = False
    try:
        source = owned.root / f"{helper.HELPER_CLASS}.java"
        source.write_text(helper.HELPER_SOURCE, encoding="utf-8")
        suite = owned.root / "OutputTest.java"
        suite.write_text('''
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
public class OutputTest {
    @Test void output() {
        assertEquals("selected", System.getProperty("fixture.value"));
        byte[] data = new byte[256 * 1024];
        java.util.Arrays.fill(data, (byte)'x');
        System.out.write(data, 0, data.length);
    }
}
''', encoding="utf-8")
        classes = owned.root / "classes"
        classes.mkdir()
        compiled = _process_tree.run(
            [jdk.tool("javac"), "--release", str(JvmProfile.helper_release), "-cp", classpath, "-d", str(classes),
             str(source), str(suite)], cwd=str(owned.root), timeout=60,
            text=True, encoding="utf-8", errors="replace")
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        context = ProjectContext(str(owned.root), Interpreter(jdk.tool("java")),
                                 interpreter_flags=("-ea",))
        project = JavaProject(context.root, "fixture", 8, (), (), (), (), jars,
                              test_jvm_args=("-Xmx96m",),
                              test_system_properties={"fixture.value": "selected"})
        request = WarmedSuiteBenchmarkRequest(Suite(str(owned.root), (str(suite),)),
            "native_lifecycle", 15, 60, startup_timeout_s=30, forks=1, warmup_runs=1, runs=2)
        channel = owned.root / "channel"
        channel.mkdir()
        result = _warmed_launch.execute(
            context, executor_for(context), project, jdk,
            # The launcher jars the run classpath leaves out come with the tools.
            _junit.Toolset("fixture", tuple(path for path in jars
                                            if Path(path).name.startswith(LAUNCHER_JARS))),
            request=request, protocol=native.ForkProtocol(uuid.uuid4().hex, 0, 1, 2),
            classes=str(classes), helper_classes=str(classes), class_names=("OutputTest",),
            channel=channel, total_deadline=time.monotonic() + 60)
        assert result.outcome is Outcome.COMPLETED
        assert result.transcript.finished
        assert all(item.counts.passed == 1 for item in result.transcript.iterations)
        assert (channel / "stdout.log").read_bytes() == b"x" * (3 * 256 * 1024)
        assert (channel / "stderr.log").read_bytes() == b""
        assert "-Xmx96m" in (channel / "launch.args").read_text()
        success = True
    finally:
        if success:
            owned.remove()
        else:
            owned.verify()
