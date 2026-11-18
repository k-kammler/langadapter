"""PIT's public session adopts the common guard without starting a JVM."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sys
from threading import Event

import pytest

import langadapter as lb
from langadapter.java._mutation import MutationSession
from langadapter.mutation import MutationRequest
from langadapter.mutation import MutationRun
from langadapter.mutation import MutationSession as CommonMutationSession


@pytest.fixture
def mutation_request(tmp_path):
    (tmp_path / "test_target.java").write_text("class TargetTest {}", encoding="utf-8")
    return MutationRequest("Target.java", lb.Suite(str(tmp_path), ("test_target.java",)))


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    result = lb.create("java", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    monkeypatch.setattr(result, "_project_file", lambda field, path: Path(tmp_path) / path)
    monkeypatch.setattr(result, "_suite_files", lambda suite: list(suite.test_files))
    return result


def test_pit_session_is_common_and_measures_in_its_prepared_project(adapter, mutation_request,
                                                                     monkeypatch):
    session = MutationSession(adapter, workers=2)
    assert isinstance(session, CommonMutationSession)
    native = MutationRun.failed("Target.java", lb.Outcome.TIMED_OUT, "native incomplete")
    called = []
    def measure(request):
        called.append((request, session._prepared))
        return native
    monkeypatch.setattr(session, "measure", measure)
    with session:
        first = session.run(mutation_request)
        assert len(called) == 1 and called[0][1]
        assert first.outcome is lb.Outcome.TIMED_OUT and first.score() is None
        assert first.resolution.requested == "measure" and first.resolution.method == "measured"
    assert not session.is_open and session._prepared is None
    assert native.resolution is None


def test_invalid_preflight_preserves_pit_session(adapter, mutation_request, monkeypatch):
    session = MutationSession(adapter)
    monkeypatch.setattr(session, "measure", lambda request: pytest.fail("invalid request ran"))
    invalid = replace(mutation_request, timeout_journal=True)
    with pytest.raises(lb.InvalidRequest, match="timeout_journal"):
        session.run(invalid)
    assert session.is_open
    session.close()


def test_closed_pit_does_not_resolve_paths_or_probe_tools(adapter, mutation_request, monkeypatch):
    session = MutationSession(adapter)
    session.close()
    monkeypatch.setattr(adapter, "_project_file", lambda *args: pytest.fail("closed session resolved target"))
    result = session.run(mutation_request)
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE and result.score() is None
    assert result.resolution.requested == "measure"
    assert result.settings["workers"].value == 1
    session.close()


def test_pit_overlap_and_close_are_refused_before_native_state_changes(adapter, mutation_request, monkeypatch):
    session = MutationSession(adapter)
    started, release = Event(), Event()
    def measure(request):
        started.set()
        assert release.wait(5)
        return MutationRun.failed(request.target, lb.Outcome.TIMED_OUT, "fixture")
    monkeypatch.setattr(session, "measure", measure)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(session.run, mutation_request)
        try:
            assert started.wait(5)
            with pytest.raises(lb.InvalidRequest, match="active"):
                session.run(mutation_request)
            with pytest.raises(lb.InvalidRequest, match="active"):
                session.close()
        finally:
            release.set()
        assert future.result().outcome is lb.Outcome.TIMED_OUT
    session.close()


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(3), OSError("failed execution")])
def test_pit_cancellation_closes_common_lifetime(adapter, mutation_request, monkeypatch, error):
    session = MutationSession(adapter)
    def broken(request):
        raise error
    monkeypatch.setattr(session, "measure", broken)
    with pytest.raises(type(error)):
        session.run(mutation_request)
    assert not session.is_open and session._prepared is None
