"""Mutation runs with Mull through the real GCC, gcov, clang and Mull."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import math
import shutil
import time
from pathlib import Path

import pytest

import langadapter as la
from langadapter.errors import InvalidRequest
from langadapter.mutation import (
    DETECTED_STATUSES, KILLED_STATUS, SURVIVED_STATUS, TIMEOUT_STATUS, UNTESTED_STATUS,
    FAILED_INITIALIZATION, FAILED_INSTRUMENT_CHECK, INCOMPLETE_KEY, UNTESTED_MUTANTS_IN_CATALOGUE,
    Catalogue, MutationRequest, TimeoutRule, matrix_score_percent,
)
from langadapter.runs import PerTestEvidenceRequest
from langadapter.sources import PoolMember

from langadapter.c import _mull, _mutation
from langadapter.c.adapter import CAdapter
from conftest import needs_gcc, needs_mull, negative_count_output
from test_runs import (CLAMP_HEAD, CLAMP_TESTS, CLAMP_TESTS_ONE_WITHOUT_COUNTERS,
                       CLAMP_TESTS_TWO_BELOW, NEGATIVE_COUNT, NO_COUNTERS, RETURN_HI, RETURN_LO,
                       _c_test, _context, _refusing)

SOURCE = b"int pick(int x) {\n    return x < 3 ? x + 1 : 0;\n}\n"


def test_a_mutants_diff_is_its_line_with_mulls_replacement():
    mutant = _mutation._MullMutant("cxx_lt_to_ge:/s/pick.c:2:14:2:15", "cxx_lt_to_ge", ">=",
                                   2, 14, 2, 15, 0)
    assert mutant.engine_id == "cxx_lt_to_ge:2:14:2:15"
    assert mutant.diff(SOURCE, "src/pick.c") == (
        "--- a/src/pick.c\n+++ b/src/pick.c\n@@ -2 +2 @@\n"
        "-    return x < 3 ? x + 1 : 0;\n+    return x >= 3 ? x + 1 : 0;\n")


def test_the_config_names_the_operators_and_the_target_alone(tmp_path):
    source = tmp_path / "stage" / "src" / "pick.c"
    lines = _mull.config(tmp_path / "mull.yml", ("cxx_add_to_sub",), source).read_text() \
        .splitlines()
    assert lines[0] == 'mutators: ["cxx_add_to_sub"]'
    key, _, value = lines[1].partition(": ")
    [pattern] = json.loads(value)
    assert key == "includePaths" and re.fullmatch(pattern, os.path.realpath(source))
    assert not re.fullmatch(pattern, os.path.realpath(source.with_name("pickxc")))


def test_the_default_operators_are_mulls_default_group():
    assert len(_mull.DEFAULT) == len(set(_mull.DEFAULT)) == 18
    assert set(_mull.DEFAULT) < set(_mull.ALL)
    assert "cxx_remove_void_call" not in _mull.DEFAULT


@needs_mull
def test_the_default_operators_give_mulls_own_default_group(project, adapter, suite,
                                                            monkeypatch):
    """The default catalogue, the adapter's explicit list, gives the mutants
    Mull's own cxx_default group gives on the same target and tests."""
    root = project("calc_make")
    default = _run(adapter(root), suite(CLAMP_TESTS))
    # The group's name is no operator the adapter offers. Mull expands it.
    monkeypatch.setattr(_mull, "ALL", _mull.ALL + ("cxx_default",))
    group = _run(adapter(root), suite(CLAMP_TESTS),
                 catalogue=Catalogue(operators=("cxx_default",)))
    assert default.outcome is la.Outcome.COMPLETED, default.reason
    assert group.outcome is la.Outcome.COMPLETED, group.reason
    assert default.mutants
    assert {(m.key.operator, m.key.line) for m in default.mutants} == {
        (m.key.operator, m.key.line) for m in group.mutants}


@needs_gcc
def test_a_mutation_run_without_mull_starts_no_build(project, suite, tmp_path):
    """Without a usable Mull a mutation run misses setup before the
    project's build: the compiler compiles nothing."""
    from test_runs import logging_gcc

    gcc, log = logging_gcc(tmp_path / "tools")
    environment = CAdapter.context_environment({"c_mull": str(tmp_path / "mull-runner-18")})
    c = CAdapter(la.ProjectContext(root=str(project("calc_make")), environment=environment,
                                   interpreter=la.Interpreter(path=str(gcc))))
    run = _run(c, suite(CLAMP_TESTS))
    assert run.outcome is la.Outcome.MISSING_SETUP, run.reason
    assert "Mull was not found" in run.reason and not run.mutants
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert not [line for line in lines if " -c " in f" {line} "], lines


def test_a_missing_mull_is_missing_setup_for_mutation_alone(project, adapter, tmp_path):
    root = project("calc_make")
    c = adapter(root, c_mull=str(tmp_path / "mull-runner-18"))
    if c._gate() is not None:
        pytest.skip("needs Linux and gcc on PATH")
    report = c.capabilities()
    by = {capability.name: capability for capability in report.capabilities}
    assert by["mutation"].availability is la.Availability.MISSING_SETUP
    assert "Mull was not found" in by["mutation"].reason
    assert by["coverage"].availability is la.Availability.AVAILABLE
    assert report.mutation_engines == ()


