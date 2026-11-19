"""Suite runs, coverage, per-test evidence and timing through the real GCC and gcov."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import langadapter as la
from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest, SuiteRunRequest

from conftest import PROJECTS, needs_gcc, negative_count_output

pytestmark = needs_gcc

CLAMP_TESTS = """\
#include "utest.h"
#include "calc.h"

UTEST(clamp, below_the_range) {
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}

UTEST(clamp, inside_the_range) {
    EXPECT_EQ(5L, clamp(5, 0, 10));
}

UTEST_MAIN()
"""


def test_a_crash_ends_only_its_own_test_and_the_others_keep_their_outcomes(project, adapter,
                                                                          suite):
    root = project("calc_make")
    run = adapter(root).run_suite(SuiteRunRequest(suite("""\
#include <stddef.h>
#include "utest.h"
#include "calc.h"

UTEST(sign, negative) {
    EXPECT_EQ(-1, sign(-3));
}

UTEST(sign, wrong_on_purpose) {
    EXPECT_EQ(1, sign(0));
}

UTEST(crash, on_purpose) {
    volatile int *p = NULL;
    EXPECT_EQ(1, *p);
}

UTEST(sign, positive) {
    EXPECT_EQ(1, sign(4));
}

UTEST_MAIN()
"""), timeout_s=120, coverage_target="src/calc.c"))
    outcomes = {t.test.qualname: t.outcome for t in run.tests}
    assert run.outcome is la.Outcome.TEST_FAILURES
    assert outcomes == {"sign.negative": "passed", "sign.wrong_on_purpose": "failed",
                        "crash.on_purpose": "error", "sign.positive": "passed"}
    failed = next(t for t in run.tests if t.outcome == "failed")
    assert "Actual : 1 vs 0" in failed.message
    crashed = next(t for t in run.tests if t.outcome == "error")
    assert "SIGSEGV" in crashed.message
    assert (run.counts.passed, run.counts.failed, run.counts.errors) == (2, 1, 1)


#: A test whose output ends without a line break, so that utest's result
#: line is glued to it, a test that prints a result line of its own and
#: then fails, a failing test whose output ends without a line break, a
#: passing test that writes a failed result line of its own to stderr, and
#: one that prints a failed result line of its own and then passes.
GLUED_RESULTS = """\
#include <stdio.h>
#include "utest.h"
#include "calc.h"

UTEST(output, without_a_line_break) {
    printf("no line break");
    EXPECT_EQ(-1, sign(-3));
}

UTEST(output, a_result_line_on_stderr) {
    fprintf(stderr, "[  FAILED  ] output.a_result_line_on_stderr (1ns)\\n");
    EXPECT_EQ(-1, sign(-3));
}

UTEST(output, failing_without_a_line_break) {
    EXPECT_EQ(1, sign(0));
    printf("after the check");
}

UTEST(output, printing_a_result_line) {
    printf("[       OK ] output.printing_a_result_line (1ns)\\n");
    EXPECT_EQ(1, sign(0));
}

UTEST(output, printing_a_failed_line) {
    printf("[  FAILED  ] output.printing_a_failed_line (1ns)\\n");
    EXPECT_EQ(-1, sign(-3));
}

UTEST_MAIN()
"""
#: Their outcomes: utest's own result line is the last one for a test on
#: stdout.
GLUED_OUTCOMES = {"output.without_a_line_break": "passed",
                  "output.a_result_line_on_stderr": "passed",
                  "output.failing_without_a_line_break": "failed",
                  "output.printing_a_result_line": "failed",
                  "output.printing_a_failed_line": "passed"}


def _glued_read(run) -> None:
    """GLUED_RESULTS' outcomes, and each failed test's message: its checks,
    up to the result line its outcome comes from, and the test's own text
    before a result glued to it."""
    tests = {t.test.qualname: t for t in run.tests}
    assert {name: t.outcome for name, t in tests.items()} == GLUED_OUTCOMES
    glued = tests["output.failing_without_a_line_break"].message
    printed = tests["output.printing_a_result_line"].message
    assert "Actual : 1 vs 0" in glued and glued.endswith("after the check")
    assert "Actual : 1 vs 0" in printed and printed.startswith("[       OK ]")


def test_a_result_line_after_output_without_a_line_break_is_the_tests_result(
        project, adapter, suite):
    """utest prints a test's result line right after the test's own output,
    which may end without a line break. A result line the test prints itself
    comes before utest's own and is not its result."""
    root = project("calc_make")
    _glued_read(adapter(root).run_suite(SuiteRunRequest(suite(GLUED_RESULTS), timeout_s=120)))


def test_a_delivered_program_reads_each_tests_last_result_line(project, adapter, suite):
    """A run as delivered runs each program whole: each test's result is
    the last result line for it before the next test begins."""
    _glued_read(_delivered(adapter(project("calc_make")), suite(GLUED_RESULTS)))


def test_the_benchmark_names_the_tests_a_suite_run_finds_failed(project, adapter, suite):
    """The benchmark reads a failing program's tests as a suite run does:
    each by its last result line on stdout, also one glued to its output."""
    timed = adapter(project("calc_make")).tests.benchmark(BenchmarkRequest(
        suite(GLUED_RESULTS), runs=1, timeout_s=120))
    assert timed.outcome is la.Outcome.TEST_FAILURES, timed.reason
    assert timed.reason.endswith(": failing tests: output.failing_without_a_line_break, "
                                 "output.printing_a_result_line"), timed.reason


#: calc.h with code: an inline function at lines 40 to 42, past every line
#: of calc.c, so its lines in gcov's record are told apart by number.
CALC_H_WITH_CODE = ("#ifndef CALC_H\n#define CALC_H\n\n"
                    "long clamp(long x, long lo, long hi);\nint sign(long x);\n"
                    "double hypotenuse(double a, double b);\n" + "\n" * 33
                    + "static inline long twice(long x) {\n    return 2 * x;\n}\n\n#endif\n")


def test_coverage_is_the_target_alone_with_counted_branches(project, adapter, suite):
    root = project("calc_make")
    (root / "include" / "calc.h").write_text(CALC_H_WITH_CODE, encoding="utf-8")
    with (root / "src" / "calc.c").open("a", encoding="utf-8") as source:
        source.write("\nlong doubled(long x) {\n    return twice(x);\n}\n")
    run = adapter(root).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    coverage = run.coverage
    assert coverage.target.path == "src/calc.c"
    # clamp's lines run, sign and hypotenuse do not, and the #if 0 block is no code.
    assert set(coverage.covered) == {6, 7, 8, 10, 13}
    assert {16, 17, 20, 21} <= set(coverage.missing())
    assert not [line for line in coverage.statements if 24 <= line <= 28]
    # doubled (34, 35) is the target's own code, and the header's lines are not.
    assert {34, 35} <= set(coverage.missing())
    assert not [line for line in coverage.statements if line >= 40]
    branches = {(b.source, b.destination) for b in coverage.branches}
    assert {(7, 0), (7, 1), (10, 0), (10, 1)} <= branches
    taken = {(b.source, b.destination) for b in coverage.taken}
    assert (7, 0) in taken and (7, 1) in taken and len(taken & {(10, 0), (10, 1)}) == 1
    texts = adapter(root).coverage_texts(coverage)
    assert "10->1 of 2 branches untaken" in texts["missing_branches"]


def test_a_target_its_build_names_with_a_leading_dot_keeps_its_coverage(project, adapter,
                                                                        suite):
    root = project("calc_make")
    makefile = root / "Makefile"
    makefile.write_text(makefile.read_text(encoding="utf-8").replace("-c $< -o $@",
                                                                      "-c ./$< -o $@"),
                        encoding="utf-8")
    run = adapter(root).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert set(run.coverage.covered) == {6, 7, 8, 10, 13}


#: A gcovr package that logs its arguments to GCOVR_LOG and runs the real gcovr.
GCOVR_SHIM = """\
import os
import runpy
import sys

with open(os.environ["GCOVR_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
shim = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != shim]
for name in [m for m in sys.modules if m == "gcovr" or m.startswith("gcovr.")]:
    del sys.modules[name]
runpy.run_module("gcovr", run_name="__main__", alter_sys=True)
"""


#: Tests that share the test file's static state in their order: the last sees what the
#: first set, as in one process. The middle one crashes with CRASH defined.
IN_ORDER = """\
#include "utest.h"
#include "calc.h"

static int seen;

UTEST(state, first_sets) {
    seen = 1;
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}

UTEST(state, in_between) {
#ifdef CRASH
    volatile int *nothing = 0;
    *nothing = 1;
#endif
    ASSERT_EQ(1, seen);
}

UTEST(state, last_sees) {
    ASSERT_EQ(1, seen);
    EXPECT_EQ(5L, clamp(5, 0, 10));
}

UTEST_MAIN()
"""


