"""Private operation-local accounting for acquired commands and sessions:
adapter-owned handles, not arbitrary processes or file authorship. Only the
actual lifecycle owner may confirm disposal. No PID is probed or reclaimed,
and an interrupted launch without a handle remains uncertain."""
from __future__ import annotations

from contextvars import ContextVar
import contextlib
import threading

from langadapter._owned_directory import OwnershipRefused


_CURRENT = ContextVar("langadapter_execution_scope", default=None)


class _Ticket:
    pass


class _Scope:
    def __init__(self, *, parent=None, cleanup=False):
        self._resources = {}
        self._lock = threading.RLock()
        self._entry = threading.Lock()
        self._token = None
        self._thread = None
        self._borrow_thread = None
        self._operations = 0
        self._revoked = False
        self._persistent = {}
        self._recovery_thread = None
        self._parent = parent
        self._cleanup = cleanup
        self._prepared_owner = None
        self._children = {}
        # The worker threads ``lend`` admits as the use's actors, while it lasts.
        self._lending = False
        self._lent = set()

    def __enter__(self):
        current = _CURRENT.get()
        if current is not self._parent or not self._entry.acquire(blocking=False):
            raise OwnershipRefused("Another execution scope is already active: workspace uses "
                                   "cannot be nested")
        parent_entered = False
        try:
            if current is not None:
                current._enter_child(cleanup=self._cleanup)
                parent_entered = True
            self.require_quiescent()
            self._revoked = False
            self._token = _CURRENT.set(self)
            self._thread = threading.get_ident()
            return self
        except BaseException:
            self._entry.release()
            if parent_entered:
                current._exit_child(failed=True)
            raise

    def __exit__(self, kind, error, traceback):
        failed = error is not None
        try:
            try:
                self.finish_use()
            except OwnershipRefused as cleanup:
                failed = True
                if error is None:
                    raise
                error._langadapter_execution_scope = self
                if hasattr(error, "add_note"):
                    error.add_note(str(cleanup))
        finally:
            with self._lock:
                self._revoked = True
                _CURRENT.reset(self._token)
                self._token = None
                self._thread = None
                self._entry.release()
                if self._parent is not None:
                    self._parent._exit_child(failed=failed)

    def _enter_child(self, *, cleanup):
        with self._lock:
            actor = self._borrow_thread if self._borrow_thread is not None else self._thread
            if (self._token is None or _CURRENT.get() is not self
                    or actor != threading.get_ident() or self._operations):
                raise OwnershipRefused("Another operation owns the containing workspace")
            if not cleanup:
                self.require_active()
                self.require_idle(ignore_children=True)
            self._operations += 1

    def _exit_child(self, *, failed):
        with self._lock:
            self._operations -= 1
            if failed:
                self._revoked = True

    def track_child(self, dependency):
        with self._lock:
            self.require_active()
            self.track(dependency, "independent prepared child")
            self._children[id(dependency)] = dependency

    def finish_use(self):
        with self._lock:
            self._revoked = True
            self.require_quiescent()

    def prepare_cleanup(self):
        with self._lock:
            self.require_quiescent()
            self._revoked = True

    def require_active(self):
        with self._lock:
            actor = self._borrow_thread if self._borrow_thread is not None else self._thread
            if (self._recovery_thread == threading.get_ident()
                    and _CURRENT.get() is self):
                return
            if self._revoked and self._resources:
                # Revoked with resources unresolved (an interrupted launch): raise
                # them as a pending cleanup for the caller, not a plain refusal.
                labels = ", ".join(label for _, label in self._resources.values())
                error = OwnershipRefused(
                    "The workspace use was revoked while these resources are unresolved: "
                    + labels + ". Clean them up before using the workspace again")
                error._langadapter_execution_scope = self
                error._langadapter_resources = tuple(
                    resource for resource, _ in self._resources.values())
                raise error
            current = threading.get_ident()
            if (self._revoked or self._token is None or _CURRENT.get() is not self
                    or (actor != current and current not in self._lent)):
                raise OwnershipRefused("Execution requires the current workspace use on its owning thread")

    @contextlib.contextmanager
    def lend(self):
        """Admit worker threads (``admit``) as the use's actors for the block,
        which the use's actor opens and which ends only once they are done."""
        with self._lock:
            self.require_active()
            if self._lending:
                raise OwnershipRefused("The workspace use is already lent to worker threads")
            self._lending = True
        try:
            yield
        finally:
            with self._lock:
                self._lending = False
                self._lent.clear()

    def admit(self):
        """Run the current thread as one of the use's actors until ``dismiss``."""
        with self._lock:
            if not self._lending:
                raise OwnershipRefused("The workspace use lends no worker threads now")
            self._lent.add(threading.get_ident())

    def dismiss(self):
        with self._lock:
            self._lent.discard(threading.get_ident())

    @contextlib.contextmanager
    def operation(self, *, recovery=False, idle=False):
        token = None
        parent = None
        failed = False
        with self._lock:
            if (recovery and self._borrow_thread is None and self._recovery_thread is None
                    and (self._token is None or (self._revoked and _CURRENT.get() is self
                                                and self._thread == threading.get_ident()))):
                current = _CURRENT.get()
                if self._operations or current not in (None, self, self._parent):
                    raise OwnershipRefused("Another operation owns this cleanup scope")
                if current is not None and current is self._parent:
                    current._enter_child(cleanup=True)
                    parent = current
                self._recovery_thread = threading.get_ident()
                token = _CURRENT.set(self)
            else:
                self.require_active()
                if not recovery:
                    self._refuse_failed_session()
            if idle:
                self.require_idle()
            self._operations += 1
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            with self._lock:
                self._operations -= 1
                if token is not None:
                    _CURRENT.reset(token)
                    self._recovery_thread = None
                if parent is not None:
                    parent._exit_child(failed=failed)

    def _refuse_failed_session(self):
        """Refuse an ordinary operation while a registered mutation session's
        cleanup has failed: it may have left the target mutated or its
        workers running. The session is the refusal's ``cleanup_handle``,
        whose ``close()`` retries as a recovery."""
        from langadapter.mutation import MutationSession
        with self._lock:
            for resource, label in self._resources.values():
                if isinstance(resource, MutationSession) and resource._cleanup_failure is not None:
                    error = OwnershipRefused(
                        f"The {label}'s cleanup failed. Close it before other operations in this use")
                    error.cleanup_handle = resource
                    raise error

    def register_persistent(self, owner, resources):
        """Bind native handles acquired in this scope to their mutation session."""
        with self._lock:
            self.require_active()
            from langadapter.mutation import MutationSession
            if not isinstance(owner, MutationSession) or self._resources.get(id(owner), (None,))[0] is not owner:
                raise OwnershipRefused("Persistent commands require their registered mutation session")
            for resource in resources:
                entry = self._resources.get(id(resource))
                if entry is None and getattr(resource, "_langadapter_command_disposed", False) is True:
                    continue
                if entry is None or entry[0] is not resource or isinstance(resource, _Ticket):
                    raise OwnershipRefused("Persistent command is not acquired in this execution scope")
                self._persistent[id(resource)] = (resource, owner)

    def require_idle(self, *, ignore_children=False):
        from langadapter.mutation import MutationSession
        with self._lock:
            if self._operations:
                raise OwnershipRefused("Workspace has an active execution operation")
            for resource, label in self._resources.values():
                if ignore_children and self._children.get(id(resource)) is resource:
                    continue
                owner = resource if isinstance(resource, MutationSession) else (
                    self._persistent.get(id(resource), (None, None))[1])
                if (not isinstance(owner, MutationSession) or
                        self._resources.get(id(owner), (None,))[0] is not owner or
                        owner._operation.locked() or owner._cleanup_failure is not None):
                    error = OwnershipRefused("Workspace still owns unresolved resources: " + label)
                    error._langadapter_execution_scope = self
                    error._langadapter_resources = (resource,)
                    raise error

    def _only_retained_sessions(self):
        """Whether every resource left is a registered, idle mutation session or
        one of its commands, whose failed cleanup its own handle retries."""
        from langadapter.mutation import MutationSession
        with self._lock:
            if self._operations:
                return False
            for resource, _ in self._resources.values():
                owner = resource if isinstance(resource, MutationSession) else (
                    self._persistent.get(id(resource), (None, None))[1])
                if (not isinstance(owner, MutationSession)
                        or self._resources.get(id(owner), (None,))[0] is not owner
                        or owner._operation.locked()):
                    return False
            return True

    @contextlib.contextmanager
    def borrow(self, validate):
        primary = None
        with self._lock:
            if self._revoked or self._token is None or self._borrow_thread is not None:
                raise OwnershipRefused("Prepared worker requires an idle active use")
            if _CURRENT.get() not in (None, self):
                raise OwnershipRefused("Another execution scope is active on this worker")
            self._refuse_failed_session()
            self.require_idle()
            self._borrow_thread = threading.get_ident()
            token = _CURRENT.set(self)
        try:
            try:
                validate()
            except BaseException:
                with self._lock:
                    self._revoked = True
                raise
            yield
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                self.require_idle()
            except BaseException as cleanup:
                # A session keeping resources after a failed or interrupted
                # cleanup holds the use, refusing new work, only until its own
                # close() succeeds. Anything else revokes the use.
                if not self._only_retained_sessions():
                    with self._lock:
                        self._revoked = True
                if primary is None:
                    raise
                primary._langadapter_execution_scope = self
                primary._langadapter_scope_cleanup_error = cleanup
                if hasattr(primary, "add_note"):
                    try:
                        primary.add_note(str(cleanup))
                    except BaseException:
                        pass
            finally:
                with self._lock:
                    _CURRENT.reset(token)
                    self._borrow_thread = None

    def track(self, resource, label):
        with self._lock:
            if self._token is not None or self._recovery_thread is not None:
                self.require_active()
            resource._langadapter_execution_scope = self
            self._resources[id(resource)] = (resource, label)
        return resource

    def reserve_launch(self):
        with self._lock:
            self.require_active()
            ticket = _Ticket()
            ticket._langadapter_execution_scope = self
            self._resources[id(ticket)] = (ticket, "unfinished native launch")
            return ticket

    def acquired(self, ticket, resource):
        with self._lock:
            if self._resources.get(id(ticket), (None,))[0] is not ticket:
                raise OwnershipRefused("Native acquisition has no current launch reservation")
            # Admission preceded acquisition. Revocation must not drop its handle.
            resource._langadapter_execution_scope = self
            self._resources[id(resource)] = (resource, "owned command")
            del self._resources[id(ticket)]

    def completed(self, resource):
        with self._lock:
            entry = self._resources.get(id(resource))
            if entry is not None and entry[0] is resource:
                del self._resources[id(resource)]
                self._persistent.pop(id(resource), None)
                self._children.pop(id(resource), None)

    def require_quiescent(self, *, current_operation=False):
        with self._lock:
            if current_operation:
                self.require_active()
            elif self._borrow_thread is not None or self._operations:
                error = OwnershipRefused("Workspace has an active worker or operation")
                error._langadapter_execution_scope = self
                raise error
            if not self._resources:
                return
            labels = ", ".join(label for _, label in self._resources.values())
            error = OwnershipRefused("Workspace still owns unresolved resources: " + labels)
            error._langadapter_execution_scope = self
            error._langadapter_resources = tuple(resource for resource, _ in self._resources.values())
            raise error


