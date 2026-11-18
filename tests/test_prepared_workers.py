from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _execution_scope as life, _process_tree
from langadapter._owned_directory import OwnershipRefused
from langadapter._prepared_copy import WorkspaceBusy
from langadapter.mutation import MutationRequest
from langadapter.mutation import MutationCleanupError, MutationSession
from langadapter.mutation import MutationRun
from langadapter.runs import SuiteRunRequest


@pytest.fixture(autouse=True)
def no_native(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Prepared worker fixtures start no native processes")
    monkeypatch.setattr(_process_tree.subprocess, "Popen", forbidden)
    monkeypatch.setattr(_process_tree.subprocess, "run", forbidden)


@pytest.fixture
def prepared(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "target.py").write_text("original", encoding="ascii")
    owner = lb.prepare_project(lb.ProjectContext(str(source), lb.Interpreter(path=sys.executable)),
                               parent=str(tmp_path), receipt_parent=str(tmp_path))
    return owner


class Session(MutationSession):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.failed = False

    def measure(self, request):
        self.calls += 1
        return MutationRun.failed(request.target, lb.Outcome.INFRASTRUCTURE_FAILURE, "controlled result")

    def release(self):
        if self.failed:
            raise OSError("controlled cleanup refused")


def _thread(action):
    failures = []
    def run():
        try:
            action()
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    return failures


def _request(root, tmp_path):
    suite = tmp_path / "tests"
    suite.mkdir(exist_ok=True)
    (suite / "test_target.py").write_text("def test_target(): pass\n", encoding="ascii")
    return MutationRequest(target=str(Path(root) / "target.py"),
                           suite=lb.Suite(str(suite), ("test_target.py",)))


@pytest.mark.parametrize("language", ["python", "java", "prepared-worker-fixture"])
def test_public_worker_reuses_context_session_and_lease(prepared, tmp_path, monkeypatch, language):
    class Native(lb.LangAdapter):
        language = "prepared-worker-fixture"
    if language == Native.language:
        lb.register(language, (".worker-fixture",), Native)
    try:
        with prepared.use() as context:
            native = lb.create(language, context)
            monkeypatch.setattr(type(native), "operation_context_issue", lambda *args: "")
            monkeypatch.setattr(type(native), "execution_setup_issue", lambda *args: "")
            monkeypatch.setattr(type(native), "native_mutation_engines", ("controlled",))
            monkeypatch.setattr(type(native), "mutation_execution_kinds", ("local",))
            monkeypatch.setattr(type(native), "validate_mutation_session_options", lambda *a, **k: None,
                                raising=False)
            monkeypatch.setattr(type(native), "open_mutation_session", lambda *a, **k: Session(), raising=False)
            session = native.mutations.open_session("controlled")
            asked = _request(context.root, tmp_path)
            lease = prepared._prepared_copy.lease.read_bytes()
            (Path(context.root) / "target.py").write_text("during use", encoding="ascii")
            for _ in range(2):
                def borrowed():
                    with prepared.worker(context) as selected:
                        assert selected is context and life._CURRENT.get() is session._langadapter_execution_scope
                        assert lb.create(language, selected).context is selected
                        assert session.run(asked).resolution.method == "measured"
                        assert (Path(selected.root) / "target.py").read_text() == "during use"
                        assert prepared._prepared_copy.lease.read_bytes() == lease
                    assert life._CURRENT.get() is None
                assert _thread(borrowed) == []
            assert session.calls == 2
            session.close()
        with prepared.use() as selected:
            assert (Path(selected.root) / "target.py").read_text() == "original"
        prepared.close()
    finally:
        if language == Native.language:
            lb.unregister(language)


def test_copied_context_and_contextvar_are_not_worker_admission(prepared):
    with prepared.use() as context:
        inherited = copy_context()
        def refused():
            with pytest.raises(OwnershipRefused):
                lb.create("python", context)
            with pytest.raises(OwnershipRefused, match="exact issued"):
                with prepared.worker(replace(context)):
                    pytest.fail("copied context acquired authority")
        assert _thread(lambda: inherited.run(refused)) == []
    prepared.close()


@pytest.mark.parametrize("route", ["tests", "sources", "mutation", "executor"])
def test_actual_execution_call_blocks_worker_admission(prepared, tmp_path, monkeypatch, route):
    from langadapter import execution
    from langadapter.sources import SourceResult

    with prepared.use() as context:
        adapter = lb.create("python", context)
        scope = context._prepared_copy_binding.scope

        def body():
            assert scope._operations > 0
            errors = _thread(lambda: _borrow_once(prepared, context))
            assert len(errors) == 1 and isinstance(errors[0], OwnershipRefused)

        monkeypatch.setattr(type(adapter), "execution_setup_issue", lambda *a: "")
        if route == "tests":
            request = SuiteRunRequest(_request(context.root, tmp_path).suite, timeout_s=1)
            def run(*args, **kwargs):
                body()
                return adapter.tests._suite_failure(request, lb.Outcome.COMPLETED, "controlled")
            monkeypatch.setattr(type(adapter), "run_suite", run)
            assert adapter.tests.run_suite(request).outcome is lb.Outcome.COMPLETED
        elif route == "sources":
            def validate(*args, **kwargs):
                body()
                return SourceResult("available", "def test_it(): pass", "controlled")
            monkeypatch.setattr(type(adapter), "validate_source", validate)
            assert adapter.sources.validate_source("def test_it(): pass").status.value == "completed"
        elif route == "mutation":
            def opened(*args, **kwargs):
                body()
                return Session()
            monkeypatch.setattr(type(adapter), "open_mutation_session", opened)
            session = adapter.mutations.open_session("cosmic_ray")
            session.close()
        else:
            def capture(*args, **kwargs):
                body()
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            monkeypatch.setattr(execution, "run_capture", capture)
            assert execution.LocalExecutor(context).run(["controlled"]).returncode == 0
        assert _thread(lambda: _borrow_once(prepared, context)) == []
    prepared.close()


def test_finished_use_revokes_before_lease_release(prepared, monkeypatch):
    with prepared.use() as context:
        old = context
        original = type(prepared._prepared_copy)._release
        def release(prepared_copy, record):
            errors = _thread(lambda: _borrow_once(prepared, old))
            assert len(errors) == 1 and isinstance(errors[0], OwnershipRefused)
            original(prepared_copy, record)
        monkeypatch.setattr(type(prepared._prepared_copy), "_release", release)
    monkeypatch.setattr(type(prepared._prepared_copy), "_release", original)
    prepared.close()


def test_worker_creation_does_not_acquire_and_entry_revalidates_generation(prepared):
    with prepared.use() as old:
        deferred = prepared.worker(old)
        assert lb.create("python", old).context is old
    with prepared.use():
        def refused():
            with pytest.raises(OwnershipRefused, match="exact issued"):
                with deferred:
                    pytest.fail("stale worker entered")
        assert _thread(refused) == []
    prepared.close()


def test_worker_cannot_replace_an_unrelated_active_scope(prepared):
    with prepared.use() as context:
        def refused():
            with life._Scope() as other:
                with pytest.raises(OwnershipRefused, match="Another execution scope"):
                    with prepared.worker(context):
                        pytest.fail("unrelated scope was replaced")
                assert life._CURRENT.get() is other
        assert _thread(refused) == []
    prepared.close()


def test_active_borrow_refuses_parent_competitor_and_close(prepared):
    entered, release = threading.Event(), threading.Event()
    errors = []
    with prepared.use() as context:
        def borrow():
            try:
                with prepared.worker(context):
                    entered.set()
                    assert release.wait(3)
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=borrow)
        worker.start()
        assert entered.wait(3)
        try:
            with pytest.raises(OwnershipRefused):
                lb.create("python", context)
            assert len(_thread(lambda: prepared.worker(context).__enter__())) == 1
            with pytest.raises(WorkspaceBusy):
                prepared.close()
        finally:
            release.set()
            worker.join(3)
        assert not worker.is_alive() and not errors
        assert lb.create("python", context).context is context
    prepared.close()