def _run(adapter, suite, target="src/calc.c", **fields):
    with adapter.mutations.open_session(_mull.ENGINE, workers=fields.pop("workers", 1)) \
            as session:
        return session.run(MutationRequest(target=target, suite=suite, **fields))


@needs_mull
def test_a_mutation_run_classifies_every_mutant(project, adapter, suite):
    root = project("calc_make")
    run = _run(adapter(root), suite(CLAMP_TESTS), attribution="all")
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    by = {(m.key.operator, m.key.line): m for m in run.mutants}
    negated = by[("cxx_lt_to_ge", 7)]
    assert negated.status == KILLED_STATUS
    assert sorted(t.qualname for t in negated.killing_tests) == [
        "clamp.below_the_range", "clamp.inside_the_range"]
    assert negated.diff.endswith("-    if (x < lo) {\n+    if (x >= lo) {\n")
    assert negated.row["id"] == "cxx_lt_to_ge:7:11:7:12"
    assert by[("cxx_lt_to_le", 7)].status == SURVIVED_STATUS
    assert by[("cxx_gt_to_le", 10)].killers == ("test_calc.c::clamp.inside_the_range",)
    # An equivalent mutant runs and survives: for x == hi both branches
    # return the same value.
    assert by[("cxx_gt_to_ge", 10)].row["native_status"] == "survived"
    # Mull mutates the compiled code: nothing in the #if 0 block.
    assert not [m for m in run.mutants if 24 <= m.key.line <= 28]
    # sign, hypotenuse and span never ran: their mutants survive unrun.
    unrun = [m for m in run.mutants if 16 <= m.key.line <= 21 or m.key.line == 31]
    assert unrun and {m.row["native_status"] for m in unrun} == {"not_executed"}
    assert all(m.row["tests_run"] == 0 for m in unrun)
    assert run.summary["not_executed"] == len(unrun)
    matrix = run.to_kill_matrix()
    killed = sum(1 for m in run.mutants if m.status in DETECTED_STATUSES)
    assert matrix_score_percent(matrix) == pytest.approx(100.0 * killed / len(run.mutants))
    assert run.identity.attribution == "all"
    assert run.settings["attribution"].value == "all"
    tools = dict(run.identity.observation.toolchain.tools)
    assert re.fullmatch(r"\d+\.\d+\.\d+", tools["mull"])
    assert int(tools["mull_llvm"].split(".")[0]) in _mull.LLVM_RELEASES
    assert tools["clang"].startswith(tools["mull_llvm"][:2])
    build = run.settings["mull_build"].value
    assert "clang version" in build["compiler"] and build["left_out"] == []


@needs_mull
def test_the_first_killer_ends_a_mutants_run_by_default(project, adapter, suite):
    root = project("calc_make")
    run = _run(adapter(root), suite(CLAMP_TESTS))
    negated = next(m for m in run.mutants if (m.key.operator, m.key.line) == ("cxx_lt_to_ge", 7))
    assert negated.killers == ("test_calc.c::clamp.below_the_range",)
    assert negated.row["tests_run"] == 1


@needs_mull
def test_workers_give_the_same_verdicts(project, adapter, suite):
    root = project("calc_make")
    tests = suite(CLAMP_TESTS)
    one = _run(adapter(root), tests, attribution="all")
    three = _run(adapter(root), tests, attribution="all", workers=3)
    assert ([(m.key, m.status, m.killers) for m in one.mutants]
            == [(m.key, m.status, m.killers) for m in three.mutants])


@needs_mull
def test_a_suite_that_does_not_pass_is_refused_before_the_first_mutant(project, adapter, suite):
    """A red origin ends as test_failures with every test that did not
    pass, as Java's does, not at its first failing test."""
    root = project("calc_make")
    red = CLAMP_TESTS.replace("EXPECT_EQ(5L", "EXPECT_EQ(6L").replace("ASSERT_EQ(0L",
                                                                    "ASSERT_EQ(1L")
    run = _run(adapter(root), suite(red))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason
    assert "did not pass" in run.reason and not run.mutants
    # Java's shape, which Testuary shows the model with the tests' names.
    assert run.failure == FAILED_INITIALIZATION
    assert sorted(t.qualname for t in run.stopping_tests) == [
        "clamp.below_the_range", "clamp.inside_the_range"]
    assert "clamp.below_the_range" in run.reason and "clamp.inside_the_range" in run.reason