def test_a_suite_run_runs_each_test_file_whole_as_delivered(project, adapter, suite):
    """A test file's tests run in one process in their order of definition,
    as a user who copied the file runs it: the later tests see the state the
    first one set."""
    run = adapter(project("calc_make")).run_suite(SuiteRunRequest(
        suite(IN_ORDER), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("state.first_sets", "passed"), ("state.in_between", "passed"),
        ("state.last_sees", "passed")]
    assert set(run.coverage.covered) == {6, 7, 8, 10, 13}


def test_a_crash_in_the_middle_leaves_every_other_test_its_outcome_and_counts(
        project, adapter, suite):
    """The middle test crashes: the program runs again without it, so the
    others run together in one complete process, whose counters cover the
    lines both of them ran."""
    crashing = "#define CRASH\n" + IN_ORDER
    run = adapter(project("calc_make")).run_suite(SuiteRunRequest(
        suite(crashing), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    outcomes = {t.test.qualname: t.outcome for t in run.tests}
    assert outcomes == {"state.first_sets": "passed", "state.in_between": "error",
                        "state.last_sees": "passed"}, outcomes
    crashed = next(t for t in run.tests if t.test.qualname == "state.in_between")
    assert "SIGSEGV" in crashed.message, crashed.message
    assert set(run.coverage.covered) == {6, 7, 8, 10, 13}


#: A fixture test and one index of an indexed test that crash with CRASH
#: defined, and a test after both. The crashing tests call nothing of the
#: target, so the file covers the same lines with and without the crashes.
FIXTURE_AND_INDEX = """\
#include "utest.h"
#include "calc.h"

static void crash(void) {
    volatile int *nothing = 0;
    *nothing = 1;
}

struct fix {
    long lo;
};

UTEST_F_SETUP(fix) {
    utest_fixture->lo = 0;
}

UTEST_F_TEARDOWN(fix) {
    (void)utest_fixture;
}

UTEST_F(fix, crashes) {
#ifdef CRASH
    crash();
#endif
}

UTEST_F(fix, clamps_below) {
    ASSERT_EQ(0L, clamp(-5, utest_fixture->lo, 10));
}

struct idx {
    int index;
};

UTEST_I_SETUP(idx) {
    utest_fixture->index = (int)utest_index;
}

UTEST_I_TEARDOWN(idx) {
    (void)utest_fixture;
}

UTEST_I(idx, signs, 3) {
#ifdef CRASH
    if (1 == utest_fixture->index) {
        crash();
    }
#endif
    EXPECT_EQ(utest_fixture->index ? 1 : 0, sign(utest_fixture->index));
}

UTEST(after, clamps_inside) {
    EXPECT_EQ(5L, clamp(5, 0, 10));
}

UTEST_MAIN()
"""


def test_a_crash_in_a_fixture_test_or_one_index_leaves_the_others_their_counts(
        project, adapter, suite):
    """A crash in a UTEST_F test and in one index of a UTEST_I test: each
    restart leaves out only the crashed test or index, so the other tests,
    the other indices among them, have their outcomes, and the coverage is
    the file's without the crashes."""
    c = adapter(project("calc_make"))
    whole = c.run_suite(SuiteRunRequest(
        suite(FIXTURE_AND_INDEX), timeout_s=120, coverage_target="src/calc.c"))
    assert whole.outcome is la.Outcome.COMPLETED, whole.reason_text(None)
    run = c.run_suite(SuiteRunRequest(
        suite("#define CRASH\n" + FIXTURE_AND_INDEX), timeout_s=120,
        coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    outcomes = {t.test.text.split("::")[1]: t.outcome for t in run.tests}
    assert outcomes == {"fix.crashes": "error", "fix.clamps_below": "passed",
                        "idx.signs/0": "passed", "idx.signs/1": "error",
                        "idx.signs/2": "passed", "after.clamps_inside": "passed"}, outcomes
    assert set(run.coverage.covered) == set(whole.coverage.covered)
    assert set(run.coverage.taken) == set(whole.coverage.taken)


def test_only_the_program_a_crash_restarts_links_the_test_filter(project, suite, tmp_path):
    """The normal, the timed and the delivered programs are the test file's
    build alone: only the program a crash restarts links data/utest_skip.c."""
    from langadapter.c.adapter import CAdapter

    gcc, log = logging_gcc(tmp_path / "tools")
    c = CAdapter(la.ProjectContext(root=str(project("calc_make")),
                                   interpreter=la.Interpreter(path=str(gcc))))
    crashing = "#define CRASH\n" + IN_ORDER
    assert c.run_suite(SuiteRunRequest(suite(IN_ORDER), timeout_s=120)).outcome is (
        la.Outcome.COMPLETED)
    assert _delivered(c, suite(crashing)).outcome is la.Outcome.TEST_FAILURES
    timed = c.tests.benchmark(BenchmarkRequest(suite(IN_ORDER), runs=1, timeout_s=120))
    assert timed.outcome is la.Outcome.COMPLETED, timed.reason
    assert "utest_skip" not in log.read_text(encoding="utf-8")
    run = c.run_suite(SuiteRunRequest(suite(crashing), timeout_s=120))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason
    links = [line.split() for line in log.read_text(encoding="utf-8").splitlines()
             if "-c" not in line.split() and "utest_skip" in line]
    assert len(links) == 1 and links[0][links[0].index("-o") + 1].endswith("-restart"), links


#: The reviewer's probe files (C3, verification-work/gc-review): a head and
#: three passing tests, each case a test file built from them.
PROBE_HEAD = """\
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/resource.h>
#include "utest.h"
#include "calc.h"

static void crash(void) { volatile int *nothing = 0; *nothing = 1; }
"""
PROBE_A = "UTEST(t, a_clamps_below) { ASSERT_EQ(0L, clamp(-5, 0, 10)); }\n"
PROBE_B = "UTEST(t, b_clamps_above) { ASSERT_EQ(10L, clamp(50, 0, 10)); }\n"
PROBE_C = "UTEST(t, c_sign) { ASSERT_EQ(1, sign(4)); }\n"


def _probe(adapter_of, suite, body: str, *, timeout_s: float = 120, coverage: bool = True,
           main: str = "UTEST_MAIN()\n"):
    """A suite run of the probe file ``body``, between its head and ``main``."""
    return adapter_of.run_suite(SuiteRunRequest(
        suite(PROBE_HEAD + body + main), timeout_s=timeout_s,
        coverage_target="src/calc.c" if coverage else None))


def _outcomes(run) -> dict:
    return {t.test.qualname: t.outcome for t in run.tests}


@pytest.mark.parametrize("bye, signal_name", [("abort()", "SIGABRT"), ("crash()", "SIGSEGV"),
                                              ("_exit(3)", "exit 3")])
def test_a_program_that_ends_badly_after_its_tests_names_the_test(project, adapter, suite,
                                                                  bye, signal_name):
    """A test installs an exit handler that ends the program badly
    after utest's closing line. The run is not complete: each test runs in
    its own process, and the one whose process ends badly is the error.
    The coverage is None, never a measured nothing."""
    body = (f"static void bye(void) {{ {bye}; }}\n" + PROBE_A
            + "UTEST(t, installs_atexit) { atexit(bye); ASSERT_EQ(10L, clamp(50, 0, 10)); }\n"
            + PROBE_C)
    run = _probe(adapter(project("calc_make")), suite, body)
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    assert _outcomes(run) == {"t.a_clamps_below": "passed", "t.installs_atexit": "error",
                              "t.c_sign": "passed"}
    [named] = [t for t in run.tests if t.outcome == "error"]
    assert signal_name in named.message, named.message
    assert run.coverage is None


@pytest.mark.parametrize("limit", ["RLIMIT_NOFILE", "RLIMIT_FSIZE"])
def test_a_test_that_lowers_a_process_limit_for_the_counters_is_named(project, adapter, suite,
                                                                      limit):
    """A test lowers the open-files or the file-size limit, so the
    program cannot write its counters at its end (exit 0, or SIGXFSZ). The
    test whose own process writes no counters, or ends badly, is the error,
    and the run neither reads zeros nor raises."""
    body = (PROBE_A + "UTEST(t, lowers_the_limit) { struct rlimit low = {3, 3}; "
            f"ASSERT_EQ(0, setrlimit({limit}, &low)); ASSERT_EQ(10L, clamp(50, 0, 10)); }}\n"
            + PROBE_C)
    run = _probe(adapter(project("calc_make")), suite, body)
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    assert _outcomes(run) == {"t.a_clamps_below": "passed", "t.lowers_the_limit": "error",
                              "t.c_sign": "passed"}
    assert run.coverage is None


def test_a_restart_keeps_the_outcomes_the_first_process_gave(project, adapter, suite):
    """A test that is not repeatable passes in the first process.
    The restart after a later crash runs it again in a fresh, empty working
    directory, and it keeps its first outcome. The coverage is the
    restart's whole process."""
    c = adapter(project("calc_make"))
    baseline = _probe(c, suite, PROBE_A + PROBE_B + PROBE_C)
    once = ("UTEST(t, a_creates) { int fd = open(\"probe-once.txt\", "
            "O_CREAT | O_EXCL | O_WRONLY, 0600); ASSERT_GE(fd, 0); close(fd); "
            "ASSERT_EQ(0L, clamp(-5, 0, 10)); }\n")
    run = _probe(c, suite, once + "UTEST(t, m_crashes) { crash(); }\n" + PROBE_B + PROBE_C)
    assert _outcomes(run) == {"t.a_creates": "passed", "t.m_crashes": "error",
                              "t.b_clamps_above": "passed", "t.c_sign": "passed"}
    assert [t.test.qualname for t in run.tests] == [
        "t.a_creates", "t.m_crashes", "t.b_clamps_above", "t.c_sign"]
    assert set(run.coverage.covered) == set(baseline.coverage.covered)


def test_a_test_that_prints_a_run_line_of_another_name_leaves_the_run_incomplete(
        project, adapter, suite):
    """A passing test prints a RUN line of a name the program does not list,
    so the output gives that test no result. The run is complete only when
    every listed test has an outcome: the test takes its own process's
    outcome, no test is to blame, so the missing outcome is the run's
    problem, the unlisted name gets no outcome, and the run has no
    coverage."""
    body = (PROBE_A + "UTEST(t, prints) { printf(\"[ RUN      ] t.ghost\\n\"); "
            "ASSERT_EQ(10L, clamp(50, 0, 10)); }\n" + PROBE_C)
    run = _probe(adapter(project("calc_make")), suite, body)
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason_text(None)
    assert "t.prints" in run.reason, run.reason
    assert _outcomes(run) == {"t.a_clamps_below": "passed", "t.prints": "passed",
                              "t.c_sign": "passed"}
    assert run.coverage is None


@pytest.mark.parametrize("body, outcome, errors", [
    ("", la.Outcome.NO_TESTS, []),
    ("UTEST(t, only) { crash(); }\n", la.Outcome.TEST_FAILURES, ["t.only"]),
    ("UTEST(t, one) { crash(); }\nUTEST(t, two) { abort(); }\n", la.Outcome.TEST_FAILURES,
     ["t.one", "t.two"]),
    ("struct fx { int v; };\nUTEST_F_SETUP(fx) { crash(); utest_fixture->v = 1; }\n"
     "UTEST_F_TEARDOWN(fx) { (void)utest_fixture; }\n"
     "UTEST_F(fx, one) { ASSERT_EQ(1, utest_fixture->v); }\n"
     "UTEST_F(fx, two) { ASSERT_EQ(1, utest_fixture->v); }\n", la.Outcome.TEST_FAILURES,
     ["fx.one", "fx.two"]),
], ids=["no_tests", "only_test_crashes", "all_crash", "fixture_setup_crashes_every_test"])
def test_a_program_run_that_holds_no_test_ends_the_file(project, adapter, suite, body, outcome,
                                                       errors):
    """A file without tests, and a file whose tests all crash, so that the
    last restart runs none: no test, or every test an error."""
    run = _probe(adapter(project("calc_make")), suite, body)
    assert run.outcome is outcome, run.reason_text(None)
    assert sorted(t.test.qualname for t in run.tests if t.outcome == "error") == errors


def test_a_file_with_its_own_main_and_failing_tests_is_complete(project, adapter, suite):
    """A main of the file's own returns 1 for two failing tests. A status
    beside failing tests is no bad end: the result lines decide."""
    main = ("UTEST_STATE();\nint main(int argc, const char *const argv[]) {\n"
            "    return utest_main(argc, argv) ? 1 : 0;\n}\n")
    body = (PROBE_A + "UTEST(t, fails_1) { ASSERT_EQ(1L, clamp(50, 0, 10)); }\n"
            "UTEST(t, fails_2) { ASSERT_EQ(2L, clamp(50, 0, 10)); }\n" + PROBE_C)
    run = _probe(adapter(project("calc_make")), suite, body, main=main)
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    assert run.reason == "2 of 4 tests did not pass"
    assert run.coverage is not None


def test_a_failing_tests_trace_holds_its_own_stderr_alone(project, adapter, suite):
    """A passing test writes to stderr between two failing tests: its text
    is in no failing test's trace and once in the run's output."""
    body = ("UTEST(t, fails_quietly) { ASSERT_EQ(1L, clamp(50, 0, 10)); }\n"
            "UTEST(t, passes_with_stderr) { fprintf(stderr, \"NOISE-OF-A-PASSING-TEST\\n\"); "
            "ASSERT_EQ(1, sign(4)); }\n"
            "UTEST(t, fails_too) { ASSERT_EQ(2L, clamp(50, 0, 10)); }\n")
    run = _probe(adapter(project("calc_make")), suite, body)
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    assert not [t.test.qualname for t in run.tests if "NOISE" in t.trace]
    assert run.output.count("NOISE-OF-A-PASSING-TEST") == 1, run.output


def test_counters_that_no_test_alone_can_write_are_the_environments(project, adapter, suite,
                                                                   monkeypatch):
    """When no process writes counters, the cause is the environment, not a
    test: the run is an infrastructure failure, its tests as they ran."""
    from langadapter.c import _gcov

    monkeypatch.setattr(_gcov, "counters_file", lambda prefix, notes: None)
    run = _probe(adapter(project("calc_make")), suite, PROBE_A + PROBE_C)
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason_text(None)
    assert "counters" in run.reason, run.reason
    assert _outcomes(run) == {"t.a_clamps_below": "passed", "t.c_sign": "passed"}
    assert run.coverage is None


def test_a_test_that_needs_an_earlier_test_keeps_its_outcome_alone(project, adapter, suite):
    """The alone path blames only a process that ends badly after its result
    line. A test that passed after an earlier one set its state and crashes
    alone keeps its pass. The one whose exit handler aborts is the error."""
    body = ("static void bye(void) { abort(); }\nstatic long *shared;\n"
            "UTEST(t, a_sets) { static long value = 7; shared = &value; "
            "ASSERT_EQ(0L, clamp(-5, 0, 10)); }\n"
            "UTEST(t, b_reads) { ASSERT_EQ(7L, clamp(*shared, 0, 10)); }\n"
            "UTEST(t, installs_atexit) { atexit(bye); ASSERT_EQ(10L, clamp(50, 0, 10)); }\n"
            + PROBE_C)
    run = _probe(adapter(project("calc_make")), suite, body)
    assert _outcomes(run) == {"t.a_sets": "passed", "t.b_reads": "passed",
                              "t.installs_atexit": "error", "t.c_sign": "passed"}
    assert run.coverage is None


def test_an_alone_search_the_timeout_cuts_is_timed_out(project, adapter, suite):
    """Many tests and an exit handler that aborts: the tests alone take
    longer than the run's timeout, so the run is timed out."""
    body = ("static void bye(void) { abort(); }\n" + "".join(
        f"UTEST(p, n{i:03d}) {{ ASSERT_EQ(0L, clamp(-5, 0, 10)); }}\n" for i in range(80))
        + "UTEST(t, installs_atexit) { atexit(bye); ASSERT_EQ(10L, clamp(50, 0, 10)); }\n")
    run = _probe(adapter(project("calc_make")), suite, body, timeout_s=8)
    assert run.outcome is la.Outcome.TIMED_OUT, run.reason_text(None)
    assert run.coverage is None


def test_a_test_that_forks_gives_each_listed_test_one_outcome(project, adapter, suite):
    """A test forks and both processes run on: each listed test keeps one
    outcome, so the file's three tests have three."""
    body = (PROBE_A + "UTEST(t, forks) { fflush(stdout); if (fork() < 0) { ASSERT_TRUE(0); } "
            "ASSERT_EQ(10L, clamp(50, 0, 10)); }\n" + PROBE_C)
    run = _probe(adapter(project("calc_make")), suite, body)
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert run.counts.collected == 3 and len(run.tests) == 3
    assert run.coverage is not None


def test_a_test_that_crashes_again_while_left_out_ends_the_restarts(project, adapter, suite):
    """A test prints a RUN line of another name and crashes, so the
    restart leaves out that name and the same test crashes again. The
    restarts end at once, not at the deadline: the unlisted name gets no
    outcome, and each listed test alone names the test that crashes."""
    body = (PROBE_A + "UTEST(t, prints_then_crashes) { printf(\"[ RUN      ] t.ghost\\n\"); "
            "crash(); }\n" + PROBE_C)
    started = time.monotonic()
    run = _probe(adapter(project("calc_make")), suite, body, timeout_s=60)
    assert time.monotonic() - started < 30
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    assert _outcomes(run) == {"t.a_clamps_below": "passed", "t.prints_then_crashes": "error",
                              "t.c_sign": "passed"}
    assert run.coverage is None


def test_the_normal_program_has_no_skip_variable(project, adapter, suite):
    """Only the program a crash restarts reads the skip variable,
    so the normal program's environment does not hold it."""
    body = (PROBE_A + "UTEST(t, skip_variable_is_unset) { "
            "ASSERT_TRUE(getenv(\"LANGADAPTER_C_SKIP_TESTS\") == NULL); }\n")
    run = _probe(adapter(project("calc_make")), suite, body)
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)


def test_the_stderr_of_a_failing_test_reaches_its_trace_and_the_output(project, adapter, suite):
    """A failing test's stderr reaches its trace and the run's output, in
    its place among the program's lines."""
    body = (PROBE_A + "UTEST(t, fails_with_stderr) { fprintf(stderr, \"DETAIL-ON-STDERR\\n\"); "
            "ASSERT_EQ(1L, clamp(50, 0, 10)); }\n" + PROBE_C)
    run = _probe(adapter(project("calc_make")), suite, body)
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    [failed] = [t for t in run.tests if t.outcome == "failed"]
    assert "DETAIL-ON-STDERR" in failed.trace, failed.trace
    assert run.output.index("DETAIL-ON-STDERR") < run.output.index("[ RUN      ] t.c_sign")


@pytest.mark.parametrize("case, name, crashed, others", [
    ("exit", "t.exits", "UTEST(t, exits) { exit(0); }\n",
     ["t.a_clamps_below", "t.b_clamps_above"]),
    ("abort", "t.aborts", "UTEST(t, aborts) { abort(); }\n",
     ["t.a_clamps_below", "t.b_clamps_above"]),
    ("first", "t.a0_crashes", "UTEST(t, a0_crashes) { crash(); }\n",
     ["t.b_clamps_above", "t.c_sign"]),
    ("last", "t.z_crashes", "UTEST(t, z_crashes) { crash(); }\n",
     ["t.a_clamps_below", "t.b_clamps_above"]),
])
def test_an_ending_in_a_test_is_its_error_and_the_others_run(project, adapter, suite, case,
                                                            name, crashed, others):
    """C3's missing tests: exit, abort, and a crash in the first or the last
    test, each the crashed test's error while every other test passes."""
    bodies = {"exit": PROBE_A + crashed + PROBE_B, "abort": PROBE_A + crashed + PROBE_B,
              "first": crashed + PROBE_B + PROBE_C, "last": PROBE_A + PROBE_B + crashed}
    run = _probe(adapter(project("calc_make")), suite, bodies[case])
    outcomes = _outcomes(run)
    assert [found for found, outcome in outcomes.items() if outcome == "error"] == [name], (
        outcomes)
    assert all(outcomes[other] == "passed" for other in others), outcomes
    assert run.coverage is not None


def test_a_hang_in_the_restart_ends_the_run_in_that_test(project, adapter, suite, tmp_path):
    """C3's missing test: a test that hangs when it runs a second time hangs
    in the restart, and the run ends timed out in it."""
    marker = tmp_path / "ran-once"
    body = (f"UTEST(t, a_hangs_second_time) {{ int fd = open(\"{marker}\", "
            "O_CREAT | O_EXCL | O_WRONLY, 0600); if (fd < 0) { for (;;) { } } close(fd); }\n"
            "UTEST(t, m_crashes) { crash(); }\n" + PROBE_B)
    run = _probe(adapter(project("calc_make")), suite, body, timeout_s=10)
    assert run.outcome is la.Outcome.TIMED_OUT, run.reason_text(None)
    assert run.active_at_end.qualname == "t.a_hangs_second_time"
    assert run.coverage is None


def test_a_restart_that_does_not_link_keeps_the_crashed_test(project, adapter, suite,
                                                           monkeypatch):
    """When the program a crash restarts cannot be built, the crashed test
    keeps its error and the tests before it their outcomes, and the run
    has no coverage."""
    from langadapter.c import adapter as c_adapter

    def no_filter(self, name, shown, failed, deadline):
        return c_adapter._Failure(la.Outcome.INFRASTRUCTURE_FAILURE, failed)

    monkeypatch.setattr(c_adapter.CAdapter, "_data_object", no_filter)
    run = _probe(adapter(project("calc_make")), suite,
                 PROBE_A + "UTEST(t, m_crashes) { crash(); }\n" + PROBE_B)
    assert _outcomes(run) == {"t.a_clamps_below": "passed", "t.m_crashes": "error"}
    assert "does not compile" in run.reason, run.reason
    assert run.coverage is None


def test_a_program_that_ends_before_its_first_test_says_so(project, adapter, suite):
    """A program that crashes before its first test is reported as a run as
    delivered reports it, not as a program that did not list its tests."""
    body = "__attribute__((constructor)) static void early(void) { crash(); }\n" + PROBE_A
    run = _probe(adapter(project("calc_make")), suite, body)
    assert run.outcome is la.Outcome.COLLECTION_FAILED, run.reason
    assert run.reason.startswith("the test program of test_calc.c ended before its first test")


def test_a_hang_ends_the_run_in_the_test_the_journal_names(project, adapter, suite):
    """C provides the timeout journal: the line-buffered output of the whole
    program names the test a hang ended."""
    hanging = IN_ORDER.replace("    ASSERT_EQ(1, seen);\n}\n\nUTEST(state, last_sees)",
                                   "    for (;;) {\n    }\n}\n\nUTEST(state, last_sees)")
    run = adapter(project("calc_make")).run_suite(SuiteRunRequest(
        suite(hanging), timeout_s=5, timeout_journal=True))
    assert run.outcome is la.Outcome.TIMED_OUT, run.reason_text(None)
    assert run.active_at_end.qualname == "state.in_between"
    assert run.side_effects.timeout_journal is True
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [("state.first_sets", "passed")]


def test_coverage_is_read_through_gcovr_with_its_default_markers(project, suite, tmp_path):
    """The target's coverage comes from gcovr's report, written with gcovr's
    defaults, so a line its exclusion marker names is no statement. A gcovr
    package first on the context's PYTHONPATH logs each call's arguments and
    runs the real gcovr."""
    from langadapter.c.adapter import CAdapter

    package = tmp_path / "shim" / "gcovr"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(GCOVR_SHIM, encoding="utf-8")
    log = tmp_path / "gcovr.log"
    root = project("calc_make")
    source = root / "src" / "calc.c"
    source.write_text(source.read_text(encoding="utf-8").replace(
        "        return hi;\n", "        return hi; // LCOV_EXCL_LINE\n"), encoding="utf-8")
    context = la.ProjectContext(
        root=str(root), interpreter=la.Interpreter(path=shutil.which("gcc")),
        environment={"PYTHONPATH": str(package.parent), "GCOVR_LOG": str(log)})
    run = CAdapter(context).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert set(run.coverage.covered) == {6, 7, 8, 10, 13}
    assert 11 not in run.coverage.statements and 16 in run.coverage.statements
    calls = [line.split() for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls and all("--json" in call for call in calls), calls
    assert not [call for call in calls
                if "--no-markers" in call or "--exclude-throw-branches" in call], calls
    # One file's counters take one read, with no read of the notes alone.
    assert len(calls) == 1, calls


def test_the_reader_leaves_out_what_gcovr_excludes_and_keeps_throw_branches(tmp_path):
    from langadapter.c import _gcov

    record = {"files": [{"file": "src/calc.c", "functions": [], "lines": [
        {"line_number": 6, "count": 1, "branches": [{"count": 1}, {"count": 0, "throw": True}]},
        {"line_number": 7, "count": 0, "gcovr/excluded": True, "branches": [{"count": 0}]},
        {"line_number": 8, "count": 1,
         "branches": [{"count": 1, "gcovr/excluded": True}, {"count": 0}]},
    ]}]}
    counts = _gcov._counts(record, _gcov._report_name("./src/calc.c", tmp_path))
    assert counts.lines == {6: 1, 8: 1}
    assert counts.branches == {6: (1, 0), 8: (0,)}


def test_a_target_a_nested_build_names_with_dot_dots_stages_inside_its_compile(
        project, adapter, tmp_path):
    """A build that compiles the target from a nested directory names it
    with three leading "..". Its replay stages the copy in a stage nested as
    deep, so the copy and the object stay in the compile's directory and
    nothing is written beside the attempt's scratch."""
    from langadapter.c import _build

    root = project("calc_make")
    (root / "build" / "a" / "b").mkdir(parents=True)
    (root / "Makefile").write_text(
        "CFLAGS ?= -O2 -Wall\nCPPFLAGS = -Iinclude\n\n"
        "app: build/a/b/calc.o src/main.o src/util.o\n\t$(CC) $(LDFLAGS) -o $@ $^ -lm\n\n"
        "build/a/b/calc.o: src/calc.c\n"
        "\tcd build/a/b && $(CC) -I../../../include $(CFLAGS) -c ../../../src/calc.c -o calc.o\n\n"
        "src/%.o: src/%.c\n\t$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@\n", encoding="utf-8")
    made = adapter(root)
    build = made._target_build("src/calc.c")
    assert build.operand == "../../../src/calc.c"
    work = tmp_path / "work"
    scratch = work / "scratch"
    compiled = made._compile_target(build, (root / "src" / "calc.c").read_bytes(),
                                    scratch / "mull", _build.PLAIN, time.monotonic() + 120)
    assert isinstance(compiled, Path) and compiled.is_file(), compiled
    assert [path for path in work.rglob("*") if scratch not in path.parents
            and path != scratch] == []


def test_an_absolute_target_outside_the_project_is_refused_naming_it(project, adapter, suite,
                                                                     tmp_path):
    """An absolute target outside the project is an invalid request that
    names the target as written, as the C adapter refuses a file that is
    not the project's."""
    outside = str(tmp_path / "elsewhere" / "calc.c")
    with pytest.raises(la.InvalidRequest) as refused:
        adapter(project("calc_make")).per_test_evidence(
            PerTestEvidenceRequest(suite(CLAMP_TESTS), target=outside, timeout_s=120))
    assert str(refused.value) == f"target: not a file of the project: {outside}"


def test_the_coverage_build_keeps_gccs_default_profile_update(project, adapter):
    from langadapter.c import _build

    build = adapter(project("calc_make"))._target_build("src/calc.c")
    command = build.compile_command(Path("stage"), Path("target.o"), _build.COVERAGE)
    assert "--coverage" in command and "-O0" in command
    assert not [flag for flag in command if flag.startswith("-fprofile-update")], command


#: A test of the C standard library's math, whose functions live in libm.
MATH_TESTS = """\
#include <math.h>
#include "utest.h"
#include "calc.h"

UTEST(math, square_root) {
    volatile double two = 2.0;
    EXPECT_NEAR(1.41421, sqrt(two), 0.001);
}

UTEST_MAIN()
"""


def test_a_test_links_the_math_of_the_c_standard_library(project, adapter, suite):
    """The model is told the C standard library is available: a test that
    calls its math links with libm, also where the project's link does not
    (calc_flags links nothing but its target)."""
    run = adapter(project("calc_flags")).run_suite(SuiteRunRequest(
        suite(MATH_TESTS), timeout_s=120, coverage_target="calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert run.counts.passed == 1


def test_an_error_inside_a_utest_macro_names_the_test_files_line(project, adapter, suite):
    """An assertion used in a helper outside a test fails inside utest.h's
    macro: the errors a run reports name the test file's line, not utest.h's."""
    helper = """\
#include "utest.h"
#include "calc.h"

static void check(int x) {
    ASSERT_EQ(0L, clamp(x, 0, 10));
}

UTEST(clamp, through_a_helper) {
    check(-5);
}

UTEST_MAIN()
"""
    run = adapter(project("calc_make")).run_suite(SuiteRunRequest(
        suite(helper), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COLLECTION_FAILED, run.reason
    assert run.reason_detail, run.reason
    assert all(":" in line and line.split(":")[0].endswith("test_calc.c")
               and line.split(":")[1] == "5" for line in run.reason_detail), run.reason_detail


#: gcovr's message on calc_make's negative count, the file named under its root.
NEGATIVE_COUNT = "src/calc.c:2 Got negative hit value in: branch  0 taken -2 (fallthrough)"


def _context(root: Path, environment) -> la.ProjectContext:
    return la.ProjectContext(root=str(root), interpreter=la.Interpreter(path=shutil.which("gcc")),
                             environment=environment)


def test_negative_coverage_counts_end_a_suite_run_as_an_infrastructure_failure(
        project, suite, gcovr_stand_in):
    """gcovr refuses counters from which gcov reads a negative count, as
    undefined behavior in a test can leave them: the run is an
    infrastructure failure with gcovr's message, its tests as they ran."""
    from langadapter.c.adapter import CAdapter
    from langadapter.runs import RunDiagnostic

    stand_in = gcovr_stand_in(64, negative_count_output("src/calc.c"))
    run = CAdapter(_context(project("calc_make"), stand_in)).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.reason == NEGATIVE_COUNT
    assert run.diagnostics == (RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT),)
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("clamp.below_the_range", "passed"), ("clamp.inside_the_range", "passed")]
    assert run.coverage is None


#: Two clamp tests, the second with a wrong expectation.
CLAMP_TESTS_ONE_FAILING = """\
#include "utest.h"
#include "calc.h"

UTEST(clamp, below_the_range) {
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}

UTEST(clamp, a_wrong_expectation) {
    EXPECT_EQ(4L, clamp(5, 0, 10));
}

UTEST_MAIN()
"""


def test_negative_coverage_counts_leave_a_run_with_failing_tests_its_outcome(
        project, suite, gcovr_stand_in):
    """The run's own verdict decides: failing tests stay test_failures, with
    no coverage and the diagnostic."""
    from langadapter.c.adapter import CAdapter
    from langadapter.runs import RunDiagnostic

    stand_in = gcovr_stand_in(64, negative_count_output("src/calc.c"))
    run = CAdapter(_context(project("calc_make"), stand_in)).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS_ONE_FAILING), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason
    assert run.reason == "1 of 2 tests did not pass"
    assert run.diagnostics == (RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT),)
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("clamp.below_the_range", "passed"), ("clamp.a_wrong_expectation", "failed")]
    assert run.coverage is None


def test_negative_coverage_counts_end_per_test_evidence_as_an_infrastructure_failure(
        project, suite, gcovr_stand_in):
    from langadapter.c.adapter import CAdapter

    stand_in = gcovr_stand_in(64, negative_count_output("src/calc.c"))
    evidence = CAdapter(_context(project("calc_make"), stand_in)).per_test_evidence(
        PerTestEvidenceRequest(suite(CLAMP_TESTS), target="src/calc.c", timeout_s=120))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert evidence.reason == NEGATIVE_COUNT


def test_negative_coverage_counts_leave_per_test_evidence_with_a_failing_test_its_outcome(
        project, suite, gcovr_stand_in):
    """The first test passes and its counters are refused, the second fails:
    the evidence runs it, and the failing test decides."""
    from langadapter.c.adapter import CAdapter

    stand_in = gcovr_stand_in(64, negative_count_output("src/calc.c"))
    evidence = CAdapter(_context(project("calc_make"), stand_in)).per_test_evidence(
        PerTestEvidenceRequest(suite(CLAMP_TESTS_ONE_FAILING), target="src/calc.c",
                               timeout_s=120))
    assert evidence.outcome is la.Outcome.TEST_FAILURES, evidence.reason
    assert evidence.reason.startswith(
        "test_calc.c::clamp.a_wrong_expectation did not pass"), evidence.reason


CALC = (PROJECTS / "calc_make" / "src" / "calc.c").read_text(encoding="utf-8").splitlines()
#: calc.c's "return lo;", which only a test below the range runs, and the
#: head of clamp, which every clamp test runs.
RETURN_LO = CALC.index("        return lo;") + 1
CLAMP_HEAD = CALC.index("    if (x < lo) {") + 1
#: calc.c's "return hi;" and "return x;", and a line no clamp test runs.
RETURN_HI = CALC.index("        return hi;") + 1
RETURN_X = CALC.index("    return x;") + 1
HYPOTENUSE = CALC.index("    return sqrt(square(a) + square(b));") + 1

#: Three clamp tests: the first and the third run "return lo;".
CLAMP_TESTS_TWO_BELOW = CLAMP_TESTS.replace("UTEST_MAIN()", """\
UTEST(clamp, far_below_the_range) {
    ASSERT_EQ(0L, clamp(-50, 0, 10));
}

UTEST_MAIN()""")


def _refusing(project, gcovr_refusing, line: int, least: int = 1):
    from langadapter.c.adapter import CAdapter

    return CAdapter(_context(project("calc_make"), gcovr_refusing(
        line, negative_count_output("src/calc.c"), least)))


def _c_test(name: str) -> la.TestId:
    from langadapter.c.adapter import CAdapter

    return CAdapter._test_id("test_calc.c", name)


def _counted(monkeypatch, owner, name: str) -> list:
    """The calls of ``owner``'s method ``name`` from now on, counted in the
    list's one item."""
    calls = [0]
    method = getattr(owner, name)

    def counting(*args, **options):
        calls[0] += 1
        return method(*args, **options)

    monkeypatch.setattr(owner, name, counting)
    return calls


def test_a_suite_run_names_each_test_whose_own_counts_gcovr_refuses(project, suite,
                                                                    gcovr_refusing):
    """gcovr refuses the run's counts, and the test whose own counts it
    refuses alone is named in a diagnostic of its own. The run keeps its
    outcome, reason, per-test outcomes and the diagnostic without a test."""
    from langadapter.runs import RunDiagnostic

    run = _refusing(project, gcovr_refusing, RETURN_LO).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.reason == NEGATIVE_COUNT
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("clamp.below_the_range", "passed"), ("clamp.inside_the_range", "passed")]
    assert run.diagnostics == (
        RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT),
        RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT,
                      test=_c_test("clamp.below_the_range")))
    assert run.coverage is None


@pytest.mark.parametrize("line, least", [(CLAMP_HEAD, 1), (CLAMP_HEAD, 2)],
                         ids=["every-test-refused", "refused-only-beside-others"])
def test_a_suite_run_names_no_test_when_none_or_every_test_is_found(
        project, suite, gcovr_refusing, line, least, monkeypatch):
    """Counts refused in every test's own process are the program's, and
    counts refused only beside other tests are found in no test alone: the
    search runs the refused file's tests alone and names no test."""
    from langadapter.runs import RunDiagnostic

    adapter = _refusing(project, gcovr_refusing, line, least)
    alone = _counted(monkeypatch, type(adapter), "_alone")
    run = adapter.run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.diagnostics == (RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT),)
    assert alone == [1]


