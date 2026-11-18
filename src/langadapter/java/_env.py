"""Project paths and metadata belong to the selected execution environment."""
from __future__ import annotations

import math
import subprocess
import time
from langadapter.errors import InvalidRequest
import os
import posixpath
import re
from pathlib import Path, PurePosixPath
from langadapter import _host, _process_tree
from langadapter.sources import bounded
from langadapter.execution import ContainerExecutor, Executor
from langadapter.outcomes import FileIdentity
from langadapter._execution_scope import reraise_unresolved
from dataclasses import dataclass
from typing import Mapping, Optional
from langadapter.versions import java_version


# The time left before an absolute monotonic deadline, for the Java calls
# that take one.

def remaining(deadline: float | None, limit: float | None, stage: str):
    if deadline is None:
        return limit
    try:
        valid = (not isinstance(deadline, bool) and isinstance(deadline, (int, float))
                 and math.isfinite(deadline))
    except OverflowError:
        valid = False
    if not valid:
        raise InvalidRequest("deadline", "requires a finite absolute monotonic deadline")
    left = deadline - time.monotonic()
    if left <= 0:
        raise subprocess.TimeoutExpired(stage, 0)
    return left if limit is None else min(left, limit)


class JavaEnv:
    """Where one Java operation runs and until when: the selected executor
    and the operation's absolute deadline, which every step's limit shares.
    Project files are read locally or through a container's executor, a container's
    never from the host (build reports go to mounted host scratch)."""

    def __init__(self, executor: Executor | None = None, *, deadline: float | None = None):
        self.executor = executor
        self.container = isinstance(executor, ContainerExecutor)
        self.deadline = deadline
        #: The classpath separator where the commands run.
        self.separator = ":" if self.container else os.pathsep

    def left(self, limit: float | None, stage: str):
        """``limit`` or the time left, the smaller. Past the deadline,
        ``TimeoutExpired`` for ``stage``."""
        return remaining(self.deadline, limit, stage)

    def run(self, argv, limit: float | None, stage: str, **options):
        """``argv`` run by the executor within ``left(limit, stage)``, which
        is checked again afterwards: a success after the deadline is a
        timeout."""
        done = self.executor.run(argv, timeout=self.left(limit, stage), **options)
        self.left(limit, stage)
        return done

    def launch(self, argv, timeout: float | None, **options):
        """``argv`` run where the operation runs: a container's through its
        executor, a local one supervised with its process tree, its output
        read as UTF-8."""
        if self.container:
            return self.executor.run(argv, timeout=timeout, **options)
        return _process_tree.run(argv, timeout=timeout, text=True, encoding="utf-8",
                                 errors="replace", **options)

    def translate(self, value) -> str:
        """``value`` as the commands see it: in a container, its container path."""
        return self.executor._to_container(str(value)) if self.container else str(value)

    def path_list(self, paths) -> str:
        """``paths`` translated and joined with the classpath separator."""
        return self.separator.join(self.translate(path) for path in paths)

    def java_arguments(self, classpath, main: str, *arguments: str, jvm_args=(),
                       properties: Mapping[str, str] = {}) -> list:
        """The ``java`` arguments that run ``main`` with ``arguments`` on
        ``classpath``: the JVM arguments, the system properties as ``-D``
        options and ``-cp``, paths as the commands see them."""
        return [*map(self.translate, jvm_args),
                *(self.translate(f"-D{key}={value}") for key, value in properties.items()),
                "-cp", self.path_list(classpath), main, *arguments]

    def path(self, value):
        if self.container:
            return PurePosixPath(posixpath.normpath(self.executor._to_container(str(value))))
        return Path(value).resolve()

    def relative(self, path, root) -> str:
        if self.container:
            return posixpath.relpath(str(path), str(root))
        return os.path.relpath(path, root).replace(os.sep, "/")

    def read(self, path) -> str:
        if not self.container:
            self.left(60, "project file read")
            value = Path(path).read_text(encoding="utf-8")
            self.left(60, "project file read")
            return value
        done = self.run(["cat", "--", str(path)], 60, "project file read")
        if done.returncode != 0:
            raise OSError(bounded(f"cannot read container project file {path}: {done.stderr}"))
        return done.stdout

    def _exists(self, path, flag) -> bool:
        if not self.container:
            self.left(60, "project path probe")
            value = Path(path).is_file() if flag == "-f" else Path(path).is_dir()
            self.left(60, "project path probe")
            return value
        done = self.run(["test", flag, str(path)], 60, "project path probe")
        if done.returncode not in (0, 1):
            raise OSError(bounded(f"container project path probe failed: {path}: {done.stderr}"))
        return done.returncode == 0

    def is_file(self, path) -> bool:
        return self._exists(path, "-f")

    def is_dir(self, path) -> bool:
        return self._exists(path, "-d")

    def _canonical_paths(self, *paths):
        if not self.container:
            self.left(60, "project canonical paths")
            return tuple(Path(path).resolve() for path in paths)
        done = self.run(["realpath", "-e", "-z", "--", *map(self.translate, paths)],
                        60, "project canonical paths")
        if done.returncode != 0:
            raise OSError(bounded(f"cannot resolve container project paths: {done.stderr}"))
        records = done.stdout.split("\0")
        if (len(records) != len(paths) + 1 or records[-1] != ""
                or any(not value.startswith("/") or ".." in PurePosixPath(value).parts
                       for value in records[:-1])):
            raise OSError("container canonical path report is incomplete or invalid")
        return tuple(PurePosixPath(value) for value in records[:-1])

    def project_file(self, field: str, value, root):
        """The existing file ``value`` inside the project's real root, links
        resolved. Otherwise an ``InvalidRequest`` naming ``field``."""
        if not self.container:
            path = Path(value)
            if not path.is_absolute():
                path = Path(root) / path
            root, path = self._canonical_paths(root, path)
        else:
            path = PurePosixPath(self.executor._to_container(str(value)))
            if not path.is_absolute():
                path = PurePosixPath(self.executor._to_container(str(root))) / path
            if not self.is_file(path):
                raise InvalidRequest(field, f"no file at {path}")
            # Keep intermediate '..' until realpath follows each actual symlink.
            root, path = self._canonical_paths(root, path)
        try:
            path.relative_to(root)
        except ValueError:
            raise InvalidRequest(field, f"file is outside the project root: {path}") from None
        if not self.is_file(path):
            raise InvalidRequest(field, f"no file at {path}")
        return path

    def identity(self, path, root) -> FileIdentity:
        """The file's identity, hashed in the selected environment only."""
        if not self.container:
            return FileIdentity.of(Path(path), Path(root))
        root, path = self._canonical_paths(root, path)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            raise InvalidRequest("path", f"{path} does not lie under {root}") from None
        done = self.run(["sha256sum", "-z", "--", str(path)], 60, "project input identity")
        if done.returncode != 0:
            raise OSError(bounded(f"cannot hash container project file {path}: {done.stderr}"))
        digest = done.stdout[:64]
        if (re.fullmatch("[0-9a-f]{64}", digest) is None
                or done.stdout[64:] != f"  {path}\0"):
            raise OSError("container file hash report is incomplete or invalid")
        return FileIdentity(relative, digest)


