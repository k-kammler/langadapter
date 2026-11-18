"""Run commands with bounded cleanup of their owned processes.

``command`` (or ``start`` and ``finish``) contains a command as the host
allows. On Windows a suspended process joins a private job before it
executes, and its scope ends only when the job has no active process,
children of an exited parent included. On Linux a supervisor adopts it
(``langadapter._linux_command``). Both confirm that every process the
command started has ended. Other POSIX hosts (macOS above all) start it in a
new session, so it and the descendants it does not move elsewhere share one
process group: stopping signals the group, SIGTERM and after a grace period
SIGKILL, and disposal kills what is left and waits until the group is empty.
A descendant that leaves the group (a new session, a daemon) is neither
ended nor seen, so the observations record ``SUPERVISION`` and the first
command warns. A host without processes starts nothing
(``langadapter._host``). Host containment does not establish completion of
commands inside a container.

A ``Cancellation`` bound with ``cancellable`` lets another thread stop the
processes an operation starts, and ``cleanup`` runs cleanup outside it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import warnings
from typing import Any, Optional, Sequence, Iterable, Iterator, Union
import contextlib
from contextvars import ContextVar
from types import SimpleNamespace
from langadapter._linux_command import _Command
from langadapter._linux_job import ControlError
from langadapter import _execution_scope, _host
from langadapter.errors import OperationCancelled


#: The supervision of a command in its own process group, as the
#: observations record it (``ExecutionIdentity.supervision``).
SUPERVISION = "process_group_unconfirmed"

_GROUP_SECONDS = 10.0
_WARNED = threading.Event()


def applies() -> bool:
    """Whether this host runs commands in process groups: it runs commands
    but cannot confirm their cleanup."""
    return _host.runs_commands() and not _host.confirms_cleanup()


def local_supervision() -> Optional[str]:
    """``SUPERVISION`` where local commands run in process groups, else None."""
    return SUPERVISION if applies() else None


def warning() -> str:
    """The warning of what a run in a process group cannot confirm."""
    return (f"LangAdapter runs commands on {sys.platform} in their own process group: "
            f"stopping a command ends its group, but a descendant that leaves the group "
            f"(a new session, a daemon) is neither ended nor seen, so cleanup is not "
            f"confirmed (the observations record supervision {SUPERVISION!r}). "
            f"Windows and Linux confirm it.")


def warn_once() -> None:
    """Warn of the unconfirmed cleanup, once per process."""
    if not _WARNED.is_set():
        _WARNED.set()
        warnings.warn(warning(), RuntimeWarning, stacklevel=3)


def start_group(args: Sequence[str], **options: Any) -> subprocess.Popen:
    """Start ``args`` as the leader of a new session and process group."""
    warn_once()
    if options.get("start_new_session") is False or "process_group" in options:
        raise ValueError("a command on this host owns its own process group")
    options["start_new_session"] = True
    proc = subprocess.Popen(args, **options)
    proc._langadapter_process_group = proc.pid
    return proc


def is_group_command(proc: Any) -> bool:
    return getattr(proc, "_langadapter_process_group", None) is not None


def _signal_group(proc: Any, sig: int) -> None:
    try:
        os.killpg(proc._langadapter_process_group, sig)
    except ProcessLookupError:
        pass


def _group_empty(proc: Any) -> bool:
    try:
        os.killpg(proc._langadapter_process_group, 0)
    except ProcessLookupError:
        return True
    return False


def cancel(proc: Any) -> None:
    """Ask the command's group to end (SIGTERM), without waiting."""
    _signal_group(proc, signal.SIGTERM)


def terminate(proc: Any) -> None:
    """End the command's group now (SIGKILL), without waiting."""
    _signal_group(proc, signal.SIGKILL)


def stop_group(proc: Any, grace: float = 10.0) -> None:
    """End the command's group: SIGTERM, then SIGKILL after ``grace``
    seconds, and wait for the command."""
    cancel(proc)
    try:
        proc.wait(timeout=max(0.0, grace))
    except subprocess.TimeoutExpired:
        terminate(proc)
        proc.wait(timeout=10)
    # Descendants that ignored SIGTERM end with the group.
    terminate(proc)


