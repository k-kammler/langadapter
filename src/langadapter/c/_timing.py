"""The timing of C tests: repeated runs after discarded warm-up runs.

The target and the test programs are built once, before the repetitions.
Each repetition runs each test program once, in the order of the suite's
files, with all its tests in its one process, as utest.h's own main runs
them, as a suite run runs them too. A time is the whole suite's. A
project's own programs (``c_tests`` ``programs``) are judged as their suite
run judges them (``_programs``). ctest's tests (``c_tests`` ``ctest``) run as
a suite run runs them, each test ctest lists in a process of its own
(``_ctest``).
"""
from __future__ import annotations

import functools
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import langadapter as la
from langadapter import placement, resources
from langadapter.runs import Benchmark, BenchmarkRequest, _report_progress

from langadapter.c import _ctest, _programs, _utest

#: What a repetition's time covers for C tests.
INCLUDES = ("each test program's process on this machine from start to exit, one after "
            "another: program start and all its tests. The target and the tests are "
            "built before the repetitions")
#: What a repetition's time covers for ctest's tests.
CTEST_INCLUDES = ("each test ctest lists, in a process of its own on this machine, one after "
                  "another, with its arguments, working folder and environment: program "
                  "start and the test. The target is built and the tests' programs relinked "
                  "before the repetitions, and a test a suite run does not run is not run")
#: What a repetition's time covers for Python tests of an extension.
EXTENSION_INCLUDES = ("the runner's process on this machine: interpreter start, the "
                      "extension's load, collection and the tests. The extension is built "
                      "before the repetitions")
#: What a repetition's time covers in a container, by what it covers on
#: this machine: the same processes in the container, each with the
#: container runtime's execution overhead.
CONTAINER_INCLUDES = {
    INCLUDES: ("each test program's process in the container from start to exit, one after "
               "another, each with the container runtime's execution overhead: program start "
               "and all its tests. The target and the tests are built before the repetitions"),
    CTEST_INCLUDES: ("each test ctest lists, in a process of its own in the container, one "
                     "after another, with its arguments, working folder and environment and "
                     "the container runtime's execution overhead: program start and the test. "
                     "The target is built and the tests' programs relinked before the "
                     "repetitions, and a test a suite run does not run is not run"),
    EXTENSION_INCLUDES: ("the runner's process in the container: interpreter start, the "
                         "extension's load, collection and the tests, and the container "
                         "runtime's execution overhead. The extension is built before the "
                         "repetitions"),
}


def unmeasured(request: BenchmarkRequest, identity: la.ObservationIdentity,
               outcome: la.Outcome, reason: str, includes: str = INCLUDES) -> Benchmark:
    """A benchmark that ended before its repetitions."""
    return Benchmark(outcome, reason, (), request.warmup_runs,
                     request.cpus if outcome is not la.Outcome.UNSUPPORTED else None,
                     includes, identity)


class Step(NamedTuple):
    """One process of a repetition: what its failure names, its command,
    its working directory, its variables (None: the adapter's), its own time
    limit (None: the repetition's), the judge of its finished run, which
    gives why it did not pass or None, and whether the judge reads its
    output whole (a ctest test its regular expressions judge)."""

    label: str
    argv: Sequence[str]
    cwd: str
    env: Optional[Mapping[str, str]]
    own_timeout: Optional[float]
    judge: Callable[[object], Optional[Tuple[la.Outcome, str]]]
    whole: bool = False


def program_steps(programs: Mapping[str, Path], cwd: str, *, own_programs: bool) -> List[Step]:
    """Each test file's program by its file, run whole in ``cwd``, judged by
    utest's lines, or a project's own programs (``own_programs``) as their
    suite run judges them (``_programs.verdict``)."""
    judge = _own_program_failure if own_programs else _failure
    return [Step(file, [str(program)], cwd, None, None, functools.partial(judge, file))
            for file, program in programs.items()]


def ctest_steps(programs_dir: Path, env: Mapping[str, str]) -> List[Step]:
    """Each test of ctest's listing in ``programs_dir`` that a suite run runs, as
    it runs it (``_ctest.command``, ``env`` the adapter's variables), judged
    by ctest's rules."""
    steps = []
    for name, spec in _ctest.runnable(programs_dir):
        argv, environment, own = _ctest.command(spec, env)
        steps.append(Step(name, argv, spec["cwd"], environment, own,
                          functools.partial(_ctest_failure, name, spec),
                          _ctest.judged_by_output(spec)))
    return steps


