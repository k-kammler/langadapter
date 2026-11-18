"""Private child accounting for a separate, single-threaded Linux supervisor.

Never instantiate this in the application's process. The supervisor adopts only
its command's orphaned descendants. It is not a sandbox against external
services. This module is standard-library-only so a selected container helper can
use it independently of the project's environment.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Mapping, Optional, Sequence

_REPORT_REPLACE_SECONDS = 0.5
_REPORT_REPLACE_ATTEMPTS = 51
_READ_SETTLE_SECONDS = 0.5
_READ_SETTLE_ATTEMPTS = 51


class SupervisionUnavailable(RuntimeError):
    """The selected helper lacks the facilities required for child accounting."""


class ChildAccountingError(RuntimeError):
    """Owned process termination or native exit accounting is unconfirmed."""


class ControlError(RuntimeError):
    """The command's private control channel cannot confirm its lifetime."""


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        call = libc.prctl
    except AttributeError as error:
        raise OSError("Linux prctl facility is unavailable") from error
    call.restype = ctypes.c_int
    if call(ctypes.c_int(option), ctypes.c_size_t(value), ctypes.c_size_t(0),
            ctypes.c_size_t(0), ctypes.c_size_t(0)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _children() -> tuple[int, ...]:
    with Path(f"/proc/self/task/{os.getpid()}/children").open("rb") as stream:
        return tuple(int(value) for value in stream.read().split())


def _pidfd_works() -> bool:
    """Whether this interpreter and kernel provide process descriptors: a
    Python built without them, an older kernel or a container's system call
    filter can each withhold them."""
    if (not callable(getattr(os, "pidfd_open", None))
            or not callable(getattr(signal, "pidfd_send_signal", None))):
        return False
    try:
        descriptor = os.pidfd_open(os.getpid(), 0)
    except OSError:
        return False
    try:
        signal.pidfd_send_signal(descriptor, 0, None, 0)
    except OSError:
        return False
    finally:
        os.close(descriptor)
    return True


def _childless() -> bool:
    """Whether the supervisor has no child, False when that cannot be read.
    A helper that already has a child before the preflight refuses before
    it has a command."""
    try:
        return not _children()
    except Exception:
        return False


def _prepare() -> str:
    """Check the supervisor and enable child adoption. The supervision
    mode: "pidfd" with process descriptors, else "process_group"."""
    if sys.platform != "linux" or sys.version_info < (3, 10):
        raise SupervisionUnavailable("Supervision requires Linux and Python 3.10+")
    if not all(callable(getattr(os, name, None)) for name in ("waitid", "killpg")) or not all(
            callable(getattr(signal, name, None)) for name in ("pthread_sigmask", "sigtimedwait")):
        raise SupervisionUnavailable("Supervision requires Linux waitid and signal masks")
    try:
        tasks = tuple(Path("/proc/self/task").iterdir())
        if len(tasks) != 1 or tasks[0].name != str(os.getpid()):
            raise SupervisionUnavailable("Supervisor must be single-threaded")
        try:
            existing = _children()
        except OSError as error:
            raise SupervisionUnavailable(
                f"Supervision requires procfs child accounting "
                f"(/proc/<pid>/task/<tid>/children): {error}") from error
        if existing:
            raise SupervisionUnavailable("Supervisor must start without children")
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise SupervisionUnavailable("Supervisor requires default child exit disposition")
        try:
            os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            pass
        else:
            raise SupervisionUnavailable("Supervisor already owns a child")
        # Only this dedicated process changes adoption and SIGCHLD disposition.
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        _prctl(36, 1)  # PR_SET_CHILD_SUBREAPER
        value = ctypes.c_int()
        _prctl(37, ctypes.addressof(value))  # PR_GET_CHILD_SUBREAPER
        if value.value != 1:
            raise SupervisionUnavailable("Linux did not enable child adoption")
        return "pidfd" if _pidfd_works() else "process_group"
    except (OSError, ValueError, ChildAccountingError) as error:
        raise SupervisionUnavailable(f"Linux supervision unavailable: {error}") from error


class _OwnedCommand:
    """One command and all children adopted by this dedicated supervisor.

    In "pidfd" mode procfs membership is used only to obtain owned pidfds
    for signaling. An empty/racy procfs listing is never accepted as
    completion: waitpid's ECHILD is the final accounting observation. No
    other thread/handler may create, wait for or automatically reap this
    process's children.

    Without process descriptors ("process_group" mode) the command leads its
    own session and process group. Its exit is observed without reaping it,
    so the group's ID stays reserved and the group can be signaled. The
    other children are signaled by PID while they are unreaped, which keeps
    their PIDs reserved too, and the command is reaped last.
    """

    _BATCH = 64
    _OBSERVATION_SECONDS = 0.02

    def __init__(self) -> None:
        self.mode = _prepare()
        self.process: Optional[subprocess.Popen] = None
        self.returncode: Optional[int] = None
        self.quiescent = True
        self._started = False
        # The command's own lifetime, from just before it starts to the
        # observation of its exit, timed here so that this supervisor's start
        # and stop stay outside it, as a plain subprocess timing would.
        self._started_ns: Optional[int] = None
        self._ended_ns: Optional[int] = None
        self._exit_fd: Optional[int] = None
        self._leader_reaped = False

    def start(self, argv: Sequence[str], *, cwd: Optional[str] = None,
              env: Optional[Mapping[str, str]] = None) -> None:
        if self._started:
            raise RuntimeError("A supervisor owns only one command")
        if (isinstance(argv, (str, bytes)) or not argv
                or any(not isinstance(value, str) or "\0" in value for value in argv)):
            raise ValueError("Native argv must be a nonempty string sequence")
        self._started = True
        self.quiescent = False
        # Inherit the helper's exact streams. Never mix control into tool output.
        self._started_ns = time.monotonic_ns()
        if self.mode == "process_group":
            self.process = subprocess.Popen(argv, cwd=cwd, env=env, close_fds=True,
                                            start_new_session=True)
            # Blocked only now, so the command starts with the usual mask. A
            # child's exit then wakes the wait for the command's exit.
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCHLD})
            return
        self.process = subprocess.Popen(argv, cwd=cwd, env=env, close_fds=True)
        try:
            self._exit_fd = os.pidfd_open(self.process.pid, 0)
        except (AttributeError, OSError):
            self._exit_fd = None

    @property
    def elapsed_ns(self) -> Optional[int]:
        """The command's own lifetime, once its exit was observed."""
        if self._started_ns is None or self._ended_ns is None:
            return None
        return self._ended_ns - self._started_ns

    def wait_exit(self, seconds: float) -> None:
        """Wait up to ``seconds``, ending early when the command exits."""
        if self._exit_fd is not None and self.returncode is None:
            select.select([self._exit_fd], [], [], seconds)
        elif self.mode == "process_group" and self.returncode is None:
            signal.sigtimedwait((signal.SIGCHLD,), seconds)
        else:
            time.sleep(seconds)

    def _close_exit_fd(self) -> None:
        if self._exit_fd is not None:
            os.close(self._exit_fd)
            self._exit_fd = None

    def _reap(self, *, deadline: Optional[float] = None) -> bool:
        self.quiescent = False
        end = time.monotonic() + self._OBSERVATION_SECONDS
        if deadline is not None:
            end = min(end, deadline)
        if self.mode == "process_group":
            return self._reap_group(end)
        for _ in range(self._BATCH):
            if time.monotonic() >= end:
                return False
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except InterruptedError:
                continue
            except ChildProcessError:
                if self.process is not None and self.returncode is None:
                    raise ChildAccountingError("Native command exit status was lost")
                self.quiescent = True
                return True
            if pid == 0:
                return False
            if self.process is not None and pid == self.process.pid:
                if self._ended_ns is None:
                    self._ended_ns = time.monotonic_ns()
                self._close_exit_fd()
                self.returncode = os.waitstatus_to_exitcode(status)
                self.process.returncode = self.returncode
        return False

    def _reap_group(self, end: float) -> bool:
        leader = self.process.pid if self.process is not None else None
        if leader is not None and self.returncode is None:
            try:
                exited = os.waitid(os.P_PID, leader, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except InterruptedError:
                return False
            except ChildProcessError:
                # No longer this supervisor's child: its PID and group ID are
                # not reserved, so the group is never signaled again.
                self._leader_reaped = True
                raise ChildAccountingError("Native command exit status was lost")
            if exited is not None:
                self._ended_ns = time.monotonic_ns()
                self.returncode = (exited.si_status if exited.si_code == os.CLD_EXITED
                                   else -exited.si_status)
                self.process.returncode = self.returncode
        others = [pid for pid in _children() if pid != leader]
        for index, pid in enumerate(others):
            if index >= self._BATCH or time.monotonic() >= end:
                return False
            try:
                os.waitpid(pid, os.WNOHANG)
            except (InterruptedError, ChildProcessError):
                continue
        if leader is not None and not self._leader_reaped:
            # The command last: once it exited and no other child is listed.
            if self.returncode is None or any(pid != leader for pid in _children()):
                return False
            try:
                os.waitpid(leader, os.WNOHANG)
            except InterruptedError:
                return False
            except ChildProcessError:
                pass
            self._leader_reaped = True
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except InterruptedError:
            return False
        except ChildProcessError:
            if self.process is not None and self.returncode is None:
                raise ChildAccountingError("Native command exit status was lost")
            self.quiescent = True
            return True
        return False

    def poll(self) -> Optional[int]:
        self._reap()
        return self.returncode

    def _signal_children(self, sig: int, *, deadline: Optional[float] = None) -> None:
        # No concurrent reaper runs between this membership read and pidfd open.
        # Exited children remain zombies, so their PIDs cannot be recycled here.
        if self.mode == "process_group":
            self._signal_group(sig, deadline=deadline)
            return
        for index, pid in enumerate(_children()):
            if index >= self._BATCH or (deadline is not None and time.monotonic() >= deadline):
                break
            try:
                descriptor = os.pidfd_open(pid, 0)
            except ProcessLookupError:
                continue
            try:
                try:
                    signal.pidfd_send_signal(descriptor, sig, None, 0)
                except ProcessLookupError:
                    pass
            finally:
                os.close(descriptor)

    def _signal_group(self, sig: int, *, deadline: Optional[float] = None) -> None:
        leader = self.process.pid if self.process is not None else None
        if leader is not None and not self._leader_reaped:
            # The unreaped command keeps its group's ID reserved.
            try:
                os.killpg(leader, sig)
            except ProcessLookupError:
                pass
        for index, pid in enumerate(pid for pid in _children() if pid != leader):
            if index >= self._BATCH or (deadline is not None and time.monotonic() >= deadline):
                break
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def close(self, *, grace: float = 5.0, kill_wait: float = 5.0) -> None:
        """Terminate/reap owned descendants or retain unconfirmed accounting.

        Failure leaves this live supervisor retryable. Callers must not exit it
        or release the prepared copy as if it had confirmed completion.
        """
        self._close_exit_fd()
        start = time.monotonic()
        for sig, deadline in ((signal.SIGTERM, start + grace),
                              (signal.SIGKILL, start + grace + kill_wait)):
            while True:
                if self.quiescent:
                    return
                self._signal_children(sig, deadline=deadline)
                if self._reap(deadline=deadline):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.02, remaining))
        # One separately bounded final observation, never an unbounded drain.
        if not self._reap():
            raise ChildAccountingError("Owned Linux descendants did not stop and reap")


