"""Private helper control, liveness and the control channel without child launches."""
import json
import signal
import sys
from types import SimpleNamespace

import pytest

from langadapter import _linux_job as control

TOKEN = "a" * 32


def request(**overrides):
    return {"token": TOKEN, "version": 1, "argv": ["native", "a b"], "cwd": "/project",
            "lease_seconds": 1, "grace": 0, "kill_wait": 1, **overrides}


@pytest.fixture
def harness(monkeypatch):
    clock = [0.0]
    calls = []
    reports = []
    data = {"request.json": request(), "lease.json": {"token": TOKEN, "generation": 0},
            "cancel.json": None}
    codes = [None, 7]
    close_failures = []
    job = SimpleNamespace(process=None, returncode=None, quiescent=True)

    def start(argv, **options):
        calls.append(("start", argv, options))
        job.process = SimpleNamespace(pid=42)
        job.quiescent = False

    def poll():
        code = codes.pop(0) if codes else None
        job.returncode = code
        return code

    def close(**options):
        calls.append(("close", options))
        if close_failures:
            failure = close_failures.pop(0)
            if failure is not None:
                raise failure
        job.quiescent = True

    def report(state, **fields):
        reports.append((state, {**fields, "quiescent": job.quiescent,
                                "returncode": job.returncode}))

    def sleep(duration):
        clock[0] += duration

    def forbidden(*args, **kwargs):
        pytest.fail("No native process or signal setup belongs in helper control fixtures")

    job.start, job.poll, job.close = start, poll, close
    channel = SimpleNamespace(token=TOKEN, read=lambda name, **options: data[name], report=report)
    cancel = SimpleNamespace(signal=None, install=lambda: {15: "prior"},
                             restore=lambda old: calls.append(("restore", old)))
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(control.time, "sleep", sleep)
    monkeypatch.setattr(control.subprocess, "Popen", forbidden)
    monkeypatch.setattr(control.subprocess, "run", forbidden)
    monkeypatch.setattr(control.signal, "signal", forbidden)
    return SimpleNamespace(job=job, channel=channel, cancel=cancel, clock=clock,
                           data=data, calls=calls, reports=reports, codes=codes,
                           close_failures=close_failures)


def serve(harness, **options):
    return control._serve(harness.channel, job_factory=lambda: harness.job,
                          cancellation=harness.cancel, **options)


@pytest.mark.parametrize("code", [0, 7, -11])
def test_success_preserves_native_status_and_requires_cleanup_before_report(harness, code):
    harness.codes[:] = [None, code]
    assert serve(harness) == 0
    assert harness.calls[0] == ("start", ["native", "a b"], {"cwd": "/project"})
    assert harness.calls[1] == ("close", {"grace": 0, "kill_wait": 1})
    assert [state for state, fields in harness.reports] == ["READY", "RUNNING", "EXITED", "COMPLETE"]
    assert harness.reports[-2][1]["returncode"] == code
    assert harness.reports[-2][1]["quiescent"] is False
    assert harness.reports[-1][1]["quiescent"] is True
    assert harness.reports[-1][1]["returncode"] == code
    assert harness.calls[-1] == ("restore", {15: "prior"})


@pytest.mark.parametrize("environment", [{}, {"PATH": "/selected", "VALUE": "exact"}])
def test_captured_native_environment_is_admitted_exactly(harness, environment):
    harness.data["request.json"]["native_environment"] = environment
    harness.codes[:] = [7]
    assert serve(harness) == 0
    assert harness.calls[0] == ("start", ["native", "a b"], {"cwd": "/project", "env": environment})
    assert harness.reports[-1][0] == "COMPLETE" and harness.job.returncode == 7


@pytest.mark.parametrize("cause", ["file", "signal", "disconnect"])
def test_cancellation_and_disconnect_cleanup_are_not_native_success(harness, cause):
    harness.codes[:] = [None]
    if cause == "file":
        harness.data["cancel.json"] = {"token": TOKEN}
    elif cause == "signal":
        harness.cancel.signal = 15
    assert serve(harness) == 0
    assert harness.reports[-1][0] == "CANCELLED"
    assert harness.reports[-1][1]["reason"] == (
        "host control lease expired" if cause == "disconnect" else "cancelled before native start")
    assert harness.reports[-1][1]["quiescent"] is True