def require_mounted_output(path: str, executor: Executor) -> None:
    """Refuse container reports or compiler output outside the host work directory."""
    if isinstance(executor, ContainerExecutor):
        try:
            Path(path).resolve().relative_to(Path(executor._host_work_dir).resolve())
        except (TypeError, ValueError, OSError):
            raise OSError("output must be in the container's mounted host work folder") from None


# The project's JDK: Java's counterpart of the project's Python interpreter.
#
# A Java context names its JDK in ``ProjectContext.interpreter`` by its
# ``java`` executable. The adapter compiles tests and runs suites, coverage
# and mutation with it: JDK 11 or newer, as far as the pinned tools support.

@dataclass(frozen=True)
class Jdk:
    """A JDK: its home, its feature version and its full version text."""

    home: str
    feature: int
    version: str

    def tool(self, name: str) -> str:
        """The path of one of the JDK's tools (``java``, ``javac``)."""
        suffix = ".exe" if os.name == "nt" else ""
        return str(Path(self.home) / "bin" / f"{name}{suffix}")


class _ContainerJdk(Jdk):
    def tool(self, name: str) -> str:
        return str(PurePosixPath(self.home) / "bin" / name)


def _version(text: str) -> Optional[str]:
    match = re.search(r'^JAVA_VERSION="([^"\r\n]+)"\r?$', text, re.MULTILINE)
    if match is None:
        return None
    version = match.group(1)
    try:
        feature_of(version)
    except (AttributeError, ValueError):
        return None
    return version


