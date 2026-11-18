"""Report writer and control reader checks with no supervisor or target
process."""
from __future__ import annotations

import errno
import json
import os
from types import SimpleNamespace

import pytest

from langadapter import _linux_job as helper


@pytest.fixture
def writer(tmp_path, monkeypatch):
    root = tmp_path / "control"
    root.mkdir()
    token = "a" * 32
    channel = helper._Channel(str(root), token)
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    system = SimpleNamespace(**vars(os))
    # Linux's rename replaces an existing report, as os.replace does on Windows.
    system.rename = os.replace
    monkeypatch.setattr(helper, "os", system)
    monkeypatch.setattr(helper, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    job = SimpleNamespace(process=SimpleNamespace(pid=123), returncode=7, quiescent=True)
    return SimpleNamespace(channel=channel, root=root, system=system, clock=clock,
                           rename=os.replace, job=job, token=token)


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM])
def test_transient_report_replace_denial_preserves_native_record(writer, code):
    writer.channel.report("READY")
    prior = (writer.root / "status.json").read_bytes()
    attempts = []

    def denied_then_published(*args, **kwargs):
        attempts.append(args)
        if len(attempts) <= 2:
            assert (writer.root / "status.json").read_bytes() == prior
            raise PermissionError(code, "mapped reader holds report")
        writer.rename(*args, **kwargs)

    writer.system.rename = denied_then_published
    writer.channel.report("COMPLETE", job=writer.job)
    report = json.loads((writer.root / "status.json").read_bytes())
    assert report == {"version": 1, "token": writer.token, "helper_pid": os.getpid(),
                      "state": "COMPLETE", "command_pid": 123, "returncode": 7,
                      "quiescent": True, "reason": None}
    assert len(attempts) == 3 and writer.clock.sleeps == [0.01, 0.01]
    assert not tuple(writer.root.glob(".status-*.tmp"))


@pytest.mark.parametrize("frozen_clock", [False, True])
def test_persistent_report_denial_has_time_and_attempt_bounds(writer, frozen_clock):
    writer.channel.report("RUNNING", job=SimpleNamespace(
        process=SimpleNamespace(pid=123), returncode=None, quiescent=False))
    prior = (writer.root / "status.json").read_bytes()
    denied = PermissionError(errno.EACCES, "persistent report denial")
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise denied

    if frozen_clock:
        helper.time.sleep = writer.clock.sleeps.append
    writer.system.rename = refuse
    with pytest.raises(PermissionError) as caught:
        writer.channel.report("COMPLETE", job=writer.job)
    assert caught.value is denied
    assert 2 <= len(attempts) <= helper._REPORT_REPLACE_ATTEMPTS
    assert sum(writer.clock.sleeps) <= helper._REPORT_REPLACE_SECONDS + 1e-9
    assert (writer.root / "status.json").read_bytes() == prior
    assert not tuple(writer.root.glob(".status-*.tmp"))


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "I/O failed"),
                                     PermissionError("no native sharing code"), KeyboardInterrupt()])
def test_other_report_failures_are_not_retried_or_masked(writer, failure):
    cleanup = OSError(errno.EIO, "temporary cleanup failed")
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(args)
        raise failure

    writer.system.rename = fail
    writer.system.unlink = lambda *args, **kwargs: (_ for _ in ()).throw(cleanup)
    with pytest.raises(type(failure)) as caught:
        writer.channel.report("COMPLETE", job=writer.job)
    assert caught.value is failure
    assert len(attempts) == 1 and not writer.clock.sleeps
    assert not (writer.root / "status.json").exists()
    assert len(tuple(writer.root.glob(".status-*.tmp"))) == 1


@pytest.mark.parametrize("persistent", [False, True])
def test_serve_report_denial_keeps_failure_distinct_from_native_exit(writer, persistent):
    events = []
    attempts = 0

    class Job:
        process = None
        returncode = None
        quiescent = True

        def start(self, argv, *, cwd):
            events.append("start")
            self.process = SimpleNamespace(pid=123)
            self.quiescent = False

        def poll(self):
            self.returncode = 7
            return 7

        def close(self, **kwargs):
            events.append("close")
            if self.process is not None and self.returncode is None:
                self.returncode = -15
            self.quiescent = True

    job = Job()

    def report_rename(source, *args, **kwargs):
        nonlocal attempts
        record = json.loads((writer.root / source).read_bytes())
        if record["state"] == "RUNNING":
            attempts += 1
            if persistent or attempts == 1:
                raise PermissionError(errno.EACCES, "mapped report denial")
        writer.rename(source, *args, **kwargs)

    def read(name, *, optional=False):
        if name == "request.json":
            return {"version": 1, "token": writer.token, "argv": ["native"],
                    "cwd": None, "lease_seconds": 15, "grace": 0.01, "kill_wait": 0.01}
        if name == "lease.json":
            return {"token": writer.token, "generation": 0}
        assert optional
        return None

    writer.system.rename = report_rename
    writer.channel.read = read
    cancellation = SimpleNamespace(signal=None, install=lambda: {}, restore=lambda _: None)
    result = helper._serve(writer.channel, job_factory=lambda: job, cancellation=cancellation)
    report = json.loads((writer.root / "status.json").read_bytes())
    assert events[0] == "start" and events[-1] == "close"
    assert report["quiescent"] is True and report["command_pid"] == 123
    if persistent:
        assert result == 1 and report["state"] == "FAILED" and report["returncode"] == -15
        assert report["error"]["type"] == "PermissionError"
        assert report["error"]["errno"] == errno.EACCES
    else:
        assert result == 0 and report["state"] == "COMPLETE" and report["returncode"] == 7
        assert attempts == 2 and "error" not in report


