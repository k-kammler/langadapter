"""The process-group route of POSIX hosts other than Windows and Linux,
forced on Linux with real processes: a command leads its own group,
stopping, cancelling and disposal end the group, a failed launch leaves
nothing, and the observations record unconfirmed cleanup. A descendant
that leaves the group is not seen, the route's stated limit."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import langadapter as lb
from langadapter import _linux_command, _process_tree
from langadapter import _process_tree as _posix_group
from langadapter.errors import OperationCancelled
from langadapter.runs import SuiteRunRequest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="forces the POSIX route with Linux processes")


@pytest.fixture(autouse=True)
def route(monkeypatch):
    """The process-group route in place of the Linux supervisor, its
    warning already given (the tests turn warnings into errors)."""
    monkeypatch.setattr(_posix_group, "applies", lambda: True)
    monkeypatch.setattr(_linux_command, "local_supervision", lambda: None)
    warned = threading.Event()
    warned.set()
    monkeypatch.setattr(_posix_group, "_WARNED", warned)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is gone for every purpose here.
    status = Path(f"/proc/{pid}/stat")
    return status.exists() and status.read_text().split(") ")[-1][0] != "Z"


def _group_empty(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


def _wait_for(path: Path, seconds: float = 10.0) -> str:
    deadline = time.monotonic() + seconds
    while not (path.exists() and path.read_text().strip()):
        assert time.monotonic() < deadline, f"{path} was not written"
        time.sleep(0.05)
    return path.read_text().strip()


def test_a_command_runs_as_the_leader_of_its_own_group_and_warns(monkeypatch):
    monkeypatch.setattr(_posix_group, "_WARNED", threading.Event())
    with pytest.warns(RuntimeWarning, match="cleanup is not confirmed"):
        completed = _process_tree.run(["sh", "-c", "echo $$; ps -o pgid= -p $$"], timeout=30)
    pid, pgid = completed.stdout.split()
    assert completed.returncode == 0 and pid == pgid and int(pgid) != os.getpgrp()


def test_stopping_a_command_ends_its_whole_group(tmp_path):
    marker = tmp_path / "child.pid"
    proc = _process_tree.start(["sh", "-c", f"sleep 300 & echo $! > {marker}; wait"])
    child = int(_wait_for(marker))
    assert _posix_group.is_group_command(proc) and _alive(child)
    _process_tree.stop(proc, grace=2)
    assert _process_tree.finish(proc) is True
    assert not _alive(child) and _group_empty(proc.pid)


def test_a_descendant_that_ignores_sigterm_ends_with_the_group(tmp_path):
    marker = tmp_path / "child.pid"
    proc = _process_tree.start(["sh", "-c", f"(trap '' TERM; sleep 300) & echo $! > {marker}; wait"])
    child = int(_wait_for(marker))
    _process_tree.stop(proc, grace=1)
    assert _process_tree.finish(proc) is True
    assert not _alive(child)


def test_communicate_starts_no_output_reader_for_the_disposal_to_join():
    """POSIX communicate starts no reader threads, so the disposal closes
    the streams without joining any."""
    with _process_tree.command(["sh", "-c", "sleep 1; echo 1"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE) as proc:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.communicate(timeout=0.05)
        assert proc.communicate(timeout=30)[0].strip() == b"1"
        assert not hasattr(proc, "stdout_thread") and not hasattr(proc, "stderr_thread")


def test_disposal_ends_what_is_left_after_the_leader_exits(tmp_path):
    marker = tmp_path / "child.pid"
    with _process_tree.command(["sh", "-c", f"sleep 300 & echo $! > {marker}"]) as proc:
        proc.wait(timeout=30)
        child = int(_wait_for(marker))
        assert _alive(child)
    assert not _alive(child) and _group_empty(proc.pid)


def test_a_timed_out_run_ends_the_group(tmp_path):
    marker = tmp_path / "child.pid"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _process_tree.run(["sh", "-c", f"sleep 300 & echo $! > {marker}; wait"], timeout=1)
    assert time.monotonic() - started < 60
    assert not _alive(int(_wait_for(marker)))


def test_a_cancel_ends_the_group_and_the_operation_is_cancelled(tmp_path):
    marker = tmp_path / "child.pid"
    cancellation = _process_tree.Cancellation()
    with _process_tree.cancellable(cancellation):
        with pytest.raises(OperationCancelled):
            with _process_tree.command(["sh", "-c", f"sleep 300 & echo $! > {marker}; wait"]) as proc:
                child = int(_wait_for(marker))
                cancellation.cancel()
                proc.wait(timeout=30)
    assert not _alive(child) and _group_empty(proc.pid)


def test_a_launch_that_fails_leaves_nothing_and_is_finished():
    with pytest.raises(FileNotFoundError) as failed:
        _process_tree.start(["/no/such/program"])
    owner = failed.value._langadapter_command
    assert owner._langadapter_nothing_launched is True and owner._langadapter_command_disposed is True
    assert _process_tree.finish(owner) is True


def test_a_descendant_that_leaves_the_group_is_not_seen(tmp_path):
    """The stated limit: a new session is outside the group, so disposal
    neither ends nor sees it, and the observations say cleanup is not
    confirmed."""
    marker = tmp_path / "escaped.pid"
    script = (f"import os, subprocess; p = subprocess.Popen(['sleep', '300'], start_new_session=True); "
              f"open({str(marker)!r}, 'w').write(str(p.pid))")
    with _process_tree.command([sys.executable, "-c", script]) as proc:
        proc.wait(timeout=30)
    escaped = int(_wait_for(marker))
    try:
        assert _alive(escaped) and _group_empty(proc.pid)
    finally:
        os.kill(escaped, signal.SIGKILL)


def test_a_suite_run_records_the_unconfirmed_supervision(tmp_path):
    (tmp_path / "test_one.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    run = adapter.tests.run_suite(SuiteRunRequest(lb.Suite(str(tmp_path), ("test_one.py",)), 120))
    assert run.outcome is lb.Outcome.COMPLETED and run.counts.passed == 1
    assert run.identity.execution.supervision == "process_group_unconfirmed"
