"""Java mutation through PIT: the fixture's kills and survivors in the
common classes, one kill matrix for Maven and Gradle and for JUnit 4, 5 and
6, PIT's test names as the suite runs' test identities, and run failures.

PIT's default mutators give the fixture's ``Calc`` 7 mutants. The suite
kills the negated conditionals and replaced returns but never calls
``sign(0)``, so the two conditional boundaries (lines 8 and 11) survive."""
from __future__ import annotations

import os

import pytest

import langadapter as lb
from langadapter.mutation import (
    MutationClass, DEFAULT_IMPLEMENTATION, BudgetedSuite, PerSuiteRequest, _verdict_of,
    Catalogue, MutationRequest, TimeoutRule,
)
from langadapter.runs import SuiteRunRequest

TARGET = "src/main/java/demo/Calc.java"
BOUNDARY = "org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator"
NEGATE = "org.pitest.mutationtest.engine.gregor.mutators.NegateConditionalsMutator"

MORE = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class CalcMoreTest {
    @ParameterizedTest
    @ValueSource(ints = {3, 7})
    void positives(int value) {
        assertEquals(1, Calc.sign(value));
    }

    @Nested
    class Clamping {
        @Test
        void keepsInside() {
            assertEquals(5, Calc.clamp(5, 0, 10));
        }
    }
}
"""

LOOP = """\
package demo;

public final class Loop {
    private Loop() {
    }

    public static long count(long n) {
        long i = 0;
        while (i < n) {
            i++;
        }
        return i;
    }
}
"""

LOOP_TEST = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class LoopTest {
    @Test
    void counts() {
        assertEquals(3, Loop.count(3));
    }
}
"""

RED = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcRedTest {
    @Test
    void wrong() {
        assertEquals(2, Calc.sign(5));
    }
}
"""


def _adapter(project, jdks, maven, gradle, feature=21):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))


def _write(project, name, text, main=False):
    base = project / "src" / ("main" if main else "test") / "java" / "demo"
    (base / name).write_text(text, encoding="utf-8", newline="\n")
    return f"demo/{name}"


def _request(project, *files, target=TARGET, **settings):
    return MutationRequest(
        target=target,
        suite=lb.Suite(str(project / "src" / "test" / "java"),
                       files or ("demo/CalcTest.java",)),
        **settings)


def _mutate(adapter, request, workers=1):
    assert adapter.mutation_engines() == ("pit",)
    with adapter.open_mutation_session("pit", workers=workers) as session:
        return session.run(request)


RANGE = """\
package demo;

public final class Range {
    private Range() {
    }

    public static boolean inside(int x, int lo, int hi) {
        return x >= lo && x <= hi;
    }
}
"""

RANGE_TESTS = {
    "RangeTest.java": """\
package demo;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

class RangeTest {
    @Test
    void middle() {
        assertTrue(Range.inside(5, 0, 10));
    }
}
""",
    "RangeEdgeTest.java": """\
package demo;

import static org.junit.jupiter.api.Assertions.assertFalse;

import org.junit.jupiter.api.Test;

