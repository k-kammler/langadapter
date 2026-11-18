"""Mutation runs of a C target with Mull (``_mull``).

Mull's IR frontend, a plugin of the clang Mull was built for, compiles every
mutant of the target into one object, each behind a switch: the environment
variable its identifier names. The adapter compiles the target once that
way, links it as the project links it (each test file's program, or the
Python extension), lists the mutants with ``mull-runner``'s dry run (its
Mutation Testing Elements report), and runs the tests itself with one
mutant switched on at a time, so that it knows which test killed which
mutant: ``mull-runner`` judges a mutant by one run of the whole program.

A run first checks the suite on the unmutated target twice. With gcov's
counters (the coverage build) every test must pass, and the counters tell
which functions ran. With C tests each test writes its own counters, so they
also tell which tests executed which function. On Mull's build with no
mutant switched on every test must pass again, and its time is the passing
suite's time of the per-mutant timeout. Then each mutant, with the
session's workers in parallel:

1. inside a function no test executed is classified without running: it
   survives, flagged ``not_executed`` and counted, as PIT's NO_COVERAGE is.
   This is the run's one coverage filter. Mull's own would need clang's
   coverage build.
2. runs its tests with its switch on, within the per-mutant timeout. C
   tests: the tests that executed its function, each in a process of its
   own. Python tests: the whole suite in one pytest process with Mull's
   extension, as Cosmic Ray runs Python mutants. The failing tests are its
   killers (the first one with attribution ``first``, every one with
   ``all``).

Mull's build is not the project's: clang in place of the project's GCC,
without the recorded flags ``TargetBuild.mull_left_out`` names (link-time
optimization, the prefix maps, and any flag clang refuses), and with what
clang reported it ignored. The run names each difference (the setting
``mull_build``). A suite that does not pass on Mull's build is refused with
them. Mutants under which the test runner itself fails (``runner_error``:
pytest uses some modules it may test, as it uses CPython's bisect) are
detected and counted apart.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import langadapter as la
from langadapter import _process_tree
from langadapter._execution_scope import unresolved_command
from langadapter._linux_job import ControlError
from langadapter._words import listed
from langadapter.errors import InvalidRequest
from langadapter.execution import execution_identity, run_parallel, work_dir_tempdir
from langadapter.mutation import (
    BY_FAILING_TEST, BY_TIMEOUT, FAILED_INITIALIZATION, FAILED_INSTRUMENT_CHECK, FROM_DEFAULT,
    FROM_REQUEST, KILLED_STATUS, NON_VIABLE, NON_VIABLE_STATUS, SURVIVED, SURVIVED_STATUS,
    TIMEOUT_STATUS, UNTESTED, UNTESTED_MUTANTS_IN_CATALOGUE, UNTESTED_STATUS,
    WITHOUT_NAMED_TEST, Classification,
    EffectiveSetting, Mutant, MutationClass, MutationIdentity, MutationRequest, MutationRun,
    MutationSession, TimeoutRule, _DEFAULT_ATTRIBUTION, _DEFAULT_TIMEOUT_RULE,
    _DEFAULT_WALL_CLOCK_LIMIT_S, _chosen, _common_settings, mutant_key, refresh_summary,
)
from langadapter.runs import SuiteRunRequest, with_detail

from langadapter.c import _build, _extension, _gcov, _mull

NOT_EXECUTED = "no test executed the mutant's function, so the mutant cannot change an outcome"
#: The operator sets a catalogue may name.
NAMED_SETS = {"default": _mull.DEFAULT, "all": _mull.ALL}
#: The pytest plugins a mutant's Python run leaves out, as the Python
#: adapter's own runs do.
DISABLED_PLUGINS = ("logfire", "anyio")
#: pytest's exit status for an error of its own.
PYTEST_INTERNAL_ERROR = 3
#: The rounds in which Mull's build leaves out the flags clang refuses.
REFUSAL_ROUNDS = 8
#: What Mull's runner says when it finds no mutant, in place of a report.
NO_MUTANTS = "No mutants found."
#: What clang says of a flag it refuses, and of one it ignores.
_REFUSED = re.compile(r"error: (?:unknown argument:?|unsupported option) '([^']+)'")
_IGNORED = re.compile(r"warning: (?:optimization flag|argument unused during compilation:|"
                      r"unknown warning option) '([^']+)'")


class CMutationSession(MutationSession):
    """A session of Mull for one adapter: its runs measure the mutants with
    ``workers`` of them at once."""

    def __init__(self, adapter, workers: int = 1) -> None:
        super().__init__(workers_requested=workers, workers_started=workers)
        self.adapter = adapter

    def _validate_request(self, request: MutationRequest) -> None:
        request.validate()
        operators(request)
        if request.timeout_journal and not self.adapter._extension_mode:
            raise InvalidRequest("timeout_journal", "the C engine keeps no journal for C "
                                 "tests: each test of a mutant runs in its own process and "
                                 "is named when it ends the run")
        if request.passing_suite_s is not None:
            raise InvalidRequest("passing_suite_s", "the C engine times the suite in its own "
                                 "check, each test as it runs under a mutant, and applies the "
                                 "per-mutant timeout rule to that time")

    def measure(self, request: MutationRequest) -> MutationRun:
        return measure_mutants(self.adapter, request, request.workers or self.workers_started)

    def release(self) -> None:
        pass


def operators(request: MutationRequest) -> Tuple[str, ...]:
    """The operators the request's catalogue selects: Mull's default group
    for the full catalogue, a named set (``default``, ``all``), or a list of
    Mull's operators."""
    catalogue = request.catalogue
    if catalogue.kind == "full":
        return _mull.DEFAULT
    if catalogue.kind == "named":
        if catalogue.name not in NAMED_SETS:
            raise InvalidRequest("catalogue.name", f"the C engine's sets are "
                                 f"{', '.join(sorted(NAMED_SETS))}")
        return NAMED_SETS[catalogue.name]
    unknown = [name for name in catalogue.operators if name not in _mull.ALL]
    if unknown:
        raise InvalidRequest("catalogue.operators", f"unknown C operators: {', '.join(unknown)}")
    return tuple(catalogue.operators)


