"""Prepared copies of a project, and execution contexts bound to one use of
a copy.

A copy belongs to its owner but is no sandbox: its commands can still reach
files outside it. Keep results outside the copy, build requests against the
root ``use()`` yields, and close sessions before ``use()`` ends. A use that
was interrupted, or whose commands may still run, keeps its copy and receipt.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import posixpath
import re
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional, Sequence

from langadapter import _execution_scope, _process_tree, _project_environments
from langadapter._owned_directory import OwnershipRefused
from langadapter._prepared_copy import PreparedCopy, WorkspaceBusy, _ordinary, _timeout
from langadapter.context import ContainerExecution, ProjectContext, TestFrameworkConfig
from langadapter.errors import InvalidRequest
from langadapter.context import ProjectInputSelection
from langadapter.suites import Suite


_MARKER = ".langadapter-project-context"


class _ChildDependency:
    """The prepared project's place in its parent scope's children."""


def _overlap(first, second):
    return first == second or first in second.parents or second in first.parents


def _prepared_ancestors(scope):
    while scope is not None:
        if scope._prepared_owner is not None:
            prepared_copy = scope._prepared_owner._prepared_copy
            prepared_copy.owned.verify()
            prepared_copy.baseline.verify()
            yield scope._prepared_owner
        scope = scope._parent


def _independent_parent(context, *, parent=None, receipt_parent=None):
    current = _execution_scope._CURRENT.get()
    if current is None:
        return None
    containing = current._prepared_owner
    if containing is None:
        raise OwnershipRefused("Only an explicit prepared project may contain independent copies")
    current.require_active()
    current._refuse_failed_session()
    current.require_idle(ignore_children=True)
    ancestors = tuple(_prepared_ancestors(current))
    areas = tuple(path.resolve() for ancestor in ancestors
                  for directory in (ancestor._prepared_copy.owned, ancestor._prepared_copy.baseline)
                  for path in (directory.root, directory.receipt))
    if isinstance(context.execution, ContainerExecution):
        selected = context.execution
        outer = containing._context.execution
        if not isinstance(outer, ContainerExecution) or (
                selected.container, selected.runner, Path(selected.work_dir).resolve(),
                selected.container_work_dir, selected.helper_python) != (
                outer.container, outer.runner, Path(outer.work_dir).resolve(),
                outer.container_work_dir, outer.helper_python):
            raise OwnershipRefused("Independent copies in the container require the same explicit container mapping")
        source = PurePosixPath(context.root)
        for owned in (PurePosixPath(ancestor.root) for ancestor in ancestors):
            if _overlap(source, owned):
                raise OwnershipRefused("Child inputs overlap the active mutable project")
        from langadapter.execution import _mounted_work_dir
        mapped = PurePosixPath(_mounted_work_dir(selected))
        try:
            source_host = Path(selected.work_dir) / source.relative_to(mapped)
        except ValueError:
            source_host = None
    else:
        source_host = Path(context.root)
    if source_host is not None and any(_overlap(source_host.resolve(), area) for area in areas):
        raise OwnershipRefused("Child inputs overlap containing ownership areas")
    for location in (parent, receipt_parent, context.execution.work_dir):
        if location is not None:
            destination = Path(location).resolve()
            if any(destination == area or area in destination.parents for area in areas):
                raise OwnershipRefused("Child ownership and receipts must be outside containing roots")
    return current


def _verify_independent(prepared_copy, parent_scope):
    if parent_scope is None:
        return
    first = (prepared_copy.owned, prepared_copy.baseline)
    for child in first:
        child.verify()
    for ancestor in _prepared_ancestors(parent_scope):
        containing = ancestor._prepared_copy
        for child in first:
            for outer in (containing.owned, containing.baseline):
                if _overlap(child.root.resolve(), outer.root.resolve()):
                    raise OwnershipRefused("Prepared execution ownership areas overlap")
                if _overlap(child.root.resolve(), outer.receipt.resolve()):
                    raise OwnershipRefused("Prepared execution root overlaps an ancestor receipt")
                if (outer.root.resolve() in child.receipt.resolve().parents
                        or child.root.resolve() in outer.receipt.resolve().parents):
                    raise OwnershipRefused("Prepared durable receipts overlap execution ownership areas")
        if prepared_copy.container_context is not None and containing.container_context is not None:
            child = prepared_copy.container_context.execution
            outer = containing.container_context.execution
            if (child.container, child.runner, Path(child.work_dir).resolve(), child.container_work_dir,
                    child.helper_python) != (outer.container, outer.runner, Path(outer.work_dir).resolve(),
                    outer.container_work_dir, outer.helper_python):
                raise OwnershipRefused("Prepared children have different container mappings")
            if _overlap(PurePosixPath(prepared_copy.executing_root), PurePosixPath(containing.executing_root)):
                raise OwnershipRefused("Prepared execution roots in the container overlap")


class _PartialPreparation:
    """Recovery of acquired factory resources before a complete owner exists."""

    def __init__(self, scope, directories, dependency, parent):
        self._scope = scope
        self._remaining = list(directories)
        self._dependency = dependency
        self._parent = parent
        self._closed = False
        self._operation = threading.Lock()

    def close(self):
        if not self._operation.acquire(blocking=False):
            raise WorkspaceBusy("Another operation owns preparation recovery")
        try:
            if self._closed:
                return
            self._scope.require_quiescent()
            current = _execution_scope._CURRENT.get()
            parent = current if current is self._parent else None
            if current is not None and parent is None:
                raise OwnershipRefused("Preparation recovery belongs to another scope")
            with _execution_scope._Scope(parent=parent, cleanup=True):
                while self._remaining:
                    self._remaining[0].remove()
                    self._remaining.pop(0)
            self._closed = True
            if self._dependency is not None:
                _execution_scope.completed(self._dependency)
        except BaseException as error:
            error._langadapter_prepared_project = self
            raise
        finally:
            self._operation.release()


class _Binding:
    def __init__(self, prepared_copy, scope, context):
        self.prepared_copy = prepared_copy
        self.scope = scope
        self.context = context
        self.environment = context.environment
        self.record = json.loads(prepared_copy.lease.read_text(encoding="ascii"))
        self.active = True
        self.issued_context = None
        #: Whether the prepared copy seeds and harvests compiled modules
        #: through a bytecode store, whose entries are content-checked.
        self.seeds_bytecode = False

    def require_identity(self, context):
        if not self.active or context.root != self.context.root:
            raise OwnershipRefused("Prepared context is obsolete or names a different project root")
        # A context derived from the use's (ProjectContext.derived) has the
        # fields its derivation gives the use's context, and no others.
        derivation = context._derivation
        expected = (self.context if derivation is None
                    else replace(self.context, **derivation.fields(self.context)))
        for field in ("interpreter", "execution", "interpreter_flags", "test_framework"):
            if getattr(context, field) != getattr(expected, field):
                raise OwnershipRefused(f"Prepared context changed its selected {field}")
        environment = self.environment if derivation is None else expected.environment
        if context.environment != environment or (derivation is not None and any(
                name in self.environment for name, _ in derivation.added)):
            raise OwnershipRefused("Prepared context changed its selected environment")
        self.prepared_copy.verify()
        if json.loads(self.prepared_copy.lease.read_text(encoding="ascii")) != self.record:
            raise OwnershipRefused("Prepared context no longer owns the current workspace lease")

    def require(self, context):
        self.scope.require_active()
        self.require_identity(context)


def _require_context(context):
    binding = context._prepared_copy_binding
    if binding is not None:
        if not isinstance(binding, _Binding):
            raise OwnershipRefused("Prepared context has an invalid execution binding")
        binding.require(context)
        return
    paths = [Path(context.root)]
    execution = context.execution
    if isinstance(execution, ContainerExecution):
        from langadapter.execution import _mounted_work_dir

        container_root = PurePosixPath(context.root)
        mapped = PurePosixPath(_mounted_work_dir(execution))
        try:
            paths.append(Path(execution.work_dir) / container_root.relative_to(mapped))
        except ValueError:
            pass
    for path in paths:
        for candidate in (path.absolute(), path.resolve()):
            for parent in (candidate, *candidate.parents):
                if os.path.lexists(parent / _MARKER):
                    raise OwnershipRefused("Prepared project execution requires the context yielded by use()")


class PreparedProject:
    """A prepared copy of a project, made by ``prepare_project``, that one
    use at a time may run commands in. Each use starts from the copy's
    original state. A context or adapter kept from a use cannot run commands
    after that use ends, nor in a later use. A successful ``close()``
    removes the copy and its snapshot, so results that must last do not
    belong under ``root``."""

    def __init__(self, context: ProjectContext, prepared_copy: PreparedCopy, *, incomplete=False):
        self._context = context
        self._prepared_copy = prepared_copy
        self._closed = False
        self._binding = None
        self._use_scope = None
        self._use_record = None
        self._closing_scope = _execution_scope._Scope()
        self._closing_record = None
        self._remaining = [prepared_copy.owned, prepared_copy.baseline]
        self._operation = threading.Lock()
        self._incomplete = incomplete
        self._factory_scope = None
        self._parent_scope = None
        self._dependency = None
        self._bytecode = None

    @property
    def root(self) -> str:
        """The copy's root as its commands see it: a path of this machine,
        or its path in the container when the project runs in one."""
        return self._prepared_copy.executing_root

    @property
    def inputs(self) -> Optional[ProjectInputSelection]:
        """The effective immutable input selection, or None for a whole-tree copy."""
        return self._prepared_copy.selection

    def companion_path(self, path: str) -> Optional[str]:
        """Where a path in a companion directory lies in the copy, as a host
        path, or None for a path in no companion."""
        selected = Path(path).absolute().resolve()
        for index, (companion_dir, _) in enumerate(self._prepared_copy.companions):
            try:
                relative = selected.relative_to(companion_dir)
            except ValueError:
                continue
            return str(self._prepared_copy.companion_root(index).joinpath(*relative.parts))
        return None

    @contextlib.contextmanager
    def use(self) -> Iterator[ProjectContext]:
        """Begin a use of the copy and yield its ``ProjectContext``, rooted
        at the copy, for the adapters of this use. The copy starts from its
        original state. A use of a closed prepared project, a use while
        another is active and a nested use of the same prepared project are
        refused."""
        with self._exclusive_call():
            if self._incomplete:
                raise OwnershipRefused("Incomplete preparation can only be closed")
            self._adopt_parent()
            with self._use() as context:
                yield context

    def _adopt_parent(self):
        current = _execution_scope._CURRENT.get()
        if current is None:
            return
        if current._prepared_owner is self:
            raise OwnershipRefused("Do not nest another use of the same prepared project")
        if self._parent_scope is not None and current is not self._parent_scope:
            raise OwnershipRefused("Prepared child belongs to another containing use")
        current.require_active()
        current._refuse_failed_session()
        current.require_idle(ignore_children=True)
        if current._prepared_owner is None:
            raise OwnershipRefused("Prepared child copies can be made only inside a "
                                   "prepared project's use()")
        _verify_independent(self._prepared_copy, current)
        if self._dependency is None:
            self._dependency = _ChildDependency()
            current.track_child(self._dependency)
            self._parent_scope = current

    @contextlib.contextmanager
    def worker(self, execution_context: ProjectContext) -> Iterator[ProjectContext]:
        """Lend the current use to one worker thread, without a new lease or
        restoring the snapshot. Pass the context ``use()`` yielded. A second
        worker on the same use is refused, not queued. Close the worker's
        sessions before the use ends, and keep an owner whose cleanup failed
        until its cleanup succeeds."""
        binding = self._binding
        if (binding is None or execution_context is not binding.issued_context
                or execution_context._prepared_copy_binding is not binding):
            raise OwnershipRefused("Prepared worker requires the exact issued execution context")
        caller: list = []
        try:
            with binding.scope.borrow(lambda: binding.require_identity(execution_context)):
                try:
                    yield execution_context
                except BaseException as error:
                    caller.append(error)
                    raise
        except BaseException as error:
            # An error of the caller's own work that left the borrow settled
            # (a refused measurement, a rejected request) does not name this
            # owner: callers treat an error naming it as pending cleanup and
            # refuse later borrows. A failed borrow names the owner for
            # recovery. Native work left running carries its own owner, and a
            # failed session's refusal names that session.
            if (not caller or error is not caller[0]) and getattr(error, "cleanup_handle", None) is None:
                error._langadapter_prepared_project = self
            raise

    def stage_suite(self, execution_context: ProjectContext, suite: Suite, *,
                    protected_paths=(), absent_files=()) -> Suite:
        """Copy the current files of ``suite`` into the copy without
        restarting this use, and return the staged suite. Pass the exact
        issued context and the original host-side suite. Only files in the
        original project or a companion directory are copied, each into the copy
        of its directory. Other external suites stay external.
        ``Suite.setup_files`` names the setup files the runner loads by itself
        (such as conftest.py), ``absent_files`` setup files to remove from
        the copy, and ``protected_paths`` project inputs in the copy the
        commands run in. Call only while native sessions are idle."""
        binding = self._binding
        if (binding is None or execution_context is not binding.issued_context
                or execution_context._prepared_copy_binding is not binding):
            raise OwnershipRefused("Suite staging requires the exact issued execution context")
        try:
            with binding.scope.operation(idle=True):
                binding.require(execution_context)
                return self._stage_suite(suite, protected_paths, absent_files)
        except BaseException as error:
            # A failed session's refusal leaves the use admitted: the
            # session's close() lifts it.
            if getattr(error, "cleanup_handle", None) is not None:
                raise
            with binding.scope._lock:
                binding.scope._revoked = True
            error._langadapter_prepared_project = self
            raise

    def _stage_suite(self, suite, protected_paths, absent_files):
        from langadapter.execution import _mounted_work_dir
        execution = self._context.execution
        if isinstance(execution, ContainerExecution):
            mapped = PurePosixPath(_mounted_work_dir(execution))
            try:
                original = Path(execution.work_dir).resolve() / PurePosixPath(
                    self._context.root).relative_to(mapped)
            except ValueError:
                original = None
            protected = {Path(*PurePosixPath(path).relative_to(PurePosixPath(self.root)).parts)
                         for path in protected_paths}
        else:
            original = Path(self._context.root).resolve()
            protected = {Path(path).resolve().relative_to(self._prepared_copy.root.resolve())
                         for path in protected_paths}
        suite.validate()
        root = Path(suite.root).absolute()
        # No link inside the suite's directory, the directory itself included. The
        # directories above it are the caller's (macOS's temporary directory lies
        # below the link /var).
        for selected in (root, *suite.files()):
            for component in (selected.absolute(), *selected.absolute().parents):
                _ordinary(component)
                if component == root:
                    break
        root = root.resolve()
        files = tuple(path.resolve() for path in suite.files())
        for index, (companion_dir, _) in enumerate(self._prepared_copy.companions):
            if root.is_relative_to(companion_dir):
                # A suite in a companion directory is staged into its copy.
                original, destination = Path(companion_dir), self._prepared_copy.companion_root(index)
                break
        else:
            destination = self._prepared_copy.root
        if original is None or not root.is_relative_to(original):
            if original is not None and any(path.is_relative_to(original) for path in files):
                raise InvalidRequest("suite.root", "mixed external/project suites cannot be staged")
            return Suite(str(root), tuple(str(path) for path in files[:len(suite.test_files)]),
                         tuple(str(path) for path in files[len(suite.test_files):]))
        copies = []
        removals = []
        for path in files:
            relative = path.relative_to(original)
            if relative in protected or any(name in relative.parts for name in (
                    _MARKER, ".langadapter-owner")):
                raise InvalidRequest("suite", "selected suite input is a protected project input")
            copies.append((destination / relative, path.read_bytes()))
        for selected in absent_files:
            path = (root / selected).absolute()
            for component in path.parents:
                if os.path.lexists(component):
                    _ordinary(component)
            if not path.resolve().is_relative_to(root) or os.path.lexists(path):
                raise InvalidRequest("absent_files", "must name absent inputs within the original suite")
            relative = path.resolve().relative_to(original)
            if relative in protected or any(name in relative.parts for name in (
                    _MARKER, ".langadapter-owner")):
                raise InvalidRequest("absent_files", "cannot remove a protected project input")
            removals.append(destination / relative)
        # Validate every existing destination component before the first write.
        for path in [item[0] for item in copies] + removals:
            for component in (path, *path.parents):
                if os.path.lexists(component):
                    observed = _ordinary(component)
                    if stat.S_ISREG(observed.st_mode) and observed.st_nlink != 1:
                        raise OwnershipRefused(f"suite staging refuses writable file aliases: {component}")
                if component == destination:
                    break
        for path, data in copies:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for path in removals:
            if os.path.lexists(path):
                path.unlink()
        staged = tuple(str(destination / path.relative_to(original)) for path in files)
        return Suite(str(destination / root.relative_to(original)),
                     staged[:len(suite.test_files)], staged[len(suite.test_files):])

    @contextlib.contextmanager
    def _exclusive_call(self):
        if not self._operation.acquire(blocking=False):
            raise WorkspaceBusy("Another operation owns this prepared project")
        try:
            yield
        finally:
            self._operation.release()

    @contextlib.contextmanager
    def _use(self):
        if self._closed:
            raise OwnershipRefused("Prepared project is closed")
        if self._use_scope is not None or self._closing_record is not None:
            raise WorkspaceBusy("Prepared project has an active or retained use. Close its owned resources first")
        parent = _execution_scope._CURRENT.get()
        scope = _execution_scope._Scope(parent=parent)
        scope._prepared_owner = self
        self._use_scope = scope
        try:
            with self._prepared_copy.use(_scope=scope):
                selected = replace(self._context, root=self.root)
                binding = _Binding(self._prepared_copy, scope, selected)
                binding.seeds_bytecode = self._bytecode is not None
                self._binding = binding
                try:
                    context = replace(selected, _prepared_copy_binding=binding)
                    binding.issued_context = context
                    context.validate()
                    if self._bytecode is not None:
                        self._bytecode.seed(context, self._bytecode_parts())
                    yield context
                    if self._bytecode is not None:
                        self._bytecode.harvest(context, self._bytecode_parts())
                finally:
                    binding.active = False
        except BaseException as error:
            if getattr(error, "_langadapter_prepared_copy", None) is self._prepared_copy:
                self._use_record = getattr(error, "_langadapter_prepared_copy_lease", None)
            error._langadapter_prepared_project = self
            raise
        else:
            self._binding = None
            self._use_scope = None
            self._use_record = None

    def _bytecode_parts(self):
        """The parts of the copy the bytecode store keeps apart, each with its
        source directory as the context's commands see it: the project's copy,
        and each companion's under a name of its original directory, so a copy
        with other companions, or none, finds the same entries."""
        parts = [("project", str(self._prepared_copy.root))]
        for index, (companion_dir, _) in enumerate(self._prepared_copy.companions):
            name = hashlib.sha256(str(companion_dir).encode("utf-8")).hexdigest()[:16]
            parts.append((f"companion-{name}", str(self._prepared_copy.companion_root(index))))
        execution = self._context.execution
        if isinstance(execution, ContainerExecution):
            from langadapter.execution import container_path
            return [(name, container_path(execution, root)) for name, root in parts]
        return parts

    def close(self) -> None:
        """Remove the copy and its snapshot, only once verified idle. A failed
        disposal can be retried, and a cancel of the caller's operation does
        not stop it."""
        with self._exclusive_call():
            try:
                with _process_tree.cleanup():
                    self._close()
            except BaseException as error:
                error._langadapter_prepared_project = self
                raise

    def _close(self):
        if self._closed:
            return
        current = _execution_scope._CURRENT.get()
        if current is not None and current is not self._parent_scope:
            self._adopt_parent()
        if self._factory_scope is not None:
            self._factory_scope.require_quiescent()
        self._closing_scope._parent = current
        self._closing_scope._cleanup = current is not None
        with self._closing_scope:
            if self._use_scope is not None:
                self._use_scope.require_quiescent()
            binding = self._binding
            if binding is not None:
                if binding.active:
                    raise WorkspaceBusy("Prepared project is in use")
                binding.scope.require_quiescent()
                self._prepared_copy.verify()
                self._prepared_copy._release(binding.record)
                self._binding = None
            elif self._use_record is not None:
                self._prepared_copy.verify()
                self._prepared_copy._release(self._use_record)
            self._use_record = None
            self._use_scope = None
            if self._closing_record is None:
                with self._prepared_copy._exclusive(ownership_only=self._incomplete) as record:
                    self._closing_record = record
            if json.loads(self._prepared_copy.lease.read_text(encoding="ascii")) != self._closing_record:
                raise OwnershipRefused("Prepared project disposal lease changed")
            while self._remaining:
                if (self._remaining[0] is self._prepared_copy.owned
                        and self._prepared_copy.container_context is not None):
                    self._prepared_copy.verify_ownership() if self._incomplete else self._prepared_copy.verify()
                    removed = self._prepared_copy.container_executor.run(
                        ["rm", "-rf", "--", str(self._prepared_copy.root)], timeout=120)
                    if removed.returncode != 0:
                        raise OSError("removing the prepared project in the container failed")
                self._remaining[0].remove()
                self._remaining.pop(0)
            self._prepared_copy._release(self._closing_record)
            self._closed = True
        if self._dependency is not None:
            _execution_scope.completed(self._dependency)


def original_text(context: ProjectContext, text: str) -> str:
    """``text`` from a run in a prepared copy, with the copy's paths written
    as the original project's and its companions' (in either slash style, as
    created and as resolved, and in a container as the container sees them).
    Unchanged for a context that is no prepared copy."""
    binding = getattr(context, "_prepared_copy_binding", None)
    if binding is None or not text:
        return text
    return binding.prepared_copy.original_text(text)


def prepare_project(context: ProjectContext, *, parent: Optional[str] = None,
                    receipt_parent: Optional[str] = None,
                    inputs: Optional[ProjectInputSelection] = None,
                    timeout_s: Optional[float] = None,
                    companions: Sequence[str] = (),
                    bytecode=None) -> PreparedProject:
    """Copy a project that has no links and does not change while it is
    copied. The tools the context selects stay where they are, and a
    borrowed container is never removed.

    ``companions`` names directories outside the project that its runs use, a
    suite beside the project above all. Each is copied too, and all keep
    their places below the directory they share, so the paths a run prints
    relative to its working directory are those an in-place run prints. Files a
    companion's tests write beside themselves land in the copy.
    ``original_text`` writes a run's text with the original paths. In a
    container, companions are host directories in the work directory.

    ``bytecode`` is a ``langadapter.python.project.BytecodeStore``: each use
    writes its stored compiled modules into the copy before its runs and,
    after a use that ended normally, stores what they compiled, so a later
    copy's runs reuse them as a run in place reuses its own.

    Framework paths in the project are mapped to the copy, while external
    configuration and tools stay as selected. An environment value naming
    the original root is refused, never rewritten: the caller adjusts it. A
    container needs an explicit control helper and a verified mount. An
    ``inputs`` plan excludes only its named entries and protects the
    declared required inputs and the in-project framework file. The caller
    or adapter declares other known requirements. Without a plan the whole
    tree is copied except Python bytecode caches. ``timeout_s`` bounds
    preparation, not use or cleanup, through cooperative checks of local
    file system calls and the remaining bound of container command timeouts. A
    partial copy stays attached to the failure.
    """
    seconds = None
    if timeout_s is not None:
        if type(timeout_s) not in (int, float):
            raise InvalidRequest("timeout_s", "must be a finite positive number or None")
        try:
            seconds = float(timeout_s)
        except OverflowError:
            raise InvalidRequest("timeout_s", "must be a finite positive number or None") from None
        if not math.isfinite(seconds) or seconds <= 0:
            raise InvalidRequest("timeout_s", "must be a finite positive number or None")
    deadline = None if seconds is None else time.monotonic() + seconds
    _timeout(deadline, "preparation admission")
    if not isinstance(context, ProjectContext):
        raise InvalidRequest("context", "must be a ProjectContext")
    if inputs is not None and not isinstance(inputs, ProjectInputSelection):
        raise InvalidRequest("inputs", "must be a ProjectInputSelection or None")
    if isinstance(companions, (str, bytes)) or any(
            not isinstance(path, str) or not path for path in companions):
        raise InvalidRequest("companions", "must be a sequence of directory paths")
    if bytecode is not None and not all(
            callable(getattr(bytecode, name, None)) for name in ("seed", "harvest")):
        raise InvalidRequest("bytecode", "must be a BytecodeStore or None")
    context.validate()
    _timeout(deadline, "preparation admission")
    context._require_execution()
    if context._prepared_copy_binding is not None:
        raise OwnershipRefused("Do not prepare another copy from an active prepared project")
    containing = _independent_parent(context, parent=parent, receipt_parent=receipt_parent)
    dependency = _ChildDependency() if containing is not None else None
    if dependency is not None:
        containing.track_child(dependency)
    scope = _execution_scope._Scope(parent=containing)
    owner = None
    try:
        with scope:
            options = {} if inputs is None else {"inputs": inputs}
            if deadline is not None:
                options["deadline"] = deadline
            if companions:
                options["companions"] = tuple(companions)
            owner = _prepare_project(context, parent=parent, receipt_parent=receipt_parent, **options)
            owner._bytecode = bytecode
            owner._incomplete = True
            owner._factory_scope = scope
            owner._parent_scope = containing
            owner._dependency = dependency
            _verify_independent(owner._prepared_copy, containing)
            _timeout(deadline, "preparation completion")
        owner._incomplete = False
        return owner
    except BaseException as error:
        prepared_copy = getattr(error, "_langadapter_prepared_copy", None)
        directories = getattr(error, "_langadapter_owned_directories", ())
        if owner is None and prepared_copy is not None:
            owner = PreparedProject(context, prepared_copy, incomplete=True)
            owner._factory_scope = scope
            owner._parent_scope = containing
            owner._dependency = dependency
        if owner is None:
            try:
                scope.require_quiescent()
            except OwnershipRefused:
                owner = _PartialPreparation(scope, directories, dependency, containing)
            else:
                if directories:
                    owner = _PartialPreparation(scope, directories, dependency, containing)
        if owner is not None:
            error._langadapter_prepared_project = owner
        elif dependency is not None:
            _execution_scope.completed(dependency)
        raise


def _framework_required(context, source):
    framework = context.test_framework
    if framework.path is None:
        return ()
    if isinstance(context.execution, ContainerExecution):
        from langadapter.execution import executor_for

        path = PurePosixPath(executor_for(context)._to_container(framework.path))
        selected = PurePosixPath(posixpath.normpath(str(path if path.is_absolute() else source / path)))
    else:
        path = Path(framework.path)
        # Protect the lexical input, not the destination of an excluded link.
        selected = Path(os.path.abspath(path if path.is_absolute() else source / path))
    try:
        return (selected.relative_to(source).as_posix(),)
    except ValueError:
        return ()


def _prepare_project(context, *, parent, receipt_parent, inputs=None, deadline=None,
                     companions=()):
    _timeout(deadline, "preparation input admission")
    container = isinstance(context.execution, ContainerExecution)
    if container:
        if context.execution.helper_python is None:
            raise InvalidRequest("execution.helper_python",
                                 "preparation in the container requires the helper Python")
        source = PurePosixPath(context.root)
        if ".." in source.parts or "\\" in context.root:
            raise InvalidRequest("root", "preparation in the container requires a normalized absolute POSIX path")
        if (context.execution.repository_dir is not None
                and PurePosixPath(context.execution.repository_dir) != source):
            raise InvalidRequest("execution.repository_dir", "must name the source project root for preparation")
        if parent is not None and Path(parent).resolve() != Path(context.execution.work_dir).resolve():
            raise InvalidRequest("parent", "preparation in the container must use the shared work directory (execution.work_dir)")
        normalized = (str(source),)
    else:
        selected_source = Path(context.root).absolute()
        _ordinary(selected_source)
        source = selected_source.resolve()
        normalized = tuple(str(path).replace("\\", "/") for path in (selected_source, source))
    # The root's text names the project unless a character that continues a
    # directory name touches it: a sibling such as ROOT-jdk, ROOTs or ROOT.old and
    # a longer path such as /opt/ROOT are other directories, the root alone, in a
    # list or in an option is not. After a root that ends in a slash (a drive
    # root P:/, or /) every path below it is inside.
    roots = [root.casefold() if os.name == "nt" else root for root in normalized]
    patterns = [re.compile(r"(?<![\w.\-])" + re.escape(root)
                           + ("" if root.endswith("/") else r"(?![\w.\-])")) for root in roots]
    # A value naming the project is refused, unless it names an environment
    # directory inside the project that leads nowhere into its sources.
    environments = _project_environments.admitted(context.environment, patterns, normalized, container)
    if not container and context.execution.work_dir is not None:
        try:
            Path(context.execution.work_dir).resolve().relative_to(source)
        except ValueError:
            pass
        else:
            raise InvalidRequest("execution.work_dir", "must be outside the copied source project")
    if environments:
        inputs = _project_environments.left_out(inputs, environments, source)
    if inputs is not None:
        inputs = inputs.effective(_framework_required(context, source), case_sensitive=container or os.name != "nt")
    options = {} if inputs is None else {"inputs": inputs}
    if deadline is not None:
        options["deadline"] = deadline
    if companions:
        options["companions"] = companions
    if container:
        from langadapter.execution import executor_for

        prepared_copy = PreparedCopy.create_in_container(context, executor_for(context),
                                                   receipt_parent=receipt_parent, **options)
    else:
        prepared_copy = PreparedCopy.create(str(source), parent=parent or context.execution.work_dir,
                                            receipt_parent=receipt_parent, **options)
    try:
        framework = context.test_framework
        if framework.path is not None:
            if container:
                path = PurePosixPath(prepared_copy.container_executor._to_container(framework.path))
                path = PurePosixPath(posixpath.normpath(str(path if path.is_absolute() else source / path)))
            else:
                path = Path(framework.path)
                path = (path if path.is_absolute() else source / path).resolve()
            try:
                path = ((PurePosixPath(prepared_copy.executing_root) if container else prepared_copy.root)
                        / path.relative_to(source))
            except ValueError:
                pass
            framework = TestFrameworkConfig(framework.source, str(path))
        execution = (replace(context.execution, repository_dir=prepared_copy.executing_root)
                     if container else context.execution)
        selected = replace(context, execution=execution, test_framework=framework)
        # A context built on the copy or on its input snapshot is refused.
        for owned in (prepared_copy.owned, prepared_copy.baseline):
            with (owned.root / _MARKER).open("x", encoding="ascii") as stream:
                json.dump({"receipt": str(owned.receipt), "token": owned.token}, stream)
        return PreparedProject(selected, prepared_copy)
    except BaseException as error:
        error._langadapter_prepared_copy = prepared_copy
        raise
