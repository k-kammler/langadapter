"""Source operations on test files and what they report, pooled staging,
source metrics, and the text helpers the languages share.

Every edit of ``adapter.sources`` (``langadapter.capabilities.SOURCE_EDITS``)
returns a ``SourceResult`` whose status (``APPLIED`` and the constants after
it) says whether it applied and, if not, why. Its parser is empty when no
parser ran. ``adapter.sources.normalize_bindings`` removes shadowed
definitions without running the project, returns
``SourceObservation[SuiteNormalization]`` and keeps the original bytes of a
source that does not parse. The project's runtime may still reject a
normalized source.

A pool (``StagedPool``) stages the test files of several suites as one suite
under names no other suite uses, and maps each staged file, test and killer
back to its suite's slot and standalone identity. ``MetricRegistry`` merges
the values of registered metric providers per scope. ``STANDARD_METRICS``
names the metrics any language may report, each with the operation of
``adapter.sources`` that reports it. The merge repairs share
one naming policy: the first source keeps a name, a later differing
declaration takes ``fresh_name``, a binding that cannot be renamed is a
conflict the caller's ``Choose`` decides, and every rewrite goes through
``apply_edits``. ``rows`` and ``lines`` split lines where tree-sitter and the
language parsers do, unlike ``str.splitlines``. ``whole`` and ``bounded``
bound a tool's output for a result.
"""
from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field, replace
from typing import (
    Dict,
    Generic,
    Optional,
    Tuple,
    TypeVar,
    Any,
    Iterable,
    List,
    Mapping,
    Sequence,
    Set,
    Protocol,
    Callable,
    Collection,
    AnyStr,
)
from langadapter.errors import InvalidRequest
from langadapter._lizard import code_lines_and_complexities as code_lines_and_complexities
import hashlib
import re
from pathlib import Path, PurePosixPath
from langadapter.suites import Suite, TestId
from langadapter.outcomes import _relative_posix_path
import math
import subprocess
import threading
from types import MappingProxyType


_T = TypeVar("_T")


class SourceStatus(str, enum.Enum):
    """A source observation's completion or reason for unavailability."""

    COMPLETED = "completed"
    PARSE_ERROR = "parse_error"
    UNSUPPORTED = "unsupported"
    MISSING_SETUP = "missing_setup"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    TIMEOUT = "timeout"
    REFUSED = "refused"


@dataclass(frozen=True)
class SourceObservation(Generic[_T]):
    """A completed observation with its value (False and empty count), or an
    unavailable one without. A validator that failed to run gives no
    verdict, unlike one that judged the source invalid."""

    status: SourceStatus
    value: Optional[_T] = None
    parser: Optional[str] = None
    reason: str = ""

    def __post_init__(self) -> None:
        try:
            status = SourceStatus(self.status)
        except (TypeError, ValueError):
            raise InvalidRequest("status", f"unknown source status {self.status!r}") from None
        object.__setattr__(self, "status", status)
        if (status is SourceStatus.COMPLETED) != (self.value is not None):
            raise InvalidRequest("value", "only completed source observations carry a value")

    @property
    def completed(self) -> bool:
        """Whether ``status`` is ``SourceStatus.COMPLETED``."""
        return self.status is SourceStatus.COMPLETED


@dataclass(frozen=True)
class RemovedBinding:
    """A shadowed definition removed in favor of the binding kept. Lines
    are 1-based and inclusive, and ``scope`` names the enclosing classes.
    ``removed_start_line`` and ``removed_end_line`` are the first and last
    line of the removed definition, ``retained_start_line`` and
    ``retained_end_line`` those of the binding kept."""

    scope: Tuple[str, ...]
    name: str
    kind: str
    removed_start_line: int
    removed_end_line: int
    retained_start_line: int
    retained_end_line: int


@dataclass(frozen=True)
class SuiteNormalization:
    """A static normalization with every removal. A source that does not
    parse is returned whole, never partly rewritten. ``before_sha256`` and
    ``after_sha256`` are the SHA-256 of the input and of the result.
    ``parse_ok`` says whether the source parses, ``removed_bindings`` holds
    every removal, and ``rule_version`` names the normalization rule and its
    version, such as ``python-last-binding-v2``."""

    source: str
    changed: bool
    parse_ok: bool
    parse_error: Optional[str]
    removed_bindings: Tuple[RemovedBinding, ...]
    before_sha256: str
    after_sha256: str
    rule_version: str


@dataclass(frozen=True)
class CollisionResolution:
    """Static collision rewrites of the input sources. ``sources``,
    ``renames``, ``deduped`` and ``dropped_defs`` hold one entry for each
    input source, in its order, and so does ``localized_autouse`` in the
    answer of ``adapter.sources.resolve_collisions``. Elsewhere it is empty
    when nothing was localized. ``ejected`` names each rejected source's
    input index once, with its reason at the same place in
    ``eject_reasons``. ``dropped_defs`` names the dropped bindings, not the
    dependents removed with them. A conflict without a caller preference is
    reported, never decided."""

    sources: Tuple[str, ...]
    ejected: Tuple[int, ...] = ()
    eject_reasons: Tuple[str, ...] = ()
    renames: Tuple[Dict[str, str], ...] = ()
    deduped: Tuple[Tuple[str, ...], ...] = ()
    dropped_defs: Tuple[Tuple[str, ...], ...] = ()
    localized_autouse: Tuple[Tuple[str, ...], ...] = ()
    choices: Tuple[Tuple[str, int, Tuple[int, ...]], ...] = ()
    conflicts: Tuple[Tuple[str, Tuple[int, ...]], ...] = ()

#: The operation changed the source.
APPLIED = "applied"
#: The operation ran and found nothing to change. The source is the input.
UNCHANGED = "unchanged"
#: The source does not parse, so the operation did not run.
PARSE_ERROR = "parse_error"
#: The project interpreter rejects the source. No rewrite uses another grammar.
REFUSED = "refused"
#: The language does not provide the operation.
UNSUPPORTED = "unsupported"
#: A tool the operation needs could not run.
INFRASTRUCTURE_FAILURE = "infrastructure_failure"
#: The implementation needs a runtime or parser that is not available.
MISSING_SETUP = "missing_setup"
#: An execution-backed operation exceeded its limit without a source verdict.
TIMEOUT = "timeout"
#: A repair ran and the problem remains. The source is the input.
NOT_REPAIRED = "not_repaired"
#: Every status a source operation reports.
SOURCE_STATUSES = (APPLIED, UNCHANGED, PARSE_ERROR, REFUSED, UNSUPPORTED,
                   INFRASTRUCTURE_FAILURE, NOT_REPAIRED, MISSING_SETUP, TIMEOUT)


