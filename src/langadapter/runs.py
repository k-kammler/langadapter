"""Suite runs: the request, the per-test outcomes, the coverage observation
and the side effects a run applied.

A run executes the suite's files as they are and writes nothing into the
project or the suite's directories, unless the request asks for a side
effect. A run without a bytecode store compiles into a cache directory of its
own, so a source always runs as it is written. The result states each side
effect it applied. A value the run did not observe is absent (``None``),
never zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Callable, Any, Dict, FrozenSet, Optional, Sequence, Tuple
from langadapter.errors import InvalidRequest
from langadapter.outcomes import (
    ExecutionIdentity,
    FileIdentity,
    ToolchainIdentity,
    InstrumentIdentity,
    ObservationIdentity,
)
from langadapter.outcomes import Outcome
from langadapter.suites import Suite, BranchItem, EvidenceItem, LineItem, TestId
import re
import statistics
from fractions import Fraction
from statistics import mean, median, stdev


def _count(value: int, name: str, *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidRequest(name, f"must be an integer of at least {minimum}")


def _duration(value: float, name: str, *, positive: bool = False) -> None:
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and value >= 0 and (not positive or value > 0))
    except OverflowError:
        valid = False
    if not valid:
        raise InvalidRequest(name, "must be a positive finite number" if positive
                             else "must be a non-negative finite number")


def _cpus(value: tuple[int, ...] | None, name: str) -> None:
    if value is not None:
        for cpu in value:
            _count(cpu, name)
        if not value or len(set(value)) != len(value):
            raise InvalidRequest(name, "must name distinct processors")


def _iteration_index(fork: int, kind: str, repetition: int) -> None:
    _count(fork, "fork")
    _count(repetition, "repetition")
    if kind not in ("warmup", "measurement"):
        raise InvalidRequest("kind", "must be warmup or measurement")


def _counters(record: object) -> None:
    if not record.forks_completed <= record.forks_started <= record.requested_forks:
        raise InvalidRequest("forks_completed", "inconsistent admitted/completed fork counts")
    for observed, requested in (("warmup_completed", "requested_warmup_runs"),
                                ("measurements_completed", "requested_runs")):
        if getattr(record, observed) > record.forks_started * getattr(record, requested):
            raise InvalidRequest(observed, "exceeds admitted fork budget")


def _metadata(record: object) -> None:
    for name, expected in (("mode", "warmed_jvm"), ("timing_boundary", "junit_launcher_execute"),
                           ("state_policy", "native_lifecycle")):
        if getattr(record, name) != expected:
            raise InvalidRequest(name, f"must be {expected}")


@dataclass(frozen=True)
class WarmedSuiteBenchmarkRequest:
    """A warmed timing of ``suite`` (``tests.benchmark_warmed``): ``forks``
    fresh JVM processes, each running ``warmup_runs`` discarded iterations
    and then ``runs`` measured iterations of the suite.

    - ``suite``: the suite to time.
    - ``state_policy``: ``native_lifecycle``, the only accepted value: each
      iteration runs the suite's own fixtures as JUnit runs them, and static
      state stays in the JVM.
    - ``iteration_timeout_s``: the time limit of one iteration in seconds,
      until its record arrives.
    - ``total_timeout_s``: the time limit of the whole operation in seconds,
      the build and every fork included.
    - ``startup_timeout_s``: the time limit in seconds from the launch of a
      fork until its JVM reports that it is ready.
    - ``forks``: the number of forks, at least 1.
    - ``warmup_runs``: the discarded iterations of each fork, at least 1.
    - ``runs``: the measured iterations of each fork, at least 1.
    - ``serialize``: when true, the forks hold the process-wide benchmark
      lock, so they wait for the other benchmarks of this process.
    - ``cpus``: the distinct processor numbers the forks run on, passed to
      ``taskset -c``, or None for no processor set.
    - ``runner_options``: None, since Java suite runs take no runner options.
    - ``on_progress``: receives a ``WarmedBenchmarkProgress`` at each stage.
      Its failure adds a diagnostic and never changes the result.
    """

    suite: Suite
    state_policy: str
    iteration_timeout_s: float
    total_timeout_s: float
    startup_timeout_s: float = 60
    forks: int = 3
    warmup_runs: int = 5
    runs: int = 10
    serialize: bool = True
    cpus: tuple[int, ...] | None = None
    runner_options: object | None = None
    on_progress: Callable[[WarmedBenchmarkProgress], None] | None = field(
        default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.cpus is not None:
            object.__setattr__(self, "cpus", tuple(self.cpus))

    def validate(self) -> None:
        """Refuse a state policy other than ``native_lifecycle``, an invalid
        count, timeout, ``serialize`` value, processor set, progress callback
        or suite with ``InvalidRequest``, naming the field."""
        if self.state_policy != "native_lifecycle":
            raise InvalidRequest("state_policy", "must explicitly be native_lifecycle")
        for name in ("forks", "warmup_runs", "runs"):
            _count(getattr(self, name), name, minimum=1)
        for name in ("iteration_timeout_s", "total_timeout_s", "startup_timeout_s"):
            _duration(getattr(self, name), name, positive=True)
        if not isinstance(self.serialize, bool):
            raise InvalidRequest("serialize", "must be bool")
        _cpus(self.cpus, "cpus")
        if self.on_progress is not None and not callable(self.on_progress):
            raise InvalidRequest("on_progress", "must be callable")
        if not isinstance(self.suite, Suite):
            raise InvalidRequest("suite", "must be a Suite")
        self.suite.validate()


@dataclass(frozen=True)
class WarmedBenchmarkProgress:
    """One progress event of a warmed timing, as the request's
    ``on_progress`` receives it. It reports progress and is no evidence that
    an iteration passed.

    - ``stage``: ``setup`` before the build, ``fork_start`` and ``fork_end``
      around a fork, ``iteration_start`` and ``iteration_end`` around an
      iteration, and ``complete`` once the result is built.
    - ``fork``: the 0-based fork of the event, None for ``setup`` and
      ``complete``.
    - ``kind``: ``warmup`` or ``measurement`` in an iteration event, else None.
    - ``repetition``: the 0-based index of the iteration within its kind in
      an iteration event, else None.
    - ``requested_forks``: the request's ``forks``.
    - ``requested_warmup_runs``: the request's ``warmup_runs``.
    - ``requested_runs``: the request's ``runs``.
    - ``forks_started``: the forks started so far.
    - ``forks_completed``: the forks so far that finished every requested
      iteration with a complete record.
    - ``warmup_completed``: the warm-up iterations so far with a complete
      record.
    - ``measurements_completed``: the measured iterations so far with a
      complete record.
    - ``elapsed_ns``: in an ``iteration_end`` event, the iteration's time in
      nanoseconds as the JVM measured it, else None.
    - ``operation_elapsed_s``: the time since the operation started, in
      seconds.
    - ``outcome``: the iteration's outcome at ``iteration_end``, the fork's
      at ``fork_end`` and the result's at ``complete``, else None.
    - ``reason``: the fork's reason at ``fork_end`` and the result's at
      ``complete``, else empty.
    """

    stage: str
    fork: int | None
    kind: str | None
    repetition: int | None
    requested_forks: int
    requested_warmup_runs: int
    requested_runs: int
    forks_started: int
    forks_completed: int
    warmup_completed: int
    measurements_completed: int
    elapsed_ns: int | None = None
    operation_elapsed_s: float | None = None
    outcome: Outcome | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.stage not in ("setup", "fork_start", "iteration_start",
                              "iteration_end", "fork_end", "complete"):
            raise InvalidRequest("stage", "unknown warmed benchmark stage")
        for name in ("requested_forks", "requested_warmup_runs", "requested_runs"):
            _count(getattr(self, name), name, minimum=1)
        for name in ("forks_started", "forks_completed", "warmup_completed",
                     "measurements_completed"):
            _count(getattr(self, name), name)
        if self.fork is not None:
            _count(self.fork, "fork")
        if self.repetition is not None:
            _count(self.repetition, "repetition")
        if self.kind is not None and self.kind not in ("warmup", "measurement"):
            raise InvalidRequest("kind", "must be warmup, measurement or None")
        if self.stage in ("iteration_start", "iteration_end"):
            if self.fork is None or self.kind is None or self.repetition is None:
                raise InvalidRequest("stage", "iteration progress requires fork, kind and repetition")
            if self.stage == "iteration_start" and (self.elapsed_ns is not None or self.outcome is not None):
                raise InvalidRequest("stage", "iteration admission has no native duration or outcome yet")
        elif self.kind is not None or self.repetition is not None:
            raise InvalidRequest("stage", "non-iteration progress has no kind or repetition")
        if self.elapsed_ns is not None:
            _count(self.elapsed_ns, "elapsed_ns")
        if self.operation_elapsed_s is not None:
            _duration(self.operation_elapsed_s, "operation_elapsed_s")
        _counters(self)
        if self.fork is not None and self.fork >= self.requested_forks:
            raise InvalidRequest("fork", "exceeds requested fork range")
        if self.repetition is not None:
            if self.kind is None or self.fork is None:
                raise InvalidRequest("repetition", "requires fork and iteration kind")
            maximum = self.requested_warmup_runs if self.kind == "warmup" else self.requested_runs
            if self.repetition >= maximum:
                raise InvalidRequest("repetition", "exceeds requested iteration range")
        if self.outcome is not None and not isinstance(self.outcome, Outcome):
            raise InvalidRequest("outcome", "must be Outcome or None")


@dataclass(frozen=True)
class WarmedTestCounts:
    """The tests of one warmed iteration by status, as the fork's JUnit
    listener counted them. Each count is None when test discovery did not
    finish.

    - ``discovered``: the tests of the test plan, dynamic tests included.
    - ``started``: the tests that started.
    - ``passed``: the tests that succeeded.
    - ``skipped``: the tests that were skipped.
    - ``aborted``: the tests JUnit reported as aborted.
    - ``failed``: the tests that failed.
    - ``container_failures``: the test containers, such as test classes,
      that failed.
    """

    discovered: int | None = None
    started: int | None = None
    passed: int | None = None
    skipped: int | None = None
    aborted: int | None = None
    failed: int | None = None
    container_failures: int | None = None

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if value is not None:
                _count(value, name)


@dataclass(frozen=True)
class WarmedTestObservation:
    """One test or test container of a warmed iteration, as JUnit
    identified it.

    - ``unique_id``: JUnit's unique identifier of it, never empty.
    - ``display_name``: JUnit's display name of it.
    - ``kind``: ``test`` or ``container``.
    - ``status``: ``successful``, ``skipped``, ``aborted`` or ``failed``, or
      ``discovered`` when it neither finished nor was skipped.
    - ``reason``: the reason of a skip, or the exception it finished with,
      else empty.
    """

    unique_id: str
    display_name: str
    kind: str
    status: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.unique_id, str) or not self.unique_id:
            raise InvalidRequest("unique_id", "must retain a nonempty native identifier")
        if self.kind not in ("test", "container"):
            raise InvalidRequest("kind", "must be test or container")
        if self.status not in ("discovered", "successful", "skipped", "aborted", "failed"):
            raise InvalidRequest("status", "unknown native test status")


@dataclass(frozen=True)
class WarmedIteration:
    """One warm-up or measured iteration of a fork: one execution of the
    suite's test plan in the fork's JVM.

    - ``fork``: the 0-based fork it ran in.
    - ``kind``: ``warmup`` or ``measurement``.
    - ``repetition``: its 0-based index within its kind in the fork.
    - ``elapsed_ns``: the time of ``Launcher.execute`` in nanoseconds, which
      the JVM measured with ``System.nanoTime``, None when the test plan did
      not execute. A zero adds a diagnostic on the timer's resolution.
    - ``outcome``: the iteration's outcome as the fork reported it:
      ``completed``, ``no_tests``, ``test_failures``, ``collection_failed``
      or ``infrastructure_failure``.
    - ``report_complete``: whether the record is complete: the test plan
      finished, and JUnit identified each item as either a test or a
      container.
    - ``counts``: the tests by status.
    - ``tests``: each test and test container JUnit identified, each
      identifier once.
    - ``reason``: why the outcome is not ``completed``, else empty.
    - ``diagnostics``: the stack trace of an error the fork caught, and the
      note on a zero time.
    """

    fork: int
    kind: str
    repetition: int
    elapsed_ns: int | None
    outcome: Outcome
    report_complete: bool
    counts: WarmedTestCounts
    tests: tuple[WarmedTestObservation, ...]
    reason: str = ""
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _iteration_index(self.fork, self.kind, self.repetition)
        if self.elapsed_ns is not None:
            _count(self.elapsed_ns, "elapsed_ns")
        object.__setattr__(self, "tests", tuple(self.tests))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        resolution_note = "Native elapsed_ns is zero; timer resolution limits this observation."
        if self.elapsed_ns == 0 and resolution_note not in self.diagnostics:
            object.__setattr__(self, "diagnostics", self.diagnostics + (resolution_note,))
        if not isinstance(self.report_complete, bool):
            raise InvalidRequest("report_complete", "must be bool")
        if not isinstance(self.outcome, Outcome):
            raise InvalidRequest("outcome", "must be Outcome")
        if not isinstance(self.counts, WarmedTestCounts):
            raise InvalidRequest("counts", "must be WarmedTestCounts")
        if not all(isinstance(test, WarmedTestObservation) for test in self.tests):
            raise InvalidRequest("tests", "must contain WarmedTestObservation records")
        if len({test.unique_id for test in self.tests}) != len(self.tests):
            raise InvalidRequest("tests", "duplicate native identifier in one iteration")


@dataclass(frozen=True)
class WarmedFork:
    """One fork of a warmed timing: a fresh JVM process in its own copy of
    the build, which ran the warm-up iterations and then the measured ones.

    - ``fork``: its 0-based index.
    - ``warmup_completed``: its warm-up iterations with a complete record.
    - ``measurements_completed``: its measured iterations with a complete
      record.
    - ``complete``: whether it finished and every requested iteration has a
      complete record.
    - ``disposal_confirmed``: whether the end of its process was confirmed,
      which a complete fork requires.
    - ``allowed_cpus``: the processors a process started like the fork may
      run on, read once before it: an allowed set, not the processors used,
      and None where the platform reports none or the reading failed.
    - ``samples_ns``: the times in nanoseconds of its measured iterations
      with a complete record, in order.
    - ``mean_ms``: the mean of ``samples_ns`` in milliseconds.
    - ``median_ms``: the median of ``samples_ns`` in milliseconds.
    - ``min_ms``: the shortest of ``samples_ns`` in milliseconds.
    - ``max_ms``: the longest of ``samples_ns`` in milliseconds.

    The four statistics are None unless the result holds its summaries
    (``WarmedSuiteBenchmark.mean_of_fork_means_ms``).
    """

    fork: int
    warmup_completed: int
    measurements_completed: int
    complete: bool
    disposal_confirmed: bool
    allowed_cpus: tuple[int, ...] | None
    samples_ns: tuple[int, ...]
    mean_ms: float | None = None
    median_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None

    def __post_init__(self) -> None:
        for name in ("fork", "warmup_completed", "measurements_completed"):
            _count(getattr(self, name), name)
        object.__setattr__(self, "samples_ns", tuple(self.samples_ns))
        if self.allowed_cpus is not None:
            object.__setattr__(self, "allowed_cpus", tuple(self.allowed_cpus))
        _cpus(self.allowed_cpus, "allowed_cpus")
        if not isinstance(self.complete, bool) or not isinstance(self.disposal_confirmed, bool):
            raise InvalidRequest("complete", "completion and disposal flags must be bool")
        if self.complete and not self.disposal_confirmed:
            raise InvalidRequest("disposal_confirmed", "complete fork requires confirmed disposal")
        for sample in self.samples_ns:
            _count(sample, "samples_ns")
        for name in ("mean_ms", "median_ms", "min_ms", "max_ms"):
            value = getattr(self, name)
            if value is not None:
                _duration(value, name)


@dataclass(frozen=True)
class WarmedBenchmarkIdentity:
    """The functional settings and inputs of a warmed timing. Before its
    setup has run, the toolchain is unknown, and ``project_inputs``,
    ``build_tool`` and ``engine_version`` are empty.

    - ``suite``: the identities of the suite's files.
    - ``project_inputs``: the identities of the project's main Java sources
      in the copy of the build.
    - ``toolchain``: the JDK, with the versions of the JUnit Platform and,
      where the project uses it, JUnit 4.
    - ``execution``: where the forks ran.
    - ``build_tool``: the project's build tool, ``maven`` or ``gradle``.
    - ``build_version``: the build tool's version, which a warmed timing
      does not read: None.
    - ``engine_version``: the version of the JUnit Platform that ran the
      tests.
    - ``jvm_args``: the arguments of the fork JVMs: the project's test JVM
      arguments, then the context's interpreter flags.
    - ``runner_options``: the runner options as pairs of texts, empty for
      Java.
    - ``mode``: ``warmed_jvm``.
    - ``timing_boundary``: ``junit_launcher_execute``: a sample times
      ``Launcher.execute`` of the test plan.
    - ``state_policy``: the request's ``state_policy``.
    - ``forks``: the request's ``forks``.
    - ``warmup_runs``: the request's ``warmup_runs``.
    - ``runs``: the request's ``runs``.
    - ``iteration_timeout_s``: the request's ``iteration_timeout_s``.
    - ``total_timeout_s``: the request's ``total_timeout_s``.
    - ``startup_timeout_s``: the request's ``startup_timeout_s``.
    - ``requested_cpus``: the request's ``cpus``.
    - ``serialize``: the request's ``serialize``.
    - ``schema_version``: the version of this record's form, 1.
    """

    suite: tuple[FileIdentity, ...]
    project_inputs: tuple[FileIdentity, ...]
    toolchain: ToolchainIdentity
    execution: ExecutionIdentity
    build_tool: str | None
    build_version: str | None
    engine_version: str | None
    jvm_args: tuple[str, ...]
    runner_options: tuple[tuple[str, str], ...]
    mode: str
    timing_boundary: str
    state_policy: str
    forks: int
    warmup_runs: int
    runs: int
    iteration_timeout_s: float
    total_timeout_s: float
    startup_timeout_s: float
    requested_cpus: tuple[int, ...] | None
    serialize: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("suite", "project_inputs", "jvm_args"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "runner_options", tuple(tuple(pair) for pair in self.runner_options))
        if self.requested_cpus is not None:
            object.__setattr__(self, "requested_cpus", tuple(self.requested_cpus))
        _cpus(self.requested_cpus, "requested_cpus")
        _metadata(self)
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise InvalidRequest("schema_version", "must be 1")
        for name in ("forks", "warmup_runs", "runs"):
            _count(getattr(self, name), name, minimum=1)
        for name in ("iteration_timeout_s", "total_timeout_s", "startup_timeout_s"):
            _duration(getattr(self, name), name, positive=True)
        if not isinstance(self.serialize, bool):
            raise InvalidRequest("serialize", "must be bool")
        if not isinstance(self.toolchain, ToolchainIdentity) or not isinstance(self.execution, ExecutionIdentity):
            raise InvalidRequest("identity", "requires existing toolchain and execution identities")
        object.__setattr__(self, "toolchain", replace(
            self.toolchain, tools=tuple(tuple(pair) for pair in self.toolchain.tools)))
        if not all(isinstance(item, FileIdentity) for item in self.suite + self.project_inputs):
            raise InvalidRequest("identity", "the suite and the project inputs must be "
                                 "FileIdentity records")
        if not all(len(pair) == 2 and all(isinstance(item, str) for item in pair)
                   for pair in self.runner_options):
            raise InvalidRequest("runner_options", "requires text key/value pairs")


@dataclass(frozen=True)
class WarmedSuiteBenchmark:
    """The result of a warmed timing (``tests.benchmark_warmed``): the
    record of every iteration, the samples of each fork and, for a complete
    observation, summary statistics in milliseconds.

    - ``outcome``: the operation's outcome.
    - ``reason``: why the outcome is not ``completed`` or the result is not
      ``complete``, else empty.
    - ``validity``: ``complete`` when every requested fork finished every
      iteration with a time, ``non_repeatable`` when the tests that ran or
      their outcomes changed between iterations, ``incomplete`` when records
      are missing, and ``no_samples`` when no fork ran.
    - ``requested_forks``: the request's ``forks``.
    - ``requested_warmup_runs``: the request's ``warmup_runs``.
    - ``requested_runs``: the request's ``runs``.
    - ``forks_started``: the forks whose records were received.
    - ``forks_completed``: the forks that finished every requested iteration
      with a complete record.
    - ``warmup_completed``: the warm-up iterations with a complete record,
      over all forks.
    - ``measurements_completed``: the measured iterations with a complete
      record, over all forks.
    - ``iterations``: the record of every iteration, fork by fork.
    - ``forks``: the record of each fork.
    - ``operation_elapsed_s``: the operation's elapsed time in seconds, None
      when the language does not offer the operation or the adapter refused
      to start it.
    - ``identity``: the functional settings and inputs.
    - ``requested_cpus``: the request's ``cpus``.
    - ``allowed_cpus``: the processors every fork was allowed alike
      (``WarmedFork.allowed_cpus``), None when they differ or none was read.
    - ``cancelled``: whether the operation was cancelled. A cancelled
      operation raises before its result is built, so a result holds False.
    - ``disposal_confirmed``: whether the end of every fork's process was
      confirmed.
    - ``diagnostics``: notes on the run, such as a progress observer's
      failure, a failed cleanup or the output of a failed setup step.
    - ``mode``: ``warmed_jvm``.
    - ``timing_boundary``: ``junit_launcher_execute``: a sample times
      ``Launcher.execute`` of the test plan.
    - ``clock``: ``java.lang.System.nanoTime``, the clock of the samples.
    - ``unit``: ``ns``, the unit of the samples.
    - ``state_policy``: the request's ``state_policy``, ``native_lifecycle``.
    - ``comparison``: always ``not_assessed``.
    - ``mean_of_fork_means_ms``: the mean of the forks' ``mean_ms``.
    - ``median_of_fork_medians_ms``: the median of the forks' ``median_ms``.
    - ``sample_min_ms``: the shortest sample of all forks in milliseconds.
    - ``sample_max_ms``: the longest sample of all forks in milliseconds.
    - ``between_fork_mean_sd_ms``: the sample standard deviation of the
      forks' ``mean_ms``, None with a single fork.

    The five summaries and the forks' statistics are present only when
    ``validity`` is ``complete``, ``outcome`` is ``completed``, the
    operation was not cancelled and the end of every fork was confirmed.
    """

    outcome: Outcome
    reason: str
    validity: str
    requested_forks: int
    requested_warmup_runs: int
    requested_runs: int
    forks_started: int
    forks_completed: int
    warmup_completed: int
    measurements_completed: int
    iterations: tuple[WarmedIteration, ...]
    forks: tuple[WarmedFork, ...]
    operation_elapsed_s: float | None
    identity: WarmedBenchmarkIdentity
    requested_cpus: tuple[int, ...] | None
    allowed_cpus: tuple[int, ...] | None
    cancelled: bool
    disposal_confirmed: bool
    diagnostics: tuple[str, ...] = ()
    mode: str = "warmed_jvm"
    timing_boundary: str = "junit_launcher_execute"
    clock: str = "java.lang.System.nanoTime"
    unit: str = "ns"
    state_policy: str = "native_lifecycle"
    comparison: str = "not_assessed"
    mean_of_fork_means_ms: float | None = None
    median_of_fork_medians_ms: float | None = None
    sample_min_ms: float | None = None
    sample_max_ms: float | None = None
    between_fork_mean_sd_ms: float | None = None

    def __post_init__(self) -> None:
        for name in ("iterations", "forks", "diagnostics"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for name in ("requested_cpus", "allowed_cpus"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, tuple(value))
            _cpus(getattr(self, name), name)
        if self.validity not in ("complete", "incomplete", "non_repeatable", "no_samples"):
            raise InvalidRequest("validity", "unknown warmed observation validity")
        if not isinstance(self.outcome, Outcome):
            raise InvalidRequest("outcome", "must be Outcome")
        for name in ("requested_forks", "requested_warmup_runs", "requested_runs"):
            _count(getattr(self, name), name, minimum=1)
        for name in ("forks_started", "forks_completed", "warmup_completed",
                     "measurements_completed"):
            _count(getattr(self, name), name)
        if self.operation_elapsed_s is not None:
            _duration(self.operation_elapsed_s, "operation_elapsed_s")
        _counters(self)
        if not isinstance(self.cancelled, bool) or not isinstance(self.disposal_confirmed, bool):
            raise InvalidRequest("cancelled", "cancellation and disposal flags must be bool")
        if not isinstance(self.identity, WarmedBenchmarkIdentity):
            raise InvalidRequest("identity", "must be WarmedBenchmarkIdentity")
        _metadata(self)
        for name, expected in (("clock", "java.lang.System.nanoTime"), ("unit", "ns"),
                               ("comparison", "not_assessed")):
            if getattr(self, name) != expected:
                raise InvalidRequest(name, f"must be {expected}")
        if (self.requested_forks, self.requested_warmup_runs, self.requested_runs,
                self.requested_cpus) != (self.identity.forks, self.identity.warmup_runs,
                                         self.identity.runs, self.identity.requested_cpus):
            raise InvalidRequest("identity", "the requested forks, warm-up runs, runs and CPUs "
                                 "must match the identity's")
        keys = []
        for iteration in self.iterations:
            if not isinstance(iteration, WarmedIteration):
                raise InvalidRequest("iterations", "must contain WarmedIteration records")
            maximum = self.requested_warmup_runs if iteration.kind == "warmup" else self.requested_runs
            if iteration.fork >= self.forks_started or iteration.repetition >= maximum:
                raise InvalidRequest("iterations", "exceeds admitted iteration range")
            keys.append((iteration.fork, iteration.kind, iteration.repetition))
        if len(set(keys)) != len(keys):
            raise InvalidRequest("iterations", "duplicate iteration record")
        for kind, counter in (("warmup", "warmup_completed"),
                              ("measurement", "measurements_completed")):
            observed = sum(item.report_complete for item in self.iterations if item.kind == kind)
            if observed != getattr(self, counter):
                raise InvalidRequest(counter, "does not match complete native records")
        fork_ids = []
        for fork in self.forks:
            if not isinstance(fork, WarmedFork) or fork.fork >= self.forks_started:
                raise InvalidRequest("forks", "invalid or unadmitted fork record")
            fork_ids.append(fork.fork)
            records = [item for item in self.iterations if item.fork == fork.fork]
            warmups = sum(item.report_complete for item in records if item.kind == "warmup")
            measured = sum(item.report_complete for item in records if item.kind == "measurement")
            samples = tuple(item.elapsed_ns for item in records if item.kind == "measurement"
                            and item.report_complete and item.elapsed_ns is not None)
            if (fork.warmup_completed, fork.measurements_completed, fork.samples_ns) != (
                    warmups, measured, samples):
                raise InvalidRequest("forks", "counters/samples do not match native records")
            if fork.complete and (warmups != self.requested_warmup_runs or measured != self.requested_runs):
                raise InvalidRequest("forks", "complete fork lacks requested records")
        if len(set(fork_ids)) != len(fork_ids):
            raise InvalidRequest("forks", "duplicate fork record")
        if sum(fork.complete for fork in self.forks) != self.forks_completed:
            raise InvalidRequest("forks_completed", "does not match complete native forks")
        if self.validity == "complete":
            if (self.forks_completed != self.requested_forks or not self.disposal_confirmed
                    or self.cancelled or any(not item.report_complete or item.elapsed_ns is None
                                            for item in self.iterations)):
                raise InvalidRequest("validity", "complete requires all records and confirmed disposal")
            patterns = [tuple(sorted((item.unique_id, item.kind, item.status)
                                     for item in iteration.tests)) for iteration in self.iterations]
            if not patterns or not patterns[0] or any(pattern != patterns[0] for pattern in patterns):
                raise InvalidRequest("validity", "complete requires stable native population/outcomes")
            if (self.outcome is Outcome.COMPLETED and any(
                    iteration.outcome is not Outcome.COMPLETED for iteration in self.iterations)):
                raise InvalidRequest("outcome", "completed observation contains failed native iteration")
            if self.outcome is Outcome.COMPLETED and any(
                    any(test.status == "failed" for test in iteration.tests) or not any(
                        test.kind == "test" and test.status == "successful" for test in iteration.tests)
                    for iteration in self.iterations):
                raise InvalidRequest("outcome", "successful timing requires native passing test evidence")
            if self.outcome is Outcome.COMPLETED:
                for iteration in self.iterations:
                    for name, kind, status in (("passed", "test", "successful"),
                                               ("skipped", "test", "skipped"),
                                               ("aborted", "test", "aborted"),
                                               ("failed", "test", "failed"),
                                               ("container_failures", "container", "failed")):
                        count = getattr(iteration.counts, name)
                        observed = sum(test.kind == kind and test.status == status
                                       for test in iteration.tests)
                        if count is not None and count != observed:
                            raise InvalidRequest("counts", f"{name} contradicts native {iteration.kind} "
                                                 f"test evidence in fork {iteration.fork}, "
                                                 f"repetition {iteration.repetition}")
        summaries = (self.mean_of_fork_means_ms, self.median_of_fork_medians_ms,
                     self.sample_min_ms, self.sample_max_ms, self.between_fork_mean_sd_ms)
        for value in summaries:
            if value is not None:
                _duration(value, "summary")
        if (self.validity != "complete" or self.outcome is not Outcome.COMPLETED
                or self.cancelled or not self.disposal_confirmed) and (
                any(value is not None for value in summaries) or any(
                    value is not None for fork in self.forks
                    for value in (fork.mean_ms, fork.median_ms, fork.min_ms, fork.max_ms))):
            raise InvalidRequest("summary", "unverified warmed observations have no summaries")
        if self.requested_forks == 1 and self.between_fork_mean_sd_ms is not None:
            raise InvalidRequest("between_fork_mean_sd_ms", "requires at least two forks")


#: The outcome of one test. A test whose setup or teardown fails is an
#: error, and an unexpected pass of a test marked to fail counts as passed.
PASSED = "passed"
#: A test whose own code failed, such as an assertion.
FAILED = "failed"
#: A test whose setup or teardown failed.
ERROR = "error"
#: A test the run skipped.
SKIPPED = "skipped"
#: A test marked to fail that failed.
XFAILED = "xfailed"

#: What a run's wall time covers.
WALL_TIME_SCOPE = ("the runner's process from start to exit: interpreter "
                   "start, collection, the tests and, when coverage was "
                   "requested, its tracing and data writing")


#: The largest session seed: Python refuses a larger PYTHONHASHSEED.
_MAX_SEED = 2**32 - 1


@dataclass(frozen=True)
class SuiteRunRequest:
    """A run of ``suite`` with a timeout in seconds. ``coverage_target``
    names a file (relative to the project root, or absolute under it) whose
    coverage the run measures. ``runner_options`` are the language's own
    (for Python, ``PytestOptions``). The side effects are off by default:

    - ``isolation_config``: the project's test configuration, copied into the
      suite root and used from there.
    - ``remove_created_files``: before the run, removes the files that earlier
      runs of this adapter with this option created in the project tree, not
      files written between runs.
    - ``timeout_journal``: the per-test journal, from which the result names
      the test that was running when a timeout or a crash ended the run
      (``SuiteRun.active_at_end``), written by a plugin of the adapter, or
      by the determinism instrument a suite's setup file carries.

    ``record_leaks`` records the tests that added, rebound or removed a name
    of the module under test or left xarray's options or numpy's error or
    print settings changed (``SuiteRun.leaks``): the determinism instrument
    records them when the suite's setup file carries one, else a read-only
    observer loaded with the run, which compares each test's start and end
    and restores nothing. ``module_under_test`` (by default the coverage
    target) is the module it compares. ``session_seed`` runs the suite as
    delivered: with Python's hash seed, the global
    generators of ``random``, numpy and sympy, and hypothesis's seed option
    set to it once when the session starts, 0 to ``2**32 - 1`` as Python's
    hash seed allows. ``reverse_order`` runs the tests
    in the reverse of their collected order. ``timeout_s`` is the timeout in
    seconds."""

    suite: Suite
    timeout_s: float
    coverage_target: Optional[str] = None
    runner_options: Any = None
    module_under_test: Optional[str] = None
    isolation_config: bool = False
    remove_created_files: bool = False
    record_leaks: bool = False
    session_seed: Optional[int] = None
    reverse_order: bool = False
    timeout_journal: bool = False

    def validate(self) -> None:
        """Refuse an invalid suite, timeout or session seed with
        ``InvalidRequest``, naming the field."""
        self.suite.validate()
        _duration(self.timeout_s, "timeout_s", positive=True)
        if self.session_seed is not None and (
                not isinstance(self.session_seed, int) or isinstance(
                    self.session_seed, bool) or not 0 <= self.session_seed <= _MAX_SEED):
            raise InvalidRequest("session_seed",
                                 f"must be an integer from 0 to {_MAX_SEED}, Python's hash seed range")


@dataclass(frozen=True)
class CollectionRequest:
    """Collect the listed suite without running test bodies. Collection
    still runs imports, setup files and collection hooks. Configuration
    isolation is a side effect, as for a suite run.

    - ``suite``: the suite to collect.
    - ``timeout_s``: the timeout of the collection in seconds.
    - ``runner_options``: the language's own options (for Python,
      ``PytestOptions``).
    - ``isolation_config``: copies the project's test configuration into
      the suite root and uses it from there.
    """

    suite: Suite
    timeout_s: float
    runner_options: Any = None
    isolation_config: bool = False

    def validate(self) -> None:
        """Refuse an invalid suite or timeout with ``InvalidRequest``,
        naming the field."""
        self.suite.validate()
        _duration(self.timeout_s, "timeout_s", positive=True)


@dataclass(frozen=True)
class TestOutcome:
    """One test's outcome and its time in seconds as the runner reported
    it (setup, call and teardown), absent when the report has none. A
    failed or erroneous test's ``message`` is its report's message,
    ``trace`` the report's text (the exception, its message and the
    stack, as the runner wrote them) and ``exception`` the exception's
    type where the report names it. ``time_s`` is that time in seconds."""

    __test__ = False  # not a test class for pytest's collection

    test: TestId
    outcome: str
    time_s: Optional[float] = None
    message: str = ""
    trace: str = ""
    exception: str = ""


@dataclass(frozen=True)
class RunCounts:
    """The run's tests by outcome, each absent when the runner did not
    report it. ``collected`` counts every test with an outcome. A test
    marked to fail that failed counts as skipped, as the runner's report
    has it."""

    collected: Optional[int] = None
    passed: Optional[int] = None
    failed: Optional[int] = None
    errors: Optional[int] = None
    skipped: Optional[int] = None


@dataclass(frozen=True)
class CoverageObservation:
    """The coverage of one target: its statements and the statements the
    run covered, and its branches and the branches the run took, in the
    language's own representation. For Python these are coverage.py's arcs
    between lines, where a negative destination leaves the code object that
    starts at that line. Percentages are derived from these sets."""

    target: FileIdentity
    statements: Tuple[int, ...]
    covered: Tuple[int, ...]
    branches: Tuple[BranchItem, ...]
    taken: Tuple[BranchItem, ...]
    #: Raw lines traced by the native instrument, including excluded lines.
    #: These do not contribute to ``line_percent`` or the statement matrix.
    traced_lines: Optional[Tuple[int, ...]] = None

    @property
    def line_percent(self) -> Optional[float]:
        """The share of the target's statements the suite covered, in
        percent, None without statements."""
        # Scaled before dividing: 23 of 40 is exactly 57.5, where dividing
        # first gives 57.49999999999999, which rounds to 57 instead of 58.
        if not self.statements:
            return None
        return 100.0 * len(self.covered) / len(self.statements)

    @property
    def branch_percent(self) -> Optional[float]:
        """The share of the target's branches the suite took, in percent, None
        without branches."""
        if not self.branches:
            return None
        return 100.0 * len(self.taken) / len(self.branches)

    def missing(self) -> Tuple[int, ...]:
        """The target's statements the suite did not cover, in order."""
        covered = set(self.covered)
        return tuple(n for n in self.statements if n not in covered)

    def missing_branches(self) -> Tuple[BranchItem, ...]:
        """The target's branches the suite did not take, in order."""
        taken = set(self.taken)
        return tuple(b for b in self.branches if b not in taken)

    def to_coverage_matrix(self) -> Dict[str, Any]:
        """The observation as a coverage matrix (schema 1): every
        statement covered or missing, every branch taken or missing, and
        the counts. The arcs keep the tool's destinations."""
        covered = set(self.covered)
        taken = set(self.taken)
        lines = [{"line": n, "status": "covered" if n in covered
                  else "missing"} for n in self.statements]
        branches = [{"from": b.source, "to": b.destination,
                     "status": "taken" if b in taken else "missing"}
                    for b in sorted(self.branches,
                                    key=lambda b: (b.source, b.destination))]
        return {
            "schema_version": 1,
            "target_file": self.target.path,
            "lines": lines,
            "branches": branches,
            "summary": {
                "total_lines": len(self.statements),
                "covered_lines": len(self.covered),
                "missing_lines": len(self.statements) - len(self.covered),
                "covered_branches": len(self.taken),
                "missing_branches": len(self.branches) - len(self.taken),
            },
        }


