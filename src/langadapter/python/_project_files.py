"""The project files a request names (a coverage target, the module under
test, a mutation target), checked and identified by their bytes where the
project runs. In a container context its paths are POSIX on any host and
the project interpreter checks and reads them inside the container. The
empty pytest-benchmark storage a run or a mutation session created in the
project root goes at its end.
"""
from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional, Sequence, Union

from langadapter._execution_scope import reraise_unresolved
from langadapter.context import ContainerExecution, ProjectContext
from langadapter.errors import InvalidRequest, OwnershipRefused
from langadapter.execution import Executor
from langadapter.outcomes import FileIdentity

#: A path of the project: a host path, or a POSIX path inside the container.
ProjectPath = Union[Path, PurePosixPath]

_IS_FILE = "import os, sys; sys.exit(0 if os.path.isfile(sys.argv[1]) else 1)"
#: pytest-benchmark's storage where the project configures none, which its
#: plugin makes in the working directory whenever pytest configures,
#: ``--benchmark-disable`` included. Runs and mutation sessions start pytest
#: in the project root.
BENCHMARK_STORAGE = ".benchmarks"
_EXISTS = "import os, sys; sys.exit(0 if os.path.lexists(sys.argv[1]) else 1)"
_REMOVE_EMPTY_DIR = ("import os, sys\n"
                     "p = sys.argv[1]\n"
                     "if os.path.isdir(p) and not os.path.islink(p) and not os.listdir(p):\n"
                     "    os.rmdir(p)\n")
_READ = ("import base64, sys; sys.stdout.write(base64.b64encode("
         "open(sys.argv[1], 'rb').read()).decode())")


def in_container(context: ProjectContext) -> bool:
    return isinstance(context.execution, ContainerExecution)


def project_root(context: ProjectContext) -> ProjectPath:
    """The project root, to join and compare project paths with."""
    if in_container(context):
        return PurePosixPath(context.root)
    return Path(context.root)


#: The context variable naming ``python_import_root``, the directory below the
#: project root that the project's package is imported from.
IMPORT_ROOT_VARIABLE = "LANGADAPTER_PYTHON_IMPORT_ROOT"


def import_root_setting(value: Any) -> str:
    """``python_import_root``'s directory below the project as a POSIX path,
    or "" for the project itself (empty, . or none). A value that names no
    directory below the project, an absolute, drive or UNC path or one with a
    .. part, raises ValueError with the reason. The setup's check and the
    runs' environment both read the setting through it."""
    text = str(value or "").strip().replace("\\", "/")
    if text in ("", ".", "none"):
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or PureWindowsPath(text).drive or ".." in path.parts:
        raise ValueError(f"{text!r} is no directory below the project")
    return "" if path.as_posix() == "." else path.as_posix()


def checked_import_root(value: Any) -> str:
    """``import_root_setting(value)`` for a context's variables: a value that
    names no directory below the project raises InvalidRequest on
    ``python_import_root``."""
    try:
        return import_root_setting(value)
    except ValueError as exc:
        raise InvalidRequest("python_import_root", str(exc)) from None


def import_root_name(context: ProjectContext) -> str:
    """``python_import_root`` as ``context`` names it
    (``IMPORT_ROOT_VARIABLE``), or "" for the project root itself."""
    return str(context.environment.get(IMPORT_ROOT_VARIABLE, "") or "").strip()


def import_root(context: ProjectContext) -> ProjectPath:
    """The directory the project's package is imported from: the project root
    with ``python_import_root`` below it."""
    name = import_root_name(context)
    return project_root(context) / name if name else project_root(context)


def _in_container(implementation: Any, script: str, path: ProjectPath) -> Optional[int]:
    """The exit status of ``script`` run on ``path`` by the project's
    interpreter in the container, or None when it could not run, as where
    the context names no work directory the executor accepts."""
    try:
        done = implementation._executor().run(
            [implementation.interpreter, *implementation._runtime_flags(), "-c", script,
             str(path)], timeout=60)
    except (OSError, subprocess.SubprocessError, OwnershipRefused) as error:
        reraise_unresolved(error)
        return None
    return done.returncode


def storage_exists(implementation: Any) -> bool:
    """Whether ``BENCHMARK_STORAGE`` is in the project root of
    ``implementation``'s context. A container's answer that cannot be had
    reads as present, so nothing is removed."""
    context = implementation.context
    path = project_root(context) / BENCHMARK_STORAGE
    if in_container(context):
        return _in_container(implementation, _EXISTS, path) != 1
    return os.path.lexists(path)


def remove_created_storage(implementation: Any, existed: Optional[bool]) -> None:
    """Remove ``BENCHMARK_STORAGE`` from the project root when ``existed``,
    ``storage_exists`` at the start of a run or session, is False and the
    storage is still empty, so neither writes into the project. A storage
    that was there before, or that holds files, stays."""
    if existed is not False:
        return
    context = implementation.context
    path = project_root(context) / BENCHMARK_STORAGE
    if in_container(context):
        _in_container(implementation, _REMOVE_EMPTY_DIR, path)
        return
    try:
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        # Written into meanwhile: it holds files and stays.
        pass


def project_file(implementation: Any, field: str, path: str) -> ProjectPath:
    """The project file ``path`` names (relative to the root or absolute,
    resolved on the host), checked through ``implementation``, the adapter's
    implementation. Raises ``InvalidRequest`` on ``field`` when it lies outside
    the root or is not a file where the project runs."""
    context = implementation.context
    root = project_root(context)
    if in_container(context):
        candidate = PurePosixPath(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = PurePosixPath(posixpath.normpath(str(candidate)))
        if root not in candidate.parents:
            raise InvalidRequest(
                field, f"{path} does not lie under the project root")
        checked = implementation._executor().run(
            [implementation.interpreter, *implementation._runtime_flags(), "-c", _IS_FILE,
             str(candidate)], timeout=60)
        if checked.returncode != 0:
            raise InvalidRequest(field, f"no file at {path} in the container")
        return candidate
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise InvalidRequest(
            field, f"{path} does not lie under the project root") from None
    if not candidate.is_file():
        raise InvalidRequest(field, f"no file at {path}")
    return candidate


def file_identity(implementation: Any, path: ProjectPath) -> FileIdentity:
    """The identity of ``path`` (from ``project_file``): its path relative
    to the project root and the digest of its bytes where the project runs."""
    context = implementation.context
    if not in_container(context):
        return FileIdentity.of(Path(path), Path(context.root))
    data = read_container_bytes(implementation._executor(), implementation.interpreter,
                                str(path), flags=implementation._runtime_flags())
    if data is None:
        raise RuntimeError(f"{path} could not be read in the container")
    relative = PurePosixPath(path).relative_to(project_root(context))
    return FileIdentity(relative.as_posix(), hashlib.sha256(data).hexdigest())


def read_container_bytes(executor: Executor, python: str,
                         path: str, *, flags: Sequence[str]) -> Optional[bytes]:
    """The exact bytes of ``path`` in the container, read by ``python``
    with ``flags`` there, or None when it cannot be read. base64 keeps every byte over the
    executor's text channel."""
    try:
        completed = executor.run([python, *flags, "-c", _READ, path], timeout=60)
    except Exception as exc:
        reraise_unresolved(exc)
        return None
    if completed.returncode != 0:
        return None
    try:
        return base64.b64decode((completed.stdout or "").strip(),
                                validate=True)
    except Exception:
        return None