def dispose(proc: Any, *, primary: Optional[BaseException] = None) -> bool:
    """End what is left of the command's group, wait until it is empty and
    close its streams: True when done. A failure is noted on ``primary``
    (then False) or raised. What left the group is not seen."""
    failures = []
    try:
        if proc.poll() is None:
            terminate(proc)
        proc.wait(timeout=10)
        terminate(proc)
        deadline = time.monotonic() + _GROUP_SECONDS
        while not _group_empty(proc):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"processes of the command's group {proc._langadapter_process_group} "
                                   f"did not end")
            time.sleep(0.05)
    except BaseException as cleanup:
        failures.append(cleanup)
    # No output reader to join: on POSIX, communicate reads both pipes in
    # this thread (selectors) and starts none, unlike on Windows.
    return _settle(proc, failures, primary=primary)


def _settle(proc: Any, failures: list, *, primary: Optional[BaseException] = None,
            readers: bool = False) -> bool:
    """The disposals' common tail: the output confirmed, the output
    ``readers`` joined (Windows, where communicate starts them), the streams
    closed unless quiescence is uncertain, and ``failures`` noted on
    ``primary`` (then False) or raised. True when nothing failed."""
    if proc is not None:
        if getattr(proc, "_langadapter_output_unconfirmed", False):
            failures.append(RuntimeError("Command output draining remains unconfirmed"))
        if readers and not failures:
            deadline = time.monotonic() + _OUTPUT_DRAIN_SECONDS
            for name in ("stdout_thread", "stderr_thread"):
                reader = getattr(proc, name, None)
                if reader is not None:
                    try:
                        reader.join(timeout=max(0.0, deadline - time.monotonic()))
                        if reader.is_alive():
                            raise RuntimeError("Command output reader did not finish")
                    except BaseException as cleanup:
                        failures.append(cleanup)
        # Buffered close can acquire a lock held by communicate's live reader.
        # Keep every stream and its process handle when quiescence is uncertain.
        for name in (() if failures else ("stdin", "stdout", "stderr")):
            stream = getattr(proc, name, None)
            if stream is not None:
                try:
                    stream.close()
                except BaseException as cleanup:
                    failures.append(cleanup)
    if not failures:
        return True
    error = primary if primary is not None else failures[0]
    error._langadapter_command = proc
    for cleanup in failures[0 if primary is not None else 1:]:
        _cleanup_note(error, cleanup)
    if primary is None:
        raise failures[0]
    return False


_OUTPUT_DRAIN_SECONDS = 10.0


def stop(proc: subprocess.Popen, grace: float = 10.0) -> None:
    """End ``proc``, a command ``start`` launched, with its descendants, and
    wait for it. A Linux command's supervisor sends SIGTERM, then SIGKILL
    after its own grace period, and a process group is signaled the same
    way. On Windows the command's job is terminated."""
    _stop_route(proc).stop(proc, grace)


def _stop_linux(proc: Any, grace: float) -> None:
    proc.cancel()
    proc.wait(timeout=max(0.0, grace) + 10)


def _stop_windows(proc: Any, grace: float) -> None:
    job = getattr(proc, "_langadapter_command_job", None)
    if job is None:
        raise TypeError("stop ends only a command that start launched")
    if getattr(job, "_handle", True) is None:
        job.close()
    else:
        job.stop(grace=grace)
    proc.wait(timeout=grace)


def _terminate_windows(proc: Any) -> None:
    if proc.poll() is None:
        proc._langadapter_command_job.terminate()


def _finish_linux(proc: Any, *, primary: Optional[BaseException] = None) -> bool:
    try:
        return proc.close(primary=primary) is True
    except ControlError:
        # A command that ended without completing (its lease expired),
        # disposed of, whose failure was raised where it ran: only
        # disposal counts here. A failure nobody saw is raised.
        if not proc._closed or not proc._unfinished_raised:
            raise
        return True


