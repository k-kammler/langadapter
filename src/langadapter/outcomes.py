"""The outcome and the identity every result of an executing operation carries.

Each result has exactly one outcome and a reason. An execution failure is an
outcome, not an exception, unless a command the operation started may still
run: then the original exception is raised, and the prepared copy and its
owner stay until the command is cleaned up. A value the adapter did not
observe is absent (``None``), never zero, full coverage or success.

The identity names the target, suite, toolchain, instrument and execution,
never a machine's path: files are named relative to the project or suite root
and identified by their content's hash, so the same bytes and toolchain carry
the same identity on any machine. Diagnostics may name absolute paths.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Optional, Tuple
from langadapter.errors import InvalidRequest


class Outcome(str, enum.Enum):
    """What happened when an operation executed a tool."""

    #: The operation ran and its observation is valid. For a suite run, no
    #: collected test failed or raised an error (skips and expected failures aside).
    COMPLETED = "completed"
    #: The suite ran and at least one test failed or raised an error.
    TEST_FAILURES = "test_failures"
    #: Nothing was collected.
    NO_TESTS = "no_tests"
    #: An error during collection stopped the run before its tests ran.
    COLLECTION_FAILED = "collection_failed"
    #: The operation reached its timeout.
    TIMED_OUT = "timed_out"
    #: A tool crashed, its report is unreadable, a session or file it needs
    #: is gone, or an instrument check failed.
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    #: The language, engine or context does not provide the operation.
    UNSUPPORTED = "unsupported"
    #: A tool or file the operation needs is absent from the project environment.
    MISSING_SETUP = "missing_setup"

    def __str__(self) -> str:
        return self.value


def _relative_posix_path(value: Any) -> bool:
    """Whether ``value`` is a non-empty relative POSIX path: a string with
    no backslash that is neither absolute nor on a Windows drive."""
    return (isinstance(value, str) and bool(value) and "\\" not in value
            and not PurePosixPath(value).is_absolute() and not PureWindowsPath(value).drive)


@dataclass(frozen=True)
class FileIdentity:
    """A file by its relative POSIX path and the SHA-256 of its bytes."""

    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not _relative_posix_path(self.path):
            raise InvalidRequest(
                "path", f"must be a relative POSIX path: {self.path!r}")

    @classmethod
    def of(cls, file: Path, root: Path) -> "FileIdentity":
        """The identity of ``file``, named relative to ``root``."""
        try:
            relative = Path(file).resolve().relative_to(Path(root).resolve())
        except ValueError:
            raise InvalidRequest(
                "path", f"{file} does not lie under {root}") from None
        return cls(relative.as_posix(),
                   hashlib.sha256(Path(file).read_bytes()).hexdigest())

    def to_dict(self) -> Dict[str, str]:
        """The identity as plain data: ``path`` and ``sha256``."""
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class ToolchainIdentity:
    """The interpreter's implementation and version, each tool's version by
    distribution, and the source operations' parser and grammar, if one ran."""

    implementation: str
    version: str
    tools: Tuple[Tuple[str, str], ...] = ()
    structural_parser: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(sorted(self.tools)))

    # Records persist the runtime's implementation and version as text (to_dict,
    # repr). Adapters build the identity only through ``of``, from a ``Version``.

    @classmethod
    def of(cls, version: Any, tools: Tuple[Tuple[str, str], ...] = (),
           structural_parser: Optional[str] = None) -> "ToolchainIdentity":
        """The identity of a toolchain whose runtime is ``version`` (a
        ``langadapter.versions.Version``), with its version text as found."""
        return cls(version.implementation, version.text, tuple(tools), structural_parser)

    @classmethod
    def recorded(cls, implementation: str, version: str, tools: Tuple[Tuple[str, str], ...] = (),
                 structural_parser: Optional[str] = None) -> "ToolchainIdentity":
        """An identity read back from a record, with its texts as recorded."""
        return cls(implementation, version, tuple(tools), structural_parser)

    def to_dict(self) -> Dict[str, Any]:
        """The identity as plain data, with ``tools`` as a mapping from each
        tool to its version."""
        return {
            "implementation": self.implementation,
            "version": self.version,
            "tools": dict(self.tools),
            "structural_parser": self.structural_parser,
        }


@dataclass(frozen=True)
class InstrumentIdentity:
    """An instrument that ran with the suite, by name and version."""

    name: str
    version: str

    def to_dict(self) -> Dict[str, str]:
        """The identity as plain data: ``name`` and ``version``."""
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True)
class ExecutionIdentity:
    """Where the observation ran: ``local``, or ``container`` with its image if
    known. ``supervision`` is ``pidfd``, ``process_group`` (no process
    descriptors), ``unreported`` (a container helper before its first command),
    ``process_group_unconfirmed`` (a non-Linux POSIX host, which ends a group
    without confirming every descendant ended), or None (unsupervised).
    ``environment`` holds the variables a derived context added
    (``ProjectContext.derived``), with its work directory written ``<work_dir>``,
    or None for a context that was not derived."""

    kind: str
    image: Optional[str] = None
    supervision: Optional[str] = None
    environment: Optional[Tuple[Tuple[str, str], ...]] = None

    def __post_init__(self) -> None:
        if self.kind not in ("local", "container"):
            raise InvalidRequest("kind", "must be local or container")
        if self.supervision not in (None, "pidfd", "process_group", "unreported",
                                    "process_group_unconfirmed"):
            raise InvalidRequest("supervision", "must be pidfd, process_group, unreported, "
                                 "process_group_unconfirmed or None")

    def to_dict(self) -> Dict[str, Optional[str]]:
        """The identity as plain data: ``kind`` and ``image``, and
        ``supervision`` and ``environment`` only when they are set."""
        # A record of unsupervised commands has no supervision key.
        record = {"kind": self.kind, "image": self.image}
        if self.supervision is not None:
            record["supervision"] = self.supervision
        if self.environment is not None:
            record["environment"] = dict(self.environment)
        return record


@dataclass(frozen=True)
class ObservationIdentity:
    """The identity every observation carries. ``target`` is None without a
    target (a suite run without coverage), ``instrument`` when none ran."""

    target: Optional[FileIdentity]
    suite: Tuple[FileIdentity, ...]
    toolchain: ToolchainIdentity
    instrument: Optional[InstrumentIdentity]
    execution: ExecutionIdentity

    def __post_init__(self) -> None:
        object.__setattr__(self, "suite", tuple(
            sorted(self.suite, key=lambda f: f.path)))

    def to_dict(self) -> Dict[str, Any]:
        """The identity as plain data, each part in its own ``to_dict``
        form, None for an absent target or instrument."""
        return {
            "target": self.target.to_dict() if self.target else None,
            "suite": [f.to_dict() for f in self.suite],
            "toolchain": self.toolchain.to_dict(),
            "instrument": (self.instrument.to_dict()
                           if self.instrument else None),
            "execution": self.execution.to_dict(),
        }