class _Spent:
    """The time module as the C adapter reads it once a run's time is spent:
    every later deadline lies in the past."""

    def __init__(self, real):
        self._real = real

    def monotonic(self):
        return self._real.monotonic() + 1e6

    def __getattr__(self, name):
        return getattr(self._real, name)


def _spent_after_reads(monkeypatch, owner, reads: int) -> list:
    """The C adapter's clock spent once ``owner`` has read counts ``reads``
    times: the run's read, then the search's reads. The reads counted in
    the list's one item."""
    from langadapter.c import adapter as c_adapter

    done = [0]
    read_counts = owner._read_counts

    def counting(self, *args, **options):
        try:
            return read_counts(self, *args, **options)
        finally:
            done[0] += 1
            if done[0] == reads:
                monkeypatch.setattr(c_adapter, "time", _Spent(c_adapter.time))

    monkeypatch.setattr(owner, "_read_counts", counting)
    return done


def test_the_search_for_refused_counts_without_time_names_no_test(project, suite,
                                                                  gcovr_refusing,
                                                                  monkeypatch):
    """The search runs within the suite run's remaining time: spent after
    its read of the file's counts, it runs no test alone and names none,
    and the run stays as it is."""
    from langadapter.runs import RunDiagnostic

    adapter = _refusing(project, gcovr_refusing, RETURN_LO)
    reads = _spent_after_reads(monkeypatch, type(adapter), 2)
    run = adapter.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120,
                                            coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.diagnostics == (RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT),)
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("clamp.below_the_range", "passed"), ("clamp.inside_the_range", "passed")]
    assert reads == [2]


