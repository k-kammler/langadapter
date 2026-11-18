"""Private Windows containment for commands created suspended: an unnamed job
that permits no breakaway and kills its processes on handle close. Admission
must finish before the suspended command can start children. It adopts no
running process and manages no container children.
"""
from __future__ import annotations

import ctypes as ct
import math
import time
from typing import Any


CREATE_SUSPENDED = 0x00000004
_KILL_ON_CLOSE = 0x00002000
_THREAD_RESUME = 0x00000002
_THREAD_QUERY = 0x00000800
_STATUS_NO_MORE_ENTRIES = ct.c_int32(0x8000001A).value
_DWORD = ct.c_uint32
_LONG = ct.c_int32
_HANDLE = ct.c_void_p


class _Limits(ct.Structure):
    _fields_ = [
        ("process_time", ct.c_int64), ("job_time", ct.c_int64),
        ("flags", _DWORD), ("min_working_set", ct.c_size_t),
        ("max_working_set", ct.c_size_t), ("process_limit", _DWORD),
        ("affinity", ct.c_size_t), ("priority", _DWORD), ("scheduling", _DWORD),
    ]


class _ExtendedLimits(ct.Structure):
    _fields_ = [
        ("basic", _Limits), ("io", ct.c_uint64 * 6),
        ("process_memory", ct.c_size_t), ("job_memory", ct.c_size_t),
        ("peak_process_memory", ct.c_size_t), ("peak_job_memory", ct.c_size_t),
    ]


class _Accounting(ct.Structure):
    _fields_ = [
        ("user_time", ct.c_int64), ("kernel_time", ct.c_int64),
        ("period_user_time", ct.c_int64), ("period_kernel_time", ct.c_int64),
        ("page_faults", _DWORD), ("total", _DWORD),
        ("active", _DWORD), ("terminated", _DWORD),
    ]


