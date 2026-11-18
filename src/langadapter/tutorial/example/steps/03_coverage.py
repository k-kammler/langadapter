"""Step 3 of the Toy tutorial: measure coverage.

run_suite measures coverage when the request names a coverage_target:
the source lines the run evaluated and the branches it took.
per_test_evidence gives each test's own coverage, from the test run alone.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import langadapter as la
from langadapter.runs import (
    ERROR, FAILED, KILLS_NOT_REQUESTED, KILLS_UNUSABLE, KILLS_USABLE, PASSED, PerTestEvidence,
    PerTestEvidenceRequest, RunCounts, SuiteRun, SuiteRunRequest, TestEvidence, TestOutcome,
    CoverageObservation,
)
from langadapter.capabilities import CAPABILITIES
from langadapter.tutorial.example import tools
from langadapter.versions import Version
from langadapter.sources import TestUnit
from langadapter.outcomes import FileIdentity
from langadapter.suites import BranchItem, KillItem, LineItem

#: The Toy's tools are the Toy's toolchain, version 1.
TOOLCHAIN = la.ToolchainIdentity.of(Version("toy", (1,), "1", "context"))


class ToyAdapter(la.LangAdapter):
    """The Toy language's adapter at step 3 of the tutorial."""

    # Facts: the language, its files and its tests.
    language = "toy"
    file_extensions = (".toy",)
    display_name = "Toy"
    # The program that runs the tests. The Toy's tools run inside Python.
    runtime = "python"
    test_file_name = "test_{stem}.toy"
    test_framework = "Toy tests"
    code_fence = "text"
    # The Toy keeps no per-test journal: run_suite refuses the request.
    provides_timeout_journal = False

    #: What a request can ask that this adapter does not provide.
    _NOT_PROVIDED = (("the leak record", "record_leaks"),
                     ("a run as delivered", "session_seed"),
                     ("the reverse order", "reverse_order"),
                     ("the timeout journal", "timeout_journal"),
                     ("the isolated configuration", "isolation_config"),
                     ("the removal of created files", "remove_created_files"))


    def _identity(self, request) -> la.ObservationIdentity:
        return la.ObservationIdentity(
            target=None, suite=request.suite.identity(), toolchain=TOOLCHAIN,
            instrument=None, execution=la.ExecutionIdentity("local"))

    def _functions(self) -> Dict[str, "tools.Function"]:
        """The functions of every source file of the project: its .toy files
        that are not test files."""
        functions: Dict[str, tools.Function] = {}
        for path in sorted(Path(self.context.root).rglob("*.toy")):
            if not path.name.startswith("test_"):
                functions.update(tools.load_source(path.read_text(encoding="utf-8")))
        return functions

    def _tests(self, suite) -> List[Tuple[str, "tools.Test"]]:
        """Each test of the suite with its file, relative to the suite's root.
        LoadError names a line that is not a test."""
        root = Path(suite.root)
        found = []
        for path in suite.files()[:len(suite.test_files)]:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
            try:
                found += [(relative, test) for test in tools.load_tests(
                    path.read_text(encoding="utf-8"))]
            except tools.LoadError as exc:
                raise tools.LoadError(exc.line, exc.message, relative) from None
        return found

    @staticmethod
    def _ended(outcome, reason, identity, wall=None) -> SuiteRun:
        return SuiteRun(outcome=outcome, reason=reason, counts=RunCounts(), tests=(),
                        unmatched=(), wall_time_s=wall, output="", coverage=None,
                        identity=identity)


    def run_suite(self, request: SuiteRunRequest) -> SuiteRun:
        """Run the suite's test files against the project's source files.

        Return each test's outcome (passed, failed or error) and the run's
        outcome: completed, test_failures, collection_failed when a test file
        does not load (the reason names its line) or no_tests. Why: a consumer
        reads each test's outcome to keep the passing tests and remove the
        failing ones. Test it: a passing and a failing test read passed and
        failed, and a file with a line that is not a test reads
        collection_failed."""
        request.validate()
        identity = self._identity(request)
        for name, field_name in self._NOT_PROVIDED:
            if getattr(request, field_name):
                return self._ended(la.Outcome.UNSUPPORTED,
                                   f"the Toy adapter does not provide {name}", identity)
        started = time.monotonic()
        try:
            functions = self._functions()
            tests = self._tests(request.suite)
        except tools.LoadError as exc:
            return self._ended(la.Outcome.COLLECTION_FAILED, str(exc), identity)
        if not tests:
            return self._ended(la.Outcome.NO_TESTS, "no test", identity,
                               wall=time.monotonic() - started)
        results, trace = tools.run([test for _, test in tests], functions)
        outcomes = [TestOutcome(la.TestId(file, test.name, text=f"{file}::{test.name}"),
                                {"passed": PASSED, "failed": FAILED}.get(outcome, ERROR),
                                None, message)
                    for (file, _), (test, outcome, message) in zip(tests, results)]
        coverage = self._coverage(request, functions, trace)
        failing = [t for t in outcomes if t.outcome in (FAILED, ERROR)]
        counts = RunCounts(collected=len(outcomes),
                           passed=sum(t.outcome == PASSED for t in outcomes),
                           failed=sum(t.outcome == FAILED for t in outcomes),
                           errors=sum(t.outcome == ERROR for t in outcomes), skipped=0)
        return SuiteRun(
            outcome=la.Outcome.TEST_FAILURES if failing else la.Outcome.COMPLETED,
            reason=f"{len(failing)} of {len(outcomes)} tests did not pass" if failing else "",
            counts=counts, tests=tuple(outcomes), unmatched=(),
            wall_time_s=time.monotonic() - started, output="", coverage=coverage,
            identity=identity)


    def _coverage(self, request, functions, trace) -> Optional[CoverageObservation]:
        """Coverage, in run_suite, when the request names a coverage_target:
        that file's statements (one per function) and the ones the run
        evaluated, its branches (two per conditional) and the ones it took.
        Why: a consumer measures how much of the code the tests reach.
        Test it: a run of the example with its source as the target reports
        a covered line."""
        if not request.coverage_target:
            return None
        return self._observed(request.coverage_target, functions, trace)

    def _observed(self, name, functions, trace) -> CoverageObservation:
        """The coverage of the source file ``name`` in ``trace``."""
        root = Path(self.context.root)
        target = root / name
        own = tools.load_source(target.read_text(encoding="utf-8"))
        relative = target.relative_to(root).as_posix()
        statements = tuple(sorted(f.line for f in own.values()))
        branches = tuple(BranchItem(relative, line, part) for line, part in tools.branches(own))
        return CoverageObservation(
            target=FileIdentity.of(target, root), statements=statements,
            covered=tuple(n for n in statements if n in trace.lines), branches=branches,
            taken=tuple(b for b in branches if (b.source, b.destination) in trace.branches))


    @staticmethod
    def _kills(mutation_run):
        """Whether ``mutation_run``'s kills are usable, its killers that name no
        test, and each test's kill items: none without a mutation run."""
        if mutation_run is None:
            return KILLS_NOT_REQUESTED, (), {}
        unmapped = mutation_run.unmatched_killers()
        killed = {}
        for mutant in () if unmapped else mutation_run.mutants:
            for test in mutant.killing_tests:
                killed.setdefault(test, set()).add(KillItem(str(mutant.row.get("id"))))
        return (KILLS_UNUSABLE if unmapped else KILLS_USABLE), unmapped, killed

    def per_test_evidence(self, request: PerTestEvidenceRequest) -> PerTestEvidence:
        """Each test's own coverage: the target's lines and branches the test
        evaluates when it runs alone, its time, and, with a mutation run, the
        mutants it kills. A suite that is not green carries no evidence and
        names each test that does not pass alone. Why:
        a consumer reduces a suite to the fewest tests that keep what it
        covered and killed. Test it: each example test reports the lines it
        covers."""
        request.validate()
        identity = self._identity(request)
        refused = KILLS_UNUSABLE if request.mutation_run is not None else KILLS_NOT_REQUESTED

        def ended(outcome, reason, stopping=()) -> PerTestEvidence:
            return PerTestEvidence(outcome, reason, (), (), (), refused, (), None, None, identity,
                                   stopping_tests=tuple(stopping))

        for name, field_name in self._NOT_PROVIDED:
            if getattr(request, field_name, None):
                return ended(la.Outcome.UNSUPPORTED, f"the Toy adapter does not provide {name}")
        try:
            functions = self._functions()
            tests = self._tests(request.suite)
        except tools.LoadError as exc:
            return ended(la.Outcome.COLLECTION_FAILED, str(exc))
        if not tests:
            return ended(la.Outcome.NO_TESTS, "no test")
        kills, unmapped, killed = self._kills(request.mutation_run)
        evidence, suite, stopping, first = [], tools.Trace(), [], ""
        started = time.monotonic()
        for file, test in tests:
            began = time.monotonic()
            [(_, outcome, message)], trace = tools.run([test], functions)
            test_id = la.TestId(file, test.name, text=f"{file}::{test.name}")
            if outcome != "passed":
                # Every test runs, and each that does not pass alone is named as
                # the suite run names it, so a consumer can remove exactly those.
                first = first or f"{test.name} did not pass: {message}"
                stopping.append(test_id)
                continue
            suite.lines |= trace.lines
            suite.branches |= trace.branches
            own = self._observed(request.target, functions, trace)
            items = {LineItem(own.target.path, line) for line in own.covered} | set(own.taken)
            evidence.append(TestEvidence(test_id, frozenset(items | killed.get(test_id, set())),
                                         1000.0 * (time.monotonic() - began)))
        if stopping:
            return ended(la.Outcome.TEST_FAILURES, first, stopping)
        return PerTestEvidence(
            la.Outcome.COMPLETED, "", tuple(evidence), (), (), kills, unmapped,
            1000.0 * (time.monotonic() - started),
            self._observed(request.target, functions, suite), identity)


    #: The capabilities this step provides.
    _PROVIDED = frozenset({"suite_runs", "coverage", "per_test_evidence"})

    def capabilities(self) -> la.CapabilityReport:
        """What runs here, all in this Python: the capabilities this step
        provides are available, the others unsupported."""
        return la.CapabilityReport(
            language=self.language, interpreter="the Toy's tools",
            outcome=la.Outcome.COMPLETED, reason="", runtime=None,
            capabilities=tuple(
                la.Capability(name, la.Availability.AVAILABLE) if name in self._PROVIDED
                else la.Capability(name, la.Availability.UNSUPPORTED,
                                   "the Toy adapter does not provide it")
                for name in CAPABILITIES),
            tools=(), branch_coverage="counters", mutation_engines=())

    def language_facts(self) -> la.LanguageFacts:
        return la.LanguageFacts(
            language=self.language, test_framework=self.test_framework,
            code_fence=self.code_fence, runtime_version=None,
            test_file_patterns=("test_*.toy",),
            test_function_pattern="test <name>: <left> == <right>",
            test_class_pattern=None, timeout_marker=None)

    def test_vocabulary(self) -> Dict[str, str]:
        """The words for the Toy's tests (``langadapter.vocabulary``), which a
        consumer can put into a model's input. Why: the defaults are general
        sentences, and the one-file rule is the entry that says what a test
        file looks like, so it gives the Toy's test form and one example test.
        Test it: every entry is text."""
        return {"single_file_rule": (
            "Produce one test file, ``test_<target_stem>.toy``. Write one test "
            "per line in the form ``test <name>: <left> == <right>``, such as "
            "``test one: f(1) == 2``. Do not create helper files.")}

    @classmethod
    def import_statement(cls, target: str, root: str) -> str:
        """The import hint for a first test of ``target``, which a consumer
        shows a model. Why: a Toy test calls the target's functions by name,
        and a consumer without a hint asks for the language's import
        conventions, which invites an import line the loader refuses. Test
        it: the hint says that no import line is needed."""
        return "none; a Toy test calls the target's functions by name, without an import line"


    def test_units(self, source: str):
        """Each test of the test file ``source`` with its span, or None when a
        line is not a test. LangAdapter builds removing named tests, counting
        tests and naming duplicate tests on it. Why: a consumer removes a
        failing test instead of the whole file, and refuses a repair that
        loses a test. Test it: removing one named test leaves the others, and
        the count drops by one."""
        try:
            tests = tools.load_tests(source)
        except tools.LoadError:
            return None
        starts = [0]
        for line in source.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))
        return [TestUnit(test.name, starts[test.line - 1], starts[test.line])
                for test in tests]

# LangAdapter reads this name from the file to register the language.
langadapter_language = ToyAdapter
