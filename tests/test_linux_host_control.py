"""Receipt-bound host control checks without helper or command launches."""
import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from langadapter._linux_command import ControlError, _Control
from langadapter import _linux_job as helper
from langadapter import _linux_command as channel_module
from langadapter._owned_directory import OwnershipRefused


@pytest.fixture(autouse=True)
def no_process(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail("host control tests must not launch or signal a process")

    monkeypatch.setattr(subprocess, "Popen", refused)
    monkeypatch.setattr(subprocess, "run", refused)
    monkeypatch.setattr(os, "kill", refused)


@pytest.fixture
def control(tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    return _Control.create(["native", "exact argument"], cwd="relative/project",
                           parent=str(tmp_path), receipt_parent=str(receipts))


def report(control, **overrides):
    data = {"version": 1, "token": control.owned.token, "helper_pid": 42,
            "state": "COMPLETE", "command_pid": 43, "returncode": 7,
            "quiescent": True, "reason": None}
    data.update(overrides)
    return data


def write_report(control, **overrides):
    data = report(control, **overrides)
    (control.owned.root / "status.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def test_request_and_lease_are_receipt_bound_without_environment(control):
    root = control.owned.root
    assert control.owned.receipt.parent != root
    request = json.loads((root / "request.json").read_text())
    assert request == {"version": 1, "token": control.owned.token,
                       "argv": ["native", "exact argument"], "cwd": "relative/project",
                       "lease_seconds": 60, "grace": 5, "kill_wait": 5}
    assert json.loads((root / "lease.json").read_text())["generation"] == 0
    control.pulse()
    assert json.loads((root / "lease.json").read_text())["generation"] == 1
    control.cancel()
    assert json.loads((root / "cancel.json").read_text()) == {"token": control.owned.token}
    assert not list(root.glob(".control-*"))
    control.owned.verify()


@pytest.mark.parametrize("change", [
    {"argv": []}, {"argv": "command"}, {"argv": [None]}, {"argv": ["a\0b"]},
    {"cwd": ""}, {"cwd": 3}, {"cwd": "a\0b"},
    {"native_environment": []}, {"native_environment": {"A=B": "x"}},
    {"native_environment": {"": "x"}}, {"native_environment": {"A\0": "x"}},
    {"native_environment": {"A": None}}, {"native_environment": {"A": "a\0b"}},
])
def test_invalid_request_is_refused_before_its_dir_exists(tmp_path, change):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    arguments = {"argv": ["native"], **change}
    with pytest.raises(ControlError, match="Invalid"):
        _Control.create(arguments.pop("argv"), parent=str(tmp_path),
                        receipt_parent=str(receipts), **arguments)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["receipts"]
    assert not any(receipts.iterdir())


def test_lease_renewal_waits_on_no_storage_flush(control, monkeypatch):
    """Lease renewal does not fsync, which can wait on unrelated writeback
    longer than the lease under a machine stall. One-time requests flush."""
    from langadapter import _linux_command as _linux_control

    flushed = []
    monkeypatch.setattr(_linux_control.os, "fsync", lambda fd: flushed.append(fd))
    control.pulse()
    assert flushed == []
    control.cancel()
    assert len(flushed) == 1


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_temporary_windows_read_conflict_retries_same_atomic_lease(control, monkeypatch, winerror):
    original = os.replace
    attempts = []
    clock = [0.0]
    failure = PermissionError("mapped reader holds lease")
    failure.winerror = winerror

    def replace(source, destination):
        attempts.append((source, destination))
        if len(attempts) < 3:
            raise failure
        return original(source, destination)

    monkeypatch.setattr(channel_module.os, "replace", replace)
    monkeypatch.setattr(channel_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(channel_module.time, "sleep", lambda seconds:
                        clock.__setitem__(0, clock[0] + seconds))
    control.pulse()
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert json.loads((control.owned.root / "lease.json").read_text())["generation"] == 1
    assert control.generation == 2 and not list(control.owned.root.glob(".control-*"))


@pytest.mark.parametrize("winerror", [5, None, 2])
def test_permanent_lease_write_failure_is_bounded_retains_owner_and_old_generation(
    control, monkeypatch, winerror,
):
    before = (control.owned.root / "lease.json").read_bytes()
    clock = [0.0]
    attempts = []
    failure = PermissionError("persistent replacement refusal")
    failure.winerror = winerror

    def replace(*args):
        attempts.append(args)
        raise failure

    monkeypatch.setattr(channel_module.os, "replace", replace)
    monkeypatch.setattr(channel_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(channel_module.time, "sleep", lambda seconds:
                        clock.__setitem__(0, clock[0] + seconds))
    with pytest.raises(PermissionError) as caught:
        control.pulse()
    assert caught.value is failure and failure._langadapter_control is control.owned
    assert clock[0] <= channel_module._REPLACE_SECONDS
    if winerror == 5:
        assert len(attempts) > 1
    else:
        assert len(attempts) == 1
    assert control.generation == 1
    assert (control.owned.root / "lease.json").read_bytes() == before
    control.owned.verify()


def test_control_rechecks_ownership_before_retrying_windows_replacement(control, monkeypatch):
    failure = PermissionError("reader holds file")
    failure.winerror = 5
    attempts = []

    def replace(*args):
        attempts.append(args)
        raise failure

    def ownership_lost(seconds):
        control.owned.marker.write_text("not the owner", encoding="ascii")

    monkeypatch.setattr(channel_module.os, "replace", replace)
    monkeypatch.setattr(channel_module.time, "sleep", ownership_lost)
    with pytest.raises(OwnershipRefused):
        control.pulse()
    assert len(attempts) == 1 and control.generation == 1


@pytest.mark.parametrize("argv", ["native", [], [1], ["native\0wrong"]])
def test_invalid_request_allocates_nothing(tmp_path, argv):
    with pytest.raises(ControlError):
        _Control.create(argv, parent=str(tmp_path), receipt_parent=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("returncode", [0, 7, -11, -15])
def test_completed_native_exit_is_not_helper_exit(control, returncode):
    data = write_report(control, returncode=returncode)
    assert control.observe(helper_pid=42) == data
    assert control.completed(helper_returncode=0, helper_pid=42)["returncode"] == returncode
    with pytest.raises(ControlError):
        control.completed(helper_returncode=1, helper_pid=42)
    control.owned.verify()  # Confirming completion never deletes observations.


def test_atomic_publication_between_lstat_and_open_keeps_current_identity(control, monkeypatch):
    write_report(control, state="RUNNING", returncode=None, quiescent=False)
    expected = report(control)
    native_open = os.open
    attempts = []

    def publish(path, flags, *args, **kwargs):
        if Path(path).name == "status.json":
            attempts.append(path)
            replacement = control.owned.root / "published.tmp"
            replacement.write_text(json.dumps(expected), encoding="utf-8")
            os.replace(replacement, path)
        return native_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", publish)
    assert control.observe(helper_pid=42) == expected
    assert len(attempts) == 1
    control.owned.verify()


@pytest.mark.parametrize("continuous", [False, True])
def test_report_changes_after_open_yield_bounded_control_without_stale_reads(
        control, monkeypatch, continuous):
    write_report(control, state="RUNNING", returncode=None, quiescent=False)
    expected = report(control)
    native_fstat, native_fdopen, native_lstat = os.fstat, os.fdopen, Path.lstat
    status = control.owned.root / "status.json"
    pending = []
    changes, reads, closed = [], [], []

    def publish(descriptor):
        opened = native_fstat(descriptor)
        if continuous or not changes:
            replacement = control.owned.root / f"published-{len(changes)}.tmp"
            replacement.write_text(json.dumps(expected), encoding="utf-8")
            if sys.platform == "win32":
                # Windows cannot replace an open file: expose the next
                # identity now and publish it on close.
                pending.append(replacement)
            else:
                os.replace(replacement, status)
            changes.append(opened.st_ino)
        return opened

    def current_entry(path, *args, **kwargs):
        if path == status and pending:
            return native_lstat(pending[0], *args, **kwargs)
        return native_lstat(path, *args, **kwargs)

    class Stream:
        def __init__(self, native):
            self.native = native

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.native.close()
            closed.append(True)
            if pending:
                os.replace(pending.pop(), status)

        def fileno(self):
            return self.native.fileno()

        def read(self, size):
            reads.append(size)
            return self.native.read(size)

    monkeypatch.setattr(os, "fstat", publish)
    monkeypatch.setattr(os, "fdopen", lambda *a, **k: Stream(native_fdopen(*a, **k)))
    if sys.platform == "win32":
        monkeypatch.setattr(Path, "lstat", current_entry)
    if continuous:
        with pytest.raises(ControlError, match="did not settle"):
            control.observe(helper_pid=42)
        assert len(changes) == len(closed) == channel_module._REPORT_ATTEMPTS
        assert not reads
    else:
        assert control.observe(helper_pid=42) == expected
        assert len(changes) == 1 and len(closed) == 2
        assert reads == [channel_module._LIMIT + 1]
    control.owned.verify()


class RacingStream:
    """A report stream whose read can fail as during a drvfs rename."""

    def __init__(self, native, fail, streams):
        self.native, self.fail = native, fail
        streams.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.native.close()

    def fileno(self):
        return self.native.fileno()

    def read(self, size):
        self.fail("read")
        return self.native.read(size)


def fake_clock(monkeypatch, *, frozen=False):
    clock, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        if not frozen:
            clock[0] += seconds

    monkeypatch.setattr(channel_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(channel_module.time, "sleep", sleep)
    return sleeps


@pytest.mark.parametrize("point", ["open", "entry", "read"])
def test_report_replacement_seen_missing_or_without_data_is_read_again(
        control, monkeypatch, point):
    # On WSL's mount of a Windows drive (drvfs), a read racing the atomic
    # rename can find the name missing or the file without data.
    expected = write_report(control)
    status = control.owned.root / "status.json"
    native_open, native_fdopen, native_lstat = os.open, os.fdopen, Path.lstat
    failures, entries, streams = [], [], []

    def fail(name):
        if name == point and not failures:
            failures.append(OSError(errno.ENODATA, "No data available") if name == "read"
                            else FileNotFoundError(errno.ENOENT, "No such file", str(status)))
            raise failures[-1]

    def opened(path, *args, **kwargs):
        if Path(path) == status:
            fail("open")
        return native_open(path, *args, **kwargs)

    def entry(path, *args, **kwargs):
        if path == status:
            entries.append(path)
            if len(entries) == 2:
                fail("entry")
        return native_lstat(path, *args, **kwargs)

    sleeps = fake_clock(monkeypatch)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fdopen", lambda *a, **k: RacingStream(native_fdopen(*a, **k),
                                                                   fail, streams))
    monkeypatch.setattr(Path, "lstat", entry)
    assert control.observe(helper_pid=42) == expected
    assert len(failures) == 1 and sleeps == [0.01]
    assert all(stream.native.closed for stream in streams)
    control.owned.verify()


@pytest.mark.parametrize("frozen", [False, True])
def test_persistent_report_race_has_time_and_attempt_bounds(control, monkeypatch, frozen):
    write_report(control)
    native_fdopen = os.fdopen
    failure = OSError(errno.ENODATA, "No data available")
    streams = []

    def fail(name):
        raise failure

    sleeps = fake_clock(monkeypatch, frozen=frozen)
    monkeypatch.setattr(os, "fdopen", lambda *a, **k: RacingStream(native_fdopen(*a, **k),
                                                                   fail, streams))
    with pytest.raises(ControlError, match="did not settle") as caught:
        control.observe(helper_pid=42)
    assert caught.value.__cause__ is failure
    assert 2 <= len(streams) <= helper._READ_SETTLE_ATTEMPTS
    assert sum(sleeps) <= helper._READ_SETTLE_SECONDS + 1e-9
    assert all(stream.native.closed for stream in streams)
    control.owned.verify()


@pytest.mark.parametrize("point", ["open", "read"])
def test_other_report_read_failures_are_not_retried(control, monkeypatch, point):
    write_report(control)
    status = control.owned.root / "status.json"
    native_open, native_fdopen = os.open, os.fdopen
    failure = (PermissionError(errno.EACCES, "denied") if point == "open"
               else OSError(errno.EIO, "I/O failed"))
    streams = []

    def fail(name):
        if name == point:
            raise failure

    def opened(path, *args, **kwargs):
        if Path(path) == status:
            fail("open")
        return native_open(path, *args, **kwargs)

    sleeps = fake_clock(monkeypatch)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fdopen", lambda *a, **k: RacingStream(native_fdopen(*a, **k),
                                                                   fail, streams))
    with pytest.raises(OSError) as caught:
        control.observe(helper_pid=42)
    assert caught.value is failure and not sleeps
    assert all(stream.native.closed for stream in streams)
    control.owned.verify()


@pytest.mark.parametrize("field,value", [("token", "foreign"), ("helper_pid", 99)])
def test_atomic_publication_never_retries_wrong_identity(control, monkeypatch, field, value):
    write_report(control)
    native_open = os.open
    attempts = []

    def publish(path, flags, *args, **kwargs):
        if Path(path).name == "status.json":
            attempts.append(path)
            replacement = control.owned.root / "published.tmp"
            replacement.write_text(json.dumps(report(control, **{field: value})), encoding="utf-8")
            os.replace(replacement, path)
        return native_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", publish)
    with pytest.raises(ControlError, match="identity or format"):
        control.observe(helper_pid=42)
    assert len(attempts) == 1
    control.owned.verify()


@pytest.mark.parametrize("field,value", [("st_mode", stat.S_IFLNK),
                                        ("st_size", channel_module._LIMIT + 1)])
def test_changed_status_path_must_still_be_bounded_and_regular(
        control, monkeypatch, field, value):
    write_report(control)
    status = control.owned.root / "status.json"
    native_lstat, native_open = Path.lstat, os.open
    entries, opened = [], []

    def current_entry(path, *args, **kwargs):
        entry = native_lstat(path, *args, **kwargs)
        if path == status:
            entries.append(entry)
            if len(entries) == 2:
                changed = SimpleNamespace(st_mode=entry.st_mode, st_size=entry.st_size,
                                          st_dev=entry.st_dev, st_ino=entry.st_ino)
                setattr(changed, field, value)
                return changed
        return entry

    def tracked_open(path, *args, **kwargs):
        descriptor = native_open(path, *args, **kwargs)
        if Path(path) == status:
            opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(Path, "lstat", current_entry)
    monkeypatch.setattr(os, "open", tracked_open)
    with pytest.raises(ControlError, match="bounded regular file"):
        control.observe(helper_pid=42)
    assert len(entries) == 2 and len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    control.owned.verify()


@pytest.mark.parametrize("override", [
    {"version": True}, {"version": 2}, {"token": "wrong"}, {"extra": 1},
    {"helper_pid": False}, {"helper_pid": 0}, {"helper_pid": 2147483648},
    {"state": "other"}, {"state": []}, {"quiescent": False},
])
def test_bad_terminal_reports_cannot_confirm_completion(control, override):
    write_report(control, **override)
    with pytest.raises(ControlError):
        control.completed(helper_returncode=0, helper_pid=42)
    control.owned.verify()


@pytest.mark.parametrize("state,pid,quiescent", [("READY", None, True), ("RUNNING", 43, False)])
def test_startup_report_does_not_prove_completion(control, state, pid, quiescent):
    data = write_report(control, state=state, command_pid=pid, returncode=None,
                        quiescent=quiescent)
    assert control.observe(helper_pid=42) == data
    with pytest.raises(ControlError):
        control.completed(helper_returncode=0, helper_pid=42)


def test_failed_report_keeps_native_and_error_evidence(control):
    error = {"type": "PermissionError", "message": "pidfd refused",
             "errno": 1, "filename": "/controlled/native"}
    data = write_report(control, state="FAILED", reason="pidfd refused", error=error)
    assert control.completed(helper_returncode=1, helper_pid=42) == data
    with pytest.raises(ControlError):
        control.completed(helper_returncode=0, helper_pid=42)
    write_report(control, state="FAILED", reason="still active", error=error, quiescent=False)
    with pytest.raises(ControlError):
        control.completed(helper_returncode=1, helper_pid=42)
    control.owned.verify()


def test_cancelled_report_is_not_native_success(control):
    write_report(control, state="CANCELLED", reason="host control lease expired", returncode=-9)
    data = control.completed(helper_returncode=0, helper_pid=42)
    assert data["state"] == "CANCELLED" and data["returncode"] == -9
    control.owned.verify()


def test_prelaunch_cancellation_has_no_fabricated_native_identity_or_code(control):
    write_report(control, state="CANCELLED", reason="cancelled before native start",
                 command_pid=None, returncode=None)
    data = control.completed(helper_returncode=0, helper_pid=42)
    assert data["command_pid"] is None and data["returncode"] is None
    control.owned.verify()


@pytest.mark.parametrize("quiescent", [False, True])
def test_native_exit_report_is_not_helper_completion(control, quiescent):
    write_report(control, state="EXITED", quiescent=quiescent)
    assert control.observe(helper_pid=42)["returncode"] == 7
    with pytest.raises(ControlError, match="does not confirm"):
        control.completed(helper_returncode=0, helper_pid=42)


def test_missing_or_wrong_helper_identity_refuses_release(control):
    assert control.observe() is None
    with pytest.raises(ControlError):
        control.completed(helper_returncode=0)
    write_report(control)
    with pytest.raises(ControlError):
        control.observe(helper_pid=44)
    with pytest.raises(ControlError):
        control.observe(helper_pid=True)
    control.owned.verify()


@pytest.mark.parametrize("data", [b"{}", b"[]", b"invalid", b"\xff", b"x" * (2 * 1024 * 1024 + 1)],
                         ids=["empty", "array", "malformed", "encoding", "oversized"])
def test_unreadable_or_oversized_report_is_not_completion(control, data):
    (control.owned.root / "status.json").write_bytes(data)
    with pytest.raises(ControlError):
        control.completed(helper_returncode=0)


def test_foreign_report_link_is_never_read_or_removed(control, tmp_path):
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps(report(control)), encoding="utf-8")
    path = control.owned.root / "status.json"
    try:
        path.symlink_to(foreign)
    except OSError as error:
        pytest.skip(f"host symlinks unavailable: {error}")
    with pytest.raises(ControlError, match="regular file"):
        control.completed(helper_returncode=0)
    assert foreign.exists()
    control.owned.verify()


def test_request_receipt_replacement_stops_host_updates(control):
    root = control.owned.root
    before = (root / "lease.json").read_bytes()
    control.owned.receipt.write_text("{}", encoding="utf-8")
    with pytest.raises(OwnershipRefused):
        control.pulse()
    assert control.generation == 1 and (root / "lease.json").read_bytes() == before


def test_failed_atomic_update_retains_owner_and_lease(control, monkeypatch):
    before = (control.owned.root / "lease.json").read_bytes()
    error = OSError("replace refused")
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises(OSError) as caught:
        control.pulse()
    assert caught.value is error and error._langadapter_control is control.owned
    assert control.generation == 1
    assert (control.owned.root / "lease.json").read_bytes() == before
    assert not list(control.owned.root.glob(".control-*"))
    control.owned.verify()


def test_replaced_root_does_not_delete_a_foreign_temporary(control, monkeypatch):
    root, kept = control.owned.root, control.owned.root.with_name("original-control")
    error = KeyboardInterrupt("writer interrupted after root replacement")
    foreign = []

    def replace(source, destination):
        root.rename(kept)
        root.mkdir()
        replacement = root / Path(source).name
        replacement.write_text("foreign bytes", encoding="utf-8")
        foreign.append(replacement)
        raise error

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(KeyboardInterrupt) as caught:
        control.pulse()
    assert caught.value is error and foreign[0].read_text() == "foreign bytes"
    assert (kept / foreign[0].name).exists()
    assert control.owned.receipt.exists()


@pytest.mark.parametrize("operation", ["pulse", "observe"])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_failed_stream_open_closes_descriptor_without_replacing_primary(
        control, monkeypatch, operation, cleanup_failure):
    write_report(control)
    before = (control.owned.root / "lease.json").read_bytes()
    primary = KeyboardInterrupt("stream open interrupted")
    cleanup = OSError("descriptor close refused after closing")
    closed = []
    close = os.close

    def fail_open(*args, **kwargs):
        raise primary

    def close_owned(descriptor):
        close(descriptor)
        closed.append(descriptor)
        if cleanup_failure:
            raise cleanup

    monkeypatch.setattr(os, "fdopen", fail_open)
    monkeypatch.setattr(os, "close", close_owned)
    with pytest.raises(KeyboardInterrupt) as caught:
        getattr(control, operation)()
    assert caught.value is primary and len(closed) == 1
    assert (control.owned.root / "lease.json").read_bytes() == before
    assert not list(control.owned.root.glob(".control-*"))
    if cleanup_failure and hasattr(primary, "add_note"):
        # Exceptions carry notes from Python 3.11.
        assert "descriptor close refused" in " ".join(primary.__notes__)
    control.owned.verify()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux directory-fd helper protocol")
@pytest.mark.parametrize("mode", ["zero", "nonzero", "signal", "failed_start", "cancel",
                                  "cancel_before_start"])
def test_host_and_helper_share_real_file_protocol_without_launch(control, mode):
    code = {"zero": 0, "nonzero": 7, "signal": -11, "cancel": -15}.get(mode)
    if mode == "cancel_before_start":
        control.cancel()

    class Job:
        process = None
        returncode = None
        quiescent = True

        def start(self, argv, *, cwd):
            assert argv == ["native", "exact argument"] and cwd == "relative/project"
            self.quiescent = False
            if mode == "failed_start":
                raise FileNotFoundError(2, "native absent", "native")
            self.process = SimpleNamespace(pid=43)
            if mode == "cancel":
                control.cancel()

        def poll(self):
            self.returncode = None if mode == "cancel" else code
            return self.returncode

        def close(self, **bounds):
            self.quiescent = True
            self.returncode = code

    cancel = SimpleNamespace(signal=None, install=lambda: {}, restore=lambda _: None)
    channel = helper._Channel(str(control.owned.root), control.owned.token)
    result = helper._serve(channel, job_factory=Job, cancellation=cancel)
    observation = control.completed(helper_returncode=result, helper_pid=os.getpid())
    assert observation["returncode"] == code
    assert observation["state"] == ("FAILED" if mode == "failed_start" else
                                    "CANCELLED" if mode in {"cancel", "cancel_before_start"}
                                    else "COMPLETE")
    if mode == "failed_start":
        assert observation["command_pid"] is None and result == 1
        assert observation["error"]["type"] == "FileNotFoundError"
        assert observation["error"]["errno"] == 2
    control.owned.verify()


def test_a_terminal_report_carries_the_commands_own_duration(control):
    write_report(control, elapsed_ns=123_456_789)
    assert control.completed(helper_returncode=0, helper_pid=42)["elapsed_ns"] == 123_456_789


def test_a_terminal_report_carries_the_supervision_mode(control):
    write_report(control, supervision="process_group")
    assert control.completed(helper_returncode=0, helper_pid=42)["supervision"] == "process_group"