@dataclass(frozen=True)
class TargetIdentity:
    """The target source a generated-test validity policy checks against.
    Its text, which the policy compares with literals in the tests, is left
    out of equality, ``repr`` and ``record()``.

    - ``source_path``: the target's path, absolute or relative to
      ``project_root`` as the language gives it.
    - ``project_root``: the project's root.
    - ``module_name``: the target's module: its importable name for Python,
      ``package.Class`` for Java, and its path without ``.c`` for C.
    - ``policy_version``: the version of the validity policy.
    - ``target_source_sha256``: the SHA-256 of the target's text.
    - ``target_source``: the target's text.
    """

    source_path: str
    project_root: str
    module_name: str
    language: str
    policy_version: str
    target_source_sha256: str
    target_source: str = field(repr=False, compare=False, default="")

    def record(self) -> Dict[str, str]:
        """The identity as plain data, without ``target_source``."""
        return {
            "source_path": self.source_path,
            "project_root": self.project_root,
            "module_name": self.module_name,
            "language": self.language,
            "policy_version": self.policy_version,
            "target_source_sha256": self.target_source_sha256,
        }


@dataclass(frozen=True)
class SuiteValidityFinding:
    """One violation of the generated-test validity policy. ``rule_id``
    names the rule, and ``policy_version`` the version of the policy.
    ``start_line``, ``start_col``, ``end_line`` and ``end_col`` give its
    span in the test source, lines counted from 1 and columns as the
    language's parser counts them: from 0 for the Python adapter's tests, as
    Python's ``ast`` counts them, and from 1 for Java's and C's tests,
    Python tests of a C extension included. ``enclosing_test`` names the
    test that holds it, None outside any test."""

    rule_id: str
    policy_version: str
    operation: str
    diagnostic: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
    enclosing_test: Optional[str]


@dataclass(frozen=True)
class SuiteAuditFlag:
    """A review-only source-access signal, not automatic rejection.
    ``rule_id`` names the rule. ``start_line``, ``start_col``, ``end_line``
    and ``end_col`` give its span in the test source, lines counted from 1
    and columns as the language's parser counts them: from 0 for the Python
    adapter's tests, as Python's ``ast`` counts them, except an
    ``unparseable_source`` flag's, which count from 1 as a ``SyntaxError``
    does, and from 1 for Java's and C's tests."""

    rule_id: str
    diagnostic: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int


@dataclass(frozen=True)
class SourceResult:
    """The result of a source operation: its status, the resulting source
    (the input when it did not apply), the parser, the requested names that
    matched nothing, the items removed, a detail such as the parse error,
    and for an applied syntax repair (``salvage_syntax``) how many repairs
    it made."""

    status: str
    source: str
    parser: str
    unmatched: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    detail: str = ""
    repairs: int = 0

    @property
    def applied(self) -> bool:
        """Whether ``status`` is ``APPLIED``."""
        return self.status == APPLIED


@dataclass(frozen=True)
class StructureCheck:
    """Whether a source parses, with the first error's position counted from
    1 (0 when it parses). ``message`` names the error and its position
    ("syntax error at line 3:5"). ``snippet`` is the whole source text the
    error covers, stripped, possibly several lines, for the caller to cut.
    Both are empty when it parses. ``snippet`` is None for a language that
    gives none, whose ``message`` is then its whole text. ``ok`` says
    whether the source parses."""

    ok: bool
    message: str
    line: int
    column: int
    parser: str
    snippet: Optional[str] = None


@dataclass(frozen=True)
class TestUnit:
    """One test of a test source from a language's ``test_units`` hook: its
    qualified name (``Class.test`` or ``test``) and the span removing it cuts,
    ``source[start:end]``. Overloads are separate units of one name."""

    __test__ = False  # not a test class for pytest's collection

    name: str
    start: int
    end: int


@dataclass(frozen=True)
class TestMetrics:
    """Counts of a test source, absent when it does not parse: code lines,
    tests, assertions, fixtures and parametrized cases. ``code_lines``
    counts lizard's lines of code (blank lines, comments and docstrings left
    out), and ``parametrized_cases`` the cases the parametrized tests run."""

    __test__ = False  # not a test class for pytest's collection

    parsed: bool
    parser: str
    code_lines: Optional[int] = None
    tests: Optional[int] = None
    assertions: Optional[int] = None
    fixtures: Optional[int] = None
    parametrized_cases: Optional[int] = None
    detail: str = ""


@dataclass(frozen=True)
class SourceValidation:
    """Whether the project interpreter accepts a source: ``valid``,
    ``syntax_error`` (with position and message), or ``timeout`` or
    ``infrastructure_failure``, which are no verdict on the source.
    ``interpreter`` identifies the interpreter, as a diagnostic.
    ``source_sha256`` is the SHA-256 of the checked source."""

    status: str
    message: str
    line: int
    column: int
    interpreter: str
    source_sha256: str = ""

    @property
    def valid(self) -> bool:
        """Whether ``status`` is ``valid``."""
        return self.status == "valid"


def _collision_answer(parser: str, failures: Sequence[Tuple[int, str]],
                      rejected: Sequence[Tuple[int, str]],
                      renamed: Sequence[Tuple[Tuple[str, str], ...]],
                      **aligned: Any) -> "SourceObservation[CollisionResolution]":
    """A built-in engine's answer to ``resolve_collisions``. When the
    validation of some source could not run (``failures``: its index and
    detail), the answer is an infrastructure failure, the one status the
    engines record, whose reason names each such source. Otherwise it
    completes with ``parser`` and the ``CollisionResolution``: ``rejected``
    holds (index, reason) for each source left out, ``renamed`` each
    source's (old, new) pairs, and ``aligned`` the other fields."""
    if failures:
        return SourceObservation(
            SourceStatus.INFRASTRUCTURE_FAILURE, parser=parser,
            reason="; ".join(f"source {index}: {detail}" for index, detail in failures))
    reasons = dict(sorted(rejected))
    return SourceObservation(SourceStatus.COMPLETED, parser=parser, value=CollisionResolution(
        ejected=tuple(reasons), eject_reasons=tuple(reasons.values()),
        renames=tuple(dict(pairs) for pairs in renamed), **aligned))


