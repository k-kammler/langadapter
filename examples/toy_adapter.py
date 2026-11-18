"""An adapter for a made-up language, the Toy, registered from outside the
package: step 1 of the Toy tutorial (langadapter tutorial), which runs
the tests.

The facts name the language and its files. run_suite runs a suite with
the Toy's own tools and reports each test's outcome, and the run's
outcome when a test file does not load. The tutorial's later steps add test
units, coverage, mutation testing, load repair and the version declaration.

Run it on a directory of ``test_*.toy`` files, or check it against the
shared adapter contract on such a directory::

    python examples/toy_adapter.py <directory>
    python examples/toy_adapter.py --check <directory>
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import langadapter as la
from langadapter.runs import (
    ERROR, FAILED, PASSED, RunCounts, SuiteRun, SuiteRunRequest, TestOutcome,
)
from langadapter.capabilities import CAPABILITIES
from langadapter.tutorial.example import tools
from langadapter.versions import Version

#: The Toy's tools are the Toy's toolchain, version 1.
TOOLCHAIN = la.ToolchainIdentity.of(Version("toy", (1,), "1", "context"))


class ToyAdapter(la.LangAdapter):
    """The Toy language's adapter at step 1 of the tutorial."""

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

    #: What a request can ask that this adapter does not provide yet.
    _NOT_PROVIDED = (("coverage", "coverage_target"),
                     ("the leak record", "record_leaks"), ("a run as delivered", "session_seed"),
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
        does not load (the reason names its line) or no_tests. Why: a
        consumer reads each test's outcome to keep the passing tests and
        remove the failing ones. Required for running tests. Test it: a
        passing and a failing test read passed and failed, and a file with a
        line that is not a test reads collection_failed."""
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


    def _coverage(self, request, functions, trace) -> None:
        """No coverage yet: step 3 adds it."""
        return None


    #: The capabilities this step provides.
    _PROVIDED = frozenset({"suite_runs"})

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
            tools=(), branch_coverage="none", mutation_engines=())

    def language_facts(self) -> la.LanguageFacts:
        return la.LanguageFacts(
            language=self.language, test_framework=self.test_framework,
            code_fence=self.code_fence, runtime_version=None,
            test_file_patterns=("test_*.toy",),
            test_function_pattern="test <name>: <left> == <right>",
            test_class_pattern=None, timeout_marker=None)


def register() -> None:
    """Register the Toy adapter, once."""
    if "toy" not in la.names():
        la.register("toy", [".toy"], ToyAdapter)


def _project(directory: str) -> Tuple[la.ProjectContext, la.Suite]:
    context = la.ProjectContext(root=directory,
                                interpreter=la.Interpreter(path=sys.executable))
    files = sorted(p.relative_to(directory).as_posix()
                   for p in Path(directory).rglob("test_*.toy"))
    return context, la.Suite(root=directory, test_files=files)


def run(directory: str) -> SuiteRun:
    """Run every ``test_*.toy`` file under ``directory``."""
    register()
    context, suite = _project(directory)
    return la.create("toy", context).run_suite(SuiteRunRequest(suite=suite, timeout_s=60))


def check_contract(directory: str) -> Tuple[str, ...]:
    """The shared contract's problems of this adapter on ``directory``."""
    from langadapter.conformance import AdapterExample, check_adapter

    register()
    context, suite = _project(directory)
    return check_adapter(AdapterExample("toy", context, suite))


USAGE = """usage:
    python examples/toy_adapter.py <directory of test_*.toy files>
    python examples/toy_adapter.py --check <directory>"""


if __name__ == "__main__":
    if len(sys.argv) != (3 if sys.argv[1:2] == ["--check"] else 2):
        raise SystemExit(USAGE)
    if not Path(sys.argv[-1]).is_dir():
        raise SystemExit(f"{sys.argv[-1]} is no directory")
    if not any(Path(sys.argv[-1]).rglob("test_*.toy")):
        raise SystemExit(f"{sys.argv[-1]} holds no test_*.toy file")
    if sys.argv[1] == "--check":
        problems = check_contract(sys.argv[2])
        print("\n".join(problems) or "the Toy adapter keeps the adapter contract")
        sys.exit(1 if problems else 0)
    result = run(sys.argv[1])
    print(f"{result.outcome}: {result.reason or 'every test passed'}")
    for test in result.tests:
        print(f"  {test.outcome:7} {test.test}"
              + (f" ({test.message})" if test.message else ""))
    sys.exit(0 if result.outcome is la.Outcome.COMPLETED else 1)