@dataclass(frozen=True)
class SideEffects:
    """What a run did beyond executing the suite: the ``instrument`` that ran
    with it and where it came from (``instrument_source``: ``plugin``, loaded
    from the adapter's own directory, or ``suite``, the suite's setup file
    carrying it), the files it wrote into the suite's root, by name
    (``files_written``), the files it removed, relative to the project root
    (``files_removed``), whether the leak record was taken (``leak_record``),
    the session seed of a run as delivered, whether the order was reversed
    and whether the run wrote the per-test timeout journal."""

    instrument: Optional[InstrumentIdentity] = None
    instrument_source: Optional[str] = None
    files_written: Tuple[str, ...] = ()
    files_removed: Tuple[str, ...] = ()
    leak_record: bool = False
    session_seed: Optional[int] = None
    reverse_order: bool = False
    timeout_journal: bool = False


@dataclass(frozen=True)
class Leak:
    """A test that left shared state changed, as the leak record (the
    instrument's, or without it the read-only observer's) found it once the
    test's own fixtures were torn down: the names of the module under test
    it added, rebound to another
    object and removed (names that begin and end with a double underscore
    left out), and the xarray options and numpy error and print settings it
    left changed. ``xarray_options`` holds the names of the xarray options
    it changed, ``numpy_errors`` those of numpy's error settings and
    ``numpy_print`` those of numpy's print options."""

    test: TestId
    added: Tuple[str, ...] = ()
    rebound: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    xarray_options: Tuple[str, ...] = ()
    numpy_errors: Tuple[str, ...] = ()
    numpy_print: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RunDiagnostic:
    """A native diagnostic kept independently of the bounded process log.

    ``kind`` names what it reports:

    - ``unsound_test``: a warning that marks a test leaving shared state
      behind (``LanguageFacts.unsound_test_patterns``).
    - ``active_at_end``: a test the timeout journal shows active at its last
      update. ``journal_unavailable``: why the journal could not be read.
    - ``coverage_counts_inconsistent``: gcovr refused a C run's coverage
      counters because gcov read a negative count, or one of 2^32 or more,
      from them. ``message`` is gcovr's message. The run has no coverage,
      and its tests' outcomes stay as they ran. A run whose tests all passed is then an
      infrastructure failure with that message as its reason, and a run
      with another outcome, such as failing tests, keeps it. A C run of
      whole programs then holds one more such diagnostic for each test
      whose own counts gcovr refuses, with ``test`` and that test's
      message.
    - ``compile_error``: a C test file did not compile or link, and the run
      is ``collection_failed``. ``message`` is the step that failed, such
      as "the test file test_calc.c does not compile". The compiler's or
      linker's error lines are the run's ``reason_detail``.

    ``test`` is absent when attribution is unavailable. ``native_test``
    retains the tool's original attribution without claiming a matched test.
    """

    kind: str
    message: str
    test: Optional[TestId] = None
    native_test: Optional[str] = None