#: Each kind of command's stop route: cancel (stop_all's first pass, None when
#: the kind has none), terminate (now, without waiting), stop (and wait) and
#: finish (dispose, True when confirmed). The entries look the module's
#: functions up when called.
_STOP_ROUTES = {
    "linux": SimpleNamespace(cancel=lambda proc: proc.cancel(), terminate=lambda proc: proc.cancel(),
                             stop=lambda proc, grace: _stop_linux(proc, grace),
                             finish=lambda proc, *, primary: _finish_linux(proc, primary=primary)),
    "group": SimpleNamespace(cancel=lambda proc: cancel(proc), terminate=lambda proc: terminate(proc),
                             stop=lambda proc, grace: stop_group(proc, grace=grace),
                             finish=lambda proc, *, primary: dispose(proc, primary=primary)),
    "windows": SimpleNamespace(cancel=None, terminate=lambda proc: _terminate_windows(proc),
                               stop=lambda proc, grace: _stop_windows(proc, grace),
                               finish=lambda proc, *, primary: _dispose(
                                   proc, getattr(proc, "_langadapter_command_job", None), primary=primary)),
}


def _stop_route(proc: Any) -> SimpleNamespace:
    """The command's stop route: its Linux supervisor (a ``_Command``), its
    POSIX process group, or its Windows job."""
    if isinstance(proc, _Command):
        return _STOP_ROUTES["linux"]
    return _STOP_ROUTES["group" if is_group_command(proc) else "windows"]


def stop_all(procs: Iterable[subprocess.Popen], grace: float = 10.0) -> None:
    """End several commands with their descendants. Every Linux command and
    every process-group command is cancelled before any is waited for, so
    they end together."""
    procs = list(procs)
    cancelled = False
    for proc in procs:
        route = _stop_route(proc)
        if route.cancel is not None:
            route.cancel(proc)
            cancelled = True
    if cancelled:
        deadline = time.monotonic() + grace
        for proc in procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    for proc in procs:
        stop(proc, grace=grace)


def _cleanup_note(error: BaseException, cleanup: BaseException) -> None:
    if hasattr(error, "add_note"):
        error.add_note(f"Command cleanup failed: {cleanup}")


class Cancellation:
    """The native work of one operation, which another thread may stop.
    Every process the operation's thread starts while the cancellation is
    bound (``cancellable``) is registered until finished. ``cancel`` asks
    each to end at once, without waiting, and refuses every later start with
    ``OperationCancelled``. The operation's thread sees its processes end and
    finishes and disposes of them as without a cancel. A cancellation bound
    while another is bound is its child, which the other's ``cancel`` cancels.
    """

    def __init__(self) -> None:
        # Held while signaling, and by ``finish`` before it reaps a
        # process: a signal never reaches a process ID already reused.
        # Reentrant: a signal handler may cancel on a thread that holds it.
        self._lock = threading.RLock()
        self._cancelled = False
        self._live: list = []
        self._children: list = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Stop the registered processes and refuse new ones, here and in
        every child. Safe from any thread or signal handler, and repeatable."""
        with self._lock:
            self._cancelled = True
            for proc in self._live:
                _terminate(proc)
            for child in list(self._children):
                child.cancel()

    def _adopt(self, child: "Cancellation") -> None:
        # The parent's lock is taken before the child's, never the reverse.
        with self._lock:
            self._children.append(child)
            if self._cancelled:
                child.cancel()

    def _disown(self, child: "Cancellation") -> None:
        with self._lock:
            self._children = [item for item in self._children if item is not child]

    def _admit(self, proc: Any) -> None:
        with self._lock:
            if self._cancelled:
                _terminate(proc)
            self._live.append(proc)
            proc._langadapter_cancellation = self

    def _release(self, proc: Any) -> None:
        with self._lock:
            self._live = [item for item in self._live if item is not proc]


_CANCELLATION: ContextVar[Optional[Cancellation]] = ContextVar(
    "langadapter_cancellation", default=None)


def cancelled() -> bool:
    """Whether the operation of this thread was cancelled."""
    cancellation = _CANCELLATION.get()
    return cancellation is not None and cancellation.cancelled


@contextlib.contextmanager
def cancellable(cancellation: Optional[Cancellation]) -> Iterator[Optional[Cancellation]]:
    """Bind ``cancellation`` to the processes this thread starts. None
    starts them outside any cancellation. One bound while another is
    becomes its child for as long as it is bound."""
    outer = _CANCELLATION.get()
    linked = (cancellation is not None and outer is not None
              and outer is not cancellation)
    if linked:
        outer._adopt(cancellation)
    token = _CANCELLATION.set(cancellation)
    try:
        yield cancellation
    finally:
        _CANCELLATION.reset(token)
        if linked:
            outer._disown(cancellation)


@contextlib.contextmanager
def cleanup() -> Iterator[None]:
    """Run an operation's cleanup outside its thread's cancellation: a
    cancelled operation must still release what it holds, so a cancel
    neither refuses nor stops its cleanup's commands. Their timeouts bound
    them."""
    with cancellable(None):
        yield


def _terminate(proc: Any) -> None:
    """Ask ``proc`` and its descendants to end now, without waiting. What
    fails here, the owner's disposal still finds."""
    with contextlib.suppress(Exception):
        _stop_route(proc).terminate(proc)