@pytest.mark.parametrize("boundary", ["pending_file", "pending_signal", "install_signal",
                                      "setup_file", "setup_signal", "ready_file", "ready_signal",
                                      "lease_file", "lease_signal"])
def test_cancelled_admission_never_starts_even_an_immediately_exiting_command(harness, boundary):
    harness.codes[:] = [0]
    def factory():
        return harness.job

    if boundary == "pending_file":
        harness.data["cancel.json"] = {"token": TOKEN}
    elif boundary == "pending_signal":
        harness.cancel.signal = 15
    elif boundary == "install_signal":
        def install():
            harness.cancel.signal = 15
            return {15: "prior"}

        harness.cancel.install = install
    elif boundary.startswith("setup"):
        def factory():
            if boundary == "setup_file":
                harness.data["cancel.json"] = {"token": TOKEN}
            else:
                harness.cancel.signal = 15
            return harness.job
    elif boundary.startswith("ready"):
        report = harness.channel.report

        def ready(state, **fields):
            report(state, **fields)
            if state == "READY":
                if boundary == "ready_file":
                    harness.data["cancel.json"] = {"token": TOKEN}
                else:
                    harness.cancel.signal = 15

        harness.channel.report = ready
    else:
        read = harness.channel.read
        leases = []

        def admission_read(name, **kwargs):
            value = read(name, **kwargs)
            if name == "lease.json":
                leases.append(name)
                if len(leases) == 2:
                    if boundary == "lease_file":
                        harness.data["cancel.json"] = {"token": TOKEN}
                    else:
                        harness.cancel.signal = 15
            return value

        harness.channel.read = admission_read
    assert control._serve(harness.channel, job_factory=factory, cancellation=harness.cancel) == 0
    assert not any(call[0] == "start" for call in harness.calls)
    assert harness.codes == [0]  # No native poll or return code is invented.
    assert harness.job.process is None and harness.job.returncode is None
    assert [state for state, _ in harness.reports] == ["READY", "CANCELLED"]
    assert harness.reports[-1][1]["quiescent"]


@pytest.mark.parametrize("progress", [False, True])
def test_admission_rechecks_host_lease_after_setup(harness, progress):
    harness.codes[:] = [7]

    def factory():
        harness.clock[0] = 2
        if progress:
            harness.data["lease.json"]["generation"] = 1
        return harness.job

    result = control._serve(harness.channel, job_factory=factory, cancellation=harness.cancel)
    if progress:
        assert result == 0
        assert harness.reports[-1][0] == "COMPLETE"
        assert harness.job.returncode == 7
    else:
        assert result == 1
        assert not any(call[0] == "start" for call in harness.calls)
        assert harness.job.process is None and harness.job.returncode is None
        assert harness.reports[-1][0] == "FAILED"
        assert isinstance(harness.reports[-1][1]["error"], control.ControlError)


@pytest.mark.parametrize("cause", ["file", "signal"])
def test_post_launch_cancellation_still_cleans_the_acquired_owner(harness, cause):
    start = harness.job.start

    def launch(*args, **kwargs):
        start(*args, **kwargs)
        if cause == "file":
            harness.data["cancel.json"] = {"token": TOKEN}
        else:
            harness.cancel.signal = 15

    harness.job.start = launch
    harness.codes[:] = [None]
    assert serve(harness) == 0
    assert len([call for call in harness.calls if call[0] == "start"]) == 1
    assert harness.reports[-1][0] == "CANCELLED"
    assert harness.reports[-1][1]["reason"] == "cancelled"
    assert harness.job.quiescent


def test_failed_cleanup_is_reported_and_retried_in_same_owner_not_new_work(harness):
    original = PermissionError("owned accounting refused")
    harness.close_failures[:] = [original, original, None]
    assert serve(harness) == 1
    assert [state for state, fields in harness.reports] == ["READY", "RUNNING", "EXITED", "FAILED", "FAILED"]
    assert harness.reports[-2][1]["quiescent"] is False
    assert harness.reports[-1][1]["quiescent"] is True
    assert harness.reports[-1][1]["error"] is original
    assert len([call for call in harness.calls if call[0] == "start"]) == 1


