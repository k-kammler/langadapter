"""Stopping a process ends the processes it started: a looping grandchild
does not outlive the stop, also on Windows, where in a virtual environment
it runs under the interpreter's launcher."""
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from langadapter import _process_tree, execution, placement

#: The grandchild: names its process id in a file, then waits forever.
CHILD = ("import os, sys, time\n"
         "with open(sys.argv[1], 'w') as handle:\n"
         "    handle.write(str(os.getpid()))\n"
         "while True:\n"
         "    time.sleep(0.05)\n")
#: The child: starts the grandchild and waits for it.
PARENT = ("import subprocess, sys\n"
          "subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n")


def _alive(pid: int) -> bool:
    """Whether the process runs (a zombie has ended)."""
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
    if Path("/proc/self/stat").exists():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _gone(pid: int, within: float = 15.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def _start_tree(tmp_path, name):
    """A started child that started a looping grandchild, and the
    grandchild's id. The caller finishes the child."""
    pid_file = tmp_path / f"{name}.pid"
    proc = _process_tree.start([sys.executable, "-c", PARENT, CHILD,
                                str(pid_file)])
    deadline = time.monotonic() + 60
    while not (pid_file.is_file() and pid_file.read_text()):
        if time.monotonic() > deadline:
            _process_tree.stop(proc)
            _process_tree.finish(proc)
            pytest.fail("the grandchild did not start")
        time.sleep(0.05)
    return proc, int(pid_file.read_text())


def test_stop_ends_the_process_and_its_looping_grandchild(tmp_path):
    proc, grandchild = _start_tree(tmp_path, "one")
    assert _alive(grandchild)

    _process_tree.stop(proc, grace=5)

    assert proc.poll() is not None
    assert _gone(grandchild)
    assert _process_tree.finish(proc)


def test_stop_all_ends_every_tree(tmp_path):
    trees = [_start_tree(tmp_path, name) for name in ("a", "b")]

    _process_tree.stop_all([proc for proc, _ in trees], grace=5)

    for proc, grandchild in trees:
        assert proc.poll() is not None
        assert _gone(grandchild)
        assert _process_tree.finish(proc)


def test_stopping_an_ended_process_is_harmless():
    proc = _process_tree.start([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)

    _process_tree.stop(proc, grace=1)

    assert proc.returncode == 0
    assert _process_tree.finish(proc)


def test_only_a_started_command_can_be_stopped():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        with pytest.raises(TypeError, match="start launched"):
            _process_tree.stop(proc, grace=1)
    finally:
        proc.wait(timeout=60)


def test_run_returns_what_subprocess_run_returns():
    result = _process_tree.run(
        [sys.executable, "-c", "import sys; print('out'); "
         "print('err', file=sys.stderr); sys.exit(3)"],
        timeout=60, text=True)

    assert result.returncode == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_run_into_files_gives_their_values_as_its_output():
    """Output copied into files as it arrives is what a capture holds."""
    code = ("import sys; sys.stdout.write('out\\n' * 50000); "
            "sys.stderr.write('err\\n' * 50000); sys.exit(3)")
    captured = _process_tree.run([sys.executable, "-c", code], timeout=60)
    copied = _process_tree.run([sys.executable, "-c", code], timeout=60,
                               into=(io.BytesIO(), io.BytesIO()))

    assert copied.returncode == captured.returncode == 3
    assert (copied.stdout, copied.stderr) == (captured.stdout, captured.stderr)
    assert copied.stdout.count(b"out") == 50000


def _run_into(argv, *, timeout):
    return _process_tree.run(argv, timeout=timeout, into=(io.BytesIO(), io.BytesIO()))


@pytest.mark.parametrize("text", [False, True])
def test_run_passes_input_without_changing_its_mode(text):
    data = "first\nsecond\n" if text else b"first\nsecond\n"
    code = ("import sys; sys.stdout.write(sys.stdin.read())" if text else
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())")

    result = _process_tree.run([sys.executable, "-c", code],
                               input=data, text=text, timeout=30)

    assert result.returncode == 0
    assert result.stdout == data


def test_run_refuses_input_and_stdin_before_starting_a_process(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("conflicting input must be refused before starting a process")

    monkeypatch.setattr(_process_tree.subprocess, "Popen", forbidden)

    with pytest.raises(ValueError, match="stdin and input"):
        _process_tree.run(["unused"], input=b"input", stdin=subprocess.PIPE,
                          timeout=30)


def test_a_run_past_its_timeout_ends_its_tree(tmp_path):
    pid_file = tmp_path / "run.pid"
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        _process_tree.run([sys.executable, "-c", PARENT, CHILD, str(pid_file)],
                          timeout=20)

    assert time.monotonic() - started < 60
    assert pid_file.is_file()
    assert _gone(int(pid_file.read_text()))


@pytest.mark.parametrize("run", [_process_tree.run, placement.run_timed, execution.run_capture,
                                 _run_into],
                         ids=["process-tree", "timing", "capture", "into-files"])
def test_timeout_ends_descendants_with_their_own_output_streams(tmp_path, run):
    pid_file = tmp_path / "independent-child.pid"
    parent = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],\n"
        "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print('started timing command', flush=True)\n"
        "print('timing diagnostic', file=sys.stderr, flush=True)\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired) as expired:
            run([sys.executable, "-c", parent, CHILD, str(pid_file)],
                timeout=10, **({"text": True} if run is _process_tree.run else {}))
        assert pid_file.is_file(), "the descendant must start before the timeout"
        assert _gone(int(pid_file.read_text()), within=3)
        for captured, expected in ((expired.value.stdout, "started timing command"),
                                   (expired.value.stderr, "timing diagnostic")):
            if isinstance(captured, bytes):
                captured = captured.decode("utf-8")
            assert expected in captured
    finally:
        # Keep a failing test from leaving its own child running.
        if pid_file.is_file():
            pid = int(pid_file.read_text())
            if _alive(pid):
                import signal

                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


@pytest.mark.skipif(sys.platform != "linux", reason="the supervisor times commands on Linux")
def test_a_supervised_command_is_timed_without_its_supervisor():
    """The supervisor's own start and stop stay outside the command's time."""
    start = time.perf_counter()
    done = _process_tree.run([sys.executable, "-c", "import time; time.sleep(0.3)"],
                             timeout=60)
    outside = time.perf_counter() - start
    assert done.returncode == 0
    assert 0.3 <= done.native_elapsed_s < outside


@pytest.mark.skipif(sys.platform == "linux", reason="only Linux commands are supervised")
def test_an_unsupervised_command_carries_no_supervisor_time():
    done = _process_tree.run([sys.executable, "-c", "pass"], timeout=60)
    assert not hasattr(done, "native_elapsed_s")


def test_a_launch_that_started_nothing_leaves_its_scope_closable():
    """A launch that fails with an ordinary error and leaves no process
    gives up its reservation, so the prepared copy closes and the error is
    reported."""
    from langadapter import _execution_scope as life

    scope = life._Scope()
    with pytest.raises(OSError) as caught:
        with scope:
            _process_tree.start(["langadapter-no-such-command-anywhere"])
    # Linux's launcher names its own closed command (_Command); elsewhere the
    # launch names a disposed owner.
    if sys.platform != "linux":
        assert caught.value._langadapter_command._langadapter_command_disposed is True
    scope.require_quiescent()


def test_finishing_the_owner_of_a_launch_that_started_nothing_succeeds():
    """The owner such a launch names (a Windows job, a stand-in elsewhere)
    is finished with nothing to wait for, on every host, so a mutation
    session holding it cleans up."""
    with pytest.raises(OSError) as caught:
        _process_tree.start(["langadapter-no-such-command-anywhere"])
    owner = (caught.value._langadapter_command if sys.platform != "linux"
             else caught.value._langadapter_linux_command)
    assert _process_tree.finish(owner) is True
    assert owner._langadapter_command_disposed is True
    error = OSError("nothing started")
    _process_tree._nothing_left(error)
    assert _process_tree.finish(error._langadapter_command) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows job admission")
def test_a_refused_admission_whose_process_is_gone_leaves_its_scope_closable(monkeypatch):
    from langadapter import _execution_scope as life, _windows_job

    def refused(self, process):
        raise OSError("admission refused")

    monkeypatch.setattr(_windows_job.CommandJob, "admit_suspended", refused)
    scope = life._Scope()
    with pytest.raises(OSError, match="admission refused") as caught:
        with scope:
            _process_tree.start([sys.executable, "-c", "pass"])
    process = caught.value._langadapter_command
    assert process.poll() is not None
    scope.require_quiescent()


def test_an_interrupted_launch_keeps_its_reservation(monkeypatch):
    """An interruption inside the launch may leave a process it never
    returned: the reservation stays."""
    from langadapter import _execution_scope as life
    from langadapter._owned_directory import OwnershipRefused

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("launch interrupted")

    monkeypatch.setattr(_process_tree.subprocess, "Popen", interrupted)
    scope = life._Scope()
    with pytest.raises(KeyboardInterrupt):
        with scope:
            _process_tree.start([sys.executable, "-c", "pass"])
    if sys.platform != "linux":
        with pytest.raises(OwnershipRefused, match="unfinished native launch"):
            scope.require_quiescent()


def test_a_cancellation_stops_what_its_operation_started_and_refuses_more():
    """When another thread (the CLI's signal handler) cancels an operation,
    each process it started is asked to end at once and disposed of, and
    OperationCancelled is raised instead of the process's result. A later
    start is refused, and a cancel after the process finished touches
    nothing."""
    import threading
    import time as clock

    from langadapter.errors import OperationCancelled

    cancellation = _process_tree.Cancellation()
    outcome = {}

    def operation():
        with _process_tree.cancellable(cancellation):
            started = clock.monotonic()
            try:
                _process_tree.run([sys.executable, "-c", "import time; time.sleep(120)"],
                                  timeout=180)
            except BaseException as error:
                outcome["error"] = error
            outcome["seconds"] = clock.monotonic() - started
            try:
                _process_tree.start([sys.executable, "-c", "pass"])
            except OperationCancelled as refused:
                outcome["refused"] = refused

    worker = threading.Thread(target=operation)
    worker.start()
    deadline = clock.monotonic() + 30
    while not cancellation._live and clock.monotonic() < deadline:
        clock.sleep(0.05)
    assert cancellation._live
    cancellation.cancel()
    worker.join(60)
    assert not worker.is_alive()
    assert outcome["seconds"] < 60 and "refused" in outcome
    assert isinstance(outcome["error"], OperationCancelled)
    assert cancellation._live == []
    cancellation.cancel()


def test_a_launch_that_left_nothing_running_carries_no_owner(tmp_path):
    """A command that cannot start leaves nothing to clean up."""
    from langadapter import recovery_owners

    with pytest.raises(OSError) as failed:
        _process_tree.start([str(tmp_path / "missing-executable")])
    assert recovery_owners(failed.value) == ()
