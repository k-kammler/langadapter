"""Actual private controller with owned-command stand-ins, never native launch."""
import json
import subprocess

import pytest

from langadapter.java import _warmed as controller, _warmed as native
from langadapter.outcomes import Outcome

from test_warmed_protocol import report


class Owner:
    args = ["java", "@fixture.args"]

    def __init__(self):
        self.returncode = None
        self.native_code = 0
        self.actions = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        assert timeout > 0
        self.actions.append("wait")
        self.returncode = self.native_code
        return self.returncode


@pytest.fixture
def scope(monkeypatch, tmp_path):
    owner = Owner()
    protocol = native.ForkProtocol("b" * 32, 0, 1, 2)
    clock = [100.0]
    monkeypatch.setattr(controller.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(controller.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def stop(received):
        assert received is owner
        owner.actions.append("stop")
        if owner.returncode is None:
            owner.returncode = -15

    def drain(received):
        assert received is owner
        owner.actions.append("drain")

    def finish(received, *, primary):
        assert received is owner
        owner.actions.append(("finish", primary))
        return True

    monkeypatch.setattr(controller._process_tree, "stop", stop)
    monkeypatch.setattr(controller._process_tree, "drain_output", drain)
    monkeypatch.setattr(controller._process_tree, "finish", finish)
    return owner, protocol, tmp_path, clock


def write(directory, protocol, event, index=None, **changes):
    name = event if index is None else f"{event}-{index}"
    (directory / f"{name}.json").write_text(json.dumps(report(protocol, event, index, **changes)),
                                          encoding="utf-8")


def green_reports(scope):
    _, protocol, directory, _ = scope
    write(directory, protocol, "ready")
    for index in range(3):
        write(directory, protocol, "start", index)
        write(directory, protocol, "iteration", index)
    write(directory, protocol, "complete")


def drive(scope, **kwargs):
    owner, protocol, directory, _ = scope
    return controller.drive(owner, directory, protocol, total_deadline=120.0,
                            startup_deadline=105.0, iteration_timeout_s=2.0, **kwargs)


def test_normal_fork_receives_raw_records_before_positive_shared_disposal(scope):
    green_reports(scope)
    events = []
    result = drive(scope, on_event=events.append)
    owner, protocol, directory, _ = scope
    assert result.outcome is Outcome.COMPLETED
    assert result.transcript.finished and len(result.transcript.iterations) == 3
    assert [event.event for event in events] == [
        "ready", "start", "iteration", "start", "iteration", "start", "iteration", "complete"]
    assert [path.read_text() for path in sorted(directory.glob("*.permit"))] == [protocol.token] * 3
    assert owner.actions == ["wait", "drain", ("finish", None)]


@pytest.mark.parametrize("stage", ["startup", "start", "discovery_or_execution", "terminal"])
def test_stage_deadline_never_reuses_incomplete_native_fork(scope, stage):
    owner, protocol, directory, clock = scope
    if stage != "startup":
        write(directory, protocol, "ready")
    if stage in ("discovery_or_execution", "terminal"):
        write(directory, protocol, "start", 0)
    if stage == "terminal":
        for index in range(3):
            write(directory, protocol, "start", index)
            write(directory, protocol, "iteration", index)
    result = drive(scope)
    assert result.outcome is Outcome.TIMED_OUT
    assert not result.transcript.finished
    assert owner.actions[-3:-1] == ["stop", "drain"]
    assert isinstance(owner.actions[-1][1], subprocess.TimeoutExpired)
    expected = 105 if stage == "startup" else 120 if stage == "terminal" else 102
    assert clock[0] == pytest.approx(expected)
    assert len(result.transcript.iterations) == (3 if stage == "terminal" else 0)


def test_total_deadline_is_shared_not_reset_for_each_iteration(scope):
    green_reports(scope)
    _, _, directory, clock = scope

    def observe(event):
        if event.event == "iteration":
            clock[0] += 11

    result = drive(scope, on_event=observe)
    assert result.outcome is Outcome.TIMED_OUT
    assert len(result.transcript.iterations) == 2
    assert not (directory / "run-2.permit").exists()


def test_timeout_retains_received_warmup_without_inventing_measurement(scope):
    _, protocol, directory, _ = scope
    write(directory, protocol, "ready")
    write(directory, protocol, "start", 0)
    write(directory, protocol, "iteration", 0)
    result = drive(scope)
    assert result.outcome is Outcome.TIMED_OUT
    assert [(item.kind, item.elapsed_ns) for item in result.transcript.iterations] == [("warmup", 123)]
    assert not (directory / "run-2.permit").exists()


def test_observer_exception_is_outside_native_sample_and_diagnostic_only(scope):
    green_reports(scope)

    def broken(event):
        raise ValueError("observer fixture")

    result = drive(scope, on_event=broken)
    assert result.outcome is Outcome.COMPLETED
    assert len(result.diagnostics) == 8
    assert all(item.elapsed_ns == 123 for item in result.transcript.iterations)


def test_changed_native_population_stops_before_next_iteration_without_pruning(scope):
    green_reports(scope)
    owner, protocol, directory, _ = scope
    changed = report(protocol, "iteration", 1)
    changed["tests"][0]["unique_id"] = "different-native-test"
    (directory / "iteration-1.json").write_text(json.dumps(changed))
    result = drive(scope)
    assert result.outcome is Outcome.COMPLETED
    assert "changed" in result.reason
    assert result.transcript.repeatable is False and not result.transcript.finished
    assert len(result.transcript.iterations) == 2
    assert not (directory / "run-2.permit").exists()
    assert owner.actions == ["stop", "drain", ("finish", None)]


def test_native_failure_is_not_retried_for_a_passing_sample(scope):
    _, protocol, directory, _ = scope
    write(directory, protocol, "ready")
    write(directory, protocol, "start", 0)
    payload = report(protocol, "iteration", 0, outcome="test_failures", reason="assertion failed")
    payload["tests"][0]["status"] = "failed"
    payload["counts"].update(passed=0, failed=1)
    (directory / "iteration-0.json").write_text(json.dumps(payload))
    write(directory, protocol, "complete", outcome="test_failures", iterations_completed=1)
    result = drive(scope)
    assert result.outcome is Outcome.TEST_FAILURES and result.reason == "assertion failed"
    assert len(result.transcript.iterations) == 1
    assert not (directory / "run-1.permit").exists()


def test_stateful_second_iteration_failure_keeps_native_terminal_verdict(scope):
    green_reports(scope)
    _, protocol, directory, _ = scope
    payload = report(protocol, "iteration", 1, outcome="test_failures", reason="persistent state")
    payload["tests"][0]["status"] = "failed"
    payload["counts"].update(passed=0, failed=1)
    (directory / "iteration-1.json").write_text(json.dumps(payload))
    write(directory, protocol, "complete", outcome="test_failures", iterations_completed=2)
    result = drive(scope)
    assert result.outcome is Outcome.TEST_FAILURES
    assert result.reason == "persistent state"
    assert result.transcript.finished and result.transcript.repeatable is False
    assert len(result.transcript.iterations) == 2
    assert not (directory / "run-2.permit").exists()


@pytest.mark.parametrize("issue", ["lost_report", "abnormal_exit", "malformed", "stale_permit"])
def test_native_root_exit_and_reports_do_not_replace_protocol_or_disposal(scope, issue):
    green_reports(scope)
    owner, _, directory, _ = scope
    if issue == "lost_report":
        (directory / "ready.json").unlink()
        owner.returncode = 0
    elif issue == "abnormal_exit":
        owner.native_code = 7
    elif issue == "malformed":
        (directory / "iteration-0.json").write_text("{broken")
    else:
        (directory / "run-0.permit").write_text("stale")
    result = drive(scope)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert len([item for item in owner.actions if isinstance(item, tuple) and item[0] == "finish"]) == 1


def test_actual_interrupt_preserves_primary_and_disposes_once(scope):
    green_reports(scope)
    owner, _, _, _ = scope
    primary = KeyboardInterrupt("owned cancellation")

    def interrupted(event):
        raise primary

    with pytest.raises(KeyboardInterrupt) as caught:
        drive(scope, on_event=interrupted)
    assert caught.value is primary
    assert owner.actions == ["stop", "drain", ("finish", primary)]


@pytest.mark.parametrize("primary_kind", ["timeout", "interrupt", "normal"])
def test_uncertain_disposal_preserves_exact_owner_and_primary(scope, monkeypatch, primary_kind):
    owner, _, _, _ = scope
    if primary_kind != "timeout":
        green_reports(scope)
    primary = KeyboardInterrupt("exact primary")

    def uncertain(received, *, primary):
        assert received is owner
        owner.actions.append(("finish", primary))
        return False

    def interrupted(event):
        raise primary

    monkeypatch.setattr(controller._process_tree, "finish", uncertain)
    expected = KeyboardInterrupt if primary_kind == "interrupt" else (
        subprocess.TimeoutExpired if primary_kind == "timeout" else RuntimeError)
    with pytest.raises(expected) as caught:
        drive(scope, on_event=interrupted if primary_kind == "interrupt" else None)
    assert caught.value._langadapter_command is owner
    if primary_kind == "interrupt":
        assert caught.value is primary


def test_secondary_cleanup_failure_remains_attached_to_primary_owner(scope, monkeypatch):
    green_reports(scope)
    owner, _, _, _ = scope
    primary = KeyboardInterrupt("primary")
    secondary = OSError("cleanup failure")

    def interrupted(event):
        raise primary

    def broken_stop(received):
        assert received is owner
        raise secondary

    monkeypatch.setattr(controller._process_tree, "stop", broken_stop)
    monkeypatch.setattr(controller._process_tree, "finish", lambda received, *, primary: False)
    with pytest.raises(KeyboardInterrupt) as caught:
        drive(scope, on_event=interrupted)
    assert caught.value is primary
    assert caught.value._langadapter_command is owner
    assert caught.value._langadapter_warmed_cleanup_error is secondary
    if hasattr(primary, "add_note"):
        assert any("cleanup failure" in note for note in primary.__notes__)


def test_finish_exception_does_not_replace_original_cancellation(scope, monkeypatch):
    green_reports(scope)
    owner, _, _, _ = scope
    primary = KeyboardInterrupt("original cancellation")
    secondary = OSError("disposal exception")

    def interrupted(event):
        raise primary

    def broken_finish(received, *, primary):
        assert received is owner
        raise secondary

    monkeypatch.setattr(controller._process_tree, "finish", broken_finish)
    with pytest.raises(KeyboardInterrupt) as caught:
        drive(scope, on_event=interrupted)
    assert caught.value is primary
    assert primary._langadapter_command is owner
    assert primary._langadapter_warmed_cleanup_error is secondary


@pytest.mark.parametrize("denials", [3, 1000])
def test_a_report_held_just_after_its_rename_is_read_again(scope, monkeypatch, denials):
    """A report still held just after its rename (Windows) is read again, and
    a hold beyond the bound fails the run."""
    green_reports(scope)
    read, seen = native.read, []

    def held(path, protocol):
        seen.append(path.name)
        if path.name == "iteration-1.json" and seen.count(path.name) <= denials:
            raise PermissionError(13, "Permission denied", str(path))
        return read(path, protocol)

    monkeypatch.setattr(controller, "read", held)
    result = drive(scope)
    if denials == 3:
        assert result.outcome is Outcome.COMPLETED and seen.count("iteration-1.json") == 4
    else:
        assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
        assert "Permission denied" in result.reason


def test_a_report_written_just_before_the_fork_exits_is_received(monkeypatch):
    """A fork may write its report and exit between the controller's look
    for the file and its look at the process: the report is received."""
    import time
    from types import SimpleNamespace

    class Report:
        name = "run-0.json"
        looks = 0

        def exists(self):
            self.looks += 1
            return self.looks > 1

    event = object()
    monkeypatch.setattr(controller, "read", lambda path, protocol: event)
    accepted, observed = [], []
    transcript = SimpleNamespace(protocol=None, accept=accepted.append)
    owner = SimpleNamespace(poll=lambda: 0, args=["java"])
    assert controller._receive(Report(), transcript, owner, time.monotonic() + 10,
                               observed.append) is event
    assert accepted == observed == [event]


def test_the_fork_reads_a_held_permit_again_as_long_as_the_controller_does():
    """A file just renamed into place can be held briefly on Windows: the
    fork's helper reads its permit again for as long as the controller reads
    a report again (_DENIED_SECONDS)."""
    from langadapter.java import _warmed_fork

    source = _warmed_fork.HELPER_SOURCE
    assert f"DENIED_MILLIS = {round(controller._DENIED_SECONDS * 1000)}L;" in source
    assert "readHeld(permit)" in source and "Files.readAllBytes(permit)" not in source
