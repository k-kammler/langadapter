"""Actual executor and persistent-command seams with controlled adoption owners."""
import io
import subprocess
from types import SimpleNamespace

import pytest

from langadapter import _process_tree as tree, execution, placement


@pytest.fixture
def linux(monkeypatch):
    calls = []
    state = SimpleNamespace(admission=None, execute=None, close=None)

    class Command:
        def __init__(self, args, options):
            self.args = args
            self.text = options.get("text", False)
            self.returncode = None
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

        @classmethod
        def start(cls, args, **options):
            calls.append(("admit", args, options))
            if state.admission is not None:
                raise state.admission
            return cls(args, options)

        def cancel(self):
            calls.append(("cancel", self.args))
            self.returncode = -15

        def wait(self, timeout=None):
            calls.append(("wait", self.args, timeout))
            return self.returncode

        def communicate(self, **options):
            calls.append(("communicate", self.args, options))
            if state.execute is not None:
                error, state.execute = state.execute, None
                raise error
            self.returncode = 7 if self.returncode is None else self.returncode
            return ("out\n", "error\n") if self.text else (b"out\r\n", b"error\n")

        def close(self, *, primary=None):
            calls.append(("close", self.args, primary))
            if state.close is not None:
                if primary is None:
                    raise state.close
                primary._langadapter_linux_command = self
                return False
            for stream in (self.stdin, self.stdout, self.stderr):
                stream.close()
            return True

    def forbidden(*args, **kwargs):
        pytest.fail("Supervised Linux caller must not launch/fallback/signal a PID")

    monkeypatch.setattr(tree, "_Command", Command)
    monkeypatch.setattr(tree._host, "runs_commands", lambda: True)
    monkeypatch.setattr(tree, "os", SimpleNamespace(name="posix", killpg=forbidden))
    monkeypatch.setattr(tree.subprocess, "Popen", forbidden)
    return calls, state


@pytest.mark.parametrize("runner", [tree.run, execution.run_capture, placement.run_timed])
def test_linux_actual_callers_preserve_native_args_outputs_and_exit(linux, runner):
    calls, _ = linux
    result = runner(["selected-native", "arg"], timeout=7, env={"PATH": "selected"})
    assert result.args == ["selected-native", "arg"] and result.returncode == 7
    assert result.stdout == (b"out\r\n" if runner is tree.run else "out\n")
    assert result.stderr == (b"error\n" if runner is tree.run else "error\n")
    assert calls[0][0] == "admit" and calls[0][1] == result.args
    assert calls[0][2]["env"] == {"PATH": "selected"}
    assert calls[-1] == ("close", result.args, None)


def test_persistent_start_and_finish_use_the_same_owner(linux):
    calls, _ = linux
    owner = tree.start(["worker"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert owner.args == ["worker"]
    tree.finish(owner)
    assert calls == [("admit", ["worker"], {"stdout": subprocess.DEVNULL,
                                          "stderr": subprocess.DEVNULL}),
                     ("close", ["worker"], None)]


def test_linux_timeout_retains_caller_budget_and_partial_output(linux):
    calls, state = linux
    state.execute = subprocess.TimeoutExpired(["native"], 7, output=b"early")
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        tree.run(["native"], timeout=7, input=b"input")
    assert (caught.value.cmd, caught.value.timeout) == (["native"], 7)
    assert (caught.value.output, caught.value.stderr) == (b"out\r\n", b"error\n")
    assert ("cancel", ["native"]) in calls
    assert ("wait", ["native"], 20.0) in calls
    assert calls[-1] == ("close", ["native"], caught.value)


def test_linux_multi_worker_stop_requests_all_cancellations_before_wait(linux):
    calls, _ = linux
    first, second = tree.start(["first"]), tree.start(["second"])
    calls.clear()
    tree.stop_all([first, second], grace=2)
    assert calls[:2] == [("cancel", ["first"]), ("cancel", ["second"])]
    assert all(call[0] in {"cancel", "wait"} for call in calls)


@pytest.mark.parametrize("error", [OSError("supervisor refused"), KeyboardInterrupt("admission stopped")])
def test_linux_refused_admission_never_falls_back_to_raw_native(linux, error):
    calls, state = linux
    state.admission = error
    with pytest.raises(type(error)) as caught:
        tree.run(["native"], timeout=7)
    assert caught.value is error
    assert len(calls) == 1 and calls[0][0] == "admit"


def test_linux_cleanup_failure_preserves_original_execution_error_and_owner(linux):
    calls, state = linux
    original = OSError("read failed")
    state.execute, state.close = original, OSError("owner still uncertain")
    with pytest.raises(OSError) as caught:
        tree.run(["native"], timeout=7)
    assert caught.value is original
    assert original._langadapter_linux_command.args == ["native"]
    assert calls[-1] == ("close", ["native"], original)


def test_linux_completed_native_result_cannot_hide_failed_disposal(linux):
    _, state = linux
    state.close = OSError("owner still uncertain")
    with pytest.raises(OSError, match="owner still uncertain"):
        tree.run(["native"], timeout=7)
