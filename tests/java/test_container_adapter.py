"""Java's public factory selects the declared container executor for discovery."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _linux_container


def test_public_factory_probes_only_the_selected_container(monkeypatch, tmp_path):
    calls = []

    def capture(argv, **kwargs):
        calls.append(argv)
        assert argv[:2] == ["docker", "exec"]
        assert "selected-container" in argv
        assert "JAVA_EXTRA=selected" in argv
        container = argv[argv.index("selected-container") + 1:]
        if container == ["cat", "/opt/jdk/release"]:
            return subprocess.CompletedProcess(argv, 0, 'JAVA_VERSION="21.0.7"\n', "")
        assert container == ["/opt/jdk/bin/javac", "-version"]
        return subprocess.CompletedProcess(argv, 0, "javac 21.0.7\n", "")

    def supervised(executor, argv, *, helper_python, **options):
        assert helper_python == "/control/python"
        return capture(executor._command(argv, env=options.get("env")))

    monkeypatch.setattr(_linux_container, "run", supervised)
    context = lb.ProjectContext(
        root="/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution(
            container="selected-container", work_dir=str(tmp_path), helper_python="/control/python"),
        environment={"JAVA_EXTRA": "selected"})

    found = lb.create("java", context)._jdk()

    assert found.tool("javac") == "/opt/jdk/bin/javac"
    assert len(calls) == 2


@pytest.mark.parametrize("operation", ["_jdk", "runtime_version", "project", "probe_capabilities"])
@pytest.mark.parametrize("disposed", [False, True])
def test_public_jdk_probe_does_not_lose_container_ownership(monkeypatch, tmp_path, operation, disposed):
    owner = SimpleNamespace(_closed=True, _langadapter_command_disposed=disposed)
    error = OSError("container probe failed")
    error._langadapter_command = owner

    def fail(executor, argv, **kwargs):
        if argv[0] == "cat" and Path(argv[-1]).name == ".langadapter-owner":
            return subprocess.CompletedProcess(argv, 0, Path(argv[-1]).read_text(encoding="ascii"), "")
        raise error

    monkeypatch.setattr(_linux_container, "run", fail)
    context = lb.ProjectContext("/project", lb.Interpreter(path="/opt/jdk/bin/java"),
        execution=lb.ContainerExecution("selected", work_dir=str(tmp_path),
                                        helper_python="/control/python"))
    adapter = lb.create("java", context)
    if not disposed:
        with pytest.raises(OSError) as caught:
            getattr(adapter, operation)()
        assert caught.value is error and caught.value._langadapter_command is owner
    elif operation == "runtime_version":
        assert adapter.runtime_version() is None
    elif operation == "probe_capabilities":
        assert adapter.probe_capabilities().outcome is lb.Outcome.MISSING_SETUP
    elif operation == "_jdk":
        # A probe that could not run says nothing about the JDK: no "no JDK".
        with pytest.raises(OSError, match="could not be probed") as caught:
            adapter._jdk()
        assert caught.value.__cause__ is error
    else:
        resolution = adapter.project()
        assert resolution.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
        assert "could not be probed: container probe failed" in resolution.reason