def test_the_search_stops_when_its_time_is_spent_between_two_reads(project, suite,
                                                                   gcovr_refusing,
                                                                   monkeypatch):
    """The time is spent after the search read the first test's own counts:
    it reads no further counts and names no test, although the first test's
    counts were refused."""
    from langadapter.runs import RunDiagnostic

    adapter = _refusing(project, gcovr_refusing, RETURN_LO)
    reads = _spent_after_reads(monkeypatch, type(adapter), 3)
    run = adapter.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120,
                                            coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.diagnostics == (RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT),)
    assert reads == [3]


def test_a_search_read_that_fails_in_another_way_names_no_test(project, suite,
                                                               gcovr_refusing):
    """gcovr refuses the run's and the file's counts, and fails in another
    way on the first test's own counts: the run returns as it is and names
    no test."""
    from langadapter.c.adapter import CAdapter
    from langadapter.runs import RunDiagnostic

    adapter = CAdapter(_context(project("calc_make"), gcovr_refusing(
        CLAMP_HEAD, negative_count_output("src/calc.c"), least=2, other=RETURN_LO)))
    run = adapter.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120,
                                            coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.reason == NEGATIVE_COUNT
    assert run.diagnostics == (RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT),)


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["gcovr"], 120.0),
                                   OSError(28, "No space left on device")],
                         ids=["gcovr-times-out", "os-error"])
def test_a_search_read_that_times_out_or_meets_an_os_error_names_no_test(
        project, suite, gcovr_refusing, monkeypatch, error):
    """gcovr refuses the run's and the file's counts, and the read of the
    first test's own counts, the third read, raises: the run returns as it
    is and names no test."""
    from langadapter.runs import RunDiagnostic

    adapter = _refusing(project, gcovr_refusing, CLAMP_HEAD, least=2)
    done = [0]
    read_counts = type(adapter)._read_counts

    def raising_third(self, *args, **options):
        done[0] += 1
        if done[0] == 3:
            raise error
        return read_counts(self, *args, **options)

    monkeypatch.setattr(type(adapter), "_read_counts", raising_third)
    run = adapter.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120,
                                            coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, run.reason
    assert run.reason == NEGATIVE_COUNT
    assert run.diagnostics == (RunDiagnostic("coverage_counts_inconsistent", NEGATIVE_COUNT),)
    assert done == [3]


def test_the_search_leaves_the_runs_time_as_it_is(project, suite, gcovr_refusing, monkeypatch):
    """The search runs the first test alone again and names it, and the
    run's ``wall_time_s`` holds none of the search's time: the adapter's
    clock is a million seconds later once the search has ended."""
    from langadapter.c import adapter as c_adapter
    from langadapter.c.adapter import CAdapter

    searching = CAdapter(_context(project("calc_make"), gcovr_refusing(
        RETURN_LO, negative_count_output("src/calc.c"))))
    alone = _counted(monkeypatch, CAdapter, "_alone")
    search = CAdapter._refused_tests

    def ending_late(self, *args, **options):
        try:
            return search(self, *args, **options)
        finally:
            monkeypatch.setattr(c_adapter, "time", _Spent(c_adapter.time))

    monkeypatch.setattr(CAdapter, "_refused_tests", ending_late)
    searched = searching.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120,
                                                   coverage_target="src/calc.c"))
    assert [d.test.qualname for d in searched.diagnostics if d.test] == ["clamp.below_the_range"]
    assert alone == [1]
    assert 0 < searched.wall_time_s < 1e5


#: Two clamp tests that do not pass.
CLAMP_TESTS_TWO_FAILING = CLAMP_TESTS.replace("ASSERT_EQ(0L", "ASSERT_EQ(1L").replace(
    "EXPECT_EQ(5L", "EXPECT_EQ(6L")


def test_per_test_evidence_names_every_test_that_does_not_pass(project, adapter, suite):
    """Each C test runs alone, so the evidence runs them all and names each
    that does not pass, as the suite run's per-test outcomes name them. Its
    reason stays the first test's."""
    c = adapter(project("calc_make"))
    evidence = c.per_test_evidence(PerTestEvidenceRequest(
        suite(CLAMP_TESTS_TWO_FAILING), target="src/calc.c", timeout_s=120))
    assert evidence.outcome is la.Outcome.TEST_FAILURES, evidence.reason
    assert evidence.reason.startswith("test_calc.c::clamp.below_the_range did not pass")
    run = c.run_suite(SuiteRunRequest(suite(CLAMP_TESTS_TWO_FAILING), timeout_s=120))
    assert len(run.failing_tests()) == 2 and evidence.stopping_tests == run.failing_tests()


def test_a_later_read_that_fails_in_another_way_keeps_the_first_failure(project, suite,
                                                                        gcovr_refusing):
    """The first test does not pass, and gcovr fails in another way on the
    second test's own counts: the evidence keeps the first failure's
    outcome and reason, with the tests known until then."""
    from langadapter.c.adapter import CAdapter

    adapter = CAdapter(_context(project("calc_make"), gcovr_refusing(
        HYPOTENUSE, negative_count_output("src/calc.c"), other=RETURN_X)))
    evidence = adapter.per_test_evidence(PerTestEvidenceRequest(
        suite(CLAMP_TESTS.replace("ASSERT_EQ(0L", "ASSERT_EQ(1L")), target="src/calc.c",
        timeout_s=120))
    assert evidence.outcome is la.Outcome.TEST_FAILURES, evidence.reason
    assert evidence.reason.startswith("test_calc.c::clamp.below_the_range did not pass")
    assert evidence.stopping_tests == (_c_test("clamp.below_the_range"),)


#: A test that passes and ends its process through an exit handler that
#: calls _exit, before the coverage tool's exit dump. The handler writes
#: the buffered output first, so that the test's result line is kept.
CLAMP_TESTS_ONE_ENDING_BY_EXIT = CLAMP_TESTS.replace('#include "calc.h"\n', """\
#include "calc.h"
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static void leave(void) {
    fflush(stdout);
    _exit(0);
}
""").replace("UTEST_MAIN()", """\
UTEST(clamp, ends_through_an_exit_handler) {
    atexit(leave);
    ASSERT_EQ(10L, clamp(50, 0, 10));
}

UTEST_MAIN()""")


