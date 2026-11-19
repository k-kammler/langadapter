"""A project's own test programs (c_tests programs), the baseline of
projects such as c-algorithms, through the real GCC, gcov and Mull."""
from __future__ import annotations

import pytest

import langadapter as la
from langadapter.mutation import MutationRequest
from langadapter.runs import (BenchmarkRequest, PerTestEvidenceRequest, SuiteRunRequest)
from langadapter.suites import LineItem

from langadapter.c import _mull
from conftest import needs_gcc, needs_mull

pytestmark = needs_gcc

SETTINGS = dict(c_tests="programs", c_build_kind="flags", c_flags="-Isrc -O2",
                c_link="src/*.c", c_target="src/calc.c")
BROKEN = """\
#include <assert.h>

#include "calc.h"
#include "framework.h"

static void test_sign(void) {
    assert(sign(-3) == 1);
}

static UnitTestFunction tests[] = {test_sign, 0};

int main(void) {
    run_tests(tests);
    return 0;
}
"""


def _suite(root, *files):
    return la.Suite(str(root), files or ("test/test-calc.c",), ("test/framework.c",
                                                                "test/framework.h"))


def test_each_program_is_one_test_judged_by_its_exit_status(project, adapter):
    root = project("calc_programs")
    (root / "test" / "test-broken.c").write_text(BROKEN, encoding="utf-8")
    c = adapter(root, **SETTINGS)
    run = c.run_suite(SuiteRunRequest(_suite(root, "test/test-calc.c", "test/test-broken.c"),
                                      timeout_s=120))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    verdicts = {t.test.text: t.outcome for t in run.tests}
    assert verdicts == {"test/test-calc.c::main": "passed", "test/test-broken.c::main": "failed"}
    [broken] = [t for t in run.tests if t.outcome == "failed"]
    # A failed assert ends the program by SIGABRT, with assert's line kept.
    assert "SIGABRT" in broken.message and "Assertion" in broken.message
    assert "utest.h" not in dict(run.identity.toolchain.tools)


#: Programs that end otherwise: with exit status 3, and by SIGSEGV.
ENDINGS = {"test/test-exit.c": "int main(void) {\n    return 3;\n}\n",
           "test/test-segv.c": "#include <signal.h>\n\nint main(void) {\n"
                               "    raise(SIGSEGV);\n    return 0;\n}\n"}


def test_another_exit_status_fails_and_another_signal_is_an_error(project, adapter):
    root = project("calc_programs")
    for name, text in ENDINGS.items():
        (root / name).write_text(text, encoding="utf-8")
    run = adapter(root, **SETTINGS).run_suite(SuiteRunRequest(_suite(root, *ENDINGS),
                                                              timeout_s=120))
    by = {t.test.file: t for t in run.tests}
    assert by["test/test-exit.c"].outcome == "failed"
    assert "the program ended with exit 3" in by["test/test-exit.c"].message
    assert by["test/test-segv.c"].outcome == "error"
    assert "the program ended by SIGSEGV" in by["test/test-segv.c"].message


#: A program whose stderr, the end of its output, has no line break.
UNENDED_PROGRAM = {"test/test-unended.c": "#include <stdio.h>\n\nint main(void) {\n"
                                          "    fprintf(stderr, \"no final break\");\n"
                                          "    return 1;\n}\n"}


def test_a_failed_programs_trace_ends_its_last_line(project, adapter):
    """A failed program's trace ends with a line break also when its stderr,
    the trace's end, has none."""
    root = project("calc_programs")
    for name, text in UNENDED_PROGRAM.items():
        (root / name).write_text(text, encoding="utf-8")
    run = adapter(root, **SETTINGS).run_suite(SuiteRunRequest(_suite(root, *UNENDED_PROGRAM),
                                                              timeout_s=120))
    [failed] = run.tests
    assert failed.outcome == "failed", run.reason_text(None)
    assert failed.trace.endswith("no final break\n"), failed.trace