class RangeEdgeTest {
    @Test
    void below() {
        assertFalse(Range.inside(-1, 0, 10));
    }
}
""",
}


@pytest.mark.mutation_tool
def test_each_pit_mutant_has_one_identity_across_runs(java_project, jdks, maven, gradle):
    """Mutants of one operator, line and text differ by their key's
    occurrence, and runs with different suites give each the same key."""
    project = java_project("maven")
    target = _write(project, "Range.java", RANGE, main=True)
    adapter = _adapter(project, jdks, maven, gradle)
    keys = []
    for name, text in RANGE_TESTS.items():
        run = _mutate(adapter, _request(project, _write(project, name, text),
                                        target=f"src/main/java/{target}"))
        assert run.outcome is lb.Outcome.COMPLETED, run.reason
        assert len({m.key for m in run.mutants}) == len(run.mutants)
        keys.append({m.key: m.engine_id for m in run.mutants})
        rows = run.to_kill_matrix()["mutants"]
        assert len({(r["key"]["file"], r["key"]["operator"], r["key"]["occurrence"])
                    for r in rows}) == len(rows)
        # PIT's own id is stable across runs, so it is the shown id.
        assert all(r["mutant_id"] == r["id"] for r in rows)
        assert all(m.mutant_id == m.engine_id for m in run.mutants)
    shared = [m for m in run.mutants
              if sum((o.key.operator, o.key.line, o.diff)
                     == (m.key.operator, m.key.line, m.diff) for o in run.mutants) > 1]
    assert shared, "the fixture has mutants of one operator, line and text"
    assert keys[0] == keys[1]


def _verdicts(run):
    return {m.engine_id: (m.status, m.classification.mutation_class,
                          m.classification.subcategory,
                          tuple(sorted(t.qualname for t in m.killing_tests)))
            for m in run.mutants}


@pytest.mark.mutation_tool
def test_pit_kills_and_survivors_are_alike_for_both_builds(
        java_project, jdks, maven, gradle):
    matrices = {}
    for build in ("maven", "gradle"):
        project = java_project(build)
        run = _mutate(_adapter(project, jdks, maven, gradle),
                      _request(project, attribution="all"))
        assert run.outcome is lb.Outcome.COMPLETED, run.reason
        assert run.complete and run.reason == ""
        counts = run.counts()
        assert counts == {MutationClass.DETECTED: 5, MutationClass.UNDETECTED: 2,
                          MutationClass.NOT_RUN: 0}
        assert run.score() == pytest.approx(500 / 7)
        assert {(m.key.line, m.key.operator) for m in run.survivors()} == {
            (8, BOUNDARY), (11, BOUNDARY)}
        assert all(m.classification.subcategory == "survived"
                   and m.killers == () for m in run.survivors())
        negated = next(m for m in run.mutants
                       if (m.key.line, m.key.operator) == (8, NEGATE))
        assert negated.classification.subcategory == "failing_test"
        assert {t.qualname for t in negated.killing_tests} == {
            "CalcTest.negative", "CalcTest.positive"}
        assert all(t.file == "demo/CalcTest.java" for t in negated.killing_tests)
        assert set(negated.killers) == {"demo.CalcTest#negative()",
                                        "demo.CalcTest#positive()"}
        assert run.unmatched_killers() == ()
        assert run.identity.observation.target.path == TARGET
        assert dict(run.identity.observation.toolchain.tools)["pitest"] == "1.30.0"
        assert run.identity.attribution == "all"
        assert run.settings["catalogue"].source == "default"
        assert set(run.requested_operators) >= {BOUNDARY, NEGATE}
        assert {m.key.operator for m in run.mutants} <= set(run.requested_operators)
        # The diff is the source line, then PIT's description of the change.
        source = (project / TARGET).read_text(encoding="utf-8").splitlines()
        assert all(m.diff.splitlines()[0]
                   == f"{TARGET}:{m.key.line}: {source[m.key.line - 1].strip()}"
                   and m.diff.count("\n") == 1 for m in run.mutants)
        assert all("Mutator in " in m.diff.splitlines()[1] for m in run.mutants)
        matrices[build] = run.to_kill_matrix()
    assert matrices["maven"] == matrices["gradle"]


@pytest.mark.mutation_tool
def test_first_attribution_names_one_killer_and_the_same_verdicts(
        java_project, jdks, maven, gradle):
    """The first killer is the first by test name, whatever the times:
    ``CalcMoreTest.positives`` before ``CalcTest.positive``."""
    project = java_project("maven")
    more = _write(project, "CalcMoreTest.java", MORE)
    adapter = _adapter(project, jdks, maven, gradle)
    first = _mutate(adapter, _request(project, "demo/CalcTest.java", more))
    every = _mutate(adapter, _request(project, "demo/CalcTest.java", more,
                                      attribution="all"))
    assert first.identity.attribution == "first"
    assert all(len(m.killers) == 1 for m in first.mutants
               if m.status == "killed")
    assert ({k: v[:3] for k, v in _verdicts(first).items()}
            == {k: v[:3] for k, v in _verdicts(every).items()})
    killers = {m.key: m.killers for m in every.mutants}
    shared = 0
    for mutant in first.mutants:
        if mutant.status == "killed":
            assert mutant.killers[0] == min(killers[mutant.key])
            shared += len(killers[mutant.key]) > 1
    assert shared


@pytest.mark.mutation_tool
@pytest.mark.parametrize(("junit", "build"), [("junit4", "gradle"),
                                              ("junit6", "maven")])
def test_junit_4_and_6_suites_mutate_like_junit_5(
        java_project, jdks, maven, gradle, junit, build):
    reference = java_project("maven")
    expected = _verdicts(_mutate(_adapter(reference, jdks, maven, gradle),
                                 _request(reference, attribution="all")))
    project = java_project(build, junit=junit)
    run = _mutate(_adapter(project, jdks, maven, gradle),
                  _request(project, attribution="all"))
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert _verdicts(run) == expected
    platform = dict(run.identity.observation.toolchain.tools)["junit-platform"]
    assert platform.startswith("6." if junit == "junit6" else "1.")


@pytest.mark.mutation_tool
def test_parameterized_and_nested_killers_are_the_suite_runs_tests(
        java_project, jdks, maven, gradle):
    project = java_project("gradle")
    more = _write(project, "CalcMoreTest.java", MORE)
    adapter = _adapter(project, jdks, maven, gradle)
    suite_run = adapter.run_suite(SuiteRunRequest(
        suite=lb.Suite(str(project / "src" / "test" / "java"),
                       ("demo/CalcTest.java", more)), timeout_s=300))
    assert suite_run.outcome is lb.Outcome.COMPLETED, suite_run.output
    run = _mutate(adapter, _request(project, "demo/CalcTest.java", more,
                                    attribution="all"))
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    killers = {t for m in run.mutants for t in m.killing_tests}
    assert killers <= {t.test for t in suite_run.tests}
    assert {(t.qualname, t.params) for t in killers} >= {
        ("CalcMoreTest.positives", "[1]"), ("CalcMoreTest.positives", "[2]"),
        ("CalcMoreTest.Clamping.keepsInside", None)}
    assert run.unmatched_killers() == ()


@pytest.mark.mutation_tool
@pytest.mark.parametrize(("timeout", "source", "inputs"), [
    (None, "default", {"rule": "pit", "factor": 1.25, "constant_s": 4}),
    (TimeoutRule(factor=2.0, minimum_s=3), "request",
     {"rule": "pit", "factor": 2.0, "constant_s": 3}),
    (3, "request", {"rule": "fixed", "factor": 0.0, "constant_s": 3}),
])
def test_a_looping_mutant_is_detected_by_pits_timeout_rule(
        java_project, jdks, maven, gradle, timeout, source, inputs):
    """PIT bounds each test by its normal time times the factor plus the
    constant. A caller's rule gives the factor and its minimum as the
    constant, a fixed value bounds each test."""
    project = java_project("maven")
    _write(project, "Loop.java", LOOP, main=True)
    loop_test = _write(project, "LoopTest.java", LOOP_TEST)
    adapter = _adapter(project, jdks, maven, gradle)
    run = _mutate(adapter, _request(project, loop_test,
                                    target="src/main/java/demo/Loop.java",
                                    per_mutant_timeout=timeout))
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    setting = run.settings["per_mutant_timeout"]
    assert (setting.value, setting.source, dict(setting.inputs)) == (
        inputs["constant_s"], source, inputs)
    timed_out = [m for m in run.mutants if m.status == "timeout"]
    assert timed_out, [(m.engine_id, m.test_outcome) for m in run.mutants]
    assert all(m.test_outcome == "TIMED_OUT"
               and m.classification.subcategory == "timeout"
               for m in timed_out)


LOOP_KILL_AND_HANG = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class LoopBothTest {
    @Test
    void counts() {
        assertEquals(3, Loop.count(3));
    }

    @Test
    void countsNothing() throws Exception {
        Thread.sleep(300);
        assertEquals(0, Loop.count(0));
    }
}
"""


