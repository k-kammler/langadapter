"""The host side of the Linux command supervisor (``_linux_job``): the owner
of its helper handle and of its private control channel.

The channel (``_Control``) owns only its own control directory. It neither
signals a reported PID nor establishes completion without the owner's
helper handle, and is no sandbox against writers outside LangAdapter. The
native command's streams and exit status stay apart from the control
protocol. Container commands start through ``_Command.start_in_container`` with a
runner launch the caller supplies (``_linux_container``).
"""
from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import time
from typing import Mapping, Optional, Sequence
from langadapter._linux_job import (
    ControlError,
    _READ_SETTLE_SECONDS,
    _settle,
    SupervisionUnavailable,
)
from langadapter._owned_directory import OwnedDirectory
import contextlib
import math
from pathlib import Path
import subprocess
import sys
import threading
import warnings
from langadapter import _execution_scope


_LIMIT = 2 * 1024 * 1024
_REPLACE_SECONDS = 0.5
_REPORT_ATTEMPTS = 4
#: The helper's lease, and the bounds of its stop (terminate, then kill).
_LEASE_SECONDS = 60
_GRACE_SECONDS = 5
_KILL_WAIT_SECONDS = 5
_FIELDS = {"version", "token", "helper_pid", "state", "command_pid", "returncode",
           "quiescent", "reason"}
_STATES = {"READY", "RUNNING", "EXITED", "COMPLETE", "CANCELLED", "FAILED"}


def _note(error: BaseException, cleanup: BaseException) -> None:
    if hasattr(error, "add_note"):
        error.add_note(f"Control cleanup failed: {cleanup}")


def _close_failed_open(descriptor: int, error: BaseException) -> None:
    try:
        os.close(descriptor)
    except BaseException as cleanup:
        _note(error, cleanup)


def _pid(value) -> bool:
    return type(value) is int and 0 < value <= 2147483647


def _report(record: dict, *, token: str, helper_pid: Optional[int] = None) -> dict:
    """The helper's report, checked for what the host relies on: the format
    version, the token, the helper's pid, a known state and every field.
    Only this package's own staged helper writes it. ``completed`` confirms a
    terminal state against the helper's exit code."""
    if (not isinstance(record, dict)
            or set(record) - {"elapsed_ns", "supervision"} not in (_FIELDS, _FIELDS | {"error"})
            or type(record["version"]) is not int or record["version"] != 1
            or record["token"] != token or not _pid(record["helper_pid"])
            or (helper_pid is not None and record["helper_pid"] != helper_pid)
            or not isinstance(record["state"], str) or record["state"] not in _STATES):
        raise ControlError("Invalid supervisor report identity or format")
    return record