def test_the_library_sources_c_link_names_are_built_and_linked(project, adapter):
    root = project("calc_programs")
    run = adapter(root, **SETTINGS).run_suite(SuiteRunRequest(
        _suite(root), timeout_s=120, coverage_target="src/calc.c"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    covered = set(run.coverage.covered)
    # clamp_twice calls util.c's twice, which c_link's pattern built.
    assert {5, 6, 8, 11, 15} <= covered and 19 not in covered
    missing = adapter(root, **{**SETTINGS, "c_link": "lib/*.c"}).run_suite(
        SuiteRunRequest(_suite(root), timeout_s=120))
    assert missing.outcome is la.Outcome.MISSING_SETUP
    assert "lib/*.c" in missing.reason_text(None)


def test_per_test_evidence_is_per_program(project, adapter):
    root = project("calc_programs")
    (root / "test" / "test-sign.c").write_text(
        BROKEN.replace("sign(-3) == 1", "sign(-3) == -1"), encoding="utf-8")
    evidence = adapter(root, **SETTINGS).per_test_evidence(PerTestEvidenceRequest(
        _suite(root, "test/test-calc.c", "test/test-sign.c"), target="src/calc.c",
        timeout_s=120))
    assert evidence.outcome is la.Outcome.COMPLETED, evidence.reason
    lines = {e.test.text: {i.line for i in e.items if isinstance(i, LineItem)}
             for e in evidence.tests}
    assert 19 in lines["test/test-sign.c::main"] and 5 not in lines["test/test-sign.c::main"]
    assert 5 in lines["test/test-calc.c::main"] and 19 not in lines["test/test-calc.c::main"]


@needs_mull
def test_a_mutation_run_of_the_projects_own_programs(project, adapter):
    root = project("calc_programs")
    with adapter(root, **SETTINGS).mutations.open_session(_mull.ENGINE) as session:
        run = session.run(MutationRequest(target="src/calc.c", suite=_suite(root),
                                          attribution="all"))
    assert run.outcome is la.Outcome.COMPLETED, run.reason
    by = {(m.key.operator, m.key.line): m for m in run.mutants}
    assert by[("cxx_lt_to_ge", 5)].killers == ("test/test-calc.c::main",)
    assert all(m.row["native_status"] == "not_executed" for m in run.mutants
               if m.key.line == 19)
    assert run.settings["mull_build"].value["left_out"] == []


PRELUDE = """\
#define PRELUDE_LIMIT 10
"""
#: A setup file that does not compile when the prelude reaches it: GCC reads
#: an -include header before the file's first line.
SETUP_GUARD = """\
#ifdef PRELUDE_LIMIT
#error the prelude reached a setup file
#endif
"""
PRELUDE_PROGRAM = """\
#include <assert.h>

#include "calc.h"
#include "framework.h"

static void test_limit(void) {
    assert(clamp(abs(-20), 0, PRELUDE_LIMIT) == 10);
}

static UnitTestFunction tests[] = {test_limit, 0};

int main(void) {
    run_tests(tests);
    return 0;
}
"""
PRELUDE_UTEST = """\
#include "utest.h"
#include "calc.h"

UTEST(calc, prelude_limit) {
    ASSERT_EQ(10L, clamp(abs(-20), 0, PRELUDE_LIMIT));
}

UTEST_MAIN()
"""


def test_the_projects_prelude_comes_first_in_every_test_file(project, adapter, suite):
    root = project("calc_programs")
    (root / "test" / "prelude.h").write_text(PRELUDE, encoding="utf-8")
    framework = root / "test" / "framework.c"
    framework.write_text(SETUP_GUARD + framework.read_text(), encoding="utf-8")
    (root / "test" / "test-prelude.c").write_text(PRELUDE_PROGRAM, encoding="utf-8")
    # abs needs stdlib.h and PRELUDE_LIMIT the project's header. Neither test
    # includes them, and the setup file never takes them.
    prelude = {"c_test_prelude": "stdlib.h test/prelude.h"}
    programs = _suite(root, "test/test-prelude.c")
    run = adapter(root, **SETTINGS, **prelude).run_suite(SuiteRunRequest(programs, timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    generated = {**SETTINGS, "c_tests": "c", **prelude}
    run = adapter(root, **generated).run_suite(
        SuiteRunRequest(suite(PRELUDE_UTEST), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    without = adapter(root, **{**SETTINGS, "c_tests": "c"}).run_suite(
        SuiteRunRequest(suite(PRELUDE_UTEST), timeout_s=120))
    assert without.outcome is la.Outcome.COLLECTION_FAILED
    assert "PRELUDE_LIMIT" in without.reason_text(None)


@pytest.mark.parametrize("pattern", ["src/calc.c", "src/*.c"])
def test_the_target_is_never_linked_twice(project, adapter, pattern):
    root = project("calc_programs")
    run = adapter(root, **{**SETTINGS, "c_link": f"{pattern} src/util.c"}).run_suite(
        SuiteRunRequest(_suite(root), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)


def test_the_programs_support_sources_are_their_setup_files(project, adapter):
    """c_test_support names the sources the project's own programs build
    with, and setup_files gives them, so a consumer that builds the suite from
    setup_files (Testuary's run_test_files and mutation request) runs the
    programs as the project does. Other modes keep none."""
    root = project("calc_programs")
    test_dir, program = root / "test", str(root / "test" / "test-calc.c")
    c = adapter(root, **SETTINGS, c_test_support="test/framework.c test/framework.h")
    setup = c.setup_files(str(test_dir), [program])
    assert setup == (str(test_dir / "framework.c"), str(test_dir / "framework.h"))
    run = c.run_suite(SuiteRunRequest(la.Suite(str(test_dir), (program,), setup), timeout_s=120))
    assert run.outcome is la.Outcome.COMPLETED, run.reason_text(None)
    assert adapter(root, **SETTINGS).setup_files(str(test_dir), [program]) == ()
    utest = {**SETTINGS, "c_tests": "c", "c_test_support": "test/framework.c"}
    assert adapter(root, **utest).setup_files(str(test_dir), [program]) == ()


def test_the_support_sources_are_those_of_the_tree_asked_about(project, adapter, tmp_path):
    """A consumer asks for a copy's setup files too (Testuary's mutation
    runner, for its prepared copy): they are the copy's, found in the
    nearest directory at or above the suite's root that holds them."""
    import shutil

    root = project("calc_programs")
    copy = tmp_path / "copy" / "calc_programs"
    shutil.copytree(root, copy)
    c = adapter(root, **SETTINGS, c_test_support="test/framework.c")
    assert c.setup_files(str(copy / "test"), [str(copy / "test" / "test-calc.c")]) == (
        str(copy / "test" / "framework.c"),)
    assert c.setup_files(str(root / "test"), [str(root / "test" / "test-calc.c")]) == (
        str(root / "test" / "framework.c"),)


def test_a_delivered_run_of_programs_is_their_run(project, tmp_path):
    """A project's own program is one test run whole, so it is delivered as
    a suite runs it, built without counters."""
    from langadapter.c.adapter import CAdapter
    from test_runs import assert_built_without_counters, logging_gcc

    root = project("calc_programs")
    (root / "test" / "test-broken.c").write_text(BROKEN, encoding="utf-8")
    gcc, log = logging_gcc(tmp_path / "tools")
    c = CAdapter(la.ProjectContext(root=str(root), interpreter=la.Interpreter(path=str(gcc)),
                                   environment=CAdapter.context_environment(SETTINGS)))
    run = c.tests.run_as_delivered(SuiteRunRequest(
        _suite(root, "test/test-calc.c", "test/test-broken.c"), timeout_s=120))
    assert run.outcome is la.Outcome.TEST_FAILURES, run.reason_text(None)
    assert {t.test.text: t.outcome for t in run.tests} == {
        "test/test-calc.c::main": "passed", "test/test-broken.c::main": "failed"}
    assert run.coverage is None
    assert_built_without_counters(log)


def test_a_timing_judges_each_program_as_its_suite_run_does(project, adapter):
    """A timing of the project's own programs judges each as a suite run
    does: a failed assert, which ends the program by SIGABRT, is a test
    failure, not the infrastructure's."""
    root = project("calc_programs")
    (root / "test" / "test-broken.c").write_text(BROKEN, encoding="utf-8")
    timed = adapter(root, **SETTINGS).tests.benchmark(BenchmarkRequest(
        _suite(root, "test/test-calc.c", "test/test-broken.c"), runs=1, timeout_s=120))
    assert timed.outcome is la.Outcome.TEST_FAILURES, timed.reason
    assert "the program ended by SIGABRT" in timed.reason, timed.reason
