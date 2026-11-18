"""Linux child accounting; native execution is opt-in and supervisor-only."""
import ctypes
import io
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from langadapter import _linux_job as jobs


@pytest.fixture
def kernel(monkeypatch):
    events = []
    waiting = []
    children = []
    clock = [0.0]

    def wait(pid, flags):
        events.append(("wait", pid, flags))
        value = waiting.pop(0) if waiting else ChildProcessError()
        if isinstance(value, BaseException):
            raise value
        return value

    def launch(argv, **options):
        events.append(("start", argv, options))
        return SimpleNamespace(pid=42, returncode=None)

    def sleep(duration):
        clock[0] += duration

    def unavailable(*args, **kwargs):
        pytest.fail("Raw process launch is forbidden in accounting fixtures")

    monkeypatch.setattr(jobs, "_prepare", lambda: events.append(("prepare",)) or "pidfd")
    monkeypatch.setattr(jobs.subprocess, "Popen", launch)
    monkeypatch.setattr(jobs.os, "waitpid", wait, raising=False)
    monkeypatch.setattr(jobs.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(jobs.os, "waitstatus_to_exitcode", lambda status:
                        -(status & 127) if status & 127 else status >> 8, raising=False)
    monkeypatch.setattr(jobs.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(jobs.os, "pidfd_open", lambda pid, flags: events.append(
        ("open", pid, flags)) or pid + 100, raising=False)
    monkeypatch.setattr(jobs.os, "close", lambda fd: events.append(("close", fd)))
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", lambda fd, sig, info, flags:
                        events.append(("signal", fd, sig, info, flags)), raising=False)
    monkeypatch.setattr(jobs, "_children", lambda: tuple(children))
    monkeypatch.setattr(jobs.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(jobs.time, "sleep", sleep)
    monkeypatch.setattr(jobs.subprocess, "run", unavailable)
    return SimpleNamespace(events=events, waiting=waiting, children=children, clock=clock)


def command(kernel):
    job = jobs._OwnedCommand()
    job.start(["native", "a b", "--flag=x"], cwd="/project", env={"PATH": "/tools"})
    return job


@pytest.fixture
def group(kernel, monkeypatch):
    """The kernel without process descriptors: the command's exit is seen
    by waitid without reaping it, and a reaped child leaves the membership."""
    exited = []

    def waitid(idtype, ident, options):
        kernel.events.append(("waitid", ident, options))
        if isinstance(exited[0] if exited else None, BaseException):
            raise exited[0]
        return exited[0] if exited else None

    def wait(pid, flags):
        kernel.events.append(("wait", pid, flags))
        value = kernel.waiting.pop(0) if kernel.waiting else ChildProcessError()
        if isinstance(value, BaseException):
            raise value
        if value[0] in kernel.children:
            kernel.children.remove(value[0])
        return value

    monkeypatch.setattr(jobs, "_prepare", lambda: kernel.events.append(("prepare",))
                        or "process_group")
    for name, value in (("P_PID", 1), ("WEXITED", 4), ("WNOWAIT", 0x1000000),
                        ("CLD_EXITED", 1)):
        monkeypatch.setattr(jobs.os, name, value, raising=False)
    monkeypatch.setattr(jobs.os, "waitid", waitid, raising=False)
    monkeypatch.setattr(jobs.os, "waitpid", wait, raising=False)
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: kernel.events.append(
        ("killpg", pgid, sig)), raising=False)
    monkeypatch.setattr(jobs.os, "kill", lambda pid, sig: kernel.events.append(("kill", pid, sig)))
    monkeypatch.setattr(jobs.signal, "SIGCHLD", 17, raising=False)
    monkeypatch.setattr(jobs.signal, "SIG_BLOCK", 0, raising=False)
    monkeypatch.setattr(jobs.signal, "pthread_sigmask", lambda how, mask: kernel.events.append(
        ("mask", how, set(mask))), raising=False)
    monkeypatch.setattr(jobs.signal, "sigtimedwait", lambda signals, seconds: kernel.events.append(
        ("sigwait", tuple(signals), seconds)), raising=False)
    kernel.exited = exited
    return kernel


def exit_of(code=None, killed_by=None):
    """A waitid record: exited with ``code`` or killed by ``killed_by``."""
    return SimpleNamespace(si_code=1 if killed_by is None else 2,
                           si_status=code if killed_by is None else killed_by)


def accounting(kernel):
    return [event for event in kernel.events if event[0] in {"killpg", "kill", "wait", "signal"}]


def test_without_process_descriptors_the_command_leads_its_own_group(group):
    job = command(group)
    assert job.mode == "process_group"
    assert group.events[:3] == [
        ("prepare",), ("start", ["native", "a b", "--flag=x"],
                       {"cwd": "/project", "env": {"PATH": "/tools"}, "close_fds": True,
                        "start_new_session": True}),
        ("mask", 0, {17})]
    job.wait_exit(0.02)
    assert group.events[-1] == ("sigwait", (17,), 0.02)


@pytest.mark.parametrize("record,expected", [(exit_of(7), 7), (exit_of(0), 0),
                                             (exit_of(killed_by=15), -15)])
def test_the_group_leader_is_seen_exited_unreaped_and_reaped_last(group, record, expected):
    job = command(group)
    group.children[:] = [42, 99]
    group.exited.append(record)
    group.waiting.extend([(0, 0)])
    assert job.poll() == expected and job.process.returncode == expected
    assert not job.quiescent and job.elapsed_ns is not None
    assert ("waitid", 42, 4 | 1 | 0x1000000) in group.events
    group.children.append(77)
    group.waiting.extend([(99, 0), (77, 0), (42, 0)])
    assert job.poll() == expected and job.quiescent
    assert accounting(group) == [("wait", 99, 1), ("wait", 99, 1), ("wait", 77, 1),
                                 ("wait", 42, 1), ("wait", -1, 1)]


def test_group_cleanup_signals_the_group_while_its_leader_is_unreaped(group):
    job = command(group)
    group.children[:] = [42, 99]
    group.exited.append(exit_of(killed_by=15))
    group.waiting.extend([(99, 15), (42, 15)])
    job.close(grace=0.02, kill_wait=0.06)
    assert accounting(group) == [("killpg", 42, signal.SIGTERM), ("kill", 99, signal.SIGTERM),
                                 ("wait", 99, 1), ("wait", 42, 1), ("wait", -1, 1)]
    assert job.returncode == -15 and job.quiescent


def test_after_the_leader_is_reaped_only_listed_children_are_signaled(group):
    job = command(group)
    group.children[:] = [42]
    group.exited.append(exit_of(0))
    group.waiting.extend([(42, 0), (0, 0)])
    assert job.poll() == 0 and not job.quiescent
    group.children[:] = [77]
    group.waiting.extend([(77, 9)])
    job.close(grace=0, kill_wait=0.02)
    assert job.quiescent
    reaped = accounting(group).index(("wait", 42, 1))
    assert not any(event[0] == "killpg" for event in accounting(group)[reaped:])
    assert ("kill", 77, signal.SIGKILL) in accounting(group)


def test_a_lost_group_leader_is_not_reconstructed_as_success(group):
    job = command(group)
    group.exited.append(ChildProcessError())
    with pytest.raises(jobs.ChildAccountingError, match="exit status was lost"):
        job.poll()
    assert job.returncode is None and not job.quiescent
    # The group's ID is no longer reserved: cleanup never signals it and refuses.
    with pytest.raises(jobs.ChildAccountingError):
        job.close(grace=0, kill_wait=0.02)
    assert not job.quiescent
    assert not any(event[0] == "killpg" for event in group.events)


def test_a_group_whose_members_outlive_the_bounds_is_refused(group):
    job = command(group)
    group.children[:] = [42, 99]
    group.exited.append(exit_of(0))
    group.waiting.extend([(0, 0)] * 200)
    with pytest.raises(jobs.ChildAccountingError, match="did not stop"):
        job.close(grace=0, kill_wait=0.02)
    assert not job.quiescent and job.returncode == 0


def test_prepare_precedes_native_launch_with_unchanged_arguments(kernel):
    job = command(kernel)
    assert kernel.events[:2] == [
        ("prepare",), ("start", ["native", "a b", "--flag=x"],
                       {"cwd": "/project", "env": {"PATH": "/tools"}, "close_fds": True}),
    ]
    assert not job.quiescent
    with pytest.raises(RuntimeError, match="one command"):
        job.start(["second"])


@pytest.mark.parametrize("argv", [[], "command", b"command", [None], ["a\0b"]])
def test_malformed_argv_never_starts_a_command(kernel, argv):
    job = jobs._OwnedCommand()
    with pytest.raises(ValueError, match="argv"):
        job.start(argv)
    assert kernel.events == [("prepare",)]


@pytest.mark.parametrize("status,expected", [(7 << 8, 7), (0, 0), (11, -11), (15, -15)])
def test_native_exit_identity_survives_descendant_reaping(kernel, status, expected):
    job = command(kernel)
    kernel.waiting.extend([(42, status), (99, 0), ChildProcessError()])
    assert job.poll() == expected
    assert job.process.returncode == expected
    assert job.quiescent
    assert not any(event[0] == "signal" for event in kernel.events)


def test_empty_procfs_does_not_claim_quiescence_with_live_child(kernel):
    job = command(kernel)
    kernel.waiting.extend([(42, 0), (0, 0), (0, 0)])
    assert job.poll() == 0
    assert not job.quiescent
    with pytest.raises(jobs.ChildAccountingError, match="did not stop"):
        job.close(grace=0, kill_wait=0)
    assert not job.quiescent
    job.close(grace=0, kill_wait=0)
    assert job.quiescent


def test_missing_native_exit_is_not_reconstructed_as_success(kernel):
    job = command(kernel)
    with pytest.raises(jobs.ChildAccountingError, match="exit status was lost"):
        job.poll()
    assert job.returncode is None
    assert not job.quiescent


def test_cleanup_uses_only_direct_children_and_closes_pidfds(kernel):
    job = command(kernel)
    kernel.children[:] = [42, 99]
    kernel.waiting.extend([(42, 15), (99, 15), ChildProcessError()])
    job.close(grace=0.02, kill_wait=0.06)
    # The command's own pidfd, opened at its start to wake at its exit and
    # closed before cleanup, is never used to signal.
    assert [event for event in kernel.events if event[0] in {"open", "signal", "close"}] == [
        ("open", 42, 0), ("close", 142),
        ("open", 42, 0), ("signal", 142, signal.SIGTERM, None, 0), ("close", 142),
        ("open", 99, 0), ("signal", 199, signal.SIGTERM, None, 0), ("close", 199),
    ]
    assert job.returncode == -15
    assert job.quiescent


def test_kill_escalation_accounts_newly_adopted_children(kernel, monkeypatch):
    job = command(kernel)
    kernel.children[:] = [42]
    kernel.waiting.extend([(42, 15), (0, 0), (99, 9), ChildProcessError()])
    original = jobs.signal.pidfd_send_signal

    def send(fd, sig, info, flags):
        original(fd, sig, info, flags)
        kernel.children[:] = [99]

    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", send)
    job.close(grace=0.02, kill_wait=0.06)
    assert ("signal", 142, signal.SIGTERM, None, 0) in kernel.events
    assert ("signal", 199, signal.SIGKILL, None, 0) in kernel.events
    assert job.quiescent


@pytest.mark.parametrize("operation", ["open", "signal", "close"])
def test_cleanup_failure_retains_unconfirmed_state_and_can_retry(kernel, monkeypatch, operation):
    job = command(kernel)
    kernel.children[:] = [42]
    kernel.waiting.extend([(42, 0), ChildProcessError()])
    target = {"open": (jobs.os, "pidfd_open"), "signal": (jobs.signal, "pidfd_send_signal"),
              "close": (jobs.os, "close")}[operation]
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise PermissionError("accounting refused")
        patch.setattr(*target, fail)
        with pytest.raises(PermissionError, match="accounting refused"):
            job.close(grace=0.02, kill_wait=0.06)
    assert not job.quiescent
    job.close(grace=0.02, kill_wait=0.06)
    assert job.quiescent


def test_interrupted_wait_retries_without_signaling_foreign_processes(kernel):
    job = command(kernel)
    kernel.waiting.extend([InterruptedError(), (42, 0), ChildProcessError()])
    assert job.poll() == 0
    assert job.quiescent


def test_vanished_children_never_fall_back_to_numeric_pid_signals(kernel, monkeypatch):
    job = command(kernel)
    kernel.children[:] = [42, 99]
    kernel.waiting.extend([(42, 0), ChildProcessError()])

    def gone(*args):
        raise ProcessLookupError()

    monkeypatch.setattr(jobs.os, "pidfd_open", gone)
    job.close(grace=0.02, kill_wait=0.06)
    assert not any(event[0] == "signal" for event in kernel.events)
    assert job.quiescent


def test_vanished_pidfd_target_is_closed_without_a_numeric_signal(kernel, monkeypatch):
    job = command(kernel)
    kernel.children[:] = [42]
    kernel.waiting.extend([(42, 0), ChildProcessError()])

    def gone(*args):
        raise ProcessLookupError()

    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", gone)
    job.close(grace=0.02, kill_wait=0.06)
    assert ("close", 142) in kernel.events
    assert job.quiescent


def test_interrupted_signal_closes_pidfd_and_does_not_claim_completion(kernel, monkeypatch):
    job = command(kernel)
    kernel.children[:] = [42]
    kernel.waiting[:] = [(0, 0)]

    def interrupt(*args):
        raise KeyboardInterrupt("supervisor interrupted")

    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", interrupt)
    with pytest.raises(KeyboardInterrupt, match="supervisor interrupted"):
        job.close(grace=0.02, kill_wait=0.06)
    assert ("close", 142) in kernel.events
    assert not job.quiescent


def test_failed_exec_has_no_child_and_cannot_be_retried_as_new_work(kernel, monkeypatch):
    job = jobs._OwnedCommand()
    original = FileNotFoundError("native executable missing")

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(jobs.subprocess, "Popen", fail)
    with pytest.raises(FileNotFoundError) as caught:
        job.start(["missing"])
    assert caught.value is original
    assert job.process is None
    job.close(grace=0, kill_wait=0)
    assert job.quiescent
    with pytest.raises(RuntimeError, match="one command"):
        job.start(["other"])


def test_procfs_membership_is_limited_to_own_supervisor_task(monkeypatch):
    paths = []
    monkeypatch.setattr(jobs.os, "getpid", lambda: 42)
    monkeypatch.setattr(Path, "open", lambda path, mode: paths.append(
        (str(path), mode)) or io.BytesIO(b"99 100\n"))
    assert jobs._children() == (99, 100)
    assert paths == [(str(Path("/proc/self/task/42/children")), "rb")]


def test_interrupted_creation_invalidates_quiescence_before_native_call(kernel, monkeypatch):
    job = jobs._OwnedCommand()
    original = KeyboardInterrupt("creation interrupted after child start")

    def launch(*args, **kwargs):
        assert not job.quiescent
        kernel.children[:] = [42]
        raise original

    monkeypatch.setattr(jobs.subprocess, "Popen", launch)
    with pytest.raises(KeyboardInterrupt) as caught:
        job.start(["native"])
    assert caught.value is original
    assert job.process is None
    assert not job.quiescent
    kernel.waiting.extend([(42, 9), ChildProcessError()])
    job.close(grace=0, kill_wait=1)
    assert ("signal", 142, signal.SIGKILL, None, 0) in kernel.events
    assert job.quiescent
    assert job.returncode is None


def test_poll_backlog_is_batched_without_inventing_completion(kernel):
    job = command(kernel)
    kernel.waiting[:] = [(99, 0)] * 100 + [(42, 7 << 8), ChildProcessError()]
    assert job.poll() is None
    assert not job.quiescent
    assert len([event for event in kernel.events if event[0] == "wait"]) == job._BATCH
    assert job.poll() == 7
    assert job.quiescent


def test_continuing_wait_interrupts_yield_to_control_liveness(kernel):
    job = command(kernel)
    kernel.waiting[:] = [InterruptedError()] * 100 + [(42, 0), ChildProcessError()]
    assert job.poll() is None
    assert not job.quiescent
    assert len(kernel.waiting) == 38
    assert job.poll() == 0
    assert job.quiescent


def test_cleanup_signals_before_bounded_backlog_and_keeps_unknown_owner(kernel, monkeypatch):
    job = command(kernel)
    kernel.children[:] = [42]
    waits = []

    def continuing(pid, flags):
        waits.append(kernel.clock[0])
        kernel.clock[0] += 0.01
        return (99, 0) if len(waits) < 100 else (0, 0)

    monkeypatch.setattr(jobs.os, "waitpid", continuing)
    with pytest.raises(jobs.ChildAccountingError, match="did not stop"):
        job.close(grace=0.01, kill_wait=0.01)
    assert ("signal", 142, signal.SIGTERM, None, 0) in kernel.events
    assert ("signal", 142, signal.SIGKILL, None, 0) in kernel.events
    assert len(waits) <= 4
    assert kernel.clock[0] <= 0.04
    assert not job.quiescent
    assert job.process is not None


def test_signal_membership_pass_is_bounded_and_has_no_numeric_pid_fallback(kernel):
    job = command(kernel)
    kernel.children[:] = list(range(100, 200))
    job._signal_children(signal.SIGTERM)
    signals = [event for event in kernel.events if event[0] == "signal"]
    assert len(signals) == job._BATCH
    assert len([event for event in kernel.events if event[0] == "close"]) == job._BATCH


@pytest.fixture
def prerequisites(monkeypatch):
    events = []
    monkeypatch.setattr(jobs.sys, "platform", "linux")
    monkeypatch.setattr(jobs.signal, "SIGCHLD", 17, raising=False)
    monkeypatch.setattr(jobs.os, "getpid", lambda: 42)
    monkeypatch.setattr(Path, "iterdir", lambda path: iter([Path("/proc/self/task/42")]))
    monkeypatch.setattr(jobs, "_children", lambda: ())
    def wait(*args):
        raise ChildProcessError()
    monkeypatch.setattr(jobs.os, "waitpid", wait, raising=False)
    monkeypatch.setattr(jobs.os, "WNOHANG", 1, raising=False)
    monkeypatch.setattr(jobs.signal, "signal", lambda *args: events.append(("disposition", *args)))
    monkeypatch.setattr(jobs.signal, "getsignal", lambda *args: signal.SIG_DFL)
    def prctl(option, value):
        events.append(("prctl", option))
        if option == 37:
            ctypes.c_int.from_address(value).value = 1
    monkeypatch.setattr(jobs, "_prctl", prctl)
    monkeypatch.setattr(jobs.os, "pidfd_open", lambda *args: events.append(("pidfd", *args)) or 100,
                        raising=False)
    monkeypatch.setattr(jobs.signal, "pidfd_send_signal", lambda *args:
                        events.append(("probe", *args)), raising=False)
    monkeypatch.setattr(jobs.os, "close", lambda fd: events.append(("close", fd)))
    for name in ("waitid", "killpg"):
        monkeypatch.setattr(jobs.os, name, lambda *args: None, raising=False)
    for name in ("pthread_sigmask", "sigtimedwait"):
        monkeypatch.setattr(jobs.signal, name, lambda *args: None, raising=False)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *args, **kwargs:
                        pytest.fail("Prerequisite checks must not start a subject"))
    return events


