"""Windows command admission and disposal, with native execution opt-in only."""
import ctypes as ct
import os
import subprocess
import sys
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from langadapter import _windows_job as jobs


class Kernel:
    """Record the documented kernel calls without starting any processes."""

    def __init__(self):
        self.calls = []
        self.error = 0
        self.fail = set()
        #: The handles NtGetNextThread gives for the command's own threads.
        self.threads = [300]
        self.resume = 1
        self.active = 0
        self.remain_active = False
        self.close_fail = set()

    def CreateJobObjectW(self, attributes, name):
        self.calls.append(("create", attributes, name))
        return 0 if "create" in self.fail else 100

    def SetInformationJobObject(self, handle, kind, pointer, size):
        flags = ct.cast(pointer, ct.POINTER(jobs._ExtendedLimits)).contents.basic.flags
        self.calls.append(("configure", handle, kind, flags, size))
        if "configure_raises" in self.fail:
            raise KeyboardInterrupt("configuration interrupted")
        return "configure" not in self.fail

    def AssignProcessToJobObject(self, handle, process):
        self.calls.append(("assign", handle, process))
        return "assign" not in self.fail

    def NtGetNextThread(self, process, thread, access, attributes, flags, pointer):
        """ntdll's walk over one process's threads: "enumeration" fails it,
        "second" fails it after the first thread."""
        self.calls.append(("next_thread", process, thread, access))
        if "enumeration" in self.fail or ("second" in self.fail and thread is not None):
            return ct.c_int32(0xC0000022).value
        index = 0 if thread is None else self.threads.index(thread) + 1
        if index >= len(self.threads):
            return jobs._STATUS_NO_MORE_ENTRIES
        ct.cast(pointer, ct.POINTER(jobs._HANDLE)).contents.value = self.threads[index]
        return 0

    def ResumeThread(self, handle):
        self.calls.append(("resume", handle))
        return self.resume

    def QueryInformationJobObject(self, handle, kind, pointer, size, returned):
        self.calls.append(("query", handle, kind, size, returned))
        ct.cast(pointer, ct.POINTER(jobs._Accounting)).contents.active = self.active
        return "query" not in self.fail

    def TerminateJobObject(self, handle, code):
        self.calls.append(("terminate", handle, code))
        if "terminate" in self.fail:
            return 0
        if not self.remain_active:
            self.active = 0
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return handle not in self.close_fail


@pytest.fixture
def kernel(monkeypatch):
    kernel = Kernel()
    monkeypatch.setattr(jobs, "_last_error", lambda: kernel.error)
    monkeypatch.setattr(jobs, "_ntdll", lambda: kernel)
    return kernel


def names(kernel):
    return [call[0] for call in kernel.calls]


def test_job_configuration_has_no_breakaway_and_does_not_inherit_handles(kernel):
    with closing(jobs.CommandJob(kernel=kernel)):
        assert kernel.calls[:2] == [
            ("create", None, None),
            ("configure", 100, 9, jobs._KILL_ON_CLOSE, ct.sizeof(jobs._ExtendedLimits)),
        ]
    assert names(kernel)[-2:] == ["query", "close"]
    assert "terminate" not in names(kernel)


def test_admission_precedes_thread_resume_and_releases_each_owned_handle(kernel):
    with closing(jobs.CommandJob(kernel=kernel)) as job:
        job.admit_suspended(SimpleNamespace(pid=42, _handle=400))
        assert names(kernel) == [
            "create", "configure", "assign", "next_thread", "next_thread", "resume", "close",
        ]
        # Only the command's own process is asked for its threads.
        assert ("next_thread", 400, None, jobs._THREAD_RESUME) in kernel.calls
        assert ("next_thread", 400, 300, jobs._THREAD_QUERY) in kernel.calls
        assert ("assign", 100, 400) in kernel.calls
    assert [c[1] for c in kernel.calls if c[0] == "close"] == [300, 100]


@pytest.mark.parametrize("fail,match", [
    ("create", "creation"), ("configure", "configuration"),
    ("assign", "admission"), ("enumeration", "enumeration"),
])
def test_failed_prerequisite_never_resumes_a_command(kernel, fail, match):
    kernel.fail.add(fail)
    with pytest.raises(OSError, match=match):
        with closing(jobs.CommandJob(kernel=kernel)) as job:
            job.admit_suspended(SimpleNamespace(pid=42, _handle=400))
    assert "resume" not in names(kernel)
    if fail != "create":
        assert ("close", 100) in kernel.calls


def test_interrupted_configuration_closes_its_job(kernel):
    kernel.fail.add("configure_raises")
    with pytest.raises(KeyboardInterrupt, match="configuration interrupted"):
        jobs.CommandJob(kernel=kernel)
    assert kernel.calls[-1] == ("close", 100)