def settings(request: MutationRequest, workers: int, timeout_s: int,
             passing_suite_s: Optional[float], build: Dict[str, Any], extension: bool
             ) -> Dict[str, EffectiveSetting]:
    """The run's effective settings, each with where it came from. The
    timeout journal is on by default for Python tests of an extension
    (``extension``) and off for C tests, which keep none."""
    timeout = request.per_mutant_timeout
    if isinstance(timeout, TimeoutRule):
        per_mutant = EffectiveSetting(timeout_s, FROM_REQUEST, timeout.inputs(passing_suite_s))
    elif timeout is not None:
        per_mutant = EffectiveSetting(int(timeout), FROM_REQUEST, {"rule": "fixed"})
    else:
        per_mutant = EffectiveSetting(timeout_s, FROM_DEFAULT,
                                      _DEFAULT_TIMEOUT_RULE.inputs(passing_suite_s))
    return _common_settings(request, operators(request), per_mutant,
                            _chosen(request.workers, workers), extension,
                            after={"mull_build": EffectiveSetting(build, FROM_DEFAULT)})


@dataclass(frozen=True)
class _MullMutant:
    """One of Mull's mutants: the variable that switches it on, its operator,
    its replacement text and its place (lines and byte columns from 1, the
    end column after the replaced text), and its occurrence, the count of
    earlier mutants of its operator in the file."""

    switch: str
    operator: str
    replacement: str
    line: int
    column: int
    end_line: int
    end_column: int
    occurrence: int

    @property
    def engine_id(self) -> str:
        """The mutant's id, the same in every run over the same source."""
        return f"{self.operator}:{self.line}:{self.column}:{self.end_line}:{self.end_column}"

    def diff(self, data: bytes, path: str) -> str:
        """The changed lines as a unified diff without context."""
        lines = data.split(b"\n")
        before = lines[self.line - 1:self.end_line]
        changed = (before[0][:self.column - 1] + self.replacement.encode("utf-8")
                   + before[-1][self.end_column - 1:])
        old = [line.decode("utf-8", "replace") for line in before]
        new = changed.decode("utf-8", "replace").split("\n")
        first = f"{self.line}" + (f",{len(old)}" if len(old) != 1 else "")
        second = f"{self.line}" + (f",{len(new)}" if len(new) != 1 else "")
        text = [f"--- a/{path}", f"+++ b/{path}", f"@@ -{first} +{second} @@"]
        text += [f"-{line}" for line in old] + [f"+{line}" for line in new]
        return "\n".join(text) + "\n"


#: The reason of the incomplete stamp of a run the wall clock cut.
_WALL_CLOCK_LIMIT = "wall_clock_limit"


@dataclass(frozen=True)
class _Stopped:
    """An origin check that ends the run before its first mutant: the reason
    a consumer may show, the tests its failure is pinned on, the run's
    outcome and failure, and the incomplete stamp of a check the wall clock
    cut. C tests that did not pass make a red suite (``_red``), Java's
    shape. Counts gcovr refuses, or none written, keep the instrument
    check's failure (``_check_end``), and so do Python tests of an
    extension that did not pass (``_stopped_run``). A check whose every
    stop started after its time was spent ends as a run whose time was
    spent (``_timed_out``)."""

    reason: str
    stopping: Tuple[la.TestId, ...]
    outcome: la.Outcome = la.Outcome.TEST_FAILURES
    failure: Optional[str] = FAILED_INITIALIZATION
    incomplete: Optional[Dict[str, Any]] = None

    def run(self, measurement: "_Measurement") -> MutationRun:
        """The mutation run this end gives. A check the wall clock cut is a
        measured run with no mutant and its stamp, as Java's run whose report
        PIT did not write, and any other end is a run that measured
        nothing."""
        if self.incomplete is None:
            return MutationRun.failed(measurement.target, self.outcome, self.reason,
                                      failure=self.failure, stopping_tests=self.stopping)
        return MutationRun(target_file=measurement.target, mutants=(),
                           summary=dict(refresh_summary({"mutants": []})["summary"]),
                           requested_catalogue=measurement.request.catalogue.kind,
                           requested_operators=measurement.selected,
                           incomplete=self.incomplete, outcome=self.outcome, reason=self.reason)


def _red(adapter, stops: Sequence[_gcov.Stop],
         build: Optional[Dict[str, Any]] = None) -> _Stopped:
    """The origin check's tests that did not pass, ``stops``, as a red suite,
    every one of them named."""
    detail = "; ".join(f"{stop.file}::{stop.name}{stop.what}" for stop in stops)
    return _Stopped(_refused(detail, build),
                    tuple(adapter._test_id(stop.file, stop.name) for stop in stops))


def _check_end(measurement: "_Measurement", stops: Sequence[_gcov.Stop],
               listed: Mapping[str, Sequence[str]],
               build: Optional[Dict[str, Any]] = None) -> Optional[_Stopped]:
    """How a check of C tests ends, or None when every test passed with its
    counts. A test that started after the check's time was spent is left
    out first: it is not named and does not count toward every test of a
    program, and a check whose every stop started then ends as a run whose
    time was spent (``_timed_out``). Of the rest, counts gcovr refuses, or
    none written, end it as an instrument check with the first such test's
    text as its reason, naming the tests ``_gcov.named`` gives. Else the
    tests that did not pass end it as a red suite (``_red``)."""
    timely = [stop for stop in stops if not stop.late]
    if not timely:
        return _timed_out(measurement) if stops else None
    counted = [stop for stop in timely if stop.counts]
    if not counted:
        return _red(measurement.adapter, timely, build)
    named = tuple(measurement.adapter._test_id(file, name)
                  for file, name in _gcov.named(timely, listed))
    return _Stopped(counted[0].what, named, la.Outcome.INFRASTRUCTURE_FAILURE,
                    FAILED_INSTRUMENT_CHECK)