@dataclass(frozen=True)
class PolicyReport:
    """The generated-test validity policy's reading of a source for a
    target: ``completed`` with its items, possibly none, or ``parse_error``
    with the parser's message and no items. The items are its findings
    (``analyze_validity``) or review-only signals (``screen_source``).
    ``policy_version`` is the version of the policy. ``error_line`` and
    ``error_column`` give the parse error's position, 0 when the source
    parses."""

    status: str
    items: Tuple[object, ...]
    policy_version: str
    parser: str
    detail: str = ""
    error_line: int = 0
    error_column: int = 0


#: A slot name: it becomes part of file and type names in every language.
_SLOT = re.compile(r"[A-Za-z0-9_]+\Z")


def _relative(path: str, field: str) -> str:
    if not _relative_posix_path(path) or ".." in PurePosixPath(path).parts:
        raise InvalidRequest(field, f"must be a relative POSIX path inside the suite: {path!r}")
    return PurePosixPath(path).as_posix()


@dataclass(frozen=True)
class PoolMember:
    """One test file of a suite to pool. ``slot`` names the suite (letters,
    digits and underscores), ``name`` is the file's path in the suite's
    standalone run, relative to its root, and ``source`` is its text."""

    slot: str
    name: str
    source: str

    def validate(self) -> None:
        """Refuse a slot that is not letters, digits and underscores, a name
        that is no relative POSIX path inside the suite, or a source that is
        no text, with ``InvalidRequest``."""
        if not isinstance(self.slot, str) or not _SLOT.match(self.slot):
            raise InvalidRequest("members.slot",
                                 f"must be letters, digits and underscores: {self.slot!r}")
        _relative(self.name, "members.name")
        if not isinstance(self.source, str):
            raise InvalidRequest("members.source", "must be text")