@pytest.mark.mutation_tool
def test_a_hang_after_a_kill_keeps_the_kill_and_its_killer(java_project, jdks, maven, gradle):
    """A kill before a hang survives the ended minion: the negated loop
    condition fails counts() at once and hangs countsNothing(), and the
    mutant stays killed with its killer."""
    project = java_project("maven")
    _write(project, "Loop.java", LOOP, main=True)
    tests = _write(project, "LoopBothTest.java", LOOP_KILL_AND_HANG)
    adapter = _adapter(project, jdks, maven, gradle)
    runs = {attribution: _mutate(adapter, _request(
        project, tests, target="src/main/java/demo/Loop.java", per_mutant_timeout=3,
        catalogue=Catalogue(operators=(NEGATE,)), attribution=attribution))
        for attribution in ("first", "all")}
    for run in runs.values():
        assert run.outcome is lb.Outcome.COMPLETED, run.reason
    negated = {a: next(m for m in run.mutants if m.key.line == 9)
               for a, run in runs.items()}
    assert negated["first"].status == negated["all"].status == "killed"
    assert negated["all"].killers == ("demo.LoopBothTest#counts()",) == (
        negated["first"].killers)
    assert negated["all"].row["killers_incomplete"] is True
    assert "killers_incomplete" not in negated["first"].row
    assert ([m.status for m in runs["first"].mutants]
            == [m.status for m in runs["all"].mutants])