def test_partial_thread_enumeration_is_not_unique_admission(kernel):
    kernel.fail.add("second")
    with pytest.raises(OSError, match="enumeration"):
        with closing(jobs.CommandJob(kernel=kernel)) as job:
            job.admit_suspended(SimpleNamespace(pid=42, _handle=400))
    assert "resume" not in names(kernel)
    assert ("close", 300) in kernel.calls


@pytest.mark.parametrize("threads", [[], [300, 301]])
def test_missing_or_ambiguous_primary_thread_is_refused(kernel, threads):
    kernel.threads = threads
    with pytest.raises(OSError, match="unique primary thread"):
        with closing(jobs.CommandJob(kernel=kernel)) as job:
            job.admit_suspended(SimpleNamespace(pid=42, _handle=400))
    assert "resume" not in names(kernel)
    closed = [call[1] for call in kernel.calls if call[0] == "close"]
    assert set(threads) <= set(closed)


def test_a_second_threads_close_failure_still_closes_the_first(kernel):
    kernel.threads = [300, 301]
    kernel.close_fail.add(301)
    with pytest.raises(OSError, match="thread handle close"):
        with closing(jobs.CommandJob(kernel=kernel)) as job:
            job.admit_suspended(SimpleNamespace(pid=42, _handle=400))
    assert "resume" not in names(kernel)
    assert ("close", 300) in kernel.calls


@pytest.mark.parametrize("result", [0, 2, 0xFFFFFFFF])
def test_unexpected_suspend_count_fails_closed(kernel, result):
    kernel.resume = result
    with pytest.raises(OSError, match="resume"):
        with closing(jobs.CommandJob(kernel=kernel)) as job:
            job.admit_suspended(SimpleNamespace(pid=42, _handle=400))
    assert ("close", 300) in kernel.calls


@pytest.mark.parametrize("operation,handle,match", [
    ("second", 300, "enumeration"),
    ("configure", 100, "configuration"),
    ("resume", 300, "resume"),
])
def test_cleanup_failure_does_not_replace_admission_failure(kernel, operation, handle, match):
    if operation == "resume":
        kernel.resume = 0xFFFFFFFF
    else:
        kernel.fail.add(operation)
    kernel.close_fail.add(handle)
    with pytest.raises(OSError, match=match) as raised:
        with closing(jobs.CommandJob(kernel=kernel)) as job:
            job.admit_suspended(SimpleNamespace(pid=42, _handle=400))
    if sys.version_info >= (3, 11):
        assert any("cleanup failed" in note for note in raised.value.__notes__)


def test_thread_close_failure_after_resume_is_not_success(kernel):
    kernel.close_fail.add(300)
    kernel.active = 2
    with pytest.raises(OSError, match="thread handle close"):
        with closing(jobs.CommandJob(kernel=kernel)) as job:
            job.admit_suspended(SimpleNamespace(pid=42, _handle=400))
    assert ("terminate", 100, 1) in kernel.calls


def test_active_children_are_stopped_even_when_the_parent_has_ended(kernel):
    process = SimpleNamespace(pid=42, _handle=400, returncode=3)
    with closing(jobs.CommandJob(kernel=kernel)) as job:
        job.admit_suspended(process)
        kernel.active = 2
    assert names(kernel)[-4:] == ["query", "terminate", "query", "close"]
    assert process.returncode == 3
    assert kernel.active == 0


@pytest.mark.parametrize("operation,match", [("query", "accounting"), ("terminate", "termination")])
def test_failed_quiescence_check_cannot_become_successful_cleanup(kernel, operation, match):
    kernel.active = 2
    kernel.fail.add(operation)
    job = jobs.CommandJob(kernel=kernel)
    with pytest.raises(OSError, match=match):
        job.close()
    assert job._handle is None
    assert ("close", 100) in kernel.calls
    kernel.fail.clear()
    with pytest.raises(OSError, match=match):
        job.close()


def test_stop_deadline_requires_observed_zero_active_processes(kernel):
    kernel.active = 2
    kernel.remain_active = True
    with closing(jobs.CommandJob(kernel=kernel)) as job:
        with pytest.raises(OSError, match="still has active processes"):
            job.stop(grace=0)
        kernel.active = 0


@pytest.mark.parametrize("grace", [-1, float("nan"), float("inf")])
def test_invalid_stop_deadline_is_rejected_without_kernel_queries(kernel, grace):
    with closing(jobs.CommandJob(kernel=kernel)) as job:
        count = len(kernel.calls)
        with pytest.raises(ValueError, match="finite and nonnegative"):
            job.stop(grace=grace)
        assert len(kernel.calls) == count


def test_failed_close_can_be_retried_without_losing_its_handle(kernel):
    job = jobs.CommandJob(kernel=kernel)
    kernel.close_fail.add(100)
    with pytest.raises(OSError, match="job handle close"):
        job.close()
    assert job._handle == 100
    kernel.close_fail.clear()
    job.close()
    count = len(kernel.calls)
    job.close()
    assert len(kernel.calls) == count