def with_detail(reason: str, detail: Tuple[str, ...], lines: Optional[int] = 3) -> str:
    """``reason`` followed by the last ``lines`` lines of ``detail`` (all of
    them for None), as "reason: line | line"."""
    shown = detail if lines is None else detail[-lines:] if lines > 0 else ()
    return f"{reason}: {' | '.join(shown)}" if shown else reason


@dataclass(frozen=True)
class SuiteRun:
    """The result of a suite run. ``tests`` holds every test the runner
    reported, and ``unmatched`` the report entries that map to no test of
    the suite. ``wall_time_s`` covers ``WALL_TIME_SCOPE``. ``output`` is the
    runner's output, whole up to four million characters. ``reason_detail``
    holds the complete lines a ``reason`` summarizes (a Python runner's
    output lines when it exited without a usable verdict). ``reason_text``
    shows the reason with some or all of them. ``coverage`` is absent when
    not requested or not measured. ``leaks`` holds the tests the leak record
    names, in the order they finished. It is absent when the record was not
    requested or could not be taken (the suite's own instrument does not
    record leaks, or the run ended before loading it). ``active_at_end`` is
    the test the timeout journal shows running when the run ended before
    that test finished, as on a timeout or a crash: an observation, not
    proof that this test alone ends the run. It is absent without the
    journal, when every started test finished, and when the run ended
    between tests. ``coverage_report_s`` is the time of reading the coverage
    data after the runner exited (coverage.py's JSON report, JaCoCo's
    analysis), and ``coverage_statements_s`` the time of reading coverage.py's
    statements of the target from that data. Each is absent when its step
    did not run. ``runner_counts`` holds the counts the runner's own summary
    gives, where the language has one (pytest's): a test whose teardown
    also fails counts as one failure and one error, and an unexpected pass
    of a test marked to fail as neither. ``collected`` counts the tests
    that passed, failed or raised an error, less one per file the runner
    could not collect. It is None where the runner gave no counts (a run
    that ended before its session did). ``side_effects`` states what the
    run did beyond executing the suite."""

    outcome: Outcome
    reason: str
    counts: RunCounts
    tests: Tuple[TestOutcome, ...]
    unmatched: Tuple[str, ...]
    wall_time_s: Optional[float]
    output: str
    coverage: Optional[CoverageObservation]
    identity: ObservationIdentity
    side_effects: SideEffects = field(default_factory=SideEffects)
    leaks: Optional[Tuple[Leak, ...]] = None
    active_at_end: Optional[TestId] = None
    returncode: Optional[int] = None
    diagnostics: Tuple[RunDiagnostic, ...] = ()
    reason_detail: Tuple[str, ...] = ()
    coverage_report_s: Optional[float] = None
    coverage_statements_s: Optional[float] = None
    runner_counts: Optional[RunCounts] = None

    def reason_text(self, lines: Optional[int] = 3) -> str:
        """The reason followed by the last ``lines`` lines of its detail
        (all of them for None), as "reason: line | line". Three lines by
        default."""
        return with_detail(self.reason, self.reason_detail, lines)

    def failing_tests(self) -> Tuple[TestId, ...]:
        """The tests that failed or raised an error."""
        return tuple(t.test for t in self.tests
                     if t.outcome in (FAILED, ERROR))

    def outcome_of(self, test: TestId) -> Optional[str]:
        """The outcome of ``test`` in this run, None when the run did not
        report it."""
        for t in self.tests:
            if t.test == test:
                return t.outcome
        return None