def _timed_out(measurement: "_Measurement") -> _Stopped:
    """A check whose time was spent before any failure it can name: the text
    a C run gives then, and the stamp of a run the wall clock cut, its
    untested and total mutants unknown."""
    wall = _wall(measurement.request)
    return _Stopped(f"the run reached its timeout of {wall:g} s", (), la.Outcome.TIMED_OUT, None,
                    {"reason": _WALL_CLOCK_LIMIT, "limit_s": wall, "untested": None,
                     "total": None})


def _wall(request: MutationRequest) -> float:
    """The wall clock limit of a mutation run, in seconds."""
    return request.wall_clock_limit_s or _DEFAULT_WALL_CLOCK_LIMIT_S


def _stopped_run(run, detail: str, build: Optional[Dict[str, Any]] = None) -> _Stopped:
    """A check of Python tests whose run did not pass: the instrument check's
    failure with the run's ``detail``, and its failing tests named."""
    return _Stopped(_refused(detail, build), run.failing_tests(),
                    la.Outcome.INFRASTRUCTURE_FAILURE, FAILED_INSTRUMENT_CHECK)


@dataclass
class _Origin:
    """The unmutated suite's checks. From the coverage build: the functions
    with their summed counts and, for C tests, per test file the tests it
    lists and per test the functions it executed. From Mull's build: its
    mutants, the suite's time with none switched on, what differs from the
    project's build, and the test programs (C tests) or the extension
    (Python tests)."""

    functions: Tuple[_gcov.Function, ...]
    suite_s: float
    mutants: Tuple[_MullMutant, ...]
    build: Dict[str, Any]
    run_s: float = 0.0
    programs: Dict[str, Path] = field(default_factory=dict)
    extension: Optional[Path] = None
    tests: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    executed: Dict[Tuple[str, str], frozenset] = field(default_factory=dict)


@dataclass
class _Verdict:
    """One mutant's result: its native status, classification, killing
    tests, how many tests ran, and the row's further fields."""

    native: str
    classification: Classification
    killers: Tuple[la.TestId, ...] = ()
    tests_run: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _Measurement:
    """One mutation run of a target, which its origin runs and its mutants'
    verdicts share: the adapter, the request, the target's build, Mull, the
    operators, the target's bytes and path, the wall clock's end and the
    attribution. The run sets the origin, the per-mutant timeout and whether
    the runs of Python tests journal (``journal``, the effective
    ``timeout_journal``) once the origin runs are done. The mutants' runs add
    each run tried once more after the process layer failed, by its mutant
    and test, and the ids of the mutants left untested (``_supervised``)."""

    adapter: Any
    request: MutationRequest
    build: Any
    mull: Any
    selected: Tuple[str, ...]
    data: bytes
    target: str
    deadline: float
    origin: Optional[_Origin] = None
    timeout_s: int = 0
    attribution: str = ""
    journal: bool = False
    retried: List[Dict[str, str]] = field(default_factory=list)
    unsupervised: List[str] = field(default_factory=list)
    unfinished: List[str] = field(default_factory=list)


def measure_mutants(adapter, request: MutationRequest, workers: int) -> MutationRun:
    """The mutation run of ``request`` with ``workers`` mutants at once."""
    started = time.monotonic()
    target = adapter._relative_target(request.target)
    selected = operators(request)
    tools = _tools(adapter, target)
    if isinstance(tools, MutationRun):
        return tools
    mull, build = tools
    root = Path(adapter.context.root)
    data = (root / target).read_bytes()
    wall = _wall(request)
    measurement = _Measurement(adapter, request, build, mull, selected, data, target,
                               started + wall,
                               attribution=request.attribution or _DEFAULT_ATTRIBUTION)
    with work_dir_tempdir(adapter.context, adapter._executor(), prefix="langadapter_c_m_") as scratch:
        scratch = Path(scratch)
        origin = (_extension_origin if adapter._extension_mode else _origin)(measurement, scratch)
        if isinstance(origin, _Stopped):
            return origin.run(measurement)
        if isinstance(origin, str):
            return MutationRun.failed(target, la.Outcome.INFRASTRUCTURE_FAILURE, origin,
                                      failure=FAILED_INSTRUMENT_CHECK)
        passing_s = origin.suite_s
        measurement.origin, measurement.timeout_s = origin, _timeout_s(request, passing_s)
        effective = settings(request, workers, measurement.timeout_s, passing_s, origin.build,
                             adapter._extension_mode)
        measurement.journal = effective["timeout_journal"].value
        identity = MutationIdentity(
            observation=la.ObservationIdentity(
                target=la.FileIdentity.of(root / target, root), suite=request.suite.identity(),
                toolchain=adapter._mutation_toolchain_identity(mull), instrument=None,
                execution=execution_identity(adapter.context)),
            catalogue=request.catalogue.kind, operators=selected,
            per_mutant_timeout_s=measurement.timeout_s, attribution=measurement.attribution,
            workers=workers)
        verdicts = _measure_all(origin.mutants, scratch, workers, _verdict_of(measurement),
                                measurement.deadline)
    return _mutation_run(measurement, verdicts, identity, effective, wall)


def _tools(adapter, target: str):
    """Mull and the target's build, or the run that failed for want of
    them."""
    gate = adapter._gate()
    if gate is not None:
        return MutationRun.failed(target, gate[0], gate[1], failure=FAILED_INITIALIZATION)
    mull = adapter._mull_tool()
    if isinstance(mull, _mull.MullProblem):
        return MutationRun.failed(target, la.Outcome.MISSING_SETUP, mull.reason,
                                  failure=FAILED_INITIALIZATION)
    build = adapter._target_build(target)
    if isinstance(build, _build.BuildProblem):
        return MutationRun.failed(target, build.outcome, with_detail(build.reason, build.detail),
                                  failure=FAILED_INITIALIZATION)
    return mull, build


def _timeout_s(request: MutationRequest, passing_s: float) -> int:
    """The per-mutant timeout: the request's rule or seconds, else the
    default rule, of the check run's time ``passing_s``."""
    timeout = request.per_mutant_timeout
    return (timeout.seconds(passing_s) if isinstance(timeout, TimeoutRule)
            else int(timeout) if timeout is not None
            else _DEFAULT_TIMEOUT_RULE.seconds(passing_s))


