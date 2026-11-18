"""Private borrowed-container command ownership through a mapped Linux helper."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import subprocess
import time
from typing import Dict, Optional, Sequence, Tuple

from langadapter import _execution_scope, _process_tree
from langadapter._linux_command import STARTUP_BOUND_S, _Command
from langadapter._linux_job import SupervisionUnavailable
from langadapter.errors import OperationCancelled

#: A plain runner exec's round trip and when it was measured, by runner and container.
_ROUND_TRIPS: Dict[Tuple[str, str], Tuple[float, float]] = {}
#: How long a measured round trip stands for later commands, in seconds.
_ROUND_TRIP_SECONDS = 60.0
#: How long one plain runner exec of ``true`` may take, in seconds.
_ROUND_TRIP_TIMEOUT = 10
#: How each container helper (runner, container, interpreter) reports it supervises.
_SUPERVISION: Dict[Tuple[str, str, str], str] = {}


def reported_supervision(runner: str, container: str, helper_python: str):
    """The mode a container helper reported, None before its first command."""
    return _SUPERVISION.get((runner, container, helper_python))


def _runner_round_trip(executor) -> Optional[float]:
    """Seconds a plain runner exec adds around a command, added back to a
    supervised command's in-container time: the median of three execs of
    ``true`` (on a Linux host their own supervised time), kept for a minute.
    None, keeping nothing, when an exec fails or does not finish. An exec
    whose process may still run raises."""
    from langadapter.execution import run_capture

    key = (executor.runner, executor.container)
    known = _ROUND_TRIPS.get(key)
    if known is not None and time.monotonic() - known[1] < _ROUND_TRIP_SECONDS:
        return known[0]
    wrapper = executor._command(["true"])
    samples = []
    try:
        for _ in range(3):
            start = time.perf_counter()
            done = run_capture(wrapper, timeout=_ROUND_TRIP_TIMEOUT,
                               **executor._control_options())
            elapsed = time.perf_counter() - start
            if done.returncode != 0:
                return None
            native = getattr(done, "native_elapsed_s", None)
            samples.append(native if native is not None else elapsed)
    except Exception as error:
        # A failed Linux launch that names its unresolved command must propagate.
        if _execution_scope.unresolved_command(error) is not None:
            raise
        return None
    value = sorted(samples)[1]
    _ROUND_TRIPS[key] = (value, time.monotonic())
    return value


def start(executor, args: Sequence[str], *, helper_python: str,
          cwd: Optional[str] = None, env=None, input=None,
          startup: float = STARTUP_BOUND_S,
          stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    """Stage and handshake the selected helper (possibly the project's own 3.10+
    interpreter, run isolated and without site) and start ``args`` under it.
    The runner owns only its borrowed exec command, not the container. The
    caller must finish the returned owner (``_process_tree.finish``)."""
    if (not isinstance(helper_python, str) or not helper_python
            or "\\" in helper_python or "\0" in helper_python
            or not PurePosixPath(helper_python).is_absolute()
            or ".." in PurePosixPath(helper_python).parts):
        raise SupervisionUnavailable("Supervision in the container needs an explicit absolute Python 3.10+ path")
    work_dir = executor._host_work_dir
    if not work_dir or not os.path.isabs(work_dir) or not Path(work_dir).is_dir():
        raise SupervisionUnavailable("Supervision in the container needs an existing shared work folder (execution.work_dir)")
    if isinstance(args, (str, bytes)):
        raise TypeError("Native command arguments for the container must be a sequence")
    native = [executor._to_container(str(arg)) for arg in args]
    container_cwd = executor._to_container(str(cwd)) if cwd is not None else None

    cancellation = _process_tree._CANCELLATION.get()
    if cancellation is not None and cancellation.cancelled:
        raise OperationCancelled("the operation was cancelled")

    def launch(helper, channel, token, options):
        wrapper = executor._command(
            # Isolated, without site: the project's interpreter can serve, but its
            # .pth files and sitecustomize must not run in the supervisor.
            [helper_python, "-I", "-S", str(helper), str(channel), token],
            env=env, interactive=input is not None)
        # Cancel through the supervisor's control (the owner), never the runner.
        with _process_tree.cancellable(None):
            return _process_tree.start(wrapper, **options, **executor._control_options())

    options = {"stdout": stdout, "stderr": stderr}
    if input is not None:
        options["stdin"] = subprocess.PIPE
    target = {}
    if executor._captured_target(env):
        values = dict(env if env is not None else executor._context.environment)
        target["native_environment"] = {
            key: executor._to_container(value) for key, value in values.items()}
    owner = _Command.start_in_container(native, launch=launch, cwd=container_cwd,
                                 parent=work_dir, startup=startup,
                                 **target, **options)
    if cancellation is not None:
        cancellation._admit(owner)
    mode = getattr(owner, "supervision", None)
    if mode is not None:
        # Recorded at start, so records after any started command name the mode.
        _SUPERVISION[(executor.runner, executor.container, helper_python)] = mode
    return owner


def run(executor, args: Sequence[str], *, helper_python: str,
        cwd: Optional[str] = None, env=None, input: Optional[str] = None,
        timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """Return native output only after container and actual runner disposal."""
    from langadapter.execution import _as_text

    payload = input.encode("utf-8") if input is not None else None
    owner = start(executor, args, helper_python=helper_python,
                  cwd=cwd, env=env, input=payload)
    # The local route's run owns it: the timeout, disposal and cancellation.
    completed = _process_tree.run(args, timeout=timeout, input=payload, owner=owner,
                                  unconfirmed="Ownership of the container's command remains unresolved")
    completed.stdout, completed.stderr = _as_text(completed.stdout), _as_text(completed.stderr)
    if owner.supervision is not None:
        _SUPERVISION[(executor.runner, executor.container, helper_python)] = owner.supervision
    trip = _runner_round_trip(executor) if owner.native_elapsed_ns is not None else None
    if trip is not None:
        # As a clock around a plain runner exec measures it: own time plus the
        # round trip, without the supervisor's start, handshake, polling and cleanup.
        completed.native_elapsed_s = owner.native_elapsed_ns / 1e9 + trip
    elif hasattr(completed, "native_elapsed_s"):
        # Without a measured round trip a container command reports no own time.
        del completed.native_elapsed_s
    return completed