def test_preflight_verifies_adoption_and_actual_pidfd_facilities(prerequisites):
    assert jobs._prepare() == "pidfd"
    assert prerequisites == [
        ("disposition", signal.SIGCHLD, signal.SIG_DFL), ("prctl", 36),
        ("prctl", 37), ("pidfd", 42, 0), ("probe", 100, 0, None, 0), ("close", 100),
    ]


@pytest.mark.parametrize("missing", ["platform", "python", "threads", "children", "reaper",
                                     "adoption", "waitid", "sigmask", "procfs", "auto_reap",
                                     "no_membership"])
def test_missing_prerequisites_never_launch_subject(prerequisites, monkeypatch, missing):
    if missing == "platform":
        monkeypatch.setattr(jobs.sys, "platform", "darwin")
    elif missing == "python":
        monkeypatch.setattr(jobs.sys, "version_info", (3, 9))
    elif missing == "threads":
        monkeypatch.setattr(Path, "iterdir", lambda path: iter([Path("42"), Path("43")]))
    elif missing == "children":
        monkeypatch.setattr(jobs, "_children", lambda: (99,))
    elif missing == "reaper":
        monkeypatch.setattr(jobs.os, "waitpid", lambda *args: (0, 0))
    elif missing == "adoption":
        monkeypatch.setattr(jobs, "_prctl", lambda *args: None)
    elif missing == "waitid":
        monkeypatch.setattr(jobs.os, "waitid", None)
    elif missing == "sigmask":
        monkeypatch.setattr(jobs.signal, "sigtimedwait", None)
    elif missing == "auto_reap":
        monkeypatch.setattr(jobs.signal, "getsignal", lambda *args: signal.SIG_IGN)
    elif missing == "no_membership":
        # A kernel without procfs child accounting.
        def absent():
            raise FileNotFoundError(2, "No such file or directory", "/proc/1/task/1/children")
        monkeypatch.setattr(jobs, "_children", absent)
    else:
        def denied(*args):
            raise PermissionError("procfs denied")
        monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(jobs.SupervisionUnavailable) as caught:
        jobs._OwnedCommand()
    if missing == "no_membership":
        assert "child accounting" in str(caught.value)


