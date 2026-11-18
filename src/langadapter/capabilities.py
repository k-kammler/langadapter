"""What an adapter can do for its project context, and the descriptive facts
about its language.

A capability report probes the project's environment (its interpreter, local
or in the container), not the calling process: each operation is available,
unsupported by the language or engine, or missing setup, with what is missing
and where it was looked for. Language facts are data, never instructions.
``VERIFY``, ``RUN`` and ``RETRY`` are the plain next steps setup reports attach
where a person can act on an unconfirmed item (none for unimplemented operations).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional, Tuple
from langadapter.outcomes import Outcome
from langadapter.errors import InvalidRequest
from langadapter.versions import Version, capability_runtime, facts_runtime


#: The next step of an item only the tool check tries.
VERIFY = "run the tool check to try it"
#: The next step of an item only a real run tries.
RUN = "start a run, which tries it for the first time"
#: The next step of an item whose check reached its time limit.
RETRY = "run the check again with a longer time limit"

#: What first tries an item or operation: the setup verification, or only a real run.
TRIED_BY = ("verification", "run")


#: The source operations that read a source and change nothing
#: (``adapter.sources``).
SOURCE_READS = (
    "check_structure", "test_metrics", "metrics", "skeleton", "same_skeleton",
    "function_fingerprints", "imports", "import_path_candidates",
    "duplicate_test_names", "merge_collisions", "is_test_file", "validate_source",
    "target_identity", "resolve_collisions", "analyze_validity", "screen_source",
    "normalize_bindings", "stage_pool", "import_statement", "code_smells",
    "project_source_files", "call_statistics",
)
#: The source operations that return an edited source (``adapter.sources``).
SOURCE_EDITS = (
    "prune_tests", "merge_sources", "normalize_shadowed", "salvage_syntax",
    "prune_unresolvable_imports", "prune_collection_breakers", "strip_module_level_skips",
    "finalize_source",
)
#: The operations that run a suite (``adapter.tests``).
TEST_OPERATIONS = ("run_suite", "collect_suite", "run_as_delivered", "benchmark", "benchmark_warmed",
                   "per_test_evidence", "profile_suite")
#: The mutation operations (``adapter.mutations``).
MUTATION_OPERATIONS = ("engines", "operators", "open_session", "run", "per_suite")
#: Every operation's name as an operation report and a setup request name
#: it, such as ``tests.run_suite``: the four groups above with their
#: prefixes.
OPERATION_NAMES = (
    *(f"sources.{name}" for name in SOURCE_READS + SOURCE_EDITS),
    *(f"tests.{name}" for name in TEST_OPERATIONS),
    *(f"mutations.{name}" for name in MUTATION_OPERATIONS),
)
#: Optional measurements a caller may require of an operation: a suite run's
#: coverage of its ``coverage_target``.
OPERATION_FACETS = MappingProxyType({
    "tests.run_suite": ("coverage",),
})


class Availability(str, enum.Enum):
    """Whether an operation can run for the context."""

    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    MISSING_SETUP = "missing_setup"

    def __str__(self) -> str:
        return self.value


#: The capabilities a capability report covers: running a suite
#: (``suite_runs``), measuring its coverage (``coverage``), per-test
#: evidence (``per_test_evidence``) and the kills attributed to each test
#: (``per_test_kill_attribution``), mutation runs (``mutation``), merging
#: test sources (``source_merging``), and the operations that repair and
#: screen generated tests (``generated_suite_operations``):
#: ``salvage_syntax``, ``prune_unresolvable_imports``,
#: ``prune_collection_breakers``, ``strip_module_level_skips``,
#: ``analyze_validity`` and ``screen_source``.
CAPABILITIES = (
    "suite_runs",
    "coverage",
    "per_test_evidence",
    "per_test_kill_attribution",
    "mutation",
    "source_merging",
    "generated_suite_operations",
)


@dataclass(frozen=True)
class Capability:
    """One operation's availability, with the reason when it is not available."""

    name: str
    availability: Availability
    reason: str = ""

    @property
    def available(self) -> bool:
        """Whether ``availability`` is ``Availability.AVAILABLE``."""
        return self.availability is Availability.AVAILABLE


@dataclass(frozen=True)
class OperationCapability:
    """Static implementation support, separate from probed readiness (None:
    unprobed, not unsupported). A probed setup can make an implemented operation
    unsupported. Resolution says how evidence is obtained, granularity what it
    holds. ``action`` is the next step for an operation not ready, None where
    nobody can act or ``reason`` names the remedy. ``tried_by`` names what first
    tries a supported, untried operation: ``"verification"`` or ``"run"``."""

    name: str
    supported: bool
    reason: str = ""
    readiness: Optional[Availability] = None
    execution_backed: bool = False
    execution_kinds: Tuple[str, ...] = ()
    evidence_granularity: Tuple[str, ...] = ()
    resolution_methods: Tuple[str, ...] = ()
    action: Optional[str] = None
    tried_by: Optional[str] = None

    def __post_init__(self) -> None:
        if self.name not in OPERATION_NAMES:
            raise InvalidRequest("operation", f"unknown operation {self.name!r}")
        if type(self.supported) is not bool or type(self.execution_backed) is not bool:
            raise InvalidRequest("supported", "operation support and execution requirement must be booleans")
        if self.action is not None and (not isinstance(self.action, str) or not self.action.strip()):
            raise InvalidRequest("action", "must be a non-empty string or None")
        if self.readiness is not None:
            try:
                object.__setattr__(self, "readiness", Availability(self.readiness))
            except (TypeError, ValueError):
                raise InvalidRequest("readiness", f"unknown readiness {self.readiness!r}") from None
        if not self.supported:
            if self.readiness not in (None, Availability.UNSUPPORTED):
                raise InvalidRequest("readiness", "unsupported operations cannot be ready or missing setup")
            object.__setattr__(self, "readiness", Availability.UNSUPPORTED)
        for name in ("execution_kinds", "evidence_granularity", "resolution_methods"):
            values = getattr(self, name)
            if isinstance(values, (str, bytes)):
                raise InvalidRequest(name, "must be a sequence of distinct non-empty names")
            try:
                values = tuple(values)
            except TypeError:
                raise InvalidRequest(name, "must be a sequence of distinct non-empty names") from None
            if (any(not isinstance(value, str) or not value.strip() for value in values)
                    or len(set(values)) != len(values)):
                raise InvalidRequest(name, "must be a sequence of distinct non-empty names")
            object.__setattr__(self, name, values)
        if self.tried_by is not None and (self.tried_by not in TRIED_BY or self.readiness is not None):
            raise InvalidRequest("tried_by", "only a supported operation not tried yet names 'verification' or 'run'")