def _verdict_of(measurement: _Measurement) -> Callable:
    """A mutant's verdict in a worker's directory: not executed when its
    function never ran, else its run with the Python tests (each worker's
    directory keeps one harness) or the C tests."""
    harnesses: Dict[Path, Any] = {}
    lock = threading.Lock()

    def harness(worker_dir: Path):
        with lock:
            if worker_dir not in harnesses:
                harnesses[worker_dir] = _extension.Harness(measurement.adapter, worker_dir / "python")
            return harnesses[worker_dir]

    def verdict(mutant: _MullMutant, worker_dir: Path):
        function = _function_of(measurement.origin.functions, mutant.line)
        if function is not None and function.executions == 0:
            return _Verdict("not_executed", Classification(SURVIVED_STATUS,
                                                           MutationClass.UNDETECTED,
                                                           SURVIVED),
                            extra={"not_executed": True, "function": function.name,
                                   "reason": NOT_EXECUTED})
        if measurement.adapter._extension_mode:
            return _extension_verdict(measurement, mutant, harness(worker_dir))
        return _supervised(measurement, mutant,
                           lambda: _native_verdict(measurement, mutant, function, worker_dir))

    return verdict


def _supervised(measurement: _Measurement, mutant: _MullMutant,
                run: Callable[[], Optional[_Verdict]]) -> Optional[_Verdict]:
    """``run``, a mutant's run with C tests, tried once more when the process
    layer failed in one of its test processes (``ControlError``) and the
    command it left was settled (``_settled``): a storage stall can outlast
    the supervisor's bounds. A second such failure leaves the mutant
    untested. Each retry and each mutant left untested is written to the
    process's stderr as a ``RuntimeWarning`` with the mutant and the test,
    and counted in ``measurement``. A command not settled by the run's
    deadline raises the failure, since a process of it may still run."""
    for attempt in (1, 2):
        try:
            return run()
        except ControlError as error:
            if not _settled(error, measurement.deadline):
                raise
            test = getattr(error, "_langadapter_mutant_test", "a test")
            if attempt == 1:
                measurement.retried.append({"mutant": mutant.engine_id, "test": test})
                warnings.warn(f"mutant {mutant.engine_id}: its run is tried once more, since the "
                              f"process layer failed in the run of {test}: {error}",
                              RuntimeWarning, stacklevel=2)
                continue
            measurement.unsupervised.append(mutant.engine_id)
            reason = f"the process layer failed twice in the run of {test}: {error}"
            warnings.warn(f"mutant {mutant.engine_id} is untested: {reason}", RuntimeWarning,
                          stacklevel=2)
            return _Verdict("untested", Classification(UNTESTED_STATUS, MutationClass.NOT_RUN,
                                                       UNTESTED), extra={"reason": reason})


def _settled(error: ControlError, deadline: float) -> bool:
    """Whether the command ``error`` left undisposed is disposed of before
    ``deadline``: its output drained again and its containment finished
    (``_process_tree.drain_output``, ``finish``), tried each second until a
    stall that kept them from finishing has passed, unless the operation is
    cancelled. True when nothing was left."""
    owner = unresolved_command(error)
    if owner is None:
        return True
    while True:
        with contextlib.suppress(Exception):
            _process_tree.drain_output(owner)
            if _process_tree.finish(owner):
                return True
        if time.monotonic() >= deadline or _process_tree.cancelled():
            return False
        time.sleep(1.0)


def _mutation_run(measurement: _Measurement, verdicts: Dict[int, _Verdict], identity,
                  effective, wall) -> MutationRun:
    """The run's result from its mutants' verdicts: the rows, the summary and
    the outcome, timed out with the mutants the wall clock left."""
    origin, target = measurement.origin, measurement.target
    mutants = tuple(_mutant(m, verdicts.get(index), measurement.data, target,
                            measurement.adapter.test_id_text)
                    for index, m in enumerate(origin.mutants))
    summary = dict(refresh_summary({"mutants": [dict(m.row) for m in mutants]})["summary"])
    summary["not_executed"] = sum(1 for m in mutants if m.row.get("not_executed"))
    # Mutants that broke the test runner itself, apart.
    summary["runner_error"] = sum(1 for m in mutants if m.row["native_status"] == "runner_error")
    # Runs tried once more after the process layer failed (``_supervised``),
    # each by its mutant and test, as the warnings name them.
    summary["retried"] = sorted(measurement.retried, key=lambda r: (r["mutant"], r["test"]))
    summary["retried_runs"] = len(measurement.retried)
    left = sum(1 for index in range(len(origin.mutants)) if index not in verdicts)
    untested = len(measurement.unsupervised) + len(measurement.unfinished)
    incomplete = None
    outcome, reason = la.Outcome.COMPLETED, ""
    if left:
        outcome = la.Outcome.TIMED_OUT
        reason = f"the wall clock limit of {wall} s ended the run with {left} mutants left"
        incomplete = {"reason": _WALL_CLOCK_LIMIT, "limit_s": wall,
                      "untested": left + untested}
    elif untested:
        causes = []
        if measurement.unsupervised:
            causes.append("the process layer failed twice in their runs")
        if measurement.unfinished:
            causes.append("the per-mutant timeout ended between two tests")
        reason = f"{untested} mutants are untested: {listed(causes)}"
        incomplete = {"reason": UNTESTED_MUTANTS_IN_CATALOGUE, "untested": untested,
                      "total": len(mutants)}
    elif not origin.mutants:
        reason = "the catalogue has no mutant for this target"
    return MutationRun(target_file=target, mutants=mutants, summary=summary,
                       requested_catalogue=measurement.request.catalogue.kind,
                       requested_operators=measurement.selected, incomplete=incomplete,
                       outcome=outcome, reason=reason, identity=identity, settings=effective,
                       origin_run_s=origin.run_s)


