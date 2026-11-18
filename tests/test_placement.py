"""Placement of timing runs: serializing benchmarks within a process, and
the launcher of a timing process."""
from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from langadapter import placement


def test_serialized_is_a_no_op_when_disabled() -> None:
    with placement.serialized(False):
        assert True


def test_serialized_callers_do_not_interleave() -> None:
    events: list = []

    def worker(name: str, hold_seconds: float) -> None:
        with placement.serialized(True):
            events.append(f"{name}-enter")
            time.sleep(hold_seconds)
            events.append(f"{name}-exit")

    first = threading.Thread(target=worker, args=("a", 0.1))
    second = threading.Thread(target=worker, args=("b", 0.05))
    first.start()
    time.sleep(0.02)
    second.start()
    first.join()
    second.join()
    assert events.index("a-exit") < events.index("b-enter") or \
        events.index("b-exit") < events.index("a-enter")


def test_serialized_releases_on_exception() -> None:
    with pytest.raises(ValueError):
        with placement.serialized(True):
            raise ValueError("simulated benchmark failure")
    with placement.serialized(True):
        assert True


def test_no_two_serialized_benchmarks_overlap() -> None:
    counter = threading.Lock()
    inside = 0
    peak = 0

    def worker() -> None:
        nonlocal inside, peak
        with placement.serialized(True):
            with counter:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.03)
            with counter:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1


def test_unserialized_benchmarks_run_together() -> None:
    n = 4
    barrier = threading.Barrier(n, timeout=5.0)
    met = []

    def worker() -> None:
        with placement.serialized(False):
            barrier.wait()
            met.append(True)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(met) == n


def _recorded_run(monkeypatch):
    recorded = {}

    def fake_run(argv, **kw):
        recorded["argv"] = argv
        recorded["options"] = kw
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(placement._process_tree, "run", fake_run)
    return recorded


@pytest.mark.parametrize("timeout", [None, 5])
def test_a_timing_run_is_the_command(monkeypatch, timeout) -> None:
    recorded = _recorded_run(monkeypatch)
    environment = {"EXAMPLE": "value"}
    completed = placement.run_timed(["mycmd", "-x"], timeout=timeout,
                                   cwd="work", env=environment)
    assert completed.returncode == 0
    assert recorded["argv"] == ["mycmd", "-x"]
    assert recorded["options"] == {
        "text": True, "encoding": "utf-8", "errors": "replace",
        "timeout": timeout, "env": environment, "cwd": "work",
    }


def test_timing_keeps_native_exit_output_and_execution_context(tmp_path) -> None:
    environment = {**os.environ, "TIMING_EXAMPLE": "value"}
    command = [sys.executable, "-c",
               "import os, sys; print(os.getcwd()); "
               "print(os.environ['TIMING_EXAMPLE'], file=sys.stderr); sys.exit(3)"]
    completed = placement.run_timed(command, env=environment, cwd=str(tmp_path))
    assert completed.args == command
    assert completed.returncode == 3
    assert completed.stdout.strip() == str(tmp_path)
    assert completed.stderr.strip() == "value"


def test_timing_interruption_ends_the_process_tree(monkeypatch) -> None:
    stopped = []

    class Interrupted:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, **kwargs):
            raise KeyboardInterrupt

    process = Interrupted()
    monkeypatch.setattr(placement._process_tree, "command", lambda *args, **kwargs: process)
    monkeypatch.setattr(placement._process_tree, "stop", stopped.append)
    with pytest.raises(KeyboardInterrupt):
        placement.run_timed(["mycmd"], timeout=5)
    assert stopped == [process]
