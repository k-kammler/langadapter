"""Suites, test identifiers and typed per-test evidence items.

A suite is an explicit list of test files and setup files, never a directory.
Every reported test has one identifier, mapped from its tools' native forms, so
callers never parse identifiers. Per-test evidence is a set of typed items,
each with a canonical text form (its token) that ``parse_token`` reads back.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

from langadapter.errors import InvalidRequest
from langadapter.outcomes import FileIdentity, _relative_posix_path


@dataclass(frozen=True)
class Suite:
    """Test files and setup files under ``root``, a directory of this machine,
    resolved against the current directory when relative. Paths may be absolute
    or relative to ``root``, and each must name a file under it."""

    root: str
    test_files: Tuple[str, ...]
    setup_files: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "test_files", tuple(self.test_files))
        object.__setattr__(self, "setup_files", tuple(self.setup_files))
        # Runs start in other directories, so a relative root is resolved now.
        if isinstance(self.root, str) and self.root and not os.path.isabs(self.root):
            object.__setattr__(self, "root", os.path.abspath(self.root))

    def _resolve(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path(self.root) / candidate
        return candidate

    def validate(self) -> None:
        """Refuse, naming the path, a suite without test files, a directory
        given as a file, a missing file, or a file outside the root."""
        root = Path(self.root)
        if not root.is_dir():
            raise InvalidRequest("suite.root", f"no directory at {self.root}")
        if not self.test_files:
            raise InvalidRequest("suite.test_files", "names no test file")
        for kind, paths in (("suite.test_files", self.test_files),
                            ("suite.setup_files", self.setup_files)):
            for path in paths:
                resolved = self._resolve(path)
                if resolved.is_dir():
                    raise InvalidRequest(
                        kind, f"{path} is a directory. Name its files")
                if not resolved.is_file():
                    raise InvalidRequest(kind, f"no file at {path}")
                try:
                    resolved.resolve().relative_to(root.resolve())
                except ValueError:
                    raise InvalidRequest(
                        kind, f"{path} does not lie under {self.root}"
                    ) from None

    def files(self) -> Tuple[Path, ...]:
        """Every file of the suite, test files first, as absolute paths."""
        return tuple(self._resolve(p) for p in
                     self.test_files + self.setup_files)

    def identity(self) -> Tuple[FileIdentity, ...]:
        """Each file of the suite by its path relative to the root and its hash."""
        return tuple(FileIdentity.of(f, Path(self.root)) for f in self.files())


@dataclass(frozen=True)
class TestId:
    """One test: its POSIX file relative to the suite root, its qualified name
    in the file (``Class.test`` for a method) and its parameters if any.
    ``text`` is the language's canonical form, set by the adapter (for Python
    the node id ``path::Class::test[params]``). Identifiers compare by parts."""

    __test__ = False  # not a test class for pytest's collection

    file: str
    qualname: str
    params: Optional[str] = None
    text: str = ""

    def __post_init__(self) -> None:
        if not _relative_posix_path(self.file):
            raise InvalidRequest(
                "file", f"must be a relative POSIX path: {self.file!r}")
        if not self.qualname:
            raise InvalidRequest("qualname", "is required")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TestId):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def _key(self) -> Tuple[str, str, Optional[str]]:
        return (self.file, self.qualname, self.params)

    def __str__(self) -> str:
        return self.text or f"{self.file}::{self.qualname}" + (
            f"[{self.params}]" if self.params is not None else "")


@dataclass(frozen=True, order=True)
class LineItem:
    """A covered line of a file."""

    file: str
    line: int

    def token(self) -> str:
        """The canonical token, ``L:file:line``."""
        return f"L:{self.file}:{self.line}"


@dataclass(frozen=True, order=True)
class BranchItem:
    """A covered branch from line ``source`` to ``destination``. A negative one
    leaves the code object starting at line ``-destination``, as coverage records it."""

    file: str
    source: int
    destination: int

    @property
    def leaves_code_object(self) -> bool:
        """Whether the branch leaves a code object: a negative ``destination``."""
        return self.destination < 0

    def token(self) -> str:
        """The canonical token, ``B:file:source->destination``."""
        return f"B:{self.file}:{self.source}->{self.destination}"


@dataclass(frozen=True, order=True)
class KillItem:
    """A mutant the test kills, by the mutant's key."""

    mutant: str

    def token(self) -> str:
        """The canonical token, ``K:mutant``."""
        return f"K:{self.mutant}"


@dataclass(frozen=True, order=True)
class CrashItem:
    """A mutant, by key, that ended the test process while the test ran and
    had no named killer: an observation, not a proven kill. A caller may keep
    the test for it, but the test does not count as its killer."""

    mutant: str

    def token(self) -> str:
        """The canonical token, ``C:mutant``."""
        return f"C:{self.mutant}"


EvidenceItem = Union[LineItem, BranchItem, KillItem, CrashItem]
"""One item of a test's evidence: a covered line, a covered branch, a killed
mutant or a mutant that ended the test's process (``parse_token`` reads one
from its token)."""


def parse_token(token: str) -> EvidenceItem:
    """The typed item of a canonical token, or InvalidRequest for an unknown kind."""
    kind, _, rest = token.partition(":")
    try:
        if kind == "L":
            file, _, line = rest.rpartition(":")
            if file:
                return LineItem(file, int(line))
        elif kind == "B":
            file, _, arc = rest.rpartition(":")
            source, arrow, destination = arc.partition("->")
            if file and arrow:
                return BranchItem(file, int(source), int(destination))
        elif kind == "K" and rest:
            return KillItem(rest)
        elif kind == "C" and rest:
            return CrashItem(rest)
    except ValueError:
        pass
    raise InvalidRequest("token", f"not an evidence token: {token!r}")