def _refused(detail: str, build: Optional[Dict[str, Any]] = None) -> str:
    text = (f"the unmutated suite did not pass in the check run before the first mutant "
            f"({detail}), so every mutant would be killed by its failure")
    if build is not None:
        text += f"; Mull's build differs from the project's: {_differences(build)}"
    return text


def _differences(build: Dict[str, Any]) -> str:
    """What Mull's build does differently, in words."""
    parts = [f"{build['compiler']} in place of {build['project_compiler']}"]
    if build["left_out"]:
        parts.append(f"without {' '.join(build['left_out'])}")
    if build["ignored"]:
        parts.append(f"clang ignored {' '.join(build['ignored'])}")
    return ", ".join(parts)


def _mull_object(adapter, build, mull, selected, data: bytes, mull_dir: Path, deadline: float):
    """The target compiled by Mull's clang with every mutant of ``selected``,
    and what the build does differently, or why it did not compile. The
    flags clang refuses are left out, one round of them at a time."""
    refused: List[str] = []
    mull_dir.mkdir(parents=True, exist_ok=True)
    _mull.config(mull_dir / "mull.yml", selected, build.staged_source(build.stage_dir(mull_dir)))
    for _ in range(REFUSAL_ROUNDS):
        compiled = adapter._compile_target(build, data, mull_dir, _build.MULL, deadline,
                                           mull=(mull, tuple(refused)))
        if isinstance(compiled, Path):
            log = (mull_dir / "compile.log").read_text(encoding="utf-8")
            toolchain = adapter._toolchain()
            return compiled, {
                "compiler": mull.clang_line, "project_compiler": toolchain.version_line,
                "left_out": list(build.mull_left_out(refused)),
                "ignored": sorted(set(_IGNORED.findall(log))),
                "mull": mull.version, "mull_llvm": mull.llvm}
        named = [flag for flag in _REFUSED.findall(compiled.output)
                 if flag in build.compile.arguments and flag not in refused]
        if not named:
            return compiled.text()
        refused += named
    return f"Mull's clang refused more than {REFUSAL_ROUNDS} rounds of the build's flags"


def _listed(adapter, mull, holder: Path, config: Path, staged: Path, mull_dir: Path,
            deadline: float):
    """The mutants Mull compiled into ``holder`` (a linked program or library)
    in the file ``staged``, in source order, from ``mull-runner``'s dry run,
    or why they could not be listed."""
    report = mull_dir / "report"
    argv = [mull.runner, "--dry-run", "--reporters", "Elements", "--report-dir", str(report),
            "--report-name", "mutants", "--test-program", "true", str(holder)]
    done = adapter._compile(argv, str(mull_dir), deadline, {"MULL_CONFIG": str(config)})
    if done is None:
        return "the run reached its timeout while Mull listed the mutants"
    path = report / "mutants.json"
    text = ((done.stdout or "") + (done.stderr or "")).strip()
    if done.returncode == 0 and not path.is_file() and NO_MUTANTS in text:
        # Mull writes no report when it finds no mutant.
        return ()
    if done.returncode != 0 or not path.is_file():
        return f"Mull's runner did not list the mutants (exit {done.returncode}): {text[-500:]}"
    files = json.loads(path.read_text(encoding="utf-8")).get("files") or {}
    wanted = os.path.realpath(staged)
    found = sorted(
        (entry["location"]["start"]["line"], entry["location"]["start"]["column"],
         entry["mutatorName"], entry)
        for name, listed in files.items() if os.path.realpath(name) == wanted
        for entry in listed.get("mutants") or ())
    counts: Dict[str, int] = {}
    mutants = []
    for line, column, operator, entry in found:
        occurrence = counts.get(operator, 0)
        counts[operator] = occurrence + 1
        end = entry["location"]["end"]
        mutants.append(_MullMutant(entry["id"], operator, entry["replacement"], line, column,
                                   end["line"], end["column"], occurrence))
    return tuple(mutants)


def _origin(measurement: _Measurement, scratch: Path):
    """The origin runs of C tests, or why the suite cannot be measured."""
    began = time.monotonic()
    adapter, build, deadline = measurement.adapter, measurement.build, measurement.deadline
    origin_dir = scratch / "origin"
    origin_dir.mkdir()
    compiled = adapter._compile_target(build, measurement.data, origin_dir / "target",
                                       _build.COVERAGE, deadline)
    if not isinstance(compiled, Path):
        return compiled.text()
    tests = adapter._compile_tests(build, measurement.request.suite, origin_dir / "tests", deadline)
    if not isinstance(tests, dict):
        return tests.text()
    covered = _covered_runs(measurement, origin_dir, tests, compiled)
    if isinstance(covered, (str, _Stopped)):
        return covered
    names, executed, total = covered
    made = _mull_object(adapter, build, measurement.mull, measurement.selected,
                        measurement.data, scratch / "mull", deadline)
    if isinstance(made, str):
        return made
    target_object, differences = made
    programs = _mull_programs(measurement, scratch, tests, target_object)
    if isinstance(programs, str):
        return programs
    holder = adapter._linked_program(next(iter(programs.values())))
    if holder is None:
        return "no test of the suite runs a program that links the target"
    mutants = _listed(adapter, measurement.mull, holder, scratch / "mull" / "mull.yml",
                      build.staged_source(build.stage_dir(scratch / "mull")), scratch / "mull",
                      deadline)
    if isinstance(mutants, str):
        return mutants
    suite_s = _mull_suite_s(measurement, origin_dir, programs, names, differences)
    if isinstance(suite_s, _Stopped):
        return suite_s
    return _Origin(total.functions if total is not None else (), suite_s, mutants,
                   differences, run_s=time.monotonic() - began, programs=programs,
                   tests=names, executed=executed)