@dataclass(frozen=True)
class CollectionRun:
    """A collect-only observation, not a verdict that tests passed.

    ``diagnostic`` retains the collection exception summary even when it lies
    outside bounded output. Neither coverage nor per-test execution is measured.
    ``files`` holds the test files the runner collected tests from or could
    not collect, POSIX and relative to the suite's root, where the runner
    names them (pytest's). ``side_effects`` states what the collection did
    beyond collecting the suite.
    """

    outcome: Outcome
    reason: str
    identity: ObservationIdentity
    output: str
    diagnostic: str = ""
    returncode: Optional[int] = None
    wall_time_s: Optional[float] = None
    side_effects: SideEffects = field(default_factory=SideEffects)
    files: Tuple[str, ...] = ()


#: The evidence holds no kill items because the request gave no mutation
#: run (``PerTestEvidence.kills``).
KILLS_NOT_REQUESTED = "not_requested"
#: Every killer of the mutation run maps to a test of the suite, and the
#: evidence holds the kill items.
KILLS_USABLE = "usable"
#: A killer of the mutation run maps to no test of the suite
#: (``PerTestEvidence.unmapped_killers``), so the evidence holds no kill
#: items.
KILLS_UNUSABLE = "unusable"


@dataclass(frozen=True)
class PerTestEvidenceRequest:
    """Per-test evidence of ``suite`` for ``target`` (a file relative to the
    project root, or absolute under it): the target's lines and arcs each
    test covers, its time, and, from ``mutation_run`` (a ``MutationRun`` of
    the same suite), the mutants it kills. ``warmup`` runs the suite once
    before the measured run and discards it. The other fields are those of
    a ``SuiteRunRequest``, and ``module_under_test`` defaults to the target.

    - ``timeout_s``: the timeout in seconds.
    - ``runner_options``: the language's own options (for Python,
      ``PytestOptions``).
    - ``isolation_config``: copies the project's test configuration into
      the suite root and uses it from there.
    - ``remove_created_files``: before the run, removes the files that
      earlier runs of this adapter with this option created in the project
      tree.
    """

    suite: Suite
    target: str
    timeout_s: float
    warmup: bool = True
    mutation_run: Any = None
    runner_options: Any = None
    module_under_test: Optional[str] = None
    isolation_config: bool = False
    remove_created_files: bool = False

    def validate(self) -> None:
        """Refuse an invalid suite or timeout, or a missing target, with
        ``InvalidRequest``, naming the field."""
        self.suite.validate()
        _duration(self.timeout_s, "timeout_s", positive=True)
        if not self.target:
            raise InvalidRequest("target", "is required")