def test_operation_admission_refuses_borrow_before_native_body_finishes(prepared):
    with prepared.use() as context:
        scope = context._prepared_copy_binding.scope
        with scope.operation():
            assert len(_thread(lambda: prepared.worker(context).__enter__())) == 1
        assert _thread(lambda: _borrow_once(prepared, context)) == []
    prepared.close()


def _borrow_once(owner, context):
    with owner.worker(context):
        lb.create("python", context)


def test_explicit_persistent_command_owner_survives_borrows(prepared, monkeypatch):
    command = SimpleNamespace(returncode=0)
    monkeypatch.setattr(_process_tree, "_start", lambda *args, **kwargs: command)
    monkeypatch.setattr(_process_tree, "_dispose", lambda *args, **kwargs: True)
    with prepared.use() as context:
        session = Session()
        _process_tree.start(["controlled"])
        assert len(_thread(lambda: _borrow_once(prepared, context))) == 1
        session.register_owned_commands((command,))
        assert _thread(lambda: _borrow_once(prepared, context)) == []
        assert _process_tree.finish(command)
        session.close()
    prepared.close()


def test_python_group_retains_actual_native_worker_handles_between_borrows(prepared, tmp_path, monkeypatch):
    from langadapter.python import _cosmic_ray as native

    command = SimpleNamespace(returncode=0)
    starts, closes = [], []
    monkeypatch.setattr(_process_tree, "_start", lambda *a, **k: command)
    monkeypatch.setattr(_process_tree, "_dispose", lambda *a, **k: True)

    class Handle:
        use_http = True
        worker_urls = ["controlled"]
        def __enter__(self):
            starts.append(self)
            self._worker_procs = [_process_tree.start(["controlled"])]
            return self
        def __exit__(self, *args):
            assert _process_tree.finish(self._worker_procs[0])
            closes.append(self)

    class Engine:
        def __init__(self, *args, **kwargs):
            pass
        def session(self, **kwargs):
            return Handle()

    monkeypatch.setattr(native, "CosmicRayEngine", Engine)
    monkeypatch.setattr(native.MutationSession, "_validate_request", lambda self, request: request.validate())
    monkeypatch.setattr(native.MutationSession, "measure", lambda self, request:
                        MutationRun.failed(request.target, lb.Outcome.INFRASTRUCTURE_FAILURE, "controlled"))
    with prepared.use() as context:
        adapter = lb.create("python", context)
        monkeypatch.setattr(type(adapter), "execution_setup_issue", lambda *a: "")
        session = adapter.mutations.open_session("cosmic_ray", distributor="http")
        assert session.is_open and session.workers_started == 1
        request = _request(context.root, tmp_path)
        def observed():
            with prepared.worker(context):
                assert session.run(request).resolution.method == "measured"
        for _ in range(2):
            assert _thread(observed) == []
            assert session._session is starts[0] and not closes
        session.close()
        session.close()
        assert len(starts) == len(closes) == 1
    prepared.close()


