"""Checks that an adapter, a metric provider or a faster per-suite mutation
implementation keeps the shared contract.

An adapter or metric provider written outside the package runs them on a
small example of its language, in its own tests or as a script::

    from langadapter.conformance import AdapterExample, check_adapter

    problems = check_adapter(AdapterExample("toy", context, suite))
    assert not problems, "\\n".join(problems)

Each problem is one sentence naming the rule it breaks. An empty tuple means
none was found. The checks use the public operations and cover the shared
contract, not whether the language's measurements are right.
``compare_runs`` on the same example names each way a tool's run of the
unchanged suite differs from the normal run, which decides whether a test
passes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

from langadapter.adapter import LangAdapter, _unknown_setting_kinds
from langadapter.capabilities import CAPABILITIES, OPERATION_NAMES, CapabilityReport
from langadapter.context import ProjectContext
from langadapter.errors import InvalidRequest, UnsupportedLanguage
from langadapter.sources import MetricProvider, APPLIED, UNCHANGED, SourceStatus
from langadapter.mutation import (
    DEFAULT_IMPLEMENTATION, MutationRequest, MutationRun, MutationSession, PerSuiteRequest,
)
from langadapter.outcomes import Outcome
from langadapter.registry import create, language_for, names
from langadapter.runs import (
    ERROR, FAILED, PASSED, SKIPPED, XFAILED, PerTestEvidenceRequest, SuiteRun, SuiteRunRequest,
)
from langadapter.setup import ProjectSetupRequest, inspect_project
from langadapter.suites import Suite, TestId
from langadapter.versions import LanguageSupport, VersionProfile

#: The outcomes a test of an example suite can be expected to have.
TEST_OUTCOMES = (PASSED, FAILED, ERROR, SKIPPED, XFAILED)


@dataclass(frozen=True)
class AdapterExample:
    """A small project of one registered language.

    ``suite``, in the project ``context`` describes, must run (tests pass or
    fail, none fails to collect) with at least one test per declared file.
    ``expected`` gives some tests' outcomes. ``coverage_target``, a project
    source file, adds the coverage check. ``collection_breaker``, needed where
    the adapter prunes collection breakers, is a test source with one unit that
    keeps it from loading. ``langadapter.check`` expects its run to report that."""

    language: str
    context: ProjectContext
    suite: Suite
    expected: Mapping[TestId, str] = field(default_factory=dict)
    coverage_target: Optional[str] = None
    timeout_s: float = 120.0
    collection_breaker: Optional[str] = None


def check_adapter(example: AdapterExample) -> Tuple[str, ...]:
    """The contract problems of ``example.language``'s adapter on the example.

    Checked:

    - the registration claims the suite's files
    - ``create`` returns an adapter of the language bound to the context
    - ``support`` is a ``LanguageSupport``, ``profile()`` a ``VersionProfile``
    - each of ``project_settings`` has one of the four kinds of ``ProjectSetting``
    - an adapter that prunes collection breakers declares ``collection_unit``,
      removes the example's breaker and leaves a pruned source unchanged
    - the operation report lists every operation once, in order, with a reason
      for each unsupported one, and the capability report only known capabilities
    - a suite run answers consistently (counts, an outcome for a test of every
      declared file, the overall outcome, expected outcomes), or unsupported
    - ``provides_timeout_journal`` is a bool, and if true, a journal run is as consistent
    - coverage is measured where available and refused where not
    - each of the language's own metric providers (``metric_providers``)
      gives every file metric a value for the example's first test file
    - setup inspection returns a report over the same operations, given the
      suite's files inside the project root"""
    problems: List[str] = []
    language = example.language
    if language not in names():
        return (f"registration: {language} is not registered",)
    for path in example.suite.test_files:
        try:
            claimed = language_for(path)
        except UnsupportedLanguage:
            claimed = None
        if claimed != language:
            problems.append(f"registration: {path} is not claimed by {language}")

    adapter = create(language, example.context)
    if not isinstance(adapter, LangAdapter):
        return (*problems, f"creation: create returned {type(adapter).__name__}, not a LangAdapter")
    if adapter.language != language:
        problems.append(f"creation: the adapter reports language {adapter.language!r}")
    if adapter.context != example.context:
        problems.append("creation: the adapter is not bound to the given context")
    if not isinstance(adapter.support, LanguageSupport):
        # Every operation asks the profile, which classifies against it.
        return (*problems, "declaration: support must be a LanguageSupport (langadapter.versions)")
    if not isinstance(adapter.profile(), VersionProfile):
        return (*problems, "declaration: profile() must return a VersionProfile")
    problems.extend(_unknown_setting_kinds(adapter.project_settings))

    operations = adapter.operation_capabilities()
    if tuple(item.name for item in operations) != OPERATION_NAMES:
        problems.append("operation report: every operation name must appear once, in order")
    for item in operations:
        if not item.supported and not item.reason:
            problems.append(f"operation report: unsupported {item.name} gives no reason")
    supported = {item.name for item in operations if item.supported}
    if "sources.prune_collection_breakers" in supported:
        problems.extend(_collection_breaker_problems(adapter, example))

    report = adapter.probe_capabilities()
    coverage_available = False
    if not isinstance(report, CapabilityReport):
        problems.append("capability report: probe_capabilities must return a CapabilityReport")
    else:
        unknown = sorted({item.name for item in report.capabilities} - set(CAPABILITIES))
        if unknown:
            problems.append(f"capability report: unknown capabilities {', '.join(unknown)}")
        coverage_available = any(item.name == "coverage" and item.available
                                 for item in report.capabilities)

    request = SuiteRunRequest(suite=example.suite, timeout_s=example.timeout_s)
    run = adapter.tests.run_suite(request)
    if "tests.run_suite" not in supported:
        if run.outcome is not Outcome.UNSUPPORTED:
            problems.append("suite run: an unsupported operation must answer unsupported, "
                            f"not {run.outcome.value}")
    else:
        problems.extend(_suite_run_problems(run, example, coverage=False))
        if not isinstance(adapter.provides_timeout_journal, bool):
            problems.append("declaration: provides_timeout_journal must be true or false")
        elif adapter.provides_timeout_journal:
            journal = adapter.tests.run_suite(replace(request, timeout_journal=True))
            problems.extend(_suite_run_problems(journal, example, coverage=False,
                                                label="journal run"))
        if example.coverage_target is not None:
            measured = adapter.tests.run_suite(replace(request, coverage_target=example.coverage_target))
            if coverage_available:
                problems.extend(_suite_run_problems(measured, example, coverage=True))
            elif measured.outcome not in (Outcome.UNSUPPORTED, Outcome.MISSING_SETUP):
                problems.append("coverage: an adapter without available coverage must refuse a "
                                f"coverage request, not answer {measured.outcome.value}. An "
                                "adapter that measures coverage reports it available in "
                                "capabilities()")

    for provider in adapter.metric_providers():
        first = Path(example.suite.root, example.suite.test_files[0]).read_text(encoding="utf-8")
        problems.extend(f"metric provider {provider.name}: {problem}"
                        for problem in _measurement_problems(adapter.sources, provider, first))

    try:
        # The assessment refuses a report that breaks the setup contract.
        setup = inspect_project(language, ProjectSetupRequest(
            example.context, _project_files(example), ("tests.run_suite",)))
    except InvalidRequest as error:
        problems.append(f"setup: inspection must return a valid inspection ProjectSetupReport ({error})")
    else:
        if tuple(item.name for item in setup.operations) != OPERATION_NAMES:
            problems.append("setup: the inspection report must cover every operation name, in order")
    return tuple(problems)