@pytest.mark.mutation_tool
def test_a_red_suite_and_a_suite_that_does_not_compile_measure_nothing(
        java_project, jdks, maven, gradle):
    project = java_project("maven")
    red = _write(project, "CalcRedTest.java", RED)
    broken = _write(project, "CalcBrokenTest.java",
                    "package demo;\n\nclass CalcBrokenTest {\n    int x = ;\n}\n")
    adapter = _adapter(project, jdks, maven, gradle)
    run = _mutate(adapter, _request(project, red))
    assert run.outcome is lb.Outcome.TEST_FAILURES, run.reason
    assert run.failure == "initialization" and run.score() is None
    run = _mutate(adapter, _request(project, broken))
    assert run.outcome is lb.Outcome.COLLECTION_FAILED
    assert run.failure == "initialization" and "compile" in run.reason


@pytest.mark.mutation_tool
def test_the_wall_clock_ends_a_run_without_results(
        java_project, jdks, maven, gradle):
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    run = _mutate(adapter, _request(project, wall_clock_limit_s=1))
    assert run.outcome is lb.Outcome.TIMED_OUT
    assert not run.complete and run.score() is None and run.mutants == ()


@pytest.mark.mutation_tool
def test_catalogues_and_requests_pit_cannot_honor(
        java_project, jdks, maven, gradle):
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    catalogue = adapter.mutation_operators("pit")
    assert catalogue.outcome is lb.Outcome.COMPLETED
    assert {BOUNDARY, NEGATE} <= set(catalogue.operators)
    run = _mutate(adapter, _request(
        project, catalogue=Catalogue(operators=("CONDITIONALS_BOUNDARY",))))
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert run.requested_operators == (BOUNDARY,)
    assert {m.key.operator for m in run.mutants} == {BOUNDARY}
    assert len(run.survivors()) == 2
    with pytest.raises(lb.InvalidRequest, match="catalogue"):
        _mutate(adapter, _request(project, catalogue=Catalogue(
            operators=("NO_SUCH_MUTATOR",))))
    with pytest.raises(lb.InvalidRequest, match="timeout_journal"):
        _mutate(adapter, _request(project, timeout_journal=True))
    with pytest.raises(lb.InvalidRequest, match="target"):
        _mutate(adapter, _request(project, target="src/main/java/demo/No.java"))
    with pytest.raises(lb.InvalidRequest, match="engine"):
        adapter.open_mutation_session("cosmic_ray")


@pytest.mark.mutation_tool
def test_group_measure_preserves_native_survivors(java_project, jdks, maven, gradle):
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    request = _request(project, catalogue=Catalogue(operators=("CONDITIONALS_BOUNDARY",)))
    flat = _mutate(adapter, request)
    with adapter.mutations.open_session("pit") as session:
        measured = session.run(request)
    assert measured.outcome is lb.Outcome.COMPLETED and measured.complete, measured.reason
    assert measured.score() == flat.score() == 0.0
    assert measured.identity == flat.identity
    assert _verdicts(measured) == _verdicts(flat)
    assert len(measured.survivors()) == 2
    assert measured.resolution.requested == "measure" and measured.resolution.method == "measured"