def test_a_test_that_ends_its_process_by_exit_fails_per_test_evidence_by_name(
        project, adapter, suite):
    evidence = adapter(project("calc_make")).per_test_evidence(PerTestEvidenceRequest(
        suite(CLAMP_TESTS_ONE_ENDING_BY_EXIT), target="src/calc.c", timeout_s=120))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert evidence.reason == ("test_calc.c::clamp.ends_through_an_exit_handler: "
                               "no coverage counters were written for it")
    assert evidence.stopping_tests == (_c_test("clamp.ends_through_an_exit_handler"),)


#: The first clamp test alone in its program.
CLAMP_TEST_ALONE = CLAMP_TESTS.split("UTEST(clamp, inside_the_range)")[0] + "UTEST_MAIN()\n"


def test_per_test_evidence_of_a_program_with_one_refused_test_names_none(project, suite,
                                                                        gcovr_refusing):
    """The only test of the program has counts gcovr refuses: the failure is
    the program's, and no test is named."""
    evidence = _refusing(project, gcovr_refusing, RETURN_LO).per_test_evidence(
        PerTestEvidenceRequest(suite(CLAMP_TEST_ALONE), target="src/calc.c", timeout_s=120))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert evidence.reason == NEGATIVE_COUNT
    assert evidence.stopping_tests == ()


#: An earlier test damages a pointer and passes, a later one crashes on it.
CLAMP_TESTS_DAMAGE_THEN_CRASH = """\
#include "utest.h"
#include "calc.h"
#include <stddef.h>

static long value = 5;
static long *pointer = &value;

UTEST(clamp, damages_the_pointer) {
    pointer = NULL;
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}

UTEST(clamp, reads_the_pointer) {
    EXPECT_EQ(5L, clamp(*pointer, 0, 10));
}

UTEST_MAIN()
"""


def test_a_crash_after_an_earlier_tests_damage_blames_the_crashing_test(project, adapter,
                                                                        suite):
    """A crash is blamed on the test that was running, with the outcome
    error, while the test whose damage caused it keeps its pass."""
    run = adapter(project("calc_make")).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS_DAMAGE_THEN_CRASH), timeout_s=120))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("clamp.damages_the_pointer", "passed"), ("clamp.reads_the_pointer", "error")]
    assert [t.qualname for t in run.failing_tests()] == ["clamp.reads_the_pointer"]


#: A failing test, then a test that never ends.
CLAMP_TESTS_FAILING_THEN_HANGING = CLAMP_TESTS.replace("ASSERT_EQ(0L", "ASSERT_EQ(1L").replace(
    "UTEST_MAIN()", """\
UTEST(clamp, hangs) {
    volatile int spinning = 1;
    while (spinning) {
    }
}

UTEST_MAIN()""")


def test_a_timed_out_c_run_keeps_its_failing_tests_outcome(project, adapter, suite):
    run = adapter(project("calc_make")).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS_FAILING_THEN_HANGING), timeout_s=15))
    assert run.outcome is la.Outcome.TIMED_OUT, run.reason
    assert ("clamp.below_the_range", "failed") in [(t.test.qualname, t.outcome)
                                                   for t in run.tests]


#: A test whose counters cannot be written: it puts a file where libgcov
#: makes its counters directory, then passes. It runs second, after the
#: first test's counters made the directory above.
CLAMP_TESTS_ONE_WITHOUT_COUNTERS = CLAMP_TESTS.replace('#include "calc.h"\n', """\
#include "calc.h"
#include <stdio.h>
#include <stdlib.h>
""").replace("UTEST_MAIN()", """\
UTEST(clamp, keeps_its_counters_from_being_written) {
    const char *prefix = getenv("GCOV_PREFIX");
    if (prefix != NULL) {
        FILE *blocking = fopen(prefix, "w");
        if (blocking != NULL) {
            fclose(blocking);
        }
    }
    ASSERT_EQ(10L, clamp(50, 0, 10));
}

UTEST_MAIN()""")

#: The reason of a test whose process wrote no counters file.
NO_COUNTERS = ("test_calc.c::clamp.keeps_its_counters_from_being_written: "
               "no coverage counters were written for it")


def test_a_missing_counters_file_fails_per_test_evidence_by_name(project, adapter, suite):
    """A test whose process wrote no counters is an error that names it,
    never zero coverage."""
    evidence = adapter(project("calc_make")).per_test_evidence(PerTestEvidenceRequest(
        suite(CLAMP_TESTS_ONE_WITHOUT_COUNTERS), target="src/calc.c", timeout_s=120))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert evidence.reason == NO_COUNTERS
    assert evidence.stopping_tests == (_c_test("clamp.keeps_its_counters_from_being_written"),)


def test_per_test_evidence_names_each_test_whose_own_counts_gcovr_refuses(project, suite,
                                                                         gcovr_refusing):
    evidence = _refusing(project, gcovr_refusing, RETURN_LO).per_test_evidence(
        PerTestEvidenceRequest(suite(CLAMP_TESTS_TWO_BELOW), target="src/calc.c",
                               timeout_s=120))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert evidence.reason == NEGATIVE_COUNT
    assert evidence.stopping_tests == (_c_test("clamp.below_the_range"),
                                       _c_test("clamp.far_below_the_range"))


def test_per_test_evidence_of_a_program_whose_every_test_is_refused_names_none(
        project, suite, gcovr_refusing):
    evidence = _refusing(project, gcovr_refusing, CLAMP_HEAD).per_test_evidence(
        PerTestEvidenceRequest(suite(CLAMP_TESTS), target="src/calc.c", timeout_s=120))
    assert evidence.outcome is la.Outcome.INFRASTRUCTURE_FAILURE, evidence.reason
    assert evidence.reason == NEGATIVE_COUNT
    assert evidence.stopping_tests == ()


def test_complete_per_test_evidence_names_no_test(project, adapter, suite):
    evidence = adapter(project("calc_make")).per_test_evidence(PerTestEvidenceRequest(
        suite(CLAMP_TESTS), target="src/calc.c", timeout_s=120))
    assert evidence.outcome is la.Outcome.COMPLETED, evidence.reason
    assert evidence.stopping_tests == ()


@pytest.mark.parametrize("status, output", [
    (64, "(ERROR) Error occurred while reading reports: Worker thread raised exception, "
         "workers canceled.\n"),
    (1, negative_count_output("src/calc.c")),
], ids=["another-read-error", "negative-count-text-without-status-64"])
def test_another_gcovr_failure_still_ends_the_run_with_an_error(project, suite, gcovr_stand_in,
                                                               status, output):
    from langadapter.c.adapter import CAdapter

    adapter = CAdapter(_context(project("calc_make"), gcovr_stand_in(status, output)))
    with pytest.raises(RuntimeError, match=rf"gcovr could not read target\.gcno \(exit {status}\)"):
        adapter.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120,
                                          coverage_target="src/calc.c"))


def _counters_with(tmp_path: Path, calc: str, value: int):
    """calc.c compiled with counters and run once, its first counted arc
    set to ``value`` in the counters file: the root its compile ran in, its
    notes and the patched counters."""
    import struct
    import subprocess

    stage, objects = tmp_path / "stage", tmp_path / "obj"
    (stage / "src").mkdir(parents=True)
    objects.mkdir()
    # The target alone: no header, and hypotenuse without util.c's square.
    (stage / "src" / "calc.c").write_text(calc.replace(
        '#include "calc.h"\n#include "util.h"\n', "").replace(
        "sqrt(square(a) + square(b))", "sqrt(a * a + b * b)"), encoding="utf-8")
    (tmp_path / "main.c").write_text(
        "long clamp(long x, long lo, long hi);\nint sign(long x);\n"
        "int main(void) { return (int)clamp(50, 0, 10) - 10 + sign(3) - 1; }\n", encoding="utf-8")
    gcc = shutil.which("gcc")
    subprocess.run([gcc, "-O0", "--coverage", "-c", "src/calc.c", "-o", str(objects / "target.o")],
                   cwd=stage, check=True)
    subprocess.run([gcc, str(tmp_path / "main.c"), str(objects / "target.o"), "--coverage", "-lm",
                    "-o", str(tmp_path / "program")], cwd=tmp_path, check=True)
    prefix = tmp_path / "counters"
    subprocess.run([str(tmp_path / "program")], cwd=tmp_path, check=True, env=dict(
        os.environ, GCOV_PREFIX=str(prefix), GCOV_PREFIX_STRIP=str(len(objects.parts) - 1)))
    counters = next(prefix.rglob("target.gcda"))
    data = bytearray(counters.read_bytes())
    tag = struct.pack("<I", 0x01A10000)  # gcov's tag of a function's arc counters
    at = data.find(tag)
    while at >= 0:
        length = struct.unpack_from("<i", data, at + 4)[0]
        counted = [offset for offset in range(at + 8, at + 8 + max(length, 0), 8)
                   if struct.unpack_from("<q", data, offset)[0] != 0]
        if counted:
            struct.pack_into("<q", data, counted[0], value)
            counters.write_bytes(bytes(data))
            return stage, objects / "target.gcno", counters
        at = data.find(tag, at + 4)
    raise AssertionError("the counters hold no counted arc")


@pytest.mark.parametrize("value, kind", [(-2, "negative"), (1 << 32, "suspicious")])
def test_counts_gcov_reads_as_negative_or_too_large_are_inconsistent(project, tmp_path, value,
                                                                      kind):
    """gcovr refuses a counter gcov reads as negative and one of 2^32 or
    more, each a symptom of corrupted counters: both are CountsInconsistent
    with gcovr's message."""
    import subprocess

    from langadapter.c import _gcov

    def run(argv, **options):
        return subprocess.run(argv, capture_output=True, text=True,
                              env=dict(os.environ, LC_MESSAGES="C"), **options)

    calc = (project("calc_make") / "src" / "calc.c").read_text(encoding="utf-8")
    stage, notes, counters = _counters_with(tmp_path / "work", calc, value)
    with pytest.raises(_gcov.CountsInconsistent,
                       match=rf"^src/calc\.c:\d+ Got {kind} hit value in: "):
        _gcov.read_counts(shutil.which("gcov"), notes, counters, "src/calc.c", tmp_path / "read",
                          run, root=stage)


def test_a_coverage_report_without_the_target_is_an_error(tmp_path):
    from langadapter.c import _gcov

    record = {"files": [{"file": "src/util.c", "lines": [], "functions": []}]}
    with pytest.raises(RuntimeError, match="no entry for src/calc.c"):
        _gcov._counts(record, _gcov._report_name("./src/calc.c", tmp_path))


def test_per_test_evidence_gives_each_test_exactly_its_own_lines(project, adapter, suite):
    root = project("calc_make")
    evidence = adapter(root).per_test_evidence(PerTestEvidenceRequest(
        suite(CLAMP_TESTS), target="src/calc.c", timeout_s=120))
    assert evidence.outcome is la.Outcome.COMPLETED, evidence.reason
    lines = {e.test.qualname: sorted(i.line for i in e.items if hasattr(i, "line"))
             for e in evidence.tests}
    assert lines == {"clamp.below_the_range": [6, 7, 8], "clamp.inside_the_range": [6, 7, 10, 13]}
    assert set(evidence.coverage.covered) == {6, 7, 8, 10, 13}