def _collection_breaker_problems(adapter: LangAdapter, example: AdapterExample) -> List[str]:
    label = "collection breakers"
    if not adapter.collection_unit:
        return [f"{label}: an adapter that prunes them declares the unit it removes "
                "(collection_unit)"]
    if example.collection_breaker is None:
        return [f"{label}: the example of an adapter that prunes them gives one "
                "(AdapterExample.collection_breaker)"]
    pruned = adapter.sources.prune_collection_breakers(example.collection_breaker,
                                                       timeout_s=example.timeout_s)
    if pruned.status != APPLIED or not pruned.removed:
        return [f"{label}: the example's breaker must be removed and named, "
                f"not {pruned.status} ({pruned.detail})"]
    again = adapter.sources.prune_collection_breakers(pruned.source, timeout_s=example.timeout_s)
    if again.status != UNCHANGED:
        return [f"{label}: a pruned source pruned again must be unchanged, not {again.status}"]
    return []


def _project_files(example: AdapterExample) -> Tuple[str, ...]:
    """The suite's test files inside the project root, relative to it, as
    setup takes them. A suite outside the project gives none."""
    root = Path(os.path.abspath(example.context.root))
    files = []
    for name in example.suite.test_files:
        try:
            files.append(Path(os.path.abspath(Path(example.suite.root, name))).relative_to(root).as_posix())
        except ValueError:
            continue
    return tuple(files)