def measure(request: BenchmarkRequest, steps: Sequence[Step], run: Callable, *,
            cwd: str, identity: la.ObservationIdentity, includes: str = INCLUDES) -> Benchmark:
    """The times of ``steps``, run by ``run`` (the adapter's command
    runner), the processors read in ``cwd``. A repetition's time is the sum
    of its steps' process lifetimes as their supervisor timed them
    (``native_elapsed_s``), else as timed around ``run``."""
    prefix = [] if request.cpus is None else ["taskset", "-c", request.cpus]
    # Outside the timed repetitions: the processors a process started like
    # them may run on.
    allowed = allowed_cpus(run, prefix, cwd)
    times: List[float] = []
    returncode: Optional[int] = None

    def result(outcome: la.Outcome, reason: str = "") -> Benchmark:
        return Benchmark(outcome, reason, tuple(times), request.warmup_runs, request.cpus,
                         includes, identity, allowed_cpus=allowed, returncode=returncode)

    with placement.serialized(request.serialize):
        for index in range(request.warmup_runs + request.runs):
            began = time.perf_counter()
            elapsed_ms = 0.0
            for step in steps:
                returncode = None
                remaining = request.timeout_s - (time.perf_counter() - began)
                own = step.own_timeout
                try:
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(step.label, request.timeout_s)
                    start = time.perf_counter()
                    done = run([*prefix, *step.argv], cwd=step.cwd, env=step.env,
                               timeout=remaining if own is None else min(remaining, own),
                               whole=step.whole)
                except subprocess.TimeoutExpired:
                    if own is not None and own <= remaining:
                        return result(la.Outcome.TEST_FAILURES,
                                      f"{step.label}: its own timeout of {own:g} s ended it")
                    return result(la.Outcome.TIMED_OUT, f"a repetition reached the timeout of "
                                                        f"{request.timeout_s:g} s")
                except (OSError, subprocess.SubprocessError) as exc:
                    return result(la.Outcome.INFRASTRUCTURE_FAILURE,
                                  f"a repetition could not execute: {exc}")
                native = getattr(done, "native_elapsed_s", None)
                # On Linux the supervisor times each test process itself,
                # without its own start and stop. Either time ends before
                # the judging.
                elapsed_ms += (time.perf_counter() - start if native is None else native) * 1000
                returncode = done.returncode if done.returncode >= 0 else None
                failure = step.judge(done)
                if failure is not None:
                    _report_progress(request, index, elapsed_ms)
                    return result(*failure)
            _report_progress(request, index, elapsed_ms)
            if index >= request.warmup_runs:
                times.append(elapsed_ms)
    return result(la.Outcome.COMPLETED)


def _failure(file: str, done) -> Optional[Tuple[la.Outcome, str]]:
    """Why a test program's run did not pass, or None."""
    failed = _utest.failed_names(_utest.plain(done.stdout))
    if done.returncode < 0:
        return (la.Outcome.INFRASTRUCTURE_FAILURE,
                f"{file}: the test program ended by signal {-done.returncode}")
    if failed:
        return la.Outcome.TEST_FAILURES, f"{file}: failing tests: {', '.join(failed)}"
    if done.returncode != 0:
        return (la.Outcome.INFRASTRUCTURE_FAILURE,
                f"{file}: the test program exited with status {done.returncode}")
    return None


def _ctest_failure(name: str, spec: dict, done) -> Optional[Tuple[la.Outcome, str]]:
    """Why a ctest test's run did not pass by ctest's rules, or None."""
    return _verdict_failure(name, _ctest.verdict(name, spec, done))


def _own_program_failure(file: str, done) -> Optional[Tuple[la.Outcome, str]]:
    """Why a project's own test program's run did not pass by its suite
    run's rule, or None."""
    return _verdict_failure(file, _programs.verdict(_programs.NAME, done))


def _verdict_failure(label: str, one: _utest.TestRun) -> Optional[Tuple[la.Outcome, str]]:
    """Why the run judged ``one`` did not pass, or None: a failed test is a
    test failure, an error (such as a signal) the infrastructure's."""
    if one.outcome in ("passed", "skipped"):
        return None
    outcome = (la.Outcome.TEST_FAILURES if one.outcome == "failed"
               else la.Outcome.INFRASTRUCTURE_FAILURE)
    return outcome, f"{label}: {one.message.splitlines()[0]}"


def allowed_cpus(run: Callable, prefix, cwd: str) -> Optional[Tuple[int, ...]]:
    """The processors a process started with ``prefix`` may use (its
    affinity, not those used), from its status file
    (``resources._status_allowed_cpus``), or None where the platform reports
    none or the reading failed."""
    try:
        done = run([*prefix, "grep", "^Cpus_allowed_list:", "/proc/self/status"], cwd=cwd,
                   timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return resources._status_allowed_cpus(done)