def test_kernel_prerequisite_refusal_is_explicit(prerequisites, monkeypatch):
    def denied(*args):
        raise PermissionError("kernel denied")
    monkeypatch.setattr(jobs, "_prctl", denied)
    with pytest.raises(jobs.SupervisionUnavailable, match="kernel denied"):
        jobs._OwnedCommand()


@pytest.mark.parametrize("facility", ["pidfd_absent", "send_absent", "pidfd", "send"])
def test_without_process_descriptors_the_group_mode_is_chosen(prerequisites, monkeypatch,
                                                              facility):
    """A Python built without them, an older kernel or a container's system
    call filter selects the group mode."""
    def denied(*args):
        raise PermissionError("kernel denied")
    target, value = {"pidfd_absent": ((jobs.os, "pidfd_open"), None),
                     "send_absent": ((jobs.signal, "pidfd_send_signal"), None),
                     "pidfd": ((jobs.os, "pidfd_open"), denied),
                     "send": ((jobs.signal, "pidfd_send_signal"), denied)}[facility]
    monkeypatch.setattr(*target, value)
    assert jobs._prepare() == "process_group"
    assert ("prctl", 37) in prerequisites
    if facility == "send":
        assert prerequisites[-1] == ("close", 100)


def test_prctl_uses_full_width_varargs_and_keeps_errno(monkeypatch):
    calls = []

    def native(*args):
        calls.append(args)
        return -1

    monkeypatch.setattr(jobs.ctypes, "CDLL", lambda *args, **kwargs:
                        SimpleNamespace(prctl=native))
    monkeypatch.setattr(jobs.ctypes, "get_errno", lambda: 13)
    with pytest.raises(PermissionError) as caught:
        jobs._prctl(37, 0x123456789)
    assert caught.value.errno == 13
    assert native.restype is ctypes.c_int
    assert isinstance(calls[0][0], ctypes.c_int)
    assert all(isinstance(value, ctypes.c_size_t) for value in calls[0][1:])
    assert [value.value for value in calls[0]] == [37, 0x123456789, 0, 0, 0]