def _suite_run_problems(run, example: AdapterExample, *, coverage: bool,
                        label: Optional[str] = None) -> List[str]:
    label = label or ("coverage run" if coverage else "suite run")
    if not isinstance(run, SuiteRun):
        return [f"{label}: run_suite must return a SuiteRun"]
    problems = []
    if run.identity is None:
        problems.append(f"{label}: the result has no observation identity")
    if run.outcome not in (Outcome.COMPLETED, Outcome.TEST_FAILURES):
        return [*problems, f"{label}: the example suite did not run "
                           f"({run.outcome.value}: {run.reason_text()})"]
    counts = run.counts
    parts = (counts.passed, counts.failed, counts.errors, counts.skipped)
    if counts.collected is None or any(part is None for part in parts):
        problems.append(f"{label}: a run that ran reports every count")
    else:
        if sum(parts) != counts.collected:
            problems.append(f"{label}: passed, failed, errors and skipped must add up to collected")
        if len(run.tests) != counts.collected:
            problems.append(f"{label}: {counts.collected} collected but {len(run.tests)} test outcomes")
        if (run.outcome is Outcome.COMPLETED) != (counts.failed + counts.errors == 0):
            problems.append(f"{label}: the outcome must be test_failures exactly when a test "
                            "failed or had an error")
    declared = {item.path for item in example.suite.identity()[:len(example.suite.test_files)]}
    for outcome in run.tests:
        if outcome.test.file not in declared:
            problems.append(f"{label}: {outcome.test} is not in a declared test file")
    # A completed run reports the tests it ran. A suite without tests is no_tests.
    for name in sorted(declared - {outcome.test.file for outcome in run.tests}):
        problems.append(f"{label}: no test outcome for the declared test file {name}")
    for test, expected in example.expected.items():
        if expected not in TEST_OUTCOMES:
            problems.append(f"example: {test} expects unknown outcome {expected!r}")
        elif run.outcome_of(test) != expected:
            problems.append(f"{label}: {test} was {run.outcome_of(test)}, expected {expected}")
    if coverage and run.coverage is None:
        problems.append("coverage run: a completed coverage run must carry its coverage observation")
    if not coverage and run.coverage is not None:
        problems.append("suite run: coverage was measured without a coverage target")
    return problems


#: The outcomes of a tool's run that measured nothing because the adapter or
#: the context does not offer the tool: no difference from the normal run.
_NOT_OFFERED = (Outcome.UNSUPPORTED, Outcome.MISSING_SETUP)


def compare_runs(example: AdapterExample) -> Tuple[str, ...]:
    """The differences between the example suite's normal run and each tool's
    run of the same unchanged suite, with ``example.coverage_target``: the
    coverage tool's run, the per-test evidence and the mutation tool's check
    before its first mutant (a mutation run of that file with the first
    engine's default catalogue, on a new adapter, as a consumer's first
    session runs). The normal run decides whether a test
    passes, so each difference is named: a test another run gives another
    outcome, a run that refuses a suite the normal run passes or measures
    one it fails, a tool that names other tests as stopping it. A tool the
    adapter or the context does not offer is skipped. An empty tuple means
    none was found."""
    adapter = create(example.language, example.context)
    supported = {item.name for item in adapter.operation_capabilities() if item.supported}
    if "tests.run_suite" not in supported:
        return ("normal run: the adapter runs no suite, so no tool's run compares",)
    request = SuiteRunRequest(suite=example.suite, timeout_s=example.timeout_s)
    normal = adapter.tests.run_suite(request)
    if normal.outcome not in (Outcome.COMPLETED, Outcome.TEST_FAILURES):
        return (f"normal run: the example suite did not run "
                f"({normal.outcome.value}: {normal.reason_text()})",)
    verdicts = {outcome.test: outcome.outcome for outcome in normal.tests}
    failing = frozenset(test for test, verdict in verdicts.items() if verdict in (FAILED, ERROR))
    target = example.coverage_target
    if target is None:
        return ()
    problems: List[str] = []
    coverage = adapter.tests.run_suite(replace(request, coverage_target=target))
    if coverage.outcome not in _NOT_OFFERED:
        problems.extend(_verdict_problems("coverage run", verdicts, coverage))
    if "tests.per_test_evidence" in supported:
        evidence = adapter.tests.per_test_evidence(
            PerTestEvidenceRequest(example.suite, target, example.timeout_s))
        if evidence.outcome not in _NOT_OFFERED:
            problems.extend(_evidence_problems(verdicts, failing, evidence))
    if {"mutations.open_session", "mutations.run"} <= supported:
        engines = adapter.mutations.engines()
        if engines:
            # A new adapter: the check must not depend on the runs before it.
            fresh = create(example.language, example.context)
            with fresh.mutations.open_session(engines[0]) as session:
                run = session.run(MutationRequest(target=target, suite=example.suite))
            problems.extend(_mutation_problems(failing, run))
    return tuple(problems)


