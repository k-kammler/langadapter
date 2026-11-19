"""Private project snapshots with exclusive, receipt-backed restoration, and
the disposable outputs created in them.

Registered command and mutation-session owners must confirm disposal before a
lease is released. ``CreatedFiles`` removes outputs between runs: an inventory
difference is not authorship, so the active preparation lease grants disposal,
and unrelated writers and durable results must stay outside the copied root.
"""
from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Optional, Sequence, Tuple, Dict, FrozenSet, List, Set
from langadapter import _host, _process_tree
from langadapter._execution_scope import _CURRENT, _Scope
from langadapter._owned_directory import (
    OwnedDirectory, OwnershipRefused, _long_path, _remove_tree, _temporary_root,
)
from langadapter.context import ContainerExecution, ProjectContext, ProjectInputSelection
from langadapter.execution import (
    ContainerExecutor,
    Executor,
    _mounted_work_dir,
    container_path,
)


def _path_spellings(pairs: Iterable[Tuple[Any, str]]) -> Tuple[Tuple[str, str], ...]:
    """(path, original) spellings of each (path, original) pair, longest
    first: the path as given and resolved, each with either slash."""
    forms = {}
    for path, original in pairs:
        for spelling in dict.fromkeys((str(path), os.path.realpath(path))):
            forms[spelling] = original
            forms[spelling.replace("\\", "/")] = original.replace("\\", "/")
    return tuple(sorted(forms.items(), key=lambda item: -len(item[0])))


def _rewrite_paths(text: str, pairs: Sequence[Tuple[str, str]], *, ignore_case: bool) -> str:
    """``text`` with each (path, original) pair's path, and its repr,
    written as the original. A root original ("/", "C:\\") drops its
    separator before a path below it."""
    flags = re.IGNORECASE if ignore_case else 0
    for path, original in pairs:
        spellings = [(path, original)]
        if "\\" in path:
            # A string's repr writes each backslash twice: 'C:\\Users'.
            spellings.append((path.replace("\\", "\\\\"), original.replace("\\", "\\\\")))
        for path_form, original_form in spellings:
            below = original_form.rstrip("\\/")
            pattern = re.escape(path_form)
            text = re.sub(pattern + r"(?=[\\/])", lambda _: below, text, flags=flags)
            text = re.sub(pattern, lambda _: original_form, text, flags=flags)
    return text


class WorkspaceBusy(OwnershipRefused):
    """Another operation, or an uncertain interrupted operation, owns the lease."""


class WorkspacePreparationFailed(RuntimeError):
    """Preparation failed. Directories already allocated and their receipts are kept."""


def _timeout(deadline, stage, bound=None):
    if deadline is None:
        return bound
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(stage, 0)
    return remaining if bound is None else min(bound, remaining)


