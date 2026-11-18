"""Stopping an operation in flight from another thread.

A ``CancelScope`` entered around adapter calls registers every process they
start (suite runs, coverage, source operations, mutation sessions).
``cancel()``, from any thread or a signal handler, asks each process tree to
end at once and refuses new ones. The operation then raises
``langadapter.errors.OperationCancelled`` after its own cleanup, never a
result: a command ending under a cancelled scope raises it once its process
tree is disposed of, so no observation comes from a stopped process, and its
scratch is removed once nothing is unresolved. Leaving the scope after a
cancel raises it too, whatever the block returned, an error of the block's own
as its cause.

The binding belongs to the entering thread and what it calls. A thread started
in the block is outside it unless it runs in a copy of that thread's context
(``contextvars.copy_context()``). One scope may be entered on several threads.
Nested scopes and mutation sessions' own cancels are linked, so cancelling the
outer scope reaches them.
"""
from __future__ import annotations

import threading
from typing import Any, List

from langadapter import _process_tree
from langadapter.errors import OperationCancelled

__all__ = ["CancelScope", "OperationCancelled"]


class CancelScope:
    """The operations a caller may stop from another thread: enter it with ``with``
    around them, call ``cancel`` to stop them. A cancelled scope stays cancelled:
    every later operation in it starts nothing and raises ``OperationCancelled``."""

    def __init__(self) -> None:
        self._cancellation = _process_tree.Cancellation()
        self._bindings = threading.local()

    @property
    def cancelled(self) -> bool:
        """Whether ``cancel`` was called."""
        return self._cancellation.cancelled

    def cancel(self) -> None:
        """Ask every process in the scope to end now and refuse new ones, returning
        at once. Safe, and repeatable, from any thread or signal handler."""
        self._cancellation.cancel()

    def __enter__(self) -> "CancelScope":
        binding = _process_tree.cancellable(self._cancellation)
        binding.__enter__()
        stack: List[Any] = getattr(self._bindings, "stack", None) or []
        stack.append(binding)
        self._bindings.stack = stack
        return self

    def __exit__(self, kind, error, traceback) -> bool:
        self._bindings.stack.pop().__exit__(None, None, None)
        if not self.cancelled or isinstance(error, OperationCancelled):
            return False
        raise OperationCancelled("the operation was cancelled") from error
