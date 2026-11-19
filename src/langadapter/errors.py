"""The exceptions the package raises on purpose.

A failed execution is never raised: it is the result's outcome. The
LangAdapterError subclasses mark an uninterpretable request, an unregistered
language, a registration conflict, a file the library may not change or
remove, and an unreadable source. OperationCancelled derives from
KeyboardInterrupt instead. A failure that leaves something to clean up
carries its owners, which ``recovery_owners`` returns.
"""
from __future__ import annotations

from typing import Iterable, Optional, Tuple


class LangAdapterError(Exception):
    """The base of the package's errors. Cancellations derive from KeyboardInterrupt."""


class InvalidRequest(LangAdapterError, ValueError):
    """A request or project context the adapter cannot interpret. ``field``
    names the offending field, such as ``execution.helper_python``."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class ExtensionFoundOnlyByImport(InvalidRequest):
    """A dotted module item ``resolve_extension`` cannot find without running
    a parent package, such as a module in a package whose code changes its
    own search path: the finders show its top-level package but not the
    module itself. Only ``load_extension``, which imports the package, finds
    the module or finds that it does not exist."""


class UnsupportedLanguage(LangAdapterError, LookupError):
    """No adapter is registered for the requested language or file.
    ``registered`` lists the languages that are."""

    def __init__(self, requested: str, registered: Iterable[str]) -> None:
        self.requested = requested
        self.registered: Tuple[str, ...] = tuple(sorted(registered))
        listed = ", ".join(self.registered) or "none"
        super().__init__(
            f"no adapter is registered for {requested!r}. Registered: {listed}")


class RegistrationConflict(LangAdapterError, ValueError):
    """A registration claims a language name or file extension that another
    adapter holds. ``holder`` names that adapter."""

    def __init__(self, claimed: str, holder: str) -> None:
        self.claimed = claimed
        self.holder = holder
        super().__init__(
            f"{claimed!r} is registered by the {holder!r} adapter. Register "
            f"with replace=True to replace it")


class OwnershipRefused(LangAdapterError, RuntimeError):
    """A refusal to change or remove files whose ownership is unconfirmed: a
    prepared copy or scratch directory no longer matching its ownership record,
    or a cleanup of created files in a context ``prepare_project`` did not make."""


class SourceReadError(LangAdapterError, ValueError):
    """A source the parser of a source method (``check_structure``) cannot read,
    raised instead of an empty result. The message says where, ``parser`` names
    the parser. Typed source operations report ``SourceStatus.PARSE_ERROR``."""

    def __init__(self, message: str, parser: Optional[str] = None) -> None:
        super().__init__(message)
        self.parser = parser


class OperationCancelled(KeyboardInterrupt):
    """An operation its ``CancelScope`` stopped, raised after its own cleanup.
    Like an interrupt it is no failure and no result, never caught as an
    ordinary error. An error the operation raised itself is its cause."""


def recovery_owners(error: BaseException) -> Tuple[object, ...]:
    """The owners ``error`` carries whose cleanup is not confirmed, empty
    when it carries none: a mutation session whose cleanup failed (its
    ``cleanup_handle``), a prepared copy's owner, or a failed
    preparation's, whose ``close()`` removes the directories it allocated, the
    copy itself, an execution scope's failed cleanup, and a command with
    its execution scope, each once. A launch that failed and left nothing
    running carries no command or scope owner: each command it carries is
    disposed, or is a Linux command whose control closed. An owner is
    recovery evidence, never proof that nothing runs."""
    from langadapter._execution_scope import _command_owner, unresolved_command

    owners = [getattr(error, name, None) for name in (
        "cleanup_handle", "_langadapter_prepared_project", "_langadapter_prepared_copy",
        "_langadapter_scope_cleanup_error")]
    if _command_owner(error) is None or unresolved_command(error) is not None:
        owners += [getattr(error, name, None) for name in (
            "_langadapter_linux_command", "_langadapter_command", "_langadapter_execution_scope")]
    unique = []
    for owner in owners:
        if owner is not None and all(owner is not kept for kept in unique):
            unique.append(owner)
    return tuple(unique)


class MeasurementCancelled(OperationCancelled):
    """A measurement its session's ``cancel()`` stopped, raised after cleanup.
    If cleanup could not finish, ``cleanup_handle`` is the session, whose ``close()``
    retries."""