def _ordinary(path: Path) -> os.stat_result:
    identity = path.lstat()
    if (stat.S_ISLNK(identity.st_mode)
            or getattr(identity, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise OwnershipRefused(f"project snapshots cannot contain links: {path}")
    if not (stat.S_ISDIR(identity.st_mode) or stat.S_ISREG(identity.st_mode)):
        raise OwnershipRefused(f"project snapshots require ordinary files: {path}")
    return identity


def _copy_tree(source: Path, destination: Path, *, selection=None, deadline=None) -> None:
    """Copy bytes, never hard-link files or follow directory/file links."""
    _timeout(deadline, "project copying")
    from langadapter import _input_copy

    if selection is not None:
        options = {} if deadline is None else {"checkpoint": lambda: _timeout(deadline, "project copying")}
        _input_copy.tree(source, selection.to_dict(), destination=destination,
                         case_sensitive=os.name != "nt", **options)
        return
    if not stat.S_ISDIR(_ordinary(source).st_mode):
        raise OwnershipRefused(f"project source is not a directory: {source}")
    destination.mkdir()
    for event, path in _walk(source, derived=_input_copy.derived,
                             recheck="project source is not a directory: {}",
                             check=lambda: _timeout(deadline, "project copying")):
        target = destination / path.relative_to(source)
        if event == "enter":
            target.mkdir()
        elif event == "file":
            shutil.copy2(path, target, follow_symlinks=False)
            _timeout(deadline, "project copying")
            _ordinary(target)
        else:
            shutil.copystat(path, target, follow_symlinks=False)
            _timeout(deadline, "project copying")


def _walk(root: Path, *, derived=None, recheck: Optional[str] = None, check=None):
    """``root``'s ordinary entries depth first, links refused (``_ordinary``):
    ("enter", directory) before a directory's entries, ("file", path) for a
    file, ("leave", directory) after a directory's entries, ``root``'s own
    "leave" last. ``derived`` names are skipped, ``recheck`` (a message, {}
    for the directory) refuses a directory that is no longer one when it is
    listed, and ``check`` runs before each entry."""
    def visit(directory, top=False):
        if recheck is not None and not stat.S_ISDIR(_ordinary(directory).st_mode):
            raise OwnershipRefused(recheck.format(directory))
        if not top:
            yield "enter", directory
        with os.scandir(directory) as entries:
            for entry in entries:
                if check is not None:
                    check()
                if derived is not None and derived(entry.name):
                    continue
                path = directory / entry.name
                if stat.S_ISDIR(_ordinary(path).st_mode):
                    yield from visit(path)
                else:
                    yield "file", path
        yield "leave", directory

    yield from visit(root, top=True)


def _preflight_tree(source: Path, *, selection=None, deadline=None) -> None:
    """Validate ordinary inputs before allocation. Copying rechecks every path."""
    _timeout(deadline, "project input preflight")
    from langadapter import _input_copy

    if selection is not None:
        options = {} if deadline is None else {"checkpoint": lambda: _timeout(deadline, "project input preflight")}
        _input_copy.tree(source, selection.effective(case_sensitive=os.name != "nt").to_dict(),
                         case_sensitive=os.name != "nt", **options)
        return
    if not stat.S_ISDIR(_ordinary(source).st_mode):
        raise OwnershipRefused(f"project source is not a directory: {source}")
    for _event in _walk(source, derived=_input_copy.derived,
                        recheck="project input changed before enumeration: {}",
                        check=lambda: _timeout(deadline, "project input preflight")):
        pass
    _timeout(deadline, "project input preflight")


def _mirrored(source, companions, *, common=os.path.commonpath, relative=os.path.relpath):
    """The project's parts, each companion's parts (a suite outside the project)
    below the directory they share, and that directory. Refused when they share none
    (different drives) or when one of the directories contains another."""
    try:
        shared = common([source, *companions])
    except ValueError:
        raise OwnershipRefused(
            f"the project {source} and its suite directory share no common directory, for "
            f"example because they are on different drives: place the suite on the "
            f"project's drive to measure it") from None
    mirrored_dirs = [source, *companions]
    for index, mirrored_dir in enumerate(mirrored_dirs):
        for other in mirrored_dirs[index + 1:]:
            if common([mirrored_dir, other]) in (mirrored_dir, other):
                raise OwnershipRefused(
                    f"a suite directory must lie beside the project, not around or inside it "
                    f"or another suite directory: {mirrored_dir}, {other}")

    def parts(path):
        return tuple(part for part in relative(path, shared).replace("\\", "/").split("/")
                     if part not in ("", "."))

    return parts(source), tuple(parts(companion) for companion in companions), shared


#: The owned directory's name: this prefix and the eight characters mkdtemp adds.
_PROJECT_PREFIX = "langadapter_project_"


def _compact(layout, placed):
    """``layout`` with the project's own intermediate directories (below its last one
    shared with a companion) named by one character, which keeps relative paths
    the same. The project directory's name and every companion's path stay."""
    shared = max(next((index for index, (mine, theirs) in enumerate(zip(layout, parts))
                       if mine != theirs), min(len(layout), len(parts))) for parts in placed)
    own = len(layout) - 1 - shared
    if own <= 0:
        return layout
    # The first shortened directory sits beside companion directories: avoid their names.
    taken = {parts[shared] for parts in placed
             if len(parts) > shared and tuple(parts[:shared]) == tuple(layout[:shared])}
    first = next(name for name in "pqrstuvwxyz" if name not in taken)
    return (*layout[:shared], first, *("p",) * (own - 1), layout[-1])


def _working_dir_layout(parent: str, mirror: dict) -> dict:
    """``mirror`` for a copy below ``parent``, compacted where the project directory
    (the commands' working directory) would pass this host's limit, with
    ``original_layout`` kept. Refused before copying if still past it."""
    limit = _host.working_dir_limit()
    if limit is None:
        return mirror
    owned = os.path.join(_long_path(os.path.abspath(parent)), _PROJECT_PREFIX + "x" * 8)
    layout = mirror.get("layout", ("project",))
    if len(os.path.join(owned, *layout)) <= limit:
        return mirror
    compact = layout
    if mirror.get("companions"):
        compact = _compact(layout, [parts for _, parts in mirror["companions"]])
        if len(os.path.join(owned, *compact)) <= limit:
            return dict(mirror, layout=compact, original_layout=layout)
    length = len(os.path.join(owned, *compact))
    raise WorkspacePreparationFailed(
        f"the project copy's directory, where the project's commands run, would be {length} "
        f"characters long ({os.path.join(owned, *compact)}), and Windows starts no command in a "
        f"directory longer than {limit} characters, even with long paths enabled: choose a shorter "
        f"directory for the copy (parent=, or a shorter TEMP), or place the suite directories nearer "
        f"the project")


def _outside_source(parent: str, source: Path) -> None:
    try:
        Path(parent).resolve().relative_to(source.resolve())
    except ValueError:
        return
    raise OwnershipRefused("workspace and receipt parents must be outside the source tree")


def copy_container_tree(executor: Executor, source: PurePosixPath, target: Path, *, deadline=None) -> None:
    """Copy the container directory ``source`` inside the container to ``target``, a new host
    directory inside the work directory, as a prepared container project is copied. A source
    holding anything but directories and regular files is refused first."""
    _container_directory(executor, source, deadline)
    _container_ordinary(executor, source, deadline)
    _container_copy(executor, source, target, deadline)


def _container_directory(executor, source, deadline) -> None:
    directory = executor.run(["test", "-d", str(source)], timeout=_timeout(deadline, "root admission in the container", 120))
    if directory.returncode != 0:
        raise OwnershipRefused(f"container project source is not a directory: {source}")


def _container_ordinary(executor, source, deadline) -> None:
    ordinary = executor.run(
        ["find", "-P", str(source), "!", "(", "-type", "d", "-o", "-type",
         "f", ")", "-print0"], timeout=_timeout(deadline, "input preflight in the container", 120))
    if ordinary.returncode != 0 or ordinary.stdout:
        raise OwnershipRefused(f"container source contains links/special files or could not be read: {source}")


def _container_copy(executor, source, target, deadline) -> None:
    copied = executor.run(["cp", "-R", "-P", "--", str(source) + "/.",
                           str(target)], timeout=_timeout(deadline, "project copying in the container", 600))
    if copied.returncode != 0:
        raise OSError(f"container project copy failed: {copied.stdout}{copied.stderr}")


@dataclass(frozen=True)
class PreparedCopy:
    """An exclusively used snapshot, not a sandbox for untrusted test code.
    Independent writers and valuable files must stay outside both private roots
    and the source stable while copied. Only ``create`` and ``create_in_container``
    establish ownership: there is no receipt adoption."""

    owned: OwnedDirectory
    baseline: OwnedDirectory
    container_context: Optional[ProjectContext] = None
    container_executor: Optional[Executor] = None
    selection: Optional[ProjectInputSelection] = None
    #: The project's path below the owned directory: "project", or its path below the
    #: directory shared with companions, shortened where the host's limit requires.
    layout: Tuple[str, ...] = ("project",)
    #: Each companion directory (a suite outside the project) and its owned path.
    companions: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    #: The directory the owned copy stands for, as the run sees it: the project,
    #: or the directory it shares with its companions.
    original: Optional[str] = None
    #: The project's path below ``original`` where ``layout`` shortens it.
    original_layout: Optional[Tuple[str, ...]] = None

    @property
    def root(self) -> Path:
        return self.owned.root.joinpath(*self.layout)

    @property
    def original_root(self) -> Optional[str]:
        """The original project directory, as the run sees it."""
        if self.original is None or not self.companions:
            return self.original
        join = posixpath.join if self.container_context is not None else os.path.join
        return join(self.original, *(self.original_layout or self.layout))

    @property
    def mirror(self) -> Path:
        """The owned directory that stands for ``original``."""
        return self.owned.root if self.companions else self.root

    def companion_root(self, index: int) -> Path:
        return self.owned.root.joinpath(*self.companions[index][1])

    def _companion_snapshot(self, index: int) -> Path:
        return self.baseline.root / "companions" / str(index)

    def text_map(self) -> Tuple[Tuple[str, str], ...]:
        """(copied, original) spellings of the owned directory, longest first: as
        created and resolved, with either slash, or in a container the container's."""
        if self.original is None:
            return ()
        if self.container_context is not None:
            return ((container_path(self.container_context.execution, str(self.mirror)),
                     self.original),)
        copies = [(self.mirror, self.original)]
        if self.original_layout is not None:
            # A shortened project directory stands for the original project.
            copies.append((self.root, self.original_root))
        return _path_spellings(copies)

    def original_text(self, text: str) -> str:
        """``text`` with the owned copy's paths written as the original's. A
        root original ("/", "C:\\") drops its separator before a path below it."""
        return _rewrite_paths(text, self.text_map(),
                              ignore_case=os.name == "nt" and self.container_context is None)

    @property
    def inputs(self) -> Path:
        return self.baseline.root / "inputs"

    @property
    def lease(self) -> Path:
        return self.owned.receipt.with_suffix(".lease")

    @classmethod
    def create(cls, source: str, *, parent: Optional[str] = None,
               receipt_parent: Optional[str] = None,
               inputs: Optional[ProjectInputSelection] = None, deadline=None,
               companions: Sequence[str] = ()) -> PreparedCopy:
        source_path = Path(source).absolute()
        outside = tuple(Path(path).absolute().resolve() for path in companions)
        if inputs is not None:
            if not isinstance(inputs, ProjectInputSelection):
                raise OwnershipRefused("workspace inputs require ProjectInputSelection")
            inputs = inputs.effective(case_sensitive=os.name != "nt")
        if not stat.S_ISDIR(_ordinary(source_path).st_mode):
            raise OwnershipRefused(f"project source is not a directory: {source_path}")
        temporary = _temporary_root()
        for location in (parent or temporary, receipt_parent or temporary, temporary):
            for copied_dir in (source_path, *outside):
                _outside_source(location, copied_dir)
        if outside:
            layout, placed, shared = _mirrored(str(source_path.resolve()), [str(p) for p in outside])
            mirror = {"layout": layout, "original": shared,
                      "companions": tuple((str(path), parts) for path, parts in zip(outside, placed))}
            for copied_dir in outside:
                if not stat.S_ISDIR(_ordinary(copied_dir).st_mode):
                    raise OwnershipRefused(f"suite directory is not a directory: {copied_dir}")
        else:
            mirror = {"original": str(source_path)}
        mirror = _working_dir_layout(parent or temporary, mirror)
        try:
            options = {} if deadline is None else {"deadline": deadline}
            if inputs is None:
                _preflight_tree(source_path, **options)
            else:
                _preflight_tree(source_path, selection=inputs, **options)
            for copied_dir in outside:
                _preflight_tree(copied_dir, **options)
        except Exception as exc:
            if isinstance(exc, subprocess.TimeoutExpired):
                raise
            raise WorkspacePreparationFailed(
                "project input preflight failed before workspace allocation: "
                f"{type(exc).__name__}: {exc}") from exc
        _timeout(deadline, "project allocation")
        owned = OwnedDirectory.create(parent=parent, prefix="langadapter_project_",
                                      receipt_parent=receipt_parent)
        try:
            _timeout(deadline, "project baseline allocation")
            baseline = OwnedDirectory.create(prefix="langadapter_inputs_",
                                             receipt_parent=receipt_parent)
        except BaseException as exc:
            exc._langadapter_owned_directories = (
                owned, *getattr(exc, "_langadapter_owned_directories", ()))
            raise
        prepared_copy = cls(owned, baseline, selection=inputs, **mirror)
        try:
            if inputs is None:
                _copy_tree(source_path, prepared_copy.inputs, **options)
            else:
                _copy_tree(source_path, prepared_copy.inputs, selection=inputs, **options)
            prepared_copy.root.parent.mkdir(parents=True, exist_ok=True)
            _copy_tree(prepared_copy.inputs, prepared_copy.root, **options)
            prepared_copy._copy_companions(**options)
        except BaseException as exc:
            exc._langadapter_prepared_copy = prepared_copy
            if isinstance(exc, (KeyboardInterrupt, SystemExit, subprocess.TimeoutExpired)):
                raise
            failure = WorkspacePreparationFailed(
                f"project copy failed. Retained roots: {owned.root}, {baseline.root}. "
                f"Receipts: {owned.receipt}, {baseline.receipt}. "
                f"{type(exc).__name__}: {exc}")
            failure._langadapter_prepared_copy = prepared_copy
            raise failure from exc
        return prepared_copy

    @classmethod
    def create_in_container(cls, context: ProjectContext, executor: Executor, *,
                     receipt_parent: Optional[str] = None,
                     inputs: Optional[ProjectInputSelection] = None, deadline=None,
                     companions: Sequence[str] = ()) -> PreparedCopy:
        """Snapshot container inputs through the selected, explicitly mapped executor.
        Companion directories are host directories in the work directory."""
        context.validate()
        execution = context.execution
        if not isinstance(execution, ContainerExecution):
            raise OwnershipRefused("a prepared copy in the container requires ContainerExecution")
        if (not isinstance(executor, ContainerExecutor)
                or executor.container != execution.container
                or executor.runner != execution.runner):
            raise OwnershipRefused("a prepared copy in the container requires the context's container executor")
        selection_command = None
        if inputs is not None:
            if not isinstance(inputs, ProjectInputSelection):
                raise OwnershipRefused("prepared copy inputs require ProjectInputSelection")
            inputs = inputs.effective(case_sensitive=True)
            if (context.execution.helper_python is None
                    or executor.helper_python != context.execution.helper_python):
                raise OwnershipRefused("selected copying in the container requires the helper Python (execution.helper_python)")
            from langadapter import _input_copy

            # Isolated, without site and outside the project: neither a
            # project module (a root shutil.py) nor a .pth file runs in it.
            selection_command = [context.execution.helper_python, "-I", "-S", "-c",
                                 Path(_input_copy.__file__).read_text(encoding="utf-8")]
        probe = str(Path(execution.work_dir) / ".mapping-probe")
        if executor._to_container(probe) != container_path(execution, probe):
            raise OwnershipRefused("the container executor's mapping does not match the execution context")
        source = PurePosixPath(context.root)
        if ".." in source.parts or "\\" in context.root:
            raise OwnershipRefused("the source in the container must be a normalized absolute POSIX path")
        mapped = PurePosixPath(_mounted_work_dir(execution))
        if mapped == source or source in mapped.parents:
            raise OwnershipRefused("the shared work directory (execution.work_dir) must be outside the source tree")
        outside = tuple(Path(path).absolute().resolve() for path in companions)
        # The companion directories, and a project in the work directory, lie on the host too.
        hosted = (*outside, *([Path(execution.work_dir) / source.relative_to(mapped)]
                              if mapped in source.parents else []))
        temporary = _temporary_root()
        for location in (receipt_parent or temporary, temporary):
            for hosted_dir in hosted:
                _outside_source(location, hosted_dir)
        mirror = {"original": str(source)}
        if outside:
            work_dir = Path(execution.work_dir).resolve()
            for hosted_dir in outside:
                if not hosted_dir.is_relative_to(work_dir) or not stat.S_ISDIR(_ordinary(hosted_dir).st_mode):
                    raise OwnershipRefused(f"a container's suite directory must lie in the work directory: {hosted_dir}")
                _preflight_tree(hosted_dir, **({} if deadline is None else {"deadline": deadline}))
            container = [str(mapped.joinpath(*hosted_dir.relative_to(work_dir).parts)) for hosted_dir in outside]
            layout, placed, shared = _mirrored(str(source), container, common=posixpath.commonpath,
                                               relative=posixpath.relpath)
            mirror = {"layout": layout, "original": shared,
                      "companions": tuple((str(path), parts) for path, parts in zip(outside, placed))}
        _container_directory(executor, source, deadline)
        if selection_command is None:
            _container_ordinary(executor, source, deadline)
        else:
            ordinary = executor.run([*selection_command, "inspect", str(source)], cwd="/",
                input=json.dumps(inputs.to_dict()), timeout=_timeout(deadline, "input preflight in the container", 120))
            if ordinary.returncode != 0 or ordinary.stdout:
                raise OwnershipRefused(f"container source contains links/special files or could not be read: {source}")
        _timeout(deadline, "prepared copy allocation in the container")
        owned = OwnedDirectory.create(parent=execution.work_dir,
                                      prefix="langadapter_project_",
                                      receipt_parent=receipt_parent)
        try:
            _timeout(deadline, "baseline allocation in the container")
            baseline = OwnedDirectory.create(prefix="langadapter_inputs_",
                                             receipt_parent=receipt_parent)
        except BaseException as exc:
            exc._langadapter_owned_directories = (
                owned, *getattr(exc, "_langadapter_owned_directories", ()))
            raise
        prepared_copy = cls(owned, baseline, context, executor, inputs, **mirror)
        try:
            options = {} if deadline is None else {"deadline": deadline}
            prepared_copy._verify_container(**options)
            prepared_copy.root.parent.mkdir(parents=True, exist_ok=True)
            if selection_command is None:
                _container_copy(executor, source, prepared_copy.root, deadline)
            else:
                copied = executor.run([*selection_command, "copy", str(source), str(prepared_copy.root)],
                    cwd="/", input=json.dumps(inputs.to_dict()),
                    timeout=_timeout(deadline, "project copying in the container", 600))
                if copied.returncode != 0:
                    raise OSError(f"container project copy failed: {copied.stdout}{copied.stderr}")
            _copy_tree(prepared_copy.root, prepared_copy.inputs, **options)
            prepared_copy._copy_companions(**options)
        except BaseException as exc:
            exc._langadapter_prepared_copy = prepared_copy
            if isinstance(exc, (KeyboardInterrupt, SystemExit, subprocess.TimeoutExpired)):
                raise
            failure = WorkspacePreparationFailed(
                f"container project copy failed. Retained roots: {owned.root}, {baseline.root}. "
                f"Receipts: {owned.receipt}, {baseline.receipt}. "
                f"{type(exc).__name__}: {exc}")
            failure._langadapter_prepared_copy = prepared_copy
            raise failure from exc
        return prepared_copy

    def _copy_companions(self, **options) -> None:
        """Snapshot each companion directory and place its copy."""
        for index, (companion_dir, _) in enumerate(self.companions):
            snapshot = self._companion_snapshot(index)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            _copy_tree(Path(companion_dir), snapshot, **options)
            self._restore_companion(index, **options)

    def _restore_companion(self, index: int, **options) -> None:
        target = self.companion_root(index)
        if os.path.lexists(target):
            if not stat.S_ISDIR(_ordinary(target).st_mode):
                raise OwnershipRefused(f"workspace suite copy is not a directory: {target}")
            _remove_tree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(self._companion_snapshot(index), target, **options)

    @property
    def executing_root(self) -> str:
        if self.container_context is not None:
            return container_path(self.container_context.execution, str(self.root))
        return str(self.root)

    def _verify_container(self, *, deadline=None) -> None:
        if self.container_context is None:
            return
        if self.container_executor is None:
            raise OwnershipRefused("the prepared copy in the container has no selected executor")
        observed = self.container_executor.run(["cat", str(self.owned.marker)],
                                          timeout=_timeout(deadline, "ownership verification in the container", 120))
        _timeout(deadline, "ownership verification in the container")
        if observed.returncode != 0 or observed.stdout.strip() != self.owned.token:
            raise OwnershipRefused("in the container, the prepared copy's mount does not match its ownership receipt")

    def verify_ownership(self) -> None:
        self.owned.verify()
        self.baseline.verify()
        self._verify_container()

    def verify(self) -> None:
        self.verify_ownership()
        if not stat.S_ISDIR(_ordinary(self.inputs).st_mode):
            raise OwnershipRefused(f"workspace input snapshot is not a directory: {self.inputs}")
        if os.path.lexists(self.root) and not stat.S_ISDIR(_ordinary(self.root).st_mode):
            raise OwnershipRefused(f"workspace project is not a directory: {self.root}")

    @contextlib.contextmanager
    def _exclusive(self, *, ownership_only=False) -> Iterator[dict]:
        self.verify_ownership() if ownership_only else self.verify()
        record = {"pid": os.getpid(), "token": uuid.uuid4().hex}
        try:
            descriptor = os.open(self.lease, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise WorkspaceBusy(
                f"workspace has an active or uncertain lease: {self.lease}. "
                "Do not reclaim it automatically") from exc
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            json.dump(record, stream, sort_keys=True)
        # An exception keeps the lease: a PID's absence does not show descendants ended.
        yield record

    def _release(self, record: dict) -> None:
        if json.loads(self.lease.read_text(encoding="ascii")) != record:
            raise OwnershipRefused(f"workspace lease changed: {self.lease}")
        self.lease.unlink()

    @contextlib.contextmanager
    def use(self, *, _scope: Optional[_Scope] = None) -> Iterator[Path]:
        """Restore declared inputs, then hold the lease through the operation.
        A successful exit requires the caller's processes to be quiescent. An
        exception, replacement or failed restoration keeps the lease and roots."""
        if _CURRENT.get() is not None and (_scope is None or _scope._parent is not _CURRENT.get()):
            raise WorkspaceBusy("Another execution scope is already active: workspace uses "
                                "cannot be nested")
        with (_scope if _scope is not None else _Scope()) as scope:
            with self._exclusive() as record:
                try:
                    self.verify()
                    if os.path.lexists(self.root):
                        if not stat.S_ISDIR(_ordinary(self.root).st_mode):
                            raise OwnershipRefused(f"workspace project is not a directory: {self.root}")
                        _remove_tree(self.root)
                    self.root.parent.mkdir(parents=True, exist_ok=True)
                    _copy_tree(self.inputs, self.root)
                    for index in range(len(self.companions)):
                        self._restore_companion(index)
                    yield self.root
                    scope.prepare_cleanup()
                    with scope.operation(recovery=True), _process_tree.cleanup():
                        self.verify()
                    scope.finish_use()
                except BaseException as error:
                    error._langadapter_prepared_copy = self
                    error._langadapter_prepared_copy_lease = record
                    raise
                self._release(record)

def require_cleanup_prepared_copy(context: Optional[ProjectContext], project_root: str):
    """Require the current copy and completed commands before touching outputs."""
    from langadapter.preparation import _Binding

    binding = context._prepared_copy_binding if context is not None else None
    if not isinstance(binding, _Binding):
        raise OwnershipRefused("Created-file cleanup requires the context yielded by prepare_project().use()")
    if isinstance(context.execution, ContainerExecution):
        same = PurePosixPath(project_root) == PurePosixPath(context.root)
    else:
        same = Path(project_root).absolute() == Path(context.root).absolute()
    if not same:
        raise OwnershipRefused("Created-file cleanup must name the current prepared project root")
    context._require_execution()
    binding.scope.require_quiescent(current_operation=True)
    return binding.prepared_copy


def _inventory(root: Path) -> FrozenSet[str]:
    """Record ordinary relative files/directories without following any links."""
    return frozenset(path.relative_to(root).as_posix() for event, path in
                     _walk(root, recheck="Output inventory requires an ordinary project directory")
                     if event != "leave")


class CreatedFiles:
    """Outputs observed between completed runs inside one prepared context.
    Existing inputs are never selected, files added after ``end()`` stay, and
    directories are removed only when empty. ``PreparedProject.use()`` also
    restores changed or deleted inputs from the preparation's snapshot."""

    def __init__(self, context: Optional[ProjectContext] = None) -> None:
        self.context = context
        self._before: Dict[str, FrozenSet[str]] = {}
        self._created: Dict[str, Set[str]] = {}

    def begin(self, project_root: str) -> List[str]:
        """Remove previously observed disposable outputs. Failures remain errors."""
        prepared_copy = require_cleanup_prepared_copy(self.context, project_root)
        root = prepared_copy.root
        current = _inventory(root)
        created = self._created.get(project_root, set()) & current
        removed = []
        for relative in sorted(created, key=lambda name: (-len(PurePosixPath(name).parts), name)):
            path = root / relative
            for parent in path.parents:
                _ordinary(parent)
                if parent == root:
                    break
            try:
                identity = _ordinary(path)
                if stat.S_ISDIR(identity.st_mode):
                    # A new file after end() can keep this directory nonempty.
                    if any(path.iterdir()):
                        continue
                    path.rmdir()
                else:
                    path.unlink()
            except FileNotFoundError:
                continue
            removed.append(relative)
            self._created[project_root] = self._created.get(project_root, set()) - {relative}
        current = _inventory(root)
        self._created[project_root] = self._created.get(project_root, set()) & current
        self._before[project_root] = current
        return sorted(removed)

    def end(self, project_root: str) -> None:
        """Record new disposable paths only after owned commands are quiescent."""
        before = self._before.get(project_root)
        if before is None:
            return
        prepared_copy = require_cleanup_prepared_copy(self.context, project_root)
        current = _inventory(prepared_copy.root)
        self._created.setdefault(project_root, set()).update(current - before)
        del self._before[project_root]