def start(args: Sequence[str], **options: Any) -> Union[subprocess.Popen, _Command]:
    """Start a command under containment for an owner, such as a persistent
    mutation worker, that outlives a ``command`` scope and must ``finish``
    it. Under a cancelled ``Cancellation`` it raises ``OperationCancelled``."""
    cancellation = _CANCELLATION.get()
    if cancellation is not None and cancellation.cancelled:
        raise OperationCancelled("the operation was cancelled")
    ticket = _execution_scope.reserve_launch()
    try:
        proc = _start(args, **options)
    except BaseException as error:
        _execution_scope.failed_launch(ticket, error)
        raise
    _execution_scope.acquired(ticket, proc)
    if cancellation is not None:
        cancellation._admit(proc)
    return proc


class _NothingLaunched:
    """The owner a failed launch names when it left no process, marked by
    ``_nothing_left``."""


def _nothing_left(error: BaseException, owner: Any = None) -> None:
    """Mark ``error`` as a launch that left nothing running, so its launch
    reservation resolves and its scope can close (``failed_launch``)."""
    owner = owner if owner is not None else _NothingLaunched()
    owner._langadapter_command_disposed = True
    owner._langadapter_nothing_launched = True
    error._langadapter_command = owner


def _start(args: Sequence[str], **options: Any) -> Union[subprocess.Popen, _Command]:
    if not _host.runs_commands():
        # No containment here (``_host``): nothing starts.
        error = OSError(_host.refusal())
        _nothing_left(error)
        raise error
    if applies():
        try:
            return start_group(args, **options)
        except Exception as error:
            # Popen raised: the group's leader never ran.
            _nothing_left(error)
            raise
    if os.name != "nt":
        return _Command.start(args, **options)
    issue = _host.working_dir_issue(options.get("cwd"))
    if issue:
        # Windows would refuse it with "the directory name is invalid".
        error = OSError(issue)
        _nothing_left(error)
        raise error

    from langadapter._windows_job import CREATE_SUSPENDED, CommandJob

    try:
        job = CommandJob()
    except Exception as error:
        _nothing_left(error)
        raise
    proc = None
    try:
        options["creationflags"] = options.get("creationflags", 0) | CREATE_SUSPENDED
        proc = subprocess.Popen(args, **options)
        proc._langadapter_command_job = job
        job.admit_suspended(proc)
        return proc
    except BaseException as error:
        # An error, not an interruption, with the job closed and its process
        # gone, or none started: the failed launch left nothing running. An
        # interruption inside Popen may leave a process it never returned.
        if _dispose(proc, job, primary=error) and isinstance(error, Exception):
            _nothing_left(error, proc if proc is not None else job)
        raise


def _dispose(proc, job, *, primary=None) -> bool:
    failures = []
    if job is not None:
        try:
            job.close()
        except BaseException as cleanup:
            failures.append(cleanup)
    if proc is not None:
        # Refused admission may leave a suspended root outside the job.
        try:
            if job is not None and proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        except BaseException as cleanup:
            failures.append(cleanup)
    return _settle(proc, failures, primary=primary, readers=True) and job is not None