def _covered_runs(measurement: _Measurement, origin_dir: Path, tests, compiled: Path):
    """Each C test of the unmutated suite alone on the coverage build: the
    tests each file lists, the functions each test executed and the summed
    counts, else how the check ends (``_check_end``) or another failure's
    text. Every test runs, so that each that does not pass, and each whose
    own counts gcovr refuses or whose process wrote none, is named. The
    check goes on after a failing test within the run's wall clock, so once
    a hanging test used it up, each later test has one second: such a test
    did not stop the run and is not named."""
    adapter, build, deadline = measurement.adapter, measurement.build, measurement.deadline
    stops: List[_gcov.Stop] = []
    names: Dict[str, Tuple[str, ...]] = {}
    executed: Dict[Tuple[str, str], frozenset] = {}
    total: Optional[_gcov.Counts] = None
    for index, (file, test_object) in enumerate(sorted(tests.items())):
        program = origin_dir / f"program-{index}"
        linked = adapter._link(build, (test_object, compiled), program, True, deadline,
                               measurement.request.suite.root)
        if linked is not None:
            return linked.text()
        listed, failed = adapter._listed(program, adapter._environment(), str(origin_dir),
                                         max(1.0, deadline - time.monotonic()))
        if failed is not None:
            return _refused(f"{file}: {failed.message}")
        names[file] = listed
        for number, name in enumerate(listed):
            prefix = origin_dir / "counters" / f"{index}-{number}"
            env = adapter._environment(adapter._counter_variables(prefix, compiled))
            late = time.monotonic() >= deadline
            one = adapter._runner.run_one(program, name, adapter._run_test, cwd=str(origin_dir), env=env,
                                          timeout_s=max(1.0, deadline - time.monotonic()))
            if one.outcome not in ("passed", "skipped"):
                stops.append(_gcov.Stop(file, name, f": {one.message or one.outcome}", late=late))
                continue
            counts = adapter._own_counts(build, compiled, prefix, origin_dir / "read" / prefix.name,
                                         adapter._test_id(file, name))
            if isinstance(counts, str):
                stops.append(_gcov.Stop(file, name, counts, counts=True, late=late))
                continue
            total = counts if total is None else total.merged(counts)
            executed[(file, name)] = frozenset(
                (f.name, f.start_line) for f in counts.functions if f.executions > 0)
    return _check_end(measurement, stops, names) or (names, executed, total)


def _mull_programs(measurement: _Measurement, scratch: Path, tests, target_object: Path):
    """Each test file's program linked with Mull's object of the target, or
    why one did not link."""
    programs: Dict[str, Path] = {}
    for index, (file, test_object) in enumerate(sorted(tests.items())):
        program = scratch / "mull" / f"program-{index}"
        linked = measurement.adapter._link(measurement.build, (test_object, target_object),
                                           program, False, measurement.deadline,
                                           measurement.request.suite.root)
        if linked is not None:
            return linked.text()
        programs[file] = program
    return programs


def _mull_suite_s(measurement: _Measurement, origin_dir: Path, programs: Dict[str, Path],
                  names: Dict[str, Tuple[str, ...]], differences: Dict[str, Any]):
    """The unmutated suite's time on Mull's build, every mutant switched off,
    else how the check ends there (``_check_end``)."""
    adapter, deadline = measurement.adapter, measurement.deadline
    stops: List[_gcov.Stop] = []
    suite_s = 0.0
    for file, program in programs.items():
        for name in names[file]:
            one_started = time.monotonic()
            one = adapter._runner.run_one(program, name, adapter._run_test, cwd=str(origin_dir),
                                          env=adapter._environment(),
                                          timeout_s=max(1.0, deadline - one_started))
            suite_s += time.monotonic() - one_started
            if one.outcome not in ("passed", "skipped"):
                stops.append(_gcov.Stop(file, name, f" on Mull's build: {one.message or one.outcome}",
                                        late=one_started >= deadline))
    return _check_end(measurement, stops, names, differences) or suite_s


def _extension_origin(measurement: _Measurement, scratch: Path):
    """The origin runs of Python tests: the suite once with the extension's
    counters, and once on Mull's extension, each in one pytest process. A
    run that does not pass ends the check as an instrument check that names
    its failing tests (``_stopped_run``)."""
    began = time.monotonic()
    adapter, build, deadline = measurement.adapter, measurement.build, measurement.deadline
    request, mull, data = measurement.request, measurement.mull, measurement.data
    origin_dir = scratch / "origin"
    staged = adapter._stage_extension(measurement.target, data, origin_dir, coverage=True,
                                     deadline=deadline)
    if not hasattr(staged, "extension"):
        return staged.text()
    harness = _extension.Harness(adapter, origin_dir / "python")
    counters = origin_dir / "counters"
    inner = SuiteRunRequest(suite=request.suite, timeout_s=max(1.0, deadline - time.monotonic()),
                            runner_options=_pytest_options(first=False))
    run, report = harness.run(
        inner, module=build.module, extension=staged.extension, counters=counters,
        strip=adapter._counter_variables(origin_dir, staged.target_object)["GCOV_PREFIX_STRIP"])
    if not report.loaded(staged.extension):
        return report.problem(build.module, staged.extension)
    if run.outcome is not la.Outcome.COMPLETED:
        return _stopped_run(run, run.reason_text())
    try:
        counts = adapter._merged_counts(staged.build, staged.target_object, [counters],
                                        origin_dir / "read")
    except _gcov.CountsInconsistent as inconsistent:
        return str(inconsistent)
    made = _mull_object(adapter, build, mull, measurement.selected, data, scratch / "mull",
                        deadline)
    if isinstance(made, str):
        return made
    target_object, differences = made
    extension = adapter._link_extension(build, target_object, scratch / "mull", coverage=False,
                                       deadline=deadline)
    if not isinstance(extension, Path):
        return extension.text()
    mutants = _listed(adapter, mull, extension, scratch / "mull" / "mull.yml",
                      build.staged_source(build.stage_dir(scratch / "mull")), scratch / "mull",
                      deadline)
    if isinstance(mutants, str):
        return mutants
    inner = SuiteRunRequest(suite=request.suite, timeout_s=max(1.0, deadline - time.monotonic()),
                            runner_options=_pytest_options(first=False))
    run, report = harness.run(inner, module=build.module, extension=extension)
    if not report.loaded(extension):
        return report.problem(build.module, extension)
    if run.outcome is not la.Outcome.COMPLETED:
        return _stopped_run(run, f"on Mull's build: {run.reason_text()}", differences)
    return _Origin(counts.functions, run.wall_time_s or 0.0, mutants, differences,
                   run_s=time.monotonic() - began, extension=extension)