class RacingStream:
    """A control input stream whose read can fail as during a drvfs rename."""

    def __init__(self, native, fail, streams):
        self.native, self.fail = native, fail
        streams.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.native.close()

    def fileno(self):
        return self.native.fileno()

    def read(self, size=-1):
        self.fail("read")
        return self.native.read(size)


def racing(writer, fail):
    native_open, native_fdopen = writer.system.open, writer.system.fdopen
    streams = []

    def open_file(name, *args, **kwargs):
        fail("open")
        return native_open(name, *args, **kwargs)

    writer.system.open = open_file
    writer.system.fdopen = lambda *a, **k: RacingStream(native_fdopen(*a, **k), fail, streams)
    return streams


def lease(writer):
    record = {"token": writer.token, "generation": 3}
    (writer.root / "lease.json").write_text(json.dumps(record))
    return record


@pytest.mark.parametrize("point", ["open", "read"])
def test_replaced_lease_seen_missing_or_without_data_is_read_again(writer, point):
    # On WSL's mount of a Windows drive (drvfs), a read racing the host's
    # atomic lease rename can find the name missing or the file without data.
    record = lease(writer)
    failures = []

    def fail(name):
        if name == point and not failures:
            failures.append(OSError(errno.ENODATA, "No data available") if name == "read"
                            else FileNotFoundError(errno.ENOENT, "No such file", "lease.json"))
            raise failures[-1]

    streams = racing(writer, fail)
    assert writer.channel.read("lease.json") == record
    assert len(failures) == 1 and writer.clock.sleeps == [0.01]
    assert all(stream.native.closed for stream in streams)


@pytest.mark.parametrize("point", ["open", "read"])
@pytest.mark.parametrize("frozen_clock", [False, True])
def test_persistent_control_input_race_has_time_and_attempt_bounds(writer, point, frozen_clock):
    lease(writer)
    failure = (OSError(errno.ENODATA, "No data available") if point == "read"
               else FileNotFoundError(errno.ENOENT, "No such file", "lease.json"))
    attempts = []

    def fail(name):
        if name == point:
            attempts.append(name)
            raise failure

    if frozen_clock:
        helper.time.sleep = writer.clock.sleeps.append
    streams = racing(writer, fail)
    with pytest.raises(helper.ControlError, match="did not settle") as caught:
        writer.channel.read("lease.json")
    assert caught.value.__cause__ is failure
    assert 2 <= len(attempts) <= helper._READ_SETTLE_ATTEMPTS
    assert sum(writer.clock.sleeps) <= helper._READ_SETTLE_SECONDS + 1e-9
    assert all(stream.native.closed for stream in streams)


def test_absent_optional_input_and_other_read_failures_are_not_retried(writer):
    lease(writer)
    failure = OSError(errno.EIO, "I/O failed")

    def fail(name):
        if name == "read":
            raise failure

    streams = racing(writer, fail)
    assert writer.channel.read("cancel.json", optional=True) is None
    with pytest.raises(OSError) as caught:
        writer.channel.read("lease.json")
    assert caught.value is failure and not writer.clock.sleeps
    assert all(stream.native.closed for stream in streams)


@pytest.mark.parametrize("children, quiescent", [((), True), ((321,), False), (None, False)])
def test_a_report_without_a_command_is_quiescent_only_without_children(
        writer, monkeypatch, children, quiescent):
    """A helper that refuses at its preflight because its interpreter
    started a child (a .pth hook) does not report itself quiescent.
    Unreadable child membership is not quiescence either."""
    def listed():
        if children is None:
            raise OSError(errno.ENOENT, "no child membership")
        return children

    monkeypatch.setattr(helper, "_children", listed)
    refusal = helper.SupervisionUnavailable("Supervisor must start without children")
    writer.channel.report("FAILED", reason=str(refusal), error=refusal)
    report = json.loads((writer.root / "status.json").read_bytes())
    assert report["command_pid"] is None and report["quiescent"] is quiescent