def test_caught_borrower_uncertainty_retains_owner_until_actual_recovery(prepared, monkeypatch):
    command = SimpleNamespace(returncode=0)
    disposed = [False]
    monkeypatch.setattr(_process_tree, "_start", lambda *args, **kwargs: command)
    monkeypatch.setattr(_process_tree, "_dispose", lambda *args, **kwargs: disposed[0])
    with pytest.raises(OwnershipRefused):
        with prepared.use() as context:
            def failure():
                with prepared.worker(context):
                    _process_tree.start(["controlled"])
                    assert not _process_tree.finish(command)
            errors = _thread(failure)
            assert len(errors) == 1 and errors[0]._langadapter_prepared_project is prepared
            with pytest.raises(OwnershipRefused):
                lb.create("python", context)
    with pytest.raises(OwnershipRefused):
        prepared.close()
    assert Path(prepared._prepared_copy.owned.receipt).is_file()
    disposed[0] = True
    assert _process_tree.finish(command)
    prepared.close()


def test_body_primary_is_not_replaced_by_borrower_cleanup(prepared):
    primary = KeyboardInterrupt("original cancellation")
    session = None
    with pytest.raises(OwnershipRefused):
        with prepared.use() as context:
            def fail():
                nonlocal session
                with prepared.worker(context):
                    session = Session()
                    session.failed = True
                    with pytest.raises(MutationCleanupError):
                        session.close()
                    raise primary
            errors = _thread(fail)
            assert errors == [primary]
            assert primary._langadapter_scope_cleanup_error is not None
    session.failed = False
    session.close()
    prepared.close()