def test_job_accepts_only_one_command_and_cannot_reopen(kernel):
    job = jobs.CommandJob(kernel=kernel)
    process = SimpleNamespace(pid=42, _handle=400)
    with closing(job):
        job.admit_suspended(process)
        with pytest.raises(RuntimeError, match="already admitted"):
            job.admit_suspended(process)
    for operation in (lambda: job.admit_suspended(process), job.stop):
        with pytest.raises(RuntimeError, match="closed"):
            operation()
    assert names(kernel).count("assign") == 1


def test_windows_structure_widths_do_not_depend_on_host_long_width():
    assert ct.sizeof(jobs._DWORD) == ct.sizeof(jobs._LONG) == 4
    assert ct.sizeof(jobs._Accounting) == 48
    if ct.sizeof(ct.c_void_p) == 8:
        assert ct.sizeof(jobs._Limits) == 64
        assert ct.sizeof(jobs._ExtendedLimits) == 144


def test_kernel_binds_each_required_signature(monkeypatch):
    functions = [
        "CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject",
        "ResumeThread", "QueryInformationJobObject", "TerminateJobObject", "CloseHandle",
    ]
    kernel = SimpleNamespace(**{name: SimpleNamespace() for name in functions})
    calls = []
    monkeypatch.setattr(jobs.ct, "WinDLL", lambda *a, **k: (calls.append((a, k)) or kernel),
                        raising=False)
    assert jobs._kernel() is kernel
    assert calls == [(("kernel32",), {"use_last_error": True})]
    for name in functions:
        assert getattr(kernel, name).argtypes
        assert getattr(kernel, name).restype is not None


def test_ntdll_binds_the_thread_walk(monkeypatch):
    """The thread walk visits one process's threads, not every thread on
    the machine."""
    ntdll = SimpleNamespace(NtGetNextThread=SimpleNamespace())
    calls = []
    monkeypatch.setattr(jobs.ct, "WinDLL", lambda *a, **k: (calls.append((a, k)) or ntdll),
                        raising=False)
    assert jobs._ntdll() is ntdll
    assert calls == [(("ntdll",), {})]
    assert len(ntdll.NtGetNextThread.argtypes) == 6
    assert ntdll.NtGetNextThread.restype is jobs._LONG


_NATIVE = os.name == "nt" and os.environ.get("LANGADAPTER_TEST_WINDOWS_JOB") == "1"


@pytest.mark.skipif(not _NATIVE, reason="opt-in Windows command-job gate")
@pytest.mark.parametrize("parent_exits", [True, False], ids=["parent-exit", "interruption"])
def test_real_windows_job_stops_its_owned_child(tmp_path, parent_exits):
    """Use only this job's handles; never enumerate or stop other processes."""
    marker = tmp_path / "child.pid"
    child = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)"
    )
    parent = (
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "deadline=time.monotonic()+20\n"
        "while not pathlib.Path(sys.argv[2]).exists():\n"
        "    if time.monotonic()>deadline: raise RuntimeError('child did not start')\n"
        "    time.sleep(0.01)\n"
        "print('child started', flush=True)\n"
        + ("sys.exit(3)\n" if parent_exits else "time.sleep(120)\n")
    )
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ct.c_uint32, ct.c_int, ct.c_uint32]
    kernel.OpenProcess.restype = ct.c_void_p
    kernel.WaitForSingleObject.argtypes = [ct.c_void_p, ct.c_uint32]
    kernel.WaitForSingleObject.restype = ct.c_uint32
    kernel.CloseHandle.argtypes = [ct.c_void_p]
    kernel.CloseHandle.restype = ct.c_int
    process = None
    child_handle = None
    job = jobs.CommandJob()
    original = KeyboardInterrupt("controlled interruption")
    try:
        try:
            with closing(job):
                process = subprocess.Popen(
                    [sys.executable, "-c", parent, child, str(marker)],
                    creationflags=jobs.CREATE_SUSPENDED,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                assert process.poll() is None
                assert not marker.exists()
                job.admit_suspended(process)
                deadline = time.monotonic() + 30
                while not (marker.is_file() and marker.read_text()):
                    if time.monotonic() >= deadline or process.poll() not in (None, 3):
                        pytest.fail("owned child did not start")
                    time.sleep(0.01)
                child_handle = kernel.OpenProcess(0x00100000, False, int(marker.read_text()))
                assert child_handle
                if parent_exits:
                    stdout, stderr = process.communicate(timeout=30)
                    assert process.returncode == 3
                    assert stdout.strip() == "child started"
                    assert not stderr
                    assert job._active() >= 1
                else:
                    raise original
        except KeyboardInterrupt as error:
            assert not parent_exits and error is original
        assert job._handle is None
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
        if parent_exits:
            assert process.returncode == 3
        else:
            process.communicate(timeout=10)
            assert process.returncode is not None
    finally:
        try:
            job.close()
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
            if child_handle:
                assert kernel.CloseHandle(child_handle)