@pytest.mark.mutation_tool
def test_a_catalogue_of_the_reported_ids_measures_as_the_default(
        java_project, jdks, maven, gradle):
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    ids = adapter.mutation_operators("pit").operators
    listed = _mutate(adapter, _request(
        project, catalogue=Catalogue(operators=ids), attribution="all"))
    default = _mutate(adapter, _request(project, attribution="all"))
    assert listed.outcome is lb.Outcome.COMPLETED, listed.reason
    assert listed.requested_operators == default.requested_operators == ids
    assert _verdicts(listed) == _verdicts(default)
    with pytest.raises(lb.InvalidRequest, match="no mutator"):
        _mutate(adapter, _request(project, catalogue=Catalogue(
            operators=("org.example.NoSuchMutator",))))


@pytest.mark.mutation_tool
def test_per_suite_verdicts_equal_each_suites_standalone_run(
        java_project, jdks, maven, gradle):
    """Java's per-suite verdicts measure each suite alone with PIT's own
    timeout rule."""
    project = java_project("maven")
    more = _write(project, "CalcMoreTest.java", MORE)
    adapter = _adapter(project, jdks, maven, gradle)
    tests = str(project / "src" / "test" / "java")
    request = PerSuiteRequest(TARGET, (
        BudgetedSuite("calc", lb.Suite(tests, ("demo/CalcTest.java",))),
        BudgetedSuite("more", lb.Suite(tests, (more,)))), attribution="all")
    with adapter.mutations.open_session("pit") as session:
        result = session.per_suite(request)
        alone = {name: session.run(request.standalone(name))
                 for name in request.names}
    assert result.outcome is lb.Outcome.COMPLETED, result.reason
    assert result.implementation == DEFAULT_IMPLEMENTATION
    assert len(result.mutants) == 7
    for name, run in alone.items():
        assert run.outcome is lb.Outcome.COMPLETED, run.reason
        assert result.verdicts(name) == {m.key: _verdict_of(m) for m in run.mutants}


def test_a_units_size_follows_the_workers():
    """With more than one worker a unit holds at most PIT_UNIT_SIZE mutants
    of a class, so every worker is used."""
    from langadapter.java._mutation import PIT_UNIT_SIZE, pit_unit_size

    assert PIT_UNIT_SIZE == 10
    assert [pit_unit_size(workers) for workers in (1, 2, 15)] == [0, 10, 10]


@pytest.mark.mutation_tool
def test_units_spread_over_the_workers_with_the_verdicts_of_one_unit(
        java_project, jdks, maven, gradle, monkeypatch):
    """Units of one mutant on two threads give the verdicts and killers of
    one default unit, and each run records its unit size."""
    from langadapter.java import _pit
    from langadapter.java import _mutation
    from langadapter.mutation import EffectiveSetting

    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    sizes, launch = [], _pit.run

    def recorded(*args, **options):
        sizes.append(options["unit_size"])
        return launch(*args, **options)

    monkeypatch.setattr(_pit, "run", recorded)
    alone = _mutate(adapter, _request(project, attribution="all"))
    monkeypatch.setattr(_mutation, "PIT_UNIT_SIZE", 1)
    spread = _mutate(adapter, _request(project, attribution="all"), workers=2)
    assert sizes == [0, 1]
    assert alone.settings["mutation_unit_size"] == EffectiveSetting(
        0, "default", {"rule": "workers", "workers": 1, "size": 10})
    assert spread.settings["mutation_unit_size"].value == 1
    assert alone.outcome is spread.outcome is lb.Outcome.COMPLETED, (alone.reason, spread.reason)

    def verdicts(run):
        return {m.key: (m.status, tuple(sorted(t.qualname for t in m.killing_tests)))
                for m in run.mutants}
    assert len(spread.mutants) == 7
    assert verdicts(spread) == verdicts(alone)


