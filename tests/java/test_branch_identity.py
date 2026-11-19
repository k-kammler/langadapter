"""A Java branch item names the branch a test took, not a count of the line's
branches, so a reduction keeping every test's items keeps the suite's line and
branch coverage. The fixture has short-circuit conditions, a string switch and
a finally block (both rewritten by JaCoCo's filters), a lambda and a nested
class, with one test class per test so a reduced suite is a set of files."""
from __future__ import annotations

import os

import pytest

import langadapter as lb
from langadapter.runs import PerTestEvidenceRequest, SuiteRunRequest

TARGET = "src/main/java/demo/Shapes.java"

SHAPES = """\
package demo;

import java.util.function.IntUnaryOperator;

public final class Shapes {
    private static int calls;

    private Shapes() {
    }

    public static String kind(int a, int b) {
        if (a > 0 && b > 0) {
            return "both";
        }
        return a > 0 || b > 0 ? "one" : "none";
    }

    public static int code(String name) {
        switch (name) {
            case "red":
                return 1;
            case "green":
                return 2;
            default:
                return 0;
        }
    }

    public static int guarded(int x) {
        try {
            return 10 / x;
        } finally {
            if (x > 1) {
                calls++;
            }
        }
    }

    public static int apply(int x) {
        IntUnaryOperator op = v -> v > 0 ? v : -v;
        return op.applyAsInt(x);
    }

    static final class Inner {
        int twice(int x) {
            return x > 5 ? 2 * x : x;
        }
    }
}
"""

#: One test class per call: its name and the statement it runs.
CALLS = {
    "KindBoth": 'assertEquals("both", Shapes.kind(1, 1));',
    "KindFirst": 'assertEquals("one", Shapes.kind(1, 0));',
    "KindSecond": 'assertEquals("one", Shapes.kind(0, 1));',
    "KindNone": 'assertEquals("none", Shapes.kind(0, 0));',
    "CodeRed": 'assertEquals(1, Shapes.code("red"));',
    "CodeBlue": 'assertEquals(0, Shapes.code("blue"));',
    "Guarded": "assertEquals(5, Shapes.guarded(2));",
    "GuardedZero": "assertThrows(ArithmeticException.class, () -> Shapes.guarded(0));",
    "ApplyNegative": "assertEquals(3, Shapes.apply(-3));",
    "ApplyPositive": "assertEquals(3, Shapes.apply(3));",
    "InnerLarge": "assertEquals(14, new Shapes.Inner().twice(7));",
    "KindBothAgain": 'assertEquals("both", Shapes.kind(2, 3));',
    "ApplyNegativeAgain": "assertEquals(5, Shapes.apply(-5));",
}

TEST = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

class {name}Test {{
    @Test
    void call() {{
        {call}
    }}
}}
"""


def _adapter(project, jdks, maven, gradle, feature=21):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))


def _project(java_project, build):
    project = java_project(build)
    main = project / "src" / "main" / "java" / "demo"
    (main / "Shapes.java").write_text(SHAPES, encoding="utf-8", newline="\n")
    tests = project / "src" / "test" / "java" / "demo"
    for name, call in CALLS.items():
        (tests / f"{name}Test.java").write_text(
            TEST.format(name=name, call=call), encoding="utf-8", newline="\n")
    return project


def _suite(project, names):
    return lb.Suite(str(project / "src" / "test" / "java"),
                    tuple(f"demo/{name}Test.java" for name in names))


def _branches(coverage):
    return {(b.source, b.destination) for b in coverage.taken}


def _cover(tokens):
    """A greedy cover of every test's items, ties broken by name."""
    needed = set().union(*tokens.values())
    chosen = []
    while needed:
        best = max(sorted(tokens), key=lambda t: len(tokens[t] & needed))
        chosen.append(best)
        needed -= tokens[best]
    return chosen