def test_parent_interruption_retains_active_worker_and_revokes_new_admission(prepared):
    entered, release = threading.Event(), threading.Event()
    errors = []
    worker = None
    with pytest.raises(KeyboardInterrupt) as caught:
        with prepared.use() as context:
            def borrow():
                try:
                    with prepared.worker(context):
                        entered.set()
                        assert release.wait(3)
                        with pytest.raises(OwnershipRefused):
                            lb.create("python", context)
                except BaseException as error:
                    errors.append(error)
            worker = threading.Thread(target=borrow)
            worker.start()
            assert entered.wait(3)
            raise KeyboardInterrupt("parent stopped")
    try:
        assert caught.value._langadapter_prepared_project is prepared
        with pytest.raises(OwnershipRefused):
            prepared.close()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    prepared.close()


def test_actual_start_refuses_after_parent_exit_even_on_remaining_borrower(prepared, monkeypatch):
    entered, released = threading.Event(), threading.Event()
    failures, starts = [], []
    monkeypatch.setattr(_process_tree, "_start", lambda *a, **k:
                        (starts.append(a), SimpleNamespace(returncode=0))[1])
    primary = KeyboardInterrupt("parent stopped before new native launch")
    with pytest.raises(KeyboardInterrupt) as caught:
        with prepared.use() as context:
            def worker():
                try:
                    with prepared.worker(context):
                        entered.set()
                        assert released.wait(3)
                        with pytest.raises(OwnershipRefused):
                            _process_tree.start(["refused"])
                except BaseException as error:
                    failures.append(error)
            thread = threading.Thread(target=worker)
            thread.start()
            assert entered.wait(3)
            raise primary
    try:
        assert caught.value is primary and Path(prepared._prepared_copy.owned.receipt).is_file()
    finally:
        released.set()
        thread.join(3)
    assert not thread.is_alive() and failures == [] and starts == []
    prepared.close()


def test_admitted_inflight_start_keeps_acquired_handle_after_parent_revocation(prepared, monkeypatch):
    entered, released = threading.Event(), threading.Event()
    handle = SimpleNamespace(returncode=0)
    failures, received = [], []
    def start(*args, **kwargs):
        entered.set()
        assert released.wait(3)
        return handle
    monkeypatch.setattr(_process_tree, "_start", start)
    monkeypatch.setattr(_process_tree, "_dispose", lambda *a, **k: True)
    primary = KeyboardInterrupt("parent stopped during acquisition")
    with pytest.raises(KeyboardInterrupt) as caught:
        with prepared.use() as context:
            def worker():
                try:
                    with prepared.worker(context):
                        received.append(_process_tree.start(["already admitted"]))
                except BaseException as error:
                    failures.append(error)
            thread = threading.Thread(target=worker)
            thread.start()
            assert entered.wait(3)
            raise primary
    released.set()
    thread.join(3)
    assert caught.value is primary and not thread.is_alive() and received == [handle]
    assert len(failures) == 1 and isinstance(failures[0], OwnershipRefused)
    with pytest.raises(OwnershipRefused) as held:
        prepared.close()
    assert held.value._langadapter_resources == (handle,)
    assert _process_tree.finish(handle)
    prepared.close()


def test_stale_inherited_scope_refuses_launch_but_explicit_recovery_admits(monkeypatch):
    handle = SimpleNamespace(returncode=0)
    starts = []
    monkeypatch.setattr(_process_tree, "_start", lambda *a, **k: (starts.append(a), handle)[1])
    monkeypatch.setattr(_process_tree, "_dispose", lambda *a, **k: True)
    scope = life._Scope()
    with scope:
        inherited = copy_context()
        assert _process_tree.start(["ordinary"]) is handle
        assert _process_tree.finish(handle)
    with pytest.raises(OwnershipRefused):
        inherited.run(_process_tree.start, ["stale"])
    assert len(starts) == 1
    with scope.operation(recovery=True):
        assert _process_tree.start(["explicit recovery"]) is handle
        assert _process_tree.finish(handle)
    scope.require_quiescent()
    assert len(starts) == 2


def test_inflight_handle_survives_revocation_before_parent_token_is_reset(monkeypatch):
    handle = SimpleNamespace(returncode=0)
    scope = life._Scope()
    def started(*args, **kwargs):
        with scope._lock:
            scope._revoked = True
        return handle
    monkeypatch.setattr(_process_tree, "_start", started)
    monkeypatch.setattr(_process_tree, "_dispose", lambda *a, **k: True)
    with pytest.raises(OwnershipRefused) as held:
        with scope:
            assert _process_tree.start(["admitted before revocation"]) is handle
    assert held.value._langadapter_resources == (handle,)
    assert _process_tree.finish(handle)
    scope.require_quiescent()