def _pytest_options(*, first: bool):
    from langadapter.python.project import PytestOptions

    return PytestOptions(stop_at_first_failure=first, disabled_plugins=DISABLED_PLUGINS)


def _measure_all(mutants: Sequence[_MullMutant], scratch: Path, workers: int,
                 verdict: Callable, deadline: float) -> Dict[int, _Verdict]:
    """Each mutant's verdict by its index. Mutants left when the wall clock
    runs out have none. Each worker keeps one directory."""
    verdicts: Dict[int, _Verdict] = {}
    lock = threading.Lock()
    slots = list(range(workers))

    def one(index: int) -> None:
        if time.monotonic() >= deadline:
            return
        with lock:
            slot = slots.pop()
        try:
            worker_dir = scratch / f"worker-{slot}"
            worker_dir.mkdir(exist_ok=True)
            result = verdict(mutants[index], worker_dir)
            if result is not None:
                with lock:
                    verdicts[index] = result
        finally:
            with lock:
                slots.append(slot)

    # Inside a consumer's prepared use the use lends itself to the workers.
    run_parallel(one, range(len(mutants)), workers=workers)
    return verdicts


def _function_of(functions: Sequence[_gcov.Function], line: int) -> Optional[_gcov.Function]:
    return next((f for f in functions if f.start_line <= line <= f.end_line), None)


def _native_verdict(measurement: _Measurement, mutant: _MullMutant, function,
                    worker_dir: Path) -> Optional[_Verdict]:
    """A mutant's run with C tests: the tests that executed its function, each
    in its own process with the mutant switched on."""
    adapter, origin, deadline = measurement.adapter, measurement.origin, measurement.deadline
    key = (function.name, function.start_line) if function is not None else None
    budget_end = min(deadline, time.monotonic() + measurement.timeout_s)
    by_deadline = budget_end == deadline
    env = adapter._environment({mutant.switch: "1"})
    killers: List[la.TestId] = []
    run = 0
    for file, program in origin.programs.items():
        wanted = [name for name in origin.tests[file]
                  if key is None or key in origin.executed[(file, name)]]
        for name in wanted:
            remaining = budget_end - time.monotonic()
            if remaining <= 0:
                return _spent(measurement, mutant, killers, run, by_deadline)
            try:
                one = adapter._runner.run_one(program, name, adapter._run_test,
                                              cwd=str(worker_dir), env=env, timeout_s=remaining)
            except ControlError as error:
                # The test the process layer failed in, for ``_supervised``.
                error._langadapter_mutant_test = f"{file}::{name}"
                raise
            run += 1
            if one.outcome == "timed_out":
                return _cut(killers, run, by_deadline)
            if one.outcome in ("failed", "error"):
                killers.append(adapter._test_id(file, name))
                if measurement.attribution == "first":
                    return _killed(killers, run)
    if killers:
        return _killed(killers, run)
    return _Verdict("survived", Classification(SURVIVED_STATUS, MutationClass.UNDETECTED,
                                               SURVIVED), tests_run=run)


def _extension_verdict(measurement: _Measurement, mutant: _MullMutant,
                       harness) -> Optional[_Verdict]:
    """A mutant's run with Python tests: the suite in one pytest process with
    Mull's extension and the mutant switched on, stopped at the first failure
    for attribution ``first``."""
    build, attribution = measurement.build, measurement.attribution
    left = measurement.deadline - time.monotonic()
    budget = min(float(measurement.timeout_s), left)
    if budget <= 0:
        return None
    by_deadline = left <= measurement.timeout_s
    extension = measurement.origin.extension
    inner = SuiteRunRequest(suite=measurement.request.suite, timeout_s=max(1.0, budget),
                            runner_options=_pytest_options(first=attribution == "first"),
                            timeout_journal=measurement.journal)
    run, report = harness.run(inner, module=build.module, extension=extension,
                              mutant=mutant.switch)
    failing = list(run.failing_tests())
    if attribution == "first":
        failing = failing[:1]
    ran = len(run.tests)
    if not report.loaded(extension):
        return _Verdict("load_error", Classification(NON_VIABLE_STATUS, MutationClass.DETECTED,
                                                     NON_VIABLE),
                        tests_run=ran, extra={"load_error": report.problem(build.module,
                                                                           extension)})
    if run.outcome is la.Outcome.TIMED_OUT:
        return _cut(failing, ran, by_deadline)
    if run.outcome is la.Outcome.TEST_FAILURES and failing:
        return _killed(failing, ran)
    if run.outcome is la.Outcome.COMPLETED:
        return _Verdict("survived", Classification(SURVIVED_STATUS, MutationClass.UNDETECTED,
                                                   SURVIVED), tests_run=ran)
    if run.outcome is la.Outcome.COLLECTION_FAILED:
        return _Verdict("collection_error", Classification(KILLED_STATUS, MutationClass.DETECTED,
                                                           WITHOUT_NAMED_TEST),
                        tests_run=ran, extra={"reason": run.reason_text()})
    if run.outcome is la.Outcome.NO_TESTS or run.returncode == PYTEST_INTERNAL_ERROR:
        # The unmutated suite passed: the runner's own failure under the
        # mutant (it may use the module under test, as pytest uses bisect)
        # or a collection that found nothing is the mutant's doing.
        native = "no_tests" if run.outcome is la.Outcome.NO_TESTS else "runner_error"
        return _Verdict(native, Classification(KILLED_STATUS, MutationClass.DETECTED,
                                               WITHOUT_NAMED_TEST),
                        tests_run=ran, extra={"reason": run.reason_text()})
    if _crashed(run):
        return _crash_verdict(measurement, mutant, harness, run, report,
                              time.monotonic() + budget)
    return _Verdict("untested", Classification(UNTESTED_STATUS, MutationClass.NOT_RUN, UNTESTED),
                    tests_run=ran,
                    extra={"reason": f"the mutant's run ended without a verdict: "
                                     f"{run.outcome}: {run.reason_text()}"})