@pytest.mark.mutation_tool
@pytest.mark.parametrize("via", ["session", "scope"])
def test_a_cancel_ends_a_pit_measurement_in_flight(java_project, jdks, maven, gradle, via):
    """A cancel ends a long PIT measurement (each test sleeps 20 s) within
    seconds with MeasurementCancelled and closes the session, from the
    session or a CancelScope."""
    import time as clock
    from concurrent.futures import ThreadPoolExecutor

    from langadapter.mutation import MeasurementCancelled

    project = java_project("maven")
    test = project / "src" / "test" / "java" / "demo" / "CalcTest.java"
    test.write_text(test.read_text(encoding="utf-8").replace(
        "    void positive() {\n", "    void positive() throws Exception {\n"
        "        Thread.sleep(20000);\n"), encoding="utf-8", newline="\n")
    # The cancel needs no particular JDK.
    adapter = _adapter(project, jdks, maven, gradle, feature=max(jdks))
    session = adapter.open_mutation_session("pit", workers=1)
    scope = lb.CancelScope()

    def measure():
        if via == "session":
            return session.run(_request(project, wall_clock_limit_s=900))
        with scope:
            return session.run(_request(project, wall_clock_limit_s=900))

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(measure)
        deadline = clock.monotonic() + 300
        while not any("pit.args" in " ".join(str(a) for a in getattr(p, "args", ()))
                      for p in (session._cancellation._live if session._cancellation else [])):
            assert clock.monotonic() < deadline and not future.done()
            clock.sleep(0.2)
        clock.sleep(2)
        started = clock.monotonic()
        (session.cancel if via == "session" else scope.cancel)()
        with pytest.raises(MeasurementCancelled):
            future.result(timeout=120)
    assert clock.monotonic() - started < 30
    assert session._closed
    session.close()


def test_a_cancel_during_maven_resolution_leaves_no_scratch(java_project, jdks, maven, gradle,
                                                           tmp_path, monkeypatch):
    """A cancel during Maven's resolution removes the resolution's scratch
    and receipt and keeps no cut resolution for later runs."""
    import tempfile
    import time as clock
    from concurrent.futures import ThreadPoolExecutor

    from langadapter.mutation import MeasurementCancelled

    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle, feature=max(jdks))
    session = adapter.open_mutation_session("pit", workers=1)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(session.run, _request(project, wall_clock_limit_s=900))
        deadline = clock.monotonic() + 300
        while not any("mvn" in " ".join(str(a) for a in getattr(p, "args", ())).lower()
                      for p in (session._cancellation._live if session._cancellation else [])):
            assert clock.monotonic() < deadline and not future.done()
            clock.sleep(0.05)
        session.cancel()
        with pytest.raises(MeasurementCancelled):
            future.result(timeout=120)
    session.close()
    assert adapter._resolution is None

    def left():
        return sorted(p.name for p in scratch.iterdir()
                      if p.name.startswith(("langadapter_java_", "langadapter-ownership-")))

    assert left() == []
    # A later resolution starts afresh and leaves nothing either.
    assert adapter.project().outcome is lb.Outcome.COMPLETED
    assert left() == []


@pytest.mark.parametrize("status", ["MEMORY_ERROR", "RUN_ERROR"])
def test_a_minion_the_mutant_ended_is_a_kill_not_a_timeout(status):
    """A mutant that runs its minion out of memory or ends it is a kill,
    named by its killers when PIT lists any."""
    from langadapter.mutation import MutationClass
    from langadapter.java import _pit

    bare = _pit.classify(status, ())
    assert (bare.status, bare.mutation_class, bare.subcategory) == (
        "killed", MutationClass.DETECTED, "no_named_test")
    named = _pit.classify(status, ("demo.CalcTest#positive()",))
    assert (named.status, named.subcategory) == ("killed", "failing_test")


#: A target whose file declares a second top-level type.
TWO_TYPES = """\
package demo;

public final class Calc {
    private Calc() {
    }

    public static int twice(int x) {
        return CalcHelper.add(x, x);
    }
}

final class CalcHelper {
    private CalcHelper() {
    }

    static int add(int a, int b) {
        return a + b;
    }
}
"""


@pytest.mark.mutation_tool
def test_pit_mutates_a_second_top_level_type_of_the_target(java_project, jdks, maven, gradle):
    """PIT mutates every type the target's file declares, so CalcHelper's
    line has mutants, as its coverage is recorded."""
    project = java_project("maven")
    _write(project, "Calc.java", TWO_TYPES, main=True)
    _write(project, "CalcTest.java", (
        "package demo;\n\nimport static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "import org.junit.jupiter.api.Test;\n\nclass CalcTest {\n"
        "    @Test\n    void twice() {\n        assertEquals(4, Calc.twice(2));\n    }\n}\n"))
    run = _mutate(_adapter(project, jdks, maven, gradle), _request(project))
    assert run.outcome is lb.Outcome.COMPLETED, run.reason
    assert 17 in {m.key.line for m in run.mutants}
