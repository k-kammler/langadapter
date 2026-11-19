"""Where a project's commands run, and the environment they run with.

A ``LocalExecutor`` runs a command as a process of this machine. A
``ContainerExecutor`` runs it in a running container through its runner:
``docker`` or ``podman`` (``exec -w -e -i``), ``apptainer`` or
``singularity`` (``exec --cleanenv --pwd --env``). The caller builds the
command the same way for both, argv[0] (the project's interpreter or a tool)
named as the executing environment sees it, and the executor adds only the
exec wrapper and the variables it forwards.

The work directory is the one directory host and container share, and the files
the adapter writes for a command lie under it. A host path that is valid in
the container (Linux, WSL) is used unchanged on both sides. A Windows drive
path is not, so the work directory is mounted at a fixed container path and every
argument, working directory and forwarded value under it is rewritten.

A container starts from its own environment, so an executor forwards only
``FORWARDED_KEYS``, the names the caller adds and the variables with the
caller's prefixes, by default ``ENV_PREFIX``.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import re
import subprocess
import warnings
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from langadapter import _process_tree
from langadapter._owned_directory import OwnedDirectory, OwnershipRefused, _long_path
from langadapter._environment import captured, launch_environment, selected_executable
from langadapter.context import ContainerExecution, Environment, LocalExecution, ProjectContext

#: The prefix of the variables through which LangAdapter talks to its own
#: child processes.
ENV_PREFIX = "LANGADAPTER_"

#: Variables forwarded into a container whatever the caller adds. LangAdapter
#: sets the hash seed of its suite, coverage, evidence and benchmark runs (0,
#: or a run as delivered's session seed), so set order, and coverage that
#: depends on it, repeats between runs. Cosmic Ray's mutant runs keep the
#: caller's.
FORWARDED_KEYS = ("PYTHONPATH", "PYTHONHASHSEED")

# The POSIX path the host work directory is mounted at inside the container when
# the host path itself is not a valid container path.
_CONTAINER_WORK_DIR = "/workzone"
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
# Runners whose ``exec`` takes Singularity/Apptainer-style flags
# (``--pwd`` / ``--env``) rather than Docker's ``-w`` / ``-e``.
_APPTAINER_RUNNERS = ("apptainer", "singularity")


def run_parallel(function: Callable[[Any], Any], items: Iterable[Any], *,
                 workers: int) -> List[Any]:
    """``function`` applied to each of ``items`` on at most ``workers`` threads,
    for an engine that runs its units of work side by side (the C adapter's
    mutants): the results in the order of ``items``. Each call runs in a copy
    of the caller's context. Inside a prepared use (``PreparedProject.use``)
    the use lends itself to the threads for their calls, and every command
    they run is checked as the use's own. The call returns once every
    thread has ended, and then raises the first error a call raised. With one
    worker or one item the calls run on the caller's thread. Each call must
    write only in a directory of its own, never in the project's tree."""
    from langadapter import _execution_scope

    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [function(item) for item in items]
    scope = _execution_scope._CURRENT.get()
    lending = (scope.lend() if isinstance(scope, _execution_scope._Scope)
               else contextlib.nullcontext())

    def lent(item):
        if scope is None:
            return function(item)
        scope.admit()
        try:
            return function(item)
        finally:
            scope.dismiss()

    with lending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(contextvars.copy_context().run, lent, item) for item in items]
            wait(futures)
    failed = next((future.exception() for future in futures if future.exception() is not None),
                  None)
    if failed is not None:
        raise failed
    return [future.result() for future in futures]


def supervision_of(context: ProjectContext) -> Optional[str]:
    """How the context's commands are supervised: "pidfd", "process_group",
    "unreported" (a container whose control helper has started no command
    yet), "process_group_unconfirmed" (a POSIX host other than Linux), or
    None (Windows, a container without a control helper)."""
    execution = context.execution
    if isinstance(execution, ContainerExecution):
        if execution.helper_python is None:
            return None
        from langadapter import _linux_container
        return _linux_container.reported_supervision(
            execution.runner, execution.container, execution.helper_python) or "unreported"
    from langadapter import _linux_command
    from langadapter import _process_tree as _posix_group
    return _linux_command.local_supervision() or _posix_group.local_supervision()