def _crashed(run) -> bool:
    """Whether a pytest run ended by a signal or inside a test."""
    return run.active_at_end is not None or (run.returncode is not None and run.returncode < 0)


def _crash_verdict(measurement: _Measurement, mutant: _MullMutant, harness, run, report,
                   budget_end: float) -> _Verdict:
    """A mutant whose pytest process crashed: detected. Each test that failed
    before a crash is a killer. The test a crash ended is recorded as an
    observation, not as a killer. With attribution ``all`` the suite runs
    again without the tests that started, until it ends without a crash or
    the per-mutant time is spent (then the row says ``killers_incomplete``)."""
    crashes = _Crashes()
    while True:
        crashes.note(measurement.adapter, run, report)
        if measurement.attribution != "all" or not _crashed(run) or not report.started:
            break
        remaining = min(budget_end, measurement.deadline) - time.monotonic()
        if remaining <= 0:
            break
        inner = SuiteRunRequest(suite=measurement.request.suite, timeout_s=max(1.0, remaining),
                                runner_options=_pytest_options(first=False),
                                timeout_journal=measurement.journal)
        run, report = harness.run(inner, module=measurement.build.module,
                                  extension=measurement.origin.extension,
                                  deselect=crashes.started, mutant=mutant.switch)
        for test in run.failing_tests():
            if test not in crashes.killers:
                crashes.killers.append(test)
    return crashes.verdict(measurement.attribution == "all" and _crashed(run))


@dataclass
class _Crashes:
    """What a crashed mutant's runs gave: the killing tests, the tests the
    crashes ended, the tests that started, the signals and the tests that
    finished."""

    killers: List[la.TestId] = field(default_factory=list)
    crashed_in: List[str] = field(default_factory=list)
    started: List[str] = field(default_factory=list)
    signals: List[int] = field(default_factory=list)
    ran: int = 0

    def note(self, adapter, run, report) -> None:
        """Add one run of the suite and its report."""
        from langadapter.python.adapter import PythonAdapter

        test_of = PythonAdapter.test_of_nodeid
        for nodeid in report.failed:
            if test_of(nodeid) not in self.killers:
                self.killers.append(test_of(nodeid))
        ended = report.unfinished or (run.active_at_end.text if run.active_at_end else None)
        if ended is not None:
            self.crashed_in.append(adapter.test_id_text(test_of(ended)))
        if run.returncode is not None and run.returncode < 0:
            self.signals.append(-run.returncode)
        self.started += [nodeid for nodeid in report.started if nodeid not in self.started]
        self.ran += len(report.finished)

    def verdict(self, incomplete: bool) -> _Verdict:
        """Killed by the killers, else detected by the crash, with the tests the
        crashes ended, the first signal and, when the time ran out before a
        run without a crash, ``killers_incomplete``."""
        verdict = (_killed(self.killers, self.ran) if self.killers else _Verdict(
            "crashed", Classification(KILLED_STATUS, MutationClass.DETECTED, WITHOUT_NAMED_TEST),
            tests_run=self.ran))
        verdict.extra["crashed_in"] = self.crashed_in
        if self.signals:
            verdict.extra["signal"] = self.signals[0]
        if incomplete:
            verdict.extra["killers_incomplete"] = True
        return verdict


def _killed(killers, run) -> _Verdict:
    return _Verdict("killed", Classification(KILLED_STATUS, MutationClass.DETECTED,
                                             BY_FAILING_TEST), tuple(killers), run)


def _spent(measurement: _Measurement, mutant: _MullMutant, killers, run,
           by_deadline: bool) -> Optional[_Verdict]:
    """The verdict of a mutant whose time ended before its next test started:
    ``_cut``'s with killers or at the run's wall clock, else untested, since
    no test failed or ran out of time, and counted for the run's incomplete
    stamp."""
    if killers or by_deadline:
        return _cut(killers, run, by_deadline)
    measurement.unfinished.append(mutant.engine_id)
    return _Verdict("untested", Classification(UNTESTED_STATUS, MutationClass.NOT_RUN, UNTESTED),
                    tests_run=run, extra={"reason": "the per-mutant timeout ended between two "
                                                    "tests, and no test failed or ran out of time"})


def _cut(killers, run, by_deadline: bool) -> Optional[_Verdict]:
    """The verdict of a mutant whose tests ran out of time: killed by the
    tests that failed before the cut, which may not be all its killers. With
    none, the per-mutant timeout detects it, and when the run's wall clock
    was the bound it has no verdict and is untested."""
    if killers:
        verdict = _killed(killers, run)
        verdict.extra["killers_incomplete"] = True
        return verdict
    if by_deadline:
        return None
    return _Verdict("timeout", Classification(TIMEOUT_STATUS, MutationClass.DETECTED,
                                              BY_TIMEOUT), tests_run=run)


def _mutant(mutant: _MullMutant, verdict: Optional[_Verdict], data: bytes,
            target: str, text_of: Callable[[la.TestId], str]) -> Mutant:
    diff = mutant.diff(data, target)
    if verdict is None:
        verdict = _Verdict("untested", Classification(UNTESTED_STATUS, MutationClass.NOT_RUN,
                                                      UNTESTED))
    texts = tuple(text_of(test) for test in verdict.killers)
    row: Dict[str, Any] = {
        "id": mutant.engine_id, "mutant_id": mutant.engine_id, "line": mutant.line,
        "column": mutant.column, "operator": mutant.operator,
        "status": verdict.classification.status, "native_status": verdict.native,
        "killer": texts[0] if texts else None, "killers": list(texts), "diff": diff,
        "tests_run": verdict.tests_run, **verdict.extra}
    return Mutant(key=mutant_key(target, mutant.operator, mutant.occurrence, mutant.line,
                                 mutant.column, diff),
                  engine_id=mutant.engine_id, test_outcome=verdict.native,
                  worker_outcome=None, classification=verdict.classification, killers=texts,
                  diff=diff, row=row, killing_tests=tuple(verdict.killers))