def test_a_callers_error_in_a_worker_leaves_the_owner_usable(prepared, monkeypatch):
    """A caller's own error inside a worker leaves the prepared owner
    untagged, so the next borrow goes ahead; a borrow whose own cleanup
    fails still marks it."""
    with prepared.use() as context:
        def refused_then_measured():
            with pytest.raises(ValueError) as caught:
                with prepared.worker(context):
                    raise ValueError("the unmutated suite did not pass")
            assert not hasattr(caught.value, "_langadapter_prepared_project")
            assert not hasattr(caught.value, "_langadapter_execution_scope")
            with prepared.worker(context) as selected:
                assert selected is context
        assert _thread(refused_then_measured) == []

        scope = prepared._binding.scope
        def unsettled():
            raise RuntimeError("a command of the borrow is still running")
        def left_running():
            with pytest.raises(ValueError) as caught:
                with prepared.worker(context):
                    monkeypatch.setattr(scope, "require_idle", unsettled)
                    raise ValueError("refused")
            assert caught.value._langadapter_execution_scope is scope
        assert _thread(left_running) == []


class Stopped(Session):
    """A run whose workers died, and a stop that arrives while the session
    shuts them down: the first close is interrupted, the retry succeeds."""

    def __init__(self, primary):
        super().__init__()
        self.primary = primary
        self.interrupts = 1

    def measure(self, request):
        raise self.primary

    def release(self):
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt


@pytest.mark.parametrize("primary", [RuntimeError("the workers stopped"), KeyboardInterrupt()])
def test_an_interrupted_session_cleanup_stays_an_interrupt_and_the_use_recovers(
        prepared, tmp_path, primary):
    """A stop during a session's shutdown reaches the caller as itself,
    naming the session to retry. Until that retry succeeds new work is
    refused, and afterwards it goes ahead."""
    with prepared.use() as context:
        asked = _request(context.root, tmp_path)
        session = Stopped(primary)

        def measured_and_stopped():
            with prepared.worker(context):
                session.run(asked)
        errors = _thread(measured_and_stopped)
        assert len(errors) == 1 and type(errors[0]) is KeyboardInterrupt
        assert errors[0].cleanup_handle is session and not session._closed
        # The scope refuses the session's own run first, naming the session to close.
        with pytest.raises(OwnershipRefused, match="cleanup failed") as refused:
            session.run(asked)
        assert refused.value.cleanup_handle is session

        def borrowed():
            with prepared.worker(context):
                pass
        assert [type(error) for error in _thread(borrowed)] == [OwnershipRefused]
        session.close()

        def measured():
            with prepared.worker(context):
                fresh = Session()
                assert fresh.run(asked).reason == "controlled result"
                fresh.close()
        assert _thread(measured) == []
    prepared.close()


def test_an_interrupt_ending_a_session_block_is_not_replaced_by_its_cleanup_failure():
    session = Session()
    session.failed = True
    stop = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught:
        with session:
            raise stop
    assert caught.value is stop and stop.cleanup_handle is session
    session.failed = False
    session.close()
    assert session._closed


def test_a_use_revoked_by_an_interrupted_launch_says_so_on_the_next_request(prepared, monkeypatch):
    """An interrupt inside a launch may leave a process it never returned,
    so the use is revoked: the next request's refusal names the unfinished
    launch and carries the scope."""
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("launch interrupted")

    monkeypatch.setattr(_process_tree, "_start", interrupted)
    with pytest.raises(OwnershipRefused):
        with prepared.use() as context:
            def launched():
                with prepared.worker(context):
                    try:
                        _process_tree.start(["controlled"])
                    except KeyboardInterrupt:
                        pass
            assert [type(error) for error in _thread(launched)] == [OwnershipRefused]
            scope = prepared._binding.scope
            with pytest.raises(OwnershipRefused) as refused:
                scope.require_active()
            assert refused.value._langadapter_execution_scope is scope
            assert "unfinished native launch" in str(refused.value)