def _verdict_problems(label: str, verdicts: Mapping[TestId, str], run: SuiteRun) -> List[str]:
    if run.outcome not in (Outcome.COMPLETED, Outcome.TEST_FAILURES):
        return [f"{label}: the suite did not run ({run.outcome.value}: {run.reason_text()})"]
    theirs = {outcome.test: outcome.outcome for outcome in run.tests}
    problems = [f"{label}: {test} was {theirs[test]}, in the normal run {verdict}"
                for test, verdict in verdicts.items() if test in theirs and theirs[test] != verdict]
    problems += [f"{label}: no outcome for {test}, which the normal run ran"
                 for test in verdicts if test not in theirs]
    problems += [f"{label}: {test} is no test of the normal run" for test in theirs
                 if test not in verdicts]
    return problems


def _evidence_problems(verdicts: Mapping[TestId, str], failing: frozenset,
                       evidence) -> List[str]:
    label = "per-test evidence"
    if failing:
        if evidence.outcome is Outcome.COMPLETED:
            return [f"{label}: a green run of a suite whose normal run failed "
                    f"{', '.join(sorted(str(test) for test in failing))}"]
        return []
    if evidence.outcome is not Outcome.COMPLETED:
        return [f"{label}: no evidence of a suite the normal run passed "
                f"({evidence.outcome.value}: {evidence.reason})"]
    # Evidence names a parametrized function once, without parameters
    # (TestEvidence), so each case of the normal run matches its function.
    measured = {(item.test.file, item.test.qualname) for item in evidence.tests}
    return [f"{label}: no evidence for {test}, which the normal run ran"
            for test in verdicts if (test.file, test.qualname) not in measured]


def _mutation_problems(failing: frozenset, run: MutationRun) -> List[str]:
    """The differences between a mutation run's check before its first mutant
    and the normal run whose failing tests are ``failing``: the tool must
    refuse exactly a suite the normal run fails, naming those tests
    (``MutationRun.stopping_tests``), and measure one it passes."""
    label = "mutation tool's run"
    if run.outcome in _NOT_OFFERED:
        return []
    stopping = frozenset(run.stopping_tests)
    named = ", ".join(sorted(str(test) for test in stopping))
    if not failing:
        if stopping:
            return [f"{label}: stopped by {named}, which the normal run passed"]
        if not run.measured:
            return [f"{label}: measured nothing of a suite the normal run passed "
                    f"({run.outcome.value}: {run.reason})"]
        return []
    if run.measured:
        return [f"{label}: measured a suite whose normal run failed "
                f"{', '.join(sorted(str(test) for test in failing))}"]
    if stopping != failing:
        return [f"{label}: stopped by {named or 'no named test'}, where the normal run failed "
                f"{', '.join(sorted(str(test) for test in failing))}"]
    return []


#: How many disagreeing verdicts ``check_per_suite`` names one by one.
PER_SUITE_LISTED = 20