@contextlib.contextmanager
def operation(context=None):
    binding = context._prepared_copy_binding if context is not None else None
    scope = binding.scope if binding is not None else _CURRENT.get()
    with scope.operation() if isinstance(scope, _Scope) else contextlib.nullcontext():
        if context is not None:
            context._require_execution()
        yield


def track(resource, label):
    scope = _CURRENT.get()
    return scope.track(resource, label) if scope is not None else resource


def completed(resource):
    scope = getattr(resource, "_langadapter_execution_scope", None)
    if isinstance(scope, _Scope):
        scope.completed(resource)


def unresolved_command(error):
    """The command owner ``error`` carries while its process may still run,
    else None. The acquired command's own disposal proof is authoritative:
    the disposed marker, or a Linux command whose control closed."""
    from langadapter._linux_command import _Command

    for name in ("_langadapter_linux_command", "_langadapter_command"):
        owner = getattr(error, name, None)
        if owner is not None and not (
                getattr(owner, "_langadapter_command_disposed", False) is True
                or (isinstance(owner, _Command) and getattr(owner, "_closed", False) is True)):
            return owner
    return None


def reraise_unresolved(error):
    """Re-raise ``error`` rather than turn it into a value-only outcome when it
    carries an unresolved command (``unresolved_command``) or its scope has an
    unfinished launch. An open session in the scope does not count."""
    if unresolved_command(error) is not None:
        raise error
    scope = getattr(error, "_langadapter_execution_scope", None)
    if isinstance(scope, _Scope):
        with scope._lock:
            unfinished = any(isinstance(resource, _Ticket)
                             for resource, _ in scope._resources.values())
        if unfinished:
            raise error


def reserve_launch():
    scope = _CURRENT.get()
    return scope.reserve_launch() if scope is not None else None


def acquired(ticket, resource):
    if ticket is not None:
        scope = ticket._langadapter_execution_scope
        scope.acquired(ticket, resource)


def _command_owner(error):
    linux_owner = getattr(error, "_langadapter_linux_command", None)
    return linux_owner if linux_owner is not None else getattr(error, "_langadapter_command", None)


def failed_launch(ticket, error):
    if ticket is None:
        return
    scope = ticket._langadapter_execution_scope
    if _command_owner(error) is not None:
        running = unresolved_command(error)
        if running is None:
            scope.completed(ticket)
        else:
            acquired(ticket, running)
    error._langadapter_execution_scope = scope
