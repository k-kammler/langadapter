"""The warmed Java suite benchmark (``tests.benchmark_warmed``).

The project is copied into owned scratch (in a container, inside it) and
built, and the suite and helper compiled, once and untimed. Each fork runs
in its own copy of that idle build, one JVM running every warm-up and
measured iteration through the JUnit Platform Launcher helper, which times
only ``Launcher.execute``. A fork that fails, times out or changes its test
population or outcomes ends the measurement and is never retried.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from langadapter.runs import (
    WarmedIteration,
    WarmedTestCounts,
    WarmedTestObservation,
    WarmedFork,
    WarmedSuiteBenchmark,
    summarize,
    WarmedBenchmarkIdentity,
    WarmedBenchmarkProgress,
    WarmedSuiteBenchmarkRequest,
)
from langadapter.errors import InvalidRequest
from langadapter.outcomes import Outcome, FileIdentity, ToolchainIdentity
import subprocess
import time
from typing import Callable
from langadapter import _process_tree, placement
from langadapter._owned_directory import OwnershipRefused
import uuid
from langadapter._environment import selected_executable
from langadapter._execution_scope import reraise_unresolved
from langadapter.sources import bounded
from langadapter.context import ContainerExecution
from langadapter.execution import (
    ContainerExecutor,
    _retain_scratch,
    container_path,
    execution_identity,
    executor_for,
)
from langadapter.java import (
    _warmed_fork as _warmed_forks, _warmed_fork as _warmed_launch,
)
from langadapter.java._env import version_of
from langadapter.java._benchmark import (
    _allowed_cpus, _container_allowed_cpus, _container_taskset,
)
from langadapter.java._build import tool_environment
from langadapter.versions import unknown
from langadapter._prepared_copy import _copy_tree, _outside_source, copy_container_tree


# Private native-fork protocol. Reports do not certify process disposal.

MAX_REPORT_BYTES = 8 * 1024 * 1024
_COMMON = {"schema", "token", "fork", "event", "jvm_name", "jvm_start_ms"}
_NATIVE_OUTCOMES = {
    Outcome.COMPLETED, Outcome.NO_TESTS, Outcome.TEST_FAILURES,
    Outcome.COLLECTION_FAILED, Outcome.INFRASTRUCTURE_FAILURE,
}


class ProtocolError(ValueError):
    """A received report cannot establish the declared native observation."""


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProtocolError(f"{name} must be an integer of at least {minimum}")
    return value


def _text(value: object, name: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ProtocolError(f"{name} must be {'nonempty ' if nonempty else ''}text")
    return value


def _keys(value: object, expected: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ProtocolError(f"{name} has missing or unexpected fields")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ProtocolError(f"non-finite JSON constant: {value}")


@dataclass(frozen=True)
class ForkProtocol:
    token: str
    fork: int
    warmup_runs: int
    runs: int

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or re.fullmatch(r"[0-9a-f]{32}", self.token) is None:
            raise ProtocolError("token must be a fresh 32-character hexadecimal channel token")
        _integer(self.fork, "fork")
        _integer(self.warmup_runs, "warmup_runs", 1)
        _integer(self.runs, "runs", 1)

    def location(self, index: int) -> tuple[str, int]:
        _integer(index, "index")
        if index >= self.warmup_runs + self.runs:
            raise ProtocolError("iteration exceeds the requested fork budget")
        return (("warmup", index) if index < self.warmup_runs
                else ("measurement", index - self.warmup_runs))


@dataclass(frozen=True)
class NativeEvent:
    event: str
    jvm_name: str
    jvm_start_ms: int
    index: int | None = None
    iteration: WarmedIteration | None = None
    outcome: Outcome | None = None
    iterations_completed: int | None = None


def decode(data: bytes, protocol: ForkProtocol) -> NativeEvent:
    """Decode one helper report. A report with a missing or unexpected
    field is refused. A null value, such as an unknown count, stays None."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_REPORT_BYTES:
        raise ProtocolError("native report is empty or exceeds the report limit")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError(f"invalid native report: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("native report must be an object")
    event = value.get("event")
    extra = {
        "ready": {"warmup_runs", "runs"},
        "start": {"index", "kind", "repetition"},
        "iteration": {"index", "kind", "repetition", "elapsed_ns", "outcome",
                      "report_complete", "counts", "tests", "reason", "diagnostics"},
        "complete": {"outcome", "iterations_completed"},
    }
    if not isinstance(event, str) or event not in extra:
        raise ProtocolError("unknown native event")
    _keys(value, _COMMON | extra[event], "native report")
    if _integer(value["schema"], "schema", 1) != 1:
        raise ProtocolError("unsupported native report schema")
    if value["token"] != protocol.token or _integer(value["fork"], "fork") != protocol.fork:
        raise ProtocolError("native report belongs to another channel/fork")
    name = _text(value["jvm_name"], "jvm_name", nonempty=True)
    started = _integer(value["jvm_start_ms"], "jvm_start_ms")
    if event == "ready":
        if (_integer(value["warmup_runs"], "warmup_runs", 1),
                _integer(value["runs"], "runs", 1)) != (protocol.warmup_runs, protocol.runs):
            raise ProtocolError("helper admitted a different iteration budget")
        return NativeEvent(event, name, started)
    if event == "complete":
        outcome = _outcome(value["outcome"])
        count = _integer(value["iterations_completed"], "iterations_completed")
        return NativeEvent(event, name, started, outcome=outcome, iterations_completed=count)
    index = _integer(value["index"], "index")
    kind, repetition = protocol.location(index)
    if (value["kind"], _integer(value["repetition"], "repetition")) != (kind, repetition):
        raise ProtocolError("iteration location differs from its native index")
    if event == "start":
        return NativeEvent(event, name, started, index=index)
    elapsed = value["elapsed_ns"]
    if elapsed is not None:
        _integer(elapsed, "elapsed_ns")
        if elapsed > 2**63 - 1:
            raise ProtocolError("elapsed_ns exceeds the native signed-long duration")
    outcome = _outcome(value["outcome"])
    if type(value["report_complete"]) is not bool:
        raise ProtocolError("report_complete must be bool")
    if not isinstance(value["tests"], list) or not isinstance(value["diagnostics"], list):
        raise ProtocolError("native tests and diagnostics must be arrays")
    tests = []
    try:
        for test in value["tests"]:
            _keys(test, {"unique_id", "display_name", "kind", "status", "reason"}, "test")
            tests.append(WarmedTestObservation(**{
                key: _text(item, key, nonempty=key == "unique_id") for key, item in test.items()
            }))
        counts = WarmedTestCounts(**_keys(value["counts"], set(
            WarmedTestCounts.__dataclass_fields__), "counts"))
        iteration = WarmedIteration(
            protocol.fork, kind, repetition, elapsed, outcome, value["report_complete"],
            counts, tuple(tests), _text(value["reason"], "reason"),
            tuple(_text(item, "diagnostic") for item in value["diagnostics"]))
    except InvalidRequest as exc:
        raise ProtocolError(str(exc)) from exc
    _validate_iteration(iteration)
    return NativeEvent(event, name, started, index, iteration, outcome)


def _outcome(value: object) -> Outcome:
    try:
        outcome = Outcome(value)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("unknown native outcome") from exc
    if outcome not in _NATIVE_OUTCOMES:
        raise ProtocolError("outcome is not a native helper verdict")
    return outcome


def _validate_iteration(iteration: WarmedIteration) -> None:
    if not iteration.report_complete:
        if iteration.outcome is Outcome.COMPLETED:
            raise ProtocolError("incomplete native report cannot claim successful execution")
        return
    if iteration.elapsed_ns is None:
        raise ProtocolError("complete execution report has no native duration")
    for name, kind, status in (("passed", "test", "successful"), ("failed", "test", "failed"),
                               ("skipped", "test", "skipped"), ("aborted", "test", "aborted"),
                               ("container_failures", "container", "failed")):
        count = getattr(iteration.counts, name)
        if count is not None and count != sum(
                test.kind == kind and test.status == status for test in iteration.tests):
            raise ProtocolError(f"{name} contradicts native terminal identities")
    failures = any(test.status == "failed" for test in iteration.tests)
    passed = any(test.kind == "test" and test.status == "successful" for test in iteration.tests)
    if iteration.outcome is Outcome.COMPLETED and (failures or not passed):
        raise ProtocolError("successful native report requires a passing test and no failures")
    if iteration.outcome is Outcome.NO_TESTS and (passed or failures):
        raise ProtocolError("empty native verdict contradicts executed test outcomes")
    if iteration.outcome is Outcome.TEST_FAILURES and not failures:
        raise ProtocolError("test failure verdict has no native failure evidence")


def read(path: Path, protocol: ForkProtocol) -> NativeEvent:
    """The event of a bounded native report file in its owned scope."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ProtocolError("native report must be a regular owned file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ProtocolError("native report was replaced while opening")
        data = stream.read(MAX_REPORT_BYTES + 1)
    return decode(data, protocol)


class Transcript:
    """Ordered native receipt state, kept separate from OS ownership."""

    def __init__(self, protocol: ForkProtocol) -> None:
        self.protocol = protocol
        self.jvm: tuple[str, int] | None = None
        self.iterations: list[WarmedIteration] = []
        self.active_index: int | None = None
        self.finished = False
        self.outcome: Outcome | None = None

    @property
    def repeatable(self) -> bool | None:
        patterns = [tuple(sorted((test.unique_id, test.kind, test.status)
                                 for test in iteration.tests))
                    for iteration in self.iterations if iteration.report_complete]
        return None if not patterns else all(pattern == patterns[0] for pattern in patterns)

    def accept(self, event: NativeEvent) -> None:
        if self.finished:
            raise ProtocolError("native fork already completed")
        receipt = (event.jvm_name, event.jvm_start_ms)
        if event.event == "ready":
            if self.jvm is not None:
                raise ProtocolError("duplicate native admission")
            self.jvm = receipt
            return
        if self.jvm is None or receipt != self.jvm:
            raise ProtocolError("event has no matching admitted JVM receipt")
        if event.event == "start":
            if self.active_index is not None or event.index != len(self.iterations):
                raise ProtocolError("out-of-order native iteration admission")
            if self.iterations and self.iterations[-1].outcome is not Outcome.COMPLETED:
                raise ProtocolError("native fork resumed after an unsuccessful iteration")
            self.active_index = event.index
        elif event.event == "iteration":
            if self.active_index is None or event.index != self.active_index or event.iteration is None:
                raise ProtocolError("iteration report has no corresponding admission")
            self.iterations.append(event.iteration)
            self.active_index = None
        elif event.event == "complete":
            completed = sum(item.report_complete for item in self.iterations)
            if self.active_index is not None or event.iterations_completed != completed:
                raise ProtocolError("native completion lost an admitted iteration")
            if event.outcome is Outcome.COMPLETED:
                if (len(self.iterations) != self.protocol.warmup_runs + self.protocol.runs
                        or any(item.outcome is not Outcome.COMPLETED for item in self.iterations)):
                    raise ProtocolError("successful fork lacks its complete iteration budget")
            elif not self.iterations or event.outcome is not self.iterations[-1].outcome:
                raise ProtocolError("terminal native verdict differs from its last iteration")
            self.finished, self.outcome = True, event.outcome
        else:
            raise ProtocolError("unknown native event")


# Drive one acquired native fork through shared owned-command cleanup.

@dataclass(frozen=True)
class ForkExecution:
    transcript: Transcript
    outcome: Outcome
    reason: str
    diagnostics: tuple[str, ...]


#: How long, in seconds, an existing report that cannot be opened is retried:
#: on Windows a file just renamed into place can be held briefly (a scanner).
_DENIED_SECONDS = 1.0


def _remaining(deadline: float, owner) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(owner.args, 0)
    return remaining


def _receive(path: Path, transcript: Transcript, owner, deadline: float,
             observe: Callable[[NativeEvent], None]) -> NativeEvent:
    denied = None
    while True:
        remaining = _remaining(deadline, owner)
        if path.exists():
            try:
                event = read(path, transcript.protocol)
            except PermissionError:
                denied = time.monotonic() if denied is None else denied
                if time.monotonic() - denied >= _DENIED_SECONDS:
                    raise
                time.sleep(min(0.01, remaining))
                continue
            _remaining(deadline, owner)
            transcript.accept(event)
            observe(event)
            return event
        # A fork may write its report and exit between the two looks.
        if owner.poll() is not None and not path.exists():
            raise ProtocolError(f"a timing fork exited without writing {path.name}")
        time.sleep(min(0.01, remaining))


def _permit(directory: Path, protocol: ForkProtocol, index: int) -> None:
    protocol.location(index)
    temporary = directory / f".run-{index}.permit.tmp"
    destination = directory / f"run-{index}.permit"
    if destination.exists():
        raise ProtocolError("Iteration permit already exists")
    with temporary.open("xb") as stream:
        stream.write(protocol.token.encode("ascii"))
        stream.flush()
    os.replace(temporary, destination)


def drive(owner, directory: Path, protocol: ForkProtocol, *,
          total_deadline: float, startup_deadline: float, iteration_timeout_s: float,
          on_event: Callable[[NativeEvent], None] | None = None) -> ForkExecution:
    """Consume a fork the selected owned executor admitted, never a raw
    Popen. The caller owns the execution scope and scratch. The finite
    disposal path is always tried, and an uncertain disposal propagates with
    that owner."""
    transcript = Transcript(protocol)
    diagnostics = []
    primary = None
    outcome, reason = Outcome.INFRASTRUCTURE_FAILURE, "a timing fork did not finish"
    stop_for_pattern = False

    def observe(event):
        if on_event is not None:
            try:
                on_event(event)
            except Exception as error:  # observer failure is diagnostic only
                diagnostics.append(f"Progress observer failed: {error}")

    try:
        _receive(directory / "ready.json", transcript, owner,
                 min(total_deadline, startup_deadline), observe)
        for index in range(protocol.warmup_runs + protocol.runs):
            _remaining(total_deadline, owner)
            deadline = min(total_deadline, time.monotonic() + iteration_timeout_s)
            _permit(directory, protocol, index)
            _receive(directory / f"start-{index}.json", transcript, owner, deadline, observe)
            _receive(directory / f"iteration-{index}.json", transcript, owner, deadline, observe)
            if transcript.iterations[-1].outcome is not Outcome.COMPLETED:
                break
            if transcript.repeatable is False:
                stop_for_pattern = True
                outcome = Outcome.COMPLETED
                reason = "the tests that ran, or their outcomes, changed between iterations"
                break
        if not stop_for_pattern:
            _receive(directory / "complete.json", transcript, owner, total_deadline, observe)
            owner.wait(timeout=_remaining(total_deadline, owner))
            outcome = transcript.outcome
            if owner.returncode != 0:
                outcome, reason = Outcome.INFRASTRUCTURE_FAILURE, "a timing fork exited with an error"
            else:
                reason = transcript.iterations[-1].reason if transcript.iterations else ""
        else:
            # No next permit: stop the owned JVM, keeping received iterations.
            _process_tree.stop(owner)
        _process_tree.drain_output(owner)
    except BaseException as error:
        primary = error
        try:
            _process_tree.stop(owner)
            _process_tree.drain_output(owner)
        except BaseException as cleanup:
            _process_tree._cleanup_note(error, cleanup)
            error._langadapter_command = owner
            error._langadapter_warmed_cleanup_error = cleanup
            diagnostics.append(f"the cleanup of a timing fork failed: {cleanup}")
        if not isinstance(error, Exception):
            raise
        if isinstance(error, subprocess.TimeoutExpired):
            outcome, reason = Outcome.TIMED_OUT, "a timing fork reached the time limit of its stage or of the whole run"
        else:
            outcome, reason = Outcome.INFRASTRUCTURE_FAILURE, str(error)
    finally:
        try:
            disposed = _process_tree.finish(owner, primary=primary)
        except BaseException as cleanup:
            error = primary if primary is not None else cleanup
            if primary is not None:
                _process_tree._cleanup_note(primary, cleanup)
                primary._langadapter_warmed_cleanup_error = cleanup
            error._langadapter_command = owner
            raise error
        if disposed is not True:
            error = primary if primary is not None else RuntimeError("it is not confirmed that a timing fork ended")
            error._langadapter_command = owner
            raise error
    _process_tree.raise_if_cancelled(owner)
    return ForkExecution(transcript, outcome, reason, tuple(diagnostics))


# The assembly of received fork records into the result. Setup and launch
# are elsewhere, and no native evidence is inferred.

def assemble(request, identity, executions, *, outcome: Outcome, reason: str,
             operation_elapsed_s: float, allowed_cpus=None, diagnostics=()):
    """The forks' raw warm-up and measurement records as one result, with
    summary statistics only for a complete observation. The caller supplies
    the operation's measured elapsed time, the captured functional settings
    and each fork's observed allowed CPUs, or None. A cancelled operation
    raises before this."""
    request.validate()
    fields = ("forks", "warmup_runs", "runs", "iteration_timeout_s", "total_timeout_s",
              "startup_timeout_s", "serialize", "state_policy")
    if (any(getattr(identity, field) != getattr(request, field) for field in fields)
            or identity.requested_cpus != request.cpus):
        raise InvalidRequest("identity", "warmed request differs from captured functional settings")
    executions = tuple(executions)
    if len(executions) > request.forks:
        raise InvalidRequest("executions", "exceeds the requested fork count")
    affinities = ((None,) * len(executions) if allowed_cpus is None
                  else tuple(None if cpus is None else tuple(cpus) for cpus in allowed_cpus))
    if len(affinities) != len(executions):
        raise InvalidRequest("allowed_cpus", "requires one actual observation per received fork")
    iterations, forks, notes = [], [], list(diagnostics)
    for index, (execution, cpus) in enumerate(zip(executions, affinities)):
        if not isinstance(execution, ForkExecution) or not isinstance(execution.transcript, Transcript):
            raise InvalidRequest("executions", "requires received native ForkExecution records")
        transcript = execution.transcript
        if (transcript.protocol.fork != index or transcript.protocol.warmup_runs != request.warmup_runs
                or transcript.protocol.runs != request.runs):
            raise InvalidRequest("executions", "fork order/budgets differ from the request")
        records = tuple(transcript.iterations)
        iterations.extend(records)
        warmups = sum(item.report_complete for item in records if item.kind == "warmup")
        measured = sum(item.report_complete for item in records if item.kind == "measurement")
        samples = tuple(item.elapsed_ns for item in records if item.kind == "measurement"
                        and item.report_complete and item.elapsed_ns is not None)
        complete = (transcript.finished and warmups == request.warmup_runs
                    and measured == request.runs and all(item.report_complete for item in records))
        forks.append(WarmedFork(index, warmups, measured, complete, True, cpus, samples))
        notes.extend(execution.diagnostics)
        if outcome is Outcome.COMPLETED and execution.outcome is not Outcome.COMPLETED:
            outcome, reason = execution.outcome, execution.reason
    patterns = [tuple(sorted((test.unique_id, test.kind, test.status) for test in item.tests))
                for item in iterations if item.report_complete]
    changed = bool(patterns and any(pattern != patterns[0] for pattern in patterns))
    all_complete = (len(forks) == request.forks and all(fork.complete for fork in forks)
                    and all(item.elapsed_ns is not None for item in iterations))
    validity = ("non_repeatable" if changed else "complete" if all_complete
                else "incomplete" if executions else "no_samples")
    if (outcome is Outcome.COMPLETED and validity in ("incomplete", "no_samples")):
        outcome = Outcome.INFRASTRUCTURE_FAILURE
        reason = reason or "the timing records do not cover every requested run"
    if changed and not reason:
        reason = "the tests that ran, or their outcomes, changed within a fork or between forks"
    observed_cpus = (affinities[0] if affinities and affinities[0] is not None
                     and all(cpus == affinities[0] for cpus in affinities) else None)
    return summarize(WarmedSuiteBenchmark(
        outcome=outcome, reason=reason, validity=validity,
        requested_forks=request.forks, requested_warmup_runs=request.warmup_runs,
        requested_runs=request.runs, forks_started=len(executions),
        forks_completed=sum(fork.complete for fork in forks),
        warmup_completed=sum(fork.warmup_completed for fork in forks),
        measurements_completed=sum(fork.measurements_completed for fork in forks),
        iterations=tuple(iterations), forks=tuple(forks),
        operation_elapsed_s=operation_elapsed_s, identity=identity,
        requested_cpus=request.cpus, allowed_cpus=observed_cpus,
        cancelled=False, disposal_confirmed=True, diagnostics=tuple(notes)))


#: The setup directory inside the private build copy. Each fork copies it.
SETUP_DIRECTORY = ".langadapter-warmed"


def benchmark_warmed(adapter, request: WarmedSuiteBenchmarkRequest) -> WarmedSuiteBenchmark:
    """The ``WarmedSuiteBenchmark`` of ``request`` for a ``JavaAdapter``."""
    if not isinstance(request, WarmedSuiteBenchmarkRequest):
        raise InvalidRequest("request", "must be a WarmedSuiteBenchmarkRequest")
    request.validate()
    if request.runner_options is not None:
        raise InvalidRequest("runner_options", "Java suite runs take no runner options")
    started = time.monotonic()
    run = _Run(adapter, request, started)
    issue = adapter._context_issue()
    if issue:
        return run.finish(Outcome.UNSUPPORTED, issue)
    setup_issue = adapter.execution_setup_issue()
    if setup_issue:
        return run.finish(Outcome.MISSING_SETUP, setup_issue)
    refusal = adapter._runtime_refusal()
    if refusal:
        return run.finish(Outcome.UNSUPPORTED, refusal)
    with adapter._scratch("warmed") as scope:
        result = run.measure(Path(scope), started + request.total_timeout_s)
        if result.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
            _retain_scratch(scope, f"warmed benchmark failed: {result.reason}")
        return result


class _Run:
    """One operation: the private build copy, then one prepared copy per fork."""

    def __init__(self, adapter, request, started):
        self.adapter, self.request, self.started = adapter, request, started
        self.progress = _Progress(request, started)
        self.identity = _identity(adapter.context, request)

    def finish(self, outcome, reason, executions=(), affinities=None, diagnostics=()):
        result = assemble(self.request, self.identity, executions, outcome=outcome, reason=reason,
                          operation_elapsed_s=time.monotonic() - self.started,
                          allowed_cpus=affinities,
                          diagnostics=(*diagnostics, *self.progress.diagnostics))
        noted = len(self.progress.diagnostics)
        self.progress.emit("complete", None, outcome=result.outcome, reason=result.reason)
        if len(self.progress.diagnostics) > noted:
            # The final event follows the result: add its observer's failure.
            result = replace(result, diagnostics=(*result.diagnostics,
                                                  *self.progress.diagnostics[noted:]))
        return result

    def measure(self, scope: Path, deadline: float) -> WarmedSuiteBenchmark:
        request, progress = self.request, self.progress
        built_root = scope / "build"
        container = isinstance(self.adapter.context.execution, ContainerExecution)
        progress.emit("setup", None)
        try:
            build = self._build_copy(scope, built_root, deadline)
        except subprocess.TimeoutExpired as error:
            reraise_unresolved(error)
            return self.finish(Outcome.TIMED_OUT, "the total timeout ended while copying the project")
        except (OSError, subprocess.SubprocessError) as error:
            if not container:
                raise
            reraise_unresolved(error)
            return self.finish(Outcome.INFRASTRUCTURE_FAILURE,
                               f"the project could not be copied in the container: {error}")
        executor = executor_for(build)
        setup = _warmed_forks.prepare_setup(build, executor, request, built_root / SETUP_DIRECTORY,
                                      total_deadline=deadline,
                                      class_names=self.adapter._class_names)
        if setup.outcome is not Outcome.COMPLETED:
            # Head and tail: a compiler's first error explains the ones after it.
            return self.finish(setup.outcome, setup.reason,
                               diagnostics=(bounded(setup.output),) if setup.output else ())
        prepared = setup.prepared
        self.identity = _identity(build, request, prepared, built_root)
        environment = tool_environment(prepared.jdk, build.environment, executor=executor)
        prefix, outcome, reason = _processor_prefix(request, environment, executor, build.root)
        if prefix is None:
            return self.finish(outcome, reason)
        executions, affinities = [], []
        outcome, reason = Outcome.COMPLETED, ""
        with placement.serialized(request.serialize):
            for fork in range(request.forks):
                channel = scope / f"fork-{fork}"
                channel.mkdir()
                try:
                    # The build stays idle, and each fork owns a prepared copy of it.
                    copy = _warmed_forks.prepare(build, prepared, built_root=built_root,
                                                 total_deadline=deadline)
                    try:
                        with copy.use() as current:
                            affinity = (_container_allowed_cpus(current.executor, prefix,
                                                            current.context.root, environment)
                                        if container else
                                        _allowed_cpus(prefix, current.context.root, environment))
                            progress.fork_started(fork)
                            execution = _warmed_launch.execute(
                                current.context, current.executor, current.prepared.project,
                                current.prepared.jdk, current.prepared.tools, request=request,
                                processor_prefix=prefix,
                                protocol=ForkProtocol(uuid.uuid4().hex, fork, request.warmup_runs,
                                                      request.runs),
                                classes=current.prepared.classes,
                                helper_classes=current.prepared.helper_classes,
                                class_names=current.prepared.class_names, channel=channel,
                                total_deadline=deadline, on_event=progress.native(fork))
                    finally:
                        copy.close()
                except subprocess.TimeoutExpired as error:
                    reraise_unresolved(error)
                    outcome, reason = Outcome.TIMED_OUT, f"the total timeout ended before fork {fork} ran"
                    break
                except (OSError, subprocess.SubprocessError) as error:
                    reraise_unresolved(error)
                    outcome, reason = Outcome.INFRASTRUCTURE_FAILURE, f"fork {fork} could not run: {error}"
                    break
                executions.append(execution)
                affinities.append(affinity)
                progress.fork_ended(fork, execution)
                if (execution.outcome is not Outcome.COMPLETED or not execution.transcript.finished
                        or execution.transcript.repeatable is False):
                    break
        return self.finish(outcome, reason, executions, affinities)

    def _build_copy(self, scope: Path, built_root: Path, deadline: float):
        """The private build copy at ``built_root``, the same byte copy a
        prepared project uses, and the idle context of the build in it."""
        context = self.adapter.context
        if not isinstance(context.execution, ContainerExecution):
            source = Path(context.root)
            _outside_source(str(scope), source)
            _copy_tree(source, built_root, deadline=deadline)
            return replace(context, root=str(built_root), _prepared_copy_binding=None)
        # The project is in the container, so the copy is made there.
        source = PurePosixPath(context.root)
        root = container_path(context.execution, str(built_root.absolute()))
        if source == PurePosixPath(root) or source in PurePosixPath(root).parents:
            raise OwnershipRefused("workspace and receipt parents must be outside the source tree")
        copy_container_tree(self.adapter._executor, source, built_root, deadline=deadline)
        return replace(context, root=root, _prepared_copy_binding=None,
                       execution=replace(context.execution, repository_dir=root))


def _processor_prefix(request, environment, executor, cwd):
    """The processor-set prefix, or None with the outcome that refuses it.
    In a container, the container's taskset sets it."""
    if request.cpus is None:
        return [], Outcome.COMPLETED, ""
    if isinstance(executor, ContainerExecutor):
        selected, outcome, reason = _container_taskset(executor, cwd, environment)
        if selected is None:
            return None, outcome, reason
    else:
        selected = selected_executable("taskset", environment)
        if selected is None:
            return None, Outcome.UNSUPPORTED, ("a processor set needs taskset, which the "
                                               "selected environment does not have")
    return [selected, "-c", ",".join(str(cpu) for cpu in request.cpus)], Outcome.COMPLETED, ""


def _identity(context, request, prepared=None, built_root=None) -> WarmedBenchmarkIdentity:
    """The functional settings and inputs. Before setup the toolchain is unknown."""
    toolchain, build_tool, engine, project_inputs = ToolchainIdentity.of(unknown("java")), None, None, ()
    jvm_args = tuple(context.interpreter_flags)
    if prepared is not None:
        tools = (("junit-platform", prepared.tools.platform),)
        if prepared.tools.junit4:
            tools += (("junit4", prepared.tools.junit4),)
        toolchain = ToolchainIdentity.of(version_of(prepared.jdk), tools)
        build_tool, engine = prepared.project.build_tool, prepared.tools.platform
        jvm_args = (*prepared.project.test_jvm_args, *context.interpreter_flags)
        project_inputs = _project_inputs(context, prepared.project, built_root)
    return WarmedBenchmarkIdentity(
        suite=request.suite.identity(), project_inputs=project_inputs, toolchain=toolchain,
        execution=execution_identity(context),
        build_tool=build_tool, build_version=None, engine_version=engine, jvm_args=jvm_args,
        runner_options=(), mode="warmed_jvm", timing_boundary="junit_launcher_execute",
        state_policy=request.state_policy, forks=request.forks, warmup_runs=request.warmup_runs,
        runs=request.runs, iteration_timeout_s=request.iteration_timeout_s,
        total_timeout_s=request.total_timeout_s, startup_timeout_s=request.startup_timeout_s,
        requested_cpus=request.cpus, serialize=request.serialize)


def _project_inputs(context, project, built_root: Path) -> tuple[FileIdentity, ...]:
    """The main Java sources of the build copy, read from the host copy."""
    path = PurePosixPath if isinstance(context.execution, ContainerExecution) else Path
    root = path(context.root)
    files = []
    for directory in project.main_sources:
        try:
            relative = path(directory).relative_to(root)
        except ValueError:
            continue
        host = built_root / relative
        if host.is_dir():
            files.extend(sorted(host.rglob("*.java")))
    return tuple(FileIdentity.of(file, built_root) for file in files)


class _Progress:
    """Progress records for the observer, whose failure only adds a diagnostic."""

    def __init__(self, request, started):
        self.request, self.started = request, started
        self.forks_started = self.forks_completed = 0
        self.warmup_completed = self.measurements_completed = 0
        self.diagnostics = []

    def emit(self, stage, fork, kind=None, repetition=None, **values):
        if self.request.on_progress is None:
            return
        try:
            self.request.on_progress(WarmedBenchmarkProgress(
                stage, fork, kind, repetition, self.request.forks, self.request.warmup_runs,
                self.request.runs, self.forks_started, self.forks_completed,
                self.warmup_completed, self.measurements_completed,
                operation_elapsed_s=time.monotonic() - self.started, **values))
        except Exception as error:  # an observer never changes the result
            self.diagnostics.append(f"Progress observer failed: {error}")

    def fork_started(self, fork):
        self.forks_started += 1
        self.emit("fork_start", fork)

    def native(self, fork):
        def observe(event):
            if event.event == "start":
                kind, repetition = ("warmup", event.index) if event.index < self.request.warmup_runs \
                    else ("measurement", event.index - self.request.warmup_runs)
                self.emit("iteration_start", fork, kind, repetition)
            elif event.event == "iteration" and event.iteration is not None:
                iteration = event.iteration
                if iteration.report_complete:
                    if iteration.kind == "warmup":
                        self.warmup_completed += 1
                    else:
                        self.measurements_completed += 1
                self.emit("iteration_end", fork, iteration.kind, iteration.repetition,
                          elapsed_ns=iteration.elapsed_ns, outcome=iteration.outcome)
        return observe

    def fork_ended(self, fork, execution):
        records = execution.transcript.iterations
        warmups = sum(item.report_complete for item in records if item.kind == "warmup")
        measured = sum(item.report_complete for item in records if item.kind == "measurement")
        if (execution.transcript.finished and warmups == self.request.warmup_runs
                and measured == self.request.runs):
            self.forks_completed += 1
        self.emit("fork_end", fork, outcome=execution.outcome, reason=execution.reason)
