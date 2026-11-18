"""Prepared copy leases depend on lifecycle proofs, not returned native outcomes."""
import sys
import threading
from types import SimpleNamespace

import pytest

from langadapter import _execution_scope as life, _process_tree
from langadapter._owned_directory import OwnershipRefused
from langadapter._prepared_copy import PreparedCopy, WorkspaceBusy
from langadapter.context import Interpreter, ProjectContext
from langadapter.mutation import MutationCleanupError, MutationSession
from langadapter.outcomes import Outcome
from langadapter.registry import create, register, unregister
from langadapter.adapter import LangAdapter
from langadapter.runs import RunCounts, SuiteRun, SuiteRunRequest
from langadapter.suites import Suite


@pytest.fixture(autouse=True)
def no_native(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Execution-scope tests start no native processes")

    monkeypatch.setattr(_process_tree.subprocess, "Popen", forbidden)
    monkeypatch.setattr(_process_tree.subprocess, "run", forbidden)


@pytest.fixture
def prepared_copy(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "input").write_text("original", encoding="ascii")
    zone = tmp_path / "zone"
    zone.mkdir()
    return PreparedCopy.create(str(source), parent=str(zone), receipt_parent=str(tmp_path))


@pytest.fixture
def command(monkeypatch):
    handle = SimpleNamespace(returncode=0)
    disposed = [True]
    monkeypatch.setattr(_process_tree, "_start", lambda *a, **kw: handle)
    monkeypatch.setattr(_process_tree, "_dispose", lambda *a, **kw: disposed[0])
    return handle, disposed


def test_owned_command_must_finish_before_normal_scope_exit(command):
    handle, _ = command
    scope = life._Scope()
    with pytest.raises(OwnershipRefused, match="owned command") as caught:
        with scope:
            assert _process_tree.start(["native"]) is handle
    assert caught.value._langadapter_resources == (handle,)
    assert _process_tree.finish(handle) is True
    scope.require_quiescent()  # Same acquired handle can recover outside its original scope.


def test_successful_disposal_allows_reuse_without_scoring_or_pid_inference(prepared_copy, command):
    handle, _ = command
    with prepared_copy.use() as root:
        (root / "input").unlink()
        _process_tree.start(["native"], cwd=str(root))
        assert _process_tree.finish(handle) is True
    assert not prepared_copy.lease.exists()
    with prepared_copy.use() as root:
        assert (root / "input").read_text() == "original"


def test_caught_command_failure_cannot_release_prepared_copy_as_success(prepared_copy, command):
    handle, disposed = command
    disposed[0] = False
    with pytest.raises(OwnershipRefused) as caught:
        with prepared_copy.use() as root:
            _process_tree.start(["native"], cwd=str(root))
            assert _process_tree.finish(handle) is False
            # A native caller may catch this failure and return a failed observation.
    assert caught.value._langadapter_prepared_copy is prepared_copy
    assert prepared_copy.lease.is_file() and prepared_copy.root.is_dir()
    prepared_copy.owned.verify()
    with pytest.raises(WorkspaceBusy):
        with prepared_copy.use():
            pytest.fail("an uncertain lease must refuse a new use")


@pytest.mark.parametrize("error", [KeyboardInterrupt("launch interrupted"), OSError("no acquired handle")])
def test_interrupted_launch_without_a_handle_retains_uncertain_scope(prepared_copy, monkeypatch, error):
    def interrupted(*args, **kwargs):
        raise error

    monkeypatch.setattr(_process_tree, "_start", interrupted)
    with pytest.raises(type(error)) as caught:
        with prepared_copy.use():
            _process_tree.start(["native"])
    assert caught.value is error and prepared_copy.lease.is_file()
    assert life._CURRENT.get() is None
    with pytest.raises(OwnershipRefused, match="unfinished native launch"):
        error._langadapter_execution_scope.require_quiescent()


def test_failed_start_with_an_acquired_handle_keeps_that_owner(command, monkeypatch):
    handle, disposed = command
    disposed[0] = False
    error = RuntimeError("start failed after acquisition")
    error._langadapter_command = handle

    def interrupted(*args, **kwargs):
        raise error

    monkeypatch.setattr(_process_tree, "_start", interrupted)
    scope = life._Scope()
    with pytest.raises(RuntimeError) as caught:
        with scope:
            _process_tree.start(["native"])
    assert caught.value is error
    with pytest.raises(OwnershipRefused) as unresolved:
        scope.require_quiescent()
    assert unresolved.value._langadapter_resources == (handle,)
    disposed[0] = True
    assert _process_tree.finish(handle) is True
    scope.require_quiescent()


def test_reservation_does_not_poison_an_already_confirmed_failed_start(command, monkeypatch):
    handle, _ = command
    handle._langadapter_command_disposed = True
    error = RuntimeError("native admission refused, disposal confirmed")
    error._langadapter_command = handle

    def refused(*args, **kwargs):
        raise error

    monkeypatch.setattr(_process_tree, "_start", refused)
    scope = life._Scope()
    with pytest.raises(RuntimeError):
        with scope:
            _process_tree.start(["native"])
    scope.require_quiescent()


def test_generic_handle_closed_flag_is_not_owned_disposal_proof(command, monkeypatch):
    handle, _ = command
    handle._closed = True
    error = RuntimeError("wrapper stream closed, native ownership still unresolved")
    error._langadapter_command = handle

    def refused(*args, **kwargs):
        raise error

    monkeypatch.setattr(_process_tree, "_start", refused)
    scope = life._Scope()
    with pytest.raises(RuntimeError):
        with scope:
            _process_tree.start(["native"])
    with pytest.raises(OwnershipRefused) as unresolved:
        scope.require_quiescent()
    assert unresolved.value._langadapter_resources == (handle,)


@pytest.mark.parametrize("attribute", ["_langadapter_command", "_langadapter_linux_command"])
@pytest.mark.parametrize("disposed", [False, True])
def test_outcome_conversion_requires_command_owner_proof(attribute, disposed):
    error = OSError("native failed")
    owner = SimpleNamespace(_closed=True, _langadapter_command_disposed=disposed)
    setattr(error, attribute, owner)
    if disposed:
        assert life.reraise_unresolved(error) is None
    else:
        with pytest.raises(OSError) as caught:
            life.reraise_unresolved(error)
        assert caught.value is error and getattr(error, attribute) is owner


@pytest.mark.parametrize("closed", [False, True])
def test_outcome_conversion_uses_actual_linux_command_close(closed):
    from langadapter._linux_command import _Command

    owner = object.__new__(_Command)
    owner._closed = closed
    error = OSError("container failed")
    error._langadapter_command = owner
    if closed:
        assert life.reraise_unresolved(error) is None
    else:
        with pytest.raises(OSError) as caught:
            life.reraise_unresolved(error)
        assert caught.value is error


def test_outcome_conversion_preserves_unfinished_launch_but_not_open_session():
    scope = life._Scope()
    error = OSError("launch interrupted")
    error._langadapter_execution_scope = scope
    session = scope.track(SimpleNamespace(), "open mutation session")
    assert life.reraise_unresolved(error) is None
    ticket = scope.track(life._Ticket(), "unfinished native launch")
    with pytest.raises(OSError) as caught:
        life.reraise_unresolved(error)
    assert caught.value is error
    scope.completed(ticket)
    assert life.reraise_unresolved(error) is None
    scope.completed(session)
    scope.require_quiescent()


class Session(MutationSession):
    def __init__(self):
        super().__init__()
        self.failed = False

    def measure(self, request):
        pytest.fail("No native mutation measurement requested")

    def release(self):
        if self.failed:
            raise OSError("native close refused")


def test_open_returned_session_blocks_prepared_copy_even_without_workers(prepared_copy):
    session = None
    with pytest.raises(OwnershipRefused, match="mutation session"):
        with prepared_copy.use():
            session = Session()
    assert session.is_open and prepared_copy.lease.is_file()
    session.close()
    assert not session.is_open
    session._langadapter_execution_scope.require_quiescent()
    # The old uncertain lease is not reclaimed from PID/resource observations alone.
    with pytest.raises(WorkspaceBusy):
        with prepared_copy.use():
            pytest.fail("an uncertain lease must refuse a new use")


def test_successful_session_close_preserves_normal_prepared_copy_restoration(prepared_copy):
    with prepared_copy.use() as root:
        with Session() as session:
            assert session.is_open
            (root / "input").write_text("changed", encoding="ascii")
    with prepared_copy.use() as root:
        assert (root / "input").read_text() == "original"


def test_failed_session_close_preserves_original_error_and_same_retry_handle(prepared_copy):
    session = None
    with pytest.raises(MutationCleanupError) as caught:
        with prepared_copy.use():
            session = Session()
            session.failed = True
            session.close()
    assert caught.value.cleanup_handle is session and session.is_open
    assert caught.value._langadapter_prepared_copy is prepared_copy and prepared_copy.lease.is_file()
    session.failed = False
    session.close()
    session._langadapter_execution_scope.require_quiescent()


def test_scopes_are_operation_local_and_completion_may_come_from_another_thread():
    resource = SimpleNamespace()
    scope = life._Scope()
    failures = []

    def complete_elsewhere():
        try:
            assert life._CURRENT.get() is None
            life.completed(resource)
        except BaseException as error:
            failures.append(error)

    with scope:
        life.track(resource, "controlled owner")
        thread = threading.Thread(target=complete_elsewhere)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive() and not failures
    assert life._CURRENT.get() is None


def test_registering_an_independent_adapter_does_not_need_a_language_switch(prepared_copy, tmp_path, command):
    handle, disposed = command
    disposed[0] = False

    class Native(LangAdapter):
        language = "workspace-fixture"

        def run_suite(self, request):
            _process_tree.start(["fixture-native"], cwd=self.context.root)
            _process_tree.finish(handle)
            return SuiteRun(Outcome.INFRASTRUCTURE_FAILURE, "controlled failure", RunCounts(), (),
                            (), None, "", None, self.tests._identity(request))

    register(Native.language, [".scope-fixture"], Native)
    try:
        with pytest.raises(OwnershipRefused):
            with prepared_copy.use() as root:
                adapter = create(Native.language, ProjectContext(str(root), interpreter=Interpreter(
                    path=sys.executable)))
                (tmp_path / "declared.scope-fixture").write_text("declared test input", encoding="ascii")
                suite = Suite(str(tmp_path), ("declared.scope-fixture",))
                result = adapter.tests.run_suite(SuiteRunRequest(suite, timeout_s=1))
                assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
        assert prepared_copy.lease.is_file()
    finally:
        unregister(Native.language)


class _Disposable:
    """A command owner: disposed, and with its control closed, as given."""

    def __init__(self, disposed=False, closed=False):
        self._langadapter_command_disposed = disposed
        self._closed = closed


def _closed_linux_command():
    """A Linux command whose control closed, without the disposed marker."""
    from langadapter._linux_command import _Command

    command = _Command.__new__(_Command)
    command._closed, command._langadapter_command_disposed = True, False
    return command


_CLOSED_LINUX, _RUNNING = _closed_linux_command(), _Disposable()


def test_a_partial_preparation_carries_its_owner(tmp_path, monkeypatch):
    """A preparation that fails after it allocated the copy's directory, before
    the prepared copy exists, raises with its partial owner, whose close()
    removes the directory."""
    from langadapter import _prepared_copy, preparation, prepare_project, recovery_owners

    source = tmp_path / "source"
    source.mkdir()
    (source / "subject.py").write_text("value = 1\n", encoding="utf-8")
    created = _prepared_copy.OwnedDirectory.create.__func__

    def allocate(cls, **options):
        if options.get("prefix") == "langadapter_inputs_":
            raise OSError("snapshot allocation failed")
        return created(cls, **options)

    monkeypatch.setattr(_prepared_copy.OwnedDirectory, "create", classmethod(allocate))
    with pytest.raises(OSError) as failed:
        prepare_project(ProjectContext(str(source)), parent=str(tmp_path),
                        receipt_parent=str(tmp_path))
    (owned,) = failed.value._langadapter_owned_directories
    (owner,) = recovery_owners(failed.value)
    assert isinstance(owner, preparation._PartialPreparation) and owned.root.is_dir()
    owner.close()
    assert not owned.root.exists() and not owned.receipt.exists()


def test_a_refused_request_carries_no_owner(tmp_path):
    from langadapter import InvalidRequest, prepare_project, recovery_owners

    with pytest.raises(InvalidRequest) as refused:
        prepare_project(ProjectContext(str(tmp_path)), timeout_s=-1)
    assert recovery_owners(refused.value) == ()


@pytest.mark.parametrize("carriers, owned", [
    ({"_langadapter_prepared_project": "project"}, ("project",)),
    ({"_langadapter_prepared_copy": "copy"}, ("copy",)),
    ({"_langadapter_scope_cleanup_error": "cleanup"}, ("cleanup",)),
    ({"_langadapter_command": "command", "_langadapter_execution_scope": "scope"},
     ("command", "scope")),
    ({"_langadapter_execution_scope": "scope"}, ("scope",)),
    # A disposed command, or a closed Linux command, left nothing: its scope goes too.
    ({"_langadapter_command": _Disposable(disposed=True), "_langadapter_execution_scope": "scope"},
     ()),
    ({"_langadapter_linux_command": _CLOSED_LINUX}, ()),
    # A closed Linux command beside a command slot whose command may still
    # run leaves both to recover.
    ({"_langadapter_linux_command": _CLOSED_LINUX, "_langadapter_command": _RUNNING},
     (_CLOSED_LINUX, _RUNNING)),
    # One command in both slots is one owner.
    ({"_langadapter_linux_command": _RUNNING, "_langadapter_command": _RUNNING}, (_RUNNING,)),
    # Carriers that are no owner of their own.
    ({"_langadapter_owned_directories": ("dir",), "_langadapter_prepared_copy_lease": {"pid": 1},
      "_langadapter_resources": ("resource",)}, ()),
])
def test_the_recovery_owners_of_each_carrier_combination(carriers, owned):
    from langadapter import recovery_owners

    error = OSError("a failed operation")
    for name, value in carriers.items():
        setattr(error, name, value)
    assert recovery_owners(error) == owned


def test_a_closed_command_in_the_command_slot_stays_an_owner():
    """A closed control counts only for a Linux command, so a closed command of
    another kind stays an owner."""
    from langadapter import recovery_owners

    error = OSError("a failed operation")
    command = _Disposable(closed=True)
    error._langadapter_command = command
    assert recovery_owners(error) == (command,)
