"""Creation receipts for disposable directories allocated by the adapter."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from langadapter.errors import OwnershipRefused


def _long_path(path: str) -> str:
    """``path`` with Windows 8.3 short names expanded (such as
    ``C:\\Users\\LONGNA~1\\AppData\\Local\\Temp``), as commands in it print it,
    or its existing part for a path not made yet. Unchanged elsewhere, and
    links stay as they are."""
    if os.name != "nt" or "~" not in path:
        return path
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetLongPathNameW(path, buffer, len(buffer))
    if 0 < length < len(buffer):
        return buffer.value
    parent, name = os.path.split(path)
    if not name or parent == path:
        return path
    return os.path.join(_long_path(parent), name)


def _temporary_root() -> str:
    """The system's temporary directory by its physical path, where owned
    directories go by default. Reached through a link (macOS's TMPDIR lies below
    /var, a link to /private/var), a directory there has two spellings, and a
    command given one beside a resolved path (pytest's rootdir) mixes them."""
    return _long_path(os.path.realpath(tempfile.gettempdir()))


def _remove_tree(root: Path) -> None:
    """Remove a verified owned tree. An entry whose removal is refused is
    retried once after what refuses it gains owner write, inside the tree
    only: on Windows the readonly entry itself, on POSIX the directory holding
    it, since a copy keeps a source directory's mode."""
    root = root.absolute()

    def readonly(function, path, info):
        error = info[1]
        if not isinstance(error, PermissionError) or function not in (os.unlink, os.remove, os.rmdir):
            raise error
        candidate = Path(path).absolute()
        if os.name != "nt":
            candidate = candidate.parent
        try:
            candidate.relative_to(root)
        except ValueError:
            raise error
        if ".." in candidate.parts:
            raise error
        identity = None
        for entry in (candidate, *candidate.parents):
            try:
                observed = entry.lstat()
            except OSError as secondary:
                raise error from secondary
            if (stat.S_ISLNK(observed.st_mode)
                    or getattr(observed, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                raise error
            if entry == candidate:
                identity = observed
            if entry == root:
                break
        assert identity is not None
        if (not (stat.S_ISREG(identity.st_mode) or stat.S_ISDIR(identity.st_mode))
                or identity.st_mode & stat.S_IWUSR
                or (stat.S_ISREG(identity.st_mode) and identity.st_nlink != 1)):
            raise error
        # Where the host can, the change never follows a link swapped in after the check.
        unfollowed = {"follow_symlinks": False} if os.chmod in os.supports_follow_symlinks else {}
        try:
            os.chmod(candidate, identity.st_mode | stat.S_IWUSR, **unfollowed)
            function(path)
        except OSError as secondary:
            raise error from secondary

    shutil.rmtree(root, onerror=readonly)


@dataclass(frozen=True)
class OwnedDirectory:
    root: Path
    receipt: Path
    device: int
    inode: int
    token: str

    @classmethod
    def create(cls, *, parent: Optional[str] = None,
               prefix: str = "langadapter_",
               receipt_parent: Optional[str] = None) -> OwnedDirectory:
        root = Path(_long_path(tempfile.mkdtemp(
            prefix=prefix, dir=parent if parent is not None else _temporary_root()))).absolute()
        creation = _DirectoryCreation(
            root, receipt_parent if receipt_parent is not None else _temporary_root())
        try:
            creation.identity = root.lstat()
            return creation.finish(cls)
        except BaseException as error:
            error._langadapter_owned_directories = (
                *getattr(error, "_langadapter_owned_directories", ()), creation)
            raise

    @property
    def marker(self) -> Path:
        return self.root / ".langadapter-owner"

    def _record(self) -> dict:
        return {"schema": 1, "root": str(self.root), "device": self.device,
                "inode": self.inode, "token": self.token}

    def _verify_receipt(self) -> None:
        try:
            record = json.loads(self.receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OwnershipRefused(
                f"ownership cannot be verified; directory retained: {self.root}") from exc
        if record != self._record():
            raise OwnershipRefused(
                f"creation receipt does not match; directory retained: {self.root}")

    def verify(self) -> None:
        self._verify_receipt()
        try:
            identity = self.root.lstat()
        except OSError as exc:
            raise OwnershipRefused(
                f"ownership cannot be verified; directory retained: {self.root}") from exc
        if (not stat.S_ISDIR(identity.st_mode)
                or (identity.st_dev, identity.st_ino) != (self.device, self.inode)
                or getattr(self.root, "is_junction", lambda: False)()):
            raise OwnershipRefused(
                f"creation receipt does not match; directory retained: {self.root}")
        try:
            token = self.marker.read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            raise OwnershipRefused(
                f"creation marker is unavailable; directory retained: {self.root}") from exc
        if token != self.token:
            raise OwnershipRefused(
                f"creation marker does not match; directory retained: {self.root}")

    def remove(self) -> None:
        """Remove only this still-matching directory, keeping failed receipts. It
        is an exclusive-use resource, not a sandbox against an adversary
        changing paths between file system calls."""
        self._verify_receipt()
        if os.path.lexists(self.root):
            self.verify()
            _remove_tree(self.root)
        if os.path.lexists(self.root):
            raise OwnershipRefused(f"owned directory was not removed: {self.root}")
        self.receipt.unlink()


@dataclass
class _DirectoryCreation:
    """Retain actual allocations until their creation receipt can be finished."""

    root: Path
    receipt_parent: Optional[str]
    identity: Optional[os.stat_result] = None
    receipt: Optional[Path] = None
    receipt_identity: Optional[os.stat_result] = None
    marker_identity: Optional[os.stat_result] = None
    token: str = ""
    removed: bool = False
    finished: Optional[OwnedDirectory] = None

    def _verify_root(self) -> None:
        if self.identity is None:
            raise OwnershipRefused(f"allocation identity was not captured; retained: {self.root}")
        observed = self.root.lstat()
        if (not stat.S_ISDIR(observed.st_mode)
                or (observed.st_dev, observed.st_ino) != (
                    self.identity.st_dev, self.identity.st_ino)
                or getattr(self.root, "is_junction", lambda: False)()):
            raise OwnershipRefused(f"allocated directory was replaced; retained: {self.root}")

    @staticmethod
    def _write_acquired(path: Path, identity: Optional[os.stat_result], value: str) -> None:
        if identity is None:
            raise OwnershipRefused(f"allocated file identity is unavailable; retained: {path}")
        observed = path.lstat()
        if (not stat.S_ISREG(observed.st_mode)
                or (observed.st_dev, observed.st_ino) != (identity.st_dev, identity.st_ino)):
            raise OwnershipRefused(f"allocated file was replaced; retained: {path}")
        handle = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino):
                raise OwnershipRefused(f"allocated file was replaced while opening: {path}")
            stream.truncate(0)
            stream.write(value)

    def finish(self, cls=OwnedDirectory) -> OwnedDirectory:
        self._verify_root()
        if not self.token:
            self.token = uuid.uuid4().hex
        if self.receipt is None:
            handle, path = tempfile.mkstemp(
                prefix="langadapter-ownership-", suffix=".json", dir=self.receipt_parent)
            self.receipt = Path(path)
            try:
                self.receipt_identity = os.fstat(handle)
            finally:
                os.close(handle)
        assert self.identity is not None
        owned = cls(self.root, self.receipt, self.identity.st_dev,
                    self.identity.st_ino, self.token)
        self._write_acquired(self.receipt, self.receipt_identity,
                             json.dumps(owned._record(), sort_keys=True))
        if self.marker_identity is None:
            # An existing marker is not ours merely because the root is ours.
            handle = os.open(owned.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                self.marker_identity = os.fstat(handle)
            finally:
                os.close(handle)
        self._write_acquired(owned.marker, self.marker_identity, self.token)
        owned.verify()
        return owned

    def remove(self) -> None:
        if not self.removed:
            if self.finished is None:
                self.finished = self.finish()
            self.finished.remove()
            self.removed = True