def read(path: str) -> Optional[Jdk]:
    """The JDK at ``path``, its home or its ``java`` executable, from the
    home's ``release`` file, or None when ``path`` is not a JDK. A macOS JDK
    bundle (``<name>.jdk``) has its home in ``Contents/Home``."""
    candidate = Path(path)
    if _host.started_file(path):
        candidate = candidate.parent.parent
    elif (candidate / "Contents" / "Home" / "release").is_file():
        candidate = candidate / "Contents" / "Home"
    release = candidate / "release"
    javac = candidate / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    if not release.is_file() or not javac.is_file():
        return None
    try:
        version = _version(release.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if version is None:
        return None
    return Jdk(str(candidate), feature_of(version), version)


def read_in_container(path: str, executor: Executor, *,
               environment: Optional[Mapping[str, str]] = None,
               deadline: float | None = None, strict: bool = False) -> Optional[Jdk]:
    """The Linux container JDK at ``path`` (its home or ``bin/java``), read
    through its executor with explicit argv and the selected environment,
    never from the host (``read`` does that), or None. Its compiler must
    start: a runtime-only image is no prepared JDK. ``strict`` raises an
    OSError when a probe command could not run, which is no answer about
    the JDK, where None means there is none."""
    candidate = PurePosixPath(path)
    if not candidate.is_absolute() or ".." in candidate.parts or "\\" in path:
        return None
    if candidate.name == "java" and candidate.parent.name == "bin":
        candidate = candidate.parent.parent
    env = environment if environment is not None else {}
    container = JavaEnv(executor, deadline=deadline)
    try:
        release = container.run(["cat", str(candidate / "release")], 120, "container JDK release",
                            env=env)
        version = _version(release.stdout or "") if release.returncode == 0 else None
        if version is None:
            return None
        compiler = container.run([str(candidate / "bin" / "javac"), "-version"], 120,
                             "container JDK compiler", env=env)
        if compiler.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError) as exc:
        reraise_unresolved(exc)
        if deadline is not None and isinstance(exc, subprocess.TimeoutExpired):
            raise
        if strict:
            raise OSError(f"the container JDK at {path} could not be probed: {exc}") from exc
        return None
    return _ContainerJdk(str(candidate), feature_of(version), version)


def version_of(jdk: Jdk):
    """The ``langadapter.versions.Version`` of ``jdk``: its release file's
    text, read on the host or, for a container JDK, through the container."""
    return java_version(jdk.version, "guest probe" if isinstance(jdk, _ContainerJdk) else "release file")


def feature_of(version: str) -> int:
    """The feature version of a Java version text (``1.8.0_412``: 8,
    ``21.0.11``: 21). ValueError for a text without a version number."""
    feature = java_version(version, "release file").feature
    if feature is None:
        raise ValueError(f"no Java version number in {version!r}")
    return feature