def test_a_reduction_over_branch_items_keeps_the_suites_coverage(
        java_project, jdks, maven, gradle):
    found = {}
    for build in ("maven", "gradle"):
        project = _project(java_project, build)
        adapter = _adapter(project, jdks, maven, gradle)
        evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
            _suite(project, CALLS), TARGET, timeout_s=300))
        assert evidence.outcome is lb.Outcome.COMPLETED, evidence.reason
        tokens = {e.test.qualname.split("Test.")[0]: set(e.tokens())
                  for e in evidence.tests}
        assert set(tokens) == set(CALLS)
        full = adapter.run_suite(SuiteRunRequest(
            _suite(project, CALLS), timeout_s=300, coverage_target=TARGET))
        assert full.outcome is lb.Outcome.COMPLETED, full.reason
        # The suite run and the evidence name the same branches, and the
        # tests' items together are the suite's.
        assert _branches(full.coverage) == _branches(evidence.coverage)
        assert {f"B:{TARGET}:{s}->{d}" for s, d in _branches(full.coverage)} == {
            t for found_items in tokens.values() for t in found_items
            if t.startswith("B:")}
        # kind(1, 0) alone takes the first condition's true branch of the
        # return line. JaCoCo's counters would give it no item of its own.
        others = set().union(*(t for n, t in tokens.items() if n != "KindFirst"))
        assert {t for t in tokens["KindFirst"] - others if t.startswith("B:")}
        kept = _cover(tokens)
        assert "KindFirst" in kept and len(kept) == len(CALLS) - 2
        reduced = adapter.run_suite(SuiteRunRequest(
            _suite(project, kept), timeout_s=300, coverage_target=TARGET))
        assert reduced.outcome is lb.Outcome.COMPLETED, reduced.reason
        assert reduced.coverage.covered == full.coverage.covered
        assert _branches(reduced.coverage) == _branches(full.coverage)
        assert len(full.coverage.taken) < len(full.coverage.branches)
        found[build] = (tokens, _branches(full.coverage))
    assert found["maven"] == found["gradle"]


SWITCH = """\
package demo;

public final class Shapes {
    private Shapes() {
    }

    public static int code(String s) {
        switch (s) {
            case "Aa":
                return 1;
            case "BB":
                return 2;
            case "C":
                return 3;
            default:
                return 0;
        }
    }
}
"""
SWITCH_LINE = 8
SWITCH_CALLS = {"Aa": 1, "BB": 2, "C": 3, "X": 0}


def test_a_tests_branch_items_do_not_depend_on_the_runs_analyzed_with_it(
        java_project, jdks, maven, gradle, monkeypatch):
    """Recovered branches that one run's coverage contradicts are dropped for
    that run alone, so a test's items do not depend on the other tests. The
    family is altered to contradict only the run of "C" (its branch added to
    the default's set), so only that run keeps the line's counters."""
    import shutil
    import uuid

    from langadapter.java import _jacoco
    from langadapter.java import _tools

    monkeypatch.setattr(_jacoco, "IDENTITY", f"probe-family-test-{uuid.uuid4().hex}")
    places = []
    analyze = _jacoco.analyze

    def recorded(*args, **kwargs):
        places.append(_tools.cache_dir(kwargs["environment"]) / "identity"
                      / f"jacoco-{_jacoco.VERSION}-{_jacoco.IDENTITY}")
        return analyze(*args, **kwargs)

    monkeypatch.setattr(_jacoco, "analyze", recorded)
    project = java_project("maven")
    (project / "src/main/java/demo/Shapes.java").write_text(SWITCH, encoding="utf-8",
                                                            newline="\n")
    for name, expected in SWITCH_CALLS.items():
        call = f'assertEquals({expected}, Shapes.code("{name}"));'
        (project / f"src/test/java/demo/Code{name}Test.java").write_text(
            TEST.format(name=f"Code{name}", call=call), encoding="utf-8", newline="\n")
    adapter = _adapter(project, jdks, maven, gradle)
    names = [f"Code{name}" for name in SWITCH_CALLS]

    def items(chosen):
        evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
            _suite(project, chosen), TARGET, timeout_s=300))
        assert evidence.outcome is lb.Outcome.COMPLETED, evidence.reason
        return {e.test.qualname.split("Test.")[0]: {t for t in e.tokens() if t.startswith("B:")}
                for e in evidence.tests}

    try:
        assert items(names) == {f"Code{name}": {f"B:{TARGET}:{SWITCH_LINE}->{i}"}
                                for i, name in enumerate(SWITCH_CALLS)}
        cache, = set(places)
        family, = cache.glob("*.txt")
        rows = family.read_text(encoding="utf-8").splitlines()
        for i, row in enumerate(rows):
            line, sets = row.split("\t")
            if int(line) == SWITCH_LINE:
                parts = sets.split(";")
                parts[3] = ",".join(sorted(set(parts[3].split(",")) | set(parts[2].split(",")),
                                           key=int))
                rows[i] = f"{line}\t{';'.join(parts)}"
        family.write_text("\n".join(rows) + "\n", encoding="utf-8")
        together = items(names)
        assert together["CodeBB"] == items(["CodeBB"])["CodeBB"] == {
            f"B:{TARGET}:{SWITCH_LINE}->1"}
        assert together["CodeC"] == {f"B:{TARGET}:{SWITCH_LINE}->0"}
    finally:
        for place in set(places):
            shutil.rmtree(place, ignore_errors=True)