@dataclass(frozen=True)
class TestEvidence:
    """One test's evidence: the items it covers or kills, and its time in
    milliseconds from the runner's report (setup, call and teardown),
    absent when the report has no time for it. A parametrized function is
    one test here, its cases' times summed, since coverage is recorded per
    function. ``time_ms`` is that time."""

    __test__ = False  # not a test class for pytest's collection

    test: TestId
    items: FrozenSet[EvidenceItem]
    time_ms: Optional[float]

    def tokens(self) -> FrozenSet[str]:
        """The test's items as canonical tokens."""
        return frozenset(item.token() for item in self.items)


@dataclass(frozen=True)
class ContextEvidence:
    """Items recorded under a native context that names no reported test.

    These are diagnostic observations, not attribution to an invented test.
    """

    context: str
    items: FrozenSet[EvidenceItem]


@dataclass(frozen=True)
class PerTestEvidence:
    """Per-test evidence of a green run. ``tests`` holds every test the
    runner reported, ``untimed`` the tests without a time, and
    ``unmatched_contexts`` the coverage contexts that name no test of the
    suite (their items belong to no test). Code that runs when a module is
    imported is excluded from every test's items, since it runs whenever
    any kept test imports the module.
    ``outside_items`` are the target's lines and branches the suite covered
    that no test's items hold (``items_outside_tests``).
    ``kills`` says whether kill items are present: ``usable`` when every
    killer of the mutation run maps to a test of the suite, else
    ``unusable`` with ``unmapped_killers`` and no kill items, or
    ``not_requested``. ``coverage`` is the suite's coverage of the target.
    A run that is not green carries no evidence. ``stopping_tests`` are the
    tests a measurement's failure is pinned on, in the order its run met
    them, each once: a test that did not pass in the measuring run, or a
    test whose own coverage counts in that run cannot be read or are
    missing. Each is the test id the suite run gives that test. Empty when
    the evidence is complete or no test can be named. ``suite_time_ms`` is
    the suite's time in milliseconds: for Python and Java the sum of the
    tests' times, None when no test has one, and for C the elapsed time of
    the measured runs. ``side_effects`` states what the runs did beyond
    executing the suite."""

    outcome: Outcome
    reason: str
    tests: Tuple[TestEvidence, ...]
    untimed: Tuple[TestId, ...]
    unmatched_contexts: Tuple[str, ...]
    kills: str
    unmapped_killers: Tuple[str, ...]
    suite_time_ms: Optional[float]
    coverage: Optional[CoverageObservation]
    identity: ObservationIdentity
    side_effects: SideEffects = field(default_factory=SideEffects)
    unmatched_items: Tuple[ContextEvidence, ...] = ()
    outside_items: FrozenSet[EvidenceItem] = frozenset()
    stopping_tests: Tuple[TestId, ...] = ()

    def tokens(self) -> Dict[TestId, FrozenSet[str]]:
        """Each test's evidence as canonical tokens."""
        return {e.test: e.tokens() for e in self.tests}


