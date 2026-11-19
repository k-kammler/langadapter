"""Mutation measurement shared by every engine: requests and settings,
mutant classes, the kill-matrix schema and scores, the evidence behind a
recorded verdict, per-suite verdicts, run results and the mutation session.

A request's settings default to the engine's. A run records each value and
its source (``EffectiveSetting``), the per-mutant timeout also its rule and
inputs. Every mutant keeps its engine's native outcome and carries one
common class (``MutationClass``) with a subcategory. A score counts detected
mutants as killed, undetected and untested ones as not killed, and leaves
skipped ones out. Without untested and skipped mutants it is 100 minus
Cosmic Ray's cr-rate, a survival rate that counts a skipped mutant as
killed. Cosmic Ray's INCOMPETENT, where running the tests or applying the
mutation raised, measured nothing: it is untested, where cr-rate counts it
as killed.

Five methods change or explain what a run records in a mutant's kill-matrix
row (``evidence_of``). None equals a fresh measurement beyond what it states:

Verification (row key ``verification``)
    A kill without a named killing test, or by the per-mutant clock, runs
    again, at most twice. A completed run that does not kill replaces the
    record (``corrected``), as does a kill naming its killing tests
    (``recovered``). A repeated timeout (``reproduced_timeout``) or an
    unnamed kill the runner reported (``reproduced_with_report``) is kept.
    A kill without evidence in either attempt is kept and flagged
    unresolved. A nondeterministic test can give either verdict.
File attribution (row key ``adjudication``)
    An unresolved kill runs against each test file alone, and each file
    whose run kills is recorded, never a test. A kill that needs several
    files together stays unresolved (``pool_context``).
Timeout evidence (row keys ``timeout_evidence``, ``pre_attribution_status``)
    A run the clock ended is read from its per-test journal: tests that
    failed before the clock fired are measured failures (``named_killer``,
    status ``killed``). Otherwise the test running then is recorded
    (``hang``, the budget covered the whole run, so it need not hang alone).
    With every started test finished, the run is unattributed
    (``cumulative``).
Crash observation (row key ``active_at_end``)
    A kill without a named killer whose run ended during a test records the
    test the journal shows running: an observation, not a proven killer.
    The kill's status and killers stay as recorded.
Derivation (row and matrix key ``derived``)
    A subset's result is derived from a measured run without running again.
    ``DERIVATION_ASSUMPTION`` states when it equals a fresh measurement. A
    kill whose run did not record every killing test (a timeout the journal
    attributed, or a row with ``killers_incomplete``) is derived as a
    timeout is, by its line.

``MutationSession.per_suite`` measures every mutant of one target once per
suite. A suite's verdict is the one its standalone run gives
(``PerSuiteRequest.standalone``). A faster implementation must give the same
verdicts and killers (``conformance.check_per_suite``). The verdicts:

- ``killed``: a named test failed under the mutant.
- ``collection_error``: the run failed without a named test: a collection
  error, a crash, or a result without a clean test outcome.
- ``non_viable``: the mutant is not a valid program.
- ``timeout``: the per-mutant time limit ended the run.
- ``survived``: the suite passed under the mutant.
- ``untested``: no result (the run failed, the wall clock ended it first,
  or a filter skipped the mutant). Never counted as survived.

Each verdict but ``collection_error`` is the kill-matrix status of the same
word.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import (
    Any,
    Optional,
    ClassVar,
    Dict,
    Mapping,
    Tuple,
    Union,
    Callable,
    List,
    Set,
    Iterable,
)
import enum
import math
from langadapter.errors import InvalidRequest, LangAdapterError, MeasurementCancelled
from langadapter.outcomes import (
    Outcome,
    ObservationIdentity,
)
from langadapter.suites import Suite, TestId
import copy
import dataclasses
import hashlib
import contextlib
from abc import ABC, abstractmethod
from threading import Lock
from langadapter import _execution_scope, _process_tree


CORRECTED = "corrected"
"""A verification's verdict: a completed run that does not kill replaced
the recorded kill."""
RECOVERED = "recovered"
"""A verification's verdict: a run named the kill's killing tests, which
replaced the record."""
REPRODUCED_TIMEOUT = "reproduced_timeout"
"""A verification's verdict: the timeout repeated, and the record is
kept."""
REPRODUCED_WITH_REPORT = "reproduced_with_report"
"""A verification's verdict: the runner reported the unnamed kill again,
and the record is kept."""
UNRESOLVED = "unresolved"
"""A verification's verdict: neither attempt gave evidence, so the kill
is kept and flagged unresolved."""
NOT_ATTEMPTED = "not_attempted"
"""A verification's verdict: unresolved without a run, since an earlier
batch produced no evidence at all."""
CACHED_GENUINE = "cached_genuine"
"""A verification's verdict: not run again, since the session already
found the same mutant's kill genuine."""
NOT_VERIFIED = "not_verified"
"""A verification's verdict: the verification could not run, and the
sweep's record is kept."""

ATTRIBUTED = "attributed"
"""A file attribution's outcome: each test file whose run alone kills
the mutant is recorded."""
ATTRIBUTION_INCOMPLETE = "incomplete"
"""A file attribution's outcome: a test file gave no result, so the kill
stays unresolved."""
POOL_CONTEXT = "pool_context"
"""A file attribution's outcome: no test file kills alone, so the kill
needs several together and stays unresolved."""
ATTRIBUTION_FAILED = "failed"
"""A file attribution's outcome: the attribution's pass failed before it
recorded its outcomes, and the verdicts measured so far are kept."""

NAMED_KILLER = "named_killer"
"""The journal's reading of a timeout: tests failed before the clock
fired, measured failures that kill the mutant."""
HANG = "hang"
"""The journal's reading of a timeout: the test running when the clock
fired is recorded."""
CUMULATIVE = "cumulative"
"""The journal's reading of a timeout: every started test finished, so
the run is unattributed."""

DERIVATION_ASSUMPTION = (
    "Derived without running the kept tests: a mutant stays killed when a "
    "kept test is one of its recorded killing tests, when it is invalid, "
    "when it was killed without a named test, or, for a timeout or a kill "
    "whose run did not record every killing test, when the kept tests cover "
    "its line or its line is unknown. Otherwise it counts "
    "as survived. This "
    "equals a fresh measurement of the kept tests only when each kept "
    "test's verdict does not depend on the removed tests and every killing "
    "test was recorded (attribution 'all'). Kills that depend on a removed "
    "test's side effects, kills by a timeout and kills without a named test "
    "can differ from a fresh measurement of the kept tests.")
"""What a derived result assumes. A derived kill matrix states it."""


@dataclass(frozen=True)
class NativeRecord:
    """A per-mutant run as the engine recorded it: test and worker outcomes,
    named killing tests, whether the runner reported, and any kept output."""

    test_outcome: Optional[str]
    worker_outcome: Optional[str]
    killers: Tuple[str, ...] = ()
    reported: bool = False
    output: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """The record as plain data, with ``output`` only when it is kept."""
        data: Dict[str, Any] = {
            "test_outcome": self.test_outcome,
            "worker_outcome": self.worker_outcome,
            "killers": list(self.killers),
            "reported": self.reported,
        }
        if self.output is not None:
            data["output"] = self.output
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NativeRecord":
        """The record that ``to_dict`` data holds, an absent key read as
        empty."""
        return cls(test_outcome=data.get("test_outcome"),
                   worker_outcome=data.get("worker_outcome"),
                   killers=tuple(data.get("killers") or ()),
                   reported=bool(data.get("reported")),
                   output=data.get("output"))


