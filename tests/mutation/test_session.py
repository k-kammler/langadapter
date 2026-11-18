"""Common session behavior using deterministic, resource-owning fixtures."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from langadapter.errors import InvalidRequest
from langadapter.mutation import BY_FAILING_TEST, Classification, MutationClass
from langadapter.mutation import EffectiveSetting, MutationRequest
from langadapter.mutation import Mutant, MutationRun, mutant_key
from langadapter.mutation import MutationCleanupError, MutationSession
from langadapter.outcomes import Outcome
from langadapter.suites import Suite


@pytest.fixture
def mutation_request(tmp_path):
    (tmp_path / "test_target.py").write_text("def test_value(): pass\n", encoding="utf-8")
    return MutationRequest("src/target.py", Suite(str(tmp_path), ("test_target.py",)))


class Session(MutationSession):
    def __init__(self, result=None, error=None, cleanup_error=None):
        super().__init__(workers_requested=2, workers_started=2)
        self.result = result or MutationRun("src/target.py", (), {}, "full", ())
        self.error = error
        self.cleanup_error = cleanup_error
        self.run_calls = []
        self.close_calls = 0
        self.resource = object()

    def measure(self, request):
        self.run_calls.append(request)
        if self.error is not None:
            raise self.error
        return self.result

    def release(self):
        self.close_calls += 1
        if self.cleanup_error is not None:
            raise self.cleanup_error
        self.resource = None


def test_session_base_is_abstract():
    with pytest.raises(TypeError):
        MutationSession()


def test_a_run_records_its_native_measurement(mutation_request):
    session = Session()
    run = session.run(mutation_request)
    assert session.run_calls == [mutation_request]
    assert (run.resolution.requested, run.resolution.method, run.resolution.reason) == (
        "measure", "measured", "native measurement requested")
    assert session.result.resolution is None
    assert session.workers_requested == session.workers_started == 2
    assert session.is_open
    session.close()
    assert not session.is_open
    assert run.to_kill_matrix()["resolution"] == {
        "requested": "measure", "method": "measured", "prior_identity": None,
        "conditions": [], "reason": "native measurement requested"}


def test_a_failed_native_run_keeps_its_outcome(mutation_request):
    session = Session(MutationRun.failed("src/target.py", Outcome.MISSING_SETUP, "no tool"))
    run = session.run(mutation_request)
    assert run.outcome is Outcome.MISSING_SETUP
    assert run.score() is None
    assert run.summary == {}
    assert run.resolution.method == "measured"


def test_results_snapshot_nested_data_and_survive_close(mutation_request):
    settings = {"workers": EffectiveSetting(2, "default", {"nested": [1]})}
    mutant = Mutant(
        mutant_key("src/target.py", "op", 0, 1, 1, "diff"), "1", "KILLED", "NORMAL",
        Classification("killed", MutationClass.DETECTED, BY_FAILING_TEST),
        ("test_value",), "diff", {"status": "killed", "killers": ["test_value"]})
    raw = MutationRun("src/target.py", (mutant,), {"total": 1}, "full", (), settings=settings)
    session = Session(raw)
    first = session.run(mutation_request)
    raw.summary["total"] = 10
    raw.settings["workers"].inputs["nested"].append(2)
    raw.mutants[0].row["killers"].append("other_test")
    second = session.run(mutation_request)
    session.close()
    assert first.summary == {"total": 1}
    assert first.settings["workers"].inputs == {"nested": [1]}
    assert first.mutants[0].row["killers"] == ["test_value"]
    assert second.mutants[0].row["killers"] == ["test_value", "other_test"]
    assert second.summary == {"total": 10}
    second.summary["total"] = 20
    assert first.summary == {"total": 1}
    assert raw.summary == {"total": 10}


def test_closed_session_does_not_validate_probe_or_stage(mutation_request, monkeypatch):
    session = Session()
    session.close()
    session.close()
    monkeypatch.setattr(MutationRequest, "validate", lambda self: pytest.fail("closed validation"))
    run = session.run(mutation_request)
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert run.failure == "session"
    assert run.score() is None
    assert run.resolution.requested == "measure"
    assert not session.run_calls
    assert session.close_calls == 1
    with pytest.raises(InvalidRequest, match="closed"):
        session.__enter__()


def test_failed_close_retains_live_handle_and_can_retry(mutation_request, monkeypatch):
    session = Session(cleanup_error=OSError("busy worker"))
    resource = session.resource
    with pytest.raises(MutationCleanupError) as failed:
        session.close()
    assert failed.value.cleanup_handle is session
    assert isinstance(failed.value.__cause__, OSError)
    assert session.is_open
    assert session.resource is resource
    monkeypatch.setattr(MutationRequest, "validate", lambda self: pytest.fail("cleanup-pending validation"))
    run = session.run(mutation_request)
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "cleanup must be retried" in run.reason
    assert not session.run_calls
    session.cleanup_error = None
    failed.value.cleanup_handle.close()
    assert session.resource is None
    assert not session.is_open


def test_an_incomplete_native_run_keeps_its_outcome(mutation_request):
    incomplete = MutationRun("src/target.py", (), {}, "full", (), outcome=Outcome.TIMED_OUT,
                             incomplete={"pending": 1}, reason="wall clock ended")
    with Session(incomplete) as session:
        run = session.run(mutation_request)
        assert run.outcome is Outcome.TIMED_OUT
        assert run.reason == "wall clock ended"
        assert run.incomplete == {"pending": 1}
        assert run.score() is None


def test_failed_open_status_never_invokes_native_run(mutation_request):
    class FailedOpen(MutationSession):
        def __init__(self):
            super().__init__(outcome=Outcome.MISSING_SETUP, reason="engine unavailable")

        def measure(self, request):
            pytest.fail("failed session launched native run")

        def release(self):
            pass

    with FailedOpen() as session:
        run = session.run(mutation_request)
        assert run.outcome is Outcome.MISSING_SETUP
        assert run.reason == "engine unavailable"
        assert run.resolution.method == "measured"


@pytest.mark.parametrize("error", [RuntimeError("worker failed"), KeyboardInterrupt(), SystemExit(3)])
def test_operation_exception_or_cancellation_quiesces_before_propagating(mutation_request, error):
    session = Session(error=error)
    with pytest.raises(type(error)) as raised:
        session.run(mutation_request)
    assert raised.value is error
    assert session.resource is None
    assert not session.is_open
    session.close()
    assert session.close_calls == 1


def test_cancellation_cleanup_failure_preserves_original_cause_and_handles(mutation_request):
    original = KeyboardInterrupt()
    session = Session(error=original, cleanup_error=OSError("busy"))
    # The interrupt stays the error and carries the cleanup handle and failure.
    with pytest.raises(KeyboardInterrupt) as failed:
        session.run(mutation_request)
    assert failed.value is original
    assert failed.value.cleanup_handle is session
    if hasattr(original, "add_note"):
        assert "busy" in "".join(original.__notes__)
    assert session.resource is not None
    session.cleanup_error = None
    session.close()


def test_context_cleanup_failure_preserves_body_exception():
    session = Session(cleanup_error=OSError("busy"))
    original = ValueError("body failed")
    with pytest.raises(MutationCleanupError) as failed:
        with session:
            raise original
    assert failed.value.__cause__ is original
    assert failed.value.cleanup_handle is session
    session.cleanup_error = None
    session.close()


def test_overlap_is_refused_before_touching_engine_and_run_can_finish(mutation_request):
    entered, release = Event(), Event()

    class WaitingSession(Session):
        def measure(self, request):
            entered.set()
            assert release.wait(10), "test did not release the fake operation"
            return super().measure(request)

    session = WaitingSession()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(session.run, mutation_request)
        try:
            assert entered.wait(10)
            with pytest.raises(InvalidRequest, match="active"):
                session.run(mutation_request)
            with pytest.raises(InvalidRequest, match="active"):
                session.close()
            assert session.close_calls == 0
        finally:
            release.set()
        assert future.result().resolution.method == "measured"
    assert len(session.run_calls) == 1
    session.close()


def test_invalid_request_releases_guard_without_closing_healthy_session(mutation_request):
    session = Session()
    with pytest.raises(InvalidRequest):
        session.run(replace(mutation_request, attribution="file"))
    assert session.is_open
    assert not session.run_calls
    session.run(mutation_request)
    session.close()


def test_progress_must_be_callable_and_never_changes_request_identity(mutation_request):
    events = []
    with_progress = replace(mutation_request, progress=events.append)
    assert with_progress == mutation_request and "append" not in repr(with_progress)
    with pytest.raises(InvalidRequest, match="progress"):
        replace(mutation_request, progress="every second").validate()


@pytest.mark.parametrize("options", [
    {"workers_requested": True}, {"workers_requested": 0}, {"workers_started": 2},
    {"workers_started": -1}, {"outcome": "completed"}, {"reason": None},
])
def test_invalid_session_facts_refused(options):
    class Minimal(MutationSession):
        def measure(self, request):
            raise AssertionError("not launched")

        def release(self):
            pass

    with pytest.raises(InvalidRequest):
        Minimal(**options)


class SlowSession(Session):
    """A run whose native work is a real process that runs for minutes."""

    def measure(self, request):
        import sys

        from langadapter import _process_tree

        self.run_calls.append(request)
        done = _process_tree.run([sys.executable, "-c", "import time; time.sleep(120)"],
                                 timeout=180)
        self.returncode = done.returncode
        return self.result


@pytest.mark.parametrize("cleanup_error", [None, OSError("workers still busy")])
def test_a_cancelled_measurement_ends_at_once_and_closes_the_session(
        mutation_request, cleanup_error):
    """Cancelling a measurement running on another thread ends its process
    at once, closes the session and raises MeasurementCancelled, a
    KeyboardInterrupt, with no result. A cleanup that cannot finish rides on
    it as cleanup_handle. A cancelled session runs nothing more, and
    cancelling again or when idle is harmless."""
    import time as clock

    from langadapter.mutation import MeasurementCancelled

    session = SlowSession(cleanup_error=cleanup_error)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(session.run, mutation_request)
        deadline = clock.monotonic() + 30
        while session._cancellation is None or not session._cancellation._live:
            assert clock.monotonic() < deadline
            clock.sleep(0.05)
        started = clock.monotonic()
        session.cancel()
        session.cancel()
        with pytest.raises(MeasurementCancelled) as cancelled:
            future.result(timeout=60)
    assert clock.monotonic() - started < 30
    assert isinstance(cancelled.value, KeyboardInterrupt)
    if cleanup_error is None:
        assert session.close_calls == 1 and session.resource is None
        assert not hasattr(cancelled.value, "cleanup_handle")
    else:
        assert cancelled.value.cleanup_handle is session and session.resource is not None
        session.cleanup_error = None
    with pytest.raises(MeasurementCancelled):
        session.run(mutation_request)
    session.close()
    assert session.resource is None
    idle = Session()
    idle.cancel()
    with pytest.raises(MeasurementCancelled):
        idle.run(mutation_request)
    idle.close()


@pytest.mark.parametrize("cleanup_error", [None, OSError("workers still busy")])
def test_a_cancel_while_idle_closes_the_session_before_the_next_operation_raises(
        mutation_request, cleanup_error):
    """A cancel with no measurement in flight: the next operation runs
    nothing, closes the session and raises MeasurementCancelled. A cleanup
    that cannot finish rides on it as cleanup_handle."""
    from langadapter.mutation import MeasurementCancelled

    session = Session(cleanup_error=cleanup_error)
    session.cancel()
    with pytest.raises(MeasurementCancelled) as cancelled:
        session.run(mutation_request)
    assert session.run_calls == []
    if cleanup_error is None:
        assert session.close_calls == 1 and session.resource is None
    else:
        assert cancelled.value.cleanup_handle is session and session.resource is not None