def items_outside_tests(coverage: Optional[CoverageObservation],
                        tests: Sequence[TestEvidence], file: str) -> FrozenSet[EvidenceItem]:
    """The target's lines (``file`` names it as the tests' items do) and
    branches that the suite covered and no test's items hold: code that ran
    outside every test, at import, in a fixture, ``setUp`` or
    ``tearDown``, in another thread, in a class's static initialization or
    a ``@BeforeAll``. A reduction that keeps every test's items keeps these
    only through the code that runs them. Empty without the suite's
    coverage."""
    if coverage is None:
        return frozenset()
    held = {item for test in tests for item in test.items}
    covered = {LineItem(file, line) for line in coverage.covered} | set(coverage.taken)
    return frozenset(covered - held)


#: A processor list as ``taskset -c`` takes it: numbers and ranges.
_CPU_LIST = re.compile(r"\d+(-\d+)?(,\d+(-\d+)?)*")


@dataclass(frozen=True)
class BenchmarkProgress:
    """One finished repetition: its kind, 1-based index, total and time.

    The event precedes classification of the repetition's exit status.
    It is progress, not evidence that the repetition passed.
    ``kind`` is ``warmup`` or ``measured``, and ``elapsed_ms`` is the
    repetition's time in milliseconds.
    """

    kind: str
    index: int
    total: int
    elapsed_ms: float