def test_missing_setup_never_starts_a_command(harness):
    original = control.SupervisionUnavailable("pidfds denied")

    def unavailable():
        raise original

    assert control._serve(harness.channel, job_factory=unavailable, cancellation=harness.cancel) == 1
    assert not any(call[0] == "start" for call in harness.calls)
    assert harness.reports[-1][0] == "FAILED"
    assert harness.reports[-1][1]["error"] is original


def test_failed_exec_retains_original_exception_type_and_closes_owner(harness):
    original = FileNotFoundError(2, "native missing", "subject")

    def fail(*args, **kwargs):
        raise original

    harness.job.start = fail
    assert serve(harness) == 1
    assert [state for state, fields in harness.reports] == ["READY", "FAILED"]
    assert harness.reports[-1][1]["error"] is original
    assert harness.job.quiescent


def test_lost_status_report_refuses_success_even_when_children_are_reaped(harness):
    original = OSError("report unavailable")
    report = harness.channel.report

    def fail(state, **fields):
        if state == "COMPLETE":
            raise original
        report(state, **fields)

    harness.channel.report = fail
    assert serve(harness) == 1
    assert harness.reports[-1][0] == "FAILED"
    assert harness.reports[-1][1]["error"] is original
    assert harness.job.quiescent


def test_wrong_cancellation_identity_is_failure_not_an_instruction(harness):
    harness.data["cancel.json"] = {"token": "b" * 32}
    assert serve(harness) == 1
    assert harness.reports[-1][0] == "FAILED"
    assert isinstance(harness.reports[-1][1]["error"], control.ControlError)


def test_false_close_success_cannot_publish_complete(harness):
    original = harness.job.close
    count = 0

    def close(**options):
        nonlocal count
        count += 1
        if count == 1:
            return
        original(**options)

    harness.job.close = close
    assert serve(harness) == 1
    assert "COMPLETE" not in [state for state, fields in harness.reports]
    assert harness.reports[-1][1]["quiescent"] is True


def test_restore_failure_refuses_otherwise_complete_helper(harness):
    error = OSError("signal restoration refused")

    def fail(old):
        raise error

    harness.cancel.restore = fail
    with pytest.raises(OSError) as caught:
        serve(harness)
    assert caught.value is error
    assert harness.reports[-1][0] == "FAILED"
    assert harness.reports[-1][1]["error"] is error


def test_restore_failure_never_replaces_initial_failure(harness):
    original = FileNotFoundError("native missing")

    def missing(*args, **kwargs):
        raise original

    def restore(old):
        raise OSError("signal restoration refused")

    harness.job.start = missing
    harness.cancel.restore = restore
    assert serve(harness) == 1
    assert harness.reports[-1][1]["error"] is original


@pytest.mark.parametrize("failure", ["channel", "serve"])
def test_helper_entry_point_has_separate_failure_status_without_tool_output(monkeypatch, failure):
    def channel(*args):
        if failure == "channel":
            raise control.ControlError("channel refused")
        return SimpleNamespace()

    def serve(value):
        raise KeyboardInterrupt()

    monkeypatch.setattr(control, "_Channel", channel)
    monkeypatch.setattr(control, "_serve", serve)
    assert control._main(["owned-root", TOKEN]) == (2 if failure == "channel" else 1)


def test_liveness_uses_progress_not_clock_or_file_timestamp(harness):
    lease = control._Liveness(TOKEN, 1)
    assert not lease.observe({"token": TOKEN, "generation": 0})
    harness.clock[0] = 0.5
    assert not lease.observe({"token": TOKEN, "generation": 0})
    harness.clock[0] = 0.9
    assert not lease.observe({"token": TOKEN, "generation": 1})
    harness.clock[0] = 1.1
    assert not lease.observe({"token": TOKEN, "generation": 1})
    harness.clock[0] = 1.9
    assert lease.observe({"token": TOKEN, "generation": 1})


def test_a_stall_of_the_helper_itself_is_not_counted_against_the_host(harness):
    """A machine-wide stall that the helper wakes from does not expire a
    live host's lease."""
    lease = control._Liveness(TOKEN, 60)
    assert not lease.observe({"token": TOKEN, "generation": 0})
    harness.clock[0] = 65.0
    assert not lease.observe({"token": TOKEN, "generation": 0})
    harness.clock[0] = 65.5
    assert not lease.observe({"token": TOKEN, "generation": 1})
    # A host that stops renewing while the helper runs is still lost: gaps
    # of the stall threshold count whole.
    for step in range(1, 61):
        harness.clock[0] = 65.5 + step
        assert lease.observe({"token": TOKEN, "generation": 1}) is (step == 60)


