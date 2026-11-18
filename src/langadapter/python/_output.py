"""Readers of pytest's output text: the outcome and the diagnostic of a
collection, the output without the run's own report line, the warnings that
make a passing run unsound (an unawaited coroutine, a destroyed pending task,
an unraisable exception), the counts of pytest's closing summary, the kind of
a failed run and a short excerpt of its output. Whether a warning turns a run
red is the caller's decision.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

from langadapter.outcomes import Outcome


def collection_outcome(returncode: int, output: str) -> Tuple[Outcome, str]:
    """The outcome and reason of a collect-only process, never a test verdict."""
    if returncode == 0:
        return Outcome.COMPLETED, ""
    if returncode == 5:
        return Outcome.NO_TESTS, "no tests collected"
    if returncode == 2 and "ERROR collecting" in output:
        return Outcome.COLLECTION_FAILED, "pytest reported a collection error"
    return Outcome.INFRASTRUCTURE_FAILURE, f"collection exited with {returncode}"


def collection_diagnostic(output: str) -> str:
    """A short summary of a collection error from pytest's output: the last
    three exception lines, else the last collection-error header, else the
    last three non-blank lines."""
    exceptions = [line.strip() for line in output.splitlines()
                  if line.startswith("E   ") and line.strip() != "E"]
    if exceptions:
        return " | ".join(exceptions[-3:])
    headers = [line.strip() for line in output.splitlines()
               if "ERROR" in line and "collect" in line.lower()]
    if headers:
        return headers[-1]
    return " | ".join([line.strip() for line in output.splitlines()
                       if line.strip()][-3:])


_REPORT_LINE = re.compile(r"-+ generated xml file: (.*) -+\s*$")


def without_report_line(output: str, report: str) -> str:
    """``output`` without the line pytest prints for ``report``, the run's JUnit
    report, so it reads as a run's without a report. A test's own lookalike
    line stays: the line must name the report's own directory and file, unique to
    the run (in either slash style and letter case)."""
    parts = report.replace("\\", "/").rstrip("/").split("/")
    own = "/".join(parts[-2:]).lower()

    def printed(line: str) -> bool:
        match = _REPORT_LINE.match(line)
        return bool(match) and match.group(1).replace("\\", "/").lower().endswith(own)

    return "".join(line for line in output.splitlines(keepends=True) if not printed(line))

#: The warnings that mark a test leaving shared state behind: a coroutine
#: never awaited, a pending task destroyed, an exception that could not be
#: raised. A suite run's record plugin records the tests of their warnings.
UNSOUND_TEST_PATTERNS = (
    r"coroutine '[^']+' was never awaited",
    r"Task was destroyed but it is pending!",
    r"PytestUnraisableExceptionWarning",
)


def unsound_test_warnings(pytest_output: str) -> List[str]:
    """The distinct unsoundness warnings of ``pytest_output``, by kind in the
    order of ``UNSOUND_TEST_PATTERNS`` (unawaited coroutine, destroyed pending task,
    unraisable exception), then by first appearance."""
    if not pytest_output:
        return []
    found: List[str] = []
    seen: Set[str] = set()
    for pattern in UNSOUND_TEST_PATTERNS:
        for match in re.finditer(pattern, pytest_output):
            text = match.group(0)
            if text not in seen:
                seen.add(text)
                found.append(text)
    return found


def recorded_unsound_tests(recorded) -> List[Tuple[str, str]]:
    """``(test node id, warning)`` for each unsoundness warning a run record
    names a test for. ``recorded`` holds the record's ``[node id, text]``
    pairs in the order pytest reported them. The pairs follow pytest's
    warnings summary: grouped by text in the order each text first appears,
    within a group in the reported order. Each line of a text gives its
    first match in the order of ``UNSOUND_TEST_PATTERNS``, and each pair of a
    test and a match comes once."""
    groups: Dict[str, List[str]] = {}
    for nodeid, text in recorded:
        groups.setdefault(text, []).append(nodeid)
    patterns = [re.compile(p) for p in UNSOUND_TEST_PATTERNS]
    results: List[Tuple[str, str]] = []
    for text, nodeids in groups.items():
        for nodeid in nodeids:
            for line in text.splitlines():
                match = next(filter(None, (pattern.search(line) for pattern in patterns)), None)
                if match is not None and (nodeid, match.group(0)) not in results:
                    results.append((nodeid, match.group(0)))
    return results


#: pytest's closing summary line, between optional rules: its outcome
#: counts (or "no tests ran") and the duration.
_SUMMARY = re.compile(
    r"^=*\s*((?:\d+ [a-z]+, )*\d+ [a-z]+|no tests ran) in [\d.]+(?:s| seconds)\b.*$",
    re.MULTILINE)
#: pytest's own line naming the collected items, before any test runs.
_COLLECTED = re.compile(r"^(?:collecting \.\.\. )?collected (\d+) items?", re.MULTILINE)
#: The files pytest could not collect: its section header and its short
#: summary's line naming a file, not a test.
_COLLECTION_ERRORS = (re.compile(r"^_+ ERROR collecting (\S+) _+$", re.MULTILINE),
                      re.compile(r"^ERROR ([^\s:]+)(?: - .*)?$", re.MULTILINE))
#: Terminal color codes, which a forced color setting adds.
_COLOR = re.compile(r"\x1b\[[0-9;]*m")
_OUTCOMES = {"passed": "passed", "failed": "failed", "error": "errors", "errors": "errors"}


def pytest_counts(output) -> Dict[str, int]:
    """The test counts of pytest's text as pytest counts them: the outcomes of
    the last line shaped like its closing summary (not a test's own "3 passed")
    and the ``collected N items`` count, else the outcomes' sum less one per
    file pytest could not collect. A collection error counts as an error, not a
    collected test, a failing test with a failing teardown as a failure and an
    error, and an unexpected pass of a non-strict xfail as neither."""
    text = _COLOR.sub("", output or "")
    counts = dict.fromkeys(("passed", "failed", "errors"), 0)
    summaries = _SUMMARY.findall(text)
    if summaries:
        for number, word in re.findall(r"(\d+) ([a-z]+)", summaries[-1]):
            if word in _OUTCOMES:
                counts[_OUTCOMES[word]] = int(number)
    collected = _COLLECTED.search(text)
    if collected is not None:
        return {"collected": int(collected.group(1)), **counts}
    broken = {name for pattern in _COLLECTION_ERRORS for name in pattern.findall(text)}
    return {"collected": max(sum(counts.values()) - len(broken), 0), **counts}


def _pytest_failure_kind(output: str, returncode: int) -> str:
    """``test_failure`` for exit 1 with failed or error tests in pytest's
    closing summary (``pytest_counts``, not text a test printed),
    else ``infrastructure``: coverage can exit 1 after every test passed,
    when a test patched a builtin coverage uses at shutdown.
    """
    counts = pytest_counts(output)
    if returncode == 1 and (counts["failed"] or counts["errors"]):
        return "test_failure"
    return "infrastructure"


def _compact_pytest_output(full_output: str, returncode: int) -> str:
    """The summary, failure and error lines of pytest output, for a
    benchmark's error report. Without any, ``all tests passed`` for exit
    status 0, else the last 800 characters with the exit status.
    """
    lines = full_output.split("\n")
    keep: List[str] = []
    in_failure = False
    for line in lines:
        if re.search(r"\d+\s+(passed|failed|error)", line):
            keep.append(line)
            continue
        if (line.startswith("FAILED") or line.startswith("ERROR")
                or "FAILURES" in line or "ERRORS" in line):
            in_failure = True
            keep.append(line)
            continue
        if in_failure:
            if line.startswith("=") and "short test summary" in line.lower():
                in_failure = False
                keep.append(line)
            elif line.startswith("=") and len(line) > 3:
                in_failure = False
            else:
                keep.append(line)
            continue
        if "collection" in line.lower() and "error" in line.lower():
            keep.append(line)
        if ("ModuleNotFoundError" in line or "ImportError" in line
                or "SyntaxError" in line):
            keep.append(line)
    output = "\n".join(keep).strip()
    if not output:
        if returncode == 0:
            output = "all tests passed"
        else:
            output = full_output[-800:].strip()
            output = f"tests failed (exit code {returncode}):\n{output}"
    elif returncode != 0 and "FAILED" not in output.upper():
        output += f"\n\ntests failed (exit code {returncode})"
    return output