def finish(proc: subprocess.Popen, *, primary: Optional[BaseException] = None) -> bool:
    """Dispose of the command's containment and streams, and return whether
    that is confirmed. A preserved ``primary`` error never turns failed
    disposal into success, and a process without a job cannot show that
    its descendants ended. The owner of a launch that failed and left
    nothing running (``_nothing_left``) counts as finished."""
    if getattr(proc, "_langadapter_nothing_launched", False) is True:
        _execution_scope.completed(proc)
        return True
    cancellation = getattr(proc, "_langadapter_cancellation", None)
    if cancellation is not None:
        cancellation._release(proc)
    proc._langadapter_command_disposed = False
    disposed = _stop_route(proc).finish(proc, primary=primary)
    proc._langadapter_command_disposed = disposed
    if disposed:
        _execution_scope.completed(proc)
    return disposed


@contextlib.contextmanager
def command(args: Sequence[str], *, owner: Any = None,
            unconfirmed: str = "Command containment disposal is unconfirmed", **options: Any):
    """Own one command's launcher and the host's containment for the scope,
    in which the process handle and streams stay valid. A refused Windows
    admission never falls back to uncontained execution. A command that ends
    while its cancellation is cancelled raises ``OperationCancelled`` once
    disposed of: whatever stopped it, what it reported is no observation.
    ``owner``, a command another route started (a container's), is owned in
    place of a new one. ``unconfirmed`` names a disposal left unconfirmed."""
    proc = owner if owner is not None else start(args, **options)
    primary = None
    try:
        yield proc
    except BaseException as error:
        primary = error
        error._langadapter_command = proc
        raise
    finally:
        if not finish(proc, primary=primary) and primary is None:
            error = RuntimeError(unconfirmed)
            error._langadapter_command = proc
            raise error
    raise_if_cancelled(proc)


def raise_if_cancelled(proc: Any) -> None:
    """Raise ``OperationCancelled`` when the cancellation ``proc`` was
    started under is cancelled. Its owner calls it once ``proc`` is
    disposed of, before building an observation from what it reported."""
    cancellation = getattr(proc, "_langadapter_cancellation", None)
    if cancellation is not None and cancellation.cancelled:
        raise OperationCancelled("the operation was cancelled")


def collect_output(proc: subprocess.Popen, timeout: float):
    """The output of a running command once it ended, read for at most
    ``timeout`` seconds, or None while it still runs. Reading while waiting
    keeps a command that writes more than its pipes hold from stalling. A
    retried read keeps what the last one read. A failed read leaves the
    output unconfirmed, as ``drain_output`` does."""
    try:
        output = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except BaseException as error:
        proc._langadapter_output_unconfirmed = True
        error._langadapter_command = proc
        raise
    proc._langadapter_output_unconfirmed = False
    return output


def drain_output(proc: subprocess.Popen):
    """The command's remaining output, drained within a bound after the
    launcher exited or was stopped. A descendant may keep the pipes open, so
    a drain that times out stops the containment and tries once more. The
    launcher's return code and captured output stay unchanged."""
    try:
        output = proc.communicate(timeout=_OUTPUT_DRAIN_SECONDS)
    except subprocess.TimeoutExpired as error:
        try:
            stop(proc)
            output = proc.communicate(timeout=_OUTPUT_DRAIN_SECONDS)
        except BaseException as cleanup:
            _cleanup_note(error, cleanup)
            proc._langadapter_output_unconfirmed = True
            error._langadapter_command = proc
            raise error
    except BaseException as error:
        proc._langadapter_output_unconfirmed = True
        error._langadapter_command = proc
        raise
    proc._langadapter_output_unconfirmed = False
    return output


#: The most bytes one read of a copied pipe takes.
_COPY_BYTES = 65536


def _copy(read: int, file: Any) -> None:
    """Copy the pipe ``read`` into ``file`` until every write end closed."""
    with open(read, "rb", buffering=0) as stream:
        for data in iter(lambda: stream.read(_COPY_BYTES), b""):
            file.write(data)