@pytest.mark.parametrize("gap", [2.0, 8.0, 70.0])
def test_a_silent_host_is_lost_within_three_leases_however_the_helper_stalls(harness, gap):
    """At most twice the lease is excused per renewal, so however long the
    helper stalls, the deadline is three leases after the last renewal, seen
    at the helper's next observation."""
    lease = control._Liveness(TOKEN, 60)
    lease.observe({"token": TOKEN, "generation": 0})
    while not lease.observe({"token": TOKEN, "generation": 0}):
        harness.clock[0] += gap
    assert 60 <= harness.clock[0] < 3 * 60 + gap


def test_slow_helper_observations_still_expire_a_silent_host(harness):
    lease = control._Liveness(TOKEN, 3)
    lease.observe({"token": TOKEN, "generation": 0})
    expired = []
    for step in range(1, 8):
        harness.clock[0] = 1.5 * step
        expired.append(lease.observe({"token": TOKEN, "generation": 0}))
    # Each 1.5 s gap counts 1 s: the 3 s lease ends at the third gap.
    assert expired == [False, False, True, True, True, True, True]


@pytest.mark.parametrize("record", [{"token": "b" * 32, "generation": 0},
                                   {"token": TOKEN, "generation": True},
                                   {"token": TOKEN, "generation": -1},
                                   {"token": TOKEN, "generation": 0, "timestamp": 1}])
def test_invalid_liveness_is_not_an_extended_lease(harness, record):
    with pytest.raises(control.ControlError):
        control._Liveness(TOKEN, 1).observe(record)


def test_backwards_liveness_is_not_reusable(harness):
    lease = control._Liveness(TOKEN, 1)
    lease.observe({"token": TOKEN, "generation": 2})
    with pytest.raises(control.ControlError, match="backwards"):
        lease.observe({"token": TOKEN, "generation": 1})


def test_cancel_signal_install_keeps_native_ignored_dispositions(monkeypatch):
    monkeypatch.setattr(signal, "SIGHUP", 1, raising=False)
    changes = []
    monkeypatch.setattr(signal, "getsignal", lambda sig:
                        signal.SIG_IGN if sig == signal.SIGHUP else signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", lambda sig, handler:
                        changes.append((sig, handler)) or signal.SIG_DFL)
    cancel = control._Cancellation()
    prior = cancel.install()
    assert signal.SIGHUP not in prior
    assert all(sig != signal.SIGHUP for sig, handler in changes)
    cancel.receive(15, None)
    assert cancel.signal == 15
    cancel.restore(prior)
    assert changes[-2:] == [(signal.SIGTERM, signal.SIG_DFL), (signal.SIGINT, signal.SIG_DFL)]


@pytest.mark.skipif(sys.platform != "linux", reason="The report reads Linux child membership")
@pytest.mark.parametrize("case", ["regular", "token", "path_escape", "report"])
def test_control_channel_reads_and_reports_without_processes(tmp_path, case):
    root = tmp_path / "control"
    root.mkdir()
    path = root / "lease.json"
    record = {"token": TOKEN, "generation": 0}
    path.write_text(json.dumps(record))
    channel = control._Channel(str(root), TOKEN)
    if case == "regular":
        assert channel.read("lease.json") == record
        assert channel.read("cancel.json", optional=True) is None
    elif case == "token":
        path.write_text(json.dumps({**record, "token": "b" * 32}))
        with pytest.raises(control.ControlError, match="identity"):
            channel.read("lease.json")
    elif case == "path_escape":
        with pytest.raises(control.ControlError, match="Unknown control file"):
            channel.read("../foreign")
    else:
        channel.report("FAILED", error=FileNotFoundError(2, "missing", "native"))
        report = json.loads((root / "status.json").read_text())
        assert report["token"] == TOKEN
        assert report["quiescent"] is True
        assert report["error"] == {"type": "FileNotFoundError", "errno": 2,
                                   "filename": "native", "message": "[Errno 2] missing: 'native'"}
        assert not tuple(root.glob(".status-*.tmp"))