class _Control:
    """One private request and its monotonically advancing host lease. The
    command owner serializes pulse and cancel. This class launches no
    helper, sends no signal, starts no thread and removes nothing on failure.
    Host and container may map the same channel at different paths."""

    def __init__(self, owned: OwnedDirectory) -> None:
        owned.verify()
        self.owned = owned
        self.generation = 0

    @classmethod
    def create(cls, argv: Sequence[str], *, cwd: Optional[str] = None,
               parent: Optional[str] = None, receipt_parent: Optional[str] = None,
               native_environment: Optional[Mapping[str, str]] = None):
        # The helper uses its request as written: the caller's parts are checked here.
        if (isinstance(argv, (str, bytes)) or not argv
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv)):
            raise ControlError("Invalid native command arguments")
        if cwd is not None and (not isinstance(cwd, str) or not cwd or "\0" in cwd):
            raise ControlError("Invalid native working directory")
        if native_environment is not None and (not isinstance(native_environment, dict) or any(
                not isinstance(key, str) or not key or "=" in key or "\0" in key
                or not isinstance(value, str) or "\0" in value
                for key, value in native_environment.items())):
            raise ControlError("Invalid captured native environment")
        request = {"version": 1, "token": "", "argv": list(argv),
                   "cwd": cwd, "lease_seconds": _LEASE_SECONDS,
                   "grace": _GRACE_SECONDS, "kill_wait": _KILL_WAIT_SECONDS}
        if native_environment is not None:
            request["native_environment"] = dict(native_environment)
        owned = OwnedDirectory.create(parent=parent, receipt_parent=receipt_parent,
                                      prefix="langadapter-command-")
        try:
            control = cls(owned)
            request["token"] = owned.token
            control._write("request.json", request)
            control.pulse()
            return control
        except BaseException as error:
            error._langadapter_control = owned
            raise

    def _write(self, name: str, record: dict, *, durable: bool = True) -> None:
        if name not in {"request.json", "lease.json", "cancel.json"}:
            raise ControlError("Unknown host control file")
        data = json.dumps(record, ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(data) > _LIMIT:
            raise ControlError("Host control record exceeds its size bound")
        self.owned.verify()
        descriptor, temporary = tempfile.mkstemp(prefix=".control-", dir=self.owned.root)
        primary = None
        try:
            try:
                stream = os.fdopen(descriptor, "wb")
            except BaseException as error:
                _close_failed_open(descriptor, error)
                raise
            with stream:
                stream.write(data)
                stream.flush()
                if durable:
                    os.fsync(stream.fileno())
            deadline = time.monotonic() + _REPLACE_SECONDS
            while True:
                self.owned.verify()
                try:
                    os.replace(temporary, self.owned.root / name)
                    break
                except OSError as error:
                    remaining = deadline - time.monotonic()
                    if getattr(error, "winerror", None) not in {5, 32, 33} or remaining <= 0:
                        raise
                    # A mapped Windows reader may temporarily deny atomic replacement.
                    time.sleep(min(0.01, remaining))
            self.owned.verify()
        except BaseException as error:
            primary = error
            error._langadapter_control = self.owned
            raise
        finally:
            try:
                self.owned.verify()
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except BaseException as cleanup:
                if primary is None:
                    raise
                _note(primary, cleanup)

    def pulse(self) -> None:
        # Liveness needs no durability, and an fsync can wait on unrelated
        # writeback beyond the lease. Atomic replacement keeps reads whole.
        self._write("lease.json", {"token": self.owned.token, "generation": self.generation},
                    durable=False)
        self.generation += 1

    def cancel(self) -> None:
        self._write("cancel.json", {"token": self.owned.token})

    def observe(self, *, helper_pid: Optional[int] = None) -> Optional[dict]:
        if helper_pid is not None and not _pid(helper_pid):
            raise ControlError("Invalid expected helper identity")
        path = self.owned.root / "status.json"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        deadline = time.monotonic() + _READ_SETTLE_SECONDS
        changes = settles = 0
        while changes < _REPORT_ATTEMPTS:
            self.owned.verify()
            try:
                before = path.lstat()
            except FileNotFoundError:
                return None
            if (not stat.S_ISREG(before.st_mode) or before.st_size > _LIMIT
                    or getattr(path, "is_junction", lambda: False)()):
                raise ControlError("Supervisor report must be a bounded regular file")
            try:
                descriptor = os.open(path, flags)
            except FileNotFoundError as error:
                # The report existed at lstat. Its replacement is in progress.
                _settle(deadline, settles, error)
                settles += 1
                continue
            try:
                stream = os.fdopen(descriptor, "rb")
            except BaseException as error:
                _close_failed_open(descriptor, error)
                raise
            with stream:
                opened = os.fstat(stream.fileno())
                try:
                    current = path.lstat()
                except FileNotFoundError as error:
                    _settle(deadline, settles, error)
                    settles += 1
                    continue
                if (not stat.S_ISREG(opened.st_mode) or opened.st_size > _LIMIT
                        or not stat.S_ISREG(current.st_mode) or current.st_size > _LIMIT
                        or getattr(path, "is_junction", lambda: False)()):
                    raise ControlError("Supervisor report must be a bounded regular file")
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    # Published by atomic rename: never read an obsolete or
                    # aliased descriptor, open the current file again.
                    self.owned.verify()
                    changes += 1
                    continue
                try:
                    data = stream.read(_LIMIT + 1)
                except OSError as error:
                    if error.errno != errno.ENODATA:
                        raise
                    _settle(deadline, settles, error)
                    settles += 1
                    continue
            self.owned.verify()
            if len(data) > _LIMIT:
                raise ControlError("Supervisor report exceeded its size bound")
            try:
                record = json.loads(data)
            except (ValueError, UnicodeError) as error:
                raise ControlError("Unreadable supervisor report") from error
            return _report(record, token=self.owned.token, helper_pid=helper_pid)
        raise ControlError("Supervisor report publication did not settle within its read bound")

    def completed(self, *, helper_returncode: int, helper_pid: Optional[int] = None) -> dict:
        """The terminal report, validated against ``helper_returncode``. The
        caller must first wait for its helper handle (container route included):
        an integer alone proves no process stopped. Nothing is removed here."""
        record = self.observe(helper_pid=helper_pid)
        if (type(helper_returncode) is not int or record is None
                or record["state"] not in {"COMPLETE", "CANCELLED", "FAILED"}
                or not record["quiescent"]
                or helper_returncode != (1 if record["state"] == "FAILED" else 0)):
            raise ControlError("Helper exit does not confirm owned command completion")
        return record


_OPTIONS = {"cwd", "env", "stdin", "stdout", "stderr", "text", "encoding",
            "errors", "bufsize", "close_fds", "universal_newlines"}
_INTERVAL = 0.1
#: Seconds a supervisor has from its own start to report the native command
#: started. A helper that exits fails at once, and the bound fails one that
#: lives without admitting. A start on a loaded host can take over 15 s.
STARTUP_BOUND_S = 60.0
#: The native command's own start failures, as the supervisor reports them.
_START_FAILURES = {"FileNotFoundError": FileNotFoundError, "PermissionError": PermissionError,
                   "NotADirectoryError": NotADirectoryError}


def _seconds(value, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 120):
        raise ControlError(f"Invalid supervisor {name}")
    return float(value)


def _supported_host() -> bool:
    return sys.platform == "linux"


_LOCAL_SUPERVISION: list = []


def local_supervision():
    """How this host supervises local commands, decided once here since the
    helper is this interpreter. None off Linux."""
    if not _supported_host():
        return None
    if not _LOCAL_SUPERVISION:
        from langadapter._linux_job import _pidfd_works
        _LOCAL_SUPERVISION.append("pidfd" if _pidfd_works() else "process_group")
    return _LOCAL_SUPERVISION[0]


class _Command:
    """The owner of one helper handle, with serialized control writes and
    bounded disposal. It never signals a reported PID or kills the adoption
    helper. A failed disposal keeps the handle and channel for the same
    owner to retry. Native work has only the caller's timeout, setup and
    cleanup their own bounds. The heartbeat leaves the native environment
    unchanged."""

    def __init__(self, args: Sequence[str], control: _Control, *, cleanup: float):
        self.args = list(args)
        self.control = control
        self.helper = None
        self.pid = None
        self.returncode = None
        #: The command's own lifetime as its supervisor timed it, once known.
        self.native_elapsed_ns = None
        #: How the supervisor supervised it, once reported.
        self.supervision = None
        self._record = None
        self._output = None
        self._cleanup = cleanup
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._heartbeat_error = None
        #: The longest pause between two of this process's lease renewals
        #: and the process CPU spent during it, and the slowest renewal, in
        #: seconds: a lease expiry names them (``_lease_note``).
        self._renewal_pause = (0.0, 0.0)
        self._slowest_renewal = 0.0
        self._cancelled = False
        self._closed = False
        self._container = False
        self._cwd = None
        self._helper_identity = None
        self._unfinished_raised = False
        #: The helper never started: its Popen raised an error, which leaves
        #: no process, so nothing of the launch runs.
        self._unstarted = False
        self._heartbeat = threading.Thread(target=self._beat, daemon=True,
                                            name="langadapter-command-lease")
        _execution_scope.track(self, "Linux command owner")

    @classmethod
    def start(cls, args: Sequence[str], *, startup: float = STARTUP_BOUND_S, cleanup: float = 30,
              parent: Optional[str] = None, receipt_parent: Optional[str] = None,
              **options):
        if not _supported_host():
            raise SupervisionUnavailable("Local supervision requires Linux")
        startup, cleanup = _seconds(startup, "startup bound"), _seconds(cleanup, "cleanup bound")
        if set(options) - _OPTIONS:
            raise ControlError("Unsupported supervised process options: "
                               + ", ".join(sorted(set(options) - _OPTIONS)))
        if isinstance(args, (str, bytes)):
            raise ControlError("Native argv must be a string sequence")
        args = list(args)
        cwd = options.get("cwd")
        control = _Control.create(args, cwd=os.fspath(cwd) if cwd is not None else None,
                                  parent=parent, receipt_parent=receipt_parent)
        owner = cls(args, control, cleanup=cleanup)
        owner._cwd = os.fspath(cwd) if cwd is not None else None
        try:
            owner._heartbeat.start()
            helper = Path(__file__).with_name("_linux_job.py")
            # Isolated and without site, as the container helper: it needs only the
            # standard library, and no environment's .pth files may run in it.
            owner.helper = subprocess.Popen(
                [sys.executable, "-I", "-S", str(helper), str(control.owned.root),
                 control.owned.token],
                start_new_session=True, **options)
            owner._admitted(startup)
            return owner
        except BaseException as error:
            owner._retain(error)
            # An error, not an interruption, before the helper existed left
            # nothing running. An interruption inside Popen may leave a
            # process it never returned, so its owner keeps the control.
            owner._unstarted = owner.helper is None and isinstance(error, Exception)
            owner.close(primary=error)
            raise

    @classmethod
    def start_in_container(cls, args: Sequence[str], *, launch, cwd: Optional[str],
                    parent: str, startup: float = STARTUP_BOUND_S, cleanup: float = 30,
                    native_environment=None, **options):
        """Start ``args`` through a container helper in a mapped channel, which
        reports READY before it starts the command. ``launch`` receives the
        staged helper and channel and returns the runner's handle, whose host
        PID is never taken for the helper's container identity."""
        startup, cleanup = _seconds(startup, "startup bound"), _seconds(cleanup, "cleanup bound")
        if set(options) - (_OPTIONS - {"cwd", "env"}):
            raise ControlError("Unsupported process options for the container")
        control = _Control.create(args, cwd=cwd, parent=parent,
                                  native_environment=native_environment)
        owner = cls(args, control, cleanup=cleanup)
        owner._container = True
        owner._cwd = cwd
        try:
            helper = control.owned.root / "supervisor.py"
            with helper.open("xb") as stream:
                stream.write(Path(__file__).with_name("_linux_job.py").read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            control.owned.verify()
            owner._heartbeat.start()
            owner.helper = launch(helper, control.owned.root, control.owned.token, options)
            owner._admitted(startup)
            return owner
        except BaseException as error:
            owner._retain(error)
            owner.close(primary=error)
            raise

    def _observe(self):
        expected = self._helper_identity if self._container else self.helper.pid
        record = self.control.observe(helper_pid=expected)
        if record is not None and self._container and self._helper_identity is None:
            self._helper_identity = record["helper_pid"]
        return record

    def _terminal(self, code):
        expected = self._helper_identity if self._container else self.helper.pid
        record = self.control.completed(helper_returncode=code, helper_pid=expected)
        if self._container and self._helper_identity is None:
            self._helper_identity = record["helper_pid"]
        return record

    @property
    def stdin(self):
        return self.helper.stdin

    @property
    def stdout(self):
        return self.helper.stdout

    @property
    def stderr(self):
        return self.helper.stderr

    def _retain(self, error: BaseException) -> None:
        error._langadapter_linux_command = self
        error._langadapter_control = self.control.owned

    def _write(self, operation) -> None:
        if not self._lock.acquire(timeout=1):
            raise ControlError("Supervisor control writer did not become available")
        try:
            operation()
        finally:
            self._lock.release()

    def _beat(self) -> None:
        last = (time.monotonic(), time.process_time())
        try:
            while not self._stopped.wait(1):
                # A control write of the caller can hold the writer through a
                # storage stall: this beat then gives way to the next one.
                if not self._lock.acquire(timeout=1):
                    continue
                try:
                    if not self._stopped.is_set():
                        began = time.monotonic()
                        self.control.pulse()
                        now, cpu = time.monotonic(), time.process_time()
                        self._slowest_renewal = max(self._slowest_renewal, now - began)
                        if now - last[0] > self._renewal_pause[0]:
                            self._renewal_pause = (now - last[0], cpu - last[1])
                        last = (now, cpu)
                finally:
                    self._lock.release()
        except BaseException as error:
            self._heartbeat_error = error
            self._stopped.set()

    def _lease_note(self, reason) -> str:
        """For a lease expiry, this process's renewal record: a pause with
        about as much process CPU means a busy thread held the interpreter,
        one with almost none that the process did not run, a slow renewal
        that storage stalled."""
        if reason != "host control lease expired":
            return ""
        pause, cpu = self._renewal_pause
        return (f" (this process: longest pause between lease renewals {pause:.1f} s, "
                f"{cpu:.1f} s of its CPU meanwhile; slowest renewal {self._slowest_renewal:.1f} s)")

    def _health(self):
        if self._heartbeat_error is not None:
            self._retain(self._heartbeat_error)
            raise self._heartbeat_error
        record = self._observe()
        if record is not None and record["state"] == "FAILED":
            error = self._failure(record)
            error._langadapter_supervisor_report = record
            self._retain(error)
            raise error
        return record

    def _start_failure(self, record):
        """The native command's own start failure, or None: its program or
        working directory is missing or not executable, and no native process
        exists. The supervisor reports it only once nothing it owns remains,
        so it is an ordinary start failure, as a local launch raises it."""
        failure = record.get("error") or {}
        kind = _START_FAILURES.get(failure.get("type"))
        name = failure.get("filename")
        if (kind is None or record["state"] != "FAILED" or record["command_pid"] is not None
                or name is None or name not in (self.args[0], self._cwd)):
            return None
        number = failure.get("errno")
        if isinstance(number, int) and not isinstance(number, bool):
            return kind(number, os.strerror(number), name)
        return kind(failure.get("message") or "the native command could not start")

    @staticmethod
    def _refused(record) -> bool:
        """Whether the supervisor refused at its preflight (an interpreter
        older than 3.10, no child adoption) and reports no child and nothing
        left running: a refusal, not an unresolved owner."""
        failure = record.get("error") or {}
        return (record["state"] == "FAILED" and record["command_pid"] is None
                and record.get("quiescent") is True
                and failure.get("type") == "SupervisionUnavailable")

    def _failure(self, record):
        start = self._start_failure(record)
        if start is not None:
            return start
        failure = record["error"]
        kind = (SupervisionUnavailable if record["command_pid"] is None
                and failure["type"] == "SupervisionUnavailable" else ControlError)
        return kind("Supervisor failed: " + failure["message"])

    def _admitted(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            record = self._health()
            if record is not None and record.get("supervision") is not None:
                # The mode is known from the helper's readiness on.
                self.supervision = record["supervision"]
            if record is not None and record["command_pid"] is not None:
                self.pid = record["command_pid"]
                if self.helper.poll() is not None:
                    self._completed()
                return
            if self.helper.poll() is not None:
                if self._container and record is None:
                    raise SupervisionUnavailable("The container's helper exited without a readiness handshake")
                self._completed()
                if self._record["command_pid"] is not None:
                    # A fast command run and reported between the last
                    # health read and this check of the helper.
                    return
                raise ControlError("The supervisor stopped before it admitted the command")
            if time.monotonic() >= deadline:
                raise ControlError("The supervisor did not admit the command within the setup "
                                   "time limit")
            time.sleep(min(_INTERVAL, max(0.0, deadline - time.monotonic())))

    def _native_exit(self, record) -> int:
        pid, code = record["command_pid"], record["returncode"]
        if ((self.pid is not None and self.pid != pid)
                or (self.returncode is not None and self.returncode != code)):
            raise ControlError("Supervisor native identity or exit changed after observation")
        self.returncode, self.pid = code, pid
        return code

    def _completed(self) -> int:
        # A terminal marker alone does not stop the helper or its adopted children.
        code = self.helper.wait(timeout=0)
        self._stop_heartbeat()
        if self._heartbeat_error is not None:
            raise self._heartbeat_error
        record = self._terminal(code)
        self._record = record
        self.native_elapsed_ns = record.get("elapsed_ns")
        self.supervision = record.get("supervision")
        return self._outcome(record)

    def _outcome(self, record) -> int:
        """The native exit a terminal report establishes, or its failure."""
        if record["state"] == "FAILED":
            raise self._failure(record)
        if record["state"] == "CANCELLED" and (
                not self._cancelled or record["returncode"] is None
                or record["reason"] != "cancelled"):
            # Raised to whoever waits for the command, so its disposal later
            # need not raise it again.
            self._unfinished_raised = True
            raise ControlError("Native execution did not complete: " + record["reason"]
                               + self._lease_note(record["reason"]))
        return self._native_exit(record)

    def poll(self):
        if self._closed:
            # Disposal confirmed and the channel removed: the terminal report
            # read at close is final.
            return self._outcome(self._record)
        try:
            return self._settled()
        except BaseException as error:
            self._retain(error)
            raise

    def _settled(self):
        """The command's exit once the helper ended or reported it, else None."""
        record = self._health()
        if self.helper.poll() is not None:
            return self._completed()
        if record is not None and record["state"] in {"EXITED", "COMPLETE"}:
            return self._native_exit(record)
        return None

    def _remaining(self, deadline, timeout, output=None, stderr=None) -> float:
        if deadline is None:
            return _INTERVAL
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(self.args, timeout, output=output, stderr=stderr)
        return min(_INTERVAL, remaining)

    def wait(self, timeout=None):
        if self._closed:
            return self._outcome(self._record)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        try:
            while True:
                settled = self._settled()
                if settled is not None:
                    return settled
                interval = self._remaining(deadline, timeout)
                try:
                    self.helper.wait(timeout=interval)
                except subprocess.TimeoutExpired:
                    continue
                return self._completed()
        except BaseException as error:
            self._retain(error)
            raise

    def communicate(self, input=None, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        first, output, stderr = True, None, None
        previously_exited = self.returncode is not None
        drain_deadline = ((deadline if deadline is not None else time.monotonic() + self._cleanup)
                          if previously_exited else None)
        try:
            while True:
                record = self._health()
                if record is not None and record["state"] in {"EXITED", "COMPLETE"}:
                    self._native_exit(record)
                    if drain_deadline is None:
                        self._remaining(deadline, timeout, output, stderr)
                        drain_deadline = time.monotonic() + self._cleanup
                if drain_deadline is not None:
                    remaining = drain_deadline - time.monotonic()
                    if remaining <= 0:
                        if previously_exited and timeout is not None:
                            raise subprocess.TimeoutExpired(self.args, timeout,
                                                            output=output, stderr=stderr)
                        self._langadapter_output_unconfirmed = True
                        raise ControlError("Native command exited but supervised output disposal did not finish")
                    interval = min(_INTERVAL, remaining)
                else:
                    interval = self._remaining(deadline, timeout, output, stderr)
                pending = input if first else None
                first = False
                try:
                    output, stderr = self.helper.communicate(input=pending, timeout=interval)
                except subprocess.TimeoutExpired as error:
                    output, stderr = error.output, error.stderr
                    continue
                self._output = (output, stderr)
                self._completed()
                return output, stderr
        except BaseException as error:
            self._retain(error)
            if not isinstance(error, subprocess.TimeoutExpired):
                error.output, error.stderr = output, stderr
            raise

    def cancel(self) -> None:
        if self._closed:
            # Nothing it owned remains to cancel.
            return
        self._cancelled = True
        self._write(self.control.cancel)

    def _stop_heartbeat(self) -> None:
        self._stopped.set()
        if self._heartbeat.is_alive():
            # A renewal blocked in a storage stall finishes its write first.
            # The cleanup bound, not one second, limits that wait.
            self._heartbeat.join(timeout=self._cleanup)
            if self._heartbeat.is_alive():
                raise ControlError("Supervisor heartbeat did not stop")

    def _remove_control(self) -> None:
        """Remove the control directory and its receipt after confirmed disposal,
        when the helper has exited with a terminal report and the heartbeat
        has stopped. A failed removal keeps both and warns. The disposal
        stands."""
        owned = self.control.owned
        try:
            owned.remove()
        except Exception as error:
            with contextlib.suppress(Exception):
                warnings.warn(f"supervisor control directory retained: {owned.root}; "
                              f"ownership receipt: {owned.receipt}; "
                              f"{type(error).__name__}: {error}",
                              RuntimeWarning, stacklevel=3)

    def close(self, *, primary=None) -> bool:
        if self._closed:
            _execution_scope.completed(self)
            return True
        failures = []
        # A command that ended without completing (its lease expired) under a
        # terminal report, so nothing it owned remains: its own failure.
        unfinished = None
        try:
            self._stop_heartbeat()
        except BaseException as error:
            failures.append(error)
        # A command that never started has no helper to settle: only its
        # heartbeat and control directory remain.
        if not self._unstarted:
            try:
                if self.helper is None or self.helper.poll() is None:
                    self.cancel()
                if self.helper is None:
                    raise ControlError("Supervisor startup has no acquired process handle")
                code = self.helper.wait(timeout=self._cleanup)
                self._record = self._terminal(code)
                started = (self._start_failure(self._record) is None
                           and not self._refused(self._record))
                if self._record["state"] == "FAILED" and started:
                    raise ControlError("Supervisor failed: " + self._record["error"]["message"])
                if self._record["state"] == "COMPLETE":
                    self._native_exit(self._record)
                elif self._record["command_pid"] is not None:
                    if not self._cancelled or self._record["reason"] != "cancelled":
                        unfinished = ControlError(
                            "Native execution did not complete: " + self._record["reason"]
                            + self._lease_note(self._record["reason"]))
                    else:
                        self._native_exit(self._record)
                elif primary is None and started:
                    raise ControlError("The supervisor stopped before it admitted the command")
            except BaseException as error:
                failures.append(error)
        if self._heartbeat_error is not None:
            failures.append(self._heartbeat_error)
        if getattr(self, "_langadapter_output_unconfirmed", False):
            failures.append(ControlError("Command output draining remains unconfirmed"))
        if not failures and self._container:
            from langadapter import _process_tree
            try:
                if self._output is None and self._record["command_pid"] is not None:
                    output = self.helper.communicate(timeout=self._cleanup)
                    if self._terminal(code) != self._record:
                        raise ControlError("The container's terminal report changed while draining output")
                    self._output = output
                if primary is not None and self._output is not None:
                    primary.output, primary.stderr = self._output
                if _process_tree.finish(self.helper) is not True:
                    raise ControlError("The container's runner disposal remains unconfirmed")
            except BaseException as error:
                failures.append(error)
        if not failures:
            streams = (() if self.helper is None
                       else (self.helper.stdin, self.helper.stdout, self.helper.stderr))
            for stream in streams:
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as error:
                        failures.append(error)
            self._closed = not failures
        if failures and unfinished is not None:
            failures.insert(0, unfinished)
        if failures:
            error = primary if primary is not None else failures[0]
            self._retain(error)
            if primary is not None:
                for failure in failures:
                    _note(primary, failure)
            else:
                for failure in failures[1:]:
                    _note(error, failure)
                raise error
            return False
        # The marker every confirmed disposal carries (``_process_tree``).
        self._langadapter_command_disposed = True
        _execution_scope.completed(self)
        self._remove_control()
        if unfinished is not None:
            if primary is None:
                self._retain(unfinished)
                raise unfinished
            if str(primary) != str(unfinished):
                _note(primary, unfinished)
        return True