def _kernel():
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([_HANDLE, ct.c_wchar_p], _HANDLE),
        "SetInformationJobObject": ([_HANDLE, ct.c_int, _HANDLE, _DWORD], ct.c_int),
        "AssignProcessToJobObject": ([_HANDLE, _HANDLE], ct.c_int),
        "ResumeThread": ([_HANDLE], _DWORD),
        "QueryInformationJobObject": ([_HANDLE, ct.c_int, _HANDLE, _DWORD, _HANDLE], ct.c_int),
        "TerminateJobObject": ([_HANDLE, ct.c_uint], ct.c_int),
        "CloseHandle": ([_HANDLE], ct.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def _ntdll():
    """NtGetNextThread, which walks one process's threads (kept since Vista).
    The documented route, a snapshot of all threads, took about 0.3 s per timed command."""
    ntdll = ct.WinDLL("ntdll")
    ntdll.NtGetNextThread.argtypes = [_HANDLE, _HANDLE, _DWORD, _DWORD, _DWORD,
                                      ct.POINTER(_HANDLE)]
    ntdll.NtGetNextThread.restype = _LONG
    return ntdll


def _last_error() -> int:
    return getattr(ct, "get_last_error", lambda: 0)()


def _failed(operation: str) -> OSError:
    error = _last_error()
    return OSError(error, f"Windows command containment {operation} failed")


def _cleanup_note(error: BaseException, cleanup: BaseException) -> None:
    if hasattr(error, "add_note"):
        error.add_note(f"Command containment cleanup failed: {cleanup}")


class CommandJob:
    """One factory-created job owning all its handles. ``admit_suspended``
    accepts only a command its caller created with CREATE_SUSPENDED. A refused
    admission must not fall back to an uncontained run. Normal exit requires
    observing zero active processes, not a parent PID check."""

    def __init__(self, *, kernel: Any = None, ntdll: Any = None) -> None:
        self._kernel = _kernel() if kernel is None else kernel
        self._ntdll = _ntdll() if ntdll is None else ntdll
        self._handle = self._kernel.CreateJobObjectW(None, None)
        self._admitted = False
        self._closed_error = None
        if not self._handle:
            raise _failed("creation")
        limits = _ExtendedLimits()
        limits.basic.flags = _KILL_ON_CLOSE
        try:
            if not self._kernel.SetInformationJobObject(
                    self._handle, 9, ct.byref(limits), ct.sizeof(limits)):
                raise _failed("configuration")
        except BaseException as error:
            try:
                if not self._kernel.CloseHandle(self._handle):
                    raise _failed("job handle close")
            except BaseException as cleanup:
                _cleanup_note(error, cleanup)
            self._handle = None
            raise error

    def admit_suspended(self, process: Any) -> None:
        if self._handle is None or self._admitted:
            raise RuntimeError("command job is closed or already admitted")
        if not self._kernel.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise _failed("admission")
        self._admitted = True
        thread = self._suspended_thread(int(process._handle))
        try:
            if self._kernel.ResumeThread(thread) != 1:
                raise _failed("resume")
        except BaseException as error:
            try:
                if not self._kernel.CloseHandle(thread):
                    raise _failed("thread handle close")
            except BaseException as cleanup:
                _cleanup_note(error, cleanup)
            raise
        else:
            if not self._kernel.CloseHandle(thread):
                raise _failed("thread handle close")

    def _next_thread(self, process: int, thread: Any, access: int):
        """The thread after ``thread`` (None: the first), None when no more."""
        found = _HANDLE()
        status = self._ntdll.NtGetNextThread(process, thread, access, 0, 0, ct.byref(found))
        if status == _STATUS_NO_MORE_ENTRIES:
            return None
        if status != 0 or not found.value:
            raise OSError(f"Windows command thread enumeration failed "
                          f"(status {status & 0xFFFFFFFF:#010x})")
        return found.value

    def _suspended_thread(self, process: int):
        """The suspended command's one thread, opened for resuming."""
        thread = self._next_thread(process, None, _THREAD_RESUME)
        if thread is None:
            raise OSError("Windows suspended command has no unique primary thread")
        try:
            other = self._next_thread(process, thread, _THREAD_QUERY)
            if other is not None:
                if not self._kernel.CloseHandle(other):
                    raise _failed("thread handle close")
                raise OSError("Windows suspended command has no unique primary thread")
        except BaseException as error:
            try:
                if not self._kernel.CloseHandle(thread):
                    raise _failed("thread handle close")
            except BaseException as cleanup:
                _cleanup_note(error, cleanup)
            raise
        return thread

    def _active(self) -> int:
        accounting = _Accounting()
        if not self._kernel.QueryInformationJobObject(
                self._handle, 1, ct.byref(accounting), ct.sizeof(accounting), None):
            raise _failed("accounting")
        return accounting.active

    def terminate(self) -> None:
        """Ask every process of the job to end now, without waiting (``stop`` waits)."""
        if self._handle is None:
            return
        if not self._kernel.TerminateJobObject(self._handle, 1):
            raise _failed("termination")

    def stop(self, grace: float = 10.0) -> None:
        if self._handle is None:
            raise RuntimeError("command job is closed")
        if not math.isfinite(grace) or grace < 0:
            raise ValueError("command stop grace must be finite and nonnegative")
        if not self._active():
            return
        if not self._kernel.TerminateJobObject(self._handle, 1):
            raise _failed("termination")
        deadline = time.monotonic() + grace
        while self._active():
            if time.monotonic() >= deadline:
                raise OSError("Windows command job still has active processes")
            time.sleep(0.01)

    def close(self) -> None:
        if self._handle is None:
            if self._closed_error is not None:
                raise self._closed_error
            return
        try:
            self.stop()
        except BaseException as error:
            try:
                if not self._kernel.CloseHandle(self._handle):
                    raise _failed("job handle close")
                self._handle = None
                self._closed_error = error
            except BaseException as cleanup:
                _cleanup_note(error, cleanup)
            raise
        else:
            if not self._kernel.CloseHandle(self._handle):
                raise _failed("job handle close")
            self._handle = None
