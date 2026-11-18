"""Command admission and disposal without changing native tool results."""
import ctypes as ct
import io
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from langadapter import _process_tree as tree, _windows_job as jobs
from langadapter import execution, placement


@pytest.fixture
def controlled(monkeypatch):
    calls = []
    state = SimpleNamespace(admission=None, close=None, launch=None, execute=None)

    class Process:
        returncode = None

        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            for stream in (self.stdin, self.stdout, self.stderr):
                stream.close()
            return False

        def communicate(self, **options):
            calls.append(("communicate", options))
            if state.execute is not None:
                error, state.execute = state.execute, None
                raise error
            self.returncode = 3 if self.returncode is None else self.returncode
            return b"out", b"err"

        def poll(self):
            calls.append("poll")
            return self.returncode

        def kill(self):
            calls.append("root-kill")
            self.returncode = -9

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            assert self.returncode is not None
            return self.returncode

    process = Process()

    class Job:
        def __init__(self):
            calls.append("job-create")

        def admit_suspended(self, proc):
            assert proc is process
            calls.append("job-admit")
            if state.admission is not None:
                raise state.admission

        def stop(self, grace=10):
            calls.append(("job-stop", grace))
            if process.returncode is None:
                process.returncode = -9

        def close(self):
            calls.append("job-close")
            if state.close is not None:
                raise state.close
            if state.admission is None:
                self.stop()

    def launch(args, **options):
        calls.append(("launch", args, options))
        if state.launch is not None:
            raise state.launch
        return process

    monkeypatch.setattr(tree, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(jobs, "CommandJob", Job)
    monkeypatch.setattr(tree.subprocess, "Popen", launch)
    monkeypatch.setattr(tree.subprocess, "run", lambda *a, **k: pytest.fail("no taskkill fallback"))
    return calls, state, process


def test_command_is_suspended_and_admitted_before_use(controlled):
    calls, _, process = controlled
    env = {"EXAMPLE": "value"}
    with tree.command(["tool", "argument"], creationflags=0x08000000,
                      cwd="work", env=env) as admitted:
        assert admitted is process
        assert calls[:3] == [
            "job-create", ("launch", ["tool", "argument"], {
                "creationflags": 0x08000004, "cwd": "work", "env": env}), "job-admit"]
    assert calls[3:5] == ["job-close", ("job-stop", 10)]
    assert "root-kill" not in calls
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    assert process._langadapter_command_disposed is True


def test_primary_error_does_not_turn_failed_disposal_into_permission(controlled):
    _, state, process = controlled
    owner = tree.start(["tool"])
    original = KeyboardInterrupt("execution interrupted")
    state.close = OSError("containment refused")
    assert tree.finish(owner, primary=original) is False
    assert process._langadapter_command_disposed is False
    assert original._langadapter_command is process
    assert not process.stdout.closed
    state.close = None
    assert tree.finish(owner) is True
    assert process._langadapter_command_disposed is True


def test_a_root_exit_alone_is_no_proof_that_its_descendants_are_disposed():
    root = SimpleNamespace(wait=lambda timeout: 0, poll=lambda: 0)
    assert tree.finish(root) is False
    assert root._langadapter_command_disposed is False


@pytest.mark.parametrize("runner", [tree.run, execution.run_capture, placement.run_timed])
def test_completed_launchers_are_disposed_without_changing_results(controlled, runner):
    calls, _, _ = controlled
    result = runner(["tool"], timeout=7)
    assert (result.args, result.returncode) == (["tool"], 3)
    assert result.stdout in (b"out", "out") and result.stderr in (b"err", "err")
    assert "job-close" in calls
    assert "root-kill" not in calls
    assert calls.index("job-admit") < next(i for i, call in enumerate(calls)
                                           if isinstance(call, tuple) and call[0] == "communicate")


def test_run_preserves_input_and_captured_modes(controlled):
    calls, _, _ = controlled
    result = tree.run(["tool"], timeout=7, input=b"exact\n", text=False)
    launch = next(call[2] for call in calls if isinstance(call, tuple) and call[0] == "launch")
    assert launch["stdin"] == subprocess.PIPE
    assert launch["stdout"] == launch["stderr"] == subprocess.PIPE
    assert launch["text"] is False
    assert ("communicate", {"input": b"exact\n", "timeout": 7}) in calls
    assert result.stdout == b"out"


@pytest.mark.parametrize("error", [OSError("assignment refused"), KeyboardInterrupt("admission interrupted")])
def test_refused_admission_kills_only_its_suspended_root_and_preserves_error(controlled, error):
    calls, state, process = controlled
    state.admission = error
    with pytest.raises(type(error)) as raised:
        tree.run(["tool"], timeout=7)
    assert raised.value is error
    assert "root-kill" in calls
    assert not any(isinstance(call, tuple) and call[0] == "communicate" for call in calls)
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


def test_failed_launch_closes_the_unused_job(controlled):
    calls, state, _ = controlled
    error = FileNotFoundError("tool absent")
    state.launch = error
    with pytest.raises(FileNotFoundError) as raised:
        tree.run(["tool"], timeout=7)
    assert raised.value is error
    assert "job-close" in calls
    assert "root-kill" not in calls


def test_job_creation_failure_starts_nothing(controlled, monkeypatch):
    calls, _, _ = controlled
    error = OSError("no job available")

    def refused():
        raise error

    monkeypatch.setattr(jobs, "CommandJob", refused)
    with pytest.raises(OSError) as raised:
        tree.run(["tool"], timeout=7)
    assert raised.value is error and calls == []


def test_timeout_stops_job_then_drains_output(controlled):
    calls, state, _ = controlled
    state.execute = subprocess.TimeoutExpired(["tool"], 7, output=b"early")
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        tree.run(["tool"], timeout=7)
    assert (raised.value.cmd, raised.value.timeout) == (["tool"], 7)
    assert (raised.value.stdout, raised.value.stderr) == (b"out", b"err")
    assert ("job-stop", 10) in calls
    assert calls.index(("job-stop", 10)) < calls.index(("communicate", {
        "timeout": tree._OUTPUT_DRAIN_SECONDS}))


@pytest.mark.parametrize("error", [KeyboardInterrupt("interrupted"), OSError("read failed")])
def test_execution_error_is_not_replaced_by_failed_disposal(controlled, error):
    _, state, process = controlled
    state.execute, state.close = error, OSError("disposal uncertain")
    with pytest.raises(type(error)) as raised:
        tree.run(["tool"], timeout=7)
    assert raised.value is error
    if hasattr(error, "__notes__"):
        assert "disposal uncertain" in " ".join(error.__notes__)
    assert not any(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    assert error._langadapter_command is process


def test_uncertain_successful_disposal_is_not_success(controlled):
    _, state, _ = controlled
    error = state.close = OSError("accounting failed")
    with pytest.raises(OSError) as raised:
        tree.run(["tool"], timeout=7)
    assert raised.value is error


def test_stream_close_failure_is_not_success(controlled):
    _, _, process = controlled
    error = OSError("stream close failed")
    process.stdout = SimpleNamespace(close=lambda: (_ for _ in ()).throw(error))
    with pytest.raises(OSError) as raised:
        tree.run(["tool"], timeout=7)
    assert raised.value is error
    assert process.stdin.closed and process.stderr.closed


def test_unconfirmed_root_wait_retains_streams_and_process(controlled):
    _, _, process = controlled
    error = subprocess.TimeoutExpired("root", 10)
    process.wait = lambda timeout: (_ for _ in ()).throw(error)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        tree.run(["tool"], timeout=7)
    assert raised.value is error
    assert not any(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    assert error._langadapter_command is process


@pytest.mark.parametrize("failure", ["job", "wait", "reader"])
@pytest.mark.parametrize("primary", [False, True])
def test_failed_disposal_does_not_close_a_locked_buffered_pipe(
        controlled, monkeypatch, failure, primary):
    _, state, process = controlled
    assert tree.start(["tool"]) is process
    entered, disposing, finished = (threading.Event() for _ in range(3))
    read_fd, write_fd = os.pipe()

    class RawPipe(io.RawIOBase):
        def readable(self):
            return True

        def readinto(self, buffer):
            entered.set()  # BufferedReader holds its lock before this call.
            data = os.read(read_fd, len(buffer))
            buffer[:len(data)] = data
            return len(data)

    raw = RawPipe()
    stream = process.stdout = io.BufferedReader(raw)
    reader = threading.Thread(target=stream.read, daemon=True)
    reader.start()
    assert entered.wait(2), "the actual pipe reader must acquire its buffer lock"
    process.returncode = 0
    error = OSError("original containment failure")
    if failure == "job":
        state.close = error
    elif failure == "wait":
        process.wait = lambda timeout: (_ for _ in ()).throw(error)
    else:
        process.stdout_thread = reader
        monkeypatch.setattr(tree, "_OUTPUT_DRAIN_SECONDS", 0.05)
    original = KeyboardInterrupt("original execution interruption") if primary else None
    observed = []

    def dispose():
        disposing.set()
        try:
            tree.finish(process, primary=original)
        except BaseException as result:
            observed.append(result)
        finally:
            finished.set()

    worker = threading.Thread(target=dispose, daemon=True)
    try:
        worker.start()
        assert disposing.wait(2)
        assert finished.wait(2), "disposal must finish while the pipe writer is still open"
        assert reader.is_alive() and not stream.closed
        if primary:
            assert not observed and original._langadapter_command is process
        else:
            assert len(observed) == 1
            assert observed[0]._langadapter_command is process
            if failure != "reader":
                assert observed[0] is error
    finally:
        os.close(write_fd)
        reader.join(timeout=2)
        worker.join(timeout=2)
        assert not reader.is_alive() and not worker.is_alive()
        stream.close()
        os.close(read_fd)


def test_post_exit_drain_is_bounded_and_stops_pipe_holding_children(controlled):
    calls, state, process = controlled
    assert tree.start(["tool"]) is process
    process.returncode = 7
    state.execute = subprocess.TimeoutExpired("tool", tree._OUTPUT_DRAIN_SECONDS,
                                             output=b"partial")
    assert tree.drain_output(process) == (b"out", b"err")
    assert process.returncode == 7
    assert calls.index(("communicate", {"timeout": tree._OUTPUT_DRAIN_SECONDS})) < (
        calls.index(("job-stop", 10)))
    assert calls.count(("communicate", {"timeout": tree._OUTPUT_DRAIN_SECONDS})) == 2


def test_failed_post_exit_drain_preserves_primary_output_and_live_handles(controlled):
    _, _, process = controlled
    assert tree.start(["tool"]) is process
    process.returncode = 7
    first = subprocess.TimeoutExpired("tool", 10, output=b"partial", stderr=b"reason")
    second = subprocess.TimeoutExpired("tool", 10, output=b"later")
    errors = iter((first, second))

    def communicate(timeout):
        assert timeout == tree._OUTPUT_DRAIN_SECONDS
        raise next(errors)

    process.communicate = communicate
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        tree.drain_output(process)
    assert caught.value is first and first._langadapter_command is process
    if sys.version_info >= (3, 11):
        assert f"Command cleanup failed: {second}" in first.__notes__
    assert first.output == b"partial" and first.stderr == b"reason"
    assert process.returncode == 7
    tree.finish(process, primary=first)
    assert not any(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    process.communicate = lambda timeout: (b"all output", b"all diagnostic")
    assert tree.drain_output(process) == (b"all output", b"all diagnostic")
    tree.finish(process)
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))


def test_the_root_wait_after_its_job_stops_stays_bounded(controlled):
    _, _, process = controlled
    assert tree.start(["tool"]) is process
    waits = []

    def wait(timeout=None):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("root", timeout)

    process.wait = wait
    with pytest.raises(subprocess.TimeoutExpired):
        tree.stop(process, grace=0.01)
    assert waits == [0.01]
    del process.wait
    assert tree.finish(process)


def test_interrupted_reader_join_preserves_primary_and_streams(controlled):
    _, _, process = controlled
    assert tree.start(["tool"]) is process
    process.returncode = 0
    cleanup = KeyboardInterrupt("reader join interrupted")
    process.stdout_thread = SimpleNamespace(join=lambda **kw: (_ for _ in ()).throw(cleanup))
    primary = OSError("original execution failure")
    tree.finish(process, primary=primary)
    assert primary._langadapter_command is process
    assert not any(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    if hasattr(primary, "add_note"):
        # Exceptions carry notes from Python 3.11.
        assert "reader join interrupted" in " ".join(primary.__notes__)


def test_timeout_remains_primary_when_stop_fails(controlled, monkeypatch):
    _, state, _ = controlled
    error = state.execute = subprocess.TimeoutExpired(["tool"], 7, output=b"early", stderr=b"diagnostic")
    monkeypatch.setattr(tree, "stop", lambda proc: (_ for _ in ()).throw(OSError("stop failed")))
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        tree.run(["tool"], timeout=7)
    assert raised.value is error
    assert (error.stdout, error.stderr) == (b"early", b"diagnostic")


_NATIVE = os.name == "nt" and os.environ.get("LANGADAPTER_TEST_WINDOWS_JOB") == "1"


@pytest.mark.skipif(not _NATIVE, reason="opt-in Windows production command-job gate")
@pytest.mark.parametrize("runner", [tree.run, execution.run_capture, placement.run_timed],
                         ids=["command", "capture", "timing"])
def test_native_completed_launcher_cannot_leave_child_running(tmp_path, monkeypatch, runner):
    marker = tmp_path / "child.pid"
    child = ("import os, pathlib, sys, time; "
             "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)")
    parent = (
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "deadline=time.monotonic()+20\n"
        "while not pathlib.Path(sys.argv[2]).exists():\n"
        "    if time.monotonic()>deadline: raise RuntimeError('child did not start')\n"
        "    time.sleep(0.01)\n"
        "print('native output', flush=True)\n"
        "print('native diagnostic', file=sys.stderr, flush=True)\n"
        "sys.exit(3)\n"
    )
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ct.c_uint32, ct.c_int, ct.c_uint32]
    kernel.OpenProcess.restype = ct.c_void_p
    kernel.WaitForSingleObject.argtypes = [ct.c_void_p, ct.c_uint32]
    kernel.WaitForSingleObject.restype = ct.c_uint32
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [ct.c_void_p], ct.c_int
    observed = []
    original = jobs.CommandJob

    class ObservedJob(original):
        def close(self):
            handle = None
            if marker.is_file():
                handle = kernel.OpenProcess(0x00100000, False, int(marker.read_text()))
            try:
                assert handle, "capture the owned child's handle before disposal"
                assert self._active() >= 1, "the parent exited but its child is still active"
                super().close()
                assert kernel.WaitForSingleObject(handle, 5000) == 0
                observed.append("child stopped")
            finally:
                if handle:
                    assert kernel.CloseHandle(handle)
                if self._handle is not None:
                    super().close()

    monkeypatch.setattr(jobs, "CommandJob", ObservedJob)
    result = runner([sys.executable, "-c", parent, child, str(marker)], timeout=30)
    assert result.returncode == 3
    assert result.stdout in (b"native output\r\n", b"native output\n", "native output\n")
    assert result.stderr in (b"native diagnostic\r\n", b"native diagnostic\n", "native diagnostic\n")
    assert observed == ["child stopped"]