@dataclass(frozen=True)
class Verification:
    """One kill's verification: verdict, whether by the clock, the sweep's and
    each attempt's record (None: no result), and whether the sweep's was replaced.
    ``attempts`` holds each attempt's record."""

    kind: ClassVar[str] = "verification"

    verdict: str
    clock: bool
    recorded: NativeRecord
    attempts: Tuple[Optional[NativeRecord], ...] = ()
    replaced: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """The verification as plain data, each record in its ``to_dict``
        form."""
        return {
            "verdict": self.verdict,
            "clock": self.clock,
            "recorded": self.recorded.to_dict(),
            "attempts": [a.to_dict() if a is not None else None
                         for a in self.attempts],
            "replaced": self.replaced,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Verification":
        """The verification that ``to_dict`` data holds."""
        return cls(
            verdict=data["verdict"], clock=bool(data.get("clock")),
            recorded=NativeRecord.from_dict(data.get("recorded") or {}),
            attempts=tuple(NativeRecord.from_dict(a) if a is not None
                           else None for a in data.get("attempts") or ()),
            replaced=bool(data.get("replaced")))


@dataclass(frozen=True)
class FileAttribution:
    """The file attribution of an unresolved kill: its outcome and each test
    file's verdict alone (True kills, False does not, None no result)."""

    kind: ClassVar[str] = "file_attribution"

    outcome: str
    verdicts: Tuple[Tuple[str, Optional[bool]], ...]

    @property
    def files(self) -> Tuple[str, ...]:
        """The files that kill the mutant alone."""
        return tuple(name for name, killed in self.verdicts if killed)

    def to_dict(self) -> Dict[str, Any]:
        """The attribution as plain data: ``outcome``, and ``files`` with
        each file's verdict."""
        return {"outcome": self.outcome, "files": dict(self.verdicts)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FileAttribution":
        """The attribution that ``to_dict`` data holds."""
        return cls(outcome=data["outcome"],
                   verdicts=tuple(sorted((data.get("files") or {}).items())))


@dataclass(frozen=True)
class TimeoutEvidence:
    """The journal's reading of a run the clock ended: ``label``, the
    failing or running ``tests``, and the status before the reading.
    ``status_before`` is that status."""

    kind: ClassVar[str] = "timeout"

    label: str
    tests: Tuple[str, ...]
    status_before: str


@dataclass(frozen=True)
class CrashObservation:
    """The test running when a mutant ended the test process, on a kill
    without a named killer. An observation, not a proven killer.
    ``active_test`` names that test."""

    kind: ClassVar[str] = "crash_observation"

    active_test: str


@dataclass(frozen=True)
class DerivedVerdict:
    """A mutant in a derived result: its measured status and deciding rule.
    ``measured_status`` is the mutant's status in the measured run."""

    kind: ClassVar[str] = "derived"

    measured_status: str
    rule: str

    def to_dict(self) -> Dict[str, Any]:
        """The verdict as plain data: ``measured_status`` and ``rule``."""
        return {"measured_status": self.measured_status, "rule": self.rule}


@dataclass(frozen=True)
class Derivation:
    """How a result was derived: the kept tests, the target lines they
    cover, the measured run's attribution and the assumption. ``kept_tests``
    names the kept tests, sorted."""

    kept_tests: Tuple[str, ...]
    covered_lines: Tuple[int, ...]
    attribution: Optional[str]
    assumption: str = DERIVATION_ASSUMPTION

    def to_dict(self) -> Dict[str, Any]:
        """The derivation as plain data, its ``kind`` ``test_subset``."""
        return {"kind": "test_subset",
                "kept_tests": list(self.kept_tests),
                "covered_lines": list(self.covered_lines),
                "attribution": self.attribution,
                "assumption": self.assumption}


Evidence = Union[Verification, FileAttribution, TimeoutEvidence,
                 CrashObservation, DerivedVerdict]
"""One part of a kill-matrix row's evidence, as ``evidence_of`` reads
it."""


def _require_engine(engine: str, offered: str, language: str) -> None:
    """Refuse a mutation engine other than ``offered``, the one the
    ``language`` adapter offers."""
    if engine != offered:
        raise InvalidRequest("engine", f"unknown mutation engine {engine!r}. The {language} "
                             f"adapter offers {offered}")


def evidence_of(row: Mapping[str, Any]) -> Tuple[Evidence, ...]:
    """The evidence a kill-matrix row records, in the methods' order."""
    items: list = []
    if row.get("verification"):
        items.append(Verification.from_dict(row["verification"]))
    if row.get("adjudication"):
        items.append(FileAttribution.from_dict(row["adjudication"]))
    label = row.get("timeout_evidence")
    if label:
        tests = tuple(row.get("killers") or ()) if label != CUMULATIVE else ()
        items.append(TimeoutEvidence(
            label=label, tests=tests,
            status_before=row.get("pre_attribution_status") or row["status"]))
    if row.get("active_at_end"):
        items.append(CrashObservation(row["active_at_end"]))
    if row.get("derived"):
        items.append(DerivedVerdict(
            measured_status=row["derived"]["measured_status"],
            rule=row["derived"]["rule"]))
    return tuple(items)


class MutationClass(str, enum.Enum):
    """What a measurement established about one mutant."""

    #: A failing test, a crash or a timeout detected the mutant, or it is not
    #: a valid program.
    DETECTED = "detected"
    #: The suite let the mutant live.
    UNDETECTED = "undetected"
    #: The mutant was not measured: a filter skipped it, or no result exists.
    NOT_RUN = "not_run"

    def __str__(self) -> str:
        return self.value


BY_FAILING_TEST = "failing_test"
"""A detected mutant's subcategory: a named test failed under it."""
WITHOUT_NAMED_TEST = "no_named_test"
"""A detected mutant's subcategory: killed without a named test, by a
crash or an error at collection alike."""
BY_TIMEOUT = "timeout"
"""A detected mutant's subcategory: the per-mutant time limit ended the
run."""
NON_VIABLE = "non_viable"
"""A detected mutant's subcategory: it does not compile or load, a
property of the source."""
SURVIVED = "survived"
"""An undetected mutant's subcategory: the suite passed under it."""
INCOMPETENT = "incompetent"
"""The subcategory of a mutant not run: the tool could not run the tests
for it (Cosmic Ray's INCOMPETENT)."""
SKIPPED = "skipped"
"""The subcategory of a mutant not run: a filter excluded its operator."""
UNTESTED = "untested"
"""The subcategory of a mutant not run: the run ended before it, or no
test outcome was recorded."""

SUBCATEGORIES = {
    MutationClass.DETECTED: (BY_FAILING_TEST, WITHOUT_NAMED_TEST, BY_TIMEOUT,
                             NON_VIABLE),
    MutationClass.UNDETECTED: (SURVIVED,),
    MutationClass.NOT_RUN: (SKIPPED, UNTESTED, INCOMPETENT),
}
"""The subcategories each class admits (``Classification``)."""


@dataclass(frozen=True)
class Classification:
    """One mutant's kill-matrix status, common class and subcategory. A
    skipped mutant's status is ``skipped``. A kill matrix leaves it out."""

    status: str
    mutation_class: MutationClass
    subcategory: str

    def __post_init__(self) -> None:
        if self.subcategory not in SUBCATEGORIES[self.mutation_class]:
            raise ValueError(
                f"subcategory {self.subcategory!r} is not one of class "
                f"{self.mutation_class.value!r}")


ATTRIBUTIONS = ("first", "all")
"""Killing tests named per mutant: the first (its run stops there) or
all."""
FROM_REQUEST = "request"
"""Where an effective setting's value came from: the request named it."""
FROM_DEFAULT = "default"
"""Where an effective setting's value came from: the engine's default."""


@dataclass(frozen=True)
class TimeoutRule:
    """A per-mutant timeout of ``factor`` times the passing suite's time,
    rounded up, at least ``minimum_s`` and at most ``cap_s`` seconds (a cap
    below the minimum wins). A missing suite time gives the minimum. The
    default is TestGenEval's rule: 1.5 times the suite time, at least 10 s."""

    factor: float = 1.5
    minimum_s: int = 10
    cap_s: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.factor > 0:
            raise InvalidRequest("per_mutant_timeout.factor",
                                 "must be positive")
        if int(self.minimum_s) < 1:
            raise InvalidRequest("per_mutant_timeout.minimum_s",
                                 "must be at least 1 second")
        if self.cap_s is not None and int(self.cap_s) < 1:
            raise InvalidRequest("per_mutant_timeout.cap_s",
                                 "must be at least 1 second")

    def seconds(self, passing_suite_s: Optional[float]) -> int:
        """The per-mutant timeout for a suite that passes in
        ``passing_suite_s`` seconds."""
        suite_s = passing_suite_s if passing_suite_s is not None else 0.0
        value = max(int(self.minimum_s), math.ceil(self.factor * suite_s))
        if self.cap_s is not None:
            value = min(value, int(self.cap_s))
        return value

    def inputs(self, passing_suite_s: Optional[float]) -> Dict[str, Any]:
        """The rule and the suite time it is applied to."""
        return {"factor": self.factor, "minimum_s": int(self.minimum_s),
                "cap_s": self.cap_s, "passing_suite_s": passing_suite_s}


@dataclass(frozen=True)
class Catalogue:
    """The mutants a run generates: the engine's full catalogue (default), a
    named operator set of the engine, or an explicit list of operators."""

    name: Optional[str] = None
    operators: Optional[Tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if self.operators is not None:
            object.__setattr__(self, "operators", tuple(self.operators))
            if self.name is not None:
                raise InvalidRequest(
                    "catalogue", "names an operator set or lists operators, "
                    "not both")
            if not self.operators:
                raise InvalidRequest("catalogue.operators",
                                     "lists no operator")

    @property
    def kind(self) -> str:
        """``full``, ``named`` or ``operators``."""
        if self.operators is not None:
            return "operators"
        return "named" if self.name else "full"


@dataclass(frozen=True)
class OperatorCatalogue:
    """The operators an engine registers in the project environment, by its
    names, else None with the outcome and reason (``missing_setup``: not
    installed, ``infrastructure_failure``: registry unreadable), and the
    engine's ``tool_version`` when readable."""

    engine: str
    operators: Optional[Tuple[str, ...]]
    outcome: Outcome = Outcome.COMPLETED
    reason: str = ""
    tool_version: Optional[str] = None


@dataclass(frozen=True)
class MutationProgress:
    """``finished`` of ``total`` items done (total None when unknown) after
    ``elapsed_s``, the items named by ``kind``: ``mutants`` measured, or
    ``kill verifications``. Progress, not evidence: a finished mutant's
    verdict can still change when its kill is verified or the run fails."""

    kind: str
    finished: int
    total: Optional[int]
    elapsed_s: float


@dataclass(frozen=True)
class MutationRequest:
    """One mutation run: mutate ``target`` (a project source file, relative
    to its root or absolute under it) and measure the mutants with
    ``suite``. Engine defaults for the other fields: the full catalogue, its
    per-mutant timeout rule applied to ``passing_suite_s``, a one-hour wall
    clock, the first killing test per mutant, the session's workers, and
    unresolved kills kept and flagged.

    ``workers=1`` runs the mutants one at a time, and ``fail_on_unresolved``
    fails a run with an unresolved kill (both suit a retry after running
    out of memory). ``timeout_journal`` journals each per-mutant run's tests
    for the timeout evidence and crash observations. Its default is the
    engine's: on for Cosmic Ray, off for an engine that keeps no such journal
    (PIT, and Mull for C tests), which refuses it. ``progress`` receives
    ``MutationProgress`` events from a separate thread where the engine
    observes them: a local Cosmic Ray run's mutants and every Cosmic Ray
    run's kill verifications, none of PIT's or Mull's. A callback failure
    never changes the run.

    - ``catalogue``: the mutants the run generates.
    - ``per_mutant_timeout``: the time limit of each mutant's run, in whole
      seconds or as a ``TimeoutRule``.
    - ``passing_suite_s``: the passing suite's time a rule applies to. The C
      engine (Mull) times the suite in its own check, each test as it runs
      under a mutant, applies a rule to that time and refuses this field.
    - ``wall_clock_limit_s``: the time limit of the whole run in whole
      seconds.
    - ``attribution``: the killing tests each mutant records: ``first``,
      where its run stops, or ``all``.
    """

    target: str
    suite: Suite
    catalogue: Catalogue = Catalogue()
    per_mutant_timeout: Union[int, TimeoutRule, None] = None
    passing_suite_s: Optional[float] = None
    wall_clock_limit_s: Optional[int] = None
    attribution: Optional[str] = None
    workers: Optional[int] = None
    fail_on_unresolved: Optional[bool] = None
    timeout_journal: Optional[bool] = None
    progress: Optional[Callable[[MutationProgress], None]] = field(
        default=None, repr=False, compare=False)

    def validate(self) -> None:
        """Refuse a request the engine cannot interpret, naming the field."""
        self.suite.validate()
        timeout = self.per_mutant_timeout
        if timeout is not None and (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, TimeoutRule))):
            raise InvalidRequest("per_mutant_timeout",
                                 "must be seconds or a TimeoutRule")
        if isinstance(timeout, int) and timeout < 1:
            raise InvalidRequest("per_mutant_timeout",
                                 "must be at least 1 second")
        if self.passing_suite_s is not None and self.passing_suite_s < 0:
            raise InvalidRequest("passing_suite_s", "must not be negative")
        if self.wall_clock_limit_s is not None and self.wall_clock_limit_s < 1:
            raise InvalidRequest("wall_clock_limit_s",
                                 "must be at least 1 second")
        if self.attribution is not None and self.attribution not in ATTRIBUTIONS:
            raise InvalidRequest(
                "attribution", f"must be one of {', '.join(ATTRIBUTIONS)}")
        if self.workers is not None and self.workers < 1:
            raise InvalidRequest("workers", "must be at least 1")
        if self.timeout_journal is not None and not isinstance(
                self.timeout_journal, bool):
            raise InvalidRequest("timeout_journal", "must be true or false")
        if self.progress is not None and not callable(self.progress):
            raise InvalidRequest("progress", "must be callable")


@dataclass(frozen=True)
class EffectiveSetting:
    """A setting's value, its source (``request`` or ``default``), a rule's inputs."""

    value: Any
    source: str
    inputs: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """The setting as plain data: ``value``, ``source`` and ``inputs``."""
        return {"value": self.value, "source": self.source,
                "inputs": dict(self.inputs)}


def _chosen(requested: Any, default: Any) -> EffectiveSetting:
    """A setting from the request's value, else from the engine's default."""
    if requested is not None:
        return EffectiveSetting(requested, FROM_REQUEST)
    return EffectiveSetting(default, FROM_DEFAULT)


#: Every engine's wall clock limit, in seconds, and kill attribution for a
#: request that names none, and the per-mutant timeout rule of the engines
#: that time each mutant themselves (C's and Python's).
_DEFAULT_WALL_CLOCK_LIMIT_S = 3600
_DEFAULT_ATTRIBUTION = "first"
_DEFAULT_TIMEOUT_RULE = TimeoutRule()


def _common_settings(request: MutationRequest, operators: Iterable[str],
                     per_mutant: EffectiveSetting, workers: EffectiveSetting,
                     timeout_journal: bool, *,
                     after_workers: Optional[Mapping[str, EffectiveSetting]] = None,
                     after: Optional[Mapping[str, EffectiveSetting]] = None,
                     ) -> Dict[str, EffectiveSetting]:
    """The effective settings every engine records, in this order: the
    request's catalogue with ``operators``, ``per_mutant``, the wall clock
    limit, the attribution, ``workers``, ``after_workers``,
    fail_on_unresolved, timeout_journal (``timeout_journal`` unless the
    request names it) and ``after``."""
    catalogue = request.catalogue
    return {
        "catalogue": EffectiveSetting(
            {"kind": catalogue.kind, "name": catalogue.name, "operators": list(operators)},
            FROM_DEFAULT if catalogue.kind == "full" else FROM_REQUEST),
        "per_mutant_timeout": per_mutant,
        "wall_clock_limit": _chosen(request.wall_clock_limit_s, _DEFAULT_WALL_CLOCK_LIMIT_S),
        "attribution": _chosen(request.attribution, _DEFAULT_ATTRIBUTION),
        "workers": workers,
        **(after_workers or {}),
        "fail_on_unresolved": _chosen(request.fail_on_unresolved, False),
        "timeout_journal": _chosen(request.timeout_journal, timeout_journal),
        **(after or {}),
    }




def _record(value: Any, keys: Tuple[str, ...], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise InvalidRequest(field, f"must contain exactly {', '.join(keys)}")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidRequest(field, "must be text")
    return value


def _sequence(value: Any, field: str) -> Tuple[Any, ...]:
    if not isinstance(value, (tuple, list)):
        raise InvalidRequest(field, "must be a list or tuple")
    return tuple(value)


@dataclass(frozen=True)
class MutationResolution:
    """The requested operation and the mechanism used: a run measures. It
    never changes verdicts, completeness or score. Its record holds
    ``prior_identity`` as null and ``conditions`` as an empty list, the
    record's fixed form."""

    requested: str
    method: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.requested != "measure":
            raise InvalidRequest("resolution.requested", "must be measure")
        if self.method != "measured":
            raise InvalidRequest("resolution.method", "must be measured")
        _text(self.reason, "resolution.reason")

    def to_dict(self) -> Dict[str, Any]:
        """The resolution's record, with ``prior_identity`` null and
        ``conditions`` empty."""
        return {"requested": self.requested, "method": self.method, "prior_identity": None,
                "conditions": [], "reason": self.reason}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MutationResolution":
        """The resolution a record holds. A record with other keys, a
        ``prior_identity`` or conditions raises ``InvalidRequest``."""
        data = _record(value, ("requested", "method", "prior_identity", "conditions", "reason"),
                       "resolution")
        if data["prior_identity"] is not None:
            raise InvalidRequest("resolution.prior_identity", "must be null")
        if _sequence(data["conditions"], "resolution.conditions"):
            raise InvalidRequest("resolution.conditions", "must be empty")
        return cls(requested=data["requested"], method=data["method"], reason=data["reason"])


def resolution_of(matrix: Optional[Mapping[str, Any]]) -> Optional[MutationResolution]:
    """The matrix's resolution, or None (unknown provenance). Refuses an
    invalid record and a resolution on a matrix derived for a test subset."""
    if matrix is None or "resolution" not in matrix:
        return None
    derived = matrix.get("derived")
    if isinstance(derived, Mapping) and (
            "assumption" in derived or derived.get("kind") == "test_subset"):
        raise InvalidRequest("resolution", "a result derived for an assumed subset of "
                                 "the tests has no known resolution")
    return MutationResolution.from_dict(matrix["resolution"])


KILLED_STATUS = "killed"
"""The status of a mutant a test failed under, named or not, or whose
timeout the per-test journal traced to a failing test."""
TIMEOUT_STATUS = "timeout"
"""The status of a mutant whose run reached the per-mutant time limit."""
NON_VIABLE_STATUS = "non_viable"
"""The status of a mutant that does not compile or load."""
SURVIVED_STATUS = "survived"
"""The status of a mutant the suite let live."""
SKIPPED_STATUS = "skipped"
"""The status of a mutant a filter excluded. A kill matrix leaves it out."""
UNTESTED_STATUS = "untested"
"""The status of a mutant not measured: the run ended before it, or no
test outcome was recorded."""
# As in Cosmic Ray's cr-rate (TestGenEval's 100 - survival_rate) every status
# but survived counts as killed. An untested mutant counts as not killed, where
# cr-rate leaves it out and a partial measurement would read as whole.
DETECTED_STATUSES = frozenset({KILLED_STATUS, TIMEOUT_STATUS, NON_VIABLE_STATUS})
"""The statuses of a detected mutant, which a score counts as killed: the
class ``MutationClass.DETECTED``."""


def _mutants(matrix: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A stored matrix's rows, read through ``upgrade_kill_matrix``."""
    resolution_of(matrix)
    if matrix and matrix.get("schema_version") != KILL_MATRIX_SCHEMA_VERSION:
        matrix = upgrade_kill_matrix(matrix)
    return list((matrix or {}).get("mutants") or [])


def _operator_filtered(
    mutants: List[Dict[str, Any]], operators: Optional[Set[str]],
) -> List[Dict[str, Any]]:
    """The mutants with an ``operator`` in ``operators`` (None: all)."""
    if operators is None:
        return mutants
    return [m for m in mutants if m.get("operator") in operators]


def has_operators(matrix: Optional[Dict[str, Any]]) -> bool:
    """Whether any mutant carries an ``operator``, so selective scores derive."""
    return any("operator" in m for m in _mutants(matrix))


def killed_ids(
    matrix: Optional[Dict[str, Any]], *, operators: Optional[Set[str]] = None,
) -> Set[Any]:
    """The ids of killed mutants, optionally of a selective operator set."""
    return {
        m["id"] for m in _operator_filtered(_mutants(matrix), operators)
        if m.get("status") in DETECTED_STATUSES and "id" in m
    }


def valid_ids(
    matrix: Optional[Dict[str, Any]], *, operators: Optional[Set[str]] = None,
) -> Set[Any]:
    """The ids that count toward the score: killed, survived and untested
    mutants, optionally of a selective operator set. Without untested and
    skipped mutants this is Cosmic Ray's denominator (``num_results``), which
    counts a skipped mutant too."""
    return {
        m["id"] for m in _operator_filtered(_mutants(matrix), operators)
        if "id" in m
        and (m.get("status") in DETECTED_STATUSES
             or m.get("status") in (SURVIVED_STATUS, UNTESTED_STATUS))
    }


def score_percent(killed: Set[Any], valid: Set[Any]) -> Optional[float]:
    """The percentage of valid mutants killed, or None without valid ones."""
    if not valid:
        return None
    return 100.0 * len(killed & valid) / len(valid)


INCOMPLETE_KEY = "incomplete"
"""The stamp on a matrix whose run the wall clock ended with mutants
left. Its verdicts are kept, but the mutants left are no random sample:
the slow ones are those a timeout would have counted as detected."""
UNTESTED_MUTANTS_IN_CATALOGUE = "untested_mutants_in_catalogue"
"""The reason of the ``INCOMPLETE_KEY`` stamp on a matrix whose catalogue
holds untested mutants."""


def matrix_score_percent(
    matrix: Optional[Dict[str, Any]], *, operators: Optional[Set[str]] = None,
) -> Optional[float]:
    """The mutation score of a kill matrix, optionally of a selective
    operator set, or None when the matrix is stamped incomplete: the ratio
    of its resolved part would read high, since timeouts count as detected.
    ``matrix_lower_bound_percent`` gives such a matrix's lower bound."""
    resolution_of(matrix)
    if isinstance(matrix, dict) and matrix.get(INCOMPLETE_KEY):
        return None
    k = killed_ids(matrix, operators=operators)
    v = valid_ids(matrix, operators=operators)
    return score_percent(k, v)


def matrix_lower_bound_percent(
    matrix: Optional[Dict[str, Any]], *, operators: Optional[Set[str]] = None,
) -> Optional[float]:
    """The lowest score a kill matrix admits (untested not killed), also when
    incomplete, else ``matrix_score_percent``. None without a valid mutant."""
    resolution_of(matrix)
    k = killed_ids(matrix, operators=operators)
    v = valid_ids(matrix, operators=operators)
    return score_percent(k, v)


def unattributed_kill_fraction(
    matrix: Optional[Dict[str, Any]],
) -> Optional[float]:
    """The fraction of killed mutants that are ``killed`` without a named
    killer (every test triggers them, as a collection error does), or None
    without kills. Timeouts and ``non_viable`` mutants, killed by nature,
    count only in the denominator."""
    killed = [m for m in _mutants(matrix) if m.get("status") in DETECTED_STATUSES]
    if not killed:
        return None
    unattributed = sum(
        1 for m in killed
        if m.get("status") == KILLED_STATUS
        and not (m.get("killers") or m.get("killer"))
    )
    return unattributed / len(killed)


def timeout_kill_count(matrix: Optional[Dict[str, Any]]) -> int:
    """How many mutants the per-mutant clock killed (``timeout``)."""
    return sum(1 for m in _mutants(matrix) if m.get("status") == TIMEOUT_STATUS)


def unresolved_kill_count(matrix: Optional[Dict[str, Any]]) -> int:
    """How many mutants carry a kill flagged unresolved."""
    return sum(1 for m in _mutants(matrix) if m.get("unresolved"))


RULE_NOT_KILLED = "not_killed"
"""A subset rule (``derivation_rule``): the measured run did not kill
the mutant."""
RULE_NON_VIABLE = "non_viable"
"""A subset rule: the mutant stays killed since it is not viable, a
property of the source."""
RULE_WITHOUT_NAMED_TEST = "without_named_test"
"""A subset rule: the mutant stays killed since it was killed without a
named test, which every test triggers."""
RULE_KEPT_KILLER = "kept_killer"
"""A subset rule: the mutant stays killed since a kept test is one of
its recorded killers."""
RULE_COVERED_LINE = "covered_line"
"""A subset rule: the mutant stays killed since it is a timeout, or a
kill whose run did not record every killing test, on a line the subset
covers."""
RULE_TIMEOUT_WITHOUT_LINE = "timeout_without_line"
"""A subset rule: the mutant stays killed since it is such a kill and
its line is unknown."""
RULE_NO_KEPT_KILLER = "no_kept_killer"
"""A subset rule: the mutant survives, with no kept killer and, for such
a kill, no covered line."""
_KILLING_RULES = frozenset({RULE_NON_VIABLE, RULE_WITHOUT_NAMED_TEST, RULE_KEPT_KILLER,
                            RULE_COVERED_LINE, RULE_TIMEOUT_WITHOUT_LINE})


def derivation_rule(
    m: Dict[str, Any],
    kept_killed_ids: Set[str],
    covers_line: Callable[[Optional[int]], bool],
) -> str:
    """The rule (``RULE_*``) that decides mutant ``m``'s status under a
    test subset: ``kept_killed_ids`` are the mutants its tests kill with a
    named failing test (the ``K:<mutant_id>`` items of their per-test
    evidence), and ``covers_line(line)`` whether it reaches a line. A
    killed mutant stays killed when it is not viable, killed without a named
    test, killed by a kept test, or, on a covered or unknown line, a timeout
    or a kill whose run did not record every killing test: a timeout the
    journal attributed to the tests that failed before the clock fired
    (``pre_attribution_status``), or a kill a run of every test did not
    record (``killers_incomplete``). Reads only opaque mutant ids, line
    numbers and these two row keys."""
    status = m.get("status")
    if status not in DETECTED_STATUSES:
        return RULE_NOT_KILLED
    if status == NON_VIABLE_STATUS:
        return RULE_NON_VIABLE
    killers = m.get("killers") or ([m["killer"]] if m.get("killer") else [])
    if status == KILLED_STATUS and not killers:
        return RULE_WITHOUT_NAMED_TEST
    if m.get("id") in kept_killed_ids:
        return RULE_KEPT_KILLER
    if (status == TIMEOUT_STATUS or m.get("pre_attribution_status")
            or m.get("killers_incomplete")):
        line = m.get("line")
        if line is None:
            return RULE_TIMEOUT_WITHOUT_LINE
        return RULE_COVERED_LINE if covers_line(line) else RULE_NO_KEPT_KILLER
    return RULE_NO_KEPT_KILLER


def killed_under(
    m: Dict[str, Any],
    kept_killed_ids: Set[str],
    covers_line: Callable[[Optional[int]], bool],
) -> bool:
    """Whether mutant ``m`` stays killed under a test subset."""
    return derivation_rule(m, kept_killed_ids, covers_line) in _KILLING_RULES


def _restricted_row(m: Dict[str, Any], rule: str) -> Dict[str, Any]:
    """A copy of row ``m`` under ``rule``: a kill the subset does not
    reproduce becomes survived without killers or killing tests. Other
    fields are kept."""
    nm = dict(m)
    if m.get("status") in DETECTED_STATUSES and rule not in _KILLING_RULES:
        nm["status"] = SURVIVED_STATUS
        nm["killer"] = None
        nm["killers"] = []
        nm.pop("killing_tests", None)
        nm.pop("unmatched_killers", None)
    return nm


def _summarize(mutants: List[Dict[str, Any]]) -> Dict[str, int]:
    """The summary counts of a mutant list's statuses: ``detected`` counts
    ``DETECTED_STATUSES``, ``timeout`` and ``non_viable`` count two of them."""
    detected = sum(1 for m in mutants if m.get("status") in DETECTED_STATUSES)
    survived = sum(1 for m in mutants if m.get("status") == SURVIVED_STATUS)
    return {
        "total": len(mutants),
        "detected": detected,
        "survived": survived,
        "timeout": sum(1 for m in mutants if m.get("status") == TIMEOUT_STATUS),
        "non_viable": sum(1 for m in mutants if m.get("status") == NON_VIABLE_STATUS),
        "untested": len(mutants) - detected - survived,
        "unresolved": sum(1 for m in mutants if m.get("unresolved")),
    }


def refresh_summary(matrix: Dict[str, Any]) -> Dict[str, Any]:
    """Refresh a matrix's summary after in-place mutant status edits. A
    matrix of an earlier schema version first takes the keys of its upgrade
    (``upgrade_kill_matrix``), so its rows are new copies: a row taken from
    it before that first refresh no longer belongs to it."""
    if matrix.get("schema_version") != KILL_MATRIX_SCHEMA_VERSION:
        matrix.update(upgrade_kill_matrix(matrix))
    matrix["summary"] = _summarize(_mutants(matrix))
    return matrix


def restrict_kill_matrix(
    matrix: Optional[Dict[str, Any]],
    *,
    kept_killed_ids: Set[str],
    kept_lines: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """The kill matrix derived for a test subset from the mutants it kills
    (``kept_killed_ids``) and the lines it covers (``kept_lines``), by
    ``derivation_rule``, as ``MutationRun.derive_for_tests`` does: a kill
    not reproduced becomes survived without killers, all else is kept, and
    a smaller subset can only remove kills. Its ``derived`` names the kept
    kills, lines and assumption, with ``of_derived`` for a derived input,
    and each row's ``derived`` its measured status and rule. Any resolution
    is removed."""

    kept = set(kept_killed_ids)
    lines = set(kept_lines or ())
    out: List[Dict[str, Any]] = []
    for m in _mutants(matrix):
        rule = derivation_rule(m, kept, lines.__contains__)
        row = _restricted_row(m, rule)
        # A row derived before keeps the status that was measured.
        earlier = m.get("derived") if isinstance(m.get("derived"), dict) else {}
        row["derived"] = {"measured_status": earlier.get("measured_status", m.get("status")),
                          "rule": rule}
        out.append(row)
    result = upgrade_kill_matrix(matrix) if matrix else {}
    result.pop("resolution", None)
    result["derived"] = {"kind": "kill_subset", "kept_killed_ids": sorted(kept, key=str),
                         "covered_lines": sorted(lines),
                         "of_derived": isinstance((matrix or {}).get("derived"), dict),
                         "assumption": DERIVATION_ASSUMPTION}
    result["mutants"] = out
    return refresh_summary(result)


def survivors_from_matrix(
    matrix: Optional[Dict[str, Any]], *, operators: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """The surviving mutants of a stored kill matrix, each ``{id, line,
    diff}`` in matrix order, optionally of a selective operator set."""
    out: List[Dict[str, Any]] = []
    for m in _operator_filtered(_mutants(matrix), operators):
        if m.get("status") == SURVIVED_STATUS:
            out.append({
                "id": m.get("id"),
                "line": m.get("line"),
                "diff": m.get("diff"),
            })
    return out


#: The outcomes of a run that measured its mutants.
_MEASURED = (Outcome.COMPLETED, Outcome.TIMED_OUT)

FAILED_SESSION = "session"
"""What failed in a run that measured nothing (``MutationRun.failure``):
the session failed or was closed."""
FAILED_INITIALIZATION = "initialization"
"""What failed in a run that measured nothing: the tool did not
initialize."""
FAILED_INSTRUMENT_CHECK = "instrument_check"
"""What failed in a run that measured nothing: the instrument gave no
verdicts."""
FAILED_SESSION_READ = "session_read"
"""What failed in a run that measured nothing: the tool's session was
unreadable."""
FAILED_WORKERS = "workers"
"""What failed in a run that measured nothing: the worker raised for
every mutant, or no mutant has a test outcome."""
FAILURES = (FAILED_SESSION, FAILED_INITIALIZATION, FAILED_INSTRUMENT_CHECK,
            FAILED_SESSION_READ, FAILED_WORKERS)
"""Every kind of failure of a run that measured nothing."""

KILL_MATRIX_SCHEMA_VERSION = 3
"""The kill-matrix schema a run writes. A matrix of an earlier version
reads as this one through ``upgrade_kill_matrix``."""

#: The words of version 2 and earlier, and the words that replace them.
_EARLIER_STATUSES = {"ok_killed": KILLED_STATUS, "bad_timeout": TIMEOUT_STATUS,
                     "ok_suspicious": NON_VIABLE_STATUS, "bad_survived": SURVIVED_STATUS}
_EARLIER_SUMMARY_KEYS = {"killed": "detected", "suspicious": "non_viable"}
_EARLIER_RULES = {"invalid": RULE_NON_VIABLE}
_EARLIER_VERDICTS = {"not_measured": UNTESTED_STATUS}
#: The C engine's native status of a mutant under which the Python extension
#: did not load, also the row key of the loader's message. No other engine
#: writes either.
_LOAD_ERROR = "load_error"


def _upgrade_row(row: Dict[str, Any]) -> bool:
    """Row ``row`` of an earlier version in this version's words, in place.
    True for a C mutant under which the Python extension did not load, which
    version 2 recorded as killed: it becomes non_viable, with its derived
    status and rule, as the C engine records it."""
    load_error = (_EARLIER_STATUSES.get(row.get("status")) == KILLED_STATUS
                  and row.get("native_status") == _LOAD_ERROR and _LOAD_ERROR in row)
    derived = row.get("derived") if isinstance(row.get("derived"), dict) else {}
    for record, key, words in (
            (row, "status", _EARLIER_STATUSES),
            (row, "pre_attribution_status", _EARLIER_STATUSES),
            (derived, "measured_status", _EARLIER_STATUSES),
            (derived, "rule", _EARLIER_RULES),
            *((verdict, "verdict", _EARLIER_VERDICTS)
              for verdict in (row.get("suite_verdicts") or {}).values())):
        if key in record:
            record[key] = words.get(record[key], record[key])
    if load_error:
        row["status"] = NON_VIABLE_STATUS
        for key, word in (("measured_status", NON_VIABLE_STATUS), ("rule", RULE_NON_VIABLE)):
            if key in derived:
                derived[key] = word
    return load_error


def upgrade_kill_matrix(matrix: Mapping[str, Any]) -> Dict[str, Any]:
    """A stored kill matrix of an earlier schema version, or of none, as a
    new matrix of ``KILL_MATRIX_SCHEMA_VERSION``: each row's ``status``,
    ``pre_attribution_status`` and derived status and rule, its per-suite
    verdicts and the summary's keys in this version's words, every other
    value unchanged but one. A C mutant under which the Python extension did
    not load (native status ``load_error``), which version 2 recorded as
    killed, becomes non_viable as the C engine records it, still
    detected, and the summary's counts follow it. A matrix of this version
    comes back as an equal copy. Raises ``InvalidRequest`` for a later or
    unreadable version."""
    version = matrix.get("schema_version")
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)
                                or version > KILL_MATRIX_SCHEMA_VERSION):
        raise InvalidRequest("schema_version", f"must be at most {KILL_MATRIX_SCHEMA_VERSION}, "
                             f"got {version!r}")
    result = copy.deepcopy(dict(matrix))
    if version == KILL_MATRIX_SCHEMA_VERSION:
        return result
    load_errors = False
    for row in result.get("mutants") or ():
        load_errors |= _upgrade_row(row)
    if isinstance(result.get("summary"), dict):
        result["summary"] = {_EARLIER_SUMMARY_KEYS.get(key, key): value
                             for key, value in result["summary"].items()}
        if load_errors:
            result["summary"].update(_summarize(result["mutants"]))
    result["schema_version"] = KILL_MATRIX_SCHEMA_VERSION
    return result


@dataclass(frozen=True)
class MutantKey:
    """A mutant's identity across runs over one source and catalogue: file,
    operator, occurrence, location, change SHA-256 (engine ids hold per run).
    ``diff_sha256`` is the SHA-256 of the mutant's diff, None without a
    diff."""

    file: Optional[str]
    operator: Optional[str]
    occurrence: Optional[int]
    line: Optional[int]
    column: Optional[int]
    diff_sha256: Optional[str]


@dataclass(frozen=True)
class Mutant:
    """One mutant of a run: its kill-matrix ``row`` and the row's typed
    reading. ``killers`` are as the test runner printed them,
    ``killing_tests`` the same tests within the suite when the run knows
    it, and ``unmatched_killers`` those naming no test of the suite. A kill
    attributed to a file is in neither: its evidence names the file."""

    key: MutantKey
    engine_id: Any
    test_outcome: Optional[str]
    worker_outcome: Optional[str]
    classification: Classification
    killers: Tuple[str, ...]
    diff: Optional[str]
    row: Mapping[str, Any]
    killing_tests: Tuple[TestId, ...] = ()
    unmatched_killers: Tuple[str, ...] = ()

    @property
    def status(self) -> str:
        """The kill-matrix status."""
        return self.row["status"]

    @property
    def mutant_id(self) -> Any:
        """The row's ``mutant_id``, stable across runs over the same source and
        catalogue, else the engine id (Cosmic Ray's changes per session)."""
        return self.row.get("mutant_id", self.engine_id)

    @property
    def evidence(self) -> Tuple[Evidence, ...]:
        """The evidence recorded for this mutant (``evidence_of``)."""
        return evidence_of(self.row)


@dataclass(frozen=True)
class MutationIdentity:
    """What a mutation run observed: the observation's identity, catalogue,
    requested operators, per-mutant timeout, attribution and workers.
    ``per_mutant_timeout_s`` is the value of the run's ``per_mutant_timeout``
    setting in seconds: the timeout, or for PIT the constant its rule adds
    to the factor times each test's time."""

    observation: ObservationIdentity
    catalogue: str
    operators: Tuple[str, ...]
    per_mutant_timeout_s: int
    attribution: str
    workers: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "operators", tuple(self.operators))

    def to_dict(self) -> Dict[str, Any]:
        """The identity as plain data: the observation's ``to_dict`` with
        the run's settings added."""
        return {**self.observation.to_dict(),
                "catalogue": self.catalogue,
                "operators": list(self.operators),
                "per_mutant_timeout_s": self.per_mutant_timeout_s,
                "attribution": self.attribution,
                "workers": self.workers}


@dataclass(frozen=True)
class MutationRun:
    """The result of one mutation run over one target file, complete after
    its session is gone (``to_kill_matrix`` serializes it). ``outcome`` is
    ``completed``, ``timed_out`` (the wall clock cut it, incomplete), or a
    failure without mutants, counts or score, where ``failure`` is one of
    ``FAILURES``. ``derivation`` is set on a result derived for a test
    subset. ``resolution`` is None when not recorded. ``origin_run_s`` is
    the time of the unmutated run that checked the suite passes and where
    the tests load the target from: operation time, not mutant time (None
    without such a run). ``stopping_tests`` are, as for
    ``PerTestEvidence``, the tests the failure of that run is pinned on: a
    test that did not pass in it, or whose own coverage counts in it cannot
    be read or are missing. The run then measured nothing. Empty
    otherwise."""

    target_file: str
    mutants: Tuple[Mutant, ...]
    summary: Mapping[str, Any]
    requested_catalogue: str
    requested_operators: Tuple[str, ...]
    incomplete: Optional[Mapping[str, Any]] = None
    outcome: Outcome = Outcome.COMPLETED
    reason: str = ""
    identity: Optional[MutationIdentity] = None
    settings: Mapping[str, EffectiveSetting] = field(default_factory=dict)
    derivation: Optional[Derivation] = None
    failure: Optional[str] = None
    resolution: Optional[MutationResolution] = None
    origin_run_s: Optional[float] = None
    stopping_tests: Tuple[TestId, ...] = ()

    def __post_init__(self) -> None:
        if self.resolution is not None:
            if not isinstance(self.resolution, MutationResolution):
                raise InvalidRequest("resolution", "must be MutationResolution or None")
            if self.derivation is not None:
                raise InvalidRequest("resolution", "a result derived for an assumed subset of "
                                     "the tests has no known resolution")

    @classmethod
    def failed(cls, target_file: str, outcome: Outcome, reason: str, *,
               failure: Optional[str] = None,
               identity: Optional[MutationIdentity] = None,
               settings: Optional[Mapping[str, EffectiveSetting]] = None,
               stopping_tests: Iterable[TestId] = (),
               ) -> "MutationRun":
        """A run that measured nothing, with the reason, any known
        ``failure`` and the tests that stopped it."""
        if failure is not None and failure not in FAILURES:
            raise InvalidRequest(
                "failure", f"must be one of {', '.join(FAILURES)}")
        return cls(target_file=target_file, mutants=(), summary={},
                   requested_catalogue="", requested_operators=(),
                   outcome=outcome, reason=reason, identity=identity,
                   settings=dict(settings or {}), failure=failure,
                   stopping_tests=tuple(stopping_tests))

    @property
    def measured(self) -> bool:
        """Whether the run completed or was cut by the wall clock."""
        return self.outcome in _MEASURED

    @property
    def complete(self) -> bool:
        """Whether the run was measured and every mutant of its catalogue has
        a result. A run that measured nothing, because a step failed or the
        suite does not pass, is not complete."""
        return self.measured and self.incomplete is None

    def counts(self) -> Optional[Dict[MutationClass, int]]:
        """The mutants per common class, or None if nothing was measured."""
        if not self.measured:
            return None
        counts = dict.fromkeys(MutationClass, 0)
        for mutant in self.mutants:
            counts[mutant.classification.mutation_class] += 1
        return counts

    def score(self) -> Optional[float]:
        """The mutation score over the common classes, in percent. None when
        incomplete, nothing was measured, or no mutant is valid."""
        if not self.complete:
            return None
        return self.progress_score()

    def progress_score(self) -> Optional[float]:
        """``score`` for any measured run, a lower bound while incomplete (untested
        not killed). None when nothing was measured or no mutant is valid."""
        if not self.measured:
            return None
        killed = valid = 0
        for mutant in self.mutants:
            klass = mutant.classification.mutation_class
            if klass is MutationClass.DETECTED:
                killed += 1
                valid += 1
            elif (klass is MutationClass.UNDETECTED
                  or mutant.classification.subcategory in (UNTESTED, INCOMPETENT)):
                valid += 1
        # Scaled before dividing, as every LangAdapter percentage.
        return 100.0 * killed / valid if valid else None

    def survivors(self) -> Tuple[Mutant, ...]:
        """The mutants the suite let live, with their changes."""
        return tuple(m for m in self.mutants
                     if m.classification.mutation_class
                     is MutationClass.UNDETECTED)

    def unmatched_killers(self) -> Tuple[str, ...]:
        """Every killer of the run that names no test of its suite."""
        return tuple(sorted({killer for m in self.mutants
                             for killer in m.unmatched_killers}))

    def derive_for_tests(self, kept: Iterable[TestId], *,
                         covered_lines: Iterable[int]) -> "MutationRun":
        """The result for the ``kept`` tests of this run's suite, derived by
        ``derivation_rule`` without running them. ``covered_lines`` are the
        target lines they cover. Each mutant records its measured status and
        rule, the result its ``Derivation``. Raises ``InvalidRequest`` for a
        derived result, a kept test outside the suite, and a kill whose
        killer the run did not place in its suite (attributed to a file, or
        a run without a suite)."""
        if self.derivation is not None:
            raise InvalidRequest(
                "run", "is derived. Derive from the measured run")
        kept_tests = frozenset(kept)
        if self.identity is not None:
            files = {f.path for f in self.identity.observation.suite}
            outside = sorted(str(t) for t in kept_tests if t.file not in files)
            if outside:
                raise InvalidRequest(
                    "kept", f"not tests of the run's suite: "
                    f"{', '.join(outside)}")
        for mutant in self.mutants:
            if (mutant.status in DETECTED_STATUSES
                    and len(mutant.killing_tests) != len(mutant.killers)):
                raise InvalidRequest(
                    "run", f"mutant {mutant.engine_id} "
                    f"({mutant.key.operator} at line {mutant.key.line}) was "
                    f"killed by {', '.join(mutant.killers)}, which the run "
                    f"did not place among its suite's tests. A derivation "
                    f"cannot decide it: measure the kept tests instead")
        lines = frozenset(covered_lines)
        kept_ids = {mutant.row.get("id") for mutant in self.mutants
                    if kept_tests.intersection(mutant.killing_tests)}
        mutants = []
        for mutant in self.mutants:
            rule = derivation_rule(dict(mutant.row), kept_ids,
                                   lines.__contains__)
            row = _restricted_row(dict(mutant.row), rule)
            row["derived"] = DerivedVerdict(mutant.status, rule).to_dict()
            if row["status"] == mutant.status:
                # A kill that stays names only the kept tests among its killers.
                pairs = [(killer, test) for killer, test
                         in zip(mutant.killers, mutant.killing_tests) if test in kept_tests]
                if len(pairs) != len(mutant.killers):
                    row["killers"] = [killer for killer, _ in pairs]
                    row["killer"] = row["killers"][0] if pairs else None
                    mutant = dataclasses.replace(
                        mutant, killers=tuple(row["killers"]),
                        killing_tests=tuple(test for _, test in pairs))
                mutants.append(dataclasses.replace(mutant, row=row))
            else:
                mutants.append(dataclasses.replace(
                    mutant, row=row, killers=(), killing_tests=(),
                    unmatched_killers=(), classification=Classification(
                        row["status"], MutationClass.UNDETECTED, SURVIVED)))
        attribution = self.identity.attribution if self.identity else None
        return dataclasses.replace(
            self, mutants=tuple(mutants),
            summary=_summarize([dict(m.row) for m in mutants]),
            derivation=Derivation(
                kept_tests=tuple(sorted(str(t) for t in kept_tests)),
                covered_lines=tuple(sorted(lines)),
                attribution=attribution), resolution=None, origin_run_s=None)

    def to_kill_matrix(
        self, fields: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """The run as a kill matrix: schema, target, a row per mutant with
        its ``key`` and, for a kill with killers, its ``killing_tests`` and
        any ``unmatched_killers``, counts, catalogue provenance, with a known
        identity the suite's files and hashes, the execution and the
        attribution (``first`` or ``all``, as the run recorded killing
        tests), any derivation and resolution, and the incomplete stamp.
        ``fields`` are added but cannot replace the resolution."""
        rows = [copy.deepcopy(dict(m.row)) for m in self.mutants]
        for row, mutant in zip(rows, self.mutants):
            # The mutant across runs: the engine's id holds within one run.
            row["key"] = dataclasses.asdict(mutant.key)
            # What kill attribution reads (LangAdapter.kill_tokens).
            if mutant.killers:
                row["killing_tests"] = [dataclasses.asdict(test) for test in mutant.killing_tests]
            if mutant.unmatched_killers:
                row["unmatched_killers"] = list(mutant.unmatched_killers)
        payload: Dict[str, Any] = {
            "schema_version": KILL_MATRIX_SCHEMA_VERSION,
            "target_file": self.target_file,
            "mutants": rows,
            "summary": dict(self.summary),
            "catalogue_provenance": {
                "requested_catalogue": self.requested_catalogue,
                "requested_operators": list(self.requested_operators),
                "observed_operators": sorted({
                    str(row.get("operator")) for row in rows
                    if row.get("operator") is not None
                }),
                "observed_mutant_ids": sorted({
                    str(row.get("id")) for row in rows
                    if row.get("id") is not None
                }),
            },
        }
        if self.identity is not None:
            payload["suite"] = [
                f.to_dict() for f in self.identity.observation.suite]
            payload["execution"] = self.identity.observation.execution.to_dict()
            payload["attribution"] = self.identity.attribution
        if self.stopping_tests:
            payload["stopping_tests"] = [test.text for test in self.stopping_tests]
        if self.derivation is not None:
            payload["derived"] = self.derivation.to_dict()
        if self.resolution is not None:
            payload["resolution"] = self.resolution.to_dict()
        if fields is not None and "resolution" in fields and (
                self.resolution is None or fields["resolution"] != payload["resolution"]):
            raise InvalidRequest("fields.resolution", "cannot replace the run's resolution")
        payload.update(fields or {})
        resolution_of(payload)
        if self.incomplete is not None:
            payload[INCOMPLETE_KEY] = dict(self.incomplete)
        return payload


def mutant_key(file: Optional[str], operator: Optional[str],
               occurrence: Optional[int], line: Optional[int],
               column: Optional[int], diff: Optional[str]) -> MutantKey:
    """The key of a mutant from its engine record."""
    return MutantKey(
        file=file, operator=operator,
        occurrence=int(occurrence) if occurrence is not None else None,
        line=int(line) if line is not None else None,
        column=int(column) if column is not None else None,
        diff_sha256=(hashlib.sha256(diff.encode("utf-8")).hexdigest()
                     if diff else None),
    )


COLLECTION_ERROR = "collection_error"
"""A per-suite verdict: the run failed without a named test, by a
collection error, a crash or a result without a clean test outcome."""
VERDICTS = (KILLED_STATUS, COLLECTION_ERROR, NON_VIABLE_STATUS, TIMEOUT_STATUS,
            SURVIVED_STATUS, UNTESTED_STATUS)
"""Every per-suite verdict, in the order of the module's table: the
statuses of those words and ``COLLECTION_ERROR``. ``UNTESTED_STATUS`` is a
suite without a result for the mutant, never counted as survived."""

DEFAULT_IMPLEMENTATION = "per_suite_runs"
"""The implementation that runs one standalone measurement per suite."""


@dataclass(frozen=True)
class BudgetedSuite:
    """One suite of a per-suite request: unique ``name``, the suite and its
    per-mutant budget as in ``MutationRequest`` (seconds, a rule applied to
    its own passing time, or None for the engine's rule)."""

    name: str
    suite: Suite
    per_mutant_timeout: Union[int, TimeoutRule, None] = None
    passing_suite_s: Optional[float] = None

    def validate(self) -> None:
        """Refuse an empty name, a suite that is no ``Suite`` or an invalid
        per-mutant timeout with ``InvalidRequest``, naming the field."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise InvalidRequest("suites.name", "must be non-empty text")
        if not isinstance(self.suite, Suite):
            raise InvalidRequest("suites.suite", "must be a Suite")
        timeout = self.per_mutant_timeout
        if timeout is not None and (isinstance(timeout, bool)
                                    or not isinstance(timeout, (int, TimeoutRule))):
            raise InvalidRequest("suites.per_mutant_timeout",
                                 "must be seconds, a TimeoutRule or None")
        if isinstance(timeout, int) and timeout < 1:
            raise InvalidRequest("suites.per_mutant_timeout",
                                 "must be at least 1 second")


@dataclass(frozen=True)
class PerSuiteRequest:
    """Measure ``target``'s mutants with each of ``suites``. The other fields
    are ``MutationRequest`` settings of every standalone run (each run is
    bounded by ``wall_clock_limit_s``).

    - ``catalogue``: the mutants each run generates.
    - ``attribution``: the killing tests each mutant records, ``first`` or
      ``all``.
    - ``workers``: the workers of each run.
    - ``fail_on_unresolved``: fails a run with an unresolved kill.
    - ``timeout_journal``: journals each per-mutant run's tests for the
      timeout evidence and crash observations.
    - ``progress``: receives ``MutationProgress`` events where the engine
      observes them.
    """

    target: str
    suites: Tuple[BudgetedSuite, ...]
    catalogue: Catalogue = Catalogue()
    attribution: Optional[str] = None
    wall_clock_limit_s: Optional[int] = None
    workers: Optional[int] = None
    fail_on_unresolved: Optional[bool] = None
    timeout_journal: Optional[bool] = None
    progress: Optional[Callable[[MutationProgress], None]] = field(
        default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "suites", tuple(self.suites))

    @property
    def names(self) -> Tuple[str, ...]:
        """The suites' names, in request order."""
        return tuple(suite.name for suite in self.suites)

    def validate(self) -> None:
        """Refuse a request no standalone run could interpret."""
        if not self.suites:
            raise InvalidRequest("suites", "must name at least one suite")
        for suite in self.suites:
            if not isinstance(suite, BudgetedSuite):
                raise InvalidRequest("suites", "must hold BudgetedSuite values")
            suite.validate()
        names = self.names
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise InvalidRequest("suites.name",
                                 f"names must be unique: {', '.join(repeated)}")
        if self.attribution is not None and self.attribution not in ATTRIBUTIONS:
            raise InvalidRequest(
                "attribution", f"must be one of {', '.join(ATTRIBUTIONS)}")
        for name in names:
            self.standalone(name).validate()

    def standalone(self, name: str) -> MutationRequest:
        """The standalone run that defines suite ``name``'s verdicts."""
        suite = next((s for s in self.suites if s.name == name), None)
        if suite is None:
            raise InvalidRequest("name", f"no suite is named {name!r}")
        return MutationRequest(
            target=self.target, suite=suite.suite, catalogue=self.catalogue,
            per_mutant_timeout=suite.per_mutant_timeout,
            passing_suite_s=suite.passing_suite_s,
            wall_clock_limit_s=self.wall_clock_limit_s,
            attribution=self.attribution, workers=self.workers,
            fail_on_unresolved=self.fail_on_unresolved,
            timeout_journal=self.timeout_journal, progress=self.progress)


@dataclass(frozen=True)
class SuiteVerdict:
    """One suite's verdict for one mutant, with the killers its run recorded
    (all or the first, by attribution) and the tests it placed them in."""

    verdict: str
    killers: Tuple[str, ...] = ()
    killing_tests: Tuple[TestId, ...] = ()

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"verdict {self.verdict!r} is not one of "
                             f"{', '.join(VERDICTS)}")
        object.__setattr__(self, "killers", tuple(self.killers))
        object.__setattr__(self, "killing_tests", tuple(self.killing_tests))

    def to_dict(self) -> Dict[str, Any]:
        """The verdict as plain data, with ``killing_tests`` only when it
        has killers."""
        payload: Dict[str, Any] = {"verdict": self.verdict, "killers": list(self.killers)}
        if self.killers:
            payload["killing_tests"] = [dataclasses.asdict(test) for test in self.killing_tests]
        return payload


@dataclass(frozen=True)
class MutantVerdicts:
    """One mutant's verdict from every suite, by suite name in request
    order. ``key`` matches the mutant across the suites' runs."""

    key: MutantKey
    diff: Optional[str]
    verdicts: Mapping[str, SuiteVerdict]


@dataclass(frozen=True)
class PerSuiteRun:
    """The result of ``MutationSession.per_suite``. ``runs`` holds each
    suite's run, equal to its standalone run, with its outcome, identity,
    settings and unresolved kills. ``mutants`` aligns each mutant's verdicts
    across the suites. ``outcome`` is completed when every run completed,
    otherwise the first other outcome, with the suites in ``reason``.
    ``implementation`` names what measured the suites."""

    target: str
    outcome: Outcome
    reason: str
    implementation: str
    runs: Mapping[str, MutationRun]
    mutants: Tuple[MutantVerdicts, ...]

    def verdicts(self, name: str) -> Dict[MutantKey, SuiteVerdict]:
        """Suite ``name``'s verdict for every mutant."""
        if name not in self.runs:
            raise InvalidRequest("name", f"no suite is named {name!r}")
        return {mutant.key: mutant.verdicts[name] for mutant in self.mutants}

    def kill_matrix(self, name: Optional[str] = None, *,
                    fields: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Suite ``name``'s own kill matrix, or without a name the merge of
        all suites, every row with the mutant's ``key``. A merged row is
        ``killed`` when any suite killed or hit a collection error, else
        ``non_viable`` when a suite's run found that the mutant did not load
        (the C engine's ``load_error``, which belongs to the mutant, not to a
        suite), else ``untested`` when any has no result, then ``timeout``,
        ``non_viable``, ``survived``. Its killers are every suite's in
        order, ``suite_verdicts`` each suite's verdict. A merge with untested
        rows is stamped incomplete."""
        if name is not None:
            if name not in self.runs:
                raise InvalidRequest("name", f"no suite is named {name!r}")
            return self.runs[name].to_kill_matrix(fields)
        rows = []
        did_not_load = _did_not_load(self.runs)
        for mutant in self.mutants:
            row = _representative_row(self.runs, mutant)
            killers: List[str] = []
            for verdict in mutant.verdicts.values():
                killers.extend(k for k in verdict.killers if k not in killers)
            row.update(status=_merged_status(mutant, did_not_load),
                       key=dataclasses.asdict(mutant.key),
                       killer=killers[0] if killers else None,
                       killers=killers,
                       suite_verdicts={suite: verdict.to_dict() for suite, verdict
                                       in mutant.verdicts.items()})
            rows.append(row)
        payload: Dict[str, Any] = {
            "schema_version": KILL_MATRIX_SCHEMA_VERSION,
            "target_file": self.target,
            "mutants": rows,
            "summary": _summarize(rows),
            "per_suite": {"implementation": self.implementation,
                          "suites": list(self.runs),
                          "outcomes": {suite: run.outcome.value
                                       for suite, run in self.runs.items()}},
        }
        executions = [run.identity.observation.execution.to_dict()
                      for run in self.runs.values() if run.identity is not None]
        if executions:
            # The suites' runs share one session, so one execution.
            payload["execution"] = executions[0]
        payload.update(fields or {})
        if payload["summary"]["untested"]:
            payload[INCOMPLETE_KEY] = {
                "reason": UNTESTED_MUTANTS_IN_CATALOGUE,
                "untested": payload["summary"]["untested"],
                "total": payload["summary"]["total"]}
        return payload


def _verdict_of(mutant: Mutant) -> SuiteVerdict:
    """A measured mutant's verdict, from its common class and subcategory."""
    klass = mutant.classification.mutation_class
    sub = mutant.classification.subcategory
    if sub == NON_VIABLE:
        return SuiteVerdict(NON_VIABLE_STATUS)
    if klass is MutationClass.DETECTED:
        verdict = (KILLED_STATUS if sub == BY_FAILING_TEST
                   else TIMEOUT_STATUS if sub == BY_TIMEOUT else COLLECTION_ERROR)
        return SuiteVerdict(verdict, mutant.killers, mutant.killing_tests)
    if klass is MutationClass.UNDETECTED:
        return SuiteVerdict(SURVIVED_STATUS)
    return SuiteVerdict(UNTESTED_STATUS)


def _assemble(request: PerSuiteRequest, runs: Mapping[str, MutationRun],
             implementation: str) -> PerSuiteRun:
    """The suites' runs aligned by mutant key. A mutant missing from a
    suite's run, of a run that measured nothing, or whose key occurs twice
    in it (counted in the reason) is ``untested`` for that suite."""
    names = request.names
    missing = [name for name in names if name not in runs]
    if missing:
        raise InvalidRequest("runs", f"no run for suite(s) {', '.join(missing)}")
    order: List[MutantKey] = []
    diffs: Dict[MutantKey, Optional[str]] = {}
    by_suite: Dict[str, Dict[MutantKey, Mutant]] = {}
    ambiguous = 0
    for name in names:
        run = runs[name]
        seen: Dict[MutantKey, Mutant] = {}
        repeated = set()
        for mutant in (run.mutants if run.measured else ()):
            if mutant.key in seen:
                repeated.add(mutant.key)
            seen[mutant.key] = mutant
            if mutant.key not in diffs:
                order.append(mutant.key)
                diffs[mutant.key] = mutant.diff
        for key in repeated:
            del seen[key]
        ambiguous += len(repeated)
        by_suite[name] = seen
    mutants = tuple(
        MutantVerdicts(key, diffs[key], {
            name: (_verdict_of(by_suite[name][key]) if key in by_suite[name]
                   else SuiteVerdict(UNTESTED_STATUS))
            for name in names})
        for key in order)
    incomplete = [name for name in names if runs[name].outcome is not Outcome.COMPLETED]
    outcome, reasons = Outcome.COMPLETED, []
    if incomplete:
        outcome = runs[incomplete[0]].outcome
        reasons.append(f"{len(incomplete)} of {len(names)} suites did not complete: "
                       + ", ".join(f"{name} ({runs[name].outcome.value}: "
                                   f"{runs[name].reason})" for name in incomplete))
    if ambiguous:
        reasons.append(f"{ambiguous} mutant key(s) occur twice in one suite's run "
                       f"and have no verdict for that suite")
    return PerSuiteRun(target=_target_name(request, runs), outcome=outcome,
                       reason=". ".join(reasons), implementation=implementation,
                       runs={name: runs[name] for name in names},
                       mutants=mutants)


def _target_name(request: PerSuiteRequest, runs: Mapping[str, MutationRun]) -> str:
    for run in runs.values():
        if run.measured and run.target_file:
            return run.target_file
    return request.target


#: The row fields that identify a mutant rather than one suite's measurement.
_IDENTITY_FIELDS = ("id", "line", "operator", "diff")


def _representative_row(runs: Mapping[str, MutationRun],
                        mutant: MutantVerdicts) -> Dict[str, Any]:
    """The identifying fields of the mutant's row in the first suite that
    measured it (the engine id holds within that run only)."""
    for run in runs.values():
        for candidate in run.mutants:
            if candidate.key == mutant.key:
                return {name: copy.deepcopy(candidate.row[name])
                        for name in _IDENTITY_FIELDS if name in candidate.row}
    return {"id": None, "line": mutant.key.line, "operator": mutant.key.operator,
            "diff": mutant.diff}


def _did_not_load(runs: Mapping[str, MutationRun]) -> Set[Tuple[str, MutantKey]]:
    """The (suite, key) pairs of the mutants whose suite's run found that
    they did not load: the C engine's native status ``load_error``."""
    return {(name, mutant.key) for name, run in runs.items()
            for mutant in run.mutants if mutant.row.get("native_status") == _LOAD_ERROR}


def _merged_status(mutant: MutantVerdicts, did_not_load: Set[Tuple[str, MutantKey]]) -> str:
    """The union row's status, in the order ``PerSuiteRun.kill_matrix``
    states: a non_viable verdict of a mutant that did not load ranks with
    killed, since the load error belongs to the mutant."""
    kinds = {verdict.verdict for verdict in mutant.verdicts.values()}
    if kinds & {KILLED_STATUS, COLLECTION_ERROR}:
        return KILLED_STATUS
    if any(verdict.verdict == NON_VIABLE_STATUS and (name, mutant.key) in did_not_load
           for name, verdict in mutant.verdicts.items()):
        return NON_VIABLE_STATUS
    if UNTESTED_STATUS in kinds:
        return UNTESTED_STATUS
    if TIMEOUT_STATUS in kinds:
        return TIMEOUT_STATUS
    if NON_VIABLE_STATUS in kinds:
        return NON_VIABLE_STATUS
    return SURVIVED_STATUS


class MutationCleanupError(LangAdapterError):
    """Cleanup failed. ``cleanup_handle.close()`` retries it, and the handle
    keeps its native resources. Never treat it as a successful close or a
    scored observation. An interrupt or exit propagates as itself instead,
    carrying the unclosed session as ``cleanup_handle``."""

    def __init__(self, cleanup_handle: "MutationSession", reason: str) -> None:
        self.cleanup_handle = cleanup_handle
        self.reason = reason
        super().__init__(f"mutation session cleanup failed: {reason}")


def _interrupted(error: BaseException) -> bool:
    """An interrupt or exit, which no cleanup error may replace."""
    return not isinstance(error, Exception)


def _carry_cleanup(interrupt: BaseException, session: "MutationSession",
                   cleanup: Optional[BaseException] = None) -> None:
    """Let ``interrupt`` name the session whose cleanup it leaves unfinished."""
    try:
        interrupt.cleanup_handle = session
        if cleanup is not None and hasattr(interrupt, "add_note"):
            interrupt.add_note(str(cleanup))
    except BaseException:
        pass


class MutationSession(ABC):
    """A mutation session: one engine measuring one request after another.

    An engine constructs it once its opening completed or a failed opening
    stopped what it started. An opening that fails after the construction,
    such as a worker fleet that does not start, makes each run an
    infrastructure failure with its reason once ``close`` stopped what it
    started. It owns its
    resources in ``measure`` and ``release`` and keeps cleanup handles until
    ``release`` succeeds. A
    concurrent operation is refused, not queued. After an exception or a
    cancel the session closes what it owns. If that fails, the error raised
    carries it as ``cleanup_handle``. Results stay valid after later runs
    and ``close``."""

    def __init__(self, *, workers_requested: int = 1, workers_started: int = 0,
                 outcome: Outcome = Outcome.COMPLETED, reason: str = "") -> None:
        if type(workers_requested) is not int or workers_requested < 1:
            raise InvalidRequest("workers_requested", "must be a positive integer")
        if type(workers_started) is not int or not 0 <= workers_started <= workers_requested:
            raise InvalidRequest("workers_started", "must be between zero and workers_requested")
        if not isinstance(outcome, Outcome):
            raise InvalidRequest("outcome", "must be Outcome")
        if not isinstance(reason, str):
            raise InvalidRequest("reason", "must be text")
        self._workers_requested = workers_requested
        self._workers_started = workers_started
        self._outcome = outcome
        self._reason = reason
        self._operation = Lock()
        self._closed = False
        self._cleanup_failure: Optional[str] = None
        self._cancelled = False
        self._cancellation: Optional[_process_tree.Cancellation] = None
        _execution_scope.track(self, "mutation session")

    def _opening_failed(self, reason: str, workers_started: int = 0) -> None:
        """Record why the opening failed once ``close`` released what it
        started. Each run is an infrastructure failure with ``reason``, as for
        a session constructed with them, and the scope no longer counts the
        session, which holds nothing."""
        self._outcome = Outcome.INFRASTRUCTURE_FAILURE
        self._reason = reason
        self._workers_started = workers_started
        # A run reports the failed opening, which the closed check would hide.
        self._closed = False

    @property
    def outcome(self) -> Outcome:
        """``completed`` for a session that opened, else the outcome each of
        its runs reports, such as ``missing_setup``."""
        return self._outcome

    @property
    def reason(self) -> str:
        """Why the session did not open, empty for one that opened."""
        return self._reason

    @property
    def workers_requested(self) -> int:
        """The number of workers the session was asked for."""
        return self._workers_requested

    @property
    def workers_started(self) -> int:
        """The number of workers it started, from 0 to
        ``workers_requested``."""
        return self._workers_started

    @property
    def is_open(self) -> bool:
        """Whether the session measures runs: it is not closed, and its
        outcome is ``completed``."""
        return not self._closed and self.outcome is Outcome.COMPLETED

    def __enter__(self) -> "MutationSession":
        if self._closed:
            raise InvalidRequest("session", "is closed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.close()
        except MutationCleanupError as cleanup:
            if exc is None:
                raise
            if not _interrupted(exc):
                raise cleanup from exc
            # The interrupt that ends the block propagates, with the failure.
            _carry_cleanup(exc, self, cleanup)

    def run(self, request: MutationRequest) -> MutationRun:
        """Measure the request and record its attempted mechanism."""
        scope = getattr(self, "_langadapter_execution_scope", None)
        with scope.operation() if scope is not None else contextlib.nullcontext():
            return self._observe_owned(request)

    def _observe_owned(self, request):
        if not isinstance(request, MutationRequest):
            raise InvalidRequest("request", "must be MutationRequest")
        if not self._operation.acquire(blocking=False):
            raise InvalidRequest("session", "another operation is active")
        try:
            return self._cancellable(lambda: self._observe_locked(request))
        finally:
            self._operation.release()

    def cancel(self) -> None:
        """Stop the measurement in flight, from any thread or a signal
        handler: its processes are asked to end, and the operation raises
        ``MeasurementCancelled`` after its cleanup has closed the session,
        which then runs nothing more. Harmless when repeated or idle."""
        self._cancelled = True
        cancellation = self._cancellation
        if cancellation is not None:
            cancellation.cancel()

    def _cancellable(self, work):
        """``work()``, whose processes ``cancel()`` stops. Once cancelled it raises
        ``MeasurementCancelled`` after closing, any cleanup error riding on it."""
        if self._cancelled:
            # Cancelled while idle: the session closes before the operation ends.
            stop = MeasurementCancelled("the mutation session was cancelled")
            self._close_after(stop)
            raise stop
        cancellation = _process_tree.Cancellation()
        self._cancellation = cancellation
        if self._cancelled:
            cancellation.cancel()
        try:
            with _process_tree.cancellable(cancellation):
                result = work()
        except BaseException as error:
            if not cancellation.cancelled:
                raise
            stop = (error if isinstance(error, MeasurementCancelled)
                    else MeasurementCancelled("the measurement was cancelled"))
            if isinstance(error, MutationCleanupError):
                _carry_cleanup(stop, self, error)
            else:
                self._close_after(stop)
            if stop is error:
                raise
            raise stop from error
        finally:
            self._cancellation = None
        if cancellation.cancelled:
            stop = MeasurementCancelled("the measurement was cancelled")
            self._close_after(stop)
            raise stop
        return result

    def per_suite(self, request: PerSuiteRequest) -> PerSuiteRun:
        """Each suite's verdict for every mutant of the target, as its
        standalone run gives it. Without a faster implementation each suite
        is measured alone, in request order, within this operation. A failed
        suite's run does not affect the others."""
        return self._per_suite(request, fast=True)

    def _per_suite(self, request: PerSuiteRequest, *, fast: bool) -> PerSuiteRun:
        """``per_suite``. With ``fast`` false always the standalone runs."""
        if not isinstance(request, PerSuiteRequest):
            raise InvalidRequest("request", "must be PerSuiteRequest")
        request.validate()
        scope = getattr(self, "_langadapter_execution_scope", None)
        with scope.operation() if scope is not None else contextlib.nullcontext():
            if not self._operation.acquire(blocking=False):
                raise InvalidRequest("session", "another operation is active")
            try:
                return self._cancellable(lambda: self._per_suite_locked(request, fast=fast))
            finally:
                self._operation.release()

    def _per_suite_locked(self, request: PerSuiteRequest, *, fast: bool) -> PerSuiteRun:
        """``_per_suite`` while this caller holds the operation."""
        if (not self._closed and self._cleanup_failure is None
                and self.outcome is Outcome.COMPLETED):
            # Every suite is checked before the first one runs.
            for name in request.names:
                self._validate_request(request.standalone(name))
            faster = self._fast_runs(request) if fast else None
            if faster is not None:
                runs, implementation = faster
                return _assemble(request, {
                    name: self._resolved(runs[name]) for name in request.names},
                    implementation)
        runs = {name: self._observe_locked(request.standalone(name)) for name in request.names}
        return _assemble(request, runs, DEFAULT_IMPLEMENTATION)

    def _fast_runs(self, request: PerSuiteRequest):
        """The faster implementation's checked runs, or None. A failure
        closes the session."""
        try:
            fast = self.per_suite_fast(request)
            if fast is None:
                return None
            runs, implementation = fast
            if not isinstance(implementation, str) or not implementation:
                raise TypeError("a per-suite implementation must name itself")
            checked = {}
            for name in request.names:
                run = runs[name]
                if not isinstance(run, MutationRun) or run.derivation is not None:
                    raise TypeError("a per-suite implementation must return a measured "
                                    "MutationRun for every suite")
                checked[name] = copy.deepcopy(run)
            return checked, implementation
        except InvalidRequest:
            raise
        except BaseException as original:
            self._close_after(original)
            raise

    def per_suite_fast(self, request: PerSuiteRequest):
        """A faster per-suite measurement as ``(runs by suite name,
        implementation name)``, equal to the standalone runs, or None."""
        return None

    def _observe_locked(self, request):
        """One measurement while this caller holds the operation."""
        if self._closed:
            result = self._failed_result(request, Outcome.INFRASTRUCTURE_FAILURE,
                                         "the session is closed")
        elif self._cleanup_failure is not None:
            result = self._failed_result(
                request, Outcome.INFRASTRUCTURE_FAILURE,
                f"cleanup must be retried before further operations: {self._cleanup_failure}")
        elif self.outcome is not Outcome.COMPLETED:
            request.validate()
            result = self._failed_result(request, self.outcome, self.reason)
        else:
            started = False
            try:
                self._validate_request(request)
                started = True
                result = self.measure(request)
                if not isinstance(result, MutationRun):
                    raise TypeError("a native mutation run must return MutationRun")
                if result.derivation is not None:
                    raise TypeError("a native mutation run cannot return an assumed subset")
                result = copy.deepcopy(result)
            except BaseException as original:
                if isinstance(original, InvalidRequest) and not started:
                    raise
                self._close_after(original)
                raise
        return self._resolved(result)

    @staticmethod
    def _resolved(result: MutationRun) -> MutationRun:
        return replace(result, resolution=MutationResolution(
            requested="measure", method="measured", reason="native measurement requested"))

    def close(self) -> None:
        """Close only when no operation is active, and retain handles on failure."""
        if self._closed:
            return
        scope = getattr(self, "_langadapter_execution_scope", None)
        with scope.operation(recovery=True) if scope is not None else contextlib.nullcontext():
            self._close_exclusive()

    def register_owned_commands(self, resources) -> None:
        """Declare acquired persistent commands owned and closed by this session."""
        scope = getattr(self, "_langadapter_execution_scope", None)
        if scope is not None:
            scope.register_persistent(self, tuple(resources))

    def _close_exclusive(self):
        if not self._operation.acquire(blocking=False):
            raise InvalidRequest("session", "cannot close while another operation is active")
        try:
            self._close_owned()
        finally:
            self._operation.release()

    def _close_after(self, primary: BaseException) -> None:
        """Close after ``primary`` ended an operation: a cleanup failure
        replaces an ordinary error, chained. An interrupt stays and carries it."""
        try:
            self._close_owned()
        except MutationCleanupError as cleanup:
            if not _interrupted(primary):
                raise cleanup from primary
            _carry_cleanup(primary, self, cleanup)

    def _close_owned(self) -> None:
        if self._closed:
            return
        try:
            # No cancel, of the session or its caller's scope, stops cleanup.
            with _process_tree.cleanup():
                self.release()
        except Exception as exc:
            self._cleanup_failure = f"{type(exc).__name__}: {exc}"
            raise MutationCleanupError(self, self._cleanup_failure) from exc
        except BaseException as exc:
            # Cleanup an interrupt stopped is unfinished: this handle keeps
            # what it holds, refuses further runs, and close() retries.
            self._cleanup_failure = f"cleanup was interrupted ({type(exc).__name__})"
            _carry_cleanup(exc, self)
            raise
        self._closed = True
        self._cleanup_failure = None
        _execution_scope.completed(self)

    @abstractmethod
    def measure(self, request: MutationRequest) -> MutationRun:
        """The engine's measurement of ``request``: the hook that ``run`` and
        ``per_suite`` call once they validated the request, took the session's
        lock and found it open. A caller uses ``run``, since ``measure`` alone
        does none of that and records no resolution. A returned result means
        this operation is quiescent."""

    @abstractmethod
    def release(self) -> None:
        """The engine's release of what the session holds: the hook that
        ``close`` calls once no operation is active. It keeps every handle when
        cleanup fails. A caller uses ``close``."""

    def _validate_request(self, request: MutationRequest) -> None:
        """Validate before changing engine state. InvalidRequest leaves it reusable."""
        request.validate()

    def _failed_result(self, request: MutationRequest, outcome: Outcome,
                       reason: str) -> MutationRun:
        """Record supplied facts only, never probe or stage in this hook."""
        return MutationRun.failed(request.target, outcome, reason, failure=FAILED_SESSION)