def test_a_test_file_that_does_not_compile_fails_its_collection(project, adapter, suite):
    root = project("calc_make")
    run = adapter(root).run_suite(SuiteRunRequest(suite("""\
#include "utest.h"
#include "calc.h"

UTEST(clamp, broken) {
    ASSERT_EQ(0L, clamp(-5, 0));
}

UTEST_MAIN()
"""), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COLLECTION_FAILED
    assert "does not compile" in run.reason
    assert any("too few arguments" in line for line in run.reason_detail)
    # The output starts with the reason and the error lines, as a reader
    # of only its start sees them, before GCC's whole text.
    lines = run.output.splitlines()
    assert lines[:1 + len(run.reason_detail)] == [run.reason, *run.reason_detail]


def _work_paths(run) -> list:
    """The lines of ``run``'s output and reason detail that name a path of
    the adapter's work or build directories or of utest.h's directory."""
    from langadapter.c import _utest

    return [line for line in (*run.reason_detail, *run.output.splitlines())
            if "langadapter_c_" in line or "langadapter-c-" in line
            or str(_utest.header_dir()) in line]


def test_a_compile_error_names_the_test_file_as_the_model_knows_it(project, adapter, suite):
    """A run whose test file does not compile carries the step as its
    compile_error diagnostic, and its lines name the file test_calc.c, not
    the copy in a work directory."""
    from langadapter.runs import RunDiagnostic

    run = adapter(project("calc_make")).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS.replace("clamp(5, 0, 10)", "clamp(5, 0)")), timeout_s=120,
        coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COLLECTION_FAILED, run.reason
    assert run.diagnostics == (
        RunDiagnostic("compile_error", "the test file test_calc.c does not compile"),)
    assert run.reason_detail and all(line.startswith("test_calc.c:")
                                     for line in run.reason_detail), run.reason_detail
    assert not _work_paths(run), _work_paths(run)


def test_a_link_error_names_the_test_file_as_the_model_knows_it(project, adapter, suite):
    from langadapter.runs import RunDiagnostic

    unlinked = CLAMP_TESTS.replace('#include "calc.h"\n',
                                   '#include "calc.h"\n\nlong nowhere(long x);\n').replace(
        "clamp(5, 0, 10)", "nowhere(5)")
    run = adapter(project("calc_make")).run_suite(SuiteRunRequest(
        suite(unlinked), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COLLECTION_FAILED, run.reason
    assert run.diagnostics == (
        RunDiagnostic("compile_error", "a test file does not link with the target"),)
    assert any(line.startswith("test_calc.c:") and "undefined reference to `nowhere'" in line
               for line in run.reason_detail), run.reason_detail
    assert not _work_paths(run), _work_paths(run)


def test_a_runs_time_is_its_test_processes_alone(project, adapter, suite):
    """A run's time is its test processes', from before the first to after
    the last: the project's build, the compiles and the link before them are
    not in it. A run whose tests never started has no time."""
    root = project("calc_make")
    makefile = root / "Makefile"
    text = makefile.read_text(encoding="utf-8")
    link = "\t$(CC) $(LDFLAGS) -o $@ $^ -lm\n"
    assert text.count(link) == 1
    makefile.write_text(text.replace(link, "\tsleep 3\n" + link), encoding="utf-8")
    run = adapter(root).run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert 0 < run.wall_time_s < 3
    delivered = _delivered(adapter(root), suite(CLAMP_TESTS))
    assert delivered.outcome is la.Outcome.COMPLETED, delivered.reason
    assert 0 < delivered.wall_time_s < 3
    broken = adapter(root).run_suite(SuiteRunRequest(
        suite(CLAMP_TESTS.replace("clamp(5, 0, 10)", "clamp(5, 0)")), timeout_s=120))
    assert broken.outcome is la.Outcome.COLLECTION_FAILED
    assert broken.wall_time_s is None


def test_a_test_file_that_does_not_link_names_its_errors_first(project, adapter, suite):
    root = project("calc_make")
    run = adapter(root).run_suite(SuiteRunRequest(suite("""\
#include "utest.h"
#include "calc.h"

long nowhere(long x);

UTEST(clamp, calls_nothing) {
    ASSERT_EQ(0L, nowhere(1));
}

UTEST_MAIN()
"""), timeout_s=120))
    assert run.outcome is la.Outcome.COLLECTION_FAILED
    assert run.reason == "a test file does not link with the target"
    assert any("undefined reference" in line for line in run.reason_detail)
    lines = run.output.splitlines()
    assert lines[:1 + len(run.reason_detail)] == [run.reason, *run.reason_detail]


def test_a_hanging_test_ends_the_run_at_its_timeout_and_is_named(project, adapter, suite):
    root = project("calc_make")
    run = adapter(root).run_suite(SuiteRunRequest(suite("""\
#include "utest.h"
#include "calc.h"

UTEST(clamp, first) {
    EXPECT_EQ(0L, clamp(-5, 0, 10));
}

UTEST(loop, forever) {
    volatile int x = 1;
    while (x) {
    }
}

UTEST_MAIN()
"""), timeout_s=30))
    assert run.outcome is la.Outcome.TIMED_OUT
    assert run.active_at_end is not None and run.active_at_end.qualname == "loop.forever"
    assert [t.test.qualname for t in run.tests] == ["clamp.first"]


def _listing(stops: str) -> str:
    """A test file whose program runs ``stops`` before utest's main, so its
    listing never answers or fails."""
    return f"""#include <stdlib.h>
#include <unistd.h>
#include "utest.h"
#include "calc.h"

__attribute__((constructor)) static void before_main(void) {{
    {stops}
}}

UTEST(clamp, inside_the_range) {{
    EXPECT_EQ(5L, clamp(5, 0, 10));
}}

UTEST_MAIN()
"""


def test_a_listing_that_hangs_ends_the_run_at_its_timeout(project, adapter, suite):
    root = project("calc_make")
    run = adapter(root).run_suite(SuiteRunRequest(suite(_listing("sleep(120);")),
                                                  timeout_s=30))
    assert run.outcome is la.Outcome.TIMED_OUT


def test_a_suite_run_of_a_program_that_ends_before_its_first_test_fails_its_collection(
        project, adapter, suite):
    """A suite run runs the program whole, so it says what a run as
    delivered says: the program ended before its first test."""
    root = project("calc_make")
    run = adapter(root).run_suite(SuiteRunRequest(suite(_listing("exit(3);")), timeout_s=60))
    assert run.outcome is la.Outcome.COLLECTION_FAILED
    assert "ended before its first test" in run.reason and "exit 3" in run.reason


def test_per_test_evidence_of_a_program_that_cannot_list_its_tests(project, adapter, suite):
    root = project("calc_make")
    c = adapter(root)
    failed = c.per_test_evidence(PerTestEvidenceRequest(
        suite(_listing("exit(3);")), target="src/calc.c", timeout_s=60))
    assert failed.outcome is la.Outcome.COLLECTION_FAILED
    assert "did not list its tests" in failed.reason and "exit 3" in failed.reason
    hung = c.per_test_evidence(PerTestEvidenceRequest(
        suite(_listing("sleep(120);")), target="src/calc.c", timeout_s=30, warmup=False))
    assert hung.outcome is la.Outcome.TIMED_OUT


@pytest.mark.parametrize("field, value, name", [
    ("record_leaks", True, "the leak record"),
    ("session_seed", 0, "a session seed"),
    ("session_seed", 7, "a session seed"),
    ("reverse_order", True, "the reverse order"),
    ("isolation_config", True, "the isolated configuration"),
    ("remove_created_files", True, "the removal of created files"),
])
def test_a_side_effect_the_adapter_does_not_provide_is_refused(project, adapter, suite, field,
                                                                value, name):
    root = project("calc_make")
    run = adapter(root).run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120,
                                                  **{field: value}))
    assert run.outcome is la.Outcome.UNSUPPORTED
    assert run.reason == f"the C adapter does not provide {name}"
    assert run.tests == ()


def test_the_project_build_runs_with_the_contexts_environment(project, suite):
    from langadapter.c.adapter import CAdapter

    root = project("calc_make")
    makefile = root / "Makefile"
    guard = "ifndef CONTEXT_MARK\n$(error CONTEXT_MARK is not set)\nendif\n"
    makefile.write_text(guard + makefile.read_text(encoding="utf-8"), encoding="utf-8")
    context = la.ProjectContext(root=str(root),
                                interpreter=la.Interpreter(path=shutil.which("gcc")),
                                environment={"CONTEXT_MARK": "1"})
    run = CAdapter(context).run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason


#: A test that asks for 3 GiB without touching it: free unbounded, refused
#: under a smaller address-space bound.
LARGE_ALLOCATION_TESTS = """\
#include <stdlib.h>
#include "utest.h"
#include "calc.h"

UTEST(memory, three_gib) {
    void *block = malloc((size_t)3 << 30);
    ASSERT_TRUE(block != NULL);
    free(block);
}

UTEST_MAIN()
"""


def test_an_address_space_limit_bounds_each_test_process(project, adapter, suite):
    root = project("calc_make")
    free = adapter(root).run_suite(SuiteRunRequest(suite(LARGE_ALLOCATION_TESTS), timeout_s=120))
    assert free.outcome is la.Outcome.COMPLETED, free.reason
    bounded = adapter(root, c_address_space_limit="1G").run_suite(
        SuiteRunRequest(suite(LARGE_ALLOCATION_TESTS), timeout_s=120))
    assert bounded.outcome is la.Outcome.TEST_FAILURES, bounded.reason
    assert ("address_space_limit", str(1 << 30)) in bounded.identity.toolchain.tools
    assert "address_space_limit" not in dict(free.identity.toolchain.tools)


def test_an_address_space_limit_that_is_no_size_is_refused(project, adapter, suite):
    from langadapter.c.adapter import CAdapter

    root = project("calc_make")
    assert CAdapter.setup_problem("c_address_space_limit", "lots", root, None) == (
        "The value must be a number of bytes, with K, M or G for powers of 1024, such as 2G.")
    run = adapter(root, c_address_space_limit="lots").run_suite(
        SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert run.outcome is la.Outcome.MISSING_SETUP
    assert "c_address_space_limit" in run.reason


def test_an_address_space_limit_without_prlimit_is_refused(project, suite, tmp_path):
    from langadapter.c.adapter import CAdapter

    root = project("calc_make")
    empty = tmp_path / "no_tools"
    empty.mkdir()
    context = la.ProjectContext(
        root=str(root), interpreter=la.Interpreter(path=shutil.which("gcc")),
        environment={**CAdapter.context_environment({"c_address_space_limit": "1G"}),
                     "PATH": str(empty)})
    run = CAdapter(context).run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert run.outcome is la.Outcome.MISSING_SETUP
    assert run.reason == "c_address_space_limit needs prlimit (util-linux) on PATH"


def test_without_the_extra_for_c_runs_are_missing_setup(project, adapter, suite, monkeypatch):
    import importlib.util

    found = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *rest: None if name == "gcovr" else found(name, *rest))
    reason = ('the C adapter needs LangAdapter\'s extra for C, which installs gcovr: '
              'pip install "langadapter[c]"')
    tested = adapter(project("calc_make"))
    run = tested.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert (run.outcome, run.reason) == (la.Outcome.MISSING_SETUP, reason)
    report = tested.capabilities()
    assert (report.outcome, report.reason) == (la.Outcome.MISSING_SETUP, reason)


def test_on_a_system_other_than_linux_runs_are_missing_setup(project, suite, monkeypatch):
    from langadapter.c.adapter import CAdapter

    monkeypatch.setattr(sys, "platform", "darwin")
    reason = "the C adapter runs on Linux only in this version"
    tested = CAdapter(la.ProjectContext(root=str(project("calc_make")),
                                        interpreter=la.Interpreter(path="/usr/bin/gcc")))
    run = tested.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert (run.outcome, run.reason) == (la.Outcome.MISSING_SETUP, reason)
    report = tested.capabilities()
    assert (report.outcome, report.reason) == (la.Outcome.MISSING_SETUP, reason)


def test_a_build_that_timed_out_is_made_again(project, adapter, suite, tmp_path, monkeypatch):
    import langadapter.c.adapter as c_adapter

    root = project("calc_make")
    marker = tmp_path / "slept"
    makefile = root / "Makefile"
    recipe = "\t$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@"
    makefile.write_text(makefile.read_text(encoding="utf-8").replace(
        recipe, f"\t@test -f {marker} || {{ touch {marker}; sleep 6; }}\n{recipe}"),
        encoding="utf-8")
    monkeypatch.setattr(c_adapter, "_BUILD_TIMEOUT_S", 3.0)
    c = adapter(root)
    first = c.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert first.outcome is la.Outcome.TIMED_OUT, first.reason
    # The first build slept and timed out. The same adapter builds again.
    second = c.run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert second.outcome is la.Outcome.COMPLETED, second.reason


def test_make_archives_with_the_ar_on_the_contexts_path(project, suite, tmp_path):
    """The ar the make build archives with is the one on the context's PATH."""
    from langadapter.c.adapter import CAdapter
    from test_cmake import _logging_ar

    root = project("calc_make")
    makefile = root / "Makefile"
    makefile.write_text(makefile.read_text(encoding="utf-8").replace(
        "app: src/main.o src/calc.o src/util.o\n",
        "app: src/main.o libcalc.a src/util.o\n"
    ) + "\nlibcalc.a: src/calc.o\n\t$(AR) rcs $@ $^\n", encoding="utf-8")
    log = tmp_path / "ar.log"
    tools = _logging_ar(tmp_path / "tools", log)
    context = la.ProjectContext(root=str(root), interpreter=la.Interpreter(path=shutil.which("gcc")),
                                environment={"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"})
    run = CAdapter(context).run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert "libcalc.a" in log.read_text(encoding="utf-8")


def test_the_compiler_comes_from_the_contexts_environment(project, tmp_path, monkeypatch):
    """A CC of this process that the context does not set is not the
    project's compiler: cc on the context's PATH is."""
    from langadapter.c.adapter import CAdapter

    other = tmp_path / "process-cc"
    other.write_text(f'#!/bin/sh\nexec "{shutil.which("gcc")}" "$@"\n', encoding="utf-8")
    other.chmod(0o755)
    monkeypatch.setenv("CC", str(other))
    path = os.environ["PATH"]
    choice = CAdapter.discover_runtime(str(project("calc_make")), {}, environment={"PATH": path})
    assert choice.interpreter.path == shutil.which("cc", path=path)


@pytest.mark.parametrize("version, line", [
    ("14.2.1", "gcov (GCC) 14.2.1 20240910"),
    ("11.4.1", "gcov (GCC) 11.4.1 20231218 (Red Hat 11.4.1-3)"),
    ("13.3.0", "gcov (Ubuntu 13.3.0-6ubuntu2~24.04) 13.3.0"),
])
def test_gcovs_version_is_a_word_of_its_line(tmp_path, version, line):
    """gcov's line may carry a date or a package release after its version.
    The compiler's version is found as a word of it, and another is refused."""
    import subprocess

    from langadapter.c import _toolchain

    for name in ("gcc", "gcov"):
        (tmp_path / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (tmp_path / name).chmod(0o755)
    gcc = str(tmp_path / "gcc")

    def run(compiler_version):
        answers = {"--version": f"gcc (GCC) {compiler_version}\n",
                   "-dumpfullversion": f"{compiler_version}\n"}
        return lambda argv: subprocess.CompletedProcess(argv, 0, answers.get(argv[1], "")
                                                        if argv[0] == gcc else line + "\n", "")
    found = _toolchain.probe(gcc, run(version))
    assert isinstance(found, _toolchain.Toolchain), found
    other = _toolchain.probe(gcc, run("9.9.9"))
    assert isinstance(other, _toolchain.ToolchainProblem)
    assert f"is version {version}" in other.reason


def test_a_target_compiled_to_an_os_object_is_replaced_in_its_link(tmp_path):
    """A target compiled to .os (libtool's naming) is an input of its recorded
    link like .o, so the link takes the staged object in its place."""
    from langadapter.c._build import TargetBuild
    from langadapter.c._recorder import Command

    command_dir = str(tmp_path)
    build = TargetBuild(
        kind="make", scratch_dir=tmp_path, source=tmp_path / "calc.c", written="calc.c",
        compile=Command(command_dir, "gcc", ("-c", "calc.c", "-o", "calc.os"), 0),
        link_inputs=(), link_flags=(), test_flags=(), defines_main=False,
        consumer=Command(command_dir, "gcc", ("-shared", "calc.os", "util.os", "-o", "libcalc.so"), 0))
    staged = tmp_path / "staged.o"
    command = build.artifact_command(staged, tmp_path / "out.so", coverage=False)
    assert str(staged) in command and str(tmp_path / "util.os") in command
    assert str(tmp_path / "calc.os") not in command and "calc.os" not in command


def test_a_file_included_from_the_include_path_is_found_in_the_replay(project, adapter, suite):
    """The files -include and -imacros name, found by the build on its
    include path and not in its working directory, are found by the target's
    replay and the tests' compile."""
    root = project("calc_make")
    (root / "include" / "config.h").write_text("#define CALC_CONFIG 1\n", encoding="utf-8")
    (root / "include" / "macros.h").write_text("#define CALC_MACROS 1\n", encoding="utf-8")
    makefile = root / "Makefile"
    makefile.write_text(makefile.read_text(encoding="utf-8").replace(
        "CPPFLAGS = -Iinclude\n", "CPPFLAGS = -Iinclude -include config.h -imacros macros.h\n"),
        encoding="utf-8")
    run = adapter(root).run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120,
                                                  coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)


def test_the_project_tree_stays_unbuilt(project, adapter, suite):
    root = project("calc_make")
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    run = adapter(root).run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before


def test_a_project_without_a_build_file_compiles_the_target_alone(project, adapter, suite):
    root = project("calc_flags")
    run = adapter(root, c_flags="-O1").run_suite(SuiteRunRequest(suite("""\
#include "utest.h"
#include "calc.h"

UTEST(calc, adds) {
    EXPECT_EQ(5, add(2, 3));
}

UTEST(calc, larger) {
    EXPECT_EQ(7, max(7, 2));
}

UTEST_MAIN()
"""), timeout_s=120, coverage_target="calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert set(run.coverage.covered) == {3, 4, 7, 8, 9}
    assert set(run.coverage.missing()) == {11}


#: Tests of src/util.c that include no project header, so only their file's
#: name (test_util.c) names their target.
SQUARE_TESTS = """\
#include "utest.h"

double square(double x);

UTEST(util, squares) {
    EXPECT_TRUE(square(3.0) == 9.0);
}

UTEST_MAIN()
"""


def test_the_target_comes_from_the_test_file_name(project, adapter, suite):
    root = project("calc_make")
    run = adapter(root).run_suite(SuiteRunRequest(suite(SQUARE_TESTS, "test_util.c"),
                                                  timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert [t.test.qualname for t in run.tests] == ["util.squares"]
    assert run.coverage is None


def test_an_indexed_test_reports_each_index(project, adapter, suite):
    root = project("calc_make")
    run = adapter(root).run_suite(SuiteRunRequest(suite("""\
#include "utest.h"
#include "calc.h"

struct range { long lo; };

UTEST_I_SETUP(range) {
    utest_fixture->lo = (long)utest_index;
}

UTEST_I_TEARDOWN(range) {
    (void)utest_fixture;
}

UTEST_I(range, keeps_its_lower_end, 2) {
    EXPECT_EQ(utest_fixture->lo, clamp(utest_fixture->lo, utest_fixture->lo, 10));
}

UTEST_MAIN()
"""), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert sorted((t.test.qualname, t.test.params) for t in run.tests) == [
        ("range.keeps_its_lower_end", "0"), ("range.keeps_its_lower_end", "1")]


def test_the_run_options_hold_in_a_copy_of_the_project(project, adapter, suite, tmp_path):
    root = project("calc_make")
    options = adapter(root).request_options("failing", target=str(root / "src" / "calc.c"))
    assert options["runner_options"].target == "src/calc.c"
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    run = adapter(copy).run_suite(SuiteRunRequest(suite(CLAMP_TESTS), timeout_s=120, **options))
    assert run.outcome is la.Outcome.COMPLETED, run.reason


def test_c_tests_are_timed_with_each_program_run_once(project, adapter, suite):
    root = project("calc_make")
    progress = []
    timed = adapter(root).tests.benchmark(BenchmarkRequest(
        suite(CLAMP_TESTS), runs=3, timeout_s=120, progress=progress.append))
    assert timed.outcome is la.Outcome.COMPLETED, timed.reason
    assert len(timed.times_ms) == 3 and timed.median_ms > 0
    assert [event.kind for event in progress] == ["warmup", "measured", "measured", "measured"]
    assert timed.returncode == 0 and timed.allowed_cpus
    failing = adapter(root).tests.benchmark(BenchmarkRequest(
        suite(CLAMP_TESTS.replace("ASSERT_EQ(0L", "ASSERT_EQ(1L")), runs=3, timeout_s=120))
    assert failing.outcome is la.Outcome.TEST_FAILURES
    assert "clamp.below_the_range" in failing.reason and failing.times_ms == ()
    # Each program runs once, all its tests in its one process, so a test that
    # leaves state behind fails the next one, which one process for each test
    # would not show.
    shared = adapter(root).tests.benchmark(BenchmarkRequest(
        suite(SHARED_STATE), runs=1, timeout_s=120))
    assert shared.outcome is la.Outcome.TEST_FAILURES, shared.reason
    assert "state.second" in shared.reason and shared.times_ms == ()


def logging_gcc(tool_dir):
    """A gcc in the new ``tool_dir`` that logs each command line and runs the
    real gcc, with the real gcov beside it. It returns the script and its log."""
    from langadapter.c import _toolchain

    real = shutil.which("gcc")
    tool_dir.mkdir()
    log = tool_dir / "compiles.log"
    gcc = tool_dir / "gcc"
    gcc.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec "{real}" "$@"\n', encoding="utf-8")
    gcc.chmod(0o755)
    (tool_dir / "gcov").symlink_to(_toolchain.gcov_beside(real))
    return gcc, log


def assert_built_without_counters(log):
    """The logged compiles hold the target's and none asks for coverage
    counters."""
    lines = log.read_text(encoding="utf-8").splitlines()
    assert any("calc.c" in line and " -c " in f" {line} " for line in lines), lines
    assert not [line for line in lines if "--coverage" in line.split()]


def test_a_timing_builds_the_tests_once_before_its_repetitions(project, suite, tmp_path):
    from langadapter.c.adapter import CAdapter

    gcc, log = logging_gcc(tmp_path / "tools")
    context = la.ProjectContext(root=str(project("calc_make")),
                                interpreter=la.Interpreter(path=str(gcc)))
    timed = CAdapter(context).tests.benchmark(BenchmarkRequest(
        suite(CLAMP_TESTS), runs=3, timeout_s=120))
    assert timed.outcome is la.Outcome.COMPLETED, timed.reason
    assert len(timed.times_ms) == 3
    compiles = [line for line in log.read_text(encoding="utf-8").splitlines()
                if "test_calc.c" in line and " -c " in f" {line} "]
    assert len(compiles) == 1, compiles


@pytest.mark.skipif(shutil.which("taskset") is None, reason="needs taskset")
def test_a_timing_runs_on_the_processors_asked_for(project, adapter, suite):
    cpu = min(os.sched_getaffinity(0))
    timed = adapter(project("calc_make")).tests.benchmark(BenchmarkRequest(
        suite(CLAMP_TESTS), runs=1, timeout_s=120, cpus=str(cpu)))
    assert timed.outcome is la.Outcome.COMPLETED, timed.reason
    assert (timed.cpus, timed.allowed_cpus) == (str(cpu), (cpu,))


def test_two_merged_test_files_run_all_their_tests(project, adapter, suite):
    root = project("calc_make")
    c = adapter(root)
    second = CLAMP_TESTS.replace("clamp(-5, 0, 10)", "clamp(-7, 0, 10)")
    merged = c.sources.merge_sources(CLAMP_TESTS, second, separator="// second")
    assert merged.status == "applied", merged.detail
    run = c.run_suite(SuiteRunRequest(suite(merged.source), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert sorted(t.test.qualname for t in run.tests) == [
        "clamp.below_the_range", "clamp.below_the_range_2", "clamp.inside_the_range",
        "clamp.inside_the_range_2"]


def test_two_merged_files_share_a_prototype_they_write_differently(project, adapter, suite):
    """A function both files only declare, each in its own words, is the
    one the target defines: the second file's calls keep its name."""
    root = project("calc_make")
    c = adapter(root)
    first = CLAMP_TESTS.replace('#include "calc.h"\n', "long clamp(long, long, long);\n")
    second = CLAMP_TESTS.replace('#include "calc.h"\n',
                                 "long clamp(long x, long lo, long hi);\n")
    merged = c.sources.merge_sources(first, second, separator="// second")
    assert merged.status == "applied", merged.detail
    assert merged.detail == "shared clamp"
    run = c.run_suite(SuiteRunRequest(suite(merged.source), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    assert len(run.tests) == 4


SHARED_STATE = """\
#include "utest.h"
#include "calc.h"

static int calls;

UTEST(state, first) {
    calls++;
    EXPECT_EQ(1, calls);
}

UTEST(state, second) {
    calls++;
    EXPECT_EQ(1, calls);
}

UTEST_MAIN()
"""


def _delivered(c, suite, *, timeout_s=120, sources=None):
    return c.tests.run_as_delivered(SuiteRunRequest(suite, timeout_s=timeout_s), sources=sources)


def test_a_delivered_run_runs_a_files_tests_in_one_process_in_their_order(
        project, suite, tmp_path):
    """As a user runs a copied file: its program once, by utest.h's own main,
    so a test that leaves state behind changes the next one, as a suite run
    shows it too. It is built without coverage counters."""
    from langadapter.c.adapter import CAdapter

    gcc, log = logging_gcc(tmp_path / "tools")
    c = CAdapter(la.ProjectContext(root=str(project("calc_make")),
                                   interpreter=la.Interpreter(path=str(gcc))))
    tests = suite(SHARED_STATE)
    assert c.tests.supports("run_as_delivered")
    alone = c.run_suite(SuiteRunRequest(tests, timeout_s=120))
    assert alone.outcome is la.Outcome.TEST_FAILURES, alone.reason
    assert [(t.test.qualname, t.outcome) for t in alone.tests] == [
        ("state.first", "passed"), ("state.second", "failed")]
    log.write_text("", encoding="utf-8")
    run = _delivered(c, tests)
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("state.first", "passed"), ("state.second", "failed")]
    assert "Actual : 1 vs 2" in run.tests[1].message
    assert run.coverage is None
    assert_built_without_counters(log)


CHATTY = """\
#include <stdio.h>
#include "utest.h"
#include "calc.h"

static void lines(void) {
    for (int line = 0; line < 100000; line++) {
        printf("line %d of a long output\\n", line);
    }
}

UTEST(chatty, writes_first) {
    lines();
    EXPECT_EQ(1, sign(4));
}

UTEST(chatty, fails_between) {
    EXPECT_EQ(1, sign(-4));
}

UTEST(chatty, passes_between) {
    printf("no line break");
    EXPECT_EQ(-1, sign(-4));
}

UTEST(chatty, writes_last) {
    lines();
    EXPECT_EQ(1, sign(4));
}

UTEST_MAIN()
"""


def test_tests_that_write_more_than_is_kept_leave_every_verdict(project, adapter, suite):
    """A program's output beyond twice OUTPUT_KEPT bytes keeps its start, its
    end and utest's RUN and result lines between them, also a result line
    glued to a test's output without a line break: a delivered run, one
    process for all tests, still reads each test's verdict, as a suite run
    does."""
    c = adapter(project("calc_make"))
    tests = suite(CHATTY)
    expected = [("chatty.writes_first", "passed"), ("chatty.fails_between", "failed"),
                ("chatty.passes_between", "passed"), ("chatty.writes_last", "passed")]
    for run in (c.run_suite(SuiteRunRequest(tests, timeout_s=300)),
                _delivered(c, tests, timeout_s=300)):
        assert [(t.test.qualname, t.outcome) for t in run.tests] == expected, run.reason
        # 5.6 MB written, the program's stream kept to about 2 MiB.
        assert len(run.output) < 5 * 1024 * 1024
        assert "bytes of the test output omitted ...]" in run.output


def test_a_crash_ends_the_delivered_program_in_the_test_it_ran(project, adapter, suite):
    run = _delivered(adapter(project("calc_make")), suite("""\
#include <stddef.h>
#include "utest.h"
#include "calc.h"

UTEST(sign, negative) {
    EXPECT_EQ(-1, sign(-3));
}

UTEST(crash, on_purpose) {
    volatile int *p = NULL;
    EXPECT_EQ(1, *p);
}

UTEST(sign, positive) {
    EXPECT_EQ(1, sign(4));
}

UTEST_MAIN()
"""))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason
    # The test after the crash did not run.
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("sign.negative", "passed"), ("crash.on_purpose", "error")]
    assert "SIGSEGV" in run.tests[1].message


def test_a_crash_after_a_result_line_the_test_printed_ends_it_in_that_test(
        project, adapter, suite):
    """utest's own result line is followed by the next test's RUN line or by
    its closing line. A crash after a result line the test printed itself,
    before either, ends the delivered program in that test."""
    run = _delivered(adapter(project("calc_make")), suite("""\
#include <stddef.h>
#include <stdio.h>
#include "utest.h"
#include "calc.h"

UTEST(crash, after_printing_a_result_line) {
    printf("[       OK ] crash.after_printing_a_result_line (1ns)\\n");
    volatile int *p = NULL;
    EXPECT_EQ(1, *p);
}

UTEST_MAIN()
"""))
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("crash.after_printing_a_result_line", "error")], run.reason
    assert "SIGSEGV" in run.tests[0].message


def test_a_delivered_program_past_its_timeout_names_the_test_it_ran(project, adapter, suite):
    run = _delivered(adapter(project("calc_make")), suite("""\
#include "utest.h"
#include "calc.h"

UTEST(clamp, first) {
    EXPECT_EQ(0L, clamp(-5, 0, 10));
}

UTEST(loop, forever) {
    volatile int x = 1;
    while (x) {
    }
}

UTEST(clamp, last) {
    EXPECT_EQ(5L, clamp(5, 0, 10));
}

UTEST_MAIN()
"""), timeout_s=30)
    assert run.outcome is la.Outcome.TIMED_OUT
    assert run.active_at_end is not None and run.active_at_end.qualname == "loop.forever"
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [("clamp.first", "passed")]


def test_a_delivered_run_takes_a_files_replacement_and_leaves_the_file(
        project, adapter, suite):
    c = adapter(project("calc_make"))
    tests = suite(CLAMP_TESTS)
    replaced = CLAMP_TESTS.replace("EXPECT_EQ(5L, clamp(5, 0, 10));",
                                   "EXPECT_EQ(6L, clamp(5, 0, 10));")
    run = _delivered(c, tests, sources={"test_calc.c": replaced})
    assert [(t.test.qualname, t.outcome) for t in run.tests] == [
        ("clamp.below_the_range", "passed"), ("clamp.inside_the_range", "failed")]
    assert (Path(tests.root) / "test_calc.c").read_text(encoding="utf-8") == CLAMP_TESTS


def test_a_reverse_delivered_order_is_refused_for_c_tests(project, adapter):
    c = adapter(project("calc_make"))
    assert c.delivered_order_refusal(False) is None
    assert "reverse-order run is not offered for C tests" in c.delivered_order_refusal(True)


def test_a_staged_pool_runs_each_suites_file_under_its_staged_name(
        project, adapter, tmp_path):
    from langadapter.sources import PoolMember

    c = adapter(project("calc_make"))
    second = CLAMP_TESTS.replace("EXPECT_EQ(5L, clamp(5, 0, 10));",
                                 "EXPECT_EQ(6L, clamp(5, 0, 10));")
    pool = c.stage_pool([PoolMember("s1", "test_calc.c", CLAMP_TESTS),
                         PoolMember("s2", "test_calc.c", second)])
    pool_dir = tmp_path / "pool"
    pool_dir.mkdir()
    run = c.run_suite(SuiteRunRequest(pool.write(str(pool_dir)), timeout_s=120,
                                      coverage_target="src/calc.c"))
    verdicts = {t.test.text: t.outcome for t in run.tests}
    assert verdicts == {
        "test_calc_s1.c::clamp.below_the_range": "passed",
        "test_calc_s1.c::clamp.inside_the_range": "passed",
        "test_calc_s2.c::clamp.below_the_range": "passed",
        "test_calc_s2.c::clamp.inside_the_range": "failed"}
    failed = next(t.test for t in run.tests if t.outcome == "failed")
    assert pool.original_test(failed)[0] == "s2"


CRASHING = """\
#include <stdio.h>
#include <stdlib.h>
#include "utest.h"
#include "calc.h"

UTEST(crash, first_passes) {
    EXPECT_EQ(1, sign(4));
}

UTEST(crash, then_aborts) {
    fprintf(stderr, "about to abort\\n");
    abort();
}

UTEST_MAIN()
"""


def test_a_delivered_runs_output_keeps_each_tests_lines_apart(project, adapter, suite):
    """Each test's section of a delivered run ends with a line break, so the
    run's output keeps one test's result line and the next test's RUN line
    apart, and the crashed test's stderr on lines of its own."""
    c = adapter(project("calc_make"))
    lines = _delivered(c, suite(SHARED_STATE)).output.splitlines()
    assert any(line.strip().startswith("[ RUN") and line.strip().endswith("state.second")
               for line in lines), lines
    crashed = _delivered(c, suite(CRASHING)).output.splitlines()
    assert "about to abort" in [line.strip() for line in crashed], crashed


def test_c_provides_the_generated_suite_operations(project, adapter):
    """C repairs generated suites (syntax, skips, collection breakers and
    imports), and its capability report says so."""
    report = adapter(project("calc_make")).capabilities()
    [found] = [item for item in report.capabilities
               if item.name == "generated_suite_operations"]
    assert found.availability is la.Availability.AVAILABLE, found


def test_a_timings_repetition_is_its_test_processes_own_time(project, adapter, suite,
                                                             monkeypatch):
    """A repetition's time is the sum of its test processes' lifetimes as
    their supervisor timed them, not the time around them, which holds each
    process's start, its stop and its judging."""
    c = adapter(project("calc_make"))
    run = c._run_test

    def supervised(argv, **options):
        done = run(argv, **options)
        done.native_elapsed_s = 2.5
        return done

    monkeypatch.setattr(c, "_run_test", supervised)
    timed = c.tests.benchmark(BenchmarkRequest(suite(CLAMP_TESTS), runs=2, timeout_s=120))
    assert timed.outcome is la.Outcome.COMPLETED, timed.reason
    assert timed.times_ms == (2500.0, 2500.0)


JOINED = """\
#include <stdio.h>
#include <stdlib.h>
#include "utest.h"
#include "calc.h"

UTEST(join, first_writes_without_a_line_break) {
    fprintf(stderr, "no line break");
    EXPECT_EQ(1, sign(4));
}

UTEST(join, then_aborts_mid_line) {
    printf("partial");
    fflush(stdout);
    fprintf(stderr, "about to abort\\n");
    abort();
}

UTEST(join, last_writes_without_a_line_break) {
    fprintf(stderr, "no line break");
    EXPECT_EQ(1, sign(4));
}

UTEST_MAIN()
"""


def test_a_suite_runs_output_and_traces_keep_each_tests_lines_apart(project, adapter, suite):
    """A test file's program runs whole, its stderr in its stdout as they
    came: the crashed test's trace holds its own lines of both, and no text
    an earlier test wrote to stderr without a line break."""
    run = adapter(project("calc_make")).run_suite(
        SuiteRunRequest(suite(JOINED), timeout_s=120))
    [aborted] = [t for t in run.tests if t.test.qualname == "join.then_aborts_mid_line"]
    trace = [line.strip() for line in aborted.trace.splitlines()]
    assert "partialabout to abort" in trace, aborted.trace
    assert "no line break" not in aborted.trace, aborted.trace
    assert {t.test.qualname: t.outcome for t in run.tests} == {
        "join.first_writes_without_a_line_break": "passed",
        "join.then_aborts_mid_line": "error", "join.last_writes_without_a_line_break": "passed"}


UNENDED = """\
#include <stdio.h>
#include "utest.h"
#include "calc.h"

UTEST(trace, fails_without_a_final_break) {
    fprintf(stderr, "no final break");
    ASSERT_EQ(2, sign(4));
}

UTEST_MAIN()
"""


def test_a_failed_tests_trace_holds_its_stderr_where_the_test_wrote_it(project, adapter,
                                                                      suite):
    """A failed test's stderr without a line break stays in its trace, before
    utest's result line, which ends the trace."""
    run = adapter(project("calc_make")).run_suite(
        SuiteRunRequest(suite(UNENDED), timeout_s=120))
    [failed] = run.tests
    assert failed.outcome == "failed", run.reason_text(None)
    assert 0 <= failed.trace.find("no final break") < failed.trace.find("[  FAILED  ]"), \
        failed.trace


def test_a_timing_through_a_container_says_its_processes_run_there(project, adapter, suite,
                                                                    monkeypatch):
    """A timing through a container's executor states that its processes run
    in the container, with the container runtime's execution overhead. Here
    the toolchain check ends it before the repetitions."""
    import subprocess

    class Container:
        def run(self, argv, **options):
            return subprocess.CompletedProcess(argv, 1, "", "")

    c = adapter(project("calc_make"))
    monkeypatch.setattr(c, "_executor", Container)
    timed = c.tests.benchmark(BenchmarkRequest(suite(CLAMP_TESTS), runs=1, timeout_s=60))
    assert timed.outcome is la.Outcome.MISSING_SETUP, timed.reason
    assert timed.includes.startswith("each test program's process in the container"), timed
    assert "the container runtime's execution overhead" in timed.includes


def test_a_timings_allowed_processors_are_read_strictly(project, adapter, suite, monkeypatch):
    """A timing reads its allowed processors with the strict parser Java's
    container path uses: a list that names a processor twice gives none."""
    import subprocess

    c = adapter(project("calc_make"))
    run = c._run_test

    def status(argv, **options):
        if "grep" in argv:
            return subprocess.CompletedProcess(argv, 0, "Cpus_allowed_list:\t1,1\n", "")
        return run(argv, **options)

    monkeypatch.setattr(c, "_run_test", status)
    timed = c.tests.benchmark(BenchmarkRequest(suite(CLAMP_TESTS), runs=1, timeout_s=120))
    assert timed.outcome is la.Outcome.COMPLETED, timed.reason
    assert timed.allowed_cpus is None


UNNAMED = """\
#include "utest.h"

UTEST(nothing, passes) {
    ASSERT_EQ(1, 1);
}

UTEST_MAIN()
"""


def test_a_run_without_a_target_says_every_place_to_name_it(project, adapter, suite):
    """A run whose tests name no target (no request field, no c_target, no
    test_<stem>.c after a source file, no include of a source's header)
    names every place the target is read from, c_target among them."""
    run = adapter(project("calc_make")).run_suite(
        SuiteRunRequest(suite(UNNAMED, "test_nothing.c"), timeout_s=120))
    assert run.outcome is la.Outcome.UNSUPPORTED, run.reason_text(None)
    for place in ("coverage_target", "the runner options' target", "c_target",
                  "test_<stem>.c", "include"):
        assert place in run.reason, (place, run.reason)


STDBUF_MISSING = ("a suite run or a run as delivered of C tests needs stdbuf (coreutils) on "
                  "PATH, which keeps their output line-buffered")


def _without_stdbuf(monkeypatch):
    """The context's PATH holds no stdbuf."""
    from langadapter.c import _toolchain

    which = _toolchain.which
    monkeypatch.setattr(_toolchain, "which", lambda name, *args, **options: (
        None if name == "stdbuf" else which(name, *args, **options)))


@pytest.mark.parametrize("delivered", [False, True], ids=["suite_run", "run_as_delivered"])
def test_a_run_of_c_tests_without_stdbuf_is_missing_setup_before_any_build(
        project, suite, tmp_path, monkeypatch, delivered):
    """A suite run and a run as delivered of C tests start each program
    under stdbuf. Without stdbuf on the context's PATH either is missing
    setup in the same order, before anything is compiled."""
    from langadapter.c.adapter import CAdapter

    _without_stdbuf(monkeypatch)
    gcc, log = logging_gcc(tmp_path / "tools")
    c = CAdapter(la.ProjectContext(root=str(project("calc_make")),
                                   interpreter=la.Interpreter(path=str(gcc))))
    tests = suite(CLAMP_TESTS)
    run = (_delivered(c, tests) if delivered
           else c.run_suite(SuiteRunRequest(tests, timeout_s=120)))
    assert (run.outcome, run.reason) == (la.Outcome.MISSING_SETUP, STDBUF_MISSING)
    compiles = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert not [line for line in compiles if " -c " in f" {line} "], compiles


def test_the_readiness_report_names_a_missing_stdbuf(project, adapter, monkeypatch):
    """A project of C tests without stdbuf on the context's PATH reads
    missing setup in its readiness, before any run is asked for."""
    _without_stdbuf(monkeypatch)
    c = adapter(project("calc_make"))
    assert c.execution_setup_issue() == STDBUF_MISSING
    report = c.capabilities()
    assert (report.outcome, report.reason) == (la.Outcome.MISSING_SETUP, STDBUF_MISSING)