def check_per_suite(session: MutationSession, request: PerSuiteRequest) -> Tuple[str, ...]:
    """The problems of a session's faster per-suite implementation on
    ``request``, against the reference (each suite measured on its own) in the
    same session: every suite must get the same outcome and mutants, and each
    mutant the same verdict and killers. Without a faster implementation there
    is nothing to compare. Choose suites that reach every verdict the engine
    gives, and suites that would disturb each other in one process."""
    faster = session.per_suite(request)
    if faster.implementation == DEFAULT_IMPLEMENTATION:
        return ()
    reference = session._per_suite(request, fast=False)
    label = f"per-suite {faster.implementation}"
    problems: List[str] = []
    for name in request.names:
        ours, theirs = faster.runs[name].outcome, reference.runs[name].outcome
        if ours is not theirs:
            problems.append(f"{label}: suite {name} ended {ours.value}, "
                            f"the reference {theirs.value}")
    ours = {mutant.key: mutant for mutant in faster.mutants}
    theirs = {mutant.key: mutant for mutant in reference.mutants}
    for key in sorted(set(theirs) - set(ours), key=repr):
        problems.append(f"{label}: no verdicts for the reference's mutant {_named(key)}")
    for key in sorted(set(ours) - set(theirs), key=repr):
        problems.append(f"{label}: mutant {_named(key)} is not in the reference")
    differing = []
    for key in theirs:
        if key not in ours:
            continue
        for name in request.names:
            got, want = ours[key].verdicts[name], theirs[key].verdicts[name]
            if got != want:
                differing.append(
                    f"{label}: suite {name}, mutant {_named(key)}: {got.verdict} "
                    f"{list(got.killers)}, the reference {want.verdict} {list(want.killers)}")
    problems.extend(differing[:PER_SUITE_LISTED])
    if len(differing) > PER_SUITE_LISTED:
        problems.append(f"{label}: {len(differing) - PER_SUITE_LISTED} more verdicts differ")
    return tuple(problems)


def _named(key) -> str:
    return f"{key.operator} at line {key.line} (occurrence {key.occurrence})"


def check_metric_provider(provider: MetricProvider, *, language: str, source: str,
                          unparsable: Optional[str] = None,
                          root: Optional[str] = None) -> Tuple[str, ...]:
    """The contract problems of a metric provider on one source of ``language``.

    The provider is registered as users do, on a source-only adapter of
    ``language`` (rooted at ``root``, by default the working directory). Checked:
    it registers with a name and unique definitions clashing with no existing
    metric, measuring ``source`` gives every file-scope definition a finite value, and for
    ``unparsable`` source no metric reports a number."""
    if language not in names():
        return (f"registration: {language} is not registered",)
    sources = create(language, ProjectContext(os.path.abspath(root or "."))).sources
    try:
        sources.register_metric_provider(provider)
    except InvalidRequest as error:
        return (f"registration: {error}",)
    return _measurement_problems(sources, provider, source, unparsable)


def _measurement_problems(sources, provider: MetricProvider, source: str,
                          unparsable: Optional[str] = None) -> Tuple[str, ...]:
    """The problems of a registered provider's values for ``source``, and
    for ``unparsable`` source, where given."""
    definitions = provider.definitions()
    defined = tuple(definition.name for definition in definitions)
    observation = sources.metrics(source, names=defined)
    problems: List[str] = []
    if observation.status != SourceStatus.COMPLETED or observation.value is None:
        return (f"measurement: the source was not measured ({observation.status}: {observation.reason})",)
    metrics = observation.value
    for value in metrics.unavailable:
        problems.append(f"measurement: {value.definition.name} is unavailable "
                        f"({value.status.value}: {value.reason})")
    measured = {value.definition.name for value in metrics.file.metrics
                if value.status is SourceStatus.COMPLETED}
    for definition in definitions:
        if "file" in definition.scopes and definition.name not in measured:
            problems.append(f"measurement: file metric {definition.name} has no value")
    if unparsable is not None:
        problems.extend(_unparsable_problems(sources, defined, unparsable))
    return tuple(problems)


def _unparsable_problems(sources, defined: Tuple[str, ...], unparsable: str) -> List[str]:
    """The metrics among ``defined`` that report a number for ``unparsable``
    source, each as a problem."""
    failed = sources.metrics(unparsable, names=defined).value
    numbers = [value.definition.name for scope in (failed.file, *failed.functions)
               for value in scope.metrics if value.value is not None] if failed else []
    return [f"unparsable source: {name} reports a number for source it cannot read" for name in numbers]