def _warned(source, flags):
    """The ``flags`` that Mull's clang names in a warning when it compiles
    ``source`` with them, sorted."""
    import subprocess

    def run(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    clang = _mull.probe("", run, os.environ).clang
    done = run([clang, *flags, "-c", str(source), "-o", os.devnull])
    warnings = [line for line in done.stderr.splitlines() if "warning:" in line]
    return sorted(flag for flag in flags if any(f"'{flag}'" in line for line in warnings))


@needs_mull
def test_mull_and_its_clang_come_from_the_contexts_path(project, tmp_path, monkeypatch):
    """The context's PATH, not this process's, says where mull-runner-N and
    clang-N are, as for the compiler and the other tools: a context whose
    PATH holds Mull offers it to a caller whose PATH does not."""
    path = os.environ["PATH"]
    compiler = shutil.which("gcc")
    monkeypatch.setenv("PATH", str(tmp_path))
    c = CAdapter(la.ProjectContext(
        root=str(project("calc_make")), interpreter=la.Interpreter(path=compiler),
        environment={**CAdapter.context_environment({}), "PATH": path}))
    assert c.mutation_engines() == (_mull.ENGINE,)


@needs_mull
def test_mulls_build_names_what_it_does_differently(project, adapter, suite):
    root = project("calc_flags")
    c = adapter(root, c_flags="-O2 -flto -ffat-lto-objects -fconserve-stack -fno-tree-vrp")
    tests = suite('#include "utest.h"\n#include "calc.h"\n\n'
                  "UTEST(calc, adds) {\n    ASSERT_EQ(5, add(2, 3));\n}\n\nUTEST_MAIN()\n")
    run = _run(c, tests, target="calc.c")
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    build = run.settings["mull_build"].value
    assert build["left_out"] == ["-flto", "-ffat-lto-objects", "-fconserve-stack"]
    # What clang ignores depends on its release: the run names the given
    # flags that this clang names in a warning when it compiles the target.
    assert build["ignored"] == _warned(root / "calc.c", ["-O2", "-fno-tree-vrp"])
    assert "gcc" in build["project_compiler"].lower()
    # A suite that passes on the project's build and not on clang's is
    # refused, and the reason names the differences.
    (root / "calc.c").write_text((root / "calc.c").read_text().replace(
        "return a + b;", "#ifdef __clang__\n    return 0;\n#endif\n    return a + b;"))
    run = _run(adapter(root, c_flags="-O2 -flto"), tests, target="calc.c")
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason
    assert [t.qualname for t in run.stopping_tests] == ["calc.adds"]
    assert run.failure == FAILED_INITIALIZATION
    assert "on Mull's build" in run.reason and "without -flto" in run.reason


@needs_mull
def test_counts_gcovr_refuses_in_the_origin_end_the_run_as_run_suite_does(project, suite,
                                                                          gcovr_stand_in):
    """The origin check reads each test's counts: counters gcovr refuses
    (CountsInconsistent) end the run as an infrastructure failure with
    gcovr's message, as a suite run ends."""
    adapter = CAdapter(_context(project("calc_make"),
                                gcovr_stand_in(64, negative_count_output("src/calc.c"))))
    run = _run(adapter, suite(CLAMP_TESTS))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.failure == FAILED_INSTRUMENT_CHECK
    assert run.reason.startswith("src/calc.c:2 Got negative hit value in: "), run.reason


@needs_mull
def test_a_missing_counters_file_stops_the_check_by_name(project, adapter, suite):
    """A test whose process wrote no counters in the check on the counters
    build is named, with the failure kind of counts gcovr refuses."""
    run = _run(adapter(project("calc_make")), suite(CLAMP_TESTS_ONE_WITHOUT_COUNTERS))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.failure == FAILED_INSTRUMENT_CHECK and not run.mutants
    assert run.reason == NO_COUNTERS
    assert run.stopping_tests == (_c_test("clamp.keeps_its_counters_from_being_written"),)


@needs_mull
def test_the_check_names_every_test_whose_own_counts_gcovr_refuses(project, suite,
                                                                    gcovr_refusing):
    run = _run(_refusing(project, gcovr_refusing, RETURN_LO), suite(CLAMP_TESTS_TWO_BELOW))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.failure == FAILED_INSTRUMENT_CHECK
    assert run.reason == NEGATIVE_COUNT
    assert run.stopping_tests == (_c_test("clamp.below_the_range"),
                                  _c_test("clamp.far_below_the_range"))


@needs_mull
def test_the_check_of_a_program_whose_every_test_is_refused_names_none(project, suite,
                                                                       gcovr_refusing,
                                                                       monkeypatch):
    """Both tests run alone and have their counts refused: the failure is
    the program's, and no test is named."""
    ran = _spent_after(monkeypatch, NEVER)
    run = _run(_refusing(project, gcovr_refusing, CLAMP_HEAD), suite(CLAMP_TESTS))
    assert run.failure == FAILED_INSTRUMENT_CHECK, run.reason
    assert run.stopping_tests == ()
    assert ran == [2]


@needs_mull
def test_a_late_test_whose_counts_are_refused_is_left_out(project, suite, gcovr_refusing,
                                                          monkeypatch):
    """The second test starts after the time was spent, passes in its one
    second and has its counts refused: it is left out, and the check ends
    as a run whose time was spent."""
    tests = CLAMP_TESTS.replace("UTEST(clamp, inside_the_range) {\n    EXPECT_EQ(5L, clamp(5, 0, 10));",
                                "UTEST(clamp, above_the_range) {\n    EXPECT_EQ(10L, clamp(50, 0, 10));")
    _spent_after(monkeypatch, 1)
    run = _run(_refusing(project, gcovr_refusing, RETURN_HI), suite(tests), wall_clock_limit_s=600)
    assert run.outcome is la.Outcome.TIMED_OUT, run.reason
    assert run.reason == "the run reached its timeout of 600 s"
    assert run.stopping_tests == ()


#: A test that never ends, on every build or on Mull's alone, then a test
#: that sleeps two seconds. Mull's build is the program under a "mull"
#: folder.
HANGING_TESTS = """\
#include "utest.h"
#include "calc.h"
#include <string.h>
#include <unistd.h>

static int on_mulls_build(void) {
    char path[4096];
    ssize_t length = readlink("/proc/self/exe", path, sizeof path - 1);
    if (length < 0) {
        return 0;
    }
    path[length] = 0;
    return strstr(path, "/mull/") != NULL;
}

UTEST(clamp, hangs) {
    if (HANGS_EVERYWHERE || on_mulls_build()) {
        for (;;) {
            sleep(1);
        }
    }
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}

UTEST(clamp, slow) {
    sleep(2);
    EXPECT_EQ(5L, clamp(5, 0, 10));
}

UTEST_MAIN()
"""


def _compiles_untimed(monkeypatch) -> None:
    """The check's clock, on which compiling and linking take no time: it
    stands still from a compile's start to its end, and a compile reads
    every deadline as far ahead. The wall clock then counts only the runs
    of tests and the reads of their counts, whatever the machine's load."""
    import time

    taken = [0.0]
    depth = [0]

    class Clock:
        def __init__(self, real):
            self._real = real

        def monotonic(self):
            return self._real.monotonic() - taken[0] - (NEVER if depth[0] else 0)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def untimed(method):
        def compiling(*args, **options):
            began = time.monotonic()
            depth[0] += 1
            try:
                return method(*args, **options)
            finally:
                depth[0] -= 1
                if not depth[0]:
                    taken[0] += time.monotonic() - began
        return compiling

    for name in ("_compile", "_compile_target", "_compile_tests", "_link"):
        monkeypatch.setattr(CAdapter, name, untimed(getattr(CAdapter, name)))
    from langadapter.c import adapter as c_adapter

    monkeypatch.setattr(_mutation, "time", Clock(_mutation.time))
    monkeypatch.setattr(c_adapter, "time", Clock(c_adapter.time))


@needs_mull
@pytest.mark.parametrize("everywhere, wall", [(1, 10), (0, 30)],
                         ids=["counters-build", "mulls-build"])
def test_a_hanging_test_is_named_and_a_test_after_it_is_not(project, adapter, suite,
                                                            monkeypatch, everywhere, wall):
    """A real hanging test uses up the check's wall clock, which counts no
    compile, and is named. The slow test starts after that, has one second
    and fails, and is named in neither the names nor the reason's list."""
    tests = f"#define HANGS_EVERYWHERE {everywhere}\n" + HANGING_TESTS
    _compiles_untimed(monkeypatch)
    run = _run(adapter(project("calc_make")), suite(tests), wall_clock_limit_s=wall)
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason
    assert run.failure == FAILED_INITIALIZATION
    assert run.stopping_tests == (_c_test("clamp.hangs"),)
    assert "clamp.hangs" in run.reason and "clamp.slow" not in run.reason
    assert ("on Mull's build" in run.reason) is (not everywhere)


@needs_mull
def test_mulls_clang_rejecting_the_target_names_no_test(project, adapter, suite):
    """A failure no test caused keeps its instrument check and names none."""
    root = project("calc_make")
    calc = root / "src" / "calc.c"
    calc.write_text("#ifdef __clang__\n#error the target refuses clang\n#endif\n"
                    + calc.read_text(encoding="utf-8"), encoding="utf-8")
    run = _run(adapter(root), suite(CLAMP_TESTS))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.failure == FAILED_INSTRUMENT_CHECK
    assert run.stopping_tests == ()


#: A clamp test that sleeps three seconds, then passes.
SLOW_TEST = """UTEST(clamp, slow_inside_the_range) {
    sleep(3);
    EXPECT_EQ(5L, clamp(5, 0, 10));
}

UTEST_MAIN()"""


#: A number of runs the clock of ``_spent_after`` is never spent after.
NEVER = 10 ** 6


def _spent_after(monkeypatch, calls: int) -> list:
    """The check's clock, which reads every deadline as spent once ``calls``
    tests have run alone. The tests run alone, counted in the list's one
    item."""
    from langadapter.c import _utest

    ran = [0]
    run_one = _utest.run_one

    class Clock:
        def __init__(self, real):
            self._real = real

        def monotonic(self):
            return self._real.monotonic() + (1e6 if ran[0] >= calls else 0)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def counted(*args, **options):
        found = run_one(*args, **options)
        ran[0] += 1
        return found

    monkeypatch.setattr(_utest, "run_one", counted)
    monkeypatch.setattr(_mutation, "time", Clock(_mutation.time))
    return ran


@needs_mull
def test_a_test_started_after_the_wall_clock_was_spent_is_not_named(project, adapter, suite,
                                                                    monkeypatch):
    """The first test fails before the time is spent and is named. The slow
    test starts after it, has one second and fails: it did not stop the
    run, so neither the names nor the reason's list hold it."""
    tests = CLAMP_TESTS.replace('#include "calc.h"\n',
                                '#include "calc.h"\n#include <unistd.h>\n')
    tests = tests.replace("ASSERT_EQ(0L", "ASSERT_EQ(1L").replace(
        "UTEST(clamp, inside_the_range)", "UTEST(clamp, unused)").replace("UTEST_MAIN()", SLOW_TEST)
    _spent_after(monkeypatch, 1)
    run = _run(adapter(project("calc_make")), suite(tests), wall_clock_limit_s=600)
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason
    assert run.failure == FAILED_INITIALIZATION
    assert run.stopping_tests == (_c_test("clamp.below_the_range"),)
    assert "clamp.below_the_range" in run.reason and "slow_inside" not in run.reason


@needs_mull
# The counters build runs the three tests alone first (calls 1 to 3), then
# Mull's build compiles and runs them again (calls 4 to 6).
@pytest.mark.parametrize("calls", [1, 4], ids=["counters-build", "mulls-build"])
def test_a_check_whose_every_failing_test_started_late_is_timed_out(project, adapter, suite,
                                                                    monkeypatch, calls):
    """Every test that failed started after the time was spent: the check
    names no test and ends as a run whose time was spent, with the text a C
    run gives then."""
    tests = CLAMP_TESTS.replace('#include "calc.h"\n',
                                '#include "calc.h"\n#include <unistd.h>\n')
    tests = tests.replace("UTEST(clamp, inside_the_range)", "UTEST(clamp, unused)").replace(
        "UTEST_MAIN()", SLOW_TEST)
    ran = _spent_after(monkeypatch, calls)
    run = _run(adapter(project("calc_make")), suite(tests), wall_clock_limit_s=600)
    assert run.outcome is la.Outcome.TIMED_OUT, run.reason
    assert run.reason == "the run reached its timeout of 600 s"
    assert run.stopping_tests == () and not run.mutants
    # Cut by the wall clock: incomplete, and so stamped in its kill matrix.
    assert run.incomplete == {"reason": "wall_clock_limit", "limit_s": 600, "untested": None,
                              "total": None}
    assert run.complete is False
    assert run.to_kill_matrix()[INCOMPLETE_KEY] == run.incomplete
    # The counters build's three runs, then Mull's three where it got there.
    assert ran == [3 if calls == 1 else 6]


@needs_mull
def test_an_unknown_operator_is_refused(project, adapter, suite):
    root = project("calc_make")
    with pytest.raises(InvalidRequest):
        _run(adapter(root), suite(CLAMP_TESTS), catalogue=Catalogue(operators=("nope",)))


@needs_mull
def test_a_target_without_mutants_completes_with_none(project, adapter, suite):
    """Mull writes no report when it finds no mutant (calc.c has no %): the
    run completes without mutants, not as a listing that failed."""
    root = project("calc_make")
    run = _run(adapter(root), suite(CLAMP_TESTS),
               catalogue=Catalogue(operators=("cxx_rem_to_div",)))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert run.mutants == () and run.reason == "the catalogue has no mutant for this target"


@needs_mull
def test_per_test_evidence_carries_each_tests_kills(project, adapter, suite):
    root = project("calc_make")
    c = adapter(root)
    tests = suite(CLAMP_TESTS)
    run = _run(c, tests, attribution="all")
    evidence = c.per_test_evidence(PerTestEvidenceRequest(
        tests, target="src/calc.c", timeout_s=120, mutation_run=run))
    assert evidence.kills == "usable"
    kills = {e.test.qualname: {i.mutant for i in e.items if hasattr(i, "mutant")}
             for e in evidence.tests}
    negated = next(m for m in run.mutants if (m.key.operator, m.key.line) == ("cxx_lt_to_ge", 7))
    assert negated.row["id"] in kills["clamp.below_the_range"]
    assert negated.row["id"] in kills["clamp.inside_the_range"]


@needs_mull
def test_stored_kills_map_back_to_the_tests(project, adapter, suite):
    root = project("calc_make")
    c = adapter(root)
    run = _run(c, suite(CLAMP_TESTS), attribution="all")
    names = {"test_calc.c::clamp.below_the_range", "test_calc.c::clamp.inside_the_range"}
    tokens, unmatched = c.kill_tokens(run.to_kill_matrix(), names)
    assert unmatched == ()
    assert set(tokens) == names


#: Under clamp's x < lo turned x >= lo, the first test fails and the second
#: never returns, so the per-mutant time ends after the first killer.
CUT_TESTS = """#include "utest.h"
#include "calc.h"

UTEST(clamp, below_the_range) {
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}

UTEST(clamp, waits_for_the_middle) {
    while (clamp(5, 0, 10) != 5) {
    }
}

UTEST_MAIN()
"""


@needs_mull
def test_killers_cut_by_the_time_limit_are_marked_incomplete(project, adapter, suite):
    root = project("calc_make")
    run = _run(adapter(root), suite(CUT_TESTS), attribution="all", per_mutant_timeout=5)
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    cut = {(m.key.operator, m.key.line): m for m in run.mutants}[("cxx_lt_to_ge", 7)]
    assert cut.status == KILLED_STATUS
    assert cut.row.get("killers_incomplete") is True
    assert [t.qualname for t in cut.killing_tests] == ["clamp.below_the_range"]


#: A test that never returns under a mutant that changes clamp(5, 0, 10),
#: and that no other test kills.
WAITING_TESTS = """#include "utest.h"
#include "calc.h"

UTEST(clamp, waits_for_the_middle) {
    while (clamp(5, 0, 10) != 5) {
    }
}

UTEST_MAIN()
"""


@needs_mull
def test_a_mutant_the_wall_clock_cuts_is_untested_not_a_timeout(project, adapter, suite):
    """When the run's wall clock, not the per-mutant timeout, ends a mutant's
    tests, the mutant is untested: a timeout counts as detected."""
    root = project("calc_make")
    run = _run(adapter(root), suite(WAITING_TESTS), per_mutant_timeout=600,
               wall_clock_limit_s=60)
    statuses = [m.status for m in run.mutants]
    assert run.outcome is la.Outcome.TIMED_OUT, run.reason
    assert TIMEOUT_STATUS not in statuses
    assert run.incomplete["untested"] == statuses.count(UNTESTED_STATUS) > 0


@needs_mull
def test_a_mutant_the_wall_clock_cuts_keeps_the_kills_measured_before(project, adapter, suite):
    """When the run's wall clock ends a mutant's tests after one failed, that
    test kills it, and its killers are marked incomplete."""
    root = project("calc_make")
    run = _run(adapter(root), suite(CUT_TESTS), attribution="all", per_mutant_timeout=600,
               wall_clock_limit_s=60, catalogue=Catalogue(operators=("cxx_lt_to_ge",)))
    cut = {(m.key.operator, m.key.line): m for m in run.mutants}[("cxx_lt_to_ge", 7)]
    assert cut.status == KILLED_STATUS
    assert cut.row.get("killers_incomplete") is True
    assert [t.qualname for t in cut.killing_tests] == ["clamp.below_the_range"]


#: Eight tests that each take a quarter of a second and run clamp without
#: checking it, so that no test kills a mutant of clamp, and one that never
#: returns under a mutant that changes clamp(5, 0, 10).
QUARTER_TEST = """UTEST(slow, quarter_{index}) {{
    volatile long x = clamp({index}, 0, 10);
    (void)x;
    usleep(250000);
}}

"""
QUARTER_TESTS = ("""#include <unistd.h>
#include "utest.h"
#include "calc.h"

""" + "".join(QUARTER_TEST.format(index=index) for index in range(8))
              + """UTEST(clamp, waits_for_the_middle) {
    while (clamp(5, 0, 10) != 5) {
    }
}

UTEST_MAIN()
""")
#: clamp's two comparisons turned around, which changes clamp(5, 0, 10), and
#: widened, which does not.
CLAMP_COMPARISONS = {("cxx_lt_to_ge", 7): TIMEOUT_STATUS, ("cxx_lt_to_le", 7): SURVIVED_STATUS,
                     ("cxx_gt_to_ge", 10): SURVIVED_STATUS, ("cxx_gt_to_le", 10): TIMEOUT_STATUS}


@needs_mull
def test_a_rule_applies_to_the_check_runs_time_with_each_test_alone(project, adapter, suite):
    """For C tests a TimeoutRule applies to the time of the run's own check,
    where each test runs alone as under a mutant. A passing_suite_s, the time
    of another run form such as a normal run of one program, is refused.
    Tests that take longer alone than such a time let a mutant no test kills
    survive, and a mutant that hangs a test is a timeout."""
    root = project("calc_make")
    fields = dict(workers=2, per_mutant_timeout=TimeoutRule(factor=3, minimum_s=1),
                  catalogue=Catalogue(operators=tuple(sorted({op for op, _ in CLAMP_COMPARISONS}))))
    with pytest.raises(InvalidRequest) as refused:
        _run(adapter(root), suite(QUARTER_TESTS), passing_suite_s=0.05, **fields)
    assert refused.value.field == "passing_suite_s"
    run = _run(adapter(root), suite(QUARTER_TESTS), **fields)
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    by = {(m.key.operator, m.key.line): m.status for m in run.mutants}
    assert {key: by[key] for key in CLAMP_COMPARISONS} == CLAMP_COMPARISONS
    applied = run.settings["per_mutant_timeout"]
    check_s = applied.inputs["passing_suite_s"]
    assert check_s > 2 and applied.value == math.ceil(3 * check_s), applied


#: Two tests that run clamp without checking it.
UNCHECKED_TESTS = """#include "utest.h"
#include "calc.h"

UTEST(clamp, runs_once) {
    volatile long x = clamp(5, 0, 10);
    (void)x;
}

UTEST(clamp, runs_twice) {
    volatile long x = clamp(5, 0, 10);
    (void)x;
}

UTEST_MAIN()
"""


@needs_mull
def test_a_budget_that_ends_between_two_tests_leaves_the_mutant_untested(
        project, adapter, suite, monkeypatch):
    """When a fixed per-mutant time ends between two tests, no test failed or
    ran out of time: the mutant has no verdict, as when the wall clock ends
    its run, and the run is marked incomplete. A timeout needs a test the
    time cut while it ran."""
    run_test = CAdapter._run_test

    def slow_after(self, argv, **options):
        done = run_test(self, argv, **options)
        if any(name.startswith("cxx_") for name in options.get("env") or {}):
            time.sleep(2.5)  # past the 2 s budget, after the test and before the next
        return done

    monkeypatch.setattr(CAdapter, "_run_test", slow_after)
    root = project("calc_make")
    run = _run(adapter(root), suite(UNCHECKED_TESTS), per_mutant_timeout=2,
               catalogue=Catalogue(operators=("cxx_lt_to_le",)))
    [mutant] = [m for m in run.mutants if m.key.line == 7]
    assert mutant.status == UNTESTED_STATUS and mutant.row["tests_run"] == 1, mutant.row
    assert run.incomplete == {"reason": UNTESTED_MUTANTS_IN_CATALOGUE, "untested": 1,
                              "total": len(run.mutants)}


#: The one operator of the stalled runs: its mutant on line 7 runs
#: clamp.below_the_range first, the test that kills it.
LESS_THAN = Catalogue(operators=("cxx_lt_to_ge",))
#: Two operators with a mutant each in clamp, which the tests execute.
TWO_IN_CLAMP = Catalogue(operators=("cxx_lt_to_ge", "cxx_gt_to_le"))


def _stall(monkeypatch, times=1, *, mutants=1, settle=True):
    """The process layer's failure under a stalled disk, in the first test
    run of ``times`` runs of each of the first ``mutants`` mutants (a Mull
    switch in the environment) of a real run: the test and its supervisor
    end, then the output disposal is unconfirmed and the supervisor's
    ``ControlError`` raised, as when its cleanup bound passes. Without
    ``settle`` every later drain of those commands fails the same way.
    Returns the stalled commands."""
    from langadapter import _linux_command
    from langadapter._linux_job import ControlError

    run_test, communicate = CAdapter._run_test, _linux_command._Command.communicate
    state = {"armed": False}
    made: dict = {}
    stalled = []

    def mutant_test(self, argv, **options):
        switch = next((name for name in options.get("env") or {} if name.startswith("cxx_")),
                      None)
        if switch is not None and switch not in made and len(made) < mutants:
            made[switch] = 0
        # A stalled run ends at its first test, so the next run of the
        # switch is the mutant's next run.
        state["armed"] = switch in made and made[switch] < times
        if state["armed"]:
            made[switch] += 1
        return run_test(self, argv, **options)

    def drained(self, input=None, timeout=None):
        if state["armed"]:
            state["armed"] = False
            stalled.append(self)
        elif settle or self not in stalled:
            return communicate(self, input=input, timeout=timeout)
        communicate(self, input=input, timeout=timeout)
        self._langadapter_output_unconfirmed = True
        raise ControlError("Native command exited but supervised output disposal did not finish")

    monkeypatch.setattr(CAdapter, "_run_test", mutant_test)
    monkeypatch.setattr(_linux_command._Command, "communicate", drained)
    return stalled


@needs_mull
def test_a_mutants_run_the_process_layer_failed_is_settled_and_tried_once_more(
        project, adapter, suite, monkeypatch):
    """One per-mutant test run ends in the process layer's failure: its
    command is settled, the mutant's run is tried once more and logged with
    the mutant and the test, and every mutant has its undisturbed verdict."""
    root, tests = project("calc_make"), suite(CLAMP_TESTS)
    undisturbed = _run(adapter(root), tests, catalogue=LESS_THAN)
    stalled = _stall(monkeypatch)
    with pytest.warns(RuntimeWarning, match="tried once more") as logged:
        run = _run(adapter(root), tests, catalogue=LESS_THAN)
    assert len(stalled) == 1
    assert run.outcome is la.Outcome.COMPLETED and run.incomplete is None, run.reason
    assert run.summary["retried_runs"] == 1
    assert [m.status for m in run.mutants] == [m.status for m in undisturbed.mutants]
    [line] = [str(w.message) for w in logged if "tried once more" in str(w.message)]
    assert "cxx_lt_to_ge:7:11:7:12" in line and "test_calc.c::clamp.below_the_range" in line


@needs_mull
def test_a_mutant_whose_run_fails_twice_in_the_process_layer_is_untested(
        project, adapter, suite, monkeypatch):
    """The failure again in the second try: the mutant is untested with the
    reason, both are logged, and the run completes with the untested-mutants
    stamp and no score. Nothing raises."""
    root = project("calc_make")
    stalled = _stall(monkeypatch, 2)
    with pytest.warns(RuntimeWarning) as logged:
        run = _run(adapter(root), suite(CLAMP_TESTS), catalogue=LESS_THAN)
    assert len(stalled) == 2
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    [untested] = [m for m in run.mutants if m.status == UNTESTED_STATUS]
    assert untested.row["id"] == "cxx_lt_to_ge:7:11:7:12"
    assert "test_calc.c::clamp.below_the_range" in untested.row["reason"]
    assert run.incomplete == {"reason": UNTESTED_MUTANTS_IN_CATALOGUE, "untested": 1,
                              "total": len(run.mutants)}
    assert run.summary["retried_runs"] == 1
    assert run.score() is None
    lines = [str(w.message) for w in logged]
    assert [line for line in lines if "tried once more" in line]
    [line] = [line for line in lines if "is untested" in line]
    assert "cxx_lt_to_ge:7:11:7:12" in line and "test_calc.c::clamp.below_the_range" in line


@needs_mull
def test_the_summary_names_each_retried_run_by_the_log_lines_mutant_and_test(
        project, adapter, suite, monkeypatch):
    """Two mutants' runs each fail once in the process layer: the summary
    lists both retries with the mutant and test ids of their log lines, and
    counts them."""
    stalled = _stall(monkeypatch, mutants=2)
    with pytest.warns(RuntimeWarning, match="tried once more") as logged:
        run = _run(adapter(project("calc_make")), suite(CLAMP_TESTS), catalogue=TWO_IN_CLAMP)
    made = sorted(re.match(r"mutant (\S+): its run is tried once more, since the process layer "
                           r"failed in the run of (\S+): ", str(w.message)).groups()
                  for w in logged if "tried once more" in str(w.message))
    assert len(stalled) == len(made) == 2
    assert run.summary["retried"] == [{"mutant": mutant, "test": test} for mutant, test in made]
    assert run.summary["retried_runs"] == len(run.summary["retried"])


@needs_mull
def test_a_command_the_process_layer_cannot_settle_still_raises(project, adapter, suite,
                                                                monkeypatch):
    """A command whose disposal fails until the run's deadline: its failure
    is raised, since a process of it may still run."""
    from langadapter._linux_job import ControlError

    stalled = _stall(monkeypatch, settle=False)
    with pytest.raises(ControlError, match="disposal did not finish"):
        _run(adapter(project("calc_make")), suite(CLAMP_TESTS), catalogue=LESS_THAN,
             wall_clock_limit_s=30)
    assert len(stalled) == 1


def test_a_pooled_kill_of_c_tests_goes_to_its_suite(project, adapter):
    c = adapter(project("calc_make"))
    killer = la.TestId(file="test_calc_001.c", qualname="clamp.below_the_range")
    text = c.test_id_text(killer)
    matrix = {"mutants": [{"id": "m1", "killers": [text],
                           "killing_tests": [dataclasses.asdict(killer)]}]}
    assert c.pool_kills(matrix, {"test_calc_001.c": 1}, {1: {text}}) == {1: {text: {"K:m1"}}}


@needs_mull
def test_a_real_pools_kills_go_to_the_suite_whose_test_made_them(project, adapter, tmp_path):
    """A staged pool of two suites whose tests share their names, run through
    Mull: each kill goes to the suite whose file holds the killing test."""
    c = adapter(project("calc_make"))
    second = CLAMP_TESTS.replace("EXPECT_EQ(5L, clamp(5, 0, 10));",
                                 "EXPECT_EQ(10L, clamp(15, 0, 10));")
    pool = c.stage_pool([PoolMember("s1", "test_calc.c", CLAMP_TESTS),
                         PoolMember("s2", "test_calc.c", second)])
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    run = _run(c, pool.write(str(pool_dir)), attribution="all")
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    files = {"test_calc_s1.c": 1, "test_calc_s2.c": 2}
    assert_pooled_kills(c, run, files, ("clamp.below_the_range", "clamp.inside_the_range"))


def assert_pooled_kills(adapter, run, files, qualnames):
    """``adapter``'s pool_kills of the pooled ``run`` give each kill to the
    suite (``files``: a pooled file's name to its suite) whose file holds the
    killing test, and both suites kill."""
    texts = {index: {adapter.test_id_text(la.TestId(file=name, qualname=qualname))
                     for qualname in qualnames}
             for name, index in files.items()}
    expected = {}
    for mutant in run.mutants:
        for test in mutant.killing_tests:
            index = files[Path(test.file).name]
            expected.setdefault(index, {}).setdefault(adapter.test_id_text(test), set()).add(
                f"K:{mutant.row['id']}")
    assert set(expected) == {1, 2}
    assert adapter.pool_kills(run.to_kill_matrix(), files, texts) == expected


@needs_mull
def test_mutants_run_side_by_side_inside_a_prepared_copy(project, adapter, suite, tmp_path):
    root = project("calc_make")
    tests = suite(CLAMP_TESTS)
    owner = la.prepare_project(adapter(root).context, parent=str(tmp_path),
                               receipt_parent=str(tmp_path))
    try:
        with owner.use() as used:
            before = {path.relative_to(used.root).as_posix(): path.read_bytes()
                      for path in Path(used.root).rglob("*") if path.is_file()}
            run = _run(CAdapter(used), tests, workers=3, attribution="all")
            assert run.outcome is la.Outcome.COMPLETED, run.reason
            assert run.settings["workers"].value == 3
            by = {(m.key.operator, m.key.line): m for m in run.mutants}
            assert by[("cxx_lt_to_ge", 7)].status == KILLED_STATUS
            # Each worker wrote in its own directory of the scratch, none in the copy.
            assert {path.relative_to(used.root).as_posix(): path.read_bytes()
                    for path in Path(used.root).rglob("*") if path.is_file()} == before
    finally:
        owner.close()