@dataclass(frozen=True)
class StagedFile:
    """One pooled test file: its suite's ``slot``, its ``original`` path in
    the suite's standalone run, its ``path`` in the pool, the text that runs
    there and the top-level types renamed in it as (original, staged) pairs
    (none for Python). ``self_references`` lists where the file refers to its
    own module or class name in a way the staged name changes at run time (a
    name in a string or read at run time, an import of its own class): its
    verdicts in the pool may then differ from its standalone run."""

    slot: str
    original: str
    path: str
    source: str
    renamed: Tuple[Tuple[str, str], ...] = ()
    self_references: Tuple[str, ...] = ()

    @property
    def sha256(self) -> str:
        """The SHA-256 of the staged text, UTF-8 encoded."""
        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StagedPool:
    """The pooled suite's test files, in member order, and the setup files
    every suite shares, as (relative path, text) pairs staged unchanged."""

    files: Tuple[StagedFile, ...]
    setup: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "setup", tuple(self.setup))

    @property
    def slots(self) -> Tuple[str, ...]:
        """The slots, in member order."""
        return tuple(dict.fromkeys(f.slot for f in self.files))

    def staged(self, slot: str) -> Tuple[StagedFile, ...]:
        """The files of ``slot``."""
        files = tuple(f for f in self.files if f.slot == slot)
        if not files:
            raise InvalidRequest("slot", f"no suite is staged as {slot!r}")
        return files

    def write(self, root: str) -> Suite:
        """Write the pooled files as given, line endings included, below the
        existing directory ``root`` and return the pooled suite."""
        base = Path(root)
        if not base.is_dir():
            raise InvalidRequest("root", f"no directory at {root}")
        for path, text in [(f.path, f.source) for f in self.files] + list(self.setup):
            target = base / PurePosixPath(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        return Suite(str(base), tuple(f.path for f in self.files),
                     tuple(path for path, _ in self.setup))

    def slot_of(self, path: str) -> Optional[str]:
        """The slot of the pooled file ``path`` (relative POSIX), or None."""
        return next((f.slot for f in self.files if f.path == path), None)

    def original_test(self, test: TestId) -> Tuple[str, TestId]:
        """The slot of the pooled run's ``test`` and the test as the slot's
        standalone run identifies it."""
        slot = self.slot_of(test.file)
        if slot is None:
            raise InvalidRequest("test", f"{test} is none of the pooled files")
        staged = next(f for f in self.files if f.path == test.file)
        return slot, TestId(staged.original, self._names(slot, test.qualname),
                            test.params, self.original_text(test.text, slot))

    def slot_of_killer(self, killer: str) -> Optional[str]:
        """The slot whose file or type a killer as the engine printed it
        names, or None when it names none or more than one."""
        found = {f.slot for f in self.files
                 if _mentions(killer, PurePosixPath(f.path).name, path=True)
                 or any(_mentions(killer, staged) for _, staged in f.renamed)}
        return found.pop() if len(found) == 1 else None

    def original_text(self, text: str, slot: str) -> str:
        """``text`` (a test id or a killer as the engine printed it) with the
        pooled names of ``slot`` written as its standalone run names them."""
        for staged in self.staged(slot):
            text = _replace(text, staged.path, staged.original, path=True)
            text = _replace(text, PurePosixPath(staged.path).name,
                            PurePosixPath(staged.original).name, path=True)
            for original, renamed in staged.renamed:
                text = _replace(text, renamed, original)
        return text

    def killed_by(self, kill_matrix: Mapping[str, Any], slot: str) -> Set[Any]:
        """The ids of the mutants a named killer of ``slot`` killed in the
        pooled run's saved ``kill_matrix``."""
        self.staged(slot)
        return {row.get("id") for row in kill_matrix.get("mutants") or ()
                if any(self.slot_of_killer(str(killer)) == slot for killer in _killers(row))}

    def derive(self, kill_matrix: Mapping[str, Any], slot: str, *,
               covered_lines: Iterable[int]) -> Dict[str, Any]:
        """The kill matrix of ``slot`` derived from the pooled run's saved
        ``kill_matrix`` by ``langadapter.mutation.restrict_kill_matrix``,
        keeping the mutants the slot's tests killed (``killed_by``) and
        ``covered_lines``, the target lines they cover. A row that stays
        killed names only the slot's killers and killing tests, as its
        standalone run names them. The result is marked derived, with
        ``pool_slot``. It equals the standalone run only when the slot's
        tests behave in the pool as alone and the pooled run recorded every
        killing test (attribution ``all``)."""
        from langadapter.mutation import DETECTED_STATUSES, restrict_kill_matrix

        derived = restrict_kill_matrix(kill_matrix, kept_killed_ids=self.killed_by(kill_matrix, slot),
                                       kept_lines=set(covered_lines))
        for row in derived["mutants"]:
            own = [self.original_text(str(killer), slot) for killer in _killers(row)
                   if self.slot_of_killer(str(killer)) == slot]
            tests = [asdict(self.original_test(TestId(**test))[1])
                     for test in row.pop("killing_tests", ()) if self.slot_of(test["file"]) == slot]
            row.pop("unmatched_killers", None)
            if row.get("status") in DETECTED_STATUSES:
                row["killers"] = own
                row["killer"] = own[0] if own else None
                if own:
                    row["killing_tests"] = tests
        derived["derived"]["pool_slot"] = slot
        return derived

    def _names(self, slot: str, qualname: str) -> str:
        renames = {staged: original for f in self.staged(slot) for original, staged in f.renamed}
        return ".".join(renames.get(part, part) for part in qualname.split("."))


def _killers(row: Mapping[str, Any]) -> Tuple[Any, ...]:
    return tuple(row.get("killers") or ([row["killer"]] if row.get("killer") else ()))


def _bounded(name: str, *, path: bool) -> "re.Pattern[str]":
    # The name alone, not inside a longer identifier (for a file name, path
    # component). A type name may follow its package's dot.
    before = r"(?<![\w.-])" if path else r"(?<!\w)"
    return re.compile(before + re.escape(name) + r"(?!\w)")


def _mentions(text: str, name: str, *, path: bool = False) -> bool:
    return bool(_bounded(name, path=path).search(text))


def _replace(text: str, old: str, new: str, *, path: bool = False) -> str:
    if old == new:
        return text
    return _bounded(old, path=path).sub(lambda _: new, text)


def check_members(members: Sequence[PoolMember]) -> Tuple[PoolMember, ...]:
    """Validate the members of a pool: at least one, each valid, no file
    named twice within one slot."""
    if isinstance(members, (str, bytes)) or not isinstance(members, Iterable):
        raise InvalidRequest("members", "must be a sequence of PoolMember values")
    members = tuple(members)
    if not members:
        raise InvalidRequest("members", "must name at least one suite file")
    seen: Dict[Tuple[str, str], int] = {}
    for member in members:
        if not isinstance(member, PoolMember):
            raise InvalidRequest("members", "must hold PoolMember values")
        member.validate()
        key = (member.slot, PurePosixPath(member.name).as_posix())
        if key in seen:
            raise InvalidRequest("members", f"slot {member.slot} names {member.name} twice")
        seen[key] = 1
    return members


def check_setup(setup: Sequence[Tuple[str, str]]) -> Tuple[Tuple[str, str], ...]:
    """Validate the shared setup files: (relative path, text) pairs."""
    if isinstance(setup, (str, bytes)) or not isinstance(setup, Iterable):
        raise InvalidRequest("setup", "must be a sequence of (path, text) pairs")
    pairs: List[Tuple[str, str]] = []
    for item in setup:
        if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[1], str):
            raise InvalidRequest("setup", "must be a sequence of (path, text) pairs")
        pairs.append((_relative(item[0], "setup"), item[1]))
    paths = [path for path, _ in pairs]
    repeated = sorted({path for path in paths if paths.count(path) > 1})
    if repeated:
        raise InvalidRequest("setup", f"names a file twice: {', '.join(repeated)}")
    return tuple(pairs)


def unique_paths(pool: StagedPool, *, by_name: bool) -> Optional[str]:
    """None when the pool's files and setup files have distinct paths (and,
    with ``by_name``, distinct file names), else the reason."""
    paths = [f.path for f in pool.files] + [path for path, _ in pool.setup]
    keys = [PurePosixPath(p).name if by_name else p for p in paths]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        return f"the pooled files would share the name(s) {', '.join(repeated)}"
    return None