def _settle(deadline: float, attempt: int, error: OSError) -> None:
    """Wait briefly before reading a control file again during its replacement.

    On the WSL mount of a Windows drive (drvfs), a read racing an atomic rename
    can find the name missing or the file without data (ENODATA).
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0 or attempt + 1 >= _READ_SETTLE_ATTEMPTS:
        raise ControlError(
            "Control file replacement did not settle within its read bound") from error
    time.sleep(min(0.01, remaining))


class _Liveness:
    """Lease progress timed by the helper, not by host/container clock agreement.

    A gap between two of the helper's own observations longer than
    ``_STALL_SECONDS`` means the helper itself did not run (the machine or
    its storage stalled), so the host could not renew either: only the gap's
    first ``_STALL_SECONDS`` count against the host. At most twice the lease
    is excused per renewal, so a silent host is declared lost at most three
    leases after its last renewal (seen at the helper's next observation),
    however the helper stalls. A host that stops renewing while the helper
    runs is still declared lost after the lease.
    """

    _STALL_SECONDS = 1.0

    def __init__(self, token: str, duration: float) -> None:
        self.token = token
        self.duration = duration
        self.generation = -1
        self.deadline = time.monotonic()
        self.observed: Optional[float] = None
        self.excused = 0.0

    def observe(self, record: dict) -> bool:
        if (set(record) != {"token", "generation"} or record["token"] != self.token
                or type(record["generation"]) is not int or record["generation"] < 0):
            raise ControlError("Invalid control liveness record")
        if record["generation"] < self.generation:
            raise ControlError("Control liveness moved backwards")
        now = time.monotonic()
        if self.observed is not None and now - self.observed > self._STALL_SECONDS:
            excuse = min(now - self.observed - self._STALL_SECONDS,
                         2 * self.duration - self.excused)
            self.deadline += excuse
            self.excused += excuse
        self.observed = now
        if record["generation"] > self.generation:
            self.generation = record["generation"]
            self.deadline = now + self.duration
            self.excused = 0.0
        return now >= self.deadline


class _Channel:
    """The command's private control files in the directory the host created.

    The host keeps the directory's creation receipt and lease. This helper
    removes nothing but its own pending report.
    """

    _FILES = {"request.json", "lease.json", "cancel.json"}

    def __init__(self, path: str, token: str) -> None:
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
            raise ControlError("Invalid control identity")
        self.path = Path(path)
        self.token = token

    def read(self, name: str, *, optional: bool = False) -> Optional[dict]:
        if name not in self._FILES:
            raise ControlError("Unknown control file")
        deadline = time.monotonic() + _READ_SETTLE_SECONDS
        attempt = 0
        while True:
            try:
                with os.fdopen(os.open(str(self.path / name), os.O_RDONLY), "rb") as stream:
                    data = stream.read()
                break
            except FileNotFoundError as error:
                if optional:
                    return None
                # The host replaces its lease while this helper runs.
                _settle(deadline, attempt, error)
            except OSError as error:
                if error.errno != errno.ENODATA:
                    raise
                _settle(deadline, attempt, error)
            attempt += 1
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise ControlError("Unreadable control JSON") from error
        if not isinstance(result, dict) or result.get("token") != self.token:
            raise ControlError("Control input identity does not match")
        return result

    def report(self, state: str, *, job=None, reason: Optional[str] = None,
               error: Optional[BaseException] = None) -> None:
        record = {"version": 1, "token": self.token, "helper_pid": os.getpid(),
                  "state": state, "command_pid": job.process.pid if job and job.process else None,
                  "returncode": job.returncode if job else None,
                  "quiescent": job.quiescent if job else _childless(), "reason": reason}
        elapsed = getattr(job, "elapsed_ns", None) if job else None
        if elapsed is not None:
            record["elapsed_ns"] = elapsed
        mode = getattr(job, "mode", None) if job else None
        if mode is not None:
            record["supervision"] = mode
        if error is not None:
            record["error"] = {"type": type(error).__name__, "message": str(error),
                               "errno": getattr(error, "errno", None),
                               "filename": getattr(error, "filename", None)}
        temporary = str(self.path / f".status-{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            deadline = time.monotonic() + _REPORT_REPLACE_SECONDS
            for attempt in range(_REPORT_REPLACE_ATTEMPTS):
                try:
                    os.rename(temporary, str(self.path / "status.json"))
                    break
                except PermissionError as denied:
                    remaining = deadline - time.monotonic()
                    if (denied.errno not in {errno.EACCES, errno.EPERM} or remaining <= 0
                            or attempt + 1 == _REPORT_REPLACE_ATTEMPTS):
                        raise
                    # Mapped Windows readers can briefly deny atomic replacement.
                    time.sleep(min(0.01, remaining))
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class _Cancellation:
    def __init__(self) -> None:
        self.signal = None

    def receive(self, signum, frame) -> None:
        self.signal = signum

    def install(self) -> dict:
        previous = {}
        try:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                handler = signal.getsignal(sig)
                # Preserve ignored dispositions for the native command. Its
                # control lease still bounds disconnect/cancellation handling.
                if handler != signal.SIG_IGN:
                    previous[sig] = signal.signal(sig, self.receive)
        except BaseException as error:
            try:
                self.restore(previous)
            except BaseException as cleanup:
                if hasattr(error, "add_note"):
                    error.add_note(f"Signal restoration failed: {cleanup}")
            raise
        return previous

    @staticmethod
    def restore(previous: dict) -> None:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _cancel_requested(channel, cancellation) -> bool:
    cancel = channel.read("cancel.json", optional=True)
    if cancel is not None and (set(cancel) != {"token"} or cancel["token"] != channel.token):
        raise ControlError("Invalid cancellation record")
    return cancellation.signal is not None or cancel is not None


def _serve(channel, *, job_factory=_OwnedCommand, cancellation=None) -> int:
    """Serve one native command. Failed accounting keeps the supervisor alive.

    A host must still verify its own helper handle and final report before
    treating quiescence as confirmed.
    """
    job = None
    primary = None
    previous = {}
    cancellation = cancellation or _Cancellation()
    try:
        config = channel.read("request.json")
        liveness = _Liveness(channel.token, config["lease_seconds"])
        if liveness.observe(channel.read("lease.json")):
            raise ControlError("Host control lease expired before startup")
        previous = cancellation.install()
        job = job_factory()
        channel.report("READY", job=job)
        # Stops received during setup/READY must precede native creation.
        lease_expired = liveness.observe(channel.read("lease.json"))
        if _cancel_requested(channel, cancellation):
            job.close(grace=config["grace"], kill_wait=config["kill_wait"])
            if not job.quiescent:
                raise ChildAccountingError("Cancelled startup did not confirm child reaping")
            channel.report("CANCELLED", job=job, reason="cancelled before native start")
            return 0
        if lease_expired:
            raise ControlError("Host control lease expired before native start")
        options = ({"env": config["native_environment"]}
                   if "native_environment" in config else {})
        job.start(config["argv"], cwd=config["cwd"], **options)
        channel.report("RUNNING", job=job)
        reason = None
        while job.poll() is None:
            if _cancel_requested(channel, cancellation):
                reason = "cancelled"
                break
            if liveness.observe(channel.read("lease.json")):
                reason = "host control lease expired"
                break
            # Wake at the command's exit, so its observed end is not late.
            wait = getattr(job, "wait_exit", None)
            if wait is None:
                time.sleep(0.02)
            else:
                wait(0.02)
        if reason is None:
            channel.report("EXITED", job=job)
        job.close(grace=config["grace"], kill_wait=config["kill_wait"])
        if not job.quiescent:
            raise ChildAccountingError("Cleanup did not confirm owned child reaping")
        channel.report("COMPLETE" if reason is None else "CANCELLED", job=job, reason=reason)
        return 0
    except BaseException as error:
        primary = error
        # Keep adoption alive while cleanup remains unconfirmed. Never exit
        # successfully or make a new command after a failed close.
        while True:
            cleanup = None
            try:
                if job is not None:
                    job.close(grace=0, kill_wait=1)
                    if not job.quiescent:
                        raise ChildAccountingError("Owned child reaping remains unconfirmed")
            except BaseException as failure:
                cleanup = failure
            try:
                channel.report("FAILED", job=job, reason=str(primary), error=primary)
            except BaseException:
                pass
            if cleanup is None:
                return 1
            time.sleep(1)
    finally:
        try:
            cancellation.restore(previous)
        except BaseException as cleanup:
            error = primary or cleanup
            if primary is not None and hasattr(primary, "add_note"):
                primary.add_note(f"Signal restoration failed: {cleanup}")
            try:
                channel.report("FAILED", job=job, reason=str(error), error=error)
            except BaseException:
                pass
            if primary is None:
                raise


def _main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        channel = _Channel(argv[0], argv[1])
    except ControlError:
        return 2
    try:
        return _serve(channel)
    except BaseException:
        # Failure is carried by the private report, never mixed into tool output.
        return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