class _Copies:
    """Two pipes whose write ends a command gets as its stdout and stderr,
    each read end copied into its file of ``into`` by a thread of this
    process as the output arrives. The command's ``communicate`` then only
    waits, and its pipes close within its timeout as captured pipes do."""

    def __init__(self, into: Sequence[Any]) -> None:
        self.files = tuple(into)
        pipes = [os.pipe(), os.pipe()]
        self.ends = {"stdout": pipes[0][1], "stderr": pipes[1][1]}
        self._writes = [write for _, write in pipes]
        self._threads = [threading.Thread(target=_copy, args=(read, file), daemon=True,
                                          name="langadapter-output")
                         for (read, _), file in zip(pipes, self.files)]
        for thread in self._threads:
            thread.start()

    def __enter__(self) -> "_Copies":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._close_writes()

    def _close_writes(self) -> None:
        """Close this process's write ends: the copies end once every
        process holding one ended."""
        while self._writes:
            os.close(self._writes.pop())

    def _output(self, seconds: Optional[float], timeout: Optional[float]) -> tuple:
        """The files' values once both copies ended within ``seconds``, else
        ``TimeoutExpired`` for ``timeout``."""
        deadline = None if seconds is None else time.monotonic() + seconds
        for thread in self._threads:
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            raise subprocess.TimeoutExpired("the command's output", timeout)
        return tuple(file.getvalue() for file in self.files)

    def read(self, proc: Any, input: Any, timeout: Optional[float]) -> tuple:
        """The output of the started ``proc`` once it ended and its pipes
        closed, both within ``timeout``."""
        self._close_writes()
        began = time.monotonic()
        proc.communicate(input=input, timeout=timeout)
        left = None if timeout is None else max(0.0, timeout - (time.monotonic() - began))
        return self._output(left, timeout)

    def drained(self, proc: Any) -> tuple:
        """The output once ``proc`` was stopped, within ``drain_output``'s
        bound: a descendant that keeps a pipe open is stopped once more, and
        a copy that still runs leaves the output unconfirmed."""
        drain_output(proc)
        try:
            return self._output(_OUTPUT_DRAIN_SECONDS, _OUTPUT_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            stop(proc)
        try:
            return self._output(_OUTPUT_DRAIN_SECONDS, _OUTPUT_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            proc._langadapter_output_unconfirmed = True
            raise


def _streams(owner: Any, copies: Optional[_Copies]) -> dict:
    """A command's stdout and stderr: an owner's own, the copies' write
    ends, else captured pipes."""
    if owner is not None:
        return {}
    if copies is not None:
        return copies.ends
    return {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}


def run(args: Sequence[str], *, timeout: Optional[float], input: Any = None, owner: Any = None,
        into: Optional[Sequence[Any]] = None, **options: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` with captured output, except that a timeout, or
    an interruption, ends the process's whole tree. A timeout raises
    ``subprocess.TimeoutExpired`` with the output read so far. ``owner``
    and ``unconfirmed`` are ``command``'s: an owner comes with its pipes.
    ``into``, two binary files with ``getvalue`` (stdout's, then stderr's,
    such as ``io.BytesIO``), receives the output instead as it arrives
    (``_Copies``), and the output is their values."""
    if input is not None and owner is None:
        if "stdin" in options:
            raise ValueError("stdin and input arguments may not both be used")
        options["stdin"] = subprocess.PIPE
    if into is not None and owner is not None:
        raise ValueError("an owner comes with its pipes, which into cannot replace")
    with _Copies(into) if into is not None else contextlib.nullcontext() as copies:
        with command(args, owner=owner, **_streams(owner, copies), **options) as proc:
            try:
                stdout, stderr = (copies.read(proc, input, timeout) if copies is not None
                                  else proc.communicate(input=input, timeout=timeout))
            except subprocess.TimeoutExpired as error:
                try:
                    stop(proc)
                    stdout, stderr = (copies.drained(proc) if copies is not None
                                      else drain_output(proc))
                except BaseException as cleanup:
                    _cleanup_note(error, cleanup)
                    raise error
                raise subprocess.TimeoutExpired(args, timeout, output=stdout,
                                                stderr=stderr) from None
            except BaseException as error:
                try:
                    stop(proc)
                except BaseException as cleanup:
                    _cleanup_note(error, cleanup)
                raise
    completed = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    elapsed = getattr(proc, "native_elapsed_ns", None)
    if elapsed is not None:
        # A supervised Linux command's own lifetime, timed by its supervisor.
        completed.native_elapsed_s = elapsed / 1e9
    return completed