def _text(value, field, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise InvalidRequest(field, "must be non-empty text without surrounding whitespace")


def _items(value, field):
    try:
        return tuple(value)
    except TypeError:
        raise InvalidRequest(field, "must be an iterable of values") from None


@dataclass(frozen=True)
class MetricDefinition:
    """What one metric measures: its ``name``, its ``unit``, the
    ``definition_version`` that changes whenever its meaning does, and the
    ``scopes`` it is reported for, ``file``, ``function`` or both."""

    name: str
    unit: str
    definition_version: str
    scopes: Tuple[str, ...]

    def __post_init__(self):
        for field in ("name", "unit", "definition_version"):  # noqa: F402
            _text(getattr(self, field), field)
        scopes = _items(self.scopes, "scopes")
        if not scopes or any(not isinstance(scope, str) or scope not in {"file", "function"} for scope in scopes):
            raise InvalidRequest("scopes", "must contain file or function")
        if len(set(scopes)) != len(scopes):
            raise InvalidRequest("scopes", "must not contain duplicates")
        object.__setattr__(self, "scopes", tuple(sorted(scopes)))


@dataclass(frozen=True)
class StandardMetric:
    """A metric any language may report under one ``name``: its ``meaning``,
    its ``unit``, the ``scopes`` a provider may report it for, and the
    ``operation`` of ``adapter.sources`` that reports it, ``metrics`` for a
    metric of a file's source text and ``call_statistics`` for a metric of
    the project. A provider's definition of the name keeps the unit and
    reports at most these scopes."""

    name: str
    meaning: str
    unit: str
    scopes: Tuple[str, ...]
    operation: str


def _standard(name, meaning, *, unit="count", scopes=("file", "function"), operation="metrics"):
    return StandardMetric(name, meaning, unit, scopes, operation)


#: Each standard metric's name mapped to its ``StandardMetric``. A consumer
#: may request any of them from any language and sends each to the
#: operation it names. A language that does not measure one reports it
#: unavailable, never as a number. A function's values cover its whole text,
#: nested functions included, except ``cyclomatic_complexity``.
STANDARD_METRICS: Mapping[str, StandardMetric] = MappingProxyType({metric.name: metric for metric in (
    _standard("nloc", "the lines that hold code, not counting blank lines, comments or "
              "Python's docstrings", unit="lines"),
    _standard("function_count", "the functions and methods declared"),
    _standard("class_count", "the classes declared"),
    _standard("loop_count", "the loops"),
    _standard("loop_max_nesting", "the deepest nesting of loops"),
    _standard("cyclomatic_sum", "the sum of the McCabe complexity of each function"),
    _standard("recursive_function_count", "the functions that call their own name"),
    _standard("io_call_count", "the calls to the language's file, socket and process functions"),
    _standard("cyclomatic_complexity", "a function's McCabe complexity, its nested function "
              "bodies left out", scopes=("function",)),
    _standard("call_site_count_estimate", "the call sites of a function's bare name in the "
              "project's other source files, test files left out, matched by name without "
              "resolving it, and for the file their sum over its functions",
              operation="call_statistics"),
    _standard("calling_file_count_estimate", "the project's other source files that call a "
              "function's bare name, test files left out, and for the file those that call "
              "any of its functions, each counted once", operation="call_statistics"),
)})


def _not_measured(name: str, language: str) -> MetricValue:
    """A requested standard metric that no registered provider measures."""
    standard = STANDARD_METRICS[name]
    elsewhere = "" if standard.operation == "metrics" else f", {standard.operation} does"
    return MetricValue(MetricDefinition(name, standard.unit, "standard-metrics-1", standard.scopes),
                       "langadapter.standard", None, SourceStatus.UNSUPPORTED, None,
                       f"the {language} adapter's metrics operation does not report {name}{elsewhere}")


def _keep_standard(definitions: Sequence[MetricDefinition]) -> None:
    """Refuse a provider's definition of a standard metric whose operation,
    unit or scopes differ from ``STANDARD_METRICS``."""
    for item in definitions:
        standard = STANDARD_METRICS.get(item.name)
        if standard is not None and (standard.operation != "metrics" or item.unit != standard.unit
                                     or not set(item.scopes) <= set(standard.scopes)):
            raise InvalidRequest("definitions", (
                f"the standard metric {item.name} is reported by {standard.operation} in "
                f"{standard.unit} for at most the scopes {', '.join(standard.scopes)}"))


@dataclass(frozen=True)
class MetricValue:
    """One metric's observation of one scope by ``provider`` with ``parser``.
    A ``completed`` status carries a finite number as ``value``, any other
    none, and ``reason`` says why."""

    definition: MetricDefinition
    provider: str
    parser: Optional[str]
    status: SourceStatus
    value: int | float | None
    reason: str = ""

    def __post_init__(self):
        if not isinstance(self.definition, MetricDefinition):
            raise InvalidRequest("definition", "must be a MetricDefinition")
        _text(self.provider, "provider")
        _text(self.parser, "parser", optional=True)
        if not isinstance(self.reason, str):
            raise InvalidRequest("reason", "must be text")
        try:
            status = SourceStatus(self.status)
        except (ValueError, TypeError):
            raise InvalidRequest("status", "must be a source observation status") from None
        number = self.value
        if status == SourceStatus.COMPLETED:
            if (isinstance(number, bool) or not isinstance(number, (int, float))
                    or isinstance(number, float) and not math.isfinite(number)):
                raise InvalidRequest("value", "completed metrics require a finite number")
        elif number is not None:
            raise InvalidRequest("value", "unavailable metrics must not contain a number")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class MetricScope:
    """The metrics of one scope of a source. A ``file`` scope has no name,
    span or signature. A ``function`` scope has its name, first and last
    line (1-based, inclusive) and signature, if the language has one.
    ``metrics`` holds at most one value per metric defined for the scope.
    ``start_line`` and ``end_line`` are that first and last line, None for
    a ``file`` scope."""

    kind: str
    name: str
    start_line: Optional[int]
    end_line: Optional[int]
    signature: Optional[str]
    metrics: Tuple[MetricValue, ...]

    def __post_init__(self):
        if self.kind == "file":
            if self.name != "" or any(value is not None for value in (
                    self.start_line, self.end_line, self.signature)):
                raise InvalidRequest("scope", "file scope has no name, span or signature")
        elif self.kind == "function":
            _text(self.name, "name")
            _text(self.signature, "signature", optional=True)
            if (any(isinstance(line, bool) or not isinstance(line, int)
                    for line in (self.start_line, self.end_line))
                    or self.start_line < 1 or self.end_line < self.start_line):
                raise InvalidRequest("span", "function scope requires a positive ordered span")
        else:
            raise InvalidRequest("kind", "must be file or function")
        metrics = _items(self.metrics, "metrics")
        if any(not isinstance(metric, MetricValue) or self.kind not in metric.definition.scopes
               for metric in metrics):
            raise InvalidRequest("metrics", "must contain values defined for this scope")
        if len({metric.definition.name for metric in metrics}) != len(metrics):
            raise InvalidRequest("metrics", "must not contain duplicate names")
        object.__setattr__(self, "metrics", tuple(sorted(metrics, key=lambda metric: metric.definition.name)))

    @property
    def key(self):
        """The scope's identity: its kind, name, span and signature."""
        return (self.kind, self.name, self.start_line, self.end_line, self.signature)


@dataclass(frozen=True)
class SourceMetrics:
    """Every metric of one source: the ``file`` scope, the ``functions``
    scopes, and in ``unavailable`` the metrics no scope could report and
    why."""

    language: str
    file: MetricScope
    functions: Tuple[MetricScope, ...]
    unavailable: Tuple[MetricValue, ...] = ()

    def __post_init__(self):
        _text(self.language, "language")
        if not isinstance(self.file, MetricScope) or self.file.kind != "file":
            raise InvalidRequest("file", "must be a file metric scope")
        functions = _items(self.functions, "functions")
        if any(not isinstance(scope, MetricScope) or scope.kind != "function" for scope in functions):
            raise InvalidRequest("functions", "must contain function scopes")
        if len({scope.key for scope in functions}) != len(functions):
            raise InvalidRequest("functions", "must not contain duplicate scope identities")
        unavailable = _items(self.unavailable, "unavailable")
        if any(not isinstance(metric, MetricValue) or metric.status == SourceStatus.COMPLETED
               for metric in unavailable):
            raise InvalidRequest("unavailable", "must contain unavailable metric values")
        if len({metric.definition.name for metric in unavailable}) != len(unavailable):
            raise InvalidRequest("unavailable", "must not contain duplicate metric names")
        observed = {metric.definition.name for scope in (self.file, *functions) for metric in scope.metrics}
        if any(metric.definition.name in observed for metric in unavailable):
            raise InvalidRequest("unavailable", "unscoped failures conflict with scoped observations")
        object.__setattr__(self, "functions", tuple(sorted(functions, key=lambda scope: (
            scope.name, scope.start_line, scope.end_line, scope.signature or ""))))
        object.__setattr__(self, "unavailable", tuple(sorted(unavailable, key=lambda metric: metric.definition.name)))


class MetricProvider(Protocol):
    """A metric provider: a ``name``, its metric ``definitions()``, and
    ``measure(source, *, language, parser)``, which returns the scopes it
    measured as a source observation, unavailable (with no number) for
    source the provider cannot read."""

    @property
    def name(self) -> str:
        """The provider's name."""

    def definitions(self) -> Tuple[MetricDefinition, ...]:
        """The definitions of the metrics the provider measures."""

    def measure(self, source: str, *, language: str,
                parser: Optional[str]) -> SourceObservation[Tuple[MetricScope, ...]]:
        """The scopes the provider measured in ``source``, unavailable with
        no number when the provider cannot read it."""


class _StructuralProvider:
    """An adapter's eight file counters as a metric provider: structural
    hints, not coverage or mutation measurements. Code lines leave out
    blank lines and comments, complexity sums one plus the decisions of
    each function's own body, and IO calls follow each language's name
    vocabulary. An adapter that declares ``_lizard_metrics`` (Python's and
    Java's) counts code lines and complexity with lizard, whose version
    their definitions then name: code lines leave out Python's docstrings,
    and a declaration lizard reports no function for (a Java method without
    a body, a Python def whose body is on its signature's line) has no
    complexity of its own. Its other counters follow the language's
    tree-sitter grammar. An adapter with ``scoped_optimization_metrics`` also
    reports them per declaration, with its own-body complexity, unsupported
    where the adapter gives none."""

    name = "langadapter.structural"
    _names = ("cyclomatic_sum", "loop_count", "loop_max_nesting",
              "recursive_function_count", "function_count", "class_count",
              "io_call_count", "nloc")
    #: The counters lizard counts.
    _lizard_names = ("cyclomatic_sum", "nloc")

    def __init__(self, adapter):
        from langadapter import _lizard

        self.adapter = adapter
        self._scoped = getattr(adapter, "scoped_optimization_metrics", None)
        self._lizard = bool(getattr(adapter, "_lizard_metrics", False))
        lizard = f"lizard-{_lizard.version()}" if self._lizard else None

        def definition(name, version):
            standard = STANDARD_METRICS[name]
            return MetricDefinition(name, standard.unit, version,
                                    standard.scopes if callable(self._scoped) else ("file",))

        self._definitions = tuple(definition(
            name, f"{lizard}-1" if lizard and name in self._lizard_names
            else "tree-sitter-structural-1") for name in self._names)
        if callable(self._scoped):
            self._definitions += (definition(
                "cyclomatic_complexity",
                f"{lizard}-own-body-1" if lizard else "tree-sitter-own-body-1"),)

    def definitions(self):
        return self._definitions

    def measure(self, source, *, language, parser):
        actual = self.adapter.structural_parser_identity()
        if callable(self._scoped):
            return self._measure_scoped(source, actual)
        values = self.adapter.optimization_metrics(source)
        if set(values) != set(self._names):
            raise ValueError("structural counter definitions do not match their advertised names")
        if all(value is None for value in values.values()):
            return SourceObservation(SourceStatus.PARSE_ERROR, parser=actual,
                                     reason="structural parser could not parse the source")
        metrics = tuple(MetricValue(
            definition, self.name, actual,
            SourceStatus.COMPLETED if values[definition.name] is not None else SourceStatus.PARSE_ERROR,
            values[definition.name], "" if values[definition.name] is not None else "counter unavailable")
            for definition in self._definitions)
        return SourceObservation(SourceStatus.COMPLETED,
                                 (MetricScope("file", "", None, None, None, metrics),), parser=actual)

    def _measure_scoped(self, source, parser):
        result = self._scoped(source)
        if result is None:
            return SourceObservation(SourceStatus.PARSE_ERROR, parser=parser,
                                     reason="structural parser could not parse the source")
        keys = [(item.name, item.start_line, item.end_line, item.signature)
                for item in result.functions]
        if len(set(keys)) != len(keys):
            return SourceObservation(SourceStatus.REFUSED, parser=parser,
                                     reason="ambiguous function declarations share a line-only scope identity")

        def values(counts, kind):
            expected = {item.name for item in self._definitions if kind in item.scopes}
            if set(counts) != expected:
                raise ValueError("structural counters do not match their advertised scope definitions")
            return tuple(
                MetricValue(item, self.name, parser, SourceStatus.COMPLETED, counts[item.name])
                if counts[item.name] is not None else
                MetricValue(item, self.name, parser, SourceStatus.UNSUPPORTED, None,
                            "lizard finds no function in this declaration"
                            if self._lizard and item.name == "cyclomatic_complexity"
                            else "the backend gives no value for this declaration")
                for item in self._definitions if kind in item.scopes)

        scopes = [MetricScope("file", "", None, None, None, values(dict(result.file), "file"))]
        for item in result.functions:
            counts = dict(item.counts)
            counts["cyclomatic_complexity"] = item.cyclomatic_complexity
            scopes.append(MetricScope("function", item.name, item.start_line, item.end_line,
                                      item.signature, values(counts, "function")))
        return SourceObservation(SourceStatus.COMPLETED, tuple(scopes), parser=parser)


@dataclass(frozen=True)
class _RegisteredProvider:
    name: str
    definitions: Tuple[MetricDefinition, ...]
    provider: MetricProvider


class MetricRegistry:
    """A registry local to one adapter object. Each collection of metrics uses
    the registrations as they are when it starts."""

    def __init__(self):
        self._bindings = {}
        self._lock = threading.RLock()

    def register(self, provider: MetricProvider, *, replace: bool = False):
        """Register ``provider`` for each metric it defines. A metric name
        already registered raises ``InvalidRequest`` unless ``replace`` is
        true, and so do a provider without a ``name``, ``definitions`` or
        ``measure`` and a definition of a standard metric that differs from
        ``STANDARD_METRICS``."""
        name = getattr(provider, "name", None)
        _text(name, "provider.name")
        if not isinstance(replace, bool):
            raise InvalidRequest("replace", "must be a boolean")
        get_definitions = getattr(provider, "definitions", None)
        if not callable(get_definitions):
            raise InvalidRequest("provider", "must implement definitions")
        definitions = _items(get_definitions(), "definitions")
        if not definitions or any(not isinstance(item, MetricDefinition) for item in definitions):
            raise InvalidRequest("definitions", "must contain metric definitions")
        if not callable(getattr(provider, "measure", None)):
            raise InvalidRequest("provider", "must implement measure")
        if len({item.name for item in definitions}) != len(definitions):
            raise InvalidRequest("definitions", "must not contain duplicate names")
        _keep_standard(definitions)
        registration = _RegisteredProvider(name, definitions, provider)
        with self._lock:
            duplicates = sorted(item.name for item in definitions if item.name in self._bindings)
            if duplicates and not replace:
                raise InvalidRequest("definitions", f"metric names already registered: {', '.join(duplicates)}")
            for definition in definitions:
                self._bindings[definition.name] = registration

    def definitions(self) -> Tuple[MetricDefinition, ...]:
        """The definition of every registered metric, by name."""
        with self._lock:
            bindings = dict(self._bindings)
        return tuple(next(item for item in registration.definitions if item.name == name)
                     for name, registration in sorted(bindings.items()))

    def measure(self, source: str, *, language: str, parser: Optional[str],
                names: Optional[Sequence[str]], parser_context=None) -> SourceObservation[SourceMetrics]:
        """The ``SourceMetrics`` of ``source`` for the metrics ``names``,
        every registered one for None, each from its provider. A standard
        metric without a provider, and one whose provider fails, is listed
        in ``unavailable`` with its reason, and an unknown name raises
        ``InvalidRequest``. ``parser_context``, when given, is called for
        the parser the providers receive."""
        with self._lock:
            bindings = dict(self._bindings)
        if isinstance(names, (str, bytes)):
            raise InvalidRequest("names", "must be a sequence of metric names, not text")
        try:
            requested = tuple(sorted(bindings)) if names is None else tuple(names)
        except TypeError:
            raise InvalidRequest("names", "must be a sequence of metric names") from None
        for name in requested:
            _text(name, "names")
        unknown = sorted(set(requested) - bindings.keys() - STANDARD_METRICS.keys())
        if unknown:
            raise InvalidRequest("names", f"unknown metrics: {', '.join(unknown)}")
        unavailable = [_not_measured(name, language) for name in sorted(set(requested) - bindings.keys())]
        requested = tuple(sorted(set(requested) & bindings.keys()))
        if not requested:
            return SourceObservation(SourceStatus.COMPLETED, SourceMetrics(
                language, MetricScope("file", "", None, None, None, ()), (), tuple(unavailable)))
        if parser_context is not None:
            try:
                parser = parser_context()
            except Exception:
                parser = None  # Context is optional. Values retain their own parser.
        selected = {}
        for name in requested:
            registration = bindings[name]
            selected.setdefault(id(registration), (registration, []))[1].append(name)
        results = []
        for registration, selected_names in selected.values():
            definitions = {item.name: item for item in registration.definitions}
            try:
                observation = registration.provider.measure(source, language=language, parser=parser)
                if not isinstance(observation, SourceObservation):
                    raise InvalidRequest("provider result", "must be a SourceObservation")
                if observation.status != SourceStatus.COMPLETED:
                    unavailable.extend(MetricValue(definitions[name], registration.name, observation.parser,
                                                   observation.status, None, observation.reason)
                                       for name in selected_names)
                    continue
                scopes = self._validate(observation.value, source, registration, selected_names)
                results.append((registration, selected_names, scopes))
            except Exception as exc:
                status = (SourceStatus.TIMEOUT if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
                          else SourceStatus.MISSING_SETUP if isinstance(exc, ImportError)
                          else SourceStatus.INFRASTRUCTURE_FAILURE)
                unavailable.extend(MetricValue(definitions[name], registration.name, None, status,
                                               None, f"{type(exc).__name__}: {exc}")
                                   for name in selected_names)
        conflicts = self._conflicts(results)
        composed = {}
        for index, (registration, selected_names, scopes) in enumerate(results):
            if index in conflicts:
                definitions = {item.name: item for item in registration.definitions}
                unavailable.extend(MetricValue(definitions[name], registration.name, None,
                                               SourceStatus.REFUSED, None, "conflicting function scope identity")
                                   for name in selected_names)
                continue
            for scope in scopes:
                values = tuple(metric for metric in scope.metrics if metric.definition.name in selected_names)
                if not values:
                    continue
                current = composed.setdefault(scope.key, [])
                current.extend(values)
        file_key = ("file", "", None, None, None)
        file = MetricScope(*file_key, tuple(composed.pop(file_key, ())))
        functions = tuple(MetricScope(*key, tuple(values)) for key, values in composed.items())
        return SourceObservation(SourceStatus.COMPLETED, SourceMetrics(language, file, functions, tuple(unavailable)))

    @staticmethod
    def _validate(scopes, source, registration, selected_names):
        scopes = _items(scopes, "provider result")
        if any(not isinstance(scope, MetricScope) for scope in scopes):
            raise InvalidRequest("provider result", "must contain metric scopes")
        if len({scope.key for scope in scopes}) != len(scopes):
            raise InvalidRequest("provider result", "contains duplicate scope identities")
        definitions = {item.name: item for item in registration.definitions}
        for scope in scopes:
            if scope.kind == "function" and scope.end_line > len(source.splitlines()):
                raise InvalidRequest("provider result", "function span exceeds source bounds")
            values = {metric.definition.name: metric for metric in scope.metrics}
            for metric in values.values():
                if metric.provider != registration.name or definitions.get(metric.definition.name) != metric.definition:
                    raise InvalidRequest("provider result", "definition or provider differs from registration")
            if any(name not in values and scope.kind in definitions[name].scopes for name in selected_names):
                raise InvalidRequest("provider result", "requested metric missing from an applicable scope")
        file_names = {metric.definition.name for scope in scopes if scope.kind == "file" for metric in scope.metrics}
        if any("file" in definitions[name].scopes and name not in file_names for name in selected_names):
            raise InvalidRequest("provider result", "requested file metric is absent")
        return tuple(replace(scope, metrics=tuple(metric for metric in scope.metrics
                                                  if metric.definition.name in selected_names))
                     for scope in scopes if any(metric.definition.name in selected_names
                                                for metric in scope.metrics))

    @staticmethod
    def _conflicts(results):
        conflicts = set()
        for i, (_, _, left) in enumerate(results):
            for j in range(i + 1, len(results)):
                right = results[j][2]
                for first in (scope for scope in left if scope.kind == "function"):
                    for second in (scope for scope in right if scope.kind == "function"):
                        if first.key == second.key or first.name != second.name:
                            continue
                        overlap = first.start_line <= second.end_line and second.start_line <= first.end_line
                        same_signature = (first.signature == second.signature or first.signature is None
                                          or second.signature is None)
                        same_start = first.start_line == second.start_line
                        known_overloads = (first.key in {scope.key for scope in right}
                                           and second.key in {scope.key for scope in left})
                        if overlap and (same_signature or same_start and not known_overloads):
                            conflicts.update((i, j))
        return conflicts


#: ``(name, owners) -> the owner that keeps name``, or None to leave the
#: conflict unresolved. ``owners`` are the indices of the sources that bind
#: ``name`` un-renamably.
Choose = Callable[[str, Tuple[int, ...]], Optional[int]]

#: A byte range of a source's UTF-8 text and its replacement.
Edit = Tuple[int, int, str]


def fresh_name(name: str, taken: Collection[str]) -> str:
    """The first ``<name>_<k>``, k from 2, not in ``taken``. The caller
    takes it when its rename succeeds."""
    k = 2
    while f"{name}_{k}" in taken:
        k += 1
    return f"{name}_{k}"


def apply_edits(text: str, edits: Iterable[Edit]) -> str:
    """``text`` with the byte-range ``edits`` applied. Of overlapping edits,
    the one that starts first is kept, the longest at an equal start. A
    repeated edit counts once."""
    return _edited(text, edits)[0].decode("utf-8")


def _edited(text: str, edits: Iterable[Edit]) -> Tuple[bytes, List[int]]:
    """``text``'s UTF-8 bytes with ``edits`` applied as ``apply_edits``
    applies them, and the offsets in those bytes where each applied edit's
    replacement starts and ends, in ascending order."""
    data = text.encode("utf-8")
    chosen = []
    for start, end, new in sorted(dict.fromkeys(edits), key=lambda e: (e[0], -e[1])):
        if chosen and start < chosen[-1][1]:
            continue
        chosen.append((start, end, new))
    out = bytearray()
    junctions: List[int] = []
    position = 0
    for start, end, new in chosen:
        out += data[position:start]
        junctions.append(len(out))
        out += new.encode("utf-8")
        junctions.append(len(out))
        position = end
    out += data[position:]
    return bytes(out), junctions


_LINE_END = re.compile(r"\r\n|\r|\n")


def rows(text: AnyStr) -> List[AnyStr]:
    """``text``'s lines with their ends, as tree-sitter counts its rows:
    each line ends after a newline. Bytes split at the same points, so a
    tree-sitter column, a byte offset, indexes a row of ``text.encode()``."""
    newline = b"\n" if isinstance(text, bytes) else "\n"
    parts = text.split(newline)
    last = parts.pop()
    return [part + newline for part in parts] + ([last] if last else [])


def lines(text: str) -> List[str]:
    """``text``'s lines with their ends, as Python and javac number them:
    each line ends after a newline, a carriage return, or both."""
    found: List[str] = []
    start = 0
    for end in _LINE_END.finditer(text):
        found.append(text[start:end.end()])
        start = end.end()
    if start < len(text):
        found.append(text[start:])
    return found


def line_number(text: str, index: int) -> int:
    """The line, numbered as ``lines`` numbers it, holding ``text[index]``."""
    return 1 + len(_LINE_END.findall(text, 0, index))


def bare(line: AnyStr) -> AnyStr:
    """``line`` of ``rows`` or ``lines`` without its line end."""
    if line.endswith(b"\n" if isinstance(line, bytes) else "\n"):
        line = line[:-1]
    if line.endswith(b"\r" if isinstance(line, bytes) else "\r"):
        line = line[:-1]
    return line


#: The most characters of one captured text a result carries: a bound against
#: memory exhaustion, not a display limit (the caller cuts what it shows).
SAFETY_CHARS = 4_000_000


def whole(text: str, what: str, *, limit: int = SAFETY_CHARS) -> str:
    """``text`` whole up to ``limit`` characters, else its first
    ``limit // 3`` and last ``limit - limit // 3`` characters around a
    marker that says how many characters of ``what`` were omitted."""
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return (f"{text[:head]}\n[... {len(text) - head - tail} chars of {what} omitted ...]\n"
            f"{text[-tail:]}")


def bounded(output: str, *, limit: int = 5000, head: int = 1800,
            tail: int = 3200) -> str:
    """``output`` whole up to ``limit`` characters, else its first ``head``
    and last ``tail`` characters around a truncation marker, for
    diagnostics, per-mutant output and exception messages. The head keeps
    what was printed before a long failure traceback."""
    if len(output) > limit:
        return output[:head] + "\n...[truncated]...\n" + output[-tail:]
    return output
