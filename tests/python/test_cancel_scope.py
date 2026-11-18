"""A ``CancelScope`` cancelled from another thread stops a suite run, with or
without coverage, within seconds: OperationCancelled, no result, no test
process left and, once the prepared copy is closed, no temporary files."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.runs import SuiteRunRequest

SLOW = ("import time\n\n\n"
        "def test_slow():\n"
        "    with open({pid_file!r}, 'w') as handle:\n"
        "        handle.write(str(__import__('os').getpid()))\n"
        "    time.sleep(120)\n")


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
def test_a_cancelled_suite_run_gives_no_result_and_leaves_nothing(tmp_path, monkeypatch,
                                                                   coverage):
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(scratch))
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "target.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    pid_file = tmp_path / "pid.txt"
    (root / "tests" / "test_slow.py").write_text(
        SLOW.format(pid_file=str(pid_file)), encoding="utf-8")
    owner = lb.prepare_project(lb.ProjectContext(
        str(root), lb.Interpreter(path=sys.executable)))
    scope = lb.CancelScope()
    outcome = {}

    def tool():
        try:
            with scope:
                with owner.use() as used:
                    copy = Path(used.root)
                    request = SuiteRunRequest(
                        lb.Suite(str(copy / "tests"), ("test_slow.py",)), timeout_s=300,
                        coverage_target=str(copy / "target.py") if coverage else None)
                    outcome["result"] = lb.create("python", used).tests.run_suite(request)
        except BaseException as error:  # the test reads it
            outcome["error"] = error

    thread = threading.Thread(target=tool)
    thread.start()
    try:
        deadline = time.monotonic() + 180
        while not (pid_file.exists() and pid_file.read_text()):
            assert time.monotonic() < deadline and thread.is_alive(), outcome
            time.sleep(0.1)
        pid = int(pid_file.read_text())
        started = time.monotonic()
        scope.cancel()
        thread.join(60)
        assert not thread.is_alive() and time.monotonic() - started < 30
    finally:
        scope.cancel()
        thread.join(60)
    assert "result" not in outcome, outcome
    assert isinstance(outcome["error"], lb.OperationCancelled), outcome
    assert not _alive(pid)
    owner.close()
    assert sorted(path.name for path in scratch.iterdir()) == []


def test_a_use_a_cancel_ended_is_released_and_closed_inside_the_scope(tmp_path, monkeypatch):
    """Closing inside a cancelled scope still releases the prepared use: its
    cleanup runs outside the cancel, so the copy, inputs, receipts and lease
    go, and the cancelled use stores no bytecode."""
    from langadapter.python.project import BytecodeStore

    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(scratch))
    stores = tmp_path / "stores"
    stores.mkdir()
    store = BytecodeStore.create(parent=str(stores), receipt_parent=str(stores))
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    pid_file = tmp_path / "pid.txt"
    (root / "tests" / "test_slow.py").write_text(
        SLOW.format(pid_file=str(pid_file)), encoding="utf-8")
    owner = lb.prepare_project(lb.ProjectContext(
        str(root), lb.Interpreter(path=sys.executable)), bytecode=store)
    scope = lb.CancelScope()
    outcome = {}

    def tool():
        try:
            with scope:
                with owner.use() as used:
                    request = SuiteRunRequest(
                        lb.Suite(str(Path(used.root) / "tests"), ("test_slow.py",)),
                        timeout_s=300)
                    try:
                        lb.create("python", used).tests.run_suite(request)
                    except lb.OperationCancelled as stopped:
                        # The run's own stop, handled before the use is released.
                        outcome["stopped"] = stopped
                owner.close()
                outcome["closed"] = True
        except BaseException as error:  # the test reads it
            outcome["error"] = error

    thread = threading.Thread(target=tool)
    thread.start()
    try:
        deadline = time.monotonic() + 180
        while not (pid_file.exists() and pid_file.read_text()):
            assert time.monotonic() < deadline and thread.is_alive(), outcome
            time.sleep(0.1)
        scope.cancel()
        thread.join(60)
        assert not thread.is_alive()
    finally:
        scope.cancel()
        thread.join(60)
    try:
        assert "stopped" in outcome and outcome.get("closed"), outcome
        assert isinstance(outcome["error"], lb.OperationCancelled), outcome
        assert sorted(path.name for path in scratch.iterdir()) == []
        assert not list(Path(store.root).rglob("*.pyc"))
    finally:
        store.close()
