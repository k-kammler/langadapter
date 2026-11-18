"""Mapped container ownership using real channels and controlled acquired handles."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _linux_command, _linux_container, _process_tree
from langadapter._linux_job import ControlError, SupervisionUnavailable
from langadapter.context import Environment
from langadapter.execution import ContainerExecutor, executor_for


@pytest.fixture
def route(tmp_path, monkeypatch):
    zone = tmp_path / "zone"
    zone.mkdir()
    executor = ContainerExecutor("borrowed", work_dir=str(zone), container_work_dir="/mounted",
                                 forward_keys=("NATIVE_SETTING",))
    state = SimpleNamespace(calls=[], channel=None, token=None, report=None, files={},
                            clock=0.0, finish=True, missing=False, extra={})

    def forbidden(*args, **kwargs):
        pytest.fail("Container composition checks launch no native process or signal")

    class Thread:
        def __init__(self, **kwargs):
            self.alive = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, **kwargs):
            self.alive = False

    def write(state_name, pid=None, code=None, *, helper_pid=701):
        state.report = {"version": 1, "token": state.token, "helper_pid": helper_pid,
                        "state": state_name, "command_pid": pid, "returncode": code,
                        "quiescent": state_name != "RUNNING", "reason": None, **state.extra}
        (state.channel / "status.json").write_text(json.dumps(state.report), encoding="utf-8")

    class Runner:
        pid, returncode = 42, None

        def __init__(self):
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

        def poll(self):
            # The helper starts the command on its own after READY.
            if not state.missing and state.report["state"] == "READY" and self.returncode is None:
                write("RUNNING", 702)
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                if (state.channel / "cancel.json").exists():
                    write("CANCELLED")
                    state.report["reason"] = "cancelled before native start"
                    (state.channel / "status.json").write_text(json.dumps(state.report), encoding="utf-8")
                    self.returncode = 0
                else:
                    raise subprocess.TimeoutExpired("selected runner", timeout)
            return self.returncode

        def communicate(self, input=None, timeout=None):
            state.calls.append(("communicate", input, timeout))
            # The channel as the running command sees it; a confirmed
            # disposal removes the directory afterwards.
            state.files = {path.name: path.read_bytes() for path in state.channel.iterdir()}
            write("COMPLETE", 702, 7)
            self.returncode = 0
            return b"native\r\noutput", b"native diagnostic"

    runner = Runner()

    def launch(argv, **options):
        state.calls.append(("launch", argv, options))
        state.channel = next(zone.glob("langadapter-command-*"))
        request = json.loads((state.channel / "request.json").read_text())
        state.token = request["token"]
        assert "admission_required" not in request
        if state.missing:
            runner.returncode = 127
        else:
            write("READY")
        return runner

    def finish(handle, **kwargs):
        assert handle is runner
        state.calls.append(("finish_host", kwargs))
        handle._langadapter_command_disposed = state.finish
        return state.finish

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(_process_tree, "start", launch)
    # The outer owner still uses the real finish; only its acquired host handle is controlled.
    real_finish = _process_tree.finish
    monkeypatch.setattr(_process_tree, "finish", lambda handle, **kw:
                        finish(handle, **kw) if handle is runner else real_finish(handle, **kw))
    monkeypatch.setattr(_linux_command.threading, "Thread", Thread)
    monkeypatch.setattr(_linux_command.time, "monotonic", lambda: state.clock)
    monkeypatch.setattr(_linux_command.time, "sleep", lambda delay:
                        setattr(state, "clock", state.clock + delay))
    communicate = _linux_command._Command.communicate

    def capture_timeout(owner, input=None, timeout=None):
        state.calls.append(("owner_communicate", input, timeout))
        return communicate(owner, input=input, timeout=timeout)

    monkeypatch.setattr(_linux_command._Command, "communicate", capture_timeout)
    return SimpleNamespace(executor=executor, state=state, runner=runner, zone=zone, write=write)


def test_actual_container_channel_keeps_captured_target_and_host_control_separate(route):
    selected = route.zone.parent / ("docker.exe" if os.name == "nt" else "docker")
    selected.write_text("selected runner", encoding="ascii")
    selected.chmod(0o755)
    route.executor.runner = str(selected)
    route.executor.control_environment = Environment({"CONTROL": "host"}, inherit=False)
    native = Environment({"PATH": "/container/bin", "RESOURCE": str(route.zone / "resource")}, inherit=False)
    owner = _linux_container.start(route.executor, ["/container/jdk/bin/java"],
                              helper_python="/control/python", env=native)
    request = json.loads((route.state.channel / "request.json").read_text())
    assert request["native_environment"] == {"PATH": "/container/bin", "RESOURCE": "/mounted/resource"}
    launch = next(call for call in route.state.calls if call[0] == "launch")
    assert launch[2]["env"] == {"CONTROL": "host"}
    assert launch[1][0] == str(selected)
    assert "CONTROL=host" not in launch[1]
    assert owner.communicate(timeout=1) == (b"native\r\noutput", b"native diagnostic")
    assert _process_tree.finish(owner) is True
    assert owner.returncode == 7 and route.runner._langadapter_command_disposed is True


def test_a_container_command_is_timed_as_a_plain_runner_exec(route, monkeypatch):
    """A container command's time is its own time inside the container plus a
    plain runner round trip, without the supervisor's start, handshake,
    polling and cleanup."""
    communicate = type(route.runner).communicate

    def timed(runner, input=None, timeout=None):
        result = communicate(runner, input=input, timeout=timeout)
        route.state.report["elapsed_ns"] = 1_234_000_000
        (route.state.channel / "status.json").write_text(json.dumps(route.state.report),
                                                         encoding="utf-8")
        return result

    monkeypatch.setattr(type(route.runner), "communicate", timed)
    monkeypatch.setattr(_linux_container, "_runner_round_trip", lambda executor: 0.25)
    result = _linux_container.run(route.executor, ["/bin/native"], helper_python="/control/python3")
    assert result.returncode == 7
    assert result.native_elapsed_s == pytest.approx(1.484)


def test_a_container_helpers_supervision_mode_is_recorded_for_its_route(route, monkeypatch):
    communicate = type(route.runner).communicate

    def reported(runner, input=None, timeout=None):
        result = communicate(runner, input=input, timeout=timeout)
        route.state.report["supervision"] = "process_group"
        (route.state.channel / "status.json").write_text(json.dumps(route.state.report),
                                                         encoding="utf-8")
        return result

    monkeypatch.setattr(type(route.runner), "communicate", reported)
    monkeypatch.setattr(_linux_container, "_SUPERVISION", {})
    _linux_container.run(route.executor, ["/bin/native"], helper_python="/control/python3")
    assert _linux_container.reported_supervision(
        route.executor.runner, "borrowed", "/control/python3") == "process_group"


def test_a_container_helpers_mode_is_recorded_when_its_command_starts(route, monkeypatch):
    """A container helper's mode is recorded when its command starts, so a
    record made after a failed command, or before the run, names it."""
    route.state.extra = {"supervision": "pidfd"}
    monkeypatch.setattr(_linux_container, "_SUPERVISION", {})
    owner = _linux_container.start(route.executor, ["/bin/native"], helper_python="/control/python3")
    assert _linux_container.reported_supervision(
        route.executor.runner, "borrowed", "/control/python3") == "pidfd"
    owner.communicate(timeout=1)
    assert _process_tree.finish(owner) is True


def test_the_runner_round_trip_is_a_median_measured_again_after_a_minute(monkeypatch):
    from langadapter import execution

    clock = [100.0]
    samples = iter([0.30, 0.20, 0.25, 0.40, 0.10, 0.50])
    calls = []

    def capture(argv, **options):
        calls.append(argv)
        done = subprocess.CompletedProcess(argv, 0, "", "")
        done.native_elapsed_s = next(samples)
        return done

    executor = ContainerExecutor("borrowed", work_dir=tempfile.gettempdir())
    monkeypatch.setattr(execution, "run_capture", capture)
    monkeypatch.setattr(_linux_container.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_linux_container, "_ROUND_TRIPS", {})
    assert _linux_container._runner_round_trip(executor) == 0.25
    assert len(calls) == 3 and calls[0][-1] == "true" and calls[0][:2] == ["docker", "exec"]
    clock[0] += 59
    assert _linux_container._runner_round_trip(executor) == 0.25 and len(calls) == 3
    clock[0] += 2
    assert _linux_container._runner_round_trip(executor) == 0.40 and len(calls) == 6


@pytest.mark.parametrize("failure", ["exit", "timeout", "error"])
def test_a_failed_round_trip_leaves_the_command_without_a_native_time(
        route, monkeypatch, failure):
    """A failed or raising exec of ``true`` leaves the completed command's
    result standing, without a native time, and nothing of it is kept."""
    from langadapter import execution

    communicate = type(route.runner).communicate
    calls = []

    def timed(runner, input=None, timeout=None):
        result = communicate(runner, input=input, timeout=timeout)
        route.state.report["elapsed_ns"] = 1_234_000_000
        (route.state.channel / "status.json").write_text(json.dumps(route.state.report),
                                                         encoding="utf-8")
        return result

    def capture(argv, timeout=None, **options):
        calls.append(timeout)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if failure == "error":
            raise OSError("runner refused")
        return subprocess.CompletedProcess(argv, 125, "", "no such container")

    monkeypatch.setattr(type(route.runner), "communicate", timed)
    monkeypatch.setattr(execution, "run_capture", capture)
    monkeypatch.setattr(_linux_container, "_ROUND_TRIPS", {})
    result = _linux_container.run(route.executor, ["/bin/native"], helper_python="/control/python3")
    assert result.returncode == 7 and not hasattr(result, "native_elapsed_s")
    assert calls == [_linux_container._ROUND_TRIP_TIMEOUT] and _linux_container._ROUND_TRIPS == {}


def test_a_round_trip_exec_left_running_raises(monkeypatch):
    from langadapter import execution

    command = SimpleNamespace(_langadapter_command_disposed=False)
    failure = subprocess.TimeoutExpired(["true"], 10)
    failure._langadapter_command = command

    def capture(argv, **options):
        raise failure

    executor = ContainerExecutor("borrowed", work_dir=tempfile.gettempdir())
    monkeypatch.setattr(execution, "run_capture", capture)
    monkeypatch.setattr(_linux_container, "_ROUND_TRIPS", {})
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        _linux_container._runner_round_trip(executor)
    assert caught.value is failure and _linux_container._ROUND_TRIPS == {}


@pytest.mark.parametrize("closed", [False, True])
def test_a_round_trip_exec_whose_linux_launch_failed_raises_unless_it_closed(monkeypatch, closed):
    """A supervised exec whose launch failed names its Linux command only.
    While its control is open the process may still run and the probe
    raises. Once it closed, the probe gives no time."""
    from langadapter import execution
    from langadapter._linux_command import _Command

    command = object.__new__(_Command)
    command._closed = closed
    failure = OSError("supervisor did not admit the command")
    failure._langadapter_linux_command = command

    def capture(argv, **options):
        raise failure

    executor = ContainerExecutor("borrowed", work_dir=tempfile.gettempdir())
    monkeypatch.setattr(execution, "run_capture", capture)
    monkeypatch.setattr(_linux_container, "_ROUND_TRIPS", {})
    if closed:
        assert _linux_container._runner_round_trip(executor) is None
    else:
        with pytest.raises(OSError) as caught:
            _linux_container._runner_round_trip(executor)
        assert caught.value is failure
    assert _linux_container._ROUND_TRIPS == {}


def test_selected_container_helper_precedes_native_start_and_preserves_route(route):
    args = ["/jdk/bin/java", "-cp", str(route.zone / "classes"), "Example"]
    result = _linux_container.run(route.executor, args, helper_python="/control/python3",
                             cwd=str(route.zone / "project"),
                             env={"NATIVE_SETTING": "selected", "HOST_ONLY": "not forwarded"},
                             input="byte exact\n", timeout=17)
    assert result.returncode == 7 and result.stdout == "native\noutput"
    assert result.stderr == "native diagnostic" and result.args == args
    request = json.loads(route.state.files["request.json"])
    assert request["argv"] == ["/jdk/bin/java", "-cp", "/mounted/classes", "Example"]
    assert request["cwd"] == "/mounted/project"
    launch = route.state.calls[0]
    assert launch[1][:6] == ["docker", "exec", "-i", "-e", "NATIVE_SETTING=selected", "borrowed"]
    assert launch[1][6:9] == ["/control/python3", "-I", "-S"]
    assert launch[1][9].startswith("/mounted/langadapter-command-")
    assert "HOST_ONLY" not in str(launch) and "cwd" not in launch[2]
    assert route.state.files["supervisor.py"] == Path(_linux_command.__file__).with_name("_linux_job.py").read_bytes()
    assert not route.state.channel.exists()
    assert ("owner_communicate", b"byte exact\n", 17) in route.state.calls
    assert ("communicate", b"byte exact\n", 0.1) in route.state.calls
    assert any(call[0] == "finish_host" for call in route.state.calls)
    assert route.runner.stdout.closed and route.runner.stderr.closed


def test_container_acquisition_forwards_caller_file_output_without_pipe_capture(route):
    stdout_path, stderr_path = route.zone / "stdout.log", route.zone / "stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        owner = _linux_container.start(route.executor, ["/jdk/bin/java"],
                                  helper_python="/control/python3",
                                  stdout=stdout, stderr=stderr)
        call = route.state.calls[0]
        assert call[2]["stdout"] is stdout and call[2]["stderr"] is stderr
        assert "stdin" not in call[2]
        call[2]["stdout"].write(b"large native output\n" * 50000)
        call[2]["stderr"].write(b"native diagnostic")
        owner.communicate(timeout=17)
        assert _process_tree.finish(owner) is True
        assert not stdout.closed and not stderr.closed
    assert stdout_path.read_bytes() == b"large native output\n" * 50000
    assert stderr_path.read_bytes() == b"native diagnostic"


@pytest.mark.parametrize("context_selected", [False, True])
def test_public_executor_explicit_supervisor_uses_actual_owned_route(route, monkeypatch, context_selected):
    from langadapter import execution

    def forbidden(*args, **kwargs):
        pytest.fail("Explicit container supervision must never call the ordinary runner route")

    monkeypatch.setattr(execution, "run_capture", forbidden)
    context = lb.ProjectContext("/container/project", lb.Interpreter(path="/jdk/bin/java"),
        execution=lb.ContainerExecution("borrowed", str(route.zone),
            container_work_dir="/mounted", helper_python="/control/python3"),
        environment={"NATIVE_SETTING": "selected"})
    context.validate()
    selected = (executor_for(context) if context_selected else ContainerExecutor(
        "borrowed", work_dir=str(route.zone), container_work_dir="/mounted",
        helper_python="/control/python3", forward_keys=("NATIVE_SETTING",)))
    result = selected.run(["/jdk/bin/java", "-cp", str(route.zone / "classes"), "Example"],
                          cwd=context.root, env=dict(context.environment), input="raw\n", timeout=17)
    assert result.returncode == 7 and result.stdout == "native\noutput"
    assert result.stderr == "native diagnostic"
    request = json.loads(route.state.files["request.json"])
    assert request["argv"] == ["/jdk/bin/java", "-cp", "/mounted/classes", "Example"]
    assert request["cwd"] == context.root
    assert not route.state.channel.exists()
    assert route.state.calls[0][1][6:9] == ["/control/python3", "-I", "-S"]
    assert route.runner._langadapter_command_disposed is True


def test_public_supervised_executor_missing_helper_refuses_without_fallback(route, monkeypatch):
    from langadapter import execution

    monkeypatch.setattr(execution, "run_capture", lambda *a, **kw:
                        pytest.fail("No unsupervised fallback is allowed"))
    route.state.missing = True
    selected = ContainerExecutor("borrowed", work_dir=str(route.zone),
                                 helper_python="/control/python3")
    with pytest.raises(SupervisionUnavailable) as caught:
        selected.run(["/jdk/bin/java"], timeout=1)
    assert caught.value._langadapter_control.receipt.is_file()


def test_container_helper_without_readiness_keeps_its_receipt(route):
    route.state.missing = True
    with pytest.raises(SupervisionUnavailable) as caught:
        _linux_container.start(route.executor, ["/jdk/bin/java"], helper_python="/control/python3")
    # A helper that never answered leaves disposal unconfirmed and keeps its receipt.
    assert caught.value._langadapter_linux_command.control.owned.receipt.is_file()
    assert not any(call[0] == "communicate" for call in route.state.calls)


def test_missing_container_facilities_remain_setup_failure_without_native_admission(route, monkeypatch):
    launch = _process_tree.start

    def unavailable(*args, **kwargs):
        runner = launch(*args, **kwargs)
        route.write("FAILED")
        route.state.report.update(reason="setup unavailable", error={
            "type": "SupervisionUnavailable", "message": "pidfds unavailable",
            "errno": None, "filename": None})
        (route.state.channel / "status.json").write_text(json.dumps(route.state.report), encoding="utf-8")
        runner.returncode = 1
        return runner

    monkeypatch.setattr(_process_tree, "start", unavailable)
    with pytest.raises(SupervisionUnavailable, match="pidfds") as caught:
        _linux_container.start(route.executor, ["native"], helper_python="/control/python3")
    # A refusal at the helper's preflight started nothing: no owner remains.
    assert not caught.value._langadapter_linux_command.control.owned.receipt.exists()


def test_container_helper_identity_cannot_turn_into_the_host_wrapper_pid(route):
    owner = _linux_container.start(route.executor, ["native"], helper_python="/control/python3")
    route.write("COMPLETE", 702, 0, helper_pid=route.runner.pid)
    route.runner.returncode = 0
    with pytest.raises(ControlError):
        owner.close()
    assert not owner._closed and not route.runner.stdout.closed
    assert not any(call[0] == "finish_host" for call in route.state.calls)


def test_container_quiescence_does_not_waive_actual_host_runner_disposal(route):
    route.state.finish = False
    with pytest.raises(ControlError, match="runner disposal") as caught:
        _linux_container.run(route.executor, ["native"], helper_python="/control/python3")
    owner = caught.value._langadapter_linux_command
    assert not owner._closed and not route.runner.stdout.closed
    assert owner.control.owned.receipt.is_file()


@pytest.mark.parametrize("drain_failure", [False, True])
def test_container_close_preserves_final_bytes_when_timed_reads_have_no_partial_output(
    route, monkeypatch, drain_failure,
):
    owner = _linux_container.start(route.executor, ["native"], helper_python="/control/python3")
    blocked = [drain_failure]
    observed = []

    def stopped(timeout=None):
        assert (route.state.channel / "cancel.json").is_file()
        route.write("CANCELLED", 702, -15)
        route.state.report["reason"] = "cancelled"
        (route.state.channel / "status.json").write_text(json.dumps(route.state.report), encoding="ascii")
        route.runner.returncode = 0
        return 0

    def capture(input=None, timeout=None):
        observed.append(timeout)
        if route.runner.returncode is None:
            route.state.clock += timeout
            raise subprocess.TimeoutExpired("Windows-like reader", timeout)
        if blocked[0]:
            raise subprocess.TimeoutExpired("final owned drain", timeout)
        return b"native\r\n", b"error\n"

    monkeypatch.setattr(route.runner, "wait", stopped)
    monkeypatch.setattr(route.runner, "communicate", capture)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        owner.communicate(timeout=.2)
    assert caught.value.output is None and caught.value.stderr is None
    original = KeyboardInterrupt("container interruption")
    assert _process_tree.finish(owner, primary=original) is not drain_failure
    assert owner._closed is not drain_failure and owner.returncode == -15
    if drain_failure:
        assert owner._output is None and not route.runner.stdout.closed
        assert original._langadapter_linux_command is owner
        assert not any(call[0] == "finish_host" for call in route.state.calls)
        blocked[0] = False
        assert _process_tree.finish(owner, primary=original) is True
    assert owner._output == (b"native\r\n", b"error\n")
    assert (original.output, original.stderr) == owner._output
    assert observed[-1] == owner._cleanup
    assert owner.helper._langadapter_command_disposed is True
    assert not owner.control.owned.root.exists() and not owner.control.owned.receipt.exists()


def test_container_final_drain_refuses_changed_terminal_without_cached_output(route, monkeypatch):
    owner = _linux_container.start(route.executor, ["native"], helper_python="/control/python3")
    route.write("COMPLETE", 702, 7)
    route.runner.returncode = 0

    def capture(input=None, timeout=None):
        route.write("COMPLETE", 702, 8)
        return b"native\r\n", b"error\n"

    monkeypatch.setattr(route.runner, "communicate", capture)
    with pytest.raises(ControlError, match="terminal report changed") as caught:
        owner.close()
    assert caught.value._langadapter_linux_command is owner
    assert not owner._closed and owner._output is None and owner.returncode == 7
    assert not route.runner.stdout.closed
    assert not any(call[0] == "finish_host" for call in route.state.calls)
    with pytest.raises(ControlError, match="identity or exit changed"):
        owner.close()
    assert not owner._closed and owner._output is None


@pytest.mark.parametrize("helper", [None, "python3", "/control/../python3", "C:\\python.exe"])
def test_container_control_interpreter_is_explicit_without_allocation_or_host_fallback(route, helper):
    with pytest.raises(SupervisionUnavailable):
        _linux_container.start(route.executor, ["native"], helper_python=helper)
    assert not route.state.calls and list(route.zone.iterdir()) == []


def _subject(root, mode):
    child = ("import os,time;os.setsid();open(%r,'w').write(str(os.getpid()));time.sleep(60)"
             % (root + "/child.pid"))
    return ("import os,sys,time,subprocess\n"
            "subprocess.Popen([sys.executable,'-I','-c',%r])\n" % child
            + "deadline=time.monotonic()+10\n"
            + "while not os.path.exists(%r) and time.monotonic()<deadline:time.sleep(.01)\n"
            % (root + "/release")
            + "assert os.path.exists(%r)\n" % (root + "/release")
            + "assert os.environ['LANGADAPTER_NATIVE_FIXTURE']=='exact'\n"
            + "os.write(1,b'native\\r\\n');os.write(2,b'error\\n')\n"
            + ("sys.exit(7)\n" if mode == "parent_exit" else "time.sleep(60)\n"))


@pytest.mark.parametrize("mode", ["parent_exit", "timeout", "interruption"])
def test_container_fixture_source_compiles_without_launch(mode):
    compile(_subject("/mounted/fixture", mode), "container-owned-native-subject", "exec")


@pytest.mark.parametrize("mode", ["parent_exit", "timeout", "interruption"])
def test_real_container_controller_keeps_native_streams_lease_and_escaped_child_accounting(mode):
    container = os.environ.get("LANGADAPTER_TEST_JAVA_CONTAINER")
    zone = os.environ.get("LANGADAPTER_TEST_CONTAINER_WORK_DIR")
    helper = os.environ.get("LANGADAPTER_TEST_CONTAINER_HELPER_PYTHON")
    if not (container and zone and helper):
        pytest.skip("Needs explicitly prepared borrowed Linux container, mapped work directory and control Python")
    executor = ContainerExecutor(container, work_dir=zone, container_work_dir="/workzone")
    host = Path(tempfile.mkdtemp(prefix="container-owner-fixture-", dir=zone))
    root = executor._to_container(str(host))
    observer, owner = None, None

    def wait_file(name):
        deadline = time.monotonic() + 10
        while not (host / name).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (host / name).is_file(), "Owned container fixture did not reach " + name

    try:
        owner = _linux_container.start(
            executor, [helper, "-I", "-c", _subject(root, mode)], helper_python=helper,
            env={"LANGADAPTER_NATIVE_FIXTURE": "exact"})
        wait_file("child.pid")
        pid = int((host / "child.pid").read_text())
        watch = ("import os,select;fd=os.pidfd_open(%d);poll=select.poll();"
                 "poll.register(fd,select.POLLIN);open(%r,'w').write('ready');"
                 "assert poll.poll(20000),'owned child still runs';os.close(fd);print('stopped')"
                 % (pid, root + "/observer.ready"))
        observer = _linux_container.start(executor, [helper, "-I", "-c", watch], helper_python=helper)
        wait_file("observer.ready")
        time.sleep(2.1)  # Exercise advancing liveness, not only the initial request.
        lease = json.loads((owner.control.owned.root / "lease.json").read_text())
        assert lease["generation"] >= 2
        (host / "release").write_text("go", encoding="ascii")
        if mode != "parent_exit":
            with pytest.raises(subprocess.TimeoutExpired) as caught:
                owner.communicate(timeout=.3)
            assert caught.value.output in (None, b"native\r\n")
            assert caught.value.stderr in (None, b"error\n")
            if mode == "interruption":
                original = KeyboardInterrupt("owned container interruption")
                assert _process_tree.finish(owner, primary=original) is True
                assert owner._closed and owner.returncode == -15
                assert (original.output, original.stderr) == (b"native\r\n", b"error\n")
            else:
                owner.cancel()
        if mode != "interruption":
            assert owner.communicate(timeout=20) == (b"native\r\n", b"error\n")
            assert _process_tree.finish(owner) is True
            assert owner.returncode == (7 if mode == "parent_exit" else -15)
        assert owner._output == (b"native\r\n", b"error\n")
        assert observer.communicate(timeout=20) == (b"stopped\n", b"")
        assert _process_tree.finish(observer) is True and observer.returncode == 0
        assert owner._record["quiescent"] and owner._record["helper_pid"] == owner._helper_identity
        assert owner.helper._langadapter_command_disposed is True
        assert owner._stopped.is_set() and not owner._heartbeat.is_alive()
        # A confirmed disposal removes the control directory and its receipt.
        assert not owner.control.owned.root.exists()
        assert not owner.control.owned.receipt.exists()
    finally:
        if owner is not None:
            owner.close()
        if observer is not None:
            observer.close()


def test_a_container_run_that_its_operation_cancelled_raises_after_disposal(route, monkeypatch):
    """A container command ending while its operation is cancelled raises
    OperationCancelled once disposed of, as a local command does, whether
    the cancel or its own end stopped it."""
    from langadapter.errors import OperationCancelled

    cancellation = _process_tree.Cancellation()
    communicate = type(route.runner).communicate

    def cancelled_meanwhile(runner, input=None, timeout=None):
        result = communicate(runner, input=input, timeout=timeout)
        cancellation.cancel()
        return result

    monkeypatch.setattr(type(route.runner), "communicate", cancelled_meanwhile)
    with _process_tree.cancellable(cancellation):
        with pytest.raises(OperationCancelled):
            _linux_container.run(route.executor, ["/bin/native"], helper_python="/control/python3")
    assert cancellation._live == []


def test_a_cancel_reaches_a_container_through_its_owner_not_its_runner(route):
    """A cancel stops a container command through its supervisor's control: the
    owner is registered with the cancellation, not the runner carrying the
    supervisor, so a cancel never ends the runner before the supervisor's
    terminal report. A cancelled operation starts no further container command."""
    from langadapter.errors import OperationCancelled

    cancellation = _process_tree.Cancellation()
    with _process_tree.cancellable(cancellation):
        owner = _linux_container.start(route.executor, ["/bin/native"],
                                   helper_python="/control/python3")
        assert cancellation._live == [owner] and owner.helper not in cancellation._live
        owner.communicate(timeout=1)
        assert _process_tree.finish(owner) is True
        assert cancellation._live == []
        cancellation.cancel()
        with pytest.raises(OperationCancelled):
            _linux_container.start(route.executor, ["/bin/native"],
                               helper_python="/control/python3")
