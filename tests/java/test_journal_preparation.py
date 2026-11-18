"""Journal preparation observes selected paths and reports partial setup
failures. Discovery compiles its helper the same way and keeps its reasons."""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from langadapter.execution import ContainerExecutor
from langadapter.java import _runs
from langadapter.outcomes import Outcome


class JournalContainer(ContainerExecutor):
    def __init__(self, zone):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone")
        self.calls = []

    def run(self, argv, **options):
        self.calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture(params=[False, True], ids=["native", "container"])
def preparation(request, tmp_path):
    calls = []
    if request.param:
        executor = JournalContainer(tmp_path)
        calls = executor.calls
    else:
        executor = SimpleNamespace(run=lambda argv, **options: (
            calls.append((argv, options)) or subprocess.CompletedProcess(argv, 0, "", "")))
    adapter = SimpleNamespace(_executor=executor, context=SimpleNamespace(root="/project"))
    jdk = SimpleNamespace(tool=lambda name: name)
    return adapter, jdk, tmp_path / "journal", calls


def test_success_keeps_selected_paths_and_listener_registration(preparation):
    adapter, jdk, directory, calls = preparation
    classpath = [str(directory.parent / "one.jar"), "/opt/lib/two.jar"]
    result = _runs.prepare_journal(adapter, jdk, classpath, directory, {"JAVA_EXTRA": "selected"})
    assert result.outcome is Outcome.COMPLETED and result.classes == str(directory)
    container = isinstance(adapter._executor, ContainerExecutor)
    translate = adapter._executor._to_container if container else str
    separator = ":" if container else os.pathsep
    expected = ["javac", "--release", "11", "-nowarn", "-d", translate(str(directory)),
                "-cp", separator.join(translate(entry) for entry in classpath),
                translate(str(directory / "LangAdapterJournal.java"))]
    assert calls == [(expected, {"cwd": "/project", "env": {"JAVA_EXTRA": "selected"}, "timeout": 120})]
    service = directory / "META-INF/services/org.junit.platform.launcher.TestExecutionListener"
    assert service.read_text(encoding="utf-8") == "LangAdapterJournal\n"


@pytest.mark.parametrize("stage", ["directory", "source", "services", "registration"])
def test_preparation_filesystem_failure_is_not_an_uncaught_or_successful_run(preparation, monkeypatch, stage):
    adapter, jdk, directory, calls = preparation
    original_mkdir, original_write = Path.mkdir, Path.write_text

    def mkdir(path, *args, **kwargs):
        if (stage == "directory" and path == directory) or (stage == "services" and path.name == "services"):
            raise OSError("journal filesystem unavailable")
        return original_mkdir(path, *args, **kwargs)

    def write(path, *args, **kwargs):
        if (stage == "source" and path.suffix == ".java") or (
                stage == "registration" and path.name == "org.junit.platform.launcher.TestExecutionListener"):
            raise OSError("journal filesystem unavailable")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "write_text", write)
    result = _runs.prepare_journal(adapter, jdk, [], directory, {})
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert result.classes is None
    assert "journal filesystem unavailable" in result.diagnostics
    assert len(calls) == (0 if stage in ("directory", "source") else 1)


def test_container_unmounted_helper_is_refused_before_writes_or_launch(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    executor = JournalContainer(zone)
    adapter = SimpleNamespace(_executor=executor, context=SimpleNamespace(root="/project"))
    result = _runs.prepare_journal(adapter, SimpleNamespace(tool=lambda name: name), [], tmp_path / "foreign", {})
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "work folder" in result.diagnostics
    assert not (tmp_path / "foreign").exists() and not executor.calls


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_discovery_helper_that_does_not_compile_keeps_its_outcome_and_reason(tmp_path, failure):
    calls = []

    def run(argv, **options):
        calls.append((argv, options))
        if failure == "timeout":
            raise subprocess.TimeoutExpired("javac", options["timeout"])
        return subprocess.CompletedProcess(argv, 1, "out\n", "error\n")

    project = SimpleNamespace(root="/project", run_classpath=lambda: ["/lib/app.jar"])
    adapter = SimpleNamespace(_executor=SimpleNamespace(run=run),
                              project=lambda: SimpleNamespace(project=project),
                              _jdk=lambda: SimpleNamespace(home="/jdk", tool=lambda name: name),
                              context=SimpleNamespace(environment={}))
    tools = SimpleNamespace(jars=("/lib/launcher.jar",))
    result = _runs.discover(adapter, tmp_path, str(tmp_path / "classes"), ["demo.CalcTest"], tools, 30)
    source = tmp_path / "LangAdapterDiscover.java"
    assert source.read_text(encoding="utf-8") == _runs._DISCOVER
    [(argv, options)] = calls
    assert argv == ["javac", "--release", "11", "-nowarn", "-d", str(tmp_path),
                    "-cp", os.pathsep.join(["/lib/launcher.jar", "/lib/app.jar"]), str(source)]
    assert (options["cwd"], options["timeout"]) == ("/project", 120)
    if failure == "exit":
        assert result == (Outcome.INFRASTRUCTURE_FAILURE, "discovery helper did not compile",
                          "out\nerror\n", None, None)
    else:
        assert result == (Outcome.INFRASTRUCTURE_FAILURE, "discovery helper compilation failed: "
                          "Command 'javac' timed out after 120 seconds", "", None, None)