def _report_progress(request: "BenchmarkRequest", index: int, elapsed_ms: float) -> None:
    """Report a benchmark's finished repetition ``index`` (warm-ups first) to
    the request's ``progress``: every warm-up run, and of the measured ones
    the first, the last and about every quarter. A callback never changes
    the result."""
    if request.progress is None:
        return
    if index < request.warmup_runs:
        event = BenchmarkProgress("warmup", index + 1, request.warmup_runs, elapsed_ms)
    else:
        repetition = index - request.warmup_runs + 1
        if (repetition not in (1, request.runs)
                and repetition % max(1, request.runs // 4) != 0):
            return
        event = BenchmarkProgress("measured", repetition, request.runs, elapsed_ms)
    try:
        request.progress(event)
    except Exception:  # a callback never changes the result
        pass


@dataclass(frozen=True)
class BenchmarkRequest:
    """The timing of ``suite``: ``warmup_runs`` repetitions that are
    discarded, then ``runs`` measured ones, each limited to ``timeout_s``.
    The placement: ``serialize`` waits for the other benchmarks of this
    process, and ``cpus`` is the processor set the repetitions run on where
    the project runs, as ``taskset -c`` takes it (``0-3,8``). The other
    fields are those of a ``SuiteRunRequest``, and a benchmark measures no
    coverage. ``progress`` receives an event after each warm-up
    repetition, and after the first, the last and every
    ``max(1, runs // 4)``-th measured repetition. A callback failure never
    changes the measured result.

    - ``runner_options``: the language's own options (for Python,
      ``PytestOptions``).
    - ``module_under_test``: the module that an instrument carried by a
      setup file of the suite restores before each test.
    - ``isolation_config``: copies the project's test configuration into
      the suite root and uses it from there.
    - ``remove_created_files``: before the repetitions, removes the files
      that earlier runs of this adapter with this option created in the
      project tree.
    """

    suite: Suite
    runs: int
    timeout_s: float
    warmup_runs: int = 1
    serialize: bool = True
    cpus: Optional[str] = None
    runner_options: Any = None
    module_under_test: Optional[str] = None
    isolation_config: bool = False
    remove_created_files: bool = False
    progress: Optional[Callable[[BenchmarkProgress], None]] = field(
        default=None, repr=False, compare=False)

    def validate(self) -> None:
        """Refuse an invalid suite, repetition count, timeout, processor
        list or progress callback with ``InvalidRequest``, naming the
        field."""
        self.suite.validate()
        _count(self.runs, "runs", minimum=1)
        _count(self.warmup_runs, "warmup_runs")
        _duration(self.timeout_s, "timeout_s", positive=True)
        if self.cpus is not None and not _CPU_LIST.fullmatch(self.cpus):
            raise InvalidRequest(
                "cpus", "must be a processor list such as 0-3,8")
        if self.progress is not None and not callable(self.progress):
            raise InvalidRequest("progress", "must be callable")


@dataclass(frozen=True)
class Benchmark:
    """The measured repetitions' times in milliseconds, and what they cover
    (``includes``). A time is the elapsed time of the processes ``includes``
    names, as their supervisor timed them where it does, else as timed
    around them. ``warmup_runs`` counts the repetitions discarded before
    them, ``cpus`` is the processor set given, and ``allowed_cpus`` the
    affinity of a process started like them, read once before them: an
    allowed set, not the processors used, and None where the platform
    reports none or the reading failed. Only a benchmark whose every
    repetition passed has statistics. One that ended early (a repetition
    that did not pass, the timeout) keeps the times of the measured
    repetitions that passed before it, and its outcome says why.
    ``returncode`` is the final repetition's exit code, absent when it did
    not exit normally (such as a timeout) or the request could not be
    executed. ``times_ms`` holds the measured times in order, and
    ``side_effects`` states what the repetitions did beyond executing the
    suite."""

    outcome: Outcome
    reason: str
    times_ms: Tuple[float, ...]
    warmup_runs: int
    cpus: Optional[str]
    includes: str
    identity: ObservationIdentity
    side_effects: SideEffects = field(default_factory=SideEffects)
    allowed_cpus: Optional[Tuple[int, ...]] = None
    returncode: Optional[int] = None

    def _complete(self) -> bool:
        return self.outcome is Outcome.COMPLETED and bool(self.times_ms)

    @property
    def median_ms(self) -> Optional[float]:
        """The median of ``times_ms``, None unless the outcome is
        ``completed`` with times."""
        return statistics.median(self.times_ms) if self._complete() else None

    @property
    def mean_ms(self) -> Optional[float]:
        """The mean of ``times_ms``, None unless the outcome is
        ``completed`` with times."""
        return statistics.fmean(self.times_ms) if self._complete() else None

    @property
    def min_ms(self) -> Optional[float]:
        """The shortest of ``times_ms``, None unless the outcome is
        ``completed`` with times."""
        return min(self.times_ms) if self._complete() else None

    @property
    def max_ms(self) -> Optional[float]:
        """The longest of ``times_ms``, None unless the outcome is
        ``completed`` with times."""
        return max(self.times_ms) if self._complete() else None


@dataclass(frozen=True)
class SuiteProfileRequest:
    """One profiled run of ``suite``, limited to ``timeout_s`` seconds, with
    the language's ``runner_options`` (for Python, ``PytestOptions``)."""

    suite: Suite
    timeout_s: float
    runner_options: Any = None

    def validate(self) -> None:
        """Refuse an invalid suite or timeout with ``InvalidRequest``,
        naming the field."""
        self.suite.validate()
        _duration(self.timeout_s, "timeout_s", positive=True)


@dataclass(frozen=True)
class ProfiledFunction:
    """One function a profiled run entered: its file (``path``, relative to
    the project root where the file lies inside it, else as the profiler
    names it), ``name`` and ``start_line``, its ``calls``, ``own_time_s``
    (its own code), ``inclusive_time_s`` (with its callees) and ``share``,
    its own time divided by the run's total own time. Times are seconds of
    the profiled run, the profiler's overhead included."""

    path: str
    name: str
    start_line: int
    calls: int
    own_time_s: float
    inclusive_time_s: float
    share: float


@dataclass(frozen=True)
class SuiteProfile:
    """Where one profiled run of a suite spent its time. A suite profile
    attributes time but does not time the suite: its times include the
    profiler's overhead, and ``profiler``, ``profiler_version`` and ``kind``
    say how they arose. A ``deterministic`` profiler adds a cost at every
    call and return, so often-called functions appear more expensive.
    ``total_own_time_s`` is the profiled run's total own time, not the
    suite's time, which a benchmark measures. Only a run whose every test
    passed has ``functions``, most own time first. Otherwise ``outcome``
    and ``reason`` say why."""

    outcome: Outcome
    reason: str
    profiler: Optional[str]
    profiler_version: Optional[str]
    kind: Optional[str]
    total_own_time_s: Optional[float]
    functions: Tuple[ProfiledFunction, ...] = ()


# Warmed suite timing (``tests.benchmark_warmed``), defined beside its validation.


def summarize(result: WarmedSuiteBenchmark) -> WarmedSuiteBenchmark:
    """Retain raw evidence and summarize only a globally eligible observation."""
    if not isinstance(result, WarmedSuiteBenchmark):
        raise InvalidRequest("result", "must be WarmedSuiteBenchmark")
    if (result.validity != "complete" or result.outcome is not Outcome.COMPLETED
            or result.cancelled or not result.disposal_confirmed):
        return result
    # Recheck received record invariants before calculating any headline.
    result = replace(result)

    # Rational intermediates keep nanosecond-scale variation at large offsets.
    samples = tuple(tuple(Fraction(value, 1_000_000) for value in fork.samples_ns)
                    for fork in result.forks)
    try:
        means = tuple(mean(values) for values in samples)
        medians = tuple(median(values) for values in samples)
        forks = tuple(replace(fork, mean_ms=float(fork_mean), median_ms=float(fork_median),
                              min_ms=float(min(values)), max_ms=float(max(values)))
                      for fork, values, fork_mean, fork_median
                      in zip(result.forks, samples, means, medians))
        headline_mean = float(mean(means))
        headline_median = float(median(medians))
        sample_min = float(min(min(values) for values in samples))
        sample_max = float(max(max(values) for values in samples))
        between_fork_sd = stdev(means) if len(means) > 1 else None
    except (OverflowError, ValueError) as error:
        raise InvalidRequest("samples_ns", "timing summaries must be finite representable values") from error

    return replace(result, forks=forks, mean_of_fork_means_ms=headline_mean,
                   median_of_fork_medians_ms=headline_median,
                   sample_min_ms=sample_min, sample_max_ms=sample_max,
                   between_fork_mean_sd_ms=between_fork_sd)
