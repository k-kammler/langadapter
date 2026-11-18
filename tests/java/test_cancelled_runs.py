"""A Java suite run, with or without coverage, that a ``CancelScope``
cancels from another thread raises OperationCancelled within seconds with
no result, ends the test JVM, and once the prepared copy is closed leaves
no adapter scratch or receipt in the temporary directory."""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.runs import SuiteRunRequest

SLOW = """\
package demo;

import org.junit.jupiter.api.Test;

class SlowTest {{
    @Test
    void slow() throws Exception {{
        java.nio.file.Files.writeString(java.nio.file.Path.of("{pid_file}"),
                Long.toString(ProcessHandle.current().pid()));
        Thread.sleep(120000);
    }}
}}
"""


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


@pytest.mark.parametrize("coverage", [False, True], ids=["run_tests", "measure_coverage"])
def test_a_cancelled_java_suite_run_gives_no_result_and_leaves_nothing(
        java_project, jdks, maven, gradle, tmp_path, monkeypatch, coverage):
    feature = max(jdks)
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    project = java_project("maven")
    pid_file = (tmp_path / "pid.txt").as_posix()
    (project / "src" / "test" / "java" / "demo" / "SlowTest.java").write_text(
        SLOW.format(pid_file=pid_file), encoding="utf-8")
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    owner = lb.prepare_project(lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))
    scope = lb.CancelScope()
    outcome = {}

    def tool():
        try:
            with scope:
                with owner.use() as used:
                    copy = Path(used.root)
                    request = SuiteRunRequest(
                        suite=lb.Suite(str(copy / "src" / "test" / "java"),
                                       ("demo/SlowTest.java",)),
                        timeout_s=600,
                        coverage_target="src/main/java/demo/Calc.java" if coverage else None)
                    outcome["result"] = lb.create("java", used).run_suite(request)
        except BaseException as error:  # the test reads it
            outcome["error"] = error

    thread = threading.Thread(target=tool)
    thread.start()
    try:
        deadline = time.monotonic() + 600
        pid_path = Path(pid_file)
        while not (pid_path.exists() and pid_path.read_text()):
            assert time.monotonic() < deadline and thread.is_alive(), outcome
            time.sleep(0.1)
        pid = int(pid_path.read_text())
        started = time.monotonic()
        scope.cancel()
        thread.join(60)
        assert not thread.is_alive() and time.monotonic() - started < 30
    finally:
        scope.cancel()
        thread.join(120)
    assert "result" not in outcome, outcome
    assert isinstance(outcome["error"], lb.OperationCancelled), outcome
    assert not _alive(pid)
    owner.close()
    left = sorted(path.name for path in scratch.iterdir()
                  if path.name.startswith(("langadapter", "cr_")))
    assert left == []
