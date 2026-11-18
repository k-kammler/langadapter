"""Run a project's pytest suite with the Python adapter and print what it
observed: the outcome and its reason, the counts, every test that did not
pass and the line coverage of one target.

The project's interpreter must have pytest installed, and coverage when a
target is given. The capability report says so before anything runs. The
adapter adds or changes no file of the project: its side effects are off unless
a request asks for them, and Python compiles into a cache directory of the
run's own. The tests may write files of their own.

    python examples/python_suite.py <project root> <test file> [<target>]

The test file and the target are paths under the project root.
"""
from __future__ import annotations

import sys
from typing import Optional

import langadapter as la
from langadapter.runs import SuiteRun, SuiteRunRequest


def run(project: str, test_file: str, target: Optional[str] = None,
        interpreter: str = sys.executable) -> SuiteRun:
    """The suite run of ``test_file``, with the coverage of ``target``."""
    context = la.ProjectContext(root=project,
                                interpreter=la.Interpreter(path=interpreter))
    adapter = la.create("python", context)
    needed = adapter.capabilities().capability(
        "coverage" if target else "suite_runs")
    if not needed.available:
        raise SystemExit(f"{needed.name} is {needed.availability}: "
                         f"{needed.reason}")
    suite = la.Suite(root=project, test_files=(test_file,))
    return adapter.tests.run_suite(SuiteRunRequest(
        suite=suite, timeout_s=600, coverage_target=target))


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    result = run(*sys.argv[1:4])
    counts = result.counts
    print(f"{result.outcome}: {counts.passed} passed, {counts.failed} failed, "
          f"{counts.errors} errors of {counts.collected}")
    if result.reason:
        print(f"reason: {result.reason}")
    for test in result.tests:
        if test.outcome != "passed":
            print(f"  {test.outcome:7} {test.test}: {test.message[:200]}")
    coverage = result.coverage
    if coverage is not None and coverage.line_percent is not None:
        print(f"line coverage of {coverage.target.path}: "
              f"{coverage.line_percent:.1f}% "
              f"(missing lines {list(coverage.missing())})")
    sys.exit(0 if result.outcome is la.Outcome.COMPLETED else 1)
