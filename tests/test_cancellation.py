"""The public cancel scope (``langadapter.cancellation``): an operation in
a ``CancelScope`` that another thread cancels stops its processes at once
and raises ``OperationCancelled`` after its own cleanup, never a result."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import langadapter as lb
from langadapter import _process_tree
from langadapter.errors import MeasurementCancelled, OperationCancelled
from langadapter.execution import LocalExecutor, work_dir_tempdir

SLEEP = [sys.executable, "-c", "import time; time.sleep(120)"]


def _in_thread(work):
    """Run ``work`` on its own thread: what it returned or raised."""
    outcome = {}

    def target():
        try:
            outcome["result"] = work()
        except BaseException as error:  # the test reads it
            outcome["error"] = error

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def _wait_until(condition, seconds=60):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_the_scope_and_its_error_are_public():
    assert lb.CancelScope is lb.cancellation.CancelScope
    assert lb.OperationCancelled is OperationCancelled
    assert issubclass(OperationCancelled, KeyboardInterrupt)
    assert issubclass(MeasurementCancelled, OperationCancelled)


def test_an_uncancelled_scope_changes_nothing():
    scope = lb.CancelScope()
    with scope:
        done = _process_tree.run([sys.executable, "-c", "print(1)"], timeout=60)
    assert done.returncode == 0 and not scope.cancelled
    with pytest.raises(ValueError):
        with scope:
            raise ValueError("an ordinary error")


def test_a_cancel_from_another_thread_ends_the_run_with_no_result():
    scope = lb.CancelScope()
    recorded = {}

    def operation():
        with scope:
            # Recorded inside the scope: the run itself must not return.
            recorded["run"] = _process_tree.run(SLEEP, timeout=180)

    thread, outcome = _in_thread(operation)
    _wait_until(lambda: scope._cancellation._live)
    started = time.monotonic()
    scope.cancel()
    thread.join(60)
    assert not thread.is_alive() and time.monotonic() - started < 30
    assert recorded == {}
    assert isinstance(outcome["error"], OperationCancelled)
    assert scope._cancellation._live == []
    # A cancelled scope starts nothing more.
    with pytest.raises(OperationCancelled):
        with scope:
            _process_tree.start([sys.executable, "-c", "pass"])
    scope.cancel()


def test_leaving_a_cancelled_scope_raises_whatever_the_block_did():
    scope = lb.CancelScope()
    with pytest.raises(OperationCancelled):
        with scope:
            scope.cancel()
    with pytest.raises(OperationCancelled) as raised:
        with scope:
            raise ValueError("the block's own error")
    assert isinstance(raised.value.__cause__, ValueError)
    measured = MeasurementCancelled("a mutation session's own")
    with pytest.raises(MeasurementCancelled) as raised:
        with scope:
            raise measured
    assert raised.value is measured


def test_an_inner_cancellation_is_cancelled_with_its_outer_scope():
    """A cancellation bound inside the scope, as a mutation session binds
    one, is cancelled with it while bound."""
    outer = lb.CancelScope()
    inner = _process_tree.Cancellation()
    with pytest.raises(OperationCancelled):
        with outer:
            with _process_tree.cancellable(inner):
                nested = lb.CancelScope()
                with pytest.raises(OperationCancelled):
                    with nested:
                        outer.cancel()
                assert inner.cancelled and nested.cancelled
            assert outer._cancellation._children == []
    with pytest.raises(OperationCancelled):
        with outer:
            late = _process_tree.Cancellation()
            with _process_tree.cancellable(late):
                assert late.cancelled
    assert not _process_tree.Cancellation().cancelled


def test_one_scope_entered_on_two_threads_stops_both():
    scope = lb.CancelScope()

    def operation():
        with scope:
            return _process_tree.run(SLEEP, timeout=180)

    first, first_outcome = _in_thread(operation)
    second, second_outcome = _in_thread(operation)
    _wait_until(lambda: len(scope._cancellation._live) == 2)
    scope.cancel()
    for thread in (first, second):
        thread.join(60)
        assert not thread.is_alive()
    for outcome in (first_outcome, second_outcome):
        assert isinstance(outcome.get("error"), OperationCancelled)


def test_the_scratch_of_a_cancelled_operation_is_removed(tmp_path, monkeypatch):
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable))
    executor = LocalExecutor()
    scope = lb.CancelScope()
    made = []

    def operation():
        with scope:
            with work_dir_tempdir(context, executor) as directory:
                made.append(directory)
                Path(directory, "partial.txt").write_text("x", encoding="utf-8")
                return executor.run(SLEEP, timeout=180)

    thread, outcome = _in_thread(operation)
    _wait_until(lambda: scope._cancellation._live)
    scope.cancel()
    thread.join(60)
    assert isinstance(outcome.get("error"), OperationCancelled)
    assert made and not os.path.exists(made[0])
    assert sorted(path.name for path in scratch.iterdir()) == []