@dataclass(frozen=True)
class ToolVersion:
    """A tool of the project environment: the version the adapter pins, and
    the version found there, or None when it is absent."""

    name: str
    pinned: Optional[str]
    found: Optional[str]


@dataclass(frozen=True)
class CapabilityReport:
    """The capabilities of an adapter instance for its context. ``outcome`` says
    whether the environment probe ran: ``completed``, else with ``reason``
    ``infrastructure_failure``, ``missing_setup`` (for every operation needing
    the environment) or ``unsupported`` (no readiness probe). ``branch_coverage``
    is the coverage tool's branch evidence: ``arcs``, ``counters`` or ``none``."""

    language: str
    interpreter: str
    outcome: Outcome
    reason: str
    runtime: Optional[str]
    capabilities: Tuple[Capability, ...]
    tools: Tuple[ToolVersion, ...]
    branch_coverage: str
    mutation_engines: Tuple[str, ...]
    operations: Tuple[OperationCapability, ...] = ()
    #: The version whose text ``runtime`` is (``versions.capability_runtime``), or None.
    version_record: Optional[Version] = None

    def __post_init__(self) -> None:
        _derived(self.runtime, self.version_record, capability_runtime, "runtime")
        object.__setattr__(self, "operations", tuple(self.operations))
        if len({item.name for item in self.operations}) != len(self.operations):
            raise InvalidRequest("operations", "duplicate operation entries")

    def operation(self, name: str) -> OperationCapability:
        """The entry of the operation ``name`` (``OPERATION_NAMES``), not
        available with the reason "operation is not implemented" when the
        report holds none. An unknown name raises ``InvalidRequest``."""
        if name not in OPERATION_NAMES:
            raise InvalidRequest("operation", f"unknown operation {name!r}")
        return next((item for item in self.operations if item.name == name),
                    OperationCapability(name, False, "operation is not implemented"))

    def capability(self, name: str) -> Capability:
        """The entry of the capability ``name`` (``CAPABILITIES``). A name the
        report does not hold raises ``KeyError``."""
        for capability in self.capabilities:
            if capability.name == name:
                return capability
        raise KeyError(name)


@dataclass(frozen=True)
class LanguageFacts:
    """Descriptive facts about an adapter's language and context.

    ``runtime_version`` is the interpreter's own version line, None when
    unreadable. ``timeout_marker`` is the framework's per-test timeout syntax
    with ``{seconds}`` for the value, None where it has none. ``language_level``
    is the language version the tests are written in, perhaps below the
    runtime's (``Java 8`` for a release target, ``Python 3.12``), None when
    unreadable. ``test_function_pattern`` shows how a test is written,
    such as ``test_*`` or ``@Test``, and ``test_class_pattern`` how a test
    class is named, such as ``*Test``, None where tests have no class.
    ``unmatched_evidence_kept`` says whether a reduction keeps the
    items of unmatched contexts (``PerTestEvidence.unmatched_items``): for Python
    a test run under its own context but missing from the report, for Java a
    JUnit id naming no test. ``unsound_test_patterns`` are the regular
    expressions of the warnings that mark a test leaving shared state behind:
    a suite run reports each such warning as an ``unsound_test`` entry of its
    ``diagnostics``, naming the test where the run knows it. Empty by default,
    and the language's runs report none."""

    language: str
    test_framework: str
    code_fence: str
    runtime_version: Optional[str]
    test_file_patterns: Tuple[str, ...]
    test_function_pattern: str
    test_class_pattern: Optional[str]
    timeout_marker: Optional[str]
    #: The version whose text ``runtime_version`` is (``versions.facts_runtime``), or None.
    version_record: Optional[Version] = None
    language_level: Optional[str] = None
    unmatched_evidence_kept: bool = False
    unsound_test_patterns: Tuple[str, ...] = ()
    #: The registered name of the language the tests are written in, where it
    #: is not the adapter's own (``python`` for Python tests of a C file).
    #: None: the adapter's own language.
    test_language: Optional[str] = None
    #: The code fence of the target's source, where it is not ``code_fence``,
    #: the fence of the tests (``c`` for Python tests of a C file). None:
    #: ``code_fence``.
    target_code_fence: Optional[str] = None

    def __post_init__(self) -> None:
        _derived(self.runtime_version, self.version_record, facts_runtime, "runtime_version")


def _derived(text, record, wording, field):
    """Refuse a text that is not the wording of its version record. The text is
    the stored and exported value, so it must be that version's text."""
    if record is None:
        return
    if not isinstance(record, Version):
        raise InvalidRequest("version_record", "must be a Version or None")
    if text != wording(record):
        raise InvalidRequest(field, f"must be {wording(record)!r}, the text of its version record")
