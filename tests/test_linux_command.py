from __future__ import annotations

import io
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from langadapter import _linux_command as command
from langadapter._linux_job import ControlError, SupervisionUnavailable

_REAL_CREATE = command._Control.create
#: The harness replaces threading.Thread. A real stalled heartbeat needs it.
_REAL_THREAD = threading.Thread


@pytest.fixture
def harness(monkeypatch, tmp_path):
    calls, clock = [], [0.0]
    record = {"state": "RUNNING", "command_pid": 84, "returncode": None, "reason": None}
    owned = SimpleNamespace(root=tmp_path, receipt=tmp_path / "receipt.json", token="a" * 32,
                            remove=lambda: calls.append(("remove",)))

    class Control:
        def __init__(self):
            self.owned = owned

        def pulse(self):
            calls.append(("pulse",))

        def cancel(self):
            calls.append(("cancel",))

        def observe(self, **kwargs):
            calls.append(("observe", kwargs))
            return dict(record)

        def completed(self, **kwargs):
            calls.append(("completed", kwargs))
            return dict(record)

    class Thread:
        def __init__(self, **kwargs):
            self.target = kwargs["target"]
            self.alive = False

        def start(self):
            calls.append(("thread_start",))
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, **kwargs):
            calls.append(("thread_join", kwargs))
            self.alive = False

    class Helper:
        pid, returncode = 42, None

        def __init__(self):
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

        def poll(self):
            calls.append(("poll",))
            return self.returncode

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if self.returncode is None:
                raise subprocess.TimeoutExpired("helper", timeout)
            return self.returncode

        def communicate(self, input=None, timeout=None):
            calls.append(("communicate", input, timeout))
            if self.returncode is None:
                clock[0] += timeout
                raise subprocess.TimeoutExpired("helper", timeout, output=b"partial", stderr=b"")
            return b"native out", b"native err"

    control, helper = Control(), Helper()

    def create(*args, **kwargs):
        calls.append(("create", args, kwargs))
        return control

    def launch(*args, **kwargs):
        calls.append(("launch", args, kwargs))
        return helper

    monkeypatch.setattr(command, "_supported_host", lambda: True)
    monkeypatch.setattr(command._Control, "create", create)
    monkeypatch.setattr(command.subprocess, "Popen", launch)
    monkeypatch.setattr(command.threading, "Thread", Thread)
    monkeypatch.setattr(command.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(command.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    return SimpleNamespace(calls=calls, clock=clock, record=record, helper=helper,
                           control=control, owned=owned)


def start(harness, **kwargs):
    return command._Command.start(["selected-native", "argument"], **kwargs)


def complete(harness, code=7):
    harness.record.update(state="COMPLETE", returncode=code)
    harness.helper.returncode = 0


def test_selected_native_arguments_environment_and_streams_are_unchanged(harness, tmp_path):
    env = {"PATH": "selected", "native-setting": "exact"}
    owner = start(harness, cwd=tmp_path, env=env, stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, text=True, encoding="utf-8")
    creation = next(call for call in harness.calls if call[0] == "create")
    assert creation[1] == (["selected-native", "argument"],)
    assert creation[2]["cwd"] == str(tmp_path)
    launch = next(call for call in harness.calls if call[0] == "launch")
    argv = launch[1][0]
    # Without site, a .pth that starts a process does not run in the helper.
    assert argv[:3] == [command.sys.executable, "-I", "-S"]
    assert argv[3].endswith("_linux_job.py")
    assert argv[4:] == [str(tmp_path), harness.owned.token]
    assert launch[2] == {"start_new_session": True, "cwd": tmp_path, "env": env,
                         "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                         "text": True, "encoding": "utf-8"}
    assert owner.pid == 84 and owner.helper.pid == 42 and owner.returncode is None
    assert owner.stdout is harness.helper.stdout and owner.stderr is harness.helper.stderr
    complete(harness)
    owner.close()


@pytest.mark.parametrize("code", [0, 7, -11])
def test_acquired_helper_wait_and_native_exit_are_not_interchangeable(harness, code):
    complete(harness, code)
    owner = start(harness)
    assert owner.poll() == code and owner.wait(timeout=0) == code
    assert owner.returncode == code
    assert owner._stopped.is_set() and not owner._heartbeat.is_alive()
    waits = [call for call in harness.calls if call[0] == "wait"]
    assert waits and all(call[1] == 0 for call in waits)
    assert all(call[1] == {"helper_returncode": 0, "helper_pid": 42}
               for call in harness.calls if call[0] == "completed")
    owner.close()


def test_terminal_marker_without_helper_exit_is_not_completion(harness):
    harness.record.update(state="COMPLETE", returncode=0)
    owner = start(harness)
    assert owner.poll() == 0 and owner.returncode == 0
    assert owner._record is None and owner._heartbeat.is_alive() and not owner._closed
    assert not any(call[0] == "completed" for call in harness.calls)
    complete(harness, 0)
    owner.close()


def test_native_exit_is_distinct_from_helper_and_descendant_completion(harness):
    owner = start(harness)
    harness.record.update(state="EXITED", returncode=7)
    assert owner.poll() == 7 and owner.wait() == 7
    assert owner.helper.poll() is None and owner._record is None
    assert owner._heartbeat.is_alive() and not owner._closed
    complete(harness)
    owner.close()
    assert owner._record["state"] == "COMPLETE" and owner._closed


def test_after_native_exit_disposal_does_not_consume_the_native_timeout(harness, monkeypatch):
    owner = start(harness, cleanup=1)
    calls = []

    def communicate(input=None, timeout=None):
        calls.append(input)
        harness.clock[0] += timeout
        harness.record.update(state="EXITED", returncode=7)
        if harness.clock[0] < .5:
            raise subprocess.TimeoutExpired("helper", timeout, output=b"partial")
        complete(harness)
        return b"native out", b"native err"

    monkeypatch.setattr(harness.helper, "communicate", communicate)
    assert owner.communicate(input=b"once", timeout=.2) == (b"native out", b"native err")
    assert owner.returncode == 7 and calls[0] == b"once"
    assert len(calls) > 2 and all(value is None for value in calls[1:])
    assert harness.clock[0] > .2
    owner.close()


def test_after_native_exit_missing_disposal_is_infrastructure_not_native_timeout(harness):
    owner = start(harness, cleanup=.2)
    harness.record.update(state="EXITED", returncode=7)
    with pytest.raises(ControlError, match="output disposal did not finish") as caught:
        owner.communicate(timeout=.1)
    assert caught.value.output == b"partial" and caught.value.stderr == b""
    assert owner.returncode == 7 and owner._langadapter_output_unconfirmed
    assert not harness.helper.stdout.closed
    complete(harness)
    owner._langadapter_output_unconfirmed = False
    owner.close()


def test_previously_observed_native_exit_keeps_the_explicit_output_drain_bound(harness):
    owner = start(harness)
    harness.record.update(state="EXITED", returncode=7)
    assert owner.poll() == 7
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        owner.communicate(timeout=.1)
    assert caught.value.timeout == .1 and caught.value.output == b"partial"
    assert caught.value.stderr == b"" and owner.returncode == 7
    complete(harness)
    assert owner.communicate(timeout=.1) == (b"native out", b"native err")
    owner.close()


def test_final_report_cannot_replace_an_observed_native_exit(harness):
    owner = start(harness)
    harness.record.update(state="EXITED", returncode=7)
    assert owner.poll() == 7
    complete(harness, 8)
    with pytest.raises(ControlError, match="exit changed"):
        owner.wait()
    with pytest.raises(ControlError, match="exit changed"):
        owner.close()
    assert owner.returncode == 7 and not harness.helper.stdout.closed
    complete(harness, 7)
    owner.close()


def test_poll_failure_retains_the_actual_handle_and_report(harness):
    owner = start(harness)
    harness.record.update(state="FAILED", error={"message": "child accounting refused"})
    with pytest.raises(ControlError, match="child accounting refused") as caught:
        owner.poll()
    assert caught.value._langadapter_linux_command is owner
    assert caught.value._langadapter_supervisor_report["state"] == "FAILED"
    assert owner.returncode is None and not harness.helper.stdout.closed
    complete(harness)
    owner.close()


def test_a_command_that_finished_between_two_reads_is_admitted(harness, monkeypatch):
    """A command that was admitted and finished, with its helper exited,
    between the start's last health read (still showing no command) and its
    check of the helper counts as admitted."""
    harness.record.update(state="READY", command_pid=None)
    harness.helper.returncode = 0
    finished = dict(harness.record, state="COMPLETE", command_pid=84, returncode=0)
    monkeypatch.setattr(harness.control, "completed", lambda **kwargs: dict(finished))
    owner = start(harness)
    assert owner.pid == 84 and owner.returncode == 0
    owner.close()


@pytest.mark.parametrize("kind,number,name", [
    ("FileNotFoundError", 2, "selected-native"), ("PermissionError", 13, "selected-native"),
    ("FileNotFoundError", 2, "/missing/directory"), ("NotADirectoryError", 20, "/missing/directory")])
def test_a_command_that_cannot_start_is_an_ordinary_start_failure(harness, kind, number, name):
    """The supervisor reports a program or working directory that cannot be used
    only after confirming nothing it owns remains: the launch raises the start
    failure a local launch raises, and the owner is closed, not retained."""
    harness.record.update(state="FAILED", command_pid=None, reason="start failed",
                          error={"type": kind, "message": "[Errno] start failed", "errno": number,
                                 "filename": name})
    harness.helper.returncode = 1
    with pytest.raises(OSError) as caught:
        start(harness, cwd="/missing/directory")
    assert type(caught.value).__name__ == kind and caught.value.errno == number
    assert caught.value.filename == name
    owner = caught.value._langadapter_linux_command
    assert owner._closed and harness.helper.stdout.closed


def test_a_supervisor_that_refuses_at_its_preflight_leaves_no_owner_behind(harness):
    """A helper interpreter older than 3.10 refuses at its preflight, before
    creating any process, and leaves no unresolved owner behind."""
    harness.record.update(state="FAILED", command_pid=None, reason="refused", quiescent=True,
                          error={"type": "SupervisionUnavailable",
                                 "message": "Supervision requires Linux and Python 3.10+",
                                 "errno": None, "filename": None})
    harness.helper.returncode = 1
    with pytest.raises(SupervisionUnavailable, match="Python 3.10") as caught:
        start(harness)
    assert caught.value._langadapter_linux_command._closed


def test_a_preflight_refusal_with_children_keeps_the_owner(harness):
    """A preflight refusal because the helper's interpreter started a child
    (a .pth hook) keeps the owner while the child runs."""
    harness.record.update(state="FAILED", command_pid=None, reason="refused", quiescent=False,
                          error={"type": "SupervisionUnavailable",
                                 "message": "Supervisor must start without children",
                                 "errno": None, "filename": None})
    harness.helper.returncode = 1
    with pytest.raises(SupervisionUnavailable, match="without children") as caught:
        start(harness)
    assert not caught.value._langadapter_linux_command._closed


@pytest.mark.parametrize("changes", [
    {"filename": "/control/lease.json"}, {"type": "ValueError"}, {"filename": None}])
def test_a_supervisor_failure_that_is_not_the_commands_start_stays_retained(harness, changes):
    error = {"type": "FileNotFoundError", "message": "control refused", "errno": 2,
             "filename": "selected-native"} | changes
    harness.record.update(state="FAILED", command_pid=None, reason="refused", error=error)
    harness.helper.returncode = 1
    with pytest.raises(ControlError, match="control refused") as caught:
        start(harness)
    assert not caught.value._langadapter_linux_command._closed


@pytest.mark.parametrize("reason", ["host control lease expired", "cancelled before native start"])
def test_unrequested_or_prelaunch_cancellation_is_not_a_native_result(harness, reason):
    harness.record.update(state="CANCELLED", reason=reason,
                          command_pid=None if "before" in reason else 84,
                          returncode=None if "before" in reason else -15)
    harness.helper.returncode = 0
    with pytest.raises(ControlError) as caught:
        start(harness)
    assert caught.value._langadapter_control is harness.owned
    assert caught.value._langadapter_linux_command.returncode is None
    # Its terminal report confirmed that nothing it owned remains: the
    # command failed, but its owner is resolved.
    assert caught.value._langadapter_linux_command._closed


def test_a_lease_expiry_while_running_fails_the_command_and_resolves_its_owner(harness):
    """A lease expiry fails only that command and resolves its owner, so the
    prepared copy does not refuse the whole measurement."""
    owner = start(harness)
    harness.record.update(state="CANCELLED", reason="host control lease expired",
                          returncode=-15)
    harness.helper.returncode = 0
    with pytest.raises(ControlError, match="did not complete: host control lease expired"):
        owner.close()
    assert owner._closed and owner.returncode is None
    assert owner.close() is True


def test_a_busy_writer_skips_a_beat_instead_of_ending_the_heartbeat(harness, monkeypatch):
    """A caller's control write held through a storage stall skips a beat
    and does not end the heartbeat for good, which would let the lease
    expire."""
    owner = start(harness)
    waits = iter([False, True])
    monkeypatch.setattr(owner._stopped, "wait", lambda seconds: next(waits))
    assert owner._lock.acquire(timeout=1)
    try:
        owner._beat()
    finally:
        owner._lock.release()
    assert owner._heartbeat_error is None
    assert ("pulse",) not in harness.calls


def test_a_lease_expiry_names_this_processs_longest_pause_between_renewals(harness, monkeypatch):
    """A lease expiry says how long this process went without renewing and
    how much of its CPU it spent meanwhile, and its slowest renewal: a
    pause with about as much CPU means a busy thread held the interpreter,
    one with almost none that the process did not run, a slow renewal that
    storage stalled."""
    owner = start(harness)
    pauses = iter([1.0, 63.0, None])

    def wait(seconds):
        pause = next(pauses)
        if pause is None:
            return True
        harness.clock[0] += pause
        return False

    monkeypatch.setattr(owner._stopped, "wait", wait)
    owner._beat()
    harness.record.update(state="CANCELLED", reason="host control lease expired",
                          returncode=-15)
    harness.helper.returncode = 0
    with pytest.raises(ControlError) as caught:
        owner.poll()
    message = str(caught.value)
    assert message.startswith("Native execution did not complete: host control lease expired")
    assert "longest pause between lease renewals 63.0 s" in message
    assert "of its CPU" in message and "slowest renewal 0.0 s" in message


def test_disposal_of_a_command_whose_lease_expired_is_confirmed(harness):
    """Disposing a command whose lease expiry was already raised is
    confirmed, not a second failure."""
    from langadapter import _process_tree

    owner = start(harness)
    harness.record.update(state="CANCELLED", reason="host control lease expired",
                          returncode=-15)
    harness.helper.returncode = 0
    with pytest.raises(ControlError, match="lease expired"):
        owner.poll()
    assert _process_tree.finish(owner) is True
    assert owner._closed and owner._langadapter_command_disposed is True


def test_a_lease_expiry_the_body_never_observed_is_raised_at_exit(harness, monkeypatch):
    """A lease expiry that a command context's body never observed (it only
    read the streams, never waiting) is raised when the context exits."""
    from langadapter import _process_tree

    monkeypatch.setattr(_process_tree, "_start", lambda args, **options: start(harness))
    with pytest.raises(ControlError, match="lease expired"):
        with _process_tree.command(["native"]) as proc:
            harness.record.update(state="CANCELLED", reason="host control lease expired",
                                  returncode=-15)
            harness.helper.returncode = 0
    assert proc._closed


def test_setup_bound_cancels_and_retains_without_starting_another_helper(harness, monkeypatch):
    harness.record.update(state="READY", command_pid=None)

    def cancel():
        harness.calls.append(("cancel",))
        harness.record.update(state="CANCELLED", reason="cancelled before native start")
        harness.helper.returncode = 0

    monkeypatch.setattr(harness.control, "cancel", cancel)
    with pytest.raises(ControlError, match="setup time limit") as caught:
        start(harness, startup=0.3)
    assert len([call for call in harness.calls if call[0] == "launch"]) == 1
    assert caught.value._langadapter_linux_command.helper is harness.helper
    assert any(call == ("wait", 30.0) for call in harness.calls)


def test_native_timeout_keeps_partial_bytes_and_sends_input_only_once(harness):
    owner = start(harness)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        owner.communicate(input=b"native input", timeout=0.25)
    assert caught.value.cmd == ["selected-native", "argument"]
    assert caught.value.timeout == 0.25
    assert caught.value.output == b"partial" and caught.value.stderr == b""
    communications = [call for call in harness.calls if call[0] == "communicate"]
    assert [call[1] for call in communications] == [b"native input", None, None]
    assert not any(call[0] == "cancel" for call in harness.calls)
    complete(harness)
    assert owner.communicate() == (b"native out", b"native err")
    owner.close()


def test_wait_timeout_does_not_add_a_native_execution_budget(harness, monkeypatch):
    owner = start(harness)

    def wait(timeout=None):
        harness.clock[0] += timeout
        raise subprocess.TimeoutExpired("helper", timeout)

    monkeypatch.setattr(harness.helper, "wait", wait)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        owner.wait(timeout=0.2)
    assert caught.value.cmd == owner.args and caught.value.timeout == 0.2
    assert owner.returncode is None
    complete(harness)
    monkeypatch.setattr(harness.helper, "wait", lambda timeout=None: 0)
    owner.close()


def test_requested_cancellation_preserves_native_signal_and_waits_for_reaping(harness, monkeypatch):
    owner = start(harness)

    def cancel():
        harness.record.update(state="CANCELLED", reason="cancelled", returncode=-15)
        harness.helper.returncode = 0

    monkeypatch.setattr(harness.control, "cancel", cancel)
    owner.cancel()
    assert owner.wait() == -15 and owner.returncode == -15
    owner.close()
    assert harness.helper.stdout.closed


def test_refused_disposal_keeps_streams_owner_and_primary_for_retry(harness):
    owner = start(harness)
    primary = KeyboardInterrupt("original")
    owner.close(primary=primary)
    assert primary._langadapter_linux_command is owner
    assert primary._langadapter_control is harness.owned
    assert not harness.helper.stdout.closed and not owner._closed
    complete(harness)
    owner.close()
    assert owner._closed and harness.helper.stdout.closed


def test_a_renewal_blocked_in_storage_still_lets_a_completed_command_dispose(harness):
    """A renewal blocked in a storage stall for longer than the heartbeat's
    join still lets a COMPLETE command dispose: its owner resolves, and its
    finished result is kept."""
    from langadapter import _process_tree

    owner = start(harness)
    entered, never = threading.Event(), threading.Event()

    def slow_pulse():
        entered.set()
        never.wait(3)  # a create or rename waiting on the file system journal

    harness.control.pulse = slow_pulse
    owner._heartbeat = _REAL_THREAD(target=owner._beat, daemon=True)
    owner._heartbeat.start()
    assert entered.wait(5)
    complete(harness, 0)
    assert _process_tree.finish(owner) is True
    assert owner._closed and owner.returncode == 0


def test_unstopped_heartbeat_cannot_release_streams(harness, monkeypatch):
    owner = start(harness)
    complete(harness)
    monkeypatch.setattr(owner._heartbeat, "join", lambda timeout=None: None)
    with pytest.raises(ControlError, match="heartbeat did not stop") as caught:
        owner.close()
    assert caught.value._langadapter_linux_command is owner
    assert not harness.helper.stdout.closed
    owner._heartbeat.alive = False
    owner.close()


def test_unconfirmed_output_keeps_streams_until_the_same_owner_confirms_drain(harness):
    owner = start(harness)
    complete(harness)
    owner._langadapter_output_unconfirmed = True
    with pytest.raises(ControlError, match="output draining remains unconfirmed") as caught:
        owner.close()
    assert caught.value._langadapter_linux_command is owner
    assert not harness.helper.stdout.closed
    owner._langadapter_output_unconfirmed = False
    owner.close()
    assert harness.helper.stdout.closed


def test_heartbeat_write_failure_is_preserved_and_not_native_success(harness, monkeypatch):
    owner = start(harness)
    original = OSError("control write interrupted")

    def pulse():
        raise original

    monkeypatch.setattr(harness.control, "pulse", pulse)
    monkeypatch.setattr(owner._stopped, "wait", lambda timeout: False)
    owner._beat()
    assert owner._heartbeat_error is original
    with pytest.raises(OSError) as caught:
        owner.wait()
    assert caught.value is original and original._langadapter_linux_command is owner
    complete(harness)
    with pytest.raises(OSError) as caught:
        owner.close()
    assert caught.value is original and not harness.helper.stdout.closed


def test_a_helper_that_never_started_closes_as_a_launch_that_left_nothing(harness, monkeypatch):
    """An ordinary error from the helper's Popen leaves no process: its
    control goes and a second close succeeds. An interruption keeps the
    control (next test)."""
    original = OSError(11, "Resource temporarily unavailable")

    def launch(*args, **kwargs):
        raise original

    monkeypatch.setattr(command.subprocess, "Popen", launch)
    with pytest.raises(OSError) as caught:
        start(harness)
    owner = caught.value._langadapter_linux_command
    assert caught.value is original and owner.helper is None
    assert owner._closed and owner._langadapter_command_disposed is True
    assert ("remove",) in harness.calls and ("cancel",) not in harness.calls
    assert owner.close() is True


def test_interrupted_helper_creation_keeps_control_and_original_error(harness, monkeypatch):
    original = KeyboardInterrupt("during creation")

    def launch(*args, **kwargs):
        raise original

    monkeypatch.setattr(command.subprocess, "Popen", launch)
    with pytest.raises(KeyboardInterrupt) as caught:
        start(harness)
    assert caught.value is original and original._langadapter_control is harness.owned
    assert original._langadapter_linux_command.helper is None
    assert any(call[0] == "cancel" for call in harness.calls)


@pytest.mark.parametrize("options", [{"shell": True}, {"executable": "override"},
                                     {"preexec_fn": object()}, {"pass_fds": (1,)}])
def test_unsupported_launch_options_are_refused_before_allocation(harness, options):
    with pytest.raises(ControlError, match="Unsupported supervised"):
        start(harness, **options)
    assert harness.calls == []


def test_nonlinux_refuses_before_control_creation(harness, monkeypatch):
    monkeypatch.setattr(command, "_supported_host", lambda: False)
    with pytest.raises(SupervisionUnavailable):
        start(harness)
    assert harness.calls == []


@pytest.mark.parametrize("mode", ["zero", "nonzero", "signal", "missing", "wrong_pid",
                                  "failed_start"])
def test_actual_control_reports_bind_the_acquired_helper_without_process_launch(
        harness, monkeypatch, tmp_path, mode):
    code = {"zero": 0, "nonzero": 7, "signal": -11}.get(mode)
    real, requests = [], []

    def create(*args, **kwargs):
        kwargs.update(parent=str(tmp_path), receipt_parent=str(tmp_path))
        control = _REAL_CREATE(*args, **kwargs)
        real.append(control)
        requests.append(json.loads((control.owned.root / "request.json").read_text(encoding="utf-8")))
        if mode != "missing":
            report = {"version": 1, "token": control.owned.token,
                      "helper_pid": 43 if mode == "wrong_pid" else 42,
                      "state": "FAILED" if mode == "failed_start" else "COMPLETE",
                      "command_pid": None if mode == "failed_start" else 84,
                      "returncode": code, "quiescent": True, "reason": None}
            if mode == "failed_start":
                report["error"] = {"type": "FileNotFoundError", "message": "native absent",
                                   "errno": 2, "filename": "selected-native"}
            (control.owned.root / "status.json").write_text(json.dumps(report), encoding="utf-8")
        harness.helper.returncode = 1 if mode == "failed_start" else 0
        return control

    monkeypatch.setattr(command._Control, "create", create)
    if mode == "failed_start":
        # The program could not start: an ordinary start failure, owner closed.
        with pytest.raises(FileNotFoundError) as caught:
            start(harness)
        owner = caught.value._langadapter_linux_command
        assert owner.helper is harness.helper and owner.returncode is None and owner._closed
    elif code is None:
        with pytest.raises(ControlError) as caught:
            start(harness)
        owner = caught.value._langadapter_linux_command
        assert owner.helper is harness.helper and owner.returncode is None
    else:
        owner = start(harness)
        assert owner.wait() == code and owner.pid == 84
        assert owner._record["helper_pid"] == harness.helper.pid
        owner.close()
    assert len(real) == 1
    if code is None and mode != "failed_start":
        # Disposal unconfirmed: the control directory and its receipt stay.
        real[0].owned.verify()
    else:
        # Disposal confirmed: neither the directory nor its receipt remains.
        assert not real[0].owned.root.exists() and not real[0].owned.receipt.exists()
    assert requests[0]["argv"] == ["selected-native", "argument"]
    assert "env" not in requests[0]


def test_a_confirmed_close_removes_its_control_dir_once(harness):
    owner = start(harness)
    complete(harness)
    assert owner.wait() == 7
    assert owner.close() is True and owner.close() is True
    assert harness.calls.count(("remove",)) == 1


def test_a_closed_command_answers_from_its_terminal_report_without_the_channel(harness):
    owner = start(harness)
    complete(harness)
    assert owner.close() is True
    calls = len(harness.calls)
    # A worker cleanup polls, cancels and waits finished owners again: the
    # removed channel is not read or written.
    assert owner.poll() == 7 and owner.wait(timeout=5) == 7
    owner.cancel()
    assert harness.calls[calls:] == []


def test_an_unconfirmed_close_keeps_its_control_dir(harness):
    owner = start(harness)
    # The helper never exits within the cleanup bound: disposal is uncertain.
    with pytest.raises(subprocess.TimeoutExpired):
        owner.close()
    assert not owner._closed and ("remove",) not in harness.calls


def test_a_failed_removal_keeps_the_dir_and_the_confirmed_disposal(harness):
    def refuse():
        raise OSError("busy")

    harness.owned.remove = refuse
    owner = start(harness)
    complete(harness)
    owner.wait()
    with pytest.warns(RuntimeWarning, match="supervisor control directory retained"):
        assert owner.close() is True
    assert owner._closed


@pytest.mark.skipif(sys.platform != "linux" or os.environ.get("LANGADAPTER_TEST_LINUX_JOB") != "1",
                    reason="Opt-in acquired Linux controller native fixture")
def test_finished_native_commands_leave_no_control_dirs_or_receipts(tmp_path):
    controls, receipts = tmp_path / "controls", tmp_path / "receipts"
    controls.mkdir()
    receipts.mkdir()
    for index in range(5):
        owner = command._Command.start(
            [sys.executable, "-I", "-c", f"print({index})"], parent=str(controls),
            receipt_parent=str(receipts), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert owner.communicate(timeout=15) == (f"{index}\n".encode(), b"")
        assert owner.close() is True
    assert list(controls.iterdir()) == [] and list(receipts.iterdir()) == []


def _subject(tmp_path, mode):
    child = ("import os,time; os.setsid(); "
             "open(%r,'w').write(str(os.getpid())); time.sleep(120)" % str(tmp_path / "child.pid"))
    return ("import os,sys,time,subprocess\n"
            "subprocess.Popen([sys.executable,'-I','-c',%r])\n" % child
            + "deadline=time.monotonic()+10\n"
            + "while not os.path.exists(%r) and time.monotonic()<deadline: time.sleep(.01)\n"
            % str(tmp_path / "release")
            + "assert os.path.exists(%r)\n" % str(tmp_path / "release")
            + "assert os.environ['LANGADAPTER_NATIVE_FIXTURE']=='exact'\n"
            + "os.write(1,b'native\\r\\n'); os.write(2,b'error\\n')\n"
            + ("sys.exit(7)\n" if mode == "parent_exit" else "time.sleep(120)\n"))


@pytest.mark.parametrize("mode", ["parent_exit", "timeout", "cancel"])
def test_controller_fixture_source_compiles_without_process_execution(tmp_path, mode):
    compile(_subject(tmp_path, mode), "acquired-controller-subject", "exec")


@pytest.mark.skipif(sys.platform != "linux" or os.environ.get("LANGADAPTER_TEST_LINUX_JOB") != "1",
                    reason="Opt-in acquired Linux controller native fixture")
@pytest.mark.parametrize("mode", ["parent_exit", "timeout", "cancel"])
def test_native_controller_keeps_native_exit_output_and_owned_reaping(tmp_path, mode):
    env = dict(os.environ, LANGADAPTER_NATIVE_FIXTURE="exact")
    owner, descriptor = None, None
    with subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as unrelated:
        try:
            owner = command._Command.start([sys.executable, "-I", "-c", _subject(tmp_path, mode)],
                                           parent=str(tmp_path), receipt_parent=str(tmp_path),
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            deadline = time.monotonic() + 5
            while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert (tmp_path / "child.pid").exists(), "fixture child did not reach its boundary"
            descriptor = os.pidfd_open(int((tmp_path / "child.pid").read_text()), 0)
            (tmp_path / "release").write_text("go")
            if mode == "timeout":
                with pytest.raises(subprocess.TimeoutExpired) as caught:
                    owner.communicate(timeout=.2)
                assert caught.value.cmd == owner.args
                assert caught.value.output == b"native\r\n" and caught.value.stderr == b"error\n"
                owner.cancel()
            elif mode == "cancel":
                # Wait for the subject's writes so output assertions do not race SIGTERM.
                with pytest.raises(subprocess.TimeoutExpired):
                    owner.communicate(timeout=.2)
                owner.cancel()
            assert owner.communicate(timeout=15) == (b"native\r\n", b"error\n")
            assert owner.returncode == (7 if mode == "parent_exit" else -signal.SIGTERM)
            assert owner.helper.returncode == 0 and owner.pid != owner.helper.pid
            assert owner._record["quiescent"] and owner._stopped.is_set()
            watch = select.poll()
            watch.register(descriptor, select.POLLIN | select.POLLHUP)
            assert watch.poll(1000), "escaped owned child is still running"
            assert unrelated.poll() is None
            owner.close()
            assert not owner.control.owned.root.exists()
            assert not owner.control.owned.receipt.exists()
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
                if owner is not None:
                    owner.close()
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=5)


def test_every_start_waits_the_shared_startup_bound():
    """Every supervised start, local and container, waits the shared bound, long
    enough for a healthy start under heavy load."""
    import inspect

    from langadapter import _linux_command, _linux_container

    bound = _linux_command.STARTUP_BOUND_S
    assert bound >= 60
    for function in (_linux_command._Command.start, _linux_command._Command.start_in_container,
                     _linux_container.start):
        assert inspect.signature(function).parameters["startup"].default == bound
