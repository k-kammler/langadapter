"""Native Python session ownership with isolated, process-free engine fixtures."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from langadapter.context import LocalExecution
from langadapter.errors import InvalidRequest
from langadapter.mutation import Catalogue, EffectiveSetting, MutationRequest
from langadapter.mutation import MutationRun
from langadapter.mutation import MutationCleanupError, MutationSession as CommonSession
from langadapter.outcomes import Outcome
from langadapter.python import _cosmic_ray as native
from langadapter.python._mutation import configured_catalogue
from langadapter.suites import Suite


class NativeHandle:
    def __init__(self, *, open_error=None, close_error=None, workers=2):
        self.open_error = open_error
        self.close_error = close_error
        self.use_http = workers > 1
        self.worker_urls = [f"worker:{i}" for i in range(workers)]
        self.resource = None
        self.close_calls = 0
        self.run_calls = 0

    def __enter__(self):
        self.resource = object()
        if self.open_error is not None:
            raise self.open_error
        return self

    def __exit__(self, *args):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        self.resource = None

    def run(self, **kwargs):
        self.run_calls += 1
        raise AssertionError("a test did not supply its isolated native outcome")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / "test_target.py").write_text("def test_value(): pass\n", encoding="utf-8")
    adapter = SimpleNamespace(
        context=SimpleNamespace(root=str(tmp_path), execution=LocalExecution()))
    handles = []

    class Engine:
        def __init__(self, config, adapter):
            self.config = config

        def session(self, **kwargs):
            assert handles, "test did not supply a native session handle"
            return handles.pop(0)

    monkeypatch.setattr(native, "CosmicRayEngine", Engine)
    request = MutationRequest("target.py", Suite(str(tmp_path), ("test_target.py",)))
    return adapter, handles, request


def test_failed_open_quiesces_partial_handle_before_returning_failed_session(setup):
    adapter, handles, request = setup
    handle = NativeHandle(open_error=RuntimeError("entry failed"))
    handles.append(handle)
    session = native.MutationSession(adapter, workers=2, distributor="http")
    assert isinstance(session, CommonSession)
    assert handle.close_calls == 1
    assert handle.resource is None
    assert session.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert not session.is_open
    with session:
        run = session.run(request)
        assert run.score() is None
        assert "entry failed" in run.reason
        assert run.resolution.requested == "measure"
    assert handle.run_calls == 0


def test_failed_open_cleanup_retains_partial_native_handle_for_retry(setup):
    adapter, handles, _ = setup
    original = RuntimeError("entry failed")
    handle = NativeHandle(open_error=original, close_error=OSError("still active"))
    handles.append(handle)
    with pytest.raises(MutationCleanupError) as failed:
        native.MutationSession(adapter, workers=2, distributor="http")
    session = failed.value.cleanup_handle
    assert failed.value.__cause__ is original
    assert session._session is handle
    assert handle.resource is not None
    handle.close_error = None
    session.close()
    assert session._session is None
    assert handle.resource is None


def test_cancelled_open_cleans_then_propagates_cancellation(setup):
    adapter, handles, _ = setup
    original = KeyboardInterrupt()
    handle = NativeHandle(open_error=original)
    handles.append(handle)
    with pytest.raises(KeyboardInterrupt) as raised:
        native.MutationSession(adapter, workers=2, distributor="http")
    assert raised.value is original
    assert handle.resource is None
    assert handle.close_calls == 1


def test_failed_close_does_not_discard_native_handle(setup):
    adapter, handles, request = setup
    handle = NativeHandle(close_error=OSError("active"))
    handles.append(handle)
    session = native.MutationSession(adapter, workers=2, distributor="http")
    with pytest.raises(MutationCleanupError) as failed:
        session.close()
    assert failed.value.cleanup_handle is session
    assert session._session is handle
    assert handle.resource is not None
    run = session.run(request)
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "cleanup must be retried" in run.reason
    handle.close_error = None
    session.close()
    session.close()
    assert handle.close_calls == 2
    assert session._session is None


def test_rejected_partial_fleet_returns_failed_quiescent_session(setup):
    adapter, handles, request = setup
    handle = NativeHandle(workers=1)
    handles.append(handle)
    with native.MutationSession(adapter, workers=2, distributor="http") as session:
        assert not session.is_open
        assert session.reason.startswith("1 of 2")
        run = session.run(request)
        assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
        assert run.score() is None
    assert handle.resource is None


def test_closed_native_session_does_not_resolve_target_or_validate(setup, monkeypatch):
    adapter, handles, request = setup
    handle = NativeHandle()
    handles.append(handle)
    session = native.MutationSession(adapter, workers=2, distributor="http")
    session.close()
    monkeypatch.setattr(session, "_target", lambda target: pytest.fail("closed target probe"))
    monkeypatch.setattr(MutationRequest, "validate", lambda self: pytest.fail("closed validation"))
    run = session.run(request)
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert run.resolution.method == "measured"
    assert run.settings["workers"].value == 2
    assert run.settings["per_mutant_timeout"].value == 10
    assert handle.run_calls == 0


def test_invalid_native_request_leaves_session_reusable(setup, monkeypatch):
    adapter, handles, request = setup
    handle = NativeHandle()
    handles.append(handle)
    session = native.MutationSession(adapter, workers=2, distributor="http")
    monkeypatch.setattr(session, "_target", lambda target: SimpleNamespace(name=target))
    with pytest.raises(InvalidRequest, match="catalogue.name"):
        session.run(dataclasses.replace(request, catalogue=Catalogue(name="not_an_operator_set")))
    assert session.is_open
    assert handle.run_calls == handle.close_calls == 0
    monkeypatch.setattr(session, "measure", lambda request: MutationRun("target.py", (), {}, "full", ()))
    assert session.run(request).resolution.requested == "measure"
    session.close()


def test_an_operator_cosmic_ray_does_not_register_refuses_the_run(setup, monkeypatch):
    """A single word that is no operator set stays an operator name
    (``cosmic_ray_operators``). A name outside Cosmic Ray's registry refuses
    the run before it measures, and the refusal names the operator sets."""
    adapter, handles, request = setup
    handle = NativeHandle()
    handles.append(handle)
    session = native.MutationSession(adapter, workers=2, distributor="http")
    monkeypatch.setattr(session, "_target", lambda target: SimpleNamespace(name=target))
    monkeypatch.setattr(session._engine, "full_operators", raising=False,
                        value=lambda: ["core/ReplaceAndWithOr", "core/ReplaceOrWithAnd"])
    catalogue = configured_catalogue({"cosmic_ray_operators": "wong_mathur"})
    assert catalogue.operators == ("wong_mathur",)
    with pytest.raises(InvalidRequest, match="catalogue.operators") as refused:
        session.run(dataclasses.replace(request, catalogue=catalogue))
    assert "does not register wong_mathur." in str(refused.value)
    assert "wong_mathur_selective" in str(refused.value)
    assert session.is_open and handle.run_calls == 0
    session.close()


def test_native_run_snapshots_and_derive_fallback(setup, monkeypatch):
    adapter, handles, request = setup
    handle = NativeHandle()
    handles.append(handle)
    session = native.MutationSession(adapter, workers=2, distributor="http")
    monkeypatch.setattr(session, "_target", lambda target: SimpleNamespace(name=target))
    result = MutationRun("target.py", (), {"total": 0}, "full", ())
    monkeypatch.setattr(session, "measure", lambda request: result)
    first = session.run(request)
    result.summary["total"] = 1
    second = session.run(request)
    session.close()
    assert first.summary == {"total": 0}
    assert second.summary == {"total": 1}
    assert second.resolution.requested == "measure"
    assert second.resolution.method == "measured"


def test_failed_nested_entry_is_closed_and_outer_session_is_quiesced(setup, monkeypatch):
    adapter, handles, request = setup
    outer = NativeHandle()
    nested = NativeHandle(open_error=RuntimeError("nested failed"), workers=1)
    handles.extend([outer, nested])
    session = native.MutationSession(adapter, workers=2, distributor="http")
    monkeypatch.setattr(session, "_target", lambda target: SimpleNamespace(name=target))
    monkeypatch.setattr(session, "_carries_instrument", lambda request: False)
    with pytest.raises(RuntimeError, match="nested failed"):
        session.run(dataclasses.replace(request, workers=1))
    assert nested.close_calls == outer.close_calls == 1
    assert nested.resource is outer.resource is None
    assert session._nested_session is session._session is None


def test_failed_nested_cleanup_keeps_both_handles_until_retry(setup, monkeypatch):
    adapter, handles, request = setup
    original = KeyboardInterrupt()
    outer = NativeHandle()
    nested = NativeHandle(open_error=original, close_error=OSError("nested active"), workers=1)
    handles.extend([outer, nested])
    session = native.MutationSession(adapter, workers=2, distributor="http")
    monkeypatch.setattr(session, "_target", lambda target: SimpleNamespace(name=target))
    monkeypatch.setattr(session, "_carries_instrument", lambda request: False)
    # The interrupt stays the error and carries the failed cleanup's handle.
    with pytest.raises(KeyboardInterrupt) as failed:
        session.run(dataclasses.replace(request, workers=1))
    assert failed.value is original and failed.value.cleanup_handle is session
    if hasattr(original, "add_note"):
        assert "nested active" in "".join(original.__notes__)
    assert session._nested_session is nested
    assert session._session is outer
    assert outer.resource is not None
    nested.close_error = None
    session.close()
    assert nested.resource is outer.resource is None


def test_fewer_workers_of_a_larger_http_fleet_open_their_own_session(setup, monkeypatch):
    """A run asking two workers of a four-worker http fleet opens a session
    of two, as its settings and identity record."""
    adapter, handles, request = setup
    outer = NativeHandle(workers=4)
    nested = NativeHandle(open_error=RuntimeError("nested opened"), workers=2)
    handles.append(outer)
    session = native.MutationSession(adapter, workers=4, distributor="http")
    asked = []
    monkeypatch.setattr(session._engine, "session",
                        lambda **kwargs: asked.append(kwargs) or nested)
    monkeypatch.setattr(session, "_target", lambda target: SimpleNamespace(name=target))
    monkeypatch.setattr(session, "_carries_instrument", lambda request: False)
    with pytest.raises(RuntimeError, match="nested opened"):
        session.run(dataclasses.replace(request, workers=2))
    assert [kwargs["workers"] for kwargs in asked] == [2]
    assert outer.run_calls == 0


def test_native_specific_settings_type_is_refused_before_engine_open(setup):
    adapter, handles, _ = setup
    with pytest.raises(InvalidRequest, match="CosmicRaySettings"):
        native.MutationSession(adapter, settings={"workers": EffectiveSetting(1, "default")})
    assert not handles


@pytest.mark.parametrize("handle,reason", [
    (dict(open_error=RuntimeError("entry failed")), "the worker fleet did not start: entry failed"),
    (dict(workers=1), "1 of 2 workers started; the session does not allow fewer"),
])
def test_a_failed_opening_leaves_no_open_session_in_its_scope(setup, tmp_path, handle, reason):
    """A session whose fleet did not start, or started fewer workers than it
    allows, holds nothing once it closed what the opening started: the
    prepared copy's use ends without an open session, and a run returns why
    the opening failed."""
    from langadapter._prepared_copy import PreparedCopy

    adapter, handles, request = setup
    source, zone = tmp_path / "source", tmp_path / "zone"
    source.mkdir()
    zone.mkdir()
    copy = PreparedCopy.create(str(source), parent=str(zone), receipt_parent=str(tmp_path))
    handles.append(NativeHandle(**handle))
    with copy.use():
        session = native.MutationSession(adapter, workers=2, distributor="http")
        run = session.run(request)
    assert session.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert run.reason == reason