def execution_identity(context: ProjectContext):
    """The ``ExecutionIdentity`` of an observation made in ``context``."""
    from langadapter.outcomes import ExecutionIdentity

    execution = context.execution
    derivation = getattr(context, "_derivation", None)
    return ExecutionIdentity(execution.kind, getattr(execution, "image", None),
                             supervision_of(context),
                             derivation.record() if derivation is not None else None)


def run_capture(argv: Sequence[str], *, input: Optional[str] = None,
                timeout: Optional[float] = None,
                env: Optional[Dict[str, str]] = None,
                cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run ``argv`` capturing its output, with ``input`` sent as UTF-8 bytes
    and stdout and stderr decoded with universal newlines. Text-mode stdin
    would turn ``\\n`` into ``\\r\\n`` on Windows, so a patch piped to
    ``git apply`` would fail to apply."""
    completed = _process_tree.run(
        [str(a) for a in argv],
        input=input.encode("utf-8") if input is not None else None,
        timeout=timeout, env=env, cwd=cwd,
    )
    completed.stdout = _as_text(completed.stdout)
    completed.stderr = _as_text(completed.stderr)
    return completed


def _as_text(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    # Universal newlines, as text-mode capture gives: a Windows child writes
    # \r\n and parsers expect \n.
    return (value or "").replace("\r\n", "\n").replace("\r", "\n")


def container_work_dir_for(host_work_dir: str) -> str:
    """Where the host work directory is mounted in the container: the host path
    itself when it is a valid POSIX container path (Linux, WSL), else (a
    Windows drive path) a fixed POSIX path the executor translates to."""
    path = str(host_work_dir)
    if _WINDOWS_DRIVE.match(path) or "\\" in path:
        return _CONTAINER_WORK_DIR
    return path.rstrip("/") or "/"


def _mounted_work_dir(execution) -> str:
    """Where ``execution``'s work directory is in the container: its
    ``container_work_dir``, else ``container_work_dir_for``'s mount path."""
    return (getattr(execution, "container_work_dir", None)
            or container_work_dir_for(execution.work_dir))


def rebase_path(path: str, host_work_dir: Optional[str],
                container_work_dir: Optional[str]) -> str:
    """``path``, a host path in the work directory, as POSIX in the container's
    work directory. A path outside the work directory, and every path when there
    is no work directory or both sides share it, is only written POSIX. A
    drive-letter path parses as a Windows path on any platform."""
    def posix(value) -> str:
        text = str(value)
        if _WINDOWS_DRIVE.match(text):
            return PureWindowsPath(text).as_posix()
        return Path(text).as_posix()

    rebased = PurePosixPath(posix(path))
    if not host_work_dir:
        return rebased.as_posix()
    host = PurePosixPath(posix(host_work_dir))
    if host == PurePosixPath(container_work_dir):
        return rebased.as_posix()
    # A Windows work directory named in 8.3 short names holds the directories the
    # adapter makes in it spelled long (``OwnedDirectory``), and a path in a
    # long-named work directory may be given short: either spelling is in it.
    for inside in dict.fromkeys((str(path), _long_path(str(path)))):
        for work_dir in dict.fromkeys((str(host_work_dir), _long_path(str(host_work_dir)))):
            try:
                return (PurePosixPath(container_work_dir)
                        / PurePosixPath(posix(inside)).relative_to(
                            PurePosixPath(posix(work_dir)))).as_posix()
            except ValueError:
                continue
    return rebased.as_posix()


def container_path(execution: ContainerExecution, path: str) -> str:
    """``path`` as a command in the container of ``execution`` sees it,
    rebased as the executor rewrites arguments (onto ``container_work_dir``,
    else ``container_work_dir_for``'s), for a path written into a file a
    command in the container reads, where the executor does not reach."""
    return rebase_path(path, execution.work_dir, _mounted_work_dir(execution))


def _selects_environment(context: Optional[ProjectContext]) -> bool:
    """Whether a command run without a caller's ``env`` takes ``context``'s
    environment: a captured one, or an inheriting one with values over the
    caller's environment."""
    return context is not None and (captured(context.environment)
                                    or bool(context.environment))


class Executor(ABC):
    """Runs a project command and returns its completed process. A command
    run without ``env`` gets the context's environment."""

    @abstractmethod
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        input: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Run ``argv`` and return its completed process, with its output
        and error output as text. ``cwd`` is its working directory, ``env``
        its environment, ``input`` its standard input and ``timeout`` its
        limit in seconds."""


class LocalExecutor(Executor):
    """Runs the command directly on this machine."""

    def __init__(self, context: Optional[ProjectContext] = None) -> None:
        self._context = context

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        input: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Run ``argv`` on this machine and return its completed process, as
        ``Executor.run`` describes."""
        from langadapter._execution_scope import operation
        with operation(self._context):
            return self._run_owned(argv, input=input, timeout=timeout, env=env, cwd=cwd)

    def _run_owned(self, argv, *, input, timeout, env, cwd):
        self._check_context()
        if env is None and _selects_environment(self._context):
            env = launch_environment(self._context.environment)
        return run_capture(argv, input=input, timeout=timeout, env=env, cwd=cwd)

    def _check_context(self):
        if self._context is not None:
            self._context._require_execution()
            if not isinstance(self._context.execution, LocalExecution):
                raise OwnershipRefused("Local executor does not match the selected execution context")


class ContainerExecutor(Executor):
    """Runs the command inside a running container. ``forward_prefixes`` (by
    default LangAdapter's) and ``forward_keys`` name the variables of ``env``
    that cross into it besides ``FORWARDED_KEYS``. A ``helper_python``
    selects the container control helper: every command then waits for its
    ready signal, ends only when every process it started has ended, and has
    its host runner process ended too. Without it, the runner's ``exec`` runs
    each command."""

    def __init__(self, container: str, *, runner: str = "docker",
                 work_dir: Optional[str] = None,
                 container_work_dir: Optional[str] = None,
                 forward_prefixes: Iterable[str] = (ENV_PREFIX,),
                 forward_keys: Iterable[str] = (),
                 helper_python: Optional[str] = None,
                 control_environment: Optional[Environment] = None,
                 context: Optional[ProjectContext] = None) -> None:
        self._context = context
        self.container = container
        self.runner = runner
        self.helper_python = helper_python
        self.control_environment = (None if control_environment is None
                                    else Environment(control_environment, inherit=False))
        self._host_work_dir = (str(work_dir).replace("\\", "/").rstrip("/")
                         if work_dir else None)
        self._container_work_dir = (container_work_dir
                         or (container_work_dir_for(work_dir) if work_dir
                             else None))
        windows_dir = bool(work_dir and (
            _WINDOWS_DRIVE.match(str(work_dir)) or "\\" in str(work_dir)))
        self._path_pattern = (re.compile(
            r"(?<![^=:;])" + re.escape(self._host_work_dir) + r"(?=/|$|[:;])",
            re.IGNORECASE if windows_dir else 0) if self._host_work_dir else None)
        self.forward_prefixes: Tuple[str, ...] = tuple(forward_prefixes)
        self.forward_keys: Tuple[str, ...] = tuple(
            dict.fromkeys((*FORWARDED_KEYS, *forward_keys)))

    def _to_container(self, value: str) -> str:
        """``value`` with the host work-dir path rewritten to its container
        path, bare or after ``=`` or ``:`` (``--rcfile=<wz>/cov.rc``,
        ``--cov-report=json:<wz>/cov.json``). A token that does not mention
        the work directory, or one both sides share, leaves ``value`` untouched."""
        if not self._host_work_dir or self._host_work_dir == self._container_work_dir:
            return value
        text = str(value).replace("\\", "/")
        if self._path_pattern.search(text) is None:
            return value
        return self._path_pattern.sub(lambda _: self._container_work_dir, text)

    def _forward_env(self, env: Optional[Dict[str, str]]) -> List[str]:
        if not env:
            return []
        flag = "--env" if self._apptainer() else "-e"
        out: List[str] = []
        for key, value in env.items():
            if key in self.forward_keys or (
                    self.forward_prefixes
                    and key.startswith(self.forward_prefixes)):
                out += [flag, f"{key}={self._to_container(value)}"]
        return out

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        input: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Run ``argv`` inside the container and return its completed
        process, as ``Executor.run`` describes."""
        from langadapter._execution_scope import operation
        with operation(self._context):
            return self._run_owned(argv, input=input, timeout=timeout, env=env, cwd=cwd)

    def _run_owned(self, argv, *, input, timeout, env, cwd):
        self._check_context()
        if env is None and _selects_environment(self._context):
            env = self._context.environment
        if self.helper_python is not None:
            from langadapter import _linux_container

            return _linux_container.run(self, argv, helper_python=self.helper_python,
                                    input=input, timeout=timeout, env=env, cwd=cwd)
        if self._captured_target(env):
            raise OwnershipRefused("Captured execution in the container requires the helper Python (execution.helper_python)")
        wrapper = self._command(argv, cwd=cwd, env=env, interactive=input is not None)
        options = self._control_options()
        return run_capture(wrapper, input=input, timeout=timeout, **options)

    def _captured_target(self, env) -> bool:
        return captured(env) or (self._context is not None and captured(self._context.environment))

    def _control_options(self) -> dict:
        return ({} if self.control_environment is None
                else {"env": launch_environment(self.control_environment)})

    def _apptainer(self) -> bool:
        return Path(self.runner).stem.lower() in _APPTAINER_RUNNERS

    def _check_context(self):
        if self._context is not None:
            self._context._require_execution()
            selected = self._context.execution
            if (not isinstance(selected, ContainerExecution)
                    or self.container != selected.container or self.runner != selected.runner
                    or self.helper_python != selected.helper_python
                    or self.control_environment != selected.control_environment
                    or self._host_work_dir != str(selected.work_dir).replace("\\", "/").rstrip("/")
                    or self._container_work_dir != _mounted_work_dir(selected)):
                raise OwnershipRefused("Container executor changed the selected execution context")

    def _command(self, argv: Sequence[str], *, cwd: Optional[str] = None,
                 env: Optional[Dict[str, str]] = None,
                 interactive: bool = False) -> List[str]:
        """The runner's exec command line for ``argv``, built without starting
        a host or container tool."""
        apptainer = self._apptainer()
        runner = (self.runner if self.control_environment is None else
                  selected_executable(self.runner, self.control_environment))
        if runner is None:
            raise OwnershipRefused("Captured host container runner is unavailable in the selected control environment")
        wrapper: List[str] = [runner, "exec"]
        if apptainer:
            # apptainer exec inherits the host environment: a loaded Python
            # module or virtual environment leaks PYTHONHOME, PYTHONPATH and
            # LD_LIBRARY_PATH, which break the container's Python ("No module
            # named 'encodings'"). The forwarding below adds back what the
            # command needs. docker exec starts from a clean environment.
            wrapper.append("--cleanenv")
        if interactive and not apptainer:
            wrapper.append("-i")
        if cwd:
            wrapper += (["--pwd", self._to_container(str(cwd))] if apptainer
                        else ["-w", self._to_container(str(cwd))])
        wrapper += self._forward_env(env)
        # apptainer and singularity address a running instance as
        # instance://<name>. docker and podman take the bare name.
        if apptainer and not self.container.startswith("instance://"):
            target = f"instance://{self.container}"
        else:
            target = self.container
        wrapper += [target, *[self._to_container(str(a)) for a in argv]]
        return wrapper


def executor_for(
    context: ProjectContext,
    *,
    forward_prefixes: Iterable[str] = (ENV_PREFIX,),
    forward_keys: Iterable[str] = (),
) -> Executor:
    """The executor of ``context``. A container executor forwards the
    variables with ``forward_prefixes`` (LangAdapter's by default),
    ``FORWARDED_KEYS``, the names of the context's additional environment
    and ``forward_keys``."""
    context._require_execution()
    execution = context.execution
    if isinstance(execution, ContainerExecution):
        return ContainerExecutor(
            execution.container, runner=execution.runner,
            work_dir=execution.work_dir,
            container_work_dir=execution.container_work_dir,
            helper_python=execution.helper_python,
            control_environment=execution.control_environment,
            forward_prefixes=forward_prefixes,
            forward_keys=(*context.environment, *forward_keys), context=context)
    return LocalExecutor(context)


def path_separator(context: ProjectContext) -> str:
    """The separator of a path list (PYTHONPATH and the like) in the
    executing environment: ``:`` inside a container whatever the host is,
    the host's separator for local execution."""
    if isinstance(context.execution, ContainerExecution):
        return ":"
    return os.pathsep


def prepend_path(env: Dict[str, str], name: str, entry: str,
                 separator: str) -> None:
    """Put ``entry`` at the front of the path list ``env[name]``, in place."""
    existing = env.get(name, "")
    env[name] = f"{entry}{separator}{existing}" if existing else str(entry)


def utf8_environment(env: Dict[str, str]) -> Dict[str, str]:
    """Make the Python processes started with ``env`` read and write UTF-8,
    and return ``env``, changed in place. On Windows a child writes
    redirected streams in the ANSI code page, so it cannot write a character
    that page lacks, or writes bytes a UTF-8 reader cannot decode. Values
    the caller set are kept."""
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


class _ScratchPath(str):
    """A string-compatible scope handle for a freshly owned directory."""

    retention_reason = ""


def _retain_scratch(directory: str, reason: str) -> None:
    """Keep ``directory`` with ``reason`` when the caller handles an
    execution failure."""
    if not isinstance(directory, _ScratchPath):
        raise OwnershipRefused("scratch retention requires its factory scope handle")
    directory.retention_reason = reason


def _stopped_cleanly(error: BaseException) -> bool:
    """Whether ``error`` ended a cancelled operation and names no command or
    launch left unresolved, so its commands were stopped and disposed of. A
    plain interruption establishes no such quiescence."""
    if not _process_tree.cancelled():
        return False
    from langadapter._execution_scope import reraise_unresolved
    try:
        reraise_unresolved(error)
    except BaseException as unresolved:
        if unresolved is error:
            return False
        raise
    return True


@contextlib.contextmanager
def work_dir_tempdir(context: ProjectContext, executor: Executor,
                     prefix: str = "langadapter_") -> Iterator[str]:
    """An owned temporary directory, removed only while its receipt matches.

    Exclusive scratch space, not a shared tree, in the context's work directory,
    else the system's default location. A command in a container may write
    into it as another user (some images run as root), so removal runs first
    inside the container, then on the host. A directory that was replaced,
    whose container path does not match or whose removal fails stays, with
    its receipt outside it, and a ``RuntimeWarning`` is issued. An exit by an
    exception keeps it too, since a process started in it may still run, and
    so does a failure the operation recorded for retention. The caller stops
    its commands before a normal exit. The exception to both is an operation
    a cancel stopped (``CancelScope.cancel`` or a mutation session's
    ``cancel``) with nothing left unresolved: its scratch is removed. The
    receipt lies in the system's temporary directory, but a derived context's
    (``ProjectContext.derived``) lies in its work directory, outside the
    directory, so the caller's removal of that directory removes it too.
    """
    context._require_execution()
    work_dir = context.execution.work_dir
    # Only a derived context's receipts go elsewhere: into its work directory.
    receipts = {"receipt_parent": work_dir} if context._derivation is not None else {}
    owned = OwnedDirectory.create(prefix=prefix, parent=work_dir, **receipts)
    directory = _ScratchPath(str(owned.root))
    container = isinstance(context.execution, ContainerExecution)
    marker = owned.marker

    def verify_container() -> None:
        completed = executor.run(["cat", str(marker)], timeout=120)
        if completed.returncode != 0 or (completed.stdout or "").strip() != owned.token:
            raise OwnershipRefused(
                f"container work directory does not match its creation receipt: {directory}")

    def remove() -> None:
        try:
            with _process_tree.cleanup():
                if os.path.lexists(owned.root):
                    owned.verify()
                    if container:
                        verify_container()
                        removed = executor.run(["rm", "-rf", "--", directory], timeout=120)
                        if removed.returncode != 0:
                            raise OSError("container scratch removal failed")
                owned.remove()
        except Exception as exc:
            from langadapter._execution_scope import reraise_unresolved
            reraise_unresolved(exc)
            warnings.warn(
                f"scratch directory retained: {directory}. Ownership receipt: "
                f"{owned.receipt}. {type(exc).__name__}: {exc}",
                RuntimeWarning, stacklevel=2)

    try:
        if container:
            verify_container()
        yield directory
    except BaseException as exc:
        if _stopped_cleanly(exc):
            # A cancel that left nothing unresolved: its commands were
            # stopped and disposed of, so nothing writes here.
            with contextlib.suppress(Exception):
                remove()
            raise
        # Preserve the original interruption even when warnings are errors.
        with contextlib.suppress(Exception):
            warnings.warn(
                f"scratch directory retained after interrupted operation: {directory}. "
                f"Ownership receipt: {owned.receipt}. {type(exc).__name__}: {exc}",
                RuntimeWarning, stacklevel=2)
        raise
    else:
        # The cancel may have caused a failure the operation recorded.
        if directory.retention_reason and not _process_tree.cancelled():
            with contextlib.suppress(Exception):
                warnings.warn(
                    f"scratch directory retained after interrupted operation: {directory}. "
                    f"Ownership receipt: {owned.receipt}. {directory.retention_reason}",
                    RuntimeWarning, stacklevel=2)
            return
        remove()