def test_prctl_success_does_not_invent_an_error_from_old_errno(monkeypatch):
    def native(*args):
        return 0

    monkeypatch.setattr(jobs.ctypes, "CDLL", lambda *args, **kwargs:
                        SimpleNamespace(prctl=native))
    monkeypatch.setattr(jobs.ctypes, "get_errno", lambda: 13)
    jobs._prctl(36, 1)


def test_missing_libc_prctl_is_an_explicit_facility_error(monkeypatch):
    monkeypatch.setattr(jobs.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    with pytest.raises(OSError, match="prctl facility is unavailable"):
        jobs._prctl(36, 1)


@pytest.mark.skipif(sys.platform != "linux" or os.environ.get("LANGADAPTER_TEST_LINUX_JOB") != "1",
                    reason="Opt-in dedicated Linux supervisor native fixture")
@pytest.mark.parametrize("supervision", ["pidfd", "process_group"])
@pytest.mark.parametrize("mode", ["parent_exit", "double_fork", "cancel", "signal"])
def test_native_supervisor_reaps_escaped_children_and_keeps_native_identity(
        tmp_path, mode, supervision):
    # Only the isolated helper becomes a subreaper, never the pytest process.
    # The group mode is forced as an interpreter without process descriptors
    # selects it.
    child_path = tmp_path / "child.pid"
    ready_path = tmp_path / "subject.ready"
    child = "import os,time; os.setsid(); open(%r,'w').write(str(os.getpid())); time.sleep(120)" % str(child_path)
    if mode == "double_fork":
        spawn = ("p=os.fork()\nif p==0:\n p=os.fork()\n if p==0:\n  "
                 + child.replace("; ", "\n  ") + "\n os._exit(0)\nos.waitpid(p,0)\n")
    else:
        spawn = "subprocess.Popen([sys.executable,'-I','-c',%r])\n" % child
    subject = ("import os,sys,time,signal,subprocess\n" + spawn
               + "deadline=time.monotonic()+5\n"
               + "while not os.path.exists(%r) and time.monotonic()<deadline: time.sleep(.01)\n" % str(child_path)
               + "assert os.path.exists(%r)\n" % str(child_path)
               + "os.write(1,b'output\\r\\n'); os.write(2,b'error\\n')\n"
               + "open(%r,'w').write('ready')\n" % str(ready_path)
               + ("time.sleep(120)\n" if mode == "cancel" else
                  "os.kill(os.getpid(),signal.SIGTERM)\n" if mode == "signal" else "sys.exit(7)\n"))
    helper = """
import json,os,runpy,select,sys,time
Job=runpy.run_path(sys.argv[1])['_OwnedCommand']
if sys.argv[7]=='process_group': Job.__init__.__globals__['_pidfd_works']=lambda: False
job=Job(); fd=None
assert job.mode==sys.argv[7], job.mode
try:
    job.start([sys.executable,'-I','-c',sys.argv[2]])
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        code=job.poll()
        if (sys.argv[3]=='cancel' and os.path.exists(sys.argv[6])) or code is not None: break
        time.sleep(.01)
    else: raise RuntimeError('native subject did not reach fixture boundary')
    child=int(open(sys.argv[4]).read())
    fd=os.pidfd_open(child,0)
    job.close(grace=.2,kill_wait=3)
    watch=select.poll(); watch.register(fd,select.POLLIN|select.POLLHUP)
    assert watch.poll(1000), 'owned child is still running'
    assert job.quiescent
    try: os.waitpid(-1,os.WNOHANG)
    except ChildProcessError: pass
    else: raise AssertionError('owned child was not reaped')
    open(sys.argv[5],'w').write(json.dumps({'returncode':job.returncode,'quiescent':job.quiescent}))
finally:
    if fd is not None: os.close(fd)
    job.close(grace=0,kill_wait=1)
"""
    result_path = tmp_path / "result.json"
    # Independent test-owned process is outside the supervisor, never a PID target.
    with subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as unrelated:
        try:
            result = subprocess.run([sys.executable, "-I", "-c", helper, jobs.__file__, subject,
                                     mode, str(child_path), str(result_path), str(ready_path),
                                     supervision],
                                    capture_output=True, timeout=15)
            assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
            assert result.stdout == b"output\r\n"
            assert result.stderr == b"error\n"
            import json
            record = json.loads(result_path.read_text())
            assert record == {"returncode": -signal.SIGTERM if mode in {"cancel", "signal"} else 7,
                              "quiescent": True}
            assert unrelated.poll() is None
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)


@pytest.mark.parametrize("supervision", ["pidfd", "process_group"])
@pytest.mark.parametrize("mode", ["parent_exit", "double_fork", "cancel", "signal"])
def test_native_fixture_scripts_compile_without_process_execution(tmp_path, monkeypatch, mode,
                                                                   supervision):
    import json

    class Unrelated:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout):
            return 0

    def capture(argv, **options):
        compile(argv[3], "supervisor-fixture", "exec")
        compile(argv[5], "native-subject-fixture", "exec")
        Path(argv[8]).write_text(json.dumps({
            "returncode": -signal.SIGTERM if mode in {"cancel", "signal"} else 7,
            "quiescent": True,
        }))
        return subprocess.CompletedProcess(argv, 0, b"output\r\n", b"error\n")

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Unrelated())
    monkeypatch.setattr(subprocess, "run", capture)
    test_native_supervisor_reaps_escaped_children_and_keeps_native_identity(
        tmp_path, mode, supervision)
